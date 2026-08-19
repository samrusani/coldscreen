"""Stage 4: query construction, batching, findings, key hygiene, yente mode."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from coldscreen.config import Settings
from coldscreen.models import PSC, CompanyProfile, Officer
from coldscreen.sanctions import (
    OpenSanctionsClient,
    SanctionsError,
    build_subjects,
    run_sanctions,
)

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
HOSTED = "https://api.opensanctions.org"
KEY = "fixture-opensanctions-key-not-real"


def profile() -> CompanyProfile:
    return CompanyProfile.model_validate(
        {
            "company_name": "FICTIONAL SUBJECT LTD",
            "company_number": "99999903",
            "company_status": "active",
            "jurisdiction": "england-wales",
        }
    )


def officers() -> list[Officer]:
    return [
        Officer.model_validate(
            {
                "name": "GRINDSTONE, Gertrude May",
                "officer_role": "director",
                "nationality": "British",
                "date_of_birth": {"month": 4, "year": 1970},
            }
        )
    ]


def pscs() -> list[PSC]:
    return [
        PSC.model_validate(
            {
                "name": "Mr Perceval Pinion",
                "kind": "individual-person-with-significant-control",
                "nationality": "British",
                "date_of_birth": {"month": 9, "year": 1962},
            }
        ),
        PSC.model_validate(
            {
                "name": "OPAQUE HOLDCO LTD",
                "kind": "corporate-entity-person-with-significant-control",
            }
        ),
    ]


def empty_response(qids: list[str]) -> dict[str, Any]:
    return {
        "responses": {
            qid: {"status": 200, "results": [], "total": {"value": 0, "relation": "eq"}}
            for qid in qids
        }
    }


# -- query construction --------------------------------------------------------


def test_subjects_cover_company_officers_and_individual_pscs_only() -> None:
    subjects = build_subjects(profile(), officers(), pscs())
    assert [(s.kind, s.query_schema) for s in subjects] == [
        ("company", "Company"),
        ("officer", "Person"),
        ("psc", "Person"),
    ]
    # The corporate PSC is not a screening subject in v0.1.
    assert all("OPAQUE HOLDCO" not in s.name for s in subjects)


def test_company_query_uses_verified_ftm_properties() -> None:
    subject = build_subjects(profile(), [], [])[0]
    assert subject.properties == {
        "name": ["FICTIONAL SUBJECT LTD"],
        "jurisdiction": ["gb"],
        "registrationNumber": ["99999903"],
    }


def test_person_query_disaggregates_registry_names_and_partial_dob() -> None:
    subject = build_subjects(profile(), officers(), [])[1]
    assert subject.properties["name"] == ["Gertrude May GRINDSTONE"]
    assert subject.properties["firstName"] == ["Gertrude May"]
    assert subject.properties["lastName"] == ["GRINDSTONE"]
    # Partial FtM date from the registry's {month, year} object.
    assert subject.properties["birthDate"] == ["1970-04"]
    assert subject.properties["nationality"] == ["british"]


def test_psc_names_drop_the_honorific_and_keep_natural_order() -> None:
    subject = build_subjects(profile(), [], pscs())[1]
    assert subject.properties["name"] == ["Perceval Pinion"]
    assert "firstName" not in subject.properties
    assert subject.properties["birthDate"] == ["1962-09"]


def test_all_property_values_are_lists_of_strings() -> None:
    for subject in build_subjects(profile(), officers(), pscs()):
        for values in subject.properties.values():
            assert isinstance(values, list)
            assert all(isinstance(v, str) for v in values)


def test_psc_who_is_also_an_officer_is_screened_once() -> None:
    same_person = PSC.model_validate(
        {
            "name": "Ms Gertrude May Grindstone",
            "kind": "individual-person-with-significant-control",
            "nationality": "British",
            "date_of_birth": {"month": 4, "year": 1970},
        }
    )
    subjects = build_subjects(profile(), officers(), [same_person])
    assert [(s.kind, s.name) for s in subjects] == [
        ("company", "FICTIONAL SUBJECT LTD"),
        ("officer and psc", "GRINDSTONE, Gertrude May"),
    ]
    # The merged query keeps the officer's disaggregated name form.
    assert subjects[1].properties["firstName"] == ["Gertrude May"]
    assert subjects[1].properties["birthDate"] == ["1970-04"]


def test_psc_namesake_with_different_dob_is_not_merged() -> None:
    namesake = PSC.model_validate(
        {
            "name": "Ms Gertrude May Grindstone",
            "kind": "individual-person-with-significant-control",
            "date_of_birth": {"month": 4, "year": 1971},
        }
    )
    subjects = build_subjects(profile(), officers(), [namesake])
    assert [s.kind for s in subjects] == ["company", "officer", "psc"]


# -- one batched request --------------------------------------------------------


def test_all_subjects_travel_in_one_match_request(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    route = respx_mock.post(f"{HOSTED}/match/default").respond(
        200, json=empty_response(["q001", "q002", "q003"])
    )
    result = run_sanctions(
        profile(), officers(), pscs(), settings, api_key=KEY, base_url=None, now=lambda: NOW
    )
    assert route.call_count == 1
    request = route.calls.last.request
    assert request.headers["Authorization"] == f"ApiKey {KEY}"
    params = dict(httpx.URL(str(request.url)).params)
    assert params == {"threshold": "0.7", "algorithm": "best", "limit": "5"}
    body = json.loads(request.content.decode("utf-8"))
    assert set(body["queries"].keys()) == {"q001", "q002", "q003"}
    assert body["queries"]["q001"]["schema"] == "Company"
    assert body["queries"]["q002"]["schema"] == "Person"
    assert result.screening.performed is True
    assert result.screening.dataset == "default"


# -- findings per subject --------------------------------------------------------


def _match_response(score: float, match: bool) -> dict[str, Any]:
    payload = empty_response(["q001", "q002", "q003"])
    payload["responses"]["q001"]["results"] = [
        {
            "id": "fict-001",
            "caption": "Fictional Matched Entity",
            "schema": "Company",
            "datasets": ["fict_list_a", "fict_list_b"],
            "score": score,
            "match": match,
        }
    ]
    payload["responses"]["q001"]["total"] = {"value": 1, "relation": "eq"}
    return payload


def test_match_at_or_above_threshold_is_a_red_finding(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    respx_mock.post(f"{HOSTED}/match/default").respond(200, json=_match_response(0.93, True))
    result = run_sanctions(
        profile(), officers(), pscs(), settings, api_key=KEY, base_url=None, now=lambda: NOW
    )
    finding = next(f for f in result.findings if f.id == "SAN-001")
    assert finding.severity == "red"
    assert "0.93" in finding.statement
    assert "threshold 0.7" in finding.statement
    assert "fict_list_a, fict_list_b" in finding.statement
    assert "algorithm best" in finding.statement
    assert result.screening.results[0].matched is True
    # The other subjects still get their absence findings.
    assert {f.id for f in result.findings} == {"SAN-001", "SAN-002", "SAN-003"}


def test_sub_threshold_top_hit_is_the_near_miss_info_finding(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    respx_mock.post(f"{HOSTED}/match/default").respond(200, json=_match_response(0.55, False))
    result = run_sanctions(
        profile(), officers(), pscs(), settings, api_key=KEY, base_url=None, now=lambda: NOW
    )
    finding = next(f for f in result.findings if f.id == "SAN-001")
    assert finding.severity == "info"
    assert "No sanctions match at or above threshold 0.7" in finding.statement
    assert "nearest candidate scored 0.55" in finding.statement
    # The near-miss caption never enters the finding text.
    assert "Fictional Matched Entity" not in finding.statement
    assert result.screening.results[0].matched is False
    assert result.screening.results[0].top_score == 0.55


def test_no_results_is_an_absence_finding_with_threshold_stated(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    respx_mock.post(f"{HOSTED}/match/default").respond(
        200, json=empty_response(["q001", "q002", "q003"])
    )
    result = run_sanctions(
        profile(), officers(), pscs(), settings, api_key=KEY, base_url=None, now=lambda: NOW
    )
    for finding in result.findings:
        assert "No sanctions match at or above threshold 0.7" in finding.statement
        assert finding.severity == "info"
        assert finding.evidence[0].source_url == f"{HOSTED}/match/default"


def test_resolved_algorithm_is_recorded_when_exposed(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    payload = empty_response(["q001", "q002", "q003"])
    payload["algorithm"] = "logic-v2"
    respx_mock.post(f"{HOSTED}/match/default").respond(200, json=payload)
    result = run_sanctions(
        profile(), officers(), pscs(), settings, api_key=KEY, base_url=None, now=lambda: NOW
    )
    assert result.screening.algorithm_requested == "best"
    assert result.screening.algorithm_resolved == "logic-v2"
    assert "resolved: logic-v2" in result.findings[0].statement


def test_query_limit_and_dataset_release_are_recorded(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    payload = empty_response(["q001", "q002", "q003"])
    payload["limit"] = 5
    payload["dataset_version"] = "fict-release-20260817"
    respx_mock.post(f"{HOSTED}/match/default").respond(200, json=payload)
    result = run_sanctions(
        profile(), officers(), pscs(), settings, api_key=KEY, base_url=None, now=lambda: NOW
    )
    assert result.screening.limit == 5
    assert result.screening.dataset_release == "fict-release-20260817"


def test_absent_dataset_release_stays_none(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    respx_mock.post(f"{HOSTED}/match/default").respond(
        200, json=empty_response(["q001", "q002", "q003"])
    )
    result = run_sanctions(
        profile(), officers(), pscs(), settings, api_key=KEY, base_url=None, now=lambda: NOW
    )
    assert result.screening.limit == 5
    assert result.screening.dataset_release is None


def test_merged_subject_finding_names_both_roles(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    respx_mock.post(f"{HOSTED}/match/default").respond(200, json=empty_response(["q001", "q002"]))
    same_person = PSC.model_validate(
        {
            "name": "Ms Gertrude May Grindstone",
            "kind": "individual-person-with-significant-control",
            "date_of_birth": {"month": 4, "year": 1970},
        }
    )
    result = run_sanctions(
        profile(), officers(), [same_person], settings, api_key=KEY, base_url=None, now=lambda: NOW
    )
    assert len(result.findings) == 2
    person_finding = result.findings[1]
    assert "(officer and psc)" in person_finding.statement
    assert result.screening.results[1].kind == "officer and psc"


# -- the no-key skip path --------------------------------------------------------


def test_no_key_no_endpoint_is_a_first_class_skip(settings: Settings) -> None:
    # No respx routes at all: the skip path must make no network calls.
    result = run_sanctions(
        profile(), officers(), pscs(), settings, api_key=None, base_url=None, now=lambda: NOW
    )
    assert result.screening.performed is False
    assert result.screening.skipped_reason is not None
    finding = next(f for f in result.findings if f.id == "SAN-000")
    assert "not performed" in finding.statement
    assert "no OpenSanctions key or endpoint configured" in finding.statement
    record = next(r for r in result.records if r.name == "sanctions_not_run")
    assert record.record.body["kind"] == "not_run"
    assert finding.evidence[0].source_url == record.record.url


def test_yente_mode_sends_no_auth_header(respx_mock: respx.MockRouter, settings: Settings) -> None:
    yente = "http://localhost:8000"
    route = respx_mock.post(f"{yente}/match/default").respond(
        200, json=empty_response(["q001", "q002", "q003"])
    )
    result = run_sanctions(
        profile(), officers(), pscs(), settings, api_key=None, base_url=yente, now=lambda: NOW
    )
    assert route.call_count == 1
    assert "Authorization" not in route.calls.last.request.headers
    assert result.screening.performed is True
    assert result.screening.endpoint == yente


def test_the_key_never_reaches_records_params_or_repr(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    respx_mock.post(f"{HOSTED}/match/default").respond(
        200, json=empty_response(["q001", "q002", "q003"])
    )
    result = run_sanctions(
        profile(), officers(), pscs(), settings, api_key=KEY, base_url=None, now=lambda: NOW
    )
    for named in result.records:
        assert KEY not in named.record.url
        assert KEY not in json.dumps(named.record.params)
        assert KEY not in json.dumps(named.record.body)
    client = OpenSanctionsClient(api_key=KEY)
    assert KEY not in repr(client)
    client.close()


def test_429_is_retried_then_succeeds(respx_mock: respx.MockRouter, settings: Settings) -> None:
    route = respx_mock.post(f"{HOSTED}/match/default")
    route.side_effect = [
        httpx.Response(429),
        httpx.Response(200, json=empty_response(["q001", "q002", "q003"])),
    ]
    result = run_sanctions(
        profile(),
        officers(),
        pscs(),
        settings,
        api_key=KEY,
        base_url=None,
        now=lambda: NOW,
        sleeper=lambda _s: None,
    )
    assert route.call_count == 2
    assert result.screening.performed is True


def test_terminal_client_error_raises_cleanly(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    respx_mock.post(f"{HOSTED}/match/default").respond(401)
    with pytest.raises(SanctionsError, match="401"):
        run_sanctions(
            profile(), officers(), pscs(), settings, api_key=KEY, base_url=None, now=lambda: NOW
        )


def test_evidence_record_is_persisted_with_the_raw_body(
    respx_mock: respx.MockRouter, settings: Settings
) -> None:
    payload = _match_response(0.55, False)
    respx_mock.post(f"{HOSTED}/match/default").respond(200, json=payload)
    result = run_sanctions(
        profile(), officers(), pscs(), settings, api_key=KEY, base_url=None, now=lambda: NOW
    )
    record = next(r for r in result.records if r.name == "sanctions_match")
    assert record.record.body == payload
    assert record.record.params == {"threshold": "0.7", "algorithm": "best", "limit": "5"}
