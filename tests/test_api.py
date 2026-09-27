from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from apps.insurance_claims.api import SESSION_COOKIE, create_app
from apps.insurance_claims.config import FixedClock, Settings
from apps.insurance_claims.domain import ResponseDraft, TurnUnderstanding
from apps.insurance_claims.llm.adapter import LLMError
from apps.insurance_claims.store import SESSION_IDLE_TTL, SessionStore
from tests.fakes import TODAY, ScriptedAdapter


def start(client: TestClient, **body) -> dict:
    response = client.post("/api/sessions", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def say(client: TestClient, message: str):
    return client.post("/api/session/messages", json={"message": message})


def test_index_serves_the_ui_with_security_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'id="messages"' in response.text
    assert 'id="console-toggle"' in response.text
    assert 'id="phases"' not in response.text
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert 'id="progress"' in response.text
    for asset in (
        "/static/common.js",
        "/static/app.js",
        "/static/styles.css",
        "/static/fonts/atkinson-hyperlegible-next.woff2",
    ):
        assert client.get(asset).status_code == 200


def test_console_page_is_served_and_may_be_framed_by_the_chat_page_only(client):
    response = client.get("/console")
    assert response.status_code == 200
    assert 'id="turns"' in response.text
    assert 'id="messages"' not in response.text
    assert "frame-ancestors 'self'" in response.headers["content-security-policy"]
    assert client.get("/static/console.js").status_code == 200


@pytest.mark.parametrize(("page", "script"), [("/", "app.js"), ("/console", "console.js")])
def test_every_element_a_script_looks_up_is_on_its_page(client, page, script):
    html = client.get(page).text
    ids = re.findall(r'\$\("([\w-]+)"\)', client.get(f"/static/{script}").text)
    assert ids
    assert [i for i in ids if f'id="{i}"' not in html] == []


def test_the_model_field_suggests_models_but_takes_any_name(client):
    html = client.get("/console").text
    field = html[html.index('id="key-model"') :]
    field = field[: field.index(">")]
    assert 'type="text"' in field and 'list="key-models"' in field
    assert '<datalist id="key-models"></datalist>' in html


def test_console_folds_the_demo_settings_and_has_no_session_tab(client):
    html = client.get("/console").text
    settings = html[html.index('id="demo-settings"') : html.index("</details>")]
    assert 'id="date-mode"' in settings
    assert 'id="consent-scenario"' in settings
    assert "Session" not in html


def test_the_browser_revalidates_pages_and_scripts(client):
    for path in ("/", "/console", "/static/common.js", "/static/app.js", "/static/console.js"):
        assert client.get(path).headers["cache-control"] == "no-cache", path


def test_the_console_scenarios_are_complete_scripts(client):
    """scenarios.json drives the console's scenario player: every step is a message the API accepts."""
    response = client.get("/static/scenarios.json")
    assert response.status_code == 200
    scenarios = response.json()
    assert len({s["id"] for s in scenarios}) == len(scenarios)
    consent_scenarios = client.app.state.fixtures.consent_scenarios
    for scenario in scenarios:
        assert scenario["title"] and scenario["description"]
        assert scenario["consent"] in consent_scenarios
        assert len(scenario["steps"]) >= 3
        for step in scenario["steps"]:
            assert set(step) == {"say", "expect"}
            assert 0 < len(step["say"]) <= 4000 and step["expect"]


def test_healthz_reports_the_model_without_secrets(make_client):
    client = make_client(settings=Settings(ai_api_key="sk-test-secret-value"))
    response = client.get("/healthz")
    data = response.json()
    assert data["status"] == "ok"
    assert data["real_model"] is False
    assert data["model_configured"] is False
    assert "sk-test-secret-value" not in response.text


def test_creating_a_session_sets_a_protected_cookie_and_welcomes(client):
    response = client.post("/api/sessions", json={})
    assert response.status_code == 201
    cookie = response.headers["set-cookie"].lower()
    assert f"{SESSION_COOKIE}=" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "secure" not in cookie

    data = response.json()
    assert data["reply"]["id"] == "a-0"
    assert "verify your identity" in data["reply"]["text"]
    assert "on behalf of the policyholder" in data["reply"]["text"]
    session = data["session"]
    assert [(g["name"], g["state"]) for g in session.pop("gates")] == [
        ("Identity", "waiting"),
        ("Claim", "waiting"),
        ("Email summary", "not_needed"),
    ]
    assert session.pop("memory") == []
    assert session.pop("awaiting") is None
    assert session == {
        "phase": "VERIFY_ID",
        "subflow": None,
        "status": "active",
        "verified": False,
        "identity_fields_provided": 0,
        "identity_fields_required": 3,
        "case_cycle_id": 1,
        "selected_case_id": None,
        "intent": None,
        "email_status": "not_requested",
        "as_of_date": "2026-03-10",
        "date_mode": "demo",
        "consent_scenario": "default",
    }


def test_secure_cookie_when_configured(make_client):
    client = make_client(settings=Settings(cookie_secure=True))
    response = client.post("/api/sessions", json={})
    assert "secure" in response.headers["set-cookie"].lower()


def test_session_can_be_created_without_a_body(client):
    assert client.post("/api/sessions").status_code == 201


def test_real_date_mode_and_consent_scenario_are_applied(client):
    session = start(client, date_mode="real", consent_scenario="timeout")["session"]
    assert session["as_of_date"] == TODAY.isoformat()
    assert session["date_mode"] == "real"
    assert session["consent_scenario"] == "timeout"


@pytest.mark.parametrize(
    "body",
    [{"consent_scenario": "nope"}, {"date_mode": "tomorrow"}, {"unexpected": True}],
)
def test_invalid_session_settings_are_rejected(client, body):
    response = client.post("/api/sessions", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/session"),
        ("POST", "/api/session/messages"),
        ("GET", "/api/session/outbox"),
        ("GET", "/api/session/console"),
    ],
)
def test_requests_without_a_session_get_401(client, method, path):
    body = {"message": "hello"} if method == "POST" else None
    response = client.request(method, path, json=body)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "session_expired"


