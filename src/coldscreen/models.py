"""Data models for the screening pipeline.

Implements ARCHITECTURE.md section 6. The core rule lives in the schema:
a Finding without at least one piece of Evidence cannot be constructed.

Registry payload models parse defensively. Unknown fields are ignored and
the raw JSON persisted under evidence/ remains the source of truth.
"""

from __future__ import annotations

import datetime as _datetime
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RESEARCH_AID_DISCLAIMER = (
    "This memo is a research aid generated from public sources at a point in time. "
    "It is not investment advice, not a credit reference, and not a consumer report. "
    "Do not use it for decisions regulated under the US Fair Credit Reporting Act "
    "(employment, credit, tenancy screening) or equivalent regimes. Officer and PSC "
    "data is personal data from public registers; you are responsible for processing "
    "it lawfully. Verify everything independently before acting on it."
)

OGL_ATTRIBUTION = (
    "Contains public sector information licensed under the "
    "Open Government Licence v3.0. Source: Companies House."
)


class Evidence(BaseModel):
    """A pointer to a persisted public source: where it came from and when."""

    source_url: str
    retrieved_at: datetime
    excerpt: str | None = None


class Finding(BaseModel):
    """A single screening observation. Unconstructable without evidence.

    validate_assignment covers attribute assignment after construction, so
    finding.evidence = [] fails too. Note that model_copy(update=...) skips
    validation by pydantic design; the backstop is that every casefile is
    re-validated on load, so an evidence-free finding cannot re-enter the
    pipeline from disk.
    """

    model_config = ConfigDict(validate_assignment=True)

    id: str
    stage: str
    severity: Literal["red", "amber", "info"]
    confidence: Literal["confirmed", "indicated", "unverified"]
    statement: str
    evidence: list[Evidence] = Field(min_length=1)


class Claim(BaseModel):
    """A discrete statement a company makes about itself.

    Populated by the claims extraction stage (stage 6). id is code-assigned
    (CLM-001 style), text is the company's own words as extracted from the
    deck or site, source names where they appeared ("deck p.4", "site
    /about"). Unfalsifiable puffery is kept with checkable False, never
    dropped. Because text is quoted data, it is the one kind of string the
    language gate exempts (span-level, exact match); see coldscreen.language.
    """

    id: str
    text: str
    source: str
    category: Literal["history", "financials", "team", "traction", "regulatory", "other"]
    checkable: bool


class ClaimAssessment(BaseModel):
    """The outcome of testing one checkable claim against the public record.

    basis carries Evidence copied by CODE from the casefile findings the
    model cited; the model never mints an evidence object. record_note is
    one short factual sentence for the memo table's public record column,
    written by the model and fully language-gated like narrative.

    model_assessed distinguishes assessments the model actually returned
    (True, including ones enforcement downgraded to unverified) from the
    unverified rows CODE back-fills for checkable claims the model skipped
    (False). The A4 gate opens only on model-assessed unverified rows: a
    model cannot manufacture "central claim unverifiable" support simply by
    not answering. Defaults True so casefiles written before the field
    existed load unchanged.
    """

    claim_id: str
    status: Literal["supported", "contradicted", "unverified"]
    basis: list[Evidence]
    record_note: str = ""
    model_assessed: bool = True


class Verdict(BaseModel):
    """Rubric-bound verdict. Not produced in the deterministic milestone."""

    level: Literal["red", "amber", "green"]
    triggered: list[str]
    rationale: str
    questions: list[str]


