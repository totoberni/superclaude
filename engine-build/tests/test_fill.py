"""Form-fill DRY RUN: deterministic value resolution + safe fill (W4 4b).

No playwright, no network: fill_form is driven through fake page/locator objects
over hand-built field maps, so the autouse no-network guard holds throughout.
The safety invariants (no submit path, URL-unchanged, no file touch, EEO never
touched) are each asserted directly. resolve_values is pure keyword matching
against the synthetic v1.4 SSOT; every verdict is asserted explicitly.

Scope: fill_form here is the relocated test-only harness (tests/kernel/_harness.py),
which wires the real kernel primitives (_readback, _completeness, _locate,
_safe_upload, _registry.apply_url) but reimplements the per-vendor fill()
orchestration glue. These are kernel-primitive integration tests, not production
fill() orchestration tests: green here does not by itself prove the per-vendor
orchestration is safe. That is covered separately per vendor (see
test_providers_greenhouse.py).
"""

import contextlib
from pathlib import Path

import pytest

from tests.kernel._harness import fill_form  # relocated test-only harness (engine.fill deleted, W5.1a Stage 5)
from engine.kernel.contracts import (
    Field,
    FieldMap,
    FillAssets,
    FillReport,
    FillSafetyError,
    Locator,
    ResolvedValues,
)
from engine.kernel.fill_toolkit import (
    _locate_file_input,
    _safe_click,
    _safe_upload,
)
from engine.kernel.resolve import (
    _DECLINE_SECTIONS,
    _IN_OFFICE_RE,
    _SPONSOR_NEEDED_ASSERT_RE,
    _classify_checkbox,
    _detect_onsite_cadence,
    _is_cover_letter_field,
    _is_photo_field,
    _sponsorship_assertion_polarity,
    resolve_values,
)
from engine.providers.greenhouse.capture import _SECTION_FOR_SOURCE
from engine.providers.greenhouse.resolve import GREENHOUSE_WIDGET_RESOLVER
from engine.profile_map import profile_from_real_ssot
from engine.ssot import SSOT
from engine.kernel.ssot import MISSING

_PINNED = "2026-07-03T00:00:00+00:00"
_NO_OVERRIDE = object()  # sentinel: no readback override forced (input_value()
                         # for a text/select control, file_count for a file input)


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

    `evaluate` mirrors the real `<input type=file>`'s `el.files.length`
    signal `_upload_attached` reads (NEVER `input_value()`: Greenhouse's live
    DOM proves that reads back EMPTY regardless of whether a file attached,
    so it cannot be the success signal -- see `_upload_attached`'s docstring
    in fill.py). It reports 1 file attached once `set_input_files` has been
    called, unless `file_count` forces a specific count (e.g. 0, to simulate
    a custom widget that silently swallowed the upload without ever wiring
    the native input's `files` FileList)."""

    def __init__(self, *, id=None, name=None, accept=None,
                 file_count=_NO_OVERRIDE):
        self._attrs = {"id": id, "name": name, "accept": accept}
        self._file_count_override = file_count
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

    def evaluate(self, expression, arg=None):
        if self._file_count_override is not _NO_OVERRIDE:
            return self._file_count_override
        return 1 if self.uploaded else 0


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

def test_fill_detects_readback_mismatch():
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
                       now=lambda: _PINNED)

    assert report.filled == 0
    assert len(report.readback_mismatches) == 1
    mismatch = report.readback_mismatches[0]
    assert mismatch["key"] == "first_name"
    assert mismatch["intended"] == "Test Candidate"
    assert mismatch["actual"] == "Someone Else"


def test_fill_report_json_serialises():
    import json

    fields = [_fv("first_name", "First Name", "input_text", "Test Candidate")]
    page = _FakePage(url="https://x/1/apply",
                     controls={"First Name": _FakeLocator()})
    report = fill_form("greenhouse", "acme", "5501001",
                       ResolvedValues(fields=fields, skipped=[("resume",
                                                               "file-upload")]),
                       browser_factory=_factory_for(page),
                       now=lambda: _PINNED)

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


def test_fill_never_touches_file_inputs(real_ssot_path):
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
                       now=lambda: _PINNED)

    # the file control was never looked up nor uploaded to
    assert ("label", "Resume / CV") not in page.requested
    assert ("role", "button", "Resume / CV") not in page.requested
    assert file_control.set_input_files_calls == 0
    assert dict(report.skipped)["resume"] == "file-upload"


def test_fill_leaves_eeo_fields_untouched(real_ssot_path):
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
              browser_factory=_factory_for(page),
              now=lambda: _PINNED)

    assert ("role", "combobox", "Gender") not in page.requested
    assert ("label", "Gender") not in page.requested
    assert eeo_control.filled is None
    assert eeo_control.selected is None
    assert dict(values.skipped)["demographic_gender"] == "demographic/EEO"


def test_fill_raises_when_url_changes():
    # A control whose interaction navigates the page must trip the
    # URL-unchanged invariant (possible submission/redirect). Covers the
    # HARNESS url_unchanged raise branch (tests/kernel/_harness.py:131-135)
    # directly; the production-path invariant is covered per-vendor
    # (greenhouse test_providers_greenhouse.py:927).
    navigating = _FakeLocator()
    page = _FakePage(url="https://jobs.lever.co/globex/req-77/apply",
                     controls={"First Name": navigating})
    navigating._page = page
    navigating._navigate_to = "https://jobs.lever.co/globex/req-77/thanks"
    fields = [_fv("first_name", "First Name", "input_text", "Test Candidate")]

    with pytest.raises(FillSafetyError, match="navigated during fill"):
        fill_form("lever", "globex", "req-77", ResolvedValues(fields=fields),
                  browser_factory=_factory_for(page))


# --- resolve_values -> fill_form integration ---------------------------------

def test_resolve_then_fill_end_to_end(real_ssot_path):
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
                       now=lambda: _PINNED)

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
    # greenhouse-flavored: resume_text/cover_letter_text are Greenhouse portal
    # key-text widgets that only GREENHOUSE_WIDGET_RESOLVER resolves.
    values = resolve_values(fm, ssot, {}, assets=assets,
                            vendor_resolver=GREENHOUSE_WIDGET_RESOLVER)

    assert values.values["resume_text"] == (
        "Ada Lovelace, mathematician and writer.")
    assert values.values["cover_letter_text"] == "Dear hiring manager, ..."
    assert "resume_text" not in dict(values.skipped)

    build = _control_page(values.fields,
                          url="https://boards.greenhouse.io/acme/jobs/1")
    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(build()),
                       fieldmap=fm, assets=assets,
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
    from engine.kernel.contracts import FieldValue
    return FieldValue(key=key, label=label, type=type_,
                      locator=Locator(role=role, name=label), value=value)


# =============================================================================
# W4 4c owner refinements: CV upload (2) + profile picture (3)
# =============================================================================

def _make_assets(tmp_path, *, ats=True, atsi=True, photo=True,
                 cover_letter=False):
    """Build a FillAssets over real temp files; absent legs point at a
    non-existent path so `verified()` collapses them to None.

    `cover_letter` defaults False (unlike the other legs): most existing
    callers rely on the cover-letter asset being ABSENT to exercise the
    justified-skip path, so a caller must opt in explicitly to get a real
    cover-letter document."""
    def make(name, present):
        p = tmp_path / name
        if present:
            p.write_bytes(b"stub")
        return p
    return FillAssets(cv_ats=make("cv-ats.pdf", ats),
                      cv_atsi=make("cv-atsi.pdf", atsi),
                      photo=make("Me.png", photo),
                      cover_letter=make("cover-letter.pdf", cover_letter))


def test_cv_atsi_when_no_photo_field(tmp_path, real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field("resume", "Resume", type_="input_file", role="button"))
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot),
                            assets=_make_assets(tmp_path))
    assert len(values.fields) == 1
    fv = values.fields[0]
    assert fv.asset == "cv-atsi"
    assert Path(fv.value).name == "cv-atsi.pdf"
    assert "no photo field" in fv.upload_reason


def test_cv_atsi_when_no_photo_field_regardless_of_posting_lang(
        tmp_path, real_ssot_path):
    # posting_lang no longer influences the CV choice (the owner-ratified rule
    # is purely form-structural): with no photo field the ATSI CV is uploaded
    # regardless of the posting language, so `posting_lang="it"` is incidental.
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field("resume", "Resume", type_="input_file", role="button"))
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot),
                            assets=_make_assets(tmp_path), posting_lang="it")
    fv = values.fields[0]
    assert fv.asset == "cv-atsi"
    assert Path(fv.value).name == "cv-atsi.pdf"
    assert "no photo field" in fv.upload_reason


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
    assert dict(values.skipped)["resume"] == "asset missing: cv-atsi"


