"""Shared fixtures. Each test gets an isolated temp DB and the demo repo."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codekb.config import AppConfig, ChunkingConfig, IndexConfig, load_config
from codekb.storage.sqlite_store import SqliteStore

FIXTURES = Path(__file__).parent / "fixtures"
DEMO_REPO = FIXTURES / "demo_repo"


@pytest.fixture
def demo_repo() -> Path:
    return DEMO_REPO


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    """A writable copy of the demo repo, so tests may mutate it freely."""
    import shutil

    target = tmp_path / "demo_repo"
    shutil.copytree(DEMO_REPO, target)
    return target


@pytest.fixture
def index_config() -> IndexConfig:
    return IndexConfig(
        include_extensions=frozenset({".py", ".md", ".toml", ".txt"}),
        max_file_size_kb=500,
        respect_gitignore=True,
        exclude_dirs=frozenset({".git", "__pycache__", "data", "build", ".venv"}),
        knowledge_dirs=("knowledge",),
        knowledge_default_status="draft",
    )


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    """A config rooted at tmp_path with a throwaway database."""
    base = load_config(Path(__file__).parent.parent / "config" / "config.toml")
    return replace(
        base,
        root=tmp_path,
        db_path=tmp_path / "data" / "test.sqlite3",
        index=IndexConfig(include_extensions=frozenset({".py", ".md", ".toml", ".txt"})),
        chunking=ChunkingConfig(max_lines=60, max_chars=2000, overlap_lines=5),
    )


@pytest.fixture
def store(tmp_path: Path):
    with SqliteStore(tmp_path / "test.sqlite3") as opened:
        yield opened
