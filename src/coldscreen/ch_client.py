"""Companies House REST client.

Coded from wiki/research/companies-house.md, not from memory:

- Base URL https://api.company-information.service.gov.uk, HTTP basic auth
  with the API key as username and a blank password.
- Documented limit is 600 requests per 5 minute window. This client applies
  a conservative client-side throttle (default 500 per 5 minutes) plus
  tenacity exponential backoff with jitter on 429 responses, 5xx responses,
  and transport failures. A Retry-After header is honored when present but
  never relied on, because none is documented.
- Insolvency and charges endpoints are only called when the company profile
  links to them. An insolvency 404 is ambiguous upstream (no record OR no
  such company) and is never interpreted as "company not found".
- Pagination sizes and caps are configuration, not constants, because no
  official defaults or maxima are documented. The paginator advances by the
  number of items actually received, so a server that clamps page size below
  the requested value skips nothing. Truncation is reported, never silent.
  The charges list documents no query parameters; the client still walks
  pages the same way, and stops if a later page does not advance.

Terminal non-200 responses (404, 401, other 4xx) carry a FetchRecord on the
raised exception so callers can persist them as evidence. The API key
travels only in the Authorization header. It is never placed in URLs,
params, logs, cache entries, or fetch records.
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from tenacity import RetryCallState, Retrying, retry_if_exception_type, stop_after_attempt
from tenacity.wait import wait_base, wait_random_exponential

from .http_cache import HttpCache

DEFAULT_BASE_URL = "https://api.company-information.service.gov.uk"
MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class FetchRecord:
    """One raw response, ready to persist as evidence. Auth-free by design."""

    url: str
    params: dict[str, str]
    status: int
    body: Any
    retrieved_at: datetime
    from_cache: bool = False


class CompaniesHouseError(Exception):
    """Base error for registry access problems."""


class NotFoundError(CompaniesHouseError):
    """HTTP 404. Meaning depends on the endpoint; callers decide.

    Carries the response as a FetchRecord so absorbed 404s can still be
    persisted to the evidence directory.
    """

    def __init__(self, url: str, record: FetchRecord | None = None) -> None:
        super().__init__(f"resource not found: {url}")
        self.url = url
        self.record = record


class AuthError(CompaniesHouseError):
    """HTTP 401: the API rejected the key."""

    def __init__(self, url: str, record: FetchRecord | None = None) -> None:
        super().__init__(
            f"authentication failed (HTTP 401) at {url}: check COMPANIES_HOUSE_API_KEY"
        )
        self.url = url
        self.record = record


class RetryableStatusError(CompaniesHouseError):
    """HTTP 429 or 5xx, retried with backoff."""

    def __init__(self, status: int, url: str, retry_after: float | None) -> None:
        super().__init__(f"retryable status {status} from {url}")
        self.status = status
        self.url = url
        self.retry_after = retry_after


class TransportFailure(CompaniesHouseError):
    """Network-level failure (connect, timeout, and similar), retried."""

    def __init__(self, url: str, cause: Exception) -> None:
        super().__init__(f"could not reach the Companies House API at {url}: {cause}")
        self.url = url
        self.cause = cause


class InvalidResponseError(CompaniesHouseError):
    """The API returned a 200 whose body is not valid JSON."""

    def __init__(self, url: str) -> None:
        super().__init__(f"the Companies House API returned invalid JSON from {url}")
        self.url = url


class ApiClientError(CompaniesHouseError):
    """Any other non-success response."""

    def __init__(self, status: int, url: str, record: FetchRecord | None = None) -> None:
        super().__init__(f"unexpected status {status} from {url}")
        self.status = status
        self.url = url
        self.record = record


@dataclass(frozen=True)
class PaginatedResult:
    """All items collected across pages, plus every raw page response.

    hit_page_cap is True when retrieval stopped because the configured page
    cap was reached while more records remained. server_clamped is True when
    a page returned fewer items than requested while more records remained,
    which means the server enforces a smaller page size than configured.
    did_not_advance is True when a later page added no new items (for
    example the same page repeated), so the walk stopped rather than
    spinning until the page cap.
    """

    items: list[dict[str, Any]]
    records: list[FetchRecord]
    total: int | None
    truncated: bool
    hit_page_cap: bool = False
    server_clamped: bool = False
    did_not_advance: bool = False
    top_level: dict[str, Any] = field(default_factory=dict)


class Throttle:
    """Client-side sliding window limiter: at most max_requests per window."""

    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be at least 1")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._clock = clock
        self._sleep = sleeper if sleeper is not None else time.sleep
        self._sent: deque[float] = deque()

    def acquire(self) -> None:
        """Block until a request slot is free, then claim it.

        Re-checks capacity after every sleep instead of assuming one sleep
        is enough.
        """
        while True:
            now = self._clock()
            while self._sent and now - self._sent[0] >= self.window_seconds:
                self._sent.popleft()
            if len(self._sent) < self.max_requests:
                break
            wait_for = self.window_seconds - (now - self._sent[0])
            if wait_for > 0:
                self._sleep(wait_for)
        self._sent.append(self._clock())


class _RetryAfterWait(wait_base):
    """Honor Retry-After when the server sent one; jittered backoff otherwise."""

    def __init__(self) -> None:
        self._fallback = wait_random_exponential(multiplier=1, max=30)

    def __call__(self, retry_state: RetryCallState) -> float:
        outcome = retry_state.outcome
        if outcome is not None and outcome.failed:
            exc = outcome.exception()
            if isinstance(exc, RetryableStatusError) and exc.retry_after is not None:
                return max(0.0, exc.retry_after)
        return float(self._fallback(retry_state))


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _charge_item_key(item: dict[str, Any]) -> Hashable:
    """Stable identity for a charges list item.

    charge_code is the register's own id when present. Without one, the
    whole item is the key so two distinct charges are not collapsed.
    """
    code = item.get("charge_code")
    if isinstance(code, str) and code:
        return ("charge_code", code)
    return ("body", json.dumps(item, sort_keys=True, default=str))


class CompaniesHouseClient:
    """Thin, throttled, cached client over the endpoints weekend 1 needs."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        cache: HttpCache | None = None,
        throttle: Throttle | None = None,
        timeout_seconds: float = 30.0,
        now: Callable[[], datetime] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._cache = cache
        self._throttle = throttle
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleeper if sleeper is not None else time.sleep
        self._base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self._base_url,
            auth=(api_key, ""),
            timeout=timeout_seconds,
            headers={"Accept": "application/json"},
            transport=transport,
        )

    def __repr__(self) -> str:  # never expose the key
        return f"CompaniesHouseClient(base_url={self._base_url!r})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> CompaniesHouseClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- transport ---------------------------------------------------------

    def get(self, path: str, params: dict[str, str | int] | None = None) -> FetchRecord:
        """GET a JSON resource, via cache, throttle, and retry.

        Returns a FetchRecord for HTTP 200. Raises NotFoundError, AuthError,
        or ApiClientError for terminal statuses; each carries a FetchRecord
        of the response so callers can persist it as evidence.
        """
        url = self._base_url + path
        str_params = {k: str(v) for k, v in (params or {}).items()}
        if self._cache is not None:
            hit = self._cache.get(url, str_params)
            if hit is not None:
                try:
                    cached_body = json.loads(hit.body)
                except ValueError:
                    cached_body = None
                if cached_body is not None:
                    return FetchRecord(
                        url=url,
                        params=str_params,
                        status=hit.status,
                        body=cached_body,
                        retrieved_at=hit.retrieved_at,
                        from_cache=True,
                    )
        response = self._request_with_retry(url, path, str_params)
        retrieved_at = self._now()
        if response.status_code == 200:
            try:
                body = response.json()
            except ValueError:
                raise InvalidResponseError(url) from None
            record = FetchRecord(
                url=url,
                params=str_params,
                status=response.status_code,
                body=body,
                retrieved_at=retrieved_at,
            )
            if self._cache is not None:
                self._cache.put(url, str_params, response.status_code, response.text, retrieved_at)
            return record

        # Terminal non-200: build an evidence record and raise.
        try:
            error_body: Any = response.json()
        except ValueError:
            error_body = response.text
        record = FetchRecord(
            url=url,
            params=str_params,
            status=response.status_code,
            body=error_body,
            retrieved_at=retrieved_at,
        )
        if response.status_code == 404:
            raise NotFoundError(url, record=record)
        if response.status_code == 401:
            raise AuthError(url, record=record)
        raise ApiClientError(response.status_code, url, record=record)

    def _request_with_retry(self, url: str, path: str, params: dict[str, str]) -> httpx.Response:
        retrying = Retrying(
            retry=retry_if_exception_type((RetryableStatusError, TransportFailure)),
            wait=_RetryAfterWait(),
            stop=stop_after_attempt(MAX_ATTEMPTS),
            sleep=self._sleep,
            reraise=True,
        )
        for attempt in retrying:
            with attempt:
                return self._request_once(url, path, params)
        raise AssertionError("unreachable")  # pragma: no cover

    def _request_once(self, url: str, path: str, params: dict[str, str]) -> httpx.Response:
        if self._throttle is not None:
            self._throttle.acquire()
        try:
            response = self._http.get(path, params=params)
        except httpx.TransportError as error:
            raise TransportFailure(url, error) from error
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableStatusError(
                response.status_code,
                url,
                _parse_retry_after(response.headers.get("Retry-After")),
            )
        return response

    # -- pagination --------------------------------------------------------

    def get_paginated(
        self,
        path: str,
        items_per_page: int,
        max_pages: int,
        total_key: str,
        extra_params: dict[str, str | int] | None = None,
        item_key: Callable[[dict[str, Any]], Hashable] | None = None,
        stop_if_no_total: bool = False,
    ) -> PaginatedResult:
        """Collect items across pages, stopping at max_pages or the total.

        start_index advances by the number of items actually received, not by
        the requested page size, so nothing is skipped when the server clamps
        pages to a smaller size than requested.

        When item_key is set, items are kept unique by that key and the walk
        stops if a later page adds nothing new. When stop_if_no_total is set
        and the first page carries no total, one GET is enough.
        """
        items: list[dict[str, Any]] = []
        records: list[FetchRecord] = []
        total: int | None = None
        top_level: dict[str, Any] = {}
        server_clamped = False
        stopped_early = False
        did_not_advance = False
        seen_keys: set[Hashable] = set()
        pages = 0
        start_index = 0
        while pages < max_pages:
            params: dict[str, str | int] = dict(extra_params or {})
            params["items_per_page"] = items_per_page
            params["start_index"] = start_index
            record = self.get(path, params)
            records.append(record)
            pages += 1
            body = record.body if isinstance(record.body, dict) else {}
            if pages == 1:
                top_level = {k: v for k, v in body.items() if k != "items"}
            page_items = body.get("items") or []
            dict_items = [i for i in page_items if isinstance(i, dict)]
            raw_total = body.get(total_key)
            if isinstance(raw_total, int):
                total = raw_total
            if item_key is not None:
                new_items: list[dict[str, Any]] = []
                for item in dict_items:
                    key = item_key(item)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    new_items.append(item)
                if pages > 1 and not new_items:
                    did_not_advance = True
                    stopped_early = True
                    break
                items.extend(new_items)
            else:
                items.extend(dict_items)
            if not page_items:
                stopped_early = True
                break
            if total is not None and len(items) >= total:
                stopped_early = True
                break
            if stop_if_no_total and total is None:
                stopped_early = True
                break
            if len(page_items) < items_per_page:
                server_clamped = True
            start_index += len(page_items)
        truncated = total is not None and len(items) < total
        hit_page_cap = truncated and not stopped_early
        return PaginatedResult(
            items=items,
            records=records,
            total=total,
            truncated=truncated,
            hit_page_cap=hit_page_cap,
            server_clamped=server_clamped,
            did_not_advance=did_not_advance,
            top_level=top_level,
        )

    # -- endpoints (from wiki/research/companies-house.md) ------------------

    def search_companies(self, query: str, items_per_page: int = 20) -> FetchRecord:
        return self.get("/search/companies", {"q": query, "items_per_page": items_per_page})

    def company_profile(self, company_number: str) -> FetchRecord:
        return self.get(f"/company/{company_number}")

    def officers(self, company_number: str, items_per_page: int, max_pages: int) -> PaginatedResult:
        return self.get_paginated(
            f"/company/{company_number}/officers",
            items_per_page=items_per_page,
            max_pages=max_pages,
            total_key="total_results",
        )

    def pscs(self, company_number: str, items_per_page: int, max_pages: int) -> PaginatedResult:
        return self.get_paginated(
            f"/company/{company_number}/persons-with-significant-control",
            items_per_page=items_per_page,
            max_pages=max_pages,
            total_key="total_results",
        )

    def filing_history(
        self, company_number: str, items_per_page: int, max_pages: int
    ) -> PaginatedResult:
        return self.get_paginated(
            f"/company/{company_number}/filing-history",
            items_per_page=items_per_page,
            max_pages=max_pages,
            total_key="total_count",
        )

    def charges(self, company_number: str, items_per_page: int, max_pages: int) -> PaginatedResult:
        """Walk the charges list with the same received-count paginator.

        Official spec still lists no query parameters on this endpoint. The
        walk still sends items_per_page and start_index. If a later page
        adds no new charges, retrieval stops instead of repeating the same
        page until the cap. One GET is enough when the first page already
        covers total_count, or when the response carries no total.
        """
        return self.get_paginated(
            f"/company/{company_number}/charges",
            items_per_page=items_per_page,
            max_pages=max_pages,
            total_key="total_count",
            item_key=_charge_item_key,
            stop_if_no_total=True,
        )

    def insolvency(self, company_number: str) -> FetchRecord:
        # Callers must gate this on profile links.insolvency. A 404 here is
        # ambiguous by documentation and must not mean "company not found".
        return self.get(f"/company/{company_number}/insolvency")
