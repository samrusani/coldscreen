#!/usr/bin/env python3
"""Rubric anchoring check: the same golden casefile through two local
models must produce the SAME enforced verdict level.

With enforcement in place the level is a pure function of the enforced
trigger set, so a level mismatch means the enforcement or the prompt is
broken, not the models. Trigger sets may differ on judgment triggers; the
level must not.

Usage: python scripts/anchor_check.py <model_a> <model_b>
Example: python scripts/anchor_check.py qwen2.5:7b gemma4:26b
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
from firstpass.synthesis import SynthesisError, SynthesisResult, synthesize  # noqa: E402

GOLDEN_DIR = REPO_ROOT / "tests" / "fixtures" / "golden"


def run_one(model: str) -> SynthesisResult:
    casefile = load_casefile(GOLDEN_DIR)
    provider = OllamaProvider(model=model, base_url=ollama_base_url_from_env())
    started = time.monotonic()
    try:
        result = synthesize(casefile, provider, provider_name="ollama", model=model)
    finally:
        provider.close()
    elapsed = time.monotonic() - started
    print(
        f"{model}: level={result.verdict.level}"
        f" triggered={result.verdict.triggered}"
        f" parse_retries={result.metadata.parse_retries}"
        f" wall={elapsed:.1f}s"
    )
    return result


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python scripts/anchor_check.py <model_a> <model_b>")
        return 2
    model_a, model_b = argv
    print(f"== anchor check: {model_a} vs {model_b} ==")
    try:
        result_a = run_one(model_a)
        result_b = run_one(model_b)
    except (SynthesisError, ProviderError) as error:
        print(f"FAILED: {error}")
        return 1
    if result_a.verdict.level != result_b.verdict.level:
        print(
            f"ANCHOR FAILURE: {model_a} says {result_a.verdict.level},"
            f" {model_b} says {result_b.verdict.level}. The enforcement or the"
            " prompt is underspecified; fix that, not the model choice."
        )
        return 1
    print(
        f"ANCHOR OK: both models produce {result_a.verdict.level}."
        f" Trigger sets: {model_a}={result_a.verdict.triggered}"
        f" {model_b}={result_b.verdict.triggered}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
