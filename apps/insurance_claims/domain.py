"""Domain enums, the model-facing contracts, and session/API models."""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .llm.providers import PROVIDERS

_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}")

MAX_MESSAGE_CHARS = 4000
IDENTITY_FIELDS_REQUIRED = 3


class Phase(StrEnum):
    VERIFY_ID = "VERIFY_ID"
    RESOLVE_INTENT = "RESOLVE_INTENT"
    PROCESS_CASE = "PROCESS_CASE"
    POST_PROCESS = "POST_PROCESS"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    HANDED_OFF = "handed_off"
    CANCELLED = "cancelled"
    # The SOP finished and the caller had nothing more; the conversation takes no more messages.
    ENDED = "ended"


class DateMode(StrEnum):
    DEMO = "demo"
    REAL = "real"


class Scope(StrEnum):
    IN_SCOPE = "in_scope"
    OUT_OF_SCOPE = "out_of_scope"
    MIXED = "mixed"
    UNCLEAR = "unclear"


class DialogAct(StrEnum):
    ASK_QUESTION = "ask_question"
    PROVIDE_INFORMATION = "provide_information"
    CORRECT = "correct"
    REFUSE = "refuse"
    AGREE = "agree"
    DENY = "deny"
    REQUEST_HUMAN = "request_human"
    END = "end"


class CallerRole(StrEnum):
    SELF = "self"
    REPRESENTATIVE = "representative"
    UNKNOWN = "unknown"


class IdentityField(StrEnum):
    FULL_NAME = "full_name"
    DOB = "dob"
    PHONE = "phone"
    EMAIL = "email"
    ID_LAST4 = "id_last4"


class IdType(StrEnum):
    """Identity document types that appear on policyholder records."""

    SSN_LAST4 = "ssn_last4"
    NATIONAL_ID_LAST4 = "national_id_last4"


class StatedIdType(StrEnum):
    """The document type a caller says they are giving."""

    SSN_LAST4 = "ssn_last4"
    NATIONAL_ID_LAST4 = "national_id_last4"
    UNKNOWN = "unknown"


class UpdateOp(StrEnum):
    SET = "set"
    CLEAR = "clear"


class CaseHintField(StrEnum):
    CASE_ID = "case_id"
    CASE_TYPE = "case_type"
    STATUS = "status"
    MONTH = "month"
    YEAR = "year"


class Intent(StrEnum):
    STATUS_INQUIRY = "status_inquiry"
    DENIAL_QUESTION = "denial_question"
    DOCUMENT_SUBMISSION = "document_submission"
    NEXT_STEPS = "next_steps"
    PAYMENT_QUESTION = "payment_question"
    GENERAL_CLAIM_QUESTION = "general_claim_question"
    UNKNOWN = "unknown"


class Topic(StrEnum):
    """Answerable topics: claim fields plus the follow-up guidance topics."""

    STATUS = "status"
    DENIAL_REASON = "denial_reason"
    DOCUMENTS_NEEDED = "documents_needed"
    APPEAL_DEADLINE = "appeal_deadline"
    AMOUNTS = "amounts"
    MISSING_REQUIRED_MATERIAL_ALTERNATIVES = "missing_required_material_alternatives"
    SUBMISSION_TIMING = "submission_timing"
    PROCESSING_TIME_AFTER_SUBMISSION = "processing_time_after_submission"
    SUBMISSION_METHOD = "submission_method"
    FILE_FORMAT_REQUIREMENTS = "file_format_requirements"
    RECEIPT_CONFIRMATION = "receipt_confirmation"
    OTHER = "other"


class EmotionLabel(StrEnum):
    NEUTRAL = "neutral"
    FRUSTRATED = "frustrated"
    ANGRY = "angry"
    ANXIOUS = "anxious"
    CONFUSED = "confused"
    REFUSING = "refusing"


class Intensity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EmailReply(StrEnum):
    NONE = "none"
    SEND = "send"
    SKIP = "skip"
    UNCLEAR = "unclear"


class DeliveryStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    SIMULATED_SENT = "simulated_sent"
    FAILED = "failed"
    UNKNOWN = "unknown"


