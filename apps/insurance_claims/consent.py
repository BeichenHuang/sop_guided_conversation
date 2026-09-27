"""Representative authorization: the registered-representative check and the mock
policyholder consent service."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass

from .domain import ConsentStatus
from .fixture_loader import ConsentScenario, Representative
from .identity import normalize_name

MAX_CONSENT_POLLS = 5

# Words around a relationship that do not change it: "I'm her son" -> "son".
_FILLER = frozenset({"i", "im", "i'm", "am", "is", "her", "his", "their", "my", "the", "a", "an"})
_RELATIONSHIP_SYNONYMS = {
    "mom": "mother",
    "mum": "mother",
    "dad": "father",
    "wife": "spouse",
    "husband": "spouse",
}


def normalize_relationship(value: str) -> str:
    words = [w for w in normalize_name(value).split() if w not in _FILLER]
    return " ".join(_RELATIONSHIP_SYNONYMS.get(w, w) for w in words)


def is_registered_representative(
    representatives: Iterable[Representative], party_id: str, name: str, relationship: str
) -> bool:
    """Only this policyholder's records are consulted, and the answer is a bare yes or no."""
    wanted_name = normalize_name(name)
    wanted_relationship = normalize_relationship(relationship)
    return any(
        rep.buyer_party_id == party_id
        and normalize_name(rep.rep_name) == wanted_name
        and normalize_relationship(rep.relationship) == wanted_relationship
        for rep in representatives
    )


@dataclass(frozen=True)
class ConsentOutcome:
    status: ConsentStatus
    polls: int


class ConsentService:
    """Replays a scenario's status sequence, one entry per poll."""

    def __init__(
        self,
        scenarios: Mapping[str, ConsentScenario],
        *,
        poll_interval: float = 0.0,
        max_polls: int = MAX_CONSENT_POLLS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._scenarios = scenarios
        self._poll_interval = poll_interval
        self._max_polls = max_polls
        self._sleep = sleep

    @property
    def max_polls(self) -> int:
        return self._max_polls

    async def request_and_wait(
        self, scenario: str, on_poll: Callable[[int, str], None] | None = None
    ) -> ConsentOutcome:
        sequence = self._scenarios[scenario].status_sequence
        for poll in range(1, self._max_polls + 1):
            status = sequence[min(poll, len(sequence)) - 1]
            if on_poll:
                on_poll(poll, status)
            if status == "approved":
                return ConsentOutcome(ConsentStatus.APPROVED, poll)
            if poll < self._max_polls:
                await self._sleep(self._poll_interval)
        return ConsentOutcome(ConsentStatus.TIMEOUT, self._max_polls)
