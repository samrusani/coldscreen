"""CLI behavior: screen, rerun, ambiguity exit code, secret hygiene.

Every test runs with respx active, so any unmocked network call fails the
test. The rerun tests register no routes at all: rerun must be offline.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner, Result

import firstpass.cli
from firstpass.cli import app
from firstpass.models import CaseFile

from .conftest import FIXTURES_DIR, load_fixture, mock_company_routes

SECRET = "fixture-key-0123456789-not-a-real-key"
FIXTURE_CASE_DIR = FIXTURES_DIR / "case-fabricated-widgets-ltd-99999999"
EXPECTED_MEMO = FIXTURE_CASE_DIR / "memo.md"

runner = CliRunner()


def load_synthesis_json(name: str) -> str:
    """A recorded synthesis response document as a compact JSON string."""
    raw = (FIXTURES_DIR / name / "synthesis_response.json").read_text(encoding="utf-8")
    return json.dumps(json.loads(raw))


def all_output(result: Result) -> str:
    """stdout plus stderr, tolerant of click versions that separate them."""
    text = result.output
    try:
        text += result.stderr
    except (ValueError, AttributeError):
        pass
    return text


@pytest.fixture
def screen_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolated working directory with a frozen clock and a fixture key."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", SECRET)
    monkeypatch.setenv("FIRSTPASS_SCREENED_AT", "2026-08-18T12:00:00+00:00")
    monkeypatch.setenv("FIRSTPASS_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path


def test_screen_writes_the_full_case_directory(
    screen_env: Path, respx_mock: respx.MockRouter
) -> None:
    mock_company_routes(respx_mock)
    result = runner.invoke(app, ["screen", "Fabricated Widgets Ltd"])
    assert result.exit_code == 0, result.output

    case_dir = screen_env / "cases" / "fabricated-widgets-ltd-99999999"
    assert case_dir.is_dir()
    assert (case_dir / "memo.md").is_file()
    assert (case_dir / "casefile.json").is_file()

    expected_evidence = {
        "search_companies",
        "registry_profile",
        "officers_p1",
        "psc_p1",
        "filing_history_p1",
        "charges",
        # Weekend 2: network expansion evidence for the three CURRENT
        # officers, disqualification searches for each screened person, and
        # the explicit not-run notes for the unconfigured stages. The PSC
        # duplicates officer Wanda (same normalized name and DOB month and
        # year), so she is searched exactly once.
        "appointments_widgetsmith-wanda_p1",
        "appointments_cogwheel-cornelius_p1",
        "appointments_sprocket-sybil_p1",
        "disqualified_search_widgetsmith-wanda",
        "disqualified_search_cogwheel-cornelius",
        "disqualified_search_sprocket-sybil",
        "sanctions_not_run",
        "media_not_run",
    }
    evidence_dir = case_dir / "evidence"
    for name in expected_evidence:
        assert (evidence_dir / f"{name}.json").is_file(), f"missing evidence file {name}"

    index = json.loads((evidence_dir / "index.json").read_text(encoding="utf-8"))
    assert {entry["name"] for entry in index} == expected_evidence

    casefile = CaseFile.model_validate_json(
        (case_dir / "casefile.json").read_text(encoding="utf-8")
    )
    assert casefile.subject.company_number == "99999999"
    assert casefile.verdict is None
    assert casefile.claims == []
    # Current officers plus the two resigned within the lookback window.
    assert len(casefile.officers) == 5
    # The unconfigured stages are recorded as skipped, not silently absent.
    assert casefile.sanctions is not None and casefile.sanctions.performed is False
    assert casefile.media is not None and casefile.media.performed is False
    assert casefile.network is not None and casefile.network.performed is True
    finding_ids = {f.id for f in casefile.findings}
    assert {"SAN-000", "MED-000", "NET-001", "NET-002"} <= finding_ids


def test_screen_memo_matches_the_snapshot(screen_env: Path, respx_mock: respx.MockRouter) -> None:
    mock_company_routes(respx_mock)
    result = runner.invoke(app, ["screen", "Fabricated Widgets Ltd"])
    assert result.exit_code == 0, result.output
    produced = (screen_env / "cases" / "fabricated-widgets-ltd-99999999" / "memo.md").read_bytes()
    assert produced == EXPECTED_MEMO.read_bytes()


def test_screen_accepts_a_company_number_and_skips_search(
    screen_env: Path, respx_mock: respx.MockRouter
) -> None:
    mock_company_routes(respx_mock)
    search_route = respx_mock.get("https://api.company-information.service.gov.uk/search/companies")
    result = runner.invoke(app, ["screen", "99999999"])
    assert result.exit_code == 0, result.output
    assert search_route.call_count == 0
    case_dir = screen_env / "cases" / "fabricated-widgets-ltd-99999999"
    assert not (case_dir / "evidence" / "search_companies.json").exists()


def test_screen_json_flag_prints_the_casefile(
    screen_env: Path, respx_mock: respx.MockRouter
) -> None:
    mock_company_routes(respx_mock)
    result = runner.invoke(app, ["screen", "Fabricated Widgets Ltd", "--json"])
    assert result.exit_code == 0, result.output
    assert '"company_number": "99999999"' in result.output
    assert '"verdict": null' in result.output


def test_json_stdout_is_pure_json(screen_env: Path, respx_mock: respx.MockRouter) -> None:
    """With --json the status line moves to stderr; stdout parses as JSON."""
    mock_company_routes(respx_mock)
    result = runner.invoke(app, ["screen", "Fabricated Widgets Ltd", "--json"])
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["subject"]["company_number"] == "99999999"
    assert parsed["clock_override"] is True
    assert "Case directory written" in result.stderr


def test_screen_records_the_clock_override(screen_env: Path, respx_mock: respx.MockRouter) -> None:
    mock_company_routes(respx_mock)
    result = runner.invoke(app, ["screen", "Fabricated Widgets Ltd"])
    assert result.exit_code == 0, result.output
    case_dir = screen_env / "cases" / "fabricated-widgets-ltd-99999999"
    casefile = CaseFile.model_validate_json(
        (case_dir / "casefile.json").read_text(encoding="utf-8")
    )
    assert casefile.clock_override is True
    memo = (case_dir / "memo.md").read_text(encoding="utf-8")
    assert "overridden through FIRSTPASS_SCREENED_AT" in memo


def test_ambiguous_search_exits_3_with_the_candidate_table(
    screen_env: Path, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("https://api.company-information.service.gov.uk/search/companies").respond(
        200, json=load_fixture("search_ambiguous.json")
    )
    result = runner.invoke(app, ["screen", "Fabricated Widgets"])
    assert result.exit_code == 3
    combined = all_output(result)
    assert "99999999" in combined
    assert "99999998" in combined
    assert "firstpass screen" in combined


def test_exact_title_match_is_announced(screen_env: Path, respx_mock: respx.MockRouter) -> None:
    """A resolved tie must be visible: the notice names the match and the rest."""
    mock_company_routes(respx_mock, ambiguous_search=True)
    result = runner.invoke(app, ["screen", "Fabricated Widgets Ltd"])
    assert result.exit_code == 0, result.output
    combined = all_output(result)
    assert "set aside" in combined
    assert "FABRICATED WIDGETS LTD (99999999)" in combined
    assert (screen_env / "cases" / "fabricated-widgets-ltd-99999999").is_dir()


def test_interactive_picker_selects_a_candidate(
    screen_env: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_company_routes(respx_mock, ambiguous_search=True)
    monkeypatch.setattr(firstpass.cli, "_is_interactive", lambda: True)
    result = runner.invoke(app, ["screen", "Fabricated Widgets"], input="1\n")
    assert result.exit_code == 0, result.output
    assert (screen_env / "cases" / "fabricated-widgets-ltd-99999999").is_dir()


def test_interactive_picker_zero_aborts(
    screen_env: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx_mock.get("https://api.company-information.service.gov.uk/search/companies").respond(
        200, json=load_fixture("search_ambiguous.json")
    )
    monkeypatch.setattr(firstpass.cli, "_is_interactive", lambda: True)
    result = runner.invoke(app, ["screen", "Fabricated Widgets"], input="0\n")
    assert result.exit_code == 3


def test_missing_api_key_is_a_clear_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COMPANIES_HOUSE_API_KEY", raising=False)
    result = runner.invoke(app, ["screen", "Fabricated Widgets Ltd"])
    assert result.exit_code == 1
    assert "COMPANIES_HOUSE_API_KEY" in all_output(result)


def test_401_from_the_api_names_the_key_variable(
    screen_env: Path, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("https://api.company-information.service.gov.uk/search/companies").respond(401)
    result = runner.invoke(app, ["screen", "Fabricated Widgets Ltd"])
    assert result.exit_code == 1
    combined = all_output(result)
    assert "Authentication failed" in combined
    assert "COMPANIES_HOUSE_API_KEY" in combined


def test_unreachable_api_is_a_clean_error(
    screen_env: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The CLI-built client binds time.sleep at construction; patch it first
    # so exhausting the retry policy takes no real time.
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    respx_mock.get("https://api.company-information.service.gov.uk/search/companies").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    result = runner.invoke(app, ["screen", "Fabricated Widgets Ltd"])
    assert result.exit_code == 1
    combined = all_output(result)
    assert "Could not reach the Companies House API" in combined
    assert "Traceback" not in combined


def test_malicious_company_number_from_search_is_rejected(
    screen_env: Path, respx_mock: respx.MockRouter
) -> None:
    payload = {
        "total_results": 1,
        "items": [
            {
                "title": "PATH TRAVERSAL LTD",
                "company_number": "../../evil",
                "kind": "searchresults#company",
            }
        ],
    }
    respx_mock.get("https://api.company-information.service.gov.uk/search/companies").respond(
        200, json=payload
    )
    result = runner.invoke(app, ["screen", "Path Traversal Ltd"])
    assert result.exit_code == 1
    assert "not plain alphanumeric" in all_output(result)
    assert not (screen_env / "cases").exists()


def test_nonexistent_config_path_is_an_error(
    screen_env: Path, respx_mock: respx.MockRouter
) -> None:
    missing = screen_env / "no-such-firstpass.toml"
    result = runner.invoke(app, ["screen", "Fabricated Widgets Ltd", "--config", str(missing)])
    # typer reports option validation as a usage error, exit code 2. The
    # rich error panel wraps long lines, so assert on the stable prefix.
    assert result.exit_code == 2
    assert "Invalid value for '--config'" in all_output(result)


def test_the_api_key_never_reaches_disk_or_output(
    screen_env: Path, respx_mock: respx.MockRouter
) -> None:
    mock_company_routes(respx_mock)
    result = runner.invoke(app, ["screen", "Fabricated Widgets Ltd", "--json"])
    assert result.exit_code == 0, result.output
    assert SECRET not in all_output(result)
    offenders = [
        path
        for path in screen_env.rglob("*")
        if path.is_file() and SECRET.encode() in path.read_bytes()
    ]
    assert offenders == []


def test_rerun_regenerates_the_memo_offline(tmp_path: Path, respx_mock: respx.MockRouter) -> None:
    # No routes are registered: any HTTP request would fail this test.
    case_dir = tmp_path / "case-fabricated-widgets-ltd-99999999"
    shutil.copytree(FIXTURE_CASE_DIR, case_dir)
    (case_dir / "memo.md").unlink()
    result = runner.invoke(app, ["rerun", str(case_dir)])
    assert result.exit_code == 0, result.output
    assert (case_dir / "memo.md").read_bytes() == EXPECTED_MEMO.read_bytes()


def test_rerun_without_casefile_fails_cleanly(tmp_path: Path) -> None:
    empty_dir = tmp_path / "not-a-case"
    empty_dir.mkdir()
    result = runner.invoke(app, ["rerun", str(empty_dir)])
    assert result.exit_code == 1
    assert "casefile.json" in all_output(result)


def test_rerun_with_corrupt_casefile_fails_cleanly(tmp_path: Path) -> None:
    case_dir = tmp_path / "corrupt-case"
    case_dir.mkdir()
    (case_dir / "casefile.json").write_text("{ this is not json", encoding="utf-8")
    result = runner.invoke(app, ["rerun", str(case_dir)])
    assert result.exit_code == 1
    combined = all_output(result)
    assert "casefile.json" in combined
    assert "not a valid casefile" in combined
    assert "Traceback" not in combined


def test_rerun_with_wrong_shape_casefile_fails_cleanly(tmp_path: Path) -> None:
    case_dir = tmp_path / "wrong-shape-case"
    case_dir.mkdir()
    (case_dir / "casefile.json").write_text(
        json.dumps({"subject": {"company_name": "X"}}), encoding="utf-8"
    )
    result = runner.invoke(app, ["rerun", str(case_dir)])
    assert result.exit_code == 1
    assert "not a valid casefile" in all_output(result)


# -- weekend 2: model selection, synthesis, rerun re-synthesis ---------------

OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"


def ollama_reply(content: str) -> dict[str, object]:
    return {
        "model": "fake-model:1b",
        "message": {"role": "assistant", "content": content},
        "done": True,
    }


def test_no_model_is_the_deterministic_path(screen_env: Path, respx_mock: respx.MockRouter) -> None:
    mock_company_routes(respx_mock)
    result = runner.invoke(app, ["screen", "Fabricated Widgets Ltd"])
    assert result.exit_code == 0, result.output
    memo = (screen_env / "cases" / "fabricated-widgets-ltd-99999999" / "memo.md").read_text(
        encoding="utf-8"
    )
    assert "No synthesis: no model configured" in memo
    casefile = CaseFile.model_validate_json(
        (screen_env / "cases" / "fabricated-widgets-ltd-99999999" / "casefile.json").read_text(
            encoding="utf-8"
        )
    )
    assert casefile.verdict is None
    assert casefile.synthesis is None


def test_screen_with_ollama_model_synthesizes_and_enforces(
    screen_env: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full CLI synthesis over local HTTP, with the anchoring mechanism live.

    The fixture company carries A1 (accounts overdue) and A2 (wholesale
    churn) candidates. The canned model reply says green with no triggers,
    so enforcement must add both candidates and recompute the level.
    """
    mock_company_routes(respx_mock)
    monkeypatch.setenv("FIRSTPASS_MODEL", "ollama:fake-model:1b")
    route = respx_mock.post(OLLAMA_CHAT_URL).respond(
        200, json=ollama_reply(load_synthesis_json("green"))
    )
    result = runner.invoke(app, ["screen", "Fabricated Widgets Ltd"])
    assert result.exit_code == 0, result.output
    # First-colon split: the model name keeps its own colon.
    sent = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert sent["model"] == "fake-model:1b"
    assert sent["stream"] is False
    assert sent["options"] == {"temperature": 0, "num_ctx": 16384}
    assert sent["format"]["type"] == "object"  # bare JSON schema, no envelope

    casefile = CaseFile.model_validate_json(
        (screen_env / "cases" / "fabricated-widgets-ltd-99999999" / "casefile.json").read_text(
            encoding="utf-8"
        )
    )
    assert casefile.verdict is not None
    assert casefile.verdict.level == "amber"
    assert {"A1", "A2"} <= set(casefile.verdict.triggered)
    assert casefile.verdict_enforcement is not None
    assert casefile.synthesis is not None
    assert casefile.synthesis.provider == "ollama"
    assert casefile.synthesis.model == "fake-model:1b"
    assert casefile.synthesis.model_level == "green"
    memo = (screen_env / "cases" / "fabricated-widgets-ltd-99999999" / "memo.md").read_text(
        encoding="utf-8"
    )
    assert "**AMBER**" in memo
    assert "Verdict level enforced" in memo


