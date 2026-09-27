"""A downloadable record of one conversation, from the SOP console: Markdown to read, JSON to process.

It holds what the console shows (the redacted session view, every turn's messages, reply plan and
trace, and the demo outbox), so it reveals nothing the console doesn't.
"""

from __future__ import annotations

import json
from datetime import datetime

from .domain import ConsoleOut, ConsoleTurn, ReplyPlan

_PHASE_LABELS = {
    "VERIFY_ID": "Verify identity",
    "RESOLVE_INTENT": "Resolve intent",
    "PROCESS_CASE": "Process case",
    "POST_PROCESS": "Post-process",
}


def transcript_json(console: ConsoleOut, exported_at: datetime) -> str:
    data = {"exported_at": exported_at.isoformat(timespec="seconds"), **console.model_dump(mode="json")}
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def transcript_markdown(console: ConsoleOut, exported_at: datetime) -> str:
    view = console.session
    lines = [
        "# Claims support conversation",
        "",
        f"- Exported: {exported_at:%Y-%m-%d %H:%M} UTC",
        f"- As-of date: {view.as_of_date} ({view.date_mode.value} date)",
        f"- Consent scenario: {view.consent_scenario}",
        f"- Final phase: {_PHASE_LABELS.get(view.phase.value, view.phase.value)}, status {view.status.value}",
        f"- Selected claim: {view.selected_case_id or 'none'}",
        f"- Email summary: {view.email_status.value.replace('_', ' ')}",
        "",
        "## Gates",
        "",
        *(f"- {gate.name}: {gate.state.replace('_', ' ')} ({gate.detail})" for gate in view.gates),
        "",
    ]
    for turn in console.turns:
        lines += _turn(turn)
    if console.outbox.emails:
        lines += ["## Demo outbox", "", "Simulated; no real email was sent.", ""]
        for email in console.outbox.emails:
            status = email.status.value.replace("_", " ")
            lines += [
                f"### {email.subject}",
                "",
                f"To {email.to}, {status}.",
                "",
                "```text",
                email.body,
                "```",
                "",
            ]
    return "\n".join(lines)


def _turn(turn: ConsoleTurn) -> list[str]:
    built = next((e for e in turn.trace if e.event in ("plan_built", "session_opened")), None)
    title = "Welcome" if turn.turn == 0 else f"Turn {turn.turn}"
    if built and built.rule:
        phase = _PHASE_LABELS.get(built.phase.value, built.phase.value) if built.phase else None
        title += f": {built.rule}" + (f" ({phase})" if phase else "")
    lines = [f"## {title}", ""]
    for label, message in (("Caller", turn.caller), ("Agent", turn.reply)):
        if message:
            lines += [f"**{label}**", "", *_quote(message.text), ""]
    if turn.plan:
        lines += ["Reply plan:", "", "```json", _plan(turn.plan), "```", ""]
    if turn.trace:
        lines += ["Trace:", ""]
        for event in turn.trace:
            details = ", ".join(d for d in (event.rule, event.tool, event.status) if d)
            lines.append(f"- {event.event}" + (f": {details}" if details else ""))
        lines.append("")
    return lines


def _quote(text: str) -> list[str]:
    return [f"> {line}" if line else ">" for line in text.splitlines()]


def _plan(plan: ReplyPlan) -> str:
    shown = {k: v for k, v in plan.model_dump(mode="json").items() if v is not None and v != []}
    return json.dumps(shown, indent=2, ensure_ascii=False)
