"""Shared test scaffolding. Every test runs offline against fixtures."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import respx

from firstpass.ch_client import CompaniesHouseClient
from firstpass.config import Settings

FIXTURES_DIR = Path(__file__).parent / "fixtures"
BASE_URL = "https://api.company-information.service.gov.uk"
TEST_API_KEY = "fixture-key-0123456789-not-a-real-key"
SCREENED_AT = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
COMPANY_NUMBER = "99999999"


def load_fixture(name: str) -> dict[str, Any]:
    payload = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


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
    """Register every fixture endpoint. No insolvency route: no link, no call."""
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
