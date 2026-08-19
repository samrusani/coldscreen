"""Rubric catalog fidelity, level arithmetic, candidate detection, enforcement.

The catalog test parses rubric.md itself, so the code mirror cannot drift
from the versioned rubric without failing the build.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from firstpass.models import (
    CaseFile,
    CompanyProfile,
    Evidence,
    Finding,
    InsolvencyCase,
)
from firstpass.rubric import (
    TRIGGER_INDEX,
    TRIGGERS,
    TriggerCandidate,
    compute_level,
    detect_candidates,
    enforce,
    rubric_text,
)

REPO_ROOT = Path(__file__).parent.parent
SCREENED_AT = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)


def parse_rubric_md() -> list[tuple[str, str, str]]:
    """(id, text, severity) rows from the rubric.md trigger table."""
    rows = []
    for line in (REPO_ROOT / "rubric.md").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\|\s*([RA]\d)\s*\|\s*(.+?)\s*\|\s*(RED|AMBER)\s*\|$", line)
        if match:
            rows.append((match.group(1), match.group(2), match.group(3).lower()))
    return rows


def test_catalog_mirrors_rubric_md_exactly() -> None:
    rows = parse_rubric_md()
    assert rows, "rubric.md trigger table not found"
    assert [(t.id, t.text, t.severity) for t in TRIGGERS] == rows


def test_rubric_text_carries_every_trigger_and_rule() -> None:
    text = rubric_text()
    for trigger in TRIGGERS:
        assert trigger.id in text
        assert trigger.text in text
    assert "Any RED trigger forces a RED verdict." in text


# -- compute_level: the pure function ----------------------------------------


def test_level_matrix() -> None:
    assert compute_level([]) == "green"
    assert compute_level(["A1"]) == "amber"
    assert compute_level(["A1", "A2"]) == "amber"
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


def test_detects_r2_only_with_cases_and_status_family() -> None:
    in_liquidation = _casefile(
        [_finding("REG-007", "registry", "red")], status="liquidation", insolvency=True
    )
    assert "R2" in [c.id for c in detect_candidates(in_liquidation)]
    # Cases on file but status active: not an ACTIVE insolvency event.
    active_with_history = _casefile(
        [_finding("REG-007", "registry", "red")], status="active", insolvency=True
    )
    assert "R2" not in [c.id for c in detect_candidates(active_with_history)]
    # Status in family but no cases retrieved: nothing mechanical to stand on.
    no_cases = _casefile([], status="administration", insolvency=False)
    assert "R2" not in [c.id for c in detect_candidates(no_cases)]


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
    result = enforce("amber", ["A1", "A2", "A6"], QUESTIONS, _candidates("A1", "A2"))
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


def test_duplicate_model_triggers_are_deduplicated() -> None:
    result = enforce("amber", ["A1", "A1", "A2"], QUESTIONS, [])
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


def test_r4_and_r5_are_rejected_unconditionally() -> None:
    result = enforce("red", ["R4", "R5"], QUESTIONS, [])
    assert result.level == "green"
    assert result.triggered == []
    assert (
        "rejected trigger R4: requires the claims layer which is not part of this milestone"
        in result.notes
    )
    assert (
        "rejected trigger R5: requires the claims layer which is not part of this milestone"
        in result.notes
    )


def test_amber_judgment_triggers_remain_freely_citable() -> None:
    result = enforce("amber", ["A3", "A6", "A4", "A5"], QUESTIONS, [])
    assert result.level == "amber"
    assert result.triggered == ["A3", "A6", "A4", "A5"]
    assert result.notes == []


def test_trigger_ids_are_normalized_to_catalog_casing_on_intake() -> None:
    # "a6" counts as A6; a lowercase "r1" is matched against the candidate
    # set AFTER normalization, so it is accepted when R1 was detected.
    result = enforce("red", ["a6", "r1"], QUESTIONS, _candidates("R1"))
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
        ("green", ["nonsense-id"]),
    ]
    levels = {
        enforce(level, triggered, QUESTIONS, candidates).level for level, triggered in behaviors
    }
    assert levels == {"amber"}


def test_every_catalog_id_is_known_to_the_index() -> None:
    for trigger in TRIGGERS:
        assert TRIGGER_INDEX[trigger.id] is trigger
