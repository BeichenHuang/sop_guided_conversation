from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from apps.insurance_claims.api import create_app
from apps.insurance_claims.config import DEFAULT_FIXTURES_DIR, ConfigError, Settings
from tests.fakes import ScriptedAdapter


def test_defaults_match_the_design():
    settings = Settings.from_env({})
    assert settings.ai_provider == "fake"
    assert settings.email_mode == "mock"
    assert settings.demo_as_of_date == date(2026, 3, 10)
    assert settings.consent_scenario == "default"
    assert settings.cookie_secure is False
    assert settings.fixtures_dir == DEFAULT_FIXTURES_DIR
    assert settings.demo_portal_url == "https://portal.example.com"
    assert settings.model_configured is False


def test_values_are_read_from_the_environment():
    settings = Settings.from_env(
        {
            "AI_PROVIDER": " FAKE ",
            "AI_API_KEY": "sk-test",
            "AI_MODEL": "some-model",
            "DEMO_AS_OF_DATE": "2026-03-05",
            "CONSENT_SCENARIO": "timeout",
            "COOKIE_SECURE": "true",
            "FIXTURES_DIR": "/tmp/fixtures",
            "DEMO_PORTAL_URL": "https://claims.example.org/",
        }
    )
    assert settings.ai_provider == "fake"
    assert settings.ai_api_key == "sk-test"
    assert settings.ai_model == "some-model"
    assert settings.demo_as_of_date == date(2026, 3, 5)
    assert settings.consent_scenario == "timeout"
    assert settings.cookie_secure is True
    assert settings.fixtures_dir == Path("/tmp/fixtures")
    assert settings.demo_portal_url == "https://claims.example.org"
    # A key alone does not make the fake adapter a real model.
    assert settings.model_configured is False


@pytest.mark.parametrize(
    "env",
    [
        {"AI_PROVIDER": "unknown-provider"},
        {"DEMO_AS_OF_DATE": "03/10/2026"},
        {"EMAIL_MODE": "smtp"},
        {"COOKIE_SECURE": "maybe"},
        {"DEMO_PORTAL_URL": "portal.example.com"},
    ],
)
def test_invalid_values_raise_a_clear_error(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_an_api_key_alone_selects_openai_with_the_default_model():
    settings = Settings.from_env({"AI_API_KEY": "sk-test"})
    assert settings.ai_provider == "openai"
    assert settings.ai_model == "gpt-6-luna"
    assert settings.ai_reasoning_effort == "low"
    assert settings.model_configured is True


def test_openai_without_a_key_is_a_configuration_error():
    with pytest.raises(ConfigError, match="AI_API_KEY"):
        Settings.from_env({"AI_PROVIDER": "openai"})


@pytest.mark.parametrize(("raw", "expected"), [("", None), ("NONE", "none"), ("high", "high")])
def test_reasoning_effort_can_be_changed_or_disabled(raw, expected):
    settings = Settings.from_env({"AI_API_KEY": "sk-test", "AI_REASONING_EFFORT": raw})
    assert settings.ai_reasoning_effort == expected


def test_an_unknown_reasoning_effort_is_rejected():
    with pytest.raises(ConfigError, match="AI_REASONING_EFFORT"):
        Settings.from_env({"AI_API_KEY": "sk-test", "AI_REASONING_EFFORT": "extreme"})


def test_api_key_is_not_in_repr():
    settings = Settings.from_env({"AI_API_KEY": "sk-super-secret"})
    assert "sk-super-secret" not in repr(settings)


def test_app_rejects_an_unknown_default_consent_scenario():
    with pytest.raises(ConfigError, match="CONSENT_SCENARIO"):
        create_app(Settings(consent_scenario="nope"), adapter=ScriptedAdapter())
