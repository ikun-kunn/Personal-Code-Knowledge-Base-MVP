# Project workflow

Before changing code, call the knowledge MCP tool `task_context` with the task and target path if it is connected. Otherwise run `python -m ckb index` and `python -m ckb context "<task>" --scope <target-path>`.
Read the cited rule cards and relevant implementations. Treat active rules as project conventions; draft and deprecated cards are background only.
When a new decision, pattern, or incident has lasting value, add a card under `.ckb/knowledge/` with a source or rationale.
After edits, run `python -m ckb check`, relevant tests, and reindex. Keep retrieved context limited to the task.

This file describes the knowledge workflow. Project-specific naming, layering, error, test, and API conventions belong in active rule cards.
