"""Lever provider (engine.providers.lever): the SECOND reference implementation
of the `Provider` contract, W5.3 -- the DOM path (no schema).

No patchright, no network: `fill()` is driven through a FAKE page/locator
harness mirroring the representative apply DOM at
`tests/fixtures/providers/lever/dom.html` (base text fields + urls[] fields, a
server-rendered NATIVE <select>, an EEO/demographic decline field, an optional
marketing checkbox, a REQUIRED consent checkbox = the hCaptcha-hazard control,
and a resume file input). The field map comes from that fixture parsed through
the real (offline) `engine.providers.lever.capture._parse_lever` -- Lever has NO
custom-question schema,
so the DOM IS the schema. The SSOT is a hand-built FAKE (no owner PII). A real
live-browser HAR capture against the real DOM is a SEPARATE later step (the
W5.2/W5.3 fixture-validation promise in providers/base.py); this suite proves
the LOGIC offline, matching test_providers_base.py / test_providers_greenhouse.py.
"""

import contextlib
from pathlib import Path

import pytest

import importlib

# `engine.providers.lever.capture` is a submodule shadowed at package scope by the
# `capture` Provider callable, so reach the module object via importlib (the same
# sys.modules / import_module seam the package NAME NOTE documents).
lever_capture = importlib.import_module("engine.providers.lever.capture")
from engine.fieldmap import Field, FieldMap, Locator, MANUAL_ONLY
from engine.kernel.contracts import FillAssets, FillSafetyError
from engine.kernel.capture_toolkit import CaptureShapeError
from engine.profile_map import profile_from_real_ssot
from engine.providers import _registry, lever, protocol
from engine.providers.lever.capture import LEVER_SOURCE, capture_lever
from engine.ssot import SSOT

_FIXTURES = Path(__file__).parent / "fixtures" / "providers" / "lever"
_PINNED = "2026-07-03T00:00:00+00:00"

# DELIBERATE per-vendor duplication (owner-ratified 2026-07-10): vendor test files
# stay self-contained so the per-vendor development loops never co-edit a shared
# file (DRY exception: decoupling outweighs consolidation here). A stale copy is
# self-catching: the schema-compliance assert (list(field.keys()) == _FIELD_KEYS)
# fails loudly on drift. Do not consolidate.
_TOP_KEYS = ["vendor", "posting_id", "schema_version", "captured_at", "fields"]
_FIELD_KEYS = ["key", "label", "type", "required", "options", "source",
               "locator", "step_index", "conditional_on", "decline_allowed",
               "max_length", "accept_types", "norm_type", "section"]

_SPONSOR_LABEL = ("Will you now or in the future require visa sponsorship for "
                  "employment?")
_SPONSOR_KEY = "cards[a1b2c3][field0]"
_CONSENT_KEY = "consent[privacy]"
_GENDER_KEY = "eeo[gender]"
# The required fields the primary fixture's fill() can safely land (everything
# but the hazard consent checkbox): the DOM-sweep required set for a COMPLETE run.
_SAFE_REQUIRED_LABELS = ("Full name", "Email", "Resume / CV", _SPONSOR_LABEL)


# -- fixture loaders -------------------------------------------------------


def _fieldmap() -> FieldMap:
    """The full fixture field map, parsed through the REAL offline DOM parser."""
    html = (_FIXTURES / "dom.html").read_text()
    return lever_capture._parse_lever(html, "fauxcorp", "9001", now=lambda: _PINNED)


def _fieldmap_without(*keys: str) -> FieldMap:
    """The fixture field map minus the named field(s) -- models a Lever form
    whose required set is all safely fillable (no required human-handoff)."""
    fm = _fieldmap()
    fm.fields = [f for f in fm.fields if f.key not in keys]
    return fm


def _fake_ssot() -> SSOT:
    # FAKE, invented placeholder data only -- no owner PII, matching the
    # existing real_ssot_v14.yaml fixture's convention.
    return SSOT({
        "identity": {
            "name": "Test Candidate",
            "email": "test.candidate@example.invalid",
        },
        "links": {
            "linkedin": "https://www.linkedin.com/in/test-candidate",
            "github": "https://github.com/test-candidate",
        },
        "canned_answers": {
            "visa_sponsorship_required": "no",
            "privacy_consent_default": "yes",
        },
    })


