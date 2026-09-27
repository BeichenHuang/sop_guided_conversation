"""Provider-neutral model interface.

Each turn makes at most two model calls: ``understand_turn`` proposes what the
user said, and ``realize_reply`` phrases a reply plan that the controller has
already decided. Real providers must use their native schema-constrained output
rather than forced tool choice or assistant prefill.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from ..config import ConfigError, Settings
from ..domain import ChatMessage, IdentityField, Phase, ReplyPlan, ResponseDraft, TurnUnderstanding
from .providers import PROVIDERS


class LLMError(RuntimeError):
    """A model call failed or returned output that cannot be used."""


class LLMAuthError(LLMError):
    """The provider rejected the API key; retrying with the same key won't help."""


# Called after each model call with its input, output and reasoning token counts (the evaluation's meter).
UsageHook = Callable[[int, int, int], None]


@dataclass(frozen=True)
class UnderstandContext:
    message: str
    phase: Phase
    recent_messages: tuple[ChatMessage, ...]
    provided_identity_fields: tuple[IdentityField, ...]
    pending_question: str | None = None
    caller_role: str = "unknown"


@dataclass(frozen=True)
class RealizeContext:
    plan: ReplyPlan
    facts: Mapping[str, str]
    recent_messages: tuple[ChatMessage, ...]
    violations: tuple[str, ...] = ()
    # The plan as texts for the model to phrase (planner.brief).
    brief: Mapping[str, object] = field(default_factory=dict)


class LLMAdapter(Protocol):
    provider: str
    model: str | None
    is_real_model: bool

    async def understand_turn(self, context: UnderstandContext) -> TurnUnderstanding: ...

    async def realize_reply(self, context: RealizeContext) -> ResponseDraft: ...


def build_adapter(
    provider_id: str,
    *,
    api_key: str,
    model: str,
    base_url: str | None = None,
    reasoning_effort: str | None = None,
    on_usage: UsageHook | None = None,
) -> LLMAdapter:
    """The adapter for one provider (llm/providers.py) and key."""
    provider = PROVIDERS[provider_id]
    if provider.sdk == "anthropic":
        from .anthropic_adapter import AnthropicAdapter

        return AnthropicAdapter(
            api_key=api_key, model=model, reasoning_effort=reasoning_effort, on_usage=on_usage
        )
    from .openai_adapter import OpenAIAdapter

    return OpenAIAdapter(
        api_key=api_key,
        model=model,
        base_url=base_url,
        reasoning_effort=reasoning_effort,
        on_usage=on_usage,
    )


def key_model_name(settings: Settings, provider_id: str) -> str:
    """The model a key entered in the console is used with, unless the page names another."""
    if provider_id == settings.ai_provider and settings.ai_model:
        return settings.ai_model
    return PROVIDERS[provider_id].default_model


async def adapter_for_key(
    settings: Settings, provider_id: str, api_key: str, model: str | None = None
) -> LLMAdapter:
    """An adapter for a key entered in the console, checked with the provider before it is kept."""
    adapter = build_adapter(
        provider_id,
        api_key=api_key,
        model=model or key_model_name(settings, provider_id),
        # AI_BASE_URL is the server's own endpoint choice; it applies to the server's provider only.
        base_url=settings.ai_base_url if provider_id == settings.ai_provider else None,
        reasoning_effort=settings.ai_reasoning_effort,
    )
    await adapter.check()
    return adapter


def create_adapter(settings: Settings) -> LLMAdapter:
    if settings.ai_provider == "fake":
        from .fake import FakeAdapter

        return FakeAdapter()
    if settings.ai_provider in PROVIDERS:
        return build_adapter(
            settings.ai_provider,
            api_key=settings.ai_api_key or "",
            model=settings.ai_model or PROVIDERS[settings.ai_provider].default_model,
            base_url=settings.ai_base_url,
            reasoning_effort=settings.ai_reasoning_effort,
        )
    raise ConfigError(f"No adapter is implemented for AI_PROVIDER={settings.ai_provider!r}.")
