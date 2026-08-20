#!/usr/bin/env python3
"""Language check: fail when memo or casefile prose uses accusatory language.

Memos and the tool's own casefile statements state what the public record
shows and with what confidence. They never state or imply intent. This
script is the mechanical CI enforcement. The helper strips URLs before
matching, so a source URL whose slug contains a banned word is not a hit;
the prose itself remains fully gated.

Default targets when no paths are given: every memo.md and casefile.json
under cases/ and tests/fixtures/, plus everything under
src/coldscreen/templates/. An explicit argv path named casefile.json is
scanned as a casefile. An explicit memo.md is still scanned as memo text.

Memos and templates are scanned line by line. A casefile.json is parsed as
JSON and only these tool-authored string fields are scanned, when present
and a string:

- findings[].statement
- assessments[].record_note
- verdict.rationale
- verdict.questions[]
- narrative
- verdict_enforcement
- synthesis.enforcement_notes[]
- sanctions.skipped_reason, media.skipped_reason,
  claims_extraction.skipped_reason

The raw JSON blob is never scanned. Two committed fixtures make that
design wrong: golden stores the verified claim "Our platform eliminates
fraud in widget procurement" (quoted deck data, allowed in claims[].text),
and amber stores a media title that contains "fraud". MediaItem titles
exist for synthesis input and are omitted from the memo so headline
vocabulary cannot leak; scanning them would fail that fixture and any
real pack that recorded such a headline. claims[].text, media titles and
snippets, evidence excerpts, registry payloads, officer and PSC names as
their own targets, and disqualification details are out. A missing field
is not a hit. Extra unknown keys are ignored.

Quoted-data exemption (memos only): a memo's claims table quotes the
company's own deck and site words verbatim, and those may legitimately
contain banned vocabulary. When a memo.md has a sibling casefile.json, its
stored claim texts are candidates for span-level exemption on this
line-by-line memo scan. The in-process backstop scopes those same
claim texts to the claims-table region; this script does not. But
casefile.json is an editable file, so a claim text is honored ONLY
after re-verification against its declared source label in the sibling
evidence, not a joined corpus: normalized (whitespace, case, unicode
quotes and dashes), it must be a substring of that label's extracted
text. Deck pages map as deck p.{key} from body.pages. Site records map
as site {path} where path is urlsplit(url).path or "/", using body.url
if it is a string else the record url, never final_url. A text that
has fewer than two tokens after normalize_for_match is never an
exemption, even when evidence would re-verify it. Missing source,
unknown label, or a hit only in a different section: no exemption.
No evidence, no exemption: a hand-tampered casefile cannot widen this
scan. The CI memo scan is still line-by-line across the whole file.
Prose outside the exact verified quoted strings stays fully gated.

That claim-quote exemption does not apply to the casefile fields above.
A record_note or narrative that repeats a verified claim's banned wording
is a hit, even when claims[].text stores that sentence and evidence
verifies it. Same polarity as the synthesis per-field gate: model and
tool prose reference claims by id. Do not open a laundering channel
through the casefile scan.

Registry identity exemption (memos and casefile fields): the subject's
registered name, its previous names, and the officer and PSC names render
in memos and in finding statements, and a company may legitimately be
registered under a name containing a banned word. The sibling casefile's
identity names are honored only after re-verification against the
registry evidence files (the profile's company_name and
previous_company_names, and the name field of officer and PSC list
items), compared with the same normalization. A name that the registry
evidence does not carry buys no exemption. A finding statement that names
CROOK, Cuthbert passes only when registry evidence carries that name.

The same re-verify applies to the other code-fetched strings the template
prints. Office display and address parts are honored only when profile
evidence `body.registered_office_address` carries those strings (display
is reconstructed with the same join). Network overlap names and
appointment displays are honored only when appointment evidence
`items[].appointed_to` carries the name (and number, for the display
form); profile `company_name` is already collected. Media source domains
are honored only when a `kind == "search_results"` evidence body carries
that domain, or it can be derived from `results[].url` with the same
rule as `coldscreen.media.source_domain`. Claim source labels are
honored only when they are a key in the reconstructed evidence-section
map. Disqualification detail is not re-verified here (residual: the
rendered string is reconstructed from dates, not copied from the body).
No evidence, no exemption. The casefile-field scan uses this same
widened re-verified set and still applies no claim-quote exemption.
Media query-category search terms are not exempted. The memo scan stays
line-by-line across the whole file.

Unreadable or invalid JSON on a casefile that is itself a scan target
fails closed: an error is printed to stderr and the process exits 1. A
corrupt sibling casefile used only to build memo exemptions is still
ignored, which leaves that memo scan at full strictness.

Usage: python scripts/check_language.py [path ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    from coldscreen.language import (
        claim_text_has_substance,
        find_banned_terms,
        normalize_for_match,
    )
except ImportError:  # running from a checkout without the package installed
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from coldscreen.language import (
        claim_text_has_substance,
        find_banned_terms,
        normalize_for_match,
    )


CASEFILE_NAME = "casefile.json"
MEMO_NAME = "memo.md"


def default_targets(root: Path) -> list[Path]:
    targets: list[Path] = []
    for base in (root / "cases", root / "tests" / "fixtures"):
        if base.is_dir():
            targets.extend(
                sorted(
                    p
                    for p in base.rglob("*")
                    if p.is_file() and p.name in {MEMO_NAME, CASEFILE_NAME}
                )
            )
    templates = root / "src" / "coldscreen" / "templates"
    if templates.is_dir():
        targets.extend(sorted(p for p in templates.rglob("*") if p.is_file()))
    return targets


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


def _evidence_sections(evidence_dir: Path) -> dict[str, str]:
    """Label-to-text map from sibling evidence files.

    Deck pages become deck p.{key}. Site records become site {path} from
    the requested URL (body.url if a string, else the record url; never
    final_url) via urlsplit(url).path or "/". Duplicate labels concatenate.
    Unreadable files contribute nothing.
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