def test_cover_letter_file_field_is_skipped_never_gets_the_cv(
        tmp_path, real_ssot_path):
    # Regression (live-confirmed on a real greenhouse run): a `resume` file
    # input (required) alongside a `cover_letter` file input (optional) must
    # NOT both resolve to the CV asset -- the cover letter field must be
    # skipped, never filled with `cv-atsi.pdf`.
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(
        _field("resume", "Resume", type_="input_file", role="button",
              required=True),
        _field("cover_letter", "Cover Letter", type_="input_file",
               role="button", required=False),
    )
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot),
                            assets=_make_assets(tmp_path))

    by_key = {fv.key: fv for fv in values.fields}
    assert "resume" in by_key
    assert by_key["resume"].asset == "cv-atsi"
    assert Path(by_key["resume"].value).name == "cv-atsi.pdf"

    assert "cover_letter" not in by_key
    skip_reason = dict(values.skipped)["cover_letter"]
    assert "cv-atsi" not in skip_reason
    assert skip_reason == (
        "optional cover-letter upload; no cover-letter document asset (cover "
        "letter is drafted per-posting in the manual flow)")


def test_cover_letter_file_field_uploads_the_cover_letter_asset_when_present(
        tmp_path, real_ssot_path):
    # This task: a real cover-letter document asset now exists, so a
    # `cover_letter` file field must upload it (tagged "cover-letter"),
    # never the CV and never a skip.
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(
        _field("resume", "Resume", type_="input_file", role="button",
              required=True),
        _field("cover_letter", "Cover Letter", type_="input_file",
               role="button", required=False),
    )
    assets = _make_assets(tmp_path, cover_letter=True)
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot),
                            assets=assets)

    by_key = {fv.key: fv for fv in values.fields}
    assert "cover_letter" not in dict(values.skipped)
    assert "cover_letter" in by_key
    fv = by_key["cover_letter"]
    assert fv.asset == "cover-letter"
    assert Path(fv.value).name == "cover-letter.pdf"
    assert "cover-letter document asset" in fv.upload_reason
    # The sibling resume field is unaffected: still the CV, never the
    # cover-letter document.
    assert by_key["resume"].asset == "cv-atsi"


def test_cover_letter_file_field_asset_missing_on_disk_is_skipped(
        tmp_path, real_ssot_path):
    # A cover_letter asset PATH is supplied but does not exist on disk:
    # verified() collapses it to None, so the field is skipped for the same
    # justified reason as the no-asset case.
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field("cover_letter", "Cover Letter", type_="input_file",
                          role="button", required=False))
    assets = FillAssets(cover_letter=tmp_path / "does-not-exist.pdf")
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot),
                            assets=assets)

    assert values.fields == []
    assert dict(values.skipped)["cover_letter"] == (
        "optional cover-letter upload; no cover-letter document asset (cover "
        "letter is drafted per-posting in the manual flow)")


@pytest.mark.parametrize("key,label", [
    ("cover_letter", "Cover Letter"),
    ("cover_letter_upload", "Upload your cover letter"),
    ("cover-letter", "cover-letter"),
    ("x", "Cover Letter"),
])
def test_cover_letter_field_detection_matches_key_or_label(key, label):
    assert _is_cover_letter_field(
        _field(key, label, type_="input_file", role="button"))


@pytest.mark.parametrize("key,label", [
    ("resume", "Resume"),
    ("resume", "Resume / CV"),
    ("avatar", "Profile picture"),
    ("headshot", "Headshot"),
])
def test_cover_letter_detection_does_not_misfire_on_resume_or_avatar(
        key, label):
    assert not _is_cover_letter_field(
        _field(key, label, type_="input_file", role="button"))


@pytest.mark.parametrize("label", [
    "Profile picture", "Headshot", "Upload your photo", "Profile image",
    "Foto", "Immagine del profilo", "La tua foto",
])
def test_photo_field_detection_incl_italian(label):
    assert _is_photo_field(_field("x", label, type_="input_file", role="button"))


def test_photo_field_detection_is_label_based_only():
    # `Field` (engine.kernel.contracts) carries no `accept` MIME attribute in
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
                       fieldmap=fm, assets=assets,
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
                       fieldmap=fm, assets=assets,
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
                       fieldmap=fm, assets=assets,
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
                       fieldmap=fm, assets=assets,
                       now=lambda: _PINNED)
    assert report.filled == 2                 # first_name + the uploaded CV
    assert report.uploads == [{"key": "resume", "asset": "cv-atsi",
                               "path": str(assets.cv_atsi),
                               "reason": "no photo field on the form; embedding "
                                         "the photo via the ATSI CV variant"}]
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
    # greenhouse-flavored: longitude/latitude are Greenhouse hidden portal
    # telemetry widgets that only GREENHOUSE_WIDGET_RESOLVER classifies as hidden.
    values = resolve_values(fm, ssot, profile_from_real_ssot(ssot), assets=assets,
                            vendor_resolver=GREENHOUSE_WIDGET_RESOLVER)
    # DELIBERATE (owner-ratified 2026-07-10): the gh-default completeness shim died
    # with engine.fill, so the resolver is injected into fill_form explicitly; only
    # GREENHOUSE_WIDGET_RESOLVER excludes longitude/latitude from the denominator.
    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(_control_page(values.fields)()),
                       fieldmap=fm, assets=assets,
                       vendor_resolver=GREENHOUSE_WIDGET_RESOLVER,
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
    assert dict(values.skipped)["resume"] == "asset missing: cv-atsi"

    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(_control_page(values.fields)()),
                       fieldmap=fm, assets=assets,
                       now=lambda: _PINNED)

    assert report.fillable_total == 2
    assert report.filled == 1                       # first_name only
    assert len(report.required_unfilled) == 1
    assert report.required_unfilled[0] == {
        "key": "resume", "label": "Resume / CV",
        "reason": "asset missing: cv-atsi"}
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
                       fieldmap=fm, assets=assets,
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
                       fieldmap=fm, assets=assets,
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
                       fieldmap=fm, assets=assets,
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
                       fieldmap=fm, assets=assets,
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
                       fieldmap=fm, assets=assets,
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
    from engine.kernel.fill_toolkit import _readback

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


def test_readback_mismatch_required_text_field_forces_not_complete():
    mutated = _FakeLocator(readback="Someone Else")
    fields = [_fv("first_name", "First Name", "input_text", "Test Candidate")]
    page = _FakePage(url="https://x/1/apply", controls={"First Name": mutated})
    fm = _fieldmap(_field("first_name", "First Name"))

    report = fill_form("greenhouse", "acme", "1", ResolvedValues(fields=fields),
                       browser_factory=_factory_for(page), fieldmap=fm,
                       now=lambda: _PINNED)

    assert report.filled == 0
    assert len(report.readback_mismatches) == 1
    assert report.required_unfilled == [{
        "key": "first_name", "label": "First Name",
        "reason": "value did not take (readback mismatch)"}]
    assert report.complete is False
    assert report.caption().endswith("NOT COMPLETE")


def test_readback_mismatch_required_select_forces_not_complete():
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
                       now=lambda: _PINNED)

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
    resume_input = _FakeFileInput(id="resume", accept=".pdf", file_count=0)
    page = _control_page(values.fields, file_inputs=[resume_input])()

    report = fill_form("greenhouse", "acme", "1", values,
                       browser_factory=_factory_for(page), fieldmap=fm,
                       assets=assets, now=lambda: _PINNED)

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
                       fieldmap=fm, assets=assets,
                       now=lambda: _PINNED)

    assert report.filled == 2
    assert report.required_unfilled == []
    assert report.complete is True
    assert report.caption().endswith("COMPLETE")
    assert not report.caption().endswith("NOT COMPLETE")


def test_readback_mismatch_optional_field_excluded_but_not_a_gap():
    # An OPTIONAL field whose value did not take is excluded from filled (the
    # X/Y denominator stays honest) but must NOT force NOT COMPLETE, and it is
    # still recorded in readback_mismatches for the evidence trail.
    mutated = _FakeLocator(readback="Someone Else")
    fields = [_fv("nickname", "Nickname", "input_text", "Robbo")]
    page = _FakePage(url="https://x/1/apply", controls={"Nickname": mutated})
    fm = _fieldmap(_field("nickname", "Nickname", required=False))

    report = fill_form("greenhouse", "acme", "1", ResolvedValues(fields=fields),
                       browser_factory=_factory_for(page), fieldmap=fm,
                       now=lambda: _PINNED)

    assert report.filled == 0
    assert report.required_unfilled == []
    assert len(report.readback_mismatches) == 1
    assert report.complete is True


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
    from engine.kernel.contracts import FieldValue
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
                       assets=assets, now=lambda: _PINNED)
    assert resume_input.set_input_files_calls == 1
    assert resume_input.uploaded == str(assets.cv_atsi)
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
              now=lambda: _PINNED)
    assert lever_input.uploaded == str(assets.cv_atsi)
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
              now=lambda: _PINNED)
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
                       assets=assets, now=lambda: _PINNED)
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


