#!/usr/bin/env python3
"""Language check: fail when memo output uses accusatory language.

Memos state what the public record shows and with what confidence. They
never state or imply intent. This script is the mechanical enforcement: it
scans rendered memos and the memo templates for a banned word list and exits
nonzero with file and line on any hit.

Default targets when no paths are given: every memo.md under cases/ and
tests/fixtures/, plus everything under src/firstpass/templates/.

Usage: python scripts/check_language.py [path ...]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

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


def default_targets(root: Path) -> list[Path]:
    targets: list[Path] = []
    for base in (root / "cases", root / "tests" / "fixtures"):
        if base.is_dir():
            targets.extend(sorted(base.rglob("memo.md")))
    templates = root / "src" / "firstpass" / "templates"
    if templates.is_dir():
        targets.extend(sorted(p for p in templates.rglob("*") if p.is_file()))
    return targets


def scan_file(path: Path) -> list[tuple[int, str]]:
    """Return (line number, matched term) pairs for one file."""
    hits: list[tuple[int, str]] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in BANNED_PATTERN.finditer(line):
            hits.append((line_number, match.group(0)))
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
