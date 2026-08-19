"""Stage 5: adverse media via a pluggable SearchProvider.

Coded from wiki/research/search-apis.md, not from memory:
- Tavily: POST https://api.tavily.com/search, header "Authorization:
  Bearer <key>", body {query, search_depth: "basic", max_results, topic}.
  Results carry title, url, and published date fields.

Fixed query template set per subject company name and each previous company
name: "<name>" fraud / insolvency / regulator / lawsuit / sanctions.
Officers are NOT media-searched in v0.1 (people search restraint).

The load-bearing separation: headlines and snippets live in the evidence
files, the casefile, and the synthesis input, where the model needs them to
judge A6. Rendered memos list source domain, published date, URL, and query
category only, because headlines about financial misconduct legitimately
contain vocabulary the memo language gate bans. Query categories therefore
carry memo-safe labels distinct from the raw query terms.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt
from tenacity.wait import wait_random_exponential

from .ch_client import FetchRecord
from .models import Evidence, Finding, MediaItem, MediaScreening
from .stages.registry import NamedRecord

STAGE = "media"
MAX_ATTEMPTS = 5
NOT_RUN_URL = "coldscreen:not-run/media"
FAILED_URL = "coldscreen:failed/media"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
SNIPPET_MAX_CHARS = 280

# Category key (memo-safe label) -> query term appended to the quoted name.
# The labels appear in rendered memos, so they must never collide with the
# banned-word list in coldscreen.language; the raw query terms only ever
# appear in evidence files and the synthesis input.
QUERY_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("misconduct", "fraud"),
    ("insolvency", "insolvency"),
    ("regulatory", "regulator"),
    ("litigation", "lawsuit"),
    ("sanctions", "sanctions"),
)


@dataclass(frozen=True)
class SearchResult:
    """One raw search hit, normalized across providers."""

    title: str
    url: str
    published: str | None = None
    source_domain: str = ""
    snippet: str | None = None


class SearchProvider(Protocol):
    """Pluggable web search, per ARCHITECTURE.md section 7."""

    def search(self, query: str, n: int = 5) -> list[SearchResult]: ...


class MediaSearchError(Exception):
    """Adverse media search failed after retries. Message is user-facing."""


class _RetryableMediaError(MediaSearchError):
    def __init__(self, status: int) -> None:
        super().__init__(f"retryable status {status} from the search API")


class _MediaTransportError(MediaSearchError):
    def __init__(self, cause: Exception) -> None:
        super().__init__(f"could not reach the search API: {cause}")


def source_domain(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host.removeprefix("www.")


def normalize_url(url: str) -> str:
    """Dedupe key: lowercased scheme and host, no fragment, no trailing slash."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = parts.netloc.lower()
    path = parts.path.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    return f"{scheme}://{host}{path}{query}"


