"""Configuration loading.

Precedence, highest first: CLI flag, environment variable, firstpass.toml,
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
from typing import Any

from dotenv import load_dotenv
from platformdirs import user_cache_dir

API_KEY_ENV = "COMPANIES_HOUSE_API_KEY"
ENV_PREFIX = "FIRSTPASS_"
DEFAULT_BASE_URL = "https://api.company-information.service.gov.uk"


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
    officer_lookback_years: int = 5
    wholesale_change_min: int = 2
    timeout_seconds: float = 30.0

    @property
    def cache_path(self) -> Path:
        base = Path(self.cache_dir) if self.cache_dir else Path(user_cache_dir("firstpass"))
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


def _coerce(raw: Any, target: type) -> Any:
    if target is int:
        return int(raw)
    if target is float:
        return float(raw)
    if target is str:
        return str(raw)
    return raw


def load_settings(
    config_file: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
) -> Settings:
    """Build Settings from defaults, firstpass.toml, environment, CLI flags.

    An explicitly passed config_file must exist; the caller is expected to
    have validated that. Unknown keys in the file produce a warning on
    stderr rather than being silently ignored.
    """
    env = os.environ if environ is None else environ
    toml_path = config_file or Path("firstpass.toml")
    toml_data = _read_toml(toml_path)

    known_names = {field.name for field in fields(Settings)}
    unknown = sorted(set(toml_data) - known_names)
    if unknown:
        print(
            f"warning: {toml_path} contains unknown setting(s):"
            f" {', '.join(unknown)}. They are ignored.",
            file=sys.stderr,
        )

    defaults = Settings()
    values: dict[str, Any] = {}
    for field in fields(Settings):
        target = type(getattr(defaults, field.name))
        if field.name in toml_data:
            values[field.name] = _coerce(toml_data[field.name], target)
        env_key = ENV_PREFIX + field.name.upper()
        if env_key in env:
            values[field.name] = _coerce(env[env_key], target)
    if cli_overrides:
        for key, value in cli_overrides.items():
            if value is not None:
                values[key] = value
    return Settings(**values)


def api_key_from_env(environ: dict[str, str] | None = None) -> str | None:
    """Read the Companies House API key. Never logged, never persisted."""
    env = os.environ if environ is None else environ
    key = env.get(API_KEY_ENV, "").strip()
    return key or None


def fixed_now(environ: dict[str, str] | None = None) -> datetime | None:
    """Optional frozen clock for reproducible runs, from FIRSTPASS_SCREENED_AT.

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
