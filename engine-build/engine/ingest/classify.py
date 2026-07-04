"""Sender-domain-first, then-subject email classifier (spec 7, w5-locked).

`classify(msg) -> bucket` in {"job-alert", "verification", "outcome", "review"}.
Sender-domain rules run first and are authoritative for job-alert (the LinkedIn
`jobalerts-noreply` / `jobs-listings` senders, `alert@indeed.com`, any
`*@glassdoor.com`). ATS domains (greenhouse.io, hire.lever.co, ashbyhq.com,
workable.com, myworkday*, icims.com) send BOTH verification and outcome mail, so
subject cues disambiguate those two buckets once the sender is known-ATS.

An unrecognised sender is never trusted to self-classify via its subject line:
letting subject text alone drive the bucket would let a spoofed "From" address
talk its way into "verification"/"outcome" (the anti-injection posture locked
for this engine). Unknown senders go straight to "review", first-match-wins,
never hard-fail.
"""

from __future__ import annotations

from email.utils import parseaddr

from engine.ingest.inbox import Message

BUCKETS = ("job-alert", "verification", "outcome", "review")

_LINKEDIN_ALERT_LOCALPARTS = ("jobalerts-noreply", "jobs-listings")
_JOB_ALERT_DOMAINS = ("linkedin.com", "indeed.com", "glassdoor.com")
_JOB_ALERT_SUBJECT_CUES = ("job alert",)

# myworkday.com is deliberately a suffix match (companies host on
# <company>.myworkday.com); the rest are exact-or-subdomain via _domain_matches.
_ATS_DOMAINS = ("greenhouse.io", "hire.lever.co", "ashbyhq.com", "workable.com",
                "myworkday.com", "icims.com")

_VERIFICATION_SUBJECT_CUES = ("verify", "confirm")
_OUTCOME_SUBJECT_CUES = ("application received", "interview", "unfortunately")


def classify(msg: Message) -> str:
    """First-match-wins rule-set; always returns a bucket, never raises."""
    localpart, domain = _address_parts(msg.from_addr)

    if domain == "glassdoor.com":
        return "job-alert"
    if domain == "indeed.com" and localpart == "alert":
        return "job-alert"
    if domain == "linkedin.com" and localpart in _LINKEDIN_ALERT_LOCALPARTS:
        return "job-alert"
    if domain in _JOB_ALERT_DOMAINS and _has_cue(msg.subject, _JOB_ALERT_SUBJECT_CUES):
        return "job-alert"

    if _is_ats_domain(domain):
        return _classify_ats_subject(msg.subject)

    return "review"


def _classify_ats_subject(subject: str) -> str:
    if _has_cue(subject, _VERIFICATION_SUBJECT_CUES):
        return "verification"
    if _has_cue(subject, _OUTCOME_SUBJECT_CUES):
        return "outcome"
    return "review"


def _is_ats_domain(domain: str) -> bool:
    return any(_domain_matches(domain, base) for base in _ATS_DOMAINS)


def _domain_matches(domain: str, base: str) -> bool:
    """Exact match or a genuine subdomain (`boards.` + `greenhouse.io`).

    Deliberately NOT a bare `endswith(base)`: that would also match a
    typosquat like `evilgreenhouse.io`, which does not have a `.` boundary
    before the base domain.
    """
    return domain == base or domain.endswith(f".{base}")


def _address_parts(from_addr: str) -> tuple[str, str]:
    _, addr = parseaddr(from_addr or "")
    addr = addr or (from_addr or "")
    local, _, domain = addr.rpartition("@")
    return local.lower(), domain.lower()


def _has_cue(text: str, cues: tuple[str, ...]) -> bool:
    lowered = (text or "").lower()
    return any(cue in lowered for cue in cues)
