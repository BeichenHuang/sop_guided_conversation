from __future__ import annotations

import pytest

from apps.insurance_claims.domain import (
    DateMode,
    DialogAct,
    EmotionLabel,
    EmotionReading,
    IdentityField,
    IdentityUpdate,
    Intensity,
    LedgerEntry,
    LedgerSource,
    Offer,
    PendingKind,
    PendingQuestion,
    Phase,
    ProcessTopic,
    QuestionItem,
    SafetyConcern,
    Scope,
    SessionState,
    Topic,
    TurnUnderstanding,
)
from apps.insurance_claims.interrupts import evaluate


def fresh() -> SessionState:
    return SessionState(session_id="s", date_mode=DateMode.DEMO, consent_scenario="default")


def run(state, turn=1, **understanding):
    return evaluate(state, TurnUnderstanding(**understanding), turn)


def test_safety_concerns_are_declined_but_the_sop_continues():
    result = run(
        fresh(), safety_concerns=[SafetyConcern.OTHER_PERSON_DATA, SafetyConcern.BYPASS_VERIFICATION]
    )
    assert result.declines == ["other_person_data", "bypass_verification"]
    assert result.handoff_reason is None


def test_an_explicit_request_for_a_person_is_honored_at_once():
    assert run(fresh(), dialog_acts=[DialogAct.REQUEST_HUMAN]).handoff_reason == "caller_request"


def test_yes_after_an_offer_accepts_it_but_not_when_another_question_is_pending():
    state = fresh()
    state.counters.handoff_offered_turn = 1
    assert run(state, turn=2, dialog_acts=[DialogAct.AGREE]).handoff_reason == "accepted_offer"

    state = fresh()
    state.counters.handoff_offered_turn = 1
    state.pending_question = PendingQuestion(kind=PendingKind.CONFIRM_CASE, asked_turn=1)
    assert run(state, turn=2, dialog_acts=[DialogAct.AGREE]).handoff_reason is None


def test_ok_with_a_goodbye_after_an_offer_is_a_goodbye_not_a_yes():
    state = fresh()
    state.counters.handoff_offered_turn = 1
    # "OK, I'll ask her to call you herself. Bye."
    ok_bye = [DialogAct.AGREE, DialogAct.PROVIDE_INFORMATION, DialogAct.END]
    assert run(state, turn=2, dialog_acts=ok_bye).handoff_reason is None
    # Asking for a person outright still counts, goodbye or not: "Transfer me, please. Bye."
    asked = [DialogAct.REQUEST_HUMAN, DialogAct.END]
    assert run(state, turn=2, dialog_acts=asked).handoff_reason == "caller_request"


@pytest.mark.parametrize(
    ("label", "intensity", "acknowledged"),
    [
        (EmotionLabel.FRUSTRATED, Intensity.MEDIUM, True),
        (EmotionLabel.ANXIOUS, Intensity.HIGH, True),
        (EmotionLabel.FRUSTRATED, Intensity.LOW, True),  # any feeling shown is acknowledged
        (EmotionLabel.REFUSING, Intensity.LOW, True),
        (EmotionLabel.ANXIOUS, Intensity.LOW, True),
        (EmotionLabel.NEUTRAL, Intensity.HIGH, False),
    ],
)
def test_emotions_are_acknowledged_when_they_matter(label, intensity, acknowledged):
    result = run(fresh(), emotion=EmotionReading(label=label, intensity=intensity))
    assert (result.acknowledge is not None) is acknowledged


def test_persuasion_stops_after_two_refusals():
    state = fresh()
    first = run(state, dialog_acts=[DialogAct.REFUSE])
    second = run(state, turn=2, dialog_acts=[DialogAct.REFUSE])
    assert first.offers == []
    assert second.offers == [Offer.ALTERNATIVE_FIELDS, Offer.HANDOFF]
    assert state.counters.handoff_offered_turn == 2


def test_high_anger_offers_a_person():
    result = run(fresh(), emotion=EmotionReading(label=EmotionLabel.ANGRY, intensity=Intensity.HIGH))
    assert Offer.HANDOFF in result.offers


@pytest.mark.parametrize(
    ("topic", "policy"),
    [
        (ProcessTopic.WHY_VERIFICATION, "policy.why_verify"),
        (ProcessTopic.WHY_CONSENT, "policy.why_consent"),
        (ProcessTopic.ACCEPTED_DETAILS, "policy.accepted_details"),
        (ProcessTopic.PRIVACY, "policy.privacy"),
        (ProcessTopic.CAPABILITIES, "policy.capabilities"),
    ],
)
def test_process_questions_are_answered_from_policy(topic, policy):
    assert run(fresh(), process_question=topic).inform == [policy]


def test_off_topic_ladder_counts_every_off_topic_turn():
    state = fresh()
    first = run(state, scope=Scope.OUT_OF_SCOPE)
    run(state, turn=2, scope=Scope.IN_SCOPE)  # alternating does not reset the count
    second = run(state, turn=3, scope=Scope.OUT_OF_SCOPE)
    third = run(state, turn=4, scope=Scope.OUT_OF_SCOPE)
    assert first.declines == ["off_topic"] and first.inform == [] and first.offers == []
    assert second.inform == ["policy.capabilities"]
    assert third.offers == [Offer.HANDOFF]
    assert state.counters.off_topic_total == 3