def claim_exemptions(path: Path) -> tuple[str, ...]:
    """Verified claim texts from the sibling casefile.json, when one exists.

    Each stored claim text is honored only when it has substance (two or
    more tokens after normalize_for_match), its source is a non-empty
    string naming a reconstructed evidence label, and its normalized form
    is a substring of that declared section only: not a joined corpus.
    Deck labels are deck p.{page key}. Site labels are site {path} from
    the requested URL path. A missing source, unknown label, empty
    section, or a hit only in a different section yields no exemption
    for that claim. A single-token text is dropped even when evidence
    would re-verify it. Missing or unreadable casefile, missing evidence,
    or a text that fails re-verification all yield no exemption for it:
    the scan runs at full strictness, which is the fail-closed direction.
    """
    casefile_path = path.parent / "casefile.json"
    if not casefile_path.is_file():
        return ()
    data = _load_json(casefile_path)
    claims = data.get("claims") if isinstance(data, dict) else None
    if not isinstance(claims, list):
        return ()
    sections = _evidence_sections(path.parent / "evidence")
    if not sections:
        return ()
    verified: list[str] = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        text = claim.get("text")
        source = claim.get("source")
        if not isinstance(text, str) or not isinstance(source, str) or not source.strip():
            continue
        section = sections.get(source)
        if not section:
            continue
        if not claim_text_has_substance(text):
            continue
        normalized = normalize_for_match(text)
        if normalized and normalized in normalize_for_match(section):
            verified.append(text)
    return tuple(verified)


def _casefile_identity_names(data: Any) -> list[str]:
    """Identity name strings a casefile claims: subject name, previous
    names, officer names, PSC names. Order preserved, blanks dropped."""
    if not isinstance(data, dict):
        return []
    names: list[str] = []
    subject = data.get("subject")
    if isinstance(subject, dict):
        if isinstance(subject.get("company_name"), str):
            names.append(subject["company_name"])
        previous = subject.get("previous_company_names")
        if isinstance(previous, list):
            names.extend(
                p["name"]
                for p in previous
                if isinstance(p, dict) and isinstance(p.get("name"), str)
            )
    for key in ("officers", "pscs"):
        entries = data.get(key)
        if isinstance(entries, list):
            names.extend(
                e["name"] for e in entries if isinstance(e, dict) and isinstance(e.get("name"), str)
            )
    return [name for name in names if name.strip()]


