"""Deck text extraction (stage 6 input): pdfplumber over a local PDF.

Coded from the installed pdfplumber 0.11.x API, not from memory:
- pdfplumber.open(path_or_fp, password=...) returns a PDF context manager
  whose .pages list holds Page objects; page.extract_text() returns the
  page's text ("" when the page has none).
- Every pdfminer failure during open is wrapped by pdfplumber in
  pdfplumber.utils.exceptions.PdfminerException with the original pdfminer
  exception as args[0]. PDFEncryptionError (and its PDFPasswordIncorrect
  subclass) marks encrypted documents; everything else on open means the
  bytes are not a readable PDF.

Error posture: a deck the user explicitly passed that cannot be read at all
(missing, not a file, not a PDF, encrypted) is a clean DeckError for the
CLI to report BEFORE any registry fetching, never a traceback. A valid PDF
with zero extractable text is NOT an error: it is a fact about the deck
(image-only decks exist), recorded downstream as an explicit finding.

Evidence: the extracted text is persisted as deck_text.json (page number to
text, file name, sha256 of the deck bytes). The binary itself is never
persisted.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pdfplumber
from pdfminer.pdfdocument import PDFEncryptionError
from pdfplumber.utils.exceptions import PdfminerException

from .ch_client import FetchRecord

DECK_URL_PREFIX = "firstpass:deck/"


class DeckError(Exception):
    """The deck cannot be used at all. Message is user-facing and clean."""


@dataclass(frozen=True)
class DeckPage:
    """One extracted deck page, numbered from 1."""

    number: int
    text: str

    @property
    def source_label(self) -> str:
        return f"deck p.{self.number}"


@dataclass
class DeckExtraction:
    """Everything read from one deck: pages, provenance, evidence record."""

    file_name: str
    sha256: str
    page_count: int  # pages in the document, before any cap
    pages: list[DeckPage] = field(default_factory=list)
    truncated_pages: bool = False  # the max_deck_pages cap cut pages off

    @property
    def has_text(self) -> bool:
        return any(page.text for page in self.pages)

    @property
    def pseudo_url(self) -> str:
        """Evidence pointer for deck-derived findings; never a real fetch."""
        return DECK_URL_PREFIX + self.file_name


def _normalize_page_text(raw: str | None) -> str:
    """Per-line whitespace cleanup; keeps line structure, drops blank lines."""
    if not raw:
        return ""
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def extract_deck(path: Path, max_pages: int) -> DeckExtraction:
    """Read per-page text from the deck at path, capped at max_pages pages.

    Raises DeckError with a clean, user-facing message when the file is
    missing, not a file, not a PDF, or encrypted.
    """
    if not path.exists():
        raise DeckError(f"deck not found: {path}")
    if not path.is_file():
        raise DeckError(f"deck is not a file: {path}")
    try:
        deck_bytes = path.read_bytes()
    except OSError as error:
        raise DeckError(f"deck could not be read: {path} ({error})") from None

    digest = hashlib.sha256(deck_bytes).hexdigest()
    extraction = DeckExtraction(file_name=path.name, sha256=digest, page_count=0)

    try:
        with pdfplumber.open(path) as pdf:
            extraction.page_count = len(pdf.pages)
            for page in pdf.pages[:max_pages]:
                text = _normalize_page_text(page.extract_text())
                extraction.pages.append(DeckPage(number=page.page_number, text=text))
    except PdfminerException as error:
        inner = error.args[0] if error.args else error
        if isinstance(inner, PDFEncryptionError):
            raise DeckError(
                f"deck {path.name} is encrypted. Decrypt it (or export an"
                " unprotected copy) and run the screen again."
            ) from None
        raise DeckError(
            f"deck {path.name} is not a readable PDF. Only text-based PDF decks are supported."
        ) from None

    extraction.truncated_pages = extraction.page_count > len(extraction.pages)
    return extraction


def deck_record(extraction: DeckExtraction, now: Callable[[], datetime]) -> FetchRecord:
    """The deck_text evidence record: page texts and provenance, no binary."""
    body: dict[str, Any] = {
        "kind": "deck_text",
        "file_name": extraction.file_name,
        "sha256": extraction.sha256,
        "page_count": extraction.page_count,
        "pages_extracted": len(extraction.pages),
        "truncated_pages": extraction.truncated_pages,
        "pages": {str(page.number): page.text for page in extraction.pages},
    }
    return FetchRecord(
        url=extraction.pseudo_url,
        params={},
        status=0,
        body=body,
        retrieved_at=now(),
    )
