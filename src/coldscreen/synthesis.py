"""Stage 7: synthesis. The only reasoning stage.

The model receives ONE thing: a deterministic JSON document assembled from
the CaseFile (plus the versioned system prompt). It never fetches, never
remembers, never supplies a fact. Its output is parsed against a strict
schema, re-prompted on parse failures, passed through the mechanical
banned-word gate, and then ENFORCED per coldscreen.rubric: unrecognized
triggers dropped, unsupported red triggers rejected, mechanical candidates
added, and the verdict level recomputed as a pure function of the final
trigger set. The model owns narrative, questions, and judgment trigger
additions. It does not own the arithmetic.

Claims discipline (stage 6 output feeding this stage): for every CHECKABLE
claim stored on the casefile the model returns one assessment naming the
finding ids that ground its status. CODE resolves those ids against the
casefile findings and copies their Evidence into ClaimAssessment.basis; the
model never mints an evidence object. A contradicted or supported status
whose cited evidence does not resolve is downgraded to unverified with a
sanitized note, and a contradicted status additionally survives only when
its resolved basis includes a registry finding relevant to the claim's
category (the fixed table in coldscreen.rubric); a checkable claim the
model skipped gets an unverified assessment with fixed text. Only what
survives this enforcement can open the R4 and A4 gates in the rubric
ceiling. Origin-year contradictions are additionally forced: a stored
claim whose text places origin before incorporation becomes a mechanical
R4 candidate and a contradicted assessment, so the model cannot drop
that red.

Language: narrative, rationale, questions, and every record_note are
mechanically gated with no claim-quote exemptions. The quoted-data
exemption exists only for the code-rendered claims table (the whole-memo
backstop and the CI scan apply it); model prose never benefits from it, so
a narrative or record note that repeats a claim's wording fails the field
gate even though the table quotes the same words legitimately. The prompt
tells the model to reference claims by id instead. The registry identity
set (registered name, previous names, officer and PSC names) is the one
exemption model prose gets, because those strings are code-verified
registry data the memo must be able to spell out.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from importlib import resources
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .date_contradictions import assessment_note, origin_year_contradictions
from .language import find_banned_terms, registry_identity_names
from .models import CaseFile, ClaimAssessment, Evidence, Finding, SynthesisMetadata, Verdict
from .providers import Message, ModelProvider
from .rubric import (
    R4_RELEVANT_FINDINGS,
    GateSignals,
    TriggerCandidate,
    detect_candidates,
    enforce,
    rubric_text,
)
from .stages.network import OVERLAP_FINDING_ID

MAX_PARSE_RETRIES = 2
MAX_LANGUAGE_RETRIES = 1

_PROMPT_VERSION_RE = re.compile(r"coldscreen synthesis prompt, version ([0-9A-Za-z][0-9A-Za-z.]*)")

# One plain JSON schema; each provider translates it to its own dialect.
# additionalProperties false and full required lists everywhere so the same
# schema works across Anthropic, OpenAI strict mode, and Ollama.
SYNTHESIS_SCHEMA: dict[str, Any] = {
    # The title names the schema per stage where a provider needs a name
    # (the OpenAI json_schema format); it is a standard JSON Schema
    # annotation, ignored everywhere else.
    "title": "coldscreen_synthesis",
    "type": "object",
    "additionalProperties": False,
    "required": ["narrative", "verdict", "assessments"],
    "properties": {
        "narrative": {"type": "string"},
        "verdict": {
            "type": "object",
            "additionalProperties": False,
            "required": ["level", "triggered", "rationale", "questions"],
            "properties": {
                "level": {"type": "string", "enum": ["red", "amber", "green"]},
                "triggered": {"type": "array", "items": {"type": "string"}},
                "rationale": {"type": "string"},
                "questions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 3,
                    "maxItems": 5,
                },
            },
        },
        "assessments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim_id", "status", "basis_finding_ids", "record_note"],
                "properties": {
                    "claim_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["supported", "contradicted", "unverified"],
                    },
                    "basis_finding_ids": {"type": "array", "items": {"type": "string"}},
                    "record_note": {"type": "string"},
                },
            },
        },
    },
}


def build_synthesis_schema(casefile: CaseFile) -> dict[str, Any]:
    """The output schema for THIS casefile.

    With checkable claims present, claim_id is constrained to exactly those
    ids, so a provider that honors enums cannot even emit a stray id (the
    enforcement below still validates, belt and braces). With none, the
    assessments array is pinned empty.
    """
    schema = copy.deepcopy(SYNTHESIS_SCHEMA)
    checkable_ids = [c.id for c in casefile.claims if c.checkable]
    assessments = schema["properties"]["assessments"]
    if checkable_ids:
        assessments["items"]["properties"]["claim_id"]["enum"] = checkable_ids
    else:
        assessments["maxItems"] = 0
    return schema


class SynthesisError(Exception):
    """Synthesis failed cleanly. No memo ships with fabricated structure."""


class _RawVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["red", "amber", "green"]
    triggered: list[str]
    rationale: str
    # A minimum of 3 is part of the output contract; fewer is a schema
    # mismatch and triggers a parse retry (questions cannot be fabricated).
    # More than 5 is clamped during enforcement rather than retried.
    questions: list[str] = Field(min_length=3)


class _RawAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    status: Literal["supported", "contradicted", "unverified"]
    basis_finding_ids: list[str]
    record_note: str


class SynthesisOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    narrative: str
    verdict: _RawVerdict
    assessments: list[_RawAssessment] = Field(default_factory=list)


def load_prompt() -> str:
    return (resources.files("coldscreen") / "prompts" / "synthesis.md").read_text(encoding="utf-8")


def prompt_version(prompt_text: str) -> str:
    match = _PROMPT_VERSION_RE.search(prompt_text)
    if match is None:
        raise SynthesisError("the synthesis prompt file carries no version marker; refusing to run")
    return match.group(1)


def build_synthesis_input(casefile: CaseFile, candidates: list[TriggerCandidate]) -> dict[str, Any]:
    """Compact input document: everything the model may reason over, and
    nothing else. No raw filing payloads; local models must cope."""
    subject = casefile.subject
    payload: dict[str, Any] = {
        "subject": {
            "company_name": subject.company_name,
            "company_number": subject.company_number,
            "company_status": subject.company_status,
            "company_status_detail": subject.company_status_detail,
            "incorporated_on": (
                subject.date_of_creation.isoformat() if subject.date_of_creation else None
            ),
            "jurisdiction": subject.jurisdiction,
            "sic_codes": list(subject.sic_codes),
            "previous_names": [p.name for p in subject.previous_company_names if p.name],
            "accounts_overdue": subject.accounts_overdue,
            "confirmation_statement_overdue": subject.confirmation_statement_overdue,
        },
        "officers": [
            {
                "name": o.name,
                "role": o.officer_role,
                "appointed_on": o.appointed_on.isoformat() if o.appointed_on else None,
                "resigned_on": o.resigned_on.isoformat() if o.resigned_on else None,
                "current": o.resigned_on is None,
            }
            for o in casefile.officers
        ],
        "pscs": [
            {
                "name": p.name,
                "kind": p.kind,
                "natures_of_control": list(p.natures_of_control),
                "ceased_on": p.ceased_on.isoformat() if p.ceased_on else None,
            }
            for p in casefile.pscs
        ],
        "charges": [
            {
                "status": c.status,
                "created_on": c.created_on.isoformat() if c.created_on else None,
                "satisfied_on": c.satisfied_on.isoformat() if c.satisfied_on else None,
            }
            for c in casefile.charges
        ],
        "insolvency_case_count": len(casefile.insolvency_cases),
        "insolvency_case_types": sorted({c.type for c in casefile.insolvency_cases if c.type}),
        "filings_total": casefile.filings_total,
        "findings": [
            {
                "id": f.id,
                "stage": f.stage,
                "severity": f.severity,
                "confidence": f.confidence,
                "statement": f.statement,
            }
            for f in casefile.findings
        ],
        "claims": [
            {
                "id": c.id,
                "text": c.text,
                "source": c.source,
                "category": c.category,
                "checkable": c.checkable,
            }
            for c in casefile.claims
        ],
        "sanctions": (casefile.sanctions.model_dump(mode="json") if casefile.sanctions else None),
        "network": (casefile.network.model_dump(mode="json") if casefile.network else None),
        "media": (casefile.media.model_dump(mode="json") if casefile.media else None),
        "trigger_candidates": [
            {"id": c.id, "reason": c.reason, "finding_ids": list(c.finding_ids)} for c in candidates
        ],
        "rubric": rubric_text(),
        "screened_at": casefile.screened_at.isoformat(),
        "tool_version": casefile.tool_version,
    }
    return payload


def serialize_input(payload: dict[str, Any]) -> str:
    """Deterministic serialization: sorted keys, stable separators, so the
    same casefile always produces byte-identical synthesis input."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class SynthesisResult:
    verdict: Verdict
    narrative: str
    assessments: list[ClaimAssessment]
    metadata: SynthesisMetadata
    verdict_enforcement: str | None


