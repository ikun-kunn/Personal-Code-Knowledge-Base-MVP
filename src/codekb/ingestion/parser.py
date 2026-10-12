"""Turn a file's text into :class:`~codekb.models.Chunk` objects with exact lines.

Every parser upholds one invariant:

    chunk.content == "\\n".join(lines[chunk.start_line - 1 : chunk.end_line])

so a citation like ``src/foo.py:20-46`` can be verified by opening the file.
The chunker may split a unit further, but never reorders or rewrites lines.

Nothing here imports or executes the code being parsed.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from codekb.models import Chunk, ChunkStatus, FileKind, ScannedFile, SourceType

# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

#: ATX heading: up to 3 spaces of indent, 1-6 hashes, then the title.
_ATX_RE = re.compile(r"^[ ]{0,3}(?P<hashes>#{1,6})[ \t]+(?P<text>.*?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^[ ]{0,3}(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>.*)$")
_TOML_TABLE_RE = re.compile(r"^[ \t]*\[\[?(?P<table>[^\[\]]+)\]\]?[ \t]*(#.*)?$")
#: A top-level YAML key, with or without a value. Indented keys never match.
_YAML_KEY_RE = re.compile(r"^(?P<key>[A-Za-z_][\w.\-]*)[ \t]*:")
_YAML_PAIR_RE = re.compile(r"^(?P<key>[A-Za-z_][\w.\-]*)[ \t]*:[ \t]*(?P<rest>.*)$")


def split_lines(text: str) -> list[str]:
    """Normalise newlines and split; handles files with or without a trailing newline."""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def slice_lines(lines: list[str], start: int, end: int) -> str:
    """Verbatim text for a 1-based, inclusive line range."""
    return "\n".join(lines[start - 1 : end])


def is_meaningful(lines: list[str], start: int, end: int) -> bool:
    return any(line.strip() for line in lines[start - 1 : end])


def trim_trailing_blank(lines: list[str], start: int, end: int) -> int:
    while end > start and not lines[end - 1].strip():
        end -= 1
    return end


# --------------------------------------------------------------------------- #
# Python
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PythonSymbol:
    """A top-level class or function, or one of its methods."""

    name: str
    kind: str  # "class" | "function" | "method"
    start_line: int
    end_line: int
    parent: str | None = None


_DefNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def definition_start(node: _DefNode) -> int:
    """Line of the first decorator, or the ``def``/``class`` keyword if undecorated."""
    return min([node.lineno, *(d.lineno for d in node.decorator_list)])


def extract_python_symbols(source: str) -> tuple[list[PythonSymbol], list[str]]:
    """Top-level classes/functions plus their methods, and any syntax errors.

    Nested functions are deliberately *not* emitted separately: they belong to
    their enclosing top-level unit, so chunk count tracks a module's public
    surface rather than its indentation depth.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [], [f"{exc.msg} (line {exc.lineno})"]

    symbols: list[PythonSymbol] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            symbols.append(
                PythonSymbol(
                    name=node.name,
                    kind="class",
                    start_line=definition_start(node),
                    end_line=node.end_lineno or node.lineno,
                )
            )
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(
                        PythonSymbol(
                            name=f"{node.name}.{child.name}",
                            kind="method",
                            start_line=definition_start(child),
                            end_line=child.end_lineno or child.lineno,
                            parent=node.name,
                        )
                    )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(
                PythonSymbol(
                    name=node.name,
                    kind="function",
                    start_line=definition_start(node),
                    end_line=node.end_lineno or node.lineno,
                )
            )

    symbols.sort(key=lambda s: (s.start_line, s.end_line))
    return symbols, []


