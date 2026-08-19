"""Stage 7: input assembly, prompt, parse retries, language gate, enforcement."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from firstpass.casedir import load_casefile
from firstpass.models import CaseFile, CompanyProfile, Evidence, Finding
from firstpass.rubric import detect_candidates
from firstpass.synthesis import (
    SYNTHESIS_SCHEMA,
    SynthesisError,
    apply_synthesis,
    build_synthesis_input,
    load_prompt,
    prompt_version,
    serialize_input,
    synthesize,
)

from .conftest import FIXTURES_DIR
from .fakes import FakeModelProvider, synthesis_json

NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
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


# -- prompt ---------------------------------------------------------------------


def test_prompt_loads_and_carries_version_1() -> None:
    prompt = load_prompt()
    assert prompt_version(prompt) == "1"


def test_prompt_contains_the_load_bearing_rules() -> None:
    prompt = load_prompt()
    assert "If something is not in that document, it does not exist" in prompt
    # Banned vocabulary is listed explicitly.
    for term in ("fraud", "criminal", "dishonest", "con artist"):
        assert term in prompt
    # Every rubric trigger id is defined in the prompt.
    for trigger_id in ("R1", "R2", "R3", "R4", "R5", "A1", "A2", "A3", "A4", "A5", "A6"):
        assert trigger_id in prompt
    assert "Any RED trigger forces a RED verdict" in prompt


def test_prompt_version_marker_is_required() -> None:
    with pytest.raises(SynthesisError, match="version marker"):
        prompt_version("a prompt without the marker")


# -- input assembly ----------------------------------------------------------------


def test_synthesis_input_is_deterministic_and_sorted() -> None:
    casefile = load_casefile(GOLDEN_DIR)
    candidates = detect_candidates(casefile)
    first = serialize_input(build_synthesis_input(casefile, candidates))
    second = serialize_input(build_synthesis_input(casefile, candidates))
    assert first == second
    # Sorted keys at every level: re-dumping the parsed document with
    # sort_keys must be a fixed point.
    parsed = json.loads(first)
    assert first == json.dumps(parsed, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def test_synthesis_input_carries_findings_rubric_candidates_and_media_titles() -> None:
    casefile = load_casefile(GOLDEN_DIR)
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


# -- happy path and enforcement wiring ----------------------------------------------


def test_happy_path_records_metadata_and_uses_the_schema() -> None:
    provider = FakeModelProvider([synthesis_json("amber", ["A1"])])
    result = synthesize(amber_casefile(), provider, provider_name="fake", model="canned")
    assert result.verdict.level == "amber"
    assert result.verdict.triggered == ["A1"]
    assert result.metadata.provider == "fake"
    assert result.metadata.model == "canned"
    assert result.metadata.prompt_version == "1"
    assert result.metadata.parse_retries == 0
    assert result.metadata.language_retries == 0
    assert result.verdict_enforcement is None
    call = provider.calls[0]
    assert call.json_schema == SYNTHESIS_SCHEMA
    assert "firstpass synthesis prompt" in call.system
    assert call.messages[0].role == "user"
    json.loads(call.messages[0].content)  # the input document is valid JSON


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
    with pytest.raises(SynthesisError, match="banned"):
        synthesize(minimal_casefile(), provider, provider_name="fake", model="canned")
    assert len(provider.calls) == 2


def test_language_gate_covers_questions_too() -> None:
    dirty = synthesis_json(
        "green",
        questions=["Is this criminal?", "Second question?", "Third question?"],
    )
    clean = synthesis_json("green")
    provider = FakeModelProvider([dirty, clean])
    result = synthesize(minimal_casefile(), provider, provider_name="fake", model="canned")
    assert result.metadata.language_retries == 1


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
