"""MCP stdio server behaviour, through the SDK's in-memory client.

No subprocess, no port, no socket. pytest-socket stays fully armed here:
one test below asserts that socket creation is still blocked while an
in-memory client session is open, so this file cannot quietly become the
place the network isolation gate is relaxed.

The one accommodation is the event loop. asyncio's selector event loop builds
its self-pipe from an AF_UNIX socketpair at construction time, and
pytest-socket's guard refuses AF_UNIX as well as AF_INET. The guard is
installed in pytest_runtest_setup, which runs after collection, so the loop
is constructed once at import time and reused. Nothing inside a test opens a
socket: the in-memory transport is a pair of anyio memory object streams.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import shutil
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, TypeVar

import pytest
import respx

from coldscreen.config import API_KEY_ENV

from .conftest import FIXTURES_DIR, load_fixture, mock_company_routes
from .fakes import FakeModelProvider, synthesis_json

pytest.importorskip("mcp", reason="MCP server tests need the optional mcp extra")

from mcp import Client  # noqa: E402
from mcp.types import CallToolResult  # noqa: E402

import coldscreen.mcp_server  # noqa: E402

_LOOP = asyncio.new_event_loop()
atexit.register(_LOOP.close)

T = TypeVar("T")

SECRET = "fixture-key-0123456789-not-a-real-key"
FIXTURE_CASE_DIR = FIXTURES_DIR / "case-fabricated-widgets-ltd-99999999"
EXPECTED_MEMO = FIXTURE_CASE_DIR / "memo.md"
DECK_PATH = FIXTURES_DIR / "deck_fabricated_widgets.pdf"

# Field-name fragments that would mean a key travels as a tool argument.
KEY_SHAPED = (
    "api_key",
    "apikey",
    "companies_house",
    "opensanctions",
    "tavily",
    "openai",
    "anthropic",
    "secret",
    "token",
    "password",
)


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    return _LOOP.run_until_complete(coro)


def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    """One tool call against a fresh in-memory server session."""

    async def go() -> CallToolResult:
        async with Client(coldscreen.mcp_server.build_server()) as client:
            return await client.call_tool(name, arguments)

    return run_async(go())


def error_text(result: CallToolResult) -> str:
    """Flattened text of a tool result, without assuming a content type."""
    parts: list[str] = []
    for block in result.content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def structured(result: CallToolResult) -> dict[str, Any]:
    assert result.is_error is False, error_text(result)
    payload = result.structured_content
    assert isinstance(payload, dict)
    return payload


@pytest.fixture
def mcp_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolated working directory with a frozen clock and a fixture key."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(API_KEY_ENV, SECRET)
    monkeypatch.setenv("COLDSCREEN_SCREENED_AT", "2026-08-18T12:00:00+00:00")
    monkeypatch.setenv("COLDSCREEN_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path


@pytest.fixture
def canned_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """A canned fake provider in place of any real model.

    The reply says green with no triggers; the fixture company carries A1
    and A2 candidates, so enforcement has to correct it to amber. No HTTP,
    no Ollama, no cloud SDK.
    """
    monkeypatch.setattr(
        coldscreen.mcp_server,
        "prepare_provider",
        lambda settings, model: (
            FakeModelProvider([synthesis_json("green")]),
            "fake",
            "canned",
        ),
    )


# -- the tool surface ---------------------------------------------------------


def test_both_tools_are_listed() -> None:
    async def go() -> dict[str, Any]:
        async with Client(coldscreen.mcp_server.build_server()) as client:
            listed = await client.list_tools()
            return {tool.name: tool.input_schema for tool in listed.tools}

    schemas = run_async(go())
    assert set(schemas) == {"screen_company", "rerun_case"}
    assert set(schemas["screen_company"]["properties"]) == {
        "query",
        "company_number",
        "deck_path",
        "site_url",
        "model",
        "overwrite",
        "refresh",
        "no_write",
    }
    assert set(schemas["rerun_case"]["properties"]) == {"case_dir", "model", "render_only"}


def test_no_tool_schema_can_carry_key_material() -> None:
    """Secrets come from the environment. No tool has a field for one."""

    async def go() -> list[str]:
        async with Client(coldscreen.mcp_server.build_server()) as client:
            listed = await client.list_tools()
            return [json.dumps(tool.input_schema).lower() for tool in listed.tools]

    for serialized in run_async(go()):
        for fragment in KEY_SHAPED:
            assert fragment not in serialized, f"{fragment} appears in a tool input schema"


@pytest.mark.filterwarnings("ignore:A test tried to use socket.socket")
def test_sockets_stay_blocked_during_an_in_memory_session() -> None:
    """The in-memory client needs no network, and the gate proves it."""
    import socket

    from pytest_socket import SocketBlockedError

    async def go() -> None:
        async with Client(coldscreen.mcp_server.build_server()) as client:
            await client.list_tools()
            with pytest.raises(SocketBlockedError):
                socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    run_async(go())


# -- screen_company -----------------------------------------------------------


def test_screen_company_writes_a_case_and_reports_no_synthesis(
    mcp_env: Path, respx_mock: respx.MockRouter
) -> None:
    mock_company_routes(respx_mock)
    payload = structured(call_tool("screen_company", {"query": "Fabricated Widgets Ltd"}))

    assert payload["status"] == "ok"
    assert payload["company_name"] == "FABRICATED WIDGETS LTD"
    assert payload["company_number"] == "99999999"
    assert payload["verdict"] == {"level": None, "triggered": [], "status": "synthesis not run"}

    case_dir = mcp_env / "cases" / "fabricated-widgets-ltd-99999999"
    assert Path(payload["case_dir"]) == case_dir.resolve()
    assert (case_dir / "casefile.json").is_file()
    assert (case_dir / "evidence" / "index.json").is_file()
    assert payload["memo"] == (case_dir / "memo.md").read_text(encoding="utf-8")
    assert "No synthesis: no model configured" in payload["memo"]
    # No raw evidence, no cache contents, no key material comes back.
    assert set(payload) == {
        "status",
        "company_name",
        "company_number",
        "verdict",
        "case_dir",
        "memo",
        "notices",
        "stage_failures",
        "synthesis_failure",
        "tool_version",
    }
    assert SECRET not in json.dumps(payload)


def test_screen_company_verdict_is_the_enforced_level(
    mcp_env: Path, respx_mock: respx.MockRouter, canned_model: None
) -> None:
    """The canned model says green; A1 and A2 are mechanical candidates, so
    the returned level is the enforced amber, not the model's ask."""
    mock_company_routes(respx_mock)
    payload = structured(call_tool("screen_company", {"query": "99999999", "model": "fake:canned"}))
    assert payload["verdict"]["status"] == "enforced"
    assert payload["verdict"]["level"] == "AMBER"
    assert {"A1", "A2"} <= set(payload["verdict"]["triggered"])
    assert "**AMBER**" in payload["memo"]
    assert payload["synthesis_failure"] is None


