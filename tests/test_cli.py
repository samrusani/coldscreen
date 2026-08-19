"""CLI behavior: screen, rerun, ambiguity exit code, secret hygiene.

Every test runs with respx active, so any unmocked network call fails the
test. The rerun tests register no routes at all: rerun must be offline.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
import time
import types
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner, Result

import coldscreen.cli
from coldscreen.cli import app
from coldscreen.language import find_banned_terms
from coldscreen.models import CaseFile

from .conftest import FIXTURES_DIR, load_fixture, mock_company_routes
from .fakes import synthesis_json

SECRET = "fixture-key-0123456789-not-a-real-key"
FIXTURE_CASE_DIR = FIXTURES_DIR / "case-fabricated-widgets-ltd-99999999"
EXPECTED_MEMO = FIXTURE_CASE_DIR / "memo.md"

runner = CliRunner()


def neutral_synthesis_json(level: str = "green") -> str:
    """A canned synthesis response with casefile-neutral prose.

    The snapshot fixtures' recorded responses describe THEIR casefile's
    stages ("sanctions screening ran and returned no candidates"), which is
    a stage-honesty violation when replayed against the fixture company,
    whose sanctions and media stages never run. These CLI tests exercise
    plumbing and enforcement, so they use prose that asserts nothing about
    any stage.
    """
    return synthesis_json(level)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def all_output(result: Result) -> str:
    """stdout plus stderr, tolerant of click versions that separate them."""
    text = result.output
    try:
        text += result.stderr
    except (ValueError, AttributeError):
        pass
    return text


def flat_output(result: Result) -> str:
    """all_output with ANSI styling and line wrapping removed.

    Rich wraps its error panels to the terminal width, so a phrase like
    "Invalid value for '--config'" is split across lines on a narrow CI
    terminal but not on a wide developer one. Collapsing whitespace and
    dropping escape sequences makes assertions about wording independent
    of the width the tests happen to run at.
    """
    text = _ANSI_RE.sub("", all_output(result))
    return " ".join(text.split())


@pytest.fixture
def screen_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolated working directory with a frozen clock and a fixture key."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", SECRET)
    monkeypatch.setenv("COLDSCREEN_SCREENED_AT", "2026-08-18T12:00:00+00:00")
    monkeypatch.setenv("COLDSCREEN_CACHE_DIR", str(tmp_path / "cache"))
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
        "claims_not_run",
    }
    evidence_dir = case_dir / "evidence"
    for name in expected_evidence:
        assert (evidence_dir / f"{name}.json").is_file(), f"missing evidence file {name}"

    index = json.loads((evidence_dir / "index.json").read_text(encoding="utf-8"))
    assert {entry["name"] for entry in index} == expected_evidence

    casefile = CaseFile.model_validate_json(
        (case_dir / "casefile.json").read_text(encoding="utf-8")
    )
    assert casefile.schema_version == 1
    assert casefile.subject.company_number == "99999999"
    assert casefile.verdict is None
    assert casefile.claims == []
    # Current officers plus the two resigned within the lookback window.
    assert len(casefile.officers) == 5
    # The unconfigured stages are recorded as skipped, not silently absent.
    assert casefile.sanctions is not None and casefile.sanctions.performed is False
    assert casefile.media is not None and casefile.media.performed is False
    assert casefile.network is not None and casefile.network.performed is True
    assert casefile.claims_extraction is not None
    assert casefile.claims_extraction.performed is False
    assert casefile.claims_extraction.skipped_reason == "no deck or site provided"
    finding_ids = {f.id for f in casefile.findings}
    assert {"SAN-000", "MED-000", "NET-001", "NET-002", "EXT-000"} <= finding_ids


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
    assert '"schema_version": 1' in result.output
    assert '"company_number": "99999999"' in result.output
    assert '"verdict": null' in result.output


def test_out_of_range_setting_is_a_clean_configuration_error(
    screen_env: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLDSCREEN_SANCTIONS_THRESHOLD", "1.5")
    result = runner.invoke(app, ["screen", "99999999"])
    assert result.exit_code == 1
    combined = all_output(result)
    assert "Configuration error" in combined
    assert "sanctions_threshold" in combined
    assert "Traceback" not in combined
    assert not (screen_env / "cases").exists()


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
    assert "overridden through COLDSCREEN_SCREENED_AT" in memo


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
    assert "coldscreen screen" in combined


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
    monkeypatch.setattr(coldscreen.cli, "_is_interactive", lambda: True)
    result = runner.invoke(app, ["screen", "Fabricated Widgets"], input="1\n")
    assert result.exit_code == 0, result.output
    assert (screen_env / "cases" / "fabricated-widgets-ltd-99999999").is_dir()


def test_interactive_picker_zero_aborts(
    screen_env: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx_mock.get("https://api.company-information.service.gov.uk/search/companies").respond(
        200, json=load_fixture("search_ambiguous.json")
    )
    monkeypatch.setattr(coldscreen.cli, "_is_interactive", lambda: True)
    result = runner.invoke(app, ["screen", "Fabricated Widgets"], input="0\n")
    assert result.exit_code == 3


def test_provider_transport_is_closed_after_rerun_with_model(
    tmp_path: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider's transport is closed in a finally, success or not."""
    closed: list[str] = []

    class ClosingFakeProvider:
        def complete(self, system: str, messages: object, json_schema: object = None) -> str:
            return neutral_synthesis_json("green")

        def close(self) -> None:
            closed.append("closed")

    monkeypatch.setattr(
        coldscreen.cli,
        "_prepare_provider",
        lambda settings, cli_model: (ClosingFakeProvider(), "fake", "canned"),
    )
    case_dir = tmp_path / "case-fabricated-widgets-ltd-99999999"
    shutil.copytree(FIXTURE_CASE_DIR, case_dir)
    result = runner.invoke(app, ["rerun", str(case_dir), "--model", "fake:canned"])
    assert result.exit_code == 0, result.output
    assert closed == ["closed"]


