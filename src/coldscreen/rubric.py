"""Rubric catalog, mechanical trigger detection, and verdict enforcement.

The catalog mirrors rubric.md exactly (a test parses rubric.md and asserts
the mirror). Enforcement is both a floor and a ceiling over the model's
cited triggers:

- Floor: every mechanically detected candidate is forced into the final
  trigger set whether or not the model cited it.
- Ceiling: a model-cited R1, R2, or R3 counts only when the same id is in
  the mechanically detected candidate set; otherwise it is rejected. The
  claims-and-network judgment triggers are gated on what actually survives
  in the casefile, via ClaimSignals:
    - R4 is accepted only when at least one claim assessment SURVIVED
      enforcement as contradicted with resolved evidence (the assessment
      enforcement in coldscreen.synthesis downgrades any contradicted or
      supported assessment whose cited evidence does not resolve).
    - R5 is accepted only when co-appointment overlap is recorded in the
      casefile's network expansion.
    - A4 is accepted only when at least one checkable claim's assessment
      survived as unverified AND was authored by the model. The unverified
      rows code back-fills for unassessed claims keep the memo table
      complete but never open the gate: silence is not support.
    - A3 and A5 are accepted only when at least one CHECKABLE claim exists.
      A slogan-only deck (puffery, checkable false throughout) unlocks
      nothing.
  A6 remains freely citable model judgment over the media items.

Verdict level is then a PURE FUNCTION of the enforced trigger set: any RED
trigger forces red, otherwise any AMBER trigger forces amber, otherwise
green. The model owns narrative, questions, and judgment trigger additions;
it never does the level arithmetic, and it cannot introduce a red trigger
the casefile does not support. The floor, the ceiling, and the pure level
function keep the anchoring property for the mechanical tier (same
casefile, same level, any model). R4 is judgment-tier by design: whether a
surviving contradiction is cited is the model's call, but no model can
manufacture an R4 the enforced assessments do not carry.

Enforcement notes are rendered into memos, so they are built from fixed
text and catalog-validated ids only, never from model-controlled strings.
Unrecognized cited ids are reported as a count, not echoed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .models import CaseFile

RUBRIC_VERSION = "0.1"

# Company status values in the liquidation/administration family, used by
# the mechanical R2 candidate check. Mirrors findings.RED_STATUSES.
INSOLVENCY_STATUS_FAMILY = frozenset(
    {"liquidation", "receivership", "administration", "insolvency-proceedings"}
)


@dataclass(frozen=True)
class Trigger:
    """One rubric trigger: id, definition, severity."""

    id: str
    text: str
    severity: Literal["red", "amber"]


# Mirrors the table in rubric.md, version 0.1. Do not edit one without the
# other; tests/test_rubric.py parses rubric.md and asserts equality.
TRIGGERS: tuple[Trigger, ...] = (
    Trigger("R1", "Sanctions or PEP match (entity or PSC) at or above threshold", "red"),
    Trigger("R2", "Active insolvency event", "red"),
    Trigger("R3", "Disqualified director in current officer set", "red"),
    Trigger("R4", "Material claim directly contradicted by registry record", "red"),
    Trigger("R5", "Undisclosed related-party network across officers", "red"),
    Trigger("A1", "Overdue or irregular filings", "amber"),
    Trigger("A2", "Wholesale officer changes within 12 months", "amber"),
    Trigger("A3", "Charge stack inconsistent with stated capital story", "amber"),
    Trigger("A4", "Central claim unverifiable from any public source", "amber"),
    Trigger("A5", "Corporate age or scale inconsistent with stated history", "amber"),
    Trigger("A6", "Substantive adverse media (confirmed source)", "amber"),
)

TRIGGER_INDEX: dict[str, Trigger] = {t.id: t for t in TRIGGERS}

# Case-insensitive intake: model-cited ids are normalized to catalog casing
# before any filtering, so "a6" counts as A6.
_TRIGGER_ID_BY_LOWER: dict[str, str] = {t.id.lower(): t.id for t in TRIGGERS}

# The ceiling. Red triggers with a mechanical detector: model citations are
# accepted only when the detector fired on this casefile. The judgment
# triggers R4, R5, A3, A4, and A5 are gated on ClaimSignals below.
MECHANICAL_RED_IDS = frozenset({"R1", "R2", "R3"})

RUBRIC_RULES: tuple[str, ...] = (
    "Any RED trigger forces a RED verdict.",
    "Two or more AMBER triggers cap the verdict at AMBER, regardless of narrative tone.",
    "The verdict block must cite trigger IDs. A verdict that cites no triggers must be GREEN.",
    (
        "A verdict is an opinion generated from public sources at a point in time."
        " It is a reason to ask better questions, not a substitute for judgment."
    ),
)


def rubric_text() -> str:
    """The rubric as plain text for the synthesis input document."""
    lines = [f"Scoring rubric version {RUBRIC_VERSION}. Triggers:"]
    lines.extend(f"- {t.id} ({t.severity.upper()}): {t.text}" for t in TRIGGERS)
    lines.append("Rules:")
    lines.extend(f"- {rule}" for rule in RUBRIC_RULES)
    return "\n".join(lines)


def compute_level(triggered: list[str] | set[str]) -> Literal["red", "amber", "green"]:
    """Level as a pure function of the trigger set. No exceptions."""
    known = [t for t in triggered if t in TRIGGER_INDEX]
    if any(TRIGGER_INDEX[t].severity == "red" for t in known):
        return "red"
    if any(TRIGGER_INDEX[t].severity == "amber" for t in known):
        return "amber"
    return "green"


@dataclass(frozen=True)
class TriggerCandidate:
    """A mechanically detected trigger, passed to the model as a fact it
    must address and enforced into the final trigger set afterwards."""

    id: str
    reason: str
    finding_ids: list[str] = field(default_factory=list)


def detect_candidates(casefile: CaseFile) -> list[TriggerCandidate]:
    """Mechanical R and A candidates from deterministic findings.

    Detection reads the casefile only, so it works identically at screen
    time and at rerun time. Covered here: R1, R2, R3, A1, A2. Judgment
    triggers (A3, A6, and the claims-dependent R4/R5/A4/A5) are the model's
    to add when the casefile supports them.
    """
    candidates: list[TriggerCandidate] = []
    findings = casefile.findings

    # R1: any red sanctions finding (match at or above threshold).
    san_red = [f.id for f in findings if f.stage == "sanctions" and f.severity == "red"]
    if san_red:
        candidates.append(
            TriggerCandidate(
                id="R1",
                reason="sanctions screening reported a match at or above the threshold",
                finding_ids=san_red,
            )
        )

    # R2: insolvency cases present AND company status in the family.
    status = (casefile.subject.company_status or "").strip()
    if casefile.insolvency_cases and status in INSOLVENCY_STATUS_FAMILY:
        insolvency_ids = [f.id for f in findings if f.id == "REG-007"]
        candidates.append(
            TriggerCandidate(
                id="R2",
                reason=(f"insolvency cases are on the register and company status is {status}"),
                finding_ids=insolvency_ids,
            )
        )

    # R3: any red network finding (strong, currently active disqualification).
    net_red = [f.id for f in findings if f.stage == "network" and f.severity == "red"]
    if net_red:
        candidates.append(
            TriggerCandidate(
                id="R3",
                reason="a current officer or PSC matched an active disqualification record",
                finding_ids=net_red,
            )
        )

    # A1: registry marks accounts or the confirmation statement overdue.
    overdue_ids = [f.id for f in findings if f.id in {"REG-003", "REG-004"}]
    if overdue_ids:
        candidates.append(
            TriggerCandidate(
                id="A1",
                reason="the registry marks accounts or the confirmation statement overdue",
                finding_ids=overdue_ids,
            )
        )

    # A2: the wholesale officer change finding fired at amber severity.
    churn_ids = [f.id for f in findings if f.id == "REG-009" and f.severity == "amber"]
    if churn_ids:
        candidates.append(
            TriggerCandidate(
                id="A2",
                reason="officer resignations in the last 12 months met the wholesale threshold",
                finding_ids=churn_ids,
            )
        )

    return candidates


@dataclass(frozen=True)
class ClaimSignals:
    """What the enforced casefile actually supports, for the trigger gates.

    Built by the caller AFTER assessment enforcement, from the surviving
    assessments and the casefile's network expansion, never from anything
    the model asserted. checkable_claims_present requires at least one
    CHECKABLE claim; unverified_present requires a MODEL-authored surviving
    unverified assessment (code back-fill does not count). The default (all
    False) locks every gated trigger, which is exactly the claims-free
    behavior.
    """

    checkable_claims_present: bool = False
    contradicted_with_basis: bool = False
    unverified_present: bool = False
    overlaps_present: bool = False


@dataclass(frozen=True)
class EnforcementResult:
    """The enforced verdict pieces plus every note the enforcement produced."""

    level: Literal["red", "amber", "green"]
    triggered: list[str]
    questions: list[str]
    notes: list[str]
    model_level: Literal["red", "amber", "green"]
    # One-line memo note when the computed level overrides the model's.
    level_note: str | None = None


def enforce(
    model_level: Literal["red", "amber", "green"],
    model_triggered: list[str],
    model_questions: list[str],
    candidates: list[TriggerCandidate],
    claim_signals: ClaimSignals | None = None,
) -> EnforcementResult:
    """Apply the enforcement rules, in order.

    1. Cited ids are normalized case-insensitively to catalog casing;
       anything that still is not a catalog id is dropped and noted as a
       count only (unrecognized ids are model-controlled text and never
       appear in a note).
    2. The ceiling: cited R1/R2/R3 are rejected unless mechanically
       detected on this casefile. Cited R4/R5/A3/A4/A5 are rejected unless
       the claim_signals gate for that trigger is open (module docstring);
       with no claim_signals every gate is locked, the claims-free
       behavior. Every rejection is noted with fixed text and the
       catalog-validated id.
    3. The floor: every mechanical candidate must be present; missing ones
       are added and noted.
    4. The level is computed from the final trigger set; the model's level
       is recorded and overridden with a note when it differs.
    5. Questions are clamped to at most 5 (a minimum of 3 is validated
       upstream as part of the output schema).
    """
    # Detection and the catalog live in this module; a candidate id outside
    # the catalog means they have drifted apart. Fail loudly, not quietly.
    for candidate in candidates:
        assert candidate.id in TRIGGER_INDEX, (
            f"mechanically detected candidate {candidate.id!r} is not in the rubric catalog"
        )
    signals = claim_signals or ClaimSignals()

    notes: list[str] = []

    cited: list[str] = []
    unrecognized = 0
    for raw_id in model_triggered:
        trigger_id = _TRIGGER_ID_BY_LOWER.get(raw_id.strip().lower())
        if trigger_id is None:
            unrecognized += 1
        elif trigger_id not in cited:
            cited.append(trigger_id)
    if unrecognized:
        notes.append(f"dropped {unrecognized} unrecognized trigger id(s)")

    # Fixed rejection texts for the gated judgment triggers. Catalog ids
    # only; nothing model-controlled can reach a note.
    gate_rejections = {
        "R4": (
            signals.contradicted_with_basis,
            "rejected trigger R4: no claim assessment survives as contradicted"
            " with resolved evidence",
        ),
        "R5": (
            signals.overlaps_present,
            "rejected trigger R5: no co-appointment overlap is recorded in this casefile",
        ),
        "A3": (
            signals.checkable_claims_present,
            "rejected trigger A3: no checkable claims are recorded in this casefile",
        ),
        "A4": (
            signals.unverified_present,
            "rejected trigger A4: no model-assessed checkable claim survives as unverified",
        ),
        "A5": (
            signals.checkable_claims_present,
            "rejected trigger A5: no checkable claims are recorded in this casefile",
        ),
    }

    candidate_ids = {c.id for c in candidates}
    triggered: list[str] = []
    for trigger_id in cited:
        if trigger_id in gate_rejections:
            gate_open, rejection_note = gate_rejections[trigger_id]
            if gate_open:
                triggered.append(trigger_id)
            else:
                notes.append(rejection_note)
        elif trigger_id in MECHANICAL_RED_IDS and trigger_id not in candidate_ids:
            notes.append(f"rejected trigger {trigger_id}: not supported by the casefile evidence")
        else:
            triggered.append(trigger_id)

    missing = [c.id for c in candidates if c.id not in triggered]
    for trigger_id in missing:
        triggered.append(trigger_id)
    if missing:
        notes.append(
            "added mechanically detected trigger(s) the model omitted: " + ", ".join(missing)
        )

    level = compute_level(triggered)
    level_note: str | None = None
    if level != model_level:
        level_note = (
            f"Verdict level enforced: the model proposed {model_level}, but the"
            f" enforced trigger set [{', '.join(triggered) or 'none'}] computes"
            f" to {level}. Level is a pure function of the triggers."
        )
        notes.append(level_note)

    questions = list(model_questions)
    if len(questions) > 5:
        notes.append(f"clamped questions from {len(questions)} to 5")
        questions = questions[:5]

    return EnforcementResult(
        level=level,
        triggered=triggered,
        questions=questions,
        notes=notes,
        model_level=model_level,
        level_note=level_note,
    )