def test_an_ambiguous_name_returns_candidates_and_writes_nothing(
    mcp_env: Path, respx_mock: respx.MockRouter
) -> None:
    """No TTY picker, no silent pick: the host has to choose."""
    respx_mock.get("https://api.company-information.service.gov.uk/search/companies").respond(
        200, json=load_fixture("search_ambiguous.json")
    )
    payload = structured(call_tool("screen_company", {"query": "Fabricated Widgets"}))

    assert payload["status"] == "ambiguous"
    numbers = {candidate["company_number"] for candidate in payload["candidates"]}
    assert {"99999999", "99999998"} <= numbers
    for candidate in payload["candidates"]:
        assert candidate["company_name"]
        assert "company_status" in candidate
    assert "company number" in payload["message"]
    assert not (mcp_env / "cases").exists()


def test_an_exact_name_match_reports_the_candidates_it_set_aside(
    mcp_env: Path, respx_mock: respx.MockRouter
) -> None:
    """A resolved tie is still a choice, so the host has to be told.

    The search returns two plausible companies and the query matches one
    title exactly, so the screen proceeds. The CLI prints that notice to
    stderr; an MCP host can only see it if the payload carries it.
    """
    mock_company_routes(respx_mock, ambiguous_search=True)
    payload = structured(call_tool("screen_company", {"query": "Fabricated Widgets Ltd"}))

    assert payload["status"] == "ok"
    assert payload["company_number"] == "99999999"
    notices = payload["notices"]
    assert len(notices) == 1
    assert "exact name" in notices[0]
    assert "1 other candidate(s) were set aside" in notices[0]
    assert "company number" in notices[0]


def test_the_second_call_with_a_company_number_resolves_the_ambiguity(
    mcp_env: Path, respx_mock: respx.MockRouter
) -> None:
    """company_number is how a host answers an ambiguous result, and it
    skips search entirely."""
    mock_company_routes(respx_mock, ambiguous_search=True)
    search = respx_mock.get("https://api.company-information.service.gov.uk/search/companies")
    payload = structured(
        call_tool(
            "screen_company",
            {"query": "Fabricated Widgets", "company_number": "99999999"},
        )
    )
    assert payload["status"] == "ok"
    assert payload["company_number"] == "99999999"
    assert search.call_count == 0


