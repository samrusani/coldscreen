"""Stage 6: claims extraction. The model turns deck and site text into
discrete Claim objects; code owns everything around that.

The model receives ONE deterministic JSON document (the labeled text
sections) plus the versioned claims prompt, and returns claims under a
strict JSON schema whose source field is an enum of the exact section
labels on offer, so a claim cannot cite a page that was never provided.
Every claim text is then VERIFIED as a quotation: after normalize_for_match
on both sides it must be a substring of its declared source section's
extracted text, or it is dropped with a counted finding and never stored.
Stored claim texts are the only strings the language gates ever exempt, so
this verification is the trust boundary of the whole exemption design.
Claim ids are assigned by CODE (CLM-001 style, in output order after the
drop), never taken from the model. Unfalsifiable puffery is kept with
checkable False; dropping it would hide exactly the vocabulary a screen
should surface.

Parse failures are re-prompted within the same retry budget as synthesis,
then fail cleanly: no claims are ever fabricated by this tool. A failed
extraction raises ClaimsExtractionError carrying the partial stage result,
so the CLI can keep the audit pack (deck and site evidence included) while
the run exits nonzero.

When no deck and no site are given the stage is skipped with the
established not_run pattern: a synthetic note record plus an explicit
finding, because absence of claims screening is data.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .ch_client import FetchRecord
from .config import Settings
from .deck import DeckExtraction, deck_record
from .language import normalize_for_match
from .models import Claim, ClaimsExtraction, CompanyProfile, Evidence, Finding
from .providers import Message, ModelProvider
from .site import SiteFetchResult
from .stages.registry import NamedRecord
from .synthesis import MAX_PARSE_RETRIES

ClockFn = Callable[[], datetime]

STAGE = "claims"
NOT_RUN_URL = "coldscreen:not-run/claims"
MAX_CLAIMS = 25

_PROMPT_VERSION_RE = re.compile(r"coldscreen claims prompt, version ([0-9A-Za-z][0-9A-Za-z.]*)")

CATEGORIES = ("history", "financials", "team", "traction", "regulatory", "other")


class ClaimsExtractionError(Exception):
    """Claims extraction failed cleanly. Carries the partial stage result so
    the caller can still persist the deck and site evidence it produced."""

    def __init__(self, message: str, result: ClaimsStageResult) -> None:
        super().__init__(message)
        self.result = result


class _RawClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    source: str
    category: Literal["history", "financials", "team", "traction", "regulatory", "other"]
    checkable: bool


class ClaimsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[_RawClaim] = Field(max_length=MAX_CLAIMS)


def load_prompt() -> str:
    return (resources.files("coldscreen") / "prompts" / "claims.md").read_text(encoding="utf-8")


def prompt_version(prompt_text: str) -> str:
    match = _PROMPT_VERSION_RE.search(prompt_text)
    if match is None:
        raise ClaimsExtractionError(
            "the claims prompt file carries no version marker; refusing to run",
            ClaimsStageResult(),
        )
    return match.group(1)


def build_claims_schema(source_labels: list[str]) -> dict[str, Any]:
    """The output schema, with source constrained to the labels on offer."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["claims"],
        "properties": {
            "claims": {
                "type": "array",
                "maxItems": MAX_CLAIMS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["text", "source", "category", "checkable"],
                    "properties": {
                        "text": {"type": "string", "minLength": 1},
                        "source": {"type": "string", "enum": list(source_labels)},
                        "category": {"type": "string", "enum": list(CATEGORIES)},
                        "checkable": {"type": "boolean"},
                    },
                },
            }
        },
    }


@dataclass(frozen=True)
class Section:
    """One labeled block of extracted text offered to the model."""

    source: str
    text: str


@dataclass
class ClaimsStageResult:
    """Everything stage 6 produced: claims, findings, evidence records."""

    findings: list[Finding] = field(default_factory=list)
    records: list[NamedRecord] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    extraction: ClaimsExtraction = field(default_factory=lambda: ClaimsExtraction(performed=False))


def normalize_claim_text(text: str) -> str:
    """Whitespace-collapsed single line. The stored string IS the rendered
    string, so the language gate's span exemption can match it exactly."""
    return " ".join(text.split())


