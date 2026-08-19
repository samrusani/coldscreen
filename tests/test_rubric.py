"""Rubric catalog fidelity, level arithmetic, candidate detection, enforcement.

The catalog test parses rubric.md itself (table, conditions column, rules,
and version), so the code mirror cannot drift from the versioned rubric
without failing the build.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from coldscreen.models import (
    CaseFile,
    CompanyProfile,
    Evidence,
    Finding,
    InsolvencyCase,
)
from coldscreen.rubric import (
    INSOLVENCY_STATUS_FAMILY,
    MECHANICAL_IDS,
    NOT_ACTIVE_STATUSES,
    R4_RELEVANT_FINDINGS,
    RUBRIC_RULES,
    RUBRIC_VERSION,
    TRIGGER_INDEX,
    TRIGGERS,
    GateSignals,
    TriggerCandidate,
    compute_level,
    detect_candidates,
    enforce,
    rubric_text,
)

REPO_ROOT = Path(__file__).parent.parent
SCREENED_AT = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)


def parse_rubric_md() -> list[tuple[str, str, str, str]]:
    """(id, text, severity, condition) rows from the rubric.md table."""
    rows = []
    pattern = re.compile(
        r"^\|\s*([RA]\d)\s*\|\s*(.+?)\s*\|\s*(RED|AMBER)\s*\|\s*(.+?)\s*\|$",
    )
    for line in (REPO_ROOT / "rubric.md").read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            rows.append((match.group(1), match.group(2), match.group(3).lower(), match.group(4)))
    return rows


def parse_rubric_md_rules() -> list[str]:
    """The numbered rules under rubric.md's Rules heading, in order."""
    rules = []
    for line in (REPO_ROOT / "rubric.md").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\d+\.\s+(.+?)\s*$", line)
        if match:
            rules.append(match.group(1))
    return rules


def test_catalog_mirrors_rubric_md_exactly() -> None:
    rows = parse_rubric_md()
    assert rows, "rubric.md trigger table not found"
    assert [(t.id, t.text, t.severity, t.condition) for t in TRIGGERS] == rows


def test_rules_mirror_rubric_md_exactly() -> None:
    rules = parse_rubric_md_rules()
    assert rules, "rubric.md rules not found"
    assert list(RUBRIC_RULES) == rules


def test_version_mirrors_rubric_md() -> None:
    text = (REPO_ROOT / "rubric.md").read_text(encoding="utf-8")
    match = re.search(r"Version (\d+\.\d+)\.", text)
    assert match is not None, "rubric.md version marker not found"
    assert RUBRIC_VERSION == match.group(1)


def test_rubric_text_carries_every_trigger_condition_and_rule() -> None:
    text = rubric_text()
    assert f"version {RUBRIC_VERSION}" in text
    for trigger in TRIGGERS:
        assert trigger.id in text
        assert trigger.text in text
        assert trigger.condition in text
    for rule in RUBRIC_RULES:
        assert rule in text
    assert "Any RED trigger forces a RED verdict." in text


def test_every_trigger_id_is_either_mechanical_or_signal_gated() -> None:
    """Rubric rule 4 requires a gate for every id: nothing is freely citable.

    R4 is a hybrid: it stays in the signal-gated set so a non-date
    contradiction still has to clear GateSignals, and detect_candidates may
    also emit it as a floor candidate. It is not in MECHANICAL_IDS, because
    a citation of R4 without the origin-year hit must not be accepted the
    way a citation of R1 is (only when the detector fired).
    """
    signal_gated = {"R4", "R5", "A3", "A4", "A5", "A6"}
    assert MECHANICAL_IDS | signal_gated == set(TRIGGER_INDEX)
    assert MECHANICAL_IDS & signal_gated == set()
    assert "R4" not in MECHANICAL_IDS


def test_r4_relevance_table_is_the_fixed_registry_set() -> None:
    assert R4_RELEVANT_FINDINGS == {
        "history": frozenset({"REG-002", "REG-010"}),
        "financials": frozenset({"REG-006", "REG-003"}),
        "regulatory": frozenset({"REG-003", "REG-004"}),
        "team": frozenset({"REG-008"}),
        "traction": frozenset({"REG-008"}),
    }
    # "other" is deliberately absent: no registry finding can ground a
    # contradiction of an uncategorized claim.
    assert "other" not in R4_RELEVANT_FINDINGS


# -- compute_level: the pure function ----------------------------------------


