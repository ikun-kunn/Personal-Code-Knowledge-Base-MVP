"""Configuration loading and precedence."""

from __future__ import annotations

from pathlib import Path

import pytest

from codekb.config import ConfigError, ProviderRefConfig, find_project_root, load_config

CONFIG_DIR = Path(__file__).parent.parent / "config"


def test_loads_repository_config() -> None:
    config = load_config(CONFIG_DIR / "config.toml")
    assert ".py" in config.index.include_extensions
    assert config.retrieval.keyword_top_k == 20
    assert config.retrieval.rrf_k == 60
    assert config.data_egress_policy == "local_only"


def test_root_is_inferred_from_config_location() -> None:
    config = load_config(CONFIG_DIR / "config.toml")
    assert config.root == (CONFIG_DIR.parent).resolve()
    assert config.db_path == config.root / "data" / "codekb.sqlite3"


def test_extensions_are_normalised_with_dot() -> None:
    assert load_config(CONFIG_DIR / "config.toml").index.include_extensions == frozenset(
        {".py", ".md", ".toml", ".yaml", ".yml", ".json", ".txt"}
    )


def test_missing_explicit_config_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_invalid_toml_raises(tmp_path: Path) -> None:
    bad = tmp_path / "config.toml"
    bad.write_text("[index\nbroken = ", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"Could not parse"):
        load_config(bad)


def test_unknown_egress_policy_raises(tmp_path: Path) -> None:
    bad = tmp_path / "config.toml"
    bad.write_text("[data_egress]\npolicy = 'teleport'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"data_egress\.policy"):
        load_config(bad)


def test_overlap_cannot_exceed_window(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("[chunking]\nmax_lines = 10\noverlap_lines = 50\n", encoding="utf-8")
    chunking = load_config(cfg).chunking
    assert chunking.overlap_lines < chunking.max_lines


def test_db_override_is_absolute(tmp_path: Path) -> None:
    config = load_config(CONFIG_DIR / "config.toml", db_path=Path("custom/db.sqlite3"))
    assert config.db_path.is_absolute()
    assert config.db_path.name == "db.sqlite3"


def test_db_override_respects_absolute_input(tmp_path: Path) -> None:
    target = tmp_path / "explicit.sqlite3"
    assert load_config(CONFIG_DIR / "config.toml", db_path=target).db_path == target


def test_find_project_root_discovers_repo_config() -> None:
    root = find_project_root(Path(__file__).parent)
    assert (root / "config" / "config.toml").is_file()


def test_provider_ref_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    ref = ProviderRefConfig("openai_compatible", "M", "B", "K")
    monkeypatch.setenv("M", "gpt")
    monkeypatch.setenv("B", "http://localhost:1234/v1")
    monkeypatch.setenv("K", "secret")
    assert ref.require() == ("gpt", "http://localhost:1234/v1", "secret")


def test_provider_ref_reports_missing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("M", raising=False)
    monkeypatch.delenv("B", raising=False)
    ref = ProviderRefConfig("openai_compatible", "M", "B", "K")
    with pytest.raises(ConfigError, match="Missing environment variable"):
        ref.require()


def test_secrets_are_not_read_from_config_file() -> None:
    """The TOML stores env var *names* only - never a credential value."""
    raw = (CONFIG_DIR / "config.toml").read_text(encoding="utf-8")
    assert "api_key =" not in raw
    assert "sk-" not in raw
