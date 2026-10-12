"""Parsing: exact line ranges, symbol extraction, and the verbatim-content invariant."""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest

from codekb.ingestion.parser import (
    extract_markdown_sections,
    extract_python_symbols,
    frontmatter_status_and_tags,
    parse_file,
    parse_frontmatter,
    parse_markdown,
    parse_python,
)
from codekb.models import ChunkStatus, FileKind, ScannedFile, SourceType


def assert_verbatim(chunk, source_text: str) -> None:
    """The chunk body must be exactly the source lines it claims to cover."""
    lines = source_text.replace("\r\n", "\n").split("\n")
    expected = "\n".join(lines[chunk.start_line - 1 : chunk.end_line])
    assert chunk.content == expected, (
        f"{chunk.title!r} at {chunk.start_line}-{chunk.end_line} does not match the source"
    )


# -- the core invariant ----------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    ["src/service.py", "README.md", "config/settings.toml", "knowledge/patterns/retry.md"],
)
def test_every_chunk_matches_its_source_lines(demo_repo: Path, name: str, index_config) -> None:
    from codekb.ingestion.scanner import decode_bytes, scan

    files = {f.rel_path: f for f in scan(demo_repo, index_config).files}
    scanned = files[name]
    text = decode_bytes(scanned.abs_path.read_bytes())
    chunks = parse_file(scanned, text)
    assert chunks, f"{name} produced no chunks"
    for chunk in chunks:
        assert_verbatim(chunk, text)
        assert 1 <= chunk.start_line <= chunk.end_line <= len(text.split("\n"))


# -- Python ----------------------------------------------------------------- #


def test_extracts_top_level_functions_and_classes() -> None:
    source = (Path(__file__).parent / "fixtures" / "demo_repo" / "src" / "service.py").read_text(
        encoding="utf-8"
    )
    symbols, errors = extract_python_symbols(source)
    assert errors == []
    names = {s.name for s in symbols}
    assert {"RetryPolicy", "RetryProvider", "build_payload"} <= names
    # Methods are emitted alongside their class.
    assert "RetryProvider.send" in names
    assert "RetryPolicy.next_delay" in names


def test_decorated_function_start_includes_decorator() -> None:
    source = "@staticmethod\ndef build_payload():\n    return {}\n"
    symbols, _ = extract_python_symbols(source)
    assert symbols[0].name == "build_payload"
    assert symbols[0].start_line == 1  # the decorator, not line 2


def test_async_function_is_detected() -> None:
    symbols, errors = extract_python_symbols("async def fetch():\n    return 1\n")
    assert errors == []
    assert symbols[0].name == "fetch"


def test_method_records_parent_symbol() -> None:
    symbols, _ = extract_python_symbols("class A:\n    def m(self):\n        return 1\n")
    method = next(s for s in symbols if s.name == "A.m")
    assert method.parent == "A"
    assert method.kind == "method"


def test_nested_function_is_not_a_separate_symbol() -> None:
    symbols, _ = extract_python_symbols(
        "def outer():\n    def inner():\n        return 1\n    return inner\n"
    )
    assert [s.name for s in symbols] == ["outer"]


def test_syntax_error_yields_single_labelled_chunk() -> None:
    chunks = parse_python("def broken(:\n    pass\n")
    assert len(chunks) == 1
    assert chunks[0].title is not None
    assert "unparseable" in chunks[0].title


def test_module_header_captures_imports() -> None:
    chunks = parse_python("import os\n\n\ndef go():\n    return os\n")
    header = next(c for c in chunks if c.title == "module header")
    assert "import os" in header.content
    assert header.start_line == 1


def test_empty_file_yields_no_chunks() -> None:
    assert parse_python("") == []
    assert parse_python("\n\n\n") == []


# -- Markdown --------------------------------------------------------------- #


def test_markdown_sections_split_on_headings() -> None:
    text = "# A\n\nbody a\n\n## B\n\nbody b\n"
    sections = extract_markdown_sections(text)
    titles = [s.title for s in sections]
    assert "A" in titles and "B" in titles
    assert sections[0].start_line == 1


