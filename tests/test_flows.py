"""End-to-end conversation flows with scripted understandings.

These cover the main line: grounded answers, the email summary, handoffs,
off-topic handling and emotional turns. Invariants run in strict mode.
"""

from __future__ import annotations

from apps.insurance_claims.domain import (
    DeliveryStatus,
    DialogAct,
    EmailReply,
    EmotionLabel,
    EmotionReading,
    IdentityField,
    Intensity,
    Intent,
    ProcessTopic,
    QuestionItem,
    SafetyConcern,
    Scope,
    Topic,
    TurnUnderstanding,
)
from tests.scenarios import MARGARET, MARGARET_MESSAGE, hint, ident

NO_MORE = TurnUnderstanding(dialog_acts=[DialogAct.DENY])
SEND = TurnUnderstanding(email_reply=EmailReply.SEND, dialog_acts=[DialogAct.AGREE])
SKIP = TurnUnderstanding(email_reply=EmailReply.SKIP, dialog_acts=[DialogAct.DENY])


def ask(topic, evidence, detail=None):
    return TurnUnderstanding(
        dialog_acts=[DialogAct.ASK_QUESTION],
        questions=[QuestionItem(topic=topic, detail=detail, evidence=evidence)],
    )


class FailingSender:
    async def send(self, email):
        return DeliveryStatus.FAILED


def test_t01_full_main_line_to_a_sent_summary(talk):
    c = talk()
    first = c.say(MARGARET_MESSAGE, MARGARET)
    assert first["plan"]["answer_topics"] == [
        "denial_reason",
        "documents_needed",
        "appeal_deadline",
        "submission_method",
    ]

    scan = c.say(
        "Can I use a scan of the pathology report?",
        ask(Topic.FILE_FORMAT_REQUIREMENTS, "scan of the pathology report", detail="pathology report"),
    )
    assert scan["plan"]["inform"] == ["claim.CL-2048.guidance.pathology_report"]
    assert "high-quality scan is acceptable" in scan["reply"]["text"]

    offer = c.say("No, that's all.", NO_MORE)
    assert offer["session"]["phase"] == "POST_PROCESS"
    assert offer["plan"]["ask"]["slot"] == "email_consent"
    assert "m*******@email.com" in offer["reply"]["text"]
    assert c.outbox() == []  # nothing is sent before consent

    sent = c.say("Yes, send it.", SEND)
    assert sent["session"]["email_status"] == "simulated_sent"
    [email] = c.outbox()
    assert email["to"] == "margaret@email.com"
    assert "why the claim was denied" in email["body"] and "file format requirements" in email["body"]
    assert "1985" not in email["body"] and "4472" not in email["body"]

    bye = c.say("No thanks, bye!", TurnUnderstanding(dialog_acts=[DialogAct.END]))
    assert bye["plan"]["inform"] == ["policy.closing"]
    assert bye["session"]["status"] == "ended"
    assert "conversation_ended" in [e["event"] for e in bye["trace"]]
    # The conversation is over: a later message only says so.
    later = c.say("Thanks again! Also, what about my auto claim?")
    assert later["plan"]["inform"] == ["policy.ended_already"]
    assert later["plan"].get("ask") is None
    assert later["session"]["status"] == "ended"


def test_t19_a_question_asked_before_verification_is_answered_right_after(talk):
    c = talk()
    c.say(
        "I'm Margaret Chen, born 1985-03-15. Why was my claim denied?",
        TurnUnderstanding(
            identity_updates=[
                ident("full_name", "Margaret Chen", "I'm Margaret Chen"),
                ident("dob", "1985-03-15", "born 1985-03-15"),
            ],
            intent=Intent.DENIAL_QUESTION,
            questions=[QuestionItem(topic=Topic.DENIAL_REASON, evidence="Why was my claim denied?")],
        ),
    )
    data = c.say(
        "My email is margaret@email.com",
        TurnUnderstanding(
            identity_updates=[
                ident("email", "margaret@email.com", "My email is margaret@email.com"),
            ]
        ),
    )
    assert data["session"]["selected_case_id"] == "CL-2048"  # the soft "denied" hint picks it
    assert "claim.CL-2048.denial_reason" in data["plan"]["inform"]
    assert data["plan"]["ask"]["slot"] == "anything_else"  # not "what is your question?"