def parse_python(
    text: str,
    *,
    source_type: SourceType = SourceType.CODE,
    status: ChunkStatus = ChunkStatus.VERIFIED,
) -> list[Chunk]:
    """One chunk per top-level definition and method, plus header/footer preambles.

    A file that fails to parse still yields one whole-file chunk so it remains
    searchable (and visibly labelled) rather than disappearing from the index.
    """
    lines = split_lines(text)
    symbols, errors = extract_python_symbols(text)

    if errors:
        end = trim_trailing_blank(lines, 1, len(lines))
        if not is_meaningful(lines, 1, end):
            return []
        return [
            Chunk(
                source_type=source_type,
                content=slice_lines(lines, 1, end),
                start_line=1,
                end_line=end,
                title=f"<unparseable: {errors[0]}>",
                status=status,
            )
        ]

    chunks: list[Chunk] = []
    cursor = 1
    for symbol in symbols:
        if cursor < symbol.start_line:
            head_end = trim_trailing_blank(lines, cursor, symbol.start_line - 1)
            if is_meaningful(lines, cursor, head_end):
                chunks.append(
                    Chunk(
                        source_type=source_type,
                        content=slice_lines(lines, cursor, head_end),
                        start_line=cursor,
                        end_line=head_end,
                        title="module header",
                        status=status,
                    )
                )
        end = trim_trailing_blank(lines, symbol.start_line, symbol.end_line)
        if is_meaningful(lines, symbol.start_line, end):
            chunks.append(
                Chunk(
                    source_type=source_type,
                    content=slice_lines(lines, symbol.start_line, end),
                    start_line=symbol.start_line,
                    end_line=end,
                    title=symbol.name,
                    symbol_name=symbol.name,
                    parent_symbol=symbol.parent,
                    status=status,
                    tags=(symbol.kind,),
                )
            )
        cursor = max(cursor, symbol.end_line + 1)

    tail_end = trim_trailing_blank(lines, cursor, len(lines))
    if is_meaningful(lines, cursor, tail_end):
        chunks.append(
            Chunk(
                source_type=source_type,
                content=slice_lines(lines, cursor, tail_end),
                start_line=cursor,
                end_line=tail_end,
                title="module footer",
                status=status,
            )
        )

    chunks.sort(key=lambda chunk: chunk.start_line)
    return chunks


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MarkdownSection:
    title: str
    level: int
    start_line: int
    end_line: int


def extract_markdown_sections(text: str) -> list[MarkdownSection]:
    """Split on ATX headings, ignoring ``#`` inside fenced code blocks.

    Sections are **flat and non-overlapping**: each runs from its own heading
    line to the line before the next heading of *any* level. Nesting would mean
    indexing parent text twice, which double-counts in BM25 and makes the
    budget in the context builder harder to reason about.
    """
    lines = split_lines(text)
    headings: list[tuple[int, int, str]] = []
    open_fence: str | None = None

    for line_no, line in enumerate(lines, start=1):
        fence_match = _FENCE_RE.match(line)
        if open_fence is None:
            if fence_match:
                open_fence = fence_match.group("fence")[:3]
                continue
        else:
            if fence_match and fence_match.group("fence").startswith(open_fence):
                open_fence = None
            continue
        heading_match = _ATX_RE.match(line)
        if heading_match:
            headings.append(
                (line_no, len(heading_match.group("hashes")), heading_match.group("text"))
            )

    if not headings:
        end = trim_trailing_blank(lines, 1, len(lines))
        if not is_meaningful(lines, 1, end):
            return []
        return [MarkdownSection(title="", level=0, start_line=1, end_line=end)]

    sections: list[MarkdownSection] = []
    first_line = headings[0][0]
    if first_line > 1:
        preamble_end = trim_trailing_blank(lines, 1, first_line - 1)
        if is_meaningful(lines, 1, preamble_end):
            sections.append(
                MarkdownSection(title="(preamble)", level=0, start_line=1, end_line=preamble_end)
            )

    for position, (start, level, title) in enumerate(headings):
        end = headings[position + 1][0] - 1 if position + 1 < len(headings) else len(lines)
        end = trim_trailing_blank(lines, start, end)
        if end >= start:
            sections.append(
                MarkdownSection(title=title, level=level, start_line=start, end_line=end)
            )
    return sections


def frontmatter_end_line(lines: list[str]) -> int:
    """1-based line number of the closing ``---``, or 0 when absent."""
    if not lines or lines[0].strip() != "---":
        return 0
    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            return index + 1
    return 0


def parse_frontmatter(text: str) -> dict[str, object]:
    """Parse the small YAML subset used by knowledge entries.

    Handles ``key: value`` and block sequences (``- item``) - just enough to get
    ``status``, ``tags`` and ``scope`` into the index. Richer YAML belongs to a
    real parser; this intentionally does not try to be one.
    """
    lines = split_lines(text)
    end = frontmatter_end_line(lines)
    if end == 0:
        return {}

    data: dict[str, object] = {}
    current_key: str | None = None
    for raw in lines[1 : end - 1]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        stripped = raw.strip()
        if stripped.startswith("- ") and current_key:
            bucket = data.setdefault(current_key, [])
            if isinstance(bucket, list):
                bucket.append(stripped[2:].strip().strip("\"'"))
            continue
        pair = _YAML_PAIR_RE.match(raw)
        if pair is None:
            continue
        key, rest = pair.group("key"), pair.group("rest").strip()
        if rest:
            data[key] = rest.strip("\"'")
            current_key = None
        else:
            data[key] = []
            current_key = key
    return data


