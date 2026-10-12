---
id: pattern-cjk-fulltext-search
type: implementation-pattern
status: verified
scope:
  - src/codekb/tokenizer.py
  - src/codekb/retrieval/keyword.py
  - src/codekb/storage/sqlite_store.py
tags:
  - sqlite
  - fts5
  - cjk
  - tokenization
updated: 2026-10-12
---

# CJK full-text search needs pre-tokenisation, not just query expansion

## Applicable conditions

Any project that indexes or searches CJK (or Japanese/Korean) text with SQLite
FTS5 using the default `unicode61` tokenizer.

## Problem

`unicode61` classifies Han characters as letters and has no segmentation rule,
so a run of CJK characters becomes **one token**. Indexing

```
模型请求失败之后，应避免无限重试，并设置重试上限。
```

produces exactly three tokens: `模型请求失败之后`, `应避免无限重试`,
`并设置重试上限`.

A query for `重试上限` then matches **nothing** — not even its own substring.

The trap is that expanding the *query* into bigrams makes it look fixed while
changing nothing: the bigram `重试上限` still cannot match a stored token that
spans `并设置重试上限`. Verified empirically:

```
unicode61     -> 0 hits
pre-tokenised -> 1 hit
```

## Recommended solution

Segment on **both** sides with one shared function, and store a term stream
rather than raw text.

1. `tokenizer.index_terms(text)` produces a space-separated token list.
2. `chunks_fts.content` stores that list, not the chunk body.
3. `tokenizer.build_match_query(text)` tokenises the query identically.

Because both sides call the same function, a query term and an indexed term are
comparable at all. Sharing the function is the actual requirement — two
near-identical tokenisers will drift.

Two rules that fall out of this:

- **CJK runs longer than 4 characters are replaced by overlapping bigrams.**
  Short runs stay whole. Dropping the whole run for long sequences is safe: a
  query matching it verbatim shares every one of its bigrams with the stored
  stream, so recall is unchanged and the index is smaller.
- **Terms are OR-ed, not AND-ed.** AND-ing bigrams requires one chunk to contain
  every bigram of the question, which almost no real chunk does.

The same function should emit identifier sub-tokens (`retry_request` also yields
`retry` and `request`), since that is what makes symbol search forgiving.

## Constraints

- The term stream is **derived data**. Raw text must stay in a normal table so
  citations stay checkable and the index can be rebuilt.
- Any change to the tokeniser invalidates every stored stream. That is a
  full re-index, and it must be documented next to the function.
- Keep the tokeniser in a leaf module with no storage or retrieval imports, or
  the write path and the read path will end up importing each other.

## Verification evidence

- `tests/test_keyword_search.py::test_cjk_query_matches_reordered_wording` — a
  question phrased differently from the document still matches on shared bigrams.
- `tests/test_keyword_search.py::test_short_cjk_run_is_kept_whole` and
  `::test_long_cjk_run_produces_bigrams` — both branches of the threshold.
- `tests/test_sqlite_store.py::test_replacing_a_file_does_not_leak_fts_rows` —
  no FTS row ever references a deleted chunk.

## Not applicable when

- Text is ASCII-only — `unicode61` already handles it and the term stream adds
  index size for nothing.
- The search engine already segments CJK (Elasticsearch, Meilisearch, Qdrant
  with a CJK-aware model). The pre-tokenisation step is the workaround for
  SQLite's limitation, not a general best practice.