def test_level_matrix() -> None:
    assert compute_level([]) == "green"
    assert compute_level(["A1"]) == "amber"
    assert compute_level(["A1", "A2"]) == "amber"
    assert compute_level(["A7"]) == "amber"
    assert compute_level(["R1"]) == "red"
    assert compute_level(["R3", "A1"]) == "red"
    assert compute_level(["A6", "R5", "A2"]) == "red"
    # Unknown ids carry no severity weight.
    assert compute_level(["Z9"]) == "green"
    assert compute_level(["Z9", "A1"]) == "amber"


# -- detect_candidates --------------------------------------------------------


def _finding(finding_id: str, stage: str, severity: str = "info", statement: str = "x") -> Finding:
    return Finding(
        id=finding_id,
        stage=stage,
        severity=severity,  # type: ignore[arg-type]
        confidence="confirmed",
        statement=statement,
        evidence=[Evidence(source_url="https://example.invalid/e", retrieved_at=SCREENED_AT)],
    )


def _casefile(
    findings: list[Finding],
    status: str = "active",
    insolvency: bool = False,
) -> CaseFile:
    return CaseFile(
        subject=CompanyProfile.model_validate(
            {
                "company_name": "FICTIONAL SUBJECT LTD",
                "company_number": "99999903",
                "company_status": status,
            }
        ),
        insolvency_cases=[InsolvencyCase(type="creditors-voluntary-liquidation")]
        if insolvency
        else [],
        findings=findings,
        tool_version="0.1.0.dev0",
        screened_at=SCREENED_AT,
    )


def test_detects_r1_from_red_sanctions_finding() -> None:
    casefile = _casefile([_finding("SAN-002", "sanctions", "red")])
    assert [c.id for c in detect_candidates(casefile)] == ["R1"]
    assert detect_candidates(casefile)[0].finding_ids == ["SAN-002"]


def test_amber_sanctions_finding_never_feeds_r1() -> None:
    """An officer sanctions match is an amber finding by design; it must
    open no red trigger."""
    casefile = _casefile([_finding("SAN-002", "sanctions", "amber")])
    assert detect_candidates(casefile) == []


def test_detects_r2_from_cases_or_status() -> None:
    """Rubric 0.2: cases on the register OR an insolvency status fires R2."""
    both = _casefile(
        [_finding("REG-001", "registry", "red"), _finding("REG-007", "registry", "red")],
        status="liquidation",
        insolvency=True,
    )
    both_candidates = {c.id: c for c in detect_candidates(both)}
    assert "R2" in both_candidates
    assert both_candidates["R2"].finding_ids == ["REG-007", "REG-001"]

    # Cases on file with status active: still an R2 candidate under 0.2.
    cases_only = _casefile(
        [_finding("REG-007", "registry", "red")], status="active", insolvency=True
    )
    cases_candidates = {c.id: c for c in detect_candidates(cases_only)}
    assert "R2" in cases_candidates
    assert cases_candidates["R2"].finding_ids == ["REG-007"]


@pytest.mark.parametrize(
    "status",
    sorted(INSOLVENCY_STATUS_FAMILY),
)
def test_r2_fires_from_status_alone_with_the_profile_as_evidence(status: str) -> None:
    """No case detail retrieved: the status finding alone carries R2."""
    casefile = _casefile([_finding("REG-001", "registry", "red")], status=status)
    candidates = {c.id: c for c in detect_candidates(casefile)}
    assert "R2" in candidates
    assert candidates["R2"].finding_ids == ["REG-001"]
    assert status in candidates["R2"].reason
    # An insolvency status is never also an A7 candidate.
    assert "A7" not in candidates


def test_voluntary_arrangement_is_in_the_r2_status_family() -> None:
    assert "voluntary-arrangement" in INSOLVENCY_STATUS_FAMILY


@pytest.mark.parametrize("status", sorted(NOT_ACTIVE_STATUSES))
def test_a7_fires_for_not_active_statuses(status: str) -> None:
    casefile = _casefile([_finding("REG-001", "registry", "amber")], status=status)
    candidates = {c.id: c for c in detect_candidates(casefile)}
    assert "A7" in candidates
    assert candidates["A7"].finding_ids == ["REG-001"]
    assert "R2" not in candidates


def test_active_status_yields_neither_r2_nor_a7() -> None:
    casefile = _casefile([_finding("REG-001", "registry", "info")], status="active")
    assert detect_candidates(casefile) == []


