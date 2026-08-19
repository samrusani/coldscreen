#!/usr/bin/env python3
"""Rubric anchoring check: the same casefile through two local models must
produce the SAME enforced verdict level.

The check runs on the AMBER fixture casefile (claims-free) by default,
because that is where the anchoring property is unconditional: every
trigger that can move the level there is mechanical, so a level mismatch
means the enforcement or the prompt is broken, not the models. Trigger sets
may differ on judgment triggers; the level must not.

The golden RED case is deliberately NOT the default: its level rides on
R4, a judgment trigger the enforcement gates but does not force, so two
models may legitimately land on different levels there (one cites the
surviving contradiction as material, the other does not). Set
COLDSCREEN_ANCHOR_CASE to another fixture directory to probe that behavior
knowingly.

Ollama settings are read the way the CLI reads them (coldscreen.toml, then
the environment), and the configured think value is the default for both
models. Thinking, though, is per model: the two compared need not agree about
it, because a non-thinking model can reject the field outright while a
reasoning-family model needs it set to false. Either argument may therefore
carry a think= suffix that overrides the default for that model alone. The
suffix is split off at the LAST colon and only when the tail reads think=,
because model names carry their own colon.

Usage: python scripts/anchor_check.py <model_a> <model_b>
       each argument takes an optional suffix: model[:think=false]
Example: python scripts/anchor_check.py qwen2.5:7b gemma4:26b
Example: python scripts/anchor_check.py qwen2.5-coder:7b qwen3:8b:think=false
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from coldscreen.casedir import load_casefile  # noqa: E402
from coldscreen.config import (  # noqa: E402
    Settings,
    coerce_bool,
    load_dotenv_if_present,
    load_settings,
    ollama_base_url_from_env,
)
from coldscreen.providers import ProviderError  # noqa: E402
from coldscreen.providers.ollama_p import OllamaProvider  # noqa: E402
from coldscreen.synthesis import SynthesisError, SynthesisResult, synthesize  # noqa: E402

DEFAULT_CASE_DIR = REPO_ROOT / "tests" / "fixtures" / "amber"
THINK_PREFIX = "think="


def case_dir() -> Path:
    override = os.environ.get("COLDSCREEN_ANCHOR_CASE", "").strip()
    return Path(override) if override else DEFAULT_CASE_DIR


def parse_model_argument(argument: str, default_think: bool | None) -> tuple[str, bool | None]:
    """Split "model[:think=false]" into a model name and its think value.

    The split is on the LAST colon and only when the tail reads think=,
    because Ollama model names carry their own colon and a plain name must
    survive untouched. With no suffix the model inherits the configured
    default. Accepted values are the config module's spellings.
    """
    model, separator, tail = argument.rpartition(":")
    if not separator or not tail.startswith(THINK_PREFIX):
        return argument, default_think
    if not model:
        raise ValueError(f"no model name in {argument!r}")
    return model, coerce_bool(tail[len(THINK_PREFIX) :])


def run_one(model: str, think: bool | None, settings: Settings) -> SynthesisResult:
    casefile = load_casefile(case_dir())
    provider = OllamaProvider(
        model=model,
        base_url=ollama_base_url_from_env(),
        timeout_seconds=settings.ollama_timeout_seconds,
        num_ctx=settings.ollama_num_ctx,
        think=think,
    )
    started = time.monotonic()
    try:
        result = synthesize(casefile, provider, provider_name="ollama", model=model)
    finally:
        provider.close()
    elapsed = time.monotonic() - started
    think_label = "unset" if think is None else str(think).lower()
    print(
        f"{model}: level={result.verdict.level}"
        f" triggered={result.verdict.triggered}"
        f" think={think_label}"
        f" parse_retries={result.metadata.parse_retries}"
        f" wall={elapsed:.1f}s"
    )
    return result


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "usage: python scripts/anchor_check.py <model_a> <model_b>"
            " (each argument takes an optional :think=false suffix)"
        )
        return 2
    load_dotenv_if_present()
    settings = load_settings()
    try:
        model_a, think_a = parse_model_argument(argv[0], settings.ollama_think)
        model_b, think_b = parse_model_argument(argv[1], settings.ollama_think)
    except ValueError as error:
        print(f"bad model argument: {error}")
        return 2
    print(f"== anchor check: {model_a} vs {model_b} ==")
    print(f"casefile: {case_dir()}")
    try:
        result_a = run_one(model_a, think_a, settings)
        result_b = run_one(model_b, think_b, settings)
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
