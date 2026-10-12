"""Configuration loading.

Precedence (lowest to highest):

1. Built-in defaults in this module.
2. ``config/config.toml`` discovered by walking up from the working directory.
3. Environment variables.
4. Explicit CLI flags.

Secrets are never read from the TOML file - only the *names* of environment
variables are stored there.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from codekb.models import SourceType

DEFAULT_CONFIG_RELPATH = Path("config") / "config.toml"

DEFAULT_EXTENSIONS = (".py", ".md", ".toml", ".yaml", ".yml", ".json", ".txt")

DEFAULT_EXCLUDE_DIRS = (
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "node_modules",
    "dist",
    "build",
    "data",
    ".idea",
    ".vscode",
)


@dataclass(frozen=True, slots=True)
class IndexConfig:
    include_extensions: frozenset[str] = frozenset(DEFAULT_EXTENSIONS)
    max_file_size_kb: int = 500
    respect_gitignore: bool = True
    exclude_dirs: frozenset[str] = frozenset(DEFAULT_EXCLUDE_DIRS)
    #: Directory (relative to the indexed root) whose files become
    #: ``SourceType.KNOWLEDGE`` instead of ``code``/``doc``.
    knowledge_dirs: tuple[str, ...] = ("knowledge",)
    #: Default knowledge status for files that carry no ``status:`` frontmatter.
    knowledge_default_status: str = "draft"


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    max_lines: int = 120
    max_chars: int = 4000
    overlap_lines: int = 10


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    keyword_top_k: int = 20
    semantic_top_k: int = 20
    result_top_k: int = 10
    context_token_budget: int = 6000
    rrf_k: int = 60


@dataclass(frozen=True, slots=True)
class ProviderRefConfig:
    """A model reference that keeps credentials out of the config file."""

    provider: str
    model_env: str
    base_url_env: str
    api_key_env: str

    def resolve_model(self) -> str | None:
        return os.environ.get(self.model_env) or None

    def resolve_base_url(self) -> str | None:
        return os.environ.get(self.base_url_env) or None

    def resolve_api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) or None

    def require(self) -> tuple[str, str, str]:
        model = self.resolve_model()
        base_url = self.resolve_base_url()
        if not model or not base_url:
            missing = [
                name
                for name, value in ((self.model_env, model), (self.base_url_env, base_url))
                if not value
            ]
            msg = f"Missing environment variable(s): {', '.join(missing)}"
            raise ConfigError(msg)
        return model, base_url, self.resolve_api_key() or ""


@dataclass(frozen=True, slots=True)
class AppConfig:
    root: Path
    db_path: Path
    index: IndexConfig = field(default_factory=IndexConfig)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    chat: ProviderRefConfig = field(
        default_factory=lambda: ProviderRefConfig(
            provider="openai_compatible",
            model_env="CODEKB_CHAT_MODEL",
            base_url_env="CODEKB_CHAT_BASE_URL",
            api_key_env="CODEKB_CHAT_API_KEY",
        )
    )
    embedding: ProviderRefConfig = field(
        default_factory=lambda: ProviderRefConfig(
            provider="ollama",
            model_env="CODEKB_EMBEDDING_MODEL",
            base_url_env="CODEKB_EMBEDDING_BASE_URL",
            api_key_env="CODEKB_EMBEDDING_API_KEY",
        )
    )
    data_egress_policy: str = "local_only"

    @property
    def data_dir(self) -> Path:
        return self.db_path.parent


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid."""


