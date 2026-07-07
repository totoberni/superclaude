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
import sys
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
from engine.ssot import SSOT

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

    The default resolver is read as a MODULE attribute (not a bare global) so it
    resolves through the PEP 562 `__getattr__` re-export at the bottom of this
    file -- the symbol now lives in `engine.providers.greenhouse.resolve`. A bare
    `GREENHOUSE_WIDGET_RESOLVER` load would NameError (module `__getattr__` is not
    consulted for a plain global lookup) and would also bypass a
    `monkeypatch.setattr(fieldmap, "GREENHOUSE_WIDGET_RESOLVER", ...)` seam.
    """
    from engine.kernel.resolve import coverage as _kernel_coverage
    if vendor_resolver is None:
        vendor_resolver = getattr(sys.modules[__name__],
                                  "GREENHOUSE_WIDGET_RESOLVER")
    return _kernel_coverage(fieldmap, ssot, profile,
                            vendor_resolver=vendor_resolver)


def _read_body_text(response) -> str:
    body = response.read()
    return body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- greenhouse capture + widget-resolver re-exports (W5.1 Stage 2a dedupe) ----
# The Greenhouse schema capture/parse code MOVED to
# `engine.providers.greenhouse.capture` and the portal-widget coverage resolver
# to `engine.providers.greenhouse.resolve` (single-source: each name is now
# defined ONCE, in the greenhouse package). They are re-exported here via a LAZY
# module `__getattr__` (PEP 562), exactly mirroring `engine.providers.base`, so
# every pre-Stage-2 importer keeps resolving them via `engine.fieldmap`
# unchanged:
#   * `engine.fill`'s load-time `from engine.fieldmap import
#     GREENHOUSE_WIDGET_RESOLVER, capture_greenhouse` (a from-import IS attribute
#     access, so it triggers this __getattr__);
#   * `registry._capture_greenhouse`'s call-time `from engine.fieldmap import
#     capture_greenhouse`;
#   * this module's own `coverage()` default-resolver lookup, done as a MODULE
#     attribute (`getattr(sys.modules[__name__], ...)`) precisely so it routes
#     here -- a bare global would NameError;
#   * the tests' `from engine.fieldmap import capture_greenhouse,
#     parse_greenhouse` and `monkeypatch.setattr(fieldmap, "capture_greenhouse",
#     ...)` (setattr binds a REAL attribute that shadows this __getattr__;
#     teardown restores, after which __getattr__ serves the moved object again).
# The import is DEFERRED to attribute-access time (NEVER at fieldmap load), so it
# cannot cycle with `greenhouse.capture`, which does a load-time `from
# engine.fieldmap import normalize_type, _HIDDEN_TYPES, _read_body_text,
# _utc_now_iso` of the GENERIC helpers that stay defined here.

_GREENHOUSE_CAPTURE_NAMES = frozenset({
    "capture_greenhouse", "parse_greenhouse", "greenhouse_questions_url",
    "_SECTION_FOR_SOURCE", "_fields_from_question", "_fields_from_demographic",
    "_option_labels",
})

_GREENHOUSE_RESOLVE_NAMES = frozenset({
    "GREENHOUSE_WIDGET_RESOLVER", "_GreenhouseWidgetResolver",
    "_location_widget_path", "_key_text_widget_path",
    "_KEY_TEXT_PATHS", "_LOCATION_WIDGET_KEY", "_PORTAL_WIDGET_KEYS",
})


def __getattr__(name):
    if name in _GREENHOUSE_CAPTURE_NAMES:
        import importlib
        return getattr(
            importlib.import_module("engine.providers.greenhouse.capture"), name)
    if name in _GREENHOUSE_RESOLVE_NAMES:
        import importlib
        return getattr(
            importlib.import_module("engine.providers.greenhouse.resolve"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
