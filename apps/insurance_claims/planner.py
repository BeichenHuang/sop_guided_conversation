"""Reply plans: the fixed texts a plan may reference, and the template rendering
used whenever no model reply passes the checks."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence

from .domain import (
    IDENTITY_FIELDS_REQUIRED,
    EmotionLabel,
    Fact,
    IdentityField,
    Offer,
    PlanAsk,
    Planning,
    ReplyPlan,
)

POLICY_TEXTS: Mapping[str, str] = {
    "policy.welcome": (
        "Hi, I'm the claims support assistant, and I can help with questions about your "
        "insurance claims. If you're calling on behalf of the policyholder, please let me know."
    ),
    "policy.why_verify": (
        "Claim details are protected, so I need to confirm your identity before I can share "
        "anything about them."
    ),
    "policy.why_verify_representative": (
        "Claim details are protected, so I need to confirm the policyholder's identity and your "
        "authorization before I can share anything about them."
    ),
    "policy.verified": "Thank you, your identity is verified.",
    "policy.verification_failed": "I wasn't able to verify your identity with the details provided.",
    "policy.verification_failed_representative": (
        "I wasn't able to verify the policyholder's identity with the details provided."
    ),
    "policy.verification_locked": (
        "For security, I can't continue verifying identity in this conversation after several "
        "unsuccessful attempts."
    ),
    "policy.verification_still_locked": "I still can't continue verifying identity in this conversation.",
    "policy.contact_change_unsupported": "I'm not able to update contact details in this chat.",
    "policy.representative_not_confirmed": (
        "I'm not able to confirm that you're authorized to discuss this policy, so I can't share "
        "its claim details."
    ),
    "policy.representative_role_kept": (
        "Earlier you told me you're calling on the policyholder's behalf, and I've already started "
        "checking that, so to protect the policyholder I have to continue on that basis."
    ),
    "policy.representative_verified": (
        "I've verified the policyholder's details and confirmed you're registered to act on their behalf."
    ),
    "policy.consent_approved": (
        "I sent an authorization request to the policyholder, and they approved it, so we can continue."
    ),
    "policy.consent_timeout": (
        "I sent an authorization request to the policyholder but didn't receive their approval in "
        "time. Their approval is required because these records contain their private information, "
        "so I can't share claim details yet."
    ),
    "policy.consent_still_missing": (
        "I still don't have the policyholder's approval, so I can't share their claim details."
    ),
    "policy.no_match": "I couldn't find a claim matching that description on your account.",
    "policy.lookup_unavailable": "I'm having trouble looking up claims right now.",
    "policy.safe_fallback": "Sorry, I can't share that right now.",
    "policy.why_consent": (
        "Because you're calling on the policyholder's behalf, their approval is needed before I can "
        "share their claim details. It protects their private health and financial information."
    ),
    "policy.accepted_details": (
        "To verify identity I need any three of these: full name, date of birth, phone number, the "
        "email address on file, or the last 4 digits of the SSN (or national ID for anyone without an SSN)."
    ),
    "policy.privacy": (
        "The details you share are only used to confirm identity for this conversation, and I won't "
        "repeat them back."
    ),
    "policy.capabilities": (
        "I can help with a claim's status, why it was denied, which documents are needed and how to "
        "submit them, payment amounts, and next steps."
    ),
    "policy.handed_off_already": (
        "This conversation has been transferred to a human representative (simulated in this demo). "
        "To talk with the assistant again, please start a new conversation."
    ),
    "policy.email_skipped": "No problem, I won't send the summary.",
    "policy.email_failed": "I couldn't send the summary just now.",
    "policy.email_already_sent": "That summary has already been sent.",
    "policy.closing": "Thank you for contacting claims support. Take care!",
    "policy.ended_already": (
        "This conversation has ended. To ask about anything else, please start a new conversation."
    ),
}

_FIELD_LABELS: Mapping[IdentityField, str] = {
    IdentityField.FULL_NAME: "full name",
    IdentityField.DOB: "date of birth",
    IdentityField.PHONE: "phone number",
    IdentityField.EMAIL: "email address on file",
    IdentityField.ID_LAST4: "the last 4 digits of {whose} SSN (or national ID if {who} don't have one)",
}

# A few wordings per feeling, used in turn when it has to be acknowledged again.
ACKNOWLEDGEMENTS: Mapping[EmotionLabel, tuple[str, ...]] = {
    EmotionLabel.FRUSTRATED: (
        "I understand this is frustrating.",
        "I hear you, and I'm sorry this is taking longer than you'd like.",
        "I know this isn't what you wanted to hear.",
    ),
    EmotionLabel.ANGRY: (
        "I'm sorry this has been so frustrating.",
        "I'm sorry, I know this has been a real hassle.",
        "I understand you're upset, and I want to help.",
    ),
    EmotionLabel.ANXIOUS: (
        "I understand this is stressful.",
        "I know this is a worry, so let's take it step by step.",
    ),
    EmotionLabel.CONFUSED: ("Let me make this clearer.", "Let me put that more simply."),
    EmotionLabel.REFUSING: ("I understand.", "That's okay.", "I understand, and that's your choice."),
}

OFFER_TEXTS: Mapping[Offer, str] = {
    Offer.HANDOFF: "If you'd prefer, I can connect you with a human representative.",
    Offer.ALTERNATIVE_FIELDS: "You can use any of the other details listed instead.",
    Offer.EMAIL_SUMMARY: "I can also email you a summary of this conversation.",
    Offer.RETRY_LATER: "You're welcome to try again later in a new conversation.",
    Offer.POLICYHOLDER_DIRECT: "The policyholder can also contact us directly.",
}

DECLINE_TEXTS: Mapping[str, str] = {
    "off_topic": "I can only help with questions about insurance claims, so I can't help with that.",
    "off_topic_again": "That's outside what I can help with here; I only handle insurance claims.",
    "off_topic_still": "I'm not able to help with that one either, since it isn't about an insurance claim.",
    "other_person_data": (
        "I can't share anything about another person's claims or personal details; I can only discuss "
        "claims for the policyholder verified in this conversation."
    ),
    "bypass_verification": (
        "I can't skip identity verification, because it's what keeps claim information private."
    ),
    "instruction_override": (
        "I have to follow the standard support process, so I can't change how I handle this."
    ),
    "summary_before_access": (
        "I can't email a claim summary, because I haven't been able to access the claim records in "
        "this conversation."
    ),
}
# What happens to the caller's waiting question, by what it waits for ("answer:" or "choose:").
NEXT_TEXTS: Mapping[str, str] = {
    "answer": "I'll answer your question as soon as verification is complete.",
    "choose": "Once I know which claim you mean, I'll answer your question.",
}

# Style notes for the reply model; the acknowledgement itself is a separate, one-time item.
TONE_TEXTS: Mapping[EmotionLabel, str] = {
    EmotionLabel.FRUSTRATED: (
        "The caller is frustrated: stay calm and understanding, be brief and direct, and skip cheerful "
        "filler and exclamation marks."
    ),
    EmotionLabel.ANGRY: (
        "The caller is angry: stay calm and respectful, be brief and direct, don't argue or "
        "over-apologize, and skip exclamation marks."
    ),
    EmotionLabel.ANXIOUS: "The caller is worried: be calm and reassuring, and make the next step concrete.",
    EmotionLabel.CONFUSED: "The caller is confused: use plain words and short sentences, one step at a time.",
    EmotionLabel.REFUSING: "The caller declined to share something: respect that and don't pressure them.",
}

_COUNT_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def policy_fact(fact_id: str) -> Fact:
    return Fact(id=fact_id, text=POLICY_TEXTS[fact_id], source="policy")


def policy_facts(plan: ReplyPlan) -> dict[str, Fact]:
    """Facts for a plan that only references policy texts."""
    return {fact_id: policy_fact(fact_id) for fact_id in plan.inform}


def identity_ask(
    provided: Collection[IdentityField] = (),
    *,
    countable: int | None = None,
    for_representative: bool = False,
    withheld: Collection[IdentityField] = (),
) -> PlanAsk:
    remaining = [f for f in IdentityField if f not in provided]
    counted = len(provided) if countable is None else countable
    needed = max(1, IDENTITY_FIELDS_REQUIRED - counted)
    # Details the caller declined are left out while enough others remain.
    preferred = [f for f in remaining if f not in withheld]
    options = preferred if len(preferred) >= needed else remaining
    slot = "policyholder_identity" if for_representative else "identity"
    return PlanAsk(slot=slot, one_of=[f.value for f in options], count=needed)


def render_plan(plan: ReplyPlan, facts: Mapping[str, str]) -> str:
    """The template reply: one paragraph each for the acknowledgement and declines, the facts,
    the question, and the offers, separated by blank lines."""
    opening = [DECLINE_TEXTS[code] for code in dict.fromkeys(plan.decline)]
    if acknowledgement := _acknowledgement_text(plan):
        opening.insert(0, acknowledgement)
    closing = [OFFER_TEXTS[offer] for offer in plan.offer]
    if next_text := _next_text(plan):
        closing.append(next_text)
    paragraphs = [
        opening,
        [facts[fact_id] for fact_id in [*plan.inform, *plan.available]],
        [_render_ask(plan.ask)] if plan.ask else [],
        closing,
    ]
    return "\n\n".join(" ".join(sentences) for sentences in paragraphs if sentences)


# Questions that come up again and again in one conversation, in a few wordings used in turn.
_VARIED: Mapping[str, tuple[str, ...]] = {
    "anything_else": (
        "Is there anything else I can help you with on this claim?",
        "What else would you like to know about this claim?",
        "Is there anything more about this claim I can help with?",
    ),
    "anything_else_general": (
        "Is there anything else I can help you with?",
        "What else can I help you with?",
    ),
    "closing": (
        "Is there anything else I can help you with today?",
        "Can I help with anything else today?",
    ),
}


def _acknowledgement_text(plan: ReplyPlan) -> str | None:
    if not plan.acknowledge or plan.acknowledge.label not in ACKNOWLEDGEMENTS:
        return None
    wordings = ACKNOWLEDGEMENTS[plan.acknowledge.label]
    return wordings[plan.acknowledge.variant % len(wordings)]


def _next_text(plan: ReplyPlan) -> str | None:
    return NEXT_TEXTS.get(plan.next.partition(":")[0]) if plan.next else None


def _render_ask(ask: PlanAsk) -> str:
    slot = ask.slot
    if slot in ("identity", "policyholder_identity"):
        return _identity_sentence(ask, representative=slot == "policyholder_identity")
    if slot in ("identity_retry", "policyholder_identity_retry"):
        representative = slot.startswith("policyholder")
        if not ask.one_of:
            return "Could you double-check the details you gave?"
        labels = _labels([IdentityField(v) for v in ask.one_of], representative)
        whose = "their" if representative else "your"
        return f"Could you double-check the details you gave, or use {whose} {_join_or(labels)} instead?"
    if slot == "claims_help":
        return "Is there anything about an insurance claim I can help you with?"
    if slot == "full_name":
        return "Could you tell me your full name, first and last?"
    if slot == "policyholder_full_name":
        return "Could you tell me the policyholder's full name, first and last?"
    if slot == "id_type":
        return "Is that the last 4 digits of an SSN or of a national ID?"
    if slot == "dob_format":
        return 'Could you write the date of birth with the month spelled out, for example "July 4, 1990"?'
    if slot == "representative":
        wanted = {"name": "your full name", "relationship": "your relationship to the policyholder"}
        details = " and ".join(wanted[part] for part in ask.one_of)
        return f"Since you're calling on the policyholder's behalf, could you tell me {details}?"
    if slot == "choose_case":
        return "Which claim would you like to discuss?"
    if slot == "choose_case_again":
        return "Which of the claims I listed would you like to discuss?"
    if slot == "confirm_case":
        return "Is that the claim you mean?"
    if slot == "describe_case":
        return "Could you tell me more about the claim, such as its type or when it was filed?"
    if slot == "case_question":
        return "What would you like to know about this claim?"
    if slot in _VARIED:
        wordings = _VARIED[slot]
        return wordings[ask.variant % len(wordings)]
    if slot == "email_consent":
        return "Would you like me to send it?"
    if slot == "email_retry":
        return "Would you like me to try sending it again, or skip it?"
    raise ValueError(f"no template for ask slot {slot!r}")


def identity_when_ready(fields: Sequence[IdentityField], *, representative: bool) -> Fact:
    """No more asking after the caller declines: say what would still work, for whenever they're ready."""
    if fields:
        whose = "their" if representative else "your"
        text = (
            f"Whenever you're ready, you can share {whose} {_join_or(_labels(list(fields), representative))} "
            "instead, and I'll pick up where we left off."
        )
    else:
        text = (
            "If you change your mind, you can share the details at any time and I'll pick up where we "
            "left off."
        )
    return Fact(id="policy.identity_when_ready", text=text, source="policy")


