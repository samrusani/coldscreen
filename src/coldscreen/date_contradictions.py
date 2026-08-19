"""Mechanical origin-year contradictions against the registry incorporation date.

R4 is a hybrid trigger. Its judgment path still requires a surviving
contradicted assessment with a relevant registry basis. This module is the
floor for the most common contradiction class: a stored claim whose text
places the company's origin in a calendar year before date_of_creation.

Year granularity is deliberate. "Operating since 2015" against an
incorporation date of 2019-05-14 is a contradiction; "Founded in 2018"
against 2018-06-15 is not, because a year-level claim can be true of any
day in that year. Month and day parsing would add false reds on
same-year claims and is out of scope for this detector.

The patterns are origin-shaped and narrow. Duration verbs (operating,
trading, started) require since/from; event verbs (founded, established,
incorporated, formed, launched, began) accept an optional in/on. A
leading "Since YYYY" on the claim is treated as origin. Bare "since
YYYY" in the middle of a sentence is not: "CEO since 2015" is not a
company-origin claim. A negation in the forty-eight characters before a
match ("not operating since 2015") suppresses that match.

Claim text is a verified quotation (the claims stage already refused
anything that was not a substring of its source), and the incorporation
date is a registry field. Neither is model memory. Years outside
1800-2099 are ignored as not a plausible UK company origin.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .language import normalize_for_match
from .models import CaseFile

MIN_ORIGIN_YEAR = 1800
MAX_ORIGIN_YEAR = 2099
NEGATION_WINDOW = 48

_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)"
)
# Optional day and month before the year: "14 May 2015", "May 2015", "2015".
_DATE_TAIL = rf"(?:(?:\d{{1,2}}\s+)?{_MONTH}\s+)?(\d{{4}})\b"

# "operating since 2015", "trading from May 2015", "started operations since 2014"
_DURATION_RE = re.compile(
    r"\b(?:operat(?:e[ds]?|ing)|trad(?:e[ds]?|ing)|start(?:ed|ing))\b"
    r"(?:\s+operations)?"
    r"\s+(?:since|from)\s+" + _DATE_TAIL,
    re.IGNORECASE,
)

# "founded in 2015", "established 2014", "began operations in 2016"
_EVENT_RE = re.compile(
    r"\b(?:founded|established|incorporated|formed|launched|launching|"
    r"began|begun)\b"
    r"(?:\s+operations|\s+trading)?"
    r"(?:\s+(?:in|on))?"
    r"\s+" + _DATE_TAIL,
    re.IGNORECASE,
)

# "Since 2015 we have grown" as the whole claim's lead-in.
_SINCE_LEAD_RE = re.compile(r"^since\s+" + _DATE_TAIL, re.IGNORECASE)

_NEGATION_RE = re.compile(
    r"\b(?:not|never|no longer|stopped|ceased|without)\b",
    re.IGNORECASE,
)

_ORIGIN_PATTERNS: tuple[re.Pattern[str], ...] = (_DURATION_RE, _EVENT_RE, _SINCE_LEAD_RE)


@dataclass(frozen=True)
class OriginYearContradiction:
    """One stored claim whose origin year predates incorporation."""

    claim_id: str
    claimed_year: int
    incorporated_on: date


def claimed_origin_years(text: str) -> tuple[int, ...]:
    """Calendar years a claim asserts as the company's origin, earliest first.

    Empty when the text does not match an origin-date pattern, every match
    sits in a negation window, or every captured year is outside 1800-2099.
    """
    normalized = normalize_for_match(text)
    if not normalized:
        return ()
    years: list[int] = []
    seen: set[int] = set()
    for pattern in _ORIGIN_PATTERNS:
        for match in pattern.finditer(normalized):
            window_start = max(0, match.start() - NEGATION_WINDOW)
            prefix = normalized[window_start : match.start()]
            if _NEGATION_RE.search(prefix):
                continue
            year = int(match.group(1))
            if year < MIN_ORIGIN_YEAR or year > MAX_ORIGIN_YEAR:
                continue
            if year in seen:
                continue
            seen.add(year)
            years.append(year)
    years.sort()
    return tuple(years)


def origin_year_contradictions(casefile: CaseFile) -> tuple[OriginYearContradiction, ...]:
    """Hits on this casefile: stored claims whose origin year is before
    date_of_creation. Empty when the profile has no incorporation date or
    no stored claim matches. Scans every stored claim, including puffery:
    checkable is a model flag and cannot hide a quotation the claims stage
    already verified.
    """
    incorporated_on = casefile.subject.date_of_creation
    if incorporated_on is None:
        return ()
    hits: list[OriginYearContradiction] = []
    for claim in casefile.claims:
        years = claimed_origin_years(claim.text)
        earlier = [year for year in years if year < incorporated_on.year]
        if not earlier:
            continue
        hits.append(
            OriginYearContradiction(
                claim_id=claim.id,
                claimed_year=min(earlier),
                incorporated_on=incorporated_on,
            )
        )
    return tuple(hits)


def candidate_reason(hits: tuple[OriginYearContradiction, ...]) -> str:
    """Fixed-shape reason for the R4 candidate. Years and the ISO date are
    code-derived (parsed quotation + registry field), never model prose."""
    earliest = min(hit.claimed_year for hit in hits)
    incorporated = hits[0].incorporated_on.isoformat()
    return (
        f"a stored claim places origin in {earliest}, before the registry"
        f" incorporation date of {incorporated}"
    )


def assessment_note(hit: OriginYearContradiction) -> str:
    """Record note for a mechanically contradicted origin claim. No claim
    wording: record notes are language-gated with zero quote exemptions."""
    return (
        f"Claimed origin year {hit.claimed_year} is before the registry"
        f" incorporation date of {hit.incorporated_on.isoformat()} (REG-002)."
    )
