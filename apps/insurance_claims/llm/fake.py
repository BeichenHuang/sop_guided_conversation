"""Stand-in adapter used when no real model is configured.

It does not interpret messages: every understanding is empty and every reply is
the plan's template rendering. The UI labels this mode so it is never mistaken
for the real agent.
"""

from __future__ import annotations

from ..domain import ResponseDraft, TurnUnderstanding
from ..planner import render_plan
from .adapter import RealizeContext, UnderstandContext


class FakeAdapter:
    provider = "fake"
    model: str | None = None
    is_real_model = False

    async def understand_turn(self, context: UnderstandContext) -> TurnUnderstanding:
        return TurnUnderstanding()

    async def realize_reply(self, context: RealizeContext) -> ResponseDraft:
        return ResponseDraft(text=render_plan(context.plan, context.facts))
