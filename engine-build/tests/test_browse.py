"""Browser field-map capture: ashby graphql interception + lever DOM parse.

No playwright, no network: both capture paths are driven through fake browser
factories over fixture data (a live-confirmed ashby non-user-graphql form
response, plus a legacy-shaped one exercising the one-release fallback probe,
and a server-rendered lever apply-page DOM). The autouse
no-network guard is satisfied throughout. Schema compliance, source tags, and
coverage() interop are asserted on the produced FieldMaps; shape drift is proven
to raise CaptureShapeError rather than yield an empty map.
"""

import contextlib

import pytest

from engine.browse import (
    ASHBY_SOURCE,
    LEVER_SOURCE,
    CaptureShapeError,
    capture_ashby,
    capture_lever,
)
from engine.fieldmap import MANUAL_ONLY
from engine.profile_map import profile_from_real_ssot
from engine.ssot import SSOT

_PINNED = "2026-07-03T00:00:00+00:00"

_TOP_KEYS = ["vendor", "posting_id", "schema_version", "captured_at", "fields"]
_FIELD_KEYS = ["key", "label", "type", "required", "options", "source",
               "locator", "step_index", "conditional_on"]


class _FakeResponse:
    def __init__(self, url, body):
        self.url = url
        self._body = body

    def json(self):
        return self._body


class _FakePage:
    """A stand-in for a Playwright page: records the goto, fires response
    handlers with scripted responses (ashby), and serves fixture DOM (lever)."""

    def __init__(self, *, responses=None, html=""):
        self._responses = responses or []
        self._html = html
        self._handlers = {}
        self.goto_calls = []

    def set_default_timeout(self, ms):
        pass

    def on(self, event, handler):
        self._handlers.setdefault(event, []).append(handler)

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        for handler in self._handlers.get("response", []):
            for response in self._responses:
                handler(response)

    def content(self):
        return self._html


def _factory_for(page):
    @contextlib.contextmanager
    def factory():
        yield page
    return factory


# --- Ashby graphql interception ----------------------------------------------

def _ashby_responses(ashby_form_raw):
    """A wrong-URL response, a same-URL decoy with no form, then the real form.

    The selector must ignore the non-matching URL and the empty jobPosting and
    still find the schema-carrying response.
    """
    return [
        _FakeResponse("https://jobs.ashbyhq.com/api/viewer", {"data": {}}),
        _FakeResponse("https://jobs.ashbyhq.com/_next/data/non-user-graphql",
                      {"data": {"jobPosting": None}}),
        _FakeResponse("https://jobs.ashbyhq.com/_next/data/non-user-graphql",
                      ashby_form_raw),
    ]


def test_ashby_capture_maps_graphql_schema(ashby_form_raw):
    page = _FakePage(responses=_ashby_responses(ashby_form_raw))
    fm = capture_ashby("initech", "8f2a1c40", _factory_for(page),
                       now=lambda: _PINNED)

    assert fm.vendor == "ashby"
    assert fm.posting_id == "8f2a1c40-1111-2222-3333-abcabcabcabc"
    assert fm.captured_at == _PINNED
    # exactly one page load, hitting the application URL
    assert len(page.goto_calls) == 1
    assert page.goto_calls[0][0] == (
        "https://jobs.ashbyhq.com/initech/8f2a1c40/application")

    by_key = {f.key: f for f in fm.fields}
    # the hidden field is dropped; every visible field is captured
    assert "_systemfield_hidden_source" not in by_key
    assert set(by_key) == {
        "_systemfield_name", "_systemfield_email", "_systemfield_phone",
        "_systemfield_resume", "_systemfield_linkedin", "custom_github",
        "custom_work_auth", "custom_visa", "custom_pronouns", "custom_notice"}

    name = by_key["_systemfield_name"]
    assert name.type == "input_text"
    assert name.required is True
    assert name.source == ASHBY_SOURCE
    assert name.locator.role == "textbox"
    assert name.locator.name == "Full name"
    assert name.step_index == 0          # ashby forms are static (R-WT-8 8)
    assert name.conditional_on is None

    assert by_key["_systemfield_resume"].type == "input_file"
    assert by_key["custom_work_auth"].type == "boolean"

    visa = by_key["custom_visa"]
    assert visa.type == "multi_value_single_select"
    assert visa.locator.role == "combobox"
    assert visa.options == ["Yes", "No"]

    pronouns = by_key["custom_pronouns"]
    assert pronouns.type == "multi_value_multi_select"
    assert pronouns.locator.role == "listbox"
    assert pronouns.options == ["she/her", "he/him", "they/them"]


def test_ashby_capture_schema_compliant_and_coverage_interop(
        ashby_form_raw, real_ssot_path):
    page = _FakePage(responses=_ashby_responses(ashby_form_raw))
    fm = capture_ashby("initech", "req1", _factory_for(page), now=lambda: _PINNED)

    blob = fm.to_dict()
    assert list(blob.keys()) == _TOP_KEYS
    for field in blob["fields"]:
        assert list(field.keys()) == _FIELD_KEYS

    ssot = SSOT.load(real_ssot_path)
    report = fm.coverage(ssot, profile_from_real_ssot(ssot))
    # required: name, email, resume, work_auth, visa, notice
    assert report.required_total == 6
    by_key = {f.key: f for f in report.fields}
    assert by_key["_systemfield_resume"].status == MANUAL_ONLY
    assert by_key["_systemfield_resume"].reason == "file-upload"
    assert isinstance(report.summary_line(), str)