def find_project_root(start: Path | None = None) -> Path:
    """Walk upwards looking for ``config/config.toml``; fall back to ``start``."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / DEFAULT_CONFIG_RELPATH).is_file():
            return candidate
    return current


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"Expected a table for [{name}] in config.toml")
    return value


def _str_tuple(value: Any, label: str) -> tuple[str, ...]:
    if isinstance(value, tuple) and all(isinstance(v, str) for v in value):
        return value
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{label} must be a list of strings")
    return tuple(value)


def _str_set(value: Any, label: str) -> frozenset[str]:
    items = _str_tuple(value, label)
    # Normalise so a missing leading dot in the config still works.
    return frozenset(i if i.startswith(".") else f".{i}" for i in items)


def _int(value: Any, label: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{label} must be an integer")
    return value


def _bool(value: Any, label: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{label} must be a boolean")
    return value


def _provider_ref(data: dict[str, Any], defaults: ProviderRefConfig) -> ProviderRefConfig:
    if not data:
        return defaults
    return replace(
        defaults,
        provider=str(data.get("provider", defaults.provider)),
        model_env=str(data.get("model_env", defaults.model_env)),
        base_url_env=str(data.get("base_url_env", defaults.base_url_env)),
        api_key_env=str(data.get("api_key_env", defaults.api_key_env)),
    )


def load_config(
    config_path: Path | None = None,
    *,
    db_path: Path | None = None,
) -> AppConfig:
    """Load configuration, applying CLI overrides last."""
    explicit = config_path is not None
    if config_path is None:
        config_path = find_project_root() / DEFAULT_CONFIG_RELPATH
    config_path = config_path.expanduser()

    raw: dict[str, Any] = {}
    if config_path.is_file():
        try:
            raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"Could not parse {config_path}: {exc}") from exc
    elif explicit:
        # A config the user named must exist; one we merely discovered may not.
        raise ConfigError(f"Config file not found: {config_path}")

    root = config_path.parent.parent.resolve() if config_path.is_file() else find_project_root()

    idx = _section(raw, "index")
    default_index = IndexConfig()
    index = IndexConfig(
        include_extensions=(
            _str_set(idx["include_extensions"], "index.include_extensions")
            if "include_extensions" in idx
            else default_index.include_extensions
        ),
        max_file_size_kb=_int(idx.get("max_file_size_kb"), "index.max_file_size_kb", 500),
        respect_gitignore=_bool(idx.get("respect_gitignore"), "index.respect_gitignore", True),
        exclude_dirs=(
            frozenset(_str_tuple(idx["exclude_dirs"], "index.exclude_dirs"))
            if "exclude_dirs" in idx
            else default_index.exclude_dirs
        ),
        knowledge_dirs=(
            _str_tuple(idx["knowledge_dirs"], "index.knowledge_dirs")
            if "knowledge_dirs" in idx
            else default_index.knowledge_dirs
        ),
        knowledge_default_status=str(
            idx.get("knowledge_default_status", default_index.knowledge_default_status)
        ),
    )

    chk = _section(raw, "chunking")
    default_chunking = ChunkingConfig()
    chunking = ChunkingConfig(
        max_lines=max(
            1, _int(chk.get("max_lines"), "chunking.max_lines", default_chunking.max_lines)
        ),
        max_chars=max(
            1, _int(chk.get("max_chars"), "chunking.max_chars", default_chunking.max_chars)
        ),
        overlap_lines=max(
            0,
            min(
                _int(
                    chk.get("overlap_lines"),
                    "chunking.overlap_lines",
                    default_chunking.overlap_lines,
                ),
                max(
                    0,
                    _int(chk.get("max_lines"), "chunking.max_lines", default_chunking.max_lines)
                    - 1,
                ),
            ),
        ),
    )

    ret = _section(raw, "retrieval")
    default_retrieval = RetrievalConfig()
    retrieval = RetrievalConfig(
        keyword_top_k=max(
            1,
            _int(
                ret.get("keyword_top_k"), "retrieval.keyword_top_k", default_retrieval.keyword_top_k
            ),
        ),
        semantic_top_k=max(
            1,
            _int(
                ret.get("semantic_top_k"),
                "retrieval.semantic_top_k",
                default_retrieval.semantic_top_k,
            ),
        ),
        result_top_k=max(
            1,
            _int(ret.get("result_top_k"), "retrieval.result_top_k", default_retrieval.result_top_k),
        ),
        context_token_budget=max(
            256,
            _int(
                ret.get("context_token_budget"),
                "retrieval.context_token_budget",
                default_retrieval.context_token_budget,
            ),
        ),
        rrf_k=max(1, _int(ret.get("rrf_k"), "retrieval.rrf_k", default_retrieval.rrf_k)),
    )

    egress = _section(raw, "data_egress")
    policy = str(egress.get("policy", "local_only"))
    if policy not in {"local_only", "cloud_allowed", "local_model_for_code"}:
        raise ConfigError(
            "data_egress.policy must be one of: local_only, cloud_allowed, local_model_for_code"
        )

    resolved_db = (db_path or Path("data") / "codekb.sqlite3").expanduser()
    if not resolved_db.is_absolute():
        resolved_db = root / resolved_db

    return AppConfig(
        root=root,
        db_path=resolved_db,
        index=index,
        chunking=chunking,
        retrieval=retrieval,
        chat=_provider_ref(_section(raw, "chat"), _provider_ref_default_chat()),
        embedding=_provider_ref(_section(raw, "embedding"), _provider_ref_default_embedding()),
        data_egress_policy=policy,
    )


def _provider_ref_default_chat() -> ProviderRefConfig:
    return ProviderRefConfig(
        provider="openai_compatible",
        model_env="CODEKB_CHAT_MODEL",
        base_url_env="CODEKB_CHAT_BASE_URL",
        api_key_env="CODEKB_CHAT_API_KEY",
    )


def _provider_ref_default_embedding() -> ProviderRefConfig:
    return ProviderRefConfig(
        provider="ollama",
        model_env="CODEKB_EMBEDDING_MODEL",
        base_url_env="CODEKB_EMBEDDING_BASE_URL",
        api_key_env="CODEKB_EMBEDDING_API_KEY",
    )


def knowledge_source_types() -> frozenset[SourceType]:
    return frozenset({SourceType.CODE, SourceType.DOC, SourceType.KNOWLEDGE})
