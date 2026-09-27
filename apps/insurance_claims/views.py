"""The redacted session view for the browser, including the inspector's gates and memory.

Nothing here reveals match details or identity values: the
view says which identity fields were given, never what they were or whether
they matched. Claim data appears only once access is granted.
"""

from __future__ import annotations

import calendar
from datetime import date

from .domain import (
    IDENTITY_FIELDS_REQUIRED,
    CallerRole,
    ChatMessage,
    ConsentStatus,
    ConsoleTurn,
    DeliveryStatus,
    EmailChoice,
    EntryStatus,
    GateView,
    IdentityField,
    MemoryView,
    MessageOut,
    MessageRole,
    PendingKind,
    Resolution,
    SessionState,
    SessionView,
    Topic,
)
from .processing import TOPIC_LABELS

_HINT_LABELS = {
    "case_id": "Claim number",
    "case_type": "Claim type",
    "status": "Claim status",
    "month": "Claim month",
    "year": "Claim year",
}

_IDENTITY_LABELS = {
    IdentityField.FULL_NAME: "Full name",
    IdentityField.DOB: "Date of birth",
    IdentityField.PHONE: "Phone",
    IdentityField.EMAIL: "Email",
    IdentityField.ID_LAST4: "ID last 4",
}


def session_view(state: SessionState, as_of: date) -> SessionView:
    granted = state.access_granted
    return SessionView(
        phase=state.phase,
        subflow=state.subflow_step,
        status=state.status,
        verified=granted,
        identity_fields_provided=len(state.identity_fields()),
        case_cycle_id=state.case_cycle_id,
        selected_case_id=state.case_context.selected_case_id if granted else None,
        intent=state.case_context.intent,
        email_status=state.email.status,
        as_of_date=as_of,
        date_mode=state.date_mode,
        consent_scenario=state.consent_scenario,
        awaiting=state.pending_question.kind.value if state.pending_question else None,
        gates=_gates(state),
        memory=_memory(state),
    )


def message_out(message: ChatMessage) -> MessageOut:
    return MessageOut(id=message.id, role=message.role, text=message.text)


def console_turns(state: SessionState) -> list[ConsoleTurn]:
    """Every turn so far, oldest first: the messages, the reply plan and the trace.

    The messages are this session's own transcript, which the chat page shows as well.
    """
    turns: dict[int, ConsoleTurn] = {}

    def at(turn: int) -> ConsoleTurn:
        return turns.setdefault(turn, ConsoleTurn(turn=turn))

    for message in state.messages:
        if message.role is MessageRole.USER:
            at(message.turn).caller = message_out(message)
        else:
            at(message.turn).reply = message_out(message)
    for record in state.plans:
        at(record.turn).plan = record.plan
    for event in state.trace:
        at(event.turn).trace.append(event)
    return [turns[turn] for turn in sorted(turns)]


