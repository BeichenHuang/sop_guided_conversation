from __future__ import annotations

import json
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from apps.insurance_claims.config import DEFAULT_FIXTURES_DIR
from apps.insurance_claims.domain import IdType
from apps.insurance_claims.fixture_loader import (
    DOCUMENT_GUIDANCE_KEYS,
    ClaimStatus,
    FixtureError,
    load_fixtures,
)


def test_real_fixtures_load_and_cross_check():
    data = load_fixtures(DEFAULT_FIXTURES_DIR)

    assert [p.party_id for p in data.policyholders] == ["P9", "P7", "P12", "P13"]
    id_types = {p.party_id: p.id_type for p in data.policyholders}
    assert id_types["P9"] is IdType.SSN_LAST4
    assert id_types["P12"] is IdType.NATIONAL_ID_LAST4
    assert id_types["P13"] is IdType.NATIONAL_ID_LAST4

    claim = next(c for c in data.claims if c.case_id == "CL-2048")
    assert claim.status is ClaimStatus.DENIED
    assert claim.net_fee == Decimal("1450.00")
    assert claim.appeal_deadline == date(2026, 3, 18)

    assert data.consent_scenarios["default"].status_sequence == ("pending", "approved")
    assert data.consent_scenarios["timeout"].status_sequence == ("pending",) * 5
    assert data.representatives[0].buyer_party_id == "P9"
    for key in DOCUMENT_GUIDANCE_KEYS.values():
        assert key in data.guidelines.document_guidance


def _copy_fixtures(tmp_path: Path) -> Path:
    directory = tmp_path / "fixtures"
    shutil.copytree(DEFAULT_FIXTURES_DIR, directory)
    return directory


def _edit(directory: Path, filename: str, mutate) -> None:
    path = directory / filename
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def _set(key, value, index=0):
    def mutate(data):
        data[index][key] = value

    return mutate


def _duplicate_first(data):
    data.append(dict(data[0]))


def _unknown_placeholder(data):
    data["claim_followup_guidance"][0]["en"] += " See {portal_url}."


def _unknown_topic(data):
    data["claim_followup_guidance"][0]["topic"] = "weather"


def _bad_consent_status(data):
    data["default"]["status_sequence"] = ["pending", "maybe"]


def _drop_default_scenario(data):
    del data["default"]


@pytest.mark.parametrize(
    ("filename", "mutate", "expected"),
    [
        ("claims.json", _set("party_id", "P99"), "unknown party_id 'P99'"),
        ("claims.json", _set("net_pay", 0.0), "decimal strings"),
        ("claims.json", _set("status", "approved"), "status"),
        ("claims.json", _set("appeal_deadline", "2025-01-01"), "appeal_deadline is before created_at"),
        ("policyholders.json", _set("id_type", "passport"), "id_type"),
        ("policyholders.json", _set("id_last4", "44721"), "id_last4"),
        ("policyholders.json", _duplicate_first, "duplicate 'P9'"),
        ("representatives.json", _set("buyer_name", "Someone Else"), "does not match P9"),
        ("required_document_guideline.json", _unknown_placeholder, "unknown placeholders ['portal_url']"),
        ("required_document_guideline.json", _unknown_topic, "topic"),
        ("consent_scenarios.json", _bad_consent_status, "status_sequence"),
        ("consent_scenarios.json", _drop_default_scenario, "'default' scenario"),
    ],
)
def test_broken_fixtures_are_reported(tmp_path, filename, mutate, expected):
    directory = _copy_fixtures(tmp_path)
    _edit(directory, filename, mutate)
    with pytest.raises(FixtureError) as excinfo:
        load_fixtures(directory)
    assert expected in str(excinfo.value)


def test_missing_file_is_reported(tmp_path):
    directory = _copy_fixtures(tmp_path)
    (directory / "claims.json").unlink()
    with pytest.raises(FixtureError, match="claims.json: file not found"):
        load_fixtures(directory)


def test_invalid_json_is_reported(tmp_path):
    directory = _copy_fixtures(tmp_path)
    (directory / "policyholders.json").write_text("[{", encoding="utf-8")
    with pytest.raises(FixtureError, match="policyholders.json: invalid JSON"):
        load_fixtures(directory)
