"""Origin-year contradiction detector: patterns, negations, year granularity."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from coldscreen.date_contradictions import (
    OriginYearContradiction,
    assessment_note,
    candidate_reason,
    claimed_origin_years,
    origin_year_contradictions,
)
from coldscreen.models import CaseFile, Claim, CompanyProfile, Evidence, Finding
from coldscreen.rubric import detect_candidates

SCREENED_AT = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Operating since 2015 with a national footprint", (2015,)),
        ("operating since May 2015", (2015,)),
        ("trading from 2014", (2014,)),
        ("started operations since 2012", (2012,)),
        ("Founded in 2018 by Mabel Meridian", (2018,)),
        ("founded 2016", (2016,)),
        ("established in January 2017", (2017,)),
        ("incorporated in 2015", (2015,)),
        ("formed in 2013", (2013,)),
        ("launched in 2011", (2011,)),
        ("began operations in 2010", (2010,)),
        ("Since 2015 we have grown a national footprint", (2015,)),
        ("Since May 2015 the company has traded", (2015,)),
        # Two origin years: earliest first, unique.
        ("Operating since 2015, founded in 2014", (2014, 2015)),
        # Same year twice is one year.
        ("Founded in 2015 and operating since 2015", (2015,)),
    ],
)
def test_origin_patterns_extract_the_year(text: str, expected: tuple[int, ...]) -> None:
    assert claimed_origin_years(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "CEO since 2015",
        "profits since 2015",
        "no charges since 2015",
        "operating in 2015",  # duration verbs require since/from
        "the market has grown since 2015",
        "A team of 40 widget engineers",
        "The company is debt free and self funded",
        "",
        "founded in the nineties",
        "operating since 999",  # outside 1800-2099
        "founded in 2200",
    ],
)
def test_non_origin_or_out_of_range_yields_no_year(text: str) -> None:
    assert claimed_origin_years(text) == ()


def test_iso_date_in_an_origin_claim_contributes_its_year() -> None:
    assert claimed_origin_years("incorporated on 2015-03-01") == (2015,)


@pytest.mark.parametrize(
    "text",
    [
        "not operating since 2015",
        "never founded in 2015",
        "no longer trading since 2014",
        "stopped operating since 2013",
        "ceased trading from 2012",
        "without operating since 2011",
    ],
)
def test_negation_window_suppresses_the_match(text: str) -> None:
    assert claimed_origin_years(text) == ()


def test_negation_after_the_match_does_not_suppress_it() -> None:
    assert claimed_origin_years("operating since 2015, not 2018") == (2015,)


def _profile(created: str | None) -> CompanyProfile:
    payload: dict[str, str] = {
        "company_name": "FICTIONAL SUBJECT LTD",
        "company_number": "99999903",
    }
    if created is not None:
        payload["date_of_creation"] = created
    return CompanyProfile.model_validate(payload)


def _claim(text: str, checkable: bool = True, category: str = "history") -> Claim:
    return Claim(
        id="CLM-001",
        text=text,
        source="deck p.2",
        category=category,  # type: ignore[arg-type]
        checkable=checkable,
    )


def _casefile(
    text: str,
    created: str | None,
    *,
    checkable: bool = True,
    with_reg002: bool = True,
    category: str = "history",
) -> CaseFile:
    findings: list[Finding] = []
    if with_reg002:
        excerpt = f"date_of_creation={created}" if created else "date_of_creation absent"
        findings.append(
            Finding(
                id="REG-002",
                stage="registry",
                severity="info",
                confidence="confirmed",
                statement="Incorporated.",
                evidence=[
                    Evidence(
                        source_url="https://example.invalid/profile",
                        retrieved_at=SCREENED_AT,
                        excerpt=excerpt,
                    )
                ],
            )
        )
    return CaseFile(
        subject=_profile(created),
        findings=findings,
        claims=[_claim(text, checkable=checkable, category=category)],
        tool_version="0.1.0.dev0",
        screened_at=SCREENED_AT,
    )


def test_operating_since_before_incorporation_is_a_hit() -> None:
    casefile = _casefile("Operating since 2015 with a national footprint", "2019-05-14")
    hits = origin_year_contradictions(casefile)
    assert hits == (
        OriginYearContradiction(
            claim_id="CLM-001",
            claimed_year=2015,
            incorporated_on=date(2019, 5, 14),
        ),
    )


def test_same_year_as_incorporation_is_not_a_hit() -> None:
    """Year granularity: founded in 2018 against 2018-06-15 is not a red."""
    casefile = _casefile("Founded in 2018 by Mabel Meridian", "2018-06-15")
    assert origin_year_contradictions(casefile) == ()


def test_origin_year_after_incorporation_is_not_a_hit() -> None:
    casefile = _casefile("operating since 2020", "2019-05-14")
    assert origin_year_contradictions(casefile) == ()


def test_missing_incorporation_date_yields_no_hit() -> None:
    casefile = _casefile("Operating since 2015 with a national footprint", None)
    assert origin_year_contradictions(casefile) == ()


def test_puffery_claims_are_still_scanned() -> None:
    """checkable is a model flag; a verified quotation cannot hide behind it."""
    casefile = _casefile(
        "Operating since 2015 with a national footprint",
        "2019-05-14",
        checkable=False,
        category="other",
    )
    hits = origin_year_contradictions(casefile)
    assert len(hits) == 1
    assert hits[0].claimed_year == 2015


def test_miscategorised_history_claim_still_hits() -> None:
    casefile = _casefile(
        "Operating since 2015 with a national footprint",
        "2019-05-14",
        category="financials",
    )
    assert origin_year_contradictions(casefile)


def test_detect_candidates_emits_r4_with_reg002_and_code_derived_reason() -> None:
    casefile = _casefile("Operating since 2015 with a national footprint", "2019-05-14")
    candidates = {c.id: c for c in detect_candidates(casefile)}
    assert "R4" in candidates
    assert candidates["R4"].finding_ids == ["REG-002"]
    assert candidates["R4"].reason == candidate_reason(origin_year_contradictions(casefile))
    assert "2015" in candidates["R4"].reason
    assert "2019-05-14" in candidates["R4"].reason


def test_detect_candidates_omits_r4_when_the_year_is_not_before_incorporation() -> None:
    casefile = _casefile("Founded in 2018 by Mabel Meridian", "2018-06-15")
    assert "R4" not in {c.id for c in detect_candidates(casefile)}


def test_assessment_note_names_year_date_and_finding_not_claim_text() -> None:
    hit = OriginYearContradiction(
        claim_id="CLM-001",
        claimed_year=2015,
        incorporated_on=date(2019, 5, 14),
    )
    note = assessment_note(hit)
    assert note == (
        "Claimed origin year 2015 is before the registry incorporation date"
        " of 2019-05-14 (REG-002)."
    )
    assert "Operating" not in note
    assert "footprint" not in note
