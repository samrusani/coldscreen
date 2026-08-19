"""Deterministic findings, including the explicit absence findings."""

from __future__ import annotations

from datetime import date
from typing import Any

import respx

from coldscreen.ch_client import FetchRecord
from coldscreen.config import Settings
from coldscreen.findings import build_findings
from coldscreen.models import CompanyProfile, InsolvencyCase, Officer
from coldscreen.stages.registry import NamedRecord, RegistryResult, run_registry_pass

from .conftest import (
    BASE_URL,
    COMPANY_NUMBER,
    SCREENED_AT,
    make_client,
    mock_company_routes,
)

TODAY = date(2026, 8, 18)
PROFILE_URL = f"{BASE_URL}/company/{COMPANY_NUMBER}"


def base_profile_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "company_name": "FABRICATED WIDGETS LTD",
        "company_number": COMPANY_NUMBER,
        "company_status": "active",
        "date_of_creation": "2019-05-14",
        "links": {"self": f"/company/{COMPANY_NUMBER}"},
    }
    data.update(overrides)
    return data


def make_result(**profile_overrides: Any) -> RegistryResult:
    data = base_profile_data(**profile_overrides)
    record = FetchRecord(
        url=PROFILE_URL, params={}, status=200, body=data, retrieved_at=SCREENED_AT
    )
    result = RegistryResult(profile=CompanyProfile.model_validate(data), profile_record=record)
    result.records.append(NamedRecord("registry_profile", record))
    return result


def by_id(result: RegistryResult, settings: Settings | None = None) -> dict[str, Any]:
    findings = build_findings(result, settings or Settings(), TODAY)
    assert all(len(f.evidence) >= 1 for f in findings)
    ids = [f.id for f in findings]
    assert len(ids) == len(set(ids))
    return {f.id: f for f in findings}


def officer(name: str, resigned_on: str | None = None) -> Officer:
    return Officer.model_validate(
        {
            "name": name,
            "officer_role": "director",
            "appointed_on": "2019-05-14",
            **({"resigned_on": resigned_on} if resigned_on else {}),
        }
    )


