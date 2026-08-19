"""OpenAI provider. Optional extra: pip install "coldscreen[openai]".

Coded from wiki/research/model-providers.md, not from memory:
- Responses API: client.responses.create(model, instructions, input, ...)
- JSON schema output via text={"format": {"type": "json_schema",
  "name": ..., "schema": ..., "strict": True}} (flat shape; the
  chat.completions double-nested response_format is NOT interchangeable)
- read response.output_text

The openai SDK depends on httpx2, a separate distribution that coexists
with this project's httpx pin (verified at install time). Do not respx-test
it; tests inject a fake client object instead.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from . import Message, ProviderError, ProviderNotInstalledError, ProviderResponseError

API_KEY_ENV = "OPENAI_API_KEY"
SCHEMA_NAME = "coldscreen_synthesis"

INSTALL_HINT = (
    'the openai SDK is not installed. Install the optional extra: pip install "coldscreen[openai]"'
)


def build_request_kwargs(
    model: str,
    system: str,
    messages: list[Message],
    json_schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """Pure request shape builder, unit-testable without the SDK."""
    kwargs: dict[str, Any] = {
        "model": model,
        "instructions": system,
        "input": [{"role": m.role, "content": m.content} for m in messages],
    }
    if json_schema is not None:
        kwargs["text"] = {
            "format": {
                "type": "json_schema",
                "name": SCHEMA_NAME,
                "schema": json_schema,
                "strict": True,
            }
        }
    return kwargs


class OpenAIProvider:
    """ModelProvider over the official openai SDK (Responses API)."""

    def __init__(
        self,
        model: str,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.model = model
        if client_factory is not None:
            self._client = client_factory()
            return
        try:
            import openai
        except ImportError as error:
            raise ProviderNotInstalledError(INSTALL_HINT) from error
        if not os.environ.get(API_KEY_ENV, "").strip():
            raise ProviderError(
                f"{API_KEY_ENV} is not set. Export your OpenAI API key to use this provider."
            )
        self._client = openai.OpenAI()

    def complete(
        self,
        system: str,
        messages: list[Message],
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        kwargs = build_request_kwargs(self.model, system, messages, json_schema)
        response = self._client.responses.create(**kwargs)
        text = getattr(response, "output_text", "") or ""
        if not text:
            raise ProviderResponseError("the model response contained no output text")
        return str(text)
