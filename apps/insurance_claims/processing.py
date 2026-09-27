"""Grounded facts for questions about the selected claim.

The model picks the topics; this module decides what may be said about each
one, using only the claim record, the claim schema and the document guidelines.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .claims import format_date
from .config import DEFAULT_PORTAL_URL
from .domain import Fact, Intent, Topic
from .fixture_loader import DOCUMENT_GUIDANCE_KEYS, Claim, ClaimStatus, FixtureData

# What each path must cover when the caller has not asked anything specific.
DEFAULT_TOPICS: Mapping[Intent, tuple[Topic, ...]] = {
    Intent.STATUS_INQUIRY: (Topic.STATUS,),
    Intent.DENIAL_QUESTION: (
        Topic.DENIAL_REASON,
        Topic.DOCUMENTS_NEEDED,
        Topic.APPEAL_DEADLINE,
        Topic.SUBMISSION_METHOD,
    ),
    Intent.DOCUMENT_SUBMISSION: (
        Topic.DOCUMENTS_NEEDED,
        Topic.SUBMISSION_METHOD,
        Topic.FILE_FORMAT_REQUIREMENTS,
        Topic.SUBMISSION_TIMING,
    ),
    Intent.PAYMENT_QUESTION: (Topic.AMOUNTS,),
    Intent.NEXT_STEPS: (Topic.STATUS, Topic.SUBMISSION_TIMING, Topic.PROCESSING_TIME_AFTER_SUBMISSION),
    Intent.GENERAL_CLAIM_QUESTION: (Topic.STATUS,),
}

TOPIC_LABELS: Mapping[Topic, str] = {
    Topic.STATUS: "the claim's status",
    Topic.DENIAL_REASON: "why the claim was denied",
    Topic.DOCUMENTS_NEEDED: "the documents needed",
    Topic.APPEAL_DEADLINE: "the appeal deadline",
    Topic.AMOUNTS: "the payment amounts",
    Topic.MISSING_REQUIRED_MATERIAL_ALTERNATIVES: "alternatives for documents you can't get",
    Topic.SUBMISSION_TIMING: "when to submit the documents",
    Topic.PROCESSING_TIME_AFTER_SUBMISSION: "how long review takes after submission",
    Topic.SUBMISSION_METHOD: "how to submit the documents",
    Topic.FILE_FORMAT_REQUIREMENTS: "file format requirements",
    Topic.RECEIPT_CONFIRMATION: "how to confirm the documents were received",
    Topic.OTHER: "other questions",
}

_DOCUMENT_TOPICS = frozenset(
    {
        Topic.MISSING_REQUIRED_MATERIAL_ALTERNATIVES,
        Topic.SUBMISSION_TIMING,
        Topic.PROCESSING_TIME_AFTER_SUBMISSION,
        Topic.SUBMISSION_METHOD,
        Topic.FILE_FORMAT_REQUIREMENTS,
        Topic.RECEIPT_CONFIRMATION,
    }
)


# The guideline's general timing advice ("within a week"), in days.
WEEK_DAYS = 7


@dataclass
class TopicAnswer:
    facts: list[Fact] = field(default_factory=list)
    # The first fact of each topic answers it and must be said; the rest help if relevant.
    required: set[str] = field(default_factory=set)
    # The facts point to options only a person can decide, such as an expired deadline.
    needs_handoff: bool = False
    # Document alternatives were given, so a repeat request means they are exhausted.
    gave_alternatives: bool = False
    # The timing advice already names the appeal deadline, so it isn't said again on its own.
    timing_names_deadline: bool = False


def _days_away(days: int) -> str:
    return "today" if days == 0 else f"{days} day{'s' if days != 1 else ''} from today"


def upload_link(portal_url: str, case_id: str) -> str:
    """The claim's upload page in the demo portal: a placeholder, never a real site."""
    return f"{portal_url.rstrip('/')}/claims/{case_id}/upload"


def money(amount: Decimal) -> str:
    return f"${amount:,.2f}"


def document_list(claim: Claim) -> str:
    names = list(claim.documents_needed)
    if len(names) <= 2:
        return " and ".join(f"the {n}" for n in names)
    return ", ".join(f"the {n}" for n in names[:-1]) + f", and the {names[-1]}"


