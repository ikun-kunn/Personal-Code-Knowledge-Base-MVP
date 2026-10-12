"""Split oversized parser output into retrievable pieces.

The parser produces *semantic* units (a function, a doc section). Retrieval needs
*model-sized* units. This module bridges the two without ever losing the
invariant that ``content`` maps back to real line numbers - including inside
split pieces, where the parent symbol and the piece's real offset are preserved.
"""

from __future__ import annotations

from codekb.config import ChunkingConfig
from codekb.models import Chunk


def _split_bounds(lines: list[str], config: ChunkingConfig) -> list[tuple[int, int]]:
    """Partition ``lines`` (1-based) into windows under both size limits.

    The split point is nudged towards the nearest blank line inside the window so
    pieces rarely cut through the middle of an indented block. Windows overlap by
    ``overlap_lines`` so a symbol split across a boundary stays partly retrievable
    from both sides.
    """
    total = len(lines)
    if total == 0:
        return []

    windows: list[tuple[int, int]] = []
    start = 1
    while start <= total:
        line_budget_end = min(total, start + config.max_lines - 1)

        # Apply the character budget, never returning an empty window.
        end = line_budget_end
        size = 0
        for line_no in range(start, line_budget_end + 1):
            size += len(lines[line_no - 1]) + 1
            if size > config.max_chars:
                end = line_no if line_no > start else start
                break

        if end == line_budget_end and end < total:
            # Prefer landing on a blank line in the second half of the window.
            for candidate in range(end, start + (end - start) // 2, -1):
                if not lines[candidate - 1].strip():
                    end = candidate
                    break

        windows.append((start, end))
        if end >= total:
            break
        # Overlap backwards from the end of this window, but always make progress.
        start = max(end + 1 - config.overlap_lines, start + 1)
    return windows


def split_chunk(chunk: Chunk, config: ChunkingConfig) -> list[Chunk]:
    """Return ``chunk`` unchanged, or its line-accurate pieces if oversized."""
    lines = chunk.content.split("\n")
    if len(lines) <= config.max_lines and len(chunk.content) <= config.max_chars:
        return [chunk]

    windows = _split_bounds(lines, config)
    if len(windows) <= 1:
        return [chunk]

    total = len(windows)
    base_label = chunk.symbol_name or chunk.title or ""
    pieces: list[Chunk] = []
    for position, (start, end) in enumerate(windows, start=1):
        body = "\n".join(lines[start - 1 : end])
        if not body.strip():
            continue
        pieces.append(
            Chunk(
                source_type=chunk.source_type,
                content=body,
                # Real line numbers in the *source file*, not offsets within the chunk.
                start_line=chunk.start_line + start - 1,
                end_line=chunk.start_line + end - 1,
                title=f"{base_label} [{position}/{total}]",
                symbol_name=chunk.symbol_name,
                parent_symbol=chunk.parent_symbol,
                status=chunk.status,
                tags=chunk.tags,
            )
        )
    return pieces or [chunk]


def apply_chunk_limits(chunks: list[Chunk], config: ChunkingConfig) -> list[Chunk]:
    """Split every oversized chunk, preserving overall order and coverage."""
    result: list[Chunk] = []
    for chunk in chunks:
        result.extend(split_chunk(chunk, config))
    result.sort(key=lambda item: (item.start_line, item.end_line))
    return result


def estimate_tokens(text: str) -> int:
    """A deliberately conservative token estimate used for the context budget.

    Code and CJK text tokenize far denser than English prose. Counting
    ``max(word_count, chars / 3)`` errs on the safe side, and being over budget
    costs recall while being under budget costs a truncated request.
    """
    words = len(text.split())
    return max(words, (len(text) + 2) // 3)
