"""Stage 7: input assembly, prompt, parse retries, language gate, enforcement."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from coldscreen.casedir import load_casefile
from coldscreen.language import find_banned_terms
from coldscreen.models import (
    CaseFile,
    Charge,
    Claim,
    CompanyProfile,
    Evidence,
    Finding,
    MediaItem,
    MediaScreening,
)
from coldscreen.rubric import detect_candidates
from coldscreen.synthesis import (
    MISSING_ASSESSMENT_NOTE,
    SYNTHESIS_SCHEMA,
    SynthesisError,
    _RawAssessment,
    apply_synthesis,
    build_synthesis_input,
    build_synthesis_schema,
    enforce_assessments,
    gate_signals,
    load_prompt,
    prompt_version,
    serialize_input,
    synthesize,
)

from .conftest import FIXTURES_DIR
from .fakes import FakeModelProvider, assessment_json, synthesis_json

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
AMBER_DIR = FIXTURES_DIR / "amber"
GOLDEN_DIR = FIXTURES_DIR / "golden"


def minimal_casefile(findings: list[Finding] | None = None) -> CaseFile:
    return CaseFile(
        subject=CompanyProfile.model_validate(
            {"company_name": "FICTIONAL SUBJECT LTD", "company_number": "99999903"}
        ),
        findings=findings or [],
        tool_version="0.1.0.dev0",
        screened_at=NOW,
    )


def amber_casefile() -> CaseFile:
    finding = Finding(
        id="REG-003",
        stage="registry",
        severity="amber",
        confidence="confirmed",
        statement="The registry marks the next accounts as overdue.",
        evidence=[Evidence(source_url="https://example.invalid/profile", retrieved_at=NOW)],
    )
    return minimal_casefile([finding])


CLAIMS = [
    Claim(
        id="CLM-001",
        text="Operating since 2015 with a national footprint",
        source="deck p.2",
        category="history",
        checkable=True,
    ),
    Claim(
        id="CLM-002",
        text="Our platform eliminates fraud in widget procurement",
        source="deck p.2",
        category="other",
        checkable=False,
    ),
    Claim(
        id="CLM-003",
        text="A team of 40 widget engineers",
        source="deck p.3",
        category="team",
        checkable=True,
    ),
]


def claims_casefile() -> CaseFile:
    """A casefile with two checkable claims, one puffery claim, and a
    registry finding (REG-002) whose evidence a basis can resolve to."""
    incorporation = Finding(
        id="REG-002",
        stage="registry",
        severity="info",
        confidence="confirmed",
        statement="Incorporated on 2019-05-14 (7 full years before the screening date).",
        evidence=[Evidence(source_url="https://example.invalid/profile", retrieved_at=NOW)],
    )
    casefile = minimal_casefile([incorporation])
    return casefile.model_copy(update={"claims": list(CLAIMS)})


# -- prompt ---------------------------------------------------------------------


def test_prompt_loads_and_carries_version_4() -> None:
    prompt = load_prompt()
    assert prompt_version(prompt) == "4"


def test_prompt_contains_the_load_bearing_rules() -> None:
    prompt = load_prompt()
    assert "If something is not in that document, it does not exist" in prompt
    # Banned vocabulary is listed explicitly.
    for term in ("fraud", "criminal", "dishonest", "con artist"):
        assert term in prompt
    # Every rubric trigger id is defined in the prompt, A7 included.
    for trigger_id in ("R1", "R2", "R3", "R4", "R5", "A1", "A2", "A3", "A4", "A5", "A6", "A7"):
        assert trigger_id in prompt
    assert "Any RED trigger forces a RED verdict" in prompt
    # Rubric 0.2: the fires-only-when conditions travel with the table.
    assert "Fires only when" in prompt
    # The weekend 3 assessment contract is stated.
    assert "assessments" in prompt
    assert "record_note" in prompt
    assert "basis_finding_ids" in prompt
    # The prose rules after the review fix: ids, never wording.
    assert "NEVER repeat a claim's wording" in prompt


def test_prompt_forbids_describing_not_run_or_failed_stages_as_clean() -> None:
    """The hardening-1 rule: absence and failure are stated, never dressed
    up as a clean pass. Wording asserted here so the rule cannot silently
    fall out of the prompt."""
    prompt = load_prompt()
    assert 'must never be described as clean, passed, or "no matches found"' in prompt
    assert "a failed stage is a gap in the screen, not a result" in prompt


def test_prompt_version_marker_is_required() -> None:
    with pytest.raises(SynthesisError, match="version marker"):
        prompt_version("a prompt without the marker")


# -- input assembly ----------------------------------------------------------------


def test_synthesis_input_is_deterministic_and_sorted() -> None:
    casefile = load_casefile(AMBER_DIR)
    candidates = detect_candidates(casefile)
    first = serialize_input(build_synthesis_input(casefile, candidates))
    second = serialize_input(build_synthesis_input(casefile, candidates))
    assert first == second
    # Sorted keys at every level: re-dumping the parsed document with
    # sort_keys must be a fixed point.
    parsed = json.loads(first)
    assert first == json.dumps(parsed, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def test_synthesis_input_carries_findings_rubric_candidates_and_media_titles() -> None:
    casefile = load_casefile(AMBER_DIR)
    payload = build_synthesis_input(casefile, detect_candidates(casefile))
    assert {c["id"] for c in payload["trigger_candidates"]} == {"A1", "A2"}
    assert "Any RED trigger forces a RED verdict." in payload["rubric"]
    finding_ids = {f["id"] for f in payload["findings"]}
    assert {"REG-003", "REG-009", "SAN-001", "MED-001"} <= finding_ids
    titles = [item["title"] for item in payload["media"]["items"]]
    assert any("fraud" in t for t in titles)  # the model sees headlines
    # No raw registry payloads: the profile summary is compact fields only.
    assert "registered_office_address" not in payload["subject"]
    assert "links" not in payload["subject"]


def test_synthesis_input_carries_the_stored_claims() -> None:
    payload = build_synthesis_input(claims_casefile(), [])
    assert payload["claims"] == [
        {
            "id": claim.id,
            "text": claim.text,
            "source": claim.source,
            "category": claim.category,
            "checkable": claim.checkable,
        }
        for claim in CLAIMS
    ]


# -- happy path and enforcement wiring ----------------------------------------------


def test_happy_path_records_metadata_and_uses_the_schema() -> None:
    provider = FakeModelProvider([synthesis_json("amber", ["A1"])])
    casefile = amber_casefile()
    result = synthesize(casefile, provider, provider_name="fake", model="canned")
    assert result.verdict.level == "amber"
    assert result.verdict.triggered == ["A1"]
    assert result.metadata.provider == "fake"
    assert result.metadata.model == "canned"
    assert result.metadata.prompt_version == "4"
    assert result.metadata.parse_retries == 0
    assert result.metadata.language_retries == 0
    assert result.verdict_enforcement is None
    call = provider.calls[0]
    assert call.json_schema == build_synthesis_schema(casefile)
    # No checkable claims: the schema pins the assessments array empty.
    assert call.json_schema is not None
    assert call.json_schema["properties"]["assessments"]["maxItems"] == 0
    assert "coldscreen synthesis prompt" in call.system
    assert call.messages[0].role == "user"
    json.loads(call.messages[0].content)  # the input document is valid JSON


def test_schema_for_checkable_claims_enumerates_their_ids() -> None:
    schema = build_synthesis_schema(claims_casefile())
    items = schema["properties"]["assessments"]["items"]
    assert items["properties"]["claim_id"]["enum"] == ["CLM-001", "CLM-003"]
    assert "maxItems" not in schema["properties"]["assessments"]
    # The base schema constant is never mutated by the per-casefile build.
    base_claim_id = SYNTHESIS_SCHEMA["properties"]["assessments"]["items"]["properties"]["claim_id"]
    assert "enum" not in base_claim_id
    assert "maxItems" not in SYNTHESIS_SCHEMA["properties"]["assessments"]


def test_wrong_level_is_corrected_and_noted() -> None:
    provider = FakeModelProvider([synthesis_json("green", ["A1"])])
    result = synthesize(amber_casefile(), provider, provider_name="fake", model="canned")
    assert result.verdict.level == "amber"
    assert result.metadata.model_level == "green"
    assert result.verdict_enforcement is not None
    assert "pure function" in result.verdict_enforcement


def test_unknown_trigger_ids_are_dropped() -> None:
    provider = FakeModelProvider([synthesis_json("amber", ["A1", "Q9"])])
    result = synthesize(amber_casefile(), provider, provider_name="fake", model="canned")
    assert result.verdict.triggered == ["A1"]
    notes = result.metadata.enforcement_notes
    assert "dropped 1 unrecognized trigger id(s)" in notes
    assert "Q9" not in " ".join(notes)  # count only, never the model's text


def test_lowercase_trigger_ids_count_as_their_catalog_ids() -> None:
    provider = FakeModelProvider([synthesis_json("amber", ["a1"])])
    result = synthesize(amber_casefile(), provider, provider_name="fake", model="canned")
    assert result.verdict.triggered == ["A1"]
    # The model DID cite the candidate, so nothing was added or dropped.
    assert result.metadata.enforcement_notes == []


def test_fabricated_red_trigger_is_rejected_with_note() -> None:
    provider = FakeModelProvider([synthesis_json("red", ["R1", "A1"])])
    result = synthesize(amber_casefile(), provider, provider_name="fake", model="canned")
    assert result.verdict.level == "amber"
    assert result.verdict.triggered == ["A1"]
    notes = result.metadata.enforcement_notes
    assert "rejected trigger R1: not supported by the casefile evidence" in notes


def test_missing_mechanical_candidates_are_added() -> None:
    provider = FakeModelProvider([synthesis_json("green", [])])
    result = synthesize(amber_casefile(), provider, provider_name="fake", model="canned")
    assert result.verdict.triggered == ["A1"]
    assert result.verdict.level == "amber"
    assert any("the model omitted" in n for n in result.metadata.enforcement_notes)


# -- parse retries -------------------------------------------------------------------


def test_invalid_json_then_valid_uses_one_parse_retry() -> None:
    provider = FakeModelProvider(["not json at all", synthesis_json("green")])
    result = synthesize(minimal_casefile(), provider, provider_name="fake", model="canned")
    assert result.metadata.parse_retries == 1
    assert len(provider.calls) == 2
    retry_messages = provider.calls[1].messages
    assert retry_messages[-2].role == "assistant"
    assert retry_messages[-2].content == "not json at all"
    assert retry_messages[-1].role == "user"
    assert "invalid JSON" in retry_messages[-1].content


def test_schema_mismatch_counts_as_a_parse_retry() -> None:
    wrong_shape = json.dumps({"narrative": "x", "verdict": {"level": "green"}})
    provider = FakeModelProvider([wrong_shape, synthesis_json("green")])
    result = synthesize(minimal_casefile(), provider, provider_name="fake", model="canned")
    assert result.metadata.parse_retries == 1
    assert "schema mismatch" in provider.calls[1].messages[-1].content


def test_too_few_questions_is_a_schema_mismatch() -> None:
    short = json.dumps(
        {
            "narrative": "x",
            "verdict": {
                "level": "green",
                "triggered": [],
                "rationale": "r",
                "questions": ["only one?"],
            },
        }
    )
    provider = FakeModelProvider([short, synthesis_json("green")])
    result = synthesize(minimal_casefile(), provider, provider_name="fake", model="canned")
    assert result.metadata.parse_retries == 1
    assert len(result.verdict.questions) == 3


def test_always_invalid_output_fails_cleanly_after_three_attempts() -> None:
    provider = FakeModelProvider(["junk one", "junk two", "junk three"])
    with pytest.raises(SynthesisError, match="did not produce valid output after 3 attempts"):
        synthesize(minimal_casefile(), provider, provider_name="fake", model="canned")
    assert len(provider.calls) == 3


def test_failure_attempt_count_includes_language_retries() -> None:
    # One language retry, then three parse failures: four completions total,
    # and the failure message counts every one of them.
    dirty = synthesis_json("green", narrative="A scam, plainly.")
    provider = FakeModelProvider([dirty, "junk one", "junk two", "junk three"])
    with pytest.raises(SynthesisError, match="did not produce valid output after 4 attempts"):
        synthesize(minimal_casefile(), provider, provider_name="fake", model="canned")
    assert len(provider.calls) == 4


def test_markdown_fenced_json_is_accepted() -> None:
    fenced = "```json\n" + synthesis_json("green") + "\n```"
    provider = FakeModelProvider([fenced])
    result = synthesize(minimal_casefile(), provider, provider_name="fake", model="canned")
    assert result.metadata.parse_retries == 0
    assert result.verdict.level == "green"


# -- the language gate ------------------------------------------------------------------


def test_banned_word_triggers_one_corrective_retry_then_succeeds() -> None:
    dirty = synthesis_json("green", narrative="This reads like a fraud dressed up as a filing.")
    clean = synthesis_json("green", narrative="The filings are internally consistent.")
    provider = FakeModelProvider([dirty, clean])
    result = synthesize(minimal_casefile(), provider, provider_name="fake", model="canned")
    assert result.metadata.language_retries == 1
    assert result.narrative == "The filings are internally consistent."
    corrective = provider.calls[1].messages[-1]
    assert corrective.role == "user"
    assert "banned vocabulary" in corrective.content
    assert "fraud" in corrective.content


def test_banned_word_persisting_after_retry_fails_cleanly() -> None:
    dirty = synthesis_json("green", narrative="A scam, plainly.")
    still_dirty = synthesis_json("green", rationale="It remains a scam.")
    provider = FakeModelProvider([dirty, still_dirty])
    with pytest.raises(SynthesisError, match=r"still used 1 banned term\(s\) after a corrective"):
        synthesize(minimal_casefile(), provider, provider_name="fake", model="canned")
    assert len(provider.calls) == 2


def test_language_gate_exhaustion_reports_a_count_and_never_the_terms() -> None:
    """The CLI renders this message into the synthesis-failure memo, where the
    whole-memo backstop would block it if it quoted the vocabulary. Two
    distinct terms across two fields, so the count is of distinct terms."""
    dirty = synthesis_json(
        "green",
        narrative="A scam, plainly.",
        rationale="The conduct looks criminal.",
    )
    provider = FakeModelProvider([dirty, dirty])
    with pytest.raises(SynthesisError) as caught:
        synthesize(minimal_casefile(), provider, provider_name="fake", model="canned")
    message = str(caught.value)
    assert find_banned_terms(message) == []
    assert "2 banned term(s)" in message
    assert "never use accusatory language" in message
    # The corrective retry still names them: the model needs them to comply.
    corrective = provider.calls[1].messages[-1].content
    assert "scam" in corrective
    assert "criminal" in corrective


def test_language_gate_covers_questions_too() -> None:
    dirty = synthesis_json(
        "green",
        questions=["Is this criminal?", "Second question?", "Third question?"],
    )
    clean = synthesis_json("green")
    provider = FakeModelProvider([dirty, clean])
    result = synthesize(minimal_casefile(), provider, provider_name="fake", model="canned")
    assert result.metadata.language_retries == 1


def _status_casefile(status: str) -> CaseFile:
    """A casefile whose only signal is the registered status finding."""
    status_finding = Finding(
        id="REG-001",
        stage="registry",
        severity="amber" if status == "dissolved" else "red",
        confidence="confirmed",
        statement=f"Company status is {status}.",
        evidence=[Evidence(source_url="https://example.invalid/profile", retrieved_at=NOW)],
    )
    casefile = minimal_casefile([status_finding])
    return casefile.model_copy(
        update={
            "subject": CompanyProfile.model_validate(
                {
                    "company_name": "FICTIONAL SUBJECT LTD",
                    "company_number": "99999903",
                    "company_status": status,
                }
            )
        }
    )


def test_dissolved_company_cannot_reach_green_a7_is_forced() -> None:
    """The live-screen regression that motivated rubric 0.2: a dissolved
    company reached GREEN. Now A7 is a mechanical candidate and the floor
    forces it in whatever the model says."""
    provider = FakeModelProvider([synthesis_json("green", [])])
    result = synthesize(
        _status_casefile("dissolved"), provider, provider_name="fake", model="canned"
    )
    assert result.verdict.level == "amber"
    assert result.verdict.triggered == ["A7"]
    assert any("the model omitted" in n for n in result.metadata.enforcement_notes)


def test_administration_status_alone_forces_r2_without_case_detail() -> None:
    """R2 fires from the status leg with the profile as evidence, whether
    or not insolvency case detail was retrieved."""
    provider = FakeModelProvider([synthesis_json("green", [])])
    result = synthesize(
        _status_casefile("administration"), provider, provider_name="fake", model="canned"
    )
    assert result.verdict.level == "red"
    assert result.verdict.triggered == ["R2"]


def test_prose_may_spell_out_registry_identity_names_verbatim() -> None:
    """The registry identity exemption at the per-field gate: the model can
    name a banned-word company and officer exactly as the register spells
    them, with no corrective retry."""
    from coldscreen.models import Officer

    casefile = minimal_casefile().model_copy(
        update={
            "subject": CompanyProfile.model_validate(
                {"company_name": "TOTAL SHAM TRADING LTD", "company_number": "99999905"}
            ),
            "officers": [Officer.model_validate({"name": "CROOK, Cuthbert"})],
        }
    )
    clean = synthesis_json(
        "green",
        narrative=(
            "TOTAL SHAM TRADING LTD is an active company; its sole director"
            " CROOK, Cuthbert was appointed at incorporation."
        ),
    )
    provider = FakeModelProvider([clean])
    result = synthesize(casefile, provider, provider_name="fake", model="canned")
    assert result.metadata.language_retries == 0
    assert "TOTAL SHAM TRADING LTD" in result.narrative


def test_identity_exemption_never_covers_prose_outside_the_exact_name() -> None:
    """The same words outside the registered spelling still fail the gate:
    the exemption is the exact span, not the vocabulary."""
    casefile = minimal_casefile().model_copy(
        update={
            "subject": CompanyProfile.model_validate(
                {"company_name": "TOTAL SHAM TRADING LTD", "company_number": "99999905"}
            ),
        }
    )
    dirty = synthesis_json(
        "green",
        narrative="TOTAL SHAM TRADING LTD looks like a sham to me.",
    )
    provider = FakeModelProvider([dirty, dirty])
    with pytest.raises(SynthesisError, match=r"still used 1 banned term\(s\)"):
        synthesize(casefile, provider, provider_name="fake", model="canned")


# -- the stage-honesty gate ---------------------------------------------------------


def test_clean_claim_about_a_not_run_stage_triggers_one_corrective_retry() -> None:
    """minimal_casefile has no sanctions block at all: the stage never ran,
    so 'no sanctions matches' is a lie the gate catches mechanically."""
    dishonest = synthesis_json(
        "green", narrative="No sanctions matches were found for any subject."
    )
    honest = synthesis_json(
        "green", narrative="Sanctions screening was not performed this run (SAN-000)."
    )
    provider = FakeModelProvider([dishonest, honest])
    result = synthesize(minimal_casefile(), provider, provider_name="fake", model="canned")
    assert result.metadata.language_retries == 1
    corrective = provider.calls[1].messages[-1]
    assert corrective.role == "user"
    assert "not run or failed" in corrective.content
    assert "sanctions" in corrective.content
    assert "never a result" in corrective.content


def test_persistent_clean_claim_fails_closed_with_a_sanitized_message() -> None:
    dishonest = synthesis_json("green", narrative="Sanctions screening was clean.")
    provider = FakeModelProvider([dishonest, dishonest])
    with pytest.raises(SynthesisError) as caught:
        synthesize(minimal_casefile(), provider, provider_name="fake", model="canned")
    message = str(caught.value)
    assert "described the sanctions stage(s) as clean" in message
    assert "not run or failed" in message
    # Sanitized: no model text, no banned vocabulary; safe for the failure memo.
    assert "No sanctions matches" not in message
    assert find_banned_terms(message) == []
    assert len(provider.calls) == 2


def test_clean_claim_about_a_failed_media_stage_is_caught() -> None:
    failed_media = minimal_casefile().model_copy(
        update={
            "media": MediaScreening(
                performed=False,
                failed=True,
                skipped_reason="the adverse media search stage failed after retries",
            )
        }
    )
    dishonest = synthesis_json("green", rationale="There is no adverse media on this company.")
    provider = FakeModelProvider([dishonest, dishonest])
    with pytest.raises(SynthesisError, match=r"described the adverse media stage\(s\) as clean"):
        synthesize(failed_media, provider, provider_name="fake", model="canned")


def test_clean_claims_about_stages_that_ran_are_not_gated() -> None:
    """The gate arms only for not-run or failed stages: a stage that RAN
    and found nothing may honestly say so."""
    from coldscreen.models import SanctionsScreening

    ran_clean = minimal_casefile().model_copy(
        update={
            "sanctions": SanctionsScreening(performed=True, threshold=0.7),
            "media": MediaScreening(performed=True),
        }
    )
    honest = synthesis_json(
        "green",
        narrative=(
            "Sanctions screening returned no candidates; there is no adverse"
            " media in any query category."
        ),
    )
    provider = FakeModelProvider([honest])
    result = synthesize(ran_clean, provider, provider_name="fake", model="canned")
    assert result.metadata.language_retries == 0


def test_honest_absence_statements_pass_the_gate() -> None:
    """The canonical honest sentence about skipped stages (the golden
    narrative's shape) must not trip the fixed phrase set."""
    honest = synthesis_json(
        "green",
        narrative=(
            "Sanctions screening and the adverse media search were not"
            " performed this run (SAN-000, MED-000), so those absences are"
            " open questions rather than clean results."
        ),
    )
    provider = FakeModelProvider([honest])
    result = synthesize(minimal_casefile(), provider, provider_name="fake", model="canned")
    assert result.metadata.language_retries == 0


def test_both_stages_are_named_when_both_are_misdescribed() -> None:
    dishonest = synthesis_json(
        "green",
        narrative="No sanctions matches and no adverse media were found.",
    )
    provider = FakeModelProvider([dishonest, dishonest])
    with pytest.raises(SynthesisError, match="sanctions and adverse media stage"):
        synthesize(minimal_casefile(), provider, provider_name="fake", model="canned")


# -- assessments: happy path through synthesize -----------------------------------


def test_assessments_resolve_basis_and_unlock_r4() -> None:
    casefile = claims_casefile()
    response = synthesis_json(
        "red",
        ["R4", "A4"],
        rationale="R4: CLM-001 is contradicted by REG-002. A4: CLM-003 is unverified.",
        assessments=[
            assessment_json(
                "CLM-001",
                "contradicted",
                ["REG-002"],
                "Incorporated on 2019-05-14 per the registry profile.",
            ),
            assessment_json("CLM-003", "unverified", [], "No public headcount source."),
        ],
    )
    provider = FakeModelProvider([response])
    result = synthesize(casefile, provider, provider_name="fake", model="canned")
    assert result.verdict.level == "red"
    assert result.verdict.triggered == ["R4", "A4"]
    assert result.metadata.enforcement_notes == []
    assert [a.claim_id for a in result.assessments] == ["CLM-001", "CLM-003"]
    contradicted = result.assessments[0]
    # The basis evidence was COPIED from the cited finding, not minted.
    assert contradicted.status == "contradicted"
    assert [e.source_url for e in contradicted.basis] == ["https://example.invalid/profile"]
    assert result.assessments[1].basis == []


def test_uncheckable_claims_get_no_assessment() -> None:
    casefile = claims_casefile()
    response = synthesis_json(
        "green",
        [],
        assessments=[
            assessment_json("CLM-001", "supported", ["REG-002"], "Matches the registry."),
            assessment_json("CLM-003", "supported", ["REG-002"], "Matches the registry."),
        ],
    )
    provider = FakeModelProvider([response])
    result = synthesize(casefile, provider, provider_name="fake", model="canned")
    assessed_ids = {a.claim_id for a in result.assessments}
    assert "CLM-002" not in assessed_ids  # puffery is never assessed
    assert assessed_ids == {"CLM-001", "CLM-003"}


# -- assessment enforcement unit surface ------------------------------------------


def _raw(
    claim_id: str,
    status: str = "unverified",
    basis: list[str] | None = None,
    record_note: str = "One factual sentence.",
) -> _RawAssessment:
    return _RawAssessment(
        claim_id=claim_id,
        status=status,  # type: ignore[arg-type]
        basis_finding_ids=basis or [],
        record_note=record_note,
    )


def test_contradicted_without_resolvable_basis_is_downgraded() -> None:
    casefile = claims_casefile()
    outcome = enforce_assessments([_raw("CLM-001", "contradicted", ["ZZZ-999"])], casefile)
    downgraded = outcome.assessments[0]
    assert downgraded.status == "unverified"
    assert downgraded.basis == []
    assert any(
        "downgraded the assessment for CLM-001 from contradicted to unverified" in note
        for note in outcome.notes
    )
    assert any("cited 1 finding id(s) not present" in note for note in outcome.notes)


def test_supported_with_empty_basis_is_downgraded() -> None:
    casefile = claims_casefile()
    outcome = enforce_assessments([_raw("CLM-001", "supported", [])], casefile)
    assert outcome.assessments[0].status == "unverified"
    assert any("from supported to unverified" in note for note in outcome.notes)


def test_unknown_and_uncheckable_claim_ids_are_dropped_with_a_count() -> None:
    casefile = claims_casefile()
    hostile = "CLM-666 [see](https://evil.example/fraud)"
    outcome = enforce_assessments(
        [
            _raw(hostile, "contradicted", ["REG-002"]),
            _raw("CLM-002", "supported", ["REG-002"]),  # puffery: not assessable
            _raw("CLM-001", "unverified"),
            _raw("CLM-003", "unverified"),
        ],
        casefile,
    )
    assert [a.claim_id for a in outcome.assessments] == ["CLM-001", "CLM-003"]
    note_text = " ".join(outcome.notes)
    assert "dropped 2 assessment(s) citing ids that are not checkable claims" in note_text
    # Model-controlled text never reaches a note.
    assert "evil.example" not in note_text
    assert "CLM-666" not in note_text


def test_duplicate_assessments_keep_the_first() -> None:
    casefile = claims_casefile()
    outcome = enforce_assessments(
        [
            _raw("CLM-001", "supported", ["REG-002"], "First wins."),
            _raw("CLM-001", "contradicted", ["REG-002"], "Second is dropped."),
            _raw("CLM-003", "unverified"),
        ],
        casefile,
    )
    assert outcome.assessments[0].record_note == "First wins."
    assert outcome.assessments[0].status == "supported"
    assert "dropped 1 duplicate assessment(s)" in outcome.notes


def test_missing_assessments_are_added_as_unverified_with_fixed_text() -> None:
    casefile = claims_casefile()
    outcome = enforce_assessments([], casefile)
    assert [a.claim_id for a in outcome.assessments] == ["CLM-001", "CLM-003"]
    assert all(a.status == "unverified" for a in outcome.assessments)
    assert all(a.record_note == MISSING_ASSESSMENT_NOTE for a in outcome.assessments)
    assert all(a.model_assessed is False for a in outcome.assessments)
    assert any(
        "added unverified assessment(s) the model omitted for: CLM-001, CLM-003" in note
        for note in outcome.notes
    )


def test_record_notes_are_collapsed_to_one_line() -> None:
    casefile = claims_casefile()
    outcome = enforce_assessments(
        [_raw("CLM-001", "unverified", [], "Two\nlines   here."), _raw("CLM-003")], casefile
    )
    assert outcome.assessments[0].record_note == "Two lines here."


def test_gate_signals_derive_from_enforced_state_only() -> None:
    casefile = claims_casefile()
    # A contradicted assessment whose basis did NOT resolve was downgraded:
    # it opens nothing for R4, but it IS a model-authored unverified row
    # (the model judged; its evidence failed), so A4 stays reachable.
    outcome = enforce_assessments([_raw("CLM-001", "contradicted", ["NOPE-1"])], casefile)
    signals = gate_signals(casefile, outcome.assessments)
    assert signals.checkable_history_present is True  # CLM-001 is history
    assert signals.checkable_financials_present is False
    assert signals.charges_present is False
    assert signals.claims_material_present is True
    assert signals.contradicted_with_relevant_basis is False
    assert signals.unverified_present is True
    assert signals.overlaps_present is False
    assert signals.media_items_present is False

    resolved = enforce_assessments(
        [
            _raw("CLM-001", "contradicted", ["REG-002"]),
            _raw("CLM-003", "supported", ["REG-002"]),
        ],
        casefile,
    )
    signals = gate_signals(casefile, resolved.assessments)
    assert signals.contradicted_with_relevant_basis is True
    assert signals.unverified_present is False


def test_gate_signals_charges_and_media_states() -> None:
    """charges_present tracks the register; media_items_present opens only
    on ran-with-items (the not-run and ran-empty states are both False)."""
    casefile = claims_casefile()
    assert gate_signals(casefile, []).charges_present is False
    with_charge = casefile.model_copy(update={"charges": [Charge(status="outstanding")]})
    assert gate_signals(with_charge, []).charges_present is True

    assert gate_signals(casefile, []).media_items_present is False  # media None
    not_run = casefile.model_copy(update={"media": MediaScreening(performed=False)})
    assert gate_signals(not_run, []).media_items_present is False
    ran_empty = casefile.model_copy(update={"media": MediaScreening(performed=True)})
    assert gate_signals(ran_empty, []).media_items_present is False
    ran_with_items = casefile.model_copy(
        update={
            "media": MediaScreening(
                performed=True,
                items=[
                    MediaItem(
                        title="A fictional headline",
                        url="https://fictional-gazette.example/story",
                        source_domain="fictional-gazette.example",
                        query_category="misconduct",
                    )
                ],
            )
        }
    )
    assert gate_signals(ran_with_items, []).media_items_present is True


def test_a6_gate_matrix_through_synthesize() -> None:
    """The A6 matrix end to end: a model citing A6 is accepted only when
    the media stage ran and returned at least one item."""
    base = claims_casefile()
    item = MediaItem(
        title="A fictional headline",
        url="https://fictional-gazette.example/story",
        source_domain="fictional-gazette.example",
        query_category="misconduct",
    )
    states = {
        "not_run": (MediaScreening(performed=False, skipped_reason="no key"), False),
        "ran_empty": (MediaScreening(performed=True), False),
        "ran_with_items": (MediaScreening(performed=True, items=[item]), True),
    }
    clean_assessments = [
        assessment_json("CLM-001", "unverified", [], "No source."),
        assessment_json("CLM-003", "unverified", [], "No source."),
    ]
    for label, (media, expect_accepted) in states.items():
        casefile = base.model_copy(update={"media": media})
        provider = FakeModelProvider(
            [synthesis_json("amber", ["A6"], assessments=clean_assessments)]
        )
        result = synthesize(casefile, provider, provider_name="fake", model=label)
        if expect_accepted:
            assert "A6" in result.verdict.triggered, label
        else:
            assert "A6" not in result.verdict.triggered, label
            notes = " ".join(result.metadata.enforcement_notes)
            assert "rejected trigger A6" in notes, label


def test_backfilled_unverified_rows_never_open_a4() -> None:
    """Review fix F2: the model returned nothing, code back-filled both
    checkable claims as unverified. The table is complete, the A4 gate
    stays shut: silence is not "central claim unverifiable" support."""
    casefile = claims_casefile()
    outcome = enforce_assessments([], casefile)
    assert all(a.model_assessed is False for a in outcome.assessments)
    signals = gate_signals(casefile, outcome.assessments)
    assert signals.unverified_present is False

    # One model-authored unverified row is enough to open it.
    partial = enforce_assessments([_raw("CLM-001", "unverified")], casefile)
    assert partial.assessments[0].model_assessed is True
    assert partial.assessments[1].model_assessed is False  # back-filled
    signals = gate_signals(casefile, partial.assessments)
    assert signals.unverified_present is True


def test_puffery_only_claims_do_not_open_a3_a5() -> None:
    """Review fix F3: a slogan-only deck (no checkable claims) leaves the
    per-category checkable signals False; claims material is still present
    for the R5 disclosure judgment."""
    casefile = claims_casefile()
    puffery_only = [c.model_copy(update={"checkable": False}) for c in casefile.claims]
    slogan_casefile = casefile.model_copy(update={"claims": puffery_only})
    signals = gate_signals(slogan_casefile, [])
    assert signals.checkable_history_present is False
    assert signals.checkable_financials_present is False
    assert signals.claims_material_present is True


def test_contradiction_outside_the_relevance_table_is_downgraded() -> None:
    """A team claim contradicted on the incorporation-date finding: the
    evidence resolves, but REG-002 is not in the team category's relevance
    set, so the contradiction is downgraded with a sanitized note and R4
    falls to its gate."""
    casefile = claims_casefile()
    response = synthesis_json(
        "red",
        ["R4"],
        assessments=[
            assessment_json("CLM-001", "unverified", [], "No source."),
            assessment_json("CLM-003", "contradicted", ["REG-002"], "Wrong-category basis."),
        ],
    )
    provider = FakeModelProvider([response])
    result = synthesize(casefile, provider, provider_name="fake", model="canned")
    assert result.verdict.level == "green"
    assert result.verdict.triggered == []
    by_id = {a.claim_id: a for a in result.assessments}
    assert by_id["CLM-003"].status == "unverified"
    assert by_id["CLM-003"].basis == []
    notes = " ".join(result.metadata.enforcement_notes)
    assert (
        "downgraded the assessment for CLM-003 from contradicted to unverified:"
        " its basis cites no registry finding relevant to a claim in the team"
        " category" in notes
    )
    assert "rejected trigger R4" in notes


def test_relevant_contradiction_survives_per_category() -> None:
    """The same finding id can be relevant to one category and not another:
    REG-002 grounds a history contradiction and survives."""
    casefile = claims_casefile()
    outcome = enforce_assessments([_raw("CLM-001", "contradicted", ["REG-002"])], casefile)
    survivor = outcome.assessments[0]
    assert survivor.status == "contradicted"
    assert survivor.basis
    assert not any("downgraded" in note for note in outcome.notes)


def test_fabricated_r4_without_surviving_contradiction_is_rejected() -> None:
    """The model cites R4 while its only contradiction cites unresolvable
    evidence: the assessment is downgraded and R4 falls to the gate."""
    casefile = claims_casefile()
    response = synthesis_json(
        "red",
        ["R4"],
        assessments=[
            assessment_json("CLM-001", "contradicted", ["FAKE-001"], "Note one."),
            assessment_json("CLM-003", "unverified", [], "Note two."),
        ],
    )
    provider = FakeModelProvider([response])
    result = synthesize(casefile, provider, provider_name="fake", model="canned")
    assert result.verdict.level == "green"
    assert result.verdict.triggered == []
    notes = " ".join(result.metadata.enforcement_notes)
    assert "downgraded the assessment for CLM-001" in notes
    assert "rejected trigger R4" in notes


# -- the language gate over record notes and the quoted-data exemption -------------


def test_record_note_reusing_a_claim_substring_fails_the_gate() -> None:
    """Review attack shape 3: a record_note that embeds a claim substring
    cannot launder its vocabulary; record notes have no exemptions."""
    casefile = claims_casefile()
    dirty = synthesis_json(
        "green",
        [],
        assessments=[
            assessment_json(
                "CLM-001", "unverified", [], "It claims it eliminates fraud in widget procurement."
            ),
            assessment_json("CLM-003", "unverified", [], "Fine note."),
        ],
    )
    provider = FakeModelProvider([dirty, dirty])
    with pytest.raises(SynthesisError, match=r"still used 1 banned term\(s\)"):
        synthesize(casefile, provider, provider_name="fake", model="canned")


def test_record_note_with_banned_word_fails_the_gate() -> None:
    casefile = claims_casefile()
    dirty = synthesis_json(
        "green",
        [],
        assessments=[
            assessment_json("CLM-001", "unverified", [], "This smells like fraud to me."),
            assessment_json("CLM-003", "unverified", [], "Fine note."),
        ],
    )
    clean = synthesis_json(
        "green",
        [],
        assessments=[
            assessment_json("CLM-001", "unverified", [], "No source speaks to this."),
            assessment_json("CLM-003", "unverified", [], "Fine note."),
        ],
    )
    provider = FakeModelProvider([dirty, clean])
    result = synthesize(casefile, provider, provider_name="fake", model="canned")
    assert result.metadata.language_retries == 1
    corrective = provider.calls[1].messages[-1]
    assert "fraud" in corrective.content


def test_narrative_quoting_a_claim_text_fails_the_field_gate() -> None:
    """Review fix F1a: model prose has ZERO exemptions. Repeating a stored
    claim's wording in the narrative fails even though the memo's claims
    table renders the identical words legitimately."""
    casefile = claims_casefile()
    quoting = synthesis_json(
        "green",
        [],
        narrative=(
            'The deck states "Our platform eliminates fraud in widget procurement"'
            " (CLM-002), which is not a checkable assertion."
        ),
        assessments=[
            assessment_json("CLM-001", "unverified", [], "No source."),
            assessment_json("CLM-003", "unverified", [], "No source."),
        ],
    )
    clean = synthesis_json(
        "green",
        [],
        narrative="The deck's slogan (CLM-002) is not a checkable assertion.",
        assessments=[
            assessment_json("CLM-001", "unverified", [], "No source."),
            assessment_json("CLM-003", "unverified", [], "No source."),
        ],
    )
    provider = FakeModelProvider([quoting, clean])
    result = synthesize(casefile, provider, provider_name="fake", model="canned")
    assert result.metadata.language_retries == 1
    corrective = provider.calls[1].messages[-1]
    assert "never repeat a claim's wording" in corrective.content
    assert "CLM-002" in result.narrative


def test_quoting_persisting_after_the_retry_fails_closed() -> None:
    casefile = claims_casefile()
    quoting = synthesis_json(
        "green",
        [],
        narrative='The deck says "Our platform eliminates fraud in widget procurement".',
        assessments=[
            assessment_json("CLM-001", "unverified", [], "No source."),
            assessment_json("CLM-003", "unverified", [], "No source."),
        ],
    )
    provider = FakeModelProvider([quoting, quoting])
    with pytest.raises(SynthesisError, match=r"still used 1 banned term\(s\)"):
        synthesize(casefile, provider, provider_name="fake", model="canned")


# -- apply_synthesis ------------------------------------------------------------------


def test_apply_synthesis_updates_the_casefile() -> None:
    provider = FakeModelProvider([synthesis_json("green", ["A1"])])
    casefile = amber_casefile()
    result = synthesize(casefile, provider, provider_name="fake", model="canned")
    updated = apply_synthesis(casefile, result)
    assert updated.verdict is not None
    assert updated.verdict.level == "amber"
    assert updated.narrative is not None
    assert updated.synthesis is not None
    assert updated.synthesis.provider == "fake"
    # The original is untouched: apply returns a copy.
    assert casefile.verdict is None


def test_apply_synthesis_stores_the_enforced_assessments() -> None:
    casefile = claims_casefile()
    response = synthesis_json(
        "green",
        [],
        assessments=[
            assessment_json("CLM-001", "supported", ["REG-002"], "Matches the registry."),
        ],
    )
    provider = FakeModelProvider([response])
    result = synthesize(casefile, provider, provider_name="fake", model="canned")
    updated = apply_synthesis(casefile, result)
    assert [a.claim_id for a in updated.assessments] == ["CLM-001", "CLM-003"]
    # CLM-003 was omitted by the model, so code added the unverified row.
    assert updated.assessments[1].record_note == MISSING_ASSESSMENT_NOTE
    assert casefile.assessments == []
