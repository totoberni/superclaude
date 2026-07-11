"""Greenhouse schema capture + parse (W5.1 Stage 5; moved from tests/test_fieldmap.py).

No network: `capture_greenhouse` is driven through a fake opener over the
questions=true fixture, so the autouse no-network guard is satisfied. The
`test_coverage_classifies_*` case is capture-driven end to end (capture ->
coverage), so it rides here with the capture cluster; coverage is pure keyword
matching against the synthetic SSOT (no LLM, every verdict asserted explicitly).
"""

import json

from engine.kernel.contracts import (
    SCHEMA_VERSION,
    FieldMap,
    FieldType,
    Locator,
    Section,
)
from engine.kernel.resolve import (
    ANSWERABLE,
    MANUAL_ONLY,
    MISSING_STATUS,
    coverage,
)
from engine.profile_map import profile_from_real_ssot
from engine.providers.greenhouse.capture import (
    capture_greenhouse,
    parse_greenhouse,
)
from engine.ssot import SSOT

_PINNED = "2026-07-03T00:00:00+00:00"


class _Resp:
    def __init__(self, body):
        self._body = body
        self.headers = {}

    def read(self):
        return self._body


class _CaptureOpener:
    def __init__(self, raw):
        self._body = json.dumps(raw).encode("utf-8")
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        return _Resp(self._body)


def _capture(greenhouse_questions_raw):
    opener = _CaptureOpener(greenhouse_questions_raw)
    fm = capture_greenhouse("acme", "5501001", opener, now=lambda: _PINNED)
    return fm, opener


def test_capture_hits_questions_endpoint_with_honest_ua(greenhouse_questions_raw):
    _fm, opener = _capture(greenhouse_questions_raw)
    assert len(opener.requests) == 1
    req = opener.requests[0]
    assert req.full_url == (
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs/"
        "5501001?questions=true")
    assert "abe-automations" in req.get_header("User-agent")


def test_capture_parses_questions_into_canonical_fields(greenhouse_questions_raw):
    fm, _ = _capture(greenhouse_questions_raw)
    assert fm.vendor == "greenhouse"
    assert fm.posting_id == "5501001"
    assert fm.schema_version == SCHEMA_VERSION
    assert fm.captured_at == _PINNED

    by_key = {f.key: f for f in fm.fields}
    # a standard text field
    first = by_key["first_name"]
    assert first.label == "First Name"
    assert first.type == "input_text"
    assert first.required is True
    assert first.source == "questions"
    assert first.locator == Locator(role="textbox", name="First Name")
    assert first.step_index == 0
    assert first.conditional_on is None

    # a custom select carries its enumerated option labels
    workauth = by_key["question_40002"]
    assert workauth.required is True
    assert workauth.type == "multi_value_single_select"
    assert workauth.options == ["Yes", "No"]
    assert workauth.locator.role == "combobox"

    # a file upload keeps its input_file type
    assert by_key["resume"].type == "input_file"


def test_capture_keeps_demographics_separate_and_tagged(greenhouse_questions_raw):
    fm, _ = _capture(greenhouse_questions_raw)
    by_key = {f.key: f for f in fm.fields}
    gender = by_key["demographic_90001"]
    assert gender.label == "Gender"
    assert gender.source == "demographic"
    assert gender.required is False
    assert gender.options == ["Man", "Woman", "Prefer not to disclose"]


def test_fieldmap_json_roundtrips(greenhouse_questions_raw):
    fm, _ = _capture(greenhouse_questions_raw)
    blob = json.dumps(fm.to_dict())
    restored = FieldMap.from_dict(json.loads(blob))
    assert restored.to_dict() == fm.to_dict()
    # canonical top-level key order is preserved verbatim (R-WT-8 C)
    assert list(fm.to_dict().keys()) == [
        "vendor", "posting_id", "schema_version", "captured_at", "fields"]
    assert list(fm.to_dict()["fields"][0].keys()) == [
        "key", "label", "type", "required", "options", "source",
        "locator", "step_index", "conditional_on", "decline_allowed",
        "max_length", "accept_types", "norm_type", "section"]


