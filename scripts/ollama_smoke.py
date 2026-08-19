#!/usr/bin/env python3
"""Real synthesis smoke against local Ollama, using the golden casefile.

Loads tests/fixtures/golden/casefile.json, runs actual synthesis (localhost
HTTP, no keys, no cost), and prints the enforced verdict, the trigger set,
enforcement notes, retry counts, and wall time. This is manual verification,
not CI: pytest stays fully offline.

Usage: python scripts/ollama_smoke.py <model> [more models ...]
Example: python scripts/ollama_smoke.py qwen2.5:7b
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from firstpass.casedir import load_casefile  # noqa: E402
from firstpass.config import ollama_base_url_from_env  # noqa: E402
from firstpass.providers import ProviderError  # noqa: E402
from firstpass.providers.ollama_p import OllamaProvider  # noqa: E402
from firstpass.synthesis import SynthesisError, synthesize  # noqa: E402

GOLDEN_DIR = REPO_ROOT / "tests" / "fixtures" / "golden"


def smoke(model: str) -> int:
    casefile = load_casefile(GOLDEN_DIR)
    provider = OllamaProvider(model=model, base_url=ollama_base_url_from_env())
    print(f"== ollama smoke: {model} ==")
    started = time.monotonic()
    try:
        result = synthesize(casefile, provider, provider_name="ollama", model=model)
    except (SynthesisError, ProviderError) as error:
        elapsed = time.monotonic() - started
        print(f"FAILED after {elapsed:.1f}s: {error}")
        return 1
    finally:
        provider.close()
    elapsed = time.monotonic() - started
    print(f"enforced verdict: {result.verdict.level}")
    print(f"triggered: {result.verdict.triggered}")
    print(f"model proposed level: {result.metadata.model_level}")
    print(f"parse retries: {result.metadata.parse_retries}")
    print(f"language retries: {result.metadata.language_retries}")
    if result.metadata.enforcement_notes:
        print("enforcement notes:")
        for note in result.metadata.enforcement_notes:
            print(f"  - {note}")
    else:
        print("enforcement notes: none")
    print(f"questions: {len(result.verdict.questions)}")
    print(f"wall time: {elapsed:.1f}s")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python scripts/ollama_smoke.py <model> [more models ...]")
        return 2
    status = 0
    for model in argv:
        status = max(status, smoke(model))
        print()
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
