"""Deck extraction: pdfplumber over the committed fixture deck, plus the
clean-error taxonomy (missing, not a file, not a PDF, encrypted) and the
zero-text posture (a finding downstream, never an exception)."""

from __future__ import annotations

import hashlib
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from coldscreen.deck import DeckError, deck_record, extract_deck

from .conftest import FIXTURES_DIR

DECK_PATH = FIXTURES_DIR / "deck_fabricated_widgets.pdf"
GENERATOR_PATH = Path(__file__).parent.parent / "scripts" / "make_fixture_deck.py"
NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)


def load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_fixture_deck", GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_deck_matches_its_generator_byte_for_byte() -> None:
    """The generation script under scripts/ reproduces the committed fixture
    exactly, so regeneration is diff-clean and the provenance is proven."""
    module = load_generator()
    assert module.build_pdf(module.PAGES) == DECK_PATH.read_bytes()


def test_committed_deck_is_small_and_fictional() -> None:
    size = DECK_PATH.stat().st_size
    assert 300 <= size <= 8192  # a few hundred bytes to a few KB
    extraction = extract_deck(DECK_PATH, max_pages=40)
    joined = "\n".join(page.text for page in extraction.pages)
    assert "Fabricated Widgets" in joined
    assert "entirely fictional" in joined.lower()


def test_extracts_pages_numbered_from_one_with_labels() -> None:
    extraction = extract_deck(DECK_PATH, max_pages=40)
    assert extraction.page_count == 3
    assert [p.number for p in extraction.pages] == [1, 2, 3]
    assert [p.source_label for p in extraction.pages] == ["deck p.1", "deck p.2", "deck p.3"]
    assert extraction.truncated_pages is False
    assert extraction.has_text is True
    assert "Operating since 2015 with a national footprint." in extraction.pages[1].text
    assert "The company is debt free and self funded." in extraction.pages[1].text
    assert "A team of 40 widget engineers." in extraction.pages[2].text


def test_sha256_matches_the_file_bytes() -> None:
    extraction = extract_deck(DECK_PATH, max_pages=40)
    assert extraction.sha256 == hashlib.sha256(DECK_PATH.read_bytes()).hexdigest()
    assert extraction.file_name == "deck_fabricated_widgets.pdf"


def test_page_cap_truncates_and_flags() -> None:
    extraction = extract_deck(DECK_PATH, max_pages=2)
    assert extraction.page_count == 3
    assert [p.number for p in extraction.pages] == [1, 2]
    assert extraction.truncated_pages is True


def test_oversized_deck_is_a_clean_error_before_parsing(tmp_path: Path) -> None:
    """The size gate fires from the file size alone, before any bytes are
    parsed: the oversized file here is not even a PDF and the error still
    names max_deck_bytes, never a parse failure."""
    huge = tmp_path / "huge.pdf"
    huge.write_bytes(b"x" * 2048)
    with pytest.raises(DeckError, match="max_deck_bytes"):
        extract_deck(huge, max_pages=40, max_bytes=1024)


def test_deck_at_the_size_limit_is_accepted() -> None:
    size = DECK_PATH.stat().st_size
    extraction = extract_deck(DECK_PATH, max_pages=40, max_bytes=size)
    assert extraction.page_count == 3


def test_missing_deck_is_a_clean_error(tmp_path: Path) -> None:
    with pytest.raises(DeckError, match="not found"):
        extract_deck(tmp_path / "absent.pdf", max_pages=40)


def test_directory_deck_is_a_clean_error(tmp_path: Path) -> None:
    with pytest.raises(DeckError, match="not a file"):
        extract_deck(tmp_path, max_pages=40)


def test_non_pdf_bytes_are_a_clean_error(tmp_path: Path) -> None:
    fake = tmp_path / "fake.pdf"
    fake.write_text("just some text pretending", encoding="utf-8")
    with pytest.raises(DeckError, match="not a readable PDF"):
        extract_deck(fake, max_pages=40)


def test_encrypted_deck_is_a_clean_error(tmp_path: Path) -> None:
    """A trailer declaring /Encrypt makes pdfminer treat the document as
    encrypted; the error must name the condition, never traceback."""
    locked = tmp_path / "locked.pdf"
    locked.write_bytes(
        DECK_PATH.read_bytes().replace(b"/Root 1 0 R", b"/Root 1 0 R /Encrypt 99 0 R")
    )
    with pytest.raises(DeckError, match="encrypted"):
        extract_deck(locked, max_pages=40)


def test_zero_extractable_text_is_not_an_error(tmp_path: Path) -> None:
    """An empty-text PDF extracts fine with has_text False: image-only decks
    become an explicit finding downstream, not a crash."""
    module = load_generator()
    silent = tmp_path / "silent.pdf"
    silent.write_bytes(module.build_pdf([[], []]))
    extraction = extract_deck(silent, max_pages=40)
    assert extraction.page_count == 2
    assert extraction.has_text is False
    assert all(page.text == "" for page in extraction.pages)


def test_deck_record_carries_text_and_provenance_never_the_binary() -> None:
    extraction = extract_deck(DECK_PATH, max_pages=40)
    record = deck_record(extraction, lambda: NOW)
    assert record.url == "coldscreen:deck/deck_fabricated_widgets.pdf"
    assert record.status == 0
    assert record.retrieved_at == NOW
    body = record.body
    assert body["kind"] == "deck_text"
    assert body["sha256"] == extraction.sha256
    assert body["page_count"] == 3
    assert set(body["pages"]) == {"1", "2", "3"}
    assert "Operating since 2015" in body["pages"]["2"]
    assert "%PDF" not in str(body)