def test_one_turn_round_trip(client):
    start(client)
    response = say(client, "Hi, I need help with my claim.")
    assert response.status_code == 200
    data = response.json()
    assert data["reply"]["id"] == "a-1"
    # The welcome asked in full; turn 1 asks again in shorter words rather than repeating it.
    assert data["reply"]["text"].startswith("To confirm it's you, could you share your full name")
    assert data["plan"]["ask"]["slot"] == "identity"
    assert data["plan"]["inform"] == []
    assert [event["event"] for event in data["trace"]] == [
        "understanding_completed",
        "plan_built",
        "reply_realized",
    ]
    assert data["trace"][1]["rule"] == "VERIFY_ASK_IDENTITY"
    assert data["session"]["phase"] == "VERIFY_ID"

    history = client.get("/api/session").json()["messages"]
    assert [m["role"] for m in history] == ["assistant", "user", "assistant"]
    assert history[1]["text"] == "Hi, I need help with my claim."


def test_console_shows_every_turn_with_its_plan_and_trace(client):
    start(client)
    say(client, "Hi, I need help with my claim.")
    say(client, "Why do you need that?")
    data = client.get("/api/session/console").json()

    assert data["session"]["phase"] == "VERIFY_ID"
    assert data["outbox"] == {"simulated": True, "emails": []}
    turns = data["turns"]
    assert [t["turn"] for t in turns] == [0, 1, 2]

    welcome = turns[0]
    assert welcome["caller"] is None
    assert "verify your identity" in welcome["reply"]["text"]
    assert welcome["plan"] is not None
    assert [e["event"] for e in welcome["trace"]] == ["session_opened"]

    first = turns[1]
    assert first["caller"]["text"] == "Hi, I need help with my claim."
    assert first["reply"]["role"] == "assistant"
    assert first["plan"]["ask"]["slot"] == "identity"
    assert [e["event"] for e in first["trace"]] == [
        "understanding_completed",
        "plan_built",
        "reply_realized",
    ]
    assert all(e["turn"] == 2 for e in turns[2]["trace"])
    assert turns[2]["caller"]["text"] == "Why do you need that?"


def test_the_conversation_can_be_downloaded_as_markdown_or_json(client):
    start(client)
    say(client, "Hi, I need help with my claim.")

    markdown = client.get("/api/session/export")
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert markdown.headers["content-disposition"].startswith('attachment; filename="claims-conversation-')
    text = markdown.text
    assert text.startswith("# Claims support conversation")
    assert "## Welcome: SESSION_OPENED (Verify identity)" in text
    assert "## Turn 1: VERIFY_ASK_IDENTITY (Verify identity)" in text
    assert "> Hi, I need help with my claim." in text
    assert '"slot": "identity"' in text
    assert "- plan_built: VERIFY_ASK_IDENTITY" in text
    assert "Demo outbox" not in text  # nothing sent, and nothing shown before verification

    data = client.get("/api/session/export?format=json").json()
    assert [t["turn"] for t in data["turns"]] == [0, 1]
    assert data["session"]["phase"] == "VERIFY_ID"
    assert "exported_at" in data

    assert client.get("/api/session/export?format=pdf").status_code == 422
    client.delete("/api/session")
    assert client.get("/api/session/export").status_code == 401


def test_watching_the_console_does_not_keep_a_session_alive(make_client):
    now = [datetime(2026, 3, 10, 9, 0, tzinfo=UTC)]
    client = make_client(store=SessionStore(now=lambda: now[0]))
    start(client)
    now[0] += SESSION_IDLE_TTL - timedelta(seconds=1)
    assert client.get("/api/session/console").status_code == 200
    now[0] += timedelta(seconds=2)
    assert client.get("/api/session/console").status_code == 401
    assert client.get("/api/session").status_code == 401


