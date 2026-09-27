"""Simulated-caller evaluation of the claims agent.

A model plays each scenario's caller. The conversation runs through the app's HTTP API with
the real reply model, then ends with programmatic checks and, optionally, a rubric judge.

    uv run --env-file .env python -m evals.run                     # every scenario, 3 runs each
    uv run --env-file .env python -m evals.run --scenarios david_timeout --runs 1 --no-judge
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openai import OpenAI
from pydantic import BaseModel, Field

from apps.insurance_claims.api import create_app
from apps.insurance_claims.config import Settings
from apps.insurance_claims.llm.adapter import build_adapter
from apps.insurance_claims.llm.providers import PROVIDERS

from .judge import SOP_ITEMS, Verdict, judge
from .simulator import Caller, Line, ScriptedCaller, SimulatedCaller
from .usage import Usage

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_PATH = Path(__file__).with_name("scenarios.json")
PROMPTS_PATH = ROOT / "apps" / "insurance_claims" / "llm" / "prompts.py"
RESULTS_DIR = Path(__file__).with_name("results")
DEFAULT_JUDGE_MODEL = "gpt-6-sol"


# --- scenarios ------------------------------------------------------------------------


class Expect(BaseModel):
    verified: bool | None = None
    # A case ID; "any" skips the check, and null means no claim may be selected.
    claim: str | None = "any"
    email: Literal["sent", "not_sent", "any"] = "any"
    email_to: str | None = None
    handed_off: bool | None = None
    locked: bool | None = None
    # Each group lists alternative phrases; one of them must appear in the agent's replies.
    required: list[list[str]] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)


class Scenario(BaseModel):
    id: str
    title: str
    covers: list[str] = Field(default_factory=list)
    consent_scenario: str = "default"
    date_mode: Literal["demo", "real"] = "demo"
    max_turns: int = 10
    opening: str | None = None
    # Fixed caller messages sent in order instead of a simulated caller, for probes that need exact
    # wording (and that a model may refuse to play, such as an impostor).
    script: list[str] | None = None
    caller: str
    goal: str
    expect: Expect


def load_scenarios(path: Path = SCENARIOS_PATH) -> list[Scenario]:
    return [Scenario(**item) for item in json.loads(path.read_text())["scenarios"]]


# --- one conversation -----------------------------------------------------------------


@dataclass
class TurnRecord:
    caller: str
    agent: str
    phase: str
    rule: str | None
    reply: str | None
    interrupts: list[str]
    invariants: list[str]
    seconds: float


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class RunRecord:
    scenario: str
    run: int
    greeting: str = ""
    turns: list[TurnRecord] = field(default_factory=list)
    ended: str = "max_turns"
    error: str | None = None
    session: dict[str, Any] = field(default_factory=dict)
    outbox: list[dict[str, Any]] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    verdict: dict[str, Any] | None = None
    judge_error: str | None = None
    usage: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def transcript(self) -> list[Line]:
        lines: list[Line] = [("agent", self.greeting)]
        for turn in self.turns:
            lines += [("caller", turn.caller), ("agent", turn.agent)]
        return lines

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


def converse(scenario: Scenario, app: FastAPI, caller: Caller, run: int = 1) -> RunRecord:
    """Let ``caller`` talk to the app until it is done, hands off, fails, or runs out of turns."""
    record = RunRecord(scenario=scenario.id, run=run)
    with TestClient(app) as client:
        created = client.post(
            "/api/sessions",
            json={"consent_scenario": scenario.consent_scenario, "date_mode": scenario.date_mode},
        )
        created.raise_for_status()
        record.greeting = created.json()["reply"]["text"]
        record.session = created.json()["session"]
        for index in range(scenario.max_turns):
            if index == 0 and scenario.opening:
                message = scenario.opening
            else:
                turn = caller.next_turn(record.transcript)
                if turn.done or not turn.message.strip():
                    record.ended = "caller_done"
                    break
                message = turn.message.strip()
            started = time.perf_counter()
            response = client.post("/api/session/messages", json={"message": message})
            seconds = time.perf_counter() - started
            if response.status_code != 200:
                code = (response.json().get("error") or {}).get("code", "unknown")
                record.error = f"HTTP {response.status_code} ({code}) on turn {index + 1}"
                record.ended = "error"
                break
            data = response.json()
            trace = data["trace"]
            record.turns.append(
                TurnRecord(
                    caller=message,
                    agent=data["reply"]["text"],
                    phase=data["session"]["phase"],
                    rule=next((e["rule"] for e in trace if e["event"] == "plan_built"), None),
                    reply=next((e["status"] for e in trace if e["event"] == "reply_realized"), None),
                    interrupts=[e["rule"] for e in trace if e["event"] == "interrupt"],
                    invariants=[e["rule"] for e in trace if e["event"] == "invariant_violated"],
                    seconds=round(seconds, 2),
                )
            )
            record.session = data["session"]
            if record.session["status"] in ("handed_off", "ended"):
                # A person took over, or the conversation is over: the caller can't go on.
                record.ended = record.session["status"]
                break
        record.outbox = client.get("/api/session/outbox").json()["emails"]
    return record


def check(scenario: Scenario, record: RunRecord) -> list[Check]:
    """The scenario's expectations, checked against the final state, outbox and replies."""
    expect = scenario.expect
    session = record.session
    replies = "\n".join(text for who, text in record.transcript if who == "agent").lower()
    checks = [Check("completed", record.error is None, record.error or "")]
    violations = [code for turn in record.turns for code in turn.invariants]
    checks.append(Check("invariants", not violations, ", ".join(violations)))
    if expect.verified is not None:
        checks.append(
            Check("verified", session["verified"] is expect.verified, f"verified={session['verified']}")
        )
    if expect.claim != "any":
        selected = session["selected_case_id"]
        checks.append(Check("claim", selected == expect.claim, f"selected={selected}"))
    if expect.email != "any":
        sent = [email for email in record.outbox if email["status"] == "simulated_sent"]
        if expect.email == "sent":
            right = expect.email_to is None or all(email["to"] == expect.email_to for email in sent)
            detail = ", ".join(email["to"] for email in sent) or "nothing sent"
            checks.append(Check("email", bool(sent) and right, detail))
        else:
            checks.append(Check("email", not record.outbox, f"{len(record.outbox)} email(s) in the outbox"))
    if expect.handed_off is not None:
        handed_off = session["status"] == "handed_off"
        checks.append(Check("handoff", handed_off is expect.handed_off, f"status={session['status']}"))
    if expect.locked is not None:
        gate = next(g for g in session["gates"] if g["name"] == "Identity")
        locked = gate["state"] == "blocked"
        checks.append(Check("locked", locked is expect.locked, gate["detail"]))
    if expect.forbidden:
        leaked = [phrase for phrase in expect.forbidden if phrase.lower() in replies]
        checks.append(Check("no_forbidden_text", not leaked, ", ".join(leaked)))
    if expect.required:
        missing = [
            " / ".join(group) for group in expect.required if not any(p.lower() in replies for p in group)
        ]
        checks.append(Check("required_text", not missing, ", ".join(missing)))
    return checks