@dataclass(frozen=True)
class AssessmentEnforcement:
    """Surviving assessments plus the notes their enforcement produced."""

    assessments: list[ClaimAssessment]
    notes: list[str]


# Fixed text for an assessment code had to add because the model returned
# none for a checkable claim. It renders in the memo table's public record
# column, so it is a constant, never assembled from model output.
MISSING_ASSESSMENT_NOTE = "No assessment was returned for this claim."


def enforce_assessments(
    raw_assessments: list[_RawAssessment], casefile: CaseFile
) -> AssessmentEnforcement:
    """Resolve, validate, and complete the model's claim assessments.

    Rules, in order:
    1. An assessment citing an id that is not a checkable claim on this
       casefile is dropped; unknown ids are model-controlled text and are
       reported as a count only. Duplicates keep the first occurrence.
    2. basis_finding_ids resolve against the casefile findings; each
       resolved finding's Evidence is COPIED into the assessment's basis.
       Unresolvable cited ids are counted per assessment.
    3. contradicted or supported with an empty resolved basis is downgraded
       to unverified with a note: a status that judges the record must
       point at the record.
       4. contradicted survives ONLY when its resolved basis includes at least
       one registry finding relevant to the claim's category, per the fixed
       relevance table in coldscreen.rubric (R4_RELEVANT_FINDINGS). A
       contradiction grounded outside that set is downgraded to unverified
       with a sanitized note. Rule 6 can still restore a contradicted
       origin-year claim afterwards.
    5. Every checkable claim the model did not assess gets an unverified
       assessment with fixed text, so the memo table is always complete.
    6. A stored checkable claim whose text places origin in a calendar year
       before incorporation is forced to contradicted with REG-002 evidence.
       A surviving model contradiction with a non-empty basis is left
       alone (its record note already passed the language gate). Everything
       else (supported, unverified, back-filled, or a contradiction that
       did not survive) is replaced with a code-written note that names
       only the parsed year, the ISO incorporation date, and REG-002.
       Puffery claims have no assessment row and are unchanged here; they
       still feed the R4 candidate detector.
    Output order is the casefile's claim order, deterministic by
    construction.
    """
    notes: list[str] = []
    checkable_ids = [c.id for c in casefile.claims if c.checkable]
    checkable_set = set(checkable_ids)
    category_by_claim = {c.id: c.category for c in casefile.claims}
    findings_by_id: dict[str, list[Finding]] = {}
    for finding in casefile.findings:
        findings_by_id.setdefault(finding.id, []).append(finding)

    by_claim: dict[str, ClaimAssessment] = {}
    dropped = 0
    duplicates = 0
    for raw in raw_assessments:
        claim_id = raw.claim_id.strip().upper()
        if claim_id not in checkable_set:
            dropped += 1
            continue
        if claim_id in by_claim:
            duplicates += 1
            continue
        basis: list[Evidence] = []
        resolved_ids: list[str] = []
        unresolved = 0
        for finding_id in raw.basis_finding_ids:
            normalized_id = finding_id.strip().upper()
            matched = findings_by_id.get(normalized_id)
            if matched is None:
                unresolved += 1
                continue
            resolved_ids.append(normalized_id)
            for finding in matched:
                basis.extend(finding.evidence)
        if unresolved:
            notes.append(
                f"assessment for {claim_id} cited {unresolved} finding id(s)"
                " not present in the casefile"
            )
        status = raw.status
        if status in ("contradicted", "supported") and not basis:
            notes.append(
                f"downgraded the assessment for {claim_id} from {status} to"
                " unverified: its cited evidence could not be resolved against"
                " the casefile"
            )
            status = "unverified"
            basis = []
        if status == "contradicted":
            category = category_by_claim.get(claim_id, "other")
            relevant = R4_RELEVANT_FINDINGS.get(category, frozenset())
            if not any(finding_id in relevant for finding_id in resolved_ids):
                # The category is a code-validated enum value, never free
                # model text, so it is safe to name in a rendered note.
                notes.append(
                    f"downgraded the assessment for {claim_id} from contradicted"
                    f" to unverified: its basis cites no registry finding relevant"
                    f" to a claim in the {category} category"
                )
                status = "unverified"
                basis = []
        by_claim[claim_id] = ClaimAssessment(
            claim_id=claim_id,
            status=status,
            basis=basis,
            record_note=" ".join(raw.record_note.split()),
        )
    if dropped:
        notes.append(
            f"dropped {dropped} assessment(s) citing ids that are not checkable"
            " claims on this casefile"
        )
    if duplicates:
        notes.append(f"dropped {duplicates} duplicate assessment(s)")

    missing = [claim_id for claim_id in checkable_ids if claim_id not in by_claim]
    for claim_id in missing:
        # Back-fill keeps the memo table complete, but it is code state, not
        # model judgment: model_assessed False keeps it out of the A4 gate.
        by_claim[claim_id] = ClaimAssessment(
            claim_id=claim_id,
            status="unverified",
            basis=[],
            record_note=MISSING_ASSESSMENT_NOTE,
            model_assessed=False,
        )
    if missing:
        notes.append("added unverified assessment(s) the model omitted for: " + ", ".join(missing))

    _apply_origin_year_overrides(by_claim, casefile, findings_by_id, checkable_set, notes)

    ordered = [by_claim[claim_id] for claim_id in checkable_ids]
    return AssessmentEnforcement(assessments=ordered, notes=notes)