def _assets(tmp_path, *, ats=True, atsi=True, photo=True) -> FillAssets:
    def make(name, present):
        p = tmp_path / name
        if present:
            p.write_bytes(b"stub")
        return p
    return FillAssets(cv_ats=make("cv-ats.pdf", ats),
                      cv_atsi=make("cv-atsi.pdf", atsi),
                      photo=make("Me.png", photo))


def _resolved_values(fieldmap, *, tmp_path, assets_kwargs=None):
    ssot = _fake_ssot()
    profile = profile_from_real_ssot(ssot)
    assets = _assets(tmp_path, **(assets_kwargs or {}))
    return lever.resolve_values(fieldmap, ssot, profile, assets=assets)


# -- fake DOM harness (mirrors dom.html) ------------------------------------


class _FakeTextLocator:
    """A plain text/url input: type_human -> press_sequentially, readback via
    input_value(). Raises on the forbidden fill()/click()/check() paths (the
    reCAPTCHA v3 human-cadence + hCaptcha no-auto-click invariants)."""

    def __init__(self):
        self.value = ""

    def press_sequentially(self, ch, delay=None):
        self.value += ch

    def input_value(self):
        return self.value

    def get_attribute(self, name):
        return None

    def fill(self, *args, **kwargs):
        raise AssertionError("Lever text fields must use type_human, never fill()")

    def check(self, *args, **kwargs):
        raise AssertionError("Lever must never programmatically .check() (hCaptcha)")

    def click(self, *args, **kwargs):
        raise AssertionError("Lever must never programmatically .click() a field")

    def select_option(self, *args, **kwargs):
        raise AssertionError("a text field must not be select_option'd")


class _FakeBadTextLocator(_FakeTextLocator):
    """A text input whose value silently never takes (readback always empty):
    exercises the readback-gate rejecting a value the page dropped."""

    def input_value(self):
        return ""


class _FakeSelectLocator:
    """A server-rendered native <select>: driven by select_option (NOT the
    react-select click/filter/pick dance), readback via input_value()."""

    def __init__(self):
        self.selected = None
        self.select_option_calls = 0

    def select_option(self, label=None):
        self.select_option_calls += 1
        self.selected = label

    def input_value(self):
        return self.selected or ""

    def get_attribute(self, name):
        return None

    def fill(self, *args, **kwargs):
        raise AssertionError("a native select must use select_option, never fill()")

    def click(self, *args, **kwargs):
        raise AssertionError("a native select must not be clicked (hCaptcha)")

    def check(self, *args, **kwargs):
        raise AssertionError("a native select must not be checked")

    def press_sequentially(self, *args, **kwargs):
        raise AssertionError("a native select must not be typed into")


class _FakeFileInput:
    def __init__(self, *, id=None, name=None, accept=None):
        self._attrs = {"id": id, "name": name, "accept": accept}
        self.set_input_files_calls = 0
        self.uploaded = None

    def get_attribute(self, name):
        return self._attrs.get(name)

    def set_input_files(self, files):
        self.set_input_files_calls += 1
        self.uploaded = files

    def input_value(self):
        return self.uploaded or ""

    def click(self, *args, **kwargs):
        raise AssertionError("resume upload must go via set_input_files, not a click")


class _FakeSweepLocator:
    def __init__(self, *, attrs=None, visible=True, text=""):
        self._attrs = attrs or {}
        self._visible = visible
        self._text = text

    def get_attribute(self, name):
        return self._attrs.get(name)

    def is_visible(self):
        return self._visible

    def inner_text(self):
        return self._text


class _FakeLocatorSet:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class _PageBoundContext:
    """The fake page's owning BrowserContext. `install_never_send` now targets
    the CONTEXT (so the guard covers every page/popup the context opens); the
    registration is recorded here AND mirrored onto the page so the existing
    page-level assertions keep working unchanged."""

    def __init__(self, page):
        self._page = page
        self.routed = []

    def route(self, pattern, handler):
        self.routed.append((pattern, handler))
        self._page.route(pattern, handler)


