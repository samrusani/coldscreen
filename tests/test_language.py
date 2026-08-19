"""Language rules: rendered memos never use the banned accusatory terms.

The one exemption is quoted data: the casefile's stored claim texts are the
company's own words and pass the gate span-exactly; everything around them
stays gated. scripts/check_language.py builds the same exemptions from the
sibling casefile.json of each memo it scans.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

from firstpass.casedir import load_casefile
from firstpass.language import find_banned_terms, normalize_for_match
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


# -- the quoted-data exemption ----------------------------------------------------


QUOTED = "Our platform eliminates fraud in widget procurement"


def test_exact_quoted_claim_text_is_exempt() -> None:
    text = f'The deck says "{QUOTED}" on page two.'
    assert find_banned_terms(text) == ["fraud"]
    assert find_banned_terms(text, (QUOTED,)) == []


def test_same_word_outside_the_quoted_span_still_hits() -> None:
    text = f'The deck says "{QUOTED}", which smells of fraud.'
    assert find_banned_terms(text, (QUOTED,)) == ["fraud"]


def test_every_occurrence_of_the_quoted_text_is_exempt() -> None:
    text = f"{QUOTED} and again: {QUOTED}"
    assert find_banned_terms(text, (QUOTED,)) == []


def test_partial_overlap_with_the_quoted_text_is_not_exempt() -> None:
    # The banned word must lie ENTIRELY inside an exact occurrence; a prefix
    # of the stored text ending in the banned word does not appear here.
    text = "Our platform eliminates fraud"  # not the full stored string
    assert find_banned_terms(text, (QUOTED,)) == ["fraud"]


def test_overlapping_occurrences_cannot_union_into_wider_coverage() -> None:
    """Review fix F4: stored "fraud fraud" against prose "fraud fraud fraud"
    must still flag the third token. Occurrence discovery advances by the
    match length, so self-overlap cannot widen the exempt region."""
    assert find_banned_terms("fraud fraud fraud", ("fraud fraud",)) == ["fraud"]
    # Two genuinely disjoint occurrences are both exempt.
    assert find_banned_terms("fraud fraud and fraud fraud", ("fraud fraud",)) == []


def test_normalize_for_match_folds_case_whitespace_quotes_and_dashes() -> None:
    curly = "It\u2019s a \u201cWidget\u2013Grade\u201d  PLATFORM"
    straight = 'it\'s a "widget-grade" platform'
    assert normalize_for_match(curly) == straight
    assert normalize_for_match("  a\n\tb  ") == "a b"


def test_blank_exempt_texts_exempt_nothing() -> None:
    assert find_banned_terms("a scam, plainly", ("", "  ")) == ["scam"]


def test_exemption_only_covers_the_named_terms_span() -> None:
    two_claims = ("we stop fraud", "no scam here")
    text = "we stop fraud but also a sham entirely outside any quote"
    assert find_banned_terms(text, two_claims) == ["sham"]


def _write_evidence(case_dir: Path, deck_pages: dict[str, str]) -> None:
    evidence_dir = case_dir / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    (evidence_dir / "deck_text.json").write_text(
        json.dumps(
            {
                "name": "deck_text",
                "url": "firstpass:deck/fixture.pdf",
                "body": {"kind": "deck_text", "pages": deck_pages},
            }
        ),
        encoding="utf-8",
    )


def test_check_language_honors_only_evidence_verified_claim_exemptions(tmp_path: Path) -> None:
    """A memo whose claims table quotes banned vocabulary passes only when
    the sibling casefile stores that claim AND the sibling evidence proves
    the text really came from the extracted source material."""
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text(f'| 1 | "{QUOTED}" (deck p.2) |  | not checkable |\n', encoding="utf-8")
    # Without a casefile: full strictness, the quote is a hit.
    assert module.main([str(memo)]) == 1
    # Casefile alone is NOT enough: it is an editable file, so with no
    # evidence to verify against, the scan stays strict.
    (case_dir / "casefile.json").write_text(
        json.dumps({"claims": [{"id": "CLM-001", "text": QUOTED}]}), encoding="utf-8"
    )
    assert module.main([str(memo)]) == 1
    # With the evidence carrying the text, the quotation verifies: clean.
    _write_evidence(case_dir, {"2": f"Slide two. {QUOTED}. More slide two."})
    assert module.main([str(memo)]) == 0
    # The exemption never covers prose outside the quotation.
    memo.write_text(
        f'| 1 | "{QUOTED}" (deck p.2) |  | not checkable |\nPlain fraud in prose.\n',
        encoding="utf-8",
    )
    assert module.main([str(memo)]) == 1


def test_check_language_ignores_a_tampered_claim_not_in_evidence(tmp_path: Path) -> None:
    """A hand-added casefile claim that the evidence does not contain buys
    no exemption: the scan flags its vocabulary."""
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text('| 1 | "a total scam operation" (deck p.1) |  | not checkable |\n')
    (case_dir / "casefile.json").write_text(
        json.dumps(
            {
                "claims": [
                    {"id": "CLM-001", "text": "a total scam operation"},
                    {"id": "CLM-002", "text": QUOTED},
                ]
            }
        ),
        encoding="utf-8",
    )
    # Evidence contains only the legitimate quote, not the tampered one.
    _write_evidence(case_dir, {"2": QUOTED})
    assert module.main([str(memo)]) == 1


def test_check_language_site_text_records_also_verify(tmp_path: Path) -> None:
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text(f'| 1 | "{QUOTED}" (site /about) |  | not checkable |\n', encoding="utf-8")
    (case_dir / "casefile.json").write_text(
        json.dumps({"claims": [{"id": "CLM-001", "text": QUOTED}]}), encoding="utf-8"
    )
    evidence_dir = case_dir / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "site_001.json").write_text(
        json.dumps(
            {
                "name": "site_001",
                "url": "https://widgets.example/about",
                "body": {"kind": "site_text", "text": f"About us. {QUOTED}."},
            }
        ),
        encoding="utf-8",
    )
    assert module.main([str(memo)]) == 0


def test_check_language_ignores_a_corrupt_sibling_casefile(tmp_path: Path) -> None:
    """A corrupt casefile yields no exemptions: fail-closed, not fail-open."""
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text(f'"{QUOTED}"\n', encoding="utf-8")
    (case_dir / "casefile.json").write_text("{ not json", encoding="utf-8")
    assert module.main([str(memo)]) == 1


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
