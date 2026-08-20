"""The screening pipeline as a library, independent of any user interface.

`run_screen` and `run_rerun` own the flows that used to live inside the Typer
commands. They return structured results and never exit the process, so the
CLI and the MCP server share one pipeline instead of keeping two copies of it.

Result shapes: "ok" (the run completed: the case directory is written unless
`no_write` was set), "ambiguous" (the query matched several plausible
companies and nothing was picked), "configuration" (the caller's inputs are
wrong, detected before any fetching), and "failure" (the run could not
complete). Stage failures and synthesis failures are not statuses: they
arrive on an "ok" result, because the gathered casefile is still returned
and, on the default path, the case directory is still written.

Secrets are inputs, never outputs. The already-resolved API key is a
parameter; no result object holds it.

Ambiguity is surfaced, never resolved silently. A caller that can ask a human
passes a `chooser`; a caller that cannot (the MCP server) passes none and
receives the candidate list back.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from . import __version__
from .casedir import (
    UnsafeCasePath,
    case_dir_name,
    load_casefile,
    refuse_owned_case_files,
    refuse_symlink,
    validate_company_number,
    write_case,
    write_case_text,
)
from .ch_client import (
    AuthError,
    CompaniesHouseClient,
    CompaniesHouseError,
    NotFoundError,
    Throttle,
    TransportFailure,
)
from .claims import ClaimsExtractionError, ClaimsStageResult, run_claims_stage
from .config import (
    API_KEY_ENV,
    Settings,
    fixed_now,
    ollama_base_url_from_env,
    opensanctions_base_url_from_env,
    opensanctions_key_from_env,
    tavily_key_from_env,
)
from .deck import DeckError, DeckExtraction, extract_deck
from .findings import build_findings
from .http_cache import HttpCache
from .language import (
    claim_text_has_substance,
    code_fetched_exemption_texts,
    find_banned_terms_in_memo,
)
from .media import MediaStageResult, TavilyProvider, run_media
from .models import CaseFile, CompanyCandidate, Officer
from .providers import ModelProvider, ProviderError, get_provider, parse_model_spec
from .render import render_memo
from .sanctions import SanctionsStageResult, run_sanctions
from .site import SiteError, SiteFetchResult, fetch_site, validate_site_url
from .stages.network import NetworkStageResult, run_network_expansion
from .stages.registry import NamedRecord, RegistryResult, run_registry_pass, split_officers
from .stages.resolve import Resolution, normalize_company_number, resolve
from .synthesis import SynthesisError, apply_synthesis, synthesize

# (provider, provider name, model name), as resolved from a "provider:model"
# spec. The provider is constructed before any fetching so a bad spec or a
# missing SDK fails before the API budget is spent.
PreparedProvider = tuple[ModelProvider, str, str]

# Returns the company number the caller chose, or None to decline. A chooser
# that declines leaves the ambiguity unresolved; nothing is ever picked for
# the caller.
CandidateChooser = Callable[[Resolution], str | None]

ScreenStatus = Literal["ok", "ambiguous", "configuration", "failure"]


@dataclass(frozen=True)
class ArgumentNames:
    """How the calling interface spells the pipeline's knobs.

    Error text belongs to the interface, not to the pipeline, but the
    conditions that produce it are detected here. Passing the spelling in
    keeps a message from telling an MCP host to pass a command-line flag it
    has no way to send.
    """

    deck: str = "--deck"
    site: str = "--site"
    model: str = "--model"
    overwrite: str = "--overwrite"
    noun: str = "flags"


CLI_ARGUMENTS = ArgumentNames()
MCP_ARGUMENTS = ArgumentNames(
    deck="deck_path", site="site_url", model="model", overwrite="overwrite", noun="arguments"
)


@dataclass(frozen=True)
class ScreenResult:
    """Outcome of one screen.

    `notices` carries things a human should see that are not errors (a
    resolved name tie, for example); a caller with no human attached can
    ignore them. `stage_failures` is (stage name, reason) pairs for stages
    that failed after retries; on the default path the case directory is
    written anyway and the failure is recorded in the findings.
    """

    status: ScreenStatus
    message: str | None = None
    notices: tuple[str, ...] = ()
    candidates: tuple[CompanyCandidate, ...] = ()
    case_dir: Path | None = None
    casefile: CaseFile | None = None
    memo: str | None = None
    stage_failures: tuple[tuple[str, str], ...] = ()
    synthesis_failure: str | None = None


@dataclass(frozen=True)
class CaseLoad:
    """Outcome of loading a stored casefile for a rerun."""

    status: Literal["ok", "failure"]
    message: str | None = None
    casefile: CaseFile | None = None


@dataclass(frozen=True)
class RerunResult:
    """Outcome of one rerun."""

    status: Literal["ok", "failure"]
    message: str | None = None
    case_dir: Path | None = None
    casefile: CaseFile | None = None
    memo: str | None = None
    memo_path: Path | None = None
    synthesized: bool = False


@dataclass(frozen=True)
class _Fetched:
    """Everything the deterministic registry phase produced."""

    resolution: Resolution
    company_number: str
    registry: RegistryResult
    case_dir: Path
    current_officers: list[Officer]
    network: NetworkStageResult
    notices: tuple[str, ...] = ()


def prepare_provider(settings: Settings, model_spec: str | None) -> PreparedProvider | None:
    """Resolve and construct the synthesis provider, or None when no model
    is configured. Callers run this BEFORE any fetching so a bad spec or a
    missing SDK fails fast instead of after the API budget is spent.

    Raises ProviderError for an unknown provider, a missing model name, or a
    provider SDK that is not installed.
    """
    spec = (model_spec if model_spec is not None else settings.model).strip()
    if not spec:
        return None
    provider_name, model_name = parse_model_spec(spec)
    provider = get_provider(
        provider_name,
        model_name,
        ollama_base_url=ollama_base_url_from_env(),
        ollama_timeout_seconds=settings.ollama_timeout_seconds,
        ollama_num_ctx=settings.ollama_num_ctx,
        ollama_think=settings.ollama_think,
    )
    return provider, provider_name, model_name


def close_provider(prepared: PreparedProvider | None) -> None:
    """Close the provider's transport when it has one. Always in a finally."""
    if prepared is None:
        return
    close = getattr(prepared[0], "close", None)
    if callable(close):
        close()


