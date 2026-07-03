"""ATS field-map capture + deterministic coverage classification (W4 3.1).

Reaches the FULL application schema of a posting (labels, types, required flags,
option enumerations) so that every automatable queue item can carry a concrete
field map and the SSOT playtest loop can judge coverage field by field.

Greenhouse is the browserless vendor: the sanctioned schema source is
`boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}?questions=true`, one polite
GET per posting (Lever/Ashby need a browser and land in browse.py, a later wave).
The captured shape is the R-WT-8 C canonical field map, verbatim:

    {vendor, posting_id, schema_version, captured_at,
     fields: [{key, label, type, required, options, source,
               locator: {role, name}, step_index, conditional_on}]}

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

SCHEMA_VERSION = "1"

# The three classification verdicts a required field can receive.
ANSWERABLE = "answerable"
MISSING_STATUS = "missing"
MANUAL_ONLY = "manual-only"

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

# A required "how much X experience do you have" question is answerable
# in principle: the SSOT's skills bucket can decide yes/no for any named
# technology, even if the honest answer is "no" (answerability is about
# whether the SSOT can decide, not about the polarity of the answer).
_SKILLS_EXPERIENCE_RE = re.compile(r"experience\s+(?:using|with|in)\b",
                                  re.IGNORECASE)

# Ordered label-keyword -> candidate SSOT dotted paths. First matcher whose any
# keyword is a substring of the (lowercased) label wins; within it the first
# candidate path that resolves in the SSOT makes the field answerable. Specific
# name variants precede the generic "name" so "First Name" never resolves via
# the bare fallback first. Order is load-bearing.
_ANSWER_MATCHERS: list[tuple[tuple[str, ...], list[str]]] = [
    (("first name", "given name", "forename"),
     ["identity.name", "identity.full_name", "identity.first_name"]),
    (("last name", "surname", "family name"),
     ["identity.name", "identity.full_name", "identity.last_name"]),
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
    (("sponsorship", "sponsor", "visa"),
     ["canned_answers.visa_sponsorship_required", "work_authorization"]),
    (("authorized to work", "authorised to work", "right to work",
      "eligible to work", "work authorization", "work authorisation",
      "legally authorized", "legally authorised", "work permit"),
     ["work_authorization", "canned_answers.work_authorization"]),
    (("relocat",),
     ["canned_answers.relocation", "canned_answers.willing_to_relocate"]),
    (("salary", "compensation expectation", "expected", "desired compensation"),
     ["preferences.comp_floor", "canned_answers.salary_expectation"]),
    (("country of residence", "currently located in",
      "where are you currently located", "where are you located"),
     ["identity.address", "identity.country"]),
    (("please confirm", "privacy policy", "consent to", "i agree"),
     ["canned_answers.optional_consents"]),
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
    step_index: int
    conditional_on: object | None

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
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Field":
        locator = data.get("locator") or {}
        return cls(
            key=data["key"],
            label=data["label"],
            type=data["type"],
            required=bool(data["required"]),
            options=list(data.get("options") or []),
            source=data["source"],
            locator=Locator(role=locator.get("role", ""),
                            name=locator.get("name", "")),
            step_index=int(data.get("step_index", 0)),
            conditional_on=data.get("conditional_on"),
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
    if "file" in fld.type.lower():
        return "file-upload"
    label = fld.label.lower()
    if any(word in label for word in ("upload", "attach", "resume", "cv")):
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
    if ssot.get("identity.address") is not MISSING:
        return "identity.address"
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
    out: list[Field] = []
    for sub in question.get("fields") or []:
        field_type = sub.get("type", "input_text")
        out.append(Field(
            key=sub.get("name", ""),
            label=label,
            type=field_type,
            required=required,
            options=_option_labels(sub.get("values")),
            source=source,
            locator=Locator(role=_role_for_type(field_type), name=label),
            step_index=0,
            conditional_on=None,
        ))
    return out


def _fields_from_demographic(block) -> list[Field]:
    """The demographic block is a separate object with its own question shape
    (`answer_options`, type on the question). Captured but always manual-only."""
    if not isinstance(block, dict):
        return []
    out: list[Field] = []
    for question in block.get("questions") or []:
        field_type = question.get("type", "multi_value_single_select")
        out.append(Field(
            key=f"demographic_{question.get('id', '')}",
            label=question.get("label", ""),
            type=field_type,
            required=bool(question.get("required", False)),
            options=_option_labels(question.get("answer_options")),
            source="demographic",
            locator=Locator(role=_role_for_type(field_type),
                            name=question.get("label", "")),
            step_index=0,
            conditional_on=None,
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
