"""Form-fill DRY RUN: deterministic value resolution + safe fill (W4 4b).

No playwright, no network: fill_form is driven through fake page/locator objects
over hand-built field maps, so the autouse no-network guard holds throughout.
The safety invariants (no submit path, URL-unchanged, no file touch, EEO never
touched) are each asserted directly. resolve_values is pure keyword matching
against the synthetic v1.4 SSOT; every verdict is asserted explicitly.
"""

import contextlib

import pytest

from engine.fill import (
    FillReport,
    FillSafetyError,
    ResolvedValues,
    _safe_click,
    fill_form,
    resolve_values,
)
from engine.fieldmap import Field, FieldMap, Locator
from engine.profile_map import profile_from_real_ssot
from engine.ssot import SSOT

_PINNED = "2026-07-03T00:00:00+00:00"
_NO_OVERRIDE = object()  # sentinel: input_value() is not overridden


# --- fixture builders ---------------------------------------------------------

def _field(key, label, *, type_="input_text", required=True, options=None,
           source="questions", role="textbox"):
    return Field(key=key, label=label, type=type_, required=required,
                 options=options or [], source=source,
                 locator=Locator(role=role, name=label),
                 step_index=0, conditional_on=None)


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

    def set_input_files(self, *args, **kwargs):  # must NEVER be called
        self.set_input_files_calls += 1
        raise AssertionError("set_input_files must never be called in the dry run")

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


class _FakePage:
    def __init__(self, *, url, controls=None, error_texts=None):
        self._url = url
        self._controls = {} if controls is None else controls
        self._error_texts = error_texts or []
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
                  **loc_kwargs):
    """A fake page whose controls are keyed by each field's locator name."""
    controls = {}
    page = None

    def build():
        nonlocal page
        page = _FakePage(url=url, controls=controls, **loc_kwargs)
        for fv in fields:
            controls[fv.locator.name] = _FakeLocator(page=page)
        return page

    return build


# =============================================================================
# resolve_values
# =============================================================================

def test_resolve_answerable_text(real_ssot_path):
    ssot = SSOT.load(real_ssot_path)
    fm = _fieldmap(_field("first_name", "First Name"),
                   _field("email", "Email"))
    resolved = resolve_values(fm, ssot, profile_from_real_ssot(ssot))

    assert resolved.values["first_name"] == "Test Candidate"
    assert resolved.values["email"] == "test.candidate@example.invalid"
    assert resolved.skipped == []


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
    # intended, so the diff must surface a mismatch.
    mutated = _FakeLocator(readback="Someone Else")
    fields = [_fv("first_name", "First Name", "input_text", "Test Candidate")]
    page = _FakePage(url="https://x/1/apply", controls={"First Name": mutated})

    report = fill_form("greenhouse", "acme", "1",
                       ResolvedValues(fields=fields),
                       browser_factory=_factory_for(page),
                       artifacts_dir=tmp_path, now=lambda: _PINNED)

    assert report.filled == 1
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


def _fv(key, label, type_, value, *, role="textbox"):
    from engine.fill import FieldValue
    return FieldValue(key=key, label=label, type=type_,
                      locator=Locator(role=role, name=label), value=value)