def test_mixed_messages_decline_the_off_topic_part_without_counting():
    state = fresh()
    result = run(state, scope=Scope.MIXED)
    assert result.declines == ["off_topic"]
    assert not result.off_topic
    assert state.counters.off_topic_total == 0


def test_a_refused_request_is_declined_once_not_also_as_off_topic():
    state = fresh()
    mixed = run(state, scope=Scope.MIXED, safety_concerns=[SafetyConcern.INSTRUCTION_OVERRIDE])
    assert mixed.declines == ["instruction_override"]
    off_topic = run(
        state, turn=2, scope=Scope.OUT_OF_SCOPE, safety_concerns=[SafetyConcern.OTHER_PERSON_DATA]
    )
    assert off_topic.declines == ["other_person_data"]
    assert state.counters.off_topic_total == 1  # still counted toward the ladder


def test_a_lasting_mood_is_acknowledged_once_but_keeps_its_tone():
    state = fresh()
    upset = EmotionReading(label=EmotionLabel.FRUSTRATED, intensity=Intensity.MEDIUM)
    first = run(state, emotion=upset)
    again = run(state, turn=2, emotion=upset)
    assert first.acknowledge.label is EmotionLabel.FRUSTRATED and first.tone is EmotionLabel.FRUSTRATED
    assert again.acknowledge is None and again.tone is EmotionLabel.FRUSTRATED
    # Stronger than before: acknowledged again.
    worse = run(
        state, turn=3, emotion=EmotionReading(label=EmotionLabel.FRUSTRATED, intensity=Intensity.HIGH)
    )
    assert worse.acknowledge is not None
    # A calm turn in between resets it.
    run(state, turn=4)
    back = run(state, turn=5, emotion=upset)
    assert back.acknowledge.label is EmotionLabel.FRUSTRATED
    # Acknowledged again later, it is worded differently.
    assert back.acknowledge.variant != first.acknowledge.variant
    # A different mood is acknowledged on its own.
    worried = run(
        state, turn=6, emotion=EmotionReading(label=EmotionLabel.ANXIOUS, intensity=Intensity.MEDIUM)
    )
    assert worried.acknowledge is not None and worried.tone is EmotionLabel.ANXIOUS


def test_declining_the_email_is_not_treated_as_an_upset_refusal():
    state = fresh()
    state.phase = Phase.POST_PROCESS
    result = run(state, emotion=EmotionReading(label=EmotionLabel.REFUSING, intensity=Intensity.LOW))
    assert result.acknowledge is None


def test_pushing_back_on_verification_gets_a_fresh_acknowledgement_and_counts_as_resistance():
    state = fresh()
    state.ledger.put(
        LedgerEntry(key="identity.full_name", value="Margaret Chen", source=LedgerSource.USER_SAID, turn=1)
    )
    upset = EmotionReading(label=EmotionLabel.FRUSTRATED, intensity=Intensity.MEDIUM)
    run(state, emotion=upset)
    demand = run(
        state,
        turn=2,
        emotion=upset,
        questions=[QuestionItem(topic=Topic.DENIAL_REASON, evidence="why")],
    )
    assert demand.pushback and demand.acknowledge.label is EmotionLabel.FRUSTRATED  # acknowledged again
    assert state.counters.persuasion_attempts["verify"] == 1
    again = run(state, turn=3, emotion=upset, dialog_acts=[DialogAct.REFUSE])
    assert again.pushback and "PERSUASION_LIMIT" in again.rules
    # Giving a detail is going along, even when upset: no pushback, no repeated acknowledgement.
    going_along = run(
        state,
        turn=4,
        emotion=upset,
        identity_updates=[
            IdentityUpdate(field=IdentityField.PHONE, value="650-521-2836", evidence="650-521-2836")
        ],
    )
    assert not going_along.pushback and going_along.acknowledge is None


def test_once_verified_there_is_nothing_left_to_push_back_on():
    state = fresh()
    state.identity.verified = True
    result = run(state, dialog_acts=[DialogAct.REFUSE], safety_concerns=[SafetyConcern.BYPASS_VERIFICATION])
    assert not result.pushback and "PERSUASION_LIMIT" not in result.rules


def test_pushback_is_acknowledged_even_when_no_feeling_was_flagged():
    state = fresh()
    state.ledger.put(
        LedgerEntry(key="identity.full_name", value="Margaret Chen", source=LedgerSource.USER_SAID, turn=1)
    )
    result = run(state, questions=[QuestionItem(topic=Topic.DENIAL_REASON, evidence="why")])
    assert result.pushback and result.acknowledge.label is EmotionLabel.FRUSTRATED
    assert "PUSHBACK_ACKNOWLEDGED" in result.rules


def test_yes_with_a_detail_goes_on_with_verification_instead_of_accepting_a_person():
    state = fresh()
    state.counters.handoff_offered_turn = 1
    result = run(
        state,
        turn=2,
        dialog_acts=[DialogAct.AGREE],
        identity_updates=[
            IdentityUpdate(field=IdentityField.PHONE, value="650-521-2836", evidence="650-521-2836")
        ],
    )
    assert result.handoff_reason is None


def test_repeated_off_topic_declines_change_their_wording():
    state = fresh()
    codes = [run(state, turn=n, scope=Scope.OUT_OF_SCOPE).declines for n in (1, 2, 3, 4)]
    assert codes == [["off_topic"], ["off_topic_again"], ["off_topic_still"], ["off_topic_still"]]