def test_status_matching_is_case_insensitive() -> None:
    """A mixed-case registry status must not silently miss R2 or A7."""
    mixed_liquidation = _casefile([_finding("REG-001", "registry", "red")], status="Liquidation")
    assert "R2" in {c.id for c in detect_candidates(mixed_liquidation)}
    mixed_dissolved = _casefile([_finding("REG-001", "registry", "amber")], status="DISSOLVED")
    assert "A7" in {c.id for c in detect_candidates(mixed_dissolved)}


def test_detects_r3_from_red_network_finding() -> None:
    casefile = _casefile([_finding("NET-101", "network", "red")])
    assert [c.id for c in detect_candidates(casefile)] == ["R3"]


def test_detects_a1_from_overdue_findings() -> None:
    accounts = _casefile([_finding("REG-003", "registry", "amber")])
    assert [c.id for c in detect_candidates(accounts)] == ["A1"]
    confirmation = _casefile([_finding("REG-004", "registry", "amber")])
    assert [c.id for c in detect_candidates(confirmation)] == ["A1"]


def test_detects_a2_only_from_amber_reg009() -> None:
    churn = _casefile([_finding("REG-009", "registry", "amber")])
    assert [c.id for c in detect_candidates(churn)] == ["A2"]
    quiet = _casefile([_finding("REG-009", "registry", "info")])
    assert detect_candidates(quiet) == []


def test_clean_casefile_yields_no_candidates() -> None:
    casefile = _casefile(
        [
            _finding("REG-001", "registry", "info"),
            _finding("SAN-001", "sanctions", "info"),
            _finding("NET-101", "network", "info"),
        ]
    )
    assert detect_candidates(casefile) == []


# -- enforcement matrix -------------------------------------------------------

QUESTIONS = ["One?", "Two?", "Three?"]


def _candidates(*ids: str) -> list[TriggerCandidate]:
    return [TriggerCandidate(id=i, reason="mechanical", finding_ids=[]) for i in ids]


def test_no_triggers_no_candidates_is_green() -> None:
    result = enforce("green", [], QUESTIONS, [])
    assert result.level == "green"
    assert result.triggered == []
    assert result.notes == []
    assert result.level_note is None


def test_single_amber_is_amber() -> None:
    result = enforce("amber", ["A1"], QUESTIONS, _candidates("A1"))
    assert result.level == "amber"
    assert result.triggered == ["A1"]
    assert result.notes == []


def test_multiple_amber_stays_amber() -> None:
    signals = GateSignals(media_items_present=True)
    result = enforce(
        "amber", ["A1", "A2", "A6"], QUESTIONS, _candidates("A1", "A2"), gate_signals=signals
    )
    assert result.level == "amber"
    assert result.triggered == ["A1", "A2", "A6"]


def test_single_red_is_red() -> None:
    # A genuine mechanical R1 candidate cited by the model stays red, with
    # no rejection notes: the ceiling only blocks unsupported citations.
    result = enforce("red", ["R1"], QUESTIONS, _candidates("R1"))
    assert result.level == "red"
    assert result.triggered == ["R1"]
    assert result.notes == []


def test_red_plus_amber_is_red() -> None:
    result = enforce("red", ["R2", "A1"], QUESTIONS, _candidates("R2", "A1"))
    assert result.level == "red"


def test_model_green_with_red_candidate_is_corrected_to_red() -> None:
    result = enforce("green", [], QUESTIONS, _candidates("R1"))
    assert result.level == "red"
    assert result.triggered == ["R1"]
    assert result.model_level == "green"
    assert result.level_note is not None
    assert "pure function" in result.level_note
    assert any("added mechanically detected" in note for note in result.notes)


def test_model_red_with_only_amber_triggers_is_corrected_to_amber() -> None:
    result = enforce("red", ["A1"], QUESTIONS, _candidates("A1"))
    assert result.level == "amber"
    assert result.level_note is not None


def test_unknown_trigger_ids_are_dropped_and_counted_never_echoed() -> None:
    result = enforce("amber", ["A1", "X7", "banana"], QUESTIONS, _candidates("A1"))
    assert result.triggered == ["A1"]
    # The note reports a count only: unrecognized ids are model-controlled
    # text and must never appear in memo-rendered note text.
    assert "dropped 2 unrecognized trigger id(s)" in result.notes
    joined = " ".join(result.notes)
    assert "banana" not in joined
    assert "X7" not in joined


def test_missing_candidates_are_added_and_noted() -> None:
    result = enforce("green", [], QUESTIONS, _candidates("A1", "A2"))
    assert result.triggered == ["A1", "A2"]
    assert result.level == "amber"
    assert any("the model omitted" in note for note in result.notes)


