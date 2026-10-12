# ADR-001: Start with SQLite + FTS5 and no vector database

- **Status:** Accepted
- **Date:** 2026-10-12
- **Milestone:** 1–3

## Context

CodeKB must retrieve code and documentation with accurate `path:line` citations,
and later combine keyword and semantic search. The plan named Qdrant for vectors
and suggested running it locally from the first version.

Two constraints decided this:

1. **The core loop must be verifiable with no model service.** If `index` and
   `search` depend on a running Ollama or an embedding provider, then every
   change to scanning, parsing, chunking or ranking is untestable unless those
   services are up. That makes the first milestone unverifiable in the most
   literal sense.
2. **Budget.** A separate vector service is another thing to install, run, back
   up and secure, before a single useful search has been run.

## Decision

Milestones 1–3 use SQLite alone, with FTS5 for keyword retrieval. Qdrant is
introduced in milestone 4 behind a `VectorStore` interface, in local embedded
mode (`qdrant-client` persistence) — not as a Docker service.

Vectors are derived data. SQLite remains the only source of truth for text, line
numbers and provenance; a vector is always re-hydratable from a `chunk_id`.

## Consequences

**Gained**

- The full loop — scan, parse, chunk, index, search — runs with **zero runtime
  dependencies** and **zero API keys**. Milestone 3 is genuinely usable on its
  own, which is the plan's stated acceptance bar.
- 143 tests run in ~2.5s with no services. Parsing and chunking bugs are caught
  in seconds, not minutes.
- One artefact to back up: a SQLite file.
- BM25 ranking is deterministic and inspectable, which makes retrieval bugs
  debuggable. A vector index is far harder to reason about when it goes wrong.

**Accepted costs**

- **Recall is weaker.** Paraphrased questions ("how do I avoid retrying
  forever?") miss a document that says "exponential backoff cap". This is the
  single biggest limitation of milestone 3 and the reason milestone 4 exists.
- **A migration happens later.** Adding Qdrant means writing vector upserts into
  the index path. Mitigated by keeping `VectorStore` a narrow interface and by
  committing SQLite *before* writing vectors, so the failure mode is "chunks
  without vectors" (safe, repaired by re-indexing) rather than the reverse.
- **`.gitignore` needed reimplementation.** FTS5 gave no help here, so a
  documented subset matcher was written (`ingestion/scanner.py`) rather than
  adding `pathspec` or shelling out to `git`. The subset skips unparseable
  patterns rather than treating them as matches, on the principle that an
  imperfect matcher must never silently hide files.

## Why not the alternatives

| Option | Why not |
| --- | --- |
| Qdrant from day one | Nothing to verify without embeddings; adds a service and a failure mode before the core loop exists |
| A real embedding DB from day one | Requires a chunking decision that can only be validated by retrieval quality; premature |
| Pure Python BM25, no SQLite | Re-implements persistence, durability and incremental updates that SQLite already solves correctly |
| LangChain / LlamaIndex | Hides scanning, chunking, ranking and context assembly — exactly the parts the project needs to own. Revisit in milestone 5 only if a concrete need appears |
| `pathspec` for `.gitignore` | One dependency for a feature Git already implements; the subset is ~50 lines and fully tested |

## Revisit triggers

- Index exceeds ~50k chunks → the post-MATCH over-fetch in `keyword.py` becomes
  the bottleneck.
- A real retrieval eval set (milestone 6) shows BM25 missing questions that
  embedding search would answer → that is the empirical case for milestone 4.
- A second process needs concurrent write access → move to a server-mode vector
  store and review SQLite's WAL suitability.