def test_a_missing_companies_house_key_is_a_recoverable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    result = call_tool("screen_company", {"query": "Fabricated Widgets Ltd"})

    assert result.is_error is True
    text = error_text(result)
    assert API_KEY_ENV in text
    assert "never tool arguments" in text
    assert "Traceback" not in text
    assert not (tmp_path / "cases").exists()


def test_no_write_returns_null_case_dir_and_writes_nothing(
    mcp_env: Path, respx_mock: respx.MockRouter
) -> None:
    mock_company_routes(respx_mock)
    payload = structured(call_tool("screen_company", {"query": "99999999", "no_write": True}))

    assert payload["status"] == "ok"
    assert payload["company_number"] == "99999999"
    assert payload["case_dir"] is None
    assert payload["memo"]
    assert payload["verdict"] == {"level": None, "triggered": [], "status": "synthesis not run"}
    assert SECRET not in json.dumps(payload)
    assert not (mcp_env / "cases").exists()


def test_overwrite_false_against_an_existing_case_fails_closed(
    mcp_env: Path, respx_mock: respx.MockRouter
) -> None:
    mock_company_routes(respx_mock)
    first = structured(call_tool("screen_company", {"query": "99999999"}))
    case_dir = mcp_env / "cases" / "fabricated-widgets-ltd-99999999"
    marker = (case_dir / "memo.md").read_bytes()

    second = call_tool("screen_company", {"query": "99999999"})
    assert second.is_error is True
    text = error_text(second)
    assert "already exists" in text
    # The hint names the tool's own argument, not a command-line flag.
    assert "overwrite" in text
    assert "--overwrite" not in text
    assert (case_dir / "memo.md").read_bytes() == marker
    assert first["status"] == "ok"

    third = structured(call_tool("screen_company", {"query": "99999999", "overwrite": True}))
    assert third["status"] == "ok"


def test_a_deck_without_a_model_fails_closed(mcp_env: Path, respx_mock: respx.MockRouter) -> None:
    """Claims extraction is model work. Same posture as the CLI, and it
    fails before any fetching."""
    search = respx_mock.get("https://api.company-information.service.gov.uk/search/companies")
    result = call_tool("screen_company", {"query": "99999999", "deck_path": str(DECK_PATH)})

    assert result.is_error is True
    text = error_text(result)
    assert "needs a model" in text
    assert "deck_path" in text
    assert search.call_count == 0
    assert not (mcp_env / "cases").exists()


def test_a_deck_path_that_does_not_exist_is_a_clean_error(
    mcp_env: Path, respx_mock: respx.MockRouter, canned_model: None
) -> None:
    """deck_path gets the checks Typer gives --deck: exists, file, readable."""
    search = respx_mock.get("https://api.company-information.service.gov.uk/search/companies")
    result = call_tool(
        "screen_company",
        {"query": "99999999", "deck_path": str(mcp_env / "nowhere.pdf"), "model": "fake:canned"},
    )
    assert result.is_error is True
    text = error_text(result)
    assert "No such deck file" in text
    assert "Traceback" not in text
    assert search.call_count == 0
    assert not (mcp_env / "cases").exists()


def test_a_deck_path_that_is_a_directory_is_refused(
    mcp_env: Path, respx_mock: respx.MockRouter, canned_model: None
) -> None:
    folder = mcp_env / "decks"
    folder.mkdir()
    result = call_tool(
        "screen_company",
        {"query": "99999999", "deck_path": str(folder), "model": "fake:canned"},
    )
    assert result.is_error is True
    assert "not a file" in error_text(result)
    assert not (mcp_env / "cases").exists()


def test_overwrite_refuses_a_symlinked_memo_and_leaves_the_victim_untouched(
    mcp_env: Path, respx_mock: respx.MockRouter
) -> None:
    """Confining the case directory does not confine the write.

    A link left at memo.md inside a perfectly legitimate case directory
    would carry the write wherever it points, because the kernel resolves
    that name again at open time. The write refuses the link instead.
    """
    mock_company_routes(respx_mock)
    structured(call_tool("screen_company", {"query": "99999999"}))
    case_dir = mcp_env / "cases" / "fabricated-widgets-ltd-99999999"

    victim = mcp_env / "elsewhere" / "precious.txt"
    victim.parent.mkdir(parents=True)
    victim.write_text("do not touch\n", encoding="utf-8")
    (case_dir / "memo.md").unlink()
    (case_dir / "memo.md").symlink_to(victim)

    result = call_tool("screen_company", {"query": "99999999", "overwrite": True})
    assert result.is_error is True
    text = error_text(result)
    assert "symbolic link" in text
    assert "Traceback" not in text
    assert victim.read_text(encoding="utf-8") == "do not touch\n"