def test_bad_model_spec_fails_before_any_fetching(
    screen_env: Path, respx_mock: respx.MockRouter
) -> None:
    search_route = respx_mock.get("https://api.company-information.service.gov.uk/search/companies")
    result = runner.invoke(
        app, ["screen", "Fabricated Widgets Ltd", "--model", "sorcery:crystal-ball"]
    )
    assert result.exit_code == 1
    assert "unknown provider" in all_output(result)
    assert search_route.call_count == 0
    assert not (screen_env / "cases").exists()


def test_ollama_without_model_name_is_an_error(
    screen_env: Path, respx_mock: respx.MockRouter
) -> None:
    result = runner.invoke(app, ["screen", "Fabricated Widgets Ltd", "--model", "ollama"])
    assert result.exit_code == 1
    assert "explicit model" in all_output(result)


def test_synthesis_failure_keeps_the_audit_pack_and_exits_1(
    screen_env: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_company_routes(respx_mock)
    monkeypatch.setenv("FIRSTPASS_MODEL", "ollama:fake-model:1b")
    respx_mock.post(OLLAMA_CHAT_URL).respond(200, json=ollama_reply("this is not json"))
    result = runner.invoke(app, ["screen", "Fabricated Widgets Ltd"])
    assert result.exit_code == 1
    combined = all_output(result)
    assert "Synthesis failed" in combined
    assert "Traceback" not in combined
    case_dir = screen_env / "cases" / "fabricated-widgets-ltd-99999999"
    assert (case_dir / "casefile.json").is_file()
    memo = (case_dir / "memo.md").read_text(encoding="utf-8")
    assert "Synthesis was attempted and failed" in memo
    casefile = CaseFile.model_validate_json(
        (case_dir / "casefile.json").read_text(encoding="utf-8")
    )
    assert casefile.verdict is None


def test_rerun_render_only_stays_offline(tmp_path: Path, respx_mock: respx.MockRouter) -> None:
    # No routes registered: any HTTP request fails the test, model configured
    # or not.
    case_dir = tmp_path / "case-fabricated-widgets-ltd-99999999"
    shutil.copytree(FIXTURE_CASE_DIR, case_dir)
    (case_dir / "memo.md").unlink()
    result = runner.invoke(app, ["rerun", str(case_dir), "--render-only"])
    assert result.exit_code == 0, result.output
    assert (case_dir / "memo.md").read_bytes() == EXPECTED_MEMO.read_bytes()


def test_rerun_without_model_behaves_like_render_only_with_notice(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    case_dir = tmp_path / "case-fabricated-widgets-ltd-99999999"
    shutil.copytree(FIXTURE_CASE_DIR, case_dir)
    (case_dir / "memo.md").unlink()
    result = runner.invoke(app, ["rerun", str(case_dir)])
    assert result.exit_code == 0, result.output
    assert "No model configured" in all_output(result)
    assert (case_dir / "memo.md").read_bytes() == EXPECTED_MEMO.read_bytes()


def test_rerun_with_model_resynthesizes_without_refetching(
    tmp_path: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the Ollama route exists. Any registry, sanctions, or search call
    # would be unmocked and fail: rerun must not refetch anything.
    monkeypatch.chdir(tmp_path)
    case_dir = tmp_path / "case-fabricated-widgets-ltd-99999999"
    shutil.copytree(FIXTURE_CASE_DIR, case_dir)
    route = respx_mock.post(OLLAMA_CHAT_URL).respond(
        200, json=ollama_reply(load_synthesis_json("green"))
    )
    result = runner.invoke(app, ["rerun", str(case_dir), "--model", "ollama:fake-model:1b"])
    assert result.exit_code == 0, result.output
    assert route.call_count == 1
    casefile = CaseFile.model_validate_json(
        (case_dir / "casefile.json").read_text(encoding="utf-8")
    )
    # Enforcement applies at rerun too: the stored casefile carries A1 and
    # A2 candidates, so the canned green reply is corrected to amber.
    assert casefile.verdict is not None
    assert casefile.verdict.level == "amber"
    assert {"A1", "A2"} <= set(casefile.verdict.triggered)
    assert casefile.synthesis is not None
    assert casefile.synthesis.model == "fake-model:1b"
    memo = (case_dir / "memo.md").read_text(encoding="utf-8")
    assert "**AMBER**" in memo


def test_rerun_synthesis_failure_leaves_files_untouched(
    tmp_path: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    case_dir = tmp_path / "case-fabricated-widgets-ltd-99999999"
    shutil.copytree(FIXTURE_CASE_DIR, case_dir)
    before_casefile = (case_dir / "casefile.json").read_bytes()
    before_memo = (case_dir / "memo.md").read_bytes()
    respx_mock.post(OLLAMA_CHAT_URL).respond(200, json=ollama_reply("still not json"))
    result = runner.invoke(app, ["rerun", str(case_dir), "--model", "ollama:fake-model:1b"])
    assert result.exit_code == 1
    assert "Synthesis failed" in all_output(result)
    assert (case_dir / "casefile.json").read_bytes() == before_casefile
    assert (case_dir / "memo.md").read_bytes() == before_memo


# -- the whole-memo language backstop -----------------------------------------


def _poison_casefile(case_dir: Path) -> str:
    """Append a banned word to a deterministic finding statement: a channel
    the per-field synthesis gate never sees. Returns the poisoned JSON."""
    data = json.loads((case_dir / "casefile.json").read_text(encoding="utf-8"))
    data["findings"][0]["statement"] += " Reads like a scam."
    poisoned = json.dumps(data)
    (case_dir / "casefile.json").write_text(poisoned, encoding="utf-8")
    return poisoned


def test_language_backstop_blocks_a_poisoned_memo_on_rerun(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    """Banned text forced through a non-gated channel never reaches disk."""
    case_dir = tmp_path / "case-fabricated-widgets-ltd-99999999"
    shutil.copytree(FIXTURE_CASE_DIR, case_dir)
    _poison_casefile(case_dir)
    (case_dir / "memo.md").unlink()
    result = runner.invoke(app, ["rerun", str(case_dir), "--render-only"])
    assert result.exit_code == 1
    assert not (case_dir / "memo.md").exists()
    combined = all_output(result)
    assert "language backstop" in combined
    assert "1 banned term(s)" in combined
    # The count is reported; the term itself never is.
    assert "scam" not in combined.lower()
    assert "Traceback" not in combined


def test_language_backstop_on_rerun_with_model_leaves_files_untouched(
    tmp_path: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    case_dir = tmp_path / "case-fabricated-widgets-ltd-99999999"
    shutil.copytree(FIXTURE_CASE_DIR, case_dir)
    poisoned = _poison_casefile(case_dir)
    before_memo = (case_dir / "memo.md").read_bytes()
    respx_mock.post(OLLAMA_CHAT_URL).respond(200, json=ollama_reply(load_synthesis_json("green")))
    result = runner.invoke(app, ["rerun", str(case_dir), "--model", "ollama:fake-model:1b"])
    assert result.exit_code == 1
    assert "language backstop" in all_output(result)
    # The backstop fires before ANY write: the (poisoned) casefile and the
    # old memo are byte-identical to what was there before.
    assert (case_dir / "casefile.json").read_text(encoding="utf-8") == poisoned
    assert (case_dir / "memo.md").read_bytes() == before_memo


def test_language_backstop_blocks_a_poisoned_memo_on_screen(
    screen_env: Path, respx_mock: respx.MockRouter
) -> None:
    """A banned word arriving through source data (the registered company
    name) trips the backstop on the screen path: no case directory at all."""
    base = "https://api.company-information.service.gov.uk"
    profile = load_fixture("profile.json")
    profile["company_name"] = "TOTAL SHAM TRADING LTD"
    respx_mock.get(f"{base}/company/99999999").respond(200, json=profile)
    respx_mock.get(f"{base}/company/99999999/officers").respond(
        200, json=load_fixture("officers.json")
    )
    respx_mock.get(f"{base}/company/99999999/persons-with-significant-control").respond(
        200, json=load_fixture("psc.json")
    )
    respx_mock.get(f"{base}/company/99999999/filing-history").respond(
        200, json=load_fixture("filing_history.json")
    )
    respx_mock.get(f"{base}/company/99999999/charges").respond(
        200, json=load_fixture("charges.json")
    )
    for number in (1, 2, 3):
        respx_mock.get(f"{base}/officers/fictOfficer00{number}/appointments").respond(
            200, json=load_fixture(f"appointments_officer{number}.json")
        )
    respx_mock.get(f"{base}/search/disqualified-officers").respond(
        200, json=load_fixture("disqualified_search_empty.json")
    )
    result = runner.invoke(app, ["screen", "99999999"])
    assert result.exit_code == 1
    combined = all_output(result)
    assert "language backstop" in combined
    assert "sham" not in combined.lower()
    assert not (screen_env / "cases").exists()
