"""Chunk sizing: oversized units split, but line numbers stay real."""

from __future__ import annotations

from itertools import pairwise

from codekb.config import ChunkingConfig
from codekb.ingestion.chunker import apply_chunk_limits, estimate_tokens, split_chunk
from codekb.models import Chunk, SourceType

TIGHT = ChunkingConfig(max_lines=10, max_chars=200, overlap_lines=2)
LOOSE = ChunkingConfig(max_lines=500, max_chars=10_000, overlap_lines=0)


def make_chunk(body: str, *, start: int = 100) -> Chunk:
    return Chunk(
        source_type=SourceType.CODE,
        content=body,
        start_line=start,
        end_line=start + body.count("\n"),
        title="demo",
        symbol_name="demo_fn",
    )


def test_small_chunk_is_untouched() -> None:
    chunk = make_chunk("a = 1\nb = 2")
    assert split_chunk(chunk, LOOSE) == [chunk]


def test_oversized_chunk_splits_into_multiple_pieces() -> None:
    body = "\n".join(f"line_{i}" for i in range(50))
    pieces = split_chunk(make_chunk(body), TIGHT)
    assert len(pieces) > 1
    assert all(p.symbol_name == "demo_fn" for p in pieces)


def test_split_preserves_real_source_line_numbers() -> None:
    """First piece starts at the parent's start; later pieces are offset by it."""
    body = "\n".join(f"line_{i}" for i in range(50))
    parent = make_chunk(body, start=100)
    pieces = split_chunk(parent, TIGHT)
    assert pieces[0].start_line == 100
    for previous, piece in pairwise(pieces):
        assert piece.start_line > previous.start_line  # strictly increasing
        assert piece.end_line > previous.end_line


def test_split_pieces_reconstruct_the_parent() -> None:
    body = "\n".join(f"line_{i}" for i in range(50))
    parent = make_chunk(body, start=1)
    pieces = split_chunk(parent, TIGHT)
    # Overlap is allowed; every piece must be a verbatim slice of the parent.
    parent_lines = parent.content.split("\n")
    for piece in pieces:
        start = piece.start_line - parent.start_line
        end = piece.end_line - parent.start_line + 1
        assert piece.content == "\n".join(parent_lines[start:end])


def test_pieces_are_labelled_with_position() -> None:
    body = "\n".join(f"line_{i}" for i in range(50))
    pieces = split_chunk(make_chunk(body), TIGHT)
    assert pieces[0].title is not None
    assert "[1/" in pieces[0].title
    assert f"/{len(pieces)}]" in pieces[-1].title


def test_parents_are_covered_from_start_to_end() -> None:
    body = "\n".join(f"line_{i}" for i in range(50))
    parent = make_chunk(body, start=7)
    pieces = split_chunk(parent, TIGHT)
    assert min(p.start_line for p in pieces) == parent.start_line
    assert max(p.end_line for p in pieces) == parent.end_line


def test_char_budget_splits_long_lines() -> None:
    body = "\n".join("x" * 90 for _ in range(20))
    pieces = split_chunk(
        make_chunk(body), ChunkingConfig(max_lines=100, max_chars=200, overlap_lines=0)
    )
    assert len(pieces) > 1
    assert all(len(p.content) < 400 for p in pieces)


def test_apply_chunk_limits_keeps_order() -> None:
    chunks = [
        make_chunk("a = 1", start=1),
        make_chunk("\n".join(f"line_{i}" for i in range(50)), start=10),
        make_chunk("z = 1", start=200),
    ]
    result = apply_chunk_limits(chunks, TIGHT)
    assert [c.start_line for c in result] == sorted(c.start_line for c in result)


def test_apply_chunk_limits_is_identity_when_loose() -> None:
    chunks = [make_chunk("a = 1", start=1)]
    assert apply_chunk_limits(chunks, LOOSE) == chunks


def test_zero_overlap_does_not_loop_forever() -> None:
    body = "\n".join(f"line_{i}" for i in range(50))
    pieces = split_chunk(
        make_chunk(body), ChunkingConfig(max_lines=8, max_chars=9999, overlap_lines=0)
    )
    assert pieces
    assert pieces[0].start_line == 100


def test_estimate_tokens_is_conservative_for_cjk() -> None:
    cjk = "模型请求超时后如何重试" * 10
    assert estimate_tokens(cjk) >= len(cjk) // 3


def test_estimate_tokens_scales_with_length() -> None:
    assert estimate_tokens("word " * 100) > estimate_tokens("word " * 10)