class EmailChoice(StrEnum):
    UNDECIDED = "undecided"
    SEND = "send"
    SKIP = "skip"


class SafetyConcern(StrEnum):
    """Requests the SOP must refuse, whatever the phase."""

    OTHER_PERSON_DATA = "other_person_data"
    BYPASS_VERIFICATION = "bypass_verification"
    INSTRUCTION_OVERRIDE = "instruction_override"


class ProcessTopic(StrEnum):
    """Questions about the process itself, which are in scope."""

    WHY_VERIFICATION = "why_verification"
    WHY_CONSENT = "why_consent"
    ACCEPTED_DETAILS = "accepted_details"
    PRIVACY = "privacy"
    CAPABILITIES = "capabilities"


class Planning(StrEnum):
    STRICT = "strict"
    GROUNDED = "grounded"


class Offer(StrEnum):
    HANDOFF = "handoff"
    ALTERNATIVE_FIELDS = "alternative_fields"
    EMAIL_SUMMARY = "email_summary"
    RETRY_LATER = "retry_later"
    POLICYHOLDER_DIRECT = "policyholder_direct"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class Resolution(StrEnum):
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"
    SELECTED = "selected"
    NO_MATCH = "no_match"
    NO_CLAIMS = "no_claims"


class LedgerSource(StrEnum):
    USER_SAID = "user_said"
    TOOL = "tool"
    AGENT_SAID = "agent_said"


class EntryStatus(StrEnum):
    ACTIVE = "active"
    OPEN = "open"
    ANSWERED = "answered"


class ConsentStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    APPROVED = "approved"
    TIMEOUT = "timeout"


class PendingKind(StrEnum):
    ID_TYPE = "id_type"
    DOB_FORMAT = "dob_format"
    REPRESENTATIVE = "representative"
    CHOOSE_CASE = "choose_case"
    CONFIRM_CASE = "confirm_case"
    ANYTHING_ELSE = "anything_else"
    EMAIL_CONSENT = "email_consent"


# --- Model-facing contracts -------------------------------------------------


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IdentityUpdate(_Contract):
    field: IdentityField
    op: UpdateOp = UpdateOp.SET
    value: str | None = None
    id_type: StatedIdType | None = None
    evidence: str

    @model_validator(mode="after")
    def _check_shape(self) -> IdentityUpdate:
        if self.field is IdentityField.ID_LAST4:
            # A type-only update answers "Is that your SSN or your national ID?".
            type_only = self.id_type in (StatedIdType.SSN_LAST4, StatedIdType.NATIONAL_ID_LAST4)
            if self.op is UpdateOp.SET and not self.value and not type_only:
                raise ValueError("an id_last4 update needs digits or a stated id_type")
            if self.op is UpdateOp.SET and self.value and self.id_type is None:
                self.id_type = StatedIdType.UNKNOWN
        else:
            if self.op is UpdateOp.SET and not self.value:
                raise ValueError("a set update needs a value")
            if self.id_type is not None:
                raise ValueError("id_type only applies to id_last4")
        return self


class CaseHintUpdate(_Contract):
    field: CaseHintField
    op: UpdateOp = UpdateOp.SET
    value: str | int | None = None
    evidence: str


class RepresentativeInfo(_Contract):
    name: str | None = None
    relationship: str | None = None
    evidence: str


class QuestionItem(_Contract):
    topic: Topic
    # The specific item asked about, such as a document name, when there is one.
    detail: str | None = None
    evidence: str


class NoteItem(_Contract):
    text: str
    evidence: str


class EmotionReading(_Contract):
    label: EmotionLabel = EmotionLabel.NEUTRAL
    intensity: Intensity = Intensity.LOW
    # Set when acknowledging: which wording to use, so a repeated acknowledgement isn't word for word.
    variant: int = Field(default=0, ge=0)


