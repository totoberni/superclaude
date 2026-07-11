"""Deterministic coverage classification + normalize_type (W4 3.1).

Generic, vendor-agnostic field-map logic: the coverage keyword-matching against a
synthetic SSOT (every verdict asserted explicitly; no LLM) and the
`normalize_type` vendor-native -> canonical `FieldType` mapper. The Greenhouse
capture/parse tests moved to tests/providers/greenhouse/test_capture.py in Stage
5; this file builds its field maps by hand.
"""

from engine.fieldmap import normalize_type
from engine.kernel.contracts import (
    SCHEMA_VERSION,
    Field,
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
from engine.ssot import SSOT

_PINNED = "2026-07-03T00:00:00+00:00"


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
