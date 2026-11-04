from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

KINDS = {"rule", "decision", "pattern", "incident", "note"}
STATUSES = {"active", "draft", "deprecated"}
DEFAULT_CONFIG = {
    "extensions": [".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".md"],
    "exclude": [".git", ".venv", "venv", "node_modules", "dist", "build", "coverage", "__pycache__", ".next"],
    "max_file_bytes": 262144,
    "chunk_lines": 80,
    "overlap_lines": 10,
}


@dataclass
class Entry:
    path: str
    kind: str
    status: str
    title: str
    summary: str
    scope: str
    content: str
    start_line: int
    end_line: int
    score: float = 0.0


def init(root: Path) -> list[Path]:
    root = root.resolve()
    folder = root / ".ckb"
    knowledge = folder / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    made = []
    config = folder / "config.json"
    if not config.exists():
        config.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        made.append(config)
    example = knowledge / "style.md"
    if not example.exists():
        example.write_text(
            "---\nkind: rule\nstatus: draft\ntitle: Project coding conventions\n"
            "summary: Replace this example with your team's naming, layering, error, test and API rules.\n"
            "scope: **\n---\n\n"
            "## Naming\nDocument naming rules.\n\n## Layers\nDocument dependency direction.\n\n"
            "## Errors\nDocument error handling.\n\n## Tests\nDocument test expectations.\n\n"
            "## APIs\nDocument interface contracts.\n",
            encoding="utf-8",
        )
        made.append(example)
    return made


def config_for(root: Path) -> dict:
    path = root / ".ckb" / "config.json"
    if not path.exists():
        raise ValueError("Run `ckb init` in the project first.")
    config = json.loads(path.read_text(encoding="utf-8"))
    if not 0 <= config["overlap_lines"] < config["chunk_lines"]:
        raise ValueError("overlap_lines must be less than chunk_lines")
    return config


def add_card(root: Path, kind: str, title: str, summary: str, body: str, *,
             scope: str = "**", status: str = "draft") -> str:
    config_for(root)
    for name, value in (("title", title), ("summary", summary), ("scope", scope)):
        if not value.strip() or len(value.splitlines()) != 1 or value.splitlines()[0] != value:
            raise ValueError(f"{name} must be a nonempty single line")
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if not slug:
        slug = "card-" + hashlib.sha256(title.encode("utf-8")).hexdigest()[:12]
    folder = root / ".ckb" / "knowledge"
    if folder.resolve() != folder.absolute():
        raise ValueError("Knowledge directory must not redirect through a symlink")
    path = folder / f"{slug}.md"
    card = (f"---\nkind: {kind}\nstatus: {status}\n"
            f"title: {title}\nsummary: {summary}\nscope: {scope}\n---\n\n{body.strip()}\n")
    parse_card(card, path.name)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(card)
    return path.relative_to(root).as_posix()


def parse_card(text: str, path: str) -> dict:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{path}: missing front matter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError(f"{path}: unclosed front matter") from exc
    meta = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"{path}: invalid front matter line: {line}")
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    if meta.get("kind") not in KINDS:
        raise ValueError(f"{path}: kind must be one of {', '.join(sorted(KINDS))}")
    if meta.get("status") not in STATUSES:
        raise ValueError(f"{path}: status must be one of {', '.join(sorted(STATUSES))}")
    if not meta.get("title") or not meta.get("summary") or not meta.get("scope"):
        raise ValueError(f"{path}: title, summary and scope are required")
    meta["body"] = "\n".join(lines[end + 1:]).strip()
    meta["start_line"] = end + 2
    return meta


def _eligible(path: Path, root: Path, config: dict) -> bool:
    rel = path.relative_to(root)
    if path.is_symlink() or not path.is_file() or path.suffix.lower() not in config["extensions"]:
        return False
    if any(part in config["exclude"] or part.startswith(".") for part in rel.parts[:-1]):
        return False
    return path.stat().st_size <= config["max_file_bytes"]


def source_files(root: Path, config: dict) -> list[Path]:
    command = ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
    try:
        # The MCP server reserves stdin for protocol messages.
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, check=True)
        candidates = [root / x.decode("utf-8", errors="surrogateescape") for x in result.stdout.split(b"\0") if x]
    except (OSError, subprocess.CalledProcessError):
        candidates = list(root.rglob("*"))
    files = []
    for path in candidates:
        try:
            if _eligible(path, root, config):
                files.append(path)
        except (OSError, ValueError):
            continue
    cards = list((root / ".ckb" / "knowledge").glob("*.md"))
    return sorted(set(files + cards))