def test_t03_refusing_the_ssn_and_using_email_instead(talk):
    c = talk()
    c.say(
        "I'm calling about my denied claim from January.",
        TurnUnderstanding(
            intent=Intent.DENIAL_QUESTION,
            case_hint_updates=[hint("status", "denied", "my denied claim"), hint("month", 1, "from January")],
        ),
    )
    c.say(
        "I'm Margaret Chen, born 1985-03-15.",
        TurnUnderstanding(
            identity_updates=[
                ident("full_name", "Margaret Chen", "I'm Margaret Chen"),
                ident("dob", "1985-03-15", "born 1985-03-15"),
            ]
        ),
    )
    refused = c.say(
        "I'd rather not give my SSN.",
        TurnUnderstanding(
            dialog_acts=[DialogAct.REFUSE],
            emotion=EmotionReading(label=EmotionLabel.REFUSING),
            withheld_fields=[IdentityField.ID_LAST4],
        ),
    )
    assert refused["plan"]["acknowledge"]["label"] == "refusing"
    assert refused["plan"]["ask"]["one_of"] == ["phone", "email"]  # the SSN is not asked for again
    assert "SSN" not in refused["reply"]["text"]
    assert refused["session"]["verified"] is False
    done = c.say(
        "Use my email, margaret@email.com.",
        TurnUnderstanding(
            identity_updates=[
                ident("email", "margaret@email.com", "margaret@email.com"),
            ]
        ),
    )
    assert done["session"]["verified"] is True
    assert done["session"]["selected_case_id"] == "CL-2048"  # the early hint is reused


def test_t10_email_needs_consent_to_this_summary(talk):
    c = talk()
    # An "ok" during verification authorizes nothing.
    c.say("ok", TurnUnderstanding(dialog_acts=[DialogAct.AGREE]))
    c.say(MARGARET_MESSAGE, MARGARET)
    first_offer = c.say("That's all.", NO_MORE)
    revision_1 = first_offer["plan"]["inform"][0]

    # A follow-up question makes the offered summary stale; a new one is offered.
    follow_up = c.say("Wait, how much was paid?", ask(Topic.AMOUNTS, "how much was paid"))
    assert follow_up["plan"]["ask"]["slot"] == "email_consent"
    assert follow_up["plan"]["inform"][-2] != revision_1

    c.say("Send it.", SEND)
    [email] = c.outbox()
    assert "the payment amounts" in email["body"]


def test_skipping_is_respected_until_the_caller_asks_again(talk):
    c = talk()
    c.say(MARGARET_MESSAGE, MARGARET)
    c.say("That's all.", NO_MORE)
    skipped = c.say("No, don't send it.", SKIP)
    assert skipped["plan"]["inform"] == ["policy.email_skipped"]
    assert c.say("ok")["plan"]["ask"]["slot"] == "closing"  # no new offer on its own
    again = c.say("Actually, please email it after all.", TurnUnderstanding(email_reply=EmailReply.SEND))
    # The explicit request is the consent: the summary is shown with it and sent, once.
    assert again["plan"]["inform"] == ["summary.r2", "email.sent"]
    assert [email["id"] for email in c.outbox()] == ["email-2"]
    repeat = c.say("Can you email it again?", TurnUnderstanding(email_reply=EmailReply.SEND))
    assert repeat["plan"]["inform"] == ["policy.email_already_sent"]
    assert len(c.outbox()) == 1


def test_t11_one_summary_is_sent_once_and_failures_are_not_reported_as_sent(talk):
    c = talk()
    c.say(MARGARET_MESSAGE, MARGARET)
    c.say("That's all.", NO_MORE)
    c.say("Send it.", SEND)
    repeat = c.say("Send it again.", SEND)
    assert repeat["plan"]["ask"]["slot"] == "closing"  # already sent: no second offer, no resend
    assert len(c.outbox()) == 1

    failing = talk(mailer=FailingSender())
    failing.say(MARGARET_MESSAGE, MARGARET)
    failing.say("That's all.", NO_MORE)
    failed = failing.say("Send it.", SEND)
    assert failed["plan"]["inform"] == ["policy.email_failed"]
    assert failed["plan"]["ask"]["slot"] == "email_retry"
    assert failed["session"]["email_status"] == "failed"
    assert failing.outbox() == []
    skipped = failing.say("Skip it then.", SKIP)
    assert skipped["plan"]["inform"] == ["policy.email_skipped"]


