from __future__ import annotations

import asyncio

import pytest

from apps.insurance_claims.config import DEFAULT_FIXTURES_DIR, FixedClock, Settings
from apps.insurance_claims.domain import DateMode, TurnUnderstanding
from apps.insurance_claims.engine import ConversationEngine, SessionGoneError
from apps.insurance_claims.fixture_loader import load_fixtures
from apps.insurance_claims.store import SessionStore
from tests.fakes import TODAY, ScriptedAdapter, make_settings


class SlowAdapter(ScriptedAdapter):
    """Tracks how many understanding calls overlap."""

    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.max_active = 0

    async def understand_turn(self, context):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return TurnUnderstanding()


def _engine_and_record(adapter):
    engine = ConversationEngine(
        adapter, make_settings(), FixedClock(TODAY), load_fixtures(DEFAULT_FIXTURES_DIR)
    )
    store = SessionStore()
    state, _ = engine.open_session(store.new_session_id(), DateMode.DEMO, "default")
    return engine, store, store.add(state)


def test_turns_in_one_session_run_one_at_a_time():
    adapter = SlowAdapter()
    engine, _, record = _engine_and_record(adapter)

    async def run():
        await asyncio.gather(
            engine.handle_message(record, "first"),
            engine.handle_message(record, "second"),
        )

    asyncio.run(run())
    assert adapter.max_active == 1
    assert [m.id for m in record.state.messages] == ["a-0", "u-1", "a-1", "u-2", "a-2"]
    assert record.state.turn_index == 2


def test_a_turn_for_a_deleted_session_is_rejected():
    engine, store, record = _engine_and_record(ScriptedAdapter())
    store.delete(record.state.session_id)
    with pytest.raises(SessionGoneError):
        asyncio.run(engine.handle_message(record, "hello"))
    assert record.state.turn_index == 0


def test_real_date_mode_follows_the_clock():
    engine, _, record = _engine_and_record(ScriptedAdapter())
    assert engine.as_of_date(record.state) == Settings().demo_as_of_date
    record.state.date_mode = DateMode.REAL
    assert engine.as_of_date(record.state) == TODAY
