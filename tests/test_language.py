"""Language rules: rendered memos never use the banned accusatory terms."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from firstpass.casedir import load_casefile
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
    hits = [match.group(0) for match in module.BANNED_PATTERN.finditer(memo)]
    assert hits == []


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
