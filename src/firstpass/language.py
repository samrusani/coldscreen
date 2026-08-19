"""Banned accusatory vocabulary: the single source for the language gate.

Memos state what the public record shows and with what confidence; they
never state or imply intent, dishonesty, or criminality. This module is the
one place the banned list and the matching rules live. Every enforcement
point imports find_banned_terms, so none of them can drift: the mechanical
gate over model output (firstpass.synthesis), the whole-memo backstop that
runs before any memo reaches disk (firstpass.cli), and the CI gate over
rendered memos (scripts/check_language.py).

URLs are exempt from the scan: they are stripped before matching because
source URLs are data pointers, not prose, and real adverse-media URLs
legitimately carry words like the ones banned here in their slugs. The
memo's own prose remains fully gated.
"""

from __future__ import annotations

import re

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


def find_banned_terms(text: str) -> list[str]:
    """Every banned term occurrence in text, in order, lowercased.

    URLs are stripped first: a banned word inside a URL slug is a data
    pointer, not prose, and does not count as a hit.
    """
    prose = URL_PATTERN.sub(" ", text)
    return [match.group(0).lower() for match in BANNED_PATTERN.finditer(prose)]
