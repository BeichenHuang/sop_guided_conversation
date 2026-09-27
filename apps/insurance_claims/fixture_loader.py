"""Load the read-only fixture files and check their structure and cross-references."""

from __future__ import annotations

import json
import string
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, TypeAdapter, ValidationError

from .domain import IdType, Intent, Topic

# Claim documents use short names; the guideline file keys them by their full names.
# The mapping does not imply that an original is required.
DOCUMENT_GUIDANCE_KEYS: Mapping[str, str] = {
    "pathology report": "original pathology report",
    "office note": "treating provider office note",
}

# Placeholders that are filled from the claim itself; the rest come from
# claim_followup_settings.
CLAIM_PLACEHOLDERS = frozenset({"case_id", "documents"})

AMOUNT_FIELDS = ("expected_reimbursement_amount", "allowed_max_amount", "net_pay", "net_fee")

FIXTURE_FILES = {
    "policyholders": "policyholders.json",
    "claims": "claims.json",
    "representatives": "representatives.json",
    "consent_scenarios": "consent_scenarios.json",
    "guidelines": "required_document_guideline.json",
    "claim_schema": "claim_schema.json",
}


class FixtureError(RuntimeError):
    """A fixture file is missing, malformed, or inconsistent with the others."""


def _decimal_string(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("amounts must be decimal strings, not JSON numbers")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{value!r} is not a decimal amount") from None
    if not amount.is_finite():
        raise ValueError(f"{value!r} is not a finite amount")
    return amount


DecimalString = Annotated[Decimal, BeforeValidator(_decimal_string)]


class _Fixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LocalizedText(_Fixture):
    en: str


class Policyholder(_Fixture):
    party_id: str
    name: str
    name_aliases: tuple[str, ...] = ()
    policy_number: str
    dob: date
    id_type: IdType
    id_last4: str = Field(pattern=r"^\d{4}$")
    phone: str = Field(pattern=r"^\+\d{8,15}$")
    phone_aliases: tuple[Annotated[str, Field(pattern=r"^\+\d{8,15}$")], ...] = ()
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+$")
    email_aliases: tuple[Annotated[str, Field(pattern=r"^[^@\s]+@[^@\s]+$")], ...] = ()


class ClaimStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    DENIED = "denied"


class Claim(_Fixture):
    case_id: str
    party_id: str
    case_type: str
    created_at: date
    status: ClaimStatus
    summary: str
    denial_reason: str | None = None
    documents_needed: tuple[str, ...] = ()
    appeal_deadline: date | None = None
    expected_reimbursement_amount: DecimalString
    allowed_max_amount: DecimalString
    net_pay: DecimalString
    net_fee: DecimalString


class Representative(_Fixture):
    rep_name: str
    relationship: str
    buyer_name: str
    buyer_party_id: str


class ConsentScenario(_Fixture):
    status_sequence: tuple[Literal["pending", "approved"], ...] = Field(min_length=1)


class FollowupGuidance(_Fixture):
    topic: Topic
    intent_hints: tuple[Intent, ...]
    requires_documents: bool
    match_any: tuple[str, ...] = ()
    en: str


class DocumentGuidelines(_Fixture):
    default_guidance: LocalizedText
    case_type_guidance: dict[str, LocalizedText]
    document_guidance: dict[str, LocalizedText]
    document_alternative_guidance: dict[str, LocalizedText]
    claim_followup_settings: dict[str, LocalizedText]
    claim_followup_guidance: tuple[FollowupGuidance, ...]
    claim_followup_fallback: LocalizedText


class FieldDescription(_Fixture):
    type: str
    example: str
    description: str


class ClaimSchema(_Fixture):
    notes: tuple[str, ...] = ()
    field_descriptions: dict[str, FieldDescription]


@dataclass(frozen=True)
class FixtureData:
    policyholders: tuple[Policyholder, ...]
    claims: tuple[Claim, ...]
    representatives: tuple[Representative, ...]
    consent_scenarios: Mapping[str, ConsentScenario]
    guidelines: DocumentGuidelines
    claim_schema: ClaimSchema


_ADAPTERS: dict[str, TypeAdapter[Any]] = {
    "policyholders": TypeAdapter(tuple[Policyholder, ...]),
    "claims": TypeAdapter(tuple[Claim, ...]),
    "representatives": TypeAdapter(tuple[Representative, ...]),
    "consent_scenarios": TypeAdapter(dict[str, ConsentScenario]),
    "guidelines": TypeAdapter(DocumentGuidelines),
    "claim_schema": TypeAdapter(ClaimSchema),
}


def load_fixtures(directory: Path) -> FixtureData:
    """Read, validate and cross-check every fixture file in ``directory``."""
    parsed: dict[str, Any] = {}
    for key, filename in FIXTURE_FILES.items():
        path = directory / filename
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise FixtureError(f"{filename}: file not found in {directory}") from None
        except json.JSONDecodeError as exc:
            raise FixtureError(f"{filename}: invalid JSON ({exc})") from None
        try:
            parsed[key] = _ADAPTERS[key].validate_python(raw)
        except ValidationError as exc:
            raise FixtureError(f"{filename}: {_summarize(exc)}") from None

    data = FixtureData(**parsed)
    problems = find_relation_problems(data)
    if problems:
        raise FixtureError("fixture cross-check failed:\n- " + "\n- ".join(problems))
    return data


def find_relation_problems(data: FixtureData) -> list[str]:
    problems: list[str] = []
    holders = {p.party_id: p for p in data.policyholders}

    problems += _duplicates("policyholders.json party_id", [p.party_id for p in data.policyholders])
    problems += _duplicates("policyholders.json policy_number", [p.policy_number for p in data.policyholders])
    problems += _duplicates("claims.json case_id", [c.case_id for c in data.claims])

    for claim in data.claims:
        if claim.party_id not in holders:
            problems.append(f"claims.json {claim.case_id}: unknown party_id {claim.party_id!r}")
        if claim.status is ClaimStatus.DENIED and not claim.denial_reason:
            problems.append(f"claims.json {claim.case_id}: denied claim without denial_reason")
        if claim.appeal_deadline and claim.appeal_deadline < claim.created_at:
            problems.append(f"claims.json {claim.case_id}: appeal_deadline is before created_at")

    for rep in data.representatives:
        holder = holders.get(rep.buyer_party_id)
        if holder is None:
            problems.append(
                f"representatives.json {rep.rep_name}: unknown buyer_party_id {rep.buyer_party_id!r}"
            )
        elif holder.name != rep.buyer_name:
            problems.append(
                f"representatives.json {rep.rep_name}: buyer_name {rep.buyer_name!r} "
                f"does not match {rep.buyer_party_id} ({holder.name!r})"
            )

    if "default" not in data.consent_scenarios:
        problems.append("consent_scenarios.json: missing the 'default' scenario")

    guidelines = data.guidelines
    for short_name, key in DOCUMENT_GUIDANCE_KEYS.items():
        if key not in guidelines.document_guidance:
            problems.append(f"document name mapping {short_name!r} -> {key!r}: no document_guidance entry")
    if "default" not in guidelines.document_alternative_guidance:
        problems.append("required_document_guideline.json: document_alternative_guidance has no 'default'")

    allowed = CLAIM_PLACEHOLDERS | set(guidelines.claim_followup_settings)
    for item in guidelines.claim_followup_guidance:
        unknown = _placeholders(item.en) - allowed
        if unknown:
            problems.append(f"claim_followup_guidance {item.topic}: unknown placeholders {sorted(unknown)}")

    described = set(data.claim_schema.field_descriptions)
    missing = [name for name in AMOUNT_FIELDS if name not in described]
    if missing:
        problems.append(f"claim_schema.json: no description for {missing}")

    return problems


def _duplicates(label: str, values: list[str]) -> list[str]:
    counts = Counter(values)
    return [f"{label}: duplicate {value!r}" for value, n in sorted(counts.items()) if n > 1]


def _placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def _summarize(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:5]:
        location = ".".join(str(part) for part in error["loc"])
        parts.append(f"{location}: {error['msg']}")
    more = exc.error_count() - len(parts)
    if more > 0:
        parts.append(f"... and {more} more")
    return "; ".join(parts)
