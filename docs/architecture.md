# Architecture

## Layering

Dependencies point inward. A package never imports a *deeper* package's
internals.

```
                cli.py
                  |
        +---------+---------+
        |                   |
   retrieval/            ingestion/
        |                   |
        +---------+---------+
                  |
             storage/
                  |
             tokenizer.py  models.py  config.py
```

- `models.py`, `tokenizer.py` — leaf modules, standard library only.
- `storage/` — persistence. It decides *how* to store and *how to read back*,
  never what is relevant.
- `ingestion/` — files to chunks. Never calls a model.
- `retrieval/` — recall and ranking. Never generates an answer.
- `cli.py` — argument parsing and formatting only, so it stays trivially
  replaceable by an MCP server in milestone 7.

`tokenizer.py` sits at the bottom because both the write path
(`storage` inserting a term stream) and the read path (`retrieval` building a
MATCH clause) must produce *identical* tokens. Putting it in either package would
create either a layering inversion or a circular import.

## Index flow

```
scan()                walk + filter + gitignore + hash      (pure reads)
  -> ScannedFile[]
parse_file()          ScannedFile + text -> Chunk[]         (pure)
apply_chunk_limits()  Chunk[] -> Chunk[]                    (pure)
replace_file_chunks() one transaction: files + chunks + fts
```

Only the last step writes. Because the first three are pure, an exception in
them cannot leave the database half-updated — the caller records the error and
moves on to the next file, which keeps its previous state.

### Crash recovery

The operation order is chosen so that every interruption is recoverable by
re-running `codekb index`:

1. A file is parsed in memory. A crash here changes nothing.
2. Its rows are replaced inside one `BEGIN IMMEDIATE` transaction. A crash here
   rolls back completely.

Nothing else is mutated, so there is no state that a crash can leave stale.

### Milestone 4 extension

SQLite and Qdrant cannot share a transaction. The intended order is: commit
SQLite, *then* upsert vectors, then record completion. A crash between the two
leaves chunks without vectors, which is safe — the vector store is derived data
and the next run reconciles by comparing `chunk_id` sets. Vectors are never the
source of truth for text or line numbers; they are rebuilt by re-reading SQLite.

## Retrieval flow (milestone 3)

```
text
  -> tokenizer.build_match_query      -> OR-joined quoted terms
  -> FTS5 MATCH                       -> candidate chunk_ids + bm25
  -> store.hydrate(ids)               -> authoritative text + line numbers
  -> filter (type / status)           -> final ranked list
```

Vectors are not consulted yet. Recall will be materially worse than BM25 alone
for paraphrased questions; that is expected at this milestone and is the reason
milestone 4 exists.

## Known limits

These are deliberate, and each has a trigger for revisiting it.

| Limit | Consequence | Revisit when |
| --- | --- | --- |
| `chunk_id` is UNINDEXED in FTS5, so post-filters run in Python after an over-fetch | Post-filtering can under-fill `limit` on selective queries | A repo index exceeds ~50k chunks |
| Keyword-only retrieval | Paraphrased questions miss | Milestone 4 |
| Fixed-size bigram CJK segmentation, no dictionary | Rare words split oddly; no stemming | Recall testing on real Chinese docs shows a miss |
| `.gitignore` subset (no backslash escapes, no `core.excludesFile`) | A rare pattern may be skipped; skipped lines are never treated as matches | A user reports a missed ignore |
| Python and Markdown only | Other languages fall back to unparsed text once their extension is allowlisted | Milestone: Tree-sitter |
| Whole-file rehash on every `index` | `O(bytes)` per run | Repos above ~100 MB |
| Markdown sections are flat, so a parent's context is not co-located with its children | Long documents need two hops to read top-down | Retrieval testing shows parent context being missed |