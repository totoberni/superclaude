"""Greenhouse portal-widget coverage classification, exercised through the
kernel classifier with the Greenhouse `vendor_resolver` injected EXPLICITLY
(W5.1 Stage 2e-2).

These cases were relocated from `tests/test_fieldmap.py`, where they ran against
the transitional `engine.fieldmap.coverage` shim (which default-injected the
Greenhouse resolver for every caller). Here the injection is explicit: coverage
comes from `engine.kernel.resolve` and the real
`engine.providers.greenhouse.resolve.GREENHOUSE_WIDGET_RESOLVER` is passed in.
Assertions are IDENTICAL to the originals -- only the injection became explicit.

The `test_no_resolver_*` cases are the INVERSE: the same Greenhouse-shaped field
maps classified with NO resolver (the kernel no-op default) yield the generic
outcomes, proving the widget behavior comes solely from the injected resolver and
the seam is live. `test_run_pipeline_injects_greenhouse_resolver_not_lever`
proves the pipeline (`engine.run`) builds the resolver from the registry PER
vendor: greenhouse gets its widget resolver, lever gets the no-op.
"""

from engine.fieldmap import (
    ANSWERABLE,
    MANUAL_ONLY,
    MISSING_STATUS,
    Field,
    FieldMap,
    Locator,
)
from engine.kernel.resolve import coverage
from engine.providers.greenhouse.resolve import GREENHOUSE_WIDGET_RESOLVER
from engine.ssot import SSOT

_PINNED = "2026-07-03T00:00:00+00:00"


def _field(key, label, *, type_="input_text", source="questions"):
    return Field(key=key, label=label, type=type_, required=True, options=[],
                source=source, locator=Locator(role="textbox", name=label),
                step_index=0, conditional_on=None)


# -- relocated Greenhouse-widget cases (resolver injected explicitly) ---------

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
    report = coverage(fm, ssot, {}, vendor_resolver=GREENHOUSE_WIDGET_RESOLVER)
    by_key = {f.key: f for f in report.fields}

    assert by_key["location"].status == ANSWERABLE
    assert by_key["location"].path == "identity.current_location"

    assert by_key["longitude"].status == MANUAL_ONLY
    assert by_key["longitude"].reason == "portal-widget"
    assert by_key["latitude"].status == MANUAL_ONLY
    assert by_key["latitude"].reason == "portal-widget"


# -- Greenhouse resume_text / cover_letter_text paste textareas (gap #1) -----
# These required textareas share their LABEL ("Resume"/"Resume/CV") with the
# sibling FILE upload field, so a label-keyword match on "resume"/"cv" alone
# cannot distinguish them; they must resolve by KEY (mirrors the
# location-widget pattern above), never classify as manual-only file-upload.

def test_resume_text_textarea_is_not_manual_only_file_upload():
    fld = _field("resume_text", "Resume/CV", type_="textarea")
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[fld])
    report = coverage(
        fm, SSOT({"canned_answers": {"resume_text": "Some resume text."}}), {},
        vendor_resolver=GREENHOUSE_WIDGET_RESOLVER)
    assert report.fields[0].status != MANUAL_ONLY


def test_resume_text_resolves_by_key_to_canned_answers_resume_text():
    fld = _field("resume_text", "Resume/CV", type_="textarea")
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[fld])
    report = coverage(
        fm, SSOT({"canned_answers": {"resume_text": "Some resume text."}}), {},
        vendor_resolver=GREENHOUSE_WIDGET_RESOLVER)
    assert report.fields[0].status == ANSWERABLE
    assert report.fields[0].path == "canned_answers.resume_text"


def test_cover_letter_text_resolves_by_key_to_canned_answers_cover_letter_text():
    fld = _field("cover_letter_text", "Cover Letter", type_="textarea")
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[fld])
    report = coverage(
        fm, SSOT({"canned_answers": {"cover_letter_text": "Dear team..."}}), {},
        vendor_resolver=GREENHOUSE_WIDGET_RESOLVER)
    assert report.fields[0].status == ANSWERABLE
    assert report.fields[0].path == "canned_answers.cover_letter_text"


