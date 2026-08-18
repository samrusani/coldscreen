"""Typer CLI: `firstpass screen` and `firstpass rerun`.

screen resolves the input to one company, runs the deterministic registry
pass, and writes the case directory. rerun re-renders the memo from an
existing casefile.json with no network access at all.

Exit codes: 0 success, 1 error, 3 ambiguous company match that needs a
company number. Code 2 is left to typer for usage errors.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from . import __version__
from .casedir import case_dir_name, load_casefile, validate_company_number, write_case
from .ch_client import (
    AuthError,
    CompaniesHouseClient,
    CompaniesHouseError,
    NotFoundError,
    Throttle,
    TransportFailure,
)
from .config import (
    API_KEY_ENV,
    Settings,
    api_key_from_env,
    fixed_now,
    load_dotenv_if_present,
    load_settings,
)
from .findings import build_findings
from .http_cache import HttpCache
from .models import CaseFile
from .render import render_memo
from .stages.registry import NamedRecord, RegistryResult, run_registry_pass, split_officers
from .stages.resolve import Resolution, resolve

app = typer.Typer(
    name="firstpass",
    help=(
        "First-pass screening memos for UK companies from public registry"
        " data. Exit codes: 0 success, 1 error, 3 ambiguous company match."
    ),
    add_completion=False,
    no_args_is_help=True,
)

AMBIGUOUS_EXIT_CODE = 3


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


def _pick_candidate(resolution: Resolution) -> str:
    """Numbered picker on a terminal; candidate table plus exit code 3 otherwise."""
    lines = _candidate_lines(resolution)
    if not _is_interactive():
        typer.echo("Multiple plausible companies matched:", err=True)
        for line in lines:
            typer.echo(f"  {line}", err=True)
        typer.echo(
            "Not a terminal, so nothing was picked. Rerun with the company"
            " number, for example: firstpass screen <number>",
            err=True,
        )
        raise typer.Exit(code=AMBIGUOUS_EXIT_CODE)
    typer.echo("Multiple plausible companies matched:")
    for line in lines:
        typer.echo(f"  {line}")
    choice = typer.prompt("Select a company by list position, or 0 to abort", type=int)
    if choice < 1 or choice > len(resolution.candidates):
        typer.echo("Aborted without selecting a company.", err=True)
        raise typer.Exit(code=AMBIGUOUS_EXIT_CODE)
    return resolution.candidates[choice - 1].company_number


def _announce_exact_match(resolution: Resolution) -> None:
    """A resolved tie is still a choice; say so where the user can see it."""
    if resolution.chosen is None or not resolution.exact_title_match:
        return
    if resolution.set_aside < 1:
        return
    typer.echo(
        f"Matched {resolution.chosen.title} ({resolution.chosen.company_number})"
        f" by exact name; {resolution.set_aside} other candidate(s) were set"
        " aside. Pass a company number to choose differently.",
        err=True,
    )


def _clock(frozen: datetime | None) -> Callable[[], datetime]:
    if frozen is not None:
        fixed = frozen
        return lambda: fixed
    return lambda: datetime.now(UTC)


def _build_casefile(
    registry: RegistryResult,
    settings: Settings,
    screened_at: datetime,
    clock_override: bool,
) -> CaseFile:
    today = screened_at.date()
    current, resigned_recent = split_officers(
        registry.officers, today, settings.officer_lookback_years
    )
    findings = build_findings(registry, settings, today)
    return CaseFile(
        subject=registry.profile,
        officers=current + resigned_recent,
        pscs=registry.pscs or [],
        charges=registry.charges or [],
        filings=registry.filings,
        filings_total=registry.filings_total,
        insolvency_cases=registry.insolvency_cases or [],
        findings=findings,
        verdict=None,
        tool_version=__version__,
        screened_at=screened_at,
        clock_override=clock_override,
    )


@app.command()
def screen(
    query: Annotated[
        str, typer.Argument(help="Company name, or a company number such as 01234567.")
    ],
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
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config",
            exists=True,
            file_okay=True,
            dir_okay=False,
            help="Path to a firstpass.toml configuration file. Must exist.",
        ),
    ] = None,
) -> None:
    """Screen one company and write the case directory.

    Exit codes: 0 success, 1 error, 3 ambiguous match (pass a company number).
    """
    load_dotenv_if_present()
    settings = load_settings(config_file, {"output_dir": output_dir})
    api_key = api_key_from_env()
    if api_key is None:
        typer.echo(
            f"{API_KEY_ENV} is not set. Get a free key from the Companies House"
            " developer hub and export it as an environment variable.",
            err=True,
        )
        raise typer.Exit(code=1)

    frozen = fixed_now()
    now = _clock(frozen)

    cache = HttpCache(settings.cache_path, ttl_seconds=settings.cache_ttl_seconds)
    throttle = Throttle(settings.rate_limit_requests, settings.rate_limit_window_seconds)
    client = CompaniesHouseClient(
        api_key,
        base_url=settings.base_url,
        cache=cache,
        throttle=throttle,
        timeout_seconds=settings.timeout_seconds,
        now=now,
    )
    try:
        resolution = resolve(client, query, items_per_page=settings.items_per_page)
        if resolution.company_number is not None:
            _announce_exact_match(resolution)
            company_number = resolution.company_number
        elif resolution.is_empty:
            typer.echo(f"No companies matched {query!r}.", err=True)
            raise typer.Exit(code=1)
        else:
            company_number = _pick_candidate(resolution)

        try:
            company_number = validate_company_number(company_number)
        except ValueError:
            typer.echo(
                f"The resolved company number {company_number!r} is not plain"
                " alphanumeric, so it is not safe to use. Screening aborted.",
                err=True,
            )
            raise typer.Exit(code=1) from None

        try:
            registry = run_registry_pass(client, company_number, settings)
        except NotFoundError:
            # The profile endpoint is the one place where 404 does mean the
            # company is not on the register. Insolvency 404s never surface
            # here; the registry stage absorbs them.
            typer.echo(f"Company {company_number} was not found on the register.", err=True)
            raise typer.Exit(code=1) from None
    except AuthError:
        typer.echo(
            f"Authentication failed (HTTP 401). Check {API_KEY_ENV}.",
            err=True,
        )
        raise typer.Exit(code=1) from None
    except TransportFailure as error:
        typer.echo(
            f"Could not reach the Companies House API: {error.cause}."
            " Check your network connection and retry.",
            err=True,
        )
        raise typer.Exit(code=1) from None
    except CompaniesHouseError as error:
        typer.echo(f"Registry access failed: {error}", err=True)
        raise typer.Exit(code=1) from None
    finally:
        client.close()
        cache.close()

    casefile = _build_casefile(
        registry, settings, screened_at=now(), clock_override=frozen is not None
    )
    memo = render_memo(casefile)
    case_dir = Path(settings.output_dir) / case_dir_name(
        casefile.subject.company_name, casefile.subject.company_number
    )
    records = list(registry.records)
    if resolution.search_record is not None:
        records.insert(0, NamedRecord("search_companies", resolution.search_record))
    write_case(case_dir, casefile, records, memo)
    typer.echo(f"Case directory written: {case_dir}", err=json_output)
    if json_output:
        typer.echo(casefile.model_dump_json(indent=2))


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
) -> None:
    """Re-render memo.md from casefile.json. Fully offline, nothing is fetched."""
    casefile_path = case_dir / "casefile.json"
    try:
        casefile = load_casefile(case_dir)
    except FileNotFoundError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from None
    except ValidationError:
        typer.echo(
            f"{casefile_path} is not a valid casefile (corrupt JSON or wrong"
            " shape). Re-run the screen to regenerate it.",
            err=True,
        )
        raise typer.Exit(code=1) from None
    memo = render_memo(casefile)
    (case_dir / "memo.md").write_text(memo, encoding="utf-8")
    typer.echo(f"Memo re-rendered: {case_dir / 'memo.md'}")


def main() -> None:
    """Console script entry point."""
    app()


if __name__ == "__main__":
    main()
