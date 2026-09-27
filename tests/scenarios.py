"""Shared pieces for conversation-level tests: scripted understandings and a session driver."""

from __future__ import annotations

from apps.insurance_claims.domain import (
    CallerRole,
    CaseHintField,
    CaseHintUpdate,
    IdentityField,
    IdentityUpdate,
    Intent,
    StatedIdType,
    TurnUnderstanding,
)
from tests.fakes import ScriptedAdapter, make_settings

MARGARET_MESSAGE = (
    "I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied "
    "healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472."
)


def ident(field, value, evidence, **kwargs):
    return IdentityUpdate(field=IdentityField(field), value=value, evidence=evidence, **kwargs)


def hint(field, value, evidence):
    return CaseHintUpdate(field=CaseHintField(field), value=value, evidence=evidence)


MARGARET = TurnUnderstanding(
    caller_role=CallerRole.SELF,
    identity_updates=[
        ident("full_name", "Margaret Chen", "My name is Margaret Chen"),
        ident("dob", "1985-03-15", "DOB is 1985-03-15"),
        ident("id_last4", "4472", "SSN last four is 4472", id_type=StatedIdType.SSN_LAST4),
    ],
    policy_number="POL-9921",
    intent=Intent.DENIAL_QUESTION,
    case_hint_updates=[
        hint("case_type", "healthcare", "denied healthcare claim"),
        hint("month", 1, "from January"),
        hint("status", "denied", "my denied healthcare claim"),
    ],
)


class Conversation:
    """A session whose model proposals are scripted turn by turn."""

    def __init__(self, make_client, *, session=None, mailer=None, **settings):
        self.adapter = ScriptedAdapter()
        self.client = make_client(adapter=self.adapter, settings=make_settings(**settings), mailer=mailer)
        created = self.client.post("/api/sessions", json=session or {})
        assert created.status_code == 201
        self.trace = []

    def say(self, message, understanding=None):
        self.adapter.understandings.append(understanding or TurnUnderstanding())
        response = self.client.post("/api/session/messages", json={"message": message})
        assert response.status_code == 200, response.text
        data = response.json()
        self.trace.extend(data["trace"])
        return data

    def events(self, name):
        return [e for e in self.trace if e["event"] == name]

    def outbox(self):
        return self.client.get("/api/session/outbox").json()["emails"]