class _FakeLeverPage:
    """One fake page driving the WHOLE fill() sequence for Lever: native text
    and select controls (auto-built from the field map by locator role), the
    resume file input, and the sweep_required CSS selectors. Records every
    locator request so a test can assert the native path was used and no
    react-select / EEO control was ever touched."""

    def __init__(self, fieldmap, *,
                 url="https://jobs.lever.co/fauxcorp/9001/apply",
                 sweep_required_labels=_SAFE_REQUIRED_LABELS,
                 file_inputs=None, bad_keys=()):
        self._url = url
        self.controls = {}
        for fld in fieldmap.fields:
            role = fld.locator.role
            key = (role, fld.locator.name)
            if role == "textbox":
                self.controls[key] = (_FakeBadTextLocator()
                                      if fld.key in bad_keys
                                      else _FakeTextLocator())
            elif role == "combobox":
                self.controls[key] = _FakeSelectLocator()
        self.file_inputs = (list(file_inputs) if file_inputs is not None else
                            [_FakeFileInput(id="resume", name="resume",
                                           accept=".pdf,.doc,.docx,.txt,.rtf")])
        self.routed = []
        self.context = _PageBoundContext(self)
        self.requested = []
        self.located_css = []
        self._sweep_required = [
            _FakeSweepLocator(attrs={"aria-label": label})
            for label in sweep_required_labels]

    @property
    def url(self):
        return self._url

    def route(self, pattern, handler):
        self.routed.append((pattern, handler))

    def get_by_role(self, role, name=None):
        self.requested.append(("role", role, name))
        return self.controls[(role, name)]

    def get_by_label(self, label):
        self.requested.append(("label", label))
        return self.controls[(None, label)]

    def query_selector_all(self, selector):
        if "file" in selector:
            return list(self.file_inputs)
        return []

    def wait_for_timeout(self, ms):  # pragma: no cover - native path never waits
        pass

    def locator(self, css):
        self.located_css.append(css)
        from engine.providers import base
        if css == base._REQUIRED_CSS:
            return _FakeLocatorSet(self._sweep_required)
        if css == base._ASTERISK_CSS:
            return _FakeLocatorSet([])
        return _FakeLocatorSet([])


# =============================================================================
# capture / apply_url: thin delegation to the registry
# =============================================================================


def test_capture_delegates_to_registry_capture(monkeypatch):
    # _registry.get("lever").capture is a call-time lazy_call targeting
    # engine.providers.lever:capture, which lazily imports and calls
    # engine.providers.lever.capture.capture_lever at CALL time; patching that module
    # attribute proves capture() rides the SAME registry wiring end to end.
    calls = []

    def fake_capture(slug, job_id, browser_factory=None, *, now=None):
        calls.append((slug, job_id))
        return "SENTINEL"

    from importlib import import_module
    monkeypatch.setattr(import_module("engine.providers.lever.capture"),
                        "capture_lever", fake_capture)
    result = lever.capture("fauxcorp", "9001", opener="IGNORED")
    assert result == "SENTINEL"
    assert calls == [("fauxcorp", "9001")]
    assert _registry.get("lever").capture._target == ("engine.providers.lever", "capture")


def test_apply_url_delegates_to_registry_apply_url():
    assert (lever.apply_url("fauxcorp", "9001")
           == "https://jobs.lever.co/fauxcorp/9001/apply")


def test_lever_module_satisfies_provider_protocol():
    # The load-bearing conformance check: the module-scope shape structurally
    # satisfies the SAME Provider Protocol greenhouse does.
    assert isinstance(lever, protocol.Provider)
    assert lever.vendor == "lever"


# =============================================================================
# resolve_values: INHERITED hole-fix e structural CV/photo choice
# =============================================================================


def test_resolve_values_inherits_cv_atsi_when_no_photo_field(tmp_path):
    # The primary fixture has a resume file input but NO photo/image upload
    # field -> the kernel negative branch embeds the photo via the ATSI CV.
    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    resume = {fv.key: fv for fv in values.fields}["resume"]
    assert resume.asset == "cv-atsi"
    assert Path(resume.value).name == "cv-atsi.pdf"
    assert "no photo field" in resume.upload_reason


