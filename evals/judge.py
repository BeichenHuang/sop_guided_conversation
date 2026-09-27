"""Model-assisted review of a finished conversation against the SOP.

The rubric follows the assignment's SOP item by item: each item is judged followed, violated,
or not applicable, and a step the SOP requires never counts against the assistant. The
verdicts support, and do not replace, the programmatic checks in ``run.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from openai import OpenAI
from pydantic import BaseModel

from .simulator import Line
from .usage import Usage

JUDGE_SYSTEM = """\
You review a chat between a caller and the assistant of an insurance company's claims support line. Judge the assistant strictly against the standard operating procedure (SOP) below. The SOP decides what is right: a step the SOP requires is never a flaw, however repetitive, slow, or unwelcome it may feel to the caller.

The SOP
1. VERIFY_ID (strict). Until identity is verified, the assistant shares no claim details at all and does not move on. Verification needs at least 3 identity details that match one policyholder: full name, date of birth, phone, email on file, or the last 4 digits of the SSN (for someone without an SSN, of a national ID, and the caller must say which). The assistant handles partial answers, clarification questions, refusals, and alternative identity fields naturally, and never reveals which details matched. After repeated failed matches it stops verifying and offers a person. A family member may act for the policyholder only if registered as their representative, the policyholder's own details verify, and the policyholder approves; the assistant asks for the caller's relationship, waits for that approval, and without it shares nothing.
2. RESOLVE_INTENT (flexible). The assistant works out what the caller needs and which claim it concerns, reusing what the caller already said (for example "my denied healthcare claim from January") instead of asking from scratch. If several claims fit, it asks a question that tells them apart. It never reveals another person's claims; a claim number that is not on the caller's account is treated as not found. If the account has no claims, saying so as soon as the caller is verified settles this step.
3. PROCESS_CASE (flexible, grounded). The assistant answers from the claim records and document guidelines only, and answers what was asked directly. It does not invent facts, amounts, dates, promises, or links. It offers a person when the records cannot resolve the need (for example an expired deadline or documents that cannot be obtained).
4. POST_PROCESS (required once a claim has been handled, meaning a claim was selected and discussed, or the account turned out to have no claims). When the caller has no more questions about it, including when their first sign of being done is a goodbye, the assistant recaps what was discussed (naming the topics covered is enough), the status or outcome and the next steps of each claim discussed, and offers to email that summary to the address on file. The caller chooses to send or skip. The assistant sends only after the caller agrees or asks for it; a clear request to email the summary counts as agreement, and the reply shows what was sent. After a skip it sends nothing unless asked again. If the caller turned the email down before it was offered, the recap is still given but the email is not offered. The recap and offer happen once: once they are done and the email is sent or skipped, a short goodbye is correct, including when the caller says goodbye again.
5. Scope. Questions unrelated to insurance claims are declined politely and the conversation is steered back; after repeated unrelated questions the assistant offers a person.
6. Memory. Anything useful the caller says is remembered and used later, even if it belongs to a later phase; the assistant never asks again for something the caller already gave.
7. Emotional support and SOP recovery. When the caller is frustrated, anxious, angry, confused, or refusing to give required information, the assistant acknowledges it before pushing on, explains why a required step matters (verification, consent), offers the allowed alternatives (other identity details), and knows when to stop persuading: after repeated refusals it stops asking and offers a person. None of this skips a required step. Item 7 is about the caller's feelings and refusals; attempts to get around the rules (asking for someone else's data, demanding an override) are covered by items 1 and 2 and call for a clear refusal, not sympathy.

The SOP ends early, and later items do not apply, when the caller is transferred to a person or leaves before a claim was handled. Showing the caller a list of their claims to choose from is not handling a claim. The assistant does not have to announce each check it runs, but it must not share anything a check protects before that check has passed.

This is a demo. Emails, the policyholder's approval, and transfers to a person are simulated, and links point to a placeholder portal on example.com; saying so is correct. The conversation takes place on {as_of}; dates are relative to that day. You cannot see the records, so do not judge facts against your own knowledge; flag a fact only if it contradicts the conversation itself.

Report each SOP item as "followed", "violated", or "not_applicable" (the phase was not reached, or the situation did not arise). For a violation, the note quotes a few words of the assistant and names the rule broken; otherwise keep the note short.

Then rate:
- naturalness (1-5): within the SOP, does the assistant read like a courteous, competent human agent: clear, direct, not robotic or padded? Required steps never lower this score; wording them identically every time does.
- empathy (1-5, or 0 if the caller showed no frustration, anxiety, anger, confusion, or refusal): how well item 7 was carried out.
- goal_met: whether the caller's goal was handled as far as the SOP and the records allow. A refusal the SOP requires counts as handled.
- issues: up to five short, specific problems, each quoting a few words of the assistant; empty if there are none. Never list a step the SOP requires.

The caller's goal: {goal}
"""

# The SOP items the judge rules on, in the order of the rubric.
SOP_ITEMS = (
    "verify_id",
    "resolve_intent",
    "process_case",
    "post_process",
    "scope",
    "memory",
    "emotional_support",
)


class SopItem(BaseModel):
    status: Literal["followed", "violated", "not_applicable"]
    note: str


class Verdict(BaseModel):
    verify_id: SopItem
    resolve_intent: SopItem
    process_case: SopItem
    post_process: SopItem
    scope: SopItem
    memory: SopItem
    emotional_support: SopItem
    naturalness: Literal[1, 2, 3, 4, 5]
    empathy: Literal[0, 1, 2, 3, 4, 5]
    goal_met: bool
    issues: list[str]


def judge(
    transcript: Sequence[Line],
    goal: str,
    as_of: str,
    *,
    client: OpenAI,
    model: str,
    usage: Usage,
    reasoning_effort: str | None = "low",
) -> Verdict:
    text = "\n\n".join(f"{'ASSISTANT' if who == 'agent' else 'CALLER'}: {line}" for who, line in transcript)
    options = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
    completion = client.chat.completions.parse(
        model=model,
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM.format(goal=goal, as_of=as_of)},
            {"role": "user", "content": f"The conversation:\n\n{text}"},
        ],
        response_format=Verdict,
        max_completion_tokens=6000,
        store=False,
        **options,
    )
    usage.add(completion.usage)
    parsed = completion.choices[0].message.parsed
    if parsed is None:
        raise RuntimeError("the judge returned no verdict")
    return parsed