def test_t09_follow_up_then_another_claim_starts_a_new_cycle(talk):
    c = talk()
    c.say(MARGARET_MESSAGE, MARGARET)
    c.say("That's all.", NO_MORE)
    c.say("Send it.", SEND)
    data = c.say(
        "What about my auto claim?",
        TurnUnderstanding(
            intent=Intent.STATUS_INQUIRY,
            case_hint_updates=[hint("case_type", "auto", "my auto claim")],
        ),
    )
    assert data["session"]["case_cycle_id"] == 2
    assert data["session"]["selected_case_id"] == "CL-2102"
    assert data["session"]["email_status"] == "not_requested"  # the old consent does not carry over
    assert "claim.CL-2102.status" in data["plan"]["inform"]


def test_t17_a_real_date_after_the_deadline_changes_the_answer(talk):
    c = talk(session={"date_mode": "real"})
    data = c.say(MARGARET_MESSAGE, MARGARET)
    deadline = next(f for f in data["plan"]["inform"] if f.endswith("appeal_deadline"))
    assert deadline == "claim.CL-2048.appeal_deadline"
    assert "has already passed" in data["reply"]["text"]
    assert "handoff" in data["plan"]["offer"]


def test_no_claims_are_reported_and_summarized_honestly(talk):
    c = talk()
    c.say(
        "I'm Ava Lopez, born 1990-08-21, SSN ending 9180.",
        TurnUnderstanding(
            identity_updates=[
                ident("full_name", "Ava Lopez", "I'm Ava Lopez"),
                ident("dob", "1990-08-21", "born 1990-08-21"),
                ident("id_last4", "9180", "SSN ending 9180"),
            ]
        ),
    )
    offer = c.say("No, that's it.", NO_MORE)
    assert "No claims were found on the account." in offer["reply"]["text"]
    c.say("Yes.", SEND)
    assert c.outbox()[0]["to"] == "ava.lopez@email.com"


def test_asking_for_a_person_hands_off_without_claim_data(talk):
    c = talk()
    data = c.say("I want to talk to a real person.", TurnUnderstanding(dialog_acts=[DialogAct.REQUEST_HUMAN]))
    assert data["session"]["status"] == "handed_off"
    assert "verify your identity" in data["reply"]["text"]
    later = c.say("Hello?")
    assert later["plan"]["inform"] == ["policy.handed_off_already"]


def test_a_verified_handoff_carries_the_claim(talk):
    c = talk()
    c.say(MARGARET_MESSAGE, MARGARET)
    data = c.say("Can I talk to someone?", TurnUnderstanding(dialog_acts=[DialogAct.REQUEST_HUMAN]))
    assert "CL-2048" in data["reply"]["text"]


def test_the_off_topic_ladder_ends_with_a_person(talk):
    c = talk()
    off_topic = TurnUnderstanding(scope=Scope.OUT_OF_SCOPE)
    first = c.say("What is RL?", off_topic)
    second = c.say("Seriously, explain RL.", off_topic)
    third = c.say("Capital of France?", off_topic)
    assert first["plan"]["decline"] == ["off_topic"] and "handoff" not in first["plan"]["offer"]
    assert "policy.capabilities" in second["plan"]["inform"]
    assert "handoff" in third["plan"]["offer"]
    # Off-topic turns do not count as a lack of verification progress.
    assert "alternative_fields" not in third["plan"]["offer"]
    accepted = c.say("Yes, please.", TurnUnderstanding(dialog_acts=[DialogAct.AGREE]))
    assert accepted["session"]["status"] == "handed_off"


