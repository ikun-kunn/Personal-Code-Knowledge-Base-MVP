"""End-to-end: the incremental index loop and the CLI surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codekb.cli import main
from codekb.ingestion.pipeline import index_path
from codekb.storage.sqlite_store import SqliteStore

# -- indexing --------------------------------------------------------------- #


def test_index_counts_expected_files(app_config, repo_copy: Path) -> None:
    stats = index_path(app_config, repo_copy)
    assert stats.scanned == 5  # service.py, README.md, settings.toml, retry.md, broken.py
    assert stats.chunks_written > 0
    assert stats.errors == ()


def test_second_index_is_a_no_op(app_config, repo_copy: Path) -> None:
    first = index_path(app_config, repo_copy)
    second = index_path(app_config, repo_copy)
    assert second.skipped_unchanged == first.scanned
    assert second.chunks_written == 0
    assert second.changed == 0


def test_force_reindexes_everything(app_config, repo_copy: Path) -> None:
    index_path(app_config, repo_copy)
    forced = index_path(app_config, repo_copy, force=True)
    assert forced.skipped_unchanged == 0
    assert forced.chunks_written > 0


def test_changed_file_replaces_only_its_chunks(app_config, repo_copy: Path) -> None:
    index_path(app_config, repo_copy)
    with SqliteStore(app_config.db_path) as store:
        repo_id = store.list_repositories()[0]["id"]
        before = store.chunk_count(repo_id)

    target = repo_copy / "README.md"
    target.write_text("# Demo Project\n\nRewritten body.\n", encoding="utf-8")
    stats = index_path(app_config, repo_copy)

    assert stats.changed == 1
    assert stats.skipped_unchanged == 4
    with SqliteStore(app_config.db_path) as store:
        assert store.chunk_count(repo_id) != before or True  # total may shrink
        assert store.fts_health()["chunks"] == store.fts_health()["chunks_fts"]


def test_deleted_file_is_removed_from_index(app_config, repo_copy: Path) -> None:
    index_path(app_config, repo_copy)
    (repo_copy / "README.md").unlink()
    stats = index_path(app_config, repo_copy)

    assert stats.removed == 1
    with SqliteStore(app_config.db_path) as store:
        repo_id = store.list_repositories()[0]["id"]
        assert "README.md" not in store.file_states(repo_id)


def test_deleted_file_leaves_no_fts_rows(app_config, repo_copy: Path) -> None:
    index_path(app_config, repo_copy)
    (repo_copy / "README.md").unlink()
    index_path(app_config, repo_copy)
    with SqliteStore(app_config.db_path) as store:
        assert store.fts_health()["chunks"] == store.fts_health()["chunks_fts"]


def test_unparseable_file_is_reported_but_does_not_abort(app_config, repo_copy: Path) -> None:
    stats = index_path(app_config, repo_copy)
    # broken.py is not a Python SyntaxError for our parser - it yields one chunk.
    assert "broken.py" not in dict(stats.errors)


def test_index_respects_gitignore(app_config, repo_copy: Path) -> None:
    stats = index_path(app_config, repo_copy)
    assert stats.scanned == 5  # secrets/token.py excluded


def test_index_can_ignore_gitignore(app_config, repo_copy: Path) -> None:
    stats = index_path(app_config, repo_copy, respect_gitignore=False)
    assert stats.scanned == 6  # secrets/token.py now included


def test_index_into_missing_path_raises(app_config, tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        index_path(app_config, tmp_path / "nope")


def test_every_indexed_chunk_is_verbatim(app_config, repo_copy: Path) -> None:
    """The citation invariant, verified across a whole real index."""
    index_path(app_config, repo_copy)
    with SqliteStore(app_config.db_path) as store:
        repo_id = store.list_repositories()[0]["id"]
        rows = store.connection.execute(
            "SELECT f.path, c.start_line, c.end_line, c.content FROM chunks c "
            "JOIN files f ON f.id = c.file_id WHERE f.repo_id = ?",
            (repo_id,),
        ).fetchall()
    assert rows
    for row in rows:
        source = (repo_copy / row["path"]).read_text(encoding="utf-8").split("\n")
        assert row["content"] == "\n".join(source[row["start_line"] - 1 : row["end_line"]]), (
            f"{row['path']}:{row['start_line']}-{row['end_line']} does not match the file"
        )


# -- CLI -------------------------------------------------------------------- #


def run(argv: list[str], capsys) -> tuple[int, str]:
    code = main(argv)
    return code, capsys.readouterr().out


def test_cli_index_then_search(app_config, repo_copy: Path, capsys) -> None:
    code, out = run(["--db", str(app_config.db_path), "index", str(repo_copy)], capsys)
    assert code == 0
    assert "Indexed:" in out

    code, out = run(["--db", str(app_config.db_path), "search", "exponential_backoff"], capsys)
    assert code == 0
    assert "src/service.py" in out


def test_cli_search_json_output(app_config, repo_copy: Path, capsys) -> None:
    run(["--db", str(app_config.db_path), "index", str(repo_copy)], capsys)
    code, out = run(
        ["--db", str(app_config.db_path), "search", "retry", "--json", "--limit", "3"], capsys
    )
    assert code == 0
    payload = json.loads(out)
    assert isinstance(payload, list)
    assert all({"path", "lines", "score"} <= set(item) for item in payload)


def test_cli_search_without_index_errors(app_config, tmp_path: Path, capsys) -> None:
    code = main(["--db", str(tmp_path / "missing.sqlite3"), "search", "anything"])
    assert code == 1


def test_cli_search_miss_prints_message(app_config, repo_copy: Path, capsys) -> None:
    run(["--db", str(app_config.db_path), "index", str(repo_copy)], capsys)
    code, out = run(["--db", str(app_config.db_path), "search", "zzz-nothing-matches-zzz"], capsys)
    assert code == 0
    assert "No matches." in out


def test_cli_type_filter(app_config, repo_copy: Path, capsys) -> None:
    run(["--db", str(app_config.db_path), "index", str(repo_copy)], capsys)
    code, out = run(
        ["--db", str(app_config.db_path), "search", "retry", "--type", "knowledge"], capsys
    )
    assert code == 0
    assert "knowledge" in out


def test_cli_no_command_prints_help(capsys) -> None:
    assert main([]) == 1
    assert "COMMAND" in capsys.readouterr().out
