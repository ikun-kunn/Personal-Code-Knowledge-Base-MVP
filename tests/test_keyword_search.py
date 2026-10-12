"""Keyword retrieval: tokenisation, query building, FTS5 behaviour, filtering."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from codekb.models import Chunk, ChunkStatus, FileKind, ScannedFile, SourceType
from codekb.retrieval.keyword import (
    KeywordSearchOptions,
    build_match_query,
    extract_tokens,
    search_keyword,
)
from codekb.storage.sqlite_store import SqliteStore

# --------------------------------------------------------------------------- #
# Tokenisation
# --------------------------------------------------------------------------- #


def test_identifier_is_kept_whole() -> None:
    # Tokens are lowercased: unicode61 folds case, and both sides of the
    # comparison must use the identical form.
    assert "modelprovidertimeouterror" in extract_tokens("ModelProviderTimeoutError")


def test_snake_case_is_split_into_subtokens() -> None:
    tokens = extract_tokens("retry_request")
    assert "retry_request" in tokens
    assert "retry" in tokens
    assert "request" in tokens


def test_camel_case_is_split_into_subtokens() -> None:
    tokens = extract_tokens("HttpClient")
    assert "http" in tokens
    assert "client" in tokens


def test_dotted_identifier_keeps_full_and_parts() -> None:
    tokens = extract_tokens("codekb.providers.Chat.generate")
    assert "codekb.providers.chat.generate" in tokens
    assert "generate" in tokens
    assert "providers" in tokens


def test_short_subtokens_are_dropped() -> None:
    # "id" and "of" are too short to be useful signals.
    tokens = extract_tokens("value_of_id")
    assert "id" not in tokens
    assert "value" in tokens


def test_short_cjk_run_is_kept_whole() -> None:
    """A run at or below the threshold needs no segmentation."""
    assert extract_tokens("重试上限") == ["重试上限"]


def test_long_cjk_run_produces_bigrams() -> None:
    tokens = extract_tokens("模型请求失败之后")
    assert "模型" in tokens
    assert "请求" in tokens
    assert "之后" in tokens
    # The whole run is dropped: any query matching it verbatim shares its bigrams.
    assert "模型请求失败之后" not in tokens


def test_mixed_language_query() -> None:
    tokens = extract_tokens("retry 请求超时")
    assert "retry" in tokens
    assert "请求超时" in tokens


def test_query_with_no_tokens_returns_none() -> None:
    assert build_match_query("") is None
    assert build_match_query("!!! ???") is None


def test_query_terms_are_or_joined() -> None:
    match = build_match_query("alpha beta")
    assert match is not None
    assert match.count(" OR ") == 1


def test_query_syntax_characters_cannot_leak_into_the_match_clause() -> None:
    """FTS5 operators in user text must become literal terms, never syntax."""
    match = build_match_query('foo"bar AND (baz NEAR "qux"')
    assert match is not None
    # Every term is wrapped in quotes, so the only way a bare operator could
    # survive is if quoting failed.
    assert re.fullmatch(r'("[^"]*"(?: OR "[^"]*")*)+', match), match
    assert "NEAR" not in match  # operators are matched literally, not executed


def test_search_treats_operator_text_as_literal(indexed: SqliteStore) -> None:
    """A query made of FTS5 syntax must not raise; it just finds nothing."""
    for hostile in ["AND OR NOT", 'NEAR("a" "b")', "^foo", "col:value", "a*"]:
        assert isinstance(search_keyword(indexed, hostile), list)


def test_token_count_is_capped() -> None:
    assert len(extract_tokens(" ".join(f"tok{i}" for i in range(500)))) <= 48


# --------------------------------------------------------------------------- #
# Search against a real index
# --------------------------------------------------------------------------- #


@pytest.fixture
def indexed(store: SqliteStore, tmp_path: Path) -> SqliteStore:
    repo = store.upsert_repository(tmp_path, "demo")

    def add(rel_path: str, chunks: list[Chunk], kind: FileKind = FileKind.PYTHON) -> None:
        scanned = ScannedFile(
            abs_path=Path(rel_path),
            rel_path=rel_path,
            kind=kind,
            sha256=rel_path,
            size_bytes=100,
            source_type=chunks[0].source_type,
        )
        store.replace_file_chunks(repo, scanned, chunks)

    add(
        "src/retry.py",
        [
            Chunk(
                source_type=SourceType.CODE,
                content="class ModelProviderTimeoutError(Exception):\n    pass",
                start_line=1,
                end_line=2,
                title="ModelProviderTimeoutError",
                symbol_name="ModelProviderTimeoutError",
            ),
            Chunk(
                source_type=SourceType.CODE,
                content="def exponential_backoff(attempt):\n    return 2 ** attempt",
                start_line=10,
                end_line=11,
                title="exponential_backoff",
                symbol_name="exponential_backoff",
            ),
        ],
    )
    add(
        "knowledge/patterns/retry.md",
        [
            Chunk(
                source_type=SourceType.KNOWLEDGE,
                content="模型请求失败之后，应避免无限重试，并设置重试上限。",
                start_line=1,
                end_line=1,
                title="重试上限",
                status=ChunkStatus.VERIFIED,
                tags=("retry",),
            )
        ],
        kind=FileKind.MARKDOWN,
    )
    add(
        "knowledge/patterns/old.md",
        [
            Chunk(
                source_type=SourceType.KNOWLEDGE,
                content="旧的请求重试策略，仅供参考。",
                start_line=1,
                end_line=1,
                title="旧策略",
                status=ChunkStatus.DEPRECATED,
            )
        ],
        kind=FileKind.MARKDOWN,
    )
    return store


def test_finds_exact_class_name(indexed: SqliteStore) -> None:
    hits = search_keyword(indexed, "ModelProviderTimeoutError")
    assert hits
    assert hits[0].symbol_name == "ModelProviderTimeoutError"
    assert hits[0].rel_path == "src/retry.py"


def test_finds_identifier_via_subtoken(indexed: SqliteStore) -> None:
    """Searching `backoff` must find `exponential_backoff`."""
    hits = search_keyword(indexed, "backoff")
    assert any(hit.symbol_name == "exponential_backoff" for hit in hits)


def test_finds_cjk_content(indexed: SqliteStore) -> None:
    hits = search_keyword(indexed, "重试上限")
    assert hits
    assert any(hit.rel_path == "knowledge/patterns/retry.md" for hit in hits)


def test_cjk_query_matches_reordered_wording(indexed: SqliteStore) -> None:
    """Bigram expansion makes a differently-worded question still match."""
    hits = search_keyword(indexed, "如何避免无限重试")
    assert any(hit.rel_path == "knowledge/patterns/retry.md" for hit in hits)


def test_results_are_ranked_not_arbitrary(indexed: SqliteStore) -> None:
    hits = search_keyword(indexed, "retry backoff")
    assert len(hits) >= 2
    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True)


def test_hits_carry_keyword_origin(indexed: SqliteStore) -> None:
    hits = search_keyword(indexed, "retry")
    assert hits[0].origins["keyword"] == 1


def test_empty_query_returns_nothing(indexed: SqliteStore) -> None:
    assert search_keyword(indexed, "") == []
    assert search_keyword(indexed, "!!!") == []


def test_no_match_returns_empty(indexed: SqliteStore) -> None:
    assert search_keyword(indexed, "zzzzznotpresentzzzzz") == []


def test_limit_is_respected(indexed: SqliteStore) -> None:
    assert len(search_keyword(indexed, "retry", KeywordSearchOptions(limit=1))) == 1


def test_source_type_filter(indexed: SqliteStore) -> None:
    hits = search_keyword(
        indexed, "重试", KeywordSearchOptions(source_types=frozenset({SourceType.KNOWLEDGE}))
    )
    assert hits
    assert all(hit.source_type is SourceType.KNOWLEDGE for hit in hits)


def test_status_filter_excludes_deprecated(indexed: SqliteStore) -> None:
    hits = search_keyword(
        indexed,
        "请求重试",
        KeywordSearchOptions(statuses=frozenset({ChunkStatus.VERIFIED})),
    )
    assert all(hit.status is ChunkStatus.VERIFIED for hit in hits)


def test_search_against_empty_index(store: SqliteStore) -> None:
    assert search_keyword(store, "anything") == []


def test_hits_carry_repo_id(indexed: SqliteStore, tmp_path: Path) -> None:
    hits = search_keyword(indexed, "retry")
    assert hits
    assert all(hit.repo_id > 0 for hit in hits)


def test_repo_id_filter_actually_restricts(indexed: SqliteStore, tmp_path: Path) -> None:
    """Regression: the option existed but was never applied to hits."""
    repo_id = indexed.upsert_repository(tmp_path / "demo", "demo")
    scanned = ScannedFile(
        abs_path=Path("other.py"),
        rel_path="other.py",
        kind=FileKind.PYTHON,
        sha256="other",
        size_bytes=10,
    )
    indexed.replace_file_chunks(
        repo_id,
        scanned,
        [
            Chunk(
                source_type=SourceType.CODE,
                content="def retry_other_repository(): pass",
                start_line=1,
                end_line=1,
                title="retry_other_repository",
            )
        ],
    )

    everything = search_keyword(indexed, "retry", KeywordSearchOptions(limit=50))
    mine = search_keyword(indexed, "retry", KeywordSearchOptions(limit=50, repo_id=repo_id))
    assert len(mine) < len(everything)
    assert all(hit.repo_id == repo_id for hit in mine)


def test_repo_id_filter_isolates_completely(indexed: SqliteStore, tmp_path: Path) -> None:
    """A repo_id that matches nothing must yield nothing, not everything."""
    hits = search_keyword(indexed, "retry", KeywordSearchOptions(repo_id=999_999))
    assert hits == []


def test_hydrated_content_matches_line_range(indexed: SqliteStore, demo_repo: Path) -> None:
    """Every returned hit must be quotable verbatim from its source file."""
    hits = search_keyword(indexed, "exponential_backoff")
    assert hits
    source = (demo_repo / "src" / "service.py").read_text(encoding="utf-8").split("\n")
    for hit in hits:
        expected = "\n".join(source[hit.start_line - 1 : hit.end_line])
        if hit.rel_path == "src/service.py":
            assert hit.content == expected