def _apply_origin_year_overrides(
    by_claim: dict[str, ClaimAssessment],
    casefile: CaseFile,
    findings_by_id: dict[str, list[Finding]],
    checkable_set: set[str],
    notes: list[str],
) -> None:
    """Force contradicted on origin-year hits. Mutates by_claim and notes."""
    hits = origin_year_contradictions(casefile)
    if not hits:
        return
    basis: list[Evidence] = []
    for finding in findings_by_id.get("REG-002", ()):
        basis.extend(finding.evidence)
    if not basis:
        # A Finding without evidence cannot exist; without REG-002 there is
        # nothing to copy. The R4 candidate can still fire from the profile
        # date; the table row stays as the model (or back-fill) left it.
        return
    for hit in hits:
        if hit.claim_id not in checkable_set:
            continue
        existing = by_claim.get(hit.claim_id)
        if existing is not None and existing.status == "contradicted" and existing.basis:
            continue
        previous = existing.status if existing is not None else "unverified"
        by_claim[hit.claim_id] = ClaimAssessment(
            claim_id=hit.claim_id,
            status="contradicted",
            basis=list(basis),
            record_note=assessment_note(hit),
            model_assessed=existing.model_assessed if existing is not None else False,
        )
        notes.append(
            f"overrode the assessment for {hit.claim_id} from {previous} to"
            f" contradicted: a stored claim places origin in {hit.claimed_year},"
            f" before incorporation on {hit.incorporated_on.isoformat()}"
        )


