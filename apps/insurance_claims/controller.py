"""Phase control: check each turn's proposals, run the phase steps, and decide the reply plan.

Each turn: validate the model's proposals and remember them, handle
interruptions, then advance through phases for as long as each
gate passes, stopping at the first step that needs the caller (6.1, 6.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from . import interrupts
from . import ledger as ledger_ops
from .claims import (
    ClaimService,
    NotAuthorized,
    ToolError,
    TrustedContext,
    brief_fact,
    matches_hints,
    overview_fact,
    relaxed_fact,
)
from .config import DEFAULT_PORTAL_URL
from .consent import ConsentService, is_registered_representative
from .domain import (
    CallerRole,
    CaseContext,
    CaseHintField,
    ConsentStatus,
    DeliveryStatus,
    DialogAct,
    EmailChoice,
    EmailReply,
    EmailState,
    EmotionLabel,
    EmotionReading,
    EntryStatus,
    Fact,
    HandoffRecord,
    IdentityField,
    Intent,
    Offer,
    OutboxEmail,
    PendingKind,
    PendingQuestion,
    Phase,
    PlanAsk,
    Planning,
    ReplyPlan,
    Resolution,
    SessionState,
    SessionStatus,
    Summary,
    Topic,
    TraceEvent,
    TurnUnderstanding,
)
from .fixture_loader import Claim, FixtureData
from .identity import MAX_FAILED_ATTEMPTS, Outcome, candidates_digest, verify
from .mailer import EmailSender
from .planner import identity_ask, identity_when_ready, policy_fact
from .processing import DEFAULT_TOPICS, CaseFacts, upload_link
from .sop_spec import SOP
from .summary import earlier_claim_recap, mask_email, prepare_summary, summary_fact

RESUME_VERIFY = "VERIFY_ID.collect"
# Unproductive verification turns before alternatives and a human are offered.
NO_PROGRESS_OFFER_AFTER = 2


@dataclass(frozen=True)
class PhaseStep:
    plan: ReplyPlan
    facts: dict[str, Fact]
    events: list[TraceEvent]
    rule: str


@dataclass
class _Builder:
    """Collects one turn's plan pieces, facts and trace events."""

    turn: int
    inform: list[str] = field(default_factory=list)
    facts: dict[str, Fact] = field(default_factory=dict)
    offers: list[Offer] = field(default_factory=list)
    declines: list[str] = field(default_factory=list)
    acknowledge: EmotionReading | None = None
    tone: EmotionLabel | None = None
    answer_topics: list[Topic] = field(default_factory=list)
    available: list[str] = field(default_factory=list)
    ask: PlanAsk | None = None
    next: str | None = None
    resume: str | None = None
    rule: str = "NO_RULE"
    events: list[TraceEvent] = field(default_factory=list)

    def say(self, fact: Fact | str) -> None:
        fact = policy_fact(fact) if isinstance(fact, str) else fact
        self.facts[fact.id] = fact
        if fact.id not in self.inform:
            self.inform.append(fact.id)

    def allow(self, fact: Fact) -> None:
        """Make ``fact`` available to a grounded answer without requiring it."""
        self.facts[fact.id] = fact
        if fact.id not in self.inform and fact.id not in self.available:
            self.available.append(fact.id)

    def offer(self, *offers: Offer) -> None:
        self.offers.extend(o for o in offers if o not in self.offers)

    def event(self, event: str, **fields) -> None:
        self.events.append(TraceEvent(turn=self.turn, event=event, **fields))

    def build(self, state: SessionState) -> PhaseStep:
        plan = ReplyPlan(
            planning=Planning.GROUNDED if self.answer_topics else Planning.STRICT,
            acknowledge=self.acknowledge,
            tone=self.tone,
            decline=self.declines,
            inform=self.inform,
            ask=self.ask,
            offer=self.offers,
            next=self.next,
            resume=self.resume,
            answer_topics=self.answer_topics,
            available=self.available,
        )
        return PhaseStep(plan=plan, facts=self.facts, events=self.events, rule=self.rule)


@dataclass(frozen=True)
class _Turn:
    """Everything a phase step needs to know about the current message."""

    understanding: TurnUnderstanding
    validated: ledger_ops.ValidatedTurn
    identity_changed: bool
    off_topic: bool
    as_of: date
    # The caller refused past the persuasion limit.
    stop_persuading: bool = False
    # The caller pushed back on verification this turn without giving anything new.
    pushback: bool = False

    @property
    def acts(self) -> set[DialogAct]:
        return set(self.understanding.dialog_acts)


# Questions that only make sense for a denied claim.
_DENIAL_TOPICS = frozenset({Topic.DENIAL_REASON, Topic.APPEAL_DEADLINE})


