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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from engine.fetch import UA
from engine.ssot import MISSING, SSOT

SCHEMA_VERSION = "2"

# The three classification verdicts a required field can receive.
ANSWERABLE = "answerable"
MISSING_STATUS = "missing"
MANUAL_ONLY = "manual-only"


class FieldType:
    """The unified FieldSchema type vocabulary (W5 angle-5 spec, section 3).

    `Field.norm_type` (schema_version 2+) carries one of these, independent of
    the vendor-native `type` string kept in `Field.type` for backward
    compatibility with fill.py/coverage's existing string matching.
    """

    TEXT = "TEXT"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    URL = "URL"
    NUMBER = "NUMBER"
    DATE = "DATE"
    LONGTEXT = "LONGTEXT"
    SINGLE_SELECT = "SINGLE_SELECT"
    MULTI_SELECT = "MULTI_SELECT"
    BOOLEAN = "BOOLEAN"
    FILE = "FILE"


class Section:
    """The unified FieldSchema section vocabulary (W5 angle-5 spec, section 3).

    `Field.section` (schema_version 2+) classifies where a field came from.
    COMPLIANCE_EEOC/DEMOGRAPHIC/VOLUNTARY fields are never auto-answered
    (R-WT-8 8) and are marked `decline_allowed=True, required=False` at
    capture time.
    """

    STANDARD = "STANDARD"
    CUSTOM = "CUSTOM"
    LOCATION = "LOCATION"
    COMPLIANCE_EEOC = "COMPLIANCE_EEOC"
    DEMOGRAPHIC = "DEMOGRAPHIC"
    VOLUNTARY = "VOLUNTARY"


# Sections that are always declinable and never block a fill/coverage run.
_DECLINE_SECTIONS = frozenset({
    Section.COMPLIANCE_EEOC, Section.DEMOGRAPHIC, Section.VOLUNTARY,
})

# parse_greenhouse's `source` tag (the bucket a question arrived in) -> the
# canonical Section. Unrecognised sources fall back to STANDARD.
_SECTION_FOR_SOURCE = {
    "questions": Section.STANDARD,
    "location_questions": Section.LOCATION,
    "compliance": Section.COMPLIANCE_EEOC,
    "demographic": Section.DEMOGRAPHIC,
}