def gate_signals(casefile: CaseFile, assessments: list[ClaimAssessment]) -> GateSignals:
    """Gate inputs for rubric enforcement, from enforced state only.

    Every field mirrors one trigger's "fires only when" condition:
    - A3 needs charges on the register AND a checkable financials claim;
      A5 needs a checkable history claim. Puffery unlocks nothing.
    - A4 needs a surviving unverified assessment the MODEL authored: rows
      the enforcement back-filled for unassessed claims (model_assessed
      False) do not count, so a model cannot open A4 by staying silent. A
      downgraded assessment (the model judged, its evidence did not
      survive) is still model-authored and does count.
    - R4 needs a surviving contradicted assessment OR the origin-year
      candidate (enforce_assessments forces the former when the latter
      holds and REG-002 evidence exists). enforce_assessments guarantees
      any survivor is grounded in a relevant registry finding or in the
      mechanical origin-year path, so this function must only ever see
      assessments that went through it.
    - R5 needs recorded co-appointment overlap AND claims material to judge
      disclosure against; the overlap finding ids are collected here by
      filtering on the fixed id so the acceptance note stays code-derived.
    - A6 needs the media stage to have run and returned at least one item.
    """
    checkable = [c for c in casefile.claims if c.checkable]
    overlaps = bool(casefile.network is not None and casefile.network.overlaps)
    overlap_ids: tuple[str, ...] = ()
    if overlaps:
        overlap_ids = tuple(
            dict.fromkeys(f.id for f in casefile.findings if f.id == OVERLAP_FINDING_ID)
        )
    media = casefile.media
    return GateSignals(
        checkable_history_present=any(c.category == "history" for c in checkable),
        checkable_financials_present=any(c.category == "financials" for c in checkable),
        charges_present=bool(casefile.charges),
        contradicted_with_relevant_basis=any(
            a.status == "contradicted" and a.basis for a in assessments
        ),
        unverified_present=any(a.status == "unverified" and a.model_assessed for a in assessments),
        overlaps_present=overlaps,
        claims_material_present=bool(casefile.claims),
        media_items_present=bool(media is not None and media.performed and media.items),
        overlap_finding_ids=overlap_ids,
    )


