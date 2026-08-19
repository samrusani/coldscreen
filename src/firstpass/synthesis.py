"""Stage 7: synthesis. The only reasoning stage.

The model receives ONE thing: a deterministic JSON document assembled from
the CaseFile (plus the versioned system prompt). It never fetches, never
remembers, never supplies a fact. Its output is parsed against a strict
schema, re-prompted on parse failures, passed through the mechanical
banned-word gate, and then ENFORCED per firstpass.rubric: unrecognized
triggers dropped, unsupported red triggers rejected, mechanical candidates
added, and the verdict level recomputed as a pure function of the final
trigger set. The model owns narrative, questions, and amber judgment
trigger additions. It does not own the arithmetic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib import resources
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .language import find_banned_terms
from .models import CaseFile, SynthesisMetadata, Verdict
from .providers import Message, ModelProvider
from .rubric import TriggerCandidate, detect_candidates, enforce, rubric_text

MAX_PARSE_RETRIES = 2
MAX_LANGUAGE_RETRIES = 1

_PROMPT_VERSION_RE = re.compile(r"firstpass synthesis prompt, version ([0-9A-Za-z][0-9A-Za-z.]*)")

# One plain JSON schema; each provider translates it to its own dialect.
# additionalProperties false and full required lists everywhere so the same
# schema works across Anthropic, OpenAI strict mode, and Ollama.
SYNTHESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["narrative", "verdict"],
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
    },
}


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


class SynthesisOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    narrative: str
    verdict: _RawVerdict


def load_prompt() -> str:
    return (resources.files("firstpass") / "prompts" / "synthesis.md").read_text(encoding="utf-8")


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
    metadata: SynthesisMetadata
    verdict_enforcement: str | None


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

    messages: list[Message] = [Message(role="user", content=input_doc)]
    parse_retries = 0
    language_retries = 0

    while True:
        raw = provider.complete(prompt, messages, json_schema=SYNTHESIS_SCHEMA)
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

        rendered_fields = [parsed.narrative, parsed.verdict.rationale, *parsed.verdict.questions]
        banned = sorted(set().union(*(find_banned_terms(f) for f in rendered_fields)))
        if banned:
            if language_retries >= MAX_LANGUAGE_RETRIES:
                raise SynthesisError(
                    "synthesis failed: the model output still used banned"
                    f" vocabulary ({', '.join(banned)}) after a corrective retry."
                    " Memos state what the record shows; they never use accusatory"
                    " language."
                )
            language_retries += 1
            messages = messages + [
                Message(role="assistant", content=raw),
                Message(
                    role="user",
                    content=(
                        "Your response used banned vocabulary that must not appear"
                        f" in a memo: {', '.join(banned)}. Rewrite the narrative,"
                        " rationale, and questions without these words or their"
                        " variants. Describe what the public record shows instead"
                        " of characterizing conduct. Keep the same JSON structure"
                        " and trigger reasoning."
                    ),
                ),
            ]
            continue
        break

    result = enforce(
        model_level=parsed.verdict.level,
        model_triggered=parsed.verdict.triggered,
        model_questions=parsed.verdict.questions,
        candidates=candidates,
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
        enforcement_notes=result.notes,
        model_level=result.model_level,
    )
    return SynthesisResult(
        verdict=verdict,
        narrative=parsed.narrative,
        metadata=metadata,
        verdict_enforcement=result.level_note,
    )


def apply_synthesis(casefile: CaseFile, result: SynthesisResult) -> CaseFile:
    """A new CaseFile carrying the enforced verdict and synthesis metadata."""
    return casefile.model_copy(
        update={
            "verdict": result.verdict,
            "narrative": result.narrative,
            "synthesis": result.metadata,
            "verdict_enforcement": result.verdict_enforcement,
        }
    )
