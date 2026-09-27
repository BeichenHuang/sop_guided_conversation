"""Recovery and edge cases for the basic delivery (T08, T12, T13, and leaving early)."""

from __future__ import annotations

from apps.insurance_claims.claims import ClaimService, ToolError
from apps.insurance_claims.domain import (
    DialogAct,
    EmailReply,
    Intent,
    QuestionItem,
    ResponseDraft,
    Scope,
    StatedIdType,
    Topic,
    TurnUnderstanding,
)
from apps.insurance_claims.llm.adapter import LLMError
from tests.scenarios import MARGARET, MARGARET_MESSAGE, hint, ident


class LookupDown(ToolError):
    code = "unavailable"


def test_t12_a_mixed_message_keeps_the_relevant_part_without_counting_as_off_topic(talk):
    c = talk()
    mixed = c.say(
        "I'm Margaret Chen, born 1985-03-15. Also, what's the capital of France?",
        TurnUnderstanding(
            scope=Scope.MIXED,
            identity_updates=[
                ident("full_name", "Margaret Chen", "I'm Margaret Chen"),
                ident("dob", "1985-03-15", "born 1985-03-15"),
            ],
        ),
    )
    assert mixed["plan"]["decline"] == ["off_topic"]
    assert mixed["session"]["identity_fields_provided"] == 2
    # The first purely off-topic turn afterwards is still the first step of the ladder.
    later = c.say("What is RL?", TurnUnderstanding(scope=Scope.OUT_OF_SCOPE))
    assert "policy.capabilities" not in later["plan"]["inform"]
    assert "handoff" not in later["plan"]["offer"]


def test_t13_a_failed_lookup_is_never_reported_as_no_claims(talk, monkeypatch):
    def broken(self, ctx, hints):
        raise LookupDown("claims database unavailable")

    monkeypatch.setattr(ClaimService, "find_claims", broken)
    c = talk()
    data = c.say(MARGARET_MESSAGE, MARGARET)
    assert data["plan"]["inform"] == ["policy.verified", "policy.lookup_unavailable"]
    assert "handoff" in data["plan"]["offer"]
    assert "search.no_claims" not in data["plan"]["inform"]
    assert c.events("tool_failed")[0]["status"] == "unavailable"
    claim_gate = next(g for g in data["session"]["gates"] if g["name"] == "Claim")
    assert claim_gate["state"] == "waiting"


def test_t13_a_sent_email_stays_sent_when_the_reply_fails(talk):
    c = talk()
    c.say(MARGARET_MESSAGE, MARGARET)
    c.say("That's all.", TurnUnderstanding(dialog_acts=[DialogAct.DENY]))
    c.adapter.drafts.extend([LLMError("down"), LLMError("still down")])
    data = c.say("Send it.", TurnUnderstanding(email_reply=EmailReply.SEND))
    assert data["trace"][-1]["status"] == "fallback"
    assert data["reply"]["text"].startswith("I've sent the summary to m*******@email.com.")
    assert len(c.outbox()) == 1


def test_an_early_goodbye_closes_politely_and_a_return_continues(talk):
    c = talk()
    bye = c.say("Never mind, bye.", TurnUnderstanding(dialog_acts=[DialogAct.END]))
    assert bye["plan"]["inform"] == ["policy.closing"]
    assert bye["plan"]["ask"] is None
    assert bye["session"]["status"] == "cancelled"
    back = c.say(MARGARET_MESSAGE, MARGARET)
    assert back["session"]["status"] == "active"
    assert back["session"]["selected_case_id"] == "CL-2048"


def test_a_document_submission_call_covers_timing_too(talk):
    c = talk()
    data = c.say(MARGARET_MESSAGE, MARGARET.model_copy(update={"intent": Intent.DOCUMENT_SUBMISSION}))
    assert "submission_timing" in data["plan"]["answer_topics"]
    assert "within a week" in data["reply"]["text"]


def test_t08_an_upload_link_question_gets_the_configured_demo_link(talk):
    c = talk()
    c.say(MARGARET_MESSAGE, MARGARET)
    data = c.say(
        "Is there an upload link?",
        TurnUnderstanding(questions=[QuestionItem(topic=Topic.SUBMISSION_METHOD, evidence="upload link")]),
    )
    assert data["plan"]["inform"] == ["claim.CL-2048.submission_method", "claim.CL-2048.upload_link"]
    assert "https://portal.example.com/claims/CL-2048/upload (a demo link)" in data["reply"]["text"]


def test_t08_a_link_the_model_makes_up_is_never_shown(talk):
    c = talk()
    c.say(MARGARET_MESSAGE, MARGARET)
    made_up = ResponseDraft(text="Upload it at https://uploads.insurer.com/CL-2048. Anything else?")
    c.adapter.drafts.extend([made_up, made_up])
    data = c.say(
        "Is there an upload link?",
        TurnUnderstanding(questions=[QuestionItem(topic=Topic.SUBMISSION_METHOD, evidence="upload link")]),
    )
    assert data["trace"][-1]["status"] == "fallback"
    assert "insurer.com" not in data["reply"]["text"]
    assert "https://portal.example.com/claims/CL-2048/upload" in data["reply"]["text"]


