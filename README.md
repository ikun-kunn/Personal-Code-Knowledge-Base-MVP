# Personal Code Knowledge Base MVP

A local knowledge index for AI coding. It keeps conventions, architecture decisions, patterns, incidents, and existing code in one searchable place, then produces a small context pack with file and line references.

项目目标是让 AI 在不同模块和开发阶段遵守同一套编码规范，复用已有决策和踩坑经验，并按任务检索相关上下文。

## Quick start

Requires Python 3.10+ with SQLite FTS5 trigram support. No server, API key, or external Python package is required to run from source.

```powershell
python -m ckb init
python -m ckb index
python -m ckb search "error handling" --scope src/auth/login.py
python -m ckb context "add login retry" --scope src/auth/login.py --budget 6000
python -m ckb check
```

On a system where `python` is not on the path, use the full path to its executable. To install the `ckb` command, run `python -m pip install -e .`.

`init` creates `.ckb/config.json` and a **draft** style card. Edit that card with the project's real rules and set `status: active`. Draft and deprecated cards are excluded from ordinary retrieval. The repository's `AGENTS.md` instructs coding agents to retrieve context before editing.

Create a reusable card:

```powershell
python -m ckb add decision "Use explicit result types" --summary "Service errors use typed results" --scope "src/service/**" --body-file decision.md
python -m ckb index
```

Cards are Markdown with simple `key: value` front matter. Required keys are `kind` (`rule`, `decision`, `pattern`, `incident`, `note`), `status` (`active`, `draft`, `deprecated`), `title`, `summary`, and `scope` (a glob matching the task path). Edit cards directly when you need a filename or title outside the CLI's simple slug format.

## MCP 接入

在知识库源码目录安装 MCP 扩展。随后在目标项目运行 `ckb init`（已有配置可跳过）。

```powershell
python -m pip install -e ".[mcp]"
```

将下面的配置合并到支持 `mcpServers` 格式的客户端配置里。把 Python 路径换成安装了扩展的解释器，把项目路径换成目标项目的绝对路径。保存后重新加载客户端。

```json
{
  "mcpServers": {
    "code-knowledge": {
      "command": "C:/path/to/python.exe",
      "args": ["-m", "ckb", "--root", "C:/path/to/project", "mcp"]
    }
  }
}
```

在助手里说「先用 `task_context` 为登录接口重试取上下文，目标文件是 `src/auth/login.py`」。有提示词菜单的客户端也可选择 `development_workflow`。

- `task_context` 会在检索前增量刷新索引，按目标路径取规则并返回带行号的上下文。
  格式错误的卡片会从索引中移除，工具会返回警告并继续检索。也可以运行 `ckb check` 查看完整错误。
- `find_examples` 从现有索引查符号、架构决策或踩坑记录，只返回摘要和出处。需要先调用 `task_context` 或手动索引。
- 若希望助手写回经验，在启动参数末尾加上 `--allow-drafts`。这会启用 `capture_draft`，要求提供来源或验证证据。审核草稿后手动改成 `active`，下次刷新即可检索。

每个服务实例使用启动时指定的项目目录。多个项目可用不同名称配置多个实例。

### 从另一款代码助手复用本机 Codex 能力

在 MCP 客户端配置的 `args` 末尾追加 `"--codex-home", "C:/path/to/.codex"`，其中路径指向你的 Codex 配置目录。桥接模式需要 Python 3.11+。要转发某个 MCP，再追加 `"--upstream", "服务器名称"`，可以重复指定。

1. 调用 `list_local_capabilities` 查看启动时发现的个人及项目 Skill、Agent 定义和允许转发的服务器。
2. 调用 `list_upstream_tools` 获取选定服务器的工具及参数，再用 `call_upstream_tool` 调用。检查返回中的 `isError`。转发支持 Codex 配置中的本地 stdio 服务器，每次操作建立独立会话。
3. 用 `read_local_definition` 按名称读取 Skill 的 `SKILL.md` 或 Agent 的说明与指令。Skill 引用的脚本和资料仍需客户端访问。运行 Agent 需要客户端支持相应的代理能力。

启动时读取目录和配置。上游程序在发现工具或调用时才会启动。转发名单由启动参数指定，调用遵循上游配置里的工具启用和禁用名单。

## How retrieval works

- The index scans Git tracked and untracked, nonignored files when the directory is a Git repository. Else it scans the directory. It skips configured build and dependency directories, hidden directories, symbolic links, and files above 256 KiB.
- Code and ordinary Markdown are split into overlapping, line-numbered chunks. Knowledge cards stay whole. SHA-256 makes reindexing incremental; deleted files are removed from the index.
- SQLite FTS5 trigram search handles both English and Chinese substrings. Exact title, summary, and path matches increase relevance. A task path filters cards by scope. Active rule cards are included first in `context`.
- `context` limits characters and entries, reserving room for search results when many rules apply. Without a target path, it includes only global rules (`scope: **`). When a code excerpt must be shortened, it keeps matching lines where possible and cites the displayed range. The SQLite database stays local and is ignored by Git; cards and configuration can be versioned.

`check` validates cards and configuration. It does not lint arbitrary project code; run the project's own formatters, linters, type checks, and tests for enforcement. For code structure rules across languages, an AST rule engine is a sensible follow-up.

## GitHub reuse assessment

| Project | Reusable part | MVP decision |
| --- | --- | --- |
| [MCP reference servers](https://github.com/modelcontextprotocol/servers) | Tool interface and filesystem/memory server examples | Use as a later integration pattern. The repository explicitly calls its servers reference implementations, so this MVP exposes a CLI first. |
| [Aider](https://github.com/Aider-AI/aider) | Repository map and bounded code context ideas | Keep source and line references; avoid importing its full coding-agent stack. |
| [ast-grep](https://github.com/ast-grep/ast-grep) | Structural search and lint rules | Candidate for enforceable naming/layering/API rules after a concrete language stack is chosen. |
| [sqlite-vec](https://github.com/asg017/sqlite-vec) | Local semantic retrieval extension | Optional future layer for synonym/intent matching; it is pre-v1 and requires an embedding model. |
| [Continue](https://github.com/continuedev/continue) | IDE/CLI agent workflows | Useful integration reference, but its README currently describes the repository as read-only. |

The implementation copies no third-party code. Its first version addresses recurring gaps in similar tools: editable conventions with status and scope, source-linked evidence, bounded context, incremental refresh and deletion, and offline operation. Semantic recall, automatic rule enforcement, and editor/MCP integration remain next steps.

## Test

```powershell
python -m unittest discover -s tests -v
```