class _RegistryModel(BaseModel):
    """Base for models parsed from registry JSON: tolerate unknown fields."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class CompanyCandidate(_RegistryModel):
    """One company search result, as offered to the user for disambiguation."""

    title: str
    company_number: str
    company_status: str | None = None
    company_type: str | None = None
    date_of_creation: date | None = None
    date_of_cessation: date | None = None
    address_snippet: str | None = None


class PreviousCompanyName(_RegistryModel):
    name: str | None = None
    effective_from: date | None = None
    ceased_on: date | None = None


class CompanyProfile(_RegistryModel):
    """Company profile as returned by /company/{number}, parsed defensively."""

    company_name: str
    company_number: str
    company_status: str | None = None
    company_status_detail: str | None = None
    date_of_creation: date | None = None
    date_of_cessation: date | None = None
    company_type: str | None = Field(default=None, alias="type")
    jurisdiction: str | None = None
    sic_codes: list[str] = Field(default_factory=list)
    registered_office_address: dict[str, Any] = Field(default_factory=dict)
    registered_office_is_in_dispute: bool | None = None
    undeliverable_registered_office_address: bool | None = None
    previous_company_names: list[PreviousCompanyName] = Field(default_factory=list)
    accounts: dict[str, Any] = Field(default_factory=dict)
    confirmation_statement: dict[str, Any] = Field(default_factory=dict)
    links: dict[str, Any] = Field(default_factory=dict)

    @property
    def accounts_overdue(self) -> bool:
        """True when the next accounts are flagged overdue by the registry."""
        next_accounts = self.accounts.get("next_accounts")
        if isinstance(next_accounts, dict):
            return bool(next_accounts.get("overdue"))
        return False

    @property
    def confirmation_statement_overdue(self) -> bool:
        return bool(self.confirmation_statement.get("overdue"))

    @property
    def registered_office_display(self) -> str:
        parts = [
            self.registered_office_address.get(key)
            for key in (
                "care_of",
                "premises",
                "address_line_1",
                "address_line_2",
                "locality",
                "region",
                "postal_code",
                "country",
            )
        ]
        return ", ".join(str(p) for p in parts if p)


class DateOfBirth(_RegistryModel):
    """Partial date of birth as the officer and PSC registers expose it."""

    month: int | None = None
    year: int | None = None


class Officer(_RegistryModel):
    """One officer list entry. resigned_on absent means currently serving."""

    name: str
    officer_role: str | None = None
    appointed_on: date | None = None
    resigned_on: date | None = None
    nationality: str | None = None
    occupation: str | None = None
    country_of_residence: str | None = None
    date_of_birth: DateOfBirth | None = None
    # Corporate officers carry an identification block (registration_number
    # among other keys). Kept loose and parsed defensively where used: the
    # exact shape is not verified in the research notes.
    identification: dict[str, Any] = Field(default_factory=dict)
    links: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.resigned_on is None


class PSC(_RegistryModel):
    """Person with significant control entry. Full schema unverified; keep loose."""

    name: str | None = None
    kind: str | None = None
    natures_of_control: list[str] = Field(default_factory=list)
    notified_on: date | None = None
    ceased_on: date | None = None
    nationality: str | None = None
    date_of_birth: DateOfBirth | None = None

    @property
    def is_individual(self) -> bool:
        """True when the PSC kind marks a natural person."""
        return bool(self.kind) and str(self.kind).startswith("individual")


class Charge(_RegistryModel):
    """One registered charge."""

    charge_code: str | None = None
    status: str | None = None
    created_on: date | None = None
    delivered_on: date | None = None
    satisfied_on: date | None = None
    classification: dict[str, Any] = Field(default_factory=dict)
    particulars: dict[str, Any] = Field(default_factory=dict)
    persons_entitled: list[dict[str, Any]] = Field(default_factory=list)


class InsolvencyCase(_RegistryModel):
    """One insolvency case. Schema unverified upstream; parse defensively."""

    number: int | str | None = None
    type: str | None = None
    dates: list[dict[str, Any]] = Field(default_factory=list)
    practitioners: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class FilingSummary(_RegistryModel):
    """Filing history metadata. Filings are inventoried, never parsed."""

    transaction_id: str | None = None
    category: str | None = None
    filing_type: str | None = Field(default=None, alias="type")
    # The field is named "date" upstream; the module alias avoids the name
    # shadowing the type during annotation evaluation.
    date: _datetime.date | None = None
    description: str | None = None
    action_date: _datetime.date | None = None


class SanctionsSubjectResult(BaseModel):
    """Sanctions match outcome for one screened subject, compact.

    score is identity confidence in 0..1 from the matching engine, not risk.
    matched means at or above the configured threshold. kind "officer and
    psc" marks a person who holds both roles and was screened once.
    """

    subject: str
    kind: Literal["company", "officer", "psc", "officer and psc"]
    query_schema: Literal["Person", "Company"]
    matched: bool = False
    top_score: float | None = None
    datasets: list[str] = Field(default_factory=list)


class SanctionsScreening(BaseModel):
    """Stage 4 summary: what was screened, how, and what came back.

    limit is the per-query candidate cap that was requested;
    dataset_release is whatever dataset release or version marker the
    response exposed, when it exposed one. Both make "no match above
    threshold X" defensible later without opening the raw evidence.
    """

    performed: bool
    dataset: str | None = None
    threshold: float | None = None
    algorithm_requested: str | None = None
    algorithm_resolved: str | None = None
    limit: int | None = None
    dataset_release: str | None = None
    endpoint: str | None = None
    results: list[SanctionsSubjectResult] = Field(default_factory=list)
    skipped_reason: str | None = None
    # True when the stage was attempted and failed after retries: a failed
    # stage is recorded loudly and is never the same as an unconfigured skip.
    failed: bool = False


class AppointmentSummary(BaseModel):
    """One current officer's other current appointments, first degree only."""

    officer_name: str
    other_current_appointments: int = 0
    companies: list[str] = Field(default_factory=list)
    truncated: bool = False


class CoAppointmentOverlap(BaseModel):
    """A company (other than the subject) shared by 2+ current officers."""

    company_number: str
    company_name: str | None = None
    officer_names: list[str] = Field(default_factory=list)