class Controller:
    def __init__(
        self,
        fixtures: FixtureData,
        consent: ConsentService,
        mailer: EmailSender,
        *,
        portal_url: str = DEFAULT_PORTAL_URL,
    ) -> None:
        self._fixtures = fixtures
        self._claims = ClaimService(fixtures)
        self._consent = consent
        self._mailer = mailer
        self._portal_url = portal_url
        self._case_facts = CaseFacts(fixtures, portal_url)
        self._emails = {holder.party_id: holder.email for holder in fixtures.policyholders}
        # Contact details on file, so repeating one after verification isn't mistaken for a change.
        self._contacts = {
            holder.party_id: {
                IdentityField.PHONE: {holder.phone, *holder.phone_aliases},
                IdentityField.EMAIL: {holder.email.casefold(), *(a.casefold() for a in holder.email_aliases)},
            }
            for holder in fixtures.policyholders
        }

    @staticmethod
    def opening_step() -> PhaseStep:
        builder = _Builder(turn=0)
        builder.say("policy.welcome")
        builder.ask = identity_ask()
        builder.resume = RESUME_VERIFY
        builder.rule = "SESSION_OPENED"
        return PhaseStep(
            plan=ReplyPlan(inform=builder.inform, ask=builder.ask, resume=builder.resume),
            facts=builder.facts,
            events=[],
            rule=builder.rule,
        )

    async def run_turn(
        self, state: SessionState, understanding: TurnUnderstanding, message: str, turn: int, as_of: date
    ) -> PhaseStep:
        builder = _Builder(turn=turn)
        if state.status is SessionStatus.HANDED_OFF:
            builder.say("policy.handed_off_already")
            builder.rule = "ALREADY_HANDED_OFF"
            return builder.build(state)
        if state.status is SessionStatus.ENDED:
            builder.say("policy.ended_already")
            builder.rule = "ALREADY_ENDED"
            return builder.build(state)
        if state.status is SessionStatus.CANCELLED:
            # The caller said goodbye earlier and came back; carry on where they left off.
            state.status = SessionStatus.ACTIVE
            builder.event("session_resumed")

        validated = ledger_ops.validate(understanding, message)
        for rejection in validated.rejected:
            builder.event(
                "proposal_rejected",
                status=rejection.reason,
                rule=f"{rejection.kind}.{rejection.field or '-'}",
            )
        ledger_ops.record_turn_details(state.ledger, validated, understanding, turn)
        if understanding.intent is not Intent.UNKNOWN:
            state.case_context.intent = understanding.intent
        self._update_caller_role(state, understanding, builder)
        identity_changed = self._apply_identity(state, validated, understanding, builder)
        self._note_withheld(state, understanding)

        interruption = interrupts.evaluate(state, understanding, turn)
        for rule in interruption.rules:
            builder.event("interrupt", rule=rule)
        if interruption.handoff_reason:
            self._hand_off(state, interruption.handoff_reason, builder)
            return builder.build(state)
        builder.declines = interruption.declines
        if state.access_granted:
            # Verification is done; a push past it has nothing left to skip (other refusals remain).
            builder.declines = [d for d in builder.declines if d != "bypass_verification"]
        if understanding.email_reply is EmailReply.SEND and not state.access_granted:
            # Nothing may be summarized or sent before access.
            builder.declines.append("summary_before_access")
        builder.acknowledge = interruption.acknowledge
        builder.tone = interruption.tone
        for policy in interruption.inform:
            builder.say(policy)
        builder.offer(*interruption.offers)

        if state.phase in (Phase.PROCESS_CASE, Phase.POST_PROCESS) and self._asks_for_another_claim(
            state, validated
        ):
            self._start_new_case_cycle(state, understanding.intent, as_of, builder)

        current = _Turn(
            understanding,
            validated,
            identity_changed,
            interruption.off_topic,
            as_of,
            stop_persuading="PERSUASION_LIMIT" in interruption.rules,
            pushback=interruption.pushback,
        )
        if self._ends_before_a_case(state, current):
            # Leaving early is allowed without finishing the SOP.
            state.status = SessionStatus.CANCELLED
            state.pending_question = None
            builder.say("policy.closing")
            builder.rule = "CALLER_ENDED"
            return builder.build(state)
        for _ in range(len(Phase)):
            if state.phase is Phase.VERIFY_ID:
                advanced = await self._verify_step(state, current, builder)
            elif state.phase is Phase.RESOLVE_INTENT:
                advanced = self._resolve_step(state, current, builder)
            elif state.phase is Phase.PROCESS_CASE:
                advanced = self._process_step(state, current, builder)
            else:
                advanced = await self._post_step(state, current, builder)
            if not advanced:
                break
        return builder.build(state)

    @staticmethod
    def _ends_before_a_case(state: SessionState, turn: _Turn) -> bool:
        """A goodbye while still verifying or choosing a claim, with nothing new to act on."""
        return (
            DialogAct.END in turn.acts
            and state.phase in (Phase.VERIFY_ID, Phase.RESOLVE_INTENT)
            and not turn.identity_changed
            and not turn.validated.case_hints
            and not turn.validated.questions
        )

    # --- caller role and identity -------------------------------------------------

    @staticmethod
    def _note_withheld(state: SessionState, understanding: TurnUnderstanding) -> None:
        """Remember details the caller declined, so they are not asked for again."""
        provided = state.identity_fields()
        declined = [*state.identity.withheld, *understanding.withheld_fields]
        state.identity.withheld = list(dict.fromkeys(f for f in declined if f not in provided))

    def _update_caller_role(
        self, state: SessionState, understanding: TurnUnderstanding, builder: _Builder
    ) -> None:
        stated = understanding.caller_role
        if stated is CallerRole.REPRESENTATIVE and state.caller.role is not CallerRole.REPRESENTATIVE:
            state.caller.role = CallerRole.REPRESENTATIVE
            builder.event("caller_role_changed", status="representative")
            if state.phase is not Phase.VERIFY_ID:
                # Pause claim access until the representative is authorized.
                self._reset_case(state)
                self._enter(state, Phase.VERIFY_ID, "CALLER_BECAME_REPRESENTATIVE", builder)
        elif stated is CallerRole.SELF and state.caller.role is CallerRole.UNKNOWN:
            state.caller.role = CallerRole.SELF
        elif stated is CallerRole.SELF and state.caller.role is CallerRole.REPRESENTATIVE:
            unchecked = (
                state.caller.representative_confirmed is None
                and state.caller.consent_status is ConsentStatus.NOT_REQUESTED
            )
            if unchecked:
                # "I'm the policyholder, not calling for someone else": nothing about the representative
                # was checked yet, so verifying as the policyholder is the same as if they'd said so first.
                state.caller.role = CallerRole.SELF
                for key in ("caller.rep_name", "caller.rep_relationship"):
                    state.ledger.remove(key)
                state.subflow_step = None
                if state.pending_question and state.pending_question.kind is PendingKind.REPRESENTATIVE:
                    state.pending_question = None
                builder.event("caller_role_changed", status="self")
            elif not state.counters.asked.get("representative_role_kept"):
                # Once the representative check or consent has started, a switch could step around it.
                state.counters.asked["representative_role_kept"] = 1
                builder.say("policy.representative_role_kept")
                builder.offer(Offer.HANDOFF)
                state.counters.handoff_offered_turn = builder.turn
                builder.event("caller_role_kept", rule="REPRESENTATIVE_CHECK_STARTED")

    def _apply_identity(
        self,
        state: SessionState,
        validated: ledger_ops.ValidatedTurn,
        understanding: TurnUnderstanding,
        builder: _Builder,
    ) -> bool:
        """Record identity proposals under the freeze rules."""
        proposals = validated.identity
        rep_name = state.ledger.get("caller.rep_name")
        if rep_name is not None:
            # "I'm David Chen, calling about my mom's claim": the caller's own name is not hers.
            own = ledger_ops.normalize_text(str(rep_name.value))
            mine = [
                p
                for p in proposals
                if p.field is IdentityField.FULL_NAME
                and p.value
                and ledger_ops.normalize_text(str(p.value)) == own
            ]
            if mine:
                proposals = [p for p in proposals if p not in mine]
                builder.event(
                    "proposal_rejected", status="REPRESENTATIVES_OWN_NAME", rule="identity.full_name"
                )
        if not proposals:
            return False
        identity = state.identity
        if identity.locked:
            builder.event("identity_ignored", rule="VERIFY_LOCKED")
            return False
        if not identity.verified:
            return bool(ledger_ops.record_identity(state.ledger, proposals, builder.turn))

        if DialogAct.CORRECT in understanding.dialog_acts:
            if ledger_ops.record_identity(state.ledger, proposals, builder.turn):
                self._revoke_verification(state, builder)
                return True
            return False
        on_file = self._contacts[identity.party_id]
        if any(p.field in on_file and str(p.value).casefold() not in on_file[p.field] for p in proposals):
            builder.say("policy.contact_change_unsupported")
            builder.offer(Offer.HANDOFF)
            state.counters.handoff_offered_turn = builder.turn
            builder.event("contact_change_requested", rule="IDENTITY_FROZEN")
        else:
            builder.event("identity_update_ignored", rule="IDENTITY_FROZEN")
        return False

    def _revoke_verification(self, state: SessionState, builder: _Builder) -> None:
        state.identity.verified = False
        state.identity.party_id = None
        state.identity.verified_turn = None
        state.caller.representative_confirmed = None
        # A consent timeout stands for the rest of the session.
        if state.caller.consent_status is not ConsentStatus.TIMEOUT:
            state.caller.consent_status = ConsentStatus.NOT_REQUESTED
            state.caller.consent_polls = 0
        self._reset_case(state)
        builder.event("verification_revoked", rule="CALLER_CORRECTED_IDENTITY")
        self._enter(state, Phase.VERIFY_ID, "CALLER_CORRECTED_IDENTITY", builder)

    # --- VERIFY_ID ------------------------------------------------------------------

    async def _verify_step(self, state: SessionState, turn: _Turn, builder: _Builder) -> bool:
        identity = state.identity
        builder.resume = RESUME_VERIFY
        if identity.locked:
            return self._locked(state, builder, just_locked=False)

        representative = state.caller.role is CallerRole.REPRESENTATIVE
        if representative and state.caller.representative_confirmed is None:
            # Collect the representative's own details first.
            missing = [p for p in ("name", "relationship") if not state.ledger.get(f"caller.rep_{p}")]
            if missing:
                state.subflow_step = "representative details"
                state.pending_question = PendingQuestion(
                    kind=PendingKind.REPRESENTATIVE, asked_turn=builder.turn
                )
                builder.ask = PlanAsk(slot="representative", one_of=missing)
                builder.rule = "REPRESENTATIVE_DETAILS"
                return False

        failed_now = False
        candidates = ledger_ops.identity_candidates(state.ledger)
        digest = candidates_digest(candidates)
        if not identity.verified and digest != identity.evaluated_digest:
            result = verify(candidates, self._fixtures.policyholders)
            if result.outcome is not Outcome.INSUFFICIENT:
                identity.evaluated_digest = digest
                builder.event("verification_evaluated", tool="verify_identity", status=result.outcome.value)
            if result.outcome is Outcome.VERIFIED:
                identity.verified = True
                identity.party_id = result.party_id
                identity.verified_turn = builder.turn
                identity.no_progress = 0
            elif result.outcome is Outcome.FAILED:
                identity.failed_attempts += 1
                failed_now = True
                if identity.failed_attempts >= MAX_FAILED_ATTEMPTS:
                    identity.locked = True
                    builder.event("verification_locked", rule="VERIFY_LOCKED")
                    return self._locked(state, builder, just_locked=True)

        if not identity.verified:
            self._ask_for_identity(state, turn, failed_now, builder)
            return False

        if representative and not await self._authorize_representative(state, builder):
            return False

        gate = SOP[Phase.VERIFY_ID].gate(state)
        if not gate.passed:  # defensive: every blocking case returned above
            builder.rule = gate.reason
            return False
        if not representative:
            builder.say("policy.verified")
        self._enter(state, Phase.RESOLVE_INTENT, gate.reason, builder)
        return True

    def _locked(self, state: SessionState, builder: _Builder, *, just_locked: bool) -> bool:
        # Explained in full once; afterwards only restated, like a consent timeout.
        builder.say("policy.verification_locked" if just_locked else "policy.verification_still_locked")
        builder.offer(Offer.HANDOFF)
        state.counters.handoff_offered_turn = builder.turn
        builder.rule = "VERIFY_LOCKED"
        return False

    def _ask_for_identity(
        self, state: SessionState, turn: _Turn, failed_now: bool, builder: _Builder
    ) -> None:
        identity = state.identity
        representative = state.caller.role is CallerRole.REPRESENTATIVE
        candidates = ledger_ops.identity_candidates(state.ledger)
        provided = state.identity_fields()
        remaining = [f for f in IdentityField if f not in provided]
        # Declined details are left out while enough others remain.
        preferred = [f for f in remaining if f not in identity.withheld]

        if not turn.off_topic:
            identity.no_progress = 0 if turn.identity_changed or failed_now else identity.no_progress + 1
        open_questions = ledger_ops.open_questions(state.ledger)
        if open_questions and turn.validated.questions:
            # Said when the caller asks; repeating it on every verification turn sounds scripted.
            builder.next = f"answer:{open_questions[0].value}"

        id_entry = state.ledger.get("identity.id_last4")
        if turn.validated.ambiguous_dob:
            state.pending_question = PendingQuestion(kind=PendingKind.DOB_FORMAT, asked_turn=builder.turn)
            builder.ask = PlanAsk(slot="dob_format")
            builder.rule = "VERIFY_DOB_AMBIGUOUS"
        elif id_entry is not None and id_entry.qualifier in (None, "unknown"):
            state.pending_question = PendingQuestion(kind=PendingKind.ID_TYPE, asked_turn=builder.turn)
            builder.ask = PlanAsk(slot="id_type")
            builder.rule = "VERIFY_ID_TYPE_UNKNOWN"
        elif turn.validated.incomplete_name and IdentityField.FULL_NAME not in provided:
            # A format check on what was said, like the date format; it reveals nothing about records.
            state.pending_question = None
            builder.ask = PlanAsk(slot="policyholder_full_name" if representative else "full_name")
            builder.rule = "VERIFY_FULL_NAME"
        elif failed_now or candidates_digest(candidates) == identity.evaluated_digest:
            # The current set was checked and did not match: stay generic.
            if failed_now:
                builder.say(
                    "policy.verification_failed_representative"
                    if representative
                    else "policy.verification_failed"
                )
            slot = "policyholder_identity_retry" if representative else "identity_retry"
            builder.ask = PlanAsk(slot=slot, one_of=[f.value for f in preferred or remaining])
            builder.rule = "VERIFY_RETRY"
            state.pending_question = None
        else:
            # A bypass refusal already says why verification is needed.
            bypass_declined = "bypass_verification" in builder.declines
            if (
                self._claim_related(turn)
                and not bypass_declined
                and "policy.why_verify" not in builder.inform
                and (not state.counters.asked.get("why_verify") or turn.pushback)
            ):
                # Explained when the caller first asks about a claim, and again when they push back
                # ("I already told you who I am"); not on every turn while they're giving details.
                state.counters.asked["why_verify"] = 1
                builder.say("policy.why_verify_representative" if representative else "policy.why_verify")
            countable = sum(1 for c in candidates if c.countable)
            builder.ask = identity_ask(
                provided, countable=countable, for_representative=representative, withheld=identity.withheld
            )
            if not representative:
                # The welcome already asked in full, so any later full ask uses the shorter wording.
                builder.ask.variant = 1
            builder.rule = "VERIFY_ASK_IDENTITY"
            state.pending_question = None
            if turn.off_topic and not provided and not representative:
                # Nothing to verify yet and nothing claim-related asked: invite them back instead.
                builder.ask = PlanAsk(slot="claims_help")
                builder.rule = "VERIFY_OFF_TOPIC"
        if identity.no_progress >= NO_PROGRESS_OFFER_AFTER:
            builder.offer(Offer.ALTERNATIVE_FIELDS, Offer.HANDOFF)
            state.counters.handoff_offered_turn = builder.turn
        # Too few details are left without the ones the caller declined.
        exhausted = builder.ask is not None and any(
            IdentityField(v) in identity.withheld for v in builder.ask.one_of
        )
        if (turn.stop_persuading or exhausted) and not turn.identity_changed:
            # Stop asking: offer a person and leave the door open. The gate stays.
            state.pending_question = None
            builder.ask = None
            builder.next = None
            builder.say(identity_when_ready(preferred, representative=representative))
            builder.offers = [o for o in builder.offers if o is not Offer.ALTERNATIVE_FIELDS]
            builder.offer(Offer.HANDOFF)
            state.counters.handoff_offered_turn = builder.turn
            builder.rule = "VERIFY_STOP_PERSUADING"

    @staticmethod
    def _claim_related(turn: _Turn) -> bool:
        return bool(
            turn.validated.questions
            or turn.validated.case_hints
            or turn.understanding.intent is not Intent.UNKNOWN
        )

    async def _authorize_representative(self, state: SessionState, builder: _Builder) -> bool:
        """Registered-representative check, then policyholder consent."""
        caller = state.caller
        if caller.representative_confirmed is None:
            caller.representative_confirmed = is_registered_representative(
                self._fixtures.representatives,
                state.identity.party_id,
                str(state.ledger.get("caller.rep_name").value),
                str(state.ledger.get("caller.rep_relationship").value),
            )
            status = "confirmed" if caller.representative_confirmed else "not_confirmed"
            builder.event("tool_completed", tool="check_representative", status=status)
        if caller.representative_confirmed is False:
            builder.say("policy.representative_not_confirmed")
            builder.offer(Offer.HANDOFF)
            state.counters.handoff_offered_turn = builder.turn
            builder.rule = "REPRESENTATIVE_NOT_CONFIRMED"
            return False

        just_requested = caller.consent_status is ConsentStatus.NOT_REQUESTED
        if just_requested:
            # Say which checks passed before asking the policyholder, so the caller can follow along.
            builder.say("policy.representative_verified")

            def record_poll(poll: int, status: str) -> None:
                builder.event(
                    "consent_polled",
                    tool="request_consent",
                    status=f"{status} {poll}/{self._consent.max_polls}",
                )

            outcome = await self._consent.request_and_wait(state.consent_scenario, on_poll=record_poll)
            caller.consent_status = outcome.status
            caller.consent_polls = outcome.polls
            state.subflow_step = f"consent ({outcome.status.value} {outcome.polls}/{self._consent.max_polls})"
            if outcome.status is ConsentStatus.APPROVED:
                builder.say("policy.consent_approved")
        if caller.consent_status is ConsentStatus.TIMEOUT:
            # Stop persuading: explain once and offer the alternatives.
            builder.say("policy.consent_timeout" if just_requested else "policy.consent_still_missing")
            # Identity is already verified here; what is missing is the approval, which that says.
            builder.declines = [d for d in builder.declines if d != "bypass_verification"]
            builder.offer(Offer.RETRY_LATER, Offer.POLICYHOLDER_DIRECT, Offer.HANDOFF)
            state.counters.handoff_offered_turn = builder.turn
            builder.rule = "CONSENT_TIMEOUT"
            return False
        return True

    # --- RESOLVE_INTENT -------------------------------------------------------------

    def _resolve_step(self, state: SessionState, turn: _Turn, builder: _Builder) -> bool:
        ctx = self._trusted(state)
        pending = state.pending_question
        if pending and pending.kind is PendingKind.CONFIRM_CASE:
            if DialogAct.AGREE in turn.acts and pending.candidates:
                self._select(state, ctx, pending.candidates[0], builder)
                return True
            if DialogAct.DENY in turn.acts:
                state.pending_question = None
                builder.say("policy.no_match")
                builder.ask = PlanAsk(slot="describe_case")
                builder.offer(Offer.HANDOFF)
                state.counters.handoff_offered_turn = builder.turn
                builder.rule = "CASE_CONFIRM_DECLINED"
                return False

        hints = ledger_ops.case_hints(state.ledger)
        open_questions = ledger_ops.open_questions(state.ledger)
        # Someone asking why a claim was denied, or about its appeal deadline, most likely means a
        # denied claim. That guess is used only if it finds the claim outright; otherwise the search
        # runs on what the caller said, so no reply ever explains away a hint they never gave.
        implied_denial = state.case_context.intent is Intent.DENIAL_QUESTION or _DENIAL_TOPICS & set(
            open_questions
        )
        try:
            search = None
            if implied_denial and CaseHintField.STATUS not in hints:
                search = self._claims.find_claims(ctx, {**hints, CaseHintField.STATUS: "denied"})
                if search.relaxed or search.resolution is Resolution.NO_MATCH:
                    search = None
            if search is None:
                search = self._claims.find_claims(ctx, hints)
        except ToolError as error:
            # A failed lookup is never reported as "no claims".
            builder.event("tool_failed", tool="find_claims", status=error.code)
            builder.say("policy.lookup_unavailable")
            builder.offer(Offer.HANDOFF)
            state.counters.handoff_offered_turn = builder.turn
            builder.rule = "LOOKUP_FAILED"
            return False
        builder.event("tool_completed", tool="find_claims", status=search.resolution.value)

        case = state.case_context
        case.resolution = search.resolution
        case.candidate_ids = list(search.case_ids)
        case.relaxed = list(search.relaxed)

        if search.resolution is Resolution.SELECTED:
            self._select(state, ctx, search.case_ids[0], builder)
            return True
        if search.resolution is Resolution.NO_CLAIMS:
            self._enter(state, Phase.PROCESS_CASE, "CASE_NO_CLAIMS", builder)
            return True
        if search.resolution is Resolution.NO_MATCH:
            builder.say("policy.no_match")
            builder.ask = PlanAsk(slot="describe_case")
            builder.offer(Offer.HANDOFF)
            state.counters.handoff_offered_turn = builder.turn
            builder.rule = "CASE_NO_MATCH"
            return False

        claims = [self._claims.get_claim(ctx, case_id) for case_id in search.case_ids]
        kind = PendingKind.CONFIRM_CASE if search.relaxed and len(claims) == 1 else PendingKind.CHOOSE_CASE
        # The same list as last turn: point back to it rather than reading it all out again.
        repeat = pending is not None and pending.kind is kind and pending.candidates == list(search.case_ids)
        # A claim number asked about again still gets its direct answer: it isn't on this account.
        asked_for_number = any(field is CaseHintField.CASE_ID for field, _, _ in turn.validated.case_hints)
        if search.relaxed and (not repeat or asked_for_number):
            builder.say(relaxed_fact(hints, _account(state)))
        if repeat and kind is PendingKind.CHOOSE_CASE:
            # Listed in full last turn: name them by number, not the whole list again.
            builder.say(_candidate_numbers_fact(state, claims))
        else:
            builder.say(_candidates_fact(state, claims))
        state.pending_question = PendingQuestion(
            kind=kind, asked_turn=builder.turn, candidates=list(search.case_ids)
        )
        slot = "choose_case_again" if repeat and kind is PendingKind.CHOOSE_CASE else kind.value
        builder.ask = PlanAsk(slot=slot, one_of=list(search.case_ids))
        builder.rule = "CASE_CONFIRM" if kind is PendingKind.CONFIRM_CASE else "CASE_CHOOSE"
        if open_questions and not repeat:
            builder.next = f"choose:{open_questions[0].value}"
        return False

    def _select(self, state: SessionState, ctx: TrustedContext, case_id: str, builder: _Builder) -> None:
        claim = self._claims.get_claim(ctx, case_id)
        case = state.case_context
        case.resolution = Resolution.SELECTED
        case.selected_case_id = claim.case_id
        case.candidate_ids = []
        state.pending_question = None
        fact = overview_fact(claim)
        state.fact_bundle[fact.id] = fact
        # A compact line for the inspector; it is never part of a reply plan.
        brief = brief_fact(claim)
        state.fact_bundle[brief.id] = brief
        builder.say(fact)
        self._enter(state, Phase.PROCESS_CASE, "CASE_SELECTED", builder)

    # --- PROCESS_CASE ---------------------------------------------------------------

    def _process_step(self, state: SessionState, turn: _Turn, builder: _Builder) -> bool:
        case = state.case_context
        pending = state.pending_question.kind if state.pending_question else None
        topics = self._topics_to_answer(state, turn)
        wants_summary = turn.understanding.email_reply is EmailReply.SEND
        done = DialogAct.END in turn.acts or (
            (wants_summary or (pending is PendingKind.ANYTHING_ELSE and DialogAct.DENY in turn.acts))
            and not topics
        )

        if case.resolution is Resolution.NO_CLAIMS:
            if done:
                self._enter(state, Phase.POST_PROCESS, "CALLER_DONE", builder)
                return True
            builder.say(_no_claims_fact(state))
            builder.offer(Offer.HANDOFF)
            state.counters.handoff_offered_turn = builder.turn
            self._ask_anything_else(state, builder, "CASE_NO_CLAIMS")
            return False

        if topics:
            details = {**ledger_ops.question_details(state.ledger), **turn.validated.question_details}
            self._answer(state, self._selected_claim(state), topics, turn.as_of, builder, details)
            if wants_summary:
                # "Is a scan OK? And email me a summary": answer first, then the summary.
                self._enter(state, Phase.POST_PROCESS, "CALLER_ASKED_FOR_SUMMARY", builder)
                return True
            self._ask_anything_else(state, builder, "PROCESS_ANSWERED")
            return False
        if done:
            self._enter(state, Phase.POST_PROCESS, "CALLER_DONE", builder)
            return True
        state.pending_question = None
        builder.ask = PlanAsk(slot="case_question")
        builder.rule = "PROCESS_ASK_QUESTION"
        return False

    def _topics_to_answer(self, state: SessionState, turn: _Turn) -> list[Topic]:
        """Questions asked this turn or remembered from earlier; the intent's defaults on arrival."""
        case = state.case_context
        topics = list(dict.fromkeys([*turn.validated.questions, *ledger_ops.open_questions(state.ledger)]))
        if not topics and not case.answered and case.intent in DEFAULT_TOPICS:
            topics = list(DEFAULT_TOPICS[case.intent])
        return topics

    def _answer(
        self,
        state: SessionState,
        claim: Claim,
        topics: list[Topic],
        as_of: date,
        builder: _Builder,
        details: dict[Topic, str] | None = None,
    ) -> None:
        case = state.case_context
        answer = self._case_facts.answer(
            claim, topics, as_of, alternatives_given=case.alternatives_given, details=details or {}
        )
        for fact in answer.facts:
            state.fact_bundle[fact.id] = fact
            if fact.id in answer.required:
                builder.say(fact)
            else:
                builder.allow(fact)
        builder.answer_topics = list(dict.fromkeys([*builder.answer_topics, *topics]))
        case.answered = list(dict.fromkeys([*case.answered, *topics]))
        case.alternatives_given |= answer.gave_alternatives
        for topic in topics:
            entry = state.ledger.get(f"question.{topic.value}")
            if entry is not None:
                entry.status = EntryStatus.ANSWERED
        if answer.needs_handoff:
            builder.offer(Offer.HANDOFF)
            state.counters.handoff_offered_turn = builder.turn
        builder.event("tool_completed", tool="get_claim_details", status=",".join(t.value for t in topics))

    @staticmethod
    def _ask_anything_else(state: SessionState, builder: _Builder, rule: str) -> None:
        state.pending_question = PendingQuestion(kind=PendingKind.ANYTHING_ELSE, asked_turn=builder.turn)
        # With no claim selected, "on this claim" would point at nothing.
        slot = "anything_else" if state.case_context.selected_case_id else "anything_else_general"
        builder.ask = PlanAsk(slot=slot, variant=_next_variant(state, slot))
        builder.rule = rule

    # --- POST_PROCESS ---------------------------------------------------------------

    async def _post_step(self, state: SessionState, turn: _Turn, builder: _Builder) -> bool:
        email = state.email
        pending = state.pending_question
        claim = self._selected_claim(state) if state.case_context.selected_case_id else None
        asked_to_send = turn.understanding.email_reply is EmailReply.SEND
        ending = DialogAct.END in turn.acts

        # A follow-up about the same claim is answered here; the offered summary is then stale (6.4 rule 4).
        follow_ups = [t for t in turn.validated.questions if t not in builder.answer_topics]
        if follow_ups and claim is not None:
            self._answer(
                state,
                claim,
                follow_ups,
                turn.as_of,
                builder,
                turn.validated.question_details,
            )
            if asked_to_send:
                await self._send_on_request(state, claim, turn.as_of, builder, ending=ending)
            elif email.choice is EmailChoice.UNDECIDED:
                self._offer_summary(state, claim, turn.as_of, builder)
            else:
                self._ask_closing(state, builder, "POST_ANSWERED")
            return False

        if pending and pending.kind is PendingKind.EMAIL_CONSENT:
            reply = turn.understanding.email_reply
            wants = reply is EmailReply.SEND or (reply is EmailReply.NONE and DialogAct.AGREE in turn.acts)
            declines = reply is EmailReply.SKIP or (reply is EmailReply.NONE and DialogAct.DENY in turn.acts)
            if wants:
                await self._send_summary(state, pending, turn.as_of, builder, ending=ending)
                return False
            if declines:
                email.choice = EmailChoice.SKIP
                builder.say("policy.email_skipped")
                builder.event("email_choice", status="skip")
                self._wrap_up(state, builder, "EMAIL_SKIPPED", ending=ending)
                return False
            builder.say(summary_fact(state.summary))
            builder.ask = PlanAsk(slot="email_consent")
            builder.rule = "EMAIL_CONSENT_UNCLEAR"
            return False

        if asked_to_send:
            await self._send_on_request(state, claim, turn.as_of, builder, ending=ending)
            return False

        if email.choice is EmailChoice.UNDECIDED and turn.understanding.email_reply is EmailReply.SKIP:
            # Turned down before it was offered: still recap, but don't offer what they declined (8.3).
            summary = self._prepare_summary(state, claim, turn.as_of)
            builder.say(summary_fact(summary))
            email.choice = EmailChoice.SKIP
            builder.say("policy.email_skipped")
            builder.event("email_choice", status="skip")
            self._wrap_up(state, builder, "EMAIL_DECLINED_EARLY", ending=ending)
            return False

        # After a skip, a summary goes out only on an explicit request (above), never on its own (6.4 rule 4).
        if email.choice is EmailChoice.UNDECIDED:
            self._offer_summary(state, claim, turn.as_of, builder)
            return False

        # A goodbye at any point here, or "no" to "anything else?", ends the conversation.
        if ending or (pending and pending.kind is PendingKind.ANYTHING_ELSE and DialogAct.DENY in turn.acts):
            self._end(state, builder, "CONVERSATION_CLOSED")
            return False
        self._ask_closing(state, builder, "POST_ASK_CLOSING")
        return False

    def _offer_summary(
        self,
        state: SessionState,
        claim: Claim | None,
        as_of: date,
        builder: _Builder,
    ) -> None:
        recipient = self._emails[state.identity.party_id]
        summary = self._prepare_summary(state, claim, as_of)
        builder.say(summary_fact(summary))
        whose = (
            "the policyholder's email on file"
            if state.caller.role is CallerRole.REPRESENTATIVE
            else "the address on file"
        )
        offer = f"I can email this summary to {mask_email(recipient)} ({whose})."
        builder.say(
            Fact(id="email.offer", text=offer, source="policyholders.json", party_id=state.identity.party_id)
        )
        state.pending_question = PendingQuestion(
            kind=PendingKind.EMAIL_CONSENT, asked_turn=builder.turn, candidates=[str(summary.revision)]
        )
        builder.ask = PlanAsk(slot="email_consent")
        builder.rule = "EMAIL_OFFERED"

    def _prepare_summary(self, state: SessionState, claim: Claim | None, as_of: date) -> Summary:
        recipient = self._emails[state.identity.party_id]
        summary = prepare_summary(
            state,
            claim,
            recipient,
            as_of,
            state.counters.handoff_offered_turn is not None,
            upload_link(self._portal_url, claim.case_id) if claim else None,
        )
        state.summary = summary
        state.email.last_revision = summary.revision
        return summary

    async def _send_summary(
        self,
        state: SessionState,
        pending: PendingQuestion,
        as_of: date,
        builder: _Builder,
        *,
        ending: bool = False,
    ) -> None:
        """Send only the summary the caller was just shown, and only once."""
        summary = state.summary
        if summary is None or pending.candidates != [str(summary.revision)]:
            claim = self._selected_claim(state) if state.case_context.selected_case_id else None
            self._offer_summary(state, claim, as_of, builder)
            return
        await self._deliver(state, summary, builder, ending=ending)

    async def _send_on_request(
        self, state: SessionState, claim: Claim | None, as_of: date, builder: _Builder, *, ending: bool
    ) -> None:
        """A clear request to email the summary is consent to the summary made for it, and the reply shows
        exactly what was sent. Only an offer the caller didn't ask for needs a yes first."""
        summary = self._prepare_summary(state, claim, as_of)
        if state.outbox and state.outbox[-1].body == summary.body:
            if ending:
                # "Thanks for sending it, bye": nothing new to send, and nothing to tell them.
                self._wrap_up(state, builder, "CONVERSATION_CLOSED", ending=True)
                return
            builder.say("policy.email_already_sent")
            self._wrap_up(state, builder, "EMAIL_ALREADY_SENT", ending=ending)
            return
        builder.say(summary_fact(summary))
        builder.event("email_choice", status="requested")
        await self._deliver(state, summary, builder, ending=ending)

    async def _deliver(
        self, state: SessionState, summary: Summary, builder: _Builder, *, ending: bool
    ) -> None:
        email = state.email
        if summary.revision in email.sent_revisions:
            builder.say("policy.email_already_sent")
            self._ask_closing(state, builder, "EMAIL_ALREADY_SENT")
            return
        message = OutboxEmail(
            id=f"email-{summary.revision}",
            to=summary.recipient,
            subject=summary.subject,
            body=summary.body,
            status=DeliveryStatus.PENDING,
        )
        status = await self._mailer.send(message)
        builder.event("tool_completed", tool="send_summary", status=status.value)
        email.status = status
        if status is not DeliveryStatus.SIMULATED_SENT:
            builder.say("policy.email_failed")
            # A retry answers for this same summary.
            state.pending_question = PendingQuestion(
                kind=PendingKind.EMAIL_CONSENT, asked_turn=builder.turn, candidates=[str(summary.revision)]
            )
            builder.ask = PlanAsk(slot="email_retry")
            builder.rule = "EMAIL_FAILED"
            return
        email.choice = EmailChoice.SEND
        email.sent_revisions.append(summary.revision)
        state.outbox.append(message.model_copy(update={"status": status}))
        builder.say(
            Fact(
                id="email.sent",
                text=(
                    f"I've sent the summary to {mask_email(summary.recipient)}. This is a demo, "
                    "so it appears in the demo outbox instead of being delivered."
                ),
                source="mailer",
                party_id=state.identity.party_id,
            )
        )
        self._wrap_up(state, builder, "EMAIL_SENT", ending=ending)

    @classmethod
    def _wrap_up(cls, state: SessionState, builder: _Builder, rule: str, *, ending: bool) -> None:
        """After the email step, a caller who is saying goodbye gets a goodbye, not another question."""
        if ending:
            cls._end(state, builder, rule)
        else:
            cls._ask_closing(state, builder, rule)

    @staticmethod
    def _end(state: SessionState, builder: _Builder, rule: str) -> None:
        """The SOP is complete and the caller has nothing more: say goodbye and end the conversation."""
        state.status = SessionStatus.ENDED
        state.pending_question = None
        builder.say("policy.closing")
        builder.event("conversation_ended")
        builder.rule = rule

    @staticmethod
    def _ask_closing(state: SessionState, builder: _Builder, rule: str) -> None:
        state.pending_question = PendingQuestion(kind=PendingKind.ANYTHING_ELSE, asked_turn=builder.turn)
        builder.ask = PlanAsk(slot="closing", variant=_next_variant(state, "closing"))
        builder.rule = rule

    # --- handoff, case cycles, helpers ------------------------------------------------

    def _hand_off(self, state: SessionState, reason: str, builder: _Builder) -> None:
        """A simulated transfer; before access is granted it carries no claim data (6.4 rule 8)."""
        granted = state.access_granted
        case_id = state.case_context.selected_case_id if granted else None
        state.handoff = HandoffRecord(reason=reason, turn=builder.turn, verified=granted, case_id=case_id)
        state.status = SessionStatus.HANDED_OFF
        state.pending_question = None
        if case_id:
            text = (
                "I've asked for a human claims representative to take over. This is a demo, so no one "
                f"will actually join; in a real system they would pick up with claim {case_id}, so you "
                "wouldn't have to start over."
            )
        else:
            text = (
                "I've asked for a human representative to take over. This is a demo, so no one will "
                "actually join. For your security, a representative would verify your identity before "
                "discussing any claim details."
            )
        builder.say(
            Fact(
                id="handoff.created",
                text=text,
                source="handoff",
                case_id=case_id,
                party_id=state.identity.party_id if granted else None,
            )
        )
        builder.event("tool_completed", tool="create_handoff", status=reason)
        builder.rule = "HANDED_OFF"

    def _asks_for_another_claim(self, state: SessionState, validated: ledger_ops.ValidatedTurn) -> bool:
        """New case hints this turn that the selected claim does not satisfy."""
        selected = state.case_context.selected_case_id
        if selected is None or not validated.case_hints or not state.access_granted:
            return False
        claim = self._claims.get_claim(self._trusted(state), selected)
        new_hints = {hint: value for hint, _, value in validated.case_hints if value is not None}
        return not matches_hints(claim, new_hints)

    def _start_new_case_cycle(
        self, state: SessionState, intent: Intent, as_of: date, builder: _Builder
    ) -> None:
        if state.case_context.selected_case_id:
            # The summary still recaps a claim the caller already went over.
            claim = self._selected_claim(state)
            link = upload_link(self._portal_url, claim.case_id)
            state.earlier_claims.append(earlier_claim_recap(claim, as_of, link))
        state.case_cycle_id += 1
        # Keep only the hints and intent given this turn; older ones described the previous claim.
        for entry in state.ledger.with_prefix("case_hint."):
            if entry.turn != builder.turn:
                state.ledger.remove(entry.key)
        self._reset_case(state)
        state.case_context.intent = intent if intent is not Intent.UNKNOWN else None
        builder.event("case_cycle_started", status=str(state.case_cycle_id))
        self._enter(state, Phase.RESOLVE_INTENT, "NEW_CASE_CYCLE", builder)

    def _selected_claim(self, state: SessionState) -> Claim:
        return self._claims.get_claim(self._trusted(state), state.case_context.selected_case_id)

    def _trusted(self, state: SessionState) -> TrustedContext:
        if not state.access_granted or state.identity.party_id is None:
            raise NotAuthorized("claim access requires a verified, authorized caller")
        return TrustedContext(
            party_id=state.identity.party_id, phase=state.phase, case_cycle_id=state.case_cycle_id
        )

    @staticmethod
    def _reset_case(state: SessionState) -> None:
        """Forget the current case, its facts, its summary and any email consent (6.4 rule 5)."""
        state.case_context = CaseContext(intent=state.case_context.intent)
        state.fact_bundle = {}
        state.pending_question = None
        state.summary = None
        state.email = EmailState(
            sent_revisions=state.email.sent_revisions, last_revision=state.email.last_revision
        )

    @staticmethod
    def _enter(state: SessionState, phase: Phase, rule: str, builder: _Builder) -> None:
        state.phase = phase
        if phase is not Phase.VERIFY_ID:
            state.subflow_step = None
        builder.rule = rule
        builder.event("phase_changed", rule=rule, phase=phase)


