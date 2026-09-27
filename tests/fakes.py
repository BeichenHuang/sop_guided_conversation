"""Test doubles: a scripted model adapter and the fixed test date."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import replace
from datetime import date

from apps.insurance_claims.config import Settings
from apps.insurance_claims.domain import ResponseDraft, TurnUnderstanding
from apps.insurance_claims.llm.adapter import RealizeContext, UnderstandContext
from apps.insurance_claims.planner import render_plan

TODAY = date(2026, 9, 25)


def make_settings(**overrides) -> Settings:
    """Settings for tests: no consent polling delay, and invariant violations raise."""
    return replace(Settings(consent_poll_interval=0.0, strict_invariants=True), **overrides)


class ScriptedAdapter:
    """Returns queued results in order; an ``Exception`` in a queue is raised instead.

    With an empty queue, understanding is empty and the reply is the plan's
    template rendering.
    """

    provider = "scripted"
    model: str | None = None
    is_real_model = False

    def __init__(
        self,
        understandings: Iterable[TurnUnderstanding | Exception] = (),
        drafts: Iterable[ResponseDraft | Exception] = (),
    ) -> None:
        self.understandings = deque(understandings)
        self.drafts = deque(drafts)
        self.understand_calls: list[UnderstandContext] = []
        self.realize_calls: list[RealizeContext] = []

    async def understand_turn(self, context: UnderstandContext) -> TurnUnderstanding:
        self.understand_calls.append(context)
        item = self.understandings.popleft() if self.understandings else TurnUnderstanding()
        if isinstance(item, Exception):
            raise item
        return item

    async def realize_reply(self, context: RealizeContext) -> ResponseDraft:
        self.realize_calls.append(context)
        if self.drafts:
            item = self.drafts.popleft()
        else:
            item = ResponseDraft(text=render_plan(context.plan, context.facts))
        if isinstance(item, Exception):
            raise item
        return item
