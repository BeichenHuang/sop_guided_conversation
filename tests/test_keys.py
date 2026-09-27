"""API keys entered in the SOP console: checked, kept per browser, used for turns, never shown."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx2
import openai
import pytest
from fastapi.testclient import TestClient

from apps.insurance_claims.api import KEY_COOKIE, create_app
from apps.insurance_claims.config import FixedClock
from apps.insurance_claims.keys import KeyStore
from apps.insurance_claims.llm.adapter import LLMAuthError, LLMError
from apps.insurance_claims.llm.openai_adapter import OpenAIAdapter
from tests.fakes import TODAY, ScriptedAdapter, make_settings

GOOD_KEY = "sk-test-good-key-000000001234"
BAD_KEY = "sk-test-rejected-key-00000000"
NO_MODEL_KEY = "sk-test-no-model-access-0000"


class KeyedAdapter(ScriptedAdapter):
    """Stands in for the real model a visitor's key unlocks."""

    provider = "openai"
    model = "gpt-test"
    is_real_model = True


@pytest.fixture
def made():
    return []


@pytest.fixture
def client(made):
    async def key_adapter(provider: str, api_key: str, model: str | None):
        if api_key == BAD_KEY:
            raise LLMAuthError("OpenAI rejected the API key (HTTP 401)")
        if api_key == NO_MODEL_KEY:
            raise LLMError("this key can't use the model gpt-test")
        adapter = KeyedAdapter()
        adapter.provider, adapter.model = provider, model or "gpt-test"
        made.append(adapter)
        return adapter

    app = create_app(
        make_settings(), adapter=ScriptedAdapter(), clock=FixedClock(TODAY), key_adapter=key_adapter
    )
    with TestClient(app) as test_client:
        yield test_client


def put_key(client, key):
    return client.put("/api/model/key", json={"api_key": key})


def shown(data, *fields):
    return {field: data[field] for field in fields}


def test_without_a_key_the_stand_in_replies_and_the_page_says_so(client):
    data = client.get("/api/model").json()
    assert shown(data, "source", "real_model", "provider", "model", "key_hint") == {
        "source": "none",
        "real_model": False,
        "provider": "openai",
        "model": "gpt-6-luna",
        "key_hint": None,
    }
    assert [(p["id"], p["default_model"]) for p in data["providers"]] == [
        ("openai", "gpt-6-luna"),
        ("anthropic", "claude-opus-5"),
    ]


def test_the_page_suggests_models_with_the_default_first(client):
    providers = {p["id"]: p for p in client.get("/api/model").json()["providers"]}
    assert providers["anthropic"]["models"] == ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]
    assert providers["openai"]["models"] == ["gpt-6-luna", "gpt-6-sol"]
    for provider in providers.values():
        assert provider["models"][0] == provider["default_model"]


def test_a_key_is_checked_kept_for_this_browser_and_used_for_turns(client, made):
    response = put_key(client, f"  {GOOD_KEY}  ")
    assert response.status_code == 200
    assert shown(
        response.json(), "source", "real_model", "provider", "provider_label", "model", "key_hint"
    ) == {
        "source": "yours",
        "real_model": True,
        "provider": "openai",
        "provider_label": "OpenAI",
        "model": "gpt-test",
        "key_hint": "…1234",
    }
    cookie = response.headers["set-cookie"].lower()
    assert f"{KEY_COOKIE}=" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert GOOD_KEY not in response.headers["set-cookie"]

    client.post("/api/sessions")
    client.post("/api/session/messages", json={"message": "hello"})
    [adapter] = made
    assert len(adapter.understand_calls) == 1
    # A new conversation keeps the key.
    client.post("/api/sessions")
    client.post("/api/session/messages", json={"message": "hello again"})
    assert len(adapter.understand_calls) == 2


def test_a_key_can_be_for_another_provider_and_model(client, made):
    response = client.put(
        "/api/model/key", json={"provider": "anthropic", "model": "claude-sonnet-5", "api_key": GOOD_KEY}
    )
    assert shown(response.json(), "source", "provider", "provider_label", "model") == {
        "source": "yours",
        "provider": "anthropic",
        "provider_label": "Anthropic Claude",
        "model": "claude-sonnet-5",
    }
    [adapter] = made
    assert (adapter.provider, adapter.model) == ("anthropic", "claude-sonnet-5")


