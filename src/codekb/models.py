"""Domain types shared across ingestion, storage, retrieval and the CLI.

These are plain data carriers with no I/O and no model-service knowledge, so
every layer can depend on them without creating a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class SourceType(StrEnum):
    """What kind of text a chunk represents."""

    CODE = "code"
    DOC = "doc"
    KNOWLEDGE = "knowledge"


class ChunkStatus(StrEnum):
    """Trust level of a chunk.

    ``VERIFIED`` is the default for ordinary source code: it means "this is the
    current implementation", not "a human reviewed this". Knowledge entries
    only reach ``VERIFIED`` through the review flow in milestone 6.
    """

    VERIFIED = "verified"
    DRAFT = "draft"
    DEPRECATED = "deprecated"


class FileKind(StrEnum):
    """Detected language/format, which selects the parsing strategy."""

    PYTHON = "python"
    MARKDOWN = "markdown"
    TOML = "toml"
    YAML = "yaml"
    JSON = "json"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable unit of text with a verified line range in its source file.

    ``content`` is always verbatim source text, so ``content.splitlines()`` lines
    up exactly with ``source_path[start_line - 1 : end_line]``. That invariant is
    what makes ``path:line`` citations checkable.
    """

    source_type: SourceType
    content: str
    start_line: int
    end_line: int
    title: str | None = None
    symbol_name: str | None = None
    parent_symbol: str | None = None
    status: ChunkStatus = ChunkStatus.VERIFIED
    tags: tuple[str, ...] = ()

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1


@dataclass(frozen=True, slots=True)
class ScannedFile:
    """A file that passed every filter and is eligible for indexing."""

    abs_path: Path
    rel_path: str
    kind: FileKind
    sha256: str
    size_bytes: int
    source_type: SourceType = SourceType.CODE
    status: ChunkStatus = ChunkStatus.VERIFIED
    tags: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.rel_path.rsplit("/", 1)[-1]


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A chunk returned by retrieval, with provenance for display and citation."""

    chunk_id: int
    rel_path: str
    title: str | None
    symbol_name: str | None
    start_line: int
    end_line: int
    content: str
    source_type: SourceType
    status: ChunkStatus
    tags: tuple[str, ...]
    score: float
    #: Which repository the chunk came from. Several repositories share one
    #: index, so results are meaningless without it.
    repo_id: int = 0
    #: Per-retriever provenance, e.g. ``{"keyword": 1.0, "semantic": 2.0}``.
    origins: dict[str, int] = field(default_factory=dict)

    @property
    def location(self) -> str:
        return f"{self.rel_path}:{self.start_line}-{self.end_line}"

    @property
    def label(self) -> str:
        return self.symbol_name or self.title or self.rel_path


@dataclass(slots=True)
class IndexStats:
    """Mutable accumulator describing one ``codekb index`` run."""

    repo_root: str
    scanned: int = 0
    skipped_unchanged: int = 0
    changed: int = 0
    added: int = 0
    removed: int = 0
    chunks_written: int = 0
    chunks_deleted: int = 0
    ignored: int = 0
    errors: tuple[tuple[str, str], ...] = ()

    def summary(self) -> str:
        return (
            f"Scanned: {self.scanned} files\n"
            f"Changed: {self.changed} files\n"
            f"Indexed: {self.chunks_written} chunks\n"
            f"Skipped: {self.skipped_unchanged} unchanged files\n"
            f"Removed: {self.removed} files ({self.chunks_deleted} chunks)\n"
            f"Ignored: {self.ignored} paths"
        )
