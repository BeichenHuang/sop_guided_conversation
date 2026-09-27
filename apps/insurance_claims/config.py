"""Environment configuration and the injectable clock."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Protocol

from .llm.providers import PROVIDERS

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_FIXTURES_DIR = PACKAGE_DIR / "fixtures"

# The fixtures are only self-consistent between 2026-03-01 (latest created_at)
# and 2026-03-18 (earliest appeal deadline).
DEFAULT_DEMO_AS_OF_DATE = date(2026, 3, 10)

SUPPORTED_PROVIDERS = frozenset({"fake", *PROVIDERS})
SUPPORTED_EMAIL_MODES = frozenset({"mock"})
REASONING_EFFORTS = frozenset({"none", "low", "medium", "high"})

# The demo has no real member portal. Its links point here; example.com is reserved for examples.
DEFAULT_PORTAL_URL = "https://portal.example.com"

DEFAULT_REASONING_EFFORT = "low"

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


class ConfigError(RuntimeError):
    """The environment configuration is invalid."""


@dataclass(frozen=True)
class Settings:
    ai_provider: str = "fake"
    ai_api_key: str | None = field(default=None, repr=False)
    ai_model: str | None = None
    ai_base_url: str | None = None
    # Passed to reasoning models; empty for models that don't accept it.
    ai_reasoning_effort: str | None = DEFAULT_REASONING_EFFORT
    email_mode: str = "mock"
    demo_as_of_date: date = DEFAULT_DEMO_AS_OF_DATE
    consent_scenario: str = "default"
    # Base of the placeholder member portal that claim upload links point to.
    demo_portal_url: str = DEFAULT_PORTAL_URL
    cookie_secure: bool = False
    fixtures_dir: Path = DEFAULT_FIXTURES_DIR
    # Seconds between simulated consent polls.
    consent_poll_interval: float = 1.0
    # Raise on an invariant violation instead of replacing the reply (used by tests).
    strict_invariants: bool = False

    @property
    def model_configured(self) -> bool:
        """True only when a real provider and its API key are both configured."""
        return self.ai_provider != "fake" and bool(self.ai_api_key)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env

        api_key = _text(env, "AI_API_KEY")
        # A key on its own is enough: it selects OpenAI unless a provider is named.
        provider = (_text(env, "AI_PROVIDER") or ("openai" if api_key else "fake")).lower()
        if provider not in SUPPORTED_PROVIDERS:
            supported = ", ".join(sorted(SUPPORTED_PROVIDERS))
            raise ConfigError(f"AI_PROVIDER={provider!r} is not supported (supported: {supported}).")
        if provider != "fake" and not api_key:
            raise ConfigError(
                f"AI_PROVIDER={provider} needs AI_API_KEY to be set to that provider's API key."
            )
        model = _text(env, "AI_MODEL") or (PROVIDERS[provider].default_model if provider != "fake" else None)
        effort = env.get("AI_REASONING_EFFORT", DEFAULT_REASONING_EFFORT).strip().lower() or None
        if effort is not None and effort not in REASONING_EFFORTS:
            raise ConfigError(
                f"AI_REASONING_EFFORT must be one of {', '.join(sorted(REASONING_EFFORTS))} or empty, "
                f"got {effort!r}."
            )

        email_mode = (_text(env, "EMAIL_MODE") or "mock").lower()
        if email_mode not in SUPPORTED_EMAIL_MODES:
            raise ConfigError(f"EMAIL_MODE={email_mode!r} is not supported (supported: mock).")

        fixtures_dir = _text(env, "FIXTURES_DIR")
        return cls(
            ai_provider=provider,
            ai_api_key=api_key,
            ai_model=model,
            ai_base_url=_text(env, "AI_BASE_URL"),
            ai_reasoning_effort=effort,
            email_mode=email_mode,
            demo_as_of_date=_date(env, "DEMO_AS_OF_DATE", DEFAULT_DEMO_AS_OF_DATE),
            consent_scenario=_text(env, "CONSENT_SCENARIO") or "default",
            demo_portal_url=_url(env, "DEMO_PORTAL_URL", DEFAULT_PORTAL_URL),
            cookie_secure=_bool(env, "COOKIE_SECURE", False),
            fixtures_dir=Path(fixtures_dir) if fixtures_dir else DEFAULT_FIXTURES_DIR,
            consent_poll_interval=_seconds(env, "CONSENT_POLL_INTERVAL", 1.0),
            strict_invariants=_bool(env, "STRICT_INVARIANTS", False),
        )


def _text(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def _date(env: Mapping[str, str], name: str, default: date) -> date:
    value = _text(env, name)
    if value is None:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ConfigError(f"{name} must be an ISO date such as 2026-03-10, got {value!r}.") from None


def _url(env: Mapping[str, str], name: str, default: str) -> str:
    value = _text(env, name)
    if value is None:
        return default
    if not value.startswith(("https://", "http://")) or any(c.isspace() for c in value):
        raise ConfigError(f"{name} must be an http(s) URL, got {value!r}.")
    return value.rstrip("/")


def _seconds(env: Mapping[str, str], name: str, default: float) -> float:
    value = _text(env, name)
    if value is None:
        return default
    try:
        seconds = float(value)
    except ValueError:
        seconds = -1.0
    if not 0 <= seconds <= 10:
        raise ConfigError(f"{name} must be a number of seconds between 0 and 10, got {value!r}.")
    return seconds


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = _text(env, name)
    if value is None:
        return default
    if value.lower() in _TRUE:
        return True
    if value.lower() in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {value!r}.")


class Clock(Protocol):
    def today(self) -> date: ...


class SystemClock:
    def today(self) -> date:
        return date.today()


@dataclass(frozen=True)
class FixedClock:
    value: date

    def today(self) -> date:
        return self.value
