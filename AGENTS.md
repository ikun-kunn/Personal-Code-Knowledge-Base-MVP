# AGENTS.md

Engineering rules for **this repository**. CodeKB reads this file, so it is also
a test fixture for the product itself: `codekb index .` must surface it.

## Module boundaries

Each package owns one concern. Dependencies point inward only.

| Package | Owns | May import |
| --- | --- | --- |
| `models.py` | Plain data carriers. No I/O, no model calls. | stdlib |
| `tokenizer.py` | The single text tokenisation shared by index and query paths. | stdlib |
| `config.py` | Loading and precedence. | `models` |
| `ingestion/` | Files -> chunks. Never calls a model. | `models`, `config`, `tokenizer` |
| `storage/` | Persistence. Never decides what is relevant. | `models`, `tokenizer` |
| `retrieval/` | Recall and ranking. Never generates an answer. | `models`, `tokenizer`, `storage` |
| `cli.py` | Argument parsing and output formatting only. | everything above |

`storage` and `retrieval` must not import each other's private helpers. The
tokeniser is the one shared primitive; it lives outside both because the write
path and the read path must agree on it exactly.

## Non-negotiable invariants

1. **Line citations must be exact.** For every chunk,
   `chunk.content == "\n".join(lines[chunk.start_line - 1 : chunk.end_line])`.
   `tests/test_parser.py` and `tests/test_index_cli.py` assert this across a real
   index. A change that breaks it breaks the product's core promise.
2. **`chunks` and `chunks_fts` move together.** They are separate tables, so any
   mutation spanning both runs inside one explicit transaction.
3. **Never execute indexed code.** The indexer reads, parses and hashes files.
   It does not import, eval, or shell out to anything inside a scanned repo.
4. **Never index secrets.** Credential-shaped filenames are filtered *before*
   the file is read, and file symlinks are skipped entirely so that indexing
   `src/` cannot pull in content from outside the repository.
5. **The term stream is derived data.** `chunks_fts.content` holds
   `index_terms(...)`, not raw text. Raw text lives in `chunks`. Any change to
   `tokenizer.py` therefore requires a full re-index to stay consistent.

## Style

- `uv run ruff check .` and `uv run ruff format --check .` must pass.
- `uv run pytest` must pass. No test may be skipped to make a run green.
- Type hints on public functions; `from __future__ import annotations` everywhere.
- Prefer `pathlib` over `os.path`; prefer `StrEnum` over bare string constants.
- Docstrings state *why*, not *what*. The comment above a non-obvious decision
  should let a reader delete the code and still keep the decision.

## Configuration rules

- `config/config.toml` holds non-sensitive settings only. Credentials are
  environment variables named indirectly via `*_env` keys.
- New behaviour needs a config key with a working default, so an existing
  `config.toml` keeps working when upgraded.

## Adding a file kind

1. Add the suffix to `KIND_BY_SUFFIX` in `ingestion/scanner.py`.
2. Add a branch in `parse_file` in `ingestion/parser.py`.
3. Add a fixture file under `tests/fixtures/demo_repo/`.
4. Extend `test_every_chunk_matches_its_source_lines` coverage in
   `tests/test_parser.py` by adding the filename to the parametrised list.

## Testing rules

- Every test asserts a behaviour a user could notice. No tests that only
  restate the implementation.
- Bug fixes ship with a regression test that fails before the fix.
- The demo repo under `tests/fixtures/` includes a deliberately unparseable
  `broken.py` and a `.env`. Keep both: they cover real failure paths, and
  `tests/fixtures/` is excluded from lint for that reason.