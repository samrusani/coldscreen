#!/usr/bin/env python3
"""Real synthesis smoke against local Ollama, on a fixture casefile.

Loads a fixture casefile (default tests/fixtures/amber: the claims-free
AMBER case), runs actual synthesis (localhost HTTP, no keys, no cost), and
prints the enforced verdict, the trigger set, enforcement notes, retry
counts, and wall time. This is manual verification, not CI: pytest stays
fully offline.

Set FIRSTPASS_SMOKE_CASE to another fixture directory to smoke a different
case; tests/fixtures/golden (the RED case with stored claims) exercises the
claim assessment path against a real model.

Ollama settings are read the way the CLI reads them (firstpass.toml, then
the environment), so the run here matches a real screen. That matters most
for FIRSTPASS_OLLAMA_THINK: a reasoning-family model needs it set to false
before it will produce usable schema-constrained output.

Usage: python scripts/ollama_smoke.py <model> [more models ...]
Example: python scripts/ollama_smoke.py qwen2.5:7b
Example: FIRSTPASS_OLLAMA_THINK=false python scripts/ollama_smoke.py qwen3:8b
Example: FIRSTPASS_SMOKE_CASE=tests/fixtures/golden python scripts/ollama_smoke.py qwen3:8b
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from firstpass.casedir import load_casefile  # noqa: E402
from firstpass.config import (  # noqa: E402
    Settings,
    load_dotenv_if_present,
    load_settings,
    ollama_base_url_from_env,
)
from firstpass.providers import ProviderError  # noqa: E402
from firstpass.providers.ollama_p import OllamaProvider  # noqa: E402
from firstpass.synthesis import SynthesisError, synthesize  # noqa: E402

DEFAULT_CASE_DIR = REPO_ROOT / "tests" / "fixtures" / "amber"


def case_dir() -> Path:
    override = os.environ.get("FIRSTPASS_SMOKE_CASE", "").strip()
    return Path(override) if override else DEFAULT_CASE_DIR


def smoke(model: str, settings: Settings) -> int:
    casefile = load_casefile(case_dir())
    provider = OllamaProvider(
        model=model,
        base_url=ollama_base_url_from_env(),
        timeout_seconds=settings.ollama_timeout_seconds,
        num_ctx=settings.ollama_num_ctx,
        think=settings.ollama_think,
    )
    think = settings.ollama_think
    think_label = "unset, so the field is omitted" if think is None else str(think).lower()
    print(f"== ollama smoke: {model} ==")
    print(f"casefile: {case_dir()}")
    print(f"num_ctx: {settings.ollama_num_ctx}, think: {think_label}")
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
    load_dotenv_if_present()
    settings = load_settings()
    status = 0
    for model in argv:
        status = max(status, smoke(model, settings))
        print()
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
