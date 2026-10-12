"""Shared text tokenisation for the keyword index.

This module is deliberately dependency-free: both the *write* path
(:mod:`codekb.storage.sqlite_store`, which stores a term stream) and the *read*
path (:mod:`codekb.retrieval.keyword`, which builds a MATCH clause) must agree on
exactly one tokenisation, or a query term can never equal an indexed term.

The motivating constraint is that SQLite's ``unicode61`` tokenizer does **not**
segment CJK. A run of Han characters becomes a single token, so indexing
``并设置重试上限`` stores exactly that token and the query ``重试上限`` matches
nothing - not even its own substring. Segmenting here, once, for both sides, is
what makes CJK search work at all.
"""

from __future__ import annotations

import re

#: Latin/Cyrillic/digit identifiers; dots are kept so ``a.b.c`` stays one token.
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*|\d+")
#: Han, Hiragana, Katakana, Hangul - scripts written without word separators.
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]+")

_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")

#: Sub-tokens shorter than this are noise (they would match every ``id``/``to``).
_MIN_SUBTOKEN = 3
#: CJK runs longer than this are replaced by overlapping bigrams.
_CJK_BIGRAM_THRESHOLD = 4
#: Cap on terms per *query*, to keep MATCH clauses small. Indexing is uncapped.
_MAX_QUERY_TOKENS = 48


def _split_identifier(token: str) -> list[str]:
    """``HttpClient.send_request`` -> ``['http', 'client', 'send', 'request']``."""
    parts: list[str] = []
    for segment in token.split("."):
        pieces: list[str] = []
        for word in _CAMEL_RE.findall(segment):
            pieces.extend(p for p in word.split("_") if p)
        parts.extend(pieces)
    return [part.lower() for part in parts if len(part) >= _MIN_SUBTOKEN]


def _expand_cjk(run: str) -> list[str]:
    """Short runs stand alone; longer runs become overlapping bigrams.

    Dropping the whole run for long sequences is safe: any query that would
    match it verbatim shares every one of its bigrams with the stored term
    stream, so recall is unaffected and the index stays smaller.
    """
    if len(run) <= _CJK_BIGRAM_THRESHOLD:
        return [run]
    return [run[i : i + 2] for i in range(len(run) - 1)]


def extract_tokens(text: str, limit: int | None = _MAX_QUERY_TOKENS) -> list[str]:
    """Ordered, de-duplicated, lowercased search tokens.

    Lowercasing is safe because ``unicode61`` folds case by default, and doing it
    here guarantees the indexed and queried token streams are byte-identical.
    """
    tokens: list[str] = []

    for match in _IDENTIFIER_RE.finditer(text):
        token = match.group(0)
        tokens.append(token)
        tokens.extend(_split_identifier(token))

    for match in _CJK_RE.finditer(text):
        tokens.extend(_expand_cjk(match.group(0)))

    seen: set[str] = set()
    ordered: list[str] = []
    for token in tokens:
        lowered = token.lower()
        if lowered not in seen:
            seen.add(lowered)
            ordered.append(lowered)
    return ordered if limit is None else ordered[:limit]


def index_terms(text: str) -> str:
    """The space-separated term stream stored in ``chunks_fts.content``.

    Uncapped: this runs once per write. Raw text stays in ``chunks``; the FTS
    table is a derived index and is safe to rebuild at any time.
    """
    return " ".join(extract_tokens(text, limit=None))


def quote_term(term: str) -> str:
    """Quote a term so FTS5 treats it as a literal, never as query syntax."""
    return '"' + term.replace('"', '""') + '"'


def build_match_query(text: str) -> str | None:
    """Turn free text into an FTS5 MATCH expression, or ``None`` if unusable.

    Terms are OR-ed. AND-ing bigrams would require one chunk to contain *every*
    bigram of the question, which almost no real chunk does - recall would
    collapse to zero.
    """
    tokens = extract_tokens(text)
    if not tokens:
        return None
    return " OR ".join(quote_term(token) for token in tokens)