def test_a7_candidate_is_enforced_like_any_mechanical_trigger() -> None:
    added = enforce("green", [], QUESTIONS, _candidates("A7"))
    assert added.triggered == ["A7"]
    assert added.level == "amber"
    cited = enforce("amber", ["A7"], QUESTIONS, _candidates("A7"))
    assert cited.triggered == ["A7"]
    assert cited.notes == []


def test_duplicate_model_triggers_are_deduplicated() -> None:
    result = enforce("amber", ["A1", "A1", "A2"], QUESTIONS, _candidates("A1", "A2"))
    assert result.triggered == ["A1", "A2"]


def test_questions_clamped_to_five_with_note() -> None:
    many = [f"Question {n}?" for n in range(1, 8)]
    result = enforce("green", [], many, [])
    assert len(result.questions) == 5
    assert result.questions == many[:5]
    assert any("clamped questions" in note for note in result.notes)


# -- the enforcement ceiling ----------------------------------------------------


def test_model_cited_r1_without_candidate_is_rejected_with_note() -> None:
    result = enforce("red", ["R1"], QUESTIONS, [])
    assert result.level == "green"
    assert result.triggered == []
    assert "rejected trigger R1: not supported by the casefile evidence" in result.notes
    assert result.level_note is not None  # red proposed, green computed


def test_model_cited_r2_and_r3_need_mechanical_support() -> None:
    unsupported = enforce("red", ["R2", "R3", "A1"], QUESTIONS, _candidates("A1"))
    assert unsupported.level == "amber"
    assert unsupported.triggered == ["A1"]
    assert "rejected trigger R2: not supported by the casefile evidence" in unsupported.notes
    assert "rejected trigger R3: not supported by the casefile evidence" in unsupported.notes
    supported = enforce("red", ["R3"], QUESTIONS, _candidates("R3"))
    assert supported.level == "red"
    assert supported.triggered == ["R3"]
    assert supported.notes == []


@pytest.mark.parametrize("trigger_id", sorted(MECHANICAL_IDS))
def test_every_mechanical_id_is_rejected_without_its_candidate(trigger_id: str) -> None:
    """Rubric rule 4 closes the ceiling over the WHOLE mechanical tier: a
    cited A1 without the overdue evidence is as dead as a cited R1."""
    severity = TRIGGER_INDEX[trigger_id].severity
    result = enforce(severity, [trigger_id], QUESTIONS, [])
    assert result.triggered == []
    assert result.level == "green"
    assert f"rejected trigger {trigger_id}: not supported by the casefile evidence" in result.notes


def test_r4_and_r5_are_rejected_without_gate_signals() -> None:
    """No gate_signals means every gate is locked: claims-free behavior."""
    result = enforce("red", ["R4", "R5"], QUESTIONS, [])
    assert result.level == "green"
    assert result.triggered == []
    assert (
        "rejected trigger R4: no claim assessment survives as contradicted"
        " with relevant registry evidence" in result.notes
    )
    assert (
        "rejected trigger R5: no co-appointment overlap is recorded in this casefile"
        in result.notes
    )


# -- the gating matrix: R4, R5, A3, A4, A5, A6 -----------------------------------

LOCKED = GateSignals()
ALL_OPEN = GateSignals(
    checkable_history_present=True,
    checkable_financials_present=True,
    charges_present=True,
    contradicted_with_relevant_basis=True,
    unverified_present=True,
    overlaps_present=True,
    claims_material_present=True,
    media_items_present=True,
    overlap_finding_ids=("NET-002",),
)


@pytest.mark.parametrize(
    ("trigger_id", "open_signals", "rejection_fragment"),
    [
        (
            "R4",
            GateSignals(contradicted_with_relevant_basis=True),
            "no claim assessment survives as contradicted with relevant registry evidence",
        ),
        (
            "R5",
            GateSignals(overlaps_present=True, claims_material_present=True),
            "no co-appointment overlap is recorded",
        ),
        (
            "A3",
            GateSignals(charges_present=True, checkable_financials_present=True),
            "rejected trigger A3: no charges on the register or no checkable"
            " financials claim is recorded",
        ),
        (
            "A4",
            GateSignals(unverified_present=True),
            "no model-assessed checkable claim survives as unverified",
        ),
        (
            "A5",
            GateSignals(checkable_history_present=True),
            "rejected trigger A5: no checkable history claim is recorded",
        ),
        (
            "A6",
            GateSignals(media_items_present=True),
            "rejected trigger A6: the media stage did not run or returned no items",
        ),
    ],
)
def test_gated_trigger_accepted_only_when_its_gate_is_open(
    trigger_id: str, open_signals: GateSignals, rejection_fragment: str
) -> None:
    severity = TRIGGER_INDEX[trigger_id].severity
    accepted = enforce(severity, [trigger_id], QUESTIONS, [], gate_signals=open_signals)
    assert accepted.triggered == [trigger_id]
    assert accepted.level == severity

    rejected = enforce(severity, [trigger_id], QUESTIONS, [], gate_signals=LOCKED)
    assert rejected.triggered == []
    assert rejected.level == "green"
    assert any(rejection_fragment in note for note in rejected.notes)