class TavilyProvider:
    """SearchProvider over the Tavily REST API.

    Raw responses are collected on the provider and drained by the stage via
    take_records(), so every response is persisted as evidence while the
    protocol stays a plain search(query, n).
    """

    def __init__(
        self,
        api_key: str,
        timeout_seconds: float = 30.0,
        now: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleeper if sleeper is not None else time.sleep
        self._records: list[FetchRecord] = []
        self._http = httpx.Client(
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )

    def __repr__(self) -> str:  # never expose the key
        return "TavilyProvider()"

    def close(self) -> None:
        self._http.close()

    def take_records(self) -> list[FetchRecord]:
        records, self._records = self._records, []
        return records

    def search(self, query: str, n: int = 5) -> list[SearchResult]:
        body = {
            "query": query,
            "search_depth": "basic",
            "max_results": n,
            "topic": "news",
        }
        retrying = Retrying(
            retry=retry_if_exception_type((_RetryableMediaError, _MediaTransportError)),
            wait=wait_random_exponential(multiplier=1, max=30),
            stop=stop_after_attempt(MAX_ATTEMPTS),
            sleep=self._sleep,
            reraise=True,
        )
        response: httpx.Response | None = None
        for attempt in retrying:
            with attempt:
                try:
                    response = self._http.post(TAVILY_SEARCH_URL, json=body)
                except httpx.TransportError as error:
                    raise _MediaTransportError(error) from error
                if response.status_code == 429 or response.status_code >= 500:
                    raise _RetryableMediaError(response.status_code)
        assert response is not None
        retrieved_at = self._now()
        if response.status_code != 200:
            raise MediaSearchError(
                f"the search API returned HTTP {response.status_code}. Check TAVILY_API_KEY."
            )
        try:
            payload = response.json()
        except ValueError:
            raise MediaSearchError("the search API returned invalid JSON") from None
        self._records.append(
            FetchRecord(
                url=TAVILY_SEARCH_URL,
                params={"query": query, "max_results": str(n), "topic": "news"},
                status=response.status_code,
                body=payload,
                retrieved_at=retrieved_at,
            )
        )
        results: list[SearchResult] = []
        raw_results = payload.get("results") if isinstance(payload, dict) else None
        for item in raw_results or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            published = item.get("published_date") or item.get("published")
            snippet = item.get("content") or item.get("snippet")
            results.append(
                SearchResult(
                    title=str(item.get("title") or "").strip(),
                    url=url,
                    published=str(published) if published else None,
                    source_domain=source_domain(url),
                    snippet=str(snippet) if snippet else None,
                )
            )
        return results


@dataclass
class MediaStageResult:
    findings: list[Finding] = field(default_factory=list)
    records: list[NamedRecord] = field(default_factory=list)
    screening: MediaScreening = field(default_factory=lambda: MediaScreening(performed=False))
    # Set when the stage was attempted and failed after retries. The CLI
    # reports it, exits 1, and still writes the case directory with
    # everything gathered before the failure.
    failed_reason: str | None = None


def build_queries(company_name: str, previous_names: list[str]) -> list[tuple[str, str]]:
    """(category, query) pairs: 5 categories per name, subject name first."""
    names = [company_name] + [n for n in previous_names if n and n != company_name]
    queries: list[tuple[str, str]] = []
    for name in names:
        for category, term in QUERY_CATEGORIES:
            queries.append((category, f'"{name}" {term}'))
    return queries


def _synthesize_record(
    query: str, results: list[SearchResult], now: Callable[[], datetime]
) -> FetchRecord:
    """Evidence record for providers that expose no raw HTTP response."""
    return FetchRecord(
        url="coldscreen:search-results",
        params={"query": query},
        status=0,
        body={
            "kind": "search_results",
            "query": query,
            "results": [
                {
                    "title": r.title,
                    "url": r.url,
                    "published": r.published,
                    "source_domain": r.source_domain,
                    "snippet": r.snippet,
                }
                for r in results
            ],
        },
        retrieved_at=now(),
    )


def run_media(
    company_name: str,
    previous_names: list[str],
    provider: SearchProvider | None,
    provider_name: str | None,
    results_per_query: int,
    now: Callable[[], datetime],
) -> MediaStageResult:
    """Run stage 5, or record the explicit skip when no provider is configured."""
    result = MediaStageResult()

    if provider is None:
        note_record = FetchRecord(
            url=NOT_RUN_URL,
            params={},
            status=0,
            body={
                "kind": "not_run",
                "stage": "media",
                "reason": (
                    "no search API key configured; set TAVILY_API_KEY to enable"
                    " adverse media search"
                ),
            },
            retrieved_at=now(),
        )
        result.records.append(NamedRecord("media_not_run", note_record))
        result.screening = MediaScreening(
            performed=False,
            skipped_reason="no search API key configured",
        )
        result.findings.append(
            Finding(
                id="MED-000",
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=(
                    "Adverse media search not performed: no search API key"
                    " configured. Public web coverage was not screened this run."
                ),
                evidence=[
                    Evidence(
                        source_url=NOT_RUN_URL,
                        retrieved_at=note_record.retrieved_at,
                        excerpt="kind=not_run",
                    )
                ],
            )
        )
        return result

    queries = build_queries(company_name, previous_names)
    result.screening = MediaScreening(
        performed=True,
        provider=provider_name,
        results_per_query=results_per_query,
    )

    raw_counts: dict[str, int] = {category: 0 for category, _term in QUERY_CATEGORIES}
    deduped_counts: dict[str, int] = {category: 0 for category, _term in QUERY_CATEGORIES}
    seen_urls: set[str] = set()
    query_records: dict[str, list[FetchRecord]] = {c: [] for c, _t in QUERY_CATEGORIES}
    record_number = 0

    failure: MediaSearchError | None = None
    completed = 0
    for category, query in queries:
        try:
            results = provider.search(query, n=results_per_query)
        except MediaSearchError as error:
            # Failure posture: keep everything gathered so far, record the
            # failure loudly, and let the run continue to persistence.
            failure = error
            break
        taker = getattr(provider, "take_records", None)
        raw_records: list[FetchRecord] = list(taker()) if callable(taker) else []
        if not raw_records:
            raw_records = [_synthesize_record(query, results, now)]
        for record in raw_records:
            record_number += 1
            result.records.append(NamedRecord(f"media_{record_number:03d}", record))
        query_records[category].extend(raw_records)

        raw_counts[category] += len(results)
        for item in results[:results_per_query]:
            key = normalize_url(item.url)
            if key in seen_urls:
                continue
            seen_urls.add(key)
            deduped_counts[category] += 1
            snippet = item.snippet
            if snippet and len(snippet) > SNIPPET_MAX_CHARS:
                snippet = snippet[: SNIPPET_MAX_CHARS - 3] + "..."
            result.screening.items.append(
                MediaItem(
                    title=item.title,
                    url=item.url,
                    source_domain=item.source_domain or source_domain(item.url),
                    published=item.published,
                    query_category=category,
                    snippet=snippet,
                )
            )
        completed += 1

    result.screening.category_counts = raw_counts
    result.screening.category_counts_deduped = deduped_counts

    if failure is not None:
        failed_record = FetchRecord(
            url=FAILED_URL,
            params={},
            status=0,
            body={"kind": "stage_failed", "stage": "media", "error": str(failure)},
            retrieved_at=now(),
        )
        result.records.append(NamedRecord("media_failed", failed_record))
        result.screening.performed = False
        result.screening.failed = True
        result.screening.skipped_reason = "the adverse media search stage failed after retries"
        result.failed_reason = str(failure)
        result.findings.append(
            Finding(
                id="MED-999",
                stage=STAGE,
                severity="amber",
                confidence="confirmed",
                statement=(
                    "Adverse media search was attempted and FAILED after retries:"
                    f" {completed} of {len(queries)} queries completed before the"
                    " failure, and their raw results are kept in the evidence."
                    " A failed stage is not a zero-result search, and it is not"
                    " the same as a search that was never configured."
                ),
                evidence=[
                    Evidence(
                        source_url=FAILED_URL,
                        retrieved_at=failed_record.retrieved_at,
                        excerpt="kind=stage_failed",
                    )
                ],
            )
        )
        return result

    finding_index = 0
    for category, _term in QUERY_CATEGORIES:
        finding_index += 1
        records = query_records[category]
        evidence = [
            Evidence(
                source_url=r.url,
                retrieved_at=r.retrieved_at,
                excerpt=f"category={category}",
            )
            for r in records
        ]
        result.findings.append(
            Finding(
                id=f"MED-{finding_index:03d}",
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=(
                    f"Adverse media query category '{category}':"
                    f" {raw_counts[category]} result(s) returned,"
                    f" {deduped_counts[category]} kept after deduplication"
                    f" across {len(records)} query variant(s)."
                ),
                evidence=evidence,
            )
        )

    if not result.screening.items:
        all_records = [r for records in query_records.values() for r in records]
        result.findings.append(
            Finding(
                id="MED-006",
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=(
                    "Adverse media search returned no results in any query"
                    " category for the company name or its previous names."
                ),
                evidence=[
                    Evidence(
                        source_url=r.url,
                        retrieved_at=r.retrieved_at,
                        excerpt="zero results",
                    )
                    for r in all_records[:5]
                ],
            )
        )

    return result
