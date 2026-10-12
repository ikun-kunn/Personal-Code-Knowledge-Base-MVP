# CodeKB

A local-first engineering knowledge base over your own source code. It indexes a
repository, retrieves code and documentation with accurate `path:line`
citations, and — from milestone 5 — assembles that context for an AI assistant.

This repository currently implements **milestones 1–3**: the full loop from
scanning a project to keyword-retrieving verified snippets, with **zero model
service and zero API keys required**.

## Status

| Milestone | Scope | State |
| --- | --- | --- |
| 1 | Skeleton, config, CLI, Git, lint, tests | Done |
| 2 | Scanner, Python AST parser, Markdown/config parsers, chunker | Done |
| 3 | SQLite + FTS5, `index`, `search` | Done |
| 4 | Embeddings, Qdrant local mode, RRF fusion | Not started |
| 5 | Chat providers, `ask`, context assembly | Not started |
| 6 | Knowledge drafting, review flow, retrieval eval set | Not started |
| 7 | MCP server | Not started |

`ask` and `knowledge` are deliberately **absent** rather than stubbed: a command
that cannot work is worse than a command that does not exist.

## Requirements

- Python 3.12+ (developed and tested on 3.14)
- [`uv`](https://astral.sh/uv) — `irm https://astral.sh/uv/install.ps1 | iex`

Runtime dependencies for milestones 1–3: **none**. Everything is standard
library; `pytest` and `ruff` are dev-only.

## Quick start

```powershell
uv sync
uv run codekb index .
uv run codekb search "Gitignore negation"
```

Indexing this repository with itself:

```
$ uv run codekb index .
Scanned: 21 files
Changed: 21 files
Indexed: 178 chunks
Skipped: 0 unchanged files
Removed: 0 files (0 chunks)
Ignored: 6 paths
```

Searching it:

```
$ uv run codekb search "exponential backoff"
Query: exponential backoff

[1] src/codekb/ingestion/chunker.py:96-118
    split_chunk
[2] tests/fixtures/demo_repo/src/service.py:17-19
    RetryPolicy.next_delay
```

Every citation is checkable: open the file at the printed lines and you get
exactly the text that was indexed. That invariant is enforced by tests, not by
convention.

## Commands

```powershell
codekb index [PATH]              # incremental index; default PATH is .
  --repo-name NAME               # display name for the repository
  --force                        # re-parse even unchanged files
  --no-gitignore                 # ignore .gitignore rules

codekb search QUERY...           # BM25 keyword search
  -n, --limit N                  # default 10
  --type code|doc|knowledge      # repeatable filter
  --status verified|draft|deprecated
  --show-content                 # print chunk bodies
  --json                         # machine-readable output

codekb --db PATH                 # override the SQLite location
codekb --config PATH             # override config.toml
```

## How indexing works

```
walk → filter → hash → parse → chunk → store (single transaction)
```

1. **Filter.** Extension allowlist, size cap, excluded directories, `.gitignore`
   (nested files included; deeper files override shallower, matching Git).
2. **Secrets.** Credential-shaped filenames are rejected before the file is read.
   Binary files are rejected by a NUL-byte check.
3. **Hash.** SHA-256 of raw bytes decides *skip*, *replace* or *add*. A second
   run over an unchanged tree writes nothing.
4. **Parse.** Per kind:
   - `.py` — `ast`; one chunk per top-level class/function and per method, plus
     `module header` / `module footer` preambles. Decorators are included.
     Nested functions stay inside their parent.
   - `.md` — one chunk per ATX heading section, flat and non-overlapping, with
     `#` inside fenced code ignored and YAML frontmatter metadata applied.
   - `.toml` / `.yaml` — one chunk per `[table]` / top-level key.
   - `.json` / `.txt` — blank-line separated blocks.
   - Unparseable Python still yields one labelled chunk, so nothing silently
     vanishes from the index.
5. **Chunk.** Units over `max_lines` / `max_chars` are split with overlap. Split
   pieces keep their parent symbol and their **real** source line numbers.
6. **Store.** File row, chunks and FTS rows are written in one transaction.

Indexing never imports or executes the code it reads.

## How keyword search works

Two problems are solved before any SQL is issued.

**FTS5 cannot segment CJK.** With `tokenize='unicode61'`, a run of Han characters
becomes a *single* token. Indexing `并设置重试上限` therefore stores exactly that
token, and the query `重试上限` matches nothing — not even its own substring.
CodeKB pre-tokenises: `chunks_fts.content` stores a **term stream** built by
`tokenizer.index_terms`, and queries are built by `tokenizer.build_match_query`
using the same function. Both sides live in one token space.

**Identifiers are not words.** `retry_request` contributes the whole token *and*
its `snake_case` / `camelCase` sub-tokens, so searching `backoff` finds
`exponential_backoff`. CJK runs longer than four characters become overlapping
bigrams, which is what lets a question phrased differently from the document
still match.

Terms are OR-ed rather than AND-ed — AND-ing bigrams would require a single
chunk to contain every bigram of the question. Ranking is `bm25`. User input is
quoted, so FTS5 operators in a query are matched literally instead of executed.

## Layout

```
src/codekb/
  models.py            data carriers (no I/O)
  tokenizer.py         shared text tokenisation — write path and read path
  config.py            config loading and precedence
  cli.py               argparse only
  ingestion/
    scanner.py         walking, filtering, gitignore, hashing
    parser.py          Python AST / Markdown / config → chunks
    chunker.py         size bounding, token estimation
    pipeline.py        incremental orchestration
  storage/
    sqlite_store.py    schema, transactional sync, hydration
  retrieval/
    keyword.py         BM25 search, filtering, ranking
tests/
  fixtures/demo_repo/  a repo with a syntax error, a .env, and a gitignored dir
data/                  local index (git-ignored)
```

## Configuration

`config/config.toml` holds non-sensitive settings. Precedence is defaults →
TOML → environment → CLI flags. Credentials are never stored in the TOML; it
records the *names* of the environment variables to read (`.env.example` shows
the convention).

```toml
[index]
include_extensions = [".py", ".md", ".toml", ".yaml", ".yml", ".json", ".txt"]
max_file_size_kb = 500
respect_gitignore = true

[chunking]
max_lines = 120
max_chars = 4000
overlap_lines = 10

[retrieval]
keyword_top_k = 20
semantic_top_k = 20
result_top_k = 10
context_token_budget = 6000
rrf_k = 60

[data_egress]
policy = "local_only"
```

`data_egress.policy` is a no-op until milestone 5, but it is recorded now so
that adding a cloud provider later is a config change rather than a redesign.

## Development

```powershell
uv run pytest                       # 143 tests
uv run ruff check .
uv run ruff format --check .
```

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — layering, data flow, known limits
- [`docs/data-model.md`](docs/data-model.md) — schema and the term-stream design
- [`docs/decisions/ADR-001-storage.md`](docs/decisions/ADR-001-storage.md) — why SQLite + FTS5 first
- [`AGENTS.md`](AGENTS.md) — the engineering rules this repository holds itself to