def test_heading_inside_fenced_code_is_ignored() -> None:
    text = "# Real\n\n```python\n# fake heading\n```\n\n## Also real\n\nbody\n"
    titles = [s.title for s in extract_markdown_sections(text)]
    assert titles == ["Real", "Also real"]
    assert "fake heading" not in titles


def test_sections_are_flat_and_do_not_overlap() -> None:
    text = "# Top\n\na\n\n## Child\n\nb\n\n# Next\n\nc\n"
    sections = extract_markdown_sections(text)
    assert [s.title for s in sections] == ["Top", "Child", "Next"]
    for earlier, later in pairwise(sections):
        assert earlier.end_line < later.start_line
    assert sections[0].start_line == 1


def test_frontmatter_is_parsed() -> None:
    data = parse_frontmatter("---\nid: x\nstatus: verified\ntags:\n  - a\n  - b\n---\n\n# Title\n")
    assert data["id"] == "x"
    assert data["tags"] == ["a", "b"]


def test_frontmatter_status_and_tags() -> None:
    status, tags = frontmatter_status_and_tags(
        "---\nstatus: verified\ntags: retry, backoff\n---\n\n# T\n"
    )
    assert status is ChunkStatus.VERIFIED
    assert tags == ("retry", "backoff")


def test_frontmatter_status_override_applies_to_chunks() -> None:
    text = "---\nstatus: deprecated\ntags:\n  - old\n---\n\n# Old entry\n\nbody\n"
    chunks = parse_markdown(
        text, frontmatter_status=ChunkStatus.DEPRECATED, frontmatter_tags=("old",)
    )
    assert chunks
    assert all(c.status is ChunkStatus.DEPRECATED for c in chunks)
    assert all("old" in c.tags for c in chunks)


def test_markdown_without_frontmatter_is_unaffected() -> None:
    assert parse_frontmatter("# just markdown\n") == {}


# -- TOML / structured text ------------------------------------------------- #


def test_toml_splits_on_table_headers() -> None:
    text = "root = 1\n\n[a]\nx = 2\n\n[b]\ny = 3\n"
    scanned = ScannedFile(
        abs_path=Path("x.toml"), rel_path="x.toml", kind=FileKind.TOML, sha256="", size_bytes=0
    )
    chunks = parse_file(scanned, text)
    titles = [c.title for c in chunks]
    assert "a" in titles and "b" in titles


def test_yaml_splits_on_top_level_keys() -> None:
    text = "first: 1\nsecond:\n  nested: 2\nthird: 3\n"
    scanned = ScannedFile(
        abs_path=Path("x.yaml"), rel_path="x.yaml", kind=FileKind.YAML, sha256="", size_bytes=0
    )
    chunks = parse_file(scanned, text)
    assert {c.title for c in chunks} >= {"first", "second", "third"}


def test_text_splits_on_blank_lines() -> None:
    text = "alpha\nbeta\n\ngamma\ndelta\n"
    scanned = ScannedFile(
        abs_path=Path("x.txt"), rel_path="x.txt", kind=FileKind.TEXT, sha256="", size_bytes=0
    )
    chunks = parse_file(scanned, text)
    assert [c.content for c in chunks] == ["alpha\nbeta", "gamma\ndelta"]


# -- source type dispatch --------------------------------------------------- #


def test_knowledge_markdown_gets_knowledge_source_type(demo_repo: Path, index_config) -> None:
    from codekb.ingestion.scanner import decode_bytes, scan

    scanned = {f.rel_path: f for f in scan(demo_repo, index_config).files}[
        "knowledge/patterns/retry.md"
    ]
    text = decode_bytes(scanned.abs_path.read_bytes())
    chunks = parse_file(scanned, text)
    assert all(c.source_type is SourceType.KNOWLEDGE for c in chunks)
    assert any(c.status is ChunkStatus.VERIFIED for c in chunks)  # frontmatter wins