def test_a_general_ask_follows_an_account_with_no_claims(talk):
    c = talk()
    data = c.say(
        "I'm Ava Lopez, born 1990-08-21, SSN ending 9180.",
        TurnUnderstanding(
            identity_updates=[
                ident("full_name", "Ava Lopez", "I'm Ava Lopez"),
                ident("dob", "1990-08-21", "born 1990-08-21"),
                ident("id_last4", "9180", "SSN ending 9180", id_type=StatedIdType.SSN_LAST4),
            ]
        ),
    )
    assert data["plan"]["ask"]["slot"] == "anything_else_general"
    assert "on this claim" not in data["reply"]["text"]


def test_an_email_request_before_access_is_declined(talk):
    c = talk()
    data = c.say("Just email me the summary.", TurnUnderstanding(email_reply=EmailReply.SEND))
    assert "summary_before_access" in data["plan"]["decline"]
    assert c.outbox() == []


def test_asking_for_the_email_sends_the_summary_shown_with_the_reply(talk):
    c = talk()
    c.say(MARGARET_MESSAGE, MARGARET)
    data = c.say("Can you email me a summary?", TurnUnderstanding(email_reply=EmailReply.SEND))
    assert data["session"]["phase"] == "POST_PROCESS"
    assert data["plan"]["inform"] == ["summary.r1", "email.sent"]
    assert data["plan"]["ask"]["slot"] == "closing"
    sent = c.outbox()
    assert len(sent) == 1 and sent[0]["to"] == "margaret@email.com"
    # What was sent is what the reply showed.
    assert "Claim CL-2048 (healthcare, filed January 12, 2026) is currently denied" in data["reply"]["text"]
    assert (
        "Claim status: Claim CL-2048 (healthcare, filed January 12, 2026) is currently denied"
        in sent[0]["body"]
    )


IDENTITY_ONLY = [
    ident("full_name", "Margaret Chen", "I'm Margaret Chen"),
    ident("dob", "1985-03-15", "DOB 1985-03-15"),
    ident("id_last4", "4472", "SSN 4472", id_type=StatedIdType.SSN_LAST4),
]


def test_an_appeal_deadline_question_points_to_the_denied_claim(talk):
    c = talk()
    data = c.say(
        "I'm Margaret Chen, DOB 1985-03-15, SSN 4472. I'm worried I'll miss my appeal deadline.",
        TurnUnderstanding(
            identity_updates=IDENTITY_ONLY,
            questions=[QuestionItem(topic=Topic.APPEAL_DEADLINE, evidence="miss my appeal deadline")],
        ),
    )
    assert data["session"]["selected_case_id"] == "CL-2048"
    assert "claim.CL-2048.appeal_deadline" in data["plan"]["inform"]


def test_a_waiting_question_is_promised_an_answer_while_choosing_the_claim(talk):
    c = talk()
    data = c.say(
        "I'm Margaret Chen, DOB 1985-03-15, SSN 4472. How much will I get paid?",
        TurnUnderstanding(
            identity_updates=IDENTITY_ONLY,
            questions=[QuestionItem(topic=Topic.AMOUNTS, evidence="How much will I get paid?")],
        ),
    )
    assert data["plan"]["ask"]["slot"] == "choose_case"
    assert data["plan"]["next"] == "choose:amounts"
    assert "Once I know which claim you mean, I'll answer your question." in data["reply"]["text"]
    chosen = c.say(
        "The dental one.", TurnUnderstanding(case_hint_updates=[hint("case_type", "dental", "dental")])
    )
    assert chosen["session"]["selected_case_id"] == "CL-1899"
    assert chosen["plan"]["answer_topics"] == ["amounts"]


def test_a_question_and_an_email_request_together_get_the_answer_then_the_summary(talk):
    c = talk()
    c.say(MARGARET_MESSAGE, MARGARET)
    data = c.say(
        "Is a scan of the pathology report OK? Also, email me a summary.",
        TurnUnderstanding(
            email_reply=EmailReply.SEND,
            questions=[
                QuestionItem(topic=Topic.FILE_FORMAT_REQUIREMENTS, detail="pathology report", evidence="scan")
            ],
        ),
    )
    plan = data["plan"]
    assert plan["answer_topics"] == ["file_format_requirements"]
    assert plan["inform"][0] == "claim.CL-2048.guidance.pathology_report"
    assert plan["inform"][-2:] == ["summary.r1", "email.sent"]
    assert data["session"]["phase"] == "POST_PROCESS"
    assert "file format requirements" in c.outbox()[0]["body"]  # the answer made it into the summary
