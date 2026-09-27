"""Model providers: the registry, and the Anthropic adapter."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from apps.insurance_claims.config import ConfigError, Settings
from apps.insurance_claims.domain import Phase
from apps.insurance_claims.llm import prompts
from apps.insurance_claims.llm.adapter import (
    LLMAuthError,
    LLMError,
    UnderstandContext,
    build_adapter,
    create_adapter,
    key_model_name,
)
from apps.insurance_claims.llm.anthropic_adapter import AnthropicAdapter
from apps.insurance_claims.llm.openai_adapter import OpenAIAdapter
from apps.insurance_claims.llm.providers import PROVIDERS

CONTEXT = UnderstandContext(
    message="I'm Margaret Chen, why was my claim denied?",
    phase=Phase.VERIFY_ID,
    recent_messages=(),
    provided_identity_fields=(),
)


def usage_log():
    calls = []
    return calls, lambda i, o, r: calls.append((i, o, r))


# --- the registry and settings -----------------------------------------------------------


def test_the_page_offers_openai_and_claude_each_with_its_own_adapter():
    assert list(PROVIDERS) == ["openai", "anthropic"]
    claude = build_adapter("anthropic", api_key="k", model="claude-opus-5")
    assert isinstance(claude, AnthropicAdapter) and claude.provider == "anthropic"
    gpt = build_adapter("openai", api_key="k", model="gpt-6-luna")
    assert isinstance(gpt, OpenAIAdapter) and gpt.provider == "openai"


def test_the_server_can_use_any_provider_with_its_default_model():
    settings = Settings.from_env({"AI_PROVIDER": "anthropic", "AI_API_KEY": "sk-ant-test"})
    assert settings.ai_model == "claude-opus-5"
    assert create_adapter(settings).provider == "anthropic"
    with pytest.raises(ConfigError, match="AI_PROVIDER=anthropic needs AI_API_KEY"):
        Settings.from_env({"AI_PROVIDER": "anthropic"})
    with pytest.raises(ConfigError, match="AI_PROVIDER"):
        Settings.from_env({"AI_PROVIDER": "someone-else", "AI_API_KEY": "sk-test"})
    # A console key for another provider uses that provider's default, not the server's model.
    assert key_model_name(settings, "openai") == "gpt-6-luna"
    assert key_model_name(settings, "anthropic") == "claude-opus-5"


# --- Anthropic ---------------------------------------------------------------------------


class StubAnthropicMessages:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def claude(messages, model="claude-opus-5", effort="low", on_usage=None, models=None):
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages), models=models)
    return AnthropicAdapter(
        api_key="unused", model=model, reasoning_effort=effort, client=client, on_usage=on_usage
    )


def claude_response(parsed, stop_reason="end_turn"):
    usage = SimpleNamespace(
        input_tokens=100, cache_creation_input_tokens=20, cache_read_input_tokens=5, output_tokens=40
    )
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason, usage=usage)


def understanding():
    return prompts.UnderstandingOut.model_validate(
        {
            "scope": "in_scope",
            "dialog_acts": [],
            "caller_role": "self",
            "representative": None,
            "identity_updates": [],
            "policy_number": None,
            "intent": "denial_question",
            "case_hint_updates": [],
            "questions": [],
            "notes": [],
            "email_reply": "none",
            "emotion": {"label": "neutral", "intensity": "low"},
            "safety_concerns": [],
            "process_question": None,
            "withheld_fields": [],
        }
    )


def test_claude_request_uses_structured_output_effort_caching_and_fallbacks():
    stub = StubAnthropicMessages(claude_response(understanding()))
    calls, hook = usage_log()
    result = asyncio.run(claude(stub, on_usage=hook).understand_turn(CONTEXT))
    assert result.intent.value == "denial_question"
    call = stub.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["output_format"] is prompts.UnderstandingOut
    assert call["output_config"] == {"effort": "low"}
    assert call["cache_control"] == {"type": "ephemeral"}
    assert call["betas"] == ["server-side-fallback-2026-07-01"] and call["fallbacks"] == "default"
    assert call["system"] == prompts.understanding_messages(CONTEXT)[0]["content"]
    assert [m["role"] for m in call["messages"]] == ["user"]
    assert calls == [(125, 40, 0)]


def test_claude_models_without_effort_or_fallbacks_are_not_sent_them():
    stub = StubAnthropicMessages(claude_response(understanding()))
    asyncio.run(claude(stub, model="claude-haiku-4-5").understand_turn(CONTEXT))
    assert "output_config" not in stub.calls[0] and "fallbacks" not in stub.calls[0]


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_claude_output_is_used_only_when_the_model_finished(stop_reason):
    stub = StubAnthropicMessages(claude_response(understanding(), stop_reason=stop_reason))
    with pytest.raises(LLMError):
        asyncio.run(claude(stub).understand_turn(CONTEXT))


def _anthropic_error(cls, status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message="bad key sk-ant-leaked", response=httpx2.Response(status, request=request), body=None)


def test_claude_errors_map_to_adapter_errors_without_their_message():
    stub = StubAnthropicMessages(error=_anthropic_error(anthropic.AuthenticationError, 401))
    with pytest.raises(LLMAuthError) as rejected:
        asyncio.run(claude(stub).understand_turn(CONTEXT))
    assert "sk-ant-leaked" not in str(rejected.value)

    class Models:
        def __init__(self, error=None):
            self.error, self.calls = error, []

        async def retrieve(self, model):
            self.calls.append(model)
            if self.error:
                raise self.error

    models = Models()
    asyncio.run(claude(stub, models=models).check())
    assert models.calls == ["claude-opus-5"]
    missing = Models(_anthropic_error(anthropic.NotFoundError, 404))
    with pytest.raises(LLMError, match="can't use the model"):
        asyncio.run(claude(stub, models=missing).check())