def run_one(
    scenario: Scenario,
    run: int,
    *,
    make_app: Callable[[Usage], FastAPI],
    make_caller: Callable[[Scenario, Usage], Caller],
    judge_fn: Callable[[Sequence[Line], str, str, Usage], Verdict] | None = None,
) -> RunRecord:
    agent_usage, caller_usage, judge_usage = Usage(), Usage(), Usage()
    try:
        record = converse(scenario, make_app(agent_usage), make_caller(scenario, caller_usage), run)
    except Exception as exc:  # a crashed run is reported, not fatal to the whole evaluation
        record = RunRecord(scenario=scenario.id, run=run, ended="error", error=f"{type(exc).__name__}: {exc}")
    record.checks = (
        check(scenario, record) if record.session else [Check("completed", False, record.error or "")]
    )
    if judge_fn is not None and record.turns:
        try:
            as_of = record.session["as_of_date"]
            record.verdict = judge_fn(record.transcript, scenario.goal, as_of, judge_usage).model_dump()
        except Exception as exc:
            record.judge_error = f"{type(exc).__name__}: {exc}"
    record.usage = {
        "agent": agent_usage.to_dict(),
        "caller": caller_usage.to_dict(),
        "judge": judge_usage.to_dict(),
    }
    return record


# --- reporting ------------------------------------------------------------------------


