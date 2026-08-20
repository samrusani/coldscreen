"""Banned accusatory vocabulary: the single source for the language gate.

Memos state what the public record shows and with what confidence; they
never state or imply intent, dishonesty, or criminality. This module is the
one place the banned list and the matching rules live. Every enforcement
point imports find_banned_terms, so none of them can drift: the mechanical
gate over model output (coldscreen.synthesis), the whole-memo backstop that
runs before any memo reaches disk (coldscreen.pipeline), and the CI and
operator gate over rendered memos and the tool-authored fields of
casefile.json (coldscreen.check_language, also `coldscreen check-language`;
scripts/check_language.py is a checkout wrapper).

Three exemptions, all narrow and all span-level:

- URLs are stripped before matching, because source URLs are data pointers,
  not prose, and real adverse-media URLs legitimately carry words like the
  ones banned here in their slugs.
- Quoted data: a hit is exempt when its span lies inside an occurrence of
  one of the caller-supplied exempt_texts built from the casefile's stored
  claim texts: a company's own deck or site words may say it "fights
  fraud", and the memo's claims table quotes those words verbatim.
- Code-fetched rendered strings: registry identity names
  (registry_identity_names) plus the other strings the template prints
  from fetched data (code_fetched_exemption_texts): the registered-office
  display and its address parts, network overlap company names,
  appointment display strings, media source domains, stored claim source
  labels, and disqualification detail when present. They are code-fetched
  data, not model output. A company whose office locality or an overlap
  name contains a banned word must still be screenable honestly. Media
  query-category search terms are not in this set.

Exemption scope is deliberately narrow. Model prose fields (narrative,
rationale, questions, record notes) get NO claim-quote exemption at the
per-field gate in coldscreen.synthesis: the model references claims by id
and never repeats their wording. That gate does apply the code-fetched
set, because prose has to be able to name the company, its people, the
office, and an overlap company. The whole-memo backstop takes claim texts
but applies those exemptions only inside the rendered claims-table
region: the first line that is exactly `## Claims vs evidence` AND whose
next non-blank line is a template-controlled opener, through the next
ATX heading that starts with `## ` (the closer is not part of the
region). A heading without an opener is skipped. A missing start or a
missing closer leaves the region empty, so claim-quote exemptions apply
nowhere and code-fetched exemptions still apply to the whole memo.
Pre-table model fields (rationale, verdict_enforcement, enforcement
notes) are collapsed to one line at synthesis store and at render so a
multiline rationale cannot mint that heading. The CI memo scan still
applies claim-quote exemptions line-by-line across the file. The CI
casefile-field scan does not take claim texts: those fields are tool
prose, same polarity as the synthesis per-field gate. Claim texts
themselves are trustworthy only because the claims stage verifies each
one is a real substring of its declared source section (after
normalize_for_match on both sides) and has substance (two or more
whitespace tokens after that same normalize) before storing it.
claim_quote_is_verified is the shared re-check: CI and the on-disk
rerun path both honor a stored claim only when that predicate is true
against the sibling evidence-section map (evidence_sections). Screen
and in-memory tests that call language_backstop_failure without an
evidence dir keep the substance-only filter; claims were just verified
against in-memory sections, and evidence files may not exist yet
(--no-write). Missing or empty evidence on rerun means no claim-quote
exemptions. Single-token claim texts are never exemption spans. CI
also re-verifies identity names and the other code-fetched classes
against sibling evidence, and that re-verified set is the exemption
the casefile-field scan applies. Occurrence discovery advances by the
full match length, so overlapping occurrences of a self-similar quote
can never union into coverage of text that was never quoted as a whole.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:  # imported for typing only; language stays dependency-light
    from .models import CaseFile

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
    exemption), and by claim_quote_is_verified to re-check stored claims
    against sibling evidence. Both sides of every containment check go
    through this one function, so storage, rerun, and CI cannot drift.
    """
    folded = text.translate(_QUOTE_DASH_FOLD)
    return " ".join(folded.split()).casefold()


def claim_text_has_substance(text: str) -> bool:
    """True when, after normalize_for_match, the text has two or more tokens.

    Empty, whitespace-only, and single-token strings do not have substance.
    Storage, the substance-only backstop path, and claim_quote_is_verified
    share this helper so a one-word quote cannot become an exemption span.
    Identity names are not claims; do not apply this to them.
    """
    return len(normalize_for_match(text).split()) >= 2