def build_sections(
    deck: DeckExtraction | None,
    site: SiteFetchResult | None,
    max_claims_chars: int,
) -> tuple[list[Section], bool, int]:
    """Labeled sections in stage order, capped at max_claims_chars combined.

    Returns (sections, truncated, dropped_chars). The cap is applied across
    deck plus site text: a section straddling the cap is cut mid-text, and
    later sections are dropped entirely.
    """
    raw: list[Section] = []
    if deck is not None:
        raw.extend(
            Section(source=page.source_label, text=page.text) for page in deck.pages if page.text
        )
    if site is not None:
        raw.extend(
            Section(source=page.source_label, text=page.text) for page in site.pages if page.text
        )

    sections: list[Section] = []
    used = 0
    dropped = 0
    truncated = False
    for section in raw:
        remaining = max_claims_chars - used
        if remaining <= 0:
            truncated = True
            dropped += len(section.text)
            continue
        if len(section.text) > remaining:
            truncated = True
            dropped += len(section.text) - remaining
            sections.append(Section(source=section.source, text=section.text[:remaining]))
            used = max_claims_chars
        else:
            sections.append(section)
            used += len(section.text)
    return sections, truncated, dropped


def build_claims_input(profile: CompanyProfile, sections: list[Section]) -> dict[str, Any]:
    return {
        "company_name": profile.company_name,
        "company_number": profile.company_number,
        "sections": [{"source": s.source, "text": s.text} for s in sections],
    }


