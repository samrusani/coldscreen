"""Stage 2: registry pass.

Fetches the profile, officers, PSC register, filing history, and, only when
the profile links to them, charges, insolvency, and PSC data. Every raw
response the stage receives is kept as a named fetch record for evidence
persistence, including 404s on linked resources, so the audit pack shows
what actually came back.

Load-bearing details from wiki/research/companies-house.md:

- The deprecated has_charges and has_insolvency_history flags are ignored.
  Presence of links.charges and links.insolvency on the profile is the gate.
- An insolvency 404 is ambiguous upstream (no record OR no such company).
  If it ever occurs despite the links gate it is treated as "no insolvency
  record retrieved", never as "company not found". The same applies to a
  charges 404 behind a charges link.
- Officers and filing history 404s are treated as empty result sets; the
  profile has already proven the company exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..ch_client import CompaniesHouseClient, FetchRecord, NotFoundError, PaginatedResult
from ..config import Settings
from ..models import (
    PSC,
    Charge,
    CompanyProfile,
    FilingSummary,
    InsolvencyCase,
    Officer,
)


@dataclass(frozen=True)
class NamedRecord:
    """A raw response plus the evidence file stem it will be persisted under."""

    name: str
    record: FetchRecord


@dataclass
class RegistryResult:
    """Everything stage 2 collected, parsed and raw.

    For pscs, charges, and insolvency_cases, None means no data was
    retrieved: either the profile offered no link (link flag False, the
    endpoint was never called) or the linked resource 404ed (link flag True,
    the 404 is persisted). An empty list means the endpoint answered with
    zero items. Findings distinguish all three.
    """

    profile: CompanyProfile
    profile_record: FetchRecord
    officers: list[Officer] = field(default_factory=list)
    officers_top_level: dict[str, Any] = field(default_factory=dict)
    officers_total: int | None = None
    officers_truncated: bool = False
    officers_page_cap_hit: bool = False
    officers_server_clamped: bool = False
    pscs: list[PSC] | None = None
    pscs_link_present: bool = False
    pscs_total: int | None = None
    pscs_truncated: bool = False
    pscs_page_cap_hit: bool = False
    pscs_server_clamped: bool = False
    filings: list[FilingSummary] = field(default_factory=list)
    filings_total: int | None = None
    filings_truncated: bool = False
    filings_page_cap_hit: bool = False
    filings_server_clamped: bool = False
    charges: list[Charge] | None = None
    charges_link_present: bool = False
    charges_total: int | None = None
    charges_truncated: bool = False
    charges_page_cap_hit: bool = False
    charges_server_clamped: bool = False
    charges_did_not_advance: bool = False
    insolvency_cases: list[InsolvencyCase] | None = None
    insolvency_link_present: bool = False
    records: list[NamedRecord] = field(default_factory=list)

    def first_record(self, name: str) -> FetchRecord | None:
        for named in self.records:
            if named.name == name:
                return named.record
        return None


def split_officers(
    officers: list[Officer], today: date, lookback_years: int
) -> tuple[list[Officer], list[Officer]]:
    """Current officers, and officers resigned within the lookback window."""
    cutoff = today - timedelta(days=round(lookback_years * 365.25))
    current = [o for o in officers if o.resigned_on is None]
    resigned_recent = [o for o in officers if o.resigned_on is not None and o.resigned_on >= cutoff]
    return current, resigned_recent


def _empty_paginated() -> PaginatedResult:
    return PaginatedResult(items=[], records=[], total=None, truncated=False)


def _keep_404(result: RegistryResult, name: str, error: NotFoundError) -> None:
    """Persist an absorbed 404 so the audit pack shows the response."""
    if error.record is not None:
        result.records.append(NamedRecord(name, error.record))


def run_registry_pass(
    client: CompaniesHouseClient, company_number: str, settings: Settings
) -> RegistryResult:
    """Fetch the weekend 1 registry surface for one company."""
    profile_record = client.company_profile(company_number)
    profile = CompanyProfile.model_validate(profile_record.body)
    result = RegistryResult(profile=profile, profile_record=profile_record)
    result.records.append(NamedRecord("registry_profile", profile_record))
    links = profile.links if isinstance(profile.links, dict) else {}

    try:
        officers_pages = client.officers(
            company_number,
            items_per_page=settings.items_per_page,
            max_pages=settings.max_pages_officers,
        )
    except NotFoundError as error:
        _keep_404(result, "officers_p1", error)
        officers_pages = _empty_paginated()
    for index, record in enumerate(officers_pages.records, start=1):
        result.records.append(NamedRecord(f"officers_p{index}", record))
    result.officers = [Officer.model_validate(item) for item in officers_pages.items]
    result.officers_top_level = dict(officers_pages.top_level)
    result.officers_total = officers_pages.total
    result.officers_truncated = officers_pages.truncated
    result.officers_page_cap_hit = officers_pages.hit_page_cap
    result.officers_server_clamped = officers_pages.server_clamped

    if links.get("persons_with_significant_control"):
        result.pscs_link_present = True
        psc_pages: PaginatedResult | None
        try:
            psc_pages = client.pscs(
                company_number,
                items_per_page=settings.items_per_page,
                max_pages=settings.max_pages_psc,
            )
        except NotFoundError as error:
            _keep_404(result, "psc_p1", error)
            psc_pages = _empty_paginated()
        for index, record in enumerate(psc_pages.records, start=1):
            result.records.append(NamedRecord(f"psc_p{index}", record))
        result.pscs = [PSC.model_validate(item) for item in psc_pages.items]
        result.pscs_total = psc_pages.total
        result.pscs_truncated = psc_pages.truncated
        result.pscs_page_cap_hit = psc_pages.hit_page_cap
        result.pscs_server_clamped = psc_pages.server_clamped

    try:
        filing_pages = client.filing_history(
            company_number,
            items_per_page=settings.items_per_page,
            max_pages=settings.max_pages_filing_history,
        )
    except NotFoundError as error:
        _keep_404(result, "filing_history_p1", error)
        filing_pages = _empty_paginated()
    for index, record in enumerate(filing_pages.records, start=1):
        result.records.append(NamedRecord(f"filing_history_p{index}", record))
    result.filings = [FilingSummary.model_validate(item) for item in filing_pages.items]
    result.filings_total = filing_pages.total
    result.filings_truncated = filing_pages.truncated
    result.filings_page_cap_hit = filing_pages.hit_page_cap
    result.filings_server_clamped = filing_pages.server_clamped

    if links.get("charges"):
        result.charges_link_present = True
        try:
            charges_pages = client.charges(
                company_number,
                items_per_page=settings.items_per_page,
                max_pages=settings.max_pages_charges,
            )
        except NotFoundError as error:
            # Same posture as insolvency below: the link promised data, the
            # resource 404ed. Keep the 404 as evidence, leave charges None.
            _keep_404(result, "charges_p1", error)
        else:
            for index, record in enumerate(charges_pages.records, start=1):
                result.records.append(NamedRecord(f"charges_p{index}", record))
            result.charges = [Charge.model_validate(item) for item in charges_pages.items]
            result.charges_total = charges_pages.total
            result.charges_truncated = charges_pages.truncated
            result.charges_page_cap_hit = charges_pages.hit_page_cap
            result.charges_server_clamped = charges_pages.server_clamped
            result.charges_did_not_advance = charges_pages.did_not_advance

    if links.get("insolvency"):
        result.insolvency_link_present = True
        try:
            insolvency_record: FetchRecord | None = client.insolvency(company_number)
        except NotFoundError as error:
            # Ambiguous by documentation: nothing retrievable, but never
            # evidence that the company does not exist. insolvency_cases
            # stays None; the 404 itself is persisted as evidence.
            _keep_404(result, "insolvency", error)
            insolvency_record = None
        if insolvency_record is not None:
            result.records.append(NamedRecord("insolvency", insolvency_record))
            body = insolvency_record.body if isinstance(insolvency_record.body, dict) else {}
            cases = body.get("cases") or []
            result.insolvency_cases = [
                InsolvencyCase.model_validate(case) for case in cases if isinstance(case, dict)
            ]

    return result
