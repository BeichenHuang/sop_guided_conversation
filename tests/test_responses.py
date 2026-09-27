from __future__ import annotations

import pytest

from apps.insurance_claims.domain import PlanAsk, ReplyPlan, ResponseDraft
from apps.insurance_claims.planner import brief, render_plan
from apps.insurance_claims.responses import check_draft

FACT = "The appeal deadline for claim CL-2048 is March 18, 2026, and $1,450.00 is the allowed maximum."
PLAN = ReplyPlan(inform=["deadline"], ask=PlanAsk(slot="anything_else"))
QUESTION = "Is there anything else I can help you with on this claim?"


def problems(text, plan=PLAN, **kwargs):
    kwargs.setdefault("question_text", QUESTION)
    return check_draft(ResponseDraft(text=text), plan, {"deadline": FACT}, **kwargs)


def test_a_faithful_paraphrase_passes():
    text = (
        "Your claim CL-2048 has an appeal deadline of Mar 18, 2026; $1450 is the allowed maximum. "
        "Anything else?"
    )
    assert problems(text) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("CL-2048 is due March 18, 2026. CL-9999 too. $1,450.00. Anything else?", "CL-9999"),
        ("CL-2048 is due March 18, 2026, maybe March 20. $1,450.00. Anything else?", "date (March 20)"),
        ("CL-2048 is due March 18, 2026. You'll get $1,500.00. $1,450.00. Anything else?", "$1500.00"),
        ("CL-2048, March 18, 2026, $1,450.00. Upload at https://example.com. Anything else?", "a link"),
        ("CL-2048, March 18, 2026, $1,450.00. Write to help@insurer.com. Anything else?", "an email address"),
        ("CL-2048, March 18, 2026, $1,450.00. Call 650-555-1234. Anything else?", "a phone number"),
    ],
)
def test_invented_values_are_caught(text, expected):
    assert any(expected in p for p in problems(text))


def test_missing_key_values_are_caught():
    found = problems("Your deadline is soon. Anything else?")
    assert any("missing CL-2048, $1450.00, March 18" in p for p in found)


def test_the_question_is_required_only_when_it_is_a_question():
    assert "the reply must end with the planned question" in problems(
        "CL-2048 is due March 18, 2026, allowed maximum $1,450.00."
    )
    assert problems("CL-2048 is due March 18, 2026, allowed maximum $1,450.00.", question_text=None) == []


def test_identity_details_are_never_echoed():
    text = "CL-2048 is due March 18, 2026 ($1,450.00). Your SSN ends in 4472. Anything else?"
    assert "the reply repeats an identity detail the caller gave" in problems(text, forbidden=["4472"])
    # A claim number that happens to contain the digits is not an echo.
    assert problems("CL-2048, March 18, 2026, $1,450.00. Anything else?", forbidden=["2048"]) == []


def test_the_template_rendering_always_passes_its_own_checks():
    facts = {"deadline": FACT}
    rendered = render_plan(PLAN, facts)
    b = brief(PLAN, facts, representative=False)
    assert check_draft(ResponseDraft(text=rendered), PLAN, facts, question_text=b["question"]) == []


LINK = "https://portal.example.com/claims/CL-2048/upload"
LINK_PLAN = ReplyPlan(inform=["link"], ask=PlanAsk(slot="anything_else"))
LINK_FACT = f"The upload link for claim CL-2048 is {LINK} (a demo link)."


@pytest.mark.parametrize(
    "text",
    [
        f"For CL-2048, upload at {LINK}. Anything else?",
        f"For CL-2048, use the upload link ({LINK}). Anything else?",
        f"For CL-2048, the link is {LINK}, a demo link. Anything else?",
    ],
)
def test_a_given_link_passes_whatever_punctuation_follows_it(text):
    assert check_draft(ResponseDraft(text=text), LINK_PLAN, {"link": LINK_FACT}, question_text=QUESTION) == []


def test_a_required_link_must_be_given():
    found = check_draft(
        ResponseDraft(text="For CL-2048, use the upload link. Anything else?"),
        LINK_PLAN,
        {"link": LINK_FACT},
        question_text=QUESTION,
    )
    assert any(LINK in p for p in found)