def _gates(state: SessionState) -> list[GateView]:
    identity = state.identity
    provided = len(state.identity_fields())
    if identity.locked:
        verify = GateView(name="Identity", state="blocked", detail="Locked after repeated failed attempts")
    elif identity.verified:
        verify = GateView(name="Identity", state="passed", detail="Verified")
    else:
        verify = GateView(
            name="Identity",
            state="waiting",
            detail=f"{provided} of {IDENTITY_FIELDS_REQUIRED} details provided",
        )
    gates = [verify]

    if state.caller.role is CallerRole.REPRESENTATIVE:
        confirmed = state.caller.representative_confirmed
        gates.append(
            GateView(
                name="Representative",
                state={True: "passed", False: "blocked", None: "waiting"}[confirmed],
                detail={True: "Registered for this policy", False: "Not confirmed", None: "Pending"}[
                    confirmed
                ],
            )
        )
        consent = state.caller.consent_status
        gates.append(
            GateView(
                name="Policyholder consent",
                state={
                    ConsentStatus.APPROVED: "passed",
                    ConsentStatus.TIMEOUT: "blocked",
                    ConsentStatus.NOT_REQUESTED: "waiting",
                }[consent],
                detail={
                    ConsentStatus.APPROVED: f"Approved after {state.caller.consent_polls} checks",
                    ConsentStatus.TIMEOUT: "No response in time",
                    ConsentStatus.NOT_REQUESTED: "Requested after verification",
                }[consent],
            )
        )

    case = state.case_context
    if not state.access_granted:
        claim = GateView(name="Claim", state="waiting", detail="Locked until access is granted")
    elif case.resolution is Resolution.SELECTED:
        claim = GateView(name="Claim", state="passed", detail=f"{case.selected_case_id} selected")
    elif case.resolution is Resolution.NO_CLAIMS:
        claim = GateView(name="Claim", state="passed", detail="No claims on the account")
    else:
        claim = GateView(name="Claim", state="waiting", detail="Choosing which claim")
    gates.append(claim)

    email = state.email
    if email.status is DeliveryStatus.SIMULATED_SENT:
        gates.append(GateView(name="Email summary", state="passed", detail="Sent to the demo outbox"))
    elif email.choice is EmailChoice.SKIP:
        gates.append(GateView(name="Email summary", state="passed", detail="Skipped by the caller"))
    elif email.status is DeliveryStatus.FAILED:
        gates.append(GateView(name="Email summary", state="blocked", detail="Sending failed"))
    elif state.pending_question and state.pending_question.kind is PendingKind.EMAIL_CONSENT:
        gates.append(GateView(name="Email summary", state="waiting", detail="Offered; waiting for consent"))
    else:
        gates.append(GateView(name="Email summary", state="not_needed", detail="Offered at the end"))
    return gates


def _memory(state: SessionState) -> list[MemoryView]:
    items: list[MemoryView] = []
    ledger = state.ledger
    for field in state.identity_fields():
        items.append(MemoryView(label=_IDENTITY_LABELS[field], value="provided", source="caller"))
    if state.identity.withheld and not state.identity.verified:
        declined = ", ".join(_IDENTITY_LABELS[f] for f in state.identity.withheld)
        items.append(MemoryView(label="Prefers not to share", value=declined, source="caller"))
    if (role := ledger.get("caller.role")) and role.value:
        items.append(MemoryView(label="Calling as", value=str(role.value), source="caller"))
    for key, label in (("caller.rep_name", "Representative"), ("caller.rep_relationship", "Relationship")):
        if entry := ledger.get(key):
            items.append(MemoryView(label=label, value=str(entry.value), source="caller"))
    for entry in ledger.with_prefix("case_hint."):
        hint = entry.key.removeprefix("case_hint.")
        value = calendar.month_name[int(entry.value)] if hint == "month" else str(entry.value)
        items.append(MemoryView(label=_HINT_LABELS.get(hint, hint), value=value, source="caller"))
    for entry in ledger.with_prefix("question."):
        topic = Topic(entry.value)
        status = "answered" if entry.status is EntryStatus.ANSWERED else "open"
        items.append(MemoryView(label="Question", value=f"{TOPIC_LABELS[topic]} ({status})", source="caller"))
    for entry in ledger.with_prefix("note."):
        items.append(MemoryView(label="Note", value=str(entry.value), source="caller"))
    if state.case_context.intent:
        items.append(
            MemoryView(
                label="Reason for calling",
                value=state.case_context.intent.value.replace("_", " "),
                source="caller",
            )
        )

    if state.access_granted:
        for fact in state.fact_bundle.values():
            if fact.id.endswith(".brief"):
                items.append(MemoryView(label="Claim", value=fact.text, source="records"))
        answered = [TOPIC_LABELS[t] for t in state.case_context.answered if t in TOPIC_LABELS]
        if answered:
            items.append(
                MemoryView(label="Answered from records", value=", ".join(answered), source="records")
            )
    return items
