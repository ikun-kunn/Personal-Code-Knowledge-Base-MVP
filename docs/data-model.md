# Data model

## Entities

| Table | Holds | Key columns |
| --- | --- | --- |
| `repositories` | One row per indexed project | `root_path` (unique), `name` |
| `files` | Index state, for incremental updates | `(repo_id, path)` unique, `sha256`, `indexed_at` |
| `chunks` | Authoritative retrievable text | line range, `content`, `status`, `tags_json` |
| `chunks_fts` | Derived FTS5 index | `chunk_id` + the term stream |

## Why chunks carry their own text

The obvious alternative is to store only offsets and re-read the file at query
time. That was rejected: search would then depend on the working directory, and
a file edited after indexing would silently change results, making citations
unverifiable. Storing the text makes a `path:line` citation a fact about the
index rather than a claim about the filesystem.

The cost is staleness, which is bounded by re-running `index`.

## `chunks.source_type`

| Value | Meaning | Origin |
| --- | --- | --- |
| `code` | Source code | `.py` and other code extensions |
| `doc` | Architecture docs, READMEs, config | `.md`, `.toml`, `.yaml`, `.json`, `.txt` |
| `knowledge` | Reviewed experience entries | any file under a directory named in `index.knowledge_dirs` |

`knowledge_dirs` holds **directory names**, matched against every segment of a
path, not only against a repo-root prefix. A monorepo may therefore keep entries
at `knowledge/` and at `services/api/knowledge/` and have both typed correctly.
Matching is per-segment and exact, so `knowledge_base/` is not `knowledge/`.

## `chunks.status`

| Value | Meaning |
| --- | --- |
| `verified` | Current source of truth. **Default for all code and docs.** |
| `draft` | Candidate knowledge entry, not yet reviewed |
| `deprecated` | Retained for history; not recommended for new work |

`verified` on a code chunk means "this reflects the current implementation", not
"a human reviewed this". Knowledge entries only reach `verified` through the
review flow planned for milestone 6.

## Multiple repositories

`repositories` is keyed by `root_path`, and `files.repo_id` scopes every chunk.
`SearchHit.repo_id` carries that attribution, and `KeywordSearchOptions.repo_id`
restricts results to one repository.

`repo_id = None` means *all repositories*, which is the default for the CLI
because a first-time user has exactly one. Callers holding a multi-repo index
should always set it — otherwise results silently span projects and a relative
path becomes ambiguous.

## Incremental indexing

`files.sha256` is the whole decision:

| Comparison | Action |
| --- | --- |
| hash equal | skip — no parse, no embedding, no write |
| hash differs | replace that file's chunks and FTS rows in one transaction |
| path absent from the scan | delete the file, its chunks and its FTS rows |

## The term stream

`chunks_fts.content` stores `tokenizer.index_terms(chunk.content)` — a
space-separated token list — **not** the raw text.

This exists because SQLite's `unicode61` tokenizer treats a run of Han characters
as a single token. Verified empirically:

```
Indexed body : 模型请求失败之后，应避免无限重试，并设置重试上限。
Query        : 重试上限
unicode61    : 0 hits      <- not even its own substring
pre-tokenised: 1 hit
```

For the same reason, identifiers emit their whole token plus `snake_case` and
`camelCase` sub-tokens, and CJK runs longer than four characters are replaced by
overlapping bigrams.

**Consequence:** changing `tokenizer.py` invalidates every indexed term stream.
The term stream is derived data and is rebuilt by re-running `index --force`;
this is asserted in `tests/test_sqlite_store.py` and covered by the FTS-sync
tests.

`chunks_fts.title` and `chunks_fts.symbol_name` store raw values. They are short,
predominantly ASCII identifiers that `unicode61` folds consistently with our
lowercased query terms, and keeping them verbatim makes the index inspectable.

## Transaction boundary

`chunks` and `chunks_fts` are separate tables, so a mutation spanning both runs
inside one explicit transaction:

1. Collect the file's existing chunk ids.
2. Delete their FTS rows **first** — once the `chunks` rows go, their ids are
   gone.
3. Delete the `chunks` rows.
4. Insert new `chunks` rows, then their FTS rows.

`tests/test_sqlite_store.py` asserts no FTS row ever references a missing chunk,
after replaces and after deletions.

## Not in this schema yet

- `knowledge_metadata` as a separate table — `tags_json`, `status` and
  `source_type` on `chunks` cover the MVP. Promote them when knowledge entries
  need review history, provenance or per-scope versioning.
- Vector storage (milestone 4) — Qdrant, keyed by `chunk_id`.