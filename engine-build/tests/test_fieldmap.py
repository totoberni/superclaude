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
    FieldType,
    Locator,
    Section,
    capture_greenhouse,
    coverage,
    normalize_type,
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


def test_first_and_last_name_prefer_discrete_ssot_keys_over_full_name():
    # FIX 1: when the SSOT carries discrete identity.first_name/
    # identity.last_name keys, the matcher resolves to THOSE, not the combined
    # identity.name (previously both fields resolved via identity.name first,
    # so the full name got typed into both fields).
    ssot = SSOT({"identity": {
        "name": "Ada Lovelace", "first_name": "Ada", "last_name": "Lovelace"}})
    fm = FieldMap(vendor="acme", posting_id="1", captured_at=_PINNED, fields=[
        Field(key="first_name", label="First Name", type="input_text",
              required=True, options=[], source="questions",
              locator=Locator(role="textbox", name="First Name")),
        Field(key="last_name", label="Last Name", type="input_text",
              required=True, options=[], source="questions",
              locator=Locator(role="textbox", name="Last Name")),
    ])
    report = coverage(fm, ssot, {})
    by_key = {f.key: f for f in report.fields}

    assert by_key["first_name"].path == "identity.first_name"
    assert by_key["last_name"].path == "identity.last_name"


def test_first_and_last_name_fall_back_to_full_name_path_when_no_discrete_key():
    # No discrete first_name/last_name key at all: both fall back to the
    # combined identity.name path (split at render time in engine.fill).
    ssot = SSOT({"identity": {"name": "Ada Lovelace"}})
    fm = FieldMap(vendor="acme", posting_id="1", captured_at=_PINNED, fields=[
        Field(key="first_name", label="First Name", type="input_text",
              required=True, options=[], source="questions",
              locator=Locator(role="textbox", name="First Name")),
        Field(key="last_name", label="Last Name", type="input_text",
              required=True, options=[], source="questions",
              locator=Locator(role="textbox", name="Last Name")),
    ])
    report = coverage(fm, ssot, {})
    by_key = {f.key: f for f in report.fields}

    assert by_key["first_name"].path == "identity.name"
    assert by_key["last_name"].path == "identity.name"


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


def test_resume_file_field_is_still_manual_only_file_upload():
    # Unchanged: the sibling FILE control (not the textarea) stays manual-only
    # file-upload after narrowing `_manual_only_reason`.
    fld = _field("resume", "Resume/CV", type_="input_file")
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[fld])
    report = coverage(fm, SSOT({}), {})
    assert report.fields[0].status == MANUAL_ONLY
    assert report.fields[0].reason == "file-upload"


def test_coverage_identity_location_patterns_are_answerable(real_ssot_path):
    # NOTE: "country of residence" is deliberately NOT in this list -- gap #4
    # split it out to its own dedicated `identity.country` matcher (below),
    # never the full-address `identity.current_location`.
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[
                      _field("q2", "Are you currently located in the EU?"),
                      _field("q3", "Where are you currently located?"),
                      _field("q4", "Where are you located?"),
                      # round-3 live finding: a bare "Current location" /
                      # "Location" label (not the keyed Greenhouse widget)
                      # must classify via the same identity dotted path
                      _field("q5", "Current location"),
                      _field("q6", "Location"),
                  ])
    ssot = SSOT.load(real_ssot_path)
    report = coverage(fm, ssot, {})
    for fld in report.fields:
        assert fld.status == ANSWERABLE, fld.label
        assert fld.path == "identity.current_location"


# -- Gap #4: country-of-residence matcher resolves the discrete identity.country
# path, never the full-address identity.current_location (which matches no
# country-name option). Fake SSOT only (no PII); real_ssot_v14.yaml carries no
# identity.country key so this is exercised against a hand-built SSOT.

