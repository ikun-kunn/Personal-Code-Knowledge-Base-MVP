"""Index orchestration: scan -> parse -> chunk -> persist, incrementally.

Ordering is chosen so that a crash is always recoverable by re-running:

1. Parse and chunk in memory (pure, no side effects).
2. Write file row + chunks + FTS rows in one SQLite transaction.

A partially written file simply keeps its previous state; the next run sees a
hash mismatch and rewrites it. Vector writes (milestone 4) will be appended
*after* the SQLite commit and reconciled on the next run, because SQLite and
Qdrant cannot share a transaction.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from codekb.config import AppConfig
from codekb.ingestion.chunker import apply_chunk_limits
from codekb.ingestion.parser import parse_file
from codekb.ingestion.scanner import decode_bytes, scan
from codekb.models import Chunk, IndexStats, ScannedFile
from codekb.storage.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)


def index_path(
    config: AppConfig,
    target: Path,
    *,
    repo_name: str | None = None,
    force: bool = False,
    respect_gitignore: bool | None = None,
) -> IndexStats:
    """Bring the index for ``target`` up to date and return run statistics."""
    target = target.resolve()
    index_config = config.index
    if respect_gitignore is not None:
        index_config = replace(index_config, respect_gitignore=respect_gitignore)

    scan_result = scan(target, index_config)
    stats = IndexStats(
        repo_root=str(target),
        scanned=len(scan_result.files),
        ignored=scan_result.ignored,
    )

    with SqliteStore(config.db_path) as store:
        repo_id = store.upsert_repository(target, repo_name or target.name)
        known = store.file_states(repo_id)

        # Files that vanished since the last run.
        seen = {file.rel_path for file in scan_result.files}
        stale = sorted(set(known) - seen)
        if stale:
            removed_files, removed_chunks = store.delete_files(repo_id, stale)
            stats.removed = removed_files
            stats.chunks_deleted = removed_chunks

        errors: list[tuple[str, str]] = []
        for scanned in scan_result.files:
            previous = known.get(scanned.rel_path)
            if previous is not None and previous.sha256 == scanned.sha256 and not force:
                stats.skipped_unchanged += 1
                continue

            if previous is None:
                stats.added += 1
            stats.changed += 1

            try:
                chunks = build_chunks(config, scanned)
            except Exception as exc:
                logger.debug("Failed to parse %s", scanned.rel_path, exc_info=True)
                errors.append((scanned.rel_path, f"{type(exc).__name__}: {exc}"))
                continue

            try:
                result = store.replace_file_chunks(repo_id, scanned, chunks)
            except Exception as exc:
                logger.debug("Failed to store %s", scanned.rel_path, exc_info=True)
                errors.append((scanned.rel_path, f"{type(exc).__name__}: {exc}"))
                continue
            stats.chunks_written += result.inserted_chunks

        stats = replace(stats, errors=tuple(errors))
    return stats


def build_chunks(config: AppConfig, scanned: ScannedFile) -> list[Chunk]:
    """Decode, parse, and size-bound one file's chunks.

    Pure apart from reading the file: no writes, no execution of indexed code.
    """
    text = decode_bytes(scanned.abs_path.read_bytes())
    return apply_chunk_limits(parse_file(scanned, text), config.chunking)