def test_r4_gate_needs_a_surviving_relevant_contradiction_not_just_claims() -> None:
    claims_only = GateSignals(
        checkable_history_present=True,
        checkable_financials_present=True,
        claims_material_present=True,
        unverified_present=True,
    )
    result = enforce("red", ["R4"], QUESTIONS, [], gate_signals=claims_only)
    assert result.triggered == []
    assert result.level == "green"


def test_r4_candidate_is_accepted_even_when_the_judgment_gate_is_locked() -> None:
    """The hybrid path: detect_candidates emitted R4, so a citation cannot
    be rejected for want of a surviving assessment. The floor would add it
    anyway; citing it must not produce a rejection note."""
    result = enforce("red", ["R4"], QUESTIONS, _candidates("R4"), gate_signals=LOCKED)
    assert result.triggered == ["R4"]
    assert result.level == "red"
    assert result.notes == []


def test_r4_candidate_is_forced_when_the_model_omits_it() -> None:
    result = enforce("green", [], QUESTIONS, _candidates("R4"), gate_signals=LOCKED)
    assert result.triggered == ["R4"]
    assert result.level == "red"
    assert any("the model omitted" in note and "R4" in note for note in result.notes)


def test_mechanical_r4_floor_survives_every_citation_subset() -> None:
    """With an origin-year R4 candidate, every combination of model-cited
    catalog ids still yields red with R4 present. Gates stay locked, so no
    judgment trigger other than the candidate can join it."""
    catalog_ids = [t.id for t in TRIGGERS]
    candidates = _candidates("R4", "A1")
    for mask in range(1 << len(catalog_ids)):
        cited = [catalog_ids[i] for i in range(len(catalog_ids)) if mask & (1 << i)]
        result = enforce("green", cited, QUESTIONS, candidates, gate_signals=LOCKED)
        assert result.level == "red"
        assert "R4" in result.triggered
        assert "A1" in result.triggered
        # Locked gates: no judgment id except R4 (the candidate) survives.
        assert "R5" not in result.triggered
        assert "A6" not in result.triggered


def test_r5_gate_needs_overlap_and_claims_material_together() -> None:
    overlap_only = GateSignals(overlaps_present=True, overlap_finding_ids=("NET-002",))
    rejected = enforce("red", ["R5"], QUESTIONS, [], gate_signals=overlap_only)
    assert rejected.triggered == []
    assert (
        "rejected trigger R5: co-appointment overlap is recorded but no claims"
        " material is present to judge disclosure against" in rejected.notes
    )

    claims_only = GateSignals(claims_material_present=True)
    rejected_again = enforce("red", ["R5"], QUESTIONS, [], gate_signals=claims_only)
    assert rejected_again.triggered == []
    assert (
        "rejected trigger R5: no co-appointment overlap is recorded in this casefile"
        in rejected_again.notes
    )


def test_accepted_r5_notes_the_overlap_finding_ids() -> None:
    signals = GateSignals(
        overlaps_present=True,
        claims_material_present=True,
        overlap_finding_ids=("NET-002",),
    )
    result = enforce("red", ["R5"], QUESTIONS, [], gate_signals=signals)
    assert result.triggered == ["R5"]
    assert "trigger R5 is grounded in overlap finding(s): NET-002" in result.notes


def test_a3_gate_needs_charges_and_a_financials_claim_together() -> None:
    charges_only = GateSignals(charges_present=True)
    assert enforce("amber", ["A3"], QUESTIONS, [], gate_signals=charges_only).triggered == []
    financials_only = GateSignals(checkable_financials_present=True)
    assert enforce("amber", ["A3"], QUESTIONS, [], gate_signals=financials_only).triggered == []


