"""Stage 3: appointments fan-out, overlap, disqualification match tiers."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import respx

from firstpass.ch_client import FetchRecord
from firstpass.config import Settings
from firstpass.models import PSC, Officer
from firstpass.stages.network import (
    normalize_person_name,
    officer_id_from_links,
    run_network_expansion,
)

from .conftest import BASE_URL, make_client

TODAY = date(2026, 8, 18)
SUBJECT = "99999999"
RETRIEVED = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)

FALLBACK = FetchRecord(
    url=f"{BASE_URL}/company/{SUBJECT}/officers",
    params={},
    status=200,
    body={},
    retrieved_at=RETRIEVED,
)


def officer(name: str, officer_id: str, dob: dict[str, int] | None = None) -> Officer:
    payload: dict[str, Any] = {
        "name": name,
        "officer_role": "director",
        "appointed_on": "2020-01-01",
        "links": {"officer": {"appointments": f"/officers/{officer_id}/appointments"}},
    }
    if dob:
        payload["date_of_birth"] = dob
    return Officer.model_validate(payload)


def appointments_payload(
    name: str, companies: Sequence[tuple[str, str, str | None]]
) -> dict[str, Any]:
    """companies: (name, number, resigned_on) triples."""
    items = []
    for company_name, number, resigned_on in companies:
        item: dict[str, Any] = {
            "name": name,
            "officer_role": "director",
            "appointed_on": "2020-01-01",
            "appointed_to": {
                "company_name": company_name,
                "company_number": number,
                "company_status": "active",
            },
        }
        if resigned_on:
            item["resigned_on"] = resigned_on
        items.append(item)
    return {"total_results": len(items), "items": items}


def empty_search() -> dict[str, Any]:
    return {"total_results": 0, "items": []}


def disq_search_item(title: str, officer_id: str, kind_path: str = "natural") -> dict[str, Any]:
    return {
        "title": title,
        "address_snippet": "1 Fictional Row, Faketown",
        "links": {"self": f"/disqualified-officers/{kind_path}/{officer_id}"},
        "kind": "searchresults#disqualified-officer",
    }


def natural_detail(
    forename: str,
    surname: str,
    dob: str,
    until: str,
    start: str = "2020-02-01",
    permissions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "forename": forename,
        "surname": surname,
        "date_of_birth": dob,
        "disqualifications": [
            {
                "disqualified_from": start,
                "disqualified_until": until,
                "reason": {"act": "company-directors-disqualification-act-1986"},
            }
        ],
    }
    if permissions is not None:
        detail["permissions_to_act"] = permissions
    return detail


# -- unit helpers -------------------------------------------------------------


def test_officer_id_extraction() -> None:
    assert (
        officer_id_from_links({"officer": {"appointments": "/officers/abc123/appointments"}})
        == "abc123"
    )
    assert officer_id_from_links({}) is None
    assert officer_id_from_links({"officer": {}}) is None
    assert officer_id_from_links({"officer": {"appointments": "/not/the/shape"}}) is None


def test_name_normalization_ignores_order_case_punctuation_honorifics() -> None:
    assert normalize_person_name("WIDGETSMITH, Wanda") == normalize_person_name("Wanda Widgetsmith")
    assert normalize_person_name("Ms Wanda Widgetsmith") == normalize_person_name(
        "WIDGETSMITH, Wanda"
    )
    assert normalize_person_name("O'Brien, Seamus") == normalize_person_name("Seamus O Brien")
    assert normalize_person_name("Dr. Ada LOVELESS") == ("ada", "loveless")


# -- appointments fan-out and overlap -----------------------------------------


def test_appointments_fan_out_counts_and_evidence(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    respx_mock.get(f"{BASE_URL}/officers/fictNet001/appointments").respond(
        200,
        json=appointments_payload(
            "GRINDSTONE, Gertrude",
            [
                ("FABRICATED WIDGETS LTD", SUBJECT, None),
                ("OTHERWORK LTD", "99999810", None),
                ("FORMER HAUNT LTD", "99999811", "2021-01-01"),
            ],
        ),
    )
    respx_mock.get(f"{BASE_URL}/search/disqualified-officers").respond(200, json=empty_search())
    with make_client() as client:
        result = run_network_expansion(
            client,
            SUBJECT,
            [officer("GRINDSTONE, Gertrude", "fictNet001")],
            [],
            settings,
            TODAY,
            FALLBACK,
        )
    summary = result.expansion.appointments[0]
    # The subject itself and the resigned appointment are both excluded.
    assert summary.other_current_appointments == 1
    assert summary.companies == ["OTHERWORK LTD (99999810)"]
    names = [r.name for r in result.records]
    assert "appointments_grindstone-gertrude_p1" in names
    assert "disqualified_search_grindstone-gertrude" in names
    net001 = next(f for f in result.findings if f.id == "NET-001")
    assert "1 current appointment(s) at other companies" in net001.statement


def test_co_appointment_overlap_is_found_and_listed(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    shared = [("SHARED SIDECAR LTD", "99999812", None)]
    respx_mock.get(f"{BASE_URL}/officers/fictNet001/appointments").respond(
        200, json=appointments_payload("GRINDSTONE, Gertrude", shared)
    )
    respx_mock.get(f"{BASE_URL}/officers/fictNet002/appointments").respond(
        200, json=appointments_payload("MILLWRIGHT, Magnus", shared)
    )
    respx_mock.get(f"{BASE_URL}/search/disqualified-officers").respond(200, json=empty_search())
    with make_client() as client:
        result = run_network_expansion(
            client,
            SUBJECT,
            [
                officer("GRINDSTONE, Gertrude", "fictNet001"),
                officer("MILLWRIGHT, Magnus", "fictNet002"),
            ],
            [],
            settings,
            TODAY,
            FALLBACK,
        )
    assert len(result.expansion.overlaps) == 1
    overlap = result.expansion.overlaps[0]
    assert overlap.company_number == "99999812"
    assert overlap.officer_names == ["GRINDSTONE, Gertrude", "MILLWRIGHT, Magnus"]
    net002 = next(f for f in result.findings if f.id == "NET-002")
    assert "SHARED SIDECAR LTD (99999812)" in net002.statement
    # The deterministic substrate never claims nondisclosure.
    assert "undisclosed" not in net002.statement.lower()


def test_no_overlap_is_an_explicit_absence_finding(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    respx_mock.get(f"{BASE_URL}/officers/fictNet001/appointments").respond(
        200, json=appointments_payload("GRINDSTONE, Gertrude", [])
    )
    respx_mock.get(f"{BASE_URL}/search/disqualified-officers").respond(200, json=empty_search())
    with make_client() as client:
        result = run_network_expansion(
            client,
            SUBJECT,
            [officer("GRINDSTONE, Gertrude", "fictNet001")],
            [],
            settings,
            TODAY,
            FALLBACK,
        )
    net002 = next(f for f in result.findings if f.id == "NET-002")
    assert net002.severity == "info"
    assert "No co-appointment overlap" in net002.statement


def test_officer_without_appointments_link_is_reported_not_dropped(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    respx_mock.get(f"{BASE_URL}/search/disqualified-officers").respond(200, json=empty_search())
    linkless = Officer.model_validate({"name": "NOLINK, Norbert", "officer_role": "director"})
    with make_client() as client:
        result = run_network_expansion(client, SUBJECT, [linkless], [], settings, TODAY, FALLBACK)
    net001 = next(f for f in result.findings if f.id == "NET-001")
    assert "no appointments link" in net001.statement


# -- disqualification tiers ----------------------------------------------------


def _disq_routes(
    respx_mock: respx.MockRouter,
    search_items: list[dict[str, Any]],
    detail: dict[str, Any] | None = None,
    detail_path: str = "/disqualified-officers/natural/fictDisq001",
) -> None:
    respx_mock.get(f"{BASE_URL}/officers/fictNet001/appointments").respond(
        200, json=appointments_payload("GRINDSTONE, Gertrude", [])
    )
    respx_mock.get(f"{BASE_URL}/search/disqualified-officers").respond(
        200, json={"total_results": len(search_items), "items": search_items}
    )
    if detail is not None:
        respx_mock.get(f"{BASE_URL}{detail_path}").respond(200, json=detail)


def _run_single(settings: Settings) -> Any:
    with make_client() as client:
        return run_network_expansion(
            client,
            SUBJECT,
            [officer("GRINDSTONE, Gertrude", "fictNet001", dob={"month": 4, "year": 1970})],
            [],
            settings,
            TODAY,
            FALLBACK,
        )


def test_strong_active_disqualification_is_a_red_finding(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    _disq_routes(
        respx_mock,
        [disq_search_item("Gertrude GRINDSTONE", "fictDisq001")],
        natural_detail("Gertrude", "GRINDSTONE", "1970-04-02", until="2031-01-01"),
    )
    result = _run_single(settings)
    finding = next(f for f in result.findings if f.id == "NET-101")
    assert finding.severity == "red"
    assert "currently active disqualification" in finding.statement
    assert "2031-01-01" in finding.statement
    assert result.expansion.disqualification_checks[0].outcome == "strong_active"
    names = [r.name for r in result.records]
    assert "disqualified_record_grindstone-gertrude_1" in names


def test_strong_expired_disqualification_is_info_with_dates(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    _disq_routes(
        respx_mock,
        [disq_search_item("Gertrude GRINDSTONE", "fictDisq001")],
        natural_detail(
            "Gertrude", "GRINDSTONE", "1970-04-02", start="2019-02-01", until="2025-02-01"
        ),
    )
    result = _run_single(settings)
    finding = next(f for f in result.findings if f.id == "NET-101")
    assert finding.severity == "info"
    assert "EXPIRED" in finding.statement
    assert "2019-02-01 to 2025-02-01" in finding.statement
    assert result.expansion.disqualification_checks[0].outcome == "strong_expired"


def test_affirmative_dob_mismatch_is_likely_a_different_person(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    # Same normalized name, different birth year: never red, and worded as
    # a mismatch, not as "could not be compared".
    _disq_routes(
        respx_mock,
        [disq_search_item("Gertrude GRINDSTONE", "fictDisq001")],
        natural_detail("Gertrude", "GRINDSTONE", "1971-04-02", until="2031-01-01"),
    )
    result = _run_single(settings)
    finding = next(f for f in result.findings if f.id == "NET-101")
    assert finding.severity == "info"
    assert finding.confidence == "indicated"
    assert "the dates of birth differ" in finding.statement
    assert "likely a different person" in finding.statement
    assert "could not be confirmed" not in finding.statement
    check = result.expansion.disqualification_checks[0]
    assert check.outcome == "mismatch"
    assert check.detail == "date of birth differs"


def test_uncomparable_dob_is_name_only_and_requires_manual_check(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    # The detail record carries no parseable date of birth: the tiers
    # degrade to name-only, which still asks for a manual check.
    detail = natural_detail("Gertrude", "GRINDSTONE", "1970-04-02", until="2031-01-01")
    del detail["date_of_birth"]
    _disq_routes(
        respx_mock,
        [disq_search_item("Gertrude GRINDSTONE", "fictDisq001")],
        detail,
    )
    result = _run_single(settings)
    finding = next(f for f in result.findings if f.id == "NET-101")
    assert finding.severity == "info"
    assert finding.confidence == "unverified"
    assert "manual check" in finding.statement
    assert "date of birth could not be confirmed" in finding.statement
    assert result.expansion.disqualification_checks[0].outcome == "name_only"


def test_missing_officer_dob_can_never_be_strong(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    _disq_routes(
        respx_mock,
        [disq_search_item("Gertrude GRINDSTONE", "fictDisq001")],
        natural_detail("Gertrude", "GRINDSTONE", "1970-04-02", until="2031-01-01"),
    )
    with make_client() as client:
        result = run_network_expansion(
            client,
            SUBJECT,
            [officer("GRINDSTONE, Gertrude", "fictNet001", dob=None)],
            [],
            settings,
            TODAY,
            FALLBACK,
        )
    finding = next(f for f in result.findings if f.id == "NET-101")
    assert finding.severity == "info"
    assert result.expansion.disqualification_checks[0].outcome == "name_only"


def test_no_match_is_an_absence_finding(respx_mock: respx.MockRouter, settings: Settings) -> None:
    _disq_routes(respx_mock, [])
    result = _run_single(settings)
    finding = next(f for f in result.findings if f.id == "NET-101")
    assert finding.severity == "info"
    assert "No disqualification record matches" in finding.statement
    assert result.expansion.disqualification_checks[0].outcome == "none"


def test_search_hits_with_other_names_are_not_candidates(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    # The search fuzzy-matches; only exact normalized-name hits are followed.
    _disq_routes(respx_mock, [disq_search_item("Gertrude GRINDLEWALD", "fictDisq002")])
    result = _run_single(settings)
    finding = next(f for f in result.findings if f.id == "NET-101")
    assert "No disqualification record matches" in finding.statement
    names = [r.name for r in result.records]
    assert not any(name.startswith("disqualified_record_") for name in names)


def test_individual_pscs_are_checked_and_corporate_pscs_are_not(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    search_route = respx_mock.get(f"{BASE_URL}/search/disqualified-officers").respond(
        200, json=empty_search()
    )
    individual = PSC.model_validate(
        {"name": "Ms Hermione Handwheel", "kind": "individual-person-with-significant-control"}
    )
    corporate = PSC.model_validate(
        {"name": "OPAQUE HOLDCO LTD", "kind": "corporate-entity-person-with-significant-control"}
    )
    with make_client() as client:
        result = run_network_expansion(
            client, SUBJECT, [], [individual, corporate], settings, TODAY, FALLBACK
        )
    assert search_route.call_count == 1
    checks = result.expansion.disqualification_checks
    assert [c.subject for c in checks] == ["Ms Hermione Handwheel"]
    assert checks[0].role == "psc"


def test_active_disqualification_with_permission_to_act_is_stated(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    _disq_routes(
        respx_mock,
        [disq_search_item("Gertrude GRINDSTONE", "fictDisq001")],
        natural_detail(
            "Gertrude",
            "GRINDSTONE",
            "1970-04-02",
            until="2031-01-01",
            permissions=[
                {
                    "company_names": ["PERMITTED VENTURES LTD"],
                    "granted_on": "2024-05-01",
                    "expires_on": "2031-01-01",
                }
            ],
        ),
    )
    result = _run_single(settings)
    finding = next(f for f in result.findings if f.id == "NET-101")
    assert finding.severity == "red"
    assert "permission(s) to act" in finding.statement


# -- corporate officer disqualification tiers -----------------------------------


def corporate_officer(name: str, officer_id: str, registration_number: str | None) -> Officer:
    payload: dict[str, Any] = {
        "name": name,
        "officer_role": "corporate-director",
        "appointed_on": "2020-01-01",
        "links": {"officer": {"appointments": f"/officers/{officer_id}/appointments"}},
    }
    if registration_number is not None:
        payload["identification"] = {"registration_number": registration_number}
    return Officer.model_validate(payload)


def corporate_detail(
    name: str, company_number: str, until: str, start: str = "2020-02-01"
) -> dict[str, Any]:
    return {
        "name": name,
        "company_number": company_number,
        "country_of_registration": "England",
        "disqualifications": [
            {
                "disqualified_from": start,
                "disqualified_until": until,
                "reason": {"act": "company-directors-disqualification-act-1986"},
            }
        ],
    }


def _run_corporate(settings: Settings, registration_number: str | None) -> Any:
    with make_client() as client:
        return run_network_expansion(
            client,
            SUBJECT,
            [corporate_officer("OPAQUE HOLDCO LTD", "fictNet001", registration_number)],
            [],
            settings,
            TODAY,
            FALLBACK,
        )


def _corporate_routes(respx_mock: respx.MockRouter, detail: dict[str, Any]) -> None:
    _disq_routes(
        respx_mock,
        [disq_search_item("OPAQUE HOLDCO LTD", "fictCorp001", kind_path="corporate")],
        detail,
        detail_path="/disqualified-officers/corporate/fictCorp001",
    )


def test_corporate_strong_active_match_is_a_red_finding(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    _corporate_routes(
        respx_mock, corporate_detail("OPAQUE HOLDCO LTD", "99999820", until="2031-01-01")
    )
    result = _run_corporate(settings, "99999820")
    finding = next(f for f in result.findings if f.id == "NET-101")
    assert finding.severity == "red"
    assert "currently active disqualification" in finding.statement
    assert "registered company number" in finding.statement
    assert result.expansion.disqualification_checks[0].outcome == "strong_active"


def test_corporate_strong_expired_match_is_info_with_dates(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    _corporate_routes(
        respx_mock,
        corporate_detail("OPAQUE HOLDCO LTD", "99999820", start="2019-02-01", until="2025-02-01"),
    )
    result = _run_corporate(settings, "99999820")
    finding = next(f for f in result.findings if f.id == "NET-101")
    assert finding.severity == "info"
    assert "EXPIRED" in finding.statement
    assert "2019-02-01 to 2025-02-01" in finding.statement
    assert result.expansion.disqualification_checks[0].outcome == "strong_expired"


def test_corporate_number_mismatch_is_likely_a_different_company(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    _corporate_routes(
        respx_mock, corporate_detail("OPAQUE HOLDCO LTD", "99999821", until="2031-01-01")
    )
    result = _run_corporate(settings, "99999820")
    finding = next(f for f in result.findings if f.id == "NET-101")
    assert finding.severity == "info"
    assert "registered company numbers differ" in finding.statement
    assert "likely a different company" in finding.statement
    check = result.expansion.disqualification_checks[0]
    assert check.outcome == "mismatch"
    assert check.detail == "registered company number differs"


def test_corporate_without_officer_number_degrades_to_name_only(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    _corporate_routes(
        respx_mock, corporate_detail("OPAQUE HOLDCO LTD", "99999820", until="2031-01-01")
    )
    result = _run_corporate(settings, None)
    finding = next(f for f in result.findings if f.id == "NET-101")
    assert finding.severity == "info"
    assert finding.confidence == "unverified"
    assert "registered company number could not be compared" in finding.statement
    assert "manual check" in finding.statement
    assert result.expansion.disqualification_checks[0].outcome == "name_only"


def test_corporate_number_match_is_case_and_whitespace_insensitive(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    _corporate_routes(
        respx_mock, corporate_detail("OPAQUE HOLDCO LTD", " sc999820 ", until="2031-01-01")
    )
    result = _run_corporate(settings, "SC999820")
    finding = next(f for f in result.findings if f.id == "NET-101")
    assert finding.severity == "red"
    assert result.expansion.disqualification_checks[0].outcome == "strong_active"


# -- officer/PSC deduplication ---------------------------------------------------


def test_person_holding_both_roles_is_screened_once_with_both_roles_named(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    respx_mock.get(f"{BASE_URL}/officers/fictNet001/appointments").respond(
        200, json=appointments_payload("GRINDSTONE, Gertrude", [])
    )
    search_route = respx_mock.get(f"{BASE_URL}/search/disqualified-officers").respond(
        200, json=empty_search()
    )
    same_person_psc = PSC.model_validate(
        {
            "name": "Ms Gertrude Grindstone",
            "kind": "individual-person-with-significant-control",
            "date_of_birth": {"month": 4, "year": 1970},
        }
    )
    with make_client() as client:
        result = run_network_expansion(
            client,
            SUBJECT,
            [officer("GRINDSTONE, Gertrude", "fictNet001", dob={"month": 4, "year": 1970})],
            [same_person_psc],
            settings,
            TODAY,
            FALLBACK,
        )
    assert search_route.call_count == 1  # one human, one search
    checks = result.expansion.disqualification_checks
    assert [(c.subject, c.role) for c in checks] == [("GRINDSTONE, Gertrude", "officer and psc")]
    finding = next(f for f in result.findings if f.id == "NET-101")
    assert "(officer and PSC)" in finding.statement


def test_psc_without_matching_dob_is_still_screened_separately(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    respx_mock.get(f"{BASE_URL}/officers/fictNet001/appointments").respond(
        200, json=appointments_payload("GRINDSTONE, Gertrude", [])
    )
    search_route = respx_mock.get(f"{BASE_URL}/search/disqualified-officers").respond(
        200, json=empty_search()
    )
    # Same name but a different birth year, and a namesake with no DOB at
    # all: identity is not established, so both stay separate subjects.
    different_dob = PSC.model_validate(
        {
            "name": "Ms Gertrude Grindstone",
            "kind": "individual-person-with-significant-control",
            "date_of_birth": {"month": 4, "year": 1971},
        }
    )
    no_dob = PSC.model_validate(
        {
            "name": "Gertrude Grindstone",
            "kind": "individual-person-with-significant-control",
        }
    )
    with make_client() as client:
        result = run_network_expansion(
            client,
            SUBJECT,
            [officer("GRINDSTONE, Gertrude", "fictNet001", dob={"month": 4, "year": 1970})],
            [different_dob, no_dob],
            settings,
            TODAY,
            FALLBACK,
        )
    assert search_route.call_count == 3
    roles = [c.role for c in result.expansion.disqualification_checks]
    assert roles == ["officer", "psc", "psc"]
