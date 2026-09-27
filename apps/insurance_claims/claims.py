"""Claim lookup inside the verified account.

Every call takes a TrustedContext that only the controller creates after access
is granted. A case ID that belongs to someone else is treated exactly like one
that does not exist, so a response never confirms another person's claim.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

from .domain import CaseHintField, Fact, Phase, Resolution
from .fixture_loader import Claim, ClaimStatus, FixtureData
from .sop_spec import SOP, Tool

# Relaxation order when nothing matches: the least reliable hint goes first.
RELAX_ORDER = (
    CaseHintField.CASE_ID,
    CaseHintField.MONTH,
    CaseHintField.YEAR,
    CaseHintField.STATUS,
    CaseHintField.CASE_TYPE,
)

CASE_TYPE_SYNONYMS = {
    "healthcare": "healthcare",
    "health": "healthcare",
    "medical": "healthcare",
    "health insurance": "healthcare",
    "dental": "dental",
    "dentist": "dental",
    "auto": "auto",
    "car": "auto",
    "vehicle": "auto",
    "automobile": "auto",
}

STATUS_SYNONYMS = {
    "denied": "denied",
    "rejected": "denied",
    "declined": "denied",
    "closed": "closed",
    "settled": "closed",
    "paid": "closed",
    "completed": "closed",
    "open": "open",
    "pending": "open",
    "in progress": "open",
    "processing": "open",
}

STATUS_WORDS = {
    ClaimStatus.DENIED: "denied",
    ClaimStatus.CLOSED: "closed",
    ClaimStatus.OPEN: "open and in progress",
}

_MONTHS = {name.casefold(): number for number, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.casefold(): number for number, name in enumerate(calendar.month_abbr) if name})


class ToolError(Exception):
    code = "tool_error"


class NotAuthorized(ToolError):
    code = "not_authorized"


class NotFound(ToolError):
    code = "not_found"


@dataclass(frozen=True)
class TrustedContext:
    """Server-side identity for tool calls; the model can never construct one."""

    party_id: str
    phase: Phase
    case_cycle_id: int


@dataclass(frozen=True)
class ClaimSearch:
    resolution: Resolution
    case_ids: tuple[str, ...] = ()
    # Hints dropped to find these candidates; they need the caller's confirmation.
    relaxed: tuple[CaseHintField, ...] = field(default_factory=tuple)


def normalize_case_hint(hint: CaseHintField, value: str | int | None) -> str | int | None:
    """Return the canonical hint value, or None when it cannot be understood."""
    if value is None:
        return None
    text = str(value).strip().casefold()
    if hint is CaseHintField.CASE_ID:
        match = re.fullmatch(r"cl[\s-]?(\d+)", text)
        return f"CL-{match.group(1)}" if match else None
    if hint is CaseHintField.CASE_TYPE:
        return CASE_TYPE_SYNONYMS.get(text)
    if hint is CaseHintField.STATUS:
        return STATUS_SYNONYMS.get(text)
    if hint is CaseHintField.MONTH:
        if text.isdigit() and 1 <= int(text) <= 12:
            return int(text)
        return _MONTHS.get(text.rstrip("."))
    if hint is CaseHintField.YEAR:
        return int(text) if re.fullmatch(r"(19|20)\d\d", text) else None
    return None


class ClaimService:
    def __init__(self, fixtures: FixtureData) -> None:
        self._claims = {claim.case_id: claim for claim in fixtures.claims}

    def find_claims(self, ctx: TrustedContext, hints: Mapping[CaseHintField, str | int]) -> ClaimSearch:
        self._authorize(ctx, Tool.FIND_CLAIMS)
        own = [c for c in self._claims.values() if c.party_id == ctx.party_id]
        if not own:
            return ClaimSearch(Resolution.NO_CLAIMS)

        case_id = hints.get(CaseHintField.CASE_ID)
        if case_id is not None and any(c.case_id == case_id for c in own):
            return ClaimSearch(Resolution.SELECTED, (str(case_id),))

        # An unowned case ID matches nothing here, so it falls through to relaxation.
        matches = _filter(own, hints)
        if len(matches) == 1:
            return ClaimSearch(Resolution.SELECTED, _ids(matches))
        if matches:
            return ClaimSearch(Resolution.AMBIGUOUS, _ids(matches))

        # Nothing matched every hint: drop one hint at a time and report which one.
        for dropped in RELAX_ORDER:
            if dropped not in hints:
                continue
            remaining = {k: v for k, v in hints.items() if k is not dropped}
            matches = _filter(own, remaining)
            if matches:
                return ClaimSearch(Resolution.AMBIGUOUS, _ids(matches), relaxed=(dropped,))
        return ClaimSearch(Resolution.NO_MATCH)

    def get_claim(self, ctx: TrustedContext, case_id: str) -> Claim:
        self._authorize(ctx, Tool.GET_CLAIM_DETAILS)
        claim = self._claims.get(case_id)
        if claim is None or claim.party_id != ctx.party_id:
            raise NotFound(case_id)
        return claim

    @staticmethod
    def _authorize(ctx: TrustedContext, tool: Tool) -> None:
        if tool not in SOP[ctx.phase].tools:
            raise NotAuthorized(f"{tool} is not allowed in {ctx.phase}")


def matches_hints(claim: Claim, hints: Mapping[CaseHintField, str | int]) -> bool:
    actual = {
        CaseHintField.CASE_ID: claim.case_id,
        CaseHintField.CASE_TYPE: claim.case_type,
        CaseHintField.STATUS: claim.status.value,
        CaseHintField.MONTH: claim.created_at.month,
        CaseHintField.YEAR: claim.created_at.year,
    }
    return all(actual[hint] == value for hint, value in hints.items())


def _filter(claims: list[Claim], hints: Mapping[CaseHintField, str | int]) -> list[Claim]:
    return sorted((c for c in claims if matches_hints(c, hints)), key=lambda c: c.created_at, reverse=True)


def _ids(claims: list[Claim]) -> tuple[str, ...]:
    return tuple(c.case_id for c in claims)


def format_date(value: date) -> str:
    return f"{value:%B} {value.day}, {value.year}"


def overview_fact(claim: Claim) -> Fact:
    text = (
        f"I found the {claim.case_type} claim {claim.case_id} from {format_date(claim.created_at)}. "
        f"It is currently {STATUS_WORDS[claim.status]}."
    )
    return Fact(
        id=f"claim.{claim.case_id}.overview",
        text=text,
        source=f"claims.json:{claim.case_id}",
        case_id=claim.case_id,
        party_id=claim.party_id,
    )


def brief_fact(claim: Claim) -> Fact:
    text = (
        f"{claim.case_id}: {claim.case_type}, {format_date(claim.created_at)}, {STATUS_WORDS[claim.status]}"
    )
    return Fact(
        id=f"claim.{claim.case_id}.brief",
        text=text,
        source=f"claims.json:{claim.case_id}",
        case_id=claim.case_id,
        party_id=claim.party_id,
    )


def describe_hints(hints: Mapping[CaseHintField, str | int]) -> str:
    """Describe what the caller asked for, e.g. 'a denied auto claim from March'."""
    if CaseHintField.CASE_ID in hints:
        return f"a claim numbered {hints[CaseHintField.CASE_ID]}"
    phrase = " ".join(
        [str(hints[h]) for h in (CaseHintField.STATUS, CaseHintField.CASE_TYPE) if h in hints] + ["claim"]
    )
    text = f"{'an' if phrase[0] in 'aeiou' else 'a'} {phrase}"
    when = []
    if CaseHintField.MONTH in hints:
        when.append(calendar.month_name[int(hints[CaseHintField.MONTH])])
    if CaseHintField.YEAR in hints:
        when.append(str(hints[CaseHintField.YEAR]))
    if when:
        text += " from " + " ".join(when)
    return text


def relaxed_fact(hints: Mapping[CaseHintField, str | int], account: str) -> Fact:
    return Fact(
        id="search.relaxed", text=f"I don't see {describe_hints(hints)} on {account}.", source="claim search"
    )
