"""Companies House client: auth, throttle, retry, pagination, caching.

All network traffic is mocked with respx. Nothing here touches the network.
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest
import respx

from coldscreen.ch_client import (
    ApiClientError,
    AuthError,
    InvalidResponseError,
    NotFoundError,
    RetryableStatusError,
    Throttle,
    TransportFailure,
)
from coldscreen.http_cache import HttpCache

from .conftest import BASE_URL, COMPANY_NUMBER, TEST_API_KEY, load_fixture, make_client

PROFILE_URL = f"{BASE_URL}/company/{COMPANY_NUMBER}"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_basic_auth_key_as_username_blank_password(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(PROFILE_URL).respond(200, json=load_fixture("profile.json"))
    with make_client() as client:
        record = client.company_profile(COMPANY_NUMBER)
    assert record.status == 200
    expected = "Basic " + base64.b64encode(f"{TEST_API_KEY}:".encode()).decode()
    sent = route.calls.last.request.headers["Authorization"]
    assert sent == expected


def test_fetch_record_never_contains_the_key(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(PROFILE_URL).respond(200, json=load_fixture("profile.json"))
    with make_client() as client:
        record = client.company_profile(COMPANY_NUMBER)
        assert TEST_API_KEY not in repr(client)
    assert TEST_API_KEY not in record.url
    assert TEST_API_KEY not in str(record.params)
    assert TEST_API_KEY not in str(record.body)


def test_429_is_retried_until_success(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(PROFILE_URL)
    route.side_effect = [
        httpx.Response(429),
        httpx.Response(200, json=load_fixture("profile.json")),
    ]
    with make_client() as client:
        record = client.company_profile(COMPANY_NUMBER)
    assert record.status == 200
    assert route.call_count == 2


def test_retry_after_header_is_honored(respx_mock: respx.MockRouter) -> None:
    naps: list[float] = []
    route = respx_mock.get(PROFILE_URL)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, json=load_fixture("profile.json")),
    ]
    with make_client(sleeper=naps.append) as client:
        client.company_profile(COMPANY_NUMBER)
    assert naps == [7.0]


def test_5xx_is_retried_with_backoff(respx_mock: respx.MockRouter) -> None:
    naps: list[float] = []
    route = respx_mock.get(PROFILE_URL)
    route.side_effect = [
        httpx.Response(502),
        httpx.Response(503),
        httpx.Response(200, json=load_fixture("profile.json")),
    ]
    with make_client(sleeper=naps.append) as client:
        record = client.company_profile(COMPANY_NUMBER)
    assert record.status == 200
    assert route.call_count == 3
    assert len(naps) == 2
    assert all(nap >= 0.0 for nap in naps)


def test_persistent_429_gives_up_after_max_attempts(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(PROFILE_URL).respond(429)
    with make_client() as client, pytest.raises(RetryableStatusError):
        client.company_profile(COMPANY_NUMBER)
    assert route.call_count == 5


def test_404_raises_not_found_without_retry(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(PROFILE_URL).respond(404)
    with make_client() as client, pytest.raises(NotFoundError):
        client.company_profile(COMPANY_NUMBER)
    assert route.call_count == 1


def test_404_exception_carries_the_response_record(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(PROFILE_URL).respond(404, json={"error": "company-profile-not-found"})
    with make_client() as client, pytest.raises(NotFoundError) as excinfo:
        client.company_profile(COMPANY_NUMBER)
    record = excinfo.value.record
    assert record is not None
    assert record.status == 404
    assert record.body == {"error": "company-profile-not-found"}
    assert record.url == PROFILE_URL
    assert TEST_API_KEY not in str(record)


def test_401_raises_auth_error_naming_the_key_variable(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(PROFILE_URL).respond(401)
    with make_client() as client, pytest.raises(AuthError) as excinfo:
        client.company_profile(COMPANY_NUMBER)
    assert route.call_count == 1  # not retryable
    assert "COMPANIES_HOUSE_API_KEY" in str(excinfo.value)


def test_other_4xx_raises_client_error(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(PROFILE_URL).respond(403)
    with make_client() as client, pytest.raises(ApiClientError):
        client.company_profile(COMPANY_NUMBER)


def test_invalid_json_on_200_is_a_clean_error(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(PROFILE_URL).respond(200, content=b"<html>maintenance page</html>")
    with make_client() as client, pytest.raises(InvalidResponseError) as excinfo:
        client.company_profile(COMPANY_NUMBER)
    assert PROFILE_URL in str(excinfo.value)


def test_transport_error_is_retried_then_succeeds(respx_mock: respx.MockRouter) -> None:
    naps: list[float] = []
    route = respx_mock.get(PROFILE_URL)
    route.side_effect = [
        httpx.ConnectError("connection refused"),
        httpx.Response(200, json=load_fixture("profile.json")),
    ]
    with make_client(sleeper=naps.append) as client:
        record = client.company_profile(COMPANY_NUMBER)
    assert record.status == 200
    assert route.call_count == 2
    assert len(naps) == 1


def test_transport_errors_exhaust_to_transport_failure(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(PROFILE_URL).mock(side_effect=httpx.ConnectError("host down"))
    with make_client() as client, pytest.raises(TransportFailure) as excinfo:
        client.company_profile(COMPANY_NUMBER)
    assert route.call_count == 5
    assert "could not reach the Companies House API" in str(excinfo.value)


def test_cache_hit_skips_the_network(tmp_path: Path, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(PROFILE_URL).respond(200, json=load_fixture("profile.json"))
    cache = HttpCache(tmp_path / "cache.sqlite3")
    with make_client(cache=cache) as client:
        first = client.company_profile(COMPANY_NUMBER)
        second = client.company_profile(COMPANY_NUMBER)
    cache.close()
    assert route.call_count == 1
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.body == first.body
    assert second.retrieved_at == first.retrieved_at


def test_cached_entries_never_contain_the_key(tmp_path: Path, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(PROFILE_URL).respond(200, json=load_fixture("profile.json"))
    cache_path = tmp_path / "cache.sqlite3"
    cache = HttpCache(cache_path)
    with make_client(cache=cache) as client:
        client.company_profile(COMPANY_NUMBER)
    cache.close()
    assert TEST_API_KEY.encode() not in cache_path.read_bytes()


def test_pagination_collects_all_pages_and_flags_truncation(
    respx_mock: respx.MockRouter,
) -> None:
    url = f"{BASE_URL}/company/{COMPANY_NUMBER}/officers"
    page = {
        "total_results": 5,
        "items": [{"name": "WIDGETSMITH, Wanda"}, {"name": "COGWHEEL, Cornelius"}],
    }
    route = respx_mock.get(url).respond(200, json=page)
    with make_client() as client:
        capped = client.officers(COMPANY_NUMBER, items_per_page=2, max_pages=2)
    assert route.call_count == 2
    assert len(capped.items) == 4
    assert capped.total == 5
    assert capped.truncated is True
    assert capped.hit_page_cap is True
    assert capped.server_clamped is False
    assert [c.request.url.params["start_index"] for c in route.calls] == ["0", "2"]


def test_pagination_advances_by_items_received_not_requested(
    respx_mock: respx.MockRouter,
) -> None:
    """Regression: a server that clamps page size must not cause skipping.

    We request 100 per page; the server returns 50 per page with total 200.
    All 200 records must arrive, in order, with start_index following the
    received counts.
    """
    url = f"{BASE_URL}/company/{COMPANY_NUMBER}/officers"

    def clamped_pages(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start_index"])
        page = [{"name": f"OFFICER, Number {i:03d}"} for i in range(start, min(start + 50, 200))]
        return httpx.Response(200, json={"total_results": 200, "items": page})

    route = respx_mock.get(url).mock(side_effect=clamped_pages)
    with make_client() as client:
        result = client.officers(COMPANY_NUMBER, items_per_page=100, max_pages=10)
    assert route.call_count == 4
    assert [c.request.url.params["start_index"] for c in route.calls] == [
        "0",
        "50",
        "100",
        "150",
    ]
    assert result.items == [{"name": f"OFFICER, Number {i:03d}"} for i in range(200)]
    assert result.total == 200
    assert result.truncated is False
    assert result.hit_page_cap is False
    assert result.server_clamped is True


def test_pagination_stops_at_total_without_truncation(respx_mock: respx.MockRouter) -> None:
    url = f"{BASE_URL}/company/{COMPANY_NUMBER}/officers"
    respx_mock.get(url).respond(200, json=load_fixture("officers.json"))
    with make_client() as client:
        result = client.officers(COMPANY_NUMBER, items_per_page=100, max_pages=10)
    assert len(result.items) == 5
    assert result.total == 5
    assert result.truncated is False
    assert len(result.records) == 1


def test_throttle_sleeps_when_the_window_is_full() -> None:
    clock = FakeClock()
    naps: list[float] = []

    def sleeper(seconds: float) -> None:
        naps.append(seconds)
        clock.now += seconds

    throttle = Throttle(2, 300.0, clock=clock, sleeper=sleeper)
    throttle.acquire()
    clock.now = 10.0
    throttle.acquire()
    clock.now = 20.0
    throttle.acquire()
    assert naps == [280.0]


def test_throttle_does_not_sleep_once_the_window_has_passed() -> None:
    clock = FakeClock()
    naps: list[float] = []
    throttle = Throttle(2, 300.0, clock=clock, sleeper=naps.append)
    throttle.acquire()
    clock.now = 301.0
    throttle.acquire()
    clock.now = 302.0
    throttle.acquire()
    assert naps == []