def test_a4_gate_needs_a_surviving_unverified_assessment() -> None:
    all_supported = GateSignals(checkable_history_present=True, claims_material_present=True)
    result = enforce("amber", ["A4"], QUESTIONS, [], gate_signals=all_supported)
    assert result.triggered == []
    assert any("rejected trigger A4" in note for note in result.notes)


def test_a6_gate_matrix_not_run_ran_empty_ran_with_items() -> None:
    """The three media states: only ran-with-items opens A6. The signal for
    the first two states is identically False (gate_signals derivation from
    the casefile is covered in test_synthesis)."""
    not_run_or_empty = GateSignals(media_items_present=False)
    ran_with_items = GateSignals(media_items_present=True)
    rejected = enforce("amber", ["A6"], QUESTIONS, [], gate_signals=not_run_or_empty)
    assert rejected.triggered == []
    assert rejected.level == "green"
    accepted = enforce("amber", ["A6"], QUESTIONS, [], gate_signals=ran_with_items)
    assert accepted.triggered == ["A6"]
    assert accepted.level == "amber"


def test_open_gates_accept_the_full_judgment_set() -> None:
    result = enforce(
        "red", ["R4", "R5", "A3", "A4", "A5", "A6"], QUESTIONS, [], gate_signals=ALL_OPEN
    )
    assert result.triggered == ["R4", "R5", "A3", "A4", "A5", "A6"]
    assert result.level == "red"
    # The only note is the R5 grounding note; nothing was rejected.
    assert result.notes == ["trigger R5 is grounded in overlap finding(s): NET-002"]


def test_gate_rejections_use_fixed_text_and_catalog_ids_only() -> None:
    result = enforce("red", ["R4", "R5", "A3", "A4", "A5", "A6"], QUESTIONS, [], gate_signals=None)
    assert result.triggered == []
    assert len(result.notes) >= 6
    # Fixed text only: every note starts with the rejection prefix and a
    # catalog id; nothing model-controlled appears.
    for note in result.notes[:6]:
        assert note.startswith("rejected trigger ")
        assert note.split()[2].rstrip(":") in TRIGGER_INDEX


def test_trigger_ids_are_normalized_to_catalog_casing_on_intake() -> None:
    # "a6" counts as A6; a lowercase "r1" is matched against the candidate
    # set AFTER normalization, so it is accepted when R1 was detected.
    result = enforce(
        "red",
        ["a6", "r1"],
        QUESTIONS,
        _candidates("R1"),
        gate_signals=GateSignals(media_items_present=True),
    )
    assert result.triggered == ["A6", "R1"]
    assert result.level == "red"
    assert result.notes == []


def test_mechanical_candidates_are_validated_against_the_catalog() -> None:
    rogue = [TriggerCandidate(id="Z9", reason="drifted detector", finding_ids=[])]
    with pytest.raises(AssertionError, match="not in the rubric catalog"):
        enforce("green", [], QUESTIONS, rogue)


def test_anchor_level_is_invariant_to_fabricated_model_output() -> None:
    """Identical candidate set, wildly different model behavior: one level."""
    candidates = _candidates("A1", "A2")
    behaviors: list[tuple[Literal["red", "amber", "green"], list[str]]] = [
        ("green", []),
        ("amber", ["A1", "A2"]),
        ("red", ["R1", "A1"]),
        ("red", ["R3", "R5"]),
        ("red", ["r2"]),
        ("red", ["A6", "A3", "A4", "A5", "A7"]),
        ("green", ["nonsense-id"]),
    ]
    levels = {
        enforce(level, triggered, QUESTIONS, candidates).level for level, triggered in behaviors
    }
    assert levels == {"amber"}


def test_level_cannot_move_outside_evidence_supported_bounds() -> None:
    """The README guarantee, as a property: with fixed candidates and fixed
    gate signals, every possible model citation set stays within
    [level(candidates), level(candidates plus open gates)]."""
    candidates = _candidates("A1")
    signals = GateSignals(media_items_present=True)  # only the A6 gate is open
    floor = enforce("green", [], QUESTIONS, candidates, gate_signals=signals).level
    assert floor == "amber"
    wildest = enforce(
        "red",
        ["R1", "R2", "R3", "R4", "R5", "A3", "A4", "A5", "A6", "A7"],
        QUESTIONS,
        candidates,
        gate_signals=signals,
    )
    assert wildest.level == "amber"
    assert wildest.triggered == ["A6", "A1"]


def test_every_catalog_id_is_known_to_the_index() -> None:
    for trigger in TRIGGERS:
        assert TRIGGER_INDEX[trigger.id] is trigger