def test_resolve_values_inherits_cv_ats_and_photo_when_photo_present(tmp_path):
    # A field map that DOES carry an image/photo upload field: the structural
    # signal fires -> resume is the plain ATS CV and the photo attaches.
    fieldmap = FieldMap(vendor="lever", posting_id="9001", captured_at=_PINNED,
                        fields=[
        Field(key="resume", label="Resume / CV", type="input_file",
             required=True, options=[], source="lever_dom",
             locator=Locator(role="button", name="Resume / CV")),
        Field(key="photo", label="Profile photo", type="input_file",
             required=False, options=[], source="lever_dom",
             locator=Locator(role="button", name="Profile photo")),
    ])
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    by_key = {fv.key: fv for fv in values.fields}
    assert by_key["resume"].asset == "cv-ats"
    assert "photo field present" in by_key["resume"].upload_reason
    assert by_key["photo"].asset == "photo"
    assert Path(by_key["photo"].value).name == "Me.png"


# =============================================================================
# fill(): the ordered Provider-contract sequence (native DOM path)
# =============================================================================


def test_fill_completes_when_all_safe_required_land(tmp_path):
    # A Lever form whose required set is all safely fillable (text + native
    # select + resume; no required checkbox): every required field lands and
    # readback-confirms -> COMPLETE.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeLeverPage(fieldmap)

    report = lever.fill(page, fieldmap, values)

    assert report.vendor == "lever"
    # (1) never-send installed exactly once.
    assert len(page.routed) == 1 and page.routed[0][0] == "**"
    # (2)+(3) text via type_human, native select via select_option, resume via
    # set_input_files -- every one readback-confirmed.
    assert page.controls[("textbox", "Full name")].input_value() == "Test Candidate"
    assert page.controls[("textbox", "Email")].input_value() == \
        "test.candidate@example.invalid"
    sponsor = page.controls[("combobox", _SPONSOR_LABEL)]
    assert sponsor.select_option_calls == 1
    assert sponsor.input_value() == "No"
    assert page.file_inputs[0].set_input_files_calls == 1
    assert report.readback_mismatches == []
    # (4) live DOM sweep agrees with the capture-time required set -> no gap.
    assert report.required_unfilled == []
    assert report.complete is True
    assert report.caption().endswith("COMPLETE")
    assert not report.caption().endswith("NOT COMPLETE")


def test_fill_drives_native_select_never_react_select_combobox(tmp_path):
    # The native <select> is driven by select_option, and NO react-select
    # locator (`#react-select-...`) is ever requested -- the core Lever override.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeLeverPage(fieldmap)

    lever.fill(page, fieldmap, values)

    assert page.controls[("combobox", _SPONSOR_LABEL)].select_option_calls == 1
    assert not any("react-select" in css for css in page.located_css)


def test_fill_required_checkbox_is_human_handoff_and_not_complete(tmp_path):
    # The full fixture carries a REQUIRED consent checkbox. resolve_values ticks
    # it True, but fill() refuses to auto-click it (hCaptcha) and hands it off;
    # being required, the hand-off forces NOT_COMPLETE and the checkbox control
    # is NEVER touched (no get_by_role for it, no .check()).
    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    # resolve_values did resolve the consent checkbox to a boolean True value...
    consent = {fv.key: fv for fv in values.fields}[_CONSENT_KEY]
    assert consent.value is True

    page = _FakeLeverPage(fieldmap, sweep_required_labels=(
        _SAFE_REQUIRED_LABELS + (
            "I agree to the privacy policy and consent to the processing of my "
            "data.",)))
    report = lever.fill(page, fieldmap, values)

    assert report.complete is False
    gap = {g["key"]: g for g in report.required_unfilled}
    assert _CONSENT_KEY in gap
    assert "hCaptcha" in gap[_CONSENT_KEY]["reason"]
    # ...but fill() never requested a locator for it (no auto-click path).
    assert not any(_CONSENT_KEY in str(req) or "privacy policy" in str(req)
                   for req in page.requested)
    assert report.caption().endswith("NOT COMPLETE")