def _next_variant(state: SessionState, slot: str) -> int:
    asked = state.counters.asked.get(slot, 0)
    state.counters.asked[slot] = asked + 1
    return asked


def _account(state: SessionState) -> str:
    return "the policyholder's account" if state.caller.role is CallerRole.REPRESENTATIVE else "your account"


def _candidates_fact(state: SessionState, claims: list[Claim]) -> Fact:
    listing = "; ".join(brief_fact(claim).text for claim in claims)
    label = "this claim" if len(claims) == 1 else "these claims"
    return Fact(
        id="search.candidates",
        text=f"I see {label} on {_account(state)}: {listing}.",
        source="claims.json",
        party_id=state.identity.party_id,
    )


def _candidate_numbers_fact(state: SessionState, claims: list[Claim]) -> Fact:
    ids = [claim.case_id for claim in claims]
    listing = ids[0] if len(ids) == 1 else f"{', '.join(ids[:-1])} and {ids[-1]}"
    return Fact(
        id="search.candidate_numbers",
        text=f"The claims on {_account(state)} are {listing}.",
        source="claims.json",
        party_id=state.identity.party_id,
    )


def _no_claims_fact(state: SessionState) -> Fact:
    # Even "no claims" is account information, so it carries the party like any claim fact.
    return Fact(
        id="search.no_claims",
        text=f"I don't see any claims on {_account(state)}.",
        source="claims.json",
        party_id=state.identity.party_id,
    )
