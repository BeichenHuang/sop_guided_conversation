"""Email sending behind an interface; only the simulated sender exists."""

from __future__ import annotations

from typing import Protocol

from .domain import DeliveryStatus, OutboxEmail


class EmailSender(Protocol):
    async def send(self, email: OutboxEmail) -> DeliveryStatus: ...


class MockSender:
    """Accepts every message and sends nothing. The session's outbox records it."""

    async def send(self, email: OutboxEmail) -> DeliveryStatus:
        return DeliveryStatus.SIMULATED_SENT
