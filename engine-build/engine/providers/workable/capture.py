"""Workable schema capture + parse (W5.1 Stage 2b; moved from engine.fieldmap).

Workable is greenhouse-CLASS: its per-posting apply schema is a public,
unauthenticated GET (`apply.workable.com/api/v1/jobs/{shortcode}/form`), no
browser, so capture lives here beside `capture_greenhouse` (there is no DOM to
parse). `capture_workable` fetches it; `parse_workable` maps the payload -- a
LIST of sections `[{name, fields:[...]}]` -- onto the canonical `FieldMap` (no
I/O). Each field carries a stable vendor-native `type` string (frozen from the
SPA bundle) with its OWN role/canonical maps below: Workable's vocabulary is
disjoint from Greenhouse's `input_text`/`multi_value_*`, so `normalize_type` is
NOT reused (workable owns `_WORKABLE_TYPE_MAP`).

Only the WORKABLE-specific capture/parse code moved here. The generic helpers it
shares with Greenhouse's own capture -- `_read_body_text`, `_utc_now_iso` -- STAY
in `engine.fieldmap` and are imported from there, so there is exactly one
definition of each (the 2a RULE: a helper used by BOTH vendor captures is GENERIC
and keeps its single home in `engine.fieldmap`). `capture_workable` /
`parse_workable` now live ONLY here: their callers import them from this module
directly (`workable.fill.capture`'s call-time import, and the tests' `from
engine.providers.workable.capture import capture_workable, parse_workable`,
test_providers_workable.py:26). The registry looks `capture_workable` up lazily
as a CALL-TIME callable (`PROVIDERS["workable"].capture`), resolving the attribute
on this module when invoked, so the `monkeypatch.setattr(capture,
"capture_workable", ...)` seam on this module still works
(test_providers_workable.py:284).

The read is HTTP-only (`json` + `urllib`), browser-free; the fetch-layer
User-Agent comes from `engine.kernel.capture_toolkit` (the sanctioned capture UA),
exactly as `greenhouse.capture` does.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Callable

from engine.kernel.capture_toolkit import UA
from engine.fieldmap import (
    _read_body_text,
    _utc_now_iso,
)
from engine.kernel.contracts import (
    Field,
    FieldMap,
    FieldType,
    Locator,
    Section,
)

# Workable native `type` -> ARIA role for the a11y locator hint. A `boolean`
# renders as a yes/no radio fieldset (`fieldset[data-ui=QA_n]`), so its role is
# "radio" -- which routes it to the fill()'s checkbox/radio HUMAN HAND-OFF, the
# same Turnstile/hCaptcha defence Lever uses. An UNRECOGNISED type falls back
# to "combobox" -- a HAND-OFF role, NOT a text box -- because never-send bias
# means an unrecognised control is safer handed to a human than blindly typed
# as free text; this DIVERGES from `_role_for_type`'s textbox fallback, whose
# closed Greenhouse/Ashby vocabulary treats an unmapped entry as a genuine bug
# rather than a live SPA widget this wave never sampled.
_WORKABLE_ROLE_FOR_TYPE = {
    "text": "textbox", "email": "textbox", "phone": "textbox",
    "paragraph": "textbox", "date": "textbox", "number": "textbox",
    "boolean": "radio", "file": "button",
    "dropdown": "combobox", "multiple": "listbox",
}

# Workable native `type` -> canonical FieldType (kept separate from the shared
# `_TYPE_MAP`, whose keys are the Greenhouse/Ashby vocabulary). `group` has no
# scalar canonical type: a group is flattened into its subfields (below), so no
# emitted Field is ever typed "group".
_WORKABLE_TYPE_MAP = {
    "text": FieldType.TEXT, "email": FieldType.EMAIL, "phone": FieldType.PHONE,
    "paragraph": FieldType.LONGTEXT, "date": FieldType.DATE,
    "number": FieldType.NUMBER, "boolean": FieldType.BOOLEAN,
    "file": FieldType.FILE, "dropdown": FieldType.SINGLE_SELECT,
    "multiple": FieldType.MULTI_SELECT,
}


def workable_form_url(job_id: str) -> str:
    """The public per-posting apply-form schema endpoint (job_id IS the
    shortcode): one GET, no auth, the full typed field schema."""
    return f"https://apply.workable.com/api/v1/jobs/{job_id}/form"


def workable_apply_url(slug: str, job_id: str) -> str:
    """The public apply-page URL (job_id IS the shortcode). Workable is
    browser-free, so it has no browser-capture apply body to delegate to (cf. the
    other vendors' apply-URL builders, each in its own capture.py);
    the plugin's `apply_url()` calls this directly. Byte-identical to the old
    central `registry.workable_apply_url`."""
    return f"https://apply.workable.com/{slug}/j/{job_id}/apply/"


def capture_workable(slug: str, job_id: str, opener=None, *,
                     timeout_s: float = 20, user_agent: str = UA,
                     now: Callable[[], str] | None = None) -> FieldMap:
    """Capture one Workable posting's field map via the public form endpoint.

    `opener` is any object exposing `.open(request, timeout=...)` (a urllib
    opener in production, a fake in tests), exactly like `capture_greenhouse`;
    `slug` is accepted for signature parity but the schema URL is keyed on the
    shortcode alone. The response is an array of sections, each with a typed
    `fields` list mapped onto the canonical FieldMap by `parse_workable`.
    """
    opener = opener or urllib.request.build_opener()
    url = workable_form_url(job_id)
    request = urllib.request.Request(url, headers={
        "User-Agent": user_agent,
        "Accept": "application/json",
    })
    response = opener.open(request, timeout=timeout_s)
    raw = json.loads(_read_body_text(response))
    return parse_workable(raw, slug, job_id, now=now)


def parse_workable(raw: list, slug: str, job_id: str, *,
                   now: Callable[[], str] | None = None) -> FieldMap:
    """Map a Workable form payload (list of sections) onto the canonical
    FieldMap (no I/O). posting_id is the shortcode (the payload carries no id).

    A `group` field (education/experience) is FLATTENED into its subfields,
    keyed `<group>.<sub>` and marked non-fill (Part-1 hand-off, the group's
    "+ Add" is never opened). A subfield is required only when BOTH the group
    and the subfield are required, so an OPTIONAL group never contributes a
    blocking required field (the group container itself is not emitted).
    """
    captured_at = (now or _utc_now_iso)()
    fields: list[Field] = []
    for section in raw or []:
        if not isinstance(section, dict):
            continue
        for spec in section.get("fields") or []:
            fields.extend(_workable_fields_from(spec))
    return FieldMap(vendor="workable", posting_id=str(job_id),
                    captured_at=captured_at, fields=fields)


def _workable_fields_from(spec: dict) -> list[Field]:
    """One raw Workable field -> one Field, or (for a group) its flattened
    subfields. Section is CUSTOM for a job question (`QA_*`) or account
    attribute (`CA_*`), STANDARD for every fixed id."""
    if not isinstance(spec, dict):
        return []
    field_id = spec.get("id", "")
    section = (Section.CUSTOM if str(field_id).startswith(("QA_", "CA_"))
               else Section.STANDARD)
    if (spec.get("type") or "") == "group":
        group_required = bool(spec.get("required", False))
        subs: list[Field] = []
        for sub in spec.get("fields") or []:
            subs.append(_workable_field(sub, section,
                                        key_prefix=f"{field_id}.",
                                        gate_required=group_required))
        return subs
    return [_workable_field(spec, section)]


def _workable_field(spec: dict, section: str, *, key_prefix: str = "",
                    gate_required: bool | None = None) -> Field:
    native = spec.get("type", "text")
    required = bool(spec.get("required", False))
    if gate_required is not None:
        # A group subfield only binds when the group itself is filled.
        required = gate_required and required
    accept = spec.get("supportedFileTypes")
    return Field(
        key=f"{key_prefix}{spec.get('id', '')}",
        label=spec.get("label", ""),
        type=native,
        required=required,
        options=_workable_choice_labels(spec.get("choices")),
        source="workable_form",
        locator=Locator(role=_workable_role_for_type(native),
                        name=spec.get("label", "")),
        step_index=0,
        conditional_on=None,
        decline_allowed=False,
        max_length=spec.get("maxLength"),
        accept_types=list(accept) if isinstance(accept, list) else None,
        norm_type=_WORKABLE_TYPE_MAP.get(native, ""),
        section=section,
    )


def _workable_role_for_type(native: str) -> str:
    # Never-send bias: an unrecognised control is handed off, not blindly typed.
    return _WORKABLE_ROLE_FOR_TYPE.get(native, "combobox")


def _workable_choice_labels(choices) -> list[str]:
    """Option labels for a Workable dropdown/multiple: `choices[].body` (the
    SPA's own choice shape). No fixture exercises this yet -- the sampled forms
    carry no dropdown/multiple field -- so it is defensive parity for capture."""
    if not isinstance(choices, list):
        return []
    return [str(c.get("body", "")) for c in choices if isinstance(c, dict)]