def _identity_sentence(ask: PlanAsk, *, representative: bool) -> str:
    fields = [IdentityField(value) for value in ask.one_of]
    whose = "the policyholder's" if representative else "your"
    if IdentityField.FULL_NAME in fields and ask.count >= IDENTITY_FIELDS_REQUIRED:
        others = _labels([f for f in fields if f is not IdentityField.FULL_NAME], representative)
        if ask.variant:
            # Asked in full before (the welcome does): shorter, so it doesn't repeat word for word.
            purpose = "To confirm the policyholder's identity" if representative else "To confirm it's you"
            if len(others) == 2:
                return f"{purpose}, could you share {whose} full name, {others[0]}, and {others[1]}?"
            return f"{purpose}, could you share {whose} full name and any two of these: {_join_or(others)}?"
        if len(others) == 2:
            return (
                f"To verify {whose} identity, could you share {whose} full name, {others[0]}, "
                f"and {others[1]}?"
            )
        return (
            f"To verify {whose} identity, could you share {whose} full name and at least two of the "
            f"following: {_join_or(others)}?"
        )
    noun = "detail" if ask.count == 1 else "details"
    count = _COUNT_WORDS.get(ask.count, str(ask.count))
    return (
        f"To finish verifying {whose} identity, could you share {count} more {noun} from this list: "
        f"{_join_or(_labels(fields, representative))}?"
    )


