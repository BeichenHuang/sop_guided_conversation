"""Simulated callers: a model plays a persona with a hidden goal."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence
from typing import Literal, Protocol

from openai import OpenAI
from pydantic import BaseModel

from .usage import Usage

Speaker = Literal["agent", "caller"]
Line = tuple[Speaker, str]

CALLER_SYSTEM = """\
You are role-playing a person who is chatting with an insurance company's claims support assistant. Stay in character.

Your persona and what you want:
{persona}

Rules
- Write only your next chat message: one to three short sentences, casual and natural. No stage directions or quotes.
- Follow your persona's instructions about which details to share and when. Answer the assistant's questions when your persona allows it.
- Your persona's instructions come first. Never ask for something your persona says to decline (for example, an email summary), and before you leave, make sure every step your persona lists is done.
- Never invent personal details, claim numbers, dates, or amounts that are not in your persona. If asked for something you don't have, say so.
- Keep to your goal. Don't repeat a question the assistant has already answered.
- If the assistant's last message asks you something, answer it: the conversation isn't over while it waits for you. Say goodbye only once your goal has been handled or clearly can't be.
- Set done to true, with an empty message, only when your last message was a goodbye or accepted a transfer to a person and the assistant's reply asks you nothing more, or when the assistant says it cannot continue. Otherwise set done to false.
"""


class CallerTurn(BaseModel):
    done: bool
    message: str


class Caller(Protocol):
    def next_turn(self, transcript: Sequence[Line]) -> CallerTurn: ...


class SimulatedCaller:
    """A persona played by an OpenAI model, which sees only the visible conversation."""

    def __init__(
        self, persona: str, *, client: OpenAI, model: str, usage: Usage, reasoning_effort: str | None = "low"
    ) -> None:
        self._system = CALLER_SYSTEM.format(persona=persona)
        self._client = client
        self._model = model
        self._usage = usage
        self._reasoning_effort = reasoning_effort

    def next_turn(self, transcript: Sequence[Line]) -> CallerTurn:
        # The simulator speaks as the caller, so the roles are swapped from the agent's point of view.
        messages = [{"role": "system", "content": self._system}]
        messages += [
            {"role": "user" if speaker == "agent" else "assistant", "content": text}
            for speaker, text in transcript
        ]
        options = {"reasoning_effort": self._reasoning_effort} if self._reasoning_effort else {}
        completion = self._client.chat.completions.parse(
            model=self._model,
            messages=messages,
            response_format=CallerTurn,
            max_completion_tokens=2000,
            store=False,
            **options,
        )
        self._usage.add(completion.usage)
        parsed = completion.choices[0].message.parsed
        if parsed is None:
            raise RuntimeError("the simulated caller returned no message")
        return parsed


class ScriptedCaller:
    """Sends fixed messages in order, then ends; for testing the harness without a model."""

    def __init__(self, messages: Iterable[str]) -> None:
        self._messages = deque(messages)

    def next_turn(self, transcript: Sequence[Line]) -> CallerTurn:
        if not self._messages:
            return CallerTurn(done=True, message="")
        return CallerTurn(done=False, message=self._messages.popleft())