def test_default_app_runs_a_turn_with_the_fake_adapter():
    """With no model configured, the app runs end to end on the fake adapter."""
    with TestClient(create_app(Settings(), clock=FixedClock(TODAY))) as client:
        assert client.get("/healthz").json()["provider"] == "fake"
        start(client)
        response = say(client, "hello")
        assert response.status_code == 200
        assert "To confirm it's you" in response.json()["reply"]["text"]


@pytest.mark.parametrize(
    "payload",
    [{"message": ""}, {"message": "   "}, {"message": "x" * 4001}, {"message": "hi", "extra": 1}, {}],
)
def test_invalid_messages_are_rejected(client, payload):
    start(client)
    response = client.post("/api/session/messages", json=payload)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert "Value error" not in error["message"]
    assert len(client.get("/api/session").json()["messages"]) == 1


def test_message_at_the_length_limit_is_accepted(client):
    start(client)
    assert say(client, "x" * 4000).status_code == 200


def test_understanding_failure_returns_503_and_commits_nothing(make_client):
    adapter = ScriptedAdapter(understandings=[LLMError("bad"), LLMError("bad again")])
    client = make_client(adapter=adapter)
    start(client)
    response = say(client, "hello")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_unavailable"
    assert len(adapter.understand_calls) == 2
    assert len(client.get("/api/session").json()["messages"]) == 1
    assert [t["turn"] for t in client.get("/api/session/console").json()["turns"]] == [0]


def test_understanding_is_retried_once(make_client):
    adapter = ScriptedAdapter(understandings=[LLMError("bad"), TurnUnderstanding()])
    client = make_client(adapter=adapter)
    start(client)
    assert say(client, "hello").status_code == 200
    assert len(adapter.understand_calls) == 2


def test_failed_replies_fall_back_to_the_template(make_client):
    adapter = ScriptedAdapter(drafts=[LLMError("down"), ResponseDraft(text="   ")])
    client = make_client(adapter=adapter)
    start(client)
    data = say(client, "hello").json()
    assert data["trace"][-1]["event"] == "reply_realized"
    assert data["trace"][-1]["status"] == "fallback"
    assert "To confirm it's you" in data["reply"]["text"]
    assert len(adapter.realize_calls) == 2


def test_a_rejected_reply_is_retried_with_the_violations(make_client):
    adapter = ScriptedAdapter(
        drafts=[ResponseDraft(text=""), ResponseDraft(text="Could you share your details first?")]
    )
    client = make_client(adapter=adapter)
    start(client)
    data = say(client, "hello").json()
    assert data["reply"]["text"] == "Could you share your details first?"
    assert data["trace"][-1]["status"] == "ok"
    assert adapter.realize_calls[0].violations == ()
    assert adapter.realize_calls[1].violations == ("the reply is empty",)


def test_a_new_session_invalidates_the_previous_one(client):
    start(client)
    stale = TestClient(client.app, cookies={SESSION_COOKIE: client.cookies[SESSION_COOKIE]})
    start(client)
    assert stale.get("/api/session").status_code == 401
    assert client.get("/api/session").status_code == 200
    stale.close()


def test_deleting_the_session_clears_it(client):
    start(client)
    response = client.delete("/api/session")
    assert response.status_code == 204
    assert client.get("/api/session").status_code == 401
    assert client.delete("/api/session").status_code == 204


def test_sessions_are_isolated():
    app = create_app(Settings(), adapter=ScriptedAdapter(), clock=FixedClock(TODAY))
    with TestClient(app) as alice, TestClient(app) as bob:
        start(alice)
        start(bob)
        say(alice, "message from alice")
        bob_history = bob.get("/api/session").json()["messages"]
        assert len(bob_history) == 1
        assert all("alice" not in m["text"] for m in bob_history)
        assert len(alice.get("/api/session").json()["messages"]) == 3


def test_outbox_is_empty_before_verification(client):
    start(client)
    assert client.get("/api/session/outbox").json() == {"simulated": True, "emails": []}


def test_idle_sessions_expire(make_client):
    now = [datetime(2026, 3, 10, 9, 0, tzinfo=UTC)]
    client = make_client(store=SessionStore(now=lambda: now[0]))
    start(client)
    now[0] += SESSION_IDLE_TTL - timedelta(seconds=1)
    assert client.get("/api/session").status_code == 200
    now[0] += SESSION_IDLE_TTL + timedelta(seconds=1)
    assert client.get("/api/session").status_code == 401


def test_unexpected_errors_hide_their_details(make_client):
    adapter = ScriptedAdapter(understandings=[RuntimeError("boom: secret detail")])
    client = make_client(adapter=adapter, raise_server_exceptions=False)
    start(client)
    response = say(client, "hello")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "secret detail" not in response.text
    assert "Traceback" not in response.text
