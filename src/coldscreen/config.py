"""Configuration loading.

Precedence, highest first: CLI flag, environment variable, coldscreen.toml,
built-in default. Secrets come from the environment only. A .env file in the
working directory is loaded as a convenience when present; it never overrides
variables already set in the environment.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args, get_type_hints

from dotenv import load_dotenv
from platformdirs import user_cache_dir

API_KEY_ENV = "COMPANIES_HOUSE_API_KEY"
OPENSANCTIONS_KEY_ENV = "OPENSANCTIONS_API_KEY"
OPENSANCTIONS_BASE_URL_ENV = "OPENSANCTIONS_BASE_URL"
TAVILY_KEY_ENV = "TAVILY_API_KEY"
OLLAMA_BASE_URL_ENV = "OLLAMA_BASE_URL"
ENV_PREFIX = "COLDSCREEN_"
DEFAULT_BASE_URL = "https://api.company-information.service.gov.uk"
DEFAULT_OPENSANCTIONS_BASE_URL = "https://api.opensanctions.org"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"


@dataclass(frozen=True)
class Settings:
    """Resolved configuration for one invocation."""

    base_url: str = DEFAULT_BASE_URL
    output_dir: str = "cases"
    cache_dir: str = ""  # empty string means the platform user cache directory
    cache_ttl_days: float = 7.0
    rate_limit_requests: int = 500
    rate_limit_window_seconds: float = 300.0
    items_per_page: int = 100
    max_pages_officers: int = 10
    max_pages_psc: int = 10
    max_pages_filing_history: int = 3
    max_pages_appointments: int = 5
    officer_lookback_years: int = 5
    wholesale_change_min: int = 2
    timeout_seconds: float = 30.0
    # Synthesis model as "provider:model". Empty string means no model: the
    # screen completes deterministically and the memo says so.
    model: str = ""
    # Sanctions screening (OpenSanctions hosted API or self-hosted yente).
    # The key comes from OPENSANCTIONS_API_KEY; a non-default endpoint from
    # OPENSANCTIONS_BASE_URL. Neither is ever bundled, logged, or persisted.
    sanctions_dataset: str = "default"
    sanctions_threshold: float = 0.7
    sanctions_algorithm: str = "best"
    sanctions_limit: int = 5
    # Adverse media search.
    media_results_per_query: int = 5
    # Claims extraction inputs (stage 6). max_deck_bytes caps the deck file
    # size, checked before any parsing; max_deck_pages caps how many deck
    # pages are read; max_claims_chars caps the combined deck-plus-site text
    # handed to the model (an explicit truncation finding records any cut);
    # max_site_response_bytes caps how much of any single site response is
    # read off the wire.
    max_deck_bytes: int = 50_000_000
    max_deck_pages: int = 40
    max_claims_chars: int = 60000
    max_site_response_bytes: int = 2_000_000
    # Local model endpoint timeout: synthesis on large local models is slow.
    ollama_timeout_seconds: float = 600.0
    # Ollama context window (options.num_ctx). The daemon default (4096 on
    # most models) silently truncates the synthesis input; the default here
    # covers the prompt plus a compact casefile with headroom. Raise it for
    # unusually large casefiles, lower it for tight-memory machines.
    ollama_num_ctx: int = 16384
    # Ollama thinking mode (the top-level "think" request field). Tri-state:
    # None omits the field entirely, which is what models without thinking
    # support need because they can reject it. Reasoning-family models need
    # false for reliable schema-constrained synthesis; see providers/ollama_p.py.
    ollama_think: bool | None = None

    @property
    def cache_path(self) -> Path:
        base = Path(self.cache_dir) if self.cache_dir else Path(user_cache_dir("coldscreen"))
        return base / "http_cache.sqlite3"

    @property
    def cache_ttl_seconds(self) -> float:
        return self.cache_ttl_days * 86400.0


def load_dotenv_if_present(cwd: Path | None = None) -> None:
    """Load a .env file from the working directory when one exists."""
    directory = cwd or Path.cwd()
    env_file = directory / ".env"
    if not env_file.is_file():
        return
    load_dotenv(env_file, override=False)


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


_TRUE_SPELLINGS = frozenset({"true", "1"})
_FALSE_SPELLINGS = frozenset({"false", "0"})


def coerce_bool(raw: Any) -> bool:
    """The one definition of the accepted true/false spellings.

    TOML booleans arrive already typed; environment values and script
    arguments arrive as text.
    """
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in _TRUE_SPELLINGS:
        return True
    if text in _FALSE_SPELLINGS:
        return False
    raise ValueError(f"expected true or false (or 1 or 0), got {raw!r}")


def _coerce(raw: Any, target: type) -> Any:
    if target is bool:
        return coerce_bool(raw)
    if target is int:
        return int(raw)
    if target is float:
        return float(raw)
    if target is str:
        return str(raw)
    return raw


def _target_types() -> dict[str, type]:
    """Coercion target per setting, read from the annotations.

    The default value alone is not enough: an optional setting defaults to
    None, and None carries no useful type. ollama_think is annotated
    bool | None and coerces as bool whenever it is set at all; leaving it
    unset is what keeps it out of the request.
    """
    targets: dict[str, type] = {}
    for name, hint in get_type_hints(Settings).items():
        present = [arg for arg in get_args(hint) if arg is not type(None)]
        targets[name] = present[0] if present else hint
    return targets


# Settings that must be strictly positive integers: page sizes, caps, and
# counts where zero or a negative value would silently disable retrieval.
_POSITIVE_INT_SETTINGS = (
    "rate_limit_requests",
    "items_per_page",
    "max_pages_officers",
    "max_pages_psc",
    "max_pages_filing_history",
    "max_pages_appointments",
    "officer_lookback_years",
    "wholesale_change_min",
    "sanctions_limit",
    "media_results_per_query",
    "max_deck_bytes",
    "max_deck_pages",
    "max_claims_chars",
    "max_site_response_bytes",
    "ollama_num_ctx",
)

# Settings that must be strictly positive numbers.
_POSITIVE_FLOAT_SETTINGS = (
    "rate_limit_window_seconds",
    "timeout_seconds",
    "ollama_timeout_seconds",
)


def validate_settings(settings: Settings) -> None:
    """Reject out-of-range values with an error naming the setting."""
    for name in _POSITIVE_INT_SETTINGS:
        value = getattr(settings, name)
        if value < 1:
            raise ValueError(f"invalid setting {name}: must be a positive integer, got {value}")
    for name in _POSITIVE_FLOAT_SETTINGS:
        value = getattr(settings, name)
        if value <= 0:
            raise ValueError(f"invalid setting {name}: must be greater than zero, got {value}")
    if settings.cache_ttl_days < 0:
        raise ValueError(
            f"invalid setting cache_ttl_days: must be zero or more, got {settings.cache_ttl_days}"
        )
    if not 0.0 <= settings.sanctions_threshold <= 1.0:
        raise ValueError(
            "invalid setting sanctions_threshold: must be between 0 and 1,"
            f" got {settings.sanctions_threshold}"
        )


def load_settings(
    config_file: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
) -> Settings:
    """Build Settings from defaults, coldscreen.toml, environment, CLI flags.

    An explicitly passed config_file must exist; the caller is expected to
    have validated that. Unknown keys in the file produce a warning on
    stderr rather than being silently ignored. Values are validated on
    load; a bad value raises ValueError naming the setting.
    """
    env = os.environ if environ is None else environ
    toml_path = config_file or Path("coldscreen.toml")
    toml_data = _read_toml(toml_path)

    known_names = {field.name for field in fields(Settings)}
    unknown = sorted(set(toml_data) - known_names)
    if unknown:
        print(
            f"warning: {toml_path} contains unknown setting(s):"
            f" {', '.join(unknown)}. They are ignored.",
            file=sys.stderr,
        )

    targets = _target_types()
    values: dict[str, Any] = {}
    for field in fields(Settings):
        target = targets[field.name]
        try:
            if field.name in toml_data:
                values[field.name] = _coerce(toml_data[field.name], target)
            env_key = ENV_PREFIX + field.name.upper()
            if env_key in env:
                values[field.name] = _coerce(env[env_key], target)
        except ValueError as error:
            raise ValueError(f"invalid setting {field.name}: {error}") from None
    if cli_overrides:
        for key, value in cli_overrides.items():
            if value is not None:
                values[key] = value
    settings = Settings(**values)
    validate_settings(settings)
    return settings


def api_key_from_env(environ: dict[str, str] | None = None) -> str | None:
    """Read the Companies House API key. Never logged, never persisted."""
    env = os.environ if environ is None else environ
    key = env.get(API_KEY_ENV, "").strip()
    return key or None


def _env_value(name: str, environ: dict[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    value = env.get(name, "").strip()
    return value or None


def opensanctions_key_from_env(environ: dict[str, str] | None = None) -> str | None:
    """OpenSanctions API key. Users bring their own; never bundled or logged."""
    return _env_value(OPENSANCTIONS_KEY_ENV, environ)


def opensanctions_base_url_from_env(environ: dict[str, str] | None = None) -> str | None:
    """Non-default OpenSanctions endpoint (self-hosted yente), when set."""
    return _env_value(OPENSANCTIONS_BASE_URL_ENV, environ)


def tavily_key_from_env(environ: dict[str, str] | None = None) -> str | None:
    """Tavily search key for the adverse media stage."""
    return _env_value(TAVILY_KEY_ENV, environ)


def ollama_base_url_from_env(environ: dict[str, str] | None = None) -> str:
    """Ollama endpoint, defaulting to the local daemon."""
    return _env_value(OLLAMA_BASE_URL_ENV, environ) or DEFAULT_OLLAMA_BASE_URL


def fixed_now(environ: dict[str, str] | None = None) -> datetime | None:
    """Optional frozen clock for reproducible runs, from COLDSCREEN_SCREENED_AT.

    Used by the test suite and the fixture generator to make memo output
    byte-stable. The value is an ISO 8601 timestamp: aware input is
    converted to UTC, naive input is assumed UTC. Runs using it are marked
    in the casefile and the memo footer.
    """
    env = os.environ if environ is None else environ
    raw = env.get(ENV_PREFIX + "SCREENED_AT", "").strip()
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
