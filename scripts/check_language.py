#!/usr/bin/env python3
"""Language check: fail when memo output uses accusatory language.

Memos state what the public record shows and with what confidence. They
never state or imply intent. This script is the mechanical enforcement: it
scans rendered memos and the memo templates with the shared banned-terms
helper and exits nonzero with file and line on any hit. The helper strips
URLs before matching, so a source URL whose slug contains a banned word is
not a hit; the memo's own prose remains fully gated.

Quoted-data exemption: a memo's claims table quotes the company's own deck
and site words verbatim, and those may legitimately contain banned
vocabulary. When a memo.md has a sibling casefile.json, its stored claim
texts are candidates for span-level exemption, the same exemption the
whole-memo backstop applies. But casefile.json is an editable file, so a
claim text is honored ONLY after re-verification against the sibling
evidence: normalized (whitespace, case, unicode quotes and dashes), it must
be a substring of the extracted source text persisted in
evidence/deck_text.json or the evidence/site_*.json records. No evidence,
no exemption: a hand-tampered casefile cannot widen this scan. Prose
outside the exact verified quoted strings stays fully gated everywhere.

Registry identity exemption: the subject's registered name, its previous
names, and the officer and PSC names render throughout a memo, and a
company may legitimately be registered under a name containing a banned
word. The sibling casefile's identity names are honored the same way claim
texts are: only after re-verification against the registry evidence files
(the profile's company_name and previous_company_names, and the name field
of officer and PSC list items), compared with the same normalization. A
name that the registry evidence does not carry buys no exemption.

Default targets when no paths are given: every memo.md under cases/ and
tests/fixtures/, plus everything under src/coldscreen/templates/.

Usage: python scripts/check_language.py [path ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

try:
    from coldscreen.language import find_banned_terms, normalize_for_match
except ImportError:  # running from a checkout without the package installed
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from coldscreen.language import find_banned_terms, normalize_for_match


def default_targets(root: Path) -> list[Path]:
    targets: list[Path] = []
    for base in (root / "cases", root / "tests" / "fixtures"):
        if base.is_dir():
            targets.extend(sorted(base.rglob("memo.md")))
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

    Each stored claim text is honored only when its normalized form appears
    in the normalized extracted source material persisted under the sibling
    evidence directory: the same quotation check the claims stage applied
    before storing it. Missing or unreadable casefile, missing evidence, or
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
        text for text in texts if normalize_for_match(text) and normalize_for_match(text) in corpus
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
        for line_number, term in scan_file(target):
            print(f"{target}:{line_number}: banned term {term!r}", file=sys.stderr)
            failed = True
    if failed:
        return 1
    print(f"language check: {checked} file(s) clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