def test_overwrite_refuses_a_symlinked_evidence_directory(
    mcp_env: Path, respx_mock: respx.MockRouter
) -> None:
    """The overwrite path clears evidence/ before rewriting it. A link there
    must be refused, not emptied."""
    mock_company_routes(respx_mock)
    structured(call_tool("screen_company", {"query": "99999999"}))
    case_dir = mcp_env / "cases" / "fabricated-widgets-ltd-99999999"

    outside = mcp_env / "elsewhere"
    outside.mkdir()
    (outside / "keepme.txt").write_text("still here\n", encoding="utf-8")
    shutil.rmtree(case_dir / "evidence")
    (case_dir / "evidence").symlink_to(outside, target_is_directory=True)

    result = call_tool("screen_company", {"query": "99999999", "overwrite": True})
    assert result.is_error is True
    assert "symbolic link" in error_text(result)
    assert (outside / "keepme.txt").read_text(encoding="utf-8") == "still here\n"


def test_a_site_url_without_a_model_fails_closed(
    mcp_env: Path, respx_mock: respx.MockRouter
) -> None:
    result = call_tool(
        "screen_company", {"query": "99999999", "site_url": "https://widgets.example"}
    )
    assert result.is_error is True
    text = error_text(result)
    assert "needs a model" in text
    assert "site_url" in text
    assert not (mcp_env / "cases").exists()


def test_a_non_http_site_url_is_refused(
    mcp_env: Path, respx_mock: respx.MockRouter, canned_model: None
) -> None:
    result = call_tool(
        "screen_company",
        {"query": "99999999", "site_url": "ftp://widgets.example", "model": "fake:canned"},
    )
    assert result.is_error is True
    assert "http or https" in error_text(result)
    assert not (mcp_env / "cases").exists()


def test_a_bad_model_spec_is_a_clean_error_before_any_fetching(
    mcp_env: Path, respx_mock: respx.MockRouter
) -> None:
    search = respx_mock.get("https://api.company-information.service.gov.uk/search/companies")
    result = call_tool(
        "screen_company", {"query": "Fabricated Widgets Ltd", "model": "sorcery:crystal-ball"}
    )
    assert result.is_error is True
    assert "unknown provider" in error_text(result)
    assert search.call_count == 0


def test_an_out_of_range_setting_is_a_clean_configuration_error(
    mcp_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLDSCREEN_SANCTIONS_THRESHOLD", "1.5")
    result = call_tool("screen_company", {"query": "99999999"})
    assert result.is_error is True
    text = error_text(result)
    assert "Configuration error" in text
    assert "sanctions_threshold" in text
    assert "Traceback" not in text


def test_the_api_key_never_reaches_the_tool_result_or_disk(
    mcp_env: Path, respx_mock: respx.MockRouter
) -> None:
    mock_company_routes(respx_mock)
    result = call_tool("screen_company", {"query": "Fabricated Widgets Ltd"})
    assert SECRET not in json.dumps(result.model_dump(mode="json"))
    offenders = [
        path
        for path in mcp_env.rglob("*")
        if path.is_file() and SECRET.encode() in path.read_bytes()
    ]
    assert offenders == []


# -- rerun_case ---------------------------------------------------------------


@pytest.fixture
def staged_case(mcp_env: Path) -> Path:
    """The committed fabricated-widgets fixture, inside the output dir."""
    case_dir = mcp_env / "cases" / "case-fabricated-widgets-ltd-99999999"
    case_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE_CASE_DIR, case_dir)
    return case_dir


def test_rerun_case_render_only_matches_the_snapshot(
    staged_case: Path, respx_mock: respx.MockRouter
) -> None:
    # No routes are registered: any HTTP request would fail this test.
    (staged_case / "memo.md").unlink()
    payload = structured(
        call_tool("rerun_case", {"case_dir": str(staged_case), "render_only": True})
    )
    assert payload["status"] == "rendered_only"
    expected = EXPECTED_MEMO.read_text(encoding="utf-8")
    assert payload["memo"] == expected
    assert (staged_case / "memo.md").read_bytes() == EXPECTED_MEMO.read_bytes()
    assert payload["company_number"] == "99999999"
    # The committed fixture was screened with no model, so the rendered memo
    # and the payload both say so rather than inventing a level.
    assert payload["verdict"] == {"level": None, "triggered": [], "status": "synthesis not run"}


