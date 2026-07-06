"""Form-fill DRY RUN: deterministic value resolution + safe fill (W4 4b).

No playwright, no network: fill_form is driven through fake page/locator objects
over hand-built field maps, so the autouse no-network guard holds throughout.
The safety invariants (no submit path, URL-unchanged, no file touch, EEO never
touched) are each asserted directly. resolve_values is pure keyword matching
against the synthetic v1.4 SSOT; every verdict is asserted explicitly.
"""

import contextlib
from pathlib import Path

import pytest

from engine.fill import (
    FillAssets,
    FillReport,
    FillSafetyError,
    ResolvedValues,
    _is_photo_field,
    _locate_file_input,
    _resolve_photo,
    _safe_click,
    _safe_upload,
    default_assets,
    fill_form,
    publish_evidence,
    resolve_values,
)
from engine.fieldmap import (
    Field,
    FieldMap,
    Locator,
    _DECLINE_SECTIONS,
    _SECTION_FOR_SOURCE,
)
from engine.profile_map import profile_from_real_ssot
from engine.ssot import SSOT

_PINNED = "2026-07-03T00:00:00+00:00"
_NO_OVERRIDE = object()  # sentinel: input_value() is not overridden


# --- fixture builders ---------------------------------------------------------

def _field(key, label, *, type_="input_text", required=True, options=None,
           source="questions", role="textbox", section=None):
    """The `section` (schema_version 2+ structural signal, never derived from a
    label keyword) mirrors real `fieldmap.py` capture: `_SECTION_FOR_SOURCE.get
    (source, STANDARD)`, same as `_fields_from_question` stamps it, unless the
    caller passes an explicit override (e.g. to construct a required question
    that merely SHARES a label keyword with an EEO field while staying a
    genuinely non-demographic STANDARD-section field)."""
    resolved_section = (section if section is not None
                        else _SECTION_FOR_SOURCE.get(source, "STANDARD"))
    return Field(key=key, label=label, type=type_, required=required,
                 options=options or [], source=source,
                 locator=Locator(role=role, name=label),
                 step_index=0, conditional_on=None,
                 section=resolved_section,
                 decline_allowed=resolved_section in _DECLINE_SECTIONS)


def _fieldmap(*fields, vendor="greenhouse", posting_id="9"):
    return FieldMap(vendor=vendor, posting_id=posting_id, captured_at=_PINNED,
                    fields=list(fields))


# --- fake browser page + locator ---------------------------------------------

class _FakeLocator:
    """Records the actions taken against one control and serves readback.

    `navigate_to` simulates a control whose interaction navigates the page
    (used to prove the URL-unchanged safety invariant). `readback` overrides
    the value returned on input_value() to force a readback mismatch.
    """

    def __init__(self, *, page=None, navigate_to=None, aria_invalid=None,
                 readback=_NO_OVERRIDE):
        self._page = page
        self._navigate_to = navigate_to
        self._aria_invalid = aria_invalid
        self._readback_override = readback
        self.filled = None
        self.selected = None
        self.checked = None
        self.blurred = False
        self.set_input_files_calls = 0
        self.uploaded = None
        self.clicks = 0

    # -- actions --
    def fill(self, value):
        self.filled = value
        self._maybe_navigate()

    def select_option(self, label=None):
        self.selected = label
        self._maybe_navigate()

    def check(self):
        self.checked = True
        self._maybe_navigate()

    def click(self):
        self.clicks += 1

    def set_input_files(self, files):  # allowed now, but only for upload fields
        self.set_input_files_calls += 1
        self.uploaded = files

    def blur(self):
        self.blurred = True

    # -- readback / attrs --
    def input_value(self):
        if self._readback_override is not _NO_OVERRIDE:
            return self._readback_override
        if self.filled is not None:
            return self.filled
        if self.selected is not None:
            return self.selected
        return ""

    def is_checked(self):
        return bool(self.checked)

    def get_attribute(self, name):
        if name == "aria-invalid":
            return self._aria_invalid
        return None

    def _maybe_navigate(self):
        if self._navigate_to is not None and self._page is not None:
            self._page._url = self._navigate_to


class _FakeErrorLocator:
    def __init__(self, texts):
        self._texts = texts

    def all_inner_texts(self):
        return list(self._texts)


class _FakeFileInput:
    """A plain <input type=file> element handle: exposes id/name/accept and the
    direct `set_input_files` upload path (no click), like a Playwright handle.

    `input_value` mirrors real <input type=file> behaviour: it reads back
    whatever `set_input_files` last recorded, so a genuine attach reports
    non-empty by default. Pass `readback` to force a specific value (e.g. ""
    to simulate a custom widget that silently swallowed the upload without
    ever wiring the native input)."""

    def __init__(self, *, id=None, name=None, accept=None,
                 readback=_NO_OVERRIDE):
        self._attrs = {"id": id, "name": name, "accept": accept}
        self._readback_override = readback
        self.set_input_files_calls = 0
        self.uploaded = None
        self.clicks = 0

    def get_attribute(self, name):
        return self._attrs.get(name)

    def set_input_files(self, files):
        self.set_input_files_calls += 1
        self.uploaded = files

    def click(self):
        self.clicks += 1

    def input_value(self):
        if self._readback_override is not _NO_OVERRIDE:
            return self._readback_override
        return self.uploaded or ""


class _FakePage:
    def __init__(self, *, url, controls=None, error_texts=None,
                 file_inputs=None):
        self._url = url
        self._controls = {} if controls is None else controls
        self._error_texts = error_texts or []
        self._file_inputs = [] if file_inputs is None else file_inputs
        self.goto_calls = []
        self.screenshot_calls = []
        self.requested = []

    @property
    def url(self):
        return self._url

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))

    def get_by_role(self, role, name=None):
        self.requested.append(("role", role, name))
        return self._controls[name]

    def get_by_label(self, label):
        self.requested.append(("label", label))
        return self._controls[label]

    def query_selector_all(self, selector):
        if "file" in selector:
            return list(self._file_inputs)
        return []

    def locator(self, selector):
        if selector == ".error":
            return _FakeErrorLocator(self._error_texts)
        return _FakeErrorLocator([])

    def screenshot(self, path=None):
        self.screenshot_calls.append(path)


def _factory_for(page):
    @contextlib.contextmanager
    def factory():
        yield page
    return factory


def _control_page(fields, *, url="https://jobs.example.com/x/1/apply",
                  file_inputs=None, **loc_kwargs):
    """A fake page whose controls are keyed by each field's locator name, plus a
    <input type=file> per upload field (id = field key) so the real file-input
    location path resolves. Pass `file_inputs` to override the auto-built set."""
    controls = {}
    page = None

    def build():
        nonlocal page
        fis = (list(file_inputs) if file_inputs is not None else
               [_FakeFileInput(id=fv.key)
                for fv in fields if isinstance(fv.value, Path)])
        page = _FakePage(url=url, controls=controls, file_inputs=fis,
                         **loc_kwargs)
        for fv in fields:
            controls[fv.locator.name] = _FakeLocator(page=page)
        return page

    return build


# =============================================================================
# resolve_values
# =============================================================================

def test_resolve_answerable_text(real_ssot_path):
    # The fixture SSOT carries only a combined `identity.name` ("Test
    # Candidate"), no discrete first/last key: a First Name field resolves via
    # the full-name-path fallback and is split to the first token (FIX 1),
    # never the whole name.
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field("first_name", "First Name"),
                   _field("email", "Email"))
    resolved = resolve_values(fm, ssot, profile_from_real_ssot(ssot))

    assert resolved.values["first_name"] == "Test"
    assert resolved.values["email"] == "test.candidate@example.invalid"
    assert resolved.skipped == []


def test_resolve_discrete_first_and_last_name_fields_win_over_full_name():
    # FIX 1 regression: when the SSOT carries BOTH a combined name and
    # discrete first_name/last_name keys, each field must resolve to its OWN
    # discrete key, never the whole name (the bug: both fields got the full
    # name because `identity.name` led both candidate lists).
    ssot = SSOT({"identity": {
        "name": "Ada Lovelace", "first_name": "Ada", "last_name": "Lovelace",
        "email": "ada@example.invalid"}})
    fm = _fieldmap(_field("first_name", "First Name"),
                   _field("last_name", "Last Name"))
    resolved = resolve_values(fm, ssot, {})

    assert resolved.values["first_name"] == "Ada"
    assert resolved.values["last_name"] == "Lovelace"
    assert resolved.skipped == []


