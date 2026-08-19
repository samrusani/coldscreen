"""Banned accusatory vocabulary: the single source for the language gate.

Memos state what the public record shows and with what confidence; they
never state or imply intent, dishonesty, or criminality. This module is the
one place the banned list and the matching rules live. Every enforcement
point imports find_banned_terms, so none of them can drift: the mechanical
gate over model output (firstpass.synthesis), the whole-memo backstop that
runs before any memo reaches disk (firstpass.cli), and the CI gate over
rendered memos (scripts/check_language.py).

Two exemptions, both narrow and both span-level:

- URLs are stripped before matching, because source URLs are data pointers,
  not prose, and real adverse-media URLs legitimately carry words like the
  ones banned here in their slugs.
- Quoted data: a hit is exempt when its span lies inside an occurrence of
  one of the caller-supplied exempt_texts. The only exempt texts in this
  tool are the casefile's stored claim texts: a company's own deck or site
  words may say it "fights fraud", and the memo's claims table quotes those
  words verbatim.

Exemption scope is deliberately narrow. Model prose fields (narrative,
rationale, questions, record notes) are gated with ZERO exemptions at the
per-field gate in firstpass.synthesis: the model references claims by id
and never repeats their wording. Only the whole-memo backstop and the CI
scan take exempt texts, because the code-rendered claims table quotes the
stored claim strings verbatim. Claim texts themselves are trustworthy only
because the claims stage verifies each one is a real substring of its
declared source section (after normalize_for_match on both sides) before
storing it, and scripts/check_language.py re-verifies stored claims against
the sibling evidence files before honoring them. Occurrence discovery
advances by the full match length, so overlapping occurrences of a
self-similar quote can never union into coverage of text that was never
quoted as a whole.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

BANNED_TERMS: tuple[str, ...] = (
    "fraud",
    "fraudulent",
    "fraudster",
    "lying",
    "liar",
    "lied",
    "criminal",
    "crime",
    "scam",
    "sham",
    "dishonest",
    "dishonesty",
    "deceit",
    "deceitful",
    "crook",
    "con artist",
)


def _term_pattern(term: str) -> str:
    """One term as a regex: word-bounded, any whitespace inside multiword terms."""
    return r"\s+".join(re.escape(part) for part in term.split())


BANNED_PATTERN = re.compile(
    r"\b(" + "|".join(_term_pattern(term) for term in BANNED_TERMS) + r")\b",
    re.IGNORECASE,
)

# URLs are stripped before matching; see the module docstring. Replaced with
# a space so the removal can never join two words into a new token.
URL_PATTERN = re.compile(r"https?://\S+")

# Folding table for quotation verification: unicode single and double quote
# variants to their ASCII forms, dash variants to a plain hyphen. Applied on
# BOTH sides of every containment check, so a deck's curly apostrophe cannot
# defeat verification of a straight-quoted claim, and vice versa.
_QUOTE_DASH_FOLD = str.maketrans(
    {
        "\u2018": "'",  # left single quotation mark
        "\u2019": "'",  # right single quotation mark
        "\u201a": "'",  # single low-9 quotation mark
        "\u201b": "'",  # single high-reversed-9 quotation mark
        "\u201c": '"',  # left double quotation mark
        "\u201d": '"',  # right double quotation mark
        "\u201e": '"',  # double low-9 quotation mark
        "\u201f": '"',  # double high-reversed-9 quotation mark
        "\u2010": "-",  # hyphen
        "\u2011": "-",  # non-breaking hyphen
        "\u2012": "-",  # figure dash
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2015": "-",  # horizontal bar
        "\u2212": "-",  # minus sign
    }
)


def normalize_for_match(text: str) -> str:
    """One canonical form for quotation-containment checks.

    Whitespace runs collapse to single spaces, the text is casefolded, and
    unicode quote and dash variants fold to ASCII. Used by the claims stage
    to verify a claim's text really appears in its declared source section
    before it is stored (and so before it can ever act as a language-gate
    exemption), and by scripts/check_language.py to re-verify stored claims
    against the sibling evidence files. Both sides of every containment
    check go through this one function, so the two verifiers cannot drift.
    """
    folded = text.translate(_QUOTE_DASH_FOLD)
    return " ".join(folded.split()).casefold()


def _exempt_spans(prose: str, exempt_texts: Iterable[str]) -> list[tuple[int, int]]:
    """Every occurrence of every exempt text in prose, as (start, end) spans.

    Matching is exact and literal: the exemption exists for stored quoted
    strings rendered verbatim, so anything looser would widen the gate.
    Blank exempt texts are ignored; a zero-width span can exempt nothing.
    Discovery advances by the MATCH LENGTH, never one character: overlapping
    occurrences of a self-similar exempt text (stored "fraud fraud" against
    prose "fraud fraud fraud") must not union into a span covering a third
    token that was never part of a whole quotation.
    """
    spans: list[tuple[int, int]] = []
    for text in exempt_texts:
        if not text or not text.strip():
            continue
        start = prose.find(text)
        while start != -1:
            spans.append((start, start + len(text)))
            start = prose.find(text, start + len(text))
    return spans


def find_banned_terms(text: str, exempt_texts: Iterable[str] = ()) -> list[str]:
    """Every banned term occurrence in text, in order, lowercased.

    URLs are stripped first: a banned word inside a URL slug is a data
    pointer, not prose, and does not count as a hit. A hit whose span lies
    entirely inside an occurrence of one of exempt_texts (the casefile's
    stored claim texts, quoted data by definition) is exempt; the same word
    anywhere else in the text still counts.
    """
    prose = URL_PATTERN.sub(" ", text)
    spans = _exempt_spans(prose, exempt_texts)
    hits: list[str] = []
    for match in BANNED_PATTERN.finditer(prose):
        if any(start <= match.start() and match.end() <= end for start, end in spans):
            continue
        hits.append(match.group(0).lower())
    return hits
