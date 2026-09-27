"""In-memory session store. Single process only: all state is lost on restart."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .domain import SessionState

SESSION_IDLE_TTL = timedelta(hours=1)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class SessionRecord:
    state: SessionState
    last_active: datetime
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False


class SessionStore:
    def __init__(
        self,
        *,
        idle_ttl: timedelta = SESSION_IDLE_TTL,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._idle_ttl = idle_ttl
        self._now = now
        self._records: dict[str, SessionRecord] = {}

    @staticmethod
    def new_session_id() -> str:
        return secrets.token_urlsafe(32)

    def add(self, state: SessionState) -> SessionRecord:
        self._purge_expired()
        record = SessionRecord(state=state, last_active=self._now())
        self._records[state.session_id] = record
        return record

    def get(self, session_id: str) -> SessionRecord | None:
        record = self.peek(session_id)
        if record is not None:
            record.last_active = self._now()
        return record

    def peek(self, session_id: str) -> SessionRecord | None:
        """Like get, but not counted as activity, so watching a session doesn't keep it alive."""
        record = self._records.get(session_id)
        if record is None:
            return None
        if self._now() - record.last_active > self._idle_ttl:
            self.delete(session_id)
            return None
        return record

    def delete(self, session_id: str) -> None:
        record = self._records.pop(session_id, None)
        if record is not None:
            record.closed = True

    def __len__(self) -> int:
        return len(self._records)

    def _purge_expired(self) -> None:
        cutoff = self._now() - self._idle_ttl
        for session_id in [sid for sid, r in self._records.items() if r.last_active < cutoff]:
            self.delete(session_id)
