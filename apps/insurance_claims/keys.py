"""API keys entered in the SOP console, so a visitor to the demo can use their own model account.

A key lives only in this process's memory, inside the model adapter built for it. The browser gets
a random ID in an HttpOnly cookie, never the key; pages see at most its last four characters. Keys
are kept apart from conversations, so a new conversation keeps using the same key.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .llm.adapter import LLMAdapter

KEY_IDLE_TTL = timedelta(hours=12)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def key_hint(api_key: str) -> str:
    """Enough to recognise a key, not enough to use it."""
    return f"…{api_key[-4:]}"


@dataclass
class KeyEntry:
    adapter: LLMAdapter
    hint: str
    last_used: datetime


class KeyStore:
    def __init__(self, *, idle_ttl: timedelta = KEY_IDLE_TTL, now: Callable[[], datetime] = _utcnow) -> None:
        self._idle_ttl = idle_ttl
        self._now = now
        self._entries: dict[str, KeyEntry] = {}

    def add(self, adapter: LLMAdapter, hint: str) -> str:
        """Keep an adapter that holds a key; returns the ID for the browser's cookie."""
        self._purge_expired()
        key_id = secrets.token_urlsafe(32)
        self._entries[key_id] = KeyEntry(adapter=adapter, hint=hint, last_used=self._now())
        return key_id

    def get(self, key_id: str) -> KeyEntry | None:
        entry = self._entries.get(key_id)
        if entry is None:
            return None
        now = self._now()
        if now - entry.last_used > self._idle_ttl:
            del self._entries[key_id]
            return None
        entry.last_used = now
        return entry

    def delete(self, key_id: str) -> None:
        self._entries.pop(key_id, None)

    def __len__(self) -> int:
        return len(self._entries)

    def _purge_expired(self) -> None:
        cutoff = self._now() - self._idle_ttl
        for key_id in [k for k, entry in self._entries.items() if entry.last_used < cutoff]:
            del self._entries[key_id]