def test_fill_dom_sweep_extra_required_field_forces_not_complete(tmp_path):
    # DOM-SWEEP-AS-PRIMARY: the live sweep shows a required control the
    # capture-time (DOM-derived) field map never carried. For Lever the live
    # sweep wins, so this dom_only gap forces NOT_COMPLETE even though every
    # captured field landed.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeLeverPage(fieldmap, sweep_required_labels=(
        _SAFE_REQUIRED_LABELS + ("Cover Letter",)))

    report = lever.fill(page, fieldmap, values)

    assert report.complete is False
    reasons = [g["reason"] for g in report.required_unfilled]
    assert any("DOM sweep is authoritative" in r for r in reasons)
    assert report.caption().endswith("NOT COMPLETE")


def test_fill_readback_mismatch_on_required_field_forces_not_complete(tmp_path):
    # A required field whose value silently never takes (readback empty) must
    # NOT count as filled -> it surfaces as a genuine required gap.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeLeverPage(fieldmap, bad_keys={"email"})

    report = lever.fill(page, fieldmap, values)

    assert report.complete is False
    assert any(g["key"] == "email" for g in report.required_unfilled)
    assert any(m["key"] == "email" for m in report.readback_mismatches)


def test_fill_never_touches_eeo_demographic_field(tmp_path):
    # The Gender decline field is classified manual-only and skipped: fill()
    # never drives it (no get_by_role / select_option for the Gender control).
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    assert not any(fv.key == _GENDER_KEY for fv in values.fields)

    page = _FakeLeverPage(fieldmap)
    lever.fill(page, fieldmap, values)

    assert not any("Gender" in str(req) for req in page.requested)


def test_fill_never_send_interceptor_registered_before_any_field_access(tmp_path):
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)

    order = []

    class _OrderTrackingPage(_FakeLeverPage):
        def route(self, pattern, handler):
            order.append("route")
            super().route(pattern, handler)

        def get_by_role(self, role, name=None):
            order.append("get_by_role")
            return super().get_by_role(role, name=name)

        def query_selector_all(self, selector):
            order.append("query_selector_all")
            return super().query_selector_all(selector)

    page = _OrderTrackingPage(fieldmap)
    lever.fill(page, fieldmap, values)

    assert order[0] == "route"


def test_fill_raises_on_navigation_during_fill(tmp_path):
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)

    class _NavigatingPage(_FakeLeverPage):
        def get_by_role(self, role, name=None):
            self._url = "https://jobs.lever.co/fauxcorp/thanks"
            return super().get_by_role(role, name=name)

    page = _NavigatingPage(fieldmap)
    with pytest.raises(FillSafetyError, match="navigated during fill"):
        lever.fill(page, fieldmap, values)


def test_fill_report_reuses_the_existing_fillreport_dataclass(tmp_path):
    from engine.kernel.contracts import FillReport

    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeLeverPage(fieldmap)
    report = lever.fill(page, fieldmap, values)
    assert isinstance(report, FillReport)
    blob = report.to_dict()
    assert blob["vendor"] == "lever"
    assert blob["company"] == "9001"      # posting_id fallback (no company slug)


# =============================================================================
# capture path: lever server-rendered DOM parse (offline, fake-driven)
# =============================================================================
# No playwright, no network: the capture path is driven through a fake browser
# factory over a server-rendered lever apply-page DOM fixture. The autouse
# no-network guard is satisfied throughout. Schema compliance, source tags, and
# coverage() interop are asserted on the produced FieldMaps; shape drift is
# proven to raise CaptureShapeError rather than yield an empty map.
#
# DELIBERATE per-vendor duplication (owner-ratified 2026-07-10): vendor test files
# stay self-contained so the per-vendor development loops never co-edit a shared
# file (DRY exception: decoupling outweighs consolidation here). The fake page
# below will diverge as each vendor's capture path evolves; a shared copy would
# become a co-edit surface. Do not consolidate.


class _FakePage:
    """A stand-in for a Playwright page: records the goto and serves the
    server-rendered fixture DOM via content(). Lever's capture path reads
    content() only; it drives no response handlers (unlike the ashby GraphQL
    fake, whose page fires scripted responses into on("response") listeners)."""

    def __init__(self, *, html=""):
        self._html = html
        self.goto_calls = []

    def set_default_timeout(self, ms):
        pass

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))

    def content(self):
        return self._html


