from __future__ import annotations

from datetime import date

from apps.insurance_claims.config import DEFAULT_FIXTURES_DIR
from apps.insurance_claims.domain import DateMode, LedgerEntry, LedgerSource, SessionState, Topic
from apps.insurance_claims.fixture_loader import load_fixtures
from apps.insurance_claims.summary import mask_email, prepare_summary, summary_fact

CLAIMS = {c.case_id: c for c in load_fixtures(DEFAULT_FIXTURES_DIR).claims}


def verified_state() -> SessionState:
    state = SessionState(session_id="s", date_mode=DateMode.DEMO, consent_scenario="default")
    for key, value in (("identity.dob", "1985-03-15"), ("identity.id_last4", "4472")):
        state.ledger.put(LedgerEntry(key=key, value=value, source=LedgerSource.USER_SAID, turn=1))
    state.case_context.answered = [Topic.DENIAL_REASON, Topic.FILE_FORMAT_REQUIREMENTS]
    return state


def test_mask_email():
    assert mask_email("margaret@email.com") == "m*******@email.com"
    assert mask_email("ab@x.org") == "a***@x.org"


def test_summary_covers_discussion_outcome_and_next_steps_without_identity_details():
    summary = prepare_summary(
        verified_state(), CLAIMS["CL-2048"], "margaret@email.com", date(2026, 3, 10), False
    )
    assert summary.discussed == ["why the claim was denied", "file format requirements"]
    assert summary.outcome.startswith(
        "Claim CL-2048 (healthcare, filed January 12, 2026) is currently denied"
    )
    assert "The deadline is March 18, 2026." in summary.next_steps[0]
    assert "1985" not in summary.body and "4472" not in summary.body


def test_the_upload_link_goes_into_the_next_steps():
    link = "https://portal.example.com/claims/CL-2048/upload"
    summary = prepare_summary(
        verified_state(), CLAIMS["CL-2048"], "margaret@email.com", date(2026, 3, 10), False, link
    )
    assert f"the claim upload link ({link})" in summary.next_steps[0]
    assert link in summary.body
    assert "no real email was sent" in summary.body
    assert summary.revision == 1


def test_a_passed_deadline_turns_into_a_talk_to_a_human_step():
    summary = prepare_summary(verified_state(), CLAIMS["CL-2048"], "m@e.com", date(2026, 9, 25), False)
    assert any("has passed" in step for step in summary.next_steps)
    assert all("The deadline is" not in step for step in summary.next_steps)


def test_no_claims_summary_says_so_honestly():
    summary = prepare_summary(verified_state(), None, "ava.lopez@email.com", date(2026, 3, 10), False)
    assert summary.outcome == "No claims were found on the account."
    assert summary.case_id is None


def test_revisions_count_across_the_session():
    state = verified_state()
    state.email.last_revision = 4
    summary = prepare_summary(state, CLAIMS["CL-2102"], "m@e.com", date(2026, 3, 10), False)
    assert summary.revision == 5
    assert summary_fact(summary).id == "summary.r5"


def test_summary_shown_in_chat_has_paragraphs_and_lists_several_next_steps():
    summary = prepare_summary(verified_state(), CLAIMS["CL-2048"], "m@e.com", date(2026, 3, 10), False)
    paragraphs = summary_fact(summary).text.split("\n\n")
    assert paragraphs[0].startswith("Here's a summary of what we covered: Claim CL-2048")
    assert paragraphs[-1] == f"Next step: {summary.next_steps[0]}"

    summary.next_steps = ["Upload the report.", "Upload the office note."]
    last = summary_fact(summary).text.split("\n\n")[-1]
    assert last == "Next steps:\n- Upload the report.\n- Upload the office note."