def language_backstop_failure(memo: str, casefile: CaseFile) -> str | None:
    """Whole-memo language gate: the last line of defense before disk.

    Every rendered memo passes through here on every path (screen, rerun,
    and the synthesis-failure memo). The per-field gate on model output
    already ran; this catches banned vocabulary arriving through any other
    channel. Two exemptions, both spans of code-fetched data: the
    casefile's stored claim texts (the company's own quoted words, exempt
    only inside the rendered claims-table region) and the code-fetched
    rendered strings (identity names, office, network names, media
    domains, claim source labels, and disqualification detail when
    present), which are fetched data the memo must be able to spell out
    anywhere. Single-token claim texts are ignored even if a hand-edited
    casefile still contains them: they are not exemption spans. On a hit
    the caller writes nothing and the returned message reports a count
    only: the terms themselves never reach the output either.
    """
    count = len(
        find_banned_terms_in_memo(
            memo,
            claim_texts=tuple(
                claim.text for claim in casefile.claims if claim_text_has_substance(claim.text)
            ),
            identity_names=code_fetched_exemption_texts(casefile),
        )
    )
    if not count:
        return None
    return (
        f"The rendered memo failed the language backstop: {count} banned"
        " term(s) were found in the memo text. The memo was not written."
        " Inspect the casefile inputs; memos never use accusatory language."
    )


def _filesystem_failure(case_dir: Path, operation: str, error: OSError) -> str:
    """A filesystem failure stated in terms of what the tool was doing.

    The reason comes from `strerror` rather than `str(error)`: the caller
    gets the operation, the directory, and the kernel's reason, with no
    object reprs and no traceback.
    """
    reason = error.strerror or type(error).__name__
    return (
        f"Could not {operation} in {case_dir}: {reason}. Check the path, its"
        " permissions, and free space, then run again."
    )


def _clock(frozen: datetime | None) -> Callable[[], datetime]:
    if frozen is not None:
        fixed = frozen
        return lambda: fixed
    return lambda: datetime.now(UTC)


