"""Per-turn orchestration.

A turn runs on a working copy of the session state, which replaces the stored
state only once the reply is ready. A failed understanding step therefore
commits nothing, and neither does an unexpected error later in the turn.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from pydantic import ValidationError

from . import planner, responses
from .claims import format_date
from .config import Clock, Settings
from .consent import ConsentService
from .controller import Controller, PhaseStep
from .domain import (
    CallerRole,
    ChatMessage,
    DateMode,
    Fact,
    IdentityField,
    MessageRole,
    PlanRecord,
    ReplyPlan,
    SessionState,
    TraceEvent,
    TurnUnderstanding,
)
from .fixture_loader import FixtureData
from .identity import parse_iso_date
from .invariants import REPLY_LEAKS, Invariants, InvariantViolation
from .llm.adapter import LLMAdapter, LLMAuthError, LLMError, RealizeContext, UnderstandContext
from .mailer import EmailSender, MockSender
from .sop_spec import SOP
from .store import SessionRecord

logger = logging.getLogger(__name__)

LLM_TIMEOUT_SECONDS = 30.0
MODEL_ATTEMPTS = 2  # each model step gets one extra attempt
RECENT_MESSAGES = 12

_MODEL_FAILURES = (LLMError, TimeoutError, ValidationError)


class ModelUnavailableError(RuntimeError):
    """The understanding step failed on every attempt; nothing was committed."""


class ModelKeyRejectedError(ModelUnavailableError):
    """The provider rejected the API key; nothing was committed."""


class SessionGoneError(RuntimeError):
    """The session was deleted or replaced while this turn waited for its lock."""


@dataclass(frozen=True)
class TurnResult:
    reply: ChatMessage
    plan: ReplyPlan
    trace: list[TraceEvent]


class ConversationEngine:
    def __init__(
        self,
        adapter: LLMAdapter,
        settings: Settings,
        clock: Clock,
        fixtures: FixtureData,
        mailer: EmailSender | None = None,
    ) -> None:
        self._adapter = adapter
        self._settings = settings
        self._clock = clock
        consent = ConsentService(fixtures.consent_scenarios, poll_interval=settings.consent_poll_interval)
        self._controller = Controller(
            fixtures, consent, mailer or MockSender(), portal_url=settings.demo_portal_url
        )
        self._invariants = Invariants(fixtures)

    def as_of_date(self, state: SessionState) -> date:
        if state.date_mode is DateMode.DEMO:
            return self._settings.demo_as_of_date
        return self._clock.today()

    def open_session(
        self, session_id: str, date_mode: DateMode, consent_scenario: str
    ) -> tuple[SessionState, ChatMessage]:
        state = SessionState(session_id=session_id, date_mode=date_mode, consent_scenario=consent_scenario)
        step = Controller.opening_step()
        text = planner.render_plan(step.plan, _texts(step.facts))
        welcome = _chat_message(MessageRole.ASSISTANT, 0, text, state.case_cycle_id)
        state.messages.append(welcome)
        state.trace.append(TraceEvent(turn=0, event="session_opened", rule=step.rule, phase=state.phase))
        state.plans.append(PlanRecord(turn=0, plan=step.plan))
        return state, welcome

    async def handle_message(
        self, record: SessionRecord, text: str, adapter: LLMAdapter | None = None
    ) -> TurnResult:
        """Run one turn; ``adapter`` is the model to use, such as one with the caller's own key."""
        async with record.lock:
            if record.closed:
                raise SessionGoneError
            working = record.state.model_copy(deep=True)
            result = await self._process_turn(working, text, adapter or self._adapter)
            record.state = working
            return result

    async def _process_turn(self, state: SessionState, text: str, adapter: LLMAdapter) -> TurnResult:
        turn = state.turn_index + 1
        understand_context = UnderstandContext(
            message=text,
            phase=state.phase,
            recent_messages=self._visible_messages(state),
            provided_identity_fields=tuple(state.identity_fields()),
            pending_question=state.pending_question.kind.value if state.pending_question else None,
            caller_role=state.caller.role.value,
        )
        understanding = await self._understand(understand_context, adapter)
        trace = [TraceEvent(turn=turn, event="understanding_completed", status="ok")]

        step = await self._controller.run_turn(state, understanding, text, turn, self.as_of_date(state))
        trace.extend(step.events)
        trace.append(TraceEvent(turn=turn, event="plan_built", rule=step.rule, phase=state.phase))

        # The reply answers this message, so the model sees it last. Earlier case cycles are left out:
        # their answers are hidden from it (6.4 rule 5), so their questions would look unanswered.
        realize_messages = (
            *(m for m in self._visible_messages(state) if m.case_cycle_id == state.case_cycle_id),
            _chat_message(MessageRole.USER, turn, text, state.case_cycle_id),
        )
        reply_text, status = await self._realize(state, step, realize_messages, adapter)
        reply_text = self._enforce_invariants(state, step, reply_text, realize_messages, turn, trace)
        trace.append(TraceEvent(turn=turn, event="reply_realized", status=status))

        protected = any(step.facts[f].party_id for f in step.plan.inform if f in step.facts)
        user_message = _chat_message(MessageRole.USER, turn, text, state.case_cycle_id)
        reply = _chat_message(MessageRole.ASSISTANT, turn, reply_text, state.case_cycle_id, protected)
        state.turn_index = turn
        state.messages.extend([user_message, reply])
        state.trace.extend(trace)
        state.plans.append(PlanRecord(turn=turn, plan=step.plan))
        return TurnResult(reply=reply, plan=step.plan, trace=trace)

    def _visible_messages(self, state: SessionState) -> tuple[ChatMessage, ...]:
        """Recent messages the current phase may show the model."""
        claim_data_visible = state.access_granted and SOP[state.phase].sees_claim_data
        visible = [
            m
            for m in state.messages
            if not m.protected or (claim_data_visible and m.case_cycle_id == state.case_cycle_id)
        ]
        return tuple(visible[-RECENT_MESSAGES:])

    async def _understand(self, context: UnderstandContext, adapter: LLMAdapter) -> TurnUnderstanding:
        for attempt in range(1, MODEL_ATTEMPTS + 1):
            try:
                return await asyncio.wait_for(adapter.understand_turn(context), LLM_TIMEOUT_SECONDS)
            except LLMAuthError:
                raise ModelKeyRejectedError from None
            except _MODEL_FAILURES as exc:
                logger.warning("understand_turn attempt %d failed: %s", attempt, _failure(exc))
        raise ModelUnavailableError

    async def _realize(
        self, state: SessionState, step: PhaseStep, messages: tuple[ChatMessage, ...], adapter: LLMAdapter
    ) -> tuple[str, str]:
        """Return the reply text and whether it came from the model or the template."""
        facts = _texts(step.facts)
        brief = planner.brief(step.plan, facts, representative=state.caller.role is CallerRole.REPRESENTATIVE)
        # The plan's own wording may be used freely; the caller's identity values may not be echoed.
        allowed = [str(v) for v in brief.values() if isinstance(v, str)]
        allowed += [text for key in ("decline", "offers") for text in brief[key]]
        forbidden = _identity_values(state)
        violations: tuple[str, ...] = ()
        for attempt in range(1, MODEL_ATTEMPTS + 1):
            context = RealizeContext(
                plan=step.plan, facts=facts, recent_messages=messages, violations=violations, brief=brief
            )
            try:
                draft = await asyncio.wait_for(adapter.realize_reply(context), LLM_TIMEOUT_SECONDS)
            except _MODEL_FAILURES as exc:
                logger.warning("realize_reply attempt %d failed: %s", attempt, _failure(exc))
                continue
            problems = responses.check_draft(
                draft,
                step.plan,
                facts,
                allowed_texts=allowed,
                forbidden=forbidden,
                question_text=brief["question"],
            )
            if not problems:
                return draft.text.strip(), "ok"
            logger.warning("realize_reply attempt %d rejected: %s", attempt, "; ".join(problems))
            violations = tuple(problems)
        return planner.render_plan(step.plan, facts), "fallback"

    def _enforce_invariants(
        self,
        state: SessionState,
        step: PhaseStep,
        reply_text: str,
        messages: tuple[ChatMessage, ...],
        turn: int,
        trace: list[TraceEvent],
    ) -> str:
        violations = self._invariants.check(state, step.plan, step.facts, reply_text, messages)
        if not violations:
            return reply_text
        for violation in violations:
            logger.error("invariant violated: %s (%s)", violation.code, violation.detail)
            trace.append(TraceEvent(turn=turn, event="invariant_violated", rule=violation.code))
        if self._settings.strict_invariants:
            raise InvariantViolation(violations)
        if any(v.code in REPLY_LEAKS for v in violations):
            return planner.POLICY_TEXTS["policy.safe_fallback"]
        return reply_text


def _failure(exc: Exception) -> str:
    """What went wrong with a model call, safe to log: adapters word their errors without the
    provider's text, while a validation error could quote the caller's details."""
    if isinstance(exc, LLMError):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


def _identity_values(state: SessionState) -> list[str]:
    """Values the caller gave for verification, which a reply must never repeat."""
    values = []
    for field in (IdentityField.ID_LAST4, IdentityField.DOB):
        entry = state.ledger.get(f"identity.{field.value}")
        if entry is None or entry.value is None:
            continue
        values.append(str(entry.value))
        if field is IdentityField.DOB and (born := parse_iso_date(str(entry.value))):
            values.append(format_date(born))
    return values


def _texts(facts: Mapping[str, Fact]) -> dict[str, str]:
    return {fact_id: fact.text for fact_id, fact in facts.items()}


def _chat_message(
    role: MessageRole, turn: int, text: str, case_cycle_id: int, protected: bool = False
) -> ChatMessage:
    prefix = "u" if role is MessageRole.USER else "a"
    return ChatMessage(
        id=f"{prefix}-{turn}",
        role=role,
        text=text,
        turn=turn,
        case_cycle_id=case_cycle_id,
        protected=protected,
    )