def test_parse_is_pure_and_matches_capture(greenhouse_questions_raw):
    parsed = parse_greenhouse(greenhouse_questions_raw, "acme", "5501001",
                              now=lambda: _PINNED)
    captured, _ = _capture(greenhouse_questions_raw)
    assert parsed.to_dict() == captured.to_dict()


def test_coverage_classifies_answerable_missing_and_manual_only(
        greenhouse_questions_raw, real_ssot_path):
    fm, _ = _capture(greenhouse_questions_raw)
    ssot = SSOT.load(real_ssot_path)
    report = coverage(fm, ssot, profile_from_real_ssot(ssot))

    by_key = {f.key: f for f in report.fields}
    # only required fields are classified
    assert set(by_key) == {"first_name", "last_name", "email", "resume",
                           "question_40002", "question_40003", "question_40004"}

    assert by_key["first_name"].status == ANSWERABLE
    assert by_key["first_name"].path == "identity.name"
    assert by_key["email"].status == ANSWERABLE
    assert by_key["email"].path == "identity.email"
    assert by_key["question_40002"].status == ANSWERABLE
    assert by_key["question_40002"].path == "work_authorization"
    assert by_key["question_40003"].status == ANSWERABLE
    assert by_key["question_40003"].path == "canned_answers.visa_sponsorship_required"

    # a file upload is manual-only, never auto-answered (R-WT-8 8)
    assert by_key["resume"].status == MANUAL_ONLY
    assert by_key["resume"].reason == "file-upload"

    # an unanswerable required field is MISSING with a canned_answers guess
    unanswerable = by_key["question_40004"]
    assert unanswerable.status == MISSING_STATUS
    assert unanswerable.path.startswith("canned_answers.")
    assert unanswerable.classification() == f"missing:{unanswerable.path}"

    assert report.answerable == 5
    assert report.missing == 1
    assert report.manual_only == 1
    assert report.required_total == 7
    assert report.summary_line() == (
        "5 answerable, 1 missing, 1 manual-only of 7 required")
    assert report.missing_paths() == [unanswerable.path]


def test_capture_populates_section_and_decline_allowed_additively():
    # A synthetic questions=true payload exercising all four Greenhouse
    # buckets (questions, location_questions, compliance,
    # demographic_questions) plus an input_hidden tracking sub-field.
    raw = {
        "id": "9001",
        "questions": [
            {"label": "First Name", "required": True,
             "fields": [{"name": "first_name", "type": "input_text",
                        "values": []}]},
            {"label": "Tracking Pixel", "required": True,
             "fields": [{"name": "utm_source", "type": "input_hidden",
                        "values": []}]},
        ],
        "location_questions": [
            {"label": "Country", "required": False,
             "fields": [{"name": "country", "type": "input_text",
                        "values": []}]},
        ],
        "compliance": [
            {"label": "I certify the above is accurate", "required": True,
             "fields": [{"name": "certify", "type": "boolean",
                        "values": []}]},
        ],
        "demographic_questions": {
            "questions": [
                {"id": 1, "label": "Gender", "required": True,
                 "type": "multi_value_single_select",
                 "answer_options": [{"id": 1, "label": "Man"}]},
            ],
        },
    }
    fm = parse_greenhouse(raw, "acme", "9001", now=lambda: _PINNED)
    by_key = {f.key: f for f in fm.fields}

    # input_hidden sub-fields never become a Field at all (not a user field).
    assert "utm_source" not in by_key

    standard = by_key["first_name"]
    assert standard.section == Section.STANDARD
    assert standard.decline_allowed is False
    assert standard.required is True
    assert standard.norm_type == FieldType.TEXT

    location = by_key["country"]
    assert location.section == Section.LOCATION
    assert location.decline_allowed is False

    compliance = by_key["certify"]
    assert compliance.section == Section.COMPLIANCE_EEOC
    assert compliance.decline_allowed is True
    assert compliance.required is False  # forced despite raw required=True
    assert compliance.norm_type == FieldType.BOOLEAN

    demographic = by_key["demographic_1"]
    assert demographic.section == Section.DEMOGRAPHIC
    assert demographic.decline_allowed is True
    assert demographic.required is False  # forced despite raw required=True
    assert demographic.norm_type == FieldType.SINGLE_SELECT
