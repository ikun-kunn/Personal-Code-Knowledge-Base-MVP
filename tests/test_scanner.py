"""File scanning: filters, gitignore, sensitive-file exclusion, hashing."""

from __future__ import annotations

from pathlib import Path

import pytest

from codekb.ingestion.scanner import Gitignore, collect_ignores, is_sensitive, scan
from codekb.models import FileKind, SourceType


def test_scan_finds_expected_files(demo_repo: Path, index_config) -> None:
    result = scan(demo_repo, index_config)
    paths = {f.rel_path for f in result.files}
    assert "src/service.py" in paths
    assert "README.md" in paths
    assert "config/settings.toml" in paths
    assert "knowledge/patterns/retry.md" in paths
    assert "notes.txt" not in paths  # .txt excluded by this fixture's config


def test_scan_honours_extension_allowlist(demo_repo: Path, index_config) -> None:
    paths = {f.rel_path for f in scan(demo_repo, index_config).files}
    assert not any(path.endswith((".yaml", ".yml", ".json")) for path in paths)


def test_scan_assigns_file_kinds(demo_repo: Path, index_config) -> None:
    by_path = {f.rel_path: f for f in scan(demo_repo, index_config).files}
    assert by_path["src/service.py"].kind is FileKind.PYTHON
    assert by_path["README.md"].kind is FileKind.MARKDOWN
    assert by_path["config/settings.toml"].kind is FileKind.TOML


def test_scan_marks_knowledge_directory(demo_repo: Path, index_config) -> None:
    by_path = {f.rel_path: f for f in scan(demo_repo, index_config).files}
    assert by_path["knowledge/patterns/retry.md"].source_type is SourceType.KNOWLEDGE
    assert by_path["knowledge/patterns/retry.md"].status.value == "draft"
    assert by_path["src/service.py"].source_type is SourceType.CODE


def test_scan_excludes_sensitive_files(demo_repo: Path, index_config) -> None:
    paths = {f.rel_path for f in scan(demo_repo, index_config).files}
    assert ".env" not in paths
    assert not any(path.startswith("secrets/") for path in paths)


def test_scan_includes_gitignored_files_when_disabled(demo_repo: Path, index_config) -> None:
    from dataclasses import replace

    paths = {
        f.rel_path for f in scan(demo_repo, replace(index_config, respect_gitignore=False)).files
    }
    assert "secrets/token.py" in paths


def test_scan_skips_oversized_files(tmp_path: Path, index_config) -> None:
    from dataclasses import replace

    (tmp_path / "big.md").write_text("x" * 5000, encoding="utf-8")
    (tmp_path / "small.md").write_text("# hi", encoding="utf-8")
    config = replace(index_config, max_file_size_kb=1)
    paths = {f.rel_path for f in scan(tmp_path, config).files}
    assert paths == {"small.md"}


def test_scan_skips_binary_files(tmp_path: Path, index_config) -> None:
    (tmp_path / "blob.md").write_bytes(b"text\x00binary")
    assert scan(tmp_path, index_config).files == []


def test_content_hash_changes_with_content(repo_copy: Path, index_config) -> None:
    before = {f.rel_path: f.sha256 for f in scan(repo_copy, index_config).files}
    target = repo_copy / "README.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nextra\n", encoding="utf-8")
    after = {f.rel_path: f.sha256 for f in scan(repo_copy, index_config).files}
    assert before["README.md"] != after["README.md"]
    assert before["src/service.py"] == after["src/service.py"]


def test_scan_is_deterministic(repo_copy: Path, index_config) -> None:
    first = [f.rel_path for f in scan(repo_copy, index_config).files]
    second = [f.rel_path for f in scan(repo_copy, index_config).files]
    assert first == second == sorted(first)


# -- gitignore -------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("pattern", "path", "ignored"),
    [
        # `*.log` matches by basename at any depth, but only the `.log` suffix.
        ("*.log", "debug.log", True),
        ("*.log", "logs/debug.log", True),
        ("*.log", "logs/debug.txt", False),
        ("*.log", "debug.py", False),
        # A trailing slash restricts the pattern to directories; without one the
        # pattern matches both files and directories of that name.
        ("build/", "build/out.txt", True),
        ("build", "build/out.txt", True),
        ("build", "build.txt", False),  # name must match exactly
        # A leading slash anchors to the .gitignore's own directory.
        ("/only-root.txt", "only-root.txt", True),
        ("/only-root.txt", "nested/only-root.txt", False),
        # A slash anywhere but the end also anchors.
        ("docs/*.md", "docs/a.md", True),
        ("docs/*.md", "docs/deep/a.md", False),
        ("docs/*.md", "other/a.md", False),
        ("**/generated/*.py", "a/b/generated/x.py", True),
        ("**/generated/*.py", "generated/x.py", True),
        ("src/**", "src/a/b/c.py", True),
    ],
)
def test_gitignore_patterns(pattern: str, path: str, ignored: bool) -> None:
    assert Gitignore("", [pattern]).is_ignored(path) is ignored


def test_gitignore_negation_reincludes() -> None:
    matcher = Gitignore("", ["secrets/", "!secrets/keep.py"])
    assert matcher.is_ignored("secrets/token.py") is True
    assert matcher.is_ignored("secrets/keep.py") is False


def test_gitignore_ignores_comments_and_blanks() -> None:
    matcher = Gitignore("", ["", "# comment", "   ", "*.tmp"])
    assert matcher.is_ignored("a.tmp") is True
    assert matcher.is_ignored("a.py") is False


def test_deeper_gitignore_overrides_shallower(demo_repo: Path, index_config) -> None:
    (demo_repo / "src" / ".gitignore").write_text("*.py\n", encoding="utf-8")
    try:
        ignores = collect_ignores(demo_repo, True)
        # Deeper file wins: src/service.py is now ignored, README.md unaffected.
        assert ignores.is_ignored("src/service.py") is True
        assert ignores.is_ignored("README.md") is False
    finally:
        (demo_repo / "src" / ".gitignore").unlink()


def test_gitignore_disabled_returns_empty_stack(demo_repo: Path) -> None:
    assert collect_ignores(demo_repo, False).is_ignored("secrets/token.py") is False


# -- sensitive-file detection ---------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    [".env", ".env.local", "id_rsa", "server.pem", "cert.p12", "secrets.json"],
)
def test_is_sensitive_true(name: str) -> None:
    assert is_sensitive(Path(name)) is True


@pytest.mark.parametrize("name", ["main.py", "README.md", "environment.py"])
def test_is_sensitive_false(name: str) -> None:
    assert is_sensitive(Path(name)) is False