_JUDGED = ["SOP flags (judge)", "Natural", "Empathy", "Goal met"]


def _mean(values: Sequence[float]) -> str:
    return f"{statistics.mean(values):.1f}" if values else "–"


def _row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def summarize(scenarios: Sequence[Scenario], records: Sequence[RunRecord], meta: dict[str, Any]) -> str:
    by_id = {s.id: [r for r in records if r.scenario == s.id] for s in scenarios}
    lines = [
        "# Evaluation results (generated)",
        "",
        f"- Date: {meta['date']}; commit `{meta['commit']}`; prompt version `{meta['prompt_version']}`",
        f"- Agent: {meta.get('agent_provider', 'openai')} {meta['agent_model']} "
        f"(reasoning {meta['reasoning_effort']}); simulated callers: "
        f"{meta['caller_model']}; judge: {meta['judge_model'] or 'not run'}",
        f"- {meta['runs']} run(s) per scenario, {len(records)} in total; demo date {meta['demo_date']}",
        "",
        _row(["Scenario", "Covers", "Passed", "Failed checks", "Turns", "Fallbacks"] + _JUDGED),
        _row(["---"] * (6 + len(_JUDGED))),
    ]
    for scenario in scenarios:
        runs = by_id[scenario.id]
        if not runs:
            continue
        failed: dict[str, int] = {}
        for record in runs:
            for c in record.checks:
                if not c.passed:
                    failed[c.name] = failed.get(c.name, 0) + 1
        verdicts = [r.verdict for r in runs if r.verdict]
        flagged: dict[str, int] = {}
        for verdict in verdicts:
            for item in _violated(verdict):
                flagged[item] = flagged.get(item, 0) + 1
        cells = [
            f"{scenario.id} (scripted)" if scenario.script is not None else scenario.id,
            " ".join(scenario.covers),
            f"{sum(r.passed for r in runs)}/{len(runs)}",
            ", ".join(f"{name} ×{count}" for name, count in failed.items()) or "–",
            _mean([len(r.turns) for r in runs]),
            str(sum(1 for r in runs for t in r.turns if t.reply != "ok")),
            (", ".join(f"{item} ×{count}" for item, count in flagged.items()) or "none") if verdicts else "–",
            _mean([v["naturalness"] for v in verdicts]),
            _mean([v["empathy"] for v in verdicts if v["empathy"]]),
            f"{sum(v['goal_met'] for v in verdicts)}/{len(verdicts)}" if verdicts else "–",
        ]
        lines.append(_row(cells))
    turns = [t for r in records for t in r.turns]
    violations = sum(len(t.invariants) for t in turns)
    lines += [
        "",
        f"**Overall:** {sum(r.passed for r in records)}/{len(records)} runs passed every check; "
        f"{violations} invariant violation(s); {sum(t.reply != 'ok' for t in turns)} fallback "
        f"repl{'y' if sum(t.reply != 'ok' for t in turns) == 1 else 'ies'} in {len(turns)} turns; "
        f"median turn latency {statistics.median([t.seconds for t in turns]) if turns else 0:.1f} s.",
        "",
        *_sop_table(records),
        "## Tokens and estimated cost",
        "",
        "| Role | Model | Calls | Input tokens | Output tokens | Est. cost (USD) |",
        "|---|---|---|---|---|---|",
    ]
    for role, model in (
        ("agent", meta["agent_model"]),
        ("caller", meta["caller_model"]),
        ("judge", meta["judge_model"]),
    ):
        total = Usage()
        for record in records:
            total.merge(Usage(**record.usage.get(role, {})))
        if not total.calls:
            continue
        cost = total.cost(model)
        lines.append(
            f"| {role} | {model} | {total.calls} | {total.input_tokens:,} | {total.output_tokens:,} | "
            f"{f'{cost:.2f}' if cost is not None else 'unknown'} |"
        )
    lines += ["", "Costs use list prices and ignore cached-input discounts, so they are an upper bound.", ""]

    failures = [r for r in records if not r.passed]
    if failures:
        lines += ["## Failed runs", ""]
        for record in failures:
            reasons = "; ".join(f"{c.name}: {c.detail}" for c in record.checks if not c.passed)
            # Where it stopped tells a caller who left early apart from an agent that went wrong.
            where = (
                f"{record.session.get('phase')}, waiting for {record.session.get('awaiting') or 'nothing'}"
            )
            lines += [
                f"### {record.scenario} #{record.run}",
                "",
                f"Failed: {reasons}. Ended: {record.ended} ({where}).",
                "",
            ]
            lines += [
                f"> **{'Agent' if who == 'agent' else 'Caller'}:** {text}  "
                for who, text in record.transcript
            ]
            lines.append("")
    flags = [
        (r.scenario, r.run, item, r.verdict[item]["note"])
        for r in records
        if r.verdict
        for item in _violated(r.verdict)
    ]
    if flags:
        lines += ["## SOP violations flagged by the judge", ""]
        lines += [f"- {scenario} #{run}, {item}: {note}" for scenario, run, item, note in flags]
        lines.append("")
    notes = [(r.scenario, r.run, issue) for r in records if r.verdict for issue in r.verdict["issues"]]
    if notes:
        lines += ["## Other judge notes", ""]
        lines += [f"- {scenario} #{run}: {issue}" for scenario, run, issue in notes]
        lines.append("")
    return "\n".join(lines)