def test_resolve_full_name_only_splits_into_first_and_last():
    # FIX 1 regression: no discrete first_name/last_name key at all -> the
    # matcher falls back to the combined `identity.name`, which is split at
    # render time rather than typed whole into both fields.
    ssot = SSOT({"identity": {"name": "Ada Lovelace",
                             "email": "ada@example.invalid"}})
    fm = _fieldmap(_field("first_name", "First Name"),
                   _field("last_name", "Last Name"))
    resolved = resolve_values(fm, ssot, {})

    assert resolved.values["first_name"] == "Ada"
    assert resolved.values["last_name"] == "Lovelace"
    assert resolved.skipped == []


def test_resolve_single_token_full_name_first_gets_it_last_is_skipped():
    # A single-token name has nothing to split out for the last-name field:
    # it is honestly skipped rather than typed as an empty string.
    ssot = SSOT({"identity": {"name": "Cher", "email": "cher@example.invalid"}})
    fm = _fieldmap(_field("first_name", "First Name"),
                   _field("last_name", "Last Name"))
    resolved = resolve_values(fm, ssot, {})

    assert resolved.values["first_name"] == "Cher"
    assert "last_name" not in resolved.values
    reasons = dict(resolved.skipped)
    assert "single-token name" in reasons["last_name"]


def test_resolve_select_dict_node_matches_a_scalar_sub_value():
    # FIX 2 regression: the resolved path is a dict node (not a scalar). A
    # select field can still answer from one of the dict's own scalar values.
    ssot = SSOT({"canned_answers": {"notice_period": {
        "weeks": "2", "note": "negotiable"}}})
    fm = _fieldmap(_field("q_notice", "Notice period",
                          type_="multi_value_single_select",
                          options=["Immediate", "negotiable"], role="combobox"))
    resolved = resolve_values(fm, ssot, {})

    assert resolved.values["q_notice"] == "negotiable"
    assert resolved.skipped == []


def test_resolve_select_dict_node_no_scalar_match_is_skipped():
    # FIX 2 regression: none of the dict's scalar values match any option ->
    # honestly skipped with a reason naming the mapping, never a crash.
    ssot = SSOT({"canned_answers": {"notice_period": {
        "weeks": "2", "note": "flexible"}}})
    fm = _fieldmap(_field("q_notice", "Notice period",
                          type_="multi_value_single_select",
                          options=["Immediate", "2 weeks"], role="combobox"))
    resolved = resolve_values(fm, ssot, {})

    assert "q_notice" not in resolved.values
    reasons = dict(resolved.skipped)
    assert "resolved to a mapping with no usable scalar" in reasons["q_notice"]
    assert "canned_answers.notice_period" in reasons["q_notice"]


def test_resolve_text_field_dict_node_is_skipped_never_typed_as_a_mapping():
    # FIX 2 regression: a text (non-select) field whose path resolves to a
    # dict must never be typed as a mapping; it is skipped instead.
    ssot = SSOT({"canned_answers": {"notice_period": {"weeks": "2"}}})
    fm = _fieldmap(_field("q_notice", "Notice period"))
    resolved = resolve_values(fm, ssot, {})

    assert "q_notice" not in resolved.values
    reasons = dict(resolved.skipped)
    assert "resolved to a mapping with no usable scalar" in reasons["q_notice"]


def test_resolve_select_option_match(real_ssot_path):
    # "sponsorship" -> canned_answers.visa_sponsorship_required == "no";
    # options ["Yes","No"] -> the "No" option label (case-insensitive match).
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field("q_visa", "Do you require visa sponsorship?",
                          type_="multi_value_single_select",
                          options=["Yes", "No"], role="combobox"))
    resolved = resolve_values(fm, ssot, profile_from_real_ssot(ssot))

    assert resolved.values["q_visa"] == "No"
    assert resolved.skipped == []


def test_resolve_select_no_match_is_skipped(real_ssot_path):
    # notice_period == "1 month" matches none of these options -> skip.
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field("q_notice", "Notice period",
                          type_="multi_value_single_select",
                          options=["Immediate", "2 weeks"], role="combobox"))
    resolved = resolve_values(fm, ssot, profile_from_real_ssot(ssot))

    assert "q_notice" not in resolved.values
    assert len(resolved.skipped) == 1
    key, reason = resolved.skipped[0]
    assert key == "q_notice"
    assert "no option matches" in reason