def claim_quote_is_verified(text: object, source: object, sections: Mapping[str, str]) -> bool:
    """True when text is a substantive quotation of its declared section.

    Honor the text only when all of these hold: text and source are
    non-empty strings, claim_text_has_substance(text), sections has that
    exact source label, and normalize_for_match(text) is a substring of
    normalize_for_match(sections[source]). Missing source, unknown label,
    empty section, no substance, or a hit only in a different section:
    False. CI claim_exemptions and the on-disk rerun backstop share this
    predicate so they cannot drift.
    """
    if not isinstance(text, str) or not isinstance(source, str):
        return False
    if not text or not source.strip():
        return False
    if not claim_text_has_substance(text):
        return False
    section = sections.get(source)
    if not section:
        return False
    normalized = normalize_for_match(text)
    if not normalized:
        return False
    return normalized in normalize_for_match(section)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _display_path(url: str) -> str:
    return urlsplit(url).path or "/"


def _site_source_label(url: str) -> str:
    return f"site {_display_path(url)}"


def _site_record_url(payload: dict[str, Any], body: dict[str, Any]) -> str | None:
    """Requested URL for a site_text record. Never final_url."""
    url = body.get("url")
    if isinstance(url, str):
        return url
    top = payload.get("url")
    if isinstance(top, str):
        return top
    return None


def evidence_sections(evidence_dir: Path) -> dict[str, str]:
    """Label-to-text map from sibling evidence files.

    Deck pages become deck p.{key}. Site records become site {path} from
    the requested URL (body.url if a string, else the record url; never
    final_url) via urlsplit(url).path or "/". Duplicate labels concatenate.
    Unreadable files contribute nothing. Missing or empty evidence yields
    an empty map: no claim-quote exemptions. This is the one deck/site
    mapping; CI and the on-disk rerun path both call it.
    """
    if not evidence_dir.is_dir():
        return {}
    chunks: dict[str, list[str]] = {}
    for candidate in sorted(evidence_dir.glob("*.json")):
        payload = _load_json(candidate)
        if not isinstance(payload, dict):
            continue
        body = payload.get("body")
        if not isinstance(body, dict):
            continue
        kind = body.get("kind")
        if kind == "deck_text":
            pages = body.get("pages")
            if isinstance(pages, dict):
                for key, text in pages.items():
                    if isinstance(key, str) and isinstance(text, str):
                        chunks.setdefault(f"deck p.{key}", []).append(text)
        elif kind == "site_text" and isinstance(body.get("text"), str):
            url = _site_record_url(payload, body)
            if url is None:
                continue
            chunks.setdefault(_site_source_label(url), []).append(body["text"])
    return {label: "\n".join(texts) for label, texts in chunks.items()}


def collapse_whitespace(text: str) -> str:
    """Collapse any whitespace run, including newlines, to a single space.

    Same rule as stored record_note. Applied to pre-table model and tool
    fields (rationale, verdict_enforcement, enforcement notes) at synthesis
    store and at render so those fields cannot mint a claims-table heading
    line. Do not use this on claim texts.
    """
    return " ".join(text.split())


# Copied from CompanyProfile.registered_office_display. A pin test keeps
# this list aligned with the model property.
REGISTERED_OFFICE_ADDRESS_KEYS: tuple[str, ...] = (
    "care_of",
    "premises",
    "address_line_1",
    "address_line_2",
    "locality",
    "region",
    "postal_code",
    "country",
)


def registry_identity_names(casefile: CaseFile) -> tuple[str, ...]:
    """The registry identity exemption set for one casefile.

    The subject's registered name, its previous names, and the officer and
    PSC names, deduplicated in first-seen order. These strings were fetched
    from the registry by code (and re-validated on every casefile load), so
    exempting their exact rendered spans cannot launder model output; the
    CI scan additionally re-verifies them against the persisted registry
    evidence before honoring them. This is the name subset;
    code_fetched_exemption_texts adds the other rendered fetched strings.
    """
    names: list[str] = [casefile.subject.company_name]
    names.extend(p.name for p in casefile.subject.previous_company_names if p.name)
    names.extend(o.name for o in casefile.officers)
    names.extend(p.name for p in casefile.pscs if p.name)
    return tuple(dict.fromkeys(name for name in names if name and name.strip()))


def _appointment_name_portion(display: str) -> str | None:
    """Company-name side of an appointment display `{name} ({number})`."""
    if display.endswith(")") and " (" in display:
        name, _number = display.rsplit(" (", 1)
        return name or None
    return None


