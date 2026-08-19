"""Model provider abstraction, per ARCHITECTURE.md section 7.

Core hands every provider the same three things: a system prompt, a message
list, and optionally one plain JSON schema dict. Each implementation
translates the schema to its own dialect (anthropic output_config, openai
text.format, ollama bare format). Nothing provider-specific leaks above this
package.

The model is a text generator over the assembled casefile document, nothing
more: no provider-side tools, no web access, no memory of facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

KNOWN_PROVIDERS = ("anthropic", "openai", "ollama")

# Defaults when only a provider name is given. Ollama has no default model
# on purpose: local model names are installation-specific.
DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "openai": "gpt-5.6-sol",
}


@dataclass(frozen=True)
class Message:
    """One conversation turn handed to a provider."""

    role: Literal["user", "assistant"]
    content: str


class ModelProvider(Protocol):
    """Plain completion plus optional JSON-schema-constrained output."""

    def complete(
        self,
        system: str,
        messages: list[Message],
        json_schema: dict[str, Any] | None = None,
    ) -> str: ...


class ProviderError(Exception):
    """Base error for model provider problems. Messages are user-facing."""


class ProviderNotInstalledError(ProviderError):
    """The optional SDK extra for this provider is not installed."""


class ProviderResponseError(ProviderError):
    """The provider answered, but not with usable text."""


class ModelSpecError(ProviderError):
    """The provider:model specification could not be parsed."""


def parse_model_spec(spec: str) -> tuple[str, str]:
    """Parse "provider:model" into its parts.

    The split is on the FIRST colon only, because Ollama model names contain
    colons (ollama:qwen2.5:7b). A bare provider name falls back to that
    provider's default model; Ollama has none and requires an explicit model.
    """
    text = spec.strip()
    if not text:
        raise ModelSpecError("empty model specification")
    provider, _, model = text.partition(":")
    provider = provider.strip().lower()
    model = model.strip()
    if provider not in KNOWN_PROVIDERS:
        raise ModelSpecError(
            f"unknown provider {provider!r}: expected one of {', '.join(KNOWN_PROVIDERS)}"
            ' (format "provider:model", for example "ollama:qwen2.5:7b")'
        )
    if not model:
        default = DEFAULT_MODELS.get(provider)
        if default is None:
            raise ModelSpecError(
                'ollama needs an explicit model, for example "ollama:qwen2.5:7b":'
                " local model names are installation-specific"
            )
        model = default
    return provider, model


def get_provider(
    provider: str,
    model: str,
    ollama_base_url: str | None = None,
    ollama_timeout_seconds: float = 600.0,
    ollama_num_ctx: int = 16384,
    ollama_think: bool | None = None,
) -> ModelProvider:
    """Build the named provider. SDK imports are lazy: an extra that is not
    installed produces a clear install hint, not a bare ImportError."""
    if provider == "anthropic":
        from .anthropic_p import AnthropicProvider

        return AnthropicProvider(model=model)
    if provider == "openai":
        from .openai_p import OpenAIProvider

        return OpenAIProvider(model=model)
    if provider == "ollama":
        from .ollama_p import OllamaProvider

        return OllamaProvider(
            model=model,
            base_url=ollama_base_url,
            timeout_seconds=ollama_timeout_seconds,
            num_ctx=ollama_num_ctx,
            think=ollama_think,
        )
    raise ModelSpecError(f"unknown provider {provider!r}")