def test_resolve_consent_checkbox_is_true(real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field("q_consent", "I agree to the Privacy Policy.",
                          type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, ssot, profile_from_real_ssot(ssot))

    assert resolved.values["q_consent"] is True
    assert resolved.skipped == []


def test_resolve_skips_manual_missing_and_file(real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(
        _field("resume", "Resume / CV", type_="input_file", role="button"),
        _field("demographic_1", "Gender", type_="multi_value_single_select",
               options=["Man", "Woman"], source="demographic", role="combobox"),
        _field("q_colour", "What is your favourite colour?"),
    )
    resolved = resolve_values(fm, ssot, profile_from_real_ssot(ssot))

    assert resolved.values == {}
    reasons = dict(resolved.skipped)
    assert reasons["resume"] == "file-upload"
    assert reasons["demographic_1"] == "demographic/EEO"
    assert reasons["q_colour"].startswith("missing:canned_answers.")


def test_resolve_whitespace_only_ssot_value_is_skipped_not_a_confirmed_fill():
    # FIX 2 (honest completeness, promise C): a whitespace-only SSOT value is
    # NOT caught by `SSOT.get`'s own MISSING check (that only catches a
    # zero-length string, so "   " resolves as a real, non-MISSING value and
    # reaches the render layer) -- it must still be SKIPPED here, never
    # rendered as a fill value, since there is nothing real to type/select.
    ssot = SSOT({"identity": {"name": "   ", "email": "  "}})
    fm = _fieldmap(_field("first_name", "First Name"),
                   _field("email", "Email"))
    resolved = resolve_values(fm, ssot, {})

    assert resolved.values == {}
    reasons = dict(resolved.skipped)
    assert "empty SSOT value" in reasons["first_name"]
    assert "identity.name" in reasons["first_name"]
    assert "empty SSOT value" in reasons["email"]
    assert "identity.email" in reasons["email"]


# =============================================================================
# fill_form
# =============================================================================

def test_fill_records_fills_blur_and_screenshot(tmp_path):
    text = _FakeLocator()
    consent = _FakeLocator()
    fields = [
        _fv("first_name", "First Name", "input_text", "Test Candidate"),
        _fv("q_consent", "I agree to the Privacy Policy.", "boolean", True,
            role="checkbox"),
    ]
    controls = {"First Name": text, "I agree to the Privacy Policy.": consent}
    page = _FakePage(url="https://jobs.lever.co/globex/req-77/apply",
                     controls=controls)
    values = ResolvedValues(fields=fields)

    report = fill_form("lever", "globex", "req-77", values,
                       browser_factory=_factory_for(page),
                       artifacts_dir=tmp_path, now=lambda: _PINNED)

    assert isinstance(report, FillReport)
    assert report.filled == 2
    assert text.filled == "Test Candidate"
    assert text.blurred is True
    assert consent.checked is True
    assert consent.blurred is True
    assert report.url_unchanged is True
    assert report.readback_mismatches == []
    # exactly one page load + one screenshot under artifacts_dir
    assert len(page.goto_calls) == 1
    assert page.goto_calls[0][0] == "https://jobs.lever.co/globex/req-77/apply"
    assert len(page.screenshot_calls) == 1
    assert page.screenshot_calls[0] == report.screenshot
    assert str(tmp_path) in report.screenshot
    assert report.ts == _PINNED


def test_fill_detects_readback_mismatch(tmp_path):
    # The fake mutates the readback value: the control does not hold what we
    # intended, so the diff must surface a mismatch. A value that did not take
    # must NOT count as filled (the readback-gating fix): a required field the
    # page silently rejected must never read as done.
    mutated = _FakeLocator(readback="Someone Else")
    fields = [_fv("first_name", "First Name", "input_text", "Test Candidate")]
    page = _FakePage(url="https://x/1/apply", controls={"First Name": mutated})

    report = fill_form("greenhouse", "acme", "1",
                       ResolvedValues(fields=fields),
                       browser_factory=_factory_for(page),
                       artifacts_dir=tmp_path, now=lambda: _PINNED)

    assert report.filled == 0
    assert len(report.readback_mismatches) == 1
    mismatch = report.readback_mismatches[0]
    assert mismatch["key"] == "first_name"
    assert mismatch["intended"] == "Test Candidate"
    assert mismatch["actual"] == "Someone Else"


def test_fill_harvests_validation_errors(tmp_path):
    invalid = _FakeLocator(aria_invalid="true")
    fields = [_fv("email", "Email", "input_text", "bad")]
    page = _FakePage(url="https://x/1/apply", controls={"Email": invalid},
                     error_texts=["Please enter a valid email address."])

    report = fill_form("greenhouse", "acme", "1",
                       ResolvedValues(fields=fields),
                       browser_factory=_factory_for(page),
                       artifacts_dir=tmp_path, now=lambda: _PINNED)

    messages = [e["message"] for e in report.validation_errors]
    assert "aria-invalid" in messages
    assert "Please enter a valid email address." in messages


def test_fill_report_json_serialises(tmp_path):
    import json

    fields = [_fv("first_name", "First Name", "input_text", "Test Candidate")]
    page = _FakePage(url="https://x/1/apply",
                     controls={"First Name": _FakeLocator()})
    report = fill_form("greenhouse", "acme", "5501001",
                       ResolvedValues(fields=fields, skipped=[("resume",
                                                               "file-upload")]),
                       browser_factory=_factory_for(page),
                       artifacts_dir=tmp_path, now=lambda: _PINNED)

    blob = json.loads(json.dumps(report.to_dict()))
    assert blob["vendor"] == "greenhouse"
    assert blob["posting_id"] == "5501001"
    assert blob["filled"] == 1
    assert blob["skipped"] == [["resume", "file-upload"]]
    assert blob["url_unchanged"] is True


# =============================================================================
# SAFETY INVARIANTS
# =============================================================================

@pytest.mark.parametrize("name", [
    "Submit application", "Apply now", "Send", "Finish", "Continue",
    "SUBMIT", "submit-application",
])
def test_safe_click_refuses_submit_like_names(name):
    target = _FakeLocator()
    with pytest.raises(FillSafetyError, match="submit denylist"):
        _safe_click(target, name)
    assert target.clicks == 0


def test_safe_click_allows_a_benign_name():
    target = _FakeLocator()
    _safe_click(target, "Add another link")
    assert target.clicks == 1


def test_fill_raises_when_url_changes(tmp_path):
    # A control whose interaction navigates the page must trip the
    # URL-unchanged invariant (possible submission/redirect).
    navigating = _FakeLocator()
    page = _FakePage(url="https://jobs.lever.co/globex/req-77/apply",
                     controls={"First Name": navigating})
    navigating._page = page
    navigating._navigate_to = "https://jobs.lever.co/globex/req-77/thanks"
    fields = [_fv("first_name", "First Name", "input_text", "Test Candidate")]

    with pytest.raises(FillSafetyError, match="navigated during fill"):
        fill_form("lever", "globex", "req-77", ResolvedValues(fields=fields),
                  browser_factory=_factory_for(page), artifacts_dir=tmp_path)


def test_fill_never_touches_file_inputs(tmp_path, real_ssot_path):
    # resolve_values already excludes the file field; fill_form must additionally
    # never construct a locator for it nor call set_input_files.
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(
        _field("first_name", "First Name"),
        _field("resume", "Resume / CV", type_="input_file", role="button"),
    )
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot))
    assert "resume" not in values.values

    file_control = _FakeLocator()
    page = _FakePage(url="https://x/1/apply",
                     controls={"First Name": _FakeLocator(),
                               "Resume / CV": file_control})
    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(page),
                       artifacts_dir=tmp_path, now=lambda: _PINNED)

    # the file control was never looked up nor uploaded to
    assert ("label", "Resume / CV") not in page.requested
    assert ("role", "button", "Resume / CV") not in page.requested
    assert file_control.set_input_files_calls == 0
    assert dict(report.skipped)["resume"] == "file-upload"


def test_fill_leaves_eeo_fields_untouched(tmp_path, real_ssot_path):
    # A required EEO/demographic field is manual-only, so it never enters the
    # fill set and its control is never located.
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(
        _field("first_name", "First Name"),
        _field("demographic_gender", "Gender",
               type_="multi_value_single_select", options=["Man", "Woman"],
               source="demographic", role="combobox"),
    )
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot))
    assert "demographic_gender" not in values.values

    eeo_control = _FakeLocator()
    page = _FakePage(url="https://x/1/apply",
                     controls={"First Name": _FakeLocator(),
                               "Gender": eeo_control})
    fill_form("greenhouse", "acme", "1", values,
              browser_factory=_factory_for(page), artifacts_dir=tmp_path,
              now=lambda: _PINNED)

    assert ("role", "combobox", "Gender") not in page.requested
    assert ("label", "Gender") not in page.requested
    assert eeo_control.filled is None
    assert eeo_control.selected is None
    assert dict(values.skipped)["demographic_gender"] == "demographic/EEO"


# --- resolve_values -> fill_form integration ---------------------------------

def test_resolve_then_fill_end_to_end(tmp_path, real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(
        _field("first_name", "First Name"),
        _field("email", "Email"),
        _field("q_visa", "Do you require visa sponsorship?",
               type_="multi_value_single_select", options=["Yes", "No"],
               role="combobox"),
        _field("q_consent", "I agree to the Privacy Policy.",
               type_="boolean", role="checkbox"),
        _field("resume", "Resume / CV", type_="input_file", role="button"),
    )
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot))
    build = _control_page(values.fields,
                          url="https://boards.greenhouse.io/acme/jobs/1")

    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(build()),
                       artifacts_dir=tmp_path, now=lambda: _PINNED)

    assert report.filled == 4          # name, email, visa, consent
    assert dict(report.skipped)["resume"] == "file-upload"
    assert report.url_unchanged is True
    assert report.readback_mismatches == []


def test_resume_text_and_cover_letter_text_fill_and_form_completes(tmp_path):
    # BUG FIX (gap #1): resume_text/cover_letter_text textareas share their
    # LABEL with the sibling FILE upload field ("Resume"/"Resume/CV"), so a
    # label-keyword match on "resume"/"cv" alone previously tagged them
    # manual-only file-upload -- the required resume_text was skipped and the
    # PDF-uploading form was forced NOT COMPLETE. They must resolve by KEY,
    # render as free text, and let the form read COMPLETE.
    ssot = SSOT({
        "identity": {"name": "Ada Lovelace"},
        "canned_answers": {
            "resume_text": "Ada Lovelace, mathematician and writer.",
            "cover_letter_text": "Dear hiring manager, ...",
        },
    })
    fm = _fieldmap(
        _field("first_name", "First Name"),
        _field("resume", "Resume/CV", type_="input_file", role="button"),
        _field("resume_text", "Resume/CV", type_="textarea"),
        _field("cover_letter_text", "Cover Letter", type_="textarea",
               required=False),
    )
    assets = _make_assets(tmp_path)
    values = resolve_values(fm, ssot, {}, assets=assets)

    assert values.values["resume_text"] == (
        "Ada Lovelace, mathematician and writer.")
    assert values.values["cover_letter_text"] == "Dear hiring manager, ..."
    assert "resume_text" not in dict(values.skipped)

    build = _control_page(values.fields,
                          url="https://boards.greenhouse.io/acme/jobs/1")
    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(build()),
                       artifacts_dir=tmp_path, fieldmap=fm, assets=assets,
                       now=lambda: _PINNED)

    assert report.required_unfilled == []
    assert report.complete is True
    assert report.uploads[0]["key"] == "resume"


def test_resume_text_skipped_missing_when_ssot_lacks_the_value():
    # Never fabricated: with no canned_answers value the field is skipped as
    # MISSING, not filled from the shared "Resume/CV" label.
    ssot = SSOT({"identity": {"name": "Ada Lovelace"}})
    fm = _fieldmap(_field("resume_text", "Resume/CV", type_="textarea"))
    values = resolve_values(fm, ssot, {})

    assert "resume_text" not in values.values
    assert dict(values.skipped)["resume_text"].startswith("missing:")