def _consent_policy_ssot(**overrides):
    """An SSOT seeded with the owner-ruled (2026-07-19) `policies.consent`
    subtree (RS-g R-1 schema). `canned_answers.optional_consents` is present so a
    consent SELECT is ANSWERABLE (reaches `_render_select`); per-class overrides
    let a test flip one verdict. Extra top-level keys (e.g. a region-keyed
    `work_authorization` for an assertion) are merged in via `extra`."""
    consent = {
        "application_privacy": "consent",
        "talent_pool": "decline",
        "marketing": "decline",
        "assessment": "consent",
        "ai_notetaker": "opt_out",
        "assertion": "truthful_only",
    }
    extra = overrides.pop("extra", {})
    consent.update(overrides)
    data = {"policies": {"consent": consent},
            "canned_answers": {"optional_consents": "yes, consents"}}
    data.update(extra)
    return SSOT(data)


def test_talent_pool_checkbox_not_ticked_under_decline_policy():
    # REVERSED (owner ruling 2026-07-19): the superseded talent-pool YES split is
    # retired. The live lever wording "...consent to contact me about future job
    # opportunities" (first-pass census wrongly consented it) is a future-contact
    # opt-in -> talent_pool -> policy decline -> NEVER ticked, even though the box
    # carries the verb "consent". This test formerly asserted `q_pool is True`.
    ssot = _consent_policy_ssot()
    fm = _fieldmap(_field(
        "q_pool",
        "Palantir Technologies has my consent to contact me about future job "
        "opportunities",
        type_="boolean", role="checkbox", required=False))
    resolved = resolve_values(fm, ssot, {})
    assert "q_pool" not in resolved.values
    assert "not ticked" in dict(resolved.skipped)["q_pool"]


def test_talent_pool_bare_wording_still_declines():
    # The explicit talent-pool wording also declines under the policy.
    ssot = _consent_policy_ssot()
    fm = _fieldmap(_field(
        "q_pool", "Add me to your talent pool for future opportunities",
        type_="boolean", role="checkbox", required=False))
    resolved = resolve_values(fm, ssot, {})
    assert "q_pool" not in resolved.values


