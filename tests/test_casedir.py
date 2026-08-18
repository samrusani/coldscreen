"""Case directory hygiene: slugs, number validation, param stripping."""

from __future__ import annotations

import pytest

from firstpass.casedir import (
    case_dir_name,
    sanitize_params,
    slugify,
    validate_company_number,
)


def test_slugify_flattens_punctuation_and_case() -> None:
    assert slugify("FABRICATED WIDGETS LTD") == "fabricated-widgets-ltd"
    assert slugify("Fictional & Sons (Holdings) plc") == "fictional-sons-holdings-plc"
    assert slugify("  ") == "company"


def test_slugify_truncates_very_long_names() -> None:
    assert len(slugify("x" * 500)) <= 60


def test_case_dir_name_combines_slug_and_number() -> None:
    assert case_dir_name("FABRICATED WIDGETS LTD", "99999999") == (
        "fabricated-widgets-ltd-99999999"
    )


@pytest.mark.parametrize(
    "bad_number",
    [
        "../../evil",
        "..",
        "99999999/extra",
        "9999 9999",
        "",
        "99999999999",  # more than 10 characters
    ],
)
def test_company_numbers_that_are_not_plain_alphanumeric_are_rejected(bad_number: str) -> None:
    with pytest.raises(ValueError):
        validate_company_number(bad_number)
    with pytest.raises(ValueError):
        case_dir_name("Evil Ltd", bad_number)


def test_valid_company_numbers_pass_validation() -> None:
    assert validate_company_number("99999999") == "99999999"
    assert validate_company_number("SC123456") == "SC123456"


def test_sanitize_params_strips_credential_shaped_names() -> None:
    params = {
        "q": "fabricated widgets",
        "items_per_page": "100",
        "api_key": "should-never-happen",
        "Authorization": "also-never",
        "access_token": "never",
    }
    cleaned = sanitize_params(params)
    assert cleaned == {"q": "fabricated widgets", "items_per_page": "100"}