class DisqualificationCheck(BaseModel):
    """Outcome of the disqualification search for one person.

    role "officer and psc" marks a person who holds both roles and was
    searched once. Outcome "mismatch" means the name matched but an
    identifying detail (date of birth, or company number for corporate
    officers) affirmatively differed: likely a different person or company.
    """

    subject: str
    role: Literal["officer", "psc", "officer and psc"]
    outcome: Literal["strong_active", "strong_expired", "name_only", "mismatch", "none"]
    detail: str | None = None


class NetworkExpansion(BaseModel):
    """Stage 3 summary: appointments fan-out, overlaps, disqualifications."""

    performed: bool = False
    appointments: list[AppointmentSummary] = Field(default_factory=list)
    overlaps: list[CoAppointmentOverlap] = Field(default_factory=list)
    disqualification_checks: list[DisqualificationCheck] = Field(default_factory=list)


class MediaItem(BaseModel):
    """One deduplicated adverse media search result.

    The title lives here for the synthesis input; rendered memos never show
    it. Memos list source domain, published date, URL, and query category
    only, so headline vocabulary cannot leak into the memo.
    """

    title: str
    url: str
    source_domain: str
    published: str | None = None
    query_category: str
    snippet: str | None = None


class MediaScreening(BaseModel):
    """Stage 5 summary: query categories, counts, deduplicated items."""

    performed: bool
    provider: str | None = None
    results_per_query: int | None = None
    category_counts: dict[str, int] = Field(default_factory=dict)
    category_counts_deduped: dict[str, int] = Field(default_factory=dict)
    items: list[MediaItem] = Field(default_factory=list)
    skipped_reason: str | None = None
    # True when the stage was attempted and failed after retries. Items
    # gathered before the failure are kept: everything gathered persists.
    failed: bool = False


class ClaimsExtraction(BaseModel):
    """Stage 6 summary: what was ingested and how claims were produced.

    Mirrors the screening summaries of stages 4 and 5: performed False with
    a skipped_reason when there was nothing to extract (no deck and no
    site), provenance fields when the model ran. sources lists the exact
    source labels the model was allowed to cite ("deck p.1", "site /about"),
    deck_sha256 ties the casefile to the exact deck bytes without ever
    persisting the binary, and truncated records that the combined text hit
    the max_claims_chars cap (the finding states the numbers).
    """

    performed: bool
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    parse_retries: int = 0
    deck_file: str | None = None
    deck_sha256: str | None = None
    deck_pages: int | None = None
    site_url: str | None = None
    sources: list[str] = Field(default_factory=list)
    truncated: bool = False
    # Model-returned claims whose text failed quotation verification against
    # their declared source section and were therefore never stored. The
    # count also appears as an explicit finding.
    dropped_claims: int = 0
    skipped_reason: str | None = None


class SynthesisMetadata(BaseModel):
    """How the verdict and narrative were produced, for the audit pack."""

    provider: str
    model: str
    prompt_version: str
    parse_retries: int = 0
    language_retries: int = 0
    enforcement_notes: list[str] = Field(default_factory=list)
    model_level: Literal["red", "amber", "green"] | None = None


class CaseFile(BaseModel):
    """Everything one screening run produced, serialized to casefile.json.

    Extends the ARCHITECTURE.md section 6 sketch with the registry lists the
    memo needs so that `coldscreen rerun` can re-render fully offline from
    this file alone.

    schema_version is the casefile format version, bumped when the shape
    changes incompatibly. It leads the serialized JSON so tooling can
    dispatch on it before parsing the rest.
    """

    schema_version: int = 1
    subject: CompanyProfile
    officers: list[Officer] = Field(default_factory=list)
    pscs: list[PSC] = Field(default_factory=list)
    charges: list[Charge] = Field(default_factory=list)
    filings: list[FilingSummary] = Field(default_factory=list)
    filings_total: int | None = None
    insolvency_cases: list[InsolvencyCase] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    assessments: list[ClaimAssessment] = Field(default_factory=list)
    claims_extraction: ClaimsExtraction | None = None
    sanctions: SanctionsScreening | None = None
    network: NetworkExpansion | None = None
    media: MediaScreening | None = None
    verdict: Verdict | None = None
    narrative: str | None = None
    synthesis: SynthesisMetadata | None = None
    # One-line note rendered in the memo when the enforced verdict level
    # differs from the level the model proposed. Level is a pure function of
    # the enforced trigger set; the model never does that arithmetic.
    verdict_enforcement: str | None = None
    tool_version: str
    screened_at: datetime
    disclaimer: str = RESEARCH_AID_DISCLAIMER
    # True when COLDSCREEN_SCREENED_AT overrode the clock for this run. The
    # memo footer states it, so audit packs cannot be silently backdated.
    clock_override: bool = False
