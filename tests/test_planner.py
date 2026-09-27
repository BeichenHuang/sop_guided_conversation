from __future__ import annotations

from apps.insurance_claims.domain import (
    EmotionLabel,
    EmotionReading,
    IdentityField,
    Offer,
    PlanAsk,
    ReplyPlan,
)
from apps.insurance_claims.planner import brief, identity_ask, policy_facts, render_plan


def test_initial_identity_ask_covers_every_field():
    ask = identity_ask()
    assert ask.slot == "identity"
    assert ask.count == 3
    assert ask.one_of == [f.value for f in IdentityField]
    text = render_plan(ReplyPlan(ask=ask), {})
    assert text.startswith("To verify your identity, could you share your full name and at least two")
    assert text.endswith("?")
    assert "SSN (or national ID" in text


def test_a_repeated_full_identity_ask_is_shorter_than_the_welcome():
    first = render_plan(ReplyPlan(ask=identity_ask()), {})
    again = render_plan(ReplyPlan(ask=identity_ask().model_copy(update={"variant": 1})), {})
    assert again != first and len(again) < len(first)
    assert again.startswith("To confirm it's you, could you share your full name and any two of these:")
    assert again.endswith("?")


def test_identity_ask_counts_down_remaining_fields():
    ask = identity_ask([IdentityField.FULL_NAME, IdentityField.DOB])
    assert ask.count == 1
    assert IdentityField.FULL_NAME.value not in ask.one_of
    text = render_plan(ReplyPlan(ask=ask), {})
    assert "could you share one more detail" in text
    assert "phone number, email address on file, or the last 4 digits of your SSN" in text


def test_template_renders_plan_parts_in_order():
    plan = ReplyPlan(
        acknowledge=EmotionReading(label=EmotionLabel.FRUSTRATED),
        inform=["policy.why_verify"],
        ask=identity_ask([IdentityField.FULL_NAME, IdentityField.DOB]),
        offer=[Offer.HANDOFF],
    )
    text = render_plan(plan, {fid: fact.text for fid, fact in policy_facts(plan).items()})
    assert text.index("frustrating") < text.index("protected") < text.index("verifying your identity")
    assert text.endswith("human representative.")


def test_template_puts_each_part_in_its_own_paragraph():
    plan = ReplyPlan(
        acknowledge=EmotionReading(label=EmotionLabel.FRUSTRATED),
        inform=["policy.why_verify"],
        ask=identity_ask([IdentityField.FULL_NAME, IdentityField.DOB]),
        offer=[Offer.HANDOFF],
    )
    text = render_plan(plan, {fid: fact.text for fid, fact in policy_facts(plan).items()})
    paragraphs = text.split("\n\n")
    assert len(paragraphs) == 4
    assert "frustrating" in paragraphs[0]
    assert "protected" in paragraphs[1]
    assert paragraphs[2].endswith("?")
    assert paragraphs[3].endswith("human representative.")
    # A plan with only a question stays one paragraph.
    assert "\n" not in render_plan(ReplyPlan(ask=identity_ask()), {})


def test_a_retry_ask_is_one_question():
    ask = PlanAsk(slot="identity_retry", one_of=["phone", "email"])
    text = render_plan(ReplyPlan(ask=ask), {})
    assert text == (
        "Could you double-check the details you gave, "
        "or use your phone number or email address on file instead?"
    )
    assert text.count("?") == 1


def test_a_declined_detail_is_left_out_while_enough_others_remain():
    ask = identity_ask([IdentityField.FULL_NAME, IdentityField.DOB], withheld=[IdentityField.ID_LAST4])
    assert ask.one_of == ["phone", "email"]
    assert render_plan(ReplyPlan(ask=ask), {}).endswith("phone number or email address on file?")
    # Nothing given yet and no SSN: the other four still verify.
    fresh = identity_ask(withheld=[IdentityField.ID_LAST4])
    assert render_plan(ReplyPlan(ask=fresh), {}) == (
        "To verify your identity, could you share your full name and at least two of the following: "
        "date of birth, phone number, or email address on file?"
    )
    two_left = identity_ask(withheld=[IdentityField.ID_LAST4, IdentityField.PHONE])
    assert render_plan(ReplyPlan(ask=two_left), {}) == (
        "To verify your identity, could you share your full name, date of birth, and email address on file?"
    )
    # Too few would remain: the declined details come back.
    short = identity_ask(
        [IdentityField.FULL_NAME, IdentityField.DOB],
        withheld=[IdentityField.ID_LAST4, IdentityField.PHONE, IdentityField.EMAIL],
    )
    assert short.one_of == ["phone", "email", "id_last4"]


def test_the_tone_note_reaches_the_reply_model():
    plan = ReplyPlan(tone=EmotionLabel.ANGRY, ask=identity_ask())
    assert "calm" in brief(plan, {}, representative=False)["tone"]
    assert brief(ReplyPlan(ask=identity_ask()), {}, representative=False)["tone"] is None


def test_a_repeated_acknowledgement_uses_the_next_wording():
    first = ReplyPlan(acknowledge=EmotionReading(label=EmotionLabel.FRUSTRATED))
    again = ReplyPlan(acknowledge=EmotionReading(label=EmotionLabel.FRUSTRATED, variant=1))
    assert render_plan(first, {}) != render_plan(again, {})
    assert "for example" in brief(again, {}, representative=False)["acknowledge"]