def test_ashby_raises_when_no_form_response():
    # Only a same-URL decoy with no form; the real form never arrives.
    responses = [_FakeResponse(
        "https://jobs.ashbyhq.com/_next/data/non-user-graphql",
        {"data": {"jobPosting": None}})]
    page = _FakePage(responses=responses)
    with pytest.raises(CaptureShapeError, match="applicationFormDefinition"):
        capture_ashby("initech", "req1", _factory_for(page))


def test_ashby_capture_falls_back_to_legacy_form_definition(
        ashby_form_definition_raw):
    """A posting still served the pre-migration `applicationFormDefinition`
    shape (no `applicationForm` key at all) must still parse via the
    one-release fallback probe, not raise CaptureShapeError."""
    page = _FakePage(responses=_ashby_responses(ashby_form_definition_raw))
    fm = capture_ashby("initech", "8f2a1c40", _factory_for(page),
                       now=lambda: _PINNED)

    assert fm.vendor == "ashby"
    assert fm.posting_id == "8f2a1c40-1111-2222-3333-abcabcabcabc"

    by_key = {f.key: f for f in fm.fields}
    # the hidden field is dropped; every visible field is captured, same as
    # the live-shape fixture
    assert "_systemfield_hidden_source" not in by_key
    assert set(by_key) == {
        "_systemfield_name", "_systemfield_email", "_systemfield_phone",
        "_systemfield_resume", "_systemfield_linkedin", "custom_github",
        "custom_work_auth", "custom_visa", "custom_pronouns", "custom_notice"}

    name = by_key["_systemfield_name"]
    assert name.required is True
    assert name.source == ASHBY_SOURCE
    assert name.step_index == 0     # the legacy shape carries no step concept

    visa = by_key["custom_visa"]
    assert visa.type == "multi_value_single_select"
    assert visa.options == ["Yes", "No"]


# --- Lever server-rendered DOM parse -----------------------------------------

def test_lever_capture_parses_apply_dom(lever_apply_html):
    page = _FakePage(html=lever_apply_html)
    fm = capture_lever("globex", "req-77", _factory_for(page), now=lambda: _PINNED)

    assert fm.vendor == "lever"
    assert fm.posting_id == "req-77"
    assert fm.captured_at == _PINNED
    assert len(page.goto_calls) == 1
    assert page.goto_calls[0][0] == "https://jobs.lever.co/globex/req-77/apply"

    by_key = {f.key: f for f in fm.fields}
    # fixed base fields (name, email, phone, org, urls, resume upload)
    assert set(by_key) >= {"name", "email", "phone", "org",
                           "urls[LinkedIn]", "urls[GitHub]", "resume"}
    assert by_key["name"].label == "Full name"
    assert by_key["name"].required is True
    assert by_key["name"].source == LEVER_SOURCE
    assert by_key["name"].type == "input_text"
    assert by_key["phone"].required is False
    assert by_key["resume"].type == "input_file"
    assert by_key["resume"].required is True

    # custom card: a required select carrying its enumerated options
    select = by_key["cards[7f3a][field0]"]
    assert select.label == "Years of professional experience"
    assert select.type == "multi_value_single_select"
    assert select.locator.role == "combobox"
    assert select.required is True
    assert select.options == ["0-2 years", "3-5 years", "6+ years"]

    # custom card: a required freeform textarea (no options)
    textarea = by_key["cards[7f3a][field1]"]
    assert textarea.label == "Why do you want to work at Globex?"
    assert textarea.type == "textarea"
    assert textarea.required is True
    assert textarea.options == []


def test_lever_capture_schema_compliant_and_coverage_interop(
        lever_apply_html, real_ssot_path):
    page = _FakePage(html=lever_apply_html)
    fm = capture_lever("globex", "req-77", _factory_for(page), now=lambda: _PINNED)

    blob = fm.to_dict()
    assert list(blob.keys()) == _TOP_KEYS
    for field in blob["fields"]:
        assert list(field.keys()) == _FIELD_KEYS

    ssot = SSOT.load(real_ssot_path)
    report = fm.coverage(ssot, profile_from_real_ssot(ssot))
    # required: name, email, resume (base) + select + textarea (custom) = 5
    assert report.required_total == 5
    by_key = {f.key: f for f in report.fields}
    assert by_key["resume"].status == MANUAL_ONLY
    assert isinstance(report.summary_line(), str)


def test_lever_raises_when_form_absent():
    page = _FakePage(html="<html><body><p>This position is closed.</p></body></html>")
    with pytest.raises(CaptureShapeError, match="no recognizable form fields"):
        capture_lever("globex", "req-77", _factory_for(page))


# --- import guard -------------------------------------------------------------

def test_browse_module_imports_without_playwright():
    # This module imports engine.browse at the top with no playwright installed,
    # so a clean import already proves the guard; assert the entrypoints exist.
    import engine.browse as browse
    assert callable(browse.capture_ashby)
    assert callable(browse.capture_lever)


def test_invoking_default_factory_without_playwright_raises_clear_error():
    try:
        import playwright  # noqa: F401
    except ImportError:
        playwright_present = False
    else:
        playwright_present = True

    if playwright_present:
        pytest.skip("playwright is installed here; the not-installed error path "
                    "cannot be exercised in this venv")
    # No browser_factory -> the default factory imports playwright lazily and
    # must raise a clear, actionable install error, not a bare ImportError.
    with pytest.raises(RuntimeError, match=r"pip install playwright==1\.56\.\*"):
        capture_lever("globex", "req-77")
