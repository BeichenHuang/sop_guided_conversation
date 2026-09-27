"""Checks a model reply must pass before the user sees it.

The checks are textual, so they catch invented or missing key values, not every
semantic slip; the evaluation covers the rest. A reply that fails is retried
once with the problems listed, then replaced by the plan's template.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation

from .domain import ReplyPlan, ResponseDraft

MAX_REPLY_CHARS = 1800

_CLAIM_ID = re.compile(r"\bCL-\d+\b", re.IGNORECASE)
_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
_MONTH_NAMES = "|".join(sorted({*calendar.month_name[1:], *calendar.month_abbr[1:]}, key=len, reverse=True))
_DATE = re.compile(
    rf"\b({_MONTH_NAMES})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", re.IGNORECASE
)
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_URL = re.compile(r"\bhttps?://\S+|\bwww\.\S+", re.IGNORECASE)
_EMAIL = re.compile(r"(?<![\w*])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
_MONTHS = {name.casefold(): i for i, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.casefold(): i for i, name in enumerate(calendar.month_abbr) if name})


def check_draft(
    draft: ResponseDraft,
    plan: ReplyPlan,
    facts: Mapping[str, str],
    *,
    allowed_texts: Iterable[str] = (),
    forbidden: Iterable[str] = (),
    question_text: str | None = None,
) -> list[str]:
    """Return the problems with ``draft``; an empty list means it may be shown."""
    text = draft.text.strip()
    if not text:
        return ["the reply is empty"]
    problems: list[str] = []
    if len(text) > MAX_REPLY_CHARS:
        problems.append(f"the reply is longer than {MAX_REPLY_CHARS} characters")

    unknown = sorted(set(draft.used_fact_ids) - set(facts))
    if unknown:
        problems.append(f"the reply cites facts that were not provided: {unknown}")
    if question_text and "?" in question_text and "?" not in text:
        problems.append("the reply must end with the planned question")

    allowed = " ".join([*facts.values(), *allowed_texts])
    problems += _unsupported(text, allowed)
    for fact_id in plan.inform:
        missing = _missing_values(facts[fact_id], text)
        if missing:
            problems.append(f"fact {fact_id} is missing {', '.join(missing)}")
    for value in forbidden:
        if value and re.search(rf"(?<![\w-]){re.escape(value)}(?![\w-])", text, re.IGNORECASE):
            problems.append("the reply repeats an identity detail the caller gave")
    return problems


def _unsupported(text: str, allowed: str) -> list[str]:
    problems = []
    allowed_ids = {m.upper() for m in _CLAIM_ID.findall(allowed)}
    for claim_id in {m.upper() for m in _CLAIM_ID.findall(text)} - allowed_ids:
        problems.append(f"the reply mentions {claim_id}, which is not in the facts")
    allowed_amounts = _amounts(allowed)
    for amount in _amounts(text) - allowed_amounts:
        problems.append(f"the reply mentions ${amount}, which is not in the facts")
    allowed_dates = _dates(allowed)
    for month, day, year in _dates(text):
        if not any(m == month and d == day and (year is None or y == year) for m, d, y in allowed_dates):
            problems.append(
                f"the reply mentions a date ({calendar.month_name[month]} {day}) that is not in the facts"
            )
    for pattern, label in ((_URL, "a link"), (_EMAIL, "an email address"), (_PHONE, "a phone number")):
        for match in pattern.findall(text):
            if _clean(match) not in allowed:
                problems.append(f"the reply mentions {label} that is not in the facts")
    return problems


def _clean(match: str) -> str:
    """A link at the end of a sentence or in parentheses, without that punctuation."""
    return match.rstrip(".,;:!?)]}'\"")


def _missing_values(fact: str, text: str) -> list[str]:
    """Key values of a fact the reply must keep: claim numbers, amounts, dates and links."""
    missing = [link for link in (_clean(m) for m in _URL.findall(fact)) if link not in text]
    reply_ids = {m.upper() for m in _CLAIM_ID.findall(text)}
    missing += [
        claim_id for claim_id in {m.upper() for m in _CLAIM_ID.findall(fact)} if claim_id not in reply_ids
    ]
    reply_amounts = _amounts(text)
    missing += [f"${amount}" for amount in _amounts(fact) if amount not in reply_amounts]
    reply_dates = _dates(text)
    for month, day, year in _dates(fact):
        if not any(m == month and d == day and (y is None or y == year) for m, d, y in reply_dates):
            missing.append(f"{calendar.month_name[month]} {day}")
    return missing


def _amounts(text: str) -> set[Decimal]:
    amounts = set()
    for raw in _MONEY.findall(text):
        try:
            amounts.add(Decimal(raw.replace(",", "")).quantize(Decimal("0.01")))
        except InvalidOperation:
            continue
    return amounts


def _dates(text: str) -> set[tuple[int, int, int | None]]:
    found: set[tuple[int, int, int | None]] = set()
    for name, day, year in _DATE.findall(text):
        month = _MONTHS.get(name.casefold().rstrip("."))
        if month and 1 <= int(day) <= 31:
            found.add((month, int(day), int(year) if year else None))
    for year, month, day in _ISO_DATE.findall(text):
        found.add((int(month), int(day), int(year)))
    return found
