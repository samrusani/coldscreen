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
after re-verification against the sibling evidence: normalized
(whitespace, case, unicode quotes and dashes), it must be a substring
of the extracted source text persisted in evidence/deck_text.json or
the evidence/site_*.json records. A text that has fewer than two
tokens after normalize_for_match is never an exemption, even when
evidence would re-verify it. No evidence, no exemption: a
hand-tampered casefile cannot widen this scan.
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


def _evidence_corpus(evidence_dir: Path) -> str:
    """Extracted source text from the sibling evidence files.

    deck_text.json contributes its per-page texts; every site_*.json record
    of kind site_text contributes its extracted text. Anything unreadable
    contributes nothing.
    """
    if not evidence_dir.is_dir():
        return ""
    chunks: list[str] = []
    for candidate in sorted(evidence_dir.glob("*.json")):
        payload = _load_json(candidate)
        body = payload.get("body") if isinstance(payload, dict) else None
        if not isinstance(body, dict):
            continue
        kind = body.get("kind")
        if kind == "deck_text":
            pages = body.get("pages")
            if isinstance(pages, dict):
                chunks.extend(str(text) for text in pages.values())
        elif kind == "site_text" and isinstance(body.get("text"), str):
            chunks.append(body["text"])
    return "\n".join(chunks)


def claim_exemptions(path: Path) -> tuple[str, ...]:
    """Verified claim texts from the sibling casefile.json, when one exists.

    Each stored claim text is honored only when it has substance (two or
    more tokens after normalize_for_match) and its normalized form appears
    in the normalized extracted source material persisted under the sibling
    evidence directory: the same quotation check the claims stage applied
    before storing it. A single-token text is dropped even when evidence
    would re-verify it. Missing or unreadable casefile, missing evidence, or
    a text that fails re-verification all yield no exemption for it: the
    scan runs at full strictness, which is the fail-closed direction.
    """
    casefile_path = path.parent / "casefile.json"
    if not casefile_path.is_file():
        return ()
    data = _load_json(casefile_path)
    claims = data.get("claims") if isinstance(data, dict) else None
    if not isinstance(claims, list):
        return ()
    texts = [
        claim["text"]
        for claim in claims
        if isinstance(claim, dict) and isinstance(claim.get("text"), str)
    ]
    if not texts:
        return ()
    corpus = normalize_for_match(_evidence_corpus(path.parent / "evidence"))
    if not corpus:
        return ()
    verified = tuple(
        text
        for text in texts
        if claim_text_has_substance(text)
        and normalize_for_match(text)
        and normalize_for_match(text) in corpus
    )
    return verified


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
    fail closed. Identity exemptions are the same re-verified sibling
    evidence set the memo scan uses. Claim-quote exemptions are not
    applied: these fields are tool prose.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    exempt_texts = identity_exemptions(path)
    hits: list[tuple[str, str]] = []
    for field_path, text in _tool_authored_fields(data):
        for term in find_banned_terms(text, exempt_texts):
            hits.append((field_path, term))
    return hits


def scan_file(path: Path) -> list[tuple[int, str]]:
    """Return (line number, matched term) pairs for one file."""
    hits: list[tuple[int, str]] = []
    exempt_texts = claim_exemptions(path) + identity_exemptions(path)
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
