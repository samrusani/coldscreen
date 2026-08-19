"""Language rules: rendered memos never use the banned accusatory terms."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from firstpass.casedir import load_casefile
from firstpass.language import find_banned_terms
from firstpass.models import MediaItem, MediaScreening
from firstpass.render import render_memo

from .conftest import FIXTURES_DIR

REPO_ROOT = Path(__file__).parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_language.py"
FIXTURE_CASE_DIR = FIXTURES_DIR / "case-fabricated-widgets-ltd-99999999"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_language", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_freshly_rendered_fixture_memo_is_clean() -> None:
    module = load_script()
    casefile = load_casefile(FIXTURE_CASE_DIR)
    memo = render_memo(casefile)
    # The script and the library share one helper; both agree the memo is clean.
    assert module.find_banned_terms(memo) == []
    assert find_banned_terms(memo) == []


def test_check_language_passes_on_fixture_memo_and_templates() -> None:
    module = load_script()
    template_dir = REPO_ROOT / "src" / "firstpass" / "templates"
    targets = [str(FIXTURE_CASE_DIR / "memo.md")]
    targets.extend(str(p) for p in sorted(template_dir.rglob("*")) if p.is_file())
    assert module.main(targets) == 0


def test_check_language_fails_on_banned_terms(tmp_path: Path) -> None:
    module = load_script()
    bad = tmp_path / "memo.md"
    bad.write_text("The director committed " + "fra" + "ud here.\n", encoding="utf-8")
    assert module.main([str(bad)]) == 1


def test_check_language_matches_whole_words_only(tmp_path: Path) -> None:
    module = load_script()
    fine = tmp_path / "memo.md"
    # Banned terms appear only as substrings of longer, harmless words.
    fine.write_text("Scampi, shambolic spreadsheets, and crooked columns.\n", encoding="utf-8")
    assert module.main([str(fine)]) == 0


def test_multiword_terms_are_caught(tmp_path: Path) -> None:
    module = load_script()
    bad = tmp_path / "memo.md"
    bad.write_text("He is a con  artist, allegedly.\n", encoding="utf-8")
    assert module.main([str(bad)]) == 1


def test_explicitly_passed_missing_file_is_an_error(tmp_path: Path) -> None:
    module = load_script()
    missing = tmp_path / "does-not-exist.md"
    assert module.main([str(missing)]) == 1


# -- the URL exemption ----------------------------------------------------------


def test_urls_are_exempt_from_the_banned_scan() -> None:
    url_only = "Source: https://fictional-gazette.example/business/fraud-probe-widgets"
    assert find_banned_terms(url_only) == []


def test_prose_is_still_gated_next_to_an_exempt_url() -> None:
    text = "Coverage of the fraud inquiry: https://fictional-gazette.example/fraud-probe"
    assert find_banned_terms(text) == ["fraud"]


def test_check_language_script_shares_the_url_exemption(tmp_path: Path) -> None:
    module = load_script()
    clean = tmp_path / "memo.md"
    clean.write_text(
        "| gazette.example | 2026-05-14 | misconduct |"
        " https://gazette.example/fraud-probe-widgets |\n",
        encoding="utf-8",
    )
    assert module.main([str(clean)]) == 0
    dirty = tmp_path / "memo2.md"
    dirty.write_text(
        "The company faces a fraud inquiry: https://gazette.example/fraud-probe\n",
        encoding="utf-8",
    )
    assert module.main([str(dirty)]) == 1


def test_memo_with_banned_word_in_media_url_slug_passes() -> None:
    """The rendered media section may cite URLs whose slugs carry banned
    vocabulary; the memo's own prose stays gated."""
    casefile = load_casefile(FIXTURE_CASE_DIR)
    item = MediaItem(
        title="A fictional headline that stays out of the memo",
        url="https://fictional-gazette.example/business/fraud-probe-widgets",
        source_domain="fictional-gazette.example",
        published="2026-05-14",
        query_category="misconduct",
    )
    casefile = casefile.model_copy(
        update={
            "media": MediaScreening(
                performed=True, provider="tavily", results_per_query=5, items=[item]
            )
        }
    )
    memo = render_memo(casefile)
    assert "https://fictional-gazette.example/business/fraud-probe-widgets" in memo
    assert find_banned_terms(memo) == []
    # The same word in prose is still a hit.
    assert find_banned_terms(memo + "\nA fraud inquiry is open.\n") == ["fraud"]
