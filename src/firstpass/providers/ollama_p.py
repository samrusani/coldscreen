"""Ollama provider: local HTTP, no SDK, no key.

Coded from wiki/research/model-providers.md, not from memory:
- POST {base}/api/chat, body {model, messages, stream: false, format, options}
- format accepts a full JSON schema object, bare, no envelope
- non-streaming answer text at message.content (a single object, not a
  choices array); with a schema it is a JSON-encoded string the caller parses
- options.temperature 0 recommended for schema reliability

options.num_ctx is set explicitly because Ollama's default context window
(4096 tokens on most models) silently TRUNCATES longer prompts. The
synthesis casefile document plus the system prompt exceeds that, and a model
reasoning over a truncated casefile violates the core rule that it sees the
whole document or nothing. Verified locally 2026-08-19: gemma4:26b returned
empty content against the default window and behaves once num_ctx covers
the input. The window is configuration (settings key ollama_num_ctx, env
FIRSTPASS_OLLAMA_NUM_CTX, firstpass.toml), wired through the constructor.

The top-level "think" field is not in that wiki note; it comes from local
observation 2026-08-19: reasoning-family models (qwen3 and relatives)
returned corrupted or empty content under schema-constrained generation
while thinking was active, and behaved once "think": false was sent. Models
with no thinking support can reject the field outright, so it is tri-state
(settings key ollama_think, env FIRSTPASS_OLLAMA_THINK, firstpass.toml):
None omits it from the body entirely and is the default.

Local models keep the tool self-hostable end to end, so this provider must
never grow SDK or cloud dependencies.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..config import DEFAULT_OLLAMA_BASE_URL
from . import Message, ProviderError, ProviderResponseError

# Covers the synthesis prompt plus a compact casefile document plus thinking
# and output headroom on current local models (all of which accept >= 16k).
DEFAULT_NUM_CTX = 16384


def build_request_body(
    model: str,
    system: str,
    messages: list[Message],
    json_schema: dict[str, Any] | None,
    num_ctx: int = DEFAULT_NUM_CTX,
    think: bool | None = None,
) -> dict[str, Any]:
    """Pure request shape builder. The system prompt becomes the first
    message because /api/chat has no separate system parameter. think is
    tri-state: None leaves the field out of the body altogether."""
    chat_messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    chat_messages.extend({"role": m.role, "content": m.content} for m in messages)
    body: dict[str, Any] = {
        "model": model,
        "messages": chat_messages,
        "stream": False,
        "options": {"temperature": 0, "num_ctx": num_ctx},
    }
    if json_schema is not None:
        body["format"] = json_schema
    if think is not None:
        body["think"] = think
    return body


class OllamaProvider:
    """ModelProvider over a local (or self-hosted) Ollama daemon."""

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        timeout_seconds: float = 600.0,
        num_ctx: int = DEFAULT_NUM_CTX,
        think: bool | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self.base_url = (base_url or DEFAULT_OLLAMA_BASE_URL).rstrip("/")
        self.num_ctx = num_ctx
        self.think = think
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def complete(
        self,
        system: str,
        messages: list[Message],
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        body = build_request_body(
            self.model,
            system,
            messages,
            json_schema,
            num_ctx=self.num_ctx,
            think=self.think,
        )
        try:
            response = self._http.post("/api/chat", json=body)
        except httpx.TransportError as error:
            raise ProviderError(
                f"could not reach Ollama at {self.base_url}: {error}. Is the Ollama daemon running?"
            ) from error
        if response.status_code == 404:
            raise ProviderError(
                f"Ollama returned 404 for model {self.model!r}. Pull it first:"
                f" ollama pull {self.model}"
            )
        if response.status_code != 200:
            raise ProviderError(
                f"Ollama returned HTTP {response.status_code} from {self.base_url}/api/chat"
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise ProviderResponseError("Ollama returned a non-JSON response body") from error
        message = payload.get("message") if isinstance(payload, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content:
            done_reason = payload.get("done_reason") if isinstance(payload, dict) else None
            thinking = bool(message.get("thinking")) if isinstance(message, dict) else False
            detail = f" (done_reason: {done_reason}"
            detail += (
                ", thinking tokens were produced: reasoning-family models need"
                " ollama_think = false for schema-constrained synthesis)"
                if thinking
                else ")"
            )
            raise ProviderResponseError("Ollama response carried no message content" + detail)
        return content