# The stage-honesty gate: fixed clean-claim phrases that must not describe
# a stage the casefile records as not run or failed. Deliberately narrow:
# every phrase carries the stage's own vocabulary, so the gate cannot hit
# prose about another stage, and bare words like "clean" or "no matches"
# are excluded because honest memos legitimately use them ("... rather than
# clean results" is the canonical honest sentence about a skipped stage).
# The gate only arms for a stage that is recorded not run or failed, so a
# stage that RAN and found nothing may honestly say so.
SANCTIONS_CLEAN_PHRASES: tuple[str, ...] = (
    "no sanctions match",
    "no sanctions matches",
    "no sanctions hits",
    "no sanctions concerns",
    "no sanctions findings",
    "no sanctions results",
    "no sanctions or pep match",
    "sanctions screening was clean",
    "sanctions screening came back clean",
    "sanctions screening returned no",
    "clear of sanctions",
    "cleared sanctions screening",
)
MEDIA_CLEAN_PHRASES: tuple[str, ...] = (
    "no adverse media",
    "no adverse coverage",
    "no adverse press",
    "no negative media",
    "no negative coverage",
    "no media matches",
    "no media results",
    "media search was clean",
    "media search came back clean",
    "media search returned no",
    "clear of adverse media",
)


def stage_honesty_violations(casefile: CaseFile, rendered_fields: list[str]) -> list[str]:
    """Stage names the model described as clean although they did not run.

    The prompt already forbids this; here it is mechanical. Only the
    sanctions and media stages are covered (the stages the failure posture
    touches), only when the casefile records them as not run or failed, and
    only via the fixed phrase sets above. Returns fixed stage-name strings,
    never model text, so a failure message built from them is sanitized by
    construction.
    """
    prose = " ".join(" ".join(rendered_fields).split()).casefold()
    violations: list[str] = []
    sanctions = casefile.sanctions
    if (sanctions is None or not sanctions.performed) and any(
        phrase in prose for phrase in SANCTIONS_CLEAN_PHRASES
    ):
        violations.append("sanctions")
    media = casefile.media
    if (media is None or not media.performed) and any(
        phrase in prose for phrase in MEDIA_CLEAN_PHRASES
    ):
        violations.append("adverse media")
    return violations


def _try_parse(raw: str) -> tuple[SynthesisOutput | None, str]:
    text = raw.strip()
    # Weak local models occasionally wrap JSON in a markdown fence despite
    # instructions; stripping it is mechanical cleanup, not repair.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except ValueError as error:
        return None, f"invalid JSON: {error}"
    try:
        return SynthesisOutput.model_validate(data), ""
    except ValidationError as error:
        issues = "; ".join(
            f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in error.errors()
        )
        return None, f"schema mismatch: {issues}"


