"""Check model proposals against the user's message, then remember them.

A proposal is accepted only when its evidence can be found in the current
message. Values that code can check are checked here: digits must appear in the
evidence, numeric dates are parsed by code (which also detects ambiguity), and
the stated ID document type comes from the user's own words, not from the model.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from .claims import normalize_case_hint
from .domain import (
    CallerRole,
    CaseHintField,
    DialogAct,
    EmailReply,
    EntryStatus,
    IdentityField,
    Ledger,
    LedgerEntry,
    LedgerSource,
    Scope,
    StatedIdType,
    Topic,
    TurnUnderstanding,
    UpdateOp,
)
from .identity import (
    Candidate,
    normalize_email,
    normalize_id_last4,
    normalize_phone,
    parse_iso_date,
)

EVIDENCE_NOT_FOUND = "EVIDENCE_NOT_FOUND"
VALUE_NOT_IN_EVIDENCE = "VALUE_NOT_IN_EVIDENCE"
INVALID_VALUE = "INVALID_VALUE"
AMBIGUOUS_DATE = "AMBIGUOUS_DATE"
NOTHING_TO_QUALIFY = "NOTHING_TO_QUALIFY"
INCOMPLETE_NAME = "INCOMPLETE_NAME"
POLICYHOLDER_ROLE = "POLICYHOLDER_ROLE"
NOT_WITHDRAWN = "NOT_WITHDRAWN"

_NUMERIC_DATE = re.compile(r"\b(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{1,4})\b")
_SSN_WORDS = re.compile(r"\b(ssn|social security|social)\b")
_NATIONAL_WORDS = re.compile(r"\bnational\b")


@dataclass(frozen=True)
class Rejection:
    kind: str
    field: str | None
    reason: str


@dataclass(frozen=True)
class IdentityProposal:
    field: IdentityField
    op: UpdateOp
    value: str | None
    id_type: StatedIdType | None = None


@dataclass
class ValidatedTurn:
    identity: list[IdentityProposal] = field(default_factory=list)
    case_hints: list[tuple[CaseHintField, UpdateOp, str | int | None]] = field(default_factory=list)
    questions: list[Topic] = field(default_factory=list)
    question_details: dict[Topic, str] = field(default_factory=dict)
    notes: list[tuple[str, str]] = field(default_factory=list)
    representative_name: str | None = None
    representative_relationship: str | None = None
    policy_number: str | None = None
    rejected: list[Rejection] = field(default_factory=list)
    ambiguous_dob: bool = False
    # A name was given, but only one word of it ("my mom Margaret"): not a full name.
    incomplete_name: bool = False


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def contains(haystack: str, needle: str) -> bool:
    """Word-boundary containment on normalized text."""
    needle = normalize_text(needle)
    return bool(needle) and f" {needle} " in f" {normalize_text(haystack)} "


def stated_id_type(evidence: str, message: str) -> StatedIdType:
    """Which document the user says the digits come from, judged from their words."""
    for text in (evidence, message):
        normalized = normalize_text(text)
        ssn = bool(_SSN_WORDS.search(normalized))
        national = bool(_NATIONAL_WORDS.search(normalized))
        if ssn and national:
            return StatedIdType.UNKNOWN
        if ssn:
            return StatedIdType.SSN_LAST4
        if national:
            return StatedIdType.NATIONAL_ID_LAST4
    return StatedIdType.UNKNOWN


def validate(understanding: TurnUnderstanding, message: str) -> ValidatedTurn:
    turn = ValidatedTurn()

    for update in understanding.identity_updates:
        if not contains(message, update.evidence):
            turn.rejected.append(Rejection("identity", update.field.value, EVIDENCE_NOT_FOUND))
            continue
        if update.op is UpdateOp.CLEAR:
            # Withdrawing a detail is a correction; a stray "clear" beside new details would drop a
            # field the caller never took back.
            if DialogAct.CORRECT not in understanding.dialog_acts:
                turn.rejected.append(Rejection("identity", update.field.value, NOT_WITHDRAWN))
                continue
            turn.identity.append(IdentityProposal(update.field, UpdateOp.CLEAR, None))
            continue
        value, reason = _identity_value(update.field, update.value, update.evidence)
        if reason == AMBIGUOUS_DATE:
            turn.ambiguous_dob = True
        if reason == INCOMPLETE_NAME:
            turn.incomplete_name = True
        if reason:
            turn.rejected.append(Rejection("identity", update.field.value, reason))
            continue
        id_type = stated_id_type(update.evidence, message) if update.field is IdentityField.ID_LAST4 else None
        if update.field is IdentityField.ID_LAST4 and value is None and id_type is StatedIdType.UNKNOWN:
            turn.rejected.append(Rejection("identity", update.field.value, EVIDENCE_NOT_FOUND))
            continue
        turn.identity.append(IdentityProposal(update.field, UpdateOp.SET, value, id_type))

    for hint in understanding.case_hint_updates:
        if not contains(message, hint.evidence):
            turn.rejected.append(Rejection("case_hint", hint.field.value, EVIDENCE_NOT_FOUND))
            continue
        if hint.op is UpdateOp.CLEAR:
            turn.case_hints.append((hint.field, UpdateOp.CLEAR, None))
            continue
        value = normalize_case_hint(hint.field, hint.value)
        if value is None:
            turn.rejected.append(Rejection("case_hint", hint.field.value, INVALID_VALUE))
            continue
        turn.case_hints.append((hint.field, UpdateOp.SET, value))

    # An off-topic message carries no claim questions worth remembering. In a mixed or refused
    # message, or one asking for the summary email, a question without a claim topic is that part
    # ("what's the capital of France?", "list every claim in the database", "can you email me this?"),
    # so it is not remembered either.
    if understanding.scope is Scope.OUT_OF_SCOPE:
        questions = []
    elif (
        understanding.scope is Scope.MIXED
        or understanding.safety_concerns
        or understanding.email_reply is EmailReply.SEND
    ):
        questions = [q for q in understanding.questions if q.topic is not Topic.OTHER]
    else:
        questions = understanding.questions
    for question in questions:
        if contains(message, question.evidence):
            turn.questions.append(question.topic)
            if question.detail:
                turn.question_details[question.topic] = question.detail.strip()
        else:
            turn.rejected.append(Rejection("question", question.topic.value, EVIDENCE_NOT_FOUND))

    for note in understanding.notes:
        if contains(message, note.evidence):
            turn.notes.append((note.text, note.evidence))
        else:
            turn.rejected.append(Rejection("note", None, EVIDENCE_NOT_FOUND))

    rep = understanding.representative
    if rep is not None:
        if not contains(message, rep.evidence):
            turn.rejected.append(Rejection("representative", None, EVIDENCE_NOT_FOUND))
        else:
            if rep.name and contains(rep.evidence, rep.name):
                turn.representative_name = " ".join(rep.name.split())
            elif rep.name:
                turn.rejected.append(Rejection("representative", "name", VALUE_NOT_IN_EVIDENCE))
            relationship = " ".join(rep.relationship.split()) if rep.relationship else None
            if relationship and contains(message, f"my {relationship}"):
                # "My mom's claim" says who the policyholder is, not how the caller is related to her.
                turn.rejected.append(Rejection("representative", "relationship", POLICYHOLDER_ROLE))
            elif relationship and contains(rep.evidence, relationship):
                turn.representative_relationship = relationship
            elif relationship:
                turn.rejected.append(Rejection("representative", "relationship", VALUE_NOT_IN_EVIDENCE))

    if understanding.policy_number:
        if contains(message, understanding.policy_number):
            turn.policy_number = understanding.policy_number.strip().upper()
        else:
            turn.rejected.append(Rejection("policy_number", None, EVIDENCE_NOT_FOUND))

    return turn


def _identity_value(field_: IdentityField, value: str | None, evidence: str) -> tuple[str | None, str | None]:
    """Return (normalized value, rejection reason)."""
    if field_ is IdentityField.ID_LAST4 and not value:
        return None, None  # a type-only answer; the type is read from the evidence
    if value is None:
        return None, INVALID_VALUE
    evidence_digits = re.sub(r"\D", "", evidence)
    if field_ is IdentityField.FULL_NAME:
        if not contains(evidence, value):
            return None, VALUE_NOT_IN_EVIDENCE
        name = " ".join(value.split())
        # A first name alone can't match a record, and counting it as a failed attempt would be unfair.
        return (name, None) if len(normalize_text(name).split()) >= 2 else (None, INCOMPLETE_NAME)
    if field_ is IdentityField.EMAIL:
        email = normalize_email(value)
        return (email, None) if contains(evidence, email) else (None, VALUE_NOT_IN_EVIDENCE)
    if field_ is IdentityField.PHONE:
        phone = normalize_phone(value)
        if phone is None:
            return None, INVALID_VALUE
        return (phone, None) if phone[-10:] in evidence_digits else (None, VALUE_NOT_IN_EVIDENCE)
    if field_ is IdentityField.ID_LAST4:
        digits = normalize_id_last4(value)
        if digits is None:
            return None, INVALID_VALUE
        return (digits, None) if digits in evidence_digits else (None, VALUE_NOT_IN_EVIDENCE)
    return _dob_value(value, evidence)


def _dob_value(value: str, evidence: str) -> tuple[str | None, str | None]:
    match = _NUMERIC_DATE.search(evidence)
    if match:
        first, second, third = match.groups()
        if len(first) == 4:
            parsed = _plausible_dob(int(first), int(second), int(third))
        else:
            year = _expand_year(third)
            a, b = int(first), int(second)
            if year is None:
                return None, INVALID_VALUE
            if a <= 12 and b <= 12 and a != b:
                return None, AMBIGUOUS_DATE
            month, day = (a, b) if a <= 12 else (b, a)
            parsed = _plausible_dob(year, month, day)
        return (parsed.isoformat(), None) if parsed else (None, INVALID_VALUE)
    # A written-out date ("March 15, 1985"): accept the model's ISO value, checking the year if present.
    parsed = parse_iso_date(value)
    if parsed is None or _plausible_dob(parsed.year, parsed.month, parsed.day) is None:
        return None, INVALID_VALUE
    years = re.findall(r"\b(?:19|20)\d\d\b", evidence)
    if years and str(parsed.year) not in years:
        return None, VALUE_NOT_IN_EVIDENCE
    return parsed.isoformat(), None


def _expand_year(text: str) -> int | None:
    if len(text) == 4:
        return int(text)
    if len(text) == 2:
        pivot = date.today().year % 100
        return 2000 + int(text) if int(text) <= pivot else 1900 + int(text)
    return None


def _plausible_dob(year: int, month: int, day: int) -> date | None:
    try:
        parsed = date(year, month, day)
    except ValueError:
        return None
    return parsed if 1900 <= parsed.year and parsed < date.today() else None


# --- Writing to the ledger ----------------------------------------------------------


def record_identity(ledger: Ledger, proposals: list[IdentityProposal], turn: int) -> set[IdentityField]:
    """Apply identity proposals; return the fields whose stored value changed."""
    changed: set[IdentityField] = set()
    for proposal in proposals:
        key = f"identity.{proposal.field.value}"
        existing = ledger.get(key)
        if proposal.op is UpdateOp.CLEAR:
            if ledger.remove(key):
                changed.add(proposal.field)
            continue
        if proposal.value is None:
            # "It's my national ID": qualify the digits given earlier.
            if existing is not None and existing.qualifier != proposal.id_type:
                existing.qualifier = proposal.id_type
                changed.add(proposal.field)
            continue
        qualifier = proposal.id_type.value if proposal.id_type else None
        unstated = (None, StatedIdType.UNKNOWN.value)
        if qualifier in unstated and existing is not None and existing.qualifier not in unstated:
            # "Sorry, I meant 4471": new digits for the document the caller already named.
            qualifier = existing.qualifier
        if existing and existing.value == proposal.value and existing.qualifier == qualifier:
            continue
        ledger.put(
            LedgerEntry(
                key=key, value=proposal.value, source=LedgerSource.USER_SAID, turn=turn, qualifier=qualifier
            )
        )
        changed.add(proposal.field)
    return changed


def record_turn_details(
    ledger: Ledger, validated: ValidatedTurn, understanding: TurnUnderstanding, turn: int
) -> None:
    """Remember everything except identity values, whatever the current phase."""
    for hint, op, value in validated.case_hints:
        key = f"case_hint.{hint.value}"
        if op is UpdateOp.CLEAR:
            ledger.remove(key)
        else:
            ledger.put(LedgerEntry(key=key, value=value, source=LedgerSource.USER_SAID, turn=turn))
    for topic in validated.questions:
        ledger.put(
            LedgerEntry(
                key=f"question.{topic.value}",
                value=topic.value,
                source=LedgerSource.USER_SAID,
                turn=turn,
                qualifier=validated.question_details.get(topic),
                status=EntryStatus.OPEN,
            )
        )
    for text, evidence in validated.notes:
        index = len(ledger.with_prefix("note.")) + 1
        ledger.put(
            LedgerEntry(
                key=f"note.{index}", value=text, source=LedgerSource.USER_SAID, turn=turn, evidence=evidence
            )
        )
    if validated.representative_name:
        ledger.put(
            LedgerEntry(
                key="caller.rep_name",
                value=validated.representative_name,
                source=LedgerSource.USER_SAID,
                turn=turn,
            )
        )
    if validated.representative_relationship:
        ledger.put(
            LedgerEntry(
                key="caller.rep_relationship",
                value=validated.representative_relationship,
                source=LedgerSource.USER_SAID,
                turn=turn,
            )
        )
    if understanding.caller_role is not CallerRole.UNKNOWN:
        ledger.put(
            LedgerEntry(
                key="caller.role",
                value=understanding.caller_role.value,
                source=LedgerSource.USER_SAID,
                turn=turn,
            )
        )
    if validated.policy_number:
        ledger.put(
            LedgerEntry(
                key="account.policy_number",
                value=validated.policy_number,
                source=LedgerSource.USER_SAID,
                turn=turn,
            )
        )


def identity_candidates(ledger: Ledger) -> list[Candidate]:
    candidates = []
    for entry in ledger.with_prefix("identity."):
        field_ = IdentityField(entry.key.removeprefix("identity."))
        id_type = StatedIdType(entry.qualifier) if entry.qualifier else None
        candidates.append(Candidate(field_, str(entry.value), id_type))
    return candidates


def case_hints(ledger: Ledger) -> dict[CaseHintField, str | int]:
    return {
        CaseHintField(entry.key.removeprefix("case_hint.")): entry.value
        for entry in ledger.with_prefix("case_hint.")
        if entry.value is not None
    }


def question_details(ledger: Ledger) -> dict[Topic, str]:
    """What each open question was specifically about, when the caller said."""
    return {
        Topic(entry.value): entry.qualifier
        for entry in ledger.with_prefix("question.")
        if entry.status is EntryStatus.OPEN and entry.qualifier
    }


def open_questions(ledger: Ledger) -> list[Topic]:
    return [
        Topic(entry.value) for entry in ledger.with_prefix("question.") if entry.status is EntryStatus.OPEN
    ]