def test_country_of_residence_resolves_to_identity_country():
    ssot = SSOT({"identity": {
        "country": "Italy",
        "current_location": "12 Example Street, Testville, Italy"}})
    for label in ("What is your country of residence?",
                  "What is your current country?",
                  "Please state the country you reside in currently.",
                  "Please state the country you are located in."):
        fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                      fields=[_field("q_country", label)])
        report = coverage(fm, ssot, {})
        assert report.fields[0].status == ANSWERABLE, label
        assert report.fields[0].path == "identity.country", label


def test_country_of_residence_never_resolves_the_full_address():
    # Even though identity.current_location also resolves in the SSOT (and
    # would have won under the pre-fix matcher order), the country-of-
    # residence question must resolve identity.country, never the address.
    ssot = SSOT({"identity": {
        "country": "Italy",
        "current_location": "12 Example Street, Testville, Italy"}})
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[_field("q_country",
                                "What is your country of residence?")])
    report = coverage(fm, ssot, {})
    assert report.fields[0].path != "identity.current_location"
    assert report.fields[0].path == "identity.country"


def test_country_of_residence_missing_when_ssot_lacks_identity_country():
    # identity.current_location alone is NOT a fallback for this matcher: a
    # posting that only carries the address never gets typed into a country
    # dropdown (never fabricated).
    ssot = SSOT({"identity": {
        "current_location": "12 Example Street, Testville, Italy"}})
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[_field("q_country",
                                "What is your country of residence?")])
    report = coverage(fm, ssot, {})
    assert report.fields[0].status == MISSING_STATUS


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


# -- Accommodations/accessibility matcher: a Greenhouse optional text question
# ("It is important to us to create an accessible and inclusive... please let
# us know if you require any accommodations...") previously had no matcher and
# fell through to `_missing_path_guess`'s long slug. -----------------------

def test_accommodations_matcher_resolves_to_canned_answers():
    ssot = SSOT({"canned_answers": {
        "accommodations": "No accommodations required."}})
    for label in (
            "It is important to us to create an accessible and inclusive "
            "environment. Please let us know if you require any "
            "accommodations during our interview process.",
            "Do you require any accommodations for your interview?",
            "Please describe any reasonable adjustment you may need.",
            "Let us know if you have an accessibility need."):
        fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                      fields=[_field("q_accommodation", label)])
        report = coverage(fm, ssot, {})
        assert report.fields[0].status == ANSWERABLE, label
        assert report.fields[0].path == "canned_answers.accommodations", label


def test_accommodations_missing_without_ssot_answer():
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[_field(
                      "q_accommodation",
                      "Please let us know if you require any "
                      "accommodations.")])
    report = coverage(fm, SSOT({}), {})
    assert report.fields[0].status == MISSING_STATUS


def test_accommodations_matcher_does_not_shadow_unrelated_consent_question(
        real_ssot_path):
    # The new matcher must not intercept a question intended for an existing
    # matcher earlier in `_ANSWER_MATCHERS` (here: the consent pattern).
    ssot = SSOT.load(real_ssot_path)
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[
                      _field("q_consent",
                            "Please confirm you have read our recruiting "
                            "policy."),
                      _field("q_accommodation",
                            "Please let us know if you require any "
                            "accommodations."),
                  ])
    report = coverage(fm, ssot, {})
    by_key = {f.key: f for f in report.fields}
    assert by_key["q_consent"].path == "canned_answers.optional_consents"
    # accommodations is MISSING here (real_ssot fixture carries no
    # canned_answers.accommodations key) but must not resolve to the
    # unrelated consent path either.
    assert by_key["q_accommodation"].path != "canned_answers.optional_consents"


# -- Gap #1: employment-agreement / post-employment-restriction matcher -----

