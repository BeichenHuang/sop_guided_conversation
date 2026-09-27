from __future__ import annotations

import pytest

from apps.insurance_claims.claims import (
    ClaimService,
    NotAuthorized,
    NotFound,
    TrustedContext,
    describe_hints,
    normalize_case_hint,
)
from apps.insurance_claims.config import DEFAULT_FIXTURES_DIR
from apps.insurance_claims.domain import CaseHintField as H
from apps.insurance_claims.domain import Phase, Resolution
from apps.insurance_claims.fixture_loader import load_fixtures

SERVICE = ClaimService(load_fixtures(DEFAULT_FIXTURES_DIR))


def ctx(party="P9", phase=Phase.RESOLVE_INTENT):
    return TrustedContext(party_id=party, phase=phase, case_cycle_id=1)


def test_the_official_hints_select_cl_2048():
    search = SERVICE.find_claims(ctx(), {H.CASE_TYPE: "healthcare", H.MONTH: 1, H.STATUS: "denied"})
    assert (search.resolution, search.case_ids) == (Resolution.SELECTED, ("CL-2048",))


def test_january_healthcare_alone_is_ambiguous():
    search = SERVICE.find_claims(ctx(), {H.CASE_TYPE: "healthcare", H.MONTH: 1})
    assert search.resolution is Resolution.AMBIGUOUS
    assert search.case_ids == ("CL-2048", "CL-2011")
    assert search.relaxed == ()


def test_no_match_relaxes_one_hint_and_says_which():
    search = SERVICE.find_claims(ctx(), {H.CASE_TYPE: "auto", H.STATUS: "denied"})
    assert search.resolution is Resolution.AMBIGUOUS
    assert search.case_ids == ("CL-2102",)
    assert search.relaxed == (H.STATUS,)


def test_an_owned_case_id_wins_over_other_hints():
    search = SERVICE.find_claims(ctx(), {H.CASE_ID: "CL-2011", H.STATUS: "denied"})
    assert (search.resolution, search.case_ids) == (Resolution.SELECTED, ("CL-2011",))


def test_someone_elses_case_id_looks_exactly_like_a_missing_one():
    other = SERVICE.find_claims(ctx(), {H.CASE_ID: "CL-3001"})  # belongs to P12
    missing = SERVICE.find_claims(ctx(), {H.CASE_ID: "CL-9999"})
    assert other == missing
    assert other.relaxed == (H.CASE_ID,)
    assert "CL-3001" not in other.case_ids


@pytest.mark.parametrize("party", ["P7", "P13"])
def test_accounts_without_claims(party):
    assert SERVICE.find_claims(ctx(party), {}).resolution is Resolution.NO_CLAIMS


@pytest.mark.parametrize("case_id", ["CL-3001", "CL-0000"])
def test_get_claim_hides_other_peoples_claims(case_id):
    with pytest.raises(NotFound):
        SERVICE.get_claim(ctx(), case_id)


def test_claim_tools_are_refused_while_verifying():
    with pytest.raises(NotAuthorized):
        SERVICE.find_claims(ctx(phase=Phase.VERIFY_ID), {})
    with pytest.raises(NotAuthorized):
        SERVICE.get_claim(ctx(phase=Phase.VERIFY_ID), "CL-2048")


@pytest.mark.parametrize(
    ("hint", "raw", "expected"),
    [
        (H.CASE_TYPE, "Medical", "healthcare"),
        (H.CASE_TYPE, "car", "auto"),
        (H.CASE_TYPE, "pet", None),
        (H.STATUS, "settled", "closed"),
        (H.STATUS, "rejected", "denied"),
        (H.MONTH, "January", 1),
        (H.MONTH, "jan", 1),
        (H.MONTH, 13, None),
        (H.YEAR, "2025", 2025),
        (H.CASE_ID, "cl 2048", "CL-2048"),
        (H.CASE_ID, "2048", None),
    ],
)
def test_case_hint_normalization(hint, raw, expected):
    assert normalize_case_hint(hint, raw) == expected


def test_describing_what_the_caller_asked_for():
    assert describe_hints({H.STATUS: "denied", H.CASE_TYPE: "auto"}) == "a denied auto claim"
    assert (
        describe_hints({H.STATUS: "open", H.CASE_TYPE: "auto", H.MONTH: 3}) == "an open auto claim from March"
    )
    assert describe_hints({H.CASE_ID: "CL-3001"}) == "a claim numbered CL-3001"
