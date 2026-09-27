from __future__ import annotations

import pytest

from apps.insurance_claims.config import DEFAULT_FIXTURES_DIR
from apps.insurance_claims.domain import IdentityField as F
from apps.insurance_claims.domain import StatedIdType as T
from apps.insurance_claims.fixture_loader import load_fixtures
from apps.insurance_claims.identity import (
    Candidate,
    Outcome,
    candidates_digest,
    normalize_name,
    normalize_phone,
    verify,
)

HOLDERS = load_fixtures(DEFAULT_FIXTURES_DIR).policyholders

MARGARET = [Candidate(F.FULL_NAME, "Margaret Chen"), Candidate(F.DOB, "1985-03-15")]
MA_TIAN = [Candidate(F.FULL_NAME, "Ma Tian"), Candidate(F.DOB, "1964-09-10")]


def check(candidates):
    return verify(candidates, HOLDERS)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("650-521-2836", "+16505212836"),
        ("(650) 521 2836", "+16505212836"),
        ("+1 650 521 2836", "+16505212836"),
        ("16505212836", "+16505212836"),
        ("521-2836", None),
    ],
)
def test_phone_numbers_default_to_us(raw, expected):
    assert normalize_phone(raw) == expected


def test_name_normalization_ignores_case_and_spacing():
    assert normalize_name("  margaret   CHEN ") == normalize_name("Margaret Chen")


def test_three_fields_of_one_person_verify():
    result = check([*MARGARET, Candidate(F.ID_LAST4, "4472", T.SSN_LAST4)])
    assert result.outcome is Outcome.VERIFIED
    assert result.party_id == "P9"


def test_two_fields_are_not_enough():
    assert check(MARGARET).outcome is Outcome.INSUFFICIENT


def test_aliases_are_accepted():
    result = check(
        [
            Candidate(F.FULL_NAME, "Yaven Li"),
            Candidate(F.DOB, "1989-12-03"),
            Candidate(F.EMAIL, " YaWen.Li@Example.com "),
        ]
    )
    assert result.party_id == "P13"


# The last four digits count only for the type of ID the record holds.
@pytest.mark.parametrize(
    ("candidates", "outcome", "party"),
    [
        ([*MA_TIAN, Candidate(F.ID_LAST4, "6688", T.SSN_LAST4)], Outcome.FAILED, None),
        ([*MA_TIAN, Candidate(F.ID_LAST4, "6688", T.NATIONAL_ID_LAST4)], Outcome.VERIFIED, "P12"),
        ([*MARGARET, Candidate(F.ID_LAST4, "4472", T.NATIONAL_ID_LAST4)], Outcome.FAILED, None),
        ([*MA_TIAN, Candidate(F.ID_LAST4, "6688", T.UNKNOWN)], Outcome.INSUFFICIENT, None),
        (
            [
                Candidate(F.FULL_NAME, "Ya Wen Li"),
                Candidate(F.PHONE, "+16505212830"),
                Candidate(F.ID_LAST4, "5317", T.NATIONAL_ID_LAST4),
            ],
            Outcome.VERIFIED,
            "P13",
        ),
    ],
    ids=[
        "1-ssn-claimed-for-national-id",
        "2-national-id",
        "3-national-id-claimed-for-ssn",
        "5-type-unknown",
        "6-ya-wen-li-national-id",
    ],
)
def test_id_last4_counts_only_when_the_type_matches(candidates, outcome, party):
    result = check(candidates)
    assert result.outcome is outcome
    assert result.party_id == party


def test_fields_from_different_people_do_not_add_up():
    # Margaret's name and DOB plus Ya Wen Li's phone: two fields for one person, one for another.
    assert check([*MARGARET, Candidate(F.PHONE, "+16505212830")]).outcome is Outcome.FAILED


def test_an_extra_mismatching_field_does_not_block():
    result = check(
        [
            *MARGARET,
            Candidate(F.ID_LAST4, "4472", T.SSN_LAST4),
            Candidate(F.PHONE, "+16500000000"),
        ]
    )
    assert result.outcome is Outcome.VERIFIED


def test_digest_is_order_independent_and_changes_with_values():
    a = [*MARGARET, Candidate(F.ID_LAST4, "4472", T.SSN_LAST4)]
    assert candidates_digest(a) == candidates_digest(list(reversed(a)))
    b = [*MARGARET, Candidate(F.ID_LAST4, "4472", T.NATIONAL_ID_LAST4)]
    assert candidates_digest(a) != candidates_digest(b)
