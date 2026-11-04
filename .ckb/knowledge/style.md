---
kind: rule
status: active
title: Knowledge base coding conventions
summary: Python naming, CLI and core boundaries, errors, tests, and output contracts.
scope: **
---

## Naming
Use snake_case for modules, functions, and variables; PascalCase for data classes. Name public commands after user actions.

## Layers
Keep indexing, retrieval, and validation in `ckb/core.py`. Keep argument parsing and presentation in `ckb/cli.py`. The core must not print or depend on command-line arguments.

## Errors
Raise `ValueError` for invalid user input and malformed cards. Catch expected input and I/O errors at the CLI boundary and print one actionable error. Avoid silently accepting malformed metadata.

## Tests
Test indexing, updates, deletion, Chinese retrieval, scope filtering, and context limits with temporary directories. Run `python -m unittest discover -s tests -v` before delivery.

## APIs
Public core functions accept a project `Path` and return data. `search` returns source-linked `Entry` values; `context` returns bounded Markdown. CLI `--json` emits valid UTF-8 JSON without extra progress text.
