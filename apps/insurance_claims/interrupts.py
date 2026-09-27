"""Cross-phase interruptions, handled before the phase logic.

Each handler contributes plan fragments; the phase logic still runs afterwards
for the in-scope part of the message, and the reply ends by returning to the
pending SOP step. Only an accepted handoff stops the phase logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .domain import (
    CallerRole,
    DialogAct,
    EmotionLabel,
    EmotionReading,
    Intensity,
    Offer,
    PendingKind,
    Phase,
    ProcessTopic,
    SafetyConcern,
    Scope,
    SessionState,
    TurnUnderstanding,
)

# Product thresholds, not values given by the instruction.
PERSUASION_LIMIT = 2
OFF_TOPIC_LIST_CAPABILITIES_AT = 2
OFF_TOPIC_OFFER_HUMAN_AT = 3

# Questions with their own yes/no answer; a "yes" there never means "transfer me".
_YES_NO_PENDING = frozenset({PendingKind.CONFIRM_CASE, PendingKind.ANYTHING_ELSE, PendingKind.EMAIL_CONSENT})


@dataclass
class InterruptResult:
    declines: list[str] = field(default_factory=list)
    acknowledge: EmotionReading | None = None
    tone: EmotionLabel | None = None
    inform: list[str] = field(default_factory=list)
    offers: list[Offer] = field(default_factory=list)
    handoff_reason: str | None = None
    # A purely off-topic turn: it is not counted as a lack of verification progress.
    off_topic: bool = False
    # The caller pushed back on verification without giving anything new.
    pushback: bool = False
    rules: list[str] = field(default_factory=list)


def evaluate(state: SessionState, understanding: TurnUnderstanding, turn: int) -> InterruptResult:
    result = InterruptResult()
    acts = set(understanding.dialog_acts)

    # 1. Safety: refuse, then carry on with the SOP.
    for concern in understanding.safety_concerns:
        result.declines.append(concern.value)
        result.rules.append(f"SAFETY_{concern.value.upper()}")

    # 2. An explicit request for a person is honored right away, without persuading first.
    offered_last_turn = state.counters.handoff_offered_turn == turn - 1
    pending = state.pending_question.kind if state.pending_question else None
    # "Yes" alone accepts the offer; "yes, my phone is ..." is going on with verification instead,
    # and "OK, I'll ask her to call you. Bye." is a goodbye: the "OK" acknowledges, it doesn't accept.
    accepted_offer = (
        offered_last_turn
        and DialogAct.AGREE in acts
        and DialogAct.END not in acts
        and pending not in _YES_NO_PENDING
        and not understanding.identity_updates
    )
    if DialogAct.REQUEST_HUMAN in acts or accepted_offer:
        result.handoff_reason = "caller_request" if DialogAct.REQUEST_HUMAN in acts else "accepted_offer"
        result.rules.append("HANDOFF_REQUESTED")
        return result

    # 3. Hard stops (verification lockout, consent timeout) are enforced by the phase logic.

    # 4. Emotion first, then the reason, then the next step.
    #    Pushing back on verification means refusing, asking to skip it, or asking about the claim again
    #    after giving some details, with nothing new this turn ("I already told you who I am").
    result.pushback = (
        state.phase is Phase.VERIFY_ID
        and not state.identity.verified
        and not understanding.identity_updates
        and (
            DialogAct.REFUSE in acts
            or SafetyConcern.BYPASS_VERIFICATION in understanding.safety_concerns
            or (bool(understanding.questions) and bool(state.identity_fields()))
        )
    )
    emotion = understanding.emotion
    # Any feeling the caller shows is acknowledged before the workflow moves on (the assignment's
    # bonus), however mild. Turning down an email is just an answer, not a refusal to acknowledge.
    answer_not_refusal = emotion.label is EmotionLabel.REFUSING and state.phase is not Phase.VERIFY_ID
    if emotion.label is not EmotionLabel.NEUTRAL and not answer_not_refusal:
        result.tone = emotion.label
        if _continues(state, emotion, turn) and not result.pushback:
            # Said last turn already, and the caller is going along; saying it again every turn
            # sounds scripted, so only the tone stays. Pushback gets a fresh acknowledgement.
            result.rules.append(f"EMOTION_{emotion.label.value.upper()}_CONTINUES")
        else:
            result.acknowledge = _acknowledgement(state, emotion)
            result.rules.append(f"EMOTION_{emotion.label.value.upper()}")
    if result.pushback and result.acknowledge is None:
        # Pushing back is itself a sign of frustration, flagged or not (the assignment's example).
        label = EmotionLabel.REFUSING if DialogAct.REFUSE in acts else EmotionLabel.FRUSTRATED
        result.acknowledge = _acknowledgement(state, EmotionReading(label=label, intensity=Intensity.LOW))
        result.tone = result.tone or label
        result.rules.append("PUSHBACK_ACKNOWLEDGED")
    state.counters.last_emotion = emotion if emotion.label is not EmotionLabel.NEUTRAL else None
    state.counters.last_emotion_turn = turn
    if result.pushback:
        attempts = state.counters.persuasion_attempts.get("verify", 0) + 1
        state.counters.persuasion_attempts["verify"] = attempts
        if attempts >= PERSUASION_LIMIT:
            result.offers += [Offer.ALTERNATIVE_FIELDS, Offer.HANDOFF]
            result.rules.append("PERSUASION_LIMIT")
    if emotion.label is EmotionLabel.ANGRY and emotion.intensity is Intensity.HIGH:
        result.offers.append(Offer.HANDOFF)
        result.rules.append("HIGH_ANGER")

    # 5. Questions about the process are in scope and answered from policy.
    if understanding.process_question is not None:
        result.inform.append(_process_policy(understanding.process_question, state))
        result.rules.append(f"PROCESS_{understanding.process_question.value.upper()}")

    # 6. Off-topic: decline every time; the count only grows on purely off-topic turns. A refused
    #    request already has its own decline, so a second one would only repeat it.
    refused = bool(understanding.safety_concerns)
    if understanding.scope is Scope.OUT_OF_SCOPE:
        state.counters.off_topic_total += 1
        total = state.counters.off_topic_total
        if not refused:
            # The same refusal three times running reads as scripted, so its wording moves on.
            result.declines.append(OFF_TOPIC_DECLINES[min(total, len(OFF_TOPIC_DECLINES)) - 1])
        result.off_topic = True
        result.rules.append(f"OFF_TOPIC_{total}")
        if total >= OFF_TOPIC_OFFER_HUMAN_AT:
            result.offers.append(Offer.HANDOFF)
        elif total >= OFF_TOPIC_LIST_CAPABILITIES_AT:
            result.inform.append("policy.capabilities")
    elif understanding.scope is Scope.MIXED and not refused:
        result.declines.append("off_topic")
        result.rules.append("OFF_TOPIC_PART")

    if Offer.HANDOFF in result.offers:
        state.counters.handoff_offered_turn = turn
    return result


_INTENSITY_RANK = {Intensity.LOW: 0, Intensity.MEDIUM: 1, Intensity.HIGH: 2}

# Decline codes for the first, second, and later off-topic turns (planner.DECLINE_TEXTS).
OFF_TOPIC_DECLINES = ("off_topic", "off_topic_again", "off_topic_still")


def _acknowledgement(state: SessionState, emotion: EmotionReading) -> EmotionReading:
    """The feeling to acknowledge, with the next wording for it."""
    key = f"acknowledge.{emotion.label.value}"
    variant = state.counters.asked.get(key, 0)
    state.counters.asked[key] = variant + 1
    return emotion.model_copy(update={"variant": variant})


def _continues(state: SessionState, emotion: EmotionReading, turn: int) -> bool:
    """The same mood as last turn, and no stronger."""
    last = state.counters.last_emotion
    return (
        last is not None
        and state.counters.last_emotion_turn == turn - 1
        and last.label is emotion.label
        and _INTENSITY_RANK[emotion.intensity] <= _INTENSITY_RANK[last.intensity]
    )


def _process_policy(topic: ProcessTopic, state: SessionState) -> str:
    if topic is ProcessTopic.WHY_VERIFICATION:
        representative = state.caller.role is CallerRole.REPRESENTATIVE
        return "policy.why_verify_representative" if representative else "policy.why_verify"
    return {
        ProcessTopic.WHY_CONSENT: "policy.why_consent",
        ProcessTopic.ACCEPTED_DETAILS: "policy.accepted_details",
        ProcessTopic.PRIVACY: "policy.privacy",
        ProcessTopic.CAPABILITIES: "policy.capabilities",
    }[topic]
