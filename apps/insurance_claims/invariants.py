"""Fixed rules checked on every turn.

These are a last line of defence, independent of the controller logic they
check. In tests they run in strict mode and raise; in the app a violation is
traced and the reply is replaced with a safe one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .domain import ChatMessage, Fact, MessageRole, ReplyPlan, SessionState
from .fixture_loader import FixtureData

CLAIM_FACT_BEFORE_ACCESS = "CLAIM_FACT_BEFORE_ACCESS"
CLAIM_VALUE_BEFORE_ACCESS = "CLAIM_VALUE_BEFORE_ACCESS"
FOREIGN_CLAIM_FACT = "FOREIGN_CLAIM_FACT"
FOREIGN_CLAIM_VALUE = "FOREIGN_CLAIM_VALUE"
ASKED_FOR_KNOWN_FIELD = "ASKED_FOR_KNOWN_FIELD"

# Violations that mean the reply text itself must not be shown.
REPLY_LEAKS = frozenset(
    {CLAIM_FACT_BEFORE_ACCESS, CLAIM_VALUE_BEFORE_ACCESS, FOREIGN_CLAIM_FACT, FOREIGN_CLAIM_VALUE}
)


@dataclass(frozen=True)
class Violation:
    code: str
    detail: str


class InvariantViolation(RuntimeError):
    def __init__(self, violations: list[Violation]) -> None:
        super().__init__("; ".join(f"{v.code}: {v.detail}" for v in violations))
        self.violations = violations


class Invariants:
    def __init__(self, fixtures: FixtureData) -> None:
        # Distinctive claim values that must never be shown to the wrong person.
        self._values: list[tuple[str, str, str]] = []  # (party_id, case_id, lowercased value)
        for claim in fixtures.claims:
            for value in (claim.case_id, claim.summary, claim.denial_reason):
                if value:
                    self._values.append((claim.party_id, claim.case_id, value.casefold()))

    def check(
        self,
        state: SessionState,
        plan: ReplyPlan,
        facts: Mapping[str, Fact],
        reply_text: str,
        context_messages: Iterable[ChatMessage],
    ) -> list[Violation]:
        violations: list[Violation] = []
        shown = [facts[fact_id] for fact_id in plan.inform if fact_id in facts]
        # Only text the system produces counts; users may type claim numbers themselves.
        system_texts = [reply_text, *(f.text for f in facts.values())]
        system_texts += [m.text for m in context_messages if m.role is MessageRole.ASSISTANT]

        if not state.access_granted:
            for fact in shown:
                if fact.case_id or fact.party_id:
                    violations.append(Violation(CLAIM_FACT_BEFORE_ACCESS, fact.id))
            for _, case_id, value in self._values:
                if any(value in text.casefold() for text in system_texts):
                    violations.append(Violation(CLAIM_VALUE_BEFORE_ACCESS, case_id))
        else:
            party = state.identity.party_id
            for fact in facts.values():
                if fact.party_id and fact.party_id != party:
                    violations.append(Violation(FOREIGN_CLAIM_FACT, fact.id))
            for owner, case_id, value in self._values:
                # Echoing a claim number the caller typed is fine; its details are not.
                if owner != party and value != case_id.casefold():
                    if any(value in text.casefold() for text in system_texts):
                        violations.append(Violation(FOREIGN_CLAIM_VALUE, case_id))

        if plan.ask and plan.ask.slot in ("identity", "policyholder_identity"):
            known = {f.value for f in state.identity_fields()}
            repeated = sorted(known & set(plan.ask.one_of))
            if repeated:
                violations.append(Violation(ASKED_FOR_KNOWN_FIELD, ",".join(repeated)))
        return violations
