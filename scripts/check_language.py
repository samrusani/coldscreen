#!/usr/bin/env python3
"""Thin checkout wrapper for coldscreen.check_language.

Usage: python scripts/check_language.py [path ...]
The shipped command is `coldscreen check-language`.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from coldscreen.check_language import main
except ImportError:  # running from a checkout without the package installed
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from coldscreen.check_language import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
