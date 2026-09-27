"""Identity normalization and verification.

Verification is deterministic: each policyholder is matched separately, and one
person must match at least three distinct fields. Nothing here reports which
fields matched to the caller; that stays on the server.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from .domain import IDENTITY_FIELDS_REQUIRED, IdentityField, StatedIdType
from .fixture_loader import Policyholder

MAX_FAILED_ATTEMPTS = 3


@dataclass(frozen=True)
class Candidate:
    """One identity value the caller provided, already validated against the message."""

    field: IdentityField
    value: str
    id_type: StatedIdType | None = None

    @property
    def countable(self) -> bool:
        """An ID number only counts once the caller has said which document it is from."""
        return not (self.field is IdentityField.ID_LAST4 and self.id_type in (None, StatedIdType.UNKNOWN))


class Outcome(StrEnum):
    INSUFFICIENT = "insufficient"
    VERIFIED = "verified"
    FAILED = "failed"


@dataclass(frozen=True)
class VerificationResult:
    outcome: Outcome
    party_id: str | None = None


def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    text = re.sub(r"[^\w'\-]+", " ", text)
    return " ".join(text.split())


def normalize_email(value: str) -> str:
    return value.strip().casefold()


def normalize_phone(value: str) -> str | None:
    """Return E.164 form. Ten-digit numbers are taken as US numbers (+1)."""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if value.strip().startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    return None


def normalize_id_last4(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)
    return digits if len(digits) == 4 else None


def parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def matched_fields(holder: Policyholder, candidates: Mapping[IdentityField, Candidate]) -> set[IdentityField]:
    matched: set[IdentityField] = set()
    name = candidates.get(IdentityField.FULL_NAME)
    if name and normalize_name(name.value) in {
        normalize_name(n) for n in (holder.name, *holder.name_aliases)
    }:
        matched.add(IdentityField.FULL_NAME)
    dob = candidates.get(IdentityField.DOB)
    if dob and parse_iso_date(dob.value) == holder.dob:
        matched.add(IdentityField.DOB)
    phone = candidates.get(IdentityField.PHONE)
    if phone and normalize_phone(phone.value) in {holder.phone, *holder.phone_aliases}:
        matched.add(IdentityField.PHONE)
    email = candidates.get(IdentityField.EMAIL)
    if email and normalize_email(email.value) in {
        normalize_email(e) for e in (holder.email, *holder.email_aliases)
    }:
        matched.add(IdentityField.EMAIL)
    id_last4 = candidates.get(IdentityField.ID_LAST4)
    if (
        id_last4
        and id_last4.countable
        and id_last4.id_type.value == holder.id_type.value
        and normalize_id_last4(id_last4.value) == holder.id_last4
    ):
        matched.add(IdentityField.ID_LAST4)
    return matched


def verify(candidates: Iterable[Candidate], holders: Iterable[Policyholder]) -> VerificationResult:
    """Verify the caller against every policyholder separately."""
    by_field = {c.field: c for c in candidates}
    if sum(1 for c in by_field.values() if c.countable) < IDENTITY_FIELDS_REQUIRED:
        return VerificationResult(Outcome.INSUFFICIENT)
    passing = [h for h in holders if len(matched_fields(h, by_field)) >= IDENTITY_FIELDS_REQUIRED]
    if len(passing) == 1:
        return VerificationResult(Outcome.VERIFIED, party_id=passing[0].party_id)
    return VerificationResult(Outcome.FAILED)


def candidates_digest(candidates: Iterable[Candidate]) -> str:
    """A stable digest of the candidate set, so state need not keep a second copy of PII."""
    parts = sorted(f"{c.field.value}={c.value}|{c.id_type or ''}" for c in candidates)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