def _exact_match_notice(resolution: Resolution) -> str | None:
    """A resolved tie is still a choice; the caller must be able to see it."""
    if resolution.chosen is None or not resolution.exact_title_match:
        return None
    if resolution.set_aside < 1:
        return None
    return (
        f"Matched {resolution.chosen.title} ({resolution.chosen.company_number})"
        f" by exact name; {resolution.set_aside} other candidate(s) were set"
        " aside. Pass a company number to choose differently."
    )


def _build_casefile(
    registry: RegistryResult,
    network: NetworkStageResult,
    sanctions: SanctionsStageResult,
    media: MediaStageResult,
    claims_stage: ClaimsStageResult,
    settings: Settings,
    screened_at: datetime,
    clock_override: bool,
) -> CaseFile:
    today = screened_at.date()
    current, resigned_recent = split_officers(
        registry.officers, today, settings.officer_lookback_years
    )
    findings = (
        build_findings(registry, settings, today)
        + network.findings
        + sanctions.findings
        + media.findings
        + claims_stage.findings
    )
    return CaseFile(
        subject=registry.profile,
        officers=current + resigned_recent,
        pscs=registry.pscs or [],
        charges=registry.charges or [],
        filings=registry.filings,
        filings_total=registry.filings_total,
        insolvency_cases=registry.insolvency_cases or [],
        findings=findings,
        claims=claims_stage.claims,
        claims_extraction=claims_stage.extraction,
        sanctions=sanctions.screening,
        network=network.expansion,
        media=media.screening,
        verdict=None,
        tool_version=__version__,
        screened_at=screened_at,
        clock_override=clock_override,
    )


def _registry_phase(
    query: str,
    company_number: str | None,
    settings: Settings,
    api_key: str,
    overwrite: bool,
    chooser: CandidateChooser | None,
    now: Callable[[], datetime],
    arguments: ArgumentNames,
    refresh: bool = False,
    no_write: bool = False,
) -> _Fetched | ScreenResult:
    """Resolution, registry pass, overwrite check, network expansion.

    Everything here shares one Companies House client, so it lives in one
    function with one finally. A ScreenResult return means the run stopped.
    """
    notices: tuple[str, ...] = ()
    cache = HttpCache(settings.cache_path, ttl_seconds=settings.cache_ttl_seconds)
    throttle = Throttle(settings.rate_limit_requests, settings.rate_limit_window_seconds)
    client = CompaniesHouseClient(
        api_key,
        base_url=settings.base_url,
        cache=cache,
        refresh=refresh,
        throttle=throttle,
        timeout_seconds=settings.timeout_seconds,
        now=now,
    )
    try:
        if company_number is not None:
            # An explicitly supplied number skips search entirely, exactly as
            # a number-shaped query does. Anything that is not a plain
            # alphanumeric number is rejected below with the same message.
            resolution = Resolution(
                company_number=normalize_company_number(company_number) or company_number.strip()
            )
        else:
            resolution = resolve(client, query, items_per_page=settings.items_per_page)

        if resolution.company_number is not None:
            notice = _exact_match_notice(resolution)
            if notice is not None:
                notices += (notice,)
            chosen_number = resolution.company_number
        elif resolution.is_empty:
            return ScreenResult(
                status="failure",
                message=f"No companies matched {query!r}.",
                notices=notices,
            )
        else:
            picked = chooser(resolution) if chooser is not None else None
            if picked is None:
                # Nothing is ever picked for the caller. The candidate list
                # goes back so a second call can name the company number.
                return ScreenResult(
                    status="ambiguous",
                    message=(
                        f"{len(resolution.candidates)} plausible companies matched"
                        f" {query!r}. Choose one by company number and screen again."
                    ),
                    notices=notices,
                    candidates=tuple(resolution.candidates),
                )
            chosen_number = picked

        try:
            chosen_number = validate_company_number(chosen_number)
        except ValueError:
            return ScreenResult(
                status="failure",
                message=(
                    f"The resolved company number {chosen_number!r} is not plain"
                    " alphanumeric, so it is not safe to use. Screening aborted."
                ),
                notices=notices,
            )

        try:
            registry = run_registry_pass(client, chosen_number, settings)
        except NotFoundError:
            # The profile endpoint is the one place where 404 does mean the
            # company is not on the register. Insolvency 404s never surface
            # here; the registry stage absorbs them.
            return ScreenResult(
                status="failure",
                message=f"Company {chosen_number} was not found on the register.",
                notices=notices,
            )

        # Overwrite protection, checked as soon as the canonical name and
        # number are known and before the paid screening stages run. A
        # no-write run never creates a case directory, so an existing one
        # is not a conflict.
        case_dir = Path(settings.output_dir) / case_dir_name(
            registry.profile.company_name, chosen_number
        )
        if not no_write and case_dir.exists() and not overwrite:
            return ScreenResult(
                status="failure",
                message=(
                    f"Case directory already exists: {case_dir}. Pass"
                    f" {arguments.overwrite} to replace it, or move the existing"
                    " directory aside."
                ),
                notices=notices,
            )

        today = now().date()
        current_officers, _resigned = split_officers(
            registry.officers, today, settings.officer_lookback_years
        )
        network = run_network_expansion(
            client,
            chosen_number,
            current_officers,
            registry.pscs or [],
            settings,
            today,
            fallback_record=registry.first_record("officers_p1") or registry.profile_record,
        )
    except AuthError:
        return ScreenResult(
            status="failure",
            message=f"Authentication failed (HTTP 401). Check {API_KEY_ENV}.",
            notices=notices,
        )
    except TransportFailure as error:
        return ScreenResult(
            status="failure",
            message=(
                f"Could not reach the Companies House API: {error.cause}."
                " Check your network connection and retry."
            ),
            notices=notices,
        )
    except CompaniesHouseError as error:
        return ScreenResult(
            status="failure",
            message=f"Registry access failed: {error}",
            notices=notices,
        )
    finally:
        client.close()
        cache.close()

    return _Fetched(
        resolution=resolution,
        company_number=chosen_number,
        registry=registry,
        case_dir=case_dir,
        current_officers=current_officers,
        network=network,
        notices=notices,
    )


