"""Keyword retrieval over SQLite FTS5.

Ranking, filtering and result shaping. The tokenisation that makes queries
comparable to indexed text lives in :mod:`codekb.tokenizer`, shared with the
write path - see that module for why CJK needs pre-segmentation.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from codekb.models import ChunkStatus, SearchHit, SourceType
from codekb.storage.sqlite_store import SqliteStore
from codekb.tokenizer import build_match_query, extract_tokens, index_terms

__all__ = [
    "KeywordSearchOptions",
    "build_match_query",
    "extract_tokens",
    "format_hits",
    "index_terms",
    "search_keyword",
    "search_many",
]


@dataclass(frozen=True, slots=True)
class KeywordSearchOptions:
    repo_id: int | None = None
    source_types: frozenset[SourceType] | None = None
    statuses: frozenset[ChunkStatus] | None = None
    limit: int = 20
    #: Over-fetch multiplier. Filters are applied in Python *after* the MATCH,
    #: because ``chunk_id`` is an UNINDEXED FTS column and cannot be filtered
    #: without a full scan of the match set.
    overfetch: int = 5


def _matches_filters(hit: SearchHit, options: KeywordSearchOptions) -> bool:
    if options.source_types is not None and hit.source_type not in options.source_types:
        return False
    return not (options.statuses is not None and hit.status not in options.statuses)


def search_keyword(
    store: SqliteStore,
    text: str,
    options: KeywordSearchOptions | None = None,
) -> list[SearchHit]:
    """Rank chunks by BM25 against the FTS5 index.

    Returns hits sorted by descending relevance. User text is untrusted input, so
    a malformed MATCH clause degrades to empty results instead of raising.
    """
    options = options or KeywordSearchOptions()
    match = build_match_query(text)
    if match is None:
        return []

    fetch = max(options.limit * options.overfetch, options.limit)
    try:
        rows = store.connection.execute(
            """
            SELECT chunk_id AS id, bm25(chunks_fts) AS bm25_rank
            FROM chunks_fts
            WHERE chunks_fts MATCH ?
            ORDER BY bm25_rank
            LIMIT ?
            """,
            (match, fetch),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    # bm25() returns a negative score; more negative means more relevant.
    ranked: list[tuple[float, int]] = [(-float(row["bm25_rank"]), int(row["id"])) for row in rows]
    if not ranked:
        return []

    hydrated = store.hydrate([chunk_id for _, chunk_id in ranked])
    results: list[SearchHit] = []
    for score, chunk_id in ranked:
        hit = hydrated.get(chunk_id)
        if hit is None or not _matches_filters(hit, options):
            continue
        results.append(
            SearchHit(
                chunk_id=hit.chunk_id,
                rel_path=hit.rel_path,
                title=hit.title,
                symbol_name=hit.symbol_name,
                start_line=hit.start_line,
                end_line=hit.end_line,
                content=hit.content,
                source_type=hit.source_type,
                status=hit.status,
                tags=hit.tags,
                score=score,
                origins={"keyword": len(results) + 1},
            )
        )
        if len(results) >= options.limit:
            break
    return results


def search_many(
    store: SqliteStore,
    queries: Iterable[str],
    options: KeywordSearchOptions | None = None,
) -> list[SearchHit]:
    """Merge several keyword queries, keeping each hit's best rank."""
    merged: dict[int, SearchHit] = {}
    for query in queries:
        for hit in search_keyword(store, query, options):
            existing = merged.get(hit.chunk_id)
            if existing is None or hit.score > existing.score:
                merged[hit.chunk_id] = hit
    return sorted(merged.values(), key=lambda hit: (-hit.score, hit.rel_path, hit.start_line))


def format_hits(hits: Sequence[SearchHit], *, show_content: bool = False) -> str:
    """Render hits for display; the CLI owns its own formatting."""
    if not hits:
        return "No matches."
    lines: list[str] = []
    for position, hit in enumerate(hits, start=1):
        badges = [
            badge
            for badge in (
                str(hit.source_type) if hit.source_type is not SourceType.CODE else "",
                str(hit.status) if hit.status is not ChunkStatus.VERIFIED else "",
            )
            if badge
        ]
        suffix = f"  ({', '.join(badges)})" if badges else ""
        lines.append(f"[{position}] {hit.location}{suffix}")
        lines.append(f"    {hit.label}")
        if show_content:
            for line in hit.content.splitlines()[:8]:
                lines.append(f"    | {line}")
    return "\n".join(lines)
