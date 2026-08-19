"""Provider protocol: spec parsing, exact request shapes, error hygiene.

The request payloads are asserted against the verified shapes in
wiki/research/model-providers.md. Anthropic and OpenAI are tested through
pure shape builders plus injected fake clients (the openai SDK runs on
httpx2, which respx cannot intercept). Ollama is tested over respx.

The anchor_check argument parser is covered here too: it is the same model
argument surface one colon further out, and its splitting rule only makes
sense next to parse_model_spec.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import respx

from coldscreen.config import Settings
from coldscreen.providers import (
    Message,
    ModelSpecError,
    ProviderError,
    ProviderNotInstalledError,
    ProviderResponseError,
    get_provider,
    parse_model_spec,
)
from coldscreen.providers.anthropic_p import AnthropicProvider, extract_text
from coldscreen.providers.anthropic_p import build_request_kwargs as anthropic_kwargs
from coldscreen.providers.ollama_p import OllamaProvider
from coldscreen.providers.openai_p import OpenAIProvider
from coldscreen.providers.openai_p import build_request_kwargs as openai_kwargs

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"type": "string"}},
}

MESSAGES = [
    Message(role="user", content="the casefile document"),
    Message(role="assistant", content="previous attempt"),
    Message(role="user", content="correction request"),
]


# -- parse_model_spec --------------------------------------------------------


def test_spec_splits_on_the_first_colon_only() -> None:
    assert parse_model_spec("ollama:qwen2.5:7b") == ("ollama", "qwen2.5:7b")


def test_spec_plain_provider_and_model() -> None:
    assert parse_model_spec("anthropic:claude-opus-5") == ("anthropic", "claude-opus-5")
    assert parse_model_spec("openai:gpt-5.6-sol") == ("openai", "gpt-5.6-sol")


def test_spec_provider_only_defaults() -> None:
    assert parse_model_spec("anthropic") == ("anthropic", "claude-opus-5")
    assert parse_model_spec("openai") == ("openai", "gpt-5.6-sol")


def test_spec_ollama_requires_an_explicit_model() -> None:
    with pytest.raises(ModelSpecError, match="explicit model"):
        parse_model_spec("ollama")


def test_spec_unknown_provider_is_an_error() -> None:
    with pytest.raises(ModelSpecError, match="unknown provider"):
        parse_model_spec("sorcery:crystal-ball")


def test_spec_empty_is_an_error() -> None:
    with pytest.raises(ModelSpecError):
        parse_model_spec("   ")


def test_spec_is_case_insensitive_on_provider() -> None:
    assert parse_model_spec("Anthropic:claude-opus-5")[0] == "anthropic"


# -- anthropic shape ---------------------------------------------------------


def test_anthropic_request_shape_with_schema() -> None:
    kwargs = anthropic_kwargs("claude-opus-5", "SYSTEM", MESSAGES, SCHEMA, max_tokens=16000)
    assert kwargs == {
        "model": "claude-opus-5",
        "max_tokens": 16000,
        "system": "SYSTEM",
        "messages": [
            {"role": "user", "content": "the casefile document"},
            {"role": "assistant", "content": "previous attempt"},
            {"role": "user", "content": "correction request"},
        ],
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
    }


def test_anthropic_request_shape_without_schema_has_no_output_config() -> None:
    kwargs = anthropic_kwargs("claude-opus-5", "SYSTEM", MESSAGES[:1], None, max_tokens=16000)
    assert "output_config" not in kwargs


@dataclass
class _Block:
    type: str
    text: str = ""


@dataclass
class _AnthropicResponse:
    content: list[_Block]
    stop_reason: str = "end_turn"


class _FakeMessages:
    def __init__(self, response: _AnthropicResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _AnthropicResponse:
        self.calls.append(kwargs)
        return self.response


@dataclass
class _FakeAnthropicClient:
    messages: _FakeMessages = field(
        default_factory=lambda: _FakeMessages(
            _AnthropicResponse(content=[_Block(type="text", text='{"ok": "yes"}')])
        )
    )


def test_anthropic_provider_sends_the_exact_payload_and_reads_text() -> None:
    client = _FakeAnthropicClient()
    provider = AnthropicProvider(model="claude-opus-5", client_factory=lambda: client)
    out = provider.complete("SYSTEM", MESSAGES[:1], json_schema=SCHEMA)
    assert out == '{"ok": "yes"}'
    sent = client.messages.calls[0]
    assert sent["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    assert sent["system"] == "SYSTEM"
    assert sent["max_tokens"] == 16000


def test_anthropic_text_extraction_filters_non_text_blocks() -> None:
    response = _AnthropicResponse(
        content=[
            _Block(type="thinking", text="never surfaced"),
            _Block(type="text", text="part one "),
            _Block(type="text", text="part two"),
        ]
    )
    assert extract_text(response) == "part one part two"


def test_anthropic_refusal_stop_reason_raises() -> None:
    response = _AnthropicResponse(content=[], stop_reason="refusal")
    with pytest.raises(ProviderResponseError, match="refusal"):
        extract_text(response)


def test_anthropic_empty_content_raises() -> None:
    with pytest.raises(ProviderResponseError, match="no text"):
        extract_text(_AnthropicResponse(content=[_Block(type="tool_use")]))


def test_anthropic_missing_key_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider(model="claude-opus-5")


def test_anthropic_missing_sdk_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(ProviderNotInstalledError, match=r"coldscreen\[anthropic\]"):
        AnthropicProvider(model="claude-opus-5")


# -- openai shape ------------------------------------------------------------


def test_openai_request_shape_with_schema() -> None:
    kwargs = openai_kwargs("gpt-5.6-sol", "SYSTEM", MESSAGES, SCHEMA)
    assert kwargs == {
        "model": "gpt-5.6-sol",
        "instructions": "SYSTEM",
        "input": [
            {"role": "user", "content": "the casefile document"},
            {"role": "assistant", "content": "previous attempt"},
            {"role": "user", "content": "correction request"},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "coldscreen_synthesis",
                "schema": SCHEMA,
                "strict": True,
            }
        },
    }


def test_openai_request_shape_without_schema_has_no_text() -> None:
    kwargs = openai_kwargs("gpt-5.6-sol", "SYSTEM", MESSAGES[:1], None)
    assert "text" not in kwargs


@dataclass
class _OpenAIResponse:
    output_text: str


class _FakeResponses:
    def __init__(self, response: _OpenAIResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _OpenAIResponse:
        self.calls.append(kwargs)
        return self.response


@dataclass
class _FakeOpenAIClient:
    responses: _FakeResponses = field(
        default_factory=lambda: _FakeResponses(_OpenAIResponse(output_text='{"ok": "yes"}'))
    )


def test_openai_provider_sends_text_format_and_reads_output_text() -> None:
    client = _FakeOpenAIClient()
    provider = OpenAIProvider(model="gpt-5.6-sol", client_factory=lambda: client)
    out = provider.complete("SYSTEM", MESSAGES[:1], json_schema=SCHEMA)
    assert out == '{"ok": "yes"}'
    sent = client.responses.calls[0]
    assert sent["text"]["format"]["type"] == "json_schema"
    assert sent["text"]["format"]["strict"] is True
    assert sent["text"]["format"]["name"] == "coldscreen_synthesis"
    assert sent["instructions"] == "SYSTEM"


def test_openai_empty_output_raises() -> None:
    client = _FakeOpenAIClient(responses=_FakeResponses(_OpenAIResponse(output_text="")))
    provider = OpenAIProvider(model="gpt-5.6-sol", client_factory=lambda: client)
    with pytest.raises(ProviderResponseError, match="no output text"):
        provider.complete("SYSTEM", MESSAGES[:1])


def test_openai_missing_key_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
        OpenAIProvider(model="gpt-5.6-sol")


def test_openai_missing_sdk_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "openai", None)
    with pytest.raises(ProviderNotInstalledError, match=r"coldscreen\[openai\]"):
        OpenAIProvider(model="gpt-5.6-sol")


# -- ollama over respx -------------------------------------------------------


def test_ollama_sends_bare_schema_and_temperature_zero(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post("http://localhost:11434/api/chat").respond(
        200,
        json={"message": {"role": "assistant", "content": '{"ok": "yes"}'}, "done": True},
    )
    provider = OllamaProvider(model="qwen2.5:7b")
    out = provider.complete("SYSTEM", MESSAGES[:1], json_schema=SCHEMA)
    provider.close()
    assert out == '{"ok": "yes"}'
    sent = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert sent["model"] == "qwen2.5:7b"
    assert sent["stream"] is False
    assert sent["format"] == SCHEMA  # bare schema, no envelope
    # Temperature 0 for schema reliability; num_ctx explicit because the
    # Ollama default (4096) silently truncates the synthesis input.
    assert sent["options"] == {"temperature": 0, "num_ctx": 16384}
    # The system prompt travels as the first chat message.
    assert sent["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert sent["messages"][1] == {"role": "user", "content": "the casefile document"}


def test_ollama_num_ctx_is_configurable_through_the_constructor(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post("http://localhost:11434/api/chat").respond(
        200, json={"message": {"role": "assistant", "content": "ok"}, "done": True}
    )
    provider = OllamaProvider(model="qwen2.5:7b", num_ctx=8192)
    provider.complete("SYSTEM", MESSAGES[:1], json_schema=SCHEMA)
    provider.close()
    sent = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert sent["options"] == {"temperature": 0, "num_ctx": 8192}


@pytest.mark.parametrize("think", [True, False])
def test_ollama_sends_the_configured_think_value(think: bool, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post("http://localhost:11434/api/chat").respond(
        200, json={"message": {"role": "assistant", "content": "ok"}, "done": True}
    )
    provider = OllamaProvider(model="qwen3:8b", think=think)
    provider.complete("SYSTEM", MESSAGES[:1], json_schema=SCHEMA)
    provider.close()
    sent = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert sent["think"] is think


def test_ollama_omits_think_when_unset(respx_mock: respx.MockRouter) -> None:
    """The default must leave the field out of the body entirely: models
    with no thinking support can reject it outright."""
    route = respx_mock.post("http://localhost:11434/api/chat").respond(
        200, json={"message": {"role": "assistant", "content": "ok"}, "done": True}
    )
    provider = OllamaProvider(model="qwen2.5:7b")
    provider.complete("SYSTEM", MESSAGES[:1], json_schema=SCHEMA)
    provider.close()
    sent = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert "think" not in sent


def test_ollama_omits_format_without_schema(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post("http://localhost:11434/api/chat").respond(
        200, json={"message": {"role": "assistant", "content": "plain text"}, "done": True}
    )
    provider = OllamaProvider(model="qwen2.5:7b")
    provider.complete("SYSTEM", MESSAGES[:1])
    provider.close()
    sent = json.loads(route.calls.last.request.content.decode("utf-8"))
    assert "format" not in sent


def test_ollama_404_suggests_pulling_the_model(respx_mock: respx.MockRouter) -> None:
    respx_mock.post("http://localhost:11434/api/chat").respond(404)
    provider = OllamaProvider(model="missing-model:1b")
    with pytest.raises(ProviderError, match="ollama pull missing-model:1b"):
        provider.complete("SYSTEM", MESSAGES[:1])
    provider.close()


def test_ollama_missing_content_raises(respx_mock: respx.MockRouter) -> None:
    respx_mock.post("http://localhost:11434/api/chat").respond(200, json={"done": True})
    provider = OllamaProvider(model="qwen2.5:7b")
    with pytest.raises(ProviderResponseError, match="no message content"):
        provider.complete("SYSTEM", MESSAGES[:1])
    provider.close()


def test_ollama_base_url_override(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post("http://tin-shed.local:11434/api/chat").respond(
        200, json={"message": {"role": "assistant", "content": "ok"}, "done": True}
    )
    provider = OllamaProvider(model="qwen2.5:7b", base_url="http://tin-shed.local:11434/")
    provider.complete("SYSTEM", MESSAGES[:1])
    provider.close()
    assert route.call_count == 1


# -- factory -----------------------------------------------------------------


def test_get_provider_builds_ollama_with_base_url(respx_mock: respx.MockRouter) -> None:
    provider = get_provider("ollama", "qwen2.5:7b", ollama_base_url="http://localhost:11434")
    assert isinstance(provider, OllamaProvider)
    assert provider.num_ctx == 16384  # the documented default
    assert provider.think is None  # unset, so the field is never sent
    provider.close()


def test_get_provider_passes_the_configured_num_ctx(respx_mock: respx.MockRouter) -> None:
    provider = get_provider(
        "ollama",
        "qwen2.5:7b",
        ollama_base_url="http://localhost:11434",
        ollama_num_ctx=4096,
    )
    assert isinstance(provider, OllamaProvider)
    assert provider.num_ctx == 4096
    provider.close()


def test_get_provider_passes_the_configured_think(respx_mock: respx.MockRouter) -> None:
    provider = get_provider(
        "ollama",
        "qwen3:8b",
        ollama_base_url="http://localhost:11434",
        ollama_think=False,
    )
    assert isinstance(provider, OllamaProvider)
    assert provider.think is False
    provider.close()


def test_get_provider_unknown_name_raises() -> None:
    with pytest.raises(ModelSpecError):
        get_provider("sorcery", "crystal-ball")


# -- the ollama helper scripts: settings wiring and the think suffix ---------

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_anchor_check() -> ModuleType:
    return load_script("anchor_check")


class _RecordingProvider:
    """Captures the constructor kwargs, then refuses to talk to a daemon."""

    captured: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        type(self).captured = dict(kwargs)

    def complete(self, *args: Any, **kwargs: Any) -> str:
        raise ProviderError("no daemon in tests")

    def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("qwen2.5:7b", ("qwen2.5:7b", None)),  # a plain name keeps its own colon
        ("gemma4:26b:think=false", ("gemma4:26b", False)),
        ("qwen3:8b:think=true", ("qwen3:8b", True)),
        ("qwen3:8b:think=FALSE", ("qwen3:8b", False)),
        ("plainname:think=0", ("plainname", False)),
    ],
)
def test_anchor_check_splits_the_think_suffix(
    argument: str, expected: tuple[str, bool | None]
) -> None:
    module = load_anchor_check()
    assert module.parse_model_argument(argument, None) == expected


def test_anchor_check_suffix_overrides_the_configured_default() -> None:
    """Mixed pairs are the whole point: one model may reject the field that
    the other one needs."""
    module = load_anchor_check()
    assert module.parse_model_argument("qwen2.5-coder:7b", False) == ("qwen2.5-coder:7b", False)
    assert module.parse_model_argument("qwen2.5-coder:7b:think=true", False) == (
        "qwen2.5-coder:7b",
        True,
    )


def test_anchor_check_rejects_a_bad_suffix_and_an_empty_model() -> None:
    module = load_anchor_check()
    with pytest.raises(ValueError, match="expected true or false"):
        module.parse_model_argument("qwen3:8b:think=maybe", None)
    with pytest.raises(ValueError, match="no model name"):
        module.parse_model_argument(":think=false", None)


def test_anchor_check_builds_the_provider_from_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-model think value wins over the configured default; the rest
    of the Ollama configuration comes from the settings."""
    module = load_anchor_check()
    monkeypatch.setattr(module, "OllamaProvider", _RecordingProvider)
    settings = Settings(ollama_num_ctx=4096, ollama_timeout_seconds=12.0, ollama_think=False)
    with pytest.raises(ProviderError):
        module.run_one("qwen3:8b", True, settings)
    assert _RecordingProvider.captured["model"] == "qwen3:8b"
    assert _RecordingProvider.captured["num_ctx"] == 4096
    assert _RecordingProvider.captured["timeout_seconds"] == 12.0
    assert _RecordingProvider.captured["think"] is True


def test_ollama_smoke_builds_the_provider_from_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The smoke script has no per-model override: the settings are it."""
    module = load_script("ollama_smoke")
    monkeypatch.setattr(module, "OllamaProvider", _RecordingProvider)
    settings = Settings(ollama_num_ctx=4096, ollama_think=False)
    assert module.smoke("qwen3:8b", settings) == 1  # the provider refuses, reported not raised
    assert _RecordingProvider.captured["model"] == "qwen3:8b"
    assert _RecordingProvider.captured["num_ctx"] == 4096
    assert _RecordingProvider.captured["think"] is False
