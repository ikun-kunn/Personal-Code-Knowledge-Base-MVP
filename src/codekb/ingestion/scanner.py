"""Walk a project directory and decide what is safe and useful to index.

Hard rules for this module:

* **Read-only.** Never import, execute, or otherwise evaluate indexed code.
* **No secrets.** Credential-shaped files are skipped by name/extension before
  their contents are ever read.
* **Verbatim text.** Files are decoded as UTF-8 (errors tolerated) and hashed as
  raw bytes, so "changed" means the bytes changed.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from codekb.config import IndexConfig
from codekb.models import ChunkStatus, FileKind, ScannedFile, SourceType

#: Extension -> detected format. Unlisted extensions are never indexed.
KIND_BY_SUFFIX: dict[str, FileKind] = {
    ".py": FileKind.PYTHON,
    ".pyi": FileKind.PYTHON,
    ".md": FileKind.MARKDOWN,
    ".markdown": FileKind.MARKDOWN,
    ".toml": FileKind.TOML,
    ".yaml": FileKind.YAML,
    ".yml": FileKind.YAML,
    ".json": FileKind.JSON,
    ".txt": FileKind.TEXT,
}

#: Never indexed, regardless of extension or .gitignore.
SENSITIVE_FILENAMES = frozenset(
    {
        ".env",
        ".netrc",
        "_netrc",
        "credentials",
        "credentials.json",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "known_hosts",
        "authorized_keys",
    }
)

SENSITIVE_SUFFIXES = frozenset(
    {".pem", ".key", ".pfx", ".p12", ".jks", ".keystore", ".ppk", ".kdbx", ".crt", ".cer"}
)

#: Dot-directories that are still worth indexing for documentation.
HIDDEN_DIR_ALLOWLIST = frozenset({".github"})

_BOM = b"\xef\xbb\xbf"


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def classify_suffix(suffix: str) -> FileKind | None:
    return KIND_BY_SUFFIX.get(suffix.lower())


def is_sensitive(path: Path) -> bool:
    """Name/extension credential filter. Runs before the file is read."""
    name = path.name.lower()
    if name in SENSITIVE_FILENAMES or name.startswith(".env"):
        return True
    return path.suffix.lower() in SENSITIVE_SUFFIXES


def decode_bytes(payload: bytes) -> str:
    if payload.startswith(_BOM):
        payload = payload[len(_BOM) :]
    return payload.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# .gitignore
# --------------------------------------------------------------------------- #


def _translate_glob(pattern: str, anchored: bool) -> str:
    """Translate one gitignore glob into a regex body anchored at both ends."""
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            if pattern[i : i + 3] == "**/":
                out.append("(?:[^/]+/)*")
                i += 3
                continue
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        elif char == "[":
            j = i + 1
            if j < n and pattern[j] in "!^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:  # unterminated class -> literal '['
                out.append(re.escape(char))
                i += 1
            else:
                body = pattern[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = j + 1
        else:
            out.append(re.escape(char))
            i += 1
    body = "".join(out)
    return body if anchored else f"(?:{body}|(?:.*/)?{body})"


@dataclass(frozen=True, slots=True)
class _IgnorePattern:
    regex: re.Pattern[str]
    negated: bool
    dir_only: bool


class Gitignore:
    """A deliberately small gitignore subset: ``*``, ``**``, ``?``, ``[set]``,
    anchoring, dir-only patterns and ``!`` negation, evaluated last-match-wins.

    Not supported: backslash escapes and ``core.excludesFile``. Unparseable lines
    are skipped rather than treated as matches - an imperfect matcher must never
    silently hide files that a developer expected to be indexed.
    """

    __slots__ = ("base", "patterns")

    def __init__(self, base: str = "", lines: list[str] | None = None) -> None:
        self.base = base.rstrip("/")
        self.patterns: list[_IgnorePattern] = []
        for raw in lines or ():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            negated = stripped.startswith("!")
            if negated:
                stripped = stripped[1:]
            dir_only = stripped.endswith("/")
            stripped = stripped.rstrip("/")
            if not stripped:
                continue
            anchored = "/" in stripped
            if stripped.startswith("/"):
                stripped = stripped[1:]
            if not stripped:
                continue
            self.patterns.append(
                _IgnorePattern(
                    regex=re.compile("(?s:" + _translate_glob(stripped, anchored) + r")\Z"),
                    negated=negated,
                    dir_only=dir_only,
                )
            )

    def is_empty(self) -> bool:
        return not self.patterns

    def _relativise(self, rel_path: str) -> str | None:
        if not self.base:
            return rel_path
        prefix = f"{self.base}/"
        if not rel_path.startswith(prefix):
            return None
        return rel_path[len(prefix) :]

    def verdict(self, rel_path: str, *, is_dir: bool) -> bool | None:
        """Verdict of the *last* matching pattern, or ``None`` if none matched.

        ``is_dir`` decides whether dir-only patterns (``build/``) apply, and
        whether ancestors are also tested (a file under ``build/`` is ignored).
        """
        if not self.patterns:
            return None
        target = self._relativise(rel_path)
        if target is None:
            return None

        candidates: list[tuple[str, bool]] = []
        if is_dir:
            candidates.append((target, True))
        else:
            parts = target.split("/")
            for depth in range(1, len(parts)):
                candidates.append(("/".join(parts[:depth]), True))
            candidates.append((target, False))

        verdict: bool | None = None
        for candidate, candidate_is_dir in candidates:
            for pattern in self.patterns:
                if pattern.dir_only and not candidate_is_dir:
                    continue
                if pattern.regex.match(candidate):
                    verdict = not pattern.negated
        return verdict

    def matches(self, rel_path: str, *, is_dir: bool) -> bool:
        return self.verdict(rel_path, is_dir=is_dir) is True

    def is_ignored(self, rel_path: str) -> bool:
        """Whether a *file* at ``rel_path`` is ignored."""
        return self.matches(rel_path, is_dir=False)


@dataclass(frozen=True, slots=True)
class IgnoreStack:
    """All ``.gitignore`` files in a tree, flattened into git's precedence order.

    Git gives deeper files precedence over shallower ones and the *last* matching
    pattern wins within a file. Flattening shallow-to-deep and applying
    last-match-wins over the whole sequence reproduces both rules exactly.
    """

    rules: tuple[Gitignore, ...]

    def is_ignored(self, rel_path: str) -> bool:
        return self._verdict(rel_path, is_dir=False) is True

    def is_ignored_dir(self, rel_dir: str) -> bool:
        return self._verdict(rel_dir, is_dir=True) is True

    def _verdict(self, rel_path: str, *, is_dir: bool) -> bool | None:
        verdict: bool | None = None
        for matcher in self.rules:
            result = matcher.verdict(rel_path, is_dir=is_dir)
            if result is not None:
                verdict = result
        return verdict


def collect_ignores(root: Path, respect: bool) -> IgnoreStack:
    """Gather every ``.gitignore`` under ``root`` in shallow-to-deep order."""
    if not respect:
        return IgnoreStack(())
    collected: list[tuple[int, Gitignore]] = []
    skip_dirs = {".git", ".venv", "venv", "node_modules", "__pycache__"}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in skip_dirs and not (d.startswith(".") and d not in HIDDEN_DIR_ALLOWLIST)
        ]
        if ".gitignore" not in filenames:
            continue
        here = Path(dirpath)
        rel = here.relative_to(root).as_posix()
        rel = "" if rel == "." else rel
        lines = (here / ".gitignore").read_text(encoding="utf-8", errors="replace").splitlines()
        collected.append((rel.count("/") + (0 if rel else -1), Gitignore(rel, lines)))
    collected.sort(key=lambda item: item[0])
    return IgnoreStack(tuple(matcher for _, matcher in collected))


# --------------------------------------------------------------------------- #
# Scanner
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ScanResult:
    files: list[ScannedFile]
    #: Paths skipped for any reason (wrong extension, gitignored, binary, ...).
    ignored: int = 0
    #: Directories pruned before descent.
    pruned_dirs: int = 0


def _source_type_for(rel_path: str, kind: FileKind, knowledge_dirs: tuple[str, ...]) -> SourceType:
    if any(rel_path == d or rel_path.startswith(f"{d}/") for d in knowledge_dirs):
        return SourceType.KNOWLEDGE
    return SourceType.DOC if kind in {FileKind.MARKDOWN, FileKind.TEXT} else SourceType.CODE


def _default_status(source_type: SourceType, config: IndexConfig) -> ChunkStatus:
    if source_type is SourceType.KNOWLEDGE:
        try:
            return ChunkStatus(config.knowledge_default_status)
        except ValueError:
            return ChunkStatus.DRAFT
    return ChunkStatus.VERIFIED


def _keep_dir(name: str, config: IndexConfig) -> bool:
    if name in config.exclude_dirs:
        return False
    if name.startswith(".") and name not in HIDDEN_DIR_ALLOWLIST:
        return False
    return not is_sensitive(Path(name))


def scan(root: Path, config: IndexConfig) -> ScanResult:
    """Scan ``root`` and return indexable files plus an ignored-path tally."""
    root = root.resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {root}")

    ignores = collect_ignores(root, config.respect_gitignore)
    max_bytes = config.max_file_size_kb * 1024

    files: list[ScannedFile] = []
    ignored = 0
    pruned = 0

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        rel_dir = current.relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir

        kept: list[str] = []
        for name in dirnames:
            rel_child = f"{rel_dir}/{name}" if rel_dir else name
            if not _keep_dir(name, config):
                pruned += 1
                continue
            if ignores.is_ignored_dir(rel_child):
                pruned += 1
                ignored += 1
                continue
            kept.append(name)
        dirnames[:] = kept

        for name in sorted(filenames):
            path = current / name
            rel_path = path.relative_to(root).as_posix()

            if name.startswith(".") and not (rel_dir or "").startswith(".github"):
                ignored += 1
                continue
            if is_sensitive(path):
                ignored += 1
                continue
            suffix = path.suffix.lower()
            kind = classify_suffix(suffix)
            if kind is None or suffix not in config.include_extensions:
                ignored += 1
                continue
            if ignores.is_ignored(rel_path):
                ignored += 1
                continue

            try:
                payload = path.read_bytes()
            except OSError:
                ignored += 1
                continue
            if b"\x00" in payload:  # binary safety net
                ignored += 1
                continue
            if len(payload) > max_bytes:
                ignored += 1
                continue

            source_type = _source_type_for(rel_path, kind, config.knowledge_dirs)
            files.append(
                ScannedFile(
                    abs_path=path,
                    rel_path=rel_path,
                    kind=kind,
                    sha256=sha256_bytes(payload),
                    size_bytes=len(payload),
                    source_type=source_type,
                    status=_default_status(source_type, config),
                )
            )

    files.sort(key=lambda item: item.rel_path)
    return ScanResult(files=files, ignored=ignored, pruned_dirs=pruned)