@pytest.mark.parametrize(
    "body",
    [
        {"provider": "someone-else", "api_key": GOOD_KEY},
        {"provider": "anthropic", "model": "../../etc/passwd", "api_key": GOOD_KEY},
        {"provider": "anthropic", "model": "claude opus", "api_key": GOOD_KEY},
    ],
)
def test_unknown_providers_and_odd_model_names_are_refused(client, made, body):
    response = client.put("/api/model/key", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert made == []


def test_the_key_never_comes_back(client):
    put_key(client, GOOD_KEY)
    client.post("/api/sessions")
    client.post("/api/session/messages", json={"message": "hello"})
    for path in ("/api/model", "/api/session", "/api/session/console", "/api/session/export", "/healthz"):
        assert GOOD_KEY not in client.get(path).text, path


def test_other_browsers_keep_their_own_model(client):
    put_key(client, GOOD_KEY)
    with TestClient(client.app) as other:
        assert other.get("/api/model").json()["source"] == "none"


@pytest.mark.parametrize(
    ("key", "code"),
    [(BAD_KEY, "invalid_key"), (NO_MODEL_KEY, "key_check_failed"), ("short", "invalid_request")],
)
def test_a_key_that_cannot_be_used_is_refused_and_not_kept(client, key, code):
    response = put_key(client, key)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    assert key not in response.text
    assert client.get("/api/model").json()["source"] == "none"


def test_a_key_can_be_replaced_and_removed(client, made):
    put_key(client, GOOD_KEY)
    put_key(client, GOOD_KEY.replace("1234", "5678"))
    assert client.get("/api/model").json()["key_hint"] == "…5678"
    removed = client.delete("/api/model/key")
    assert removed.json()["source"] == "none"
    assert client.get("/api/model").json()["source"] == "none"


def test_a_key_rejected_during_a_turn_says_so_and_commits_nothing(client, made):
    put_key(client, GOOD_KEY)
    made[0].understandings.append(LLMAuthError("OpenAI rejected the API key (HTTP 401)"))
    client.post("/api/sessions")
    response = client.post("/api/session/messages", json={"message": "hello"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invalid_key"
    assert len(made[0].understand_calls) == 1  # not retried with the same key
    assert len(client.get("/api/session").json()["messages"]) == 1


def test_keys_are_forgotten_after_idle_hours():
    now = [datetime(2026, 3, 10, 9, 0, tzinfo=UTC)]
    store = KeyStore(idle_ttl=timedelta(hours=12), now=lambda: now[0])
    key_id = store.add(KeyedAdapter(), "…1234")
    now[0] += timedelta(hours=11)
    assert store.get(key_id) is not None
    now[0] += timedelta(hours=12, seconds=1)
    assert store.get(key_id) is None
    assert len(store) == 0


def _status_error(cls, status):
    request = httpx2.Request("GET", "https://api.openai.com/v1/models/gpt-test")
    response = httpx2.Response(status, request=request)
    return cls(message=f"bad key sk-leaked-{status}", response=response, body=None)


class StubModels:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    async def retrieve(self, model):
        self.calls.append(model)
        if self.error:
            raise self.error


def adapter_with(models):
    client = SimpleNamespace(models=models)
    return OpenAIAdapter(api_key="unused", model="gpt-test", client=client)


def test_the_openai_check_asks_for_the_model_and_maps_errors():
    models = StubModels()
    asyncio.run(adapter_with(models).check())
    assert models.calls == ["gpt-test"]

    with pytest.raises(LLMAuthError) as rejected:
        asyncio.run(adapter_with(StubModels(_status_error(openai.AuthenticationError, 401))).check())
    assert "sk-leaked" not in str(rejected.value)

    with pytest.raises(LLMError, match="can't use the model gpt-test") as missing:
        asyncio.run(adapter_with(StubModels(_status_error(openai.NotFoundError, 404))).check())
    assert not isinstance(missing.value, LLMAuthError)
