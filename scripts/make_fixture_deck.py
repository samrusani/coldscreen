#!/usr/bin/env python3
"""Regenerate the committed fictional fixture deck PDF, byte-for-byte.

The deck belongs to FABRICATED WIDGETS LTD (99999999), the fully fictional
fixture company, and plants the golden-case contradictions on purpose:

- "Operating since 2015" against a registry incorporation date of
  2019-05-14 (tests/fixtures/profile.json): the R4 material contradiction.
- "debt free" against the outstanding charge in tests/fixtures/charges.json:
  the second planted contradiction.
- "eliminates fraud" as unfalsifiable puffery whose quoted text carries
  banned vocabulary: the quoted-data exemption case. It renders in the
  memo's claims table and must pass every language gate as a span-exempt
  quotation.
- "team of 40" as a checkable claim no public source can speak to: the A4
  unverified case.

The PDF is handwritten (no PDF-writing dependency): a fixed-layout,
Helvetica-only, uncompressed structure that pdfplumber extracts exactly.
The output is deterministic, so regeneration is diff-clean unless the
content constants change.

Usage: python scripts/make_fixture_deck.py [output_path]
Default output: tests/fixtures/deck_fabricated_widgets.pdf
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "tests" / "fixtures" / "deck_fabricated_widgets.pdf"

# One inner list per page, one string per rendered text line. Everything is
# fictional; no real company, person, or product is referenced.
PAGES: list[list[str]] = [
    [
        "Fabricated Widgets Ltd",
        "Investor overview",
        "An entirely fictional company used as a test fixture.",
    ],
    [
        "Operating since 2015 with a national footprint.",
        "The company is debt free and self funded.",
        "Our platform eliminates fraud in widget procurement.",
    ],
    [
        "A team of 40 widget engineers.",
        "The most trusted name in widgets.",
    ],
]


def _escape(text: str) -> str:
    """PDF string literal escaping for the three special characters."""
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _content_stream(lines: list[str]) -> bytes:
    """One page's content: 12pt Helvetica lines from the top-left margin."""
    ops = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
    for index, line in enumerate(lines):
        if index:
            ops.append("T*")
        ops.append(f"({_escape(line)}) Tj")
    ops.append("ET")
    return "\n".join(ops).encode("ascii")


def build_pdf(pages: list[list[str]]) -> bytes:
    """A minimal valid PDF: catalog, pages tree, one shared font, and per
    page a page object plus an uncompressed content stream."""
    objects: list[bytes] = []  # bodies in object-number order, 1-indexed

    page_count = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(page_count))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, lines in enumerate(pages):
        stream = _content_stream(lines)
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
                f" /Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream"
        )

    buffer = io.BytesIO()
    buffer.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(buffer.tell())
        buffer.write(f"{number} 0 obj\n".encode("ascii"))
        buffer.write(body)
        buffer.write(b"\nendobj\n")
    xref_at = buffer.tell()
    buffer.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    buffer.write(b"0000000000 65535 f \n")
    for offset in offsets:
        buffer.write(f"{offset:010d} 00000 n \n".encode("ascii"))
    buffer.write(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
        ).encode("ascii")
    )
    return buffer.getvalue()


def main(argv: list[str]) -> int:
    output = Path(argv[0]) if argv else DEFAULT_OUTPUT
    pdf_bytes = build_pdf(PAGES)
    output.write_bytes(pdf_bytes)
    print(f"wrote {output} ({len(pdf_bytes)} bytes, {len(PAGES)} pages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
