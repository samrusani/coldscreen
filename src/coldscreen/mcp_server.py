"""MCP server (stdio only) over the screening pipeline.

Optional module: it needs the `mcp` extra (`pip install 'coldscreen[mcp]'`).
`coldscreen.cli` imports it lazily so a default install still works.

Coded against the installed mcp 2.0.0 package, not from memory: the server
object is `mcp.server.MCPServer`, tools register with `@server.tool()`,
`server.run("stdio")` is synchronous, and a tool that raises comes back to
the host as a `CallToolResult` with `is_error` set and the exception message
as text. Result field names on the installed package are snake_case
(`is_error`, `structured_content`, `input_schema`); the wire format keeps the
camelCase aliases.

Two rules this module exists to hold:

- Secrets are never tool arguments. Every key (Companies House,
  OpenSanctions, Tavily, model providers) is read from the server process
  environment, which the host sets in its own configuration. No tool schema
  has a field for one.
- Ambiguity is never resolved silently. A name matching several companies
  comes back as an "ambiguous" payload listing the candidates; the host has
  to choose and call again with `company_number`.

stdout belongs to JSON-RPC. Everything human goes to stderr, which the CLI
command handles.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__
from .config import API_KEY_ENV, Settings, api_key_from_env, load_settings
from .models import CaseFile, CompanyCandidate
from .pipeline import (
    MCP_ARGUMENTS,
    PreparedProvider,
    close_provider,
    load_case,
    prepare_provider,
    run_rerun,
    run_screen,
)
from .providers import ProviderError

SERVER_NAME = "coldscreen"

SERVER_INSTRUCTIONS = (
    "First-pass screening memos for UK companies, built only from public"
    " sources with every finding tied to evidence. screen_company runs the"
    " full pipeline and writes a case directory unless no_write is true;"
    " rerun_case re-synthesizes from a case directory that already exists"
    " without refetching anything."
    " API keys come from this server's environment and are never tool"
    " arguments. A company name that matches several companies is returned as"
    " a candidate list, never resolved for you: pick one and call again with"
    " company_number. Verdicts are a research aid, not due diligence and not"
    " a consumer report."
)


def _settings_or_fail() -> Settings:
    try:
        return load_settings()
    except ValueError as error:
        raise ToolError(f"Configuration error: {error}") from None


def _api_key_or_fail() -> str:
    key = api_key_from_env()
    if key is None:
        raise ToolError(
            f"{API_KEY_ENV} is not set in this server's environment. Companies"
            " House keys are never tool arguments: add it to the env block for"
            " this server in your host configuration and restart the server."
            " A free key comes from the Companies House developer hub."
        )
    return key


def _provider_or_fail(settings: Settings, model: str | None) -> PreparedProvider | None:
    try:
        return prepare_provider(settings, model)
    except ProviderError as error:
        raise ToolError(f"Model configuration error: {error}") from None


def _deck_path_or_fail(raw: str) -> Path:
    """The same checks `--deck` gets from Typer: exists, is a file, readable.

    Deliberately NOT confined to the output directory: a pitch deck is not
    stored there, so confining it would refuse every real deck. The residual
    is written down in PRIVACY.md rather than left implicit: a host that can
    name a local PDF causes that file's extractable text to be persisted
    into the case directory and returned in the memo.
    """
    path = Path(raw).expanduser()
    if not path.exists():
        raise ToolError(f"No such deck file: {path}")
    if not path.is_file():
        raise ToolError(f"deck_path is not a file: {path}")
    if not os.access(path, os.R_OK):
        raise ToolError(f"The deck file is not readable: {path}")
    return path


def _case_dir_or_fail(raw: str, settings: Settings) -> Path:
    """Resolve a host-supplied case directory inside the configured output dir.

    On the MCP path the path comes from a host, not from a person at a
    terminal, and rerun writes memo.md and casefile.json into it. Confining
    it to the output directory keeps that from becoming a write primitive
    aimed anywhere on the machine. The CLI, where a person types the path,
    is unchanged.

    Confining the directory is necessary but not sufficient: the writes
    themselves refuse to follow a symbolic link at memo.md or casefile.json
    (see `coldscreen.casedir.write_case_text`), because a link at one of
    those names would otherwise carry the write outside this root.
    """
    root = Path(settings.output_dir).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    root = root.resolve()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ToolError(
            f"case_dir must be inside the configured output directory ({root})."
            " Point COLDSCREEN_OUTPUT_DIR at the directory holding your cases,"
            " or pass a case directory under it."
        )
    if not resolved.is_dir():
        raise ToolError(f"No such case directory: {resolved}")
    return resolved


def _verdict_payload(casefile: CaseFile) -> dict[str, Any]:
    """Verdict level and enforced trigger ids, or an explicit not-run note.

    The level is the enforced level from the casefile, never a level the
    model asked for: enforcement already ran.
    """
    if casefile.verdict is None:
        return {"level": None, "triggered": [], "status": "synthesis not run"}
    return {
        "level": casefile.verdict.level.upper(),
        "triggered": list(casefile.verdict.triggered),
        "status": "enforced",
    }


def _case_payload(
    casefile: CaseFile,
    case_dir: Path | None,
    memo: str,
    status: str,
    notices: tuple[str, ...] = (),
    stage_failures: tuple[tuple[str, str], ...] = (),
    synthesis_failure: str | None = None,
) -> dict[str, Any]:
    """The structured content both tools return.

    `notices` carries what the CLI prints to stderr: things that are not
    errors but are still the caller's business, above all a name tie that
    was resolved by exact title match while other candidates existed. A
    resolved tie is still a choice, so the host has to be able to see it.

    Deliberately not included: raw evidence bodies, cache contents, and
    anything key-shaped. The memo is the already language-gated memo. On
    the default path it was written to disk; on a no-write screen it is
    the same memo held in memory and `case_dir` is JSON null.
    """
    return {
        "status": status,
        "company_name": casefile.subject.company_name,
        "company_number": casefile.subject.company_number,
        "verdict": _verdict_payload(casefile),
        "case_dir": str(case_dir) if case_dir is not None else None,
        "memo": memo,
        "notices": list(notices),
        "stage_failures": [{"stage": stage, "reason": reason} for stage, reason in stage_failures],
        "synthesis_failure": synthesis_failure,
        "tool_version": __version__,
    }


def _candidate_payload(candidate: CompanyCandidate) -> dict[str, Any]:
    return {
        "company_name": candidate.title,
        "company_number": candidate.company_number,
        "company_status": candidate.company_status,
        "date_of_creation": (
            candidate.date_of_creation.isoformat() if candidate.date_of_creation else None
        ),
        "address_snippet": candidate.address_snippet,
    }


def build_server() -> MCPServer:
    """Build the stdio MCP server with exactly two tools."""
    server: MCPServer = MCPServer(
        SERVER_NAME,
        title="coldscreen",
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
    )

    @server.tool()
    def screen_company(
        query: str,
        company_number: str | None = None,
        deck_path: str | None = None,
        site_url: str | None = None,
        model: str | None = None,
        overwrite: bool = False,
        refresh: bool = False,
        no_write: bool = False,
    ) -> dict[str, Any]:
        """Screen one UK company from public sources and write a case directory.

        query: company name, or a company number such as 01234567.
        company_number: the company to screen. Pass this after an "ambiguous"
        result to say which candidate you meant; query is then only a label.
        deck_path: path to a local pitch deck PDF for claims extraction.
        site_url: the company's http or https website for claims extraction.
        model: synthesis model as "provider:model", for example
        ollama:qwen3-coder:30b. Without a model the screen is deterministic
        and the memo says so. deck_path and site_url need one.
        overwrite: replace an existing case directory for this company.
        refresh: skip HTTP cache reads for this screen and still write
        successful 200 responses back into the cache.
        no_write: do not create a case directory. The payload still carries
        the memo and verdict; case_dir is JSON null. The HTTP cache is
        unchanged. An existing case directory is not a conflict.

        Returns the company name and number, the enforced verdict level with
        its rubric trigger ids (or a "synthesis not run" note), the case
        directory path (or null when no_write), the memo markdown, and any
        notices (for example a name tie resolved by exact title match, which
        you should check). A name matching several companies returns status
        "ambiguous" with the candidate list and writes nothing. API keys are
        read from this server's environment and are never accepted as
        arguments.
        """
        settings = _settings_or_fail()
        api_key = _api_key_or_fail()
        deck = _deck_path_or_fail(deck_path) if deck_path else None
        prepared = _provider_or_fail(settings, model)
        try:
            result = run_screen(
                query=query,
                settings=settings,
                api_key=api_key,
                prepared=prepared,
                company_number=company_number,
                deck=deck,
                site=site_url,
                overwrite=overwrite,
                refresh=refresh,
                no_write=no_write,
                chooser=None,
                arguments=MCP_ARGUMENTS,
            )
        except OSError as error:
            raise ToolError(
                f"The screen could not complete a filesystem operation under"
                f" {settings.output_dir}: {error.strerror or type(error).__name__}."
                " Check the output directory's permissions and free space."
            ) from None
        finally:
            close_provider(prepared)

        if result.status == "ambiguous":
            return {
                "status": "ambiguous",
                "message": result.message,
                "candidates": [_candidate_payload(c) for c in result.candidates],
            }
        if result.status != "ok" or result.casefile is None:
            raise ToolError(result.message or "The screen failed.")
        if result.case_dir is None and not no_write:
            raise ToolError(result.message or "The screen failed.")

        status = "ok"
        if result.stage_failures or result.synthesis_failure is not None:
            status = "completed_with_failures"
        written = result.case_dir.resolve() if result.case_dir is not None else None
        return _case_payload(
            result.casefile,
            written,
            result.memo or "",
            status,
            notices=result.notices,
            stage_failures=result.stage_failures,
            synthesis_failure=result.synthesis_failure,
        )

    @server.tool()
    def rerun_case(
        case_dir: str,
        model: str | None = None,
        render_only: bool = False,
    ) -> dict[str, Any]:
        """Re-synthesize and re-render a case directory that already exists.

        Nothing is refetched and no claim is re-extracted: synthesis
        re-assesses the claims already stored in the casefile. Use this to
        compare models or to recover a case whose synthesis failed.

        case_dir: an existing case directory, inside the configured output
        directory.
        model: synthesis model as "provider:model". Without one, only the
        memo is re-rendered.
        render_only: re-render the memo from the stored casefile with no
        synthesis at all.

        Returns the same shape as screen_company. API keys are read from this
        server's environment and are never accepted as arguments.
        """
        settings = _settings_or_fail()
        resolved_dir = _case_dir_or_fail(case_dir, settings)
        loaded = load_case(resolved_dir)
        if loaded.status != "ok" or loaded.casefile is None:
            raise ToolError(loaded.message or "The case directory could not be loaded.")

        prepared = None if render_only else _provider_or_fail(settings, model)
        try:
            result = run_rerun(case_dir=resolved_dir, casefile=loaded.casefile, prepared=prepared)
        except OSError as error:
            raise ToolError(
                f"Could not write the memo in {resolved_dir}:"
                f" {error.strerror or type(error).__name__}. Check the case"
                " directory's permissions and free space."
            ) from None
        finally:
            close_provider(prepared)

        if result.status != "ok" or result.casefile is None:
            raise ToolError(result.message or "The rerun failed.")
        return _case_payload(
            result.casefile,
            resolved_dir,
            result.memo or "",
            "ok" if result.synthesized else "rendered_only",
        )

    return server