def run_screen(
    *,
    query: str,
    settings: Settings,
    api_key: str,
    prepared: PreparedProvider | None = None,
    company_number: str | None = None,
    deck: Path | None = None,
    site: str | None = None,
    overwrite: bool = False,
    refresh: bool = False,
    no_write: bool = False,
    chooser: CandidateChooser | None = None,
    arguments: ArgumentNames = CLI_ARGUMENTS,
) -> ScreenResult:
    """Screen one company and, by default, write the case directory.

    `api_key` and `prepared` are already resolved by the caller: no secret
    is ever read from a user-facing argument here. `company_number`, when
    given, is the company to screen and search is skipped; `query` is then
    used only in messages. `refresh` skips HTTP cache reads for this run
    and still writes successful 200 responses back into the cache.
    `no_write` skips the case directory for this run only: `write_case` is
    not called, an existing case directory is not a conflict, and
    `case_dir` on the result is None. The HTTP cache still stores 200s.
    `overwrite` with `no_write` is a no-op.

    Stage failure posture: a sanctions or media stage that fails after
    retries does not abort the run. On the default path the case directory
    is written with everything gathered; on a no-write run the same
    casefile and memo are still returned. The failure is a distinct
    finding, synthesis proceeds over what exists, and the failure comes
    back on an "ok" result so the caller can report it and exit nonzero.
    """
    if (deck is not None or site is not None) and prepared is None:
        return ScreenResult(
            status="configuration",
            message=(
                f"Claims extraction needs a model: {arguments.deck} and"
                f" {arguments.site} turn deck and site text into discrete claims,"
                " and that conversion is model work. Configure one via"
                f" {arguments.model}, COLDSCREEN_MODEL, or coldscreen.toml, or"
                f" drop the {arguments.deck}/{arguments.site} {arguments.noun}."
            ),
        )

    if site is not None:
        try:
            site = validate_site_url(site)
        except SiteError as error:
            return ScreenResult(status="configuration", message=f"Site URL error: {error}")

    frozen = fixed_now()
    now = _clock(frozen)

    # The deck is read BEFORE any fetching: a missing, unreadable, oversized,
    # encrypted, or non-PDF deck fails fast instead of after the API budget
    # is spent.
    deck_extraction: DeckExtraction | None = None
    if deck is not None:
        try:
            deck_extraction = extract_deck(
                deck, max_pages=settings.max_deck_pages, max_bytes=settings.max_deck_bytes
            )
        except DeckError as error:
            return ScreenResult(status="configuration", message=f"Deck error: {error}")

    phase = _registry_phase(
        query=query,
        company_number=company_number,
        settings=settings,
        api_key=api_key,
        overwrite=overwrite,
        chooser=chooser,
        now=now,
        arguments=arguments,
        refresh=refresh,
        no_write=no_write,
    )
    if isinstance(phase, ScreenResult):
        return phase

    registry = phase.registry
    case_dir = phase.case_dir

    # Sanctions and media failures after retries do not abort the run: the
    # stage records the failure as a finding, the run continues to
    # persistence, and the caller reports the named stage.
    stage_failures: list[tuple[str, str]] = []

    sanctions = run_sanctions(
        registry.profile,
        phase.current_officers,
        registry.pscs or [],
        settings,
        api_key=opensanctions_key_from_env(),
        base_url=opensanctions_base_url_from_env(),
        now=now,
    )
    if sanctions.failed_reason is not None:
        stage_failures.append(("sanctions screening", sanctions.failed_reason))

    tavily_key = tavily_key_from_env()
    search_provider: TavilyProvider | None = None
    if tavily_key is not None:
        search_provider = TavilyProvider(
            tavily_key, timeout_seconds=settings.timeout_seconds, now=now
        )
    try:
        media = run_media(
            registry.profile.company_name,
            [p.name for p in registry.profile.previous_company_names if p.name],
            provider=search_provider,
            provider_name="tavily" if search_provider is not None else None,
            results_per_query=settings.media_results_per_query,
            now=now,
        )
    finally:
        if search_provider is not None:
            search_provider.close()
    if media.failed_reason is not None:
        stage_failures.append(("adverse media search", media.failed_reason))

    site_result: SiteFetchResult | None = None
    if site is not None:
        site_result = fetch_site(
            site,
            timeout_seconds=settings.timeout_seconds,
            max_response_bytes=settings.max_site_response_bytes,
            now=now,
        )

    synthesis_failure: str | None = None
    try:
        claims_stage = run_claims_stage(
            registry.profile,
            deck_extraction,
            site_result,
            provider=prepared[0] if prepared is not None else None,
            provider_name=prepared[1] if prepared is not None else None,
            model=prepared[2] if prepared is not None else None,
            settings=settings,
            now=now,
        )
    except ClaimsExtractionError as error:
        # The deck and site evidence the stage already produced is kept; the
        # judgment layer is skipped because it would reason over inputs the
        # user supplied but the run could not convert into claims.
        claims_stage = error.result
        synthesis_failure = str(error)

    casefile = _build_casefile(
        registry,
        phase.network,
        sanctions,
        media,
        claims_stage,
        settings,
        screened_at=now(),
        clock_override=frozen is not None,
    )

    if prepared is not None and synthesis_failure is None:
        provider, provider_name, model_name = prepared
        try:
            result = synthesize(casefile, provider, provider_name, model_name)
            casefile = apply_synthesis(casefile, result)
        except (SynthesisError, ProviderError) as error:
            # The deterministic work is done and paid for; keep the audit
            # pack, say plainly that synthesis failed, and exit nonzero.
            synthesis_failure = str(error)

    memo = render_memo(casefile, synthesis_failure=synthesis_failure)
    backstop = language_backstop_failure(memo, casefile)
    if backstop is not None:
        return ScreenResult(status="failure", message=backstop, notices=phase.notices)

    records = (
        list(registry.records)
        + phase.network.records
        + sanctions.records
        + media.records
        + claims_stage.records
    )
    if phase.resolution.search_record is not None:
        records.insert(0, NamedRecord("search_companies", phase.resolution.search_record))

    written_dir: Path | None = None
    if not no_write:
        evidence_dir = case_dir / "evidence"
        try:
            # Refuse the whole owned group before clearing evidence/, so a
            # link at fetch_log.json cannot leave a half-updated case.
            refuse_owned_case_files(case_dir)
            if overwrite and evidence_dir.is_dir():
                # Replacing a case: stale evidence files from the previous run
                # must not linger next to the new index. Only the tool-owned
                # evidence directory is cleared; anything else in the directory
                # is left alone. A link in place of that directory is refused
                # rather than followed, here and in write_case.
                refuse_symlink(evidence_dir, "evidence directory")
                shutil.rmtree(evidence_dir)
            write_case(case_dir, casefile, records, memo)
        except UnsafeCasePath as error:
            return ScreenResult(status="failure", message=str(error), notices=phase.notices)
        except OSError as error:
            return ScreenResult(
                status="failure",
                message=_filesystem_failure(case_dir, "write the case directory", error),
                notices=phase.notices,
            )
        written_dir = case_dir
    return ScreenResult(
        status="ok",
        notices=phase.notices,
        case_dir=written_dir,
        casefile=casefile,
        memo=memo,
        stage_failures=tuple(stage_failures),
        synthesis_failure=synthesis_failure,
    )