class TurnUnderstanding(_Contract):
    """What the understanding step proposes for one user message.

    It is a proposal only: the controller validates it before anything reaches
    session state.
    """

    scope: Scope = Scope.UNCLEAR
    dialog_acts: list[DialogAct] = Field(default_factory=list)
    caller_role: CallerRole = CallerRole.UNKNOWN
    representative: RepresentativeInfo | None = None
    identity_updates: list[IdentityUpdate] = Field(default_factory=list)
    policy_number: str | None = None
    intent: Intent = Intent.UNKNOWN
    case_hint_updates: list[CaseHintUpdate] = Field(default_factory=list)
    questions: list[QuestionItem] = Field(default_factory=list)
    notes: list[NoteItem] = Field(default_factory=list)
    email_reply: EmailReply = EmailReply.NONE
    emotion: EmotionReading = Field(default_factory=EmotionReading)
    safety_concerns: list[SafetyConcern] = Field(default_factory=list)
    process_question: ProcessTopic | None = None
    # Identity details the caller won't or can't give; they only narrow what is asked next.
    withheld_fields: list[IdentityField] = Field(default_factory=list)


class PlanAsk(_Contract):
    slot: str
    one_of: list[str] = Field(default_factory=list)
    count: int = Field(default=1, ge=1)
    # Which wording to use for a question asked again and again, so it doesn't read as scripted.
    variant: int = Field(default=0, ge=0)


class ReplyPlan(_Contract):
    """What this turn's reply must say; the model only decides how."""

    planning: Planning = Planning.STRICT
    acknowledge: EmotionReading | None = None
    # The caller's mood this turn, which shapes the style even when it was already acknowledged.
    tone: EmotionLabel | None = None
    decline: list[str] = Field(default_factory=list)
    inform: list[str] = Field(default_factory=list)
    ask: PlanAsk | None = None
    offer: list[Offer] = Field(default_factory=list)
    next: str | None = None
    resume: str | None = None
    answer_topics: list[Topic] = Field(default_factory=list)
    # Facts the reply may use if they help with the caller's message; the plan does not require them.
    available: list[str] = Field(default_factory=list)


class ResponseDraft(_Contract):
    text: str
    used_fact_ids: list[str] = Field(default_factory=list)


# --- Session state ------------------------------------------------------------


class ChatMessage(BaseModel):
    id: str
    role: MessageRole
    text: str
    turn: int
    case_cycle_id: int
    # True when the message carries claim facts; such messages are kept out of
    # model context whenever the current phase may not see claim data.
    protected: bool = False


class TraceEvent(BaseModel):
    """A redacted trace entry: event, rule code, tool and status only."""

    turn: int
    event: str
    status: str | None = None
    rule: str | None = None
    tool: str | None = None
    phase: Phase | None = None


class PlanRecord(BaseModel):
    """The reply plan of one turn, kept so the SOP console can show every turn, not just the last."""

    turn: int
    plan: ReplyPlan


class LedgerEntry(BaseModel):
    """One remembered item, with where it came from."""

    key: str
    value: str | int | None = None
    source: LedgerSource
    turn: int
    evidence: str | None = None
    # A refinement of the value, such as the stated document type of identity.id_last4.
    qualifier: str | None = None
    status: EntryStatus = EntryStatus.ACTIVE


class Ledger(BaseModel):
    entries: list[LedgerEntry] = Field(default_factory=list)

    def get(self, key: str) -> LedgerEntry | None:
        return next((entry for entry in self.entries if entry.key == key), None)

    def put(self, entry: LedgerEntry) -> None:
        """Store ``entry``, replacing any entry with the same key (a correction replaces)."""
        self.remove(entry.key)
        self.entries.append(entry)

    def remove(self, key: str) -> bool:
        before = len(self.entries)
        self.entries = [entry for entry in self.entries if entry.key != key]
        return len(self.entries) != before

    def with_prefix(self, prefix: str) -> list[LedgerEntry]:
        return [entry for entry in self.entries if entry.key.startswith(prefix)]


class IdentityState(BaseModel):
    """Verifier-owned state. Match details never leave the server."""

    verified: bool = False
    party_id: str | None = None
    verified_turn: int | None = None
    failed_attempts: int = 0
    locked: bool = False
    no_progress: int = 0
    # Digest of the candidate set last evaluated, so an unchanged set is not re-counted.
    evaluated_digest: str | None = None
    # Details the caller declined to share; they are left out of later asks while others remain.
    withheld: list[IdentityField] = Field(default_factory=list)


