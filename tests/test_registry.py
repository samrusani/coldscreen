"""Stage 2 gating: links decide what gets fetched; insolvency 404 is absorbed."""

from __future__ import annotations

from typing import Any

import respx

from coldscreen.config import Settings
from coldscreen.stages.registry import run_registry_pass

from .conftest import BASE_URL, COMPANY_NUMBER, load_fixture, make_client

PROFILE_URL = f"{BASE_URL}/company/{COMPANY_NUMBER}"


def profile_with_links(*names: str) -> dict[str, Any]:
    profile = load_fixture("profile.json")
    links = {"self": f"/company/{COMPANY_NUMBER}"}
    for name in names:
        path = "persons-with-significant-control" if name == "psc" else name
        key = "persons_with_significant_control" if name == "psc" else name
        links[key] = f"/company/{COMPANY_NUMBER}/{path}"
    for key in ("officers", "filing_history"):
        links.setdefault(key, f"/company/{COMPANY_NUMBER}/{key.replace('_', '-')}")
    profile["links"] = links
    return profile


def register_base_routes(router: respx.MockRouter, profile: dict[str, Any]) -> None:
    router.get(PROFILE_URL).respond(200, json=profile)
    router.get(f"{PROFILE_URL}/officers").respond(200, json=load_fixture("officers.json"))
    router.get(f"{PROFILE_URL}/filing-history").respond(
        200, json=load_fixture("filing_history.json")
    )


def test_no_links_means_no_charges_psc_or_insolvency_calls(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    # respx fails the test on any unmocked request, so the absence of charges,
    # PSC, and insolvency routes proves those endpoints were never called.
    register_base_routes(respx_mock, profile_with_links())
    with make_client() as client:
        result = run_registry_pass(client, COMPANY_NUMBER, settings)
    assert result.charges is None
    assert result.pscs is None
    assert result.insolvency_cases is None
    assert result.insolvency_link_present is False


def test_insolvency_link_triggers_the_call_and_parses_cases(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    register_base_routes(respx_mock, profile_with_links("insolvency"))
    insolvency_route = respx_mock.get(f"{PROFILE_URL}/insolvency").respond(
        200,
        json={
            "cases": [
                {
                    "number": 1,
                    "type": "creditors-voluntary-liquidation",
                    "dates": [{"type": "wound-up-on", "date": "2026-01-05"}],
                }
            ],
            "status": ["liquidation"],
        },
    )
    with make_client() as client:
        result = run_registry_pass(client, COMPANY_NUMBER, settings)
    assert insolvency_route.call_count == 1
    assert result.insolvency_link_present is True
    assert result.insolvency_cases is not None
    assert result.insolvency_cases[0].type == "creditors-voluntary-liquidation"
    assert result.first_record("insolvency") is not None


def test_insolvency_404_is_absorbed_never_company_not_found(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    register_base_routes(respx_mock, profile_with_links("insolvency"))
    respx_mock.get(f"{PROFILE_URL}/insolvency").respond(404)
    with make_client() as client:
        result = run_registry_pass(client, COMPANY_NUMBER, settings)
    # The pass completes: a 404 on insolvency is ambiguous upstream and must
    # never abort the screen or be read as "company does not exist".
    assert result.profile.company_number == COMPANY_NUMBER
    assert result.insolvency_link_present is True
    assert result.insolvency_cases is None
    # The 404 itself is evidence and lands in the audit pack.
    record = result.first_record("insolvency")
    assert record is not None
    assert record.status == 404
    assert record.url == f"{PROFILE_URL}/insolvency"


def test_charges_404_behind_a_link_is_absorbed_and_persisted(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    register_base_routes(respx_mock, profile_with_links("charges"))
    respx_mock.get(f"{PROFILE_URL}/charges").respond(404)
    with make_client() as client:
        result = run_registry_pass(client, COMPANY_NUMBER, settings)
    assert result.charges_link_present is True
    assert result.charges is None
    record = result.first_record("charges")
    assert record is not None
    assert record.status == 404


def test_psc_link_triggers_the_call(respx_mock: respx.MockRouter, settings: Settings) -> None:
    register_base_routes(respx_mock, profile_with_links("psc"))
    psc_route = respx_mock.get(f"{PROFILE_URL}/persons-with-significant-control").respond(
        200, json=load_fixture("psc.json")
    )
    with make_client() as client:
        result = run_registry_pass(client, COMPANY_NUMBER, settings)
    assert psc_route.call_count == 1
    assert result.pscs is not None and len(result.pscs) == 1


def test_officers_404_is_treated_as_empty_and_persisted(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    profile = profile_with_links()
    respx_mock.get(PROFILE_URL).respond(200, json=profile)
    respx_mock.get(f"{PROFILE_URL}/officers").respond(404)
    respx_mock.get(f"{PROFILE_URL}/filing-history").respond(404)
    with make_client() as client:
        result = run_registry_pass(client, COMPANY_NUMBER, settings)
    assert result.officers == []
    assert result.filings == []
    officers_404 = result.first_record("officers_p1")
    assert officers_404 is not None and officers_404.status == 404
    filings_404 = result.first_record("filing_history_p1")
    assert filings_404 is not None and filings_404.status == 404
