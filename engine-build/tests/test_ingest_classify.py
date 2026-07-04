"""engine.ingest.classify: sender-domain-first, then-subject bucket rules."""

from __future__ import annotations

from engine.ingest.classify import classify
from engine.ingest.inbox import Message
from tests.fixtures.ingest.eml_loader import load_message


def _msg(from_addr: str, subject: str = "", plain_body: str = "",
        html_body: str = "") -> Message:
    return Message(uid="x", from_addr=from_addr, subject=subject,
                   plain_body=plain_body, html_body=html_body)


# --- fixture-driven: sender-domain rules resolve job-alert buckets ---------

def test_linkedin_job_alert_fixture_classifies_job_alert():
    assert classify(load_message("linkedin_job_alert.eml")) == "job-alert"


def test_indeed_job_alert_fixture_classifies_job_alert():
    assert classify(load_message("indeed_job_alert.eml")) == "job-alert"


def test_glassdoor_job_alert_fixture_classifies_job_alert():
    assert classify(load_message("glassdoor_job_alert.eml")) == "job-alert"


# --- fixture-driven: ATS senders need SUBJECT to disambiguate --------------

def test_greenhouse_verification_fixture_classifies_verification():
    assert classify(load_message("greenhouse_verification.eml")) == "verification"


def test_lever_outcome_fixture_classifies_outcome():
    assert classify(load_message("lever_outcome_rejected.eml")) == "outcome"


# --- fixture-driven: unknown sender never trusts subject content -----------

def test_unknown_sender_fixture_classifies_review_despite_subject_cues():
    msg = load_message("unknown_sender.eml")
    # sanity: the fixture subject DOES contain "interview" and the body
    # contains "verify", to prove the sender gate (not subject content) wins.
    assert "interview" in msg.subject.lower()
    assert classify(msg) == "review"


# --- direct-construction: sender edge cases ---------------------------------

def test_glassdoor_any_localpart_is_job_alert():
    assert classify(_msg("someone@glassdoor.com", "weekly digest")) == "job-alert"


def test_indeed_non_alert_localpart_needs_subject_cue():
    # not the canonical alert@indeed.com sender; falls through to the generic
    # job-alert-domain + subject-cue rule.
    assert classify(_msg("noreply@indeed.com", "Job alert: 4 new roles")) == "job-alert"


def test_indeed_non_alert_localpart_without_cue_is_review():
    assert classify(_msg("noreply@indeed.com", "Your weekly Indeed digest")) == "review"


def test_linkedin_generic_sender_with_job_alert_subject_cue():
    assert classify(_msg("no-reply@linkedin.com", "Job alert: new roles for you")) == "job-alert"


def test_linkedin_generic_sender_without_cue_is_review():
    assert classify(_msg("no-reply@linkedin.com", "You have a new connection")) == "review"


# --- direct-construction: ATS domain coverage (incl. myworkday wildcard) ----

def test_ashby_verification_subject_cue():
    assert classify(_msg("no-reply@ashbyhq.com", "Confirm your application email")) == "verification"


def test_workable_outcome_received_cue():
    assert classify(_msg("hr@workable.com", "Application received - Data Engineer")) == "outcome"


def test_icims_outcome_interview_cue():
    assert classify(_msg("no-reply@icims.com", "Interview invitation: Data Engineer")) == "outcome"


def test_myworkday_wildcard_subdomain_matches_ats_domain():
    assert classify(_msg("no-reply@acme.myworkday.com", "Please verify your account")) == "verification"


def test_known_ats_sender_with_no_matching_subject_cue_is_review():
    assert classify(_msg("no-reply@greenhouse.io", "Thanks for your interest in Acme Corp")) == "review"


# --- direct-construction: domain-matching is not a bare endswith() ----------

def test_ats_domain_match_requires_dot_boundary_not_bare_suffix():
    # "evilgreenhouse.io" ends with "greenhouse.io" as a raw string, but is a
    # different registrable domain; must NOT be treated as an ATS sender.
    assert classify(_msg("no-reply@evilgreenhouse.io", "Please verify your email")) == "review"


def test_ats_domain_match_allows_genuine_subdomain():
    # "boards.greenhouse.io" IS a genuine Greenhouse subdomain.
    assert classify(_msg("no-reply@boards.greenhouse.io", "Please verify your email")) == "verification"


def test_unknown_sender_default_is_review():
    assert classify(_msg("hello@example.com", "hello there")) == "review"
