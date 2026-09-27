"""The model adapter for OpenAI, using Structured Outputs. AI_BASE_URL can point it at an
OpenAI-compatible endpoint instead."""

from __future__ import annotations

import logging
from typing import TypeVar

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel

from ..domain import ResponseDraft, TurnUnderstanding
from . import prompts
from .adapter import LLMAuthError, LLMError, RealizeContext, UnderstandContext, UsageHook

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

REQUEST_TIMEOUT_SECONDS = 25.0
# Reasoning tokens count toward these limits, so leave headroom above the visible output.
UNDERSTAND_MAX_TOKENS = 4000
REPLY_MAX_TOKENS = 2500


def _error(exc: openai.OpenAIError) -> LLMError:
    """Only the status and error type: provider messages can echo request details, even the key."""
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return LLMAuthError(f"the provider rejected the API key (HTTP {exc.status_code})")
    if isinstance(exc, openai.APIStatusError):
        return LLMError(f"the request failed with HTTP {exc.status_code} ({type(exc).__name__})")
    return LLMError(f"the request failed ({type(exc).__name__})")


class OpenAIAdapter:
    provider = "openai"
    is_real_model = True

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        reasoning_effort: str | None = "low",
        client: AsyncOpenAI | None = None,
        on_usage: UsageHook | None = None,
    ) -> None:
        self.model = model
        self._reasoning_effort = reasoning_effort
        self._on_usage = on_usage
        # One quick SDK retry for connection hiccups; the engine owns the retry policy beyond that.
        self._client = client or AsyncOpenAI(
            api_key=api_key, base_url=base_url, max_retries=1, timeout=REQUEST_TIMEOUT_SECONDS
        )

    async def check(self) -> None:
        """Confirm the key works and can use the model, with a request that costs nothing."""
        try:
            await self._client.models.retrieve(self.model)
        except openai.NotFoundError:
            raise LLMError(f"this key can't use the model {self.model}") from None
        except openai.OpenAIError as exc:
            raise _error(exc) from None

    async def understand_turn(self, context: UnderstandContext) -> TurnUnderstanding:
        out = await self._parse(
            prompts.understanding_messages(context), prompts.UnderstandingOut, UNDERSTAND_MAX_TOKENS
        )
        return prompts.to_understanding(out)

    async def realize_reply(self, context: RealizeContext) -> ResponseDraft:
        out = await self._parse(prompts.realize_messages(context), prompts.ReplyOut, REPLY_MAX_TOKENS)
        return ResponseDraft(text=out.text, used_fact_ids=out.used_fact_ids)

    async def _parse(self, messages: list[dict[str, str]], schema: type[T], max_tokens: int) -> T:
        options = {"reasoning_effort": self._reasoning_effort} if self._reasoning_effort else {}
        try:
            completion = await self._client.chat.completions.parse(
                model=self.model,
                messages=messages,
                response_format=schema,
                max_completion_tokens=max_tokens,
                # Conversations contain personal details; do not keep them on the provider side.
                store=False,
                **options,
            )
        except openai.OpenAIError as exc:
            raise _error(exc) from None
        self._report(getattr(completion, "usage", None))
        message = completion.choices[0].message
        if getattr(message, "refusal", None):
            raise LLMError("the model refused the request")
        if message.parsed is None:
            raise LLMError("the model returned no parsable output")
        return message.parsed

    def _report(self, usage: object) -> None:
        if self._on_usage is None:
            return
        details = getattr(usage, "completion_tokens_details", None)
        self._on_usage(
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0,
            (getattr(details, "reasoning_tokens", None) or 0) if details else 0,
        )