def test_provider_transport_is_closed_when_synthesis_fails(
    tmp_path: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[str] = []

    class FailingFakeProvider:
        def complete(self, system: str, messages: object, json_schema: object = None) -> str:
            return "not json at all"

        def close(self) -> None:
            closed.append("closed")

    monkeypatch.setattr(
        coldscreen.cli,
        "_prepare_provider",
        lambda settings, cli_model: (FailingFakeProvider(), "fake", "canned"),
    )
    case_dir = tmp_path / "case-fabricated-widgets-ltd-99999999"
    shutil.copytree(FIXTURE_CASE_DIR, case_dir)
    result = runner.invoke(app, ["rerun", str(case_dir), "--model", "fake:canned"])
    assert result.exit_code == 1
    assert closed == ["closed"]


def test_version_flag_prints_the_version_and_exits_zero() -> None:
    from coldscreen import __version__

    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert f"coldscreen {__version__}" in result.output


def test_cli_imports_and_runs_when_the_mcp_extra_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A default install has no mcp dependency. Importing the CLI must not
    care, and `coldscreen mcp` must say what to install rather than raise.

    The extra is simulated by evicting every mcp module from the import
    cache and parking None under the top-level name, which is what makes
    `import mcp` raise ImportError. The CLI module is then executed from
    source into a throwaway module object, so the cached one that the rest
    of this file uses is left alone.
    """
    for name in [n for n in sys.modules if n == "mcp" or n.startswith("mcp.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.delitem(sys.modules, "coldscreen.mcp_server", raising=False)
    monkeypatch.setitem(sys.modules, "mcp", None)

    spec = importlib.util.spec_from_file_location("coldscreen.cli", coldscreen.cli.__file__)
    assert spec is not None and spec.loader is not None
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)

    assert fresh._load_mcp_server_builder() is None
    result = runner.invoke(fresh.app, ["mcp"])
    assert result.exit_code == 1
    combined = all_output(result)
    assert "coldscreen[mcp]" in combined
    assert "Traceback" not in combined


def test_a_broken_mcp_server_module_is_not_reported_as_a_missing_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telling the user to install the extra is useless advice for a bug here.

    The stub stands in for an import inside mcp_server.py that fails while
    the mcp package itself is perfectly importable: a typo, or an SDK
    submodule that moved. That must surface, not be swallowed.
    """

    class BrokenModule(types.ModuleType):
        def __getattr__(self, name: str) -> object:
            raise ModuleNotFoundError("No module named 'mcp.server.moved_away'")

    monkeypatch.setitem(sys.modules, "coldscreen.mcp_server", BrokenModule("coldscreen.mcp_server"))
    with pytest.raises(ModuleNotFoundError, match="moved_away"):
        coldscreen.cli._load_mcp_server_builder()


def test_the_mcp_command_starts_a_stdio_server_and_keeps_stdout_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stdout belongs to JSON-RPC: the readiness line goes to stderr."""
    transports: list[str] = []

    class FakeServer:
        def run(self, transport: str) -> None:
            transports.append(transport)

    monkeypatch.setattr(coldscreen.cli, "_load_mcp_server_builder", lambda: FakeServer)
    result = runner.invoke(app, ["mcp"])
    assert result.exit_code == 0, result.output
    assert transports == ["stdio"]
    assert result.stdout == ""
    assert "ready on stdio" in result.stderr


def test_output_dir_flag_moves_the_case_directory(
    screen_env: Path, respx_mock: respx.MockRouter
) -> None:
    mock_company_routes(respx_mock)
    result = runner.invoke(app, ["screen", "99999999", "--output-dir", "elsewhere"])
    assert result.exit_code == 0, result.output
    assert (screen_env / "elsewhere" / "fabricated-widgets-ltd-99999999" / "memo.md").is_file()
    assert not (screen_env / "cases").exists()


def test_empty_search_results_exit_one_with_a_clear_message(
    screen_env: Path, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("https://api.company-information.service.gov.uk/search/companies").respond(
        200, json={"total_results": 0, "items": []}
    )
    result = runner.invoke(app, ["screen", "Nonexistent Fictional Widgets"])
    assert result.exit_code == 1
    assert "No companies matched" in all_output(result)
    assert not (screen_env / "cases").exists()


def test_profile_404_for_a_company_number_exits_one(
    screen_env: Path, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("https://api.company-information.service.gov.uk/company/99999999").respond(
        404, json={"error": "company-profile-not-found"}
    )
    result = runner.invoke(app, ["screen", "99999999"])
    assert result.exit_code == 1
    assert "was not found on the register" in all_output(result)
    assert not (screen_env / "cases").exists()


def test_screening_into_an_existing_case_directory_is_an_error(
    screen_env: Path, respx_mock: respx.MockRouter
) -> None:
    mock_company_routes(respx_mock)
    first = runner.invoke(app, ["screen", "99999999"])
    assert first.exit_code == 0, first.output
    case_dir = screen_env / "cases" / "fabricated-widgets-ltd-99999999"
    marker = (case_dir / "memo.md").read_bytes()

    second = runner.invoke(app, ["screen", "99999999"])
    assert second.exit_code == 1
    assert "already exists" in all_output(second)
    assert "--overwrite" in all_output(second)
    # Nothing was touched.
    assert (case_dir / "memo.md").read_bytes() == marker


def test_overwrite_flag_replaces_the_case_directory_and_clears_stale_evidence(
    screen_env: Path, respx_mock: respx.MockRouter
) -> None:
    mock_company_routes(respx_mock)
    first = runner.invoke(app, ["screen", "99999999"])
    assert first.exit_code == 0, first.output
    case_dir = screen_env / "cases" / "fabricated-widgets-ltd-99999999"
    stale = case_dir / "evidence" / "stale_leftover.json"
    stale.write_text("{}", encoding="utf-8")
    user_note = case_dir / "my-notes.txt"
    user_note.write_text("keep me", encoding="utf-8")

    second = runner.invoke(app, ["screen", "99999999", "--overwrite"])
    assert second.exit_code == 0, second.output
    assert not stale.exists()  # tool-owned evidence dir is replaced wholesale
    assert user_note.read_text(encoding="utf-8") == "keep me"  # user files survive
    assert (case_dir / "memo.md").is_file()


def test_sanctions_stage_failure_keeps_the_case_directory_and_exits_one(
    screen_env: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure posture end to end: a sanctions endpoint that 401s does not
    abort the run. The case directory is written with everything gathered,
    SAN-999 records the failure, and the CLI exits 1 naming the stage."""
    mock_company_routes(respx_mock)
    monkeypatch.setenv("OPENSANCTIONS_API_KEY", "fixture-opensanctions-key-not-real")
    respx_mock.post("https://api.opensanctions.org/match/default").respond(401)
    result = runner.invoke(app, ["screen", "99999999"])
    assert result.exit_code == 1
    combined = all_output(result)
    assert "sanctions screening stage FAILED after retries" in combined
    assert "Traceback" not in combined
    case_dir = screen_env / "cases" / "fabricated-widgets-ltd-99999999"
    assert (case_dir / "memo.md").is_file()
    assert (case_dir / "evidence" / "sanctions_failed.json").is_file()
    casefile = CaseFile.model_validate_json(
        (case_dir / "casefile.json").read_text(encoding="utf-8")
    )
    assert casefile.sanctions is not None
    assert casefile.sanctions.failed is True
    assert any(f.id == "SAN-999" for f in casefile.findings)
    memo = (case_dir / "memo.md").read_text(encoding="utf-8")
    assert "Attempted and FAILED" in memo
    assert "SAN-999" in memo


def test_media_stage_failure_keeps_gathered_results_and_exits_one(
    screen_env: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The media stage fails on the second query: the first query's results
    are kept, MED-999 records the failure, the run exits 1."""
    mock_company_routes(respx_mock)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-fixture-key-not-real")
    tavily = respx_mock.post("https://api.tavily.com/search")
    tavily.side_effect = [
        httpx.Response(
            200,
            json={
                "query": "q1",
                "results": [
                    {
                        "title": "A fictional story",
                        "url": "https://fictional-gazette.example/story",
                        "content": "Fictional content.",
                        "published_date": "2026-05-01",
                    }
                ],
            },
        ),
        httpx.Response(401),
    ]
    result = runner.invoke(app, ["screen", "99999999"])
    assert result.exit_code == 1
    combined = all_output(result)
    assert "adverse media search stage FAILED after retries" in combined
    case_dir = screen_env / "cases" / "fabricated-widgets-ltd-99999999"
    casefile = CaseFile.model_validate_json(
        (case_dir / "casefile.json").read_text(encoding="utf-8")
    )
    assert casefile.media is not None
    assert casefile.media.failed is True
    assert len(casefile.media.items) == 1  # gathered before the failure, kept
    assert any(f.id == "MED-999" for f in casefile.findings)
    memo = (case_dir / "memo.md").read_text(encoding="utf-8")
    assert "MED-999" in memo
    assert "1 result(s) gathered before the failure" in memo


def test_stage_failure_still_runs_synthesis_over_what_exists(
    screen_env: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Synthesis proceeds over the partial casefile; the exit code stays 1
    because a stage failed, even though synthesis itself succeeded."""
    mock_company_routes(respx_mock)
    monkeypatch.setenv("OPENSANCTIONS_API_KEY", "fixture-opensanctions-key-not-real")
    monkeypatch.setenv("COLDSCREEN_MODEL", "ollama:fake-model:1b")
    respx_mock.post("https://api.opensanctions.org/match/default").respond(401)
    respx_mock.post(OLLAMA_CHAT_URL).respond(
        200, json=ollama_reply(neutral_synthesis_json("green"))
    )
    result = runner.invoke(app, ["screen", "99999999"])
    assert result.exit_code == 1
    case_dir = screen_env / "cases" / "fabricated-widgets-ltd-99999999"
    casefile = CaseFile.model_validate_json(
        (case_dir / "casefile.json").read_text(encoding="utf-8")
    )
    assert casefile.verdict is not None  # synthesis ran over what exists
    assert casefile.verdict.level == "amber"  # A1 and A2 candidates enforced


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
    missing = screen_env / "no-such-coldscreen.toml"
    result = runner.invoke(app, ["screen", "Fabricated Widgets Ltd", "--config", str(missing)])
    # typer reports option validation as a usage error, exit code 2.
    assert result.exit_code == 2
    assert "Invalid value for '--config'" in flat_output(result)


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
    monkeypatch.setenv("COLDSCREEN_MODEL", "ollama:fake-model:1b")
    route = respx_mock.post(OLLAMA_CHAT_URL).respond(
        200, json=ollama_reply(neutral_synthesis_json("green"))
    )
    result = runner.invoke(app, ["screen", "Fabricated Widgets Ltd"])
    assert result.exit_code == 0, result.output
    # First-colon split: the model name keeps its own colon.
    sent = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert sent["model"] == "fake-model:1b"
    assert sent["stream"] is False
    assert sent["options"] == {"temperature": 0, "num_ctx": 16384}
    assert sent["format"]["type"] == "object"  # bare JSON schema, no envelope
    assert "think" not in sent  # unset by default, so the field is never sent

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


def test_ollama_think_setting_reaches_the_request_body(
    screen_env: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Environment to request body, the whole chain in one pass.

    Reasoning-family local models need think false to produce usable output
    under a schema, so the setting has to survive every hop.
    """
    mock_company_routes(respx_mock)
    monkeypatch.setenv("COLDSCREEN_MODEL", "ollama:fake-model:1b")
    monkeypatch.setenv("COLDSCREEN_OLLAMA_THINK", "false")
    route = respx_mock.post(OLLAMA_CHAT_URL).respond(
        200, json=ollama_reply(neutral_synthesis_json("green"))
    )
    result = runner.invoke(app, ["screen", "Fabricated Widgets Ltd"])
    assert result.exit_code == 0, result.output
    sent = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert sent["think"] is False


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
    monkeypatch.setenv("COLDSCREEN_MODEL", "ollama:fake-model:1b")
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
        200, json=ollama_reply(neutral_synthesis_json("green"))
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


# -- weekend 3: --deck and --site ----------------------------------------------

DECK_PATH = FIXTURES_DIR / "deck_fabricated_widgets.pdf"


def test_deck_without_model_is_a_clean_error_before_any_fetching(
    screen_env: Path, respx_mock: respx.MockRouter
) -> None:
    search_route = respx_mock.get("https://api.company-information.service.gov.uk/search/companies")
    result = runner.invoke(app, ["screen", "99999999", "--deck", str(DECK_PATH)])
    assert result.exit_code == 1
    combined = all_output(result)
    assert "needs a model" in combined
    assert search_route.call_count == 0
    assert not (screen_env / "cases").exists()


def test_site_without_model_is_a_clean_error(
    screen_env: Path, respx_mock: respx.MockRouter
) -> None:
    result = runner.invoke(app, ["screen", "99999999", "--site", "https://widgets.example"])
    assert result.exit_code == 1
    assert "needs a model" in all_output(result)


def test_missing_deck_path_is_a_usage_error(screen_env: Path, respx_mock: respx.MockRouter) -> None:
    missing = screen_env / "no-such-deck.pdf"
    result = runner.invoke(app, ["screen", "99999999", "--deck", str(missing)])
    assert result.exit_code == 2
    assert "Invalid value for '--deck'" in flat_output(result)


def test_non_pdf_deck_is_a_clean_error_before_any_fetching(
    screen_env: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLDSCREEN_MODEL", "ollama:fake-model:1b")
    not_a_pdf = screen_env / "notes.pdf"
    not_a_pdf.write_text("this is plain text wearing a pdf extension", encoding="utf-8")
    # No registry routes registered: reaching the network would fail loudly.
    result = runner.invoke(app, ["screen", "99999999", "--deck", str(not_a_pdf)])
    assert result.exit_code == 1
    combined = all_output(result)
    assert "not a readable PDF" in combined
    assert "Traceback" not in combined
    assert not (screen_env / "cases").exists()


def test_encrypted_deck_is_a_clean_error(
    screen_env: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLDSCREEN_MODEL", "ollama:fake-model:1b")
    # A structurally valid PDF whose trailer declares an encryption
    # dictionary: pdfminer treats it as encrypted and unreadable.
    encrypted = screen_env / "locked.pdf"
    encrypted.write_bytes(
        DECK_PATH.read_bytes().replace(b"/Root 1 0 R", b"/Root 1 0 R /Encrypt 99 0 R")
    )
    result = runner.invoke(app, ["screen", "99999999", "--deck", str(encrypted)])
    assert result.exit_code == 1
    combined = all_output(result)
    assert "encrypted" in combined
    assert "Traceback" not in combined
    assert not (screen_env / "cases").exists()


def test_non_http_site_url_is_a_clean_error(
    screen_env: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLDSCREEN_MODEL", "ollama:fake-model:1b")
    result = runner.invoke(app, ["screen", "99999999", "--site", "ftp://widgets.example"])
    assert result.exit_code == 1
    combined = all_output(result)
    assert "http or https" in combined
    assert not (screen_env / "cases").exists()


def test_claims_extraction_failure_keeps_the_audit_pack_and_skips_synthesis(
    screen_env: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model that never produces valid claims: the run exits 1, the deck
    evidence is kept, no synthesis call is made, and nothing is fabricated."""
    mock_company_routes(respx_mock)
    monkeypatch.setenv("COLDSCREEN_MODEL", "ollama:fake-model:1b")
    route = respx_mock.post(OLLAMA_CHAT_URL).respond(200, json=ollama_reply("junk, not json"))
    result = runner.invoke(app, ["screen", "99999999", "--deck", str(DECK_PATH)])
    assert result.exit_code == 1
    combined = all_output(result)
    assert "claims extraction failed" in combined
    assert "Traceback" not in combined
    # Three attempts for claims extraction, zero for synthesis.
    assert route.call_count == 3
    case_dir = screen_env / "cases" / "fabricated-widgets-ltd-99999999"
    assert (case_dir / "evidence" / "deck_text.json").is_file()
    casefile = CaseFile.model_validate_json(
        (case_dir / "casefile.json").read_text(encoding="utf-8")
    )
    assert casefile.claims == []
    assert casefile.verdict is None
    memo = (case_dir / "memo.md").read_text(encoding="utf-8")
    assert "Synthesis was attempted and failed" in memo
    assert "claims extraction failed" in memo


def test_screen_with_deck_extracts_claims_and_assesses_them(
    screen_env: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full weekend 3 path over the CLI: extraction call, synthesis call,
    claims stored with code-assigned ids, table rendered."""
    mock_company_routes(respx_mock)
    monkeypatch.setenv("COLDSCREEN_MODEL", "ollama:fake-model:1b")
    claims_reply = json.dumps(
        {
            "claims": [
                {
                    "text": "Operating since 2015 with a national footprint.",
                    "source": "deck p.2",
                    "category": "history",
                    "checkable": True,
                }
            ]
        }
    )
    synthesis_reply = synthesis_json(
        "red",
        ["R4", "A1", "A2"],
        assessments=[
            {
                "claim_id": "CLM-001",
                "status": "contradicted",
                "basis_finding_ids": ["REG-002"],
                "record_note": "Incorporated on 2019-05-14 per the registry profile.",
            }
        ],
    )
    route = respx_mock.post(OLLAMA_CHAT_URL).mock(
        side_effect=[
            httpx.Response(200, json=ollama_reply(claims_reply)),
            httpx.Response(200, json=ollama_reply(synthesis_reply)),
        ]
    )
    result = runner.invoke(app, ["screen", "99999999", "--deck", str(DECK_PATH)])
    assert result.exit_code == 0, result.output
    assert route.call_count == 2
    # The first call carried the claims schema with the source enum; the
    # second carried the synthesis schema with the claim id enum.
    first = json.loads(route.calls[0].request.content.decode("utf-8"))
    assert first["format"]["properties"]["claims"]["items"]["properties"]["source"]["enum"] == [
        "deck p.1",
        "deck p.2",
        "deck p.3",
    ]
    second = json.loads(route.calls[1].request.content.decode("utf-8"))
    assert second["format"]["properties"]["assessments"]["items"]["properties"]["claim_id"][
        "enum"
    ] == ["CLM-001"]
    case_dir = screen_env / "cases" / "fabricated-widgets-ltd-99999999"
    casefile = CaseFile.model_validate_json(
        (case_dir / "casefile.json").read_text(encoding="utf-8")
    )
    assert [c.id for c in casefile.claims] == ["CLM-001"]
    assert casefile.verdict is not None and casefile.verdict.level == "red"
    memo = (case_dir / "memo.md").read_text(encoding="utf-8")
    assert "## Claims vs evidence" in memo
    assert "| Contradicted |" in memo


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
    respx_mock.post(OLLAMA_CHAT_URL).respond(
        200, json=ollama_reply(neutral_synthesis_json("green"))
    )
    result = runner.invoke(app, ["rerun", str(case_dir), "--model", "ollama:fake-model:1b"])
    assert result.exit_code == 1
    assert "language backstop" in all_output(result)
    # The backstop fires before ANY write: the (poisoned) casefile and the
    # old memo are byte-identical to what was there before.
    assert (case_dir / "casefile.json").read_text(encoding="utf-8") == poisoned
    assert (case_dir / "memo.md").read_bytes() == before_memo


def _mock_routes_with_profile(respx_mock: respx.MockRouter, profile: dict[str, object]) -> None:
    base = "https://api.company-information.service.gov.uk"
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


def test_registered_name_with_banned_word_screens_honestly(
    screen_env: Path, respx_mock: respx.MockRouter
) -> None:
    """The registry identity exemption: a company whose REGISTERED name
    contains a banned word can be screened. The name renders verbatim, the
    memo passes the backstop, and the CI language scan re-verifies the name
    against the persisted registry evidence and agrees."""
    import importlib.util

    profile = load_fixture("profile.json")
    profile["company_name"] = "TOTAL SHAM TRADING LTD"
    _mock_routes_with_profile(respx_mock, profile)
    result = runner.invoke(app, ["screen", "99999999"])
    assert result.exit_code == 0, result.output
    case_dir = screen_env / "cases" / "total-sham-trading-ltd-99999999"
    memo = (case_dir / "memo.md").read_text(encoding="utf-8")
    assert "TOTAL SHAM TRADING LTD" in memo
    # The word appears ONLY inside the registered name's exact spans.
    assert memo.lower().count("sham") == memo.count("TOTAL SHAM TRADING LTD")
    # The CI scan agrees, via re-verification against the evidence files.
    spec = importlib.util.spec_from_file_location(
        "check_language", Path(__file__).parent.parent / "scripts" / "check_language.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main([str(case_dir / "memo.md")]) == 0


def test_language_backstop_blocks_a_poisoned_memo_on_screen(
    screen_env: Path, respx_mock: respx.MockRouter
) -> None:
    """A banned word arriving through a NON-identity source channel (the
    registered office address) still trips the backstop on the screen path:
    no case directory at all. The identity exemption covers names only."""
    profile = load_fixture("profile.json")
    profile["registered_office_address"] = {
        "address_line_1": "1 Scam Passage",
        "locality": "Faketown",
    }
    _mock_routes_with_profile(respx_mock, profile)
    result = runner.invoke(app, ["screen", "99999999"])
    assert result.exit_code == 1
    combined = all_output(result)
    assert "language backstop" in combined
    assert "scam" not in combined.lower()
    assert not (screen_env / "cases").exists()


def test_language_gate_exhaustion_memo_survives_the_backstop(
    screen_env: Path, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two gates must not collide. The model returns banned vocabulary on
    the first attempt AND on the corrective retry, so synthesis gives up. Its
    message is rendered into the failure memo, so it reports a count and never
    the vocabulary: the backstop passes the memo and the run keeps its audit
    pack instead of losing everything to a second gate hit."""
    mock_company_routes(respx_mock)
    monkeypatch.setenv("COLDSCREEN_MODEL", "ollama:fake-model:1b")
    dirty = synthesis_json("green", narrative="The filing pattern reads like a scam.")
    route = respx_mock.post(OLLAMA_CHAT_URL).respond(200, json=ollama_reply(dirty))
    result = runner.invoke(app, ["screen", "Fabricated Widgets Ltd"])

    assert result.exit_code == 1
    assert route.call_count == 2  # the first attempt plus one corrective retry
    combined = all_output(result)
    assert "Synthesis failed" in combined
    assert "1 banned term(s)" in combined
    assert "language backstop" not in combined
    assert find_banned_terms(combined) == []
    assert "Traceback" not in combined

    case_dir = screen_env / "cases" / "fabricated-widgets-ltd-99999999"
    assert (case_dir / "casefile.json").is_file()
    assert (case_dir / "evidence" / "index.json").is_file()
    memo = (case_dir / "memo.md").read_text(encoding="utf-8")
    assert "Synthesis was attempted and failed" in memo
    assert "1 banned term(s)" in memo
    assert find_banned_terms(memo) == []
    casefile = CaseFile.model_validate_json(
        (case_dir / "casefile.json").read_text(encoding="utf-8")
    )
    assert casefile.verdict is None