def code_fetched_exemption_texts(casefile: CaseFile) -> tuple[str, ...]:
    """Code-fetched strings the memo template prints, plus identity names.

    Deduplicated in first-seen order. Includes registry_identity_names,
    the registered-office display and each non-empty office address string
    for the keys that display uses, network overlap company names,
    appointment display strings and their company-name portion,
    media source domains, stored claim source labels, and disqualification
    detail when present. This is not a claim-quote exemption: claim texts
    stay region-scoped on the backstop and zero on the per-field gate.
    Media query-category search terms, media titles and snippets, finding
    statements, and filing descriptions are not in this set.
    """
    texts: list[str] = list(registry_identity_names(casefile))
    display = casefile.subject.registered_office_display
    if display:
        texts.append(display)
    address = casefile.subject.registered_office_address
    if isinstance(address, dict):
        for key in REGISTERED_OFFICE_ADDRESS_KEYS:
            value = address.get(key)
            if isinstance(value, str) and value.strip():
                texts.append(value)
    network = casefile.network
    if network is not None:
        for overlap in network.overlaps:
            if overlap.company_name and overlap.company_name.strip():
                texts.append(overlap.company_name)
        for appointment in network.appointments:
            for company in appointment.companies:
                if not company or not company.strip():
                    continue
                texts.append(company)
                name = _appointment_name_portion(company)
                if name and name.strip():
                    texts.append(name)
        for check in network.disqualification_checks:
            if check.detail and check.detail.strip():
                texts.append(check.detail)
    media = casefile.media
    if media is not None:
        for item in media.items:
            if item.source_domain and item.source_domain.strip():
                texts.append(item.source_domain)
    for claim in casefile.claims:
        if claim.source and claim.source.strip():
            texts.append(claim.source)
    return tuple(dict.fromkeys(text for text in texts if text and text.strip()))


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


# The template heading that opens the claims-vs-evidence table. Exact line
# match only: do not invent markers or HTML comments.
_CLAIMS_TABLE_HEADING = "## Claims vs evidence"

# Next-non-blank lines the template itself writes after that heading.
# A heading whose next non-blank line is not one of these is ignored.
_CLAIMS_TABLE_OPENERS: frozenset[str] = frozenset(
    {
        "What the company says about itself, against the public record. "
        "Claim text is quoted verbatim from the deck or site named in the "
        "source; the status column is the enforced assessment.",
        "| # | Claim (source) | Public record | Status |",
        "Claims extraction ran but produced no discrete claims from the "
        "provided deck or site text. See finding EXT-006.",
        "Claims extraction was not part of this casefile.",
    }
)
_CLAIMS_TABLE_OPENER_PREFIX = "Not performed:"


def _next_nonblank_content(lines: list[str], start_index: int) -> str | None:
    for line in lines[start_index:]:
        content = line.rstrip("\r\n")
        if content.strip():
            return content
    return None


def _is_claims_table_opener(content: str) -> bool:
    return content in _CLAIMS_TABLE_OPENERS or content.startswith(_CLAIMS_TABLE_OPENER_PREFIX)


def _claims_table_region(memo: str) -> tuple[int, int] | None:
    """Character offsets [start, end) of the claims-table region, or None.

    Start is the first line that is exactly the template heading whose next
    non-blank line is a template-controlled opener (the stock intro
    sentence, the table header, a `Not performed:` prefix, or one of the
    two empty-claims sentences). A heading without that opener is skipped.
    End is the next line that starts with `## ` (that closer is excluded).
    A missing start or a missing closer yields None: claim-quote
    exemptions then apply nowhere. Only the first accepted start/closer
    pair is used.
    """
    lines = memo.splitlines(keepends=True)
    start: int | None = None
    offset = 0
    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        if start is None:
            if content == _CLAIMS_TABLE_HEADING:
                nxt = _next_nonblank_content(lines, index + 1)
                if nxt is not None and _is_claims_table_opener(nxt):
                    start = offset
        elif content.startswith("## "):
            return start, offset
        offset += len(line)
    return None


def find_banned_terms_in_memo(
    memo: str,
    *,
    claim_texts: Iterable[str] = (),
    identity_names: Iterable[str] = (),
) -> list[str]:
    """Banned terms in a rendered memo, with claim quotes region-scoped.

    The memo is scanned as three pieces. Before and after the claims-table
    region, only identity_names are exempt (callers pass
    code_fetched_exemption_texts). Inside the region, claim_texts and
    identity_names are both exempt. Matching rules are find_banned_terms
    unchanged. If the region cannot be bounded, the whole memo is scanned
    with identity_names only.
    """
    identity = tuple(identity_names)
    bounds = _claims_table_region(memo)
    if bounds is None:
        return find_banned_terms(memo, identity)
    start, end = bounds
    claims_and_identity = (*claim_texts, *identity)
    return (
        find_banned_terms(memo[:start], identity)
        + find_banned_terms(memo[start:end], claims_and_identity)
        + find_banned_terms(memo[end:], identity)
    )