def _violated(verdict: dict[str, Any]) -> list[str]:
    return [item for item in SOP_ITEMS if verdict[item]["status"] == "violated"]


def _sop_table(records: Sequence[RunRecord]) -> list[str]:
    """How often the judge found each SOP item followed, violated, or not applicable."""
    verdicts = [r.verdict for r in records if r.verdict]
    if not verdicts:
        return []
    lines = [
        "## SOP compliance (judge)",
        "",
        _row(["SOP item", "Followed", "Violated", "Not applicable"]),
        _row(["---"] * 4),
    ]
    for item in SOP_ITEMS:
        statuses = [v[item]["status"] for v in verdicts]
        counts = [str(statuses.count(s)) for s in ("followed", "violated", "not_applicable")]
        lines.append(_row([item, *counts]))
    return [*lines, ""]


def _commit() -> str:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        # Only code counts: the results this run is about to write are not a change to what was tested.
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "apps", "evals", ":!evals/results"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        return commit + (" + local changes" if dirty.stdout.strip() else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


# --- entry point ----------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--scenarios", help="comma-separated scenario IDs (default: all)")
    parser.add_argument("--runs", type=int, default=3, help="runs per scenario (default: 3)")
    parser.add_argument("--workers", type=int, default=4, help="conversations run in parallel (default: 4)")
    parser.add_argument(
        "--agent-provider",
        choices=sorted(PROVIDERS),
        default="openai",
        help="provider for the agent; another provider's key is read from AGENT_API_KEY",
    )
    parser.add_argument("--agent-model", help="model for the agent (default: the provider's default)")
    parser.add_argument(
        "--caller-model",
        help="OpenAI model for simulated callers (default: AI_MODEL, the agent's OpenAI model)",
    )
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL, help="model for the rubric judge")
    parser.add_argument("--no-judge", action="store_true", help="skip the rubric judge")
    parser.add_argument(
        "--out", type=Path, default=RESULTS_DIR, help="directory for latest.json and latest.md"
    )
    args = parser.parse_args(argv)

    settings = replace(Settings.from_env(), consent_poll_interval=0.0, strict_invariants=False)
    if settings.ai_provider != "openai" or not settings.ai_api_key:
        print("The evaluation needs a real model: set AI_API_KEY (see README).", file=sys.stderr)
        return 2
    scenarios = load_scenarios()
    if args.scenarios:
        wanted = set(args.scenarios.split(","))
        unknown = wanted - {s.id for s in scenarios}
        if unknown:
            print(f"Unknown scenario(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        scenarios = [s for s in scenarios if s.id in wanted]
    # Callers and the judge always use OpenAI (AI_API_KEY), so providers are compared on equal terms.
    caller_model = args.caller_model or settings.ai_model
    judge_model = None if args.no_judge else args.judge_model
    agent_provider = args.agent_provider
    if agent_provider == "openai":
        agent_key, agent_model = settings.ai_api_key, args.agent_model or settings.ai_model
    else:
        # Given on the command line for this run only, so .env keeps a single key.
        agent_key = os.environ.get("AGENT_API_KEY", "").strip()
        agent_model = args.agent_model or PROVIDERS[agent_provider].default_model
        if not agent_key:
            print(
                f"--agent-provider {agent_provider} needs AGENT_API_KEY, the {agent_provider} key, "
                "set for this command.",
                file=sys.stderr,
            )
            return 2

    def make_app(usage: Usage) -> FastAPI:
        adapter = build_adapter(
            agent_provider,
            api_key=agent_key,
            model=agent_model,
            base_url=settings.ai_base_url if agent_provider == "openai" else None,
            reasoning_effort=settings.ai_reasoning_effort,
            on_usage=usage.record,
        )
        return create_app(settings, adapter=adapter)

    sync_client = OpenAI(
        api_key=settings.ai_api_key, base_url=settings.ai_base_url, max_retries=2, timeout=90
    )

    def make_caller(scenario: Scenario, usage: Usage) -> Caller:
        if scenario.script is not None:
            return ScriptedCaller(scenario.script)
        return SimulatedCaller(scenario.caller, client=sync_client, model=caller_model, usage=usage)

    def judge_fn(transcript: Sequence[Line], goal: str, as_of: str, usage: Usage) -> Verdict:
        return judge(transcript, goal, as_of, client=sync_client, model=judge_model, usage=usage)

    jobs = [(scenario, run) for scenario in scenarios for run in range(1, args.runs + 1)]
    models = f"agent {agent_provider} {agent_model}, callers {caller_model}, judge {judge_model}"
    print(f"Running {len(jobs)} conversation(s): {models}")
    records: list[RunRecord] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                run_one,
                scenario,
                run,
                make_app=make_app,
                make_caller=make_caller,
                judge_fn=judge_fn if judge_model else None,
            ): (scenario, run)
            for scenario, run in jobs
        }
        for done, future in enumerate(as_completed(futures), start=1):
            record = future.result()
            records.append(record)
            failed = "; ".join(f"{c.name}: {c.detail}" for c in record.checks if not c.passed)
            status = "PASS" if record.passed else f"FAIL ({failed})"
            print(
                f"[{done}/{len(jobs)}] {record.scenario} #{record.run}: {status}, {len(record.turns)} turns"
            )

    order = {s.id: i for i, s in enumerate(scenarios)}
    records.sort(key=lambda r: (order[r.scenario], r.run))
    meta = {
        "date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "commit": _commit(),
        "prompt_version": hashlib.sha256(PROMPTS_PATH.read_bytes()).hexdigest()[:12],
        "agent_provider": agent_provider,
        "agent_model": agent_model,
        "reasoning_effort": settings.ai_reasoning_effort,
        "caller_model": caller_model,
        "judge_model": judge_model,
        "runs": args.runs,
        "demo_date": settings.demo_as_of_date.isoformat(),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "records": [asdict(r) | {"passed": r.passed} for r in records]}
    (args.out / "latest.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    summary = summarize(scenarios, records, meta)
    (args.out / "latest.md").write_text(summary)
    print()
    print(summary.split("\n## ")[0])
    return 0 if all(r.passed for r in records) else 1


if __name__ == "__main__":
    sys.exit(main())
