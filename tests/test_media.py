"""Stage 5: query expansion, dedupe, findings, key hygiene, memo guarantee."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from coldscreen.media import (
    QUERY_CATEGORIES,
    TAVILY_SEARCH_URL,
    MediaSearchError,
    SearchResult,
    TavilyProvider,
    build_queries,
    normalize_url,
    run_media,
    source_domain,
)
from coldscreen.models import CaseFile, CompanyProfile
from coldscreen.render import render_memo
from coldscreen.rubric import detect_candidates
from coldscreen.synthesis import build_synthesis_input, serialize_input

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
TAVILY_KEY = "tvly-fixture-key-not-real"


class FakeSearchProvider:
    """SearchProvider without take_records: the stage synthesizes evidence."""

    def __init__(self, results_by_query: dict[str, list[SearchResult]]) -> None:
        self.results_by_query = results_by_query
        self.queries: list[str] = []

    def search(self, query: str, n: int = 5) -> list[SearchResult]:
        self.queries.append(query)
        return self.results_by_query.get(query, [])[:n]


def result_for(url: str, title: str = "A fictional headline") -> SearchResult:
    return SearchResult(
        title=title,
        url=url,
        published="2026-05-01",
        source_domain=source_domain(url),
        snippet="A fictional snippet.",
    )


# -- helpers -------------------------------------------------------------------


def test_query_templates_cover_all_categories_and_previous_names() -> None:
    queries = build_queries("CURRENT NAME LTD", ["OLD NAME LTD"])
    assert len(queries) == 10
    assert queries[0] == ("misconduct", '"CURRENT NAME LTD" fraud')
    assert ("insolvency", '"CURRENT NAME LTD" insolvency') in queries
    assert ("regulatory", '"CURRENT NAME LTD" regulator') in queries
    assert ("litigation", '"CURRENT NAME LTD" lawsuit') in queries
    assert ("sanctions", '"CURRENT NAME LTD" sanctions') in queries
    assert ("misconduct", '"OLD NAME LTD" fraud') in queries


def test_category_labels_are_memo_safe() -> None:
    from coldscreen.language import find_banned_terms

    for category, _term in QUERY_CATEGORIES:
        assert find_banned_terms(category) == []


def test_url_normalization() -> None:
    assert normalize_url("HTTPS://Example.COM/Path/") == "https://example.com/Path"
    assert normalize_url("https://example.com/a#frag") == "https://example.com/a"
    assert normalize_url("https://example.com/a?q=1") == "https://example.com/a?q=1"
    assert source_domain("https://www.example.com/a") == "example.com"


# -- run_media -----------------------------------------------------------------


def test_results_are_deduped_across_queries_and_counted(respx_mock: respx.MockRouter) -> None:
    shared = result_for("https://fictional-gazette.example/story-one")
    provider = FakeSearchProvider(
        {
            '"SUBJECT LTD" fraud': [shared, result_for("https://other.example/two")],
            # The same URL surfaces again under another category: deduped.
            '"SUBJECT LTD" lawsuit': [
                SearchResult(
                    title=shared.title,
                    url="https://fictional-gazette.example/story-one/",
                    source_domain="fictional-gazette.example",
                )
            ],
        }
    )
    result = run_media("SUBJECT LTD", [], provider, "fake", 5, lambda: NOW)
    assert result.screening.performed is True
    assert len(result.screening.items) == 2
    assert result.screening.category_counts["misconduct"] == 2
    assert result.screening.category_counts_deduped["misconduct"] == 2
    assert result.screening.category_counts["litigation"] == 1
    assert result.screening.category_counts_deduped["litigation"] == 0
    med001 = next(f for f in result.findings if f.id == "MED-001")
    assert "2 result(s) returned, 2 kept" in med001.statement
    med004 = next(f for f in result.findings if f.id == "MED-004")
    assert "1 result(s) returned, 0 kept" in med004.statement


def test_top_n_per_query_is_enforced(respx_mock: respx.MockRouter) -> None:
    many = [result_for(f"https://example.example/{n}") for n in range(10)]
    provider = FakeSearchProvider({'"SUBJECT LTD" fraud': many})
    result = run_media("SUBJECT LTD", [], provider, "fake", 3, lambda: NOW)
    assert len(result.screening.items) == 3


def test_zero_results_everywhere_is_an_absence_finding(respx_mock: respx.MockRouter) -> None:
    provider = FakeSearchProvider({})
    result = run_media("SUBJECT LTD", [], provider, "fake", 5, lambda: NOW)
    med006 = next(f for f in result.findings if f.id == "MED-006")
    assert "no results in any query category" in med006.statement
    # Five per-category findings plus the absence finding.
    assert len(result.findings) == 6


def test_every_query_produces_a_persisted_record(respx_mock: respx.MockRouter) -> None:
    provider = FakeSearchProvider({})
    result = run_media("SUBJECT LTD", ["OLD LTD"], provider, "fake", 5, lambda: NOW)
    names = [r.name for r in result.records]
    assert names == [f"media_{n:03d}" for n in range(1, 11)]
    assert all(r.record.body["kind"] == "search_results" for r in result.records)


def test_no_key_is_a_first_class_skip(respx_mock: respx.MockRouter) -> None:
    result = run_media("SUBJECT LTD", [], None, None, 5, lambda: NOW)
    assert result.screening.performed is False
    assert result.screening.failed is False
    finding = next(f for f in result.findings if f.id == "MED-000")
    assert "not performed" in finding.statement
    record = next(r for r in result.records if r.name == "media_not_run")
    assert record.record.body["kind"] == "not_run"


class FailingAfterFirstProvider:
    """One successful query, then MediaSearchError forever."""

    def __init__(self, first_results: list[SearchResult]) -> None:
        self.first_results = first_results
        self.calls = 0

    def search(self, query: str, n: int = 5) -> list[SearchResult]:
        self.calls += 1
        if self.calls == 1:
            return self.first_results[:n]
        raise MediaSearchError("the search API returned HTTP 401. Check TAVILY_API_KEY.")


def test_stage_failure_keeps_gathered_results_and_records_med_999(
    respx_mock: respx.MockRouter,
) -> None:
    """Failure posture: a mid-run failure keeps everything gathered, adds a
    MED-999 finding clearly distinct from the not-run wording, and reports
    the reason for the CLI to surface."""
    provider = FailingAfterFirstProvider([result_for("https://fictional.example/kept-story")])
    result = run_media("SUBJECT LTD", [], provider, "fake", 5, lambda: NOW)
    assert provider.calls == 2  # first query succeeded, second failed
    assert result.failed_reason is not None
    assert "401" in result.failed_reason
    assert result.screening.performed is False
    assert result.screening.failed is True
    assert [i.url for i in result.screening.items] == ["https://fictional.example/kept-story"]
    finding = next(f for f in result.findings if f.id == "MED-999")
    assert finding.severity == "amber"
    assert "attempted and FAILED" in finding.statement
    assert "1 of 5 queries completed" in finding.statement
    record = next(r for r in result.records if r.name == "media_failed")
    assert record.record.body["kind"] == "stage_failed"
    # The evidence record for the completed query is preserved too.
    assert any(r.name == "media_001" for r in result.records)


# -- the no-headline-in-memo guarantee -------------------------------------------


def test_headline_vocabulary_never_reaches_the_memo(respx_mock: respx.MockRouter) -> None:
    """A title containing banned vocabulary lives in evidence and synthesis
    input, and never in the rendered memo."""
    loaded_title = "Fictional company implicated in fraud proceedings"
    provider = FakeSearchProvider(
        {'"SUBJECT LTD" fraud': [result_for("https://fictional.example/story", loaded_title)]}
    )
    media = run_media("SUBJECT LTD", [], provider, "fake", 5, lambda: NOW)
    casefile = CaseFile(
        subject=CompanyProfile.model_validate(
            {"company_name": "SUBJECT LTD", "company_number": "99999904"}
        ),
        findings=media.findings,
        media=media.screening,
        tool_version="0.1.0.dev0",
        screened_at=NOW,
    )
    # In the casefile and in the synthesis input: yes.
    assert loaded_title in casefile.model_dump_json()
    synthesis_input = serialize_input(build_synthesis_input(casefile, detect_candidates(casefile)))
    assert loaded_title in synthesis_input
    # In the rendered memo: never, and no banned vocabulary either.
    memo = render_memo(casefile)
    assert loaded_title not in memo
    assert "fraud" not in memo.lower()
    assert "https://fictional.example/story" in memo


# -- TavilyProvider over respx ----------------------------------------------------


def test_tavily_request_shape_and_response_parsing(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(TAVILY_SEARCH_URL).respond(
        200,
        json={
            "query": '"SUBJECT LTD" fraud',
            "results": [
                {
                    "title": "A fictional story",
                    "url": "https://www.fictional.example/story",
                    "content": "Fictional content.",
                    "score": 0.91,
                    "published_date": "2026-04-30",
                }
            ],
        },
    )
    provider = TavilyProvider(TAVILY_KEY, now=lambda: NOW)
    results = provider.search('"SUBJECT LTD" fraud', n=4)
    provider.close()
    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {TAVILY_KEY}"
    body = json.loads(request.content.decode("utf-8"))
    assert body == {
        "query": '"SUBJECT LTD" fraud',
        "search_depth": "basic",
        "max_results": 4,
        "topic": "news",
    }
    assert len(results) == 1
    assert results[0].title == "A fictional story"
    assert results[0].published == "2026-04-30"
    assert results[0].source_domain == "fictional.example"
    assert results[0].snippet == "Fictional content."


def test_tavily_records_are_drained_by_take_records(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(TAVILY_SEARCH_URL).respond(200, json={"results": []})
    provider = TavilyProvider(TAVILY_KEY, now=lambda: NOW)
    provider.search("q one", n=5)
    records = provider.take_records()
    assert len(records) == 1
    assert records[0].url == TAVILY_SEARCH_URL
    assert provider.take_records() == []
    # The key never lands in the record.
    assert TAVILY_KEY not in json.dumps(records[0].params)
    assert TAVILY_KEY not in repr(provider)
    provider.close()


def test_tavily_429_is_retried(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(TAVILY_SEARCH_URL)
    route.side_effect = [
        httpx.Response(429),
        httpx.Response(200, json={"results": []}),
    ]
    provider = TavilyProvider(TAVILY_KEY, now=lambda: NOW, sleeper=lambda _s: None)
    provider.search("q", n=5)
    provider.close()
    assert route.call_count == 2


def test_tavily_terminal_error_names_the_key_variable(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(TAVILY_SEARCH_URL).respond(401)
    provider = TavilyProvider(TAVILY_KEY, now=lambda: NOW)
    with pytest.raises(MediaSearchError, match="TAVILY_API_KEY"):
        provider.search("q", n=5)
    provider.close()