def test_rerun_case_with_a_model_resynthesizes_without_refetching(
    staged_case: Path, respx_mock: respx.MockRouter, canned_model: None
) -> None:
    # Only the fake provider exists. Any registry or search call would be
    # unmocked and fail: rerun must not refetch anything.
    payload = structured(
        call_tool("rerun_case", {"case_dir": str(staged_case), "model": "fake:canned"})
    )
    assert payload["status"] == "ok"
    assert payload["verdict"]["level"] == "AMBER"
    assert {"A1", "A2"} <= set(payload["verdict"]["triggered"])


def test_rerun_case_on_a_directory_without_a_casefile_is_a_clean_error(
    mcp_env: Path,
) -> None:
    empty = mcp_env / "cases" / "not-a-case"
    empty.mkdir(parents=True)
    result = call_tool("rerun_case", {"case_dir": str(empty)})
    assert result.is_error is True
    text = error_text(result)
    assert "casefile.json" in text
    assert "Traceback" not in text


def test_rerun_case_refuses_a_path_outside_the_output_directory(mcp_env: Path) -> None:
    """A host chooses this path, so it is confined to the output directory."""
    outside = mcp_env / "elsewhere" / "case-fabricated-widgets-ltd-99999999"
    outside.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE_CASE_DIR, outside)
    before = (outside / "memo.md").read_bytes()

    result = call_tool("rerun_case", {"case_dir": str(outside), "render_only": True})
    assert result.is_error is True
    text = error_text(result)
    assert "output directory" in text
    assert "COLDSCREEN_OUTPUT_DIR" in text
    assert (outside / "memo.md").read_bytes() == before


def test_rerun_case_refuses_a_traversal_out_of_the_output_directory(mcp_env: Path) -> None:
    (mcp_env / "cases").mkdir()
    outside = mcp_env / "elsewhere"
    outside.mkdir()
    result = call_tool("rerun_case", {"case_dir": "cases/../elsewhere", "render_only": True})
    assert result.is_error is True
    assert "output directory" in error_text(result)


def test_rerun_case_refuses_a_symlinked_memo_pointing_outside(staged_case: Path) -> None:
    """The confinement check resolves case_dir; this covers the write.

    memo.md inside a confined case directory is replaced by a link to a file
    outside the output directory. The rerun must refuse rather than follow
    it, and the victim file must be byte-identical afterwards.
    """
    victim = staged_case.parent.parent / "elsewhere" / "precious.txt"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"do not touch\n")
    (staged_case / "memo.md").unlink()
    (staged_case / "memo.md").symlink_to(victim)

    result = call_tool("rerun_case", {"case_dir": str(staged_case), "render_only": True})
    assert result.is_error is True
    text = error_text(result)
    assert "symbolic link" in text
    assert "Traceback" not in text
    assert victim.read_bytes() == b"do not touch\n"


def test_rerun_case_refuses_a_symlinked_casefile_before_writing_anything(
    staged_case: Path, canned_model: None
) -> None:
    """A synthesizing rerun writes casefile.json and then memo.md. A link at
    either name is refused before the other is touched, so a refusal cannot
    leave half a case behind."""
    victim = staged_case.parent.parent / "elsewhere" / "precious.json"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"{}\n")
    stored = (staged_case / "casefile.json").read_bytes()
    memo_before = (staged_case / "memo.md").read_bytes()
    (staged_case / "casefile.json").unlink()
    (staged_case / "casefile.json").symlink_to(victim)
    # Restore a readable casefile behind the link so loading succeeds and the
    # refusal has to come from the write, not from the load.
    victim.write_bytes(stored)

    result = call_tool("rerun_case", {"case_dir": str(staged_case), "model": "fake:canned"})
    assert result.is_error is True
    assert "symbolic link" in error_text(result)
    assert victim.read_bytes() == stored
    assert (staged_case / "memo.md").read_bytes() == memo_before


def test_the_language_backstop_still_guards_the_mcp_path(staged_case: Path) -> None:
    """Banned text forced through a non-gated channel never reaches disk or
    a tool result, and the error reports a count rather than the term."""
    data = json.loads((staged_case / "casefile.json").read_text(encoding="utf-8"))
    data["findings"][0]["statement"] += " Reads like a scam."
    (staged_case / "casefile.json").write_text(json.dumps(data), encoding="utf-8")
    before = (staged_case / "memo.md").read_bytes()

    result = call_tool("rerun_case", {"case_dir": str(staged_case), "render_only": True})
    assert result.is_error is True
    text = error_text(result)
    assert "language backstop" in text
    assert "1 banned term(s)" in text
    assert "scam" not in text.lower()
    assert (staged_case / "memo.md").read_bytes() == before