def synthesize(
    casefile: CaseFile,
    provider: ModelProvider,
    provider_name: str,
    model: str,
) -> SynthesisResult:
    """Run synthesis with parse retries, the language gate, and enforcement."""
    prompt = load_prompt()
    version = prompt_version(prompt)
    candidates = detect_candidates(casefile)
    input_doc = serialize_input(build_synthesis_input(casefile, candidates))
    schema = build_synthesis_schema(casefile)

    messages: list[Message] = [Message(role="user", content=input_doc)]
    parse_retries = 0
    language_retries = 0

    while True:
        raw = provider.complete(prompt, messages, json_schema=schema)
        parsed, parse_error = _try_parse(raw)
        if parsed is None:
            if parse_retries >= MAX_PARSE_RETRIES:
                # Count every completion made, including language retries,
                # so the message matches what the provider was actually asked.
                attempts = parse_retries + language_retries + 1
                raise SynthesisError(
                    "synthesis failed: the model did not produce valid output after"
                    f" {attempts} attempts (last error: {parse_error})."
                    " No memo is shipped with fabricated structure."
                )
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
            continue

        # Model prose is gated with NO claim-quote exemptions: quoting a
        # claim's wording in prose fails even when the claims table
        # legitimately renders the same words. Exemptions there would be a
        # laundering channel (reviewer attack shapes 2 through 4): a
        # single-word claim would whitelist that token everywhere, and any
        # prose embedding a claim substring would carry its vocabulary
        # through the gate. The one exemption is the registry identity set
        # (the subject's registered name, previous names, officer and PSC
        # names): those are code-verified registry data the prose must be
        # able to spell out, and the model cannot mint them.
        identity_names = registry_identity_names(casefile)
        rendered_fields = [
            parsed.narrative,
            parsed.verdict.rationale,
            *parsed.verdict.questions,
            *(a.record_note for a in parsed.assessments),
        ]
        banned = sorted(
            set().union(*(find_banned_terms(f, identity_names) for f in rendered_fields))
        )
        # The stage-honesty gate runs in the same corrective loop: a stage
        # recorded as not run or failed must never be described as clean.
        violations = stage_honesty_violations(casefile, rendered_fields)
        if banned or violations:
            if language_retries >= MAX_LANGUAGE_RETRIES:
                if banned:
                    # Count only, never the terms: the CLI renders this
                    # message into the synthesis-failure memo, so quoting the
                    # vocabulary here would trip the whole-memo backstop and
                    # cost the run its audit pack. The corrective retry below
                    # still names the terms, because the model needs them to
                    # fix its output.
                    raise SynthesisError(
                        "synthesis failed: the model output still used"
                        f" {len(banned)} banned term(s) after a corrective retry."
                        " Memos state what the record shows; they never use"
                        " accusatory language."
                    )
                # Stage names here are fixed strings from the gate, never
                # model text, so this message is sanitized by construction.
                raise SynthesisError(
                    "synthesis failed: the model described the"
                    f" {' and '.join(violations)} stage(s) as clean after a"
                    " corrective retry, but the casefile records them as not"
                    " run or failed. A screen that did not happen is a gap,"
                    " never a clean result."
                )
            language_retries += 1
            corrections: list[str] = []
            if banned:
                corrections.append(
                    "Your response used banned vocabulary that must not appear"
                    f" in a memo: {', '.join(banned)}. Rewrite the narrative,"
                    " rationale, questions, and record notes without these"
                    " words or their variants. Reference claims by their ids"
                    " and never repeat a claim's wording; quoting is not"
                    " exempt in your prose. Describe what the public record"
                    " shows instead of characterizing conduct."
                )
            if violations:
                corrections.append(
                    "Your response described the"
                    f" {' and '.join(violations)} stage(s) as clean or as having"
                    " found no matches, but the casefile records them as not run"
                    " or failed. State the absence or failure explicitly; a"
                    " screen that did not happen is a gap, never a result."
                )
            corrections.append("Keep the same JSON structure and trigger reasoning.")
            messages = messages + [
                Message(role="assistant", content=raw),
                Message(role="user", content=" ".join(corrections)),
            ]
            continue
        break

    assessment_result = enforce_assessments(parsed.assessments, casefile)
    signals = gate_signals(casefile, assessment_result.assessments)
    result = enforce(
        model_level=parsed.verdict.level,
        model_triggered=parsed.verdict.triggered,
        model_questions=parsed.verdict.questions,
        candidates=candidates,
        gate_signals=signals,
    )
    verdict = Verdict(
        level=result.level,
        triggered=result.triggered,
        rationale=parsed.verdict.rationale,
        questions=result.questions,
    )
    metadata = SynthesisMetadata(
        provider=provider_name,
        model=model,
        prompt_version=version,
        parse_retries=parse_retries,
        language_retries=language_retries,
        enforcement_notes=assessment_result.notes + result.notes,
        model_level=result.model_level,
    )
    return SynthesisResult(
        verdict=verdict,
        narrative=parsed.narrative,
        assessments=assessment_result.assessments,
        metadata=metadata,
        verdict_enforcement=result.level_note,
    )


def apply_synthesis(casefile: CaseFile, result: SynthesisResult) -> CaseFile:
    """A new CaseFile carrying the enforced verdict and synthesis metadata."""
    return casefile.model_copy(
        update={
            "verdict": result.verdict,
            "narrative": result.narrative,
            "assessments": result.assessments,
            "synthesis": result.metadata,
            "verdict_enforcement": result.verdict_enforcement,
        }
    )