def _connect(root: Path) -> sqlite3.Connection:
    db = root / ".ckb" / "index.sqlite3"
    if not db.parent.exists():
        raise ValueError("Run `ckb init` in the project first.")
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, sha256 TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY, path TEXT NOT NULL, kind TEXT NOT NULL,
            status TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,
            scope TEXT NOT NULL, content TEXT NOT NULL,
            start_line INTEGER NOT NULL, end_line INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS entries_path ON entries(path);
        CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
            title, summary, path, content, tokenize='trigram'
        );
    """)
    return connection


def _delete_file(connection: sqlite3.Connection, rel: str) -> None:
    ids = [row[0] for row in connection.execute("SELECT id FROM entries WHERE path = ?", (rel,))]
    connection.executemany("DELETE FROM entries_fts WHERE rowid = ?", [(i,) for i in ids])
    connection.execute("DELETE FROM entries WHERE path = ?", (rel,))
    connection.execute("DELETE FROM files WHERE path = ?", (rel,))


def _chunks(text: str, rel: str, config: dict) -> list[Entry]:
    lines = text.splitlines()
    if not lines:
        return []
    size = config["chunk_lines"]
    stride = size - config["overlap_lines"]
    entries = []
    for start in range(0, len(lines), stride):
        part = lines[start:start + size]
        entries.append(Entry(rel, "code" if Path(rel).suffix != ".md" else "note", "active",
                             f"{rel}:{start + 1}", "", rel, "\n".join(part), start + 1, start + len(part)))
        if start + size >= len(lines):
            break
    return entries


def index(root: Path) -> dict:
    root = root.resolve()
    config = config_for(root)
    files = source_files(root, config)
    connection = _connect(root)
    seen = set()
    changed = 0
    try:
        with connection:
            for path in files:
                rel = path.relative_to(root).as_posix()
                seen.add(rel)
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                old = connection.execute("SELECT sha256 FROM files WHERE path = ?", (rel,)).fetchone()
                if old and old[0] == digest:
                    continue
                text = raw.decode("utf-8", errors="replace")
                if rel.startswith(".ckb/knowledge/"):
                    meta = parse_card(text, rel)
                    records = [Entry(rel, meta["kind"], meta["status"], meta["title"], meta["summary"],
                                     meta["scope"], meta["body"], meta["start_line"], len(text.splitlines()))]
                else:
                    records = _chunks(text, rel, config)
                _delete_file(connection, rel)
                for entry in records:
                    cursor = connection.execute(
                        "INSERT INTO entries(path,kind,status,title,summary,scope,content,start_line,end_line) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (entry.path, entry.kind, entry.status, entry.title, entry.summary,
                         entry.scope, entry.content, entry.start_line, entry.end_line),
                    )
                    connection.execute("INSERT INTO entries_fts(rowid,title,summary,path,content) VALUES (?,?,?,?,?)",
                                       (cursor.lastrowid, entry.title, entry.summary, entry.path, entry.content))
                connection.execute("INSERT INTO files(path,sha256) VALUES (?,?)", (rel, digest))
                changed += 1
            stale = [r[0] for r in connection.execute("SELECT path FROM files") if r[0] not in seen]
            for rel in stale:
                _delete_file(connection, rel)
        return {"scanned": len(files), "changed": changed, "removed": len(stale),
                "entries": connection.execute("SELECT count(*) FROM entries").fetchone()[0]}
    finally:
        connection.close()


def _terms(query: str) -> list[str]:
    chunks = re.findall(r"[A-Za-z0-9_]+|[\u3400-\u9fff]+", query.lower())
    result = []
    for chunk in chunks:
        if re.match(r"[\u3400-\u9fff]", chunk):
            result.extend(chunk[i:i + 3] for i in range(max(1, len(chunk) - 2)))
        else:
            result.append(chunk)
    return [x for x in dict.fromkeys(result) if len(x) >= 3]


def search(root: Path, query: str, *, scope: str = "", kind: str = "", limit: int = 8,
           include_drafts: bool = False) -> list[Entry]:
    if not query.strip():
        raise ValueError("Query cannot be empty")
    if kind and kind not in KINDS | {"code"}:
        raise ValueError("Invalid kind")
    connection = _connect(root.resolve())
    try:
        terms = _terms(query)
        if terms:
            expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
            rows = connection.execute(
                "SELECT e.*, bm25(entries_fts) AS rank FROM entries_fts "
                "JOIN entries e ON e.id = entries_fts.rowid WHERE entries_fts MATCH ? "
                "ORDER BY rank LIMIT 500", (expression,)).fetchall()
        else:
            needle = query.lower()
            rows = connection.execute(
                "SELECT *, 0.0 AS rank FROM entries WHERE "
                "instr(lower(title), ?) > 0 OR instr(lower(summary), ?) > 0 OR "
                "instr(lower(path), ?) > 0 OR instr(lower(content), ?) > 0 LIMIT 500",
                (needle, needle, needle, needle),
            ).fetchall()
        query_lower = query.lower()
        output = []
        for row in rows:
            if row["status"] != "active" and not include_drafts:
                continue
            if kind and row["kind"] != kind:
                continue
            if scope and row["kind"] != "code" and not fnmatch.fnmatch(scope, row["scope"]):
                continue
            title = row["title"].lower()
            summary = row["summary"].lower()
            content = row["content"].lower()
            path = row["path"].lower()
            score = -float(row["rank"])
            if query_lower in title:
                score += 5
            if query_lower in summary:
                score += 3
            if query_lower in path:
                score += 3
            if query_lower in content:
                score += 1
            if scope and (row["path"] == scope or path.startswith(str(Path(scope).parent).replace('\\', '/').lower() + "/")):
                score += 2
            output.append(Entry(row["path"], row["kind"], row["status"], row["title"],
                                row["summary"], row["scope"], row["content"],
                                row["start_line"], row["end_line"], score))
        output.sort(key=lambda e: (-e.score, e.path, e.start_line))
        return output[:max(0, limit)]
    finally:
        connection.close()


def context(root: Path, query: str, *, scope: str = "", budget: int = 6000, limit: int = 8) -> str:
    if budget < 200:
        raise ValueError("budget must be at least 200 characters")
    connection = _connect(root.resolve())
    try:
        rules = [Entry(r["path"], r["kind"], r["status"], r["title"], r["summary"], r["scope"],
                       r["content"], r["start_line"], r["end_line"])
                 for r in connection.execute("SELECT * FROM entries WHERE kind = 'rule' AND status = 'active'")
                 if not scope or fnmatch.fnmatch(scope, r["scope"])]
    finally:
        connection.close()
    terms = _terms(query)
    rules.sort(key=lambda entry: (
        -sum(term in (entry.title + " " + entry.summary + " " + entry.content).lower() for term in terms),
        -len(entry.scope.replace("*", "")), entry.path,
    ))
    rule_cap = min(3, max(1, limit // 2)) if limit > 0 else 0
    rules = rules[:rule_cap]
    matches = [e for e in search(root, query, scope=scope, limit=limit * 3) if e.kind != "rule"]
    selected = rules + matches
    heading = f"# Relevant project context\nTask: {query}\nScope: {scope or 'project'}\n\n"
    output = heading[:budget]
    rule_end = len(output) + int((budget - len(output)) * 0.45) if rules and matches else budget
    count = 0
    for entry in selected:
        if count >= limit:
            break
        cap = rule_end if entry.kind == "rule" and matches else budget
        section = (f"## {entry.title} [{entry.kind}]\n"
                   f"Source: {entry.path}:{entry.start_line}-{entry.end_line}\n"
                   f"Status: {entry.status}\n"
                   f"Summary: {entry.summary}\n\n{entry.content}\n\n")
        if len(output) + len(section) > cap:
            remaining = cap - len(output)
            if remaining < 180:
                continue
            section = section[:remaining - 18].rstrip() + "\n[truncated]\n\n"
        output += section
        count += 1
        if len(output) >= budget - 20:
            break
    return (output.rstrip() + "\n")[:budget]


def validate(root: Path) -> list[str]:
    config = config_for(root.resolve())
    errors = []
    for path in (root / ".ckb" / "knowledge").glob("*.md"):
        try:
            parse_card(path.read_text(encoding="utf-8"), path.relative_to(root).as_posix())
        except ValueError as exc:
            errors.append(str(exc))
    if config["chunk_lines"] <= 0 or config["max_file_bytes"] <= 0:
        errors.append("chunk_lines and max_file_bytes must be positive")
    return errors
