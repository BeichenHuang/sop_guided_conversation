"""The inspector's session view: gates and memory, and what it must never show."""

from __future__ import annotations

import json

from apps.insurance_claims.domain import (
    CallerRole,
    DialogAct,
    IdentityField,
    RepresentativeInfo,
    TurnUnderstanding,
)
from tests.scenarios import MARGARET, MARGARET_MESSAGE, ident


def gates(view):
    return {g["name"]: (g["state"], g["detail"]) for g in view["gates"]}


def memory(view, source):
    return {m["label"]: m["value"] for m in view["memory"] if m["source"] == source}


def test_a_verified_caller_sees_gates_memory_and_records_but_no_identity_values(talk):
    c = talk()
    view = c.say(MARGARET_MESSAGE, MARGARET)["session"]
    assert gates(view)["Identity"] == ("passed", "Verified")
    assert gates(view)["Claim"] == ("passed", "CL-2048 selected")
    told = memory(view, "caller")
    assert told["Date of birth"] == "provided" and told["ID last 4"] == "provided"
    assert told["Claim type"] == "healthcare" and told["Claim month"] == "January"
    assert told["Reason for calling"] == "denial question"
    records = memory(view, "records")
    assert records["Claim"] == "CL-2048: healthcare, January 12, 2026, denied"
    assert "why the claim was denied" in records["Answered from records"]
    dumped = json.dumps(view)
    assert "1985" not in dumped and "4472" not in dumped and "Margaret" not in dumped


def test_before_verification_records_stay_locked(talk):
    c = talk()
    view = c.say(
        "About my denied claim; I'm Margaret Chen.",
        TurnUnderstanding(
            identity_updates=[ident("full_name", "Margaret Chen", "I'm Margaret Chen")],
        ),
    )["session"]
    assert gates(view)["Identity"] == ("waiting", "1 of 3 details provided")
    assert gates(view)["Claim"] == ("waiting", "Locked until access is granted")
    assert memory(view, "records") == {}
    assert view["selected_case_id"] is None


def test_representative_gates_appear_for_a_representative(talk):
    c = talk()
    view = c.say(
        "I'm David Chen, her son.",
        TurnUnderstanding(
            caller_role=CallerRole.REPRESENTATIVE,
            representative=RepresentativeInfo(
                name="David Chen", relationship="son", evidence="I'm David Chen, her son"
            ),
        ),
    )["session"]
    assert gates(view)["Representative"] == ("waiting", "Pending")
    assert gates(view)["Policyholder consent"] == ("waiting", "Requested after verification")
    assert memory(view, "caller")["Relationship"] == "son"


def test_awaiting_drives_the_email_quick_replies(talk):
    c = talk()
    c.say(MARGARET_MESSAGE, MARGARET)
    view = c.say("That's all.", TurnUnderstanding(dialog_acts=[DialogAct.DENY]))["session"]
    assert view["awaiting"] == "email_consent"
    assert gates(view)["Email summary"] == ("waiting", "Offered; waiting for consent")


def test_declined_details_are_shown_so_the_ask_can_be_checked(talk):
    c = talk()
    view = c.say(
        "I'd rather not give my SSN.",
        TurnUnderstanding(dialog_acts=[DialogAct.REFUSE], withheld_fields=[IdentityField.ID_LAST4]),
    )["session"]
    assert memory(view, "caller")["Prefers not to share"] == "ID last 4"
