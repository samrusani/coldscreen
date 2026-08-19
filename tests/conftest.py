"""Shared test scaffolding. Every test runs offline against fixtures.

Offline is enforced, not assumed: pytest runs with sockets disabled
(pytest-socket via addopts), and the site stage's DNS resolver is stubbed
below to fail loudly, because getaddrinfo does not go through a socket
object and would otherwise slip past the socket guard.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import respx

import coldscreen.site
from coldscreen.ch_client import CompaniesHouseClient
from coldscreen.config import Settings

FIXTURES_DIR = Path(__file__).parent / "fixtures"
BASE_URL = "https://api.company-information.service.gov.uk"
TEST_API_KEY = "fixture-key-0123456789-not-a-real-key"
SCREENED_AT = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
COMPANY_NUMBER = "99999999"

# A globally routable address for stubbed resolutions: tests that want a
# site fetch to proceed resolve every fictional host to this. It sits just
# above the CGNAT block, so it exercises the "public address" path.
PUBLIC_TEST_ADDRESS = "100.128.0.10"


def load_fixture(name: str) -> dict[str, Any]:
    payload = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


@pytest.fixture(autouse=True)
def _scrub_ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient key, model, clock override, or COLDSCREEN_* setting may
    leak into any test.

    Tests that need one of these set it explicitly after this scrub runs.
    """
    for name in (
        "COMPANIES_HOUSE_API_KEY",
        "OPENSANCTIONS_API_KEY",
        "OPENSANCTIONS_BASE_URL",
        "TAVILY_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OLLAMA_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in list(os.environ):
        if name.startswith("COLDSCREEN_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real DNS is banned in tests, mechanically.

    pytest-socket blocks socket creation but not getaddrinfo, so the site
    stage's default resolver is replaced with one that fails the test. A
    test that needs resolution to succeed monkeypatches
    coldscreen.site.system_resolver (or passes a resolver) itself.
    """

    def _refuse(host: str) -> list[str]:
        raise AssertionError(f"real DNS resolution attempted in a test (host {host!r})")

    monkeypatch.setattr(coldscreen.site, "system_resolver", _refuse)


@pytest.fixture
def resolve_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every host to one public address, for tests that fetch."""
    monkeypatch.setattr(coldscreen.site, "system_resolver", lambda host: [PUBLIC_TEST_ADDRESS])


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def frozen_now() -> Callable[[], datetime]:
    return lambda: SCREENED_AT


def make_client(
    cache: Any = None,
    throttle: Any = None,
    sleeper: Callable[[float], None] | None = None,
) -> CompaniesHouseClient:
    return CompaniesHouseClient(
        TEST_API_KEY,
        base_url=BASE_URL,
        cache=cache,
        throttle=throttle,
        now=lambda: SCREENED_AT,
        sleeper=sleeper or (lambda _seconds: None),
    )


def mock_company_routes(router: respx.MockRouter, ambiguous_search: bool = False) -> None:
    """Register every fixture endpoint. No insolvency route: no link, no call.

    Covers the weekend 2 network expansion too: appointments for the three
    current officers and an empty disqualified-officers search. Officers 4
    and 5 are resigned, so any appointments call for them would be unmocked
    and fail the test, which is exactly the property we want.
    """
    search_fixture = "search_ambiguous.json" if ambiguous_search else "search_single.json"
    router.get(f"{BASE_URL}/search/companies").respond(200, json=load_fixture(search_fixture))
    router.get(f"{BASE_URL}/company/{COMPANY_NUMBER}").respond(
        200, json=load_fixture("profile.json")
    )
    router.get(f"{BASE_URL}/company/{COMPANY_NUMBER}/officers").respond(
        200, json=load_fixture("officers.json")
    )
    router.get(f"{BASE_URL}/company/{COMPANY_NUMBER}/persons-with-significant-control").respond(
        200, json=load_fixture("psc.json")
    )
    router.get(f"{BASE_URL}/company/{COMPANY_NUMBER}/filing-history").respond(
        200, json=load_fixture("filing_history.json")
    )
    router.get(f"{BASE_URL}/company/{COMPANY_NUMBER}/charges").respond(
        200, json=load_fixture("charges.json")
    )
    router.get(f"{BASE_URL}/officers/fictOfficer001/appointments").respond(
        200, json=load_fixture("appointments_officer1.json")
    )
    router.get(f"{BASE_URL}/officers/fictOfficer002/appointments").respond(
        200, json=load_fixture("appointments_officer2.json")
    )
    router.get(f"{BASE_URL}/officers/fictOfficer003/appointments").respond(
        200, json=load_fixture("appointments_officer3.json")
    )
    router.get(f"{BASE_URL}/search/disqualified-officers").respond(
        200, json=load_fixture("disqualified_search_empty.json")
    )
