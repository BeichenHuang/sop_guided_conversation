"""The conversation summary offered by email in POST_PROCESS.

The summary is built from the ledger and the facts actually given, never from
the verification exchange, so it contains no date of birth or ID number.
"""

from __future__ import annotations

from datetime import date

from .claims import format_date
from .domain import Fact, SessionState, Summary
from .fixture_loader import Claim, ClaimStatus
from .processing import TOPIC_LABELS, Topic, document_list

# Topics the outcome and next steps already carry; any others are listed as "also covered".
_CORE_TOPICS = frozenset(
    {
        Topic.STATUS,
        Topic.DENIAL_REASON,
        Topic.DOCUMENTS_NEEDED,
        Topic.APPEAL_DEADLINE,
        Topic.SUBMISSION_METHOD,
        Topic.SUBMISSION_TIMING,
    }
)


def mask_email(address: str) -> str:
    """m*******@email.com: enough to recognize, not enough to harvest."""
    local, _, domain = address.partition("@")
    return f"{local[:1]}{'*' * max(len(local) - 1, 3)}@{domain}"


def prepare_summary(
    state: SessionState,
    claim: Claim | None,
    recipient: str,
    as_of: date,
    handoff_offered: bool,
    upload_url: str | None = None,
) -> Summary:
    discussed = [TOPIC_LABELS[topic] for topic in state.case_context.answered if topic in TOPIC_LABELS]
    if claim is None:
        outcome = "No claims were found on the account."
        next_steps = ["If you believe a claim is missing, a human representative can look into it with you."]
    else:
        outcome = _outcome(claim)
        next_steps = _next_steps(claim, as_of, handoff_offered, upload_url)
        if not discussed:
            discussed = [f"claim {claim.case_id}"]
    also_covered = [
        TOPIC_LABELS[topic]
        for topic in state.case_context.answered
        if topic in TOPIC_LABELS and topic not in _CORE_TOPICS
    ]
    revision = state.email.last_revision + 1
    subject = (
        f"Summary of your claim conversation ({claim.case_id})"
        if claim
        else "Summary of your claims conversation"
    )
    body = _body(as_of, discussed, outcome, next_steps, state.earlier_claims)
    return Summary(
        revision=revision,
        case_id=claim.case_id if claim else None,
        discussed=discussed,
        outcome=outcome,
        next_steps=next_steps,
        also_covered=also_covered,
        earlier_claims=list(state.earlier_claims),
        recipient=recipient,
        subject=subject,
        body=body,
    )


def earlier_claim_recap(claim: Claim, as_of: date, upload_url: str | None = None) -> str:
    """A claim from an earlier case cycle, recapped like the current one: its outcome and next steps."""
    return " ".join([_outcome(claim), *_next_steps(claim, as_of, False, upload_url)])


def summary_fact(summary: Summary) -> Fact:
    lines = [f"Here's a summary of what we covered: {summary.outcome}"]
    for earlier in summary.earlier_claims:
        lines.append(f"Earlier in this conversation: {earlier}")
    if summary.also_covered:
        lines.append(f"We also went over {_join_and(summary.also_covered)}.")
    if len(summary.next_steps) == 1:
        lines.append(f"Next step: {summary.next_steps[0]}")
    elif summary.next_steps:
        lines.append("\n".join(["Next steps:", *(f"- {step}" for step in summary.next_steps)]))
    return Fact(
        id=f"summary.r{summary.revision}", text="\n\n".join(lines), source="summary", case_id=summary.case_id
    )


def _outcome(claim: Claim) -> str:
    base = f"Claim {claim.case_id} ({claim.case_type}, filed {format_date(claim.created_at)}) is currently "
    if claim.status is ClaimStatus.DENIED and claim.denial_reason:
        return base + f"denied because {claim.denial_reason}."
    return base + {ClaimStatus.CLOSED: "closed.", ClaimStatus.OPEN: "open and in progress."}.get(
        claim.status, f"{claim.status.value}."
    )


def _next_steps(claim: Claim, as_of: date, handoff_offered: bool, upload_url: str | None) -> list[str]:
    steps = []
    if claim.documents_needed:
        submit = (
            f"Submit {document_list(claim)} through the member portal or the claim upload link"
            f"{f' ({upload_url})' if upload_url else ''}, "
            "or ask support about fax or mail."
        )
        if claim.appeal_deadline and claim.appeal_deadline >= as_of:
            submit += f" The deadline is {format_date(claim.appeal_deadline)}."
        steps.append(submit)
    if claim.appeal_deadline and claim.appeal_deadline < as_of:
        steps.append(
            f"The appeal deadline ({format_date(claim.appeal_deadline)}) has passed, so talk with a human "
            "representative about the remaining options."
        )
    elif handoff_offered:
        steps.append("A human representative can help with anything the assistant couldn't resolve.")
    if claim.status is ClaimStatus.OPEN:
        steps.append(f"No action is needed on claim {claim.case_id} right now; it is still being processed.")
    return steps


def _join_and(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _body(
    as_of: date, discussed: list[str], outcome: str, next_steps: list[str], earlier_claims: list[str]
) -> str:
    lines = [
        "Hello,",
        "",
        f"Here is a summary of your claims support conversation on {format_date(as_of)}.",
        "",
        "What we discussed:",
        *[f"- {item}" for item in discussed],
        "",
        f"Claim status: {outcome}",
    ]
    if earlier_claims:
        lines += [
            "",
            "Also discussed earlier in the conversation:",
            *[f"- {item}" for item in earlier_claims],
        ]
    if next_steps:
        lines += ["", "Next steps:", *[f"- {step}" for step in next_steps]]
    lines += ["", "This summary was generated by a demo system; no real email was sent."]
    return "\n".join(lines)
