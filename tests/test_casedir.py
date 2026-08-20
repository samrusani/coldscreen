"""Case directory hygiene: slugs, number validation, param stripping, and
the refusal to write through a symbolic link."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coldscreen.casedir import (
    UnsafeCasePath,
    case_dir_name,
    fetch_log_row,
    load_casefile,
    refuse_symlink,
    sanitize_params,
    slugify,
    validate_company_number,
    write_case,
    write_case_text,
)
from coldscreen.ch_client import FetchRecord
from coldscreen.stages.registry import NamedRecord

from .conftest import FIXTURES_DIR

FIXTURE_CASE_DIR = FIXTURES_DIR / "case-fabricated-widgets-ltd-99999999"
SCREENED_AT = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _named_record(**param_overrides: str) -> NamedRecord:
    params = {"q": "fabricated widgets", **param_overrides}
    return NamedRecord(
        "search_companies",
        FetchRecord(
            url="https://api.company-information.service.gov.uk/search/companies",
            params=params,
            status=200,
            body={"items": []},
            retrieved_at=SCREENED_AT,
            from_cache=False,
        ),
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


def test_fetch_log_row_strips_credential_shaped_params() -> None:
    named = _named_record(api_key="should-never-happen", access_token="never")
    row = fetch_log_row(named)
    assert row["name"] == "search_companies"
    assert row["url"] == named.record.url
    assert row["params"] == {"q": "fabricated widgets"}
    assert row["status"] == 200
    assert row["retrieved_at"] == SCREENED_AT.isoformat()
    assert row["from_cache"] is False
    assert "body" not in row
    assert "file" not in row
    assert "api_key" not in row["params"]
    assert "access_token" not in row["params"]


def test_write_case_writes_fetch_log(tmp_path: Path) -> None:
    case_dir = tmp_path / "case"
    named = _named_record()
    write_case(case_dir, load_casefile(FIXTURE_CASE_DIR), [named], "# memo\n")

    log_path = case_dir / "fetch_log.json"
    log = json.loads(log_path.read_text(encoding="utf-8"))
    index = json.loads((case_dir / "evidence" / "index.json").read_text(encoding="utf-8"))
    assert [row["name"] for row in log] == [entry["name"] for entry in index]
    assert log == [fetch_log_row(named)]
    assert log_path.read_text(encoding="utf-8").endswith("\n")


def test_write_case_refuses_symlinked_fetch_log_before_any_write(tmp_path: Path) -> None:
    """A link at fetch_log.json must not rewrite memo, casefile, or the index."""
    case_dir = tmp_path / "case"
    named = _named_record()
    write_case(case_dir, load_casefile(FIXTURE_CASE_DIR), [named], "# first\n")

    memo_before = (case_dir / "memo.md").read_bytes()
    casefile_before = (case_dir / "casefile.json").read_bytes()
    index_before = (case_dir / "evidence" / "index.json").read_bytes()

    victim = tmp_path / "elsewhere" / "precious.json"
    victim.parent.mkdir()
    victim.write_bytes(b"do not touch\n")
    (case_dir / "fetch_log.json").unlink()
    (case_dir / "fetch_log.json").symlink_to(victim)

    with pytest.raises(UnsafeCasePath, match="symbolic link"):
        write_case(case_dir, load_casefile(FIXTURE_CASE_DIR), [named], "# second\n")
    assert victim.read_bytes() == b"do not touch\n"
    assert (case_dir / "memo.md").read_bytes() == memo_before
    assert (case_dir / "casefile.json").read_bytes() == casefile_before
    assert (case_dir / "evidence" / "index.json").read_bytes() == index_before
    assert (case_dir / "fetch_log.json").is_symlink()
