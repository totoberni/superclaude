"""Structured routing decisions per classified bucket (spec 7, w5-locked).

`route(msg, bucket) -> RoutingDecision`. Extraction only, for now:

- job-alert: pulls canonical job refs (LinkedIn `/comm/jobs/view/(\\d+)`; Indeed
  `jk=` param) that a future wave feeds into the discovery pipeline (`engine.run`) / `engine.store`.
- verification: pulls the first link in the body for an ntfy handoff.
- outcome: parses a received/rejected/interview status from the subject cue
  that also drove the classify.py bucket decision, plus a best-effort
  human-readable posting reference (the subject line itself; ATS outcome mail
  has no stable machine ID to key on without per-vendor parsing this wave
  intentionally defers).
- review: no extraction; a human looks at the message directly.

This wave stops at the dataclass: `route()` does NOT call into
`engine.run` / `engine.store` / `engine.notify`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from engine.ingest.inbox import Message

_LINKEDIN_JOB_RE = re.compile(r"/comm/jobs/view/(\d+)")
_INDEED_JK_RE = re.compile(r"[?&]jk=([0-9a-zA-Z]+)")
_URL_RE = re.compile(r"https?://\S+")

# Order matters only in that each is a distinct, non-overlapping cue in
# practice; checked in this order so a rejection ("unfortunately") is never
# masked by an incidental "interview" mention elsewhere in the subject.
_STATUS_CUES = (
    ("unfortunately", "rejected"),
    ("interview", "interview"),
    ("application received", "received"),
)


@dataclass
class RoutingDecision:
    bucket: str
    uid: str = ""
    job_refs: list[str] = field(default_factory=list)
    verify_link: str | None = None
    status: str | None = None
    posting_ref: str | None = None


def route(msg: Message, bucket: str) -> RoutingDecision:
    if bucket == "job-alert":
        return RoutingDecision(bucket=bucket, uid=msg.uid, job_refs=_job_refs(msg))
    if bucket == "verification":
        return RoutingDecision(bucket=bucket, uid=msg.uid, verify_link=_verify_link(msg))
    if bucket == "outcome":
        return RoutingDecision(bucket=bucket, uid=msg.uid,
                               status=_outcome_status(msg.subject),
                               posting_ref=(msg.subject or "").strip() or None)
    return RoutingDecision(bucket=bucket, uid=msg.uid)


def _job_refs(msg: Message) -> list[str]:
    body = "\n".join(part for part in (msg.plain_body, msg.html_body) if part)
    refs: list[str] = []
    refs.extend(f"linkedin:{job_id}" for job_id in _LINKEDIN_JOB_RE.findall(body))
    refs.extend(f"indeed:{job_id}" for job_id in _INDEED_JK_RE.findall(body))
    return _dedup(refs)


def _verify_link(msg: Message) -> str | None:
    body = "\n".join(part for part in (msg.plain_body, msg.html_body) if part)
    match = _URL_RE.search(body)
    if not match:
        return None
    return match.group(0).rstrip(').,>"\'')


def _outcome_status(subject: str) -> str | None:
    lowered = (subject or "").lower()
    for cue, status in _STATUS_CUES:
        if cue in lowered:
            return status
    return None


def _dedup(refs: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            unique.append(ref)
    return unique