def test_t14_frustration_is_acknowledged_without_lowering_the_gate(talk):
    c = talk()
    c.say(
        "I'm Margaret Chen, born 1985-03-15.",
        TurnUnderstanding(
            identity_updates=[
                ident("full_name", "Margaret Chen", "I'm Margaret Chen"),
                ident("dob", "1985-03-15", "born 1985-03-15"),
            ]
        ),
    )
    angry = c.say(
        "I already told you who I am. This is ridiculous. Just tell me why my claim was denied.",
        TurnUnderstanding(
            emotion=EmotionReading(label=EmotionLabel.FRUSTRATED, intensity=Intensity.HIGH),
            safety_concerns=[SafetyConcern.BYPASS_VERIFICATION],
            intent=Intent.DENIAL_QUESTION,
            questions=[QuestionItem(topic=Topic.DENIAL_REASON, evidence="why my claim was denied")],
        ),
    )
    plan = angry["plan"]
    assert plan["acknowledge"]["label"] == "frustrated"
    assert plan["decline"] == ["bypass_verification"]
    assert "policy.why_verify" not in plan["inform"]  # the refusal already explains why
    assert plan["ask"]["slot"] == "identity" and plan["next"] == "answer:denial_reason"
    assert angry["session"]["verified"] is False

    c.say("No.", TurnUnderstanding(dialog_acts=[DialogAct.REFUSE]))
    stop = c.say("I said no.", TurnUnderstanding(dialog_acts=[DialogAct.REFUSE]))
    # Past the persuasion limit: no more asking, a person offered, the other details still possible.
    assert stop["plan"].get("ask") is None
    assert stop["plan"]["offer"] == ["handoff"]
    assert stop["plan"]["inform"] == ["policy.identity_when_ready"]
    assert "phone number" in stop["reply"]["text"]
    assert stop["session"]["verified"] is False
    back = c.say(
        "Fine, my phone is 650-521-2836.",
        TurnUnderstanding(identity_updates=[ident("phone", "650-521-2836", "650-521-2836")]),
    )
    assert back["session"]["verified"] is True  # the door stayed open


def test_process_questions_and_safety_requests_are_handled_in_place(talk):
    c = talk()
    why = c.say(
        "Why do you need my date of birth?", TurnUnderstanding(process_question=ProcessTopic.WHY_VERIFICATION)
    )
    assert "policy.why_verify" in why["plan"]["inform"]
    assert why["plan"]["ask"]["slot"] == "identity"  # back to the SOP step
    c.say(MARGARET_MESSAGE, MARGARET)
    other = c.say(
        "What about Ava Lopez's claims?", TurnUnderstanding(safety_concerns=[SafetyConcern.OTHER_PERSON_DATA])
    )
    assert other["plan"]["decline"] == ["other_person_data"]
    assert "Ava" not in other["reply"]["text"]


def test_t14_a_caller_who_declines_everything_left_is_offered_a_person_not_let_through(talk):
    c = talk()
    c.say(
        "I'm Margaret Chen, born 1985-03-15.",
        TurnUnderstanding(
            identity_updates=[
                ident("full_name", "Margaret Chen", "I'm Margaret Chen"),
                ident("dob", "1985-03-15", "born 1985-03-15"),
            ]
        ),
    )
    worried = c.say(
        "I'm really worried about giving out my SSN, phone or email online.",
        TurnUnderstanding(
            dialog_acts=[DialogAct.REFUSE],
            emotion=EmotionReading(label=EmotionLabel.ANXIOUS, intensity=Intensity.MEDIUM),
            withheld_fields=[IdentityField.ID_LAST4, IdentityField.PHONE, IdentityField.EMAIL],
        ),
    )
    plan = worried["plan"]
    assert plan["acknowledge"]["label"] == "anxious" and plan["tone"] == "anxious"
    assert "handoff" in plan["offer"]
    assert worried["session"]["verified"] is False
    human = c.say("Yes, a person please.", TurnUnderstanding(dialog_acts=[DialogAct.REQUEST_HUMAN]))
    assert human["session"]["status"] == "handed_off"
    assert human["plan"]["inform"] == ["handoff.created"]
    tools = {e["tool"] for e in c.events("tool_completed")}
    assert not tools & {"find_claims", "get_claim_details"}  # no claim was read at any point


def test_t14_a_lasting_mood_is_not_acknowledged_every_turn(talk):
    c = talk()
    upset = EmotionReading(label=EmotionLabel.FRUSTRATED, intensity=Intensity.MEDIUM)
    first = c.say("This is so annoying. I'm Margaret Chen.", TurnUnderstanding(emotion=upset))
    second = c.say("Ugh, fine, what else do you need?", TurnUnderstanding(emotion=upset))
    assert first["plan"]["acknowledge"]["label"] == "frustrated"
    assert second["plan"].get("acknowledge") is None and second["plan"]["tone"] == "frustrated"
    assert "frustrating" not in second["reply"]["text"]
