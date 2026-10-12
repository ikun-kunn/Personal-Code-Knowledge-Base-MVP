---
id: pattern-retry
type: implementation-pattern
status: verified
scope:
  - src/service.py
tags:
  - retry
  - backoff
updated: 2026-10-12
---

# Bounded retry with exponential backoff

## Applicable conditions

A request may fail transiently and the caller can retry safely.

## Recommended approach

Cap the number of attempts and double the delay each time.

## Verification

See `src/service.py` and the retry tests.