"""Declarative SOP: what each phase may see, propose and call, and when it may end.

This is where "different steps need different levels of freedom" lives (design
doc section 4). The engine builds model context from ``visible``, tools check
``tools`` before running, and the controller leaves a phase only when its gate
passes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from .domain import CallerRole, ConsentStatus, IdentityField, Phase, Planning, Resolution, SessionState


class Ctx(StrEnum):
    """Categories of information a phase may put in front of the model."""

    MESSAGE = "message"
    RECENT_MESSAGES = "recent_messages"
    PROVIDED_FIELDS = "provided_fields"
    POLICY_TEXT = "policy_text"
    LEDGER_HINTS = "ledger_hints"
    CLAIM_CANDIDATES = "claim_candidates"
    CASE_FACTS = "case_facts"
    GUIDANCE = "guidance"
    SUMMARY = "summary"


class Act(StrEnum):
    """Kinds of model proposals a phase acts on. Everything is still remembered."""

    IDENTITY = "identity"
    DIALOG = "dialog"
    CASE_HINTS = "case_hints"
    INTENT = "intent"
    CHOOSE_CASE = "choose_case"
    TOPICS = "topics"
    HANDOFF = "handoff"
    EMAIL_CHOICE = "email_choice"


class Tool(StrEnum):
    VERIFY_IDENTITY = "verify_identity"
    CHECK_REPRESENTATIVE = "check_representative"
    REQUEST_CONSENT = "request_consent"
    FIND_CLAIMS = "find_claims"
    GET_CLAIM_DETAILS = "get_claim_details"
    GET_DOCUMENT_GUIDANCE = "get_document_guidance"
    PREPARE_SUMMARY = "prepare_summary"
    SEND_SUMMARY = "send_summary"
    CREATE_HANDOFF = "create_handoff"


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class PhaseSpec:
    phase: Phase
    visible: frozenset[Ctx]
    proposals: frozenset[Act]
    tools: frozenset[Tool]
    planning: Planning
    gate: Callable[[SessionState], GateResult]
    subflows: tuple[str, ...] = ()

    @property
    def sees_claim_data(self) -> bool:
        return bool(self.visible & {Ctx.CLAIM_CANDIDATES, Ctx.CASE_FACTS})


def verify_gate(state: SessionState) -> GateResult:
    if state.access_granted:
        return GateResult(True, "ACCESS_GRANTED")
    if state.identity.locked:
        return GateResult(False, "VERIFY_LOCKED")
    if state.identity.verified and state.caller.role is CallerRole.REPRESENTATIVE:
        if state.caller.representative_confirmed is False:
            return GateResult(False, "REPRESENTATIVE_NOT_CONFIRMED")
        if state.caller.consent_status is ConsentStatus.TIMEOUT:
            return GateResult(False, "CONSENT_TIMEOUT")
        return GateResult(False, "CONSENT_REQUIRED", missing=("consent",))
    provided = set(state.identity_fields())
    missing = tuple(f.value for f in IdentityField if f not in provided)
    return GateResult(False, "IDENTITY_INCOMPLETE", missing=missing)


def resolve_gate(state: SessionState) -> GateResult:
    if state.case_context.resolution in (Resolution.SELECTED, Resolution.NO_CLAIMS):
        return GateResult(True, f"CASE_{state.case_context.resolution.upper()}")
    return GateResult(False, "CASE_UNRESOLVED", missing=("case",))


def process_gate(state: SessionState) -> GateResult:
    # The controller leaves PROCESS_CASE once the caller has nothing more to ask.
    return GateResult(False, "AWAITING_QUESTIONS")


def post_gate(state: SessionState) -> GateResult:
    return GateResult(False, "AWAITING_EMAIL_CHOICE")


SOP: Mapping[Phase, PhaseSpec] = {
    Phase.VERIFY_ID: PhaseSpec(
        phase=Phase.VERIFY_ID,
        visible=frozenset({Ctx.MESSAGE, Ctx.RECENT_MESSAGES, Ctx.PROVIDED_FIELDS, Ctx.POLICY_TEXT}),
        proposals=frozenset({Act.IDENTITY, Act.DIALOG, Act.HANDOFF}),
        tools=frozenset(
            {Tool.VERIFY_IDENTITY, Tool.CHECK_REPRESENTATIVE, Tool.REQUEST_CONSENT, Tool.CREATE_HANDOFF}
        ),
        planning=Planning.STRICT,
        gate=verify_gate,
        subflows=("representative",),
    ),
    Phase.RESOLVE_INTENT: PhaseSpec(
        phase=Phase.RESOLVE_INTENT,
        visible=frozenset(
            {Ctx.MESSAGE, Ctx.RECENT_MESSAGES, Ctx.LEDGER_HINTS, Ctx.CLAIM_CANDIDATES, Ctx.POLICY_TEXT}
        ),
        proposals=frozenset({Act.CASE_HINTS, Act.INTENT, Act.CHOOSE_CASE, Act.DIALOG, Act.HANDOFF}),
        tools=frozenset({Tool.FIND_CLAIMS, Tool.GET_CLAIM_DETAILS, Tool.CREATE_HANDOFF}),
        planning=Planning.STRICT,
        gate=resolve_gate,
    ),
    Phase.PROCESS_CASE: PhaseSpec(
        phase=Phase.PROCESS_CASE,
        visible=frozenset(
            {
                Ctx.MESSAGE,
                Ctx.RECENT_MESSAGES,
                Ctx.LEDGER_HINTS,
                Ctx.CASE_FACTS,
                Ctx.GUIDANCE,
                Ctx.POLICY_TEXT,
            }
        ),
        proposals=frozenset({Act.TOPICS, Act.INTENT, Act.CASE_HINTS, Act.DIALOG, Act.HANDOFF}),
        tools=frozenset({Tool.GET_CLAIM_DETAILS, Tool.GET_DOCUMENT_GUIDANCE, Tool.CREATE_HANDOFF}),
        planning=Planning.GROUNDED,
        gate=process_gate,
        subflows=("document_alternatives",),
    ),
    Phase.POST_PROCESS: PhaseSpec(
        phase=Phase.POST_PROCESS,
        visible=frozenset({Ctx.MESSAGE, Ctx.RECENT_MESSAGES, Ctx.SUMMARY, Ctx.CASE_FACTS, Ctx.POLICY_TEXT}),
        proposals=frozenset({Act.EMAIL_CHOICE, Act.TOPICS, Act.DIALOG, Act.HANDOFF}),
        tools=frozenset(
            {
                Tool.GET_CLAIM_DETAILS,
                Tool.PREPARE_SUMMARY,
                Tool.SEND_SUMMARY,
                Tool.CREATE_HANDOFF,
            }
        ),
        planning=Planning.STRICT,
        gate=post_gate,
        subflows=("email",),
    ),
}
