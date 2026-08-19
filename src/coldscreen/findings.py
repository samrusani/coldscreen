"""Deterministic findings from the registry pass.

Every finding cites at least one piece of evidence, enforced by the Finding
schema. Absence is recorded as findings, never dropped. Severities here are
deterministic facts about the record; verdict logic is a later milestone and
no rubric trigger IDs are cited.

Finding IDs are fixed per check so the same input always produces the same
IDs.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from .ch_client import FetchRecord
from .config import Settings
from .models import Evidence, Finding
from .rubric import INSOLVENCY_STATUS_FAMILY, NOT_ACTIVE_STATUSES
from .stages.registry import RegistryResult, split_officers

STAGE = "registry"

# Status severity mirrors the rubric families: insolvency states are red
# (the R2 status leg), non-active non-insolvency states are amber (A7).
RED_STATUSES = INSOLVENCY_STATUS_FAMILY
AMBER_STATUSES = NOT_ACTIVE_STATUSES


def _evidence(record: FetchRecord, excerpt: str | None = None) -> list[Evidence]:
    return [Evidence(source_url=record.url, retrieved_at=record.retrieved_at, excerpt=excerpt)]


def _full_years(start: date, end: date) -> int:
    years = end.year - start.year
    if (end.month, end.day) < (start.month, start.day):
        years -= 1
    return max(years, 0)


def _truncation_statement(
    label: str, retrieved: int, total: int, page_cap_hit: bool, server_clamped: bool
) -> str:
    """State what was retrieved and why retrieval stopped, without guessing."""
    statement = f"{label} is truncated: retrieved {retrieved} of {total} records."
    if page_cap_hit:
        statement += " Retrieval stopped at the configured page cap."
    if server_clamped:
        statement += " The server returned smaller pages than requested."
    return statement


def build_findings(result: RegistryResult, settings: Settings, today: date) -> list[Finding]:
    """Build the weekend 1 finding set from one registry pass."""
    findings: list[Finding] = []
    profile = result.profile
    profile_record = result.profile_record

    # REG-001 company status. Severity matching is casefolded so a
    # mixed-case registry value cannot silently miss the family sets; the
    # statement keeps the registry's own spelling.
    status = (profile.company_status or "unknown").strip() or "unknown"
    severity: Literal["red", "amber", "info"]
    if status.casefold() in RED_STATUSES:
        severity = "red"
    elif status.casefold() in AMBER_STATUSES:
        severity = "amber"
    else:
        severity = "info"
    detail = f" ({profile.company_status_detail})" if profile.company_status_detail else ""
    findings.append(
        Finding(
            id="REG-001",
            stage=STAGE,
            severity=severity,
            confidence="confirmed",
            statement=f"Company status is {status}{detail}.",
            evidence=_evidence(profile_record, f"company_status={status}"),
        )
    )

    # REG-002 incorporation date and age
    if profile.date_of_creation is not None:
        age_years = _full_years(profile.date_of_creation, today)
        statement = (
            f"Incorporated on {profile.date_of_creation.isoformat()}"
            f" ({age_years} full years before the screening date)."
        )
        excerpt = f"date_of_creation={profile.date_of_creation.isoformat()}"
    else:
        statement = "The registry profile shows no incorporation date."
        excerpt = "date_of_creation absent"
    findings.append(
        Finding(
            id="REG-002",
            stage=STAGE,
            severity="info",
            confidence="confirmed",
            statement=statement,
            evidence=_evidence(profile_record, excerpt),
        )
    )

    # REG-003 accounts overdue
    if profile.accounts_overdue:
        findings.append(
            Finding(
                id="REG-003",
                stage=STAGE,
                severity="amber",
                confidence="confirmed",
                statement="The registry marks the next accounts as overdue.",
                evidence=_evidence(profile_record, "accounts.next_accounts.overdue=true"),
            )
        )

    # REG-004 confirmation statement overdue
    if profile.confirmation_statement_overdue:
        findings.append(
            Finding(
                id="REG-004",
                stage=STAGE,
                severity="amber",
                confidence="confirmed",
                statement="The registry marks the confirmation statement as overdue.",
                evidence=_evidence(profile_record, "confirmation_statement.overdue=true"),
            )
        )

    # REG-005 registered office flags
    office_flags = []
    if profile.registered_office_is_in_dispute:
        office_flags.append("in dispute")
    if profile.undeliverable_registered_office_address:
        office_flags.append("undeliverable")
    if office_flags:
        findings.append(
            Finding(
                id="REG-005",
                stage=STAGE,
                severity="amber",
                confidence="confirmed",
                statement=f"The registered office address is {' and '.join(office_flags)}.",
                evidence=_evidence(profile_record, "registered office flags set"),
            )
        )

    # REG-006 charges
    if not result.charges_link_present:
        findings.append(
            Finding(
                id="REG-006",
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=(
                    "No charges are registered. The company profile offers no"
                    " charges link, so the charges register was not queried."
                ),
                evidence=_evidence(profile_record, "links.charges absent"),
            )
        )
    elif result.charges is None:
        charges_404 = result.first_record("charges_p1") or profile_record
        findings.append(
            Finding(
                id="REG-006",
                stage=STAGE,
                severity="amber",
                confidence="unverified",
                statement=(
                    "The company profile links to registered charges, but the"
                    " charges resource returned 404 and could not be retrieved,"
                    " so the charge record is unverified."
                ),
                evidence=_evidence(charges_404, "links.charges present, fetch 404"),
            )
        )
    else:
        charges_record = result.first_record("charges_p1") or profile_record
        outstanding = sum(1 for c in result.charges if c.status == "outstanding")
        satisfied = sum(1 for c in result.charges if c.status in {"fully-satisfied", "satisfied"})
        part_satisfied = sum(1 for c in result.charges if c.status == "part-satisfied")
        parts = [f"{outstanding} outstanding", f"{satisfied} satisfied"]
        if part_satisfied:
            parts.append(f"{part_satisfied} part satisfied")
        findings.append(
            Finding(
                id="REG-006",
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=(
                    f"Charges register: {len(result.charges)} charge(s) listed"
                    f" ({', '.join(parts)})."
                ),
                evidence=_evidence(
                    charges_record,
                    f"items={len(result.charges)} outstanding={outstanding}",
                ),
            )
        )

    # REG-007 insolvency
    if not result.insolvency_link_present:
        findings.append(
            Finding(
                id="REG-007",
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=(
                    "No insolvency history is registered. The company profile"
                    " offers no insolvency link, so the insolvency resource was"
                    " not queried."
                ),
                evidence=_evidence(profile_record, "links.insolvency absent"),
            )
        )
    elif result.insolvency_cases is None:
        insolvency_404 = result.first_record("insolvency") or profile_record
        findings.append(
            Finding(
                id="REG-007",
                stage=STAGE,
                severity="amber",
                confidence="unverified",
                statement=(
                    "The company profile links to insolvency history, but the"
                    " insolvency resource returned 404 and could not be"
                    " retrieved. Companies House documents this response as"
                    " ambiguous, so the insolvency record is unverified."
                ),
                evidence=_evidence(insolvency_404, "links.insolvency present, fetch 404"),
            )
        )
    else:
        insolvency_record = result.first_record("insolvency") or profile_record
        count = len(result.insolvency_cases)
        types = sorted({c.type for c in result.insolvency_cases if c.type})
        type_note = f" Case types: {', '.join(types)}." if types else ""
        findings.append(
            Finding(
                id="REG-007",
                stage=STAGE,
                severity="red",
                confidence="confirmed",
                statement=f"Insolvency history is registered: {count} case(s).{type_note}",
                evidence=_evidence(insolvency_record, f"cases={count}"),
            )
        )

    # Officers
    officers_record = result.first_record("officers_p1") or profile_record
    current, _resigned_recent = split_officers(
        result.officers, today, settings.officer_lookback_years
    )
    # Prefer the register's own active_count; the fetched list backs the
    # tables and the resignation window, which the register does not expose.
    top_level_active = result.officers_top_level.get("active_count")
    active_count = top_level_active if isinstance(top_level_active, int) else len(current)

    # REG-008 active officers
    findings.append(
        Finding(
            id="REG-008",
            stage=STAGE,
            severity="info",
            confidence="confirmed",
            statement=f"{active_count} active officer(s) on the register.",
            evidence=_evidence(officers_record, f"active officers={active_count}"),
        )
    )

    # REG-009 resignations in the last 12 months
    twelve_months_ago = today - timedelta(days=365)
    resigned_12m = [
        o
        for o in result.officers
        if o.resigned_on is not None and o.resigned_on >= twelve_months_ago
    ]
    threshold = max(float(settings.wholesale_change_min), active_count / 2)
    if len(resigned_12m) >= threshold:
        findings.append(
            Finding(
                id="REG-009",
                stage=STAGE,
                severity="amber",
                confidence="confirmed",
                statement=(
                    f"Wholesale officer changes: {len(resigned_12m)} officer(s)"
                    f" resigned in the 12 months before the screening date,"
                    f" against {active_count} currently active."
                ),
                evidence=_evidence(
                    officers_record, f"resignations last 12 months={len(resigned_12m)}"
                ),
            )
        )
    else:
        findings.append(
            Finding(
                id="REG-009",
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=(
                    f"{len(resigned_12m)} officer resignation(s) in the 12 months"
                    " before the screening date."
                ),
                evidence=_evidence(
                    officers_record, f"resignations last 12 months={len(resigned_12m)}"
                ),
            )
        )

    # REG-010 previous company names
    if profile.previous_company_names:
        names = [p.name for p in profile.previous_company_names if p.name]
        listed = "; ".join(names) if names else "unnamed entries"
        findings.append(
            Finding(
                id="REG-010",
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=(
                    f"The company has {len(profile.previous_company_names)} previous"
                    f" name(s) on the register: {listed}."
                ),
                evidence=_evidence(
                    profile_record,
                    f"previous_company_names={len(profile.previous_company_names)}",
                ),
            )
        )

    # REG-011 persons with significant control
    if not result.pscs_link_present:
        findings.append(
            Finding(
                id="REG-011",
                stage=STAGE,
                severity="info",
                confidence="indicated",
                statement=(
                    "No persons with significant control entries are linked on"
                    " the register for this company."
                ),
                evidence=_evidence(profile_record, "links.persons_with_significant_control absent"),
            )
        )
    elif not result.pscs:
        psc_record = result.first_record("psc_p1") or profile_record
        if psc_record.status == 404:
            findings.append(
                Finding(
                    id="REG-011",
                    stage=STAGE,
                    severity="info",
                    confidence="indicated",
                    statement=(
                        "The company profile links to PSC data, but the PSC"
                        " resource returned 404; no entries were retrievable."
                    ),
                    evidence=_evidence(psc_record, "psc fetch 404"),
                )
            )
        else:
            findings.append(
                Finding(
                    id="REG-011",
                    stage=STAGE,
                    severity="info",
                    confidence="confirmed",
                    statement="The PSC register returned no entries.",
                    evidence=_evidence(psc_record, "psc items=0"),
                )
            )
    else:
        psc_record = result.first_record("psc_p1") or profile_record
        active_pscs = [p for p in result.pscs if p.ceased_on is None]
        natures: list[str] = []
        for psc in active_pscs:
            natures.extend(psc.natures_of_control)
        nature_note = f" Natures of control: {', '.join(sorted(set(natures)))}." if natures else ""
        findings.append(
            Finding(
                id="REG-011",
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=(
                    f"{len(result.pscs)} PSC entry(ies) on the register,"
                    f" {len(active_pscs)} not ceased.{nature_note}"
                ),
                evidence=_evidence(psc_record, f"psc items={len(result.pscs)}"),
            )
        )

    # REG-012 filing history truncation
    if result.filings_truncated and result.filings_total is not None:
        filing_record = result.first_record("filing_history_p1") or profile_record
        findings.append(
            Finding(
                id="REG-012",
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=_truncation_statement(
                    "Filing history",
                    len(result.filings),
                    result.filings_total,
                    result.filings_page_cap_hit,
                    result.filings_server_clamped,
                ),
                evidence=_evidence(
                    filing_record,
                    f"retrieved={len(result.filings)} total={result.filings_total}",
                ),
            )
        )

    # REG-013 officers truncation
    if result.officers_truncated and result.officers_total is not None:
        findings.append(
            Finding(
                id="REG-013",
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=_truncation_statement(
                    "The officer list",
                    len(result.officers),
                    result.officers_total,
                    result.officers_page_cap_hit,
                    result.officers_server_clamped,
                ),
                evidence=_evidence(
                    officers_record,
                    f"retrieved={len(result.officers)} total={result.officers_total}",
                ),
            )
        )

    # REG-014 PSC truncation
    if result.pscs is not None and result.pscs_truncated and result.pscs_total is not None:
        psc_record = result.first_record("psc_p1") or profile_record
        findings.append(
            Finding(
                id="REG-014",
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=_truncation_statement(
                    "The PSC list",
                    len(result.pscs),
                    result.pscs_total,
                    result.pscs_page_cap_hit,
                    result.pscs_server_clamped,
                ),
                evidence=_evidence(
                    psc_record,
                    f"retrieved={len(result.pscs)} total={result.pscs_total}",
                ),
            )
        )

    # REG-015 charges truncation. The list is incomplete when unique
    # retrieved items are fewer than total_count. State why retrieval
    # stopped: page cap, or a later page that did not advance.
    if (
        result.charges is not None
        and result.charges_total is not None
        and result.charges_total > len(result.charges)
    ):
        charges_truncation_record = result.first_record("charges_p1") or profile_record
        statement = (
            f"The charges list is truncated: retrieved {len(result.charges)}"
            f" of {result.charges_total} charge(s)."
        )
        if result.charges_page_cap_hit:
            statement += " Retrieval stopped at the configured page cap."
        elif result.charges_did_not_advance:
            statement += " Retrieval stopped because further pages did not advance."
        findings.append(
            Finding(
                id="REG-015",
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=statement,
                evidence=_evidence(
                    charges_truncation_record,
                    f"retrieved={len(result.charges)} total={result.charges_total}",
                ),
            )
        )

    return findings
