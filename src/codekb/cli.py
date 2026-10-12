"""``codekb`` command-line interface.

Milestones 1-3 ship ``index`` and ``search``: the full closed loop that needs no
model service and no API key. ``ask`` and ``knowledge`` arrive with milestones
5 and 6; the CLI deliberately does not stub them, because a command that cannot
work is worse than a command that does not exist.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from codekb import __version__
from codekb.config import AppConfig, ConfigError, load_config
from codekb.ingestion.pipeline import index_path
from codekb.models import ChunkStatus, SourceType
from codekb.retrieval.keyword import KeywordSearchOptions, search_keyword
from codekb.storage.sqlite_store import SqliteStore

EXIT_OK = 0
EXIT_ERROR = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codekb",
        description="Local-first engineering knowledge base over your own source code.",
    )
    parser.add_argument("--version", action="version", version=f"codekb {__version__}")
    parser.add_argument(
        "--config", type=Path, default=None, help="Path to config.toml (default: auto-discover)"
    )
    parser.add_argument("--db", type=Path, default=None, help="Override the SQLite database path")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    index_cmd = sub.add_parser("index", help="Scan a project and update the index incrementally")
    index_cmd.add_argument(
        "path", nargs="?", type=Path, default=Path(), help="Project root (default: .)"
    )
    index_cmd.add_argument("--repo-name", default=None, help="Display name for the repository")
    index_cmd.add_argument(
        "--force", action="store_true", help="Re-parse and re-index even unchanged files"
    )
    index_cmd.add_argument(
        "--no-gitignore",
        action="store_true",
        help="Ignore .gitignore rules (also honours .git/info/exclude-free operation)",
    )

    search_cmd = sub.add_parser("search", help="Keyword search over indexed code and docs")
    search_cmd.add_argument("query", nargs="+", help="Search text (quote multi-word queries)")
    search_cmd.add_argument("-n", "--limit", type=int, default=10, help="Maximum results")
    search_cmd.add_argument(
        "--type",
        dest="source_types",
        choices=[t.value for t in SourceType],
        action="append",
        help="Restrict to a source type (repeatable)",
    )
    search_cmd.add_argument(
        "--status",
        dest="statuses",
        choices=[s.value for s in ChunkStatus],
        action="append",
        help="Restrict to a knowledge status (repeatable)",
    )
    search_cmd.add_argument("--show-content", action="store_true", help="Print chunk bodies")
    search_cmd.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    return parser


def _load(args: argparse.Namespace) -> AppConfig:
    return load_config(args.config, db_path=args.db)


def cmd_index(args: argparse.Namespace, config: AppConfig) -> int:
    target = args.path
    if not target.exists():
        print(f"error: path does not exist: {target}", file=sys.stderr)
        return EXIT_ERROR

    stats = index_path(
        config,
        target,
        repo_name=args.repo_name,
        force=args.force,
        respect_gitignore=False if args.no_gitignore else None,
    )
    print(stats.summary())
    for rel_path, message in stats.errors:
        print(f"warning: {rel_path}: {message}", file=sys.stderr)
    if stats.errors:
        print(f"{len(stats.errors)} file(s) failed to index", file=sys.stderr)
    return EXIT_OK


def cmd_search(args: argparse.Namespace, config: AppConfig) -> int:
    query = " ".join(args.query)
    options = KeywordSearchOptions(
        source_types=frozenset(SourceType(t) for t in args.source_types)
        if args.source_types
        else None,
        statuses=frozenset(ChunkStatus(s) for s in args.statuses) if args.statuses else None,
        limit=max(1, args.limit),
    )

    if not config.db_path.exists():
        print(
            f"error: no index found at {config.db_path}. Run `codekb index .` first.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    with SqliteStore(config.db_path, read_only=True) as store:
        hits = search_keyword(store, query, options)

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "rank": position,
                        "path": hit.rel_path,
                        "lines": [hit.start_line, hit.end_line],
                        "symbol": hit.symbol_name,
                        "title": hit.title,
                        "source_type": str(hit.source_type),
                        "status": str(hit.status),
                        "score": round(hit.score, 6),
                        "tags": list(hit.tags),
                    }
                    for position, hit in enumerate(hits, start=1)
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_OK

    print(f"Query: {query}\n")
    for position, hit in enumerate(hits, start=1):
        badges = []
        if hit.source_type is not SourceType.CODE:
            badges.append(str(hit.source_type))
        if hit.status is not ChunkStatus.VERIFIED:
            badges.append(str(hit.status))
        suffix = f"  ({', '.join(badges)})" if badges else ""
        print(f"[{position}] {hit.location}{suffix}")
        print(f"    {hit.label}")
        if args.show_content:
            for line in hit.content.splitlines()[:12]:
                print(f"    | {line}")
            print()
    if not hits:
        print("No matches.")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not args.command:
        parser.print_help()
        return EXIT_ERROR

    try:
        config = _load(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    handlers = {"index": cmd_index, "search": cmd_search}
    handler = handlers.get(args.command)
    if handler is None:  # pragma: no cover - argparse restricts this
        parser.print_help()
        return EXIT_ERROR

    try:
        return handler(args, config)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