def _registry_evidence_names(evidence_dir: Path) -> set[str]:
    """Normalized name strings the registry evidence files actually carry.

    Sources: any evidence body's company_name and previous_company_names
    (the profile), and the name field of items entries (the officer and PSC
    lists). These are the fields the pipeline itself reads names from.
    """
    if not evidence_dir.is_dir():
        return set()
    names: set[str] = set()
    for candidate in sorted(evidence_dir.glob("*.json")):
        payload = _load_json(candidate)
        body = payload.get("body") if isinstance(payload, dict) else None
        if not isinstance(body, dict):
            continue
        if isinstance(body.get("company_name"), str):
            names.add(normalize_for_match(body["company_name"]))
        previous = body.get("previous_company_names")
        if isinstance(previous, list):
            names.update(
                normalize_for_match(p["name"])
                for p in previous
                if isinstance(p, dict) and isinstance(p.get("name"), str)
            )
        items = body.get("items")
        if isinstance(items, list):
            names.update(
                normalize_for_match(item["name"])
                for item in items
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            )
    names.discard("")
    return names


def identity_exemptions(path: Path) -> tuple[str, ...]:
    """Verified registry identity names from the sibling casefile.json.

    A name is honored only when its normalized form equals a name string
    present in the sibling registry evidence files. No evidence, no
    exemption: the scan stays at full strictness, the fail-closed
    direction.
    """
    casefile_path = path.parent / "casefile.json"
    if not casefile_path.is_file():
        return ()
    claimed = _casefile_identity_names(_load_json(casefile_path))
    if not claimed:
        return ()
    evidence_names = _registry_evidence_names(path.parent / "evidence")
    if not evidence_names:
        return ()
    verified = tuple(
        dict.fromkeys(name for name in claimed if normalize_for_match(name) in evidence_names)
    )
    return verified


# Copied from CompanyProfile.registered_office_display. A pin test keeps
# this list aligned with the model property. Do not import the model here.
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


def _office_display(address: dict[str, Any]) -> str:
    """Same join as CompanyProfile.registered_office_display."""
    parts = [address.get(key) for key in REGISTERED_OFFICE_ADDRESS_KEYS]
    return ", ".join(str(part) for part in parts if part)


def _source_domain_from_url(url: str) -> str:
    """Same two-liner as coldscreen.media.source_domain."""
    host = urlsplit(url).netloc.lower()
    return host.removeprefix("www.")


def _appointment_name_portion(display: str) -> str | None:
    if display.endswith(")") and " (" in display:
        name, _number = display.rsplit(" (", 1)
        return name or None
    return None


