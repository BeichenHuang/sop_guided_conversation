"""The evaluation harness itself, driven by scripted callers and a scripted model (no network)."""

from __future__ import annotations

from apps.insurance_claims.api import create_app
from apps.insurance_claims.domain import DialogAct, EmailReply, TurnUnderstanding
from evals.judge import JUDGE_SYSTEM, SOP_ITEMS, Verdict
from evals.run import (
    Check,
    Expect,
    RunRecord,
    Scenario,
    TurnRecord,
    load_scenarios,
    run_one,
    summarize,
)
from evals.simulator import ScriptedCaller
from evals.usage import Usage
from tests.fakes import ScriptedAdapter, make_settings
from tests.scenarios import MARGARET, MARGARET_MESSAGE

FOLLOWED = {"status": "followed", "note": "ok"}
VERDICT = {
    "verify_id": FOLLOWED,
    "resolve_intent": FOLLOWED,
    "process_case": FOLLOWED,
    "post_process": {"status": "violated", "note": "skipped the recap"},
    "scope": FOLLOWED,
    "memory": FOLLOWED,
    "emotional_support": {"status": "not_applicable", "note": "calm caller"},
    "naturalness": 4,
    "empathy": 0,
    "goal_met": True,
    "issues": ["a bit long"],
}

META = {
    "date": "2026-09-26 00:00 UTC",
    "commit": "abc1234",
    "prompt_version": "0123456789ab",
    "agent_model": "gpt-6-luna",
    "reasoning_effort": "low",
    "caller_model": "gpt-6-luna",
    "judge_model": None,
    "runs": 1,
    "demo_date": "2026-03-10",
}


def scenario(**expect) -> Scenario:
    return Scenario(
        id="margaret",
        title="Margaret",
        covers=["T01"],
        opening=MARGARET_MESSAGE,
        caller="(scripted)",
        goal="Find out why the claim was denied and get the summary emailed.",
        expect=Expect(**expect),
    )


def run_margaret(expected: Scenario) -> RunRecord:
    understandings = [
        MARGARET,
        TurnUnderstanding(dialog_acts=[DialogAct.DENY]),
        TurnUnderstanding(email_reply=EmailReply.SEND),
    ]
    return run_one(
        expected,
        1,
        make_app=lambda usage: create_app(make_settings(), adapter=ScriptedAdapter(understandings)),
        make_caller=lambda s, usage: ScriptedCaller(["No, that's all.", "Yes, send it."]),
    )


def test_the_caller_stops_once_the_conversation_has_ended():
    understandings = [
        MARGARET,
        TurnUnderstanding(dialog_acts=[DialogAct.DENY]),
        TurnUnderstanding(email_reply=EmailReply.SEND),
        TurnUnderstanding(dialog_acts=[DialogAct.DENY]),
    ]
    record = run_one(
        scenario(verified=True, claim="CL-2048", email="sent"),
        1,
        make_app=lambda usage: create_app(make_settings(), adapter=ScriptedAdapter(understandings)),
        make_caller=lambda s, usage: ScriptedCaller(
            ["No, that's all.", "Yes, send it.", "No, that's everything.", "One more thing!"]
        ),
    )
    assert record.ended == "ended"
    assert [t.rule for t in record.turns] == [
        "PROCESS_ANSWERED",
        "EMAIL_OFFERED",
        "EMAIL_SENT",
        "CONVERSATION_CLOSED",
    ]
    assert record.passed, [c for c in record.checks if not c.passed]


def test_the_scenario_file_loads_and_covers_every_persona():
    scenarios = load_scenarios()
    ids = [s.id for s in scenarios]
    assert len(ids) == len(set(ids))
    assert {"margaret_official", "margaret_frustrated", "david_default", "david_timeout"} <= set(ids)
    assert {"matian_national_id", "ava_no_claims", "off_topic", "injector", "ssn_guesser"} <= set(ids)
    assert all(s.covers and s.goal and s.caller for s in scenarios)


def test_a_scripted_conversation_runs_to_the_end_and_passes_its_checks():
    record = run_margaret(
        scenario(
            verified=True,
            claim="CL-2048",
            email="sent",
            email_to="margaret@email.com",
            required=[["pathology"], ["March 18"]],
            forbidden=["4472"],
        )
    )
    assert record.ended == "caller_done"
    assert [t.rule for t in record.turns] == ["PROCESS_ANSWERED", "EMAIL_OFFERED", "EMAIL_SENT"]
    assert record.passed, [c for c in record.checks if not c.passed]
    assert [c.name for c in record.checks] == [
        "completed",
        "invariants",
        "verified",
        "claim",
        "email",
        "no_forbidden_text",
        "required_text",
    ]


def test_checks_report_what_went_wrong():
    record = run_margaret(
        scenario(
            claim=None,
            email="not_sent",
            handed_off=True,
            locked=True,
            forbidden=["CL-2048"],
            required=[["scan"]],
        )
    )
    failed = {c.name: c.detail for c in record.checks if not c.passed}
    assert failed == {
        "claim": "selected=CL-2048",
        "email": "1 email(s) in the outbox",
        "handoff": "status=active",
        "locked": "Verified",
        "no_forbidden_text": "CL-2048",
        "required_text": "scan",
    }


def test_a_crashing_run_is_recorded_not_raised():
    def broken_app(usage: Usage):
        raise RuntimeError("no model")

    record = run_one(scenario(), 2, make_app=broken_app, make_caller=lambda s, usage: ScriptedCaller([]))
    assert record.ended == "error" and not record.passed
    assert record.checks == [Check("completed", False, "RuntimeError: no model")]


def test_the_summary_lists_scenarios_costs_and_failures():
    passing = RunRecord(
        scenario="margaret",
        run=1,
        greeting="Hi",
        turns=[TurnRecord("hello", "hi there", "VERIFY_ID", "VERIFY_ASK_IDENTITY", "ok", [], [], 1.5)],
        checks=[Check("completed", True)],
        verdict=VERDICT,
        usage={"agent": {"calls": 2, "input_tokens": 1_000_000, "output_tokens": 0, "reasoning_tokens": 0}},
    )
    failing = RunRecord(
        scenario="margaret",
        run=2,
        greeting="Hi",
        checks=[Check("completed", True), Check("email", False, "nothing sent")],
    )
    text = summarize([scenario()], [passing, failing], META)
    assert "| margaret | T01 | 1/2 | email ×1 | 0.5 | 0 | post_process ×1 | 4.0 | – | 1/1 |" in text
    assert "| post_process | 0 | 1 | 0 |" in text and "| verify_id | 1 | 0 | 0 |" in text
    assert "| emotional_support | 0 | 0 | 1 |" in text
    assert "- margaret #1, post_process: skipped the recap" in text
    assert "| agent | gpt-6-luna | 2 | 1,000,000 | 0 | 0.10 |" in text
    assert "### margaret #2" in text and "email: nothing sent" in text
    assert "- margaret #1: a bit long" in text


def test_the_judges_verdict_schema_names_every_sop_item():
    assert set(SOP_ITEMS) <= set(Verdict.model_fields)
    assert Verdict(**VERDICT).post_process.status == "violated"
    # Steps the SOP requires are spelled out, so the judge doesn't count them against the agent.
    assert "including when their first sign of being done is a goodbye" in JUDGE_SYSTEM
    assert "The recap and offer happen once" in JUDGE_SYSTEM
