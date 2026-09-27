from __future__ import annotations

from datetime import date

from apps.insurance_claims.config import DEFAULT_FIXTURES_DIR
from apps.insurance_claims.domain import Intent, Topic
from apps.insurance_claims.fixture_loader import load_fixtures
from apps.insurance_claims.processing import DEFAULT_TOPICS, CaseFacts

FIXTURES = load_fixtures(DEFAULT_FIXTURES_DIR)
FACTS = CaseFacts(FIXTURES)
CLAIMS = {claim.case_id: claim for claim in FIXTURES.claims}
DEMO_DAY = date(2026, 3, 10)
AFTER_DEADLINE = date(2026, 9, 25)


def answer(case_id, topics, as_of=DEMO_DAY, **kwargs):
    return FACTS.answer(CLAIMS[case_id], topics, as_of, **kwargs)


def texts(result):
    return {fact.id.split(".", 2)[2]: fact.text for fact in result.facts}


def test_denial_path_covers_reason_documents_deadline_and_method():
    result = answer("CL-2048", DEFAULT_TOPICS[Intent.DENIAL_QUESTION])
    t = texts(result)
    assert "did not include the pathology report" in t["denial_reason"]
    assert t["documents_needed"].endswith("the pathology report and the office note.")
    assert (
        t["appeal_deadline"]
        == "The appeal deadline for claim CL-2048 is March 18, 2026, which is 8 days from today."
    )
    assert "member portal or the claim upload link" in t["submission_method"]
    assert t["upload_link"] == (
        "The upload link for claim CL-2048 is https://portal.example.com/claims/CL-2048/upload (a demo link)."
    )
    assert result.required == {f.id for f in result.facts}
    assert not result.needs_handoff


def test_a_passed_deadline_needs_a_human_and_overrides_within_a_week():
    result = answer("CL-2048", [Topic.APPEAL_DEADLINE, Topic.SUBMISSION_TIMING], as_of=AFTER_DEADLINE)
    assert result.needs_handoff
    assert all("within a week" not in fact.text for fact in result.facts)
    assert "has already passed" in texts(result)["appeal_deadline"]


def test_submission_timing_before_the_deadline_names_both_in_one_sentence():
    result = answer("CL-2048", [Topic.SUBMISSION_TIMING])
    assert [f.id for f in result.facts] == ["claim.CL-2048.submission_timing"]
    assert result.facts[0].text == (
        "For claim CL-2048, please submit the pathology report and the office note within a week, "
        "so they arrive before the appeal deadline of March 18, 2026, which is 8 days from today."
    )
    assert result.required == {"claim.CL-2048.submission_timing"}
    # Asked together with the deadline, the deadline is still said once, inside the timing advice.
    both = answer("CL-2048", [Topic.APPEAL_DEADLINE, Topic.SUBMISSION_TIMING])
    assert [f.id for f in both.facts] == ["claim.CL-2048.submission_timing"]
    assert both.required == {"claim.CL-2048.submission_timing"}


def test_amounts_keep_their_meaning():
    t = texts(answer("CL-2048", [Topic.AMOUNTS]))
    assert "$0.00" in t["amount.net_pay"]
    assert "not a guaranteed payment" in t["amount.allowed_max"] and "$1,450.00" in t["amount.allowed_max"]
    assert "not a balance the patient owes" in t["amount.net_fee"]
    assert "not a promise of payment" in t["amount.expected"]


def test_a_question_about_one_document_leads_with_its_guidance():
    result = answer(
        "CL-2048",
        [Topic.FILE_FORMAT_REQUIREMENTS],
        details={Topic.FILE_FORMAT_REQUIREMENTS: "scan of the pathology report"},
    )
    assert result.required == {"claim.CL-2048.guidance.pathology_report"}
    assert "claim.CL-2048.guidance.office_note" not in {f.id for f in result.facts}
    assert "high-quality scan is acceptable" in texts(result)["guidance.pathology_report"]


def test_alternatives_then_a_human_review_once_exhausted():
    first = answer(
        "CL-2048",
        [Topic.MISSING_REQUIRED_MATERIAL_ALTERNATIVES],
        details={
            Topic.MISSING_REQUIRED_MATERIAL_ALTERNATIVES: "office note",
        },
    )
    assert first.required == {"claim.CL-2048.alternative.office_note"}
    assert first.gave_alternatives and not first.needs_handoff

    again = answer("CL-2048", [Topic.MISSING_REQUIRED_MATERIAL_ALTERNATIVES], alternatives_given=True)
    assert again.required == {"claim.CL-2048.alternatives_exhausted"}
    assert again.needs_handoff
    assert not any(f.id.startswith("claim.CL-2048.alternative.") for f in again.facts)


def test_a_document_without_its_own_guidance_uses_the_default():
    result = answer(
        "CL-3001",
        [Topic.MISSING_REQUIRED_MATERIAL_ALTERNATIVES],
        details={
            Topic.MISSING_REQUIRED_MATERIAL_ALTERNATIVES: "diagnosis report",
        },
    )
    fact = next(f for f in result.facts if f.id == "claim.CL-3001.alternative.diagnosis_report")
    assert fact.source.endswith("document_alternative_guidance.default")


def test_document_topics_do_not_apply_when_nothing_is_requested():
    t = texts(answer("CL-2102", [Topic.SUBMISSION_METHOD]))
    assert t["submission_method.not_applicable"].startswith(
        "No documents are currently requested for claim CL-2102"
    )


def test_placeholders_are_filled_by_code():
    t = texts(answer("CL-2048", [Topic.PROCESSING_TIME_AFTER_SUBMISSION]))
    assert "{" not in t["processing_time_after_submission"]
    assert "usually less than a week" in t["processing_time_after_submission"]


def test_unknown_follow_ups_fall_back_and_offer_a_human():
    result = answer("CL-2048", [Topic.OTHER])
    assert result.needs_handoff
    assert "separate claim-specific rule" in result.facts[0].text


def test_every_fact_is_tied_to_the_claim_owner():
    result = answer("CL-2048", list(Topic))
    assert {fact.party_id for fact in result.facts} == {"P9"}
    assert all(fact.source for fact in result.facts)


def test_a_deadline_sooner_than_a_week_replaces_the_one_week_advice():
    close = answer("CL-2048", [Topic.SUBMISSION_TIMING], as_of=date(2026, 3, 14))
    assert [f.id for f in close.facts] == ["claim.CL-2048.appeal_deadline"]
    text = close.facts[0].text
    assert text.startswith(
        "For claim CL-2048, please submit the pathology report and the office note by the appeal"
    )
    assert "March 18, 2026, which is 4 days from today" in text and "week" not in text
    # With more time, the advice stands and names the deadline it must beat.
    later = texts(answer("CL-2048", [Topic.SUBMISSION_TIMING]))
    assert later["submission_timing"].endswith(
        "before the appeal deadline of March 18, 2026, which is 8 days from today."
    )
