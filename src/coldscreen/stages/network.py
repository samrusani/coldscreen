"""Stage 3: network expansion, first degree only.

For each CURRENT officer: other appointments via /officers/{id}/appointments
(officer id extracted from links.officer.appointments), paginated with the
existing paginator, every page persisted. For each current officer and each
individual PSC: the disqualified officers search, with candidate detail
records fetched and persisted.

Match tiers per wiki/research/ftm-and-disqualifications.md:
- strong match: normalized name match AND date of birth month+year match
  (natural persons), or normalized name match AND registered company number
  match against the corporate detail record (corporate officers). Currently
  active (disqualified_until on or after the screening date) is a red
  finding (R3 candidate); expired is an info finding with dates.
- affirmative mismatch: the name matched but the distinguishing detail (DOB,
  or company number for corporate officers) is present on both sides and
  differs. Info finding stating the record is likely a different person or
  company; never red.
- name-only match (the distinguishing detail could not be compared): info
  finding, "requires manual check", never red.
- no match: absence finding.

The registry gives officer DOB as {month, year}; the disqualification detail
gives full dates. Corporate officers carry their registration number in the
officer list identification block, parsed defensively. The
natural-vs-corporate split is inferred from the links.self path, which the
research file marks unverified, so anything that does not parse cleanly
degrades to an info finding rather than guessing.

One human, one screening: an individual PSC who is also a current officer
(same normalized name AND same DOB month and year) is folded into the
officer's disqualification check, with the findings naming both roles.

Co-appointment overlap is the deterministic substrate for R5 judgment. The
finding lists shared companies and nothing more: "undisclosed" would imply
knowledge of disclosures that does not exist until claims arrive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Literal

from ..casedir import slugify
from ..ch_client import CompaniesHouseClient, FetchRecord, NotFoundError
from ..config import Settings
from ..models import (
    PSC,
    AppointmentSummary,
    CoAppointmentOverlap,
    DateOfBirth,
    DisqualificationCheck,
    Evidence,
    Finding,
    NetworkExpansion,
    Officer,
)
from .registry import NamedRecord

STAGE = "network"

# The co-appointment overlap finding id. The R5 gate's acceptance note in
# coldscreen.rubric renders overlap finding ids collected by filtering on
# this constant, so it must never be minted anywhere else.
OVERLAP_FINDING_ID = "NET-002"

# Fan-out guard: at most this many disqualification detail records are
# fetched per screened person. Common names can match many records; past
# the cap the finding says a manual check is required.
MAX_DISQ_DETAILS = 5

HONORIFICS = frozenset(
    {"mr", "mrs", "ms", "miss", "mx", "dr", "sir", "dame", "lord", "lady", "prof"}
)

_APPOINTMENTS_LINK_RE = re.compile(r"^/officers/([^/]+)/appointments/?$")


def normalize_person_name(name: str) -> tuple[str, ...]:
    """Lowercased, punctuation-free, honorific-free, order-free token tuple.

    Registry officer names arrive as "SURNAME, Forename" and disqualification
    search titles as free-order strings, so comparison must ignore order.
    """
    tokens = re.sub(r"[^a-z0-9]+", " ", name.lower()).split()
    return tuple(sorted(t for t in tokens if t not in HONORIFICS))


def officer_id_from_links(links: dict[str, Any]) -> str | None:
    """Extract the officer id from links.officer.appointments, defensively."""
    officer_link = links.get("officer")
    if not isinstance(officer_link, dict):
        return None
    appointments = officer_link.get("appointments")
    if not isinstance(appointments, str):
        return None
    match = _APPOINTMENTS_LINK_RE.match(appointments.strip())
    return match.group(1) if match else None


@dataclass(frozen=True)
class ScreenedPerson:
    """One disqualification subject: a current officer, an individual PSC,
    or one person holding both roles (screened once)."""

    name: str
    role: Literal["officer", "psc", "officer and psc"]
    date_of_birth: DateOfBirth | None
    corporate: bool = False
    # Registered number for corporate officers, from the officer list
    # identification block; None when absent or not corporate.
    company_number: str | None = None


def duplicate_officer_index(
    name: str,
    date_of_birth: DateOfBirth | None,
    officers: list[Officer],
) -> int | None:
    """Index of the current officer this person duplicates, or None.

    Identity is only assumed on a normalized name match AND a date of birth
    present on both sides with equal month and year. Anything less keeps
    both subjects screened separately.
    """
    if date_of_birth is None or date_of_birth.month is None or date_of_birth.year is None:
        return None
    wanted = normalize_person_name(name)
    for index, officer in enumerate(officers):
        dob = officer.date_of_birth
        if dob is None or dob.month != date_of_birth.month or dob.year != date_of_birth.year:
            continue
        if normalize_person_name(officer.name) == wanted:
            return index
    return None


def _officer_registration_number(officer: Officer) -> str | None:
    """registration_number from the officer's identification block.

    The block's exact shape is unverified upstream, so parse defensively:
    anything that is not a non-empty string is treated as absent.
    """
    raw = officer.identification.get("registration_number")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _subject_label(person: ScreenedPerson) -> str:
    """The person as named in finding statements, mentioning both roles
    when one screening covered both."""
    if person.role == "officer and psc":
        return f"{person.name} (officer and PSC)"
    return person.name


@dataclass
class NetworkStageResult:
    findings: list[Finding] = field(default_factory=list)
    records: list[NamedRecord] = field(default_factory=list)
    expansion: NetworkExpansion = field(default_factory=NetworkExpansion)


def _evidence(record: FetchRecord, excerpt: str | None = None) -> Evidence:
    return Evidence(source_url=record.url, retrieved_at=record.retrieved_at, excerpt=excerpt)


class _SlugRegistry:
    """Deterministic, collision-free evidence file stems."""

    def __init__(self) -> None:
        self._used: set[str] = set()

    def take(self, name: str) -> str:
        base = slugify(name, max_length=40)
        slug = base
        counter = 2
        while slug in self._used:
            slug = f"{base}-{counter}"
            counter += 1
        self._used.add(slug)
        return slug


def _parse_iso_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _dob_matches(registry_dob: DateOfBirth | None, detail_dob: Any) -> bool | None:
    """month+year comparison. None means it could not be established."""
    if registry_dob is None or registry_dob.month is None or registry_dob.year is None:
        return None
    parsed = _parse_iso_date(detail_dob)
    if parsed is None:
        return None
    return parsed.month == registry_dob.month and parsed.year == registry_dob.year


def _company_numbers_match(officer_number: str | None, detail_number: Any) -> bool | None:
    """Registered-number comparison. None means it could not be established."""
    if not officer_number:
        return None
    if not isinstance(detail_number, str) or not detail_number.strip():
        return None
    return officer_number.strip().upper() == detail_number.strip().upper()


def _detail_name_tokens(body: dict[str, Any]) -> tuple[str, ...]:
    parts = [str(body.get(key) or "") for key in ("forename", "other_forenames", "surname")]
    combined = " ".join(p for p in parts if p).strip()
    if not combined:
        combined = str(body.get("name") or "")
    return normalize_person_name(combined)


def _disqualification_windows(body: dict[str, Any]) -> list[tuple[date | None, date | None]]:
    windows: list[tuple[date | None, date | None]] = []
    raw = body.get("disqualifications")
    if not isinstance(raw, list):
        return windows
    for item in raw:
        if not isinstance(item, dict):
            continue
        windows.append(
            (
                _parse_iso_date(item.get("disqualified_from")),
                _parse_iso_date(item.get("disqualified_until")),
            )
        )
    return windows


def _permissions_note(body: dict[str, Any], today: date) -> str:
    raw = body.get("permissions_to_act")
    if not isinstance(raw, list) or not raw:
        return ""
    current = 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        expires = _parse_iso_date(item.get("expires_on"))
        if expires is None or expires >= today:
            current += 1
    if current:
        return (
            f" The record lists {current} current permission(s) to act for named"
            " companies by court permission."
        )
    return f" The record lists {len(raw)} expired permission(s) to act."


def _window_text(windows: list[tuple[date | None, date | None]]) -> str:
    parts = []
    for start, until in windows:
        start_text = start.isoformat() if start else "unknown start"
        until_text = until.isoformat() if until else "unknown end"
        parts.append(f"{start_text} to {until_text}")
    return "; ".join(parts)


def _windows_tier(detail_body: dict[str, Any], today: date) -> str:
    """Tier for a record whose identity already matched strongly."""
    windows = _disqualification_windows(detail_body)
    if any(until is not None and until >= today for _start, until in windows):
        return "strong_active"
    if any(until is not None for _start, until in windows):
        return "strong_expired"
    # Identity matched but no parseable dates: degrade rather than guess.
    return "name_only"


def _natural_tier(
    person: ScreenedPerson,
    detail_body: dict[str, Any],
    wanted: tuple[str, ...],
    today: date,
) -> str | None:
    """Tier for a natural detail record; None when the name does not match."""
    if _detail_name_tokens(detail_body) != wanted:
        return None
    dob_ok = _dob_matches(person.date_of_birth, detail_body.get("date_of_birth"))
    if dob_ok is True:
        return _windows_tier(detail_body, today)
    if dob_ok is False:
        return "mismatch"
    return "name_only"


def _corporate_tier(
    person: ScreenedPerson,
    detail_body: dict[str, Any],
    wanted: tuple[str, ...],
    today: date,
) -> str | None:
    """Tier for a corporate detail record; None when the name does not match.

    The corporate identity block is name + company_number (no DOB), so a
    strong match is normalized name AND registered company number.
    """
    if _detail_name_tokens(detail_body) != wanted:
        return None
    number_ok = _company_numbers_match(person.company_number, detail_body.get("company_number"))
    if number_ok is True:
        return _windows_tier(detail_body, today)
    if number_ok is False:
        return "mismatch"
    return "name_only"


def run_network_expansion(
    client: CompaniesHouseClient,
    subject_company_number: str,
    current_officers: list[Officer],
    pscs: list[PSC],
    settings: Settings,
    today: date,
    fallback_record: FetchRecord,
) -> NetworkStageResult:
    """Run stage 3 and return findings, evidence records, and the summary.

    fallback_record backs absence findings that would otherwise have no
    fetched response to cite (for example, no current officers at all).
    """
    result = NetworkStageResult()
    result.expansion.performed = True
    appointment_slugs = _SlugRegistry()
    disq_slugs = _SlugRegistry()

    # -- appointments fan-out ------------------------------------------------
    appointment_records: list[FetchRecord] = []
    # company_number -> (company_name, set of officer names)
    seen_companies: dict[str, tuple[str | None, list[str]]] = {}
    skipped_no_link = 0
    subject_number = subject_company_number.strip().upper()

    for officer in current_officers:
        officer_id = officer_id_from_links(officer.links)
        if officer_id is None:
            skipped_no_link += 1
            result.expansion.appointments.append(
                AppointmentSummary(officer_name=officer.name, other_current_appointments=0)
            )
            continue
        slug = appointment_slugs.take(officer.name)
        try:
            pages = client.get_paginated(
                f"/officers/{officer_id}/appointments",
                items_per_page=settings.items_per_page,
                max_pages=settings.max_pages_appointments,
                total_key="total_results",
            )
        except NotFoundError as error:
            if error.record is not None:
                result.records.append(NamedRecord(f"appointments_{slug}_p1", error.record))
                appointment_records.append(error.record)
            result.expansion.appointments.append(
                AppointmentSummary(officer_name=officer.name, other_current_appointments=0)
            )
            continue
        for index, record in enumerate(pages.records, start=1):
            result.records.append(NamedRecord(f"appointments_{slug}_p{index}", record))
        if pages.records:
            appointment_records.append(pages.records[0])

        companies: list[str] = []
        for item in pages.items:
            if item.get("resigned_on"):
                continue  # not a current appointment
            appointed_to = item.get("appointed_to")
            if not isinstance(appointed_to, dict):
                continue
            number = str(appointed_to.get("company_number") or "").strip().upper()
            if not number or number == subject_number:
                continue
            name = appointed_to.get("company_name")
            company_name = str(name) if isinstance(name, str) else None
            display = f"{company_name or 'unnamed company'} ({number})"
            if display not in companies:
                companies.append(display)
            stored_name, holders = seen_companies.setdefault(number, (company_name, []))
            if officer.name not in holders:
                holders.append(officer.name)
            if stored_name is None and company_name is not None:
                seen_companies[number] = (company_name, holders)
        result.expansion.appointments.append(
            AppointmentSummary(
                officer_name=officer.name,
                other_current_appointments=len(companies),
                companies=companies,
                truncated=pages.truncated,
            )
        )

    # NET-001: fan-out summary (or the absence of anything to expand).
    total_other = sum(a.other_current_appointments for a in result.expansion.appointments)
    if current_officers:
        statement = (
            f"Network expansion covered {len(current_officers)} current officer(s):"
            f" {total_other} current appointment(s) at other companies were found."
        )
        if skipped_no_link:
            statement += (
                f" {skipped_no_link} officer(s) had no appointments link and were not expanded."
            )
        evidence = [
            _evidence(record, "officer appointments page") for record in appointment_records
        ] or [_evidence(fallback_record, "no appointment responses retrieved")]
        result.findings.append(
            Finding(
                id="NET-001",
                stage=STAGE,
                severity="info",
                confidence="confirmed" if appointment_records else "indicated",
                statement=statement,
                evidence=evidence,
            )
        )
    else:
        result.findings.append(
            Finding(
                id="NET-001",
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement="No current officers on the register, so there was no network to expand.",
                evidence=[_evidence(fallback_record, "no current officers")],
            )
        )

    # NET-002: co-appointment overlap, or its explicit absence.
    for number, (company_name, holders) in sorted(seen_companies.items()):
        if len(holders) >= 2:
            result.expansion.overlaps.append(
                CoAppointmentOverlap(
                    company_number=number,
                    company_name=company_name,
                    officer_names=sorted(holders),
                )
            )
    overlap_evidence = [
        _evidence(record, "officer appointments page") for record in appointment_records
    ] or [_evidence(fallback_record, "no appointment responses retrieved")]
    if result.expansion.overlaps:
        listed = "; ".join(
            f"{o.company_name or 'unnamed company'} ({o.company_number}):"
            f" {', '.join(o.officer_names)}"
            for o in result.expansion.overlaps
        )
        result.findings.append(
            Finding(
                id=OVERLAP_FINDING_ID,
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=(
                    f"Co-appointment overlap: {len(result.expansion.overlaps)} other"
                    f" company(ies) where two or more current officers of the subject"
                    f" hold current appointments: {listed}."
                ),
                evidence=overlap_evidence,
            )
        )
    else:
        result.findings.append(
            Finding(
                id=OVERLAP_FINDING_ID,
                stage=STAGE,
                severity="info",
                confidence="confirmed" if appointment_records else "indicated",
                statement=(
                    "No co-appointment overlap: no other company was found where two"
                    " or more current officers of the subject hold current appointments."
                ),
                evidence=overlap_evidence,
            )
        )

    # -- disqualifications ---------------------------------------------------
    people: list[ScreenedPerson] = [
        ScreenedPerson(
            name=o.name,
            role="officer",
            date_of_birth=o.date_of_birth,
            corporate=bool(o.officer_role and o.officer_role.startswith("corporate")),
            company_number=_officer_registration_number(o),
        )
        for o in current_officers
    ]
    # One human, one screening: an individual PSC who duplicates a current
    # officer is folded into that officer's check rather than searched twice.
    for psc in pscs:
        if not psc.is_individual or not psc.name:
            continue
        dup_index = duplicate_officer_index(psc.name, psc.date_of_birth, current_officers)
        if dup_index is None:
            people.append(
                ScreenedPerson(name=psc.name, role="psc", date_of_birth=psc.date_of_birth)
            )
        else:
            people[dup_index] = replace(people[dup_index], role="officer and psc")

    finding_number = 101
    for person in people:
        finding_id = f"NET-{finding_number}"
        finding_number += 1
        _check_disqualification(client, person, today, disq_slugs, result, finding_id)

    return result


def _check_disqualification(
    client: CompaniesHouseClient,
    person: ScreenedPerson,
    today: date,
    slugs: _SlugRegistry,
    result: NetworkStageResult,
    finding_id: str,
) -> None:
    slug = slugs.take(person.name)
    label = _subject_label(person)
    try:
        search_record = client.get(
            "/search/disqualified-officers", {"q": person.name, "items_per_page": 20}
        )
    except NotFoundError as error:
        error_record = error.record
        result.expansion.disqualification_checks.append(
            DisqualificationCheck(
                subject=person.name,
                role=person.role,
                outcome="none",
                detail="search returned 404",
            )
        )
        if error_record is not None:
            result.records.append(NamedRecord(f"disqualified_search_{slug}", error_record))
            result.findings.append(
                Finding(
                    id=finding_id,
                    stage=STAGE,
                    severity="info",
                    confidence="indicated",
                    statement=(
                        f"No disqualification record found for {label}:"
                        " the disqualified officers search returned 404."
                    ),
                    evidence=[_evidence(error_record, "search 404")],
                )
            )
        return
    result.records.append(NamedRecord(f"disqualified_search_{slug}", search_record))

    body = search_record.body if isinstance(search_record.body, dict) else {}
    items = [i for i in (body.get("items") or []) if isinstance(i, dict)]
    wanted = normalize_person_name(person.name)
    candidates = [
        item for item in items if normalize_person_name(str(item.get("title") or "")) == wanted
    ]

    if not candidates:
        result.expansion.disqualification_checks.append(
            DisqualificationCheck(subject=person.name, role=person.role, outcome="none")
        )
        result.findings.append(
            Finding(
                id=finding_id,
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=(
                    f"No disqualification record matches {label} on the"
                    " disqualified officers register."
                ),
                evidence=[_evidence(search_record, f"candidates=0 of {len(items)} result(s)")],
            )
        )
        return

    overflow = len(candidates) > MAX_DISQ_DETAILS
    best: tuple[str, FetchRecord, dict[str, Any]] | None = None  # (tier, record, body)
    # An affirmative mismatch (likely a different person or company) ranks
    # below an uncomparable name-only hit: the latter is the one that still
    # needs a manual check.
    tier_rank = {"strong_active": 4, "strong_expired": 3, "name_only": 2, "mismatch": 1}
    followed = 0
    for index, item in enumerate(candidates[:MAX_DISQ_DETAILS], start=1):
        raw_links = item.get("links")
        links: dict[str, Any] = raw_links if isinstance(raw_links, dict) else {}
        self_link = str(links.get("self") or "")
        is_corporate = "/corporate/" in self_link
        is_natural = "/natural/" in self_link
        if not self_link or (not is_natural and not is_corporate):
            continue
        if is_corporate and not person.corporate:
            continue  # a corporate record cannot be this natural person
        if is_natural and person.corporate:
            continue
        try:
            detail_record = client.get(self_link)
        except NotFoundError as error:
            if error.record is not None:
                result.records.append(
                    NamedRecord(f"disqualified_record_{slug}_{index}", error.record)
                )
            continue
        followed += 1
        result.records.append(NamedRecord(f"disqualified_record_{slug}_{index}", detail_record))
        detail_body = detail_record.body if isinstance(detail_record.body, dict) else {}

        if is_corporate:
            tier = _corporate_tier(person, detail_body, wanted, today)
        else:
            tier = _natural_tier(person, detail_body, wanted, today)
        if tier is None:
            continue
        if best is None or tier_rank[tier] > tier_rank[best[0]]:
            best = (tier, detail_record, detail_body)

    if best is None:
        detail_note = (
            " Name-matched search results could not be resolved to a detail record."
            if followed == 0
            else " Followed detail records did not match on structured name."
        )
        result.expansion.disqualification_checks.append(
            DisqualificationCheck(
                subject=person.name,
                role=person.role,
                outcome="none",
                detail=detail_note.strip(),
            )
        )
        result.findings.append(
            Finding(
                id=finding_id,
                stage=STAGE,
                severity="info",
                confidence="indicated",
                statement=(
                    f"No disqualification record could be confirmed for {label}." + detail_note
                ),
                evidence=[_evidence(search_record, f"candidates={len(candidates)}")],
            )
        )
        return

    tier, detail_record, detail_body = best
    windows = _disqualification_windows(detail_body)
    windows_text = _window_text(windows)
    permissions = _permissions_note(detail_body, today)
    overflow_note = (
        f" {len(candidates) - MAX_DISQ_DETAILS} further name-matched record(s) were"
        " not examined; manual check required."
        if overflow
        else ""
    )
    # What a strong match, an uncomparable detail, or a mismatch mean here
    # depends on the record family: natural records compare DOB, corporate
    # records compare the registered company number.
    if person.corporate:
        strong_basis = "normalized name and registered company number"
        strong_excerpt = "structured name and company number match"
        uncompared_text = "the registered company number could not be compared"
        mismatch_text = (
            "the registered company numbers differ, so the record is likely a different company"
        )
        mismatch_detail = "registered company number differs"
        mismatch_excerpt = "name matched, company number differs"
    else:
        strong_basis = "normalized name and date of birth (month and year)"
        strong_excerpt = "structured name and DOB month+year match"
        uncompared_text = "date of birth could not be confirmed"
        mismatch_text = "the dates of birth differ, so the record is likely a different person"
        mismatch_detail = "date of birth differs"
        mismatch_excerpt = "name matched, date of birth differs"

    if tier == "strong_active":
        result.expansion.disqualification_checks.append(
            DisqualificationCheck(
                subject=person.name,
                role=person.role,
                outcome="strong_active",
                detail=windows_text or None,
            )
        )
        result.findings.append(
            Finding(
                id=finding_id,
                stage=STAGE,
                severity="red",
                confidence="confirmed",
                statement=(
                    f"{label} matches a currently active disqualification record"
                    f" on {strong_basis}."
                    f" Disqualification period(s): {windows_text}.{permissions}{overflow_note}"
                ),
                evidence=[
                    _evidence(detail_record, strong_excerpt),
                    _evidence(search_record, "search result"),
                ],
            )
        )
    elif tier == "strong_expired":
        result.expansion.disqualification_checks.append(
            DisqualificationCheck(
                subject=person.name,
                role=person.role,
                outcome="strong_expired",
                detail=windows_text or None,
            )
        )
        result.findings.append(
            Finding(
                id=finding_id,
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=(
                    f"{label} matches an EXPIRED disqualification record on"
                    f" {strong_basis}. The disqualification is no longer in force."
                    f" Period(s): {windows_text}.{permissions}{overflow_note}"
                ),
                evidence=[
                    _evidence(detail_record, strong_excerpt + ", expired"),
                    _evidence(search_record, "search result"),
                ],
            )
        )
    elif tier == "mismatch":
        result.expansion.disqualification_checks.append(
            DisqualificationCheck(
                subject=person.name,
                role=person.role,
                outcome="mismatch",
                detail=mismatch_detail,
            )
        )
        result.findings.append(
            Finding(
                id=finding_id,
                stage=STAGE,
                severity="info",
                confidence="indicated",
                statement=(
                    f"A disqualification record matches {label} on name only:"
                    f" {mismatch_text}.{overflow_note}"
                ),
                evidence=[
                    _evidence(detail_record, mismatch_excerpt),
                    _evidence(search_record, "search result"),
                ],
            )
        )
    else:
        result.expansion.disqualification_checks.append(
            DisqualificationCheck(
                subject=person.name,
                role=person.role,
                outcome="name_only",
                detail=None,
            )
        )
        result.findings.append(
            Finding(
                id=finding_id,
                stage=STAGE,
                severity="info",
                confidence="unverified",
                statement=(
                    f"Possible disqualification record for {label}: the name"
                    f" matches but {uncompared_text}. Requires"
                    f" manual check.{overflow_note}"
                ),
                evidence=[
                    _evidence(detail_record, "name-only match"),
                    _evidence(search_record, "search result"),
                ],
            )
        )
