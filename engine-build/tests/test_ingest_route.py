"""engine.ingest.route: per-bucket structured extraction (RoutingDecision)."""

from __future__ import annotations

from engine.ingest.classify import classify
from engine.ingest.inbox import Message
from engine.ingest.route import RoutingDecision, route
from tests.fixtures.ingest.eml_loader import load_message


def _msg(from_addr: str, subject: str = "", plain_body: str = "",
        html_body: str = "") -> Message:
    return Message(uid="u-1", from_addr=from_addr, subject=subject,
                   plain_body=plain_body, html_body=html_body)


# --- job-alert: canonical job-ID extraction ---------------------------------

def test_linkedin_job_alert_extracts_deduped_job_refs():
    msg = load_message("linkedin_job_alert.eml")
    bucket = classify(msg)
    decision = route(msg, bucket)
    assert isinstance(decision, RoutingDecision)
    assert decision.bucket == "job-alert"
    # plain + html both carry the same two links; must be deduped, in order.
    assert decision.job_refs == ["linkedin:3987654321", "linkedin:4012345678"]


def test_indeed_job_alert_extracts_jk_refs():
    msg = load_message("indeed_job_alert.eml")
    decision = route(msg, classify(msg))
    assert decision.job_refs == ["indeed:abcdef1234567890", "indeed:0123456789abcdef"]


def test_glassdoor_job_alert_extracts_no_refs():
    # No LinkedIn/Indeed job-ID pattern is defined for Glassdoor (spec 7); the
    # extractor must not spuriously invent one.
    msg = load_message("glassdoor_job_alert.eml")
    decision = route(msg, classify(msg))
    assert decision.bucket == "job-alert"
    assert decision.job_refs == []


def test_job_alert_refs_carry_the_source_uid():
    msg = _msg("alert@indeed.com", "new jobs",
              plain_body="https://www.indeed.com/viewjob?jk=deadbeef00001111")
    msg.uid = "uid-42"
    decision = route(msg, "job-alert")
    assert decision.uid == "uid-42"
    assert decision.job_refs == ["indeed:deadbeef00001111"]


# --- verification: link extraction ------------------------------------------

def test_greenhouse_verification_extracts_link():
    msg = load_message("greenhouse_verification.eml")
    decision = route(msg, classify(msg))
    assert decision.bucket == "verification"
    assert decision.verify_link == "https://boards.greenhouse.io/verify?token=abc123def456ghi789"


def test_verification_with_no_link_returns_none():
    msg = _msg("no-reply@ashbyhq.com", "Confirm your application email",
              plain_body="Please confirm your email in the app.")
    decision = route(msg, "verification")
    assert decision.verify_link is None


def test_verification_link_strips_trailing_punctuation():
    msg = _msg("no-reply@ashbyhq.com", "Confirm your email",
              plain_body="Confirm here: https://ashbyhq.com/confirm?token=abc123.")
    decision = route(msg, "verification")
    assert decision.verify_link == "https://ashbyhq.com/confirm?token=abc123"


# --- outcome: status parse + posting ref ------------------------------------

def test_lever_outcome_fixture_parses_rejected_status():
    msg = load_message("lever_outcome_rejected.eml")
    decision = route(msg, classify(msg))
    assert decision.bucket == "outcome"
    assert decision.status == "rejected"
    assert decision.posting_ref == msg.subject


def test_outcome_status_interview():
    msg = _msg("no-reply@icims.com", "Interview invitation: Data Engineer")
    assert classify(msg) == "outcome"
    decision = route(msg, "outcome")
    assert decision.status == "interview"
    assert decision.posting_ref == "Interview invitation: Data Engineer"


def test_outcome_status_received():
    msg = _msg("hr@workable.com", "Application received - Data Engineer")
    assert classify(msg) == "outcome"
    decision = route(msg, "outcome")
    assert decision.status == "received"


def test_outcome_status_unrecognized_subject_is_none():
    msg = _msg("no-reply@greenhouse.io", "An update on your application")
    decision = route(msg, "outcome")
    assert decision.status is None
    assert decision.posting_ref == "An update on your application"


# --- review: no extraction --------------------------------------------------

def test_review_bucket_extracts_nothing():
    msg = load_message("unknown_sender.eml")
    decision = route(msg, classify(msg))
    assert decision.bucket == "review"
    assert decision.job_refs == []
    assert decision.verify_link is None
    assert decision.status is None
    assert decision.posting_ref is None