class CallerState(BaseModel):
    role: CallerRole = CallerRole.UNKNOWN
    representative_confirmed: bool | None = None
    consent_status: ConsentStatus = ConsentStatus.NOT_REQUESTED
    consent_polls: int = 0


class CaseContext(BaseModel):
    intent: Intent | None = None
    resolution: Resolution = Resolution.UNRESOLVED
    selected_case_id: str | None = None
    candidate_ids: list[str] = Field(default_factory=list)
    relaxed: list[CaseHintField] = Field(default_factory=list)
    # Topics answered in this case cycle, and whether document alternatives were offered (8.2).
    answered: list[Topic] = Field(default_factory=list)
    alternatives_given: bool = False


class PendingQuestion(BaseModel):
    kind: PendingKind
    asked_turn: int
    candidates: list[str] = Field(default_factory=list)


class Fact(BaseModel):
    """A statement the reply may use, with its source."""

    id: str
    text: str
    source: str
    case_id: str | None = None
    party_id: str | None = None


class Summary(BaseModel):
    revision: int
    case_id: str | None
    discussed: list[str]
    outcome: str
    next_steps: list[str]
    # Follow-up topics beyond the outcome and next steps, such as file formats or payments.
    also_covered: list[str] = Field(default_factory=list)
    # Claims discussed earlier in this conversation, before the current one.
    earlier_claims: list[str] = Field(default_factory=list)
    recipient: str
    subject: str
    body: str


class EmailState(BaseModel):
    choice: EmailChoice = EmailChoice.UNDECIDED
    status: DeliveryStatus = DeliveryStatus.NOT_REQUESTED
    sent_revisions: list[int] = Field(default_factory=list)
    # Summary revisions are numbered across the whole session, so a new case cycle never reuses one.
    last_revision: int = 0


class HandoffRecord(BaseModel):
    """A simulated transfer to a human; no real person is contacted."""

    reason: str
    turn: int
    verified: bool
    case_id: str | None = None


class Counters(BaseModel):
    off_topic_total: int = 0
    persuasion_attempts: dict[str, int] = Field(default_factory=dict)
    # The turn in which a human was last offered, so a "yes" next turn can accept it.
    handoff_offered_turn: int | None = None
    # How often each repeated question was asked, to vary its wording.
    asked: dict[str, int] = Field(default_factory=dict)
    # The caller's mood last turn, so a lasting mood is acknowledged once rather than every turn.
    last_emotion: EmotionReading | None = None
    last_emotion_turn: int | None = None


class OutboxEmail(BaseModel):
    id: str
    to: str
    subject: str
    body: str
    status: DeliveryStatus


class SessionState(BaseModel):
    """Server-side authoritative state for one conversation."""

    session_id: str
    date_mode: DateMode
    consent_scenario: str
    phase: Phase = Phase.VERIFY_ID
    subflow_step: str | None = None
    status: SessionStatus = SessionStatus.ACTIVE
    turn_index: int = 0
    case_cycle_id: int = 1
    caller: CallerState = Field(default_factory=CallerState)
    identity: IdentityState = Field(default_factory=IdentityState)
    ledger: Ledger = Field(default_factory=Ledger)
    case_context: CaseContext = Field(default_factory=CaseContext)
    fact_bundle: dict[str, Fact] = Field(default_factory=dict)
    pending_question: PendingQuestion | None = None
    summary: Summary | None = None
    # Short descriptions of claims from earlier case cycles, so the summary still mentions them.
    earlier_claims: list[str] = Field(default_factory=list)
    email: EmailState = Field(default_factory=EmailState)
    handoff: HandoffRecord | None = None
    counters: Counters = Field(default_factory=Counters)
    outbox: list[OutboxEmail] = Field(default_factory=list)
    messages: list[ChatMessage] = Field(default_factory=list)
    trace: list[TraceEvent] = Field(default_factory=list)
    plans: list[PlanRecord] = Field(default_factory=list)

    @property
    def access_granted(self) -> bool:
        """Whether claim data may be used: identity verified, plus consent for a representative."""
        if not self.identity.verified:
            return False
        if self.caller.role is CallerRole.REPRESENTATIVE:
            return (
                self.caller.representative_confirmed is True
                and self.caller.consent_status is ConsentStatus.APPROVED
            )
        return True

    def identity_fields(self) -> list[IdentityField]:
        """Distinct identity fields the caller has provided (not the ones that matched)."""
        provided = {entry.key.removeprefix("identity.") for entry in self.ledger.with_prefix("identity.")}
        return [f for f in IdentityField if f.value in provided]