def _casefile_office_texts(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return []
    subject = data.get("subject")
    if not isinstance(subject, dict):
        return []
    texts: list[str] = []
    address = subject.get("registered_office_address")
    if isinstance(address, dict):
        display = _office_display(address)
        if display:
            texts.append(display)
        for key in REGISTERED_OFFICE_ADDRESS_KEYS:
            value = address.get(key)
            if isinstance(value, str) and value.strip():
                texts.append(value)
    return texts


def _office_evidence_texts(evidence_dir: Path) -> set[str]:
    """Normalized office strings profile evidence actually carries."""
    if not evidence_dir.is_dir():
        return set()
    texts: set[str] = set()
    for candidate in sorted(evidence_dir.glob("*.json")):
        payload = _load_json(candidate)
        body = payload.get("body") if isinstance(payload, dict) else None
        if not isinstance(body, dict):
            continue
        address = body.get("registered_office_address")
        if not isinstance(address, dict):
            continue
        display = _office_display(address)
        if display:
            texts.add(normalize_for_match(display))
        for key in REGISTERED_OFFICE_ADDRESS_KEYS:
            value = address.get(key)
            if isinstance(value, str) and value.strip():
                texts.add(normalize_for_match(value))
    texts.discard("")
    return texts


def _casefile_network_texts(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return []
    network = data.get("network")
    if not isinstance(network, dict):
        return []
    texts: list[str] = []
    overlaps = network.get("overlaps")
    if isinstance(overlaps, list):
        for overlap in overlaps:
            if isinstance(overlap, dict) and isinstance(overlap.get("company_name"), str):
                texts.append(overlap["company_name"])
    appointments = network.get("appointments")
    if isinstance(appointments, list):
        for appointment in appointments:
            if not isinstance(appointment, dict):
                continue
            companies = appointment.get("companies")
            if not isinstance(companies, list):
                continue
            for display in companies:
                if not isinstance(display, str) or not display.strip():
                    continue
                texts.append(display)
                name = _appointment_name_portion(display)
                if name and name.strip():
                    texts.append(name)
    return [text for text in texts if text.strip()]


def _network_evidence_texts(evidence_dir: Path) -> set[str]:
    """Normalized overlap/appointment names evidence actually carries.

    Sources: profile company_name, and appointment items[].appointed_to
    company_name and company_number. Display form `{name} ({number})` is
    reconstructed when both are present.
    """
    if not evidence_dir.is_dir():
        return set()
    texts: set[str] = set()
    for candidate in sorted(evidence_dir.glob("*.json")):
        payload = _load_json(candidate)
        body = payload.get("body") if isinstance(payload, dict) else None
        if not isinstance(body, dict):
            continue
        if isinstance(body.get("company_name"), str):
            texts.add(normalize_for_match(body["company_name"]))
        items = body.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            appointed_to = item.get("appointed_to")
            if not isinstance(appointed_to, dict):
                continue
            name = appointed_to.get("company_name")
            number = appointed_to.get("company_number")
            company_name = name if isinstance(name, str) and name.strip() else None
            company_number = (
                str(number).strip().upper() if isinstance(number, str) and number.strip() else ""
            )
            if company_name:
                texts.add(normalize_for_match(company_name))
            if company_number:
                texts.add(normalize_for_match(company_number))
                display = f"{company_name or 'unnamed company'} ({company_number})"
                texts.add(normalize_for_match(display))
    texts.discard("")
    return texts


def _casefile_media_domains(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return []
    media = data.get("media")
    if not isinstance(media, dict):
        return []
    items = media.get("items")
    if not isinstance(items, list):
        return []
    return [
        item["source_domain"]
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("source_domain"), str)
        and item["source_domain"].strip()
    ]


def _media_evidence_domains(evidence_dir: Path) -> set[str]:
    """Normalized source domains from search_results evidence bodies."""
    if not evidence_dir.is_dir():
        return set()
    domains: set[str] = set()
    for candidate in sorted(evidence_dir.glob("*.json")):
        payload = _load_json(candidate)
        body = payload.get("body") if isinstance(payload, dict) else None
        if not isinstance(body, dict) or body.get("kind") != "search_results":
            continue
        results = body.get("results")
        if not isinstance(results, list):
            continue
        for item in results:
            if not isinstance(item, dict):
                continue
            domain = item.get("source_domain")
            if isinstance(domain, str):
                if domain.strip():
                    domains.add(normalize_for_match(domain))
                continue
            url = item.get("url")
            if isinstance(url, str) and url.strip():
                derived = _source_domain_from_url(url)
                if derived:
                    domains.add(normalize_for_match(derived))
    domains.discard("")
    return domains


def _casefile_claim_sources(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return []
    claims = data.get("claims")
    if not isinstance(claims, list):
        return []
    return [
        claim["source"]
        for claim in claims
        if isinstance(claim, dict)
        and isinstance(claim.get("source"), str)
        and claim["source"].strip()
    ]


def _verified_subset(claimed: list[str], evidence: set[str]) -> tuple[str, ...]:
    if not claimed or not evidence:
        return ()
    return tuple(dict.fromkeys(text for text in claimed if normalize_for_match(text) in evidence))


def code_fetched_exemptions(path: Path) -> tuple[str, ...]:
    """Re-verified code-fetched strings from the sibling casefile.json.

    Identity names stay. Office, network names, media domains, and claim
    source labels join only when sibling evidence actually carries them.
    Disqualification detail is not re-verified (residual). No evidence, no
    exemption. Claim texts are not in this set.
    """
    identity = identity_exemptions(path)
    casefile_path = path.parent / "casefile.json"
    if not casefile_path.is_file():
        return identity
    data = _load_json(casefile_path)
    if not isinstance(data, dict):
        return identity
    evidence_dir = path.parent / "evidence"
    office = _verified_subset(_casefile_office_texts(data), _office_evidence_texts(evidence_dir))
    network = _verified_subset(_casefile_network_texts(data), _network_evidence_texts(evidence_dir))
    media = _verified_subset(_casefile_media_domains(data), _media_evidence_domains(evidence_dir))
    sections = _evidence_sections(evidence_dir)
    sources = tuple(
        dict.fromkeys(source for source in _casefile_claim_sources(data) if source in sections)
    )
    return tuple(dict.fromkeys((*identity, *office, *network, *media, *sources)))


def _tool_authored_fields(data: Any) -> list[tuple[str, str]]:
    """Tool-authored string fields the casefile scan actually reads.

    A missing field is not a hit. Extra unknown keys are ignored. Non-string
    values (including JSON null) are ignored. This is the field list, not
    the raw JSON blob: claim texts and media titles are deliberately out.
    """
    if not isinstance(data, dict):
        return []
    fields: list[tuple[str, str]] = []

    findings = data.get("findings")
    if isinstance(findings, list):
        for index, item in enumerate(findings):
            if isinstance(item, dict) and isinstance(item.get("statement"), str):
                fields.append((f"findings[{index}].statement", item["statement"]))

    assessments = data.get("assessments")
    if isinstance(assessments, list):
        for index, item in enumerate(assessments):
            if isinstance(item, dict) and isinstance(item.get("record_note"), str):
                fields.append((f"assessments[{index}].record_note", item["record_note"]))

    verdict = data.get("verdict")
    if isinstance(verdict, dict):
        if isinstance(verdict.get("rationale"), str):
            fields.append(("verdict.rationale", verdict["rationale"]))
        questions = verdict.get("questions")
        if isinstance(questions, list):
            for index, question in enumerate(questions):
                if isinstance(question, str):
                    fields.append((f"verdict.questions[{index}]", question))

    if isinstance(data.get("narrative"), str):
        fields.append(("narrative", data["narrative"]))
    if isinstance(data.get("verdict_enforcement"), str):
        fields.append(("verdict_enforcement", data["verdict_enforcement"]))

    synthesis = data.get("synthesis")
    if isinstance(synthesis, dict):
        notes = synthesis.get("enforcement_notes")
        if isinstance(notes, list):
            for index, note in enumerate(notes):
                if isinstance(note, str):
                    fields.append((f"synthesis.enforcement_notes[{index}]", note))

    for key in ("sanctions", "media", "claims_extraction"):
        block = data.get(key)
        if isinstance(block, dict) and isinstance(block.get("skipped_reason"), str):
            fields.append((f"{key}.skipped_reason", block["skipped_reason"]))

    return fields


def scan_casefile(path: Path) -> list[tuple[str, str]] | None:
    """Return (field path, matched term) pairs for one casefile.

    None means the file was unreadable or not valid JSON: the caller must
    fail closed. Code-fetched exemptions are the same re-verified sibling
    evidence set the memo scan uses. Claim-quote exemptions are not
    applied: these fields are tool prose.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    exempt_texts = code_fetched_exemptions(path)
    hits: list[tuple[str, str]] = []
    for field_path, text in _tool_authored_fields(data):
        for term in find_banned_terms(text, exempt_texts):
            hits.append((field_path, term))
    return hits


def scan_file(path: Path) -> list[tuple[int, str]]:
    """Return (line number, matched term) pairs for one file."""
    hits: list[tuple[int, str]] = []
    exempt_texts = claim_exemptions(path) + code_fetched_exemptions(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    for line_number, line in enumerate(text.splitlines(), start=1):
        for term in find_banned_terms(line, exempt_texts):
            hits.append((line_number, term))
    return hits


def main(argv: list[str]) -> int:
    root = Path.cwd()
    if argv:
        targets = [Path(arg) for arg in argv]
    else:
        targets = default_targets(root)
    if not targets:
        print("language check: no target files found, nothing to scan")
        return 0

    explicit = bool(argv)
    failed = False
    checked = 0
    for target in targets:
        if not target.is_file():
            # An explicitly named file that does not exist is an error, not
            # a skip: a typo must not turn into a silent pass.
            print(f"language check: no such file: {target}", file=sys.stderr)
            if explicit:
                failed = True
            continue
        checked += 1
        if target.name == CASEFILE_NAME:
            casefile_hits = scan_casefile(target)
            if casefile_hits is None:
                print(
                    f"language check: unreadable or invalid JSON: {target}",
                    file=sys.stderr,
                )
                failed = True
                continue
            for field_path, term in casefile_hits:
                print(f"{target}:{field_path}: banned term {term!r}", file=sys.stderr)
                failed = True
            continue
        for line_number, term in scan_file(target):
            print(f"{target}:{line_number}: banned term {term!r}", file=sys.stderr)
            failed = True
    if failed:
        return 1
    print(f"language check: {checked} file(s) clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
