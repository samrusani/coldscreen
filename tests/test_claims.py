"""Stage 6: sections, caps, schema, id assignment, retries, skip patterns."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from coldscreen.claims import (
    ClaimsExtractionError,
    ClaimsStageResult,
    Section,
    build_claims_schema,
    build_sections,
    load_prompt,
    normalize_claim_text,
    prompt_version,
    run_claims_stage,
)
from coldscreen.config import Settings
from coldscreen.deck import DeckExtraction, extract_deck
from coldscreen.models import CompanyProfile
from coldscreen.site import SiteFetchResult, SitePage

from .conftest import FIXTURES_DIR
from .fakes import FakeModelProvider, claim_json, claims_json

DECK_PATH = FIXTURES_DIR / "deck_fabricated_widgets.pdf"
NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)

PROFILE = CompanyProfile.model_validate(
    {"company_name": "FABRICATED WIDGETS LTD", "company_number": "99999999"}
)


def deck_extraction() -> DeckExtraction:
    return extract_deck(DECK_PATH, max_pages=40)


def site_result(text: str = "Founded long ago by nobody real.") -> SiteFetchResult:
    result = SiteFetchResult(site_url="https://widgets.example")
    result.pages.append(SitePage(url="https://widgets.example/", path="/", status=200, text=text))
    return result


def run_stage(
    deck: DeckExtraction | None = None,
    site: SiteFetchResult | None = None,
    responses: list[str] | None = None,
) -> tuple[ClaimsStageResult, FakeModelProvider]:
    provider = FakeModelProvider(responses or [claims_json()])
    stage = run_claims_stage(
        PROFILE, deck, site, provider, "fake", "canned", Settings(), lambda: NOW
    )
    return stage, provider


# -- prompt ------------------------------------------------------------------------


def test_claims_prompt_loads_and_carries_version_2() -> None:
    prompt = load_prompt()
    assert prompt_version(prompt) == "2"
    assert "verbatim" in prompt
    assert "puffery" in prompt.lower()
    assert "checkable" in prompt
    # v2: claims are about the company ITSELF; market material is omitted.
    assert "about ITSELF" in prompt
    assert "market statistics" in prompt
    assert "OMIT" in prompt
    # v2: quotation verification is disclosed to the model.
    assert "DROPPED if its text is not found" in prompt


def test_prompt_version_marker_is_required() -> None:
    with pytest.raises(ClaimsExtractionError, match="version marker"):
        prompt_version("no marker here")


# -- sections and the combined character cap ------------------------------------------


def test_sections_come_from_deck_then_site_skipping_empty_pages() -> None:
    deck = deck_extraction()
    sections, truncated, dropped = build_sections(deck, site_result(), 60000)
    assert [s.source for s in sections] == ["deck p.1", "deck p.2", "deck p.3", "site /"]
    assert truncated is False
    assert dropped == 0


def test_char_cap_truncates_mid_section_and_drops_the_rest() -> None:
    deck = deck_extraction()
    page_one_len = len(deck.pages[0].text)
    cap = page_one_len + 10  # ten characters into page two
    sections, truncated, dropped = build_sections(deck, site_result(), cap)
    assert truncated is True
    assert [s.source for s in sections] == ["deck p.1", "deck p.2"]
    assert len(sections[1].text) == 10
    expected_dropped = (
        (len(deck.pages[1].text) - 10) + len(deck.pages[2].text) + len(site_result().pages[0].text)
    )
    assert dropped == expected_dropped


# -- schema -----------------------------------------------------------------------


def test_schema_enumerates_exactly_the_offered_source_labels() -> None:
    schema = build_claims_schema(["deck p.1", "site /about"])
    source = schema["properties"]["claims"]["items"]["properties"]["source"]
    assert source["enum"] == ["deck p.1", "site /about"]
    assert schema["additionalProperties"] is False


# -- the not-run and no-text skip patterns ---------------------------------------------


def test_no_deck_and_no_site_is_the_not_run_pattern() -> None:
    provider = FakeModelProvider([])
    stage = run_claims_stage(
        PROFILE, None, None, provider, "fake", "canned", Settings(), lambda: NOW
    )
    assert stage.claims == []
    assert stage.extraction.performed is False
    assert stage.extraction.skipped_reason == "no deck or site provided"
    assert [n.name for n in stage.records] == ["claims_not_run"]
    assert stage.records[0].record.body["kind"] == "not_run"
    finding = stage.findings[0]
    assert finding.id == "EXT-000"
    assert "not performed" in finding.statement
    assert provider.calls == []  # the model is never touched


def test_zero_text_inputs_skip_the_model_with_an_explicit_finding(tmp_path: Path) -> None:
    from .test_deck import load_generator

    silent_pdf = tmp_path / "silent.pdf"
    silent_pdf.write_bytes(load_generator().build_pdf([[]]))
    silent_deck = extract_deck(silent_pdf, max_pages=40)
    provider = FakeModelProvider([])
    stage = run_claims_stage(
        PROFILE, silent_deck, None, provider, "fake", "canned", Settings(), lambda: NOW
    )
    assert stage.claims == []
    assert stage.extraction.performed is False
    assert stage.extraction.skipped_reason == "no text could be extracted from the provided inputs"
    ids = {f.id: f for f in stage.findings}
    assert "contained no extractable text" in ids["EXT-001"].statement
    assert "yielded no text" in ids["EXT-006"].statement
    assert provider.calls == []


# -- extraction ---------------------------------------------------------------------


def test_extraction_assigns_ids_in_order_and_keeps_puffery() -> None:
    responses = [
        claims_json(
            [
                claim_json("Operating since 2015", "deck p.2", "history", True),
                claim_json("The most trusted name in widgets", "deck p.3", "traction", False),
                claim_json("A team of 40 widget engineers", "deck p.3", "team", True),
            ]
        )
    ]
    stage, provider = run_stage(deck=deck_extraction(), responses=responses)
    assert [c.id for c in stage.claims] == ["CLM-001", "CLM-002", "CLM-003"]
    assert stage.claims[1].checkable is False  # puffery listed, never dropped
    assert stage.extraction.performed is True
    assert stage.extraction.parse_retries == 0
    assert stage.extraction.deck_file == "deck_fabricated_widgets.pdf"
    assert stage.extraction.sources == ["deck p.1", "deck p.2", "deck p.3"]
    ids = {f.id: f for f in stage.findings}
    assert "3 claim(s)" in ids["EXT-006"].statement
    assert "2 checkable" in ids["EXT-006"].statement
    assert "1 not checkable" in ids["EXT-006"].statement
    # The provider was called with the claims prompt and the enum schema.
    call = provider.calls[0]
    assert "coldscreen claims prompt" in call.system
    assert call.json_schema is not None
    source_enum = call.json_schema["properties"]["claims"]["items"]["properties"]["source"]["enum"]
    assert source_enum == ["deck p.1", "deck p.2", "deck p.3"]


def test_claim_text_is_whitespace_normalized_and_duplicates_collapse() -> None:
    responses = [
        claims_json(
            [
                claim_json("Operating\n since   2015", "deck p.2", "history", True),
                claim_json("Operating since 2015", "deck p.2", "history", True),
            ]
        )
    ]
    stage, _provider = run_stage(deck=deck_extraction(), responses=responses)
    assert [c.text for c in stage.claims] == ["Operating since 2015"]


def test_invalid_json_then_valid_uses_one_parse_retry() -> None:
    responses = ["not json", claims_json([claim_json("Investor overview", "deck p.1")])]
    stage, provider = run_stage(deck=deck_extraction(), responses=responses)
    assert stage.extraction.parse_retries == 1
    assert len(provider.calls) == 2
    retry_message = provider.calls[1].messages[-1]
    assert retry_message.role == "user"
    assert "invalid JSON" in retry_message.content


def test_unknown_source_label_counts_as_schema_mismatch() -> None:
    responses = [
        claims_json([claim_json("Investor overview", "deck p.99")]),
        claims_json([claim_json("Investor overview", "deck p.1")]),
    ]
    stage, provider = run_stage(deck=deck_extraction(), responses=responses)
    assert stage.extraction.parse_retries == 1
    assert "section labels" in provider.calls[1].messages[-1].content
    assert stage.claims[0].source == "deck p.1"


def test_exhausted_retries_raise_with_the_partial_result() -> None:
    provider = FakeModelProvider(["junk one", "junk two", "junk three"])
    with pytest.raises(ClaimsExtractionError, match="after 3 attempts") as caught:
        run_claims_stage(
            PROFILE, deck_extraction(), None, provider, "fake", "canned", Settings(), lambda: NOW
        )
    assert len(provider.calls) == 3
    partial = caught.value.result
    # The deck evidence and findings survive for the audit pack.
    assert [n.name for n in partial.records] == ["deck_text"]
    assert any(f.id == "EXT-001" for f in partial.findings)
    assert partial.claims == []
    assert "nothing is fabricated" in str(caught.value)


def test_truncation_finding_when_the_char_cap_is_hit() -> None:
    settings = Settings(max_claims_chars=60)
    provider = FakeModelProvider([claims_json([claim_json("Fabricated Widgets", "deck p.1")])])
    stage = run_claims_stage(
        PROFILE, deck_extraction(), None, provider, "fake", "canned", settings, lambda: NOW
    )
    assert stage.extraction.truncated is True
    ids = {f.id: f for f in stage.findings}
    assert "EXT-005" in ids
    assert "max_claims_chars" in ids["EXT-005"].statement
    # The schema only offers the sections that survived the cap.
    call_schema = provider.calls[0].json_schema
    assert call_schema is not None
    offered = call_schema["properties"]["claims"]["items"]["properties"]["source"]["enum"]
    assert offered == ["deck p.1"]
    assert stage.extraction.sources == ["deck p.1"]


def test_site_pages_become_sections_and_findings() -> None:
    site = site_result()
    responses = [claims_json([claim_json("Founded long ago", "site /", "history", True)])]
    stage, provider = run_stage(site=site, responses=responses)
    assert stage.claims[0].source == "site /"
    ids = {f.id: f for f in stage.findings}
    assert "EXT-003" in ids
    assert "1 page(s) with text" in ids["EXT-003"].statement
    assert stage.extraction.site_url == "https://widgets.example"
    assert stage.extraction.deck_file is None


def test_robots_skips_surface_as_a_finding() -> None:
    site = site_result()
    site.robots_skipped.append("/team")
    responses = [claims_json([claim_json("Founded long ago", "site /", "history", True)])]
    stage, _provider = run_stage(site=site, responses=responses)
    ids = {f.id: f for f in stage.findings}
    assert "EXT-004" in ids
    assert "/team" in ids["EXT-004"].statement
    assert "robots.txt" in ids["EXT-004"].statement


def test_site_with_every_path_robots_blocked_skips_cleanly() -> None:
    """Regression: a site whose robots.txt forbids everything produces no
    page responses at all; the outcome finding must still carry evidence."""
    site = SiteFetchResult(site_url="https://widgets.example")
    site.robots_skipped.append("/")
    provider = FakeModelProvider([])
    stage = run_claims_stage(
        PROFILE, None, site, provider, "fake", "canned", Settings(), lambda: NOW
    )
    assert stage.claims == []
    assert stage.extraction.performed is False
    ids = {f.id: f for f in stage.findings}
    assert "EXT-004" in ids  # the robots skip is recorded
    assert "EXT-006" in ids
    assert all(f.evidence for f in stage.findings)
    assert provider.calls == []


# -- quotation verification (review fix F1b) -----------------------------------


def test_fabricated_claim_text_is_dropped_with_a_counted_finding() -> None:
    """Review attack shape 1: a claim never present in the deck is dropped,
    never stored, never rendered; the drop is a counted finding and an
    extraction summary entry, and ids stay contiguous over what survives."""
    responses = [
        claims_json(
            [
                claim_json("Operating since 2015", "deck p.2", "history", True),
                claim_json(
                    "The directors were disqualified for fraud", "deck p.2", "regulatory", True
                ),
                claim_json("A team of 40 widget engineers", "deck p.3", "team", True),
            ]
        )
    ]
    stage, _provider = run_stage(deck=deck_extraction(), responses=responses)
    assert [c.text for c in stage.claims] == [
        "Operating since 2015",
        "A team of 40 widget engineers",
    ]
    assert [c.id for c in stage.claims] == ["CLM-001", "CLM-002"]
    assert stage.extraction.dropped_claims == 1
    ids = {f.id: f for f in stage.findings}
    assert (
        ids["EXT-007"].statement
        == "1 claim(s) were dropped because their text was not found in the"
        " extracted source material. Only verbatim quotations of the deck or"
        " site text are stored."
    )
    joined = " ".join(f.statement for f in stage.findings)
    assert "disqualified" not in joined  # the dropped text never surfaces


def test_verification_is_against_the_declared_source_section_only() -> None:
    """Text that exists on page 3 but is declared as page 2 fails: the
    quotation must come from the section it cites."""
    responses = [
        claims_json([claim_json("A team of 40 widget engineers", "deck p.2", "team", True)])
    ]
    stage, _provider = run_stage(deck=deck_extraction(), responses=responses)
    assert stage.claims == []
    assert stage.extraction.dropped_claims == 1


def test_verification_folds_case_whitespace_quotes_and_dashes() -> None:
    variant = "operating   SINCE 2015 with a national\u2013footprint"
    # The deck says "Operating since 2015 with a national footprint." with a
    # plain space; the en dash variant must NOT match (different token), but
    # case and whitespace folds must.
    responses = [
        claims_json(
            [
                claim_json("operating SINCE   2015", "deck p.2", "history", True),
                claim_json(variant, "deck p.2", "history", True),
            ]
        )
    ]
    stage, _provider = run_stage(deck=deck_extraction(), responses=responses)
    assert [c.text for c in stage.claims] == ["operating SINCE 2015"]
    assert stage.extraction.dropped_claims == 1


def test_no_drops_means_no_ext007_finding() -> None:
    responses = [claims_json([claim_json("Operating since 2015", "deck p.2", "history", True)])]
    stage, _provider = run_stage(deck=deck_extraction(), responses=responses)
    assert stage.extraction.dropped_claims == 0
    assert all(f.id != "EXT-007" for f in stage.findings)


def test_section_dataclass_is_frozen() -> None:
    section = Section(source="deck p.1", text="x")
    with pytest.raises(AttributeError):
        section.text = "y"  # type: ignore[misc]


def test_normalize_claim_text_collapses_whitespace() -> None:
    assert normalize_claim_text("  a\n b\t c  ") == "a b c"