# --- API models ---------------------------------------------------------------


class CreateSessionRequest(_Contract):
    date_mode: DateMode = DateMode.DEMO
    consent_scenario: str | None = None


class MessageRequest(_Contract):
    message: str = Field(max_length=MAX_MESSAGE_CHARS)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be empty")
        return value


class ApiKeyRequest(_Contract):
    provider: str = "openai"
    # The provider's default model when not given (llm/providers.py).
    model: str | None = None
    # SecretStr keeps the key out of reprs and logs.
    api_key: SecretStr

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        if value not in PROVIDERS:
            raise ValueError(f"must be one of {', '.join(PROVIDERS)}")
        return value

    @field_validator("model")
    @classmethod
    def _model_name(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        if value and (not _MODEL_NAME.fullmatch(value) or ".." in value):
            raise ValueError("this doesn't look like a model name")
        return value or None

    @field_validator("api_key")
    @classmethod
    def _plausible(cls, value: SecretStr) -> SecretStr:
        # The messages never include the value.
        key = value.get_secret_value().strip()
        if not 20 <= len(key) <= 300 or any(c.isspace() for c in key):
            raise ValueError("this doesn't look like an API key")
        return SecretStr(key)


class ProviderOut(BaseModel):
    id: str
    label: str
    default_model: str
    # Suggestions for the console's Model field; other names can be typed in.
    models: list[str] = []


class ModelOut(BaseModel):
    """Which model replies in this browser, and whose key it uses. Never the key itself."""

    source: str  # "yours" (entered in the console), "server" (AI_API_KEY) or "none" (the stand-in)
    real_model: bool
    # For "none", the provider and model a key would be used with.
    provider: str
    provider_label: str
    model: str | None
    key_hint: str | None = None
    # The providers a key can be entered for.
    providers: list[ProviderOut] = Field(default_factory=list)


class GateView(BaseModel):
    """One SOP gate as the inspector shows it, without any match details."""

    name: str
    state: str  # "passed", "waiting", "blocked" or "not_needed"
    detail: str


class MemoryView(BaseModel):
    """One remembered item. Identity values are never shown, only that they were given."""

    label: str
    value: str
    source: str  # "caller" (what the caller said) or "records" (verified claim data)


class SessionView(BaseModel):
    """The redacted projection of session state that the browser may see."""

    phase: Phase
    subflow: str | None
    status: SessionStatus
    verified: bool
    identity_fields_provided: int
    identity_fields_required: int = IDENTITY_FIELDS_REQUIRED
    case_cycle_id: int
    selected_case_id: str | None
    intent: Intent | None
    email_status: DeliveryStatus
    as_of_date: date
    date_mode: DateMode
    consent_scenario: str
    awaiting: str | None = None
    gates: list[GateView] = Field(default_factory=list)
    memory: list[MemoryView] = Field(default_factory=list)


class MessageOut(BaseModel):
    id: str
    role: MessageRole
    text: str


class SessionOut(BaseModel):
    session: SessionView
    messages: list[MessageOut]


class CreateSessionOut(BaseModel):
    reply: MessageOut
    session: SessionView


class TurnOut(BaseModel):
    reply: MessageOut
    session: SessionView
    plan: ReplyPlan
    trace: list[TraceEvent]


class OutboxOut(BaseModel):
    simulated: bool = True
    emails: list[OutboxEmail]


class ConsoleTurn(BaseModel):
    """One turn as the SOP console shows it. Turn 0 is the welcome, which has no caller message."""

    turn: int
    caller: MessageOut | None = None
    reply: MessageOut | None = None
    plan: ReplyPlan | None = None
    trace: list[TraceEvent] = Field(default_factory=list)


class ConsoleOut(BaseModel):
    session: SessionView
    turns: list[ConsoleTurn]
    outbox: OutboxOut
