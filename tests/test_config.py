"""Configuration precedence: CLI flag beats env beats coldscreen.toml beats default."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from coldscreen.config import Settings, api_key_from_env, fixed_now, load_settings


def test_defaults_match_the_documented_values() -> None:
    settings = Settings()
    assert settings.rate_limit_requests == 500
    assert settings.rate_limit_window_seconds == 300.0
    assert settings.cache_ttl_days == 7.0
    assert settings.items_per_page == 100
    assert settings.max_pages_filing_history == 3
    assert settings.max_pages_charges == 10
    assert settings.officer_lookback_years == 5
    assert settings.output_dir == "cases"
    assert settings.ollama_num_ctx == 16384
    # Unset, so the request never carries the field at all.
    assert settings.ollama_think is None
    # Claims extraction caps, per the weekend 3 work order.
    assert settings.max_deck_pages == 40
    assert settings.max_claims_chars == 60000
    assert settings.max_site_response_bytes == 2_000_000


def test_ollama_num_ctx_follows_the_precedence_chain(tmp_path: Path) -> None:
    config = tmp_path / "coldscreen.toml"
    config.write_text("ollama_num_ctx = 8192\n", encoding="utf-8")
    from_toml = load_settings(config_file=config, environ={})
    assert from_toml.ollama_num_ctx == 8192
    from_env = load_settings(config_file=config, environ={"COLDSCREEN_OLLAMA_NUM_CTX": "32768"})
    assert from_env.ollama_num_ctx == 32768
    assert isinstance(from_env.ollama_num_ctx, int)


def test_ollama_think_follows_the_precedence_chain(tmp_path: Path) -> None:
    config = tmp_path / "coldscreen.toml"
    config.write_text("ollama_think = false\n", encoding="utf-8")
    from_toml = load_settings(config_file=config, environ={})
    assert from_toml.ollama_think is False
    from_env = load_settings(config_file=config, environ={"COLDSCREEN_OLLAMA_THINK": "true"})
    assert from_env.ollama_think is True
    from_cli = load_settings(
        config_file=config,
        cli_overrides={"ollama_think": False},
        environ={"COLDSCREEN_OLLAMA_THINK": "true"},
    )
    assert from_cli.ollama_think is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("True", True),
        ("1", True),
        ("false", False),
        ("FALSE", False),
        ("0", False),
        (" false ", False),
    ],
)
def test_ollama_think_env_spellings(raw: str, expected: bool) -> None:
    settings = load_settings(environ={"COLDSCREEN_OLLAMA_THINK": raw})
    assert settings.ollama_think is expected


def test_ollama_think_rejects_an_unreadable_value() -> None:
    with pytest.raises(ValueError, match="expected true or false"):
        load_settings(environ={"COLDSCREEN_OLLAMA_THINK": "maybe"})


def test_toml_overrides_defaults(tmp_path: Path) -> None:
    config = tmp_path / "coldscreen.toml"
    config.write_text('output_dir = "elsewhere"\nitems_per_page = 25\n', encoding="utf-8")
    settings = load_settings(config_file=config, environ={})
    assert settings.output_dir == "elsewhere"
    assert settings.items_per_page == 25
    assert settings.rate_limit_requests == 500


def test_env_overrides_toml(tmp_path: Path) -> None:
    config = tmp_path / "coldscreen.toml"
    config.write_text('output_dir = "from-toml"\n', encoding="utf-8")
    settings = load_settings(config_file=config, environ={"COLDSCREEN_OUTPUT_DIR": "from-env"})
    assert settings.output_dir == "from-env"


def test_cli_overrides_env(tmp_path: Path) -> None:
    config = tmp_path / "coldscreen.toml"
    config.write_text('output_dir = "from-toml"\n', encoding="utf-8")
    settings = load_settings(
        config_file=config,
        cli_overrides={"output_dir": "from-cli"},
        environ={"COLDSCREEN_OUTPUT_DIR": "from-env"},
    )
    assert settings.output_dir == "from-cli"


def test_cli_override_of_none_is_ignored() -> None:
    settings = load_settings(cli_overrides={"output_dir": None}, environ={})
    assert settings.output_dir == "cases"


def test_env_values_are_coerced_to_field_types() -> None:
    settings = load_settings(
        environ={
            "COLDSCREEN_ITEMS_PER_PAGE": "50",
            "COLDSCREEN_CACHE_TTL_DAYS": "1.5",
        }
    )
    assert settings.items_per_page == 50
    assert settings.cache_ttl_days == 1.5


def test_api_key_comes_from_env_only_and_blank_means_absent() -> None:
    assert api_key_from_env({}) is None
    assert api_key_from_env({"COMPANIES_HOUSE_API_KEY": "  "}) is None
    assert api_key_from_env({"COMPANIES_HOUSE_API_KEY": "k"}) == "k"


def test_fixed_now_parses_iso_and_assumes_utc() -> None:
    assert fixed_now({}) is None
    parsed = fixed_now({"COLDSCREEN_SCREENED_AT": "2026-08-18T12:00:00"})
    assert parsed == datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)


def test_fixed_now_converts_aware_input_to_utc() -> None:
    parsed = fixed_now({"COLDSCREEN_SCREENED_AT": "2026-08-18T14:00:00+02:00"})
    assert parsed is not None
    assert parsed.tzinfo == UTC
    assert parsed == datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)


def test_unknown_toml_keys_warn_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "coldscreen.toml"
    config.write_text(
        'output_dir = "fine"\nmisspelled_setting = 1\nanother_typo = "x"\n',
        encoding="utf-8",
    )
    settings = load_settings(config_file=config, environ={})
    err = capsys.readouterr().err
    assert "unknown setting" in err
    assert "misspelled_setting" in err
    assert "another_typo" in err
    assert settings.output_dir == "fine"


def test_cache_ttl_seconds_derives_from_days() -> None:
    assert Settings(cache_ttl_days=2.0).cache_ttl_seconds == 172800.0


# -- validation on load ------------------------------------------------------


def test_max_deck_bytes_default_is_50_mb() -> None:
    assert Settings().max_deck_bytes == 50_000_000


def test_sanctions_threshold_must_be_within_zero_and_one() -> None:
    with pytest.raises(ValueError, match="sanctions_threshold"):
        load_settings(environ={"COLDSCREEN_SANCTIONS_THRESHOLD": "1.5"})
    with pytest.raises(ValueError, match="sanctions_threshold"):
        load_settings(environ={"COLDSCREEN_SANCTIONS_THRESHOLD": "-0.1"})
    # The boundaries themselves are valid.
    assert load_settings(environ={"COLDSCREEN_SANCTIONS_THRESHOLD": "1.0"}).sanctions_threshold
    assert load_settings(environ={"COLDSCREEN_SANCTIONS_THRESHOLD": "0"}).sanctions_threshold == 0


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("items_per_page", "0"),
        ("rate_limit_requests", "-1"),
        ("max_pages_officers", "0"),
        ("max_pages_charges", "0"),
        ("max_deck_bytes", "0"),
        ("media_results_per_query", "0"),
        ("ollama_num_ctx", "-5"),
    ],
)
def test_positive_integer_settings_reject_zero_and_negatives(name: str, value: str) -> None:
    with pytest.raises(ValueError, match=name):
        load_settings(environ={f"COLDSCREEN_{name.upper()}": value})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("timeout_seconds", "0"),
        ("rate_limit_window_seconds", "-3"),
        ("ollama_timeout_seconds", "0"),
    ],
)
def test_positive_float_settings_reject_zero_and_negatives(name: str, value: str) -> None:
    with pytest.raises(ValueError, match=name):
        load_settings(environ={f"COLDSCREEN_{name.upper()}": value})


def test_negative_cache_ttl_is_rejected_but_zero_is_allowed() -> None:
    with pytest.raises(ValueError, match="cache_ttl_days"):
        load_settings(environ={"COLDSCREEN_CACHE_TTL_DAYS": "-1"})
    assert load_settings(environ={"COLDSCREEN_CACHE_TTL_DAYS": "0"}).cache_ttl_days == 0


def test_uncoercible_value_error_names_the_setting(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="items_per_page"):
        load_settings(environ={"COLDSCREEN_ITEMS_PER_PAGE": "many"})
    config = tmp_path / "coldscreen.toml"
    config.write_text('timeout_seconds = "soon"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="timeout_seconds"):
        load_settings(config_file=config, environ={})
