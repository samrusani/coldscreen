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

    evidence_dir = case_dir / "evidence"
    for name in (
        "search_companies.json",
        "registry_profile.json",
        "officers_p1.json",
        "psc_p1.json",
        "filing_history_p1.json",
        "charges.json",
        "index.json",
    ):
        assert (evidence_dir / name).is_file(), f"missing evidence file {name}"

    index = json.loads((evidence_dir / "index.json").read_text(encoding="utf-8"))
    assert {entry["name"] for entry in index} == {
        "search_companies",
        "registry_profile",
        "officers_p1",
        "psc_p1",
        "filing_history_p1",
        "charges",
    }

    casefile = CaseFile.model_validate_json(
        (case_dir / "casefile.json").read_text(encoding="utf-8")
    )
    assert casefile.subject.company_number == "99999999"
    assert casefile.verdict is None
    assert casefile.claims == []
    # Current officers plus the two resigned within the lookback window.
    assert len(casefile.officers) == 5


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
