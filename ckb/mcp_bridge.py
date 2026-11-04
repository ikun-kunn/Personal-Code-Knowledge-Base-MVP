"""Opt-in discovery of local Codex capabilities and stdio MCP forwarding."""

import os
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _load_toml(path):
    try:
        import tomllib
    except ModuleNotFoundError as exc:
        raise ValueError("The Codex bridge requires Python 3.11 or newer") from exc
    return tomllib.loads(path.read_text(encoding="utf-8-sig"))


def _definitions(root, home):
    definitions = {}
    folders = (
        ("skill", home / "skills", "*/SKILL.md"),
        ("skill", home.parent / ".agents" / "skills", "*/SKILL.md"),
        ("skill", root / ".agents" / "skills", "*/SKILL.md"),
        ("agent", home / "agents", "*.toml"),
        ("agent", root / ".codex" / "agents", "*.toml"),
    )
    for kind, folder, pattern in folders:
        for path in sorted(folder.glob(pattern)):
            resolved = path.resolve()
            if not resolved.is_relative_to(folder.resolve()) or not resolved.is_file():
                continue
            name = path.parent.name if kind == "skill" else path.stem
            if not name.startswith("."):
                definitions[f"{kind}:{name}"] = resolved
    return definitions


def register_bridge(server, root: Path, home: Path, upstream_names: list[str]):
    config = _load_toml(home / "config.toml")
    configured = config.get("mcp_servers", {})
    upstreams = {}
    for name in upstream_names:
        entry = configured.get(name)
        if not entry or not entry.get("enabled", True) or not entry.get("command") or "url" in entry:
            raise ValueError(f"Upstream {name!r} must be an enabled stdio server in config.toml")
        if name in {"code-knowledge", "code_knowledge"}:
            raise ValueError("Do not configure the knowledge server as its own upstream")
        upstreams[name] = entry
    definitions = _definitions(root, home)

    @asynccontextmanager
    async def connect(name):
        if name not in upstreams:
            raise ValueError("Upstream is not on the startup allowlist")
        entry = upstreams[name]
        env = {key: os.environ[key] for key in entry.get("env_vars", []) if key in os.environ}
        env.update(entry.get("env", {}))
        params = StdioServerParameters(command=entry["command"], args=entry.get("args", []),
                                       cwd=entry.get("cwd", str(root)), env=env)
        timeout = timedelta(seconds=entry.get("tool_timeout_sec", 60))
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=timeout) as session:
                await session.initialize()
                yield session

    def allowed(entry, tool):
        enabled = entry.get("enabled_tools")
        return (enabled is None or tool in enabled) and tool not in entry.get("disabled_tools", [])

    @server.tool()
    def list_local_capabilities() -> dict:
        """List imported capability names. Agent definitions require a host that can run agents."""
        return {"upstreams": sorted(upstreams), "definitions": sorted(definitions)}

    @server.tool()
    def read_local_definition(capability: str) -> dict:
        """Read a listed skill or agent definition. The host decides how to apply it."""
        if capability not in definitions:
            raise ValueError("Choose a name returned by list_local_capabilities")
        path = definitions[capability]
        if path.resolve() != path:
            raise ValueError("Definition moved. Restart the bridge to rediscover it")
        if path.stat().st_size > 128_000:
            raise ValueError("Definition is larger than 128 KB")
        if capability.startswith("agent:"):
            data = _load_toml(path)
            content = "\n".join(str(data[key]) for key in ("name", "description", "developer_instructions") if key in data)
        else:
            content = path.read_text(encoding="utf-8-sig")
        return {"path": str(path), "content": content}

    if upstreams:
        @server.tool()
        async def list_upstream_tools(upstream: str) -> list[dict]:
            """Discover tool names and input schemas from an allowed upstream MCP."""
            async with connect(upstream) as session:
                tools = []
                cursor = None
                while True:
                    page = await session.list_tools(cursor=cursor)
                    tools.extend(tool.model_dump(by_alias=True, exclude_none=True)
                                 for tool in page.tools if allowed(upstreams[upstream], tool.name))
                    cursor = page.nextCursor
                    if cursor is None:
                        return tools

        @server.tool()
        async def call_upstream_tool(upstream: str, tool: str, arguments: dict | None = None) -> dict:
            """Run a tool from an allowed upstream. Discover its schema and check its effects first."""
            if upstream not in upstreams or not allowed(upstreams[upstream], tool):
                raise ValueError("Upstream or tool is not allowed")
            async with connect(upstream) as session:
                result = await session.call_tool(tool, arguments or {})
                return result.model_dump(by_alias=True, exclude_none=True)