def _fv(key, label, type_, value, *, role="textbox"):
    from engine.fill import FieldValue
    return FieldValue(key=key, label=label, type=type_,
                      locator=Locator(role=role, name=label), value=value)


# =============================================================================
# W4 4c owner refinements: CV upload (2) + profile picture (3)
# =============================================================================

def _make_assets(tmp_path, *, ats=True, atsi=True, photo=True):
    """Build a FillAssets over real temp files; absent legs point at a
    non-existent path so `verified()` collapses them to None."""
    def make(name, present):
        p = tmp_path / name
        if present:
            p.write_bytes(b"stub")
        return p
    return FillAssets(cv_ats=make("cv-ats.pdf", ats),
                      cv_atsi=make("cv-atsi.pdf", atsi),
                      photo=make("Me.png", photo))


def test_cv_default_is_ats(tmp_path, real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field("resume", "Resume", type_="input_file", role="button"))
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot),
                            assets=_make_assets(tmp_path))
    assert len(values.fields) == 1
    fv = values.fields[0]
    assert fv.asset == "cv-ats"
    assert Path(fv.value).name == "cv-ats.pdf"
    assert "default" in fv.upload_reason


def test_cv_atsi_when_italian_and_no_photo_field(tmp_path, real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field("resume", "Resume", type_="input_file", role="button"))
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot),
                            assets=_make_assets(tmp_path), posting_lang="it")
    fv = values.fields[0]
    assert fv.asset == "cv-atsi"
    assert Path(fv.value).name == "cv-atsi.pdf"
    assert "italian" in fv.upload_reason


def test_cv_stays_ats_when_photo_field_present_even_if_italian(
        tmp_path, real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(
        _field("resume", "Resume", type_="input_file", role="button"),
        _field("photo", "Profile picture", type_="input_file", role="button"),
    )
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot),
                            assets=_make_assets(tmp_path), posting_lang="it")
    by_key = {fv.key: fv for fv in values.fields}
    assert by_key["resume"].asset == "cv-ats"       # a photo field blocks cv-atsi
    assert by_key["photo"].asset == "photo"


def test_no_assets_keeps_pre_override_file_skip(tmp_path, real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field("resume", "Resume / CV", type_="input_file",
                          role="button"))
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot))
    assert values.fields == []
    assert dict(values.skipped)["resume"] == "file-upload"


def test_missing_asset_is_skipped(tmp_path, real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    assets = _make_assets(tmp_path, ats=False, atsi=False, photo=False)
    fm = _fieldmap(_field("resume", "Resume", type_="input_file", role="button"))
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot), assets=assets)
    assert values.fields == []
    assert dict(values.skipped)["resume"] == "asset missing: cv-ats"


@pytest.mark.parametrize("label", [
    "Profile picture", "Headshot", "Upload your photo", "Profile image",
    "Foto", "Immagine del profilo", "La tua foto",
])
def test_photo_field_detection_incl_italian(label):
    assert _is_photo_field(_field("x", label, type_="input_file", role="button"))


def test_photo_field_detection_is_label_based_only():
    # `Field` (engine.fieldmap) carries no `accept` MIME attribute in
    # production; detection is driven entirely by the label regex (a former
    # accept-sniffing branch was dead code and has been removed).
    fld = _field("x", "Upload your photo", type_="input_file", role="button")
    assert not hasattr(fld, "accept")
    assert _is_photo_field(fld)


def test_resume_is_not_a_photo_field():
    assert not _is_photo_field(_field("r", "Resume / CV", type_="input_file"))


def test_photo_field_uploads_photo_asset(tmp_path, real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field("headshot", "Headshot", type_="input_file",
                          role="button"))
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot),
                            assets=_make_assets(tmp_path))
    fv = values.fields[0]
    assert fv.asset == "photo"
    assert Path(fv.value).name == "Me.png"


# =============================================================================
# _safe_upload whitelist + file-chooser safety
# =============================================================================

class _FakeChooser:
    def __init__(self):
        self.files = None

    def set_files(self, files):
        self.files = files


class _ChooserInfo:
    def __init__(self, chooser):
        self.value = chooser


class _FakeUploadButton:
    """An upload trigger with NO set_input_files, forcing the file-chooser path."""

    def __init__(self):
        self.clicks = 0

    def click(self):
        self.clicks += 1


class _FakeChooserPage:
    def __init__(self):
        self.chooser = _FakeChooser()

    @contextlib.contextmanager
    def expect_file_chooser(self):
        yield _ChooserInfo(self.chooser)


def test_safe_upload_whitelist_violation_raises(tmp_path):
    good = tmp_path / "cv-ats.pdf"
    good.write_bytes(b"stub")
    assets = FillAssets(cv_ats=good)
    control = _FakeLocator()
    with pytest.raises(FillSafetyError, match="whitelist"):
        _safe_upload(control, tmp_path / "evil.pdf", assets)
    assert control.set_input_files_calls == 0


def test_safe_upload_direct_set_input_files(tmp_path):
    good = tmp_path / "cv-ats.pdf"
    good.write_bytes(b"stub")
    assets = FillAssets(cv_ats=good)
    control = _FakeLocator()
    _safe_upload(control, good, assets)
    assert control.set_input_files_calls == 1
    assert control.uploaded == str(good)
    assert control.clicks == 0


def test_file_chooser_never_clicks_denylisted_trigger(tmp_path):
    good = tmp_path / "cv-ats.pdf"
    good.write_bytes(b"stub")
    assets = FillAssets(cv_ats=good)
    button = _FakeUploadButton()
    page = _FakeChooserPage()
    # "upload" clears the allowlist gate, but "continue" is on the submit denylist.
    with pytest.raises(FillSafetyError, match="submit denylist"):
        _safe_upload(button, good, assets, page=page,
                     button_name="Continue upload")
    assert button.clicks == 0
    assert page.chooser.files is None


def test_file_chooser_refuses_unrecognised_trigger(tmp_path):
    good = tmp_path / "cv-ats.pdf"
    good.write_bytes(b"stub")
    assets = FillAssets(cv_ats=good)
    button = _FakeUploadButton()
    page = _FakeChooserPage()
    with pytest.raises(FillSafetyError, match="attach/upload/browse"):
        _safe_upload(button, good, assets, page=page, button_name="Resume / CV")
    assert button.clicks == 0


def test_file_chooser_uploads_via_allowed_trigger(tmp_path):
    good = tmp_path / "cv-ats.pdf"
    good.write_bytes(b"stub")
    assets = FillAssets(cv_ats=good)
    button = _FakeUploadButton()
    page = _FakeChooserPage()
    _safe_upload(button, good, assets, page=page, button_name="Attach a file")
    assert button.clicks == 1
    assert page.chooser.files == str(good)


# =============================================================================
# COMPLETENESS DENOMINATOR (criterion 1) + caption exactness
# =============================================================================

def test_caption_exact_complete():
    report = _report(vendor="lever", company="globex", fillable_total=5,
                     filled=5, required_unfilled=[], justified_skips=0)
    assert report.complete is True
    assert (report.caption()
            == "Lever (globex): 5/5 fields filled, 0 required unfilled - COMPLETE")


def test_caption_exact_not_complete():
    report = _report(
        vendor="greenhouse", company="acme", fillable_total=10, filled=3,
        required_unfilled=[{"key": "a", "label": "A", "reason": "missing"},
                           {"key": "b", "label": "B", "reason": "missing"}],
        justified_skips=0)
    assert report.complete is False
    assert (report.caption()
            == "Greenhouse (acme): 3/10 fields filled, "
               "2 required unfilled - NOT COMPLETE")


def test_completeness_complete_with_justified_demographic(
        tmp_path, real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    assets = _make_assets(tmp_path)
    fm = _fieldmap(
        _field("first_name", "First Name"),
        _field("email", "Email"),
        _field("gender", "Gender", type_="multi_value_single_select",
               options=["Man", "Woman"], source="demographic", role="combobox"),
    )
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot), assets=assets)
    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(_control_page(values.fields)()),
                       fieldmap=fm, assets=assets, artifacts_dir=tmp_path,
                       now=lambda: _PINNED)
    assert report.fillable_total == 3
    assert report.filled == 2
    assert report.required_unfilled == []
    assert report.justified_skips == 1        # the demographic field
    assert report.complete is True
    assert (report.caption()
            == "Greenhouse (acme): 2/3 fields filled, "
               "0 required unfilled - COMPLETE")


