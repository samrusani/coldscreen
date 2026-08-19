"""The reviewer's four attack shapes against the quoted-data exemption,
executed through the real CLI, plus the properties that now stop them.

Shapes (from the weekend 3 adversarial review):
1. A fabricated accusatory "claim" never present in the deck.
2. A single-word claim ("fraud") used as a memo-wide exemption.
3. A record_note that reuses a claim substring to launder vocabulary.
4. A hostile phrase planted as a claim and repeated verbatim in the
   narrative.

Defenses under test: quotation verification before storage (F1b), zero
exemptions on model prose fields (F1a), match-length span advancement (F4),
and check_language.py re-verification against sibling evidence. Every test
asserts the run fails closed or the hostile text never renders anywhere in
the case directory.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import respx
from typer.testing import CliRunner

from firstpass.cli import app
from firstpass.language import find_banned_terms
from firstpass.models import CaseFile

from .conftest import FIXTURES_DIR, mock_company_routes
from .fakes import assessment_json, claim_json, claims_json, synthesis_json

DECK_PATH = FIXTURES_DIR / "deck_fabricated_widgets.pdf"
OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "check_language.py"

runner = CliRunner()

# Genuine deck quotations (verbatim from the fixture deck) for the happy
# parts of each attack scenario.
GENUINE = claim_json("Operating since 2015 with a national footprint", "deck p.2", "history", True)
GENUINE_PUFFERY = claim_json(
    "Our platform eliminates fraud in widget procurement", "deck p.2", "other", False
)

CLEAN_SYNTHESIS = synthesis_json(
    "red",
    ["R4", "A1", "A2"],
    narrative="The registry contradicts the deck's history claim (CLM-001, REG-002).",
    rationale="R4: CLM-001 against REG-002. A1: REG-003. A2: REG-009.",
    assessments=[
        assessment_json(
            "CLM-001",
            "contradicted",
            ["REG-002"],
            "Incorporated on 2019-05-14 per the registry profile.",
        )
    ],
)


def load_check_language() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_language", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def attack_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "fixture-key-0123456789-not-a-real-key")
    monkeypatch.setenv("FIRSTPASS_SCREENED_AT", "2026-08-18T12:00:00+00:00")
    monkeypatch.setenv("FIRSTPASS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("FIRSTPASS_MODEL", "ollama:fake-model:1b")
    return tmp_path


def ollama_reply(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "fake-model:1b",
            "message": {"role": "assistant", "content": content},
            "done": True,
        },
    )


def case_dir(env: Path) -> Path:
    return env / "cases" / "fabricated-widgets-ltd-99999999"


def all_case_bytes(env: Path) -> bytes:
    blobs = []
    for path in sorted(case_dir(env).rglob("*")):
        if path.is_file():
            blobs.append(path.read_bytes())
    assert blobs, "no case files were written"
    return b"\n".join(blobs)


# -- attack 1: fabricated claim ---------------------------------------------------


def test_attack_1_fabricated_claim_never_renders(
    attack_env: Path, respx_mock: respx.MockRouter
) -> None:
    """A claim the deck never said, with accusatory wording, is dropped by
    quotation verification: not stored, not rendered, counted honestly."""
    mock_company_routes(respx_mock)
    hostile = "The previous owners ran a criminal scam from this address"
    respx_mock.post(OLLAMA_CHAT_URL).mock(
        side_effect=[
            ollama_reply(
                claims_json([GENUINE, claim_json(hostile, "deck p.2", "regulatory", True)])
            ),
            ollama_reply(CLEAN_SYNTHESIS),
        ]
    )
    result = runner.invoke(app, ["screen", "99999999", "--deck", str(DECK_PATH)])
    assert result.exit_code == 0, result.output

    casefile = CaseFile.model_validate_json(
        (case_dir(attack_env) / "casefile.json").read_text(encoding="utf-8")
    )
    assert [c.text for c in casefile.claims] == ["Operating since 2015 with a national footprint"]
    assert casefile.claims_extraction is not None
    assert casefile.claims_extraction.dropped_claims == 1
    assert any(
        f.id == "EXT-007" and "1 claim(s) were dropped" in f.statement for f in casefile.findings
    )
    # The hostile text reaches NO file in the audit pack.
    assert hostile.encode() not in all_case_bytes(attack_env)
    memo = (case_dir(attack_env) / "memo.md").read_text(encoding="utf-8")
    assert find_banned_terms(memo) == []
    # And the CI scan agrees.
    assert load_check_language().main([str(case_dir(attack_env) / "memo.md")]) == 0


# -- attack 2: single-word claim as a memo-wide exemption ---------------------------


def test_attack_2_single_word_claim_cannot_whitelist_prose(
    attack_env: Path, respx_mock: respx.MockRouter
) -> None:
    """ "fraud" IS a word the deck contains, so a single-word claim survives
    quotation verification. It still buys the model nothing: prose fields
    are gated with zero exemptions, so the run fails closed."""
    mock_company_routes(respx_mock)
    dirty = synthesis_json(
        "red",
        ["A1", "A2"],
        narrative="This company is committing fraud, plainly.",
        assessments=[assessment_json("CLM-001", "unverified", [], "No source speaks to this.")],
    )
    route = respx_mock.post(OLLAMA_CHAT_URL).mock(
        side_effect=[
            ollama_reply(claims_json([claim_json("fraud", "deck p.2", "other", True)])),
            ollama_reply(dirty),
            ollama_reply(dirty),  # the corrective retry stays dirty
        ]
    )
    result = runner.invoke(app, ["screen", "99999999", "--deck", str(DECK_PATH)])
    assert result.exit_code == 1
    assert route.call_count == 3  # extraction, synthesis, one language retry
    combined = result.output
    assert "banned term(s)" in combined

    # The audit pack survives; the failure memo carries no verdict and no
    # narrative, and the single stored claim renders only in the table.
    casefile = CaseFile.model_validate_json(
        (case_dir(attack_env) / "casefile.json").read_text(encoding="utf-8")
    )
    assert casefile.verdict is None
    assert casefile.narrative is None
    assert [c.text for c in casefile.claims] == ["fraud"]
    memo = (case_dir(attack_env) / "memo.md").read_text(encoding="utf-8")
    assert "committing fraud" not in memo
    assert "Synthesis was attempted and failed" in memo
    # Whole-memo scan with the stored claim as exemption: the only hit-free
    # reading is the table cell; nothing else in the memo says the word.
    assert find_banned_terms(memo, ("fraud",)) == []
    assert memo.count("fraud") == 1  # exactly the quoted table cell


# -- attack 3: record_note reuses a claim substring ---------------------------------


def test_attack_3_record_note_reuse_fails_closed(
    attack_env: Path, respx_mock: respx.MockRouter
) -> None:
    mock_company_routes(respx_mock)
    laundering = synthesis_json(
        "red",
        ["A1", "A2"],
        assessments=[
            assessment_json(
                "CLM-001",
                "unverified",
                [],
                "Per the deck it eliminates fraud in widget procurement.",
            )
        ],
    )
    route = respx_mock.post(OLLAMA_CHAT_URL).mock(
        side_effect=[
            ollama_reply(claims_json([GENUINE, GENUINE_PUFFERY])),
            ollama_reply(laundering),
            ollama_reply(laundering),
        ]
    )
    result = runner.invoke(app, ["screen", "99999999", "--deck", str(DECK_PATH)])
    assert result.exit_code == 1
    assert route.call_count == 3
    casefile = CaseFile.model_validate_json(
        (case_dir(attack_env) / "casefile.json").read_text(encoding="utf-8")
    )
    assert casefile.assessments == []  # the laundering note was never stored
    memo = (case_dir(attack_env) / "memo.md").read_text(encoding="utf-8")
    assert "Per the deck it eliminates" not in memo
    # The genuine puffery quote still renders: the positive control inside
    # the attack test.
    assert "Our platform eliminates fraud in widget procurement" in memo
    assert find_banned_terms(memo, ("Our platform eliminates fraud in widget procurement",)) == []


# -- attack 4: planted phrase reused verbatim in the narrative -----------------------


def test_attack_4_planted_phrase_reused_in_narrative_fails_closed(
    attack_env: Path, respx_mock: respx.MockRouter
) -> None:
    """The phrase is planted as a claim (dropped: not in the deck) and then
    repeated verbatim in the narrative. Prose has zero exemptions and the
    phrase was never stored, so the run fails closed and no written file
    carries it."""
    mock_company_routes(respx_mock)
    planted = "A dishonest sham built on widget crime"
    dirty = synthesis_json(
        "red",
        ["A1", "A2"],
        narrative=f'The deck admits it: "{planted}".',
        assessments=[assessment_json("CLM-001", "unverified", [], "No source.")],
    )
    route = respx_mock.post(OLLAMA_CHAT_URL).mock(
        side_effect=[
            ollama_reply(claims_json([GENUINE, claim_json(planted, "deck p.3", "other", True)])),
            ollama_reply(dirty),
            ollama_reply(dirty),
        ]
    )
    result = runner.invoke(app, ["screen", "99999999", "--deck", str(DECK_PATH)])
    assert result.exit_code == 1
    assert route.call_count == 3
    casefile = CaseFile.model_validate_json(
        (case_dir(attack_env) / "casefile.json").read_text(encoding="utf-8")
    )
    assert [c.text for c in casefile.claims] == ["Operating since 2015 with a national footprint"]
    assert casefile.claims_extraction is not None
    assert casefile.claims_extraction.dropped_claims == 1
    # The planted phrase reaches NO file: not the casefile, not the memo,
    # not the evidence index.
    assert planted.encode() not in all_case_bytes(attack_env)
    memo = (case_dir(attack_env) / "memo.md").read_text(encoding="utf-8")
    assert find_banned_terms(memo) == []
    assert load_check_language().main([str(case_dir(attack_env) / "memo.md")]) == 0
