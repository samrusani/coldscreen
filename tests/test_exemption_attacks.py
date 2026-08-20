"""The reviewer's four attack shapes against the quoted-data exemption,
executed through the real CLI, plus the properties that now stop them.

Shapes (from the weekend 3 adversarial review):
1. A fabricated accusatory "claim" never present in the deck.
2. A single-word claim ("fraud") used as an exemption primitive.
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

import json
import shutil
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import respx
from typer.testing import CliRunner, Result

from coldscreen.cli import app
from coldscreen.language import find_banned_terms
from coldscreen.models import CaseFile
from coldscreen.pipeline import language_backstop_failure
from coldscreen.render import render_memo

from .conftest import FIXTURES_DIR, mock_company_routes
from .fakes import assessment_json, claim_json, claims_json, synthesis_json

DECK_PATH = FIXTURES_DIR / "deck_fabricated_widgets.pdf"
GOLDEN_DIR = FIXTURES_DIR / "golden"
OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
PUFFERY_QUOTE = "Our platform eliminates fraud in widget procurement"
PLANTED_TWO_TOKEN = "a fraud"

runner = CliRunner()


def all_output(result: Result) -> str:
    """stdout plus stderr, tolerant of click versions that separate them."""
    text = result.output
    try:
        text += result.stderr
    except (ValueError, AttributeError):
        pass
    return text


def _write_casefile(case_dir: Path, casefile: CaseFile) -> None:
    (case_dir / "casefile.json").write_text(
        casefile.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )


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
    from coldscreen import check_language

    return check_language


@pytest.fixture
def attack_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "fixture-key-0123456789-not-a-real-key")
    monkeypatch.setenv("COLDSCREEN_SCREENED_AT", "2026-08-18T12:00:00+00:00")
    monkeypatch.setenv("COLDSCREEN_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("COLDSCREEN_MODEL", "ollama:fake-model:1b")
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
    """ "fraud" is a word the deck contains, but a single-word claim is
    not stored. Dirty narrative still fails the per-field gate. The
    failure memo must not contain the word."""
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

    # The audit pack survives; the failure memo carries no verdict, no
    # narrative, and no stored single-word claim.
    casefile = CaseFile.model_validate_json(
        (case_dir(attack_env) / "casefile.json").read_text(encoding="utf-8")
    )
    assert casefile.verdict is None
    assert casefile.narrative is None
    assert casefile.claims == []
    assert casefile.claims_extraction is not None
    assert casefile.claims_extraction.dropped_thin_claims == 1
    assert casefile.claims_extraction.dropped_claims == 0
    assert any(f.id == "EXT-009" for f in casefile.findings)
    assert all(f.id != "EXT-007" for f in casefile.findings)
    memo = (case_dir(attack_env) / "memo.md").read_text(encoding="utf-8")
    assert "committing fraud" not in memo
    assert "fraud" not in memo.lower()
    assert "Synthesis was attempted and failed" in memo
    assert find_banned_terms(memo) == []


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


# -- render-only hand-tamper: claim-quote exemptions stay in the table ------------


def test_hand_tamper_short_claim_fails_render_only_backstop(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """A hand-edited short claim cannot whitelist the same word in narrative."""
    case_dir = tmp_path / "hand-tampered-short"
    shutil.copytree(GOLDEN_DIR, case_dir)
    casefile = CaseFile.model_validate_json(
        (case_dir / "casefile.json").read_text(encoding="utf-8")
    )
    claims = list(casefile.claims)
    claims[0] = claims[0].model_copy(update={"text": "fraud"})
    _write_casefile(
        case_dir,
        casefile.model_copy(
            update={
                "claims": claims,
                "narrative": "The public record shows fraud in the filing pattern.",
            }
        ),
    )
    (case_dir / "memo.md").unlink()
    result = runner.invoke(app, ["rerun", str(case_dir), "--render-only"])
    combined = all_output(result)
    assert result.exit_code == 1
    assert "language backstop" in combined
    assert not (case_dir / "memo.md").exists()
    assert "fraud" not in combined.lower()


def test_hand_tamper_full_phrase_in_prose_fails_render_only_backstop(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """Copying a stored claim phrase into narrative is no longer a whole-memo exempt span."""
    case_dir = tmp_path / "hand-tampered-phrase"
    shutil.copytree(GOLDEN_DIR, case_dir)
    casefile = CaseFile.model_validate_json(
        (case_dir / "casefile.json").read_text(encoding="utf-8")
    )
    assert PUFFERY_QUOTE in [c.text for c in casefile.claims]
    _write_casefile(case_dir, casefile.model_copy(update={"narrative": PUFFERY_QUOTE}))
    before_memo = (case_dir / "memo.md").read_bytes()
    result = runner.invoke(app, ["rerun", str(case_dir), "--render-only"])
    combined = all_output(result)
    assert result.exit_code == 1
    assert "language backstop" in combined
    assert (case_dir / "memo.md").read_bytes() == before_memo
    assert "fraud" not in combined.lower()


def test_hand_tamper_short_claim_fails_render_only_backstop_when_prose_is_clean(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """A planted single-word claim is not an exemption: the table cell is
    a hit even when narrative stays clean."""
    case_dir = tmp_path / "hand-tampered-positive"
    shutil.copytree(GOLDEN_DIR, case_dir)
    casefile = CaseFile.model_validate_json(
        (case_dir / "casefile.json").read_text(encoding="utf-8")
    )
    claims = list(casefile.claims)
    claims[0] = claims[0].model_copy(update={"text": "fraud"})
    tampered = casefile.model_copy(update={"claims": claims})
    _write_casefile(case_dir, tampered)
    (case_dir / "memo.md").unlink()
    result = runner.invoke(app, ["rerun", str(case_dir), "--render-only"])
    combined = all_output(result)
    assert result.exit_code == 1
    assert "language backstop" in combined
    assert not (case_dir / "memo.md").exists()
    assert "fraud" not in combined.lower()
    written = CaseFile.model_validate_json((case_dir / "casefile.json").read_text(encoding="utf-8"))
    assert language_backstop_failure(render_memo(written), written) is not None


def _plant_claim(case_dir: Path, text: str, source: str) -> CaseFile:
    casefile = CaseFile.model_validate_json(
        (case_dir / "casefile.json").read_text(encoding="utf-8")
    )
    claims = list(casefile.claims)
    claims[0] = claims[0].model_copy(update={"text": text, "source": source})
    planted = casefile.model_copy(update={"claims": claims})
    _write_casefile(case_dir, planted)
    return planted


def test_hand_tamper_two_token_claim_not_in_evidence_fails_render_only(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """A hand-edited two-token claim that sibling evidence does not carry
    is not an in-process exemption on rerun."""
    case_dir = tmp_path / "hand-tampered-two-token"
    shutil.copytree(GOLDEN_DIR, case_dir)
    before_casefile = (case_dir / "casefile.json").read_bytes()
    _plant_claim(case_dir, PLANTED_TWO_TOKEN, "deck p.2")
    after_plant = (case_dir / "casefile.json").read_bytes()
    assert after_plant != before_casefile
    (case_dir / "memo.md").unlink()
    result = runner.invoke(app, ["rerun", str(case_dir), "--render-only"])
    combined = all_output(result)
    assert result.exit_code == 1
    assert "language backstop" in combined
    assert not (case_dir / "memo.md").exists()
    assert "fraud" not in combined.lower()
    assert (case_dir / "casefile.json").read_bytes() == after_plant


def test_hand_tamper_honest_puffery_still_passes_render_only(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """The same plant of a real golden quotation still passes render-only."""
    case_dir = tmp_path / "hand-tampered-honest-quote"
    shutil.copytree(GOLDEN_DIR, case_dir)
    _plant_claim(case_dir, PUFFERY_QUOTE, "deck p.2")
    result = runner.invoke(app, ["rerun", str(case_dir), "--render-only"])
    combined = all_output(result)
    assert result.exit_code == 0, combined
    assert "language backstop" not in combined
    assert (case_dir / "memo.md").is_file()
    assert PUFFERY_QUOTE in (case_dir / "memo.md").read_text(encoding="utf-8")


def test_missing_evidence_on_rerun_drops_claim_quote_exemptions(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """Missing evidence/ on a claims-bearing rerun: no claim-quote
    exemption, so a banned word in the table fails closed."""
    case_dir = tmp_path / "missing-evidence-rerun"
    shutil.copytree(GOLDEN_DIR, case_dir)
    shutil.rmtree(case_dir / "evidence")
    (case_dir / "memo.md").unlink()
    result = runner.invoke(app, ["rerun", str(case_dir), "--render-only"])
    combined = all_output(result)
    assert result.exit_code == 1
    assert "language backstop" in combined
    assert not (case_dir / "memo.md").exists()
    assert "fraud" not in combined.lower()


def test_label_aware_wrong_deck_page_fails_render_only(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """A deck p.2 claim whose text lives only on page 1 is not an
    in-process exemption on rerun."""
    case_dir = tmp_path / "wrong-page-rerun"
    shutil.copytree(GOLDEN_DIR, case_dir)
    deck_path = case_dir / "evidence" / "deck_text.json"
    payload = json.loads(deck_path.read_text(encoding="utf-8"))
    pages = payload["body"]["pages"]
    assert PLANTED_TWO_TOKEN not in pages.get("2", "")
    pages["1"] = f"{pages['1']}\n{PLANTED_TWO_TOKEN}"
    deck_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    _plant_claim(case_dir, PLANTED_TWO_TOKEN, "deck p.2")
    (case_dir / "memo.md").unlink()
    result = runner.invoke(app, ["rerun", str(case_dir), "--render-only"])
    combined = all_output(result)
    assert result.exit_code == 1
    assert "language backstop" in combined
    assert not (case_dir / "memo.md").exists()
    assert "fraud" not in combined.lower()
