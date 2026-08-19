"""Rubric catalog, mechanical trigger detection, and verdict enforcement.

The catalog mirrors rubric.md version 0.3 exactly, including each trigger's
"fires only when" evidence condition (a test parses rubric.md and asserts
the mirror). Enforcement is both a floor and a ceiling over the model's
cited triggers, and as of rubric 0.2 EVERY trigger id is gated:

- Floor: every mechanically detected candidate is forced into the final
  trigger set whether or not the model cited it.
- Ceiling: a cited trigger counts only when its evidence condition holds on
  this casefile. The mechanical ids (R1, R2, R3, A1, A2, A7) are accepted
  only when the same id is in the mechanically detected candidate set. The
  judgment ids are gated on GateSignals, built by the caller AFTER
  assessment enforcement from surviving enforced state only:
    - R4 is a hybrid. The floor fires when a stored claim places origin in
      a calendar year before the registry incorporation date (see
      coldscreen.date_contradictions); that candidate cannot be dropped.
      The judgment path still opens when at least one claim assessment
      SURVIVED enforcement as contradicted with resolved evidence relevant
      to the claim's category (the relevance table below;
      coldscreen.synthesis downgrades everything else). A citation of R4
      is accepted if the gate is open OR the date-contradiction candidate
      is present.
    - R5 opens only when co-appointment overlap is recorded in the
      casefile's network expansion AND claims material is present to judge
      disclosure against. Acceptance is noted with the overlap finding ids.
    - A3 opens only when charges exist on the register AND at least one
      checkable financials claim was extracted.
    - A4 opens only when at least one checkable claim's assessment survived
      as unverified AND was authored by the model. Code back-filled rows
      keep the memo table complete but never open the gate: silence is not
      support.
    - A5 opens only when at least one checkable history claim was
      extracted.
    - A6 opens only when the media stage ran and returned at least one
      item.

Verdict level is then a PURE FUNCTION of the enforced trigger set: any RED
trigger forces red, otherwise any AMBER trigger forces amber, otherwise
green. The model owns narrative, questions, and judgment trigger additions
inside open gates; it never does the level arithmetic, and it cannot move
the level outside evidence-supported bounds in either direction. The floor,
the ceiling, and the pure level function keep the anchoring property: same
casefile, same evidence-supported bounds, any model.

Enforcement notes are rendered into memos, so they are built from fixed
text, catalog-validated ids, and code-derived finding ids only, never from
model-controlled strings. Unrecognized cited ids are reported as a count,
not echoed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .date_contradictions import candidate_reason, origin_year_contradictions
from .models import CaseFile

RUBRIC_VERSION = "0.3"

# Company status values that ARE an insolvency state (the R2 status leg).
# rubric.md spells them with spaces; the registry uses hyphens.
INSOLVENCY_STATUS_FAMILY = frozenset(
    {
        "administration",
        "liquidation",
        "receivership",
        "insolvency-proceedings",
        "voluntary-arrangement",
    }
)

# Company status values that are not active and not an insolvency state:
# the A7 statuses. Insolvency states escalate to R2 instead.
NOT_ACTIVE_STATUSES = frozenset({"dissolved", "closed", "converted-closed", "removed"})


@dataclass(frozen=True)
class Trigger:
    """One rubric trigger: id, definition, severity, evidence condition.

    condition is the "Fires only when" column of rubric.md verbatim: the
    mechanical gate this module enforces for that id.
    """

    id: str
    text: str
    severity: Literal["red", "amber"]
    condition: str


# Mirrors the table in rubric.md, version 0.3. Do not edit one without the
# other; tests/test_rubric.py parses rubric.md and asserts equality.
TRIGGERS: tuple[Trigger, ...] = (
    Trigger(
        "R1",
        "Sanctions or PEP match (entity or PSC) at or above threshold",
        "red",
        "A sanctions match finding at or above threshold exists for the entity or a"
        " PSC. Officer matches are reported as amber findings, not R1.",
    ),
    Trigger(
        "R2",
        "Active insolvency event",
        "red",
        "Insolvency cases are on the register, or the company status is itself an"
        " insolvency state (administration, liquidation, receivership, insolvency"
        " proceedings, voluntary arrangement).",
    ),
    Trigger(
        "R3",
        "Disqualified director in current officer set",
        "red",
        "A strong-match disqualification (name and date of birth, or name and company"
        " number for corporate officers) that is currently active.",
    ),
    Trigger(
        "R4",
        "Material claim directly contradicted by registry record",
        "red",
        "A surviving contradicted claim assessment whose basis includes a registry"
        " finding relevant to the claim's category, or a stored claim whose text"
        " places the company's origin in a calendar year before the registry"
        " incorporation date.",
    ),
    Trigger(
        "R5",
        "Undisclosed related-party network across officers",
        "red",
        "Co-appointment overlap findings exist and claims material is present to"
        " judge disclosure against.",
    ),
    Trigger(
        "A1",
        "Overdue or irregular filings",
        "amber",
        "The registry marks accounts or the confirmation statement overdue.",
    ),
    Trigger(
        "A2",
        "Wholesale officer changes within 12 months",
        "amber",
        "Resignations in the last 12 months meet the wholesale threshold.",
    ),
    Trigger(
        "A3",
        "Charge stack inconsistent with stated capital story",
        "amber",
        "Charges exist on the register and at least one checkable financials claim was extracted.",
    ),
    Trigger(
        "A4",
        "Central claim unverifiable from any public source",
        "amber",
        "At least one checkable claim was assessed unverified by the model itself.",
    ),
    Trigger(
        "A5",
        "Corporate age or scale inconsistent with stated history",
        "amber",
        "At least one checkable history claim was extracted.",
    ),
    Trigger(
        "A6",
        "Substantive adverse media (confirmed source)",
        "amber",
        "The media stage ran and returned at least one item.",
    ),
    Trigger(
        "A7",
        "Company status is not active",
        "amber",
        "The registered status is dissolved, closed, converted-closed, or removed."
        " Insolvency states escalate to R2 instead.",
    ),
)

TRIGGER_INDEX: dict[str, Trigger] = {t.id: t for t in TRIGGERS}

# Case-insensitive intake: model-cited ids are normalized to catalog casing
# before any filtering, so "a6" counts as A6.
_TRIGGER_ID_BY_LOWER: dict[str, str] = {t.id.lower(): t.id for t in TRIGGERS}

# Trigger ids with a mechanical detector in detect_candidates. The ceiling
# accepts a model citation of these only when the detector fired on this
# casefile. The judgment ids (R4, R5, A3, A4, A5, A6) are gated on
# GateSignals instead. R4 is the hybrid: it stays in the judgment set so a
# non-date contradiction still has to clear the gate, AND detect_candidates
# may emit it as a floor candidate when an origin-year contradiction is
# present. Together the two sets cover the whole catalog, so no trigger is
# freely citable.
MECHANICAL_IDS = frozenset({"R1", "R2", "R3", "A1", "A2", "A7"})

# The R4 relevance table: claim category -> registry finding ids that can
# ground a contradiction of a claim in that category. Fixed and deliberately
# narrow until better sources exist. Ids are the deterministic registry
# finding ids from coldscreen.findings:
#   REG-002 incorporation date, REG-010 previous company names,
#   REG-006 charges register, REG-003 accounts overdue,
#   REG-004 confirmation statement overdue, REG-008 active officer count.
# Claim categories missing from this table (for example "other") map to the
# empty set: no registry finding can ground a contradiction of them, so a
# contradicted assessment there always downgrades.
R4_RELEVANT_FINDINGS: dict[str, frozenset[str]] = {
    "history": frozenset({"REG-002", "REG-010"}),
    "financials": frozenset({"REG-006", "REG-003"}),
    "regulatory": frozenset({"REG-003", "REG-004"}),
    "team": frozenset({"REG-008"}),
    "traction": frozenset({"REG-008"}),
}

RUBRIC_RULES: tuple[str, ...] = (
    "Any RED trigger forces a RED verdict.",
    "Any AMBER trigger yields at least AMBER. AMBER triggers never escalate to RED,"
    " however many there are.",
    "The verdict block must cite trigger IDs. A verdict that cites no triggers must be GREEN.",
    "Mechanically detected triggers cannot be dropped by the model, and triggers whose"
    " evidence conditions are not met cannot be added by it. The model's judgment"
    " operates inside those conditions, never on the level arithmetic.",
    "A verdict is an opinion generated from public sources at a point in time."
    " It is a reason to ask better questions, not a substitute for judgment.",
)


def rubric_text() -> str:
    """The rubric as plain text for the synthesis input document."""
    lines = [f"Scoring rubric version {RUBRIC_VERSION}. Triggers:"]
    lines.extend(
        f"- {t.id} ({t.severity.upper()}): {t.text}. Fires only when: {t.condition}"
        for t in TRIGGERS
    )
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
    """Mechanical candidates from deterministic findings, per rubric 0.3.

    Detection reads the casefile only, so it works identically at screen
    time and at rerun time. Covered here: R1, R2, R3, A1, A2, A7, and the
    origin-year path of R4. The remaining judgment triggers (R5, A3, A4,
    A5, A6) and the non-date path of R4 are the model's to add when their
    gates are open.
    """
    candidates: list[TriggerCandidate] = []
    findings = casefile.findings

    # R1: any red sanctions finding. Entity and PSC matches are red; officer
    # matches are recorded as amber sanctions findings and cannot arrive here.
    san_red = [f.id for f in findings if f.stage == "sanctions" and f.severity == "red"]
    if san_red:
        candidates.append(
            TriggerCandidate(
                id="R1",
                reason="sanctions screening reported a match at or above the threshold",
                finding_ids=san_red,
            )
        )

    # R2: insolvency cases on the register, OR the status is itself an
    # insolvency state (with the profile status finding as evidence, whether
    # or not case detail was retrieved). Status matching is casefolded so a
    # mixed-case registry value cannot silently miss the family sets.
    status = (casefile.subject.company_status or "").strip().casefold()
    r2_reasons: list[str] = []
    r2_ids: list[str] = []
    if casefile.insolvency_cases:
        r2_reasons.append("insolvency cases are on the register")
        r2_ids.extend(f.id for f in findings if f.id == "REG-007")
    if status in INSOLVENCY_STATUS_FAMILY:
        r2_reasons.append(f"the company status is {status}")
        r2_ids.extend(f.id for f in findings if f.id == "REG-001")
    if r2_reasons:
        candidates.append(
            TriggerCandidate(id="R2", reason=" and ".join(r2_reasons), finding_ids=r2_ids)
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

    # A7: the registered status is not active and not an insolvency state.
    if status in NOT_ACTIVE_STATUSES:
        candidates.append(
            TriggerCandidate(
                id="A7",
                reason=f"the registered company status is {status}",
                finding_ids=[f.id for f in findings if f.id == "REG-001"],
            )
        )

    # R4 floor: a stored claim places origin in a calendar year before
    # incorporation. The judgment path of R4 still goes through GateSignals;
    # this candidate is the unconditional red for the date-shaped class.
    origin_hits = origin_year_contradictions(casefile)
    if origin_hits:
        candidates.append(
            TriggerCandidate(
                id="R4",
                reason=candidate_reason(origin_hits),
                finding_ids=[f.id for f in findings if f.id == "REG-002"],
            )
        )

    return candidates


@dataclass(frozen=True)
class GateSignals:
    """What the enforced casefile actually supports, for the trigger gates.

    Built by the caller AFTER assessment enforcement, from the surviving
    assessments and the casefile's stored stage outputs, never from anything
    the model asserted. The default (all False) locks every gated trigger,
    which is exactly the claims-free, media-free behavior.

    overlap_finding_ids carries the co-appointment overlap finding ids for
    the R5 acceptance note. They are collected by code filtering on the
    fixed overlap finding id, so nothing model-controlled can reach a
    rendered note through them.
    """

    checkable_history_present: bool = False
    checkable_financials_present: bool = False
    charges_present: bool = False
    contradicted_with_relevant_basis: bool = False
    unverified_present: bool = False
    overlaps_present: bool = False
    claims_material_present: bool = False
    media_items_present: bool = False
    overlap_finding_ids: tuple[str, ...] = ()


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
    gate_signals: GateSignals | None = None,
) -> EnforcementResult:
    """Apply the enforcement rules, in order.

    1. Cited ids are normalized case-insensitively to catalog casing;
       anything that still is not a catalog id is dropped and noted as a
       count only (unrecognized ids are model-controlled text and never
       appear in a note).
    2. The ceiling: every cited id must meet its evidence condition. The
       mechanical ids (R1, R2, R3, A1, A2, A7) are rejected unless
       mechanically detected on this casefile. The judgment ids (R4, R5,
       A3, A4, A5, A6) are rejected unless their gate_signals gate is open
       (module docstring) OR the same id is in the mechanical candidate
       set (R4's origin-year floor). With no gate_signals every gate is
       locked. Every rejection is noted with fixed text and the
       catalog-validated id. An accepted R5 is noted with the overlap
       finding ids that ground it.
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
    signals = gate_signals or GateSignals()

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

    # Fixed rejection texts for the signal-gated judgment triggers. Catalog
    # ids only; nothing model-controlled can reach a note.
    gate_rejections: dict[str, tuple[bool, str]] = {
        "R4": (
            signals.contradicted_with_relevant_basis,
            "rejected trigger R4: no claim assessment survives as contradicted"
            " with relevant registry evidence",
        ),
        "R5": (
            signals.overlaps_present and signals.claims_material_present,
            (
                "rejected trigger R5: co-appointment overlap is recorded but no"
                " claims material is present to judge disclosure against"
            )
            if signals.overlaps_present
            else "rejected trigger R5: no co-appointment overlap is recorded in this casefile",
        ),
        "A3": (
            signals.charges_present and signals.checkable_financials_present,
            "rejected trigger A3: no charges on the register or no checkable"
            " financials claim is recorded in this casefile",
        ),
        "A4": (
            signals.unverified_present,
            "rejected trigger A4: no model-assessed checkable claim survives as unverified",
        ),
        "A5": (
            signals.checkable_history_present,
            "rejected trigger A5: no checkable history claim is recorded in this casefile",
        ),
        "A6": (
            signals.media_items_present,
            "rejected trigger A6: the media stage did not run or returned no"
            " items on this casefile",
        ),
    }

    candidate_ids = {c.id for c in candidates}
    triggered: list[str] = []
    for trigger_id in cited:
        if trigger_id in gate_rejections:
            gate_open, rejection_note = gate_rejections[trigger_id]
            # Hybrid path: R4 (and any future hybrid) is accepted when its
            # judgment gate is open OR detect_candidates emitted it.
            if gate_open or trigger_id in candidate_ids:
                triggered.append(trigger_id)
                if trigger_id == "R5" and signals.overlap_finding_ids:
                    notes.append(
                        "trigger R5 is grounded in overlap finding(s): "
                        + ", ".join(signals.overlap_finding_ids)
                    )
            else:
                notes.append(rejection_note)
        elif trigger_id in candidate_ids:
            triggered.append(trigger_id)
        else:
            notes.append(f"rejected trigger {trigger_id}: not supported by the casefile evidence")

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
