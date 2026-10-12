"""Storage: schema, transactional sync with FTS, incremental updates."""

from __future__ import annotations

from pathlib import Path

from codekb.models import Chunk, ChunkStatus, FileKind, ScannedFile, SourceType
from codekb.storage.sqlite_store import SqliteStore, content_hash


def scanned(rel_path: str, sha: str = "abc", kind: FileKind = FileKind.PYTHON) -> ScannedFile:
    return ScannedFile(
        abs_path=Path(rel_path),
        rel_path=rel_path,
        kind=kind,
        sha256=sha,
        size_bytes=10,
        source_type=SourceType.CODE,
    )


def chunk(name: str, body: str = "x = 1", start: int = 1) -> Chunk:
    return Chunk(
        source_type=SourceType.CODE,
        content=body,
        start_line=start,
        end_line=start + body.count("\n"),
        title=name,
        symbol_name=name,
        status=ChunkStatus.VERIFIED,
        tags=("function",),
    )


def test_schema_tables_exist(store: SqliteStore) -> None:
    rows = store.connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    ).fetchall()
    names = {row["name"] for row in rows}
    assert {"repositories", "files", "chunks", "chunks_fts"} <= names


def test_upsert_repository_is_idempotent(store: SqliteStore, tmp_path: Path) -> None:
    first = store.upsert_repository(tmp_path, "demo")
    second = store.upsert_repository(tmp_path, "demo-renamed")
    assert first == second
    assert store.list_repositories()[0]["name"] == "demo-renamed"


def test_replace_file_chunks_writes_text_and_fts(store: SqliteStore, tmp_path: Path) -> None:
    repo = store.upsert_repository(tmp_path, "demo")
    result = store.replace_file_chunks(repo, scanned("a.py"), [chunk("one"), chunk("two")])
    assert result.inserted_chunks == 2
    health = store.fts_health()
    assert health["chunks"] == health["chunks_fts"] == 2


def test_replacing_a_file_removes_its_old_chunks(store: SqliteStore, tmp_path: Path) -> None:
    repo = store.upsert_repository(tmp_path, "demo")
    store.replace_file_chunks(repo, scanned("a.py"), [chunk("one"), chunk("two"), chunk("three")])
    result = store.replace_file_chunks(repo, scanned("a.py", "new"), [chunk("only")])
    assert result.deleted_chunks == 3
    assert result.inserted_chunks == 1
    assert store.chunk_count(repo) == 1


def test_replacing_a_file_does_not_leak_fts_rows(store: SqliteStore, tmp_path: Path) -> None:
    repo = store.upsert_repository(tmp_path, "demo")
    store.replace_file_chunks(repo, scanned("a.py"), [chunk("alpha")])
    store.replace_file_chunks(repo, scanned("a.py", "v2"), [chunk("beta")])
    row = store.connection.execute(
        "SELECT COUNT(*) AS n FROM chunks_fts WHERE chunk_id NOT IN (SELECT id FROM chunks)"
    ).fetchone()
    assert row["n"] == 0


def test_delete_files_cascades(store: SqliteStore, tmp_path: Path) -> None:
    repo = store.upsert_repository(tmp_path, "demo")
    store.replace_file_chunks(repo, scanned("a.py"), [chunk("one")])
    store.replace_file_chunks(repo, scanned("b.py"), [chunk("two")])
    files, chunks = store.delete_files(repo, ["a.py"])
    assert (files, chunks) == (1, 1)
    assert store.chunk_count(repo) == 1
    assert store.fts_health()["chunks"] == store.fts_health()["chunks_fts"] == 1


def test_file_states_reflect_current_hashes(store: SqliteStore, tmp_path: Path) -> None:
    repo = store.upsert_repository(tmp_path, "demo")
    store.replace_file_chunks(repo, scanned("a.py", "h1"), [chunk("one")])
    states = store.file_states(repo)
    assert states["a.py"].sha256 == "h1"


def test_hydrate_returns_authoritative_text(store: SqliteStore, tmp_path: Path) -> None:
    repo = store.upsert_repository(tmp_path, "demo")
    store.replace_file_chunks(repo, scanned("a.py"), [chunk("one", "def one():\n    return 1")])
    chunk_id = int(store.connection.execute("SELECT id FROM chunks").fetchone()["id"])
    hits = store.hydrate([chunk_id])
    hit = hits[chunk_id]
    assert hit.content == "def one():\n    return 1"
    assert hit.rel_path == "a.py"
    assert hit.tags == ("function",)
    assert hit.source_type is SourceType.CODE


def test_hydrate_ignores_unknown_ids(store: SqliteStore) -> None:
    assert store.hydrate([999999]) == {}


def test_transaction_rolls_back_on_error(store: SqliteStore, tmp_path: Path) -> None:
    repo = store.upsert_repository(tmp_path, "demo")
    store.replace_file_chunks(repo, scanned("a.py"), [chunk("one")])
    before = store.fts_health()
    try:
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO files(repo_id, path, file_type, sha256) VALUES (?,?,?,?)",
                (repo, "x.py", "python", "z"),
            )
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert store.fts_health() == before


def test_transaction_commits_on_success(store: SqliteStore, tmp_path: Path) -> None:
    repo = store.upsert_repository(tmp_path, "demo")
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO files(repo_id, path, file_type, sha256) VALUES (?,?,?,?)",
            (repo, "q.py", "python", "z"),
        )
    assert store.file_states(repo).get("q.py") is not None


def test_tags_roundtrip_as_json(store: SqliteStore, tmp_path: Path) -> None:
    repo = store.upsert_repository(tmp_path, "demo")
    tagged = Chunk(
        source_type=SourceType.KNOWLEDGE,
        content="body",
        start_line=1,
        end_line=1,
        title="t",
        tags=("重试", "backoff"),
    )
    store.replace_file_chunks(repo, scanned("k.md", kind=FileKind.MARKDOWN), [tagged])
    chunk_id = store.connection.execute("SELECT id FROM chunks").fetchone()["id"]
    hit = store.hydrate([int(chunk_id)])[int(chunk_id)]
    assert hit.tags == ("重试", "backoff")


def test_content_hash_is_stable_and_sensitive() -> None:
    a = chunk("one")
    assert content_hash(a) == content_hash(a)
    assert content_hash(a) != content_hash(chunk("two"))


def test_clear_repo_removes_everything(store: SqliteStore, tmp_path: Path) -> None:
    repo = store.upsert_repository(tmp_path, "demo")
    store.replace_file_chunks(repo, scanned("a.py"), [chunk("one")])
    files, chunks = store.clear_repo(repo)
    assert (files, chunks) == (1, 1)
    assert store.fts_health() == {"chunks": 0, "chunks_fts": 0}