def test_full_fixture_run_produces_the_planted_findings(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    mock_company_routes(respx_mock)
    with make_client() as client:
        registry = run_registry_pass(client, COMPANY_NUMBER, settings)
    findings = {f.id: f for f in build_findings(registry, settings, TODAY)}

    assert findings["REG-001"].severity == "info"
    assert "active" in findings["REG-001"].statement

    assert "2019-05-14" in findings["REG-002"].statement
    assert "7 full years" in findings["REG-002"].statement

    assert findings["REG-003"].severity == "amber"  # planted: overdue accounts
    assert "REG-004" not in findings  # confirmation statement is not overdue
    assert "REG-005" not in findings

    assert findings["REG-006"].severity == "info"
    assert "1 outstanding" in findings["REG-006"].statement

    assert findings["REG-007"].severity == "info"  # planted: no insolvency link
    assert "no insolvency link" in findings["REG-007"].statement
    assert findings["REG-007"].evidence[0].source_url == PROFILE_URL

    assert "3 active officer(s)" in findings["REG-008"].statement
    assert findings["REG-009"].severity == "amber"  # planted: two recent resignations
    assert "Wholesale" in findings["REG-009"].statement

    assert "FICTIONAL WIDGETS LTD" in findings["REG-010"].statement
    assert findings["REG-011"].severity == "info"
    assert "ownership-of-shares-75-to-100-percent" in findings["REG-011"].statement


def test_liquidation_status_is_red() -> None:
    findings = by_id(make_result(company_status="liquidation"))
    assert findings["REG-001"].severity == "red"


def test_insolvency_family_statuses_are_red_including_voluntary_arrangement() -> None:
    """The R2 status leg, rubric 0.2: all five insolvency states are red."""
    for status in (
        "administration",
        "liquidation",
        "receivership",
        "insolvency-proceedings",
        "voluntary-arrangement",
    ):
        findings = by_id(make_result(company_status=status))
        assert findings["REG-001"].severity == "red", status


def test_dissolved_status_is_amber() -> None:
    findings = by_id(make_result(company_status="dissolved"))
    assert findings["REG-001"].severity == "amber"


def test_not_active_statuses_are_amber_per_a7() -> None:
    """The A7 statuses, rubric 0.2: dissolved, closed, converted-closed,
    removed are all amber."""
    for status in ("dissolved", "closed", "converted-closed", "removed"):
        findings = by_id(make_result(company_status=status))
        assert findings["REG-001"].severity == "amber", status


def test_status_severity_matching_is_case_insensitive() -> None:
    """A mixed-case status still lands in its family; the statement keeps
    the registry's own spelling."""
    red = by_id(make_result(company_status="Liquidation"))
    assert red["REG-001"].severity == "red"
    assert "Liquidation" in red["REG-001"].statement
    amber = by_id(make_result(company_status="Dissolved"))
    assert amber["REG-001"].severity == "amber"


def test_charges_truncation_is_an_explicit_finding() -> None:
    """total_count above the retrieved item count records REG-015."""
    from coldscreen.models import Charge

    result = make_result()
    result.charges_link_present = True
    result.charges = [Charge.model_validate({"status": "outstanding"})]
    result.charges_total = 7
    findings = by_id(result)
    truncation = findings["REG-015"]
    assert truncation.severity == "info"
    assert "retrieved 1 of 7 charge(s)" in truncation.statement
    assert "no pagination" in truncation.statement


def test_no_charges_truncation_finding_when_counts_agree() -> None:
    from coldscreen.models import Charge

    result = make_result()
    result.charges_link_present = True
    result.charges = [Charge.model_validate({"status": "outstanding"})]
    result.charges_total = 1
    findings = by_id(result)
    assert "REG-015" not in findings


def test_missing_charges_link_yields_absence_finding_citing_profile() -> None:
    findings = by_id(make_result())
    finding = findings["REG-006"]
    assert finding.severity == "info"
    assert "no" in finding.statement.lower()
    assert finding.evidence[0].source_url == PROFILE_URL


def test_missing_insolvency_link_yields_absence_finding_citing_profile() -> None:
    findings = by_id(make_result())
    finding = findings["REG-007"]
    assert finding.severity == "info"
    assert finding.evidence[0].source_url == PROFILE_URL


def test_missing_psc_link_yields_absence_finding_citing_profile() -> None:
    findings = by_id(make_result())
    finding = findings["REG-011"]
    assert finding.statement == (
        "No persons with significant control entries are linked on the register for this company."
    )
    assert finding.confidence == "indicated"
    assert finding.evidence[0].source_url == PROFILE_URL


def test_psc_endpoint_returning_zero_items_stays_confirmed() -> None:
    result = make_result()
    result.pscs_link_present = True
    result.pscs = []
    result.records.append(
        NamedRecord(
            "psc_p1",
            FetchRecord(
                url=f"{PROFILE_URL}/persons-with-significant-control",
                params={},
                status=200,
                body={"total_results": 0, "items": []},
                retrieved_at=SCREENED_AT,
            ),
        )
    )
    findings = by_id(result)
    finding = findings["REG-011"]
    assert finding.confidence == "confirmed"
    assert finding.statement == "The PSC register returned no entries."


def test_psc_404_behind_a_link_is_indicated_not_confirmed() -> None:
    result = make_result()
    result.pscs_link_present = True
    result.pscs = []
    result.records.append(
        NamedRecord(
            "psc_p1",
            FetchRecord(
                url=f"{PROFILE_URL}/persons-with-significant-control",
                params={},
                status=404,
                body="",
                retrieved_at=SCREENED_AT,
            ),
        )
    )
    findings = by_id(result)
    finding = findings["REG-011"]
    assert finding.confidence == "indicated"
    assert "404" in finding.statement


def test_charges_link_with_unretrievable_resource_is_flagged_not_dropped() -> None:
    result = make_result()
    result.charges_link_present = True
    result.charges = None
    result.records.append(
        NamedRecord(
            "charges",
            FetchRecord(
                url=f"{PROFILE_URL}/charges",
                params={},
                status=404,
                body="",
                retrieved_at=SCREENED_AT,
            ),
        )
    )
    findings = by_id(result)
    finding = findings["REG-006"]
    assert finding.severity == "amber"
    assert finding.confidence == "unverified"
    assert "404" in finding.statement
    assert finding.evidence[0].source_url == f"{PROFILE_URL}/charges"


def test_insolvency_cases_present_is_red() -> None:
    result = make_result()
    result.insolvency_link_present = True
    result.insolvency_cases = [
        InsolvencyCase.model_validate({"number": 1, "type": "creditors-voluntary-liquidation"})
    ]
    result.records.append(
        NamedRecord(
            "insolvency",
            FetchRecord(
                url=f"{PROFILE_URL}/insolvency",
                params={},
                status=200,
                body={"cases": [{"number": 1}]},
                retrieved_at=SCREENED_AT,
            ),
        )
    )
    findings = by_id(result)
    assert findings["REG-007"].severity == "red"
    assert "creditors-voluntary-liquidation" in findings["REG-007"].statement


def test_insolvency_link_with_unretrievable_resource_is_flagged_not_dropped() -> None:
    result = make_result()
    result.insolvency_link_present = True
    result.insolvency_cases = None
    findings = by_id(result)
    finding = findings["REG-007"]
    assert finding.severity == "amber"
    assert finding.confidence == "unverified"
    assert "404" in finding.statement


def test_active_officer_count_prefers_the_registry_total() -> None:
    """The register's active_count wins over the fetched list length, which
    can be smaller under truncation."""
    result = make_result()
    result.officers = [officer("WIDGETSMITH, Wanda"), officer("COGWHEEL, Cornelius")]
    result.officers_top_level = {"active_count": 7}
    findings = by_id(result)
    assert "7 active officer(s)" in findings["REG-008"].statement


def test_zero_resignations_is_recorded_explicitly() -> None:
    result = make_result()
    result.officers = [officer("WIDGETSMITH, Wanda"), officer("COGWHEEL, Cornelius")]
    findings = by_id(result)
    assert "0 officer resignation(s)" in findings["REG-009"].statement
    assert findings["REG-009"].severity == "info"


def test_wholesale_change_threshold_uses_half_of_active_count() -> None:
    result = make_result()
    result.officers = [officer(f"ACTIVE, Number {i}") for i in range(10)] + [
        officer("LEAVER, One", resigned_on="2026-03-01"),
        officer("LEAVER, Two", resigned_on="2026-05-15"),
    ]
    findings = by_id(result)
    # 2 resignations against threshold max(2, 10 / 2) = 5: not wholesale.
    assert findings["REG-009"].severity == "info"

    result_small = make_result()
    result_small.officers = [
        officer("ACTIVE, Only"),
        officer("LEAVER, One", resigned_on="2026-03-01"),
        officer("LEAVER, Two", resigned_on="2026-05-15"),
    ]
    findings_small = by_id(result_small)
    # 2 resignations against threshold max(2, 1 / 2) = 2: wholesale.
    assert findings_small["REG-009"].severity == "amber"


def test_resignations_outside_twelve_months_do_not_count() -> None:
    result = make_result()
    result.officers = [
        officer("ACTIVE, Only"),
        officer("LEAVER, Old", resigned_on="2024-01-01"),
        officer("LEAVER, Older", resigned_on="2023-01-01"),
    ]
    findings = by_id(result)
    assert findings["REG-009"].severity == "info"
    assert "0 officer resignation(s)" in findings["REG-009"].statement


def test_truncated_filing_history_states_counts_and_cause() -> None:
    result = make_result()
    result.filings_truncated = True
    result.filings_total = 250
    result.filings_page_cap_hit = True
    findings = by_id(result)
    statement = findings["REG-012"].statement
    assert "retrieved 0 of 250 records" in statement
    assert "page cap" in statement
    assert "smaller pages" not in statement


def test_truncation_from_server_clamping_names_the_server_not_the_cap() -> None:
    result = make_result()
    result.filings_truncated = True
    result.filings_total = 250
    result.filings_page_cap_hit = False
    result.filings_server_clamped = True
    findings = by_id(result)
    statement = findings["REG-012"].statement
    assert "retrieved 0 of 250 records" in statement
    assert "page cap" not in statement
    assert "smaller pages than requested" in statement


def test_no_truncation_findings_when_nothing_is_truncated() -> None:
    findings = by_id(make_result())
    assert "REG-012" not in findings
    assert "REG-013" not in findings
    assert "REG-014" not in findings