def test_completeness_required_unfilled_forces_not_complete(
        tmp_path, real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    assets = _make_assets(tmp_path)
    fm = _fieldmap(
        _field("first_name", "First Name"),
        _field("q_colour", "What is your favourite colour?"),  # required, missing
    )
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot), assets=assets)
    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(_control_page(values.fields)()),
                       fieldmap=fm, assets=assets, artifacts_dir=tmp_path,
                       now=lambda: _PINNED)
    assert report.fillable_total == 2
    assert report.filled == 1
    assert len(report.required_unfilled) == 1
    assert report.required_unfilled[0]["key"] == "q_colour"
    assert report.required_unfilled[0]["label"] == "What is your favourite colour?"
    assert report.complete is False
    assert (report.caption()
            == "Greenhouse (acme): 1/2 fields filled, "
               "1 required unfilled - NOT COMPLETE")


def test_completeness_required_field_empty_ssot_value_is_not_complete(tmp_path):
    # FIX 2 (honest completeness, promise C): a REQUIRED field whose SSOT path
    # resolves to whitespace-only must never be counted as filled -- the old
    # `_readback` would compare the (empty) intended value against the (empty)
    # control and read ok=True even though `type_human`/`fill` placed nothing.
    # It must land in required_unfilled with a clear reason, never COMPLETE.
    ssot = SSOT({"identity": {"name": "   ", "email": "test@example.invalid"}})
    assets = _make_assets(tmp_path)
    fm = _fieldmap(
        _field("first_name", "First Name"),
        _field("email", "Email"),
    )
    values = resolve_values(fm, ssot, {}, assets=assets)
    assert "first_name" not in values.values
    assert "empty SSOT value" in dict(values.skipped)["first_name"]

    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(_control_page(values.fields)()),
                       fieldmap=fm, assets=assets, artifacts_dir=tmp_path,
                       now=lambda: _PINNED)

    assert report.fillable_total == 2
    assert report.filled == 1                       # email only
    assert len(report.required_unfilled) == 1
    assert report.required_unfilled[0]["key"] == "first_name"
    assert "empty SSOT value" in report.required_unfilled[0]["reason"]
    assert report.justified_skips == 0
    assert report.complete is False
    assert report.caption().endswith("NOT COMPLETE")


def test_completeness_upload_counts_and_stays_complete(tmp_path, real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    assets = _make_assets(tmp_path)
    fm = _fieldmap(
        _field("first_name", "First Name"),
        _field("resume", "Resume / CV", type_="input_file", role="button"),
        _field("gender", "Gender", type_="multi_value_single_select",
               options=["Man", "Woman"], source="demographic", role="combobox"),
    )
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot), assets=assets)
    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(_control_page(values.fields)()),
                       fieldmap=fm, assets=assets, artifacts_dir=tmp_path,
                       now=lambda: _PINNED)
    assert report.filled == 2                 # first_name + the uploaded CV
    assert report.uploads == [{"key": "resume", "asset": "cv-ats",
                               "path": str(assets.cv_ats),
                               "reason": "default (cv-ats always preferred)"}]
    assert report.complete is True
    assert report.fillable_total == 3
    assert report.justified_skips == 1        # the demographic field


def test_hidden_portal_widgets_excluded_from_denominator(
        tmp_path, real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    assets = _make_assets(tmp_path)
    fm = _fieldmap(
        _field("first_name", "First Name"),
        _field("longitude", "Location", role="textbox"),   # pure portal telemetry
        _field("latitude", "Location", role="textbox"),
    )
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot), assets=assets)
    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(_control_page(values.fields)()),
                       fieldmap=fm, assets=assets, artifacts_dir=tmp_path,
                       now=lambda: _PINNED)
    assert report.fillable_total == 1         # longitude/latitude are hidden
    assert report.filled == 1
    assert report.complete is True


# -- required-upload-missing-asset regression (false-COMPLETE hole, W4 fix) ----

def test_completeness_required_upload_missing_asset_is_required_unfilled(
        tmp_path, real_ssot_path):
    # The bug: a REQUIRED file field whose asset is missing used to be counted
    # as a "justified skip" (matching "asset missing" unconditionally), so it
    # never reached required_unfilled and complete stayed True with no CV
    # ever attached. It must now land in required_unfilled.
    ssot = SSOT.load(real_ssot_path)
    assets = _make_assets(tmp_path, ats=False, atsi=False, photo=False)
    fm = _fieldmap(
        _field("first_name", "First Name"),
        _field("resume", "Resume / CV", type_="input_file", role="button"),
    )
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot), assets=assets)
    assert dict(values.skipped)["resume"] == "asset missing: cv-ats"

    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(_control_page(values.fields)()),
                       fieldmap=fm, assets=assets, artifacts_dir=tmp_path,
                       now=lambda: _PINNED)

    assert report.fillable_total == 2
    assert report.filled == 1                       # first_name only
    assert len(report.required_unfilled) == 1
    assert report.required_unfilled[0] == {
        "key": "resume", "label": "Resume / CV",
        "reason": "asset missing: cv-ats"}
    assert report.justified_skips == 0
    assert report.complete is False
    assert (report.caption()
            == "Greenhouse (acme): 1/2 fields filled, "
               "1 required unfilled - NOT COMPLETE")


def test_completeness_required_upload_with_asset_is_filled_and_complete(
        tmp_path, real_ssot_path):
    # The non-regression counterpart: when the required upload's asset IS
    # present, the CV is attached (filled), not skipped -- complete stays True.
    ssot = SSOT.load(real_ssot_path)
    assets = _make_assets(tmp_path)
    fm = _fieldmap(
        _field("first_name", "First Name"),
        _field("resume", "Resume / CV", type_="input_file", role="button"),
    )
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot), assets=assets)

    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(_control_page(values.fields)()),
                       fieldmap=fm, assets=assets, artifacts_dir=tmp_path,
                       now=lambda: _PINNED)

    assert report.fillable_total == 2
    assert report.filled == 2                       # first_name + uploaded CV
    assert report.required_unfilled == []
    assert report.complete is True
    assert (report.caption()
            == "Greenhouse (acme): 2/2 fields filled, "
               "0 required unfilled - COMPLETE")


def test_completeness_optional_upload_missing_asset_stays_justified(
        tmp_path, real_ssot_path):
    # An OPTIONAL upload field with a missing asset is still a justified skip
    # (never a gap) -- the requiredness gate only bites required fields.
    ssot = SSOT.load(real_ssot_path)
    assets = _make_assets(tmp_path, ats=False, atsi=False, photo=False)
    fm = _fieldmap(
        _field("first_name", "First Name"),
        _field("resume", "Resume / CV", type_="input_file", role="button",
               required=False),
    )
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot), assets=assets)

    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(_control_page(values.fields)()),
                       fieldmap=fm, assets=assets, artifacts_dir=tmp_path,
                       now=lambda: _PINNED)

    assert report.fillable_total == 2
    assert report.filled == 1                       # first_name only
    assert report.required_unfilled == []
    assert report.justified_skips == 1               # optional CV, asset missing
    assert report.complete is True
    assert (report.caption()
            == "Greenhouse (acme): 1/2 fields filled, "
               "0 required unfilled - COMPLETE")


def test_completeness_required_demographic_skip_stays_justified(
        tmp_path, real_ssot_path):
    # A required EEO/demographic field is justified REGARDLESS of requiredness
    # (policy never auto-answers these) -- distinct from the upload rule above.
    ssot = SSOT.load(real_ssot_path)
    assets = _make_assets(tmp_path)
    fm = _fieldmap(
        _field("first_name", "First Name"),
        _field("gender", "Gender", type_="multi_value_single_select",
               options=["Man", "Woman"], source="demographic", role="combobox",
               required=True),
    )
    assert fm.fields[1].required is True

    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot), assets=assets)
    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(_control_page(values.fields)()),
                       fieldmap=fm, assets=assets, artifacts_dir=tmp_path,
                       now=lambda: _PINNED)

    assert report.fillable_total == 2
    assert report.filled == 1                       # first_name only
    assert report.required_unfilled == []
    assert report.justified_skips == 1               # the demographic field
    assert report.complete is True
    assert (report.caption()
            == "Greenhouse (acme): 1/2 fields filled, "
               "0 required unfilled - COMPLETE")


