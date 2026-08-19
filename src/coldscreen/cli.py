"""Typer CLI: `coldscreen screen`, `coldscreen rerun`, `coldscreen cache`, `coldscreen mcp`.

The flows themselves live in `coldscreen.pipeline`, which is interface-free
and returns structured results. This module is the adapter: it parses
options, loads settings, resolves the provider, calls the pipeline, and maps
the result to exit codes and to stdout or stderr. `coldscreen mcp` hands the
same pipeline to an MCP stdio server (optional extra). `coldscreen cache`
prints, stats, or clears the local HTTP cache and needs no API key.

screen resolves the input to one company, runs the deterministic stages
(registry, network expansion, sanctions, adverse media), extracts claims
from an optional deck PDF and site URL (model required for that), runs
synthesis when a model is configured, and writes the case directory. rerun
re-runs synthesis, including re-assessment of the STORED claims, from the
stored casefile with no refetching and no re-extraction, or just re-renders
with --render-only. screen --refresh skips HTTP cache reads and still writes
successful 200 responses back; rerun does not fetch and has no --refresh.
screen --no-write skips the case directory for that run and prints the
casefile JSON; the HTTP cache still stores 200s. rerun has no --no-write.

Stage failure posture: a sanctions or media stage that fails after retries
does not abort the run. The case directory is written with everything
gathered, the failure is a distinct finding, synthesis proceeds over what
exists, and the CLI exits 1 naming the failed stage. Absence is data;
failure is loudly recorded data.

Exit codes: 0 success, 1 error, 3 ambiguous company match that needs a
company number. Code 2 is left to typer for usage errors.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from . import __version__
from .config import API_KEY_ENV, Settings, api_key_from_env, load_dotenv_if_present, load_settings
from .http_cache import CacheClearRefused, clear_http_cache, http_cache_stats
from .pipeline import (
    PreparedProvider,
    close_provider,
    load_case,
    prepare_provider,
    run_rerun,
    run_screen,
)
from .providers import ProviderError
from .stages.resolve import Resolution

app = typer.Typer(
    name="coldscreen",
    help=(
        "First-pass screening memos for UK companies from public registry"
        " data. Exit codes: 0 success, 1 error, 3 ambiguous company match."
    ),
    add_completion=False,
    no_args_is_help=True,
)

cache_app = typer.Typer(
    name="cache",
    help="Inspect or clear the local HTTP cache. No API key required.",
    add_completion=False,
    no_args_is_help=True,
)
app.add_typer(cache_app, name="cache")

AMBIGUOUS_EXIT_CODE = 3

MODEL_OPTION_HELP = (
    'Synthesis model as "provider:model", for example anthropic:claude-opus-5,'
    " openai:gpt-5.6-sol, or ollama:qwen2.5:7b (the split is on the first"
    " colon). Overrides COLDSCREEN_MODEL and coldscreen.toml."
)

MCP_EXTRA_MISSING = (
    "MCP server mode needs the optional mcp extra, which is not installed."
    " Install it with: pip install 'coldscreen[mcp]' (from a checkout:"
    " pip install '.[mcp]')."
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"coldscreen {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Print the coldscreen version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """First-pass screening memos for UK companies from public registry data."""


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _candidate_lines(resolution: Resolution) -> list[str]:
    lines = []
    for index, candidate in enumerate(resolution.candidates, start=1):
        bits = [f"{index}. {candidate.title} ({candidate.company_number})"]
        if candidate.company_status:
            bits.append(f"status {candidate.company_status}")
        if candidate.date_of_creation:
            bits.append(f"incorporated {candidate.date_of_creation.isoformat()}")
        if candidate.address_snippet:
            bits.append(candidate.address_snippet)
        lines.append(" | ".join(bits))
    return lines


def _pick_candidate(resolution: Resolution) -> str | None:
    """Numbered picker on a terminal; candidate table plus a decline otherwise.

    Returning None declines: nothing is picked, the pipeline reports the
    ambiguity, and the command exits 3. This function owns all of the
    messaging for both paths, so the caller adds nothing.
    """
    lines = _candidate_lines(resolution)
    if not _is_interactive():
        typer.echo("Multiple plausible companies matched:", err=True)
        for line in lines:
            typer.echo(f"  {line}", err=True)
        typer.echo(
            "Not a terminal, so nothing was picked. Rerun with the company"
            " number, for example: coldscreen screen <number>",
            err=True,
        )
        return None
    typer.echo("Multiple plausible companies matched:")
    for line in lines:
        typer.echo(f"  {line}")
    choice = typer.prompt("Select a company by list position, or 0 to abort", type=int)
    if choice < 1 or choice > len(resolution.candidates):
        typer.echo("Aborted without selecting a company.", err=True)
        return None
    return resolution.candidates[choice - 1].company_number


def _load_settings_or_exit(
    config_file: Path | None, cli_overrides: dict[str, str | None] | None
) -> Settings:
    """Load settings; a bad value is a clean configuration error, exit 1."""
    try:
        return load_settings(config_file, cli_overrides)
    except ValueError as error:
        typer.echo(f"Configuration error: {error}", err=True)
        raise typer.Exit(code=1) from None


def _prepare_provider(settings: Settings, cli_model: str | None) -> PreparedProvider | None:
    """Adapter seam over pipeline.prepare_provider, kept so the CLI resolves
    the provider itself and the tests can substitute a canned one."""
    return prepare_provider(settings, cli_model)


def _close_provider(prepared: PreparedProvider | None) -> None:
    close_provider(prepared)


@app.command()
def screen(
    query: Annotated[
        str, typer.Argument(help="Company name, or a company number such as 01234567.")
    ],
    model: Annotated[
        str | None,
        typer.Option("--model", help=MODEL_OPTION_HELP),
    ] = None,
    deck: Annotated[
        Path | None,
        typer.Option(
            "--deck",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help=(
                "Path to the company's pitch deck PDF for claims extraction."
                " Must exist and be a file; requires a model."
            ),
        ),
    ] = None,
    site: Annotated[
        str | None,
        typer.Option(
            "--site",
            help=(
                "The company's website URL (http or https) for claims extraction. Requires a model."
            ),
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help=(
                "Print the casefile JSON to stdout in addition to writing the"
                " case directory. The status line moves to stderr so stdout"
                " stays pure JSON."
            ),
        ),
    ] = False,
    output_dir: Annotated[
        str | None,
        typer.Option("--output-dir", help="Directory that receives case directories."),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option(
            "--overwrite",
            help=(
                "Replace an existing case directory for this company. Without"
                " it, screening into an existing case directory is an error."
            ),
        ),
    ] = False,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help=(
                "Bypass the HTTP cache for this screen and write fresh 200"
                " responses back into it. A later unflagged screen then sees"
                " the new pages. Does not apply to rerun."
            ),
        ),
    ] = False,
    no_write: Annotated[
        bool,
        typer.Option(
            "--no-write",
            help=(
                "Do not create a case directory. Print the casefile JSON to"
                " stdout the same way --json does. The HTTP cache is"
                " unchanged. An existing case directory is not a conflict."
                " Does not apply to rerun."
            ),
        ),
    ] = False,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config",
            exists=True,
            file_okay=True,
            dir_okay=False,
            help="Path to a coldscreen.toml configuration file. Must exist.",
        ),
    ] = None,
) -> None:
    """Screen one company and, by default, write the case directory.

    Exit codes: 0 success, 1 error, 3 ambiguous match (pass a company number).
    """
    load_dotenv_if_present()
    settings = _load_settings_or_exit(config_file, {"output_dir": output_dir})
    api_key = api_key_from_env()
    if api_key is None:
        typer.echo(
            f"{API_KEY_ENV} is not set. Get a free key from the Companies House"
            " developer hub and export it as an environment variable.",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        prepared = _prepare_provider(settings, model)
    except ProviderError as error:
        typer.echo(f"Model configuration error: {error}", err=True)
        raise typer.Exit(code=1) from None

    try:
        result = run_screen(
            query=query,
            settings=settings,
            api_key=api_key,
            prepared=prepared,
            deck=deck,
            site=site,
            overwrite=overwrite,
            chooser=_pick_candidate,
            refresh=refresh,
            no_write=no_write,
        )
    finally:
        _close_provider(prepared)

    for notice in result.notices:
        typer.echo(notice, err=True)

    if result.status == "ambiguous":
        # _pick_candidate already said everything there is to say.
        raise typer.Exit(code=AMBIGUOUS_EXIT_CODE)
    if result.status != "ok":
        typer.echo(result.message, err=True)
        raise typer.Exit(code=1)

    emit_json = json_output or no_write
    if no_write:
        typer.echo("Case directory was not written.", err=True)
    else:
        typer.echo(f"Case directory written: {result.case_dir}", err=json_output)
    for stage_name, reason in result.stage_failures:
        findings_where = "casefile findings" if no_write else "case directory findings"
        typer.echo(
            f"The {stage_name} stage FAILED after retries: {reason}\n"
            "The run continued and the failure is recorded in the"
            f" {findings_where}.",
            err=True,
        )
    if result.synthesis_failure is not None:
        if no_write:
            typer.echo(
                f"Synthesis failed: {result.synthesis_failure}\n"
                "The deterministic memo was kept in memory. The case"
                " directory was not written, so there is nothing to rerun.",
                err=True,
            )
        else:
            typer.echo(
                f"Synthesis failed: {result.synthesis_failure}\n"
                "The deterministic memo and evidence were kept. Fix the model"
                " configuration and run: coldscreen rerun " + str(result.case_dir),
                err=True,
            )
    if emit_json and result.casefile is not None:
        typer.echo(result.casefile.model_dump_json(indent=2))
    if result.stage_failures or result.synthesis_failure is not None:
        raise typer.Exit(code=1)


@app.command()
def rerun(
    case_dir: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            help="An existing case directory containing casefile.json.",
        ),
    ],
    model: Annotated[
        str | None,
        typer.Option("--model", help=MODEL_OPTION_HELP),
    ] = None,
    render_only: Annotated[
        bool,
        typer.Option(
            "--render-only",
            help="Re-render memo.md from the stored casefile without any synthesis.",
        ),
    ] = False,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config",
            exists=True,
            file_okay=True,
            dir_okay=False,
            help="Path to a coldscreen.toml configuration file. Must exist.",
        ),
    ] = None,
) -> None:
    """Re-run synthesis from the stored casefile (no refetching) and re-render.

    Synthesis includes re-assessing the STORED claims; rerun never
    re-extracts claims and never re-reads a deck or site. With
    --render-only, or with no model configured, only the memo is
    re-rendered. Fully offline except for the model call itself.
    """
    load_dotenv_if_present()
    settings = _load_settings_or_exit(config_file, None)
    loaded = load_case(case_dir)
    if loaded.status != "ok" or loaded.casefile is None:
        typer.echo(loaded.message, err=True)
        raise typer.Exit(code=1)

    prepared: PreparedProvider | None = None
    if not render_only:
        try:
            prepared = _prepare_provider(settings, model)
        except ProviderError as error:
            typer.echo(f"Model configuration error: {error}", err=True)
            raise typer.Exit(code=1) from None
        if prepared is None:
            typer.echo(
                "No model configured (COLDSCREEN_MODEL, coldscreen.toml, or"
                " --model), so only the memo was re-rendered.",
                err=True,
            )

    try:
        result = run_rerun(case_dir=case_dir, casefile=loaded.casefile, prepared=prepared)
    finally:
        _close_provider(prepared)

    if result.status != "ok":
        typer.echo(result.message, err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Memo re-rendered: {result.memo_path}")


def _load_mcp_server_builder() -> Callable[[], Any] | None:
    """The MCP server builder, or None when the optional extra is missing.

    The import is deliberately late and local: `import coldscreen.cli` must
    work on a default install, which does not ship the mcp dependency.

    Only a genuinely absent `mcp` package returns None. Anything else that
    goes wrong while importing our own module (a typo, a broken sibling
    import, an mcp submodule that moved) is re-raised rather than reported
    as a missing extra, because "install the extra" is unhelpful advice for
    a bug in this repository.
    """
    try:
        from .mcp_server import build_server
    except ModuleNotFoundError:
        try:
            import mcp  # noqa: F401
        except ModuleNotFoundError:
            return None
        raise
    return build_server


@app.command()
def mcp() -> None:
    """Serve the screening pipeline to MCP hosts over stdio.

    stdout carries JSON-RPC only; status lines and logs go to stderr. Keys
    come from this process's environment, never from tool arguments. Needs
    the optional mcp extra: pip install 'coldscreen[mcp]'.
    """
    load_dotenv_if_present()
    builder = _load_mcp_server_builder()
    if builder is None:
        typer.echo(MCP_EXTRA_MISSING, err=True)
        raise typer.Exit(code=1)
    server = builder()
    typer.echo(
        "coldscreen MCP server ready on stdio. Tools: screen_company,"
        " rerun_case. Keys are read from this process's environment.",
        err=True,
    )
    # run() is synchronous and owns the transport; stdio is the only mode
    # this project ships.
    server.run("stdio")


def _cache_settings(config_file: Path | None) -> Settings:
    load_dotenv_if_present()
    return _load_settings_or_exit(config_file, None)


@cache_app.command("path")
def cache_show_path(
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config",
            exists=True,
            file_okay=True,
            dir_okay=False,
            help="Path to a coldscreen.toml configuration file. Must exist.",
        ),
    ] = None,
) -> None:
    """Print the HTTP cache sqlite path and exit. No API key required."""
    settings = _cache_settings(config_file)
    typer.echo(str(settings.cache_path))


@cache_app.command("clear")
def cache_clear(
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config",
            exists=True,
            file_okay=True,
            dir_okay=False,
            help="Path to a coldscreen.toml configuration file. Must exist.",
        ),
    ] = None,
) -> None:
    """Delete cached HTTP pages at the configured path. No API key required.

    Missing file is success. A symbolic link at the sqlite name is refused,
    so this cannot follow a link to wipe another file. The command name is
    the confirmation; there is no prompt.
    """
    settings = _cache_settings(config_file)
    path = settings.cache_path
    try:
        result = clear_http_cache(path)
    except CacheClearRefused as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from None
    if result.missing:
        typer.echo(f"Nothing to clear (no cache file at {path})")
        return
    if result.unreadable_removed:
        typer.echo(f"Removed unreadable cache file at {path}")
        return
    if result.entries_removed == 0:
        typer.echo(f"Nothing to clear (0 entries at {path})")
        return
    noun = "entry" if result.entries_removed == 1 else "entries"
    typer.echo(f"Removed {result.entries_removed} cache {noun} from {path}")


@cache_app.command("stats")
def cache_show_stats(
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config",
            exists=True,
            file_okay=True,
            dir_okay=False,
            help="Path to a coldscreen.toml configuration file. Must exist.",
        ),
    ] = None,
) -> None:
    """Print cache path, existence, entry count, size, and TTL. No API key required.

    Does not print URLs, query params, or response bodies.
    """
    settings = _cache_settings(config_file)
    stats = http_cache_stats(settings.cache_path, settings.cache_ttl_days)
    if stats.unreadable:
        typer.echo(
            f"warning: HTTP cache at {stats.path} is unreadable",
            err=True,
        )
    typer.echo(f"path: {stats.path}")
    typer.echo(f"exists: {'true' if stats.exists else 'false'}")
    if stats.unreadable or stats.entry_count is None:
        typer.echo("entries: unreadable")
    else:
        typer.echo(f"entries: {stats.entry_count}")
    typer.echo(f"size_bytes: {stats.size_bytes}")
    typer.echo(f"ttl_days: {stats.ttl_days}")


def main() -> None:
    """Console script entry point."""
    app()


if __name__ == "__main__":
    main()
