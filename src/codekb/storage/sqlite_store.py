"""SQLite persistence: schema, transactional sync, and FTS5-backed keyword search.

One rule governs this module: **``chunks`` and ``chunks_fts`` move together.**
The full-text index is a standalone FTS5 table, so a crash between the two
writes would leave rows that search can see but hydration cannot reach. Every
mutation therefore runs inside an explicit transaction.

Vectors are *not* stored here (milestone 4). Qdrant keeps vectors plus a
``chunk_id`` payload; SQLite remains the single source of truth for text, paths
and line numbers, so a vector can always be re-hydrated from a row.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from codekb.models import Chunk, ChunkStatus, ScannedFile, SearchHit, SourceType
from codekb.tokenizer import index_terms

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repositories (
    id         INTEGER PRIMARY KEY,
    root_path  TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY,
    repo_id     INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    file_type   TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL DEFAULT 0,
    indexed_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (repo_id, path)
);

CREATE INDEX IF NOT EXISTS idx_files_repo ON files(repo_id);

CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY,
    file_id      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    source_type  TEXT NOT NULL,
    title        TEXT,
    symbol_name  TEXT,
    parent_symbol TEXT,
    start_line   INTEGER NOT NULL,
    end_line     INTEGER NOT NULL,
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'verified',
    tags_json    TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
CREATE INDEX IF NOT EXISTS idx_chunks_type_status ON chunks(source_type, status);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    path,
    title,
    symbol_name,
    content,
    tags,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


@dataclass(frozen=True, slots=True)
class FileState:
    file_id: int
    sha256: str


@dataclass(frozen=True, slots=True)
class SyncResult:
    file_id: int
    inserted_chunks: int
    deleted_chunks: int


class SqliteStoreError(RuntimeError):
    """Raised when the database is in a state the caller did not expect."""


class SqliteStore:
    """Thin, explicit data-access layer. No business logic, no model calls."""

    def __init__(self, db_path: Path, *, read_only: bool = False) -> None:
        self.db_path = Path(db_path)
        if not read_only:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if not read_only:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._init_schema()

    # -- lifecycle ---------------------------------------------------------- #

    def _init_schema(self) -> None:
        # executescript() issues its own COMMIT, so it must not run inside
        # our explicit transaction wrapper.
        self._conn.executescript(SCHEMA)
        with self.transaction():
            self._conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Explicit transaction; safe to nest only via ``savepoint``-free usage."""
        if self._conn.in_transaction:
            yield self._conn
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    # -- repositories ------------------------------------------------------- #

    def upsert_repository(self, root_path: Path, name: str) -> int:
        root = str(Path(root_path).resolve())
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO repositories(root_path, name) VALUES (?, ?) "
                "ON CONFLICT(root_path) DO UPDATE SET name = excluded.name",
                (root, name),
            )
            row = conn.execute(
                "SELECT id FROM repositories WHERE root_path = ?", (root,)
            ).fetchone()
        if row is None:  # pragma: no cover - the upsert above guarantees a row
            msg = f"Failed to register repository: {root}"
            raise SqliteStoreError(msg)
        return int(row["id"])

    def list_repositories(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM repositories ORDER BY name"))

    # -- reads used by the indexer ------------------------------------------ #

    def file_states(self, repo_id: int) -> dict[str, FileState]:
        rows = self._conn.execute(
            "SELECT id, path, sha256 FROM files WHERE repo_id = ?", (repo_id,)
        )
        return {row["path"]: FileState(int(row["id"]), row["sha256"]) for row in rows}

    def chunk_count(self, repo_id: int | None = None) -> int:
        if repo_id is None:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM chunks c JOIN files f ON f.id = c.file_id "
                "WHERE f.repo_id = ?",
                (repo_id,),
            ).fetchone()
        return int(row["n"])

    # -- writes ------------------------------------------------------------- #

    def replace_file_chunks(
        self, repo_id: int, file: ScannedFile, chunks: Sequence[Chunk]
    ) -> SyncResult:
        """Upsert the file row and swap out all of its chunks + FTS entries.

        Runs as one transaction so a crash can never leave the text index and
        the full-text index disagreeing about the same file.
        """
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO files(repo_id, path, file_type, sha256, size_bytes, indexed_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(repo_id, path) DO UPDATE SET
                    file_type  = excluded.file_type,
                    sha256     = excluded.sha256,
                    size_bytes = excluded.size_bytes,
                    indexed_at = excluded.indexed_at
                """,
                (repo_id, file.rel_path, file.kind.value, file.sha256, file.size_bytes),
            )
            row = conn.execute(
                "SELECT id FROM files WHERE repo_id = ? AND path = ?",
                (repo_id, file.rel_path),
            ).fetchone()
            if row is None:  # pragma: no cover - the upsert above guarantees a row
                msg = f"Failed to resolve file row for {file.rel_path!r}"
                raise SqliteStoreError(msg)
            file_id = int(row["id"])

            deleted = self._delete_chunks_for_file(conn, file_id)
            inserted = self._insert_chunks(conn, file_id, file.rel_path, chunks)
        return SyncResult(file_id=file_id, inserted_chunks=inserted, deleted_chunks=deleted)

    def delete_files(self, repo_id: int, rel_paths: Iterable[str]) -> tuple[int, int]:
        """Remove files and everything derived from them. Returns (files, chunks)."""
        rel_paths = list(rel_paths)
        if not rel_paths:
            return (0, 0)
        removed_chunks = 0
        removed_files = 0
        with self.transaction() as conn:
            for rel_path in rel_paths:
                row = conn.execute(
                    "SELECT id FROM files WHERE repo_id = ? AND path = ?", (repo_id, rel_path)
                ).fetchone()
                if row is None:
                    continue
                removed_chunks += self._delete_chunks_for_file(conn, int(row["id"]))
                conn.execute("DELETE FROM files WHERE id = ?", (int(row["id"]),))
                removed_files += 1
        return (removed_files, removed_chunks)

    def clear_repo(self, repo_id: int) -> tuple[int, int]:
        with self.transaction() as conn:
            rows = conn.execute("SELECT id FROM files WHERE repo_id = ?", (repo_id,)).fetchall()
            removed_chunks = 0
            for row in rows:
                removed_chunks += self._delete_chunks_for_file(conn, int(row["id"]))
            conn.execute("DELETE FROM files WHERE repo_id = ?", (repo_id,))
        return (len(rows), removed_chunks)

    # -- internals ---------------------------------------------------------- #

    @staticmethod
    def _delete_chunks_for_file(conn: sqlite3.Connection, file_id: int) -> int:
        ids = [
            int(row["id"])
            for row in conn.execute("SELECT id FROM chunks WHERE file_id = ?", (file_id,))
        ]
        if not ids:
            return 0
        # FTS rows must go first: once chunks are deleted their ids are gone.
        conn.executemany("DELETE FROM chunks_fts WHERE chunk_id = ?", [(i,) for i in ids])
        conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
        return len(ids)

    def _insert_chunks(
        self, conn: sqlite3.Connection, file_id: int, rel_path: str, chunks: Sequence[Chunk]
    ) -> int:
        inserted = 0
        for chunk in chunks:
            cursor = conn.execute(
                """
                INSERT INTO chunks(
                    file_id, source_type, title, symbol_name, parent_symbol,
                    start_line, end_line, content, content_hash, status, tags_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    str(chunk.source_type),
                    chunk.title,
                    chunk.symbol_name,
                    chunk.parent_symbol,
                    chunk.start_line,
                    chunk.end_line,
                    chunk.content,
                    content_hash(chunk),
                    str(chunk.status),
                    json.dumps(list(chunk.tags), ensure_ascii=False),
                ),
            )
            chunk_id = int(cursor.lastrowid or 0)
            conn.execute(
                "INSERT INTO chunks_fts(chunk_id, path, title, symbol_name, content, tags) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chunk_id,
                    rel_path,
                    # Short, mostly-ASCII identifiers: stored verbatim, which
                    # unicode61 folds consistently with our lowercased queries.
                    chunk.title or "",
                    chunk.symbol_name or "",
                    # The body is stored as a *term stream*, not raw text, so
                    # that CJK content and CJK queries share one token space.
                    index_terms(chunk.content),
                    " ".join(chunk.tags),
                ),
            )
            inserted += 1
        return inserted

    # -- hydration ---------------------------------------------------------- #

    def hydrate(self, chunk_ids: Sequence[int]) -> dict[int, SearchHit]:
        """Load authoritative text/metadata for chunk ids, preserving id -> hit."""
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" * len(chunk_ids))
        rows = self._conn.execute(
            f"""
            SELECT c.id, c.source_type, c.title, c.symbol_name, c.start_line, c.end_line,
                   c.content, c.status, c.tags_json, f.path, f.repo_id
            FROM chunks c JOIN files f ON f.id = c.file_id
            WHERE c.id IN ({placeholders})
            """,
            tuple(chunk_ids),
        )
        hits: dict[int, SearchHit] = {}
        for row in rows:
            try:
                tags = tuple(json.loads(row["tags_json"]))
            except (json.JSONDecodeError, TypeError):
                tags = ()
            hits[int(row["id"])] = SearchHit(
                chunk_id=int(row["id"]),
                rel_path=row["path"],
                title=row["title"],
                symbol_name=row["symbol_name"],
                start_line=int(row["start_line"]),
                end_line=int(row["end_line"]),
                content=row["content"],
                source_type=SourceType(row["source_type"]),
                status=ChunkStatus(row["status"]),
                tags=tags,
                score=0.0,
                repo_id=int(row["repo_id"]),
            )
        return hits

    def recent_sources(self, repo_id: int, limit: int = 5) -> list[str]:
        rows = self._conn.execute(
            "SELECT path FROM files WHERE repo_id = ? ORDER BY indexed_at DESC, path LIMIT ?",
            (repo_id, limit),
        )
        return [row["path"] for row in rows]

    def fts_health(self) -> dict[str, int]:
        chunks = self._conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        fts = self._conn.execute("SELECT COUNT(*) AS n FROM chunks_fts").fetchone()["n"]
        return {"chunks": int(chunks), "chunks_fts": int(fts)}


def content_hash(chunk: Chunk) -> str:
    """Stable hash of a chunk's identity, used to detect content drift."""
    payload = "\x1f".join(
        [
            str(chunk.source_type),
            chunk.symbol_name or "",
            str(chunk.start_line),
            str(chunk.end_line),
            chunk.content,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