def test_completeness_required_eeo_keyword_label_but_not_demographic_section_is_not_complete(
        tmp_path, real_ssot_path):
    # Adversarial-review finding (FIX 3): a REQUIRED question whose LABEL merely
    # contains an EEO keyword ("disability") but is NOT a genuine demographic
    # field (section stays STANDARD, source stays "questions" -- a real
    # interview-logistics question, not an EEOC/demographic capture) must never
    # be silently justified into a false COMPLETE. `_manual_only_reason`'s
    # keyword-based safety net still fires (never auto-fills a suspected-EEO
    # field), but the section gate in `_completeness` refuses to justify it,
    # so it stays a required gap -> NOT COMPLETE.
    ssot = SSOT.load(real_ssot_path)
    assets = _make_assets(tmp_path)
    fm = _fieldmap(
        _field("first_name", "First Name"),
        _field("q_accommodations",
               "Do you require any disability accommodations for the "
               "interview?", required=True),   # source="questions" -> STANDARD
    )
    assert fm.fields[1].section == "STANDARD"
    assert fm.fields[1].required is True

    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot), assets=assets)
    assert "q_accommodations" not in values.values     # never auto-filled

    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(_control_page(values.fields)()),
                       fieldmap=fm, assets=assets, artifacts_dir=tmp_path,
                       now=lambda: _PINNED)

    assert report.fillable_total == 2
    assert report.filled == 1                       # first_name only
    assert len(report.required_unfilled) == 1
    assert report.required_unfilled[0]["key"] == "q_accommodations"
    assert report.justified_skips == 0
    assert report.complete is False
    assert report.caption().endswith("NOT COMPLETE")


def test_completeness_3_of_10_with_missing_required_cv_is_not_complete(
        tmp_path, real_ssot_path):
    # The owner's exact complaint scenario: a partial fill (3/10) with a
    # required CV that was never attached must never read COMPLETE.
    ssot = SSOT.load(real_ssot_path)
    assets = _make_assets(tmp_path, ats=False, atsi=False, photo=False)
    fm = _fieldmap(
        _field("first_name", "First Name"),
        _field("email", "Email"),
        _field("q_consent", "I agree to the Privacy Policy.",
               type_="boolean", role="checkbox"),
        _field("resume", "Resume / CV", type_="input_file", role="button"),
        _field("q_opt_1", "Anything else you want to add?", required=False),
        _field("q_opt_2", "Favourite colour?", required=False),
        _field("q_opt_3", "Favourite programming language?", required=False),
        _field("q_opt_4", "Preferred pronoun?", required=False),
        _field("q_opt_5", "Referral source?", required=False),
        _field("q_opt_6", "Anything unusual about you?", required=False),
    )
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot), assets=assets)

    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(_control_page(values.fields)()),
                       fieldmap=fm, assets=assets, artifacts_dir=tmp_path,
                       now=lambda: _PINNED)

    assert report.fillable_total == 10
    assert report.filled == 3
    assert len(report.required_unfilled) == 1
    assert report.required_unfilled[0]["key"] == "resume"
    assert report.complete is False
    assert (report.caption()
            == "Greenhouse (acme): 3/10 fields filled, "
               "1 required unfilled - NOT COMPLETE")


# =============================================================================
# READBACK-GATED COMPLETENESS (the integrity fix): a field only counts as
# filled once its readback CONFIRMS the value landed; a required field the
# page silently rejected -- text/select mismatch or a swallowed upload -- must
# force NOT COMPLETE rather than silently reading COMPLETE.
# =============================================================================

def test_readback_never_confirms_a_blank_intended_value_even_if_control_reads_blank():
    # FIX 2, the readback-boundary guard (defence in depth on top of the
    # resolve-layer skip): an empty/whitespace INTENDED value must never read
    # as a confirmed fill just because the control ALSO reads back empty --
    # `fill`/`type_human` never wrote anything (type_human early-returns on
    # empty text), so "both sides are blank" is not a match, it is "nothing
    # happened". Exercised at the `_readback` function boundary directly so
    # the guard is proven to apply to every provider sharing it.
    from engine.fill import _readback

    blank_control = _FakeLocator()          # input_value() defaults to ""
    actual, ok = _readback(blank_control, "   ")
    assert ok is False
    actual, ok = _readback(blank_control, "")
    assert ok is False
    # Non-blank intent still confirms normally (no regression).
    filled_control = _FakeLocator()
    filled_control.fill("Test Candidate")
    actual, ok = _readback(filled_control, "Test Candidate")
    assert ok is True


def test_readback_mismatch_required_text_field_forces_not_complete(tmp_path):
    mutated = _FakeLocator(readback="Someone Else")
    fields = [_fv("first_name", "First Name", "input_text", "Test Candidate")]
    page = _FakePage(url="https://x/1/apply", controls={"First Name": mutated})
    fm = _fieldmap(_field("first_name", "First Name"))

    report = fill_form("greenhouse", "acme", "1", ResolvedValues(fields=fields),
                       browser_factory=_factory_for(page), fieldmap=fm,
                       artifacts_dir=tmp_path, now=lambda: _PINNED)

    assert report.filled == 0
    assert len(report.readback_mismatches) == 1
    assert report.required_unfilled == [{
        "key": "first_name", "label": "First Name",
        "reason": "value did not take (readback mismatch)"}]
    assert report.complete is False
    assert report.caption().endswith("NOT COMPLETE")


def test_readback_mismatch_required_select_forces_not_complete(tmp_path):
    # A React-combobox-like control that swallows the selection: input_value()
    # comes back empty even though select_option() was called.
    swallowed = _FakeLocator(readback="")
    label = "Do you require visa sponsorship?"
    fields = [_fv("q_visa", label, "multi_value_single_select", "No",
                  role="combobox")]
    page = _FakePage(url="https://x/1/apply", controls={label: swallowed})
    fm = _fieldmap(_field("q_visa", label, type_="multi_value_single_select",
                          options=["Yes", "No"], role="combobox"))

    report = fill_form("greenhouse", "acme", "1", ResolvedValues(fields=fields),
                       browser_factory=_factory_for(page), fieldmap=fm,
                       artifacts_dir=tmp_path, now=lambda: _PINNED)

    assert report.filled == 0
    assert len(report.readback_mismatches) == 1
    assert report.required_unfilled == [{
        "key": "q_visa", "label": label,
        "reason": "value did not take (readback mismatch)"}]
    assert report.complete is False


def test_required_upload_that_does_not_attach_forces_not_complete(
        tmp_path, real_ssot_path):
    # The file input reports set_input_files was called, but readback shows
    # zero files attached (e.g. a custom widget never wired the native
    # input): the upload must NOT count as filled, and a required resume
    # left unattached must force NOT COMPLETE.
    ssot = SSOT.load(real_ssot_path)
    assets = _make_assets(tmp_path)
    fm = _fieldmap(_field("resume", "Resume / CV", type_="input_file",
                          role="button"))
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot), assets=assets)
    resume_input = _FakeFileInput(id="resume", accept=".pdf", readback="")
    page = _control_page(values.fields, file_inputs=[resume_input])()

    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(page), fieldmap=fm,
                       assets=assets, artifacts_dir=tmp_path, now=lambda: _PINNED)

    assert resume_input.set_input_files_calls == 1
    assert report.uploads == []
    assert report.filled == 0
    assert report.required_unfilled == [{
        "key": "resume", "label": "Resume / CV",
        "reason": "upload did not attach (readback)"}]
    assert report.complete is False


def test_readback_confirmed_fill_and_upload_stays_complete(
        tmp_path, real_ssot_path):
    # Happy-path regression: readback confirms the text value and the upload
    # attach, so both count toward filled and the report stays COMPLETE.
    ssot = SSOT.load(real_ssot_path)
    assets = _make_assets(tmp_path)
    fm = _fieldmap(
        _field("first_name", "First Name"),
        _field("resume", "Resume / CV", type_="input_file", role="button"),
    )
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot), assets=assets)

    report = fill_form("lever", "globex", "req-77", values,
                       browser_factory=_factory_for(_control_page(values.fields)()),
                       fieldmap=fm, assets=assets, artifacts_dir=tmp_path,
                       now=lambda: _PINNED)

    assert report.filled == 2
    assert report.required_unfilled == []
    assert report.complete is True
    assert report.caption().endswith("COMPLETE")
    assert not report.caption().endswith("NOT COMPLETE")