# Greenhouse field `type` string -> ARIA role for the a11y locator hint. The
# HTTP questions endpoint carries no DOM, so the locator is a best-effort role
# name that the (later) browser layer can reuse; the label is the accessible
# name. Unknown types fall back to a text box.
_ROLE_FOR_TYPE = {
    "input_text": "textbox",
    "input_file": "button",
    "textarea": "textbox",
    "multi_value_single_select": "combobox",
    "multi_value_multi_select": "listbox",
    "boolean": "checkbox",
    "yes_no": "combobox",
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


# Label keywords that mark a field as EEO/demographic no matter which section it
# arrived in (defence in depth on top of the source tag).
_DEMOGRAPHIC_KEYWORDS = (
    "gender", "race", "ethnic", "veteran", "disability", "disabilities",
    "sexual orientation", "hispanic", "latino", "self-identification",
    "self identification",
)

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

# A required "how much X experience do you have" question is answerable
# in principle: the SSOT's skills bucket can decide yes/no for any named
# technology, even if the honest answer is "no" (answerability is about
# whether the SSOT can decide, not about the polarity of the answer).
_SKILLS_EXPERIENCE_RE = re.compile(r"experience\s+(?:using|with|in)\b",
                                  re.IGNORECASE)

# First/last name label keywords, shared with `engine.fill` (imported there) so
# the full-name-split fallback at render time detects the same fields these
# matchers do.
_FIRST_NAME_KEYWORDS = ("first name", "given name", "forename")
_LAST_NAME_KEYWORDS = ("last name", "surname", "family name")

# Ordered label-keyword -> candidate SSOT dotted paths. First matcher whose any
# keyword is a substring of the (lowercased) label wins; within it the first
# candidate path that resolves in the SSOT makes the field answerable. The
# discrete first_name/last_name key leads each list so a form with BOTH a First
# Name and a Last Name field never has the full name typed into both; the
# full-name paths remain as a fallback (split at render time in engine.fill)
# for an SSOT that only carries a combined name. Order is load-bearing: the
# country-of-residence matcher MUST precede the generic current-location
# matcher (below) so a "country of residence" question resolves the discrete
# `identity.country` rather than the full postal address, which matches no
# country-name option.
_ANSWER_MATCHERS: list[tuple[tuple[str, ...], list[str]]] = [
    (_FIRST_NAME_KEYWORDS,
     ["identity.first_name", "identity.name", "identity.full_name"]),
    (_LAST_NAME_KEYWORDS,
     ["identity.last_name", "identity.name", "identity.full_name"]),
    (("full name", "legal name", "your name"),
     ["identity.name", "identity.full_name"]),
    (("email", "e-mail"), ["identity.email"]),
    (("phone", "mobile number", "telephone"),
     ["identity.phone", "canned_answers.phone"]),
    (("linkedin",), ["links.linkedin", "canned_answers.linkedin"]),
    (("github",), ["links.github"]),
    (("portfolio", "personal website", "personal site", "web site", "website"),
     ["links.site", "links.website", "links.portfolio"]),
    (("notice period", "notice",),
     ["canned_answers.notice_period"]),
    (("employment agreement", "post-employment", "post employment",
      "non-compete", "noncompete", "restrictive covenant"),
     ["canned_answers.post_employment_restrictions"]),
    (("previously worked", "previously consulted", "worked at or consulted",
      "previously employed at", "previously interned"),
     ["canned_answers.previously_worked_at_company",
      "canned_answers.previously_applied_default"]),
    (("sponsorship", "sponsor", "visa"),
     ["canned_answers.sponsorship_answer_by_region",
      "canned_answers.visa_sponsorship_required",
      "canned_answers.us_visa_sponsorship_required"]),
    (("authorized to work", "authorised to work", "right to work",
      "eligible to work", "work authorization", "work authorisation",
      "legally authorized", "legally authorised", "work permit"),
     ["work_authorization", "canned_answers.work_authorization"]),
    (("relocat",),
     ["canned_answers.relocation", "canned_answers.willing_to_relocate"]),
    (("salary", "compensation expectation", "expected", "desired compensation"),
     ["preferences.comp_floor", "canned_answers.salary_expectation"]),
    (("country of residence", "current country", "country you reside",
      "country you are located"),
     ["identity.country"]),
    (("currently located in",
      "where are you currently located", "where are you located",
      "current location", "location"),
     ["identity.current_location", "identity.address", "identity.country"]),
    (("please confirm", "privacy policy", "consent to", "i agree"),
     ["canned_answers.optional_consents"]),
    (("accommodation", "accommodations", "accessible and inclusive",
      "reasonable adjustment", "accessibility need"),
     ["canned_answers.accommodations"]),
    (("name",), ["identity.name", "identity.full_name"]),
]


@dataclass
class Locator:
    role: str
    name: str


@dataclass
class Field:
    key: str
    label: str
    type: str
    required: bool
    options: list[str]
    source: str
    locator: Locator
    step_index: int | None = None
    conditional_on: dict | None = None
    # -- W5 additive extension (schema_version 2): every new field defaults so
    # every existing construction site (browse.py, tests, fixtures) keeps
    # working unchanged, and every v1-shaped cached FieldMap deserializes via
    # these same defaults (see `from_dict`).
    decline_allowed: bool = False
    max_length: int | None = None
    accept_types: list[str] | None = None
    norm_type: str = ""
    section: str = "STANDARD"

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "required": self.required,
            "options": list(self.options),
            "source": self.source,
            "locator": {"role": self.locator.role, "name": self.locator.name},
            "step_index": self.step_index,
            "conditional_on": self.conditional_on,
            "decline_allowed": self.decline_allowed,
            "max_length": self.max_length,
            "accept_types": (list(self.accept_types)
                            if self.accept_types is not None else None),
            "norm_type": self.norm_type,
            "section": self.section,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Field":
        """Reconstruct a `Field` from a dict of either shape.

        Tolerant by construction (`.get(key, <dataclass default>)` for every
        W5 field): a schema_version-1 cached row that never carried
        decline_allowed/max_length/accept_types/norm_type/section
        deserializes cleanly via these defaults, no store-side migration or
        version branch needed.
        """
        locator = data.get("locator") or {}
        accept_types = data.get("accept_types")
        raw_step = data.get("step_index")
        return cls(
            key=data["key"],
            label=data["label"],
            type=data["type"],
            required=bool(data["required"]),
            options=list(data.get("options") or []),
            source=data["source"],
            locator=Locator(role=locator.get("role", ""),
                            name=locator.get("name", "")),
            step_index=int(raw_step) if raw_step is not None else None,
            conditional_on=data.get("conditional_on"),
            decline_allowed=bool(data.get("decline_allowed", False)),
            max_length=data.get("max_length"),
            accept_types=(list(accept_types) if accept_types is not None
                         else None),
            norm_type=data.get("norm_type", ""),
            section=data.get("section", "STANDARD"),
        )


@dataclass
class FieldMap:
    vendor: str
    posting_id: str
    captured_at: str
    fields: list[Field] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "vendor": self.vendor,
            "posting_id": self.posting_id,
            "schema_version": self.schema_version,
            "captured_at": self.captured_at,
            "fields": [f.to_dict() for f in self.fields],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FieldMap":
        return cls(
            vendor=data["vendor"],
            posting_id=str(data["posting_id"]),
            captured_at=data["captured_at"],
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            fields=[Field.from_dict(f) for f in data.get("fields", [])],
        )

    def required_fields(self) -> list[Field]:
        return [f for f in self.fields if f.required]

    def coverage(self, ssot: SSOT, profile: dict) -> "CoverageReport":
        return coverage(self, ssot, profile)


@dataclass
class FieldCoverage:
    key: str
    label: str
    status: str          # answerable | missing | manual-only
    path: str            # answerable: resolving path; missing: dotted-path guess
    reason: str = ""     # manual-only: why (file-upload | demographic/EEO)

    def classification(self) -> str:
        """The compact verdict string (`missing:` carries the guessed path)."""
        if self.status == MISSING_STATUS:
            return f"{MISSING_STATUS}:{self.path}"
        return self.status


@dataclass
class CoverageReport:
    vendor: str
    posting_id: str
    fields: list[FieldCoverage]

    @property
    def answerable(self) -> int:
        return sum(1 for f in self.fields if f.status == ANSWERABLE)

    @property
    def missing(self) -> int:
        return sum(1 for f in self.fields if f.status == MISSING_STATUS)

    @property
    def manual_only(self) -> int:
        return sum(1 for f in self.fields if f.status == MANUAL_ONLY)

    @property
    def required_total(self) -> int:
        return len(self.fields)

    def missing_paths(self) -> list[str]:
        """Dotted-path guesses for every unanswerable required field (feeds 7.6)."""
        return [f.path for f in self.fields if f.status == MISSING_STATUS]

    def summary_line(self) -> str:
        return (f"{self.answerable} answerable, {self.missing} missing, "
                f"{self.manual_only} manual-only of {self.required_total} required")


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


def coverage(fieldmap: FieldMap, ssot: SSOT, profile: dict) -> CoverageReport:
    """Classify every REQUIRED field of `fieldmap` against the SSOT + profile.

    Deterministic, no LLM. Order per field: manual-only (file upload or
    EEO/demographic, never auto-answered) wins first; then a keyword match
    against the SSOT buckets makes it answerable; otherwise it is missing and
    gets a dotted-path guess (canned_answers.<slug> for an unrecognised
    question) that a questionnaire item can later resolve.
    """
    profile = profile or {}
    results: list[FieldCoverage] = []
    for fld in fieldmap.required_fields():
        results.append(_classify_field(fld, ssot, profile))
    return CoverageReport(vendor=fieldmap.vendor,
                          posting_id=fieldmap.posting_id, fields=results)


def _classify_field(fld: Field, ssot: SSOT, profile: dict) -> FieldCoverage:
    reason = _manual_only_reason(fld)
    if reason:
        return FieldCoverage(fld.key, fld.label, MANUAL_ONLY, "", reason)
    path = _answerable_path(fld, ssot, profile)
    if path is not None:
        return FieldCoverage(fld.key, fld.label, ANSWERABLE, path)
    return FieldCoverage(fld.key, fld.label, MISSING_STATUS,
                         _missing_path_guess(fld.label))


def _manual_only_reason(fld: Field) -> str:
    """"file-upload" ONLY for a genuine file control: a native file type, or a
    label carrying an explicit upload/attach verb (mirrors `engine.fill`'s
    `_is_upload_field`). A bare "resume"/"cv" label keyword is NOT enough --
    Greenhouse's paste-in `resume_text`/`cover_letter_text` textareas share
    their label with the sibling file-upload field ("Resume"/"Resume/CV"),
    so tagging on the label alone would wrongly classify a fillable free-text
    field as manual-only file-upload (never resolved, never fillable)."""
    if "file" in fld.type.lower():
        return "file-upload"
    label = fld.label.lower()
    if any(word in label for word in ("upload", "attach")):
        return "file-upload"
    if fld.source in ("demographic", "eeo", "eeoc", "compliance"):
        return "demographic/EEO"
    if any(word in label for word in _DEMOGRAPHIC_KEYWORDS):
        return "demographic/EEO"
    if fld.key.lower() in _PORTAL_WIDGET_KEYS:
        return "portal-widget"
    return ""


def _answerable_path(fld: Field, ssot: SSOT, profile: dict) -> str | None:
    location_path = _location_widget_path(fld, ssot)
    if location_path is not None:
        return location_path
    key_text_path = _key_text_widget_path(fld, ssot)
    if key_text_path is not None:
        return key_text_path
    low = fld.label.lower()
    if _SKILLS_EXPERIENCE_RE.search(low) and ssot.get("skills") is not MISSING:
        return "skills"
    for keywords, candidates in _ANSWER_MATCHERS:
        if not any(keyword in low for keyword in keywords):
            continue
        for path in candidates:
            if ssot.get(path) is not MISSING:
                return path
        if _profile_answers_work_auth(candidates, profile):
            return "profile.capabilities"
        return None
    return None


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


def _profile_answers_work_auth(candidates: list[str], profile: dict) -> bool:
    """Work-authorization questions may be answered from a profile capability
    (e.g. work_authorization_eu) even when the raw SSOT string is absent."""
    if "work_authorization" not in candidates:
        return False
    caps = profile.get("capabilities") or []
    return any(str(cap).startswith("work_authorization") for cap in caps)


def _missing_path_guess(label: str) -> str:
    """A free-form unrecognised question is answered from canned_answers (7.6)."""
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    slug = slug or "unlabelled"
    return f"canned_answers.{slug}"


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


def _role_for_type(field_type: str) -> str:
    return _ROLE_FOR_TYPE.get(field_type, "textbox")


def _read_body_text(response) -> str:
    body = response.read()
    return body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