def frontmatter_status_and_tags(text: str) -> tuple[ChunkStatus | None, tuple[str, ...]]:
    data = parse_frontmatter(text)
    raw_status = data.get("status")
    status: ChunkStatus | None = None
    if raw_status:
        try:
            status = ChunkStatus(str(raw_status))
        except ValueError:
            status = None

    raw_tags = data.get("tags")
    tags: tuple[str, ...] = ()
    if isinstance(raw_tags, list):
        tags = tuple(str(tag).strip() for tag in raw_tags if str(tag).strip())
    elif isinstance(raw_tags, str) and raw_tags.strip():
        tags = tuple(part.strip() for part in raw_tags.split(",") if part.strip())
    return status, tags


def parse_markdown(
    text: str,
    *,
    source_type: SourceType = SourceType.DOC,
    status: ChunkStatus = ChunkStatus.VERIFIED,
    frontmatter_status: ChunkStatus | None = None,
    frontmatter_tags: tuple[str, ...] = (),
) -> list[Chunk]:
    """One chunk per heading section.

    Frontmatter lines are excluded from chunk bodies so their syntax does not
    pollute search results, while ``start_line`` stays accurate to the real file.
    """
    lines = split_lines(text)
    fm_end = frontmatter_end_line(lines)
    effective_status = frontmatter_status or status
    tags = tuple(dict.fromkeys(frontmatter_tags))

    chunks: list[Chunk] = []
    for section in extract_markdown_sections(text):
        start = max(section.start_line, fm_end + 1)
        end = section.end_line
        if not is_meaningful(lines, start, end):
            continue
        title = section.title
        if title == "(preamble)" and fm_end:
            title = "(frontmatter preamble)"
        chunks.append(
            Chunk(
                source_type=source_type,
                content=slice_lines(lines, start, end),
                start_line=start,
                end_line=end,
                title=title or None,
                status=effective_status,
                tags=tags,
            )
        )
    return chunks


# --------------------------------------------------------------------------- #
# Config files and plain text
# --------------------------------------------------------------------------- #


def blank_line_blocks(lines: list[str], start: int, end: int) -> list[tuple[int, int]]:
    """Group non-blank runs into ``(start, end)`` blocks of original line numbers."""
    blocks: list[tuple[int, int]] = []
    cursor: int | None = None
    for line_no in range(start, end + 1):
        if lines[line_no - 1].strip():
            if cursor is None:
                cursor = line_no
        elif cursor is not None:
            blocks.append((cursor, line_no - 1))
            cursor = None
    if cursor is not None:
        blocks.append((cursor, end))
    return blocks


def sectioned_blocks(
    lines: list[str], header_re: re.Pattern[str]
) -> list[tuple[str | None, int, int]]:
    """Split at header lines, yielding ``(title, start, end)`` in file order."""
    blocks: list[tuple[str | None, int, int]] = []
    title: str | None = None
    start = 1

    for line_no, line in enumerate(lines, start=1):
        match = header_re.match(line)
        if match is None:
            continue
        if line_no > start:
            blocks.append((title, start, line_no - 1))
        groups = match.groupdict()
        title = groups.get("key") or (groups.get("table") or "").strip() or None
        start = line_no
    blocks.append((title, start, len(lines)))
    return blocks


def parse_structured_text(
    lines: list[str],
    kind: FileKind,
    *,
    source_type: SourceType,
    status: ChunkStatus,
) -> list[Chunk]:
    """Section-aware chunks for TOML/YAML, blank-line blocks for JSON/text."""
    if kind is FileKind.TOML:
        raw = sectioned_blocks(lines, _TOML_TABLE_RE)
    elif kind is FileKind.YAML:
        raw = sectioned_blocks(lines, _YAML_KEY_RE)
    else:
        raw = [(None, start, end) for start, end in blank_line_blocks(lines, 1, len(lines))]

    chunks: list[Chunk] = []
    for title, start, end in raw:
        end = trim_trailing_blank(lines, start, end)
        if not is_meaningful(lines, start, end):
            continue
        chunks.append(
            Chunk(
                source_type=source_type,
                content=slice_lines(lines, start, end),
                start_line=start,
                end_line=end,
                title=title,
                status=status,
            )
        )
    return chunks


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


def parse_file(file: ScannedFile, text: str) -> list[Chunk]:
    """Parse one scanned file's text into chunks according to its detected kind."""
    if file.kind is FileKind.PYTHON:
        return parse_python(text, source_type=file.source_type, status=file.status)

    if file.kind is FileKind.MARKDOWN:
        fm_status, fm_tags = frontmatter_status_and_tags(text)
        return parse_markdown(
            text,
            source_type=file.source_type,
            status=file.status,
            frontmatter_status=fm_status,
            frontmatter_tags=fm_tags,
        )

    return parse_structured_text(
        split_lines(text), file.kind, source_type=file.source_type, status=file.status
    )
