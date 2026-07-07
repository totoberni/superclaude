"""ATS field-map capture + deterministic coverage classification (W4 3.1).

Reaches the FULL application schema of a posting (labels, types, required flags,
option enumerations) so that every automatable queue item can carry a concrete
field map and the SSOT playtest loop can judge coverage field by field.

Greenhouse is the browserless vendor: the sanctioned schema source is
`boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}?questions=true`, one polite
GET per posting (Lever/Ashby need a browser and land in browse.py, a later wave).
The captured shape is the R-WT-8 C canonical field map (schema_version 2 adds
the trailing W5 fields additively; every consumer built against schema_version
1 keeps working via the new fields' defaults):

    {vendor, posting_id, schema_version, captured_at,
     fields: [{key, label, type, required, options, source,
               locator: {role, name}, step_index, conditional_on,
               decline_allowed, max_length, accept_types, norm_type,
               section}]}

`coverage(fieldmap, ssot, profile)` classifies every REQUIRED field as
answerable / missing:<dotted-path-guess> / manual-only. It is deterministic
keyword matching against the SSOT buckets (canned_answers, identity,
work_authorization, links) with NO LLM. It NEVER writes the SSOT: a MISSING
field is a dotted-path guess that feeds a questionnaire item (7.6). File uploads
and EEO/demographic fields are always manual-only and never auto-answered
(R-WT-8 8).
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from engine.fetch import UA
from engine.kernel.contracts import (  # noqa: F401
    SCHEMA_VERSION,
    Field,
    FieldMap,
    FieldType,
    Locator,
    Section,
    _ROLE_FOR_TYPE,
    _role_for_type,
)
from engine.ssot import MISSING, SSOT

# The generic coverage-classification cluster moved to engine.kernel.resolve
# (W5.1 kernel extraction). Re-exported here so every pre-Stage-2 importer
# (run.py, tests, engine.fill) keeps resolving these names unchanged; the
# Greenhouse widget resolvers below are injected into the kernel classifier via
# the `coverage` shim. Transitional until Stage 2/3 moves callers onto the
# kernel + registry injection directly.
from engine.kernel.resolve import (  # noqa: F401
    ANSWERABLE,
    MANUAL_ONLY,
    MISSING_STATUS,
    CoverageReport,
    FieldCoverage,
    _ANSWER_MATCHERS,
    _DECLINE_SECTIONS,
    _DEMOGRAPHIC_KEYWORDS,
    _FIRST_NAME_KEYWORDS,
    _LAST_NAME_KEYWORDS,
    _SKILLS_EXPERIENCE_RE,
    _answerable_path,
    _classify_field,
    _manual_only_reason,
    _missing_path_guess,
    _profile_answers_work_auth,
)

# parse_greenhouse's `source` tag (the bucket a question arrived in) -> the
# canonical Section. Unrecognised sources fall back to STANDARD.
_SECTION_FOR_SOURCE = {
    "questions": Section.STANDARD,
    "location_questions": Section.LOCATION,
    "compliance": Section.COMPLIANCE_EEOC,
    "demographic": Section.DEMOGRAPHIC,
}

# Vendor-native `type` string -> canonical FieldType. Covers the Greenhouse
# HTTP-schema vocabulary (also what Lever's DOM controls and Ashby's own
# _ASHBY_TYPE_MAP shim collapse into today, browse.py) PLUS the raw Ashby
# ApiJobPosting type strings, so a future provider can call `normalize_type`
# directly on either vocabulary without drift.
_TYPE_MAP = {
    # Greenhouse HTTP schema / Lever DOM / post-collapse Ashby.
    "input_text": FieldType.TEXT,
    "textarea": FieldType.LONGTEXT,
    "multi_value_single_select": FieldType.SINGLE_SELECT,
    "multi_value_multi_select": FieldType.MULTI_SELECT,
    "boolean": FieldType.BOOLEAN,
    "input_file": FieldType.FILE,
    # Raw Ashby ApiJobPosting field `type` (pre-collapse).
    "String": FieldType.TEXT,
    "Email": FieldType.EMAIL,
    "LongText": FieldType.LONGTEXT,
    "ValueSelect": FieldType.SINGLE_SELECT,
    "MultiValueSelect": FieldType.MULTI_SELECT,
    "Phone": FieldType.PHONE,
    "Date": FieldType.DATE,
    "Boolean": FieldType.BOOLEAN,
    "File": FieldType.FILE,
    "Number": FieldType.NUMBER,
}

# Greenhouse tracking/hidden fields (`input_hidden`) carry no user-facing
# control at all: never a fillable Field, so `normalize_type` returns "" for
# them and capture skips creating a Field for the sub-field entirely.
_HIDDEN_TYPES = frozenset({"input_hidden"})


def normalize_type(vendor_native: str) -> str:
    """Map a vendor-native `type` string onto the canonical `FieldType`.

    Reused across every capture path (Greenhouse HTTP schema, the raw Ashby
    ApiJobPosting type before browse.py's own collapse, Lever's DOM-derived
    types which already share the Greenhouse vocabulary) so downstream
    consumers reason about ONE type system. `input_hidden` has no user-facing
    control and returns "" (skip signal, not a `FieldType` member); an
    unrecognised native falls back to `FieldType.TEXT` (mirrors
    `_role_for_type`'s fallback-to-textbox convention).
    """
    key = vendor_native or ""
    if key in _HIDDEN_TYPES:
        return ""
    return _TYPE_MAP.get(key, FieldType.TEXT)


# Greenhouse's location-autocomplete widget (round-1 live-capture finding):
# the composite question shares one label across three sub-fields keyed
# `location`/`longitude`/`latitude`. All three are mechanically populated by
# the portal's JS widget, never typed by the applicant, so they are matched
# by KEY (the shared label carries no distinguishing text) ahead of the
# generic label matchers. `location` still resolves to real applicant data
# (the address); `longitude`/`latitude` are pure portal telemetry.
_LOCATION_WIDGET_KEY = "location"
_PORTAL_WIDGET_KEYS = {"longitude", "latitude"}

# Greenhouse's paste-in resume/cover-letter textareas (`resume_text`,
# `cover_letter_text`) share their label ("Resume"/"Resume/CV") with the
# sibling FILE upload field, so label keyword matching alone cannot
# distinguish them: they are matched by KEY, ahead of the generic label
# matchers, same as `_LOCATION_WIDGET_KEY` above. Each maps to the ordered
# SSOT dotted paths tried in turn; the first that resolves in the SSOT wins.
_KEY_TEXT_PATHS = {
    "resume_text": ("canned_answers.resume_text", "documents.cv_text"),
    "cover_letter_text": ("canned_answers.cover_letter_text",
                          "canned_answers.cover_letter"),
}

def greenhouse_questions_url(slug: str, job_id: str) -> str:
    """The sanctioned schema endpoint (R-WT-8 D3): one GET, questions=true."""
    return ("https://boards-api.greenhouse.io/v1/boards/"
            f"{slug}/jobs/{job_id}?questions=true")


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


# -- Workable schema capture (W5.4) --------------------------------------------
# Workable is greenhouse-CLASS: its per-posting apply schema is a public,
# unauthenticated GET (no browser), so capture lives here beside
# `capture_greenhouse` (NOT in browse.py -- there is no DOM to parse). The
# response is a LIST of sections `[{name, fields:[...]}]`; each field carries a
# stable vendor-native `type` string (frozen from the SPA bundle) that gets its
# own role/canonical maps below (Workable's vocabulary is disjoint from
# Greenhouse's `input_text`/`multi_value_*`, so `normalize_type` is not reused).

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


def coverage(fieldmap: FieldMap, ssot: SSOT, profile: dict,
             vendor_resolver=None) -> CoverageReport:
    """Classify every REQUIRED field of `fieldmap` against the SSOT (shim).

    Transitional shim over the kernel classifier (`engine.kernel.resolve.
    coverage`): injects `GREENHOUSE_WIDGET_RESOLVER` as the default so every
    pre-Stage-2 caller (run.py, tests, incl. the bare-coverage greenhouse-widget
    tests) keeps today's Greenhouse-widget behaviour. Stage 2/3 moves callers
    onto the kernel + registry injection and drops this shim. The kernel import
    is call-time to avoid an import cycle at fieldmap load.
    """
    from engine.kernel.resolve import coverage as _kernel_coverage
    return _kernel_coverage(
        fieldmap, ssot, profile,
        vendor_resolver=(vendor_resolver if vendor_resolver is not None
                         else GREENHOUSE_WIDGET_RESOLVER))


def _location_widget_path(fld: Field, ssot: SSOT) -> str | None:
    """The Greenhouse location-autocomplete widget's `location` sub-field is
    mechanically populated (never typed by the applicant): resolve it via the
    identity address directly, ahead of the generic label matchers (its label
    is shared with the manual-only `longitude`/`latitude` siblings, so label
    keyword matching alone cannot distinguish it)."""
    if fld.key.lower() != _LOCATION_WIDGET_KEY:
        return None
    # The real seeded SSOT (v1.4) keys this as identity.current_location;
    # identity.address is kept as a legacy fallback for schema drift.
    for candidate in ("identity.current_location", "identity.address"):
        if ssot.get(candidate) is not MISSING:
            return candidate
    return None


def _key_text_widget_path(fld: Field, ssot: SSOT) -> str | None:
    """The Greenhouse `resume_text`/`cover_letter_text` paste textareas' label
    ("Resume"/"Resume/CV") is shared with the sibling FILE upload field, so
    label keyword matching alone cannot distinguish them: resolve by KEY
    instead, same pattern as `_location_widget_path`. Returns None (MISSING)
    when the SSOT carries neither candidate path -- never fabricated; the
    owner seeds `canned_answers` then re-runs."""
    candidates = _KEY_TEXT_PATHS.get(fld.key.lower())
    if candidates is None:
        return None
    for candidate in candidates:
        if ssot.get(candidate) is not MISSING:
            return candidate
    return None


class _GreenhouseWidgetResolver:
    """Greenhouse portal-widget resolver (moves to providers/greenhouse/resolve.py in Stage 2).

    The `vendor_resolver` (spec 3.4) the kernel classifier consults for
    Greenhouse's location-autocomplete `location` field, the paste-in
    `resume_text`/`cover_letter_text` textareas, and the `longitude`/`latitude`
    portal-telemetry fields. Each method MIRRORS the exact membership test the
    pre-extraction `engine.fieldmap`/`engine.fill` code used: `manual_reason`
    keeps `fld.key.lower()` (the old `_manual_only_reason` portal branch) and
    `hidden_widget` keeps `(fld.key or "").lower()` (the old `_is_hidden_field`)."""
    def location_path(self, fld, ssot): return _location_widget_path(fld, ssot)
    def key_text_path(self, fld, ssot): return _key_text_widget_path(fld, ssot)
    def manual_reason(self, fld):
        return "portal-widget" if fld.key.lower() in _PORTAL_WIDGET_KEYS else ""
    def hidden_widget(self, fld):
        return (fld.key or "").lower() in _PORTAL_WIDGET_KEYS


GREENHOUSE_WIDGET_RESOLVER = _GreenhouseWidgetResolver()


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


def _read_body_text(response) -> str:
    body = response.read()
    return body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