class CaseFacts:
    def __init__(self, fixtures: FixtureData, portal_url: str = DEFAULT_PORTAL_URL) -> None:
        self._portal_url = portal_url
        self._guidelines = fixtures.guidelines
        self._schema = fixtures.claim_schema
        self._followups = {item.topic: item for item in fixtures.guidelines.claim_followup_guidance}

    def answer(
        self,
        claim: Claim,
        topics: Sequence[Topic],
        as_of: date,
        *,
        alternatives_given: bool = False,
        details: Mapping[Topic, str] | None = None,
    ) -> TopicAnswer:
        result = TopicAnswer()
        for topic in topics:
            before = len(result.facts)
            document = self._match_document(claim, (details or {}).get(topic))
            self._answer_topic(claim, topic, as_of, alternatives_given, result, document)
            if len(result.facts) > before:
                result.required.add(result.facts[before].id)
        # The same fact can serve several topics; keep the first copy.
        unique: dict[str, Fact] = {}
        for fact in result.facts:
            unique.setdefault(fact.id, fact)
        if result.timing_names_deadline:
            deadline_id = f"claim.{claim.case_id}.appeal_deadline"
            unique.pop(deadline_id, None)
            result.required.discard(deadline_id)
        result.facts = list(unique.values())
        return result

    def _answer_topic(
        self,
        claim: Claim,
        topic: Topic,
        as_of: date,
        alternatives_given: bool,
        result: TopicAnswer,
        document: str | None,
    ) -> None:
        add = result.facts.append
        if topic is Topic.STATUS:
            add(self._status(claim))
        elif topic is Topic.DENIAL_REASON:
            add(self._denial(claim))
        elif topic is Topic.DOCUMENTS_NEEDED:
            add(self._documents(claim))
        elif topic is Topic.APPEAL_DEADLINE:
            fact, passed = self._deadline(claim, as_of)
            add(fact)
            result.needs_handoff |= passed
        elif topic is Topic.AMOUNTS:
            result.facts.extend(self._amounts(claim))
        elif topic in _DOCUMENT_TOPICS:
            self._followup(claim, topic, as_of, alternatives_given, result, document)
        else:
            add(self._fact(claim, "fallback", self._fallback(claim), "required_document_guideline.json"))
            result.needs_handoff = True

    # --- claim fields ---------------------------------------------------------------

    def _status(self, claim: Claim) -> Fact:
        words = {
            ClaimStatus.DENIED: "denied",
            ClaimStatus.CLOSED: "closed",
            ClaimStatus.OPEN: "open and in progress",
        }
        text = (
            f"Claim {claim.case_id} is a {claim.case_type} claim filed on {format_date(claim.created_at)}. "
            f"It is currently {words[claim.status]}. The record's summary: {claim.summary}."
        )
        return self._fact(claim, "status", text, f"claims.json:{claim.case_id}.status")

    def _denial(self, claim: Claim) -> Fact:
        if claim.status is ClaimStatus.DENIED and claim.denial_reason:
            text = f"Claim {claim.case_id} was denied because {claim.denial_reason}."
        else:
            text = f"Claim {claim.case_id} was not denied; it is currently {claim.status.value}."
        return self._fact(claim, "denial_reason", text, f"claims.json:{claim.case_id}.denial_reason")

    def _documents(self, claim: Claim) -> Fact:
        if claim.documents_needed:
            text = f"To reconsider claim {claim.case_id}, the reviewer needs {document_list(claim)}."
        else:
            text = f"No documents are currently requested for claim {claim.case_id}."
        return self._fact(claim, "documents_needed", text, f"claims.json:{claim.case_id}.documents_needed")

    @staticmethod
    def _match_document(claim: Claim, detail: str | None) -> str | None:
        """The requested document the caller asked about, if their words name one."""
        if not detail:
            return None
        generic = {
            "the",
            "a",
            "an",
            "my",
            "her",
            "his",
            "report",
            "note",
            "copy",
            "original",
            "scan",
            "document",
        }
        wanted = set(re.sub(r"[^\w]+", " ", detail.casefold()).split()) - generic
        for name in claim.documents_needed:
            words = set(f"{name} {DOCUMENT_GUIDANCE_KEYS.get(name, '')}".casefold().split()) - generic
            if wanted & words:
                return name
        return None

    def _document_guidance(self, claim: Claim, only: str | None = None) -> list[Fact]:
        facts = []
        for name in claim.documents_needed:
            if only and name != only:
                continue
            key = DOCUMENT_GUIDANCE_KEYS.get(name)
            guidance = self._guidelines.document_guidance.get(key) if key else None
            if guidance:
                facts.append(
                    self._fact(
                        claim,
                        f"guidance.{name.replace(' ', '_')}",
                        guidance.en,
                        f"required_document_guideline.json:document_guidance.{key}",
                    )
                )
        case_type = self._guidelines.case_type_guidance.get(claim.case_type)
        if claim.documents_needed and case_type and not only:
            facts.append(
                self._fact(
                    claim,
                    "guidance.case_type",
                    case_type.en,
                    f"required_document_guideline.json:case_type_guidance.{claim.case_type}",
                )
            )
        return facts

    def _deadline(self, claim: Claim, as_of: date) -> tuple[Fact, bool]:
        source = f"claims.json:{claim.case_id}.appeal_deadline"
        if claim.appeal_deadline is None:
            text = f"There is no appeal deadline on record for claim {claim.case_id}."
            return self._fact(claim, "appeal_deadline", text, source), False
        days = (claim.appeal_deadline - as_of).days
        when = format_date(claim.appeal_deadline)
        if days < 0:
            # An expired deadline is not overridden by general timing advice.
            text = (
                f"The appeal deadline for claim {claim.case_id} was {when}, which has already passed. "
                "That does not by itself mean nothing can be done, but a human representative needs to "
                "review which options remain."
            )
            return self._fact(claim, "appeal_deadline", text, source), True
        text = f"The appeal deadline for claim {claim.case_id} is {when}, which is {_days_away(days)}."
        return self._fact(claim, "appeal_deadline", text, source), False

    def _amounts(self, claim: Claim) -> list[Fact]:
        source = f"claims.json:{claim.case_id}"
        paid = (
            f"The insurer has paid {money(claim.net_pay)} on claim {claim.case_id}; "
            "this is the finalized amount paid."
        )
        return [
            self._fact(claim, "amount.net_pay", paid, f"{source}.net_pay"),
            self._fact(
                claim,
                "amount.expected",
                f"The expected reimbursement on record is {money(claim.expected_reimbursement_amount)}. "
                "It is the amount used to reconcile payments, not a promise of payment.",
                f"{source}.expected_reimbursement_amount",
            ),
            self._fact(
                claim,
                "amount.allowed_max",
                f"The allowed maximum is {money(claim.allowed_max_amount)}: the most an in-network insurer "
                "will pay for this covered service. It is not a guaranteed payment.",
                f"{source}.allowed_max_amount",
            ),
            self._fact(
                claim,
                "amount.net_fee",
                f"The net fee is {money(claim.net_fee)}: the fee-schedule rate adjusted for contractual "
                "obligations. It is not a balance the patient owes.",
                f"{source}.net_fee",
            ),
        ]

    # --- guideline follow-ups ---------------------------------------------------------

    def _followup(
        self,
        claim: Claim,
        topic: Topic,
        as_of: date,
        alternatives_given: bool,
        result: TopicAnswer,
        document: str | None,
    ) -> None:
        if not claim.documents_needed:
            text = (
                f"No documents are currently requested for claim {claim.case_id}, so that doesn't apply here."
            )
            result.facts.append(
                self._fact(claim, f"{topic.value}.not_applicable", text, f"claims.json:{claim.case_id}")
            )
            return
        if topic is Topic.SUBMISSION_TIMING and claim.appeal_deadline and claim.appeal_deadline < as_of:
            fact, _ = self._deadline(claim, as_of)
            result.facts.append(fact)
            result.needs_handoff = True
            return
        if (
            topic is Topic.SUBMISSION_TIMING
            and claim.appeal_deadline
            and (claim.appeal_deadline - as_of).days < WEEK_DAYS
        ):
            # "Within a week" would run past the deadline, so the deadline is the advice.
            fact, _ = self._deadline(claim, as_of)
            text = f"For claim {claim.case_id}, please submit {document_list(claim)} by the appeal deadline. "
            result.facts.append(fact.model_copy(update={"text": text + fact.text}))
            return
        if topic is Topic.MISSING_REQUIRED_MATERIAL_ALTERNATIVES:
            result.facts.extend(self._alternatives(claim, alternatives_given, document))
            result.gave_alternatives = True
            result.needs_handoff |= alternatives_given
            return
        if topic is Topic.FILE_FORMAT_REQUIREMENTS and document:
            # "Can I send a scan of the pathology report?": that document's guidance answers it.
            result.facts.extend(self._document_guidance(claim, only=document))
        entry = self._followups[topic]
        result.facts.append(
            self._fact(
                claim,
                topic.value,
                self._fill(entry.en, claim),
                f"required_document_guideline.json:claim_followup_guidance.{topic.value}",
            )
        )
        if topic is Topic.SUBMISSION_METHOD:
            # The guideline says "the member portal or claim upload link"; this is that link. It comes from
            # configuration, so the model never has to make one up.
            url = upload_link(self._portal_url, claim.case_id)
            link = self._fact(
                claim,
                "upload_link",
                f"The upload link for claim {claim.case_id} is {url} (a demo link).",
                "demo portal (DEMO_PORTAL_URL)",
            )
            result.facts.append(link)
            result.required.add(link.id)
        if topic is Topic.FILE_FORMAT_REQUIREMENTS:
            result.facts.append(
                self._fact(
                    claim,
                    "guidance.default",
                    self._guidelines.default_guidance.en,
                    "required_document_guideline.json:default_guidance",
                )
            )
            if not document:
                result.facts.extend(self._document_guidance(claim))
        if topic is Topic.SUBMISSION_TIMING and claim.appeal_deadline:
            # "By when?" is answered by the deadline as much as by the advice. One sentence says how they
            # relate, so "within a week" and a deadline eight days away don't read as competing dates.
            advice = result.facts[-1]
            when = format_date(claim.appeal_deadline)
            days = _days_away((claim.appeal_deadline - as_of).days)
            text = (
                f"{advice.text.removesuffix('.')}, so they arrive before the appeal deadline of {when}, "
                f"which is {days}."
            )
            source = f"{advice.source}; claims.json:{claim.case_id}.appeal_deadline"
            result.facts[-1] = advice.model_copy(update={"text": text, "source": source})
            result.timing_names_deadline = True

    def _alternatives(self, claim: Claim, alternatives_given: bool, document: str | None) -> list[Fact]:
        """The lead fact depends on the situation: once alternatives were offered,
        a human review leads; for a named document, its own alternatives lead."""
        general = self._fact(
            claim,
            Topic.MISSING_REQUIRED_MATERIAL_ALTERNATIVES.value,
            self._fill(self._followups[Topic.MISSING_REQUIRED_MATERIAL_ALTERNATIVES].en, claim),
            "required_document_guideline.json:claim_followup_guidance.missing_required_material_alternatives",
        )
        alternatives = self._guidelines.document_alternative_guidance
        per_document = []
        for name in claim.documents_needed:
            if document and name != document:
                continue
            key = DOCUMENT_GUIDANCE_KEYS.get(name)
            entry = alternatives.get(key) if key else None
            per_document.append(
                self._fact(
                    claim,
                    f"alternative.{name.replace(' ', '_')}",
                    (entry or alternatives["default"]).en,
                    "required_document_guideline.json:document_alternative_guidance."
                    + (key if entry else "default"),
                )
            )
        if alternatives_given:
            human = self._guidelines.claim_followup_settings[
                "human_review_after_document_alternatives_exhausted"
            ]
            exhausted = self._fact(
                claim,
                "alternatives_exhausted",
                human.en,
                "required_document_guideline.json:claim_followup_settings."
                "human_review_after_document_alternatives_exhausted",
            )
            # The caller already tried the alternatives; repeating them would not help.
            return [exhausted, general]
        if document:
            return [*per_document, general]
        return [general, *per_document]

    def _fallback(self, claim: Claim) -> str:
        if claim.documents_needed:
            return self._guidelines.claim_followup_fallback.en
        return f"I don't have specific information about that for claim {claim.case_id}."

    def _fill(self, template: str, claim: Claim) -> str:
        values = {
            "case_id": claim.case_id,
            "documents": document_list(claim),
        }
        # Placeholders were checked against these keys when the fixtures loaded.
        values.update({key: text.en for key, text in self._guidelines.claim_followup_settings.items()})
        return template.format(**values)

    @staticmethod
    def _fact(claim: Claim, suffix: str, text: str, source: str) -> Fact:
        return Fact(
            id=f"claim.{claim.case_id}.{suffix}",
            text=text,
            source=source,
            case_id=claim.case_id,
            party_id=claim.party_id,
        )