def test_cover_letter_text_falls_back_to_canned_answers_cover_letter():
    fld = _field("cover_letter_text", "Cover Letter", type_="textarea")
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[fld])
    report = coverage(
        fm, SSOT({"canned_answers": {"cover_letter": "Dear team..."}}), {},
        vendor_resolver=GREENHOUSE_WIDGET_RESOLVER)
    assert report.fields[0].status == ANSWERABLE
    assert report.fields[0].path == "canned_answers.cover_letter"


def test_resume_text_missing_when_ssot_lacks_the_value():
    # Never fabricated: with neither candidate path in the SSOT, resume_text
    # resolves MISSING (a questionnaire item), never auto-answered from the
    # shared label.
    fld = _field("resume_text", "Resume/CV", type_="textarea")
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[fld])
    report = coverage(fm, SSOT({}), {},
                      vendor_resolver=GREENHOUSE_WIDGET_RESOLVER)
    assert report.fields[0].status == MISSING_STATUS


# -- inverse cases: NO resolver -> generic outcomes (the seam is live) --------

def test_no_resolver_longitude_latitude_not_portal_widget(real_ssot_path):
    # Inverse of the widget test above: with NO vendor_resolver the kernel
    # classifies generically, so longitude/latitude are no longer portal-widget
    # manual-only (they fall to the generic location matcher). This proves the
    # widget behavior comes ONLY from the injected resolver.
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[_field("longitude", "Location"),
                         _field("latitude", "Location")])
    ssot = SSOT.load(real_ssot_path)
    report = coverage(fm, ssot, {})  # no resolver -> kernel no-op default
    by_key = {f.key: f for f in report.fields}

    assert by_key["longitude"].status != MANUAL_ONLY
    assert by_key["longitude"].reason != "portal-widget"
    assert by_key["latitude"].status != MANUAL_ONLY
    assert by_key["latitude"].reason != "portal-widget"


def test_no_resolver_resume_text_falls_to_generic_missing():
    # Inverse of the key-text test above: without the resolver resume_text is
    # NOT resolved by KEY (that is Greenhouse widget behavior), so even with
    # canned_answers.resume_text seeded it falls to generic label matching,
    # which has no matcher for "Resume/CV" -> MISSING, never the keyed path.
    fld = _field("resume_text", "Resume/CV", type_="textarea")
    fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                  fields=[fld])
    report = coverage(
        fm, SSOT({"canned_answers": {"resume_text": "Some resume text."}}), {})
    assert report.fields[0].status == MISSING_STATUS
    assert report.fields[0].path != "canned_answers.resume_text"


# -- run-path injection: the pipeline builds the resolver per vendor ----------

def test_run_pipeline_injects_greenhouse_resolver_not_lever(real_ssot_path):
    # The pipeline seam (run.py) builds the resolver from the registry PER
    # vendor: greenhouse gets its widget resolver (longitude -> portal-widget
    # manual-only), lever gets none (longitude classifies generically). The
    # outcome DIFFERS by vendor, proving the injection happens at the run path,
    # not a gh-for-everyone default. Asserted via the classification outcome,
    # not by introspecting which resolver object was chosen.
    from engine.run import _coverage_with_vendor_resolver

    ssot = SSOT.load(real_ssot_path)
    gh_fm = FieldMap(vendor="greenhouse", posting_id="9", captured_at=_PINNED,
                     fields=[_field("longitude", "Location")])
    lever_fm = FieldMap(vendor="lever", posting_id="9", captured_at=_PINNED,
                       fields=[_field("longitude", "Location")])

    gh_report = _coverage_with_vendor_resolver(gh_fm, ssot, {})
    lever_report = _coverage_with_vendor_resolver(lever_fm, ssot, {})

    assert gh_report.fields[0].status == MANUAL_ONLY
    assert gh_report.fields[0].reason == "portal-widget"
    assert lever_report.fields[0].status != MANUAL_ONLY
    assert lever_report.fields[0].reason != "portal-widget"
