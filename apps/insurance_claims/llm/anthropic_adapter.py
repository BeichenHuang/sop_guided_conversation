"""The model adapter for Anthropic Claude, using structured outputs."""

from __future__ import annotations

from typing import TypeVar

import anthropic
from pydantic import BaseModel

from ..domain import ResponseDraft, TurnUnderstanding
from . import prompts
from .adapter import LLMAuthError, LLMError, RealizeContext, UnderstandContext, UsageHook

T = TypeVar("T", bound=BaseModel)

REQUEST_TIMEOUT_SECONDS = 25.0
# Thinking counts toward max_tokens; the limit only guards against runaway output.
MAX_TOKENS = 16000
# Effort is rejected by these models; the others take it inside output_config.
_NO_EFFORT = frozenset({"claude-haiku-4-5", "claude-sonnet-4-5"})
# A request these models decline for safety reasons is re-run on Anthropic's recommended fallback.
_FALLBACK_MODELS = frozenset({"claude-opus-5", "claude-fable-5-1"})
_EFFORTS = {"none": "low", "low": "low", "medium": "medium", "high": "high"}


def _error(exc: anthropic.AnthropicError) -> LLMError:
    """Only the status and error type: provider messages can echo request details."""
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return LLMAuthError(f"the provider rejected the API key (HTTP {exc.status_code})")
    if isinstance(exc, anthropic.APIStatusError):
        return LLMError(f"the request failed with HTTP {exc.status_code} ({type(exc).__name__})")
    return LLMError(f"the request failed ({type(exc).__name__})")


class AnthropicAdapter:
    provider = "anthropic"
    is_real_model = True

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        reasoning_effort: str | None = "low",
        client: anthropic.AsyncAnthropic | None = None,
        on_usage: UsageHook | None = None,
    ) -> None:
        self.model = model
        self._effort = None if model in _NO_EFFORT else _EFFORTS.get(reasoning_effort or "")
        self._on_usage = on_usage
        # The key is always passed explicitly, so the SDK never falls back to other credentials.
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, max_retries=1, timeout=REQUEST_TIMEOUT_SECONDS
        )

    async def check(self) -> None:
        """Confirm the key works and can use the model, with a request that costs nothing."""
        try:
            await self._client.models.retrieve(self.model)
        except anthropic.NotFoundError:
            raise LLMError(f"this key can't use the model {self.model}") from None
        except anthropic.AnthropicError as exc:
            raise _error(exc) from None

    async def understand_turn(self, context: UnderstandContext) -> TurnUnderstanding:
        out = await self._parse(prompts.understanding_messages(context), prompts.UnderstandingOut)
        return prompts.to_understanding(out)

    async def realize_reply(self, context: RealizeContext) -> ResponseDraft:
        out = await self._parse(prompts.realize_messages(context), prompts.ReplyOut)
        return ResponseDraft(text=out.text, used_fact_ids=out.used_fact_ids)

    async def _parse(self, messages: list[dict[str, str]], schema: type[T]) -> T:
        system, *conversation = messages
        options: dict[str, object] = {}
        if self._effort:
            options["output_config"] = {"effort": self._effort}
        if self.model in _FALLBACK_MODELS:
            options["betas"] = ["server-side-fallback-2026-07-01"]
            options["fallbacks"] = "default"
        try:
            response = await self._client.beta.messages.parse(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system["content"],
                messages=conversation,
                output_format=schema,
                # The system prompts are the same every turn; cache them when they are long enough.
                cache_control={"type": "ephemeral"},
                **options,
            )
        except anthropic.AnthropicError as exc:
            raise _error(exc) from None
        self._report(response.usage)
        # The output may not match the schema unless the model finished normally.
        if response.stop_reason == "refusal":
            raise LLMError("the model declined the request")
        if response.stop_reason == "max_tokens":
            raise LLMError("the output was cut off")
        if response.parsed_output is None:
            raise LLMError("the model returned no parsable output")
        return response.parsed_output

    def _report(self, usage: object) -> None:
        if self._on_usage is None or usage is None:
            return
        cached = (getattr(usage, "cache_creation_input_tokens", 0) or 0) + (
            getattr(usage, "cache_read_input_tokens", 0) or 0
        )
        self._on_usage((getattr(usage, "input_tokens", 0) or 0) + cached, usage.output_tokens or 0, 0)
