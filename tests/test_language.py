"""Language rules: rendered memos and tool-authored casefile fields.

Quoted data is the one exemption on memos: the casefile's stored claim
texts are the company's own words and pass the memo gate span-exactly;
everything around them stays gated. coldscreen.check_language builds those
exemptions from the sibling casefile.json of each memo it scans, after
re-verifying each claim against its declared source label in sibling
evidence. scripts/check_language.py is a thin wrapper.

The same script also parses casefile.json and scans the tool-authored
fields. Identity exemptions apply there after the same evidence
re-verification. Claim-quote exemptions do not.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from coldscreen.casedir import load_casefile
from coldscreen.language import (
    REGISTERED_OFFICE_ADDRESS_KEYS,
    claim_quote_is_verified,
    claim_text_has_substance,
    code_fetched_exemption_texts,
    evidence_sections,
    find_banned_terms,
    find_banned_terms_in_memo,
    normalize_for_match,
)
from coldscreen.media import QUERY_CATEGORIES, source_domain
from coldscreen.models import REGISTERED_OFFICE_ADDRESS_KEYS as MODEL_OFFICE_KEYS
from coldscreen.models import (
    CoAppointmentOverlap,
    CompanyProfile,
    MediaItem,
    MediaScreening,
)
from coldscreen.pipeline import language_backstop_failure
from coldscreen.render import render_memo
from coldscreen.site import SitePage, _display_path

from .conftest import FIXTURES_DIR

REPO_ROOT = Path(__file__).parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_language.py"
FIXTURE_CASE_DIR = FIXTURES_DIR / "case-fabricated-widgets-ltd-99999999"
GOLDEN_DIR = FIXTURES_DIR / "golden"
CLAIMS_TABLE_HEADER = "| # | Claim (source) | Public record | Status |"


def load_script() -> ModuleType:
    from coldscreen import check_language

    return check_language


def test_freshly_rendered_fixture_memo_is_clean() -> None:
    module = load_script()
    casefile = load_casefile(FIXTURE_CASE_DIR)
    memo = render_memo(casefile)
    # The script and the library share one helper; both agree the memo is clean.
    assert module.find_banned_terms(memo) == []
    assert find_banned_terms(memo) == []


def test_check_language_passes_on_fixture_memo_and_templates() -> None:
    module = load_script()
    template_dir = REPO_ROOT / "src" / "coldscreen" / "templates"
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


def test_claim_text_has_substance_requires_two_tokens() -> None:
    assert claim_text_has_substance("fraud") is False
    assert claim_text_has_substance("Fraud") is False
    assert claim_text_has_substance("  FRAUD\n") is False
    assert claim_text_has_substance("debt free") is True
    assert claim_text_has_substance("we fight fraud") is True
    assert claim_text_has_substance("") is False
    assert claim_text_has_substance("   \n\t  ") is False


def test_claim_quote_is_verified_is_label_and_substance_aware() -> None:
    sections = {
        "deck p.1": "Slide one mentions a fraud on this page only.",
        "deck p.2": "Slide two has no banned two-token phrase.",
    }
    assert claim_quote_is_verified("a fraud", "deck p.1", sections) is True
    assert claim_quote_is_verified("a fraud", "deck p.2", sections) is False
    assert claim_quote_is_verified("a fraud", "deck p.99", sections) is False
    assert claim_quote_is_verified("fraud", "deck p.1", sections) is False
    assert claim_quote_is_verified("", "deck p.1", sections) is False
    assert claim_quote_is_verified("a fraud", "", sections) is False
    assert claim_quote_is_verified("a fraud", "deck p.1", {}) is False


def test_language_backstop_without_evidence_dir_uses_substance_only() -> None:
    """Screen / in-memory polarity: no evidence dir means substance filter
    only. A two-token planted claim is still an exemption span until
    rerun passes a path."""
    casefile = load_casefile(GOLDEN_DIR)
    claims = list(casefile.claims)
    claims[0] = claims[0].model_copy(update={"text": "a fraud", "source": "deck p.2"})
    planted = casefile.model_copy(update={"claims": claims})
    memo = render_memo(planted)
    assert "a fraud" in memo
    assert language_backstop_failure(memo, planted) is None
    assert language_backstop_failure(memo, planted, GOLDEN_DIR / "evidence") is not None
    assert evidence_sections(GOLDEN_DIR / "evidence")["deck p.2"]


def test_blank_exempt_texts_exempt_nothing() -> None:
    assert find_banned_terms("a scam, plainly", ("", "  ")) == ["scam"]


def test_exemption_only_covers_the_named_terms_span() -> None:
    two_claims = ("we stop fraud", "no scam here")
    text = "we stop fraud but also a sham entirely outside any quote"
    assert find_banned_terms(text, two_claims) == ["sham"]


# -- claims-table region scope on the whole-memo helper -------------------------


def test_memo_helper_hits_when_the_claims_table_heading_is_missing() -> None:
    memo = "The filing pattern is fraud.\n"
    assert find_banned_terms_in_memo(memo, claim_texts=("fraud",)) == ["fraud"]


def test_memo_helper_hits_when_the_claims_table_heading_has_no_closer() -> None:
    memo = f'## Claims vs evidence\n{CLAIMS_TABLE_HEADER}\n| "fraud" |\nNo following heading.\n'
    assert find_banned_terms_in_memo(memo, claim_texts=("fraud",)) == ["fraud"]


def test_memo_helper_exempts_a_claim_only_inside_the_table_region() -> None:
    memo = (
        f'## Claims vs evidence\n{CLAIMS_TABLE_HEADER}\n| "fraud" |\n'
        "## Findings\nThe filing pattern is fraud.\n"
    )
    assert find_banned_terms_in_memo(memo, claim_texts=("fraud",)) == ["fraud"]


def test_memo_helper_uses_only_the_first_heading_pair() -> None:
    memo = (
        f'## Claims vs evidence\n{CLAIMS_TABLE_HEADER}\n| "fraud" |\n## Narrative\nclean\n'
        f'## Claims vs evidence\n| "fraud" |\n## Findings\n'
    )
    assert find_banned_terms_in_memo(memo, claim_texts=("fraud",)) == ["fraud"]


def test_memo_helper_h3_does_not_close_the_region() -> None:
    memo = (
        f'## Claims vs evidence\n{CLAIMS_TABLE_HEADER}\n| "fraud" |\n'
        "### Red\nstill in the region\n## Findings\n"
    )
    assert find_banned_terms_in_memo(memo, claim_texts=("fraud",)) == []


def test_memo_helper_trailing_space_on_heading_fails_closed() -> None:
    memo = f'## Claims vs evidence \n{CLAIMS_TABLE_HEADER}\n| "fraud" |\n## Findings\n'
    assert find_banned_terms_in_memo(memo, claim_texts=("fraud",)) == ["fraud"]


def test_memo_helper_heading_without_opener_is_skipped() -> None:
    memo = (
        "## Claims vs evidence\nThis is model prose, not an opener.\n"
        f'## Claims vs evidence\n{CLAIMS_TABLE_HEADER}\n| "fraud" |\n'
        "## Findings\nThe filing pattern is fraud.\n"
    )
    assert find_banned_terms_in_memo(memo, claim_texts=("fraud",)) == ["fraud"]


def _write_evidence(case_dir: Path, deck_pages: dict[str, str]) -> None:
    evidence_dir = case_dir / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    (evidence_dir / "deck_text.json").write_text(
        json.dumps(
            {
                "name": "deck_text",
                "url": "coldscreen:deck/fixture.pdf",
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
        json.dumps({"claims": [{"id": "CLM-001", "text": QUOTED, "source": "deck p.2"}]}),
        encoding="utf-8",
    )
    assert module.main([str(memo)]) == 1
    # With the evidence carrying the text on the declared page, clean.
    _write_evidence(case_dir, {"2": f"Slide two. {QUOTED}. More slide two."})
    assert module.main([str(memo)]) == 0
    # The exemption never covers prose outside the quotation.
    memo.write_text(
        f'| 1 | "{QUOTED}" (deck p.2) |  | not checkable |\nPlain fraud in prose.\n',
        encoding="utf-8",
    )
    assert module.main([str(memo)]) == 1


def test_claim_exemptions_ignore_thin_texts_even_when_verified(tmp_path: Path) -> None:
    """A planted single-word claim that re-verifies against evidence is
    still not an exemption: the CI helper returns no such span."""
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text('| 1 | "fraud" (deck p.2) |  | not checkable |\n', encoding="utf-8")
    (case_dir / "casefile.json").write_text(
        json.dumps({"claims": [{"id": "CLM-001", "text": "fraud", "source": "deck p.2"}]}),
        encoding="utf-8",
    )
    _write_evidence(case_dir, {"2": "Slide two mentions fraud in the deck copy."})
    assert module.claim_exemptions(memo) == ()
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
                    {"id": "CLM-001", "text": "a total scam operation", "source": "deck p.1"},
                    {"id": "CLM-002", "text": QUOTED, "source": "deck p.2"},
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
        json.dumps({"claims": [{"id": "CLM-001", "text": QUOTED, "source": "site /about"}]}),
        encoding="utf-8",
    )
    evidence_dir = case_dir / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "site_001.json").write_text(
        json.dumps(
            {
                "name": "site_001",
                "url": "https://widgets.example/about",
                "body": {
                    "kind": "site_text",
                    "url": "https://widgets.example/about",
                    "text": f"About us. {QUOTED}.",
                },
            }
        ),
        encoding="utf-8",
    )
    assert module.main([str(memo)]) == 0


def test_claim_sourced_deck_p2_does_not_use_page_one(tmp_path: Path) -> None:
    """A deck p.2 claim whose text lives only on page 1 is not exempt."""
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text(f'| 1 | "{QUOTED}" (deck p.2) |  | not checkable |\n', encoding="utf-8")
    (case_dir / "casefile.json").write_text(
        json.dumps({"claims": [{"id": "CLM-001", "text": QUOTED, "source": "deck p.2"}]}),
        encoding="utf-8",
    )
    _write_evidence(case_dir, {"1": f"Slide one. {QUOTED}."})
    assert module.claim_exemptions(memo) == ()
    assert module.main([str(memo)]) == 1


def test_claim_sourced_deck_p2_does_not_use_site_text(tmp_path: Path) -> None:
    """A deck p.2 claim whose text lives only in site_text is not exempt."""
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text(f'| 1 | "{QUOTED}" (deck p.2) |  | not checkable |\n', encoding="utf-8")
    (case_dir / "casefile.json").write_text(
        json.dumps({"claims": [{"id": "CLM-001", "text": QUOTED, "source": "deck p.2"}]}),
        encoding="utf-8",
    )
    evidence_dir = case_dir / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "site_001.json").write_text(
        json.dumps(
            {
                "name": "site_001",
                "url": "https://widgets.example/about",
                "body": {
                    "kind": "site_text",
                    "url": "https://widgets.example/about",
                    "text": f"About us. {QUOTED}.",
                },
            }
        ),
        encoding="utf-8",
    )
    assert module.claim_exemptions(memo) == ()
    assert module.main([str(memo)]) == 1


def test_claim_sourced_site_about_does_not_use_homepage(tmp_path: Path) -> None:
    """A site /about claim whose text lives only on site / is not exempt."""
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text(f'| 1 | "{QUOTED}" (site /about) |  | not checkable |\n', encoding="utf-8")
    (case_dir / "casefile.json").write_text(
        json.dumps({"claims": [{"id": "CLM-001", "text": QUOTED, "source": "site /about"}]}),
        encoding="utf-8",
    )
    evidence_dir = case_dir / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "site_001.json").write_text(
        json.dumps(
            {
                "name": "site_001",
                "url": "https://widgets.example",
                "body": {
                    "kind": "site_text",
                    "url": "https://widgets.example",
                    "text": f"Home. {QUOTED}.",
                },
            }
        ),
        encoding="utf-8",
    )
    assert module.claim_exemptions(memo) == ()
    assert module.main([str(memo)]) == 1


def test_claim_without_source_gets_no_exemption(tmp_path: Path) -> None:
    """A claim with no source key is not exempt, even when the text is
    in the evidence."""
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text(f'| 1 | "{QUOTED}" (deck p.2) |  | not checkable |\n', encoding="utf-8")
    (case_dir / "casefile.json").write_text(
        json.dumps({"claims": [{"id": "CLM-001", "text": QUOTED}]}),
        encoding="utf-8",
    )
    _write_evidence(case_dir, {"2": f"Slide two. {QUOTED}."})
    assert module.claim_exemptions(memo) == ()
    assert module.main([str(memo)]) == 1


def test_reconstructed_site_label_matches_sitepage_display_path() -> None:
    """The CI helper copies the site path rule; it must match SitePage."""
    module = load_script()
    about = "https://placid-meridian.example/about"
    home = "https://placid-meridian.example"
    assert module._display_path(about) == _display_path(about)
    assert (
        module._site_source_label(about)
        == SitePage(url=about, path=_display_path(about), status=200, text="").source_label
    )
    assert module._site_source_label(home) == "site /"
    homepage = SitePage(url=home, path=_display_path(home), status=200, text="")
    assert homepage.source_label == "site /"


def test_claim_exemptions_use_requested_url_not_final_url(tmp_path: Path) -> None:
    """A redirect landing on /about must not honor a site /about claim
    when the requested URL was the homepage. The claims stage labeled
    the request, not the landing."""
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text(f'| 1 | "{QUOTED}" (site /about) |  | not checkable |\n', encoding="utf-8")
    (case_dir / "casefile.json").write_text(
        json.dumps({"claims": [{"id": "CLM-001", "text": QUOTED, "source": "site /about"}]}),
        encoding="utf-8",
    )
    evidence_dir = case_dir / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "site_001.json").write_text(
        json.dumps(
            {
                "name": "site_001",
                "url": "https://widgets.example",
                "body": {
                    "kind": "site_text",
                    "url": "https://widgets.example",
                    "final_url": "https://widgets.example/about",
                    "text": f"About us. {QUOTED}.",
                },
            }
        ),
        encoding="utf-8",
    )
    assert module.claim_exemptions(memo) == ()
    assert module.main([str(memo)]) == 1


def test_check_language_ignores_a_corrupt_sibling_casefile(tmp_path: Path) -> None:
    """A corrupt casefile yields no exemptions: fail-closed, not fail-open."""
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text(f'"{QUOTED}"\n', encoding="utf-8")
    (case_dir / "casefile.json").write_text("{ not json", encoding="utf-8")
    assert module.main([str(memo)]) == 1


# -- the registry identity exemption ----------------------------------------------


def _write_registry_evidence(case_dir: Path, company_name: str, officer_name: str) -> None:
    evidence_dir = case_dir / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    (evidence_dir / "registry_profile.json").write_text(
        json.dumps(
            {
                "name": "registry_profile",
                "url": "https://api.company-information.service.gov.uk/company/99999998",
                "body": {
                    "company_name": company_name,
                    "company_number": "99999998",
                    "previous_company_names": [{"name": "OLD SHAM NAME LTD"}],
                },
            }
        ),
        encoding="utf-8",
    )
    (evidence_dir / "officers_p1.json").write_text(
        json.dumps(
            {
                "name": "officers_p1",
                "url": "https://api.company-information.service.gov.uk/company/99999998/officers",
                "body": {"items": [{"name": officer_name}]},
            }
        ),
        encoding="utf-8",
    )


def _write_identity_casefile(case_dir: Path, company_name: str, officer_name: str) -> None:
    (case_dir / "casefile.json").write_text(
        json.dumps(
            {
                "subject": {
                    "company_name": company_name,
                    "previous_company_names": [{"name": "OLD SHAM NAME LTD"}],
                },
                "officers": [{"name": officer_name}],
                "pscs": [],
                "claims": [],
            }
        ),
        encoding="utf-8",
    )


def test_check_language_honors_evidence_verified_identity_names(tmp_path: Path) -> None:
    """A registered name carrying a banned word passes only when the
    sibling registry evidence proves the register really spells it that
    way; the same word outside the name spans still hits."""
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text(
        "# Screening memo: TOTAL SHAM TRADING LTD\n"
        "| CROOK, Cuthbert | director |\n"
        "Previously OLD SHAM NAME LTD.\n",
        encoding="utf-8",
    )
    # No casefile: full strictness.
    assert module.main([str(memo)]) == 1
    # Casefile alone is not enough: it is an editable file.
    _write_identity_casefile(case_dir, "TOTAL SHAM TRADING LTD", "CROOK, Cuthbert")
    assert module.main([str(memo)]) == 1
    # With registry evidence carrying the same names, the memo verifies.
    _write_registry_evidence(case_dir, "TOTAL SHAM TRADING LTD", "CROOK, Cuthbert")
    assert module.main([str(memo)]) == 0
    # Prose outside the exact name spans stays gated.
    memo.write_text(
        "# Screening memo: TOTAL SHAM TRADING LTD\nThis is a sham operation.\n",
        encoding="utf-8",
    )
    assert module.main([str(memo)]) == 1


def test_check_language_ignores_an_identity_name_the_evidence_does_not_carry(
    tmp_path: Path,
) -> None:
    """A hand-added officer name buys no exemption when the registry
    evidence never returned it."""
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text("| CROOK, Cuthbert | director |\n", encoding="utf-8")
    _write_identity_casefile(case_dir, "PLAIN TRADING LTD", "CROOK, Cuthbert")
    # Evidence carries the company but a DIFFERENT officer name.
    _write_registry_evidence(case_dir, "PLAIN TRADING LTD", "HONEST, Henrietta")
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


# -- casefile.json field scan -----------------------------------------------------


def _finding(statement: str) -> dict[str, Any]:
    """A finding payload that keeps Finding.evidence at min length 1."""
    return {
        "id": "REG-001",
        "stage": "registry",
        "severity": "info",
        "confidence": "confirmed",
        "statement": statement,
        "evidence": [
            {
                "source_url": "https://registry.example/company/99999998",
                "retrieved_at": "2026-08-20T00:00:00+00:00",
            }
        ],
    }


def _write_tmp_casefile(directory: Path, payload: dict[str, Any]) -> Path:
    path = directory / "casefile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_default_targets_include_fixture_casefiles() -> None:
    module = load_script()
    targets = module.default_targets(REPO_ROOT)
    fixture_casefiles = sorted(FIXTURES_DIR.rglob("casefile.json"))
    assert fixture_casefiles
    for path in fixture_casefiles:
        assert path in targets
    for path in sorted(FIXTURES_DIR.rglob("memo.md")):
        assert path in targets
    templates = REPO_ROOT / "src" / "coldscreen" / "templates"
    for path in templates.rglob("*"):
        if path.is_file():
            assert path in targets


def test_check_language_passes_on_fixture_casefiles_and_memos() -> None:
    module = load_script()
    targets = [str(p) for p in sorted(FIXTURES_DIR.rglob("casefile.json"))]
    targets.extend(str(p) for p in sorted(FIXTURES_DIR.rglob("memo.md")))
    template_dir = REPO_ROOT / "src" / "coldscreen" / "templates"
    targets.extend(str(p) for p in sorted(template_dir.rglob("*")) if p.is_file())
    assert module.main(targets) == 0


@pytest.mark.parametrize(
    ("field_path", "payload"),
    [
        (
            "findings[0].statement",
            {"findings": [_finding("The director committed fraud.")]},
        ),
        ("narrative", {"narrative": "The public record shows fraud."}),
        (
            "verdict.rationale",
            {
                "verdict": {
                    "level": "red",
                    "triggered": [],
                    "rationale": "Because of fraud.",
                    "questions": [],
                }
            },
        ),
        (
            "verdict.questions[0]",
            {
                "verdict": {
                    "level": "green",
                    "triggered": [],
                    "rationale": "The record is silent.",
                    "questions": ["Was this fraud?"],
                }
            },
        ),
        (
            "assessments[0].record_note",
            {
                "assessments": [
                    {
                        "claim_id": "CLM-001",
                        "status": "unverified",
                        "basis": [],
                        "record_note": "Looks like fraud.",
                    }
                ]
            },
        ),
    ],
)
def test_check_language_fails_on_banned_casefile_field(
    tmp_path: Path,
    field_path: str,
    payload: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load_script()
    path = _write_tmp_casefile(tmp_path, payload)
    assert module.main([str(path)]) == 1
    err = capsys.readouterr().err
    assert f"{field_path}: banned term 'fraud'" in err


def test_check_language_ignores_media_title_on_committed_amber() -> None:
    """The committed amber fixture stores a fraud headline. Titles are not
    scanned; the tool-authored fields on that fixture are clean."""
    module = load_script()
    path = FIXTURES_DIR / "amber" / "casefile.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    titles = [item["title"] for item in data["media"]["items"]]
    assert any("fraud" in title.lower() for title in titles)
    assert module.main([str(path)]) == 0


def test_check_language_ignores_media_title_on_tmp_casefile(tmp_path: Path) -> None:
    module = load_script()
    path = _write_tmp_casefile(
        tmp_path,
        {
            "narrative": "The public record is silent on the headline.",
            "media": {
                "items": [
                    {
                        "title": "Gilded Anvil Holdings faces fraud inquiry by trade body",
                        "snippet": "A trade body opened a fraud inquiry.",
                    }
                ]
            },
        },
    )
    assert module.main([str(path)]) == 0


def test_check_language_ignores_claim_text_on_committed_golden() -> None:
    """The committed golden fixture stores a verified claim that contains
    fraud. Claim text is quoted data and is not a casefile-field hit."""
    module = load_script()
    path = FIXTURES_DIR / "golden" / "casefile.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert any("fraud" in claim["text"].lower() for claim in data["claims"])
    assert module.main([str(path)]) == 0


def test_check_language_ignores_verified_claim_text_on_tmp_casefile(
    tmp_path: Path,
) -> None:
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    path = _write_tmp_casefile(
        case_dir,
        {
            "claims": [{"id": "CLM-001", "text": QUOTED, "source": "deck p.2"}],
            "narrative": "See CLM-001.",
        },
    )
    _write_evidence(case_dir, {"2": f"Slide two. {QUOTED}."})
    assert module.main([str(path)]) == 0


def test_check_language_record_note_copying_claim_is_a_hit(tmp_path: Path) -> None:
    """Claim-quote exemption does not apply to casefile statements. A
    record_note that repeats a verified claim's fraud sentence fails even
    when sibling evidence would honour that text on a memo."""
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    path = _write_tmp_casefile(
        case_dir,
        {
            "claims": [{"id": "CLM-001", "text": QUOTED, "source": "deck p.2"}],
            "assessments": [
                {
                    "claim_id": "CLM-001",
                    "status": "unverified",
                    "basis": [],
                    "record_note": QUOTED,
                }
            ],
        },
    )
    _write_evidence(case_dir, {"2": f"Slide two. {QUOTED}."})
    assert module.main([str(path)]) == 1


def test_check_language_identity_name_in_finding_needs_registry_evidence(
    tmp_path: Path,
) -> None:
    """A finding statement that names CROOK, Cuthbert passes only when
    sibling registry evidence carries that name."""
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    statement = "Officer CROOK, Cuthbert is currently appointed."
    _write_identity_casefile(case_dir, "PLAIN TRADING LTD", "CROOK, Cuthbert")
    path = case_dir / "casefile.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["findings"] = [_finding(statement)]
    path.write_text(json.dumps(data), encoding="utf-8")
    assert module.main([str(path)]) == 1
    _write_registry_evidence(case_dir, "PLAIN TRADING LTD", "CROOK, Cuthbert")
    assert module.main([str(path)]) == 0


def test_check_language_tampered_officer_name_in_statement_fails(
    tmp_path: Path,
) -> None:
    """A hand-added officer name in a finding statement buys no exemption
    when the registry evidence never returned it."""
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    _write_identity_casefile(case_dir, "PLAIN TRADING LTD", "CROOK, Cuthbert")
    path = case_dir / "casefile.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["findings"] = [_finding("Officer CROOK, Cuthbert is currently appointed.")]
    path.write_text(json.dumps(data), encoding="utf-8")
    _write_registry_evidence(case_dir, "PLAIN TRADING LTD", "HONEST, Henrietta")
    assert module.main([str(path)]) == 1


def test_check_language_corrupt_casefile_target_fails(tmp_path: Path) -> None:
    """A casefile that is itself a scan target fails closed on bad JSON."""
    module = load_script()
    path = tmp_path / "casefile.json"
    path.write_text("{ not json", encoding="utf-8")
    assert module.main([str(path)]) == 1


# -- AUD-001: collapse pre-table fields; opener-pin the region start --------------


def test_multiline_rationale_heading_does_not_steal_the_claims_region() -> None:
    """A rationale whose middle line is the claims heading still leaves the
    real table as the region: puffery stays exempt and the heading is not
    its own line after collapse."""
    casefile = load_casefile(GOLDEN_DIR)
    assert casefile.verdict is not None
    rationale = (
        "The record supports a red verdict.\n"
        "## Claims vs evidence\n"
        "Those lines are model prose, not the table."
    )
    casefile = casefile.model_copy(
        update={"verdict": casefile.verdict.model_copy(update={"rationale": rationale})}
    )
    memo = render_memo(casefile)
    heading_lines = [line for line in memo.splitlines() if line == "## Claims vs evidence"]
    assert heading_lines == ["## Claims vs evidence"]
    quoted = "Our platform eliminates fraud in widget procurement"
    assert quoted in memo
    assert "The record supports a red verdict. ## Claims vs evidence Those lines" in memo
    assert language_backstop_failure(memo, casefile) is None


def test_rationale_that_is_exactly_the_heading_does_not_steal_the_region() -> None:
    """Opener-pin: a rationale that is exactly the heading is skipped.
    Puffery in the real table stays exempt; the same word in narrative hits."""
    casefile = load_casefile(GOLDEN_DIR)
    assert casefile.verdict is not None
    stolen = casefile.model_copy(
        update={
            "verdict": casefile.verdict.model_copy(update={"rationale": "## Claims vs evidence"}),
            "narrative": "This one is a fraud, whatever the deck says.",
        }
    )
    memo = render_memo(stolen)
    quoted = "Our platform eliminates fraud in widget procurement"
    assert quoted in memo
    assert language_backstop_failure(memo, stolen) is not None
    clean = stolen.model_copy(update={"narrative": "See CLM-001."})
    assert language_backstop_failure(render_memo(clean), clean) is None


# -- AUD-002: code-fetched rendered strings ---------------------------------------


def test_office_locality_crook_is_exempt_on_the_backstop() -> None:
    casefile = load_casefile(GOLDEN_DIR)
    address = dict(casefile.subject.registered_office_address)
    address["locality"] = "Crook"
    casefile = casefile.model_copy(
        update={
            "subject": casefile.subject.model_copy(update={"registered_office_address": address})
        }
    )
    memo = render_memo(casefile)
    assert "Crook" in memo
    assert language_backstop_failure(memo, casefile) is None
    assert language_backstop_failure(memo + "\nA crook by any other name.\n", casefile) is not None


def test_network_overlap_crook_name_is_exempt_on_the_backstop() -> None:
    casefile = load_casefile(GOLDEN_DIR)
    assert casefile.network is not None
    overlap = CoAppointmentOverlap(
        company_number="99999701",
        company_name="CROOK HALL FARM LTD",
        officer_names=["WIDGETSMITH, Wanda"],
    )
    network = casefile.network.model_copy(
        update={"overlaps": list(casefile.network.overlaps) + [overlap]}
    )
    casefile = casefile.model_copy(update={"network": network})
    memo = render_memo(casefile)
    assert "CROOK HALL FARM LTD" in memo
    assert language_backstop_failure(memo, casefile) is None
    assert language_backstop_failure(memo + "\nA crook by any other name.\n", casefile) is not None


def test_media_source_domain_fraud_org_is_exempt_on_the_backstop() -> None:
    casefile = load_casefile(GOLDEN_DIR)
    item = MediaItem(
        title="A fictional headline that stays out of the memo",
        url="https://fraud.org/widget-note",
        source_domain="fraud.org",
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
    assert "fraud.org" in memo
    assert find_banned_terms("fraud.org") == ["fraud"]
    assert language_backstop_failure(memo, casefile) is None
    assert language_backstop_failure(memo + "\nA fraud inquiry is open.\n", casefile) is not None


def test_claim_source_about_fraud_prevention_is_exempt_on_the_backstop() -> None:
    casefile = load_casefile(GOLDEN_DIR)
    claims = [
        claim.model_copy(update={"source": "site /about-fraud-prevention"}) if index == 0 else claim
        for index, claim in enumerate(casefile.claims)
    ]
    casefile = casefile.model_copy(update={"claims": claims})
    memo = render_memo(casefile)
    assert "site /about-fraud-prevention" in memo
    assert language_backstop_failure(memo, casefile) is None
    dirty = casefile.model_copy(
        update={"narrative": "This one is a fraud, whatever the deck says."}
    )
    assert language_backstop_failure(render_memo(dirty), dirty) is not None


def test_query_category_search_terms_are_not_in_the_collector() -> None:
    casefile = load_casefile(GOLDEN_DIR)
    item = MediaItem(
        title="stays out",
        url="https://news.example/story",
        source_domain="news.example",
        query_category="misconduct",
    )
    casefile = casefile.model_copy(
        update={
            "media": MediaScreening(
                performed=True, provider="tavily", results_per_query=5, items=[item]
            )
        }
    )
    texts = {text.casefold() for text in code_fetched_exemption_texts(casefile)}
    for _label, term in QUERY_CATEGORIES:
        assert term.casefold() not in texts
    assert "news.example" in texts


def test_office_display_join_matches_company_profile() -> None:
    """The script copies the office join; it must match the model property."""
    module = load_script()
    assert REGISTERED_OFFICE_ADDRESS_KEYS == MODEL_OFFICE_KEYS
    assert module.REGISTERED_OFFICE_ADDRESS_KEYS == MODEL_OFFICE_KEYS
    address = {key: f"part-{key}" for key in MODEL_OFFICE_KEYS}
    profile = CompanyProfile.model_validate(
        {
            "company_name": "PIN LTD",
            "company_number": "99999901",
            "registered_office_address": address,
        }
    )
    assert module._office_display(address) == profile.registered_office_display


def test_media_domain_rule_matches_source_domain() -> None:
    module = load_script()
    url = "https://www.Fraud.org/path?q=1"
    assert module._source_domain_from_url(url) == source_domain(url)
    assert source_domain(url) == "fraud.org"


def _write_office_evidence(case_dir: Path, address: dict[str, str]) -> None:
    evidence_dir = case_dir / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    (evidence_dir / "registry_profile.json").write_text(
        json.dumps(
            {
                "name": "registry_profile",
                "url": "https://api.company-information.service.gov.uk/company/99999998",
                "body": {
                    "company_name": "PLAIN TRADING LTD",
                    "registered_office_address": address,
                },
            }
        ),
        encoding="utf-8",
    )


def test_ci_ignores_invented_office_locality(tmp_path: Path) -> None:
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text("Registered office: 1 High Street, Crook\n", encoding="utf-8")
    invented = {"address_line_1": "1 High Street", "locality": "Crook"}
    (case_dir / "casefile.json").write_text(
        json.dumps(
            {
                "subject": {
                    "company_name": "PLAIN TRADING LTD",
                    "registered_office_address": invented,
                }
            }
        ),
        encoding="utf-8",
    )
    _write_office_evidence(case_dir, {"address_line_1": "1 High Street", "locality": "Faketown"})
    exempt = module.code_fetched_exemptions(memo)
    assert not any("crook" in text.casefold() for text in exempt)
    assert module.main([str(memo)]) == 1


def test_ci_honors_evidence_verified_office(tmp_path: Path) -> None:
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text("Registered office: 1 High Street, Crook\n", encoding="utf-8")
    address = {"address_line_1": "1 High Street", "locality": "Crook"}
    (case_dir / "casefile.json").write_text(
        json.dumps(
            {
                "subject": {
                    "company_name": "PLAIN TRADING LTD",
                    "registered_office_address": address,
                }
            }
        ),
        encoding="utf-8",
    )
    _write_office_evidence(case_dir, address)
    assert module.main([str(memo)]) == 0
    memo.write_text("Registered office: 1 High Street, Crook\nA crook outside.\n", encoding="utf-8")
    assert module.main([str(memo)]) == 1


def test_ci_ignores_invented_overlap_name(tmp_path: Path) -> None:
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text("- CROOK HALL FARM LTD (99999701): WIDGETSMITH, Wanda\n", encoding="utf-8")
    (case_dir / "casefile.json").write_text(
        json.dumps(
            {
                "subject": {"company_name": "PLAIN TRADING LTD"},
                "network": {
                    "overlaps": [
                        {"company_number": "99999701", "company_name": "CROOK HALL FARM LTD"}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    evidence_dir = case_dir / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "appointments_p1.json").write_text(
        json.dumps(
            {
                "name": "appointments_p1",
                "body": {
                    "items": [
                        {
                            "appointed_to": {
                                "company_name": "IMAGINARY COMPONENTS LTD",
                                "company_number": "99999801",
                            }
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    exempt = module.code_fetched_exemptions(memo)
    assert not any("crook" in text.casefold() for text in exempt)
    assert module.main([str(memo)]) == 1


def test_ci_honors_evidence_verified_network_name(tmp_path: Path) -> None:
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text("- CROOK HALL FARM LTD (99999701): an officer\n", encoding="utf-8")
    (case_dir / "casefile.json").write_text(
        json.dumps(
            {
                "subject": {"company_name": "PLAIN TRADING LTD"},
                "network": {
                    "overlaps": [
                        {"company_number": "99999701", "company_name": "CROOK HALL FARM LTD"}
                    ],
                    "appointments": [{"companies": ["CROOK HALL FARM LTD (99999701)"]}],
                },
            }
        ),
        encoding="utf-8",
    )
    evidence_dir = case_dir / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "appointments_p1.json").write_text(
        json.dumps(
            {
                "name": "appointments_p1",
                "body": {
                    "items": [
                        {
                            "appointed_to": {
                                "company_name": "CROOK HALL FARM LTD",
                                "company_number": "99999701",
                            }
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    assert module.main([str(memo)]) == 0
    memo.write_text("- CROOK HALL FARM LTD (99999701)\nA crook outside.\n", encoding="utf-8")
    assert module.main([str(memo)]) == 1


def test_ci_ignores_invented_media_domain(tmp_path: Path) -> None:
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text(
        "| fraud.org | 2026-05-14 | misconduct | https://news.example/story |\n",
        encoding="utf-8",
    )
    (case_dir / "casefile.json").write_text(
        json.dumps(
            {
                "subject": {"company_name": "PLAIN TRADING LTD"},
                "media": {"items": [{"source_domain": "fraud.org", "url": "https://fraud.org/x"}]},
            }
        ),
        encoding="utf-8",
    )
    evidence_dir = case_dir / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "media_search.json").write_text(
        json.dumps(
            {
                "name": "media_search",
                "body": {
                    "kind": "search_results",
                    "results": [
                        {"source_domain": "news.example", "url": "https://news.example/story"}
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    exempt = module.code_fetched_exemptions(memo)
    assert not any("fraud" in text.casefold() for text in exempt)
    assert module.main([str(memo)]) == 1


def test_ci_honors_evidence_verified_media_domain(tmp_path: Path) -> None:
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text(
        "| fraud.org | 2026-05-14 | misconduct | https://fraud.org/widget-note |\n",
        encoding="utf-8",
    )
    (case_dir / "casefile.json").write_text(
        json.dumps(
            {
                "subject": {"company_name": "PLAIN TRADING LTD"},
                "media": {
                    "items": [
                        {"source_domain": "fraud.org", "url": "https://fraud.org/widget-note"}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    evidence_dir = case_dir / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "media_search.json").write_text(
        json.dumps(
            {
                "name": "media_search",
                "body": {
                    "kind": "search_results",
                    "results": [
                        {"source_domain": "fraud.org", "url": "https://fraud.org/widget-note"}
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    assert module.main([str(memo)]) == 0
    memo.write_text("| fraud.org |\nA fraud inquiry is open.\n", encoding="utf-8")
    assert module.main([str(memo)]) == 1


def test_ci_ignores_invented_claim_source_label(tmp_path: Path) -> None:
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text(
        '| 1 | "Founded in 2018" (site /about-fraud-prevention) |  | not checkable |\n',
        encoding="utf-8",
    )
    (case_dir / "casefile.json").write_text(
        json.dumps(
            {
                "subject": {"company_name": "PLAIN TRADING LTD"},
                "claims": [
                    {
                        "id": "CLM-001",
                        "text": "Founded in 2018",
                        "source": "site /about-fraud-prevention",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    evidence_dir = case_dir / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "site_001.json").write_text(
        json.dumps(
            {
                "name": "site_001",
                "url": "https://widgets.example/about",
                "body": {
                    "kind": "site_text",
                    "url": "https://widgets.example/about",
                    "text": "About us. Founded in 2018.",
                },
            }
        ),
        encoding="utf-8",
    )
    exempt = module.code_fetched_exemptions(memo)
    assert not any("fraud" in text.casefold() for text in exempt)
    assert module.main([str(memo)]) == 1


def test_ci_honors_evidence_verified_claim_source(tmp_path: Path) -> None:
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    memo = case_dir / "memo.md"
    memo.write_text(
        '| 1 | "Founded in 2018" (site /about-fraud-prevention) |  | not checkable |\n',
        encoding="utf-8",
    )
    (case_dir / "casefile.json").write_text(
        json.dumps(
            {
                "subject": {"company_name": "PLAIN TRADING LTD"},
                "claims": [
                    {
                        "id": "CLM-001",
                        "text": "Founded in 2018",
                        "source": "site /about-fraud-prevention",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    evidence_dir = case_dir / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "site_001.json").write_text(
        json.dumps(
            {
                "name": "site_001",
                "url": "https://widgets.example/about-fraud-prevention",
                "body": {
                    "kind": "site_text",
                    "url": "https://widgets.example/about-fraud-prevention",
                    "text": "About us. Founded in 2018.",
                },
            }
        ),
        encoding="utf-8",
    )
    assert module.main([str(memo)]) == 0
    memo.write_text(
        '| 1 | "Founded in 2018" (site /about-fraud-prevention) |\nA fraud inquiry.\n',
        encoding="utf-8",
    )
    assert module.main([str(memo)]) == 1


def test_finding_statement_naming_crook_passes_when_office_evidence_carries_it(
    tmp_path: Path,
) -> None:
    """Casefile-field scan uses the widened re-verified set."""
    module = load_script()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    address = {"address_line_1": "1 High Street", "locality": "Crook"}
    path = case_dir / "casefile.json"
    path.write_text(
        json.dumps(
            {
                "subject": {
                    "company_name": "PLAIN TRADING LTD",
                    "registered_office_address": address,
                },
                "findings": [_finding("The registered office locality is Crook.")],
            }
        ),
        encoding="utf-8",
    )
    assert module.main([str(path)]) == 1
    _write_office_evidence(case_dir, address)
    assert module.main([str(path)]) == 0


# -- template interpolation inventory ---------------------------------------------


_INTERPOLATION_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)

# Every {{ }} in memo.md.j2. A new unclassified expression fails this test.
# Residuals this sprint: filing descriptions only.
_MEMO_INTERPOLATION_CLASS: dict[str, str] = {
    "c.subject.company_name": "covered",
    "c.subject.company_number": "safe",
    "c.subject.jurisdiction": "safe",
    "c.screened_at|fmt_datetime": "safe",
    "c.tool_version": "safe",
    "c.subject.registered_office_display": "covered",
    'c.subject.sic_codes|join(", ")': "safe",
    "c.verdict.level|upper": "safe",
    "t.id": "safe",
    "t.severity": "safe",
    "t.text": "safe",
    "c.verdict.rationale": "prose",
    "c.verdict_enforcement": "prose",
    "c.synthesis.provider": "safe",
    "c.synthesis.model": "safe",
    "c.synthesis.prompt_version": "safe",
    "note": "prose",
    "synthesis_failure": "prose",
    "no_synthesis_text": "safe",
    "c.disclaimer": "safe",
    "r.number": "safe",
    "r.text": "prose",
    "r.source": "covered",
    "r.record": "prose",
    "r.status": "safe",
    "c.claims_extraction.skipped_reason": "prose",
    'c.narrative or "No narrative was produced."': "prose",
    "loop.index": "safe",
    "q": "prose",
    "f.id": "safe",
    "f.confidence": "safe",
    "f.statement": "prose",
    "e.source_url": "url",
    "e.retrieved_at|fmt_date": "safe",
    "o.name": "covered",
    'o.officer_role or ""': "safe",
    "o.appointed_on|fmt_date": "safe",
    "o.resigned_on|fmt_date": "safe",
    'p.name or ""': "covered",
    'p.kind or ""': "safe",
    'p.natures_of_control|join("; ")': "safe",
    "p.notified_on|fmt_date": "safe",
    "p.ceased_on|fmt_date": "safe",
    "c.sanctions.dataset": "safe",
    "c.sanctions.threshold": "safe",
    "c.sanctions.algorithm_requested": "safe",
    "c.sanctions.algorithm_resolved": "safe",
    "r.subject": "covered",
    "r.kind": "safe",
    '"%.2f"|format(r.top_score) if r.top_score is not none else "no candidates"': "safe",
    '"match at or above threshold" if r.matched else "below threshold"': "safe",
    "c.sanctions.skipped_reason": "prose",
    "a.officer_name": "covered",
    "a.other_current_appointments": "safe",
    '": " + a.companies|join("; ") if a.companies else ""': "covered",
    '" (list truncated)" if a.truncated else ""': "safe",
    'o.company_name or "unnamed company"': "covered",
    "o.company_number": "safe",
    'o.officer_names|join(", ")': "covered",
    "line": "prose",
    "c.media.items|length": "safe",
    "m.source_domain": "covered",
    'm.published or "undated"': "safe",
    "m.query_category": "safe",
    "m.url": "url",
    "c.media.skipped_reason": "prose",
    "c.filings|length": "safe",
    "c.filings_total": "safe",
    "f.date|fmt_date": "safe",
    'f.category or ""': "safe",
    'f.description or ""': "residual",
    'ch.charge_code or ""': "safe",
    'ch.status or ""': "safe",
    "ch.created_on|fmt_date": "safe",
    "ch.satisfied_on|fmt_date": "safe",
    "c.insolvency_cases|length": "safe",
    "ogl_attribution": "safe",
}


def test_every_memo_interpolation_is_classified() -> None:
    template = (REPO_ROOT / "src" / "coldscreen" / "templates" / "memo.md.j2").read_text(
        encoding="utf-8"
    )
    found = {" ".join(match.group(1).split()) for match in _INTERPOLATION_RE.finditer(template)}
    unknown = sorted(found - set(_MEMO_INTERPOLATION_CLASS))
    unused = sorted(set(_MEMO_INTERPOLATION_CLASS) - found)
    assert unknown == []
    assert unused == []
    residuals = {expr for expr, kind in _MEMO_INTERPOLATION_CLASS.items() if kind == "residual"}
    assert residuals == {'f.description or ""'}
    kinds = set(_MEMO_INTERPOLATION_CLASS.values())
    assert kinds <= {"covered", "url", "safe", "prose", "residual"}


def test_check_language_main_is_importable() -> None:
    from coldscreen.check_language import main

    assert callable(main)


def test_wrapper_script_exits_0_on_fixture_memo() -> None:
    spec = importlib.util.spec_from_file_location("check_language_wrapper", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main([str(FIXTURE_CASE_DIR / "memo.md")]) == 0
