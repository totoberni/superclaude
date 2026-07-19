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
    _role_for_type,
)
from engine.kernel.resolve import (
    ANSWERABLE,
    MANUAL_ONLY,
    MISSING_STATUS,
    coverage,
)
from engine.profile_map import profile_from_real_ssot
from engine.providers.greenhouse.capture import (
    EDUCATION_TYPEAHEAD_TYPE,
    _greenhouse_role_for_type,
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
    # A questions=true payload exercising all four Greenhouse buckets
    # (questions, location_questions, compliance, demographic_questions) plus an
    # input_hidden tracking sub-field.
    #
    # THE `compliance` BUCKET USES THE REAL LIVE SHAPE (re-derived 2026-07-14 from
    # the boards-api payload of the live anthropic seal posting): a list of BLOCKS,
    # each with a `type`, a `description` and its own `questions` list nested ONE
    # LEVEL DEEPER than the other buckets. This fixture previously fed a FLAT
    # question, a shape greenhouse has NEVER served, and the parser silently
    # dropped the entire section on every real payload while this test stayed
    # green. A description-only block with no questions leads the live payload, so
    # it is modelled here too.
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
            {"type": "eeoc", "description": "PUBLIC BURDEN STATEMENT",
             "questions": []},
            {"type": "eeoc", "description": "Voluntary Self-Identification",
             "questions": [
                 {"label": "DisabilityStatus", "required": False,
                  "fields": [{"name": "disability_status",
                              "type": "multi_value_single_select",
                              "values": [
                                  {"label": "I do not want to answer",
                                   "value": "3"},
                                  {"label": "No", "value": "2"},
                                  {"label": "Yes", "value": "1"}]}]},
             ]},
            {"type": "eeoc", "description": "Voluntary Self-Identification",
             "questions": [
                 {"label": "Gender", "required": True,
                  "fields": [{"name": "gender",
                              "type": "multi_value_single_select",
                              "values": [{"label": "Male", "value": "1"},
                                         {"label": "Female", "value": "2"}]}]},
                 {"label": "Race", "required": False,
                  "fields": [{"name": "race", "type": "multi_value_single_select",
                              "values": [{"label": "Decline", "value": "0"}]}]},
             ]},
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

    # EVERY question nested in a compliance BLOCK becomes a Field. Declining an
    # EEOC question is not the same as being BLIND to it: the parser must SEE all
    # three so the census can account for them as justified skips.
    compliance_keys = {f.key for f in fm.fields if f.source == "compliance"}
    assert compliance_keys == {"disability_status", "gender", "race"}

    for key in ("disability_status", "gender", "race"):
        field = by_key[key]
        assert field.section == Section.COMPLIANCE_EEOC
        assert field.decline_allowed is True
        assert field.norm_type == FieldType.SINGLE_SELECT
        # forced False even for "Gender", whose raw payload says required=True:
        # an EEOC question can never block a fill.
        assert field.required is False

    assert by_key["disability_status"].options == [
        "I do not want to answer", "No", "Yes"]

    demographic = by_key["demographic_1"]
    assert demographic.section == Section.DEMOGRAPHIC
    assert demographic.decline_allowed is True
    assert demographic.required is False  # forced despite raw required=True
    assert demographic.norm_type == FieldType.SINGLE_SELECT


def test_capture_types_a_multi_value_multi_select_as_a_live_true_checkbox_group():
    """THE PHANTOM ROLE. `multi_value_multi_select` must capture as `checkbox`.

    LIVE (read-only fetch of a gitlab language-fluency question, 2026-07-14, whose
    control is REQUIRED): greenhouse renders this type as a `<fieldset
    class="checkbox" aria-required="true">` holding one native `<input
    type="checkbox">` per option. The page carries ZERO `role=listbox` nodes, so
    the kernel map's generic `listbox` is a role NO ELEMENT ON THE PAGE HAS: the
    fill built `get_by_role("listbox", name=<question>)`, resolved nothing, drove
    it anyway via `select_option`, and died in a 30-second timeout that was then
    booked as a `fill-error` on a REQUIRED field (55 such controls live across four
    boards, all required). A role the DOM does not carry is a CAPTURE bug.

    The role is not cosmetic: `checkbox` is a click-hazard role, so `fill()` hands
    the control to a human BEFORE any locator is built (pinned separately by
    `test_fill_hands_off_a_checkbox_group_before_building_any_locator`). Reverting
    this role silently restores the phantom AND the 30-second timeout with it.
    """
    raw = {
        "id": "9003",
        "questions": [
            {"label": "Which of these languages are you fluent in?",
             "required": True,
             "fields": [{"name": "question_langs[]",
                         "type": "multi_value_multi_select",
                         "values": [{"value": 1, "label": "Dutch"},
                                    {"value": 2, "label": "French"}]}]},
            {"label": "Preferred office", "required": True,
             "fields": [{"name": "question_office",
                         "type": "multi_value_single_select",
                         "values": [{"value": 1, "label": "Remote"}]}]},
        ],
        # The SECOND emission site: the demographic block builds its own Locator,
        # and it serves this type too (the live boards carry more of them here than
        # in the questions bucket). A fix applied to only one site leaves the other
        # emitting the phantom.
        "demographic_questions": {"questions": [
            {"id": 7, "label": "Which categories describe you?",
             "type": "multi_value_multi_select",
             "answer_options": [{"id": 1, "label": "Prefer not to disclose"}]},
        ]},
    }
    fm = parse_greenhouse(raw, "acme", "9003", now=lambda: _PINNED)
    by_key = {f.key: f for f in fm.fields}

    assert by_key["question_langs[]"].locator.role == "checkbox"   # questions
    assert by_key["demographic_7"].locator.role == "checkbox"      # demographic

    # The override is GREENHOUSE-LOCAL and stays that way: the shared kernel map is
    # written for the vendors that genuinely serve a listbox (ashby's pronouns
    # field is one, and it has a test to prove it), so correcting greenhouse's DOM
    # claim inside the kernel would break THEM. The locator is a claim about THIS
    # vendor's page; the kernel map is deliberately left alone.
    assert _role_for_type("multi_value_multi_select") == "listbox"

    # ... and the override may not swallow the types the kernel gets RIGHT: every
    # other type still comes from the kernel map, unmodified.
    assert by_key["question_office"].locator.role == "combobox"
    assert _greenhouse_role_for_type("input_text") == "textbox"
    assert _greenhouse_role_for_type("input_file") == "button"


def test_capture_still_reads_a_bare_compliance_question():
    """The block flattener does not lose a compliance entry that carries its
    `fields` DIRECTLY (no `questions` nesting). Greenhouse serves blocks today,
    but a parser that understands exactly one shape is how the section got
    dropped in the first place: neither shape may be silently discarded."""
    raw = {
        "id": "9002",
        "compliance": [
            {"label": "I certify the above is accurate", "required": True,
             "fields": [{"name": "certify", "type": "boolean", "values": []}]},
        ],
    }
    fm = parse_greenhouse(raw, "acme", "9002", now=lambda: _PINNED)
    by_key = {f.key: f for f in fm.fields}

    certify = by_key["certify"]
    assert certify.section == Section.COMPLIANCE_EEOC
    assert certify.decline_allowed is True
    assert certify.required is False  # forced despite raw required=True
    assert certify.norm_type == FieldType.BOOLEAN


def test_capture_emits_education_typeaheads_from_the_toggle():
    """The education section is NOT a questions bucket: Greenhouse serves only a
    top-level `education` toggle and renders School/Degree/Discipline client-side
    as async react-select typeaheads. Capture keys STRUCTURALLY on the toggle to
    emit them (the producer the fill async-typeahead branch consumes); an enabled
    toggle -> the three, each `education_typeahead` + combobox role; hidden or
    absent -> none. This is what kept them BLANK on the 2026-07-18 live run: they
    were never captured, so never driven."""
    enabled = parse_greenhouse(
        {"id": "9004", "education": "education_optional"}, "acme", "9004",
        now=lambda: _PINNED)
    edu = [f for f in enabled.fields if f.type == EDUCATION_TYPEAHEAD_TYPE]
    assert [(f.key, f.label, f.locator.role, f.section, f.required) for f in edu] == [
        ("education_school", "School", "combobox", Section.STANDARD, False),
        ("education_degree", "Degree", "combobox", Section.STANDARD, False),
        ("education_discipline", "Discipline", "combobox", Section.STANDARD, False)]

    # `education_required` -> required=True on the same three.
    required = parse_greenhouse(
        {"id": "9005", "education": "education_required"}, "acme", "9005",
        now=lambda: _PINNED)
    assert all(f.required for f in required.fields
               if f.type == EDUCATION_TYPEAHEAD_TYPE)

    # Hidden / absent toggle -> no education fields (no phantom controls).
    for raw in ({"id": "9006", "education": "education_hidden"},
                {"id": "9007"}):
        fm = parse_greenhouse(raw, "acme", raw["id"], now=lambda: _PINNED)
        assert [f for f in fm.fields if f.type == EDUCATION_TYPEAHEAD_TYPE] == []