def test_readback_mismatch_optional_field_excluded_but_not_a_gap(tmp_path):
    # An OPTIONAL field whose value did not take is excluded from filled (the
    # X/Y denominator stays honest) but must NOT force NOT COMPLETE, and it is
    # still recorded in readback_mismatches for the evidence trail.
    mutated = _FakeLocator(readback="Someone Else")
    fields = [_fv("nickname", "Nickname", "input_text", "Robbo")]
    page = _FakePage(url="https://x/1/apply", controls={"Nickname": mutated})
    fm = _fieldmap(_field("nickname", "Nickname", required=False))

    report = fill_form("greenhouse", "acme", "1", ResolvedValues(fields=fields),
                       browser_factory=_factory_for(page), fieldmap=fm,
                       artifacts_dir=tmp_path, now=lambda: _PINNED)

    assert report.filled == 0
    assert report.required_unfilled == []
    assert len(report.readback_mismatches) == 1
    assert report.complete is True


# =============================================================================
# Evidence publisher (criterion 4) + toto asset defaults (criterion 5)
# =============================================================================

def test_publish_evidence_sends_screenshot_captioned():
    from engine.notify import FakeTransport
    report = _report(vendor="ashby", company="initech", fillable_total=4,
                     filled=4, required_unfilled=[], justified_skips=0,
                     screenshot="/artifacts/fill-ashby-9.png")
    transport = FakeTransport()
    publish_evidence(report, "abe-jobsearch", transport)
    assert transport.sent_files == [(
        "abe-jobsearch", "/artifacts/fill-ashby-9.png",
        "Ashby (initech): 4/4 fields filled, 0 required unfilled - COMPLETE",
        "fill-ashby-9.png")]


def test_resolve_photo_globs_case_insensitively(tmp_path):
    root = tmp_path / "career-archive"
    pics = root / "weird" / "PROFILE PICS"
    pics.mkdir(parents=True)
    (pics / "ME.JPG").write_bytes(b"stub")
    (pics / "ME.PNG").write_bytes(b"stub")
    got = _resolve_photo(archive_root=root)
    assert got is not None
    assert got.name == "ME.PNG"               # png preferred over jpg


def test_resolve_photo_override_wins(tmp_path):
    override = tmp_path / "custom.png"
    override.write_bytes(b"stub")
    assert _resolve_photo(override=str(override)) == override


def test_resolve_photo_missing_root_is_none(tmp_path):
    assert _resolve_photo(archive_root=tmp_path / "nope") is None


def test_default_assets_fail_soft_when_absent(tmp_path):
    assets = default_assets(documents_dir=tmp_path / "docs",
                            archive_root=tmp_path / "arch")
    assert assets.cv_ats is None
    assert assets.cv_atsi is None
    assert assets.photo is None


def _report(*, vendor, company, fillable_total, filled, required_unfilled,
            justified_skips, screenshot="/x.png"):
    return FillReport(
        vendor=vendor, company=company, posting_id="1",
        fillable_total=fillable_total, filled=filled,
        required_unfilled=required_unfilled, justified_skips=justified_skips,
        uploads=[], skipped=[], readback_mismatches=[], validation_errors=[],
        url_unchanged=True, screenshot=screenshot, ts=_PINNED)


# =============================================================================
# W4 4d live-dry-run fixes: file-input location (1) + consent checkboxes (2) +
# yes/no select normalization (3)
# =============================================================================

def _upload_fv(key, label, asset, value, *, role="button"):
    from engine.fill import FieldValue
    return FieldValue(key=key, label=label, type="input_file",
                      locator=Locator(role=role, name=label), value=value,
                      asset=asset, upload_reason="test")


def _page_with_file_inputs(*inputs):
    return _FakePage(url="https://x/1/apply", file_inputs=list(inputs))


# --- (1) locate the real <input type=file>, not the fieldmap button locator ---

def test_locate_file_input_prefers_key_stem_over_accept(tmp_path):
    # A Greenhouse-style id=resume input wins by key stem even when a decoy
    # doc-accept input precedes it.
    decoy = _FakeFileInput(id="cover_letter", accept=".pdf,.doc")
    resume = _FakeFileInput(id="resume", accept=".pdf,.doc,.docx,.txt,.rtf")
    page = _page_with_file_inputs(decoy, resume)
    fv = _upload_fv("resume", "Resume/CV", "cv-ats", tmp_path / "cv-ats.pdf")
    assert _locate_file_input(page, fv) is resume


def test_locate_file_input_lever_style_id_and_name(tmp_path):
    # Lever: id="resume-upload-input" name="resume", no accept -> stem "resume".
    lever = _FakeFileInput(id="resume-upload-input", name="resume")
    page = _page_with_file_inputs(lever)
    fv = _upload_fv("resume", "Resume / CV", "cv-ats", tmp_path / "cv-ats.pdf")
    assert _locate_file_input(page, fv) is lever


def test_locate_file_input_cv_falls_back_to_doc_accept(tmp_path):
    # No stem match -> a CV takes the first input whose accept is a document type.
    generic = _FakeFileInput(id="file-1", accept="application/pdf,.docx")
    page = _page_with_file_inputs(generic)
    fv = _upload_fv("resume", "Upload document", "cv-ats", tmp_path / "cv-ats.pdf")
    assert _locate_file_input(page, fv) is generic


def test_locate_file_input_cv_accepts_input_without_accept(tmp_path):
    generic = _FakeFileInput(id="file-1")            # no accept attribute
    page = _page_with_file_inputs(generic)
    fv = _upload_fv("resume", "Upload document", "cv-ats", tmp_path / "cv-ats.pdf")
    assert _locate_file_input(page, fv) is generic


def test_locate_file_input_photo_needs_image_accept(tmp_path):
    # A photo skips the document input and lands on the image-accept input.
    doc = _FakeFileInput(id="file-1", accept=".pdf")
    img = _FakeFileInput(id="avatar", accept="image/png,image/jpeg")
    page = _page_with_file_inputs(doc, img)
    fv = _upload_fv("headshot", "Headshot", "photo", tmp_path / "Me.png")
    assert _locate_file_input(page, fv) is img


def test_locate_file_input_photo_skipped_when_no_image_input(tmp_path):
    doc = _FakeFileInput(id="file-1", accept=".pdf")
    page = _page_with_file_inputs(doc)
    fv = _upload_fv("headshot", "Headshot", "photo", tmp_path / "Me.png")
    assert _locate_file_input(page, fv) is None


def test_locate_file_input_none_when_no_file_inputs(tmp_path):
    page = _FakePage(url="https://x/1/apply")
    fv = _upload_fv("resume", "Resume", "cv-ats", tmp_path / "cv-ats.pdf")
    assert _locate_file_input(page, fv) is None


def test_cv_lands_on_greenhouse_resume_input_via_set_input_files(
        tmp_path, real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    assets = _make_assets(tmp_path)
    fm = _fieldmap(_field("resume", "Resume/CV", type_="input_file",
                          role="button"))
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot), assets=assets)
    resume_input = _FakeFileInput(id="resume",
                                  accept=".pdf,.doc,.docx,.txt,.rtf")
    page = _control_page(values.fields, file_inputs=[resume_input])()
    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(page), fieldmap=fm,
                       assets=assets, artifacts_dir=tmp_path, now=lambda: _PINNED)
    assert resume_input.set_input_files_calls == 1
    assert resume_input.uploaded == str(assets.cv_ats)
    assert resume_input.clicks == 0                  # direct upload, no click
    assert report.uploads[0]["key"] == "resume"
    assert report.complete is True


