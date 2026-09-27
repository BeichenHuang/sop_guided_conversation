from __future__ import annotations

from apps.insurance_claims.config import DEFAULT_FIXTURES_DIR
from apps.insurance_claims.domain import (
    CallerRole,
    ChatMessage,
    DateMode,
    Fact,
    LedgerEntry,
    LedgerSource,
    MessageRole,
    PlanAsk,
    ReplyPlan,
    SessionState,
)
from apps.insurance_claims.fixture_loader import load_fixtures
from apps.insurance_claims.invariants import (
    ASKED_FOR_KNOWN_FIELD,
    CLAIM_FACT_BEFORE_ACCESS,
    CLAIM_VALUE_BEFORE_ACCESS,
    FOREIGN_CLAIM_FACT,
    FOREIGN_CLAIM_VALUE,
    Invariants,
)

INVARIANTS = Invariants(load_fixtures(DEFAULT_FIXTURES_DIR))
CL_2048 = Fact(
    id="claim.CL-2048.overview",
    text="I found the healthcare claim CL-2048.",
    source="claims.json:CL-2048",
    case_id="CL-2048",
    party_id="P9",
)
CL_3001 = Fact(
    id="claim.CL-3001.overview",
    text="I found the healthcare claim CL-3001.",
    source="claims.json:CL-3001",
    case_id="CL-3001",
    party_id="P12",
)


def state(verified_party=None):
    s = SessionState(session_id="s", date_mode=DateMode.DEMO, consent_scenario="default")
    if verified_party:
        s.identity.verified = True
        s.identity.party_id = verified_party
        s.caller.role = CallerRole.SELF
    return s


def codes(s, plan=None, facts=None, reply="", messages=()):
    return [v.code for v in INVARIANTS.check(s, plan or ReplyPlan(), facts or {}, reply, messages)]


def test_nothing_fires_on_a_clean_turn():
    assert codes(state(), reply="Please share your full name.") == []


def test_claim_facts_before_access_are_caught():
    plan = ReplyPlan(inform=[CL_2048.id])
    assert CLAIM_FACT_BEFORE_ACCESS in codes(state(), plan, {CL_2048.id: CL_2048})


def test_claim_values_in_a_reply_before_access_are_caught():
    assert codes(state(), reply="Your claim CL-2048 was denied.") == [CLAIM_VALUE_BEFORE_ACCESS]


def test_a_claim_number_typed_by_the_user_is_not_a_leak():
    typed = ChatMessage(id="u-1", role=MessageRole.USER, text="about CL-2048", turn=1, case_cycle_id=1)
    assert codes(state(), messages=[typed]) == []


def test_another_persons_fact_after_access_is_caught():
    assert FOREIGN_CLAIM_FACT in codes(state("P9"), facts={CL_3001.id: CL_3001})


def test_another_persons_claim_details_in_a_reply_are_caught():
    reply = "the submitted materials did not include the treating provider diagnosis report"
    assert codes(state("P9"), reply=reply) == [FOREIGN_CLAIM_VALUE]


def test_echoing_a_claim_number_the_caller_gave_is_allowed():
    assert codes(state("P9"), reply="I don't see a claim numbered CL-3001 on your account.") == []


def test_asking_again_for_a_known_field_is_caught():
    s = state()
    s.ledger.put(
        LedgerEntry(key="identity.full_name", value="Margaret Chen", source=LedgerSource.USER_SAID, turn=1)
    )
    plan = ReplyPlan(ask=PlanAsk(slot="identity", one_of=["full_name", "dob"], count=2))
    assert codes(s, plan) == [ASKED_FOR_KNOWN_FIELD]
