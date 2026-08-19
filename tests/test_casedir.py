"""Case directory hygiene: slugs, number validation, param stripping, and
the refusal to write through a symbolic link."""

from __future__ import annotations

from pathlib import Path

import pytest

from coldscreen.casedir import (
    UnsafeCasePath,
    case_dir_name,
    refuse_symlink,
    sanitize_params,
    slugify,
    validate_company_number,
    write_case_text,
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


def test_write_case_text_writes_and_replaces_a_plain_file(tmp_path: Path) -> None:
    target = tmp_path / "memo.md"
    write_case_text(target, "first\n")
    write_case_text(target, "second\n")
    assert target.read_bytes() == b"second\n"


def test_write_case_text_refuses_a_symlink_and_leaves_the_target_alone(tmp_path: Path) -> None:
    """The whole point: a validated directory does not validate the write.

    The kernel resolves the final name again at open time, so a link at a
    tool-owned name would otherwise redirect the write out of the directory.
    """
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"do not touch\n")
    link = tmp_path / "memo.md"
    link.symlink_to(victim)

    with pytest.raises(UnsafeCasePath):
        write_case_text(link, "clobbered\n")
    assert victim.read_bytes() == b"do not touch\n"


def test_refuse_symlink_passes_a_real_directory_and_refuses_a_link(tmp_path: Path) -> None:
    real = tmp_path / "case"
    real.mkdir()
    refuse_symlink(real, "case directory")

    link = tmp_path / "linked-case"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(UnsafeCasePath):
        refuse_symlink(link, "case directory")