def serialize_input(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _try_parse(raw: str, allowed_sources: set[str]) -> tuple[ClaimsOutput | None, str]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except ValueError as error:
        return None, f"invalid JSON: {error}"
    try:
        parsed = ClaimsOutput.model_validate(data)
    except ValidationError as error:
        issues = "; ".join(
            f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in error.errors()
        )
        return None, f"schema mismatch: {issues}"
    bad_sources = sorted({c.source for c in parsed.claims} - allowed_sources)
    if bad_sources:
        return None, (
            "schema mismatch: source must be one of the provided section labels;"
            f" got {len(bad_sources)} other value(s)"
        )
    return parsed, ""


def _extract_claims(
    profile: CompanyProfile,
    sections: list[Section],
    provider: ModelProvider,
) -> tuple[list[Claim], int, int]:
    """The model loop: schema-constrained extraction with parse retries,
    then quotation verification before anything is stored.

    A claim's text is stored ONLY when, after normalize_for_match on both
    sides, it is a substring of the extracted text of its DECLARED source
    section. This is the trust boundary for the language-gate exemption:
    stored claim texts render verbatim in the memo table and act as span
    exemptions there, so a model must not be able to mint one. Claims that
    fail verification are dropped and counted, never retried and never
    stored; ids are assigned after the drop so CLM numbering stays
    contiguous over what actually exists.

    Returns (claims, parse_retries, dropped_count). Raises ValueError with
    the last parse error when the budget is exhausted; the caller wraps it
    with stage context.
    """
    prompt = load_prompt()
    labels = [section.source for section in sections]
    schema = build_claims_schema(labels)
    allowed = set(labels)
    normalized_sections = {
        section.source: normalize_for_match(section.text) for section in sections
    }
    input_doc = serialize_input(build_claims_input(profile, sections))
    messages: list[Message] = [Message(role="user", content=input_doc)]
    parse_retries = 0

    while True:
        raw = provider.complete(prompt, messages, json_schema=schema)
        parsed, parse_error = _try_parse(raw, allowed)
        if parsed is not None:
            claims: list[Claim] = []
            seen: set[tuple[str, str]] = set()
            dropped = 0
            for raw_claim in parsed.claims:
                text = normalize_claim_text(raw_claim.text)
                if not text:
                    continue
                key = (text, raw_claim.source)
                if key in seen:
                    continue  # an exact repeat adds nothing to the table
                seen.add(key)
                normalized = normalize_for_match(text)
                if not normalized or normalized not in normalized_sections[raw_claim.source]:
                    dropped += 1
                    continue
                claims.append(
                    Claim(
                        id=f"CLM-{len(claims) + 1:03d}",
                        text=text,
                        source=raw_claim.source,
                        category=raw_claim.category,
                        checkable=raw_claim.checkable,
                    )
                )
            return claims, parse_retries, dropped
        if parse_retries >= MAX_PARSE_RETRIES:
            raise ValueError(parse_error)
        parse_retries += 1
        messages = messages + [
            Message(role="assistant", content=raw),
            Message(
                role="user",
                content=(
                    f"Your previous response was not valid: {parse_error}."
                    " Return ONLY a single JSON object matching the output"
                    " contract, with no markdown fences and no commentary."
                ),
            ),
        ]


def _not_run(now: ClockFn) -> ClaimsStageResult:
    result = ClaimsStageResult()
    note_record = FetchRecord(
        url=NOT_RUN_URL,
        params={},
        status=0,
        body={
            "kind": "not_run",
            "stage": "claims",
            "reason": "no deck or site provided; there is nothing to extract claims from",
        },
        retrieved_at=now(),
    )
    result.records.append(NamedRecord("claims_not_run", note_record))
    result.extraction = ClaimsExtraction(performed=False, skipped_reason="no deck or site provided")
    result.findings.append(
        Finding(
            id="EXT-000",
            stage=STAGE,
            severity="info",
            confidence="confirmed",
            statement=(
                "Claims extraction not performed: no deck or site was provided."
                " What the company says about itself was not screened this run."
            ),
            evidence=[
                Evidence(
                    source_url=NOT_RUN_URL,
                    retrieved_at=note_record.retrieved_at,
                    excerpt="kind=not_run",
                )
            ],
        )
    )
    return result


def _deck_findings(result: ClaimsStageResult, deck: DeckExtraction, record: FetchRecord) -> None:
    deck_evidence = [
        Evidence(
            source_url=deck.pseudo_url,
            retrieved_at=record.retrieved_at,
            excerpt=f"sha256={deck.sha256}",
        )
    ]
    if deck.has_text:
        statement = (
            f"Deck ingested: {deck.file_name}, {deck.page_count} page(s),"
            f" text extracted from {sum(1 for p in deck.pages if p.text)} page(s)"
            f" (sha256 {deck.sha256})."
        )
    else:
        statement = (
            f"Deck {deck.file_name} ({deck.page_count} page(s), sha256 {deck.sha256})"
            " contained no extractable text. Image-only decks cannot be screened"
            " for claims; no OCR is attempted."
        )
    result.findings.append(
        Finding(
            id="EXT-001",
            stage=STAGE,
            severity="info",
            confidence="confirmed",
            statement=statement,
            evidence=deck_evidence,
        )
    )
    if deck.truncated_pages:
        result.findings.append(
            Finding(
                id="EXT-002",
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=(
                    f"Deck reading is truncated: {len(deck.pages)} of"
                    f" {deck.page_count} page(s) were read (max_deck_pages cap)."
                ),
                evidence=deck_evidence,
            )
        )


def _site_findings(result: ClaimsStageResult, site: SiteFetchResult, now: ClockFn) -> None:
    page_records = [n.record for n in site.records if n.name.startswith("site_0")]
    evidence_records = page_records or [n.record for n in site.records]
    evidence = [
        Evidence(source_url=r.url, retrieved_at=r.retrieved_at, excerpt="site fetch")
        for r in evidence_records[:5]
    ]
    if not evidence:
        # No response was received at all; the pseudo-evidence names the URL
        # that was attempted so the absence is still traceable.
        evidence = [
            Evidence(
                source_url=site.site_url,
                retrieved_at=now(),
                excerpt="no responses received",
            )
        ]
    fetched = f"Site ingested: {len(site.pages)} page(s) with text retrieved from {site.site_url}"
    if site.pages:
        fetched += " (" + ", ".join(page.path for page in site.pages) + ")"
    fetched += "."
    if site.failures:
        fetched += " Failures: " + "; ".join(site.failures) + "."
    result.findings.append(
        Finding(
            id="EXT-003",
            stage=STAGE,
            severity="info",
            confidence="confirmed" if page_records else "indicated",
            statement=fetched,
            evidence=evidence,
        )
    )
    if site.robots_skipped:
        robots_records = [n.record for n in site.records if n.name == "site_robots"]
        robots_evidence = [
            Evidence(source_url=r.url, retrieved_at=r.retrieved_at, excerpt="robots.txt")
            for r in robots_records
        ] or evidence
        result.findings.append(
            Finding(
                id="EXT-004",
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=(
                    "Site paths skipped per robots.txt: "
                    + ", ".join(site.robots_skipped)
                    + ". Disallowed paths are never fetched."
                ),
                evidence=robots_evidence,
            )
        )


def run_claims_stage(
    profile: CompanyProfile,
    deck: DeckExtraction | None,
    site: SiteFetchResult | None,
    provider: ModelProvider | None,
    provider_name: str | None,
    model: str | None,
    settings: Settings,
    now: ClockFn,
) -> ClaimsStageResult:
    """Run stage 6, or record the explicit skip when there is no input.

    Raises ClaimsExtractionError (carrying the partial result) when the
    model cannot produce valid claims within the retry budget.
    """
    if deck is None and site is None:
        return _not_run(now)
    if provider is None or provider_name is None or model is None:
        raise AssertionError("claims extraction requires a configured model provider")

    result = ClaimsStageResult()
    deck_rec: FetchRecord | None = None
    if deck is not None:
        deck_rec = deck_record(deck, now)
        result.records.append(NamedRecord("deck_text", deck_rec))
        _deck_findings(result, deck, deck_rec)
    if site is not None:
        result.records.extend(site.records)
        _site_findings(result, site, now)

    sections, truncated, dropped_chars = build_sections(deck, site, settings.max_claims_chars)
    if truncated:
        kept_chars = sum(len(s.text) for s in sections)
        truncation_evidence: list[Evidence] = []
        if deck_rec is not None:
            truncation_evidence.append(
                Evidence(
                    source_url=deck_rec.url,
                    retrieved_at=deck_rec.retrieved_at,
                    excerpt="deck text",
                )
            )
        if site is not None:
            truncation_evidence.extend(
                Evidence(source_url=n.record.url, retrieved_at=n.record.retrieved_at)
                for n in site.records
                if n.name.startswith("site_0")
            )
        result.findings.append(
            Finding(
                id="EXT-005",
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=(
                    f"Claims input is truncated: {kept_chars} character(s) of deck"
                    f" and site text were kept and {dropped_chars} dropped at the"
                    " max_claims_chars cap. Claims can only come from the kept text."
                ),
                evidence=truncation_evidence[:5],
            )
        )

    prompt_ver = prompt_version(load_prompt())
    result.extraction = ClaimsExtraction(
        performed=False,
        provider=provider_name,
        model=model,
        prompt_version=prompt_ver,
        deck_file=deck.file_name if deck is not None else None,
        deck_sha256=deck.sha256 if deck is not None else None,
        deck_pages=deck.page_count if deck is not None else None,
        site_url=site.site_url if site is not None else None,
        sources=[s.source for s in sections],
        truncated=truncated,
    )

    if not sections:
        result.extraction.skipped_reason = "no text could be extracted from the provided inputs"
        result.findings.append(
            Finding(
                id="EXT-006",
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=(
                    "Claims extraction not performed: the provided deck and site"
                    " inputs yielded no text to extract claims from."
                ),
                evidence=_outcome_evidence(result, deck_rec, site, now),
            )
        )
        return result

    try:
        claims, parse_retries, dropped = _extract_claims(profile, sections, provider)
    except ValueError as error:
        raise ClaimsExtractionError(
            "claims extraction failed: the model did not produce valid output"
            f" after {MAX_PARSE_RETRIES + 1} attempts (last error: {error})."
            " No claims are stored; nothing is fabricated.",
            result,
        ) from None

    result.claims = claims
    result.extraction.performed = True
    result.extraction.parse_retries = parse_retries
    result.extraction.dropped_claims = dropped
    checkable = sum(1 for c in claims if c.checkable)
    result.findings.append(
        Finding(
            id="EXT-006",
            stage=STAGE,
            severity="info",
            confidence="confirmed",
            statement=(
                f"Claims extraction produced {len(claims)} claim(s) from"
                f" {len(sections)} text section(s): {checkable} checkable,"
                f" {len(claims) - checkable} not checkable (puffery is listed,"
                " never dropped)."
            ),
            evidence=_outcome_evidence(result, deck_rec, site, now),
        )
    )
    if dropped:
        # Fixed text plus a count: the dropped texts are model-controlled
        # strings that failed quotation verification, so they never appear
        # anywhere, not even in this finding.
        result.findings.append(
            Finding(
                id="EXT-007",
                stage=STAGE,
                severity="info",
                confidence="confirmed",
                statement=(
                    f"{dropped} claim(s) were dropped because their text was not"
                    " found in the extracted source material. Only verbatim"
                    " quotations of the deck or site text are stored."
                ),
                evidence=_outcome_evidence(result, deck_rec, site, now),
            )
        )
    return result


def _outcome_evidence(
    result: ClaimsStageResult,
    deck_rec: FetchRecord | None,
    site: SiteFetchResult | None,
    now: ClockFn,
) -> list[Evidence]:
    """Evidence for the extraction outcome finding, never empty.

    Preferred: the deck record and the site page records. Fallbacks keep
    the Finding constructable in the corner where a site produced no page
    responses at all (for example robots.txt forbade every path): first any
    site record (the robots response itself), then a pointer at the site
    URL that was attempted.
    """
    evidence: list[Evidence] = []
    if deck_rec is not None:
        evidence.append(
            Evidence(
                source_url=deck_rec.url, retrieved_at=deck_rec.retrieved_at, excerpt="deck text"
            )
        )
    if site is not None:
        page_evidence = [
            Evidence(source_url=n.record.url, retrieved_at=n.record.retrieved_at)
            for n in site.records
            if n.name.startswith("site_0")
        ]
        if not page_evidence and not evidence:
            page_evidence = [
                Evidence(source_url=n.record.url, retrieved_at=n.record.retrieved_at)
                for n in site.records
            ]
        if not page_evidence and not evidence:
            page_evidence = [
                Evidence(
                    source_url=site.site_url,
                    retrieved_at=now(),
                    excerpt="no responses received",
                )
            ]
        evidence.extend(page_evidence)
    return evidence[:5]
