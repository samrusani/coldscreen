"""Stage 1: number detection and ambiguity surfacing."""

from __future__ import annotations

import pytest
import respx

from firstpass.stages.resolve import normalize_company_number, resolve

from .conftest import BASE_URL, load_fixture, make_client

SEARCH_URL = f"{BASE_URL}/search/companies"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("99999999", "99999999"),
        ("1234567", "01234567"),
        ("123456", "00123456"),
        ("SC123456", "SC123456"),
        ("sc123456", "SC123456"),
        ("NI 123456", "NI123456"),
        (" 99999999 ", "99999999"),
    ],
)
def test_company_number_shapes_are_detected(text: str, expected: str) -> None:
    assert normalize_company_number(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Fabricated Widgets Ltd",
        "12345",  # too short
        "123456789",  # too long
        "ABC12345",  # three letters is not a prefix
        "S1234567",  # one letter is not a prefix
        "SC12345",  # prefix requires six digits
    ],
)
def test_non_number_inputs_go_to_search(text: str) -> None:
    assert normalize_company_number(text) is None


def test_number_input_skips_search_entirely(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(SEARCH_URL).respond(200, json=load_fixture("search_single.json"))
    with make_client() as client:
        resolution = resolve(client, "99999999")
    assert resolution.company_number == "99999999"
    assert route.call_count == 0


def test_single_candidate_proceeds(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(SEARCH_URL).respond(200, json=load_fixture("search_single.json"))
    with make_client() as client:
        resolution = resolve(client, "Fabricated Widgets Ltd")
    assert resolution.company_number == "99999999"
    assert not resolution.is_ambiguous


def test_multiple_candidates_are_surfaced_not_resolved(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(SEARCH_URL).respond(200, json=load_fixture("search_ambiguous.json"))
    with make_client() as client:
        resolution = resolve(client, "Fabricated Widgets")
    assert resolution.company_number is None
    assert resolution.is_ambiguous
    assert [c.company_number for c in resolution.candidates] == ["99999999", "99999998"]


def test_exact_title_match_breaks_the_tie(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(SEARCH_URL).respond(200, json=load_fixture("search_ambiguous.json"))
    with make_client() as client:
        resolution = resolve(client, "fabricated widgets ltd")
    assert resolution.company_number == "99999999"
    assert not resolution.is_ambiguous


def test_no_candidates_is_empty(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(SEARCH_URL).respond(
        200, json={"total_results": 0, "items": [], "kind": "search#companies"}
    )
    with make_client() as client:
        resolution = resolve(client, "Nonexistent Fictional Company")
    assert resolution.is_empty