def test_employment_restrictions_matcher_resolves_to_canned_answers():
    ssot = SSOT({"canned_answers": {
        "post_employment_restrictions": "No. I have no non-compete."}})
    for label in (
            "Are you subject to any employment agreements and/or "
            "post-employment restrictions that would affect your ability "
            "to work for us?",
            "Do you have a non-compete clause?",
            "Are you bound by any restrictive covenant?"):
        fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                      fields=[_field("q_restrict", label)])
        report = coverage(fm, ssot, {})
        assert report.fields[0].status == ANSWERABLE, label
        assert (report.fields[0].path ==
               "canned_answers.post_employment_restrictions"), label


def test_employment_restrictions_missing_without_ssot_answer():
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[_field(
                      "q_restrict",
                      "Are you subject to any post-employment restrictions?")])
    report = coverage(fm, SSOT({}), {})
    assert report.fields[0].status == MISSING_STATUS


# -- Gap #2: previously-worked-at/consulted-for matcher ----------------------

def test_previously_worked_matcher_resolves_to_canned_answers():
    ssot = SSOT({"canned_answers": {
        "previously_worked_at_company": "No. I have never worked here."}})
    for label in ("Have you previously worked at or consulted for Acme?",
                  "Have you previously worked at this company?",
                  "Were you previously employed at Acme?"):
        fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                      fields=[_field("q_prev", label)])
        report = coverage(fm, ssot, {})
        assert report.fields[0].status == ANSWERABLE, label
        assert (report.fields[0].path ==
               "canned_answers.previously_worked_at_company"), label


def test_previously_worked_falls_back_to_previously_applied_default():
    ssot = SSOT({"canned_answers": {
        "previously_applied_default": "No, this is my first application."}})
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[_field(
                      "q_prev",
                      "Have you previously worked at or consulted for Acme?")])
    report = coverage(fm, ssot, {})
    assert report.fields[0].status == ANSWERABLE
    assert report.fields[0].path == "canned_answers.previously_applied_default"


# -- Gap #3: sponsorship matcher prefers the region-keyed dict over the -----
# legacy scalar, and still falls back to the legacy scalar for an SSOT that
# only carries it (backward compat with the pre-migration schema).

def test_sponsorship_matcher_prefers_region_dict_over_legacy_scalar():
    ssot = SSOT({"canned_answers": {
        "sponsorship_answer_by_region": {"eu": "No, I have EU work rights."},
        "visa_sponsorship_required": "no"}})
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[_field(
                      "q_visa",
                      "Will you now or in the future require sponsorship "
                      "for a visa to remain in your current location?")])
    report = coverage(fm, ssot, {})
    assert report.fields[0].status == ANSWERABLE
    assert report.fields[0].path == "canned_answers.sponsorship_answer_by_region"


def test_sponsorship_matcher_falls_back_to_legacy_scalar_key():
    # Backward compat: an SSOT that only carries the pre-migration scalar key
    # (no sponsorship_answer_by_region dict) still resolves answerable.
    ssot = SSOT({"canned_answers": {"visa_sponsorship_required": "no"}})
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[_field(
                      "q_visa",
                      "Will you now or in the future require visa "
                      "sponsorship for employment?")])
    report = coverage(fm, ssot, {})
    assert report.fields[0].status == ANSWERABLE
    assert report.fields[0].path == "canned_answers.visa_sponsorship_required"


# -- W5 additive schema extension (schema_version 2) -------------------------


def test_field_new_w5_columns_default_additively():
    # Every existing construction site (the vendor capture modules, fixtures,
    # older tests) keeps working: only the original 7 positional-ish args are required.
    fld = Field(key="k", label="L", type="input_text", required=True,
               options=[], source="questions",
               locator=Locator(role="textbox", name="L"))
    assert fld.step_index is None
    assert fld.conditional_on is None
    assert fld.decline_allowed is False
    assert fld.max_length is None
    assert fld.accept_types is None
    assert fld.norm_type == ""
    assert fld.section == "STANDARD"