def test_application_privacy_checkbox_ticked_under_consent_policy():
    # (b) An application-necessary privacy-notice box ticks when the policy says
    # consent (the policy IS the owner's ratification; no legacy SSOT answer
    # needed).
    ssot = _consent_policy_ssot()
    fm = _fieldmap(_field(
        "q_priv", "I have read and agree to the Privacy Notice.",
        type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, ssot, {})
    assert resolved.values["q_priv"] is True


def test_assessment_participation_checkbox_ticked_under_consent_policy():
    # (c) W1: an assessment-participation consent ("20 minute aptitude
    # assessment ... happy to do this?") ticks under policy consent.
    ssot = _consent_policy_ssot()
    fm = _fieldmap(_field(
        "q_assess",
        "This role includes a 20 minute aptitude assessment. Are you happy to "
        "do this?",
        type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, ssot, {})
    assert resolved.values["q_assess"] is True


def test_assertion_checkbox_false_claim_unchecked_by_name():
    # (d) W2: an ASSERTION checkbox is truthful_only. The owner is NOT
    # US-work-authorized (region-keyed work_authorization: us needs sponsorship),
    # so checking it would claim a false fact -> left unchecked with the
    # owner-ruled reason, recorded BY NAME. Never a false check.
    ssot = _consent_policy_ssot(extra={
        "work_authorization": {"us": {"sponsorship_required": True},
                               "eu": True}})
    fm = _fieldmap(_field(
        "q_usauth",
        "Are you legally authorized to work in the United States?",
        type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, ssot, {})
    assert "q_usauth" not in resolved.values
    reason = dict(resolved.skipped)["q_usauth"]
    assert "owner ruling 2026-07-19" in reason
    assert "never falsely checked" in reason


def test_assertion_checkbox_true_claim_is_ticked():
    # (d) The mirror: a provable-true assertion (EU work rights) IS ticked, via
    # the RS-b work-authorization machinery (region read from the label).
    ssot = _consent_policy_ssot(extra={
        "work_authorization": {"us": {"sponsorship_required": True},
                               "eu": True}})
    fm = _fieldmap(_field(
        "q_euauth",
        "Are you legally authorized to work in the European Union?",
        type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, ssot, {})
    assert resolved.values["q_euauth"] is True


# --- (2b) sponsorship assertion checkboxes: the INVERTED polarity -------------
# P2-2. Right-to-work and sponsorship-required are OPPOSITE claims: the owner's
# EU rights answer Yes to "authorized to work" and No to "require sponsorship".
# The yes/no SELECT path has always split them (`_select_intent` tests
# `_SPONSOR_INTENT_RE` first); the CHECKBOX classifier did not, so a
# sponsorship-phrased box (live workable rokt CA_12080) matched no consent class
# at all and parked with the generic non-consent reason. Routing it through the
# right-to-work truth INSTEAD would be far worse than the park it replaced: it
# would tick a box asserting the OPPOSITE of the truth on a legally significant
# question, which is the exact false fill this engine exists to prevent.

# The live workable rokt CA_12080 label, verbatim from the r7 census.
_LIVE_SPONSORSHIP_LABEL = (
    "Will you now or in the future require sponsorship to work for Rokt?")


def _sponsorship_assertion_ssot():
    """The consent-policy SSOT plus the SSOT facts a sponsorship assertion is
    checked against. `work_authorization` is deliberately the REGION-KEYED dict
    shape, which is the shape `_assertion_proven_true`'s right-to-work lookup
    actually consumes: seeded as prose it returns None for any label and the
    trap this section guards would be invisible. eu -> True is the owner's real
    position, so a sponsorship box misrouted to that truth WOULD be ticked.
    `canned_answers.visa_sponsorship_required` is the sponsorship truth, the
    same datum the sponsorship SELECT resolves from; `extra` REPLACES
    `canned_answers` wholesale, so the consent key is restated."""
    return _consent_policy_ssot(extra={
        "work_authorization": {"eu": True,
                               "us": {"sponsorship_required": True}},
        "canned_answers": {"optional_consents": "yes, consents",
                           "visa_sponsorship_required": "no"},
    })


def test_sponsorship_checkbox_is_never_ticked_on_eu_work_rights():
    # THE regression guard for P2-2's trap, built so that a naive fix REALLY
    # ticks: the label names the EU, and the region-keyed work_authorization
    # says eu -> authorized. Route this box through the RIGHT-TO-WORK truth (what
    # widening `_WORK_AUTH_INTENT_RE` would do) and it resolves True and gets
    # TICKED -- asserting the owner REQUIRES sponsorship, the opposite of the
    # truth, on a legally significant question. It must stay unticked. Asserted
    # as an explicit NEGATIVE.
    ssot = _sponsorship_assertion_ssot()
    fm = _fieldmap(_field(
        "q_spon_eu",
        "Will you now or in the future require visa sponsorship to work in the "
        "European Union?",
        type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, ssot, {"posting_location": "Milan, Italy"})
    assert "q_spon_eu" not in resolved.values
    assert resolved.values.get("q_spon_eu") is not True
    assert "never falsely checked" in dict(resolved.skipped)["q_spon_eu"]


def test_live_sponsorship_checkbox_is_attributed_to_its_real_class():
    # The live workable rokt CA_12080 symptom: it used to match no consent class
    # at all and park with the generic non-consent reason, so an answerable class
    # read as a policy hand-off. It is now sorted and dispositioned as the
    # assertion it is -- and, being a FALSE claim, still not ticked.
    ssot = _sponsorship_assertion_ssot()
    fm = _fieldmap(_field("CA_12080", _LIVE_SPONSORSHIP_LABEL,
                          type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, ssot, {})
    assert "CA_12080" not in resolved.values
    reason = dict(resolved.skipped)["CA_12080"]
    assert "never falsely checked" in reason
    assert reason != "non-consent checkbox not auto-checked in dry run"


def test_sponsorship_checkbox_ticks_only_the_truthful_polarity():
    # The mirror: a box asserting the owner does NOT require sponsorship is TRUE
    # under the same facts, so it ticks. The two phrasings differ only in
    # polarity and must resolve to opposite dispositions.
    ssot = _sponsorship_assertion_ssot()
    fm = _fieldmap(_field(
        "q_nospon",
        "I do not now or in the future require visa sponsorship to work in the "
        "European Union.",
        type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, ssot, {})
    assert resolved.values["q_nospon"] is True


def test_sponsorship_checkbox_with_no_readable_polarity_parks():
    # Fail closed: a sponsorship-intent label stating no requirement polarity is
    # not guessed in either direction.
    ssot = _sponsorship_assertion_ssot()
    fm = _fieldmap(_field("q_spon", "Visa sponsorship",
                          type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, ssot, {})
    assert "q_spon" not in resolved.values
    assert "never falsely checked" in dict(resolved.skipped)["q_spon"]


def test_sponsorship_checkbox_region_outside_the_ssot_still_parks():
    # The region gate, kept identical to the SELECT path's: the owner's EU facts
    # say nothing about US sponsorship, so even the polarity that WOULD tick on
    # EU facts stays honestly unfilled. The checkbox path must never be more
    # permissive than the select path.
    ssot = _sponsorship_assertion_ssot()
    fm = _fieldmap(_field(
        "q_us", "I do not require sponsorship to work in the United States.",
        type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, ssot, {})
    assert "q_us" not in resolved.values
    assert "never falsely checked" in dict(resolved.skipped)["q_us"]


def test_sponsorship_checkbox_parks_when_the_ssot_states_no_requirement():
    # No sponsorship datum in the SSOT: the claim is unproven either way, so the
    # box parks rather than borrowing the right-to-work answer.
    ssot = _consent_policy_ssot(extra={
        "work_authorization": "EU citizen; full EU work rights"})
    fm = _fieldmap(_field("CA_12080", _LIVE_SPONSORSHIP_LABEL,
                          type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, ssot, {})
    assert "CA_12080" not in resolved.values
    assert "never falsely checked" in dict(resolved.skipped)["CA_12080"]


def test_multi_sentence_negated_clause_does_not_flip_sponsorship_polarity():
    # THE hard case the polarity regex must survive: a multi-sentence sponsorship
    # checkbox whose FIRST clause is a negated statement about something ELSE.
    # "I do not need any relocation assistance" contains a negated require/need
    # verb, and if the negated-polarity regex is not anchored to the sponsorship
    # noun it reads that as "sponsorship NOT required" and TICKS the box -- which
    # asserts the owner REQUIRES sponsorship, the exact false legal fill this fix
    # prevents. Ticking must never happen here: the requirement verb governs
    # "relocation assistance", not sponsorship, so the sponsorship polarity is
    # affirmative ("Will you require visa sponsorship") and, being FALSE on EU
    # facts, the box stays unticked. Asserted as an explicit NEGATIVE.
    ssot = _sponsorship_assertion_ssot()
    fm = _fieldmap(_field(
        "q_multi",
        "I do not need any relocation assistance. Will you require visa "
        "sponsorship to work in the European Union?",
        type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, ssot, {"posting_location": "Milan, Italy"})
    assert resolved.values.get("q_multi") is not True
    assert "q_multi" not in resolved.values
    assert "never falsely checked" in dict(resolved.skipped)["q_multi"]


def test_consent_select_single_affirmative_option_is_picked():
    # (e) The live greenhouse question_37455721: a privacy-notice SELECT whose
    # sole option is a non-yes/no affirmative ("Acknowledge/Confirm") failed
    # "no option match". Under the application-privacy consent policy the single
    # affirmative option is now picked.
    ssot = _consent_policy_ssot()
    fm = _fieldmap(_field(
        "q_priv_select",
        "Please confirm that you have read and agree to the Recruitment Privacy "
        "Notice and Privacy Policy.",
        type_="multi_value_single_select", options=["Acknowledge/Confirm"]))
    resolved = resolve_values(fm, ssot, {})
    assert resolved.values["q_priv_select"] == "Acknowledge/Confirm"


def test_consent_select_not_picked_when_policy_declines():
    # The negative control: with application_privacy DECLINED, the same select
    # falls back to the honest "no option matches" skip (never auto-picked).
    ssot = _consent_policy_ssot(application_privacy="decline")
    fm = _fieldmap(_field(
        "q_priv_select",
        "Please confirm that you have read and agree to the Recruitment Privacy "
        "Notice and Privacy Policy.",
        type_="multi_value_single_select", options=["Acknowledge/Confirm"]))
    resolved = resolve_values(fm, ssot, {})
    assert "q_priv_select" not in resolved.values


def _live_shape_consent_ssot():
    """The LIVE toto SSOT shape for the consent classes (NOT the synthetic
    fixture): `canned_answers.privacy_consent_default` is seeded and
    `canned_answers.optional_consents` is ABSENT. This is the exact divergence
    that let `question_37455721` classify MISSING live (the consent matcher pointed
    ONLY at the absent `optional_consents`) while the offline test passed on the
    fixture key -- the fixture-friendlier trap F-2 names."""
    return SSOT({
        "policies": {"consent": {"application_privacy": "consent"}},
        "canned_answers": {
            "privacy_consent_default": (
                "Application-necessary privacy-policy / data-processing consents "
                "(required to submit): YES, always tick."),
        },
    })


def test_consent_select_answerable_via_privacy_consent_default_live_shape():
    # F-2 LIVE PATH: on the live SSOT (privacy_consent_default present,
    # optional_consents ABSENT) the greenhouse privacy select must still be
    # ANSWERABLE -> reach `_render_select` -> RS-g single-affirmative pick. Before
    # the matcher carried privacy_consent_default this classified MISSING and the
    # overlay then reported the live "no option match".
    ssot = _live_shape_consent_ssot()
    assert ssot.get("canned_answers.optional_consents") is MISSING  # the trap
    fm = _fieldmap(_field(
        "question_37455721",
        "Please confirm that you have read and agree to Canonical's Recruitment "
        "Privacy Notice and Privacy Policy.",
        type_="multi_value_single_select", options=["Acknowledge/Confirm"]))
    resolved = resolve_values(fm, ssot, {})
    assert resolved.values["question_37455721"] == "Acknowledge/Confirm"


_ATTESTATION_LABEL = (
    "During this application process I agree to use only my own words. I "
    "understand that plagiarism, the use of AI or other generated content will "
    "disqualify my application.")


def test_ai_attestation_select_never_auto_answered_under_consent_policy():
    # F-10 SAFETY: the AI-policy attestation is a Yes/No SELECT whose label also
    # carries "I agree", so it matches the consent matcher and (once answerable via
    # privacy_consent_default) would reach `_consent_select_option` and auto-pick
    # "Yes". It MUST fail closed: its polarity is a stance the owner did not make
    # (FX3 forbids it at generate time). Never auto-answered -> stays skipped ->
    # the content overlay routes it to the class-ii tos_forbidden verdict.
    ssot = _live_shape_consent_ssot()
    fm = _fieldmap(_field(
        "question_42878852", _ATTESTATION_LABEL,
        type_="multi_value_single_select", options=["Yes", "No"]))
    resolved = resolve_values(fm, ssot, {})
    assert "question_42878852" not in resolved.values


def test_ai_attestation_checkbox_never_auto_ticked_under_consent_policy():
    # The boolean twin: an AI-attestation CHECKBOX ("... I agree ... AI ... will
    # disqualify") classifies application_privacy via "agree" and would otherwise
    # tick True under the consent policy. It fails closed too (defense in depth,
    # the same predicate), recorded by name -- never a silent tick.
    ssot = _consent_policy_ssot()
    fm = _fieldmap(_field(
        "q_attest_cb", _ATTESTATION_LABEL, type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, ssot, {})
    assert "q_attest_cb" not in resolved.values
    assert "AI-policy attestation" in dict(resolved.skipped)["q_attest_cb"]


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


def test_fill_ticks_consent_via_check_leaves_marketing(real_ssot_path):
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
                       now=lambda: _PINNED)
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
        real_ssot_path):
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
                       fieldmap=fm, now=lambda: _PINNED)
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


# --- (4) leading-yes/no-token extraction fallback (live acceptance gaps) -----
# Many canned answers are full sentences ("No. I have no non-compete.",
# "Yes, I would relocate.") that must still map onto a bare Yes/No select
# option: `_render_select`/`_render_dict_value` extract the leading token as a
# FALLBACK after an exact option match fails. FAKE SSOT only, no owner PII.

def test_leading_no_sentence_fills_bare_no_option():
    # Gap #1 shape: employment-agreement/post-employment-restriction question,
    # answer is a full sentence starting "No.".
    ssot = SSOT({"canned_answers": {
        "post_employment_restrictions": "No. I have no non-compete."}})
    fm = _fieldmap(_field(
        "q_restrict",
        "Are you subject to any employment agreements and/or "
        "post-employment restrictions?",
        type_="multi_value_single_select", options=["Yes", "No"],
        role="combobox"))
    resolved = resolve_values(fm, ssot, {})

    assert resolved.values["q_restrict"] == "No"
    assert resolved.skipped == []


def test_leading_no_sentence_fills_bare_no_option_previously_worked():
    # Gap #2 shape: previously-worked-at-company question, same sentence shape.
    ssot = SSOT({"canned_answers": {
        "previously_worked_at_company": "No. I have never worked there."}})
    fm = _fieldmap(_field(
        "q_prev", "Have you previously worked at or consulted for Acme?",
        type_="multi_value_single_select", options=["Yes", "No"],
        role="combobox"))
    resolved = resolve_values(fm, ssot, {})

    assert resolved.values["q_prev"] == "No"
    assert resolved.skipped == []


def test_leading_yes_sentence_fills_bare_yes_option():
    ssot = SSOT({"canned_answers": {
        "post_employment_restrictions": "Yes, I would need to check my prior "
                                        "employment contract."}})
    fm = _fieldmap(_field(
        "q_restrict", "Are you subject to any post-employment restrictions?",
        type_="multi_value_single_select", options=["Yes", "No"],
        role="combobox"))
    resolved = resolve_values(fm, ssot, {})

    assert resolved.values["q_restrict"] == "Yes"


def test_leading_yes_sentence_is_skipped_when_only_specific_variants_exist():
    # No BARE "Yes" option -- only "Yes, <region>" variants -- so a leading
    # "Yes" sentence must never guess a specific variant; it stays honestly
    # skipped rather than fabricate a choice.
    ssot = SSOT({"canned_answers": {
        "post_employment_restrictions": "Yes, I would need to check my prior "
                                        "employment contract."}})
    fm = _fieldmap(_field(
        "q_restrict", "Are you subject to any post-employment restrictions?",
        type_="multi_value_single_select",
        options=["No", "Yes, in the EU", "Yes, in the UK"], role="combobox"))
    resolved = resolve_values(fm, ssot, {})

    assert "q_restrict" not in resolved.values
    reason = dict(resolved.skipped)["q_restrict"]
    assert "no option matches" in reason


def test_leading_no_sentence_still_fills_no_variant_option_when_bare_no_exists():
    # A leading "No" always wins even when the OTHER polarity only has
    # specific variants, since a bare negative reads the same regardless of
    # how the affirmative side is enumerated.
    ssot = SSOT({"canned_answers": {
        "post_employment_restrictions": "No. I have no non-compete."}})
    fm = _fieldmap(_field(
        "q_restrict", "Are you subject to any post-employment restrictions?",
        type_="multi_value_single_select",
        options=["No", "Yes, in the EU", "Yes, in the UK"], role="combobox"))
    resolved = resolve_values(fm, ssot, {})

    assert resolved.values["q_restrict"] == "No"


def test_sponsorship_region_dict_resolves_via_leading_token_extraction():
    # Gap #3 end to end: canned_answers.sponsorship_answer_by_region is a
    # DICT keyed by region; none of its sentence values exact-match an
    # enumerated option, but the EU sub-value's leading "No" token maps onto
    # the bare "No" option.
    ssot = SSOT({"canned_answers": {"sponsorship_answer_by_region": {
        "eu": "No, I have the right to work in the EU/EEA without sponsorship.",
        "uk": "Yes, I would require a Skilled Worker visa.",
        "us": "Yes, I would require an H-1B visa.",
    }}})
    fm = _fieldmap(_field(
        "q_visa",
        "Will you now or in the future require sponsorship for a visa to "
        "remain in your current location?",
        type_="multi_value_single_select",
        options=["No", "Yes, Netherlands (Highly Skilled Migrant)",
                "Yes, Ireland (Critical Skills)", "Yes, EU Blue Card"],
        role="combobox"))
    resolved = resolve_values(fm, ssot, {})

    assert resolved.values["q_visa"] == "No"
    assert resolved.skipped == []


def test_country_of_residence_fills_country_option_from_long_list():
    # Gap #4 end to end: identity.country resolves against a long
    # country-name option list; the full-address identity.current_location
    # (which matches no option in the list) is never consulted.
    ssot = SSOT({"identity": {
        "country": "Italy",
        "current_location": "12 Example Street, Testville, Italy"}})
    fm = _fieldmap(_field(
        "q_country", "What is your country of residence?",
        type_="multi_value_single_select",
        options=["France", "Germany", "Italy", "Spain", "Portugal"],
        role="combobox"))
    resolved = resolve_values(fm, ssot, {})

    assert resolved.values["q_country"] == "Italy"
    assert resolved.skipped == []


# ============================================================================ #
# RS-h: cross-domain canned mis-fill (compensation matcher hardening).
# ============================================================================ #
# Live defect (greenhouse canonical 4124053, 2026-07-18): a FREE-TEXT degree
# question "What was your bachelor's university degree result, or expected result
# if you have not yet graduated?" was filled with the compensation scalar
# "EUR 26,000 gross annual (RAL)" because the bare adjective "expected" matched
# "expected result". A compensation match now requires a compensation NOUN.

def test_degree_result_free_text_is_never_filled_from_compensation_scalar():
    ssot = SSOT({"preferences": {"comp_floor": "EUR 26,000 gross annual (RAL)"}})
    fm = _fieldmap(_field(
        "q_degree",
        "What was your bachelor's university degree result, or expected "
        "result if you have not yet graduated? Please state the GPA/grade."))
    resolved = resolve_values(fm, ssot, {})

    assert "q_degree" not in resolved.values
    assert "EUR 26,000 gross annual (RAL)" not in list(resolved.values.values())
    reason = dict(resolved.skipped)["q_degree"]
    assert reason.startswith("missing:")            # honest park, not a wrong fill


def test_legit_compensation_question_still_resolves_from_comp_floor():
    ssot = SSOT({"preferences": {"comp_floor": "EUR 47,000 gross annual (RAL)"}})
    fm = _fieldmap(_field(
        "q_comp", "What are your compensation expectations for the role?"))
    resolved = resolve_values(fm, ssot, {})

    assert resolved.values["q_comp"] == "EUR 47,000 gross annual (RAL)"
    assert resolved.skipped == []


def test_expected_salary_question_still_resolves_via_noun():
    # A salary question phrased with the (previously over-broad) "expected" still
    # matches, now anchored on the compensation NOUN "salary".
    ssot = SSOT({"preferences": {"comp_floor": "EUR 47,000 gross annual (RAL)"}})
    fm = _fieldmap(_field("q_sal", "What is your expected salary for this role?"))
    resolved = resolve_values(fm, ssot, {})

    assert resolved.values["q_sal"] == "EUR 47,000 gross annual (RAL)"


def test_bare_notice_verb_does_not_fill_notice_period_scalar():
    # Same failure mode as the compensation bug: the bare verb "notice" used to
    # land the "1 month" notice-period scalar in any free text carrying it.
    ssot = SSOT({"canned_answers": {"notice_period": "1 month"}})
    fm = _fieldmap(_field(
        "q_take_notice",
        "Is there anything you would like us to take notice of?"))
    resolved = resolve_values(fm, ssot, {})

    assert "q_take_notice" not in resolved.values
    assert "1 month" not in list(resolved.values.values())


def test_notice_period_question_still_resolves():
    ssot = SSOT({"canned_answers": {"notice_period": "1 month"}})
    fm = _fieldmap(_field("q_np", "What is your current notice period?"))
    resolved = resolve_values(fm, ssot, {})

    assert resolved.values["q_np"] == "1 month"


# ============================================================================ #
# RS-b: work_authorization mapping -> scalar by posting country.
# ============================================================================ #
# Live defect (lever palantir, 2026-07-18): "Are you legally authorized to work
# in the country for which you are applying?" failed with "work_authorization
# resolved to a mapping with no usable scalar" -- the region-keyed mapping was
# scanned blindly. The resolver now selects the region entry by posting country.

def _work_auth_ssot():
    # Structured region-keyed mapping (profile_map's shape): each region carries
    # a sponsorship_required fact. Authorized-to-work = NOT needing sponsorship.
    return SSOT({"work_authorization": {
        "eu": {"sponsorship_required": False},
        "uk": {"sponsorship_required": True},
        "us": {"sponsorship_required": True},
    }})


def _work_auth_field():
    return _fieldmap(_field(
        "q_auth",
        "Are you legally authorized to work in the country for which you are "
        "applying?",
        type_="multi_value_single_select", options=["Yes", "No"],
        role="combobox"))


def test_work_auth_mapping_resolves_no_for_us_posting():
    fm = _work_auth_field()
    resolved = resolve_values(fm, _work_auth_ssot(),
                              {"posting_location": "New York, United States"})
    # US needs sponsorship -> not authorized -> "No" (never the bare mapping).
    assert resolved.values["q_auth"] == "No"
    assert resolved.skipped == []


def test_work_auth_mapping_resolves_no_for_uk_posting():
    fm = _work_auth_field()
    resolved = resolve_values(fm, _work_auth_ssot(),
                              {"posting_location": "London, United Kingdom"})
    assert resolved.values["q_auth"] == "No"


def test_work_auth_mapping_resolves_yes_for_eu_posting():
    fm = _work_auth_field()
    resolved = resolve_values(fm, _work_auth_ssot(),
                              {"posting_location": "Berlin, Germany"})
    # EU: no sponsorship needed -> authorized -> "Yes". Proves region selection
    # differentiates (EU != US/UK), not a constant answer.
    assert resolved.values["q_auth"] == "Yes"


def test_work_auth_mapping_parks_when_country_unknown():
    fm = _work_auth_field()
    resolved = resolve_values(fm, _work_auth_ssot(), {})   # no posting_location
    assert "q_auth" not in resolved.values
    reason = dict(resolved.skipped)["q_auth"]
    assert "work authorization" in reason
    assert "posting country" in reason
    assert "no usable scalar" not in reason               # not the old blind scan


def test_work_auth_mapping_parks_when_country_unmapped():
    fm = _work_auth_field()
    resolved = resolve_values(fm, _work_auth_ssot(),
                              {"posting_location": "Somewhere, Atlantis"})
    assert "q_auth" not in resolved.values
    assert "posting country" in dict(resolved.skipped)["q_auth"]


def test_work_auth_mapping_scalar_region_values_render():
    # Robustness: a mapping whose region values are the owner's own answer
    # STRINGS (not structured facts) also resolves by country.
    ssot = SSOT({"work_authorization": {
        "eu": "Yes, I have full EU work rights",
        "us": "No, I would require visa sponsorship",
    }})
    fm = _work_auth_field()
    us = resolve_values(fm, ssot, {"posting_location": "Remote, US"})
    eu = resolve_values(fm, ssot, {"posting_location": "Milan, Italy"})
    assert us.values["q_auth"] == "No"
    assert eu.values["q_auth"] == "Yes"


# ============================================================================ #
# RS-a: visa-sponsorship SELECT value derived by posting country from the region
# policy, never inherited from a US-specific canned seed.
# ============================================================================ #
# Live defect (lever, 2026-07-18): the sponsorship radio resolved "No" from
# canned_answers.us_visa_sponsorship_required on a UK posting. The value is now
# derived from policies.sponsorship_by_region keyed by the POSTING country.

def _sponsorship_ssot():
    # The region policy seeded on toto (eu/eea -> "No", default -> "Yes"). The
    # US-specific canned seed is ALSO present, so the tests prove the policy
    # OVERRIDES it rather than merely filling a gap it left.
    return SSOT({
        "policies": {"sponsorship_by_region": {
            "eu": "No", "eea": "No", "default": "Yes"}},
        "canned_answers": {"us_visa_sponsorship_required": "No"}})


def _sponsorship_field():
    return _fieldmap(_field(
        "q_visa",
        "Will you now or in the future require sponsorship for employment visa "
        "status (e.g., H-1B, etc.)?",
        type_="multi_value_single_select", options=["Yes", "No"],
        role="combobox"))


def test_sponsorship_value_is_yes_for_uk_posting():
    fm = _sponsorship_field()
    resolved = resolve_values(fm, _sponsorship_ssot(),
                              {"posting_location": "London, United Kingdom"})
    # UK is not eu/eea -> the policy "default" ("Yes"), never the US-seed "No".
    assert resolved.values["q_visa"] == "Yes"
    assert resolved.skipped == []


def test_sponsorship_value_is_no_for_eu_posting():
    fm = _sponsorship_field()
    resolved = resolve_values(fm, _sponsorship_ssot(),
                              {"posting_location": "Berlin, Germany"})
    # An EU posting -> "No". Proves region selection differentiates (eu != uk),
    # not a constant answer.
    assert resolved.values["q_visa"] == "No"


def test_sponsorship_value_parks_when_country_unknown():
    fm = _sponsorship_field()
    resolved = resolve_values(fm, _sponsorship_ssot(), {})   # no posting_location
    assert "q_visa" not in resolved.values
    reason = dict(resolved.skipped)["q_visa"]
    assert "visa sponsorship" in reason
    assert "posting country" in reason


def test_sponsorship_policy_overrides_us_specific_canned_seed():
    # The US-seed alone gave "No" on every posting (the live bug); with the region
    # policy present a UK posting now derives "Yes" from the default instead.
    fm = _sponsorship_field()
    resolved = resolve_values(fm, _sponsorship_ssot(),
                              {"posting_location": "London, United Kingdom"})
    assert resolved.values["q_visa"] == "Yes"


def test_sponsorship_without_region_policy_keeps_legacy_scalar_behaviour():
    # Backward compat: no policies.sponsorship_by_region -> the SELECT still
    # resolves from the canned scalar exactly as before (here "No"), so the
    # existing sponsorship path is untouched for SSOTs that lack the policy.
    ssot = SSOT({"canned_answers": {"visa_sponsorship_required": "no"}})
    fm = _sponsorship_field()
    resolved = resolve_values(fm, ssot,
                              {"posting_location": "London, United Kingdom"})
    assert resolved.values["q_visa"] == "No"


# ============================================================================ #
# RS-d: in-office-commitment boolean derived from the W4-COMMUTE-GATE policy.
# ============================================================================ #
# Live defect (workable rokt, New York US): QA_11599466 "commit to 4 days/week in
# office" failed as "boolean question resolved to a non-boolean value" because
# the canned in-office answer is PROSE. The boolean is now DERIVED per posting
# from the owner commute policy (SSOT preferences.location_policy) plus the posting location.

def _commute_ssot():
    return SSOT({"preferences": {"location_policy": {
        "allowed_cities": ["Milan", "Bologna"],
        "max_onsite_days_per_week_europe": 1,
        "max_onsite_days_per_month_rest": 4,
    }}})


def _in_office_field():
    return _fieldmap(_field(
        "q_office", "Can you commit to being in the office 4 days/week?",
        type_="boolean", role="checkbox"))


def test_in_office_boolean_is_false_outside_eu():
    fm = _in_office_field()
    resolved = resolve_values(fm, _commute_ssot(),
                              {"posting_location": "New York, US"})
    # 4 days/week ~= 17 days/month, over the 4 days/month rest-of-world cap -> No.
    assert resolved.values["q_office"] is False
    assert resolved.skipped == []


def test_in_office_boolean_is_true_in_milan():
    fm = _in_office_field()
    resolved = resolve_values(fm, _commute_ssot(),
                              {"posting_location": "Milan, Italy"})
    # Milan is an owner allowed city: any in-office cadence is viable -> Yes.
    assert resolved.values["q_office"] is True


def test_in_office_boolean_parks_when_location_unknown():
    fm = _in_office_field()
    resolved = resolve_values(fm, _commute_ssot(), {})     # no posting_location
    assert "q_office" not in resolved.values
    reason = dict(resolved.skipped)["q_office"]
    assert "in-office commitment" in reason               # honest park, no coercion


def test_in_office_boolean_weekly_cap_in_europe():
    # A rest-of-Europe posting is banded by the weekly cap (1 day/week here):
    # 1 day/week is viable (True), 3 days/week is not (False).
    ok = resolve_values(
        _fieldmap(_field("q1", "Able to be in the office 1 day per week?",
                         type_="boolean", role="checkbox")),
        _commute_ssot(), {"posting_location": "Berlin, Germany"})
    no = resolve_values(
        _fieldmap(_field("q2", "Able to be in the office 3 days per week?",
                         type_="boolean", role="checkbox")),
        _commute_ssot(), {"posting_location": "Berlin, Germany"})
    assert ok.values["q1"] is True
    assert no.values["q2"] is False


def test_in_office_boolean_parks_when_no_policy():
    # No location policy in the SSOT -> fail-closed park, never a coerced answer.
    fm = _in_office_field()
    resolved = resolve_values(fm, SSOT({}),
                              {"posting_location": "New York, US"})
    assert "q_office" not in resolved.values
    assert "location policy" in dict(resolved.skipped)["q_office"]


# P2-2, second half: the MULTIPLIER cadence "4x/week". The live workable rokt
# CA_9781 label below parked as a generic non-consent checkbox because neither
# half of the gate reached it: `_detect_onsite_cadence` had no `Nx/week` pattern,
# AND `_IN_OFFICE_RE` matched "come into the office" but not the gerund
# "coming into the office" the posting actually uses.

# The live workable rokt CA_9781 label, verbatim from the r7 census.
_LIVE_CADENCE_LABEL = (
    "Rokt is a hybrid company. If this role is the right fit, would you commit "
    "to coming into the office 4x/week? Please note this is subject to change "
    "based on the needs of the business.")


def test_in_office_multiplier_cadence_derives_from_the_live_label():
    # 4x/week ~= 17 days/month, over the 4 days/month rest-of-world cap -> No,
    # the same derivation r6's "4 days/week" phrasing already reached.
    fm = _fieldmap(_field("CA_9781", _LIVE_CADENCE_LABEL,
                          type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, _commute_ssot(),
                              {"posting_location": "New York, US"})
    assert resolved.values["CA_9781"] is False
    assert resolved.skipped == []


def test_in_office_multiplier_cadence_per_month_parses():
    # The month variant of the same multiplier form: 2x/month is under the
    # 4 days/month rest-of-world cap -> Yes.
    fm = _fieldmap(_field(
        "q_office", "Would you come into the office 2x/month?",
        type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, _commute_ssot(),
                              {"posting_location": "New York, US"})
    assert resolved.values["q_office"] is True


def test_a_digit_and_an_x_are_not_a_cadence():
    # The negative control on the multiplier pattern. The label is built to be
    # the HARD case: it carries a digit, an x and the word "week", and only the
    # REQUIRED per|a|/ separator keeps "2x your week" from reading as a cadence
    # of 2 days per week. Drop that separator requirement and this box silently
    # derives an attendance answer from a sentence about time off.
    fm = _fieldmap(_field(
        "q_office",
        "We 2x your week off in year two. Are you happy to be in the office?",
        type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, _commute_ssot(),
                              {"posting_location": "New York, US"})
    assert "q_office" not in resolved.values


# --- P2-2 independent coverage -----------------------------------------------
# The pins the fix's own tests do not carry: the polarity ORDERING at unit level,
# the affirmative sponsorship shape, the work-auth non-regression, the separator
# and gerund halves of the cadence gate, and the documented side effect of
# routing `_classify_checkbox` through `_select_intent` (see the visa test).


def test_sponsorship_polarity_ordering_is_load_bearing():
    # WHY the negated form must be tested FIRST, asserted as the trap itself:
    # "will not require sponsorship" ALSO matches the affirmative requirement
    # regex, because it literally contains "require". The affirmative matcher
    # therefore cannot tell the two polarities apart on its own -- only the ORDER
    # defuses it. Both halves are pinned: that the affirmative regex really does
    # match the negated label, and that the polarity still comes out NOT-required.
    # A future edit that reorders the two branches turns every one of these into
    # its opposite, which is the false legal assertion this fix exists to prevent.
    for label in ("I will not require visa sponsorship.",
                  "Will you not require sponsorship?",
                  "I do not require sponsorship."):
        assert _SPONSOR_NEEDED_ASSERT_RE.search(label.lower()), label
        assert _sponsorship_assertion_polarity(label) is False, label
    # The affirmative shape is unambiguous in the other direction.
    assert _sponsorship_assertion_polarity(
        "I will require visa sponsorship.") is True


def test_affirmative_sponsorship_checkbox_is_never_ticked_on_eu_work_rights():
    # The first-person affirmative shape of the trap, alongside the live
    # interrogative one: the owner's EU rights make "authorized to work" TRUE,
    # and a fix that reached this box through the right-to-work truth would tick
    # it from that Yes. The box asserts sponsorship IS required, which is FALSE.
    ssot = _sponsorship_assertion_ssot()
    fm = _fieldmap(_field("q_reqspon", "I will require visa sponsorship.",
                          type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, ssot, {})
    assert "q_reqspon" not in resolved.values
    assert resolved.values.get("q_reqspon") is not True
    assert "never falsely checked" in dict(resolved.skipped)["q_reqspon"]


def test_work_auth_checkbox_is_not_captured_by_sponsorship_intent():
    # The non-regression on the currently-correct path. `_select_intent` tests
    # `_SPONSOR_INTENT_RE` BEFORE right-to-work, so the guard that matters is
    # that a pure work-auth label is not swallowed by the sponsorship branch and
    # sent through the inverted polarity. Pinned at both levels: the classifier
    # still sorts it as an assertion, and the true EU claim still ticks while the
    # false US claim still parks -- exactly as before the fix.
    assert _classify_checkbox(
        "Are you legally authorized to work in the United States?") == "assertion"
    ssot = _consent_policy_ssot(extra={
        "work_authorization": {"us": {"sponsorship_required": True},
                               "eu": True}})
    eu = resolve_values(_fieldmap(_field(
        "q_eu", "Are you legally authorized to work in the European Union?",
        type_="boolean", role="checkbox")), ssot, {})
    us = resolve_values(_fieldmap(_field(
        "q_us", "Are you legally authorized to work in the United States?",
        type_="boolean", role="checkbox")), ssot, {})
    assert eu.values["q_eu"] is True
    assert "q_us" not in us.values


def test_visa_mentioning_consent_checkbox_auto_ticks():
    """H.1 (owner ruling 2026-07-20): relaxing the gate for no-polarity visa
    mentions restores consent auto-tick.

    This test FLIPS the former `test_visa_mentioning_consent_checkbox_fails_closed`
    pin. Before H.1, `_classify_checkbox` consulted `_select_intent` before every
    consent branch, so ANY checkbox merely containing "visa" was sorted as a
    sponsorship `assertion`; a genuine consent box that only MENTIONED visa then
    parked (a real fill-rate cost). H.1 splits the two directions cleanly by
    requirement-polarity readability, and BOTH labels below contain "visa" so that
    readability is the only discriminator between them:

    (a) BENEFIT (UNCHANGED) -- a genuine visa/sponsorship claim, label "I will
        require visa sponsorship.", reads REQUIRED polarity, keeps the assertion
        slot, and on the owner's no-sponsorship-needed truth the claim is FALSE,
        so the box still PARKS instead of being ticked. The trap guard is intact.

    (b) RESTORED -- a genuine consent box that merely MENTIONS visa, label "I agree
        to the processing of my visa documentation for this application", states
        NO readable requirement polarity, so step 2 lets it fall through to the
        consent branches: it is classified `application_privacy` and AUTO-TICKS
        under the consent policy again (its pre-`_select_intent`-routing
        behaviour). The former assertion-park assertion is DELIBERATELY converted
        to an auto-tick assertion here; the fail-open hole is NOT reopened, because
        a label that DOES assert a requirement still parks (see (a)).

    Control: the same privacy box WITHOUT "visa", label "I have read and agree to
    the Privacy Notice.", also auto-ticks, so (b)'s tick is the consent policy at
    work and not an accident of the visa mention."""
    ssot = _sponsorship_assertion_ssot()

    # (a) BENEFIT (unchanged): a genuine visa/sponsorship claim is the assertion it
    # is, and the polarity protection parks the FALSE claim rather than ticking it.
    benefit = _fieldmap(_field(
        "q_reqvisa", "I will require visa sponsorship.",
        type_="boolean", role="checkbox"))
    r_benefit = resolve_values(benefit, ssot, {})
    assert _classify_checkbox(benefit.fields[0].label) == "assertion"
    assert "q_reqvisa" not in r_benefit.values
    assert "never falsely checked" in dict(r_benefit.skipped)["q_reqvisa"]

    # (b) RESTORED: a genuine consent box merely mentioning visa now AUTO-TICKS
    # where the pre-H.1 pin asserted it parked. The former assertions were
    # `_classify_checkbox(...) == "assertion"`, `"q_visadoc" not in r_cost.values`
    # and `"never falsely checked" in ...skipped`; they are converted below.
    cost = _fieldmap(_field(
        "q_visadoc",
        "I agree to the processing of my visa documentation for this application",
        type_="boolean", role="checkbox"))
    r_cost = resolve_values(cost, ssot, {})
    assert _classify_checkbox(cost.fields[0].label) == "application_privacy"
    assert r_cost.values["q_visadoc"] is True

    # Control: the same box WITHOUT "visa" also ticks -> (b) is the consent policy,
    # not an accident of the visa mention.
    plain = resolve_values(_fieldmap(_field(
        "q_priv", "I have read and agree to the Privacy Notice.",
        type_="boolean", role="checkbox")), ssot, {})
    assert plain.values["q_priv"] is True


# --- H.1 bidirectional sponsorship polarity + consent-mention relaxation ------
# H.1 (owner ruling 2026-07-20). Step 1 teaches `_sponsorship_assertion_polarity`
# THREE grammatical shapes (verb-then-noun, noun-then-verb, possession/negation);
# step 2 then lets a no-polarity visa MENTION fall through `_classify_checkbox` to
# the consent branches. The ORDER is load-bearing: without step 1 a noun-then-verb
# requirement box ("... sponsorship is needed ...") would read no polarity and be
# auto-ticked by the step-2 relaxation, asserting a FALSE legal requirement.


def test_h1_noun_then_verb_requirement_reads_required_polarity():
    # NEW shape: the visa/sponsorship noun is the SUBJECT a copula plus a
    # requirement verb governs. Every form step 2 depends on to STAY an assertion
    # (and never fall through to a consent tick).
    for label in ("I acknowledge sponsorship is needed for this role.",
                  "Sponsorship is required.",
                  "A visa is required to work here.",
                  "Visa sponsorship will be required for this position."):
        assert _sponsorship_assertion_polarity(label) is True, label


def test_h1_possession_reads_not_required_polarity():
    # NEW shape: possessing a work visa / permit / authorization is a NOT-required
    # claim (the candidate already holds the right).
    for label in ("I have a work visa.",
                  "I hold a valid work permit.",
                  "I currently possess a residence permit."):
        assert _sponsorship_assertion_polarity(label) is False, label
    # The existing verb-then-noun negation still reads NOT-required.
    assert _sponsorship_assertion_polarity("I do not require sponsorship.") is False


def test_h1_in_progress_and_negated_possession_stay_fail_closed_none():
    # The guards the possession shape needs, both fail-closed to None rather than
    # inverting polarity: an IN-PROGRESS claim ("have applied for a visa", the
    # candidate does NOT yet hold it) and a NEGATED possession ("do not have a
    # work visa", the candidate LACKS it) must not read NOT-required.
    assert _sponsorship_assertion_polarity("I have applied for a visa.") is None
    assert _sponsorship_assertion_polarity("I do not have a work visa.") is None
    assert _sponsorship_assertion_polarity("I have no visa or work permit.") is None


def test_h1_noun_then_verb_survives_multi_sentence_negated_clause():
    # The anchor discipline carried to the NEW noun-then-verb shape: a negated
    # requirement about something ELSE in a prior sentence must not flip the
    # sponsorship polarity. "Sponsorship is required" reads REQUIRED despite the
    # leading "do not need relocation" (the period ends the negator's window).
    label = ("I do not need any relocation assistance. Sponsorship is required "
             "to work in the EU.")
    assert _sponsorship_assertion_polarity(label) is True


def test_h1_slash_joined_requirement_verb_reads_required():
    # Acceptance C at unit level: a slash-joined verb ("require/ask") must still
    # read the requirement it governs (a "/" is a word separator in real labels).
    assert _sponsorship_assertion_polarity(
        "Will you require/ask for visa sponsorship to work in the EU?") is True


def test_h1_acknowledge_sponsorship_needed_classifies_assertion_and_never_ticks():
    # ACCEPTANCE A: a noun-then-verb requirement box is an ASSERTION with REQUIRED
    # polarity, and on the owner's no-sponsorship-needed EU truth the claim is
    # FALSE, so it resolves CORRECTLY to NOT ticked (not merely parked): it goes
    # through the assertion truth machinery and declines the false check. This is
    # the label that, WITHOUT step 1, would read no polarity and be auto-ticked by
    # the step-2 relaxation -- the exact fail-open hole the ordering prevents.
    label = "I acknowledge sponsorship is needed for this role."
    assert _classify_checkbox(label) == "assertion"
    assert _sponsorship_assertion_polarity(label) is True
    ssot = _sponsorship_assertion_ssot()
    fm = _fieldmap(_field("q_ack", label, type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, ssot, {})
    assert "q_ack" not in resolved.values
    assert "never falsely checked" in dict(resolved.skipped)["q_ack"]


def test_h1_interrogative_visa_requirement_never_ticks_on_eu_facts():
    # ACCEPTANCE C: an interrogative required-claim (slash-joined verb) reads
    # REQUIRED and, being FALSE on the owner's EU facts, is never ticked.
    label = "Will you require/ask for visa sponsorship to work in the EU?"
    assert _classify_checkbox(label) == "assertion"
    assert _sponsorship_assertion_polarity(label) is True
    ssot = _sponsorship_assertion_ssot()
    fm = _fieldmap(_field("q_reqeu", label, type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, ssot, {})
    assert "q_reqeu" not in resolved.values
    assert "never falsely checked" in dict(resolved.skipped)["q_reqeu"]


def test_h1_possession_visa_claim_ticks_true_on_eu_facts():
    # ACCEPTANCE D: a NOT-required claim (possession, or negated requirement) is
    # TRUE on the owner's EU facts (no sponsorship needed), so it ticks. Both a
    # possession shape and the existing negated-requirement shape are exercised.
    ssot = _sponsorship_assertion_ssot()
    for key, label in (("q_have", "I have a work visa."),
                       ("q_nospon2", "I do not require sponsorship.")):
        assert _classify_checkbox(label) == "assertion", label
        assert _sponsorship_assertion_polarity(label) is False, label
        fm = _fieldmap(_field(key, label, type_="boolean", role="checkbox"))
        resolved = resolve_values(fm, ssot, {})
        assert resolved.values[key] is True, label


def test_h1_bare_sponsorship_mention_without_consent_class_still_parks():
    # The relaxation is SCOPED: a sponsorship-intent label with no readable
    # polarity that matches NO consent class stays a (parked) assertion, so a naked
    # sponsorship mention is never auto-ticked. Guards the narrow fall-through so
    # `test_sponsorship_checkbox_with_no_readable_polarity_parks` stays honest.
    assert _classify_checkbox("Visa sponsorship") == "assertion"
    ssot = _sponsorship_assertion_ssot()
    fm = _fieldmap(_field("q_bare", "Visa sponsorship",
                          type_="boolean", role="checkbox"))
    resolved = resolve_values(fm, ssot, {})
    assert "q_bare" not in resolved.values
    assert "never falsely checked" in dict(resolved.skipped)["q_bare"]


# --- RV2 NIT-1: noun-then-verb NEGATED requirement reads NOT-required ----------
# RV2 NIT-1 (owner ruling 2026-07-20): a leading negator that governs the
# visa/sponsorship noun in a noun-then-verb requirement ("No visa is required.")
# reads NOT-required (False), not REQUIRED. Before the fix the affirmative
# `_SPONSOR_NEED_HEAD_RE` matched the bare "visa is required" tail and read the
# leading "No" as REQUIRED, ticking a legally significant box with the OPPOSITE of
# the truth. The new `_SPONSOR_NOT_NEEDED_HEAD_RE` is anchored to the sponsorship
# noun exactly like the affirmative shapes.


def test_nit1_negated_noun_then_verb_requirement_reads_not_required_polarity():
    # THE fix: a leading negator governing the visa/sponsorship noun in a
    # noun-then-verb requirement is NOT-required (False). "No visa is required."
    # and "No work visa is required for this role." read True (REQUIRED) before the
    # fix -- the exact polarity inversion RV2 NIT-1 flags.
    for label in ("No visa is required.",
                  "No sponsorship is needed.",
                  "No work visa is required for this role."):
        assert _sponsorship_assertion_polarity(label) is False, label


def test_nit1_negated_noun_then_verb_ticks_true_on_eu_facts():
    # End to end: a NOT-required claim is TRUE on the owner's EU facts (no
    # sponsorship needed), so it ticks. Mirrors the possession / negated-verb
    # acceptance, now via the noun-then-verb NEGATED shape: it classifies as an
    # assertion and, asserted-False == needed-False, resolves to a truthful tick.
    ssot = _sponsorship_assertion_ssot()
    for key, label in (("q_novisa", "No visa is required."),
                       ("q_nospon3", "No sponsorship is needed.")):
        assert _classify_checkbox(label) == "assertion", label
        assert _sponsorship_assertion_polarity(label) is False, label
        fm = _fieldmap(_field(key, label, type_="boolean", role="checkbox"))
        resolved = resolve_values(fm, ssot, {})
        assert resolved.values[key] is True, label


def test_nit1_affirmative_and_possession_shapes_stay_unchanged():
    # REGRESSION: the new negated-head detector must not disturb the affirmative
    # requirement (True), the possession / negated-verb NOT-required (False), or
    # the in-progress / negated-possession fail-closed (None) shapes.
    for label in ("Visa is required.",
                  "Sponsorship is needed.",
                  "I acknowledge sponsorship is needed for this role."):
        assert _sponsorship_assertion_polarity(label) is True, label
    for label in ("I have a work visa.",
                  "I do not require sponsorship."):
        assert _sponsorship_assertion_polarity(label) is False, label
    for label in ("I have applied for a work visa.",
                  "I do not have a work visa."):
        assert _sponsorship_assertion_polarity(label) is None, label


def test_nit1_negated_head_anchor_survives_multi_sentence_prior_clause():
    # ANCHOR for the NEW negated-head shape: a negator governing a DIFFERENT clause
    # must not flip a genuine requirement. "No relocation is required" ends at the
    # period, so the negated-head detector never reaches the visa/sponsorship noun
    # of the second sentence, and the real requirement still reads REQUIRED. The
    # minimum guarantee (must NOT read False) is asserted explicitly.
    label = "No relocation is required. Will visa sponsorship be required?"
    assert _sponsorship_assertion_polarity(label) is True
    assert _sponsorship_assertion_polarity(label) is not False


def test_multiplier_cadence_parses_every_separator_form():
    # The three live separator spellings of the multiplier form, at unit level:
    # the end-to-end tests above only exercise "/" .
    assert _detect_onsite_cadence("4x/week") == (4.0, "week")
    assert _detect_onsite_cadence("4x per week") == (4.0, "week")
    assert _detect_onsite_cadence("4x a month") == (4.0, "month")


def test_bare_multiplier_is_not_a_cadence():
    # The separator (per|a|/) is REQUIRED. A bare "2x", and a "2x" bound to any
    # non-cadence noun, must not be read as an attendance frequency -- otherwise
    # a compensation sentence would silently drive an in-office answer.
    assert _detect_onsite_cadence("2x") == (None, None)
    assert _detect_onsite_cadence("2x your base salary") == (None, None)


def test_in_office_re_matches_the_live_gerund_and_the_prior_forms():
    # Without this half of the gate the cadence fix alone would never fire: the
    # live CA_9781 question would not be recognised as an in-office question at
    # all. "coming into the office" is reachable ONLY through the widened
    # alternative -- the plain "in the office" alternative does not match it,
    # because the text reads "into the office".
    assert _IN_OFFICE_RE.search("coming into the office")
    # The pre-existing forms must keep matching (no regression from the widening).
    for form in ("come into the office", "in the office", "in-office",
                 "on-site", "in-person", "3 days in office"):
        assert _IN_OFFICE_RE.search(form), form