def _factory_for(page):
    @contextlib.contextmanager
    def factory():
        yield page
    return factory


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


def test_lever_capture_dedups_hidden_base_field_mirrors(lever_apply_html):
    """Round-3 live finding: the apply page renders every base field TWICE,
    an invisible mirror carrying the true submission `name` with no label,
    plus a labeled visible twin. The parser must collapse each duplicate
    pair back into ONE logical Field, keeping the human label, OR-ing
    `required` across the pair, and preferring the richer-source `type`."""
    page = _FakePage(html=lever_apply_html)
    fm = capture_lever("globex", "req-77", _factory_for(page), now=lambda: _PINNED)

    # exactly one Field per logical base key: 7 base + 2 custom cards + 1
    # inline-text consent checkbox = 10, never 17 raw duplicated entries
    keys = [f.key for f in fm.fields]
    assert len(keys) == len(set(keys)) == 10

    by_key = {f.key: f for f in fm.fields}
    assert by_key["name"].label == "Full name"
    assert by_key["email"].label == "Email"
    assert by_key["phone"].label == "Phone"
    assert by_key["org"].label == "Current company"
    assert by_key["urls[LinkedIn]"].label == "LinkedIn URL"
    assert by_key["urls[GitHub]"].label == "GitHub URL"
    assert by_key["resume"].label == "Resume / CV"

    # `required` is the OR of the hidden mirror and its visible twin: the
    # hidden `org` mirror is required even though the visible one is not
    assert by_key["org"].required is True
    # the hidden `resume` mirror is NOT required, but its visible twin is
    assert by_key["resume"].required is True
    # type comes from the richer source: the file-upload widget outranks
    # the hidden mirror's default input_text
    assert by_key["resume"].type == "input_file"


def test_lever_capture_empty_label_falls_back_to_enclosing_text(lever_apply_html):
    """Round-3 live finding: a consent checkbox's wording sits inline inside
    its own <label>, not in a `.application-label` div. The captured field
    must never carry an empty label (item 2)."""
    page = _FakePage(html=lever_apply_html)
    fm = capture_lever("globex", "req-77", _factory_for(page), now=lambda: _PINNED)

    by_key = {f.key: f for f in fm.fields}
    consent = by_key["consent[marketing]"]
    assert consent.label == (
        "I would like to receive occasional updates about new roles.")
    assert consent.type == "boolean"
    assert consent.required is False
    for fld in fm.fields:
        assert fld.label != ""


def test_lever_custom_checkbox_with_no_extractable_text_falls_back_to_key():
    """When NEITHER a label element, aria-label, placeholder, nor any
    enclosing text can be found, the field falls back to a descriptive
    `(unlabeled: <key>)` label rather than an empty string."""
    html = (
        "<html><body><form>"
        "<ul class=\"application-fields\">"
        "<li class=\"application-field\">"
        "<label class=\"application-label\">Full name</label>"
        "<input type=\"text\" name=\"name\" required>"
        "</li>"
        "</ul>"
        "<div class=\"application-question\">"
        "<input type=\"checkbox\" name=\"consent[opt_in]\">"
        "</div>"
        "</form></body></html>")
    page = _FakePage(html=html)
    fm = capture_lever("globex", "req-99", _factory_for(page))

    by_key = {f.key: f for f in fm.fields}
    assert by_key["consent[opt_in]"].label == "(unlabeled: consent[opt_in])"


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
    # required: name, email, org, resume (base) + select + textarea (custom)
    # = 6 (org is required post-dedup: its hidden mirror carries `required`
    # even though the visible twin does not)
    assert report.required_total == 6
    by_key = {f.key: f for f in report.fields}
    assert by_key["resume"].status == MANUAL_ONLY
    assert isinstance(report.summary_line(), str)


def test_lever_raises_when_form_absent():
    page = _FakePage(html="<html><body><p>This position is closed.</p></body></html>")
    with pytest.raises(CaptureShapeError, match="no recognizable form fields"):
        capture_lever("globex", "req-77", _factory_for(page))