def test_cv_lands_on_lever_resume_upload_input(tmp_path, real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    assets = _make_assets(tmp_path)
    fm = _fieldmap(_field("resume", "Resume / CV", type_="input_file",
                          role="button"), vendor="lever")
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot), assets=assets)
    decoy = _FakeFileInput(id="avatar", accept="image/png")
    lever_input = _FakeFileInput(id="resume-upload-input", name="resume")
    page = _control_page(values.fields, file_inputs=[decoy, lever_input])()
    fill_form("lever", "globex", "req-77", values,
              browser_factory=_factory_for(page), fieldmap=fm, assets=assets,
              artifacts_dir=tmp_path, now=lambda: _PINNED)
    assert lever_input.uploaded == str(assets.cv_ats)
    assert decoy.set_input_files_calls == 0          # image input untouched


def test_photo_lands_on_image_accept_input(tmp_path, real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    assets = _make_assets(tmp_path)
    fm = _fieldmap(_field("headshot", "Headshot", type_="input_file",
                          role="button"))
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot), assets=assets)
    resume_input = _FakeFileInput(id="cv", accept=".pdf,.doc")
    photo_input = _FakeFileInput(id="pic", accept="image/png,image/jpeg")
    page = _control_page(values.fields,
                         file_inputs=[resume_input, photo_input])()
    fill_form("greenhouse", "acme", "1", values,
              browser_factory=_factory_for(page), fieldmap=fm, assets=assets,
              artifacts_dir=tmp_path, now=lambda: _PINNED)
    assert photo_input.uploaded == str(assets.photo)
    assert Path(photo_input.uploaded).name == "Me.png"
    assert resume_input.set_input_files_calls == 0


def test_required_resume_with_no_file_input_stays_required_unfilled(
        tmp_path, real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    assets = _make_assets(tmp_path)
    fm = _fieldmap(
        _field("first_name", "First Name"),
        _field("resume", "Resume / CV", type_="input_file", role="button"),
    )
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot), assets=assets)
    page = _control_page(values.fields, file_inputs=[])()      # no file inputs
    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(page), fieldmap=fm,
                       assets=assets, artifacts_dir=tmp_path, now=lambda: _PINNED)
    assert dict(report.skipped)["resume"] == "no file input located"
    assert [r["key"] for r in report.required_unfilled] == ["resume"]
    assert report.complete is False


def test_upload_still_rejects_non_whitelisted_path(tmp_path):
    # Safety regression: even with a real file input on the page, an arbitrary
    # (non-asset) path is refused by the whitelist.
    good = tmp_path / "cv-ats.pdf"
    good.write_bytes(b"stub")
    assets = FillAssets(cv_ats=good)
    resume_input = _FakeFileInput(id="resume", accept=".pdf")
    with pytest.raises(FillSafetyError, match="whitelist"):
        _safe_upload(resume_input, tmp_path / "evil.pdf", assets)
    assert resume_input.set_input_files_calls == 0


# --- (2) consent / talent-pool / marketing checkboxes -------------------------

def test_consent_please_confirm_is_ticked(real_ssot_path):
    # The live Greenhouse "Please confirm the following:" required box.
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field("q_confirm", "Please confirm the following:",
                          type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, ssot, profile_from_real_ssot(ssot))
    assert resolved.values["q_confirm"] is True
    assert resolved.skipped == []


def test_consent_ticked_even_when_coverage_classifier_misses_path(
        real_ssot_path):
    # The live mechanism: a consent box whose SSOT matcher path is absent used
    # to classify MISSING and be skipped. The label-based checkbox classifier
    # now ticks it True from the SSOT's ratified consent answer, independent of
    # the coverage matcher.
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field("q_ack", "I acknowledge the data processing terms.",
                          type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, ssot, profile_from_real_ssot(ssot))
    assert resolved.values["q_ack"] is True


def test_talent_pool_checkbox_is_ticked_true(real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field(
        "q_pool", "Add me to your talent pool for future opportunities",
        type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, ssot, profile_from_real_ssot(ssot))
    assert resolved.values["q_pool"] is True


def test_marketing_checkbox_left_unticked(real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field("q_news", "Subscribe me to the monthly newsletter",
                          type_="boolean", role="checkbox", required=False))
    resolved = resolve_values(fm, ssot, profile_from_real_ssot(ssot))
    assert "q_news" not in resolved.values
    assert (dict(resolved.skipped)["q_news"]
            == "marketing/newsletter checkbox left unticked")


def test_marketing_wording_beats_consent_wording(real_ssot_path):
    # "I agree to receive marketing emails" carries a consent verb but is a
    # marketing opt-in -> left unticked (never opted into).
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field("q_mkt", "I agree to receive marketing emails",
                          type_="boolean", role="checkbox", required=False))
    resolved = resolve_values(fm, ssot, profile_from_real_ssot(ssot))
    assert "q_mkt" not in resolved.values


def test_fill_ticks_consent_via_check_leaves_marketing(tmp_path, real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(
        _field("q_consent", "I agree to the Privacy Policy.",
               type_="boolean", role="checkbox"),
        _field("q_news", "Subscribe to our newsletter",
               type_="boolean", role="checkbox", required=False),
    )
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot))
    consent = _FakeLocator()
    news = _FakeLocator()
    page = _FakePage(url="https://x/1/apply",
                     controls={"I agree to the Privacy Policy.": consent,
                               "Subscribe to our newsletter": news})
    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(page), fieldmap=fm,
                       artifacts_dir=tmp_path, now=lambda: _PINNED)
    assert consent.checked is True          # ticked via the safe check() path
    assert consent.clicks == 0              # no deny-listed click
    assert news.checked is None             # marketing box never touched
    assert news.clicks == 0
    assert (dict(report.skipped)["q_news"]
            == "marketing/newsletter checkbox left unticked")


# --- (3) yes/no select normalization ------------------------------------------

def test_yesno_authorization_resolves_yes(real_ssot_path):
    # SSOT work_authorization is EU prose (no literal "Yes") -> normalized Yes.
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field(
        "q_auth", "Are you legally authorized to work in the EU?",
        type_="multi_value_single_select", options=["Yes", "No"],
        role="combobox"))
    resolved = resolve_values(fm, ssot, profile_from_real_ssot(ssot))
    assert resolved.values["q_auth"] == "Yes"
    assert resolved.skipped == []


def test_yesno_authorization_no_region_resolves_yes(real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field(
        "q_auth", "Are you legally authorized to work?",
        type_="multi_value_single_select", options=["Yes", "No"],
        role="combobox"))
    resolved = resolve_values(fm, ssot, profile_from_real_ssot(ssot))
    assert resolved.values["q_auth"] == "Yes"


def test_yesno_sponsorship_resolves_no_via_phrase_options(real_ssot_path):
    # Phrase options mean no exact match; the negative sponsorship answer maps
    # to the "No, ..." option.
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field(
        "q_visa", "Will you now or in the future require visa sponsorship?",
        type_="multi_value_single_select",
        options=["Yes, I require sponsorship", "No, I do not require sponsorship"],
        role="combobox"))
    resolved = resolve_values(fm, ssot, profile_from_real_ssot(ssot))
    assert resolved.values["q_visa"] == "No, I do not require sponsorship"
    assert resolved.skipped == []


def test_yesno_us_authorization_is_skipped_and_required_unfilled(
        tmp_path, real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(
        _field("first_name", "First Name"),
        _field("q_auth_us",
               "Are you legally authorized to work in the United States?",
               type_="multi_value_single_select", options=["Yes", "No"],
               role="combobox"),
    )
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot))
    assert "q_auth_us" not in values.values
    reason = dict(values.skipped)["q_auth_us"]
    assert "region-ambiguous" in reason
    assert "questionnaire" in reason

    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(_control_page(values.fields)()),
                       fieldmap=fm, artifacts_dir=tmp_path, now=lambda: _PINNED)
    assert [r["key"] for r in report.required_unfilled] == ["q_auth_us"]
    assert report.complete is False


def test_yesno_us_sponsorship_is_skipped_not_guessed(real_ssot_path):
    # The dangerous case: SSOT visa_sponsorship_required == "no" would exact-match
    # the "No" option, but for a US posting the EU candidate DOES need US
    # sponsorship -> the region gate must skip rather than answer "No".
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field(
        "q_visa_us", "Do you require visa sponsorship to work in the US?",
        type_="multi_value_single_select", options=["Yes", "No"],
        role="combobox"))
    resolved = resolve_values(fm, ssot, profile_from_real_ssot(ssot))
    assert "q_visa_us" not in resolved.values
    assert "region-ambiguous" in dict(resolved.skipped)["q_visa_us"]
