"""The model providers a key can be used with, and how each one is called.

Endpoints are fixed here, never taken from the page: a public demo must not send requests to an
address a visitor typed in. A different OpenAI-compatible endpoint can still be set on the server
with AI_BASE_URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DEFAULT_OPENAI_MODEL = "gpt-6-luna"


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    default_model: str
    # "openai": the OpenAI SDK; "anthropic": the Anthropic SDK.
    sdk: Literal["openai", "anthropic"]
    # Models the console suggests, the default first; each has run with this app. Any other
    # model the key can use may still be typed in.
    models: tuple[str, ...] = ()


PROVIDERS: dict[str, Provider] = {
    "openai": Provider(
        "openai",
        "OpenAI",
        DEFAULT_OPENAI_MODEL,
        sdk="openai",
        models=(DEFAULT_OPENAI_MODEL, "gpt-6-sol"),
    ),
    "anthropic": Provider(
        "anthropic",
        "Anthropic Claude",
        "claude-opus-5",
        sdk="anthropic",
        models=("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"),
    ),
}