def load_case(case_dir: Path) -> CaseLoad:
    """Load a stored casefile for a rerun, reporting a clean failure."""
    try:
        return CaseLoad(status="ok", casefile=load_casefile(case_dir))
    except FileNotFoundError as error:
        return CaseLoad(status="failure", message=str(error))
    except OSError as error:
        return CaseLoad(
            status="failure",
            message=_filesystem_failure(case_dir, "read casefile.json", error),
        )
    except ValidationError:
        return CaseLoad(
            status="failure",
            message=(
                f"{case_dir / 'casefile.json'} is not a valid casefile (corrupt"
                " JSON or wrong shape). Re-run the screen to regenerate it."
            ),
        )


def run_rerun(
    *,
    case_dir: Path,
    casefile: CaseFile,
    prepared: PreparedProvider | None = None,
) -> RerunResult:
    """Re-run synthesis from a stored casefile and re-render the memo.

    Nothing is fetched and no claim is re-extracted: synthesis re-assesses
    the STORED claims. With no prepared provider only the memo is
    re-rendered. Fully offline except for the model call itself.
    """
    casefile_path = case_dir / "casefile.json"
    synthesized = False
    if prepared is not None:
        provider, provider_name, model_name = prepared
        try:
            result = synthesize(casefile, provider, provider_name, model_name)
        except (SynthesisError, ProviderError) as error:
            return RerunResult(
                status="failure",
                message=(
                    f"Synthesis failed: {error}\n"
                    "The existing casefile and memo were left untouched."
                ),
                case_dir=case_dir,
            )
        casefile = apply_synthesis(casefile, result)
        synthesized = True

    # Render and gate the memo BEFORE touching any file, so a backstop hit
    # leaves the existing casefile and memo exactly as they were.
    memo = render_memo(casefile)
    backstop = language_backstop_failure(memo, casefile)
    if backstop is not None:
        return RerunResult(status="failure", message=backstop, case_dir=case_dir)
    # Both names are tool-owned and both are refused if they have become
    # symbolic links: confining the case directory does not stop a link at
    # memo.md from redirecting the write outside it.
    memo_path = case_dir / "memo.md"
    try:
        refuse_symlink(case_dir, "case directory")
        refuse_symlink(memo_path, "memo file")
        if synthesized:
            refuse_symlink(casefile_path, "casefile")
            write_case_text(casefile_path, casefile.model_dump_json(indent=2) + "\n")
        write_case_text(memo_path, memo)
    except UnsafeCasePath as error:
        return RerunResult(status="failure", message=str(error), case_dir=case_dir)
    except OSError as error:
        return RerunResult(
            status="failure",
            message=_filesystem_failure(case_dir, "write the memo", error),
            case_dir=case_dir,
        )
    return RerunResult(
        status="ok",
        case_dir=case_dir,
        casefile=casefile,
        memo=memo,
        memo_path=memo_path,
        synthesized=synthesized,
    )
