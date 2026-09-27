from __future__ import annotations

import asyncio

import pytest

from apps.insurance_claims.config import DEFAULT_FIXTURES_DIR
from apps.insurance_claims.consent import (
    ConsentService,
    is_registered_representative,
    normalize_relationship,
)
from apps.insurance_claims.domain import ConsentStatus
from apps.insurance_claims.fixture_loader import load_fixtures

FIXTURES = load_fixtures(DEFAULT_FIXTURES_DIR)


def run(service, scenario):
    polls = []
    outcome = asyncio.run(service.request_and_wait(scenario, on_poll=lambda n, s: polls.append((n, s))))
    return outcome, polls


def test_default_scenario_is_approved_on_the_second_poll():
    outcome, polls = run(ConsentService(FIXTURES.consent_scenarios), "default")
    assert (outcome.status, outcome.polls) == (ConsentStatus.APPROVED, 2)
    assert polls == [(1, "pending"), (2, "approved")]


def test_timeout_scenario_gives_up_after_five_polls():
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    service = ConsentService(FIXTURES.consent_scenarios, poll_interval=1.0, sleep=fake_sleep)
    outcome, polls = run(service, "timeout")
    assert (outcome.status, outcome.polls) == (ConsentStatus.TIMEOUT, 5)
    assert [status for _, status in polls] == ["pending"] * 5
    assert waits == [1.0] * 4  # no wait after the last poll


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("son", "son"), ("I'm her son", "son"), ("I am her Son", "son"), ("her mom", "mother")],
)
def test_relationship_normalization(raw, expected):
    assert normalize_relationship(raw) == expected


@pytest.mark.parametrize(
    ("party", "name", "relationship", "expected"),
    [
        ("P9", "David Chen", "son", True),
        ("P9", "  david   CHEN ", "I'm her son", True),
        ("P9", "David Chen", "daughter", False),
        ("P9", "John Doe", "son", False),
        ("P12", "David Chen", "son", False),
    ],
)
def test_only_the_registered_representative_of_that_policyholder_passes(party, name, relationship, expected):
    assert is_registered_representative(FIXTURES.representatives, party, name, relationship) is expected
