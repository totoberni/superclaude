"""Field-map capture + deterministic coverage classification (W4 3.1).

No network: `capture_greenhouse` is driven through a fake opener over the
questions=true fixture, so the autouse no-network guard is satisfied. Coverage
is pure keyword matching against the synthetic v1.4 SSOT; every verdict is
asserted explicitly (no LLM, fully deterministic).
"""

import json

from engine.fieldmap import (
    ANSWERABLE,
    MANUAL_ONLY,
    MISSING_STATUS,
    SCHEMA_VERSION,
    Field,
    FieldMap,
    Locator,
    capture_greenhouse,
    coverage,
    parse_greenhouse,
)
from engine.profile_map import profile_from_real_ssot
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
        "locator", "step_index", "conditional_on"]


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


def test_coverage_required_demographic_is_manual_only():
    # A required EEO/demographic field is manual-only regardless of label match.
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[Field(
                      key="demographic_1", label="Gender",
                      type="multi_value_single_select", required=True,
                      options=["Man", "Woman"], source="demographic",
                      locator=Locator(role="combobox", name="Gender"),
                      step_index=0, conditional_on=None)])
    report = coverage(fm, SSOT({}), {})
    assert report.fields[0].status == MANUAL_ONLY
    assert report.fields[0].reason == "demographic/EEO"


def test_coverage_work_auth_from_profile_capability_when_ssot_string_absent():
    # No raw work_authorization string, but the profile asserts an EU capability.
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[Field(
                      key="question_1",
                      label="Are you legally authorized to work in the EU?",
                      type="multi_value_single_select", required=True,
                      options=["Yes", "No"], source="questions",
                      locator=Locator(role="combobox", name="wa"),
                      step_index=0, conditional_on=None)])
    report = coverage(fm, SSOT({}), {"capabilities": ["work_authorization_eu"]})
    assert report.fields[0].status == ANSWERABLE
    assert report.fields[0].path == "profile.capabilities"

    # With neither SSOT string nor a profile capability it is MISSING.
    bare = coverage(fm, SSOT({}), {})
    assert bare.fields[0].status == MISSING_STATUS


def _field(key, label, *, type_="input_text", source="questions"):
    return Field(key=key, label=label, type=type_, required=True, options=[],
                source=source, locator=Locator(role="textbox", name=label),
                step_index=0, conditional_on=None)


def test_coverage_location_widget_is_answerable_and_lat_long_are_manual_only(
        real_ssot_path):
    # Round-1 live finding: Greenhouse's location-autocomplete widget shares
    # one label across three sub-fields keyed location/longitude/latitude.
    # Only `location` carries real applicant data (the address); the other
    # two are mechanical portal telemetry and must never surface as missing.
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[_field("location", "Location"),
                         _field("longitude", "Location"),
                         _field("latitude", "Location")])
    ssot = SSOT.load(real_ssot_path)
    report = coverage(fm, ssot, {})
    by_key = {f.key: f for f in report.fields}

    assert by_key["location"].status == ANSWERABLE
    assert by_key["location"].path == "identity.address"

    assert by_key["longitude"].status == MANUAL_ONLY
    assert by_key["longitude"].reason == "portal-widget"
    assert by_key["latitude"].status == MANUAL_ONLY
    assert by_key["latitude"].reason == "portal-widget"


def test_coverage_identity_location_patterns_are_answerable(real_ssot_path):
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[
                      _field("q1", "What is your country of residence?"),
                      _field("q2", "Are you currently located in the EU?"),
                      _field("q3", "Where are you currently located?"),
                      _field("q4", "Where are you located?"),
                  ])
    ssot = SSOT.load(real_ssot_path)
    report = coverage(fm, ssot, {})
    for fld in report.fields:
        assert fld.status == ANSWERABLE, fld.label
        assert fld.path == "identity.address"


def test_coverage_skills_experience_pattern_is_answerable(real_ssot_path):
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[
                      _field("q1", "Do you have experience using Kubernetes?"),
                      _field("q2", "What is your experience with Python?"),
                      _field("q3", "How many years of experience in Java do "
                                  "you have?"),
                  ])
    ssot = SSOT.load(real_ssot_path)
    report = coverage(fm, ssot, {})
    for fld in report.fields:
        assert fld.status == ANSWERABLE, fld.label
        assert fld.path == "skills"


def test_coverage_skills_experience_pattern_missing_without_ssot_skills():
    # Answerability requires the SSOT to actually carry a skills bucket to
    # decide from -- an empty SSOT cannot answer, so it stays MISSING.
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[_field("q1", "What is your experience with Rust?")])
    report = coverage(fm, SSOT({}), {})
    assert report.fields[0].status == MISSING_STATUS


def test_coverage_consent_pattern_is_answerable(real_ssot_path):
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[
                      _field("q1", "Please confirm you have read our "
                                  "recruiting policy."),
                      _field("q2", "I have read the Privacy Policy."),
                      _field("q3", "Do you consent to a background check?"),
                      _field("q4", "I agree to the terms above."),
                  ])
    ssot = SSOT.load(real_ssot_path)
    report = coverage(fm, ssot, {})
    for fld in report.fields:
        assert fld.status == ANSWERABLE, fld.label
        assert fld.path == "canned_answers.optional_consents"
