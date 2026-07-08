"""Greenhouse schema capture + parse (W5.1 Stage 2a; moved from engine.fieldmap).

Greenhouse is the browserless vendor: the sanctioned schema source is
`boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}?questions=true`, one polite
GET per posting. `capture_greenhouse` fetches it; `parse_greenhouse` maps the
payload onto the canonical `FieldMap` (no I/O).

Only the GREENHOUSE-specific capture/parse code moved here. The generic helpers
that Workable's own capture (`capture_workable` / `parse_workable`) also relies
on -- `normalize_type` + `_HIDDEN_TYPES` (the shared vendor-native type mapper),
`_read_body_text`, `_utc_now_iso` -- STAY in `engine.fieldmap` and are imported
from there, so there is exactly one definition of each. `engine.fieldmap` keeps
a lazy re-export shim for every name moved here, so existing importers
(`engine.fill`, the tests; the old `registry.py` consumer was deleted in Stage
3c) keep resolving them via `engine.fieldmap` unchanged, and the
`monkeypatch.setattr(fieldmap, "capture_greenhouse", ...)` seam still works.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Callable

from engine.kernel.capture_toolkit import UA
from engine.fieldmap import (
    _HIDDEN_TYPES,
    _read_body_text,
    _utc_now_iso,
    normalize_type,
)
from engine.kernel.contracts import (
    Field,
    FieldMap,
    Locator,
    Section,
    _role_for_type,
)
from engine.kernel.resolve import _DECLINE_SECTIONS

# parse_greenhouse's `source` tag (the bucket a question arrived in) -> the
# canonical Section. Unrecognised sources fall back to STANDARD.
_SECTION_FOR_SOURCE = {
    "questions": Section.STANDARD,
    "location_questions": Section.LOCATION,
    "compliance": Section.COMPLIANCE_EEOC,
    "demographic": Section.DEMOGRAPHIC,
}


def greenhouse_questions_url(slug: str, job_id: str) -> str:
    """The sanctioned schema endpoint (R-WT-8 D3): one GET, questions=true."""
    return ("https://boards-api.greenhouse.io/v1/boards/"
            f"{slug}/jobs/{job_id}?questions=true")


def greenhouse_apply_url(slug: str, job_id: str) -> str:
    """The public Greenhouse apply page (the `job-boards.greenhouse.io/{slug}/
    jobs/{job_id}` host is the newer variant of the same page)."""
    return f"https://boards.greenhouse.io/{slug}/jobs/{job_id}"


def capture_greenhouse(slug: str, job_id: str, opener=None, *,
                       timeout_s: float = 20, user_agent: str = UA,
                       now: Callable[[], str] | None = None) -> FieldMap:
    """Capture one Greenhouse posting's field map via the questions endpoint.

    `opener` is any object exposing `.open(request, timeout=...)` (a urllib
    opener in production, a fake in tests); a single polite GET with the honest
    fetch-layer User-Agent is all the read costs. The response is the standard
    Greenhouse job payload with a `questions` array (plus `location_questions`,
    `compliance`, and a separate `demographic_questions` block); every one of
    those becomes a Field, tagged by the section it came from.
    """
    opener = opener or urllib.request.build_opener()
    url = greenhouse_questions_url(slug, job_id)
    request = urllib.request.Request(url, headers={
        "User-Agent": user_agent,
        "Accept": "application/json",
    })
    response = opener.open(request, timeout=timeout_s)
    raw = json.loads(_read_body_text(response))
    return parse_greenhouse(raw, slug, job_id, now=now)


def parse_greenhouse(raw: dict, slug: str, job_id: str, *,
                     now: Callable[[], str] | None = None) -> FieldMap:
    """Map a questions=true payload onto the canonical FieldMap (no I/O)."""
    captured_at = (now or _utc_now_iso)()
    posting_id = str(raw.get("id") or job_id)
    fields: list[Field] = []
    for section in ("questions", "location_questions", "compliance"):
        source = "questions" if section == "questions" else section
        for question in raw.get(section) or []:
            fields.extend(_fields_from_question(question, source))
    fields.extend(_fields_from_demographic(raw.get("demographic_questions")))
    return FieldMap(vendor="greenhouse", posting_id=posting_id,
                    captured_at=captured_at, fields=fields)


def _fields_from_question(question: dict, source: str) -> list[Field]:
    label = question.get("label", "")
    required = bool(question.get("required", False))
    section = _SECTION_FOR_SOURCE.get(source, Section.STANDARD)
    decline_allowed = section in _DECLINE_SECTIONS
    out: list[Field] = []
    for sub in question.get("fields") or []:
        field_type = sub.get("type", "input_text")
        if field_type in _HIDDEN_TYPES:
            continue  # input_hidden: portal tracking, never a user field
        out.append(Field(
            key=sub.get("name", ""),
            label=label,
            type=field_type,
            required=False if decline_allowed else required,
            options=_option_labels(sub.get("values")),
            source=source,
            locator=Locator(role=_role_for_type(field_type), name=label),
            step_index=0,
            conditional_on=None,
            decline_allowed=decline_allowed,
            norm_type=normalize_type(field_type),
            section=section,
        ))
    return out


def _fields_from_demographic(block) -> list[Field]:
    """The demographic block is a separate object with its own question shape
    (`answer_options`, type on the question). Captured but always manual-only,
    and (W5) always `decline_allowed=True, required=False` regardless of what
    the raw payload's own `required` flag says (R-WT-8 8: never auto-answered,
    never blocking)."""
    if not isinstance(block, dict):
        return []
    out: list[Field] = []
    for question in block.get("questions") or []:
        field_type = question.get("type", "multi_value_single_select")
        out.append(Field(
            key=f"demographic_{question.get('id', '')}",
            label=question.get("label", ""),
            type=field_type,
            required=False,
            options=_option_labels(question.get("answer_options")),
            source="demographic",
            locator=Locator(role=_role_for_type(field_type),
                            name=question.get("label", "")),
            step_index=0,
            conditional_on=None,
            decline_allowed=True,
            norm_type=normalize_type(field_type),
            section=Section.DEMOGRAPHIC,
        ))
    return out


def _option_labels(values) -> list[str]:
    if not isinstance(values, list):
        return []
    labels: list[str] = []
    for value in values:
        if isinstance(value, dict):
            labels.append(str(value.get("label", value.get("value", ""))))
        else:
            labels.append(str(value))
    return labels
