import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from ckb import core


@unittest.skipUnless(importlib.util.find_spec("mcp"), "MCP extra is not installed")
class McpTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_workflow(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core.init(root)
            (root / "login.py").write_text("def retry_login(): pass\n", encoding="utf-8")
            home = root / ".codex"
            home.mkdir()
            upstream_args = ["-m", "ckb", "--root", str(root), "mcp"]
            (home / "config.toml").write_text(
                "[mcp_servers.fixture]\n"
                f"command = {json.dumps(sys.executable, ensure_ascii=False)}\n"
                f"args = {json.dumps(upstream_args, ensure_ascii=False)}\n"
                f"cwd = {json.dumps(str(Path(__file__).resolve().parents[1]), ensure_ascii=False)}\n"
                'env_vars = ["PYTHONPATH"]\n'
                'enabled_tools = ["find_examples"]\n',
                encoding="utf-8",
            )
            skill = root / ".agents" / "skills" / "review"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("# Review", encoding="utf-8")
            bridge_args = ["--codex-home", str(home), "--upstream", "fixture"] if sys.version_info >= (3, 11) else []
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "ckb", "--root", str(root), "mcp", "--allow-drafts", *bridge_args],
                cwd=str(Path(__file__).resolve().parents[1]),
                env={"PYTHONPATH": os.environ.get("PYTHONPATH", "")},
            )
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=15)) as session:
                    await session.initialize()
                    expected = {"task_context", "find_examples", "capture_draft"}
                    if bridge_args:
                        expected.update({"list_local_capabilities", "read_local_definition",
                                         "list_upstream_tools", "call_upstream_tool"})
                    self.assertEqual({tool.name for tool in (await session.list_tools()).tools}, expected)
                    if bridge_args:
                        capabilities = await session.call_tool("list_local_capabilities")
                        self.assertIn("skill:review", str(capabilities.content))
                        definition = await session.call_tool("read_local_definition", {"capability": "skill:review"})
                        self.assertFalse(definition.isError, str(definition.content))
                        self.assertIn("# Review", str(definition.content))
                    result = await session.call_tool("task_context", {"task": "retry_login", "path": "login.py"})
                    self.assertFalse(result.isError)
                    self.assertIn("login.py:1-1", result.content[0].text)
                    if bridge_args:
                        tools = await session.call_tool("list_upstream_tools", {"upstream": "fixture"})
                        self.assertFalse(tools.isError)
                        self.assertIn("find_examples", str(tools.content))
                        self.assertNotIn("task_context", str(tools.content))
                        forwarded = await session.call_tool("call_upstream_tool", {
                            "upstream": "fixture", "tool": "find_examples", "arguments": {"query": "retry_login"},
                        })
                        self.assertFalse(forwarded.isError)
                        self.assertIn("login.py", str(forwarded.content))
                    examples = await session.call_tool("find_examples", {"query": "retry_login", "kind": "code"})
                    self.assertFalse(examples.isError)
                    self.assertIn("login.py", str(examples.content))
                    (root / ".ckb" / "knowledge" / "broken.md").write_text("broken", encoding="utf-8")
                    (root / "logout.py").write_text("def retry_logout(): pass\n", encoding="utf-8")
                    refreshed = await session.call_tool("task_context", {
                        "task": "retry_logout", "path": "logout.py", "budget": 500,
                    })
                    self.assertFalse(refreshed.isError, str(refreshed.content))
                    context = refreshed.content[0].text
                    self.assertIn(".ckb/knowledge/broken.md: missing front matter", context)
                    self.assertIn("def retry_logout(): pass", context)
                    self.assertLessEqual(len(context), 500)
                    self.assertIn("development_workflow", {p.name for p in (await session.list_prompts()).prompts})
                    draft = await session.call_tool("capture_draft", {
                        "kind": "incident", "title": "登录重试", "summary": "Retry transient failures",
                        "lesson": "Retry only transient failures.", "source": "login.py:1",
                    })
                    self.assertFalse(draft.isError)
            cards = list((root / ".ckb" / "knowledge").glob("card-*.md"))
            self.assertEqual(len(cards), 1)
            self.assertIn("status: draft", cards[0].read_text(encoding="utf-8"))