def test_normalize_type_maps_greenhouse_and_lever_vocabulary():
    # This is also the vocabulary Lever's DOM controls and Ashby's own
    # `_ASHBY_TYPE_MAP` collapse into (in `engine.providers.ashby.capture`).
    assert normalize_type("input_text") == FieldType.TEXT
    assert normalize_type("textarea") == FieldType.LONGTEXT
    assert normalize_type("multi_value_single_select") == FieldType.SINGLE_SELECT
    assert normalize_type("multi_value_multi_select") == FieldType.MULTI_SELECT
    assert normalize_type("boolean") == FieldType.BOOLEAN
    assert normalize_type("input_file") == FieldType.FILE
    # input_hidden is a skip signal, never a FieldType member.
    assert normalize_type("input_hidden") == ""


def test_normalize_type_maps_raw_ashby_vocabulary():
    assert normalize_type("String") == FieldType.TEXT
    assert normalize_type("Email") == FieldType.EMAIL
    assert normalize_type("LongText") == FieldType.LONGTEXT
    assert normalize_type("ValueSelect") == FieldType.SINGLE_SELECT
    assert normalize_type("MultiValueSelect") == FieldType.MULTI_SELECT
    assert normalize_type("Phone") == FieldType.PHONE
    assert normalize_type("Date") == FieldType.DATE
    assert normalize_type("Boolean") == FieldType.BOOLEAN
    assert normalize_type("File") == FieldType.FILE
    assert normalize_type("Number") == FieldType.NUMBER


def test_normalize_type_falls_back_to_text_for_unknown_native():
    assert normalize_type("some_future_vendor_type") == FieldType.TEXT
    assert normalize_type("") == FieldType.TEXT


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


def test_store_roundtrip_tolerates_v1_shaped_fieldmap_body(store):
    # A row cached before this extension: no decline_allowed/max_length/
    # accept_types/norm_type/section keys at all. Must deserialize cleanly
    # via Field.from_dict's defaults, no store-side migration required.
    v1_body = {
        "vendor": "greenhouse", "posting_id": "1", "schema_version": "1",
        "captured_at": _PINNED,
        "fields": [{
            "key": "first_name", "label": "First Name", "type": "input_text",
            "required": True, "options": [], "source": "questions",
            "locator": {"role": "textbox", "name": "First Name"},
            "step_index": 0, "conditional_on": None,
        }],
    }
    store.put_fieldmap("greenhouse", "1", "u1", v1_body, _PINNED)
    cached = store.get_fieldmap("greenhouse", "1", "u1")
    fm = FieldMap.from_dict(cached["body"])

    assert fm.schema_version == "1"
    fld = fm.fields[0]
    assert fld.key == "first_name"
    assert fld.step_index == 0
    assert fld.conditional_on is None
    # W5 fields default cleanly for a v1-shaped cached row.
    assert fld.decline_allowed is False
    assert fld.max_length is None
    assert fld.accept_types is None
    assert fld.norm_type == ""
    assert fld.section == "STANDARD"


def test_store_roundtrip_preserves_v2_shaped_fieldmap_body(store):
    fm = FieldMap(vendor="greenhouse", posting_id="2", captured_at=_PINNED,
                  fields=[Field(
                      key="certify", label="I certify", type="boolean",
                      required=False, options=[], source="compliance",
                      locator=Locator(role="checkbox", name="I certify"),
                      step_index=0, conditional_on=None,
                      decline_allowed=True, max_length=None,
                      accept_types=None, norm_type=FieldType.BOOLEAN,
                      section=Section.COMPLIANCE_EEOC)])
    store.put_fieldmap("greenhouse", "2", "u1", fm.to_dict(), fm.captured_at)
    cached = store.get_fieldmap("greenhouse", "2", "u1")
    restored = FieldMap.from_dict(cached["body"])

    assert restored.to_dict() == fm.to_dict()
    assert restored.schema_version == SCHEMA_VERSION
    fld = restored.fields[0]
    assert fld.decline_allowed is True
    assert fld.norm_type == FieldType.BOOLEAN
    assert fld.section == Section.COMPLIANCE_EEOC