def _labels(fields: list[IdentityField], representative: bool) -> list[str]:
    pronouns = {"whose": "their", "who": "they"} if representative else {"whose": "your", "who": "you"}
    return [_FIELD_LABELS[f].format(**pronouns) for f in fields]


def _join_or(items: list[str]) -> str:
    if len(items) <= 2:
        return " or ".join(items)
    return ", ".join(items[:-1]) + ", or " + items[-1]


def brief(plan: ReplyPlan, facts: Mapping[str, str], *, representative: bool) -> dict[str, object]:
    """The plan as plain texts, for the reply model to phrase."""
    return {
        "mode": "answer" if plan.planning is Planning.GROUNDED else "strict",
        "caller_is_representative": representative,
        "acknowledge": (
            f"{plan.acknowledge.label.value} ({plan.acknowledge.intensity.value}), "
            f'for example: "{_acknowledgement_text(plan)}"'
            if _acknowledgement_text(plan)
            else None
        ),
        "tone": TONE_TEXTS.get(plan.tone) if plan.tone else None,
        "decline": [DECLINE_TEXTS[code] for code in dict.fromkeys(plan.decline)],
        "facts": [{"id": fact_id, "text": facts[fact_id]} for fact_id in plan.inform],
        "optional_facts": [{"id": fact_id, "text": facts[fact_id]} for fact_id in plan.available],
        "answer_topics": [topic.value for topic in plan.answer_topics],
        "offers": [OFFER_TEXTS[offer] for offer in plan.offer],
        "next": _next_text(plan),
        "question": _render_ask(plan.ask) if plan.ask else None,
        "reference_reply": render_plan(plan, facts),
    }
