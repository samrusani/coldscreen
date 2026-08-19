"""Anthropic provider. Optional extra: pip install "firstpass-screen[anthropic]".

Coded from wiki/research/model-providers.md, not from memory:
- client.messages.create(model, max_tokens, system, messages)
- JSON schema output via output_config={"format": {"type": "json_schema",
  "schema": ...}} (the old top-level output_format is deprecated)
- response.content is a list of typed blocks; keep type == "text" only
- stop_reason can be "refusal" on current models; check before reading
- adaptive thinking shares the max_tokens budget, so leave headroom
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from . import Message, ProviderError, ProviderNotInstalledError, ProviderResponseError

API_KEY_ENV = "ANTHROPIC_API_KEY"
# Thinking is on by default on current models and shares this budget.
DEFAULT_MAX_TOKENS = 16000

INSTALL_HINT = (
    "the anthropic SDK is not installed. Install the optional extra:"
    ' pip install "firstpass-screen[anthropic]"'
)


def build_request_kwargs(
    model: str,
    system: str,
    messages: list[Message],
    json_schema: dict[str, Any] | None,
    max_tokens: int,
) -> dict[str, Any]:
    """Pure request shape builder, unit-testable without the SDK."""
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": m.role, "content": m.content} for m in messages],
    }
    if json_schema is not None:
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": json_schema}}
    return kwargs


def extract_text(response: Any) -> str:
    """Concatenate text blocks; refuse to fabricate on refusal or empty."""
    if getattr(response, "stop_reason", None) == "refusal":
        raise ProviderResponseError("the model declined to answer (stop_reason: refusal)")
    parts = [
        getattr(block, "text", "")
        for block in getattr(response, "content", [])
        if getattr(block, "type", None) == "text"
    ]
    text = "".join(parts)
    if not text:
        raise ProviderResponseError("the model response contained no text blocks")
    return text


class AnthropicProvider:
    """ModelProvider over the official anthropic SDK."""

    def __init__(
        self,
        model: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        if client_factory is not None:
            self._client = client_factory()
            return
        try:
            import anthropic
        except ImportError as error:
            raise ProviderNotInstalledError(INSTALL_HINT) from error
        if not os.environ.get(API_KEY_ENV, "").strip():
            raise ProviderError(
                f"{API_KEY_ENV} is not set. Export your Anthropic API key to use this provider."
            )
        self._client = anthropic.Anthropic()

    def complete(
        self,
        system: str,
        messages: list[Message],
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        kwargs = build_request_kwargs(self.model, system, messages, json_schema, self.max_tokens)
        response = self._client.messages.create(**kwargs)
        return extract_text(response)
