from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import core


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ckb", description="Local code knowledge base")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="project directory (default: current directory)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create configuration and a draft rule card")
    commands.add_parser("index", help="incrementally index the project")
    commands.add_parser("check", help="validate knowledge cards and configuration")
    mcp = commands.add_parser("mcp", help="serve the project over MCP stdio")
    mcp.add_argument("--allow-drafts", action="store_true", help="enable the draft writing tool")
    mcp.add_argument("--codex-home", type=Path, help="import local capability metadata from this Codex directory")
    mcp.add_argument("--upstream", action="append", default=[], help="allow a named local MCP server (repeatable)")
    search = commands.add_parser("search", help="search code and knowledge")
    search.add_argument("query")
    search.add_argument("--scope", default="", help="task path, e.g. src/auth/login.py")
    search.add_argument("--kind", default="", choices=["", "code", *sorted(core.KINDS)])
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--json", action="store_true")
    search.add_argument("--include-drafts", action="store_true")
    context = commands.add_parser("context", help="assemble a bounded, source-linked context pack")
    context.add_argument("query")
    context.add_argument("--scope", default="")
    context.add_argument("--budget", type=int, default=6000, help="maximum characters")
    context.add_argument("--limit", type=int, default=8)
    add = commands.add_parser("add", help="create a knowledge card")
    add.add_argument("kind", choices=sorted(core.KINDS))
    add.add_argument("title")
    add.add_argument("--summary", required=True)
    add.add_argument("--scope", default="**")
    add.add_argument("--body-file", type=Path, help="read Markdown body from file; defaults to stdin")
    add.add_argument("--status", choices=sorted(core.STATUSES), default="draft")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "init":
            made = core.init(root)
            print("Created: " + (", ".join(str(p.relative_to(root)) for p in made) or "nothing; already initialized"))
        elif args.command == "index":
            print(json.dumps(core.index(root), ensure_ascii=False))
        elif args.command == "check":
            errors = core.validate(root)
            for error in errors:
                print(error, file=sys.stderr)
            if errors:
                return 1
            print("Knowledge cards and configuration are valid.")
        elif args.command == "search":
            results = core.search(root, args.query, scope=args.scope, kind=args.kind,
                                  limit=args.limit, include_drafts=args.include_drafts)
            if args.json:
                print(json.dumps([vars(result) for result in results], ensure_ascii=False, indent=2))
            else:
                for result in results:
                    print(f"{result.path}:{result.start_line}-{result.end_line} [{result.kind}] {result.title}")
                    if result.summary:
                        print(f"  {result.summary}")
        elif args.command == "context":
            print(core.context(root, args.query, scope=args.scope, budget=args.budget, limit=args.limit), end="")
        elif args.command == "add":
            body = args.body_file.read_text(encoding="utf-8") if args.body_file else sys.stdin.read()
            print(core.add_card(root, args.kind, args.title, args.summary, body,
                                scope=args.scope, status=args.status))
        elif args.command == "mcp":
            try:
                from .mcp_server import create_server
            except ModuleNotFoundError as exc:
                if exc.name != "mcp":
                    raise
                raise ValueError('Install MCP support with python -m pip install -e ".[mcp]"') from exc
            core.config_for(root)
            server = create_server(root, allow_drafts=args.allow_drafts)
            if args.upstream and not args.codex_home:
                raise ValueError("--upstream requires --codex-home")
            if args.codex_home:
                from .mcp_bridge import register_bridge
                register_bridge(server, root, args.codex_home.resolve(), args.upstream)
            server.run(transport="stdio")
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ckb: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
