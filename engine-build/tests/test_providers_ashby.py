"""Ashby provider (engine.providers.ashby): the THIRD reference implementation
of the `Provider` contract, W5.3 -- the schema path WITH a controlled-component
select driver.

No patchright, no network: `fill()` is driven through a FAKE page/locator
harness mirroring the representative apply DOM behind the captured Ashby
`non-user-graphql` `ApiJobPosting` schema fixture at
`tests/fixtures/providers/ashby/form.json` (base text fields, a React
CONTROLLED-COMPONENT single-select = the visa/sponsorship question, a REQUIRED
Turnstile-gated consent checkbox, an EEOC "Gender" decline field, a resume file
input, and a hidden source field). The field map comes from that fixture parsed
through the REAL (offline) `browse._parse_ashby` -- Ashby HAS a real schema, so
the graphql form is authoritative and the DOM sweep is a CROSS-CHECK
(greenhouse semantics), NOT the primary oracle (Lever semantics). The SSOT is a
hand-built FAKE (no owner PII).

The fake select control (`_FakeAshbyReactSelect`) MODELS React's
controlled-component commit: its readback reflects the COMMITTED state, which
updates ONLY when the driver dispatches the input/change/blur events (the
berellevy technique). A naive value-set with no event dispatch is silently
ignored (readback stays empty), so `test_naive_value_set_never_commits_but_
driver_does` proves the driver actually commits and a naive set would fail.

A real live-browser HAR capture against the real DOM is a SEPARATE later step
(the W5.2/W5.3 fixture-validation promise in providers/base.py); this suite
proves the LOGIC offline, matching test_providers_greenhouse.py /
test_providers_lever.py.
"""

import json
from pathlib import Path

import pytest

# Import engine.fill at module load (before the autouse no_network fixture
# patches socket.socket): ashby.py imports engine.fill lazily at call time, and
# a FIRST import under the socket patch would drag in ssl (class SSLSocket(
# socket)) and fail. Mirrors test_providers_greenhouse.py / test_providers_lever.py.
import engine.fill  # noqa: F401
from engine import browse
from engine.fieldmap import Field, FieldMap, Locator
from engine.fill import FillAssets, FillSafetyError
from engine.profile_map import profile_from_real_ssot
from engine.providers import _registry, ashby, protocol
from engine.ssot import SSOT

_FIXTURES = Path(__file__).parent / "fixtures" / "providers" / "ashby"
_PINNED = "2026-07-03T00:00:00+00:00"

_VISA_LABEL = ("Will you now or in the future require visa sponsorship for "
               "employment?")
_VISA_KEY = "custom_visa"
_CONSENT_LABEL = ("I agree to the privacy policy and consent to the processing "
                  "of my data.")
_CONSENT_KEY = "custom_consent"
_GENDER_KEY = "custom_gender"
# The required fields the primary fixture's fill() can safely land (everything
# but the Turnstile-hazard consent checkbox): the DOM-sweep required set for a
# COMPLETE run.
_SAFE_REQUIRED_LABELS = ("Full name", "Email", "Resume", _VISA_LABEL)


# -- fixture loaders -------------------------------------------------------


def _fieldmap() -> FieldMap:
    """The full fixture field map, parsed through the REAL offline graphql
    parser (`browse._parse_ashby`) -- Ashby's authoritative schema oracle."""
    raw = json.loads((_FIXTURES / "form.json").read_text())
    posting = raw["data"]["jobPosting"]
    return browse._parse_ashby(posting, "fauxcorp", "9001", now=lambda: _PINNED)


def _fieldmap_without(*keys: str) -> FieldMap:
    """The fixture field map minus the named field(s) -- models an Ashby form
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
            "phone": "+39 000 0000000",
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
    return ashby.resolve_values(fieldmap, ssot, profile, assets=assets)


# -- fake DOM harness (mirrors the schema-derived apply DOM) ----------------


class _FakeTextLocator:
    """A plain text/email/phone input: type_human -> press_sequentially,
    readback via input_value(). Raises on the forbidden fill()/click()/check()
    paths (the reCAPTCHA v3 human-cadence + Turnstile no-auto-click invariants)."""

    def __init__(self):
        self.value = ""

    def press_sequentially(self, ch, delay=None):
        self.value += ch

    def input_value(self):
        return self.value

    def get_attribute(self, name):
        return None

    def fill(self, *args, **kwargs):
        raise AssertionError("Ashby text fields must use type_human, never fill()")

    def check(self, *args, **kwargs):
        raise AssertionError("Ashby must never programmatically .check() (Turnstile)")

    def click(self, *args, **kwargs):
        raise AssertionError("Ashby must never programmatically .click() a field")

    def select_option(self, *args, **kwargs):
        raise AssertionError("a text field must not be select_option'd")

    def evaluate(self, *args, **kwargs):
        raise AssertionError("a text field must not go through the controlled-"
                             "component evaluate driver (that is for selects)")


class _FakeBadTextLocator(_FakeTextLocator):
    """A text input whose value silently never takes (readback always empty):
    exercises the readback-gate rejecting a value the page dropped."""

    def input_value(self):
        return ""


class _FakeAshbyReactSelect:
    """Models an Ashby React CONTROLLED-COMPONENT select/combobox.

    React tracks its OWN component state: the readback (`input_value`) reflects
    the COMMITTED state, which updates only when the proper `input`/`change`
    events are dispatched after a value set (plus a `blur` for onBlur). A naive
    value-set with NO event dispatch (`set_value_naive`) is silently ignored --
    the committed value stays empty -- which is exactly the behaviour the
    berellevy driver (native setter + event dispatch) must overcome, and exactly
    why a naive fill() would fail to commit."""

    def __init__(self):
        self._dom_value = ""      # the raw element value (what a bare set writes)
        self._committed = ""      # React state; the readback source
        self.events = []
        self.evaluate_calls = 0

    # -- the berellevy commit path: native value-set + event dispatch ----------
    def evaluate(self, script, arg=None):
        self.evaluate_calls += 1
        # Model _REACT_CONTROLLED_COMMIT_JS: the native setter writes the value,
        # then input/change/blur are dispatched, so React's onChange commits.
        self._set_native(arg)
        for event_type in ("input", "change", "blur"):
            self._dispatch(event_type)
        return None

    def _set_native(self, value):
        self._dom_value = value

    def _dispatch(self, event_type):
        self.events.append(event_type)
        # React's onChange/onBlur reads el.value into component state on
        # input/change -> the committed (readback) value follows the DOM value.
        if event_type in ("input", "change"):
            self._committed = self._dom_value

    def input_value(self):
        return self._committed

    def get_attribute(self, name):
        return None

    # -- naive value-set (no events): the path that must NOT commit -------------
    def set_value_naive(self, value):
        """A bare React-ignored value-set: writes the DOM value but dispatches
        NO events, so the controlled component never commits it."""
        self._set_native(value)

    # -- forbidden paths: ashby.py must never use these on a select ------------
    def fill(self, *args, **kwargs):
        raise AssertionError(
            "an Ashby select must commit via the controlled-component driver "
            "(native setter + input/change/blur), never a naive fill()")

    def select_option(self, *args, **kwargs):
        raise AssertionError(
            "an Ashby select has no native <select>; select_option is Lever's "
            "path and must never be used here")

    def click(self, *args, **kwargs):
        raise AssertionError("an Ashby select must not be clicked (Turnstile)")

    def check(self, *args, **kwargs):
        raise AssertionError("an Ashby select must not be checked")

    def press_sequentially(self, *args, **kwargs):
        raise AssertionError("an Ashby select is not typed into char-by-char")


class _FakeAshbyReactSelectBad(_FakeAshbyReactSelect):
    """A controlled component that REJECTS the value (e.g. React re-renders from
    stale props): the input/change/blur events fire, but the committed state
    never follows, so the readback stays empty and the driver must NOT count it
    as filled."""

    def _dispatch(self, event_type):
        self.events.append(event_type)   # events fire, but state never commits


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


class _FakeAshbyPage:
    """One fake page driving the WHOLE fill() sequence for Ashby: native text
    controls, the React controlled-component select(s), the resume file input,
    and the sweep_required CSS selectors. Records every locator request so a
    test can assert the controlled-component path was used and no react-select /
    native-select / EEO / checkbox control was ever touched."""

    def __init__(self, fieldmap, *,
                 url="https://jobs.ashbyhq.com/fauxcorp/9001/application",
                 sweep_required_labels=_SAFE_REQUIRED_LABELS,
                 file_inputs=None, bad_text_keys=(), bad_select_keys=()):
        self._url = url
        self.controls = {}
        for fld in fieldmap.fields:
            role = fld.locator.role
            key = (role, fld.locator.name)
            if role == "textbox":
                self.controls[key] = (_FakeBadTextLocator()
                                      if fld.key in bad_text_keys
                                      else _FakeTextLocator())
            elif role in ("combobox", "listbox"):
                self.controls[key] = (_FakeAshbyReactSelectBad()
                                      if fld.key in bad_select_keys
                                      else _FakeAshbyReactSelect())
        self.file_inputs = (list(file_inputs) if file_inputs is not None else
                            [_FakeFileInput(id="_systemfield_resume",
                                           name="_systemfield_resume",
                                           accept=".pdf,.doc,.docx,.txt,.rtf")])
        self.routed = []
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

    def wait_for_timeout(self, ms):  # pragma: no cover - the driver never waits
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
    # _registry.get("ashby").capture is a call-time lazy_call targeting
    # engine.providers.ashby:capture, which lazily imports and calls
    # engine.providers.ashby.capture.capture_ashby at CALL time; patching that module
    # attribute proves capture() rides the SAME registry wiring end to end.
    calls = []

    def fake_capture(slug, job_id, browser_factory=None, *, now=None):
        calls.append((slug, job_id))
        return "SENTINEL"

    from importlib import import_module
    monkeypatch.setattr(import_module("engine.providers.ashby.capture"),
                        "capture_ashby", fake_capture)
    result = ashby.capture("fauxcorp", "9001", opener="IGNORED")
    assert result == "SENTINEL"
    assert calls == [("fauxcorp", "9001")]
    assert _registry.get("ashby").capture._target == ("engine.providers.ashby", "capture")


def test_apply_url_delegates_to_registry_apply_url():
    assert (ashby.apply_url("fauxcorp", "9001")
           == "https://jobs.ashbyhq.com/fauxcorp/9001/application")


def test_ashby_module_satisfies_provider_protocol():
    # The load-bearing conformance check: the module-scope shape structurally
    # satisfies the SAME Provider Protocol greenhouse and lever do.
    assert isinstance(ashby, protocol.Provider)
    assert ashby.vendor == "ashby"


# =============================================================================
# resolve_values: INHERITED hole-fix e structural CV/photo choice
# =============================================================================


def test_resolve_values_inherits_cv_atsi_when_no_photo_field(tmp_path):
    # The primary fixture has a resume file input but NO photo/image upload
    # field -> the structural rule's negative branch picks the embedded-photo
    # ATSI CV (there is nowhere on the form to carry the portrait separately).
    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    resume = {fv.key: fv for fv in values.fields}["_systemfield_resume"]
    assert resume.asset == "cv-atsi"
    assert Path(resume.value).name == "cv-atsi.pdf"
    assert "no photo field" in resume.upload_reason


def test_resolve_values_inherits_cv_ats_and_photo_when_photo_present(tmp_path):
    # A field map that DOES carry an image/photo upload field: the structural
    # signal fires -> resume stays the plain ATS CV and the real photo attaches
    # separately on the photo field.
    fieldmap = FieldMap(vendor="ashby", posting_id="9001", captured_at=_PINNED,
                        fields=[
        Field(key="_systemfield_resume", label="Resume", type="input_file",
             required=True, options=[], source="ashby_graphql",
             locator=Locator(role="button", name="Resume")),
        Field(key="custom_photo", label="Profile photo", type="input_file",
             required=False, options=[], source="ashby_graphql",
             locator=Locator(role="button", name="Profile photo")),
    ])
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    by_key = {fv.key: fv for fv in values.fields}
    assert by_key["_systemfield_resume"].asset == "cv-ats"
    assert "photo field present" in by_key["_systemfield_resume"].upload_reason
    assert by_key["custom_photo"].asset == "photo"
    assert Path(by_key["custom_photo"].value).name == "Me.png"


# =============================================================================
# fill(): the ordered Provider-contract sequence (schema + controlled-select)
# =============================================================================


def test_fill_completes_when_all_safe_required_land(tmp_path):
    # An Ashby form whose required set is all safely fillable (text + the React
    # controlled-component select + resume; no required checkbox): every
    # required field lands and readback-confirms -> COMPLETE.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeAshbyPage(fieldmap)

    report = ashby.fill(page, fieldmap, values)

    assert report.vendor == "ashby"
    # (1) never-send installed exactly once.
    assert len(page.routed) == 1 and page.routed[0][0] == "**"
    # (2)+(3) text via type_human, the select via the controlled-component
    # driver, resume via set_input_files -- every one readback-confirmed.
    assert page.controls[("textbox", "Full name")].input_value() == \
        "Test Candidate"
    assert page.controls[("textbox", "Email")].input_value() == \
        "test.candidate@example.invalid"
    visa = page.controls[("combobox", _VISA_LABEL)]
    assert visa.input_value() == "No"
    assert visa.evaluate_calls == 1
    assert visa.events == ["input", "change", "blur"]
    assert page.file_inputs[0].set_input_files_calls == 1
    assert report.readback_mismatches == []
    # (4) live DOM sweep agrees with the schema required set -> no gap.
    assert report.required_unfilled == []
    assert report.complete is True
    assert report.caption().endswith("COMPLETE")
    assert not report.caption().endswith("NOT COMPLETE")


def test_fill_drives_select_via_controlled_component_never_react_or_native(tmp_path):
    # The select commits through the OWN controlled-component driver (evaluate =
    # native setter + input/change/blur), and NEITHER a react-select locator
    # (`#react-select-...`) NOR a native select_option is ever used -- the core
    # Ashby override. The fake select raises on fill()/select_option()/click(),
    # so any naive path would fail the run loudly.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeAshbyPage(fieldmap)

    ashby.fill(page, fieldmap, values)

    visa = page.controls[("combobox", _VISA_LABEL)]
    assert visa.evaluate_calls == 1
    assert "input" in visa.events and "change" in visa.events
    assert not any("react-select" in css for css in page.located_css)


def test_naive_value_set_never_commits_but_driver_does(tmp_path):
    # The load-bearing proof: a naive value-set (no event dispatch) is silently
    # ignored by the controlled component (readback stays empty), while the
    # ashby controlled-component driver dispatches input/change/blur and the
    # value commits. Same control, two outcomes -> the driver is what commits.
    control = _FakeAshbyReactSelect()

    # naive: set the DOM value with NO event dispatch -> React never commits it.
    control.set_value_naive("No")
    assert control.input_value() == ""          # naive value-set FAILS to commit
    assert control.events == []

    # driver: the berellevy native-setter + input/change/blur commit.
    ashby._apply_ashby_controlled(control, "No")
    assert control.input_value() == "No"        # driver COMMITS
    assert control.events == ["input", "change", "blur"]


def test_fill_readback_gate_rejects_a_select_that_never_commits(tmp_path):
    # A required select whose controlled component rejects the value (events
    # fire but the state never commits) must NOT count as filled -> it surfaces
    # as a genuine required gap and a readback mismatch.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeAshbyPage(fieldmap, bad_select_keys={_VISA_KEY})

    report = ashby.fill(page, fieldmap, values)

    assert report.complete is False
    assert any(g["key"] == _VISA_KEY for g in report.required_unfilled)
    assert any(m["key"] == _VISA_KEY for m in report.readback_mismatches)
    # the events DID fire (the driver tried) -- the component just rejected them.
    assert page.controls[("combobox", _VISA_LABEL)].events == [
        "input", "change", "blur"]


def test_fill_required_checkbox_is_turnstile_handoff_and_not_complete(tmp_path):
    # The full fixture carries a REQUIRED consent checkbox. resolve_values ticks
    # it True, but fill() refuses to auto-click it (Turnstile) and hands it off;
    # being required, the hand-off forces NOT_COMPLETE and the checkbox control
    # is NEVER touched (no get_by_role for it, no .check()).
    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    # resolve_values did resolve the consent checkbox to a boolean True value...
    consent = {fv.key: fv for fv in values.fields}[_CONSENT_KEY]
    assert consent.value is True

    page = _FakeAshbyPage(fieldmap, sweep_required_labels=(
        _SAFE_REQUIRED_LABELS + (_CONSENT_LABEL,)))
    report = ashby.fill(page, fieldmap, values)

    assert report.complete is False
    gap = {g["key"]: g for g in report.required_unfilled}
    assert _CONSENT_KEY in gap
    assert "Turnstile" in gap[_CONSENT_KEY]["reason"]
    # ...but fill() never requested a locator for it (no auto-click path).
    assert not any(_CONSENT_KEY in str(req) or "privacy policy" in str(req)
                   for req in page.requested)
    assert report.caption().endswith("NOT COMPLETE")


def test_fill_dom_sweep_cross_check_schema_only_forces_not_complete(tmp_path):
    # CROSS-CHECK (greenhouse semantics): every field lands, but the live DOM
    # sweep does NOT carry a schema-required field ("Resume"). The schema is
    # authoritative and the sweep is a cross-check, so this schema_only mismatch
    # forces NOT_COMPLETE with the greenhouse wording.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    mismatched = tuple(label for label in _SAFE_REQUIRED_LABELS
                       if label != "Resume")
    page = _FakeAshbyPage(fieldmap, sweep_required_labels=mismatched)

    report = ashby.fill(page, fieldmap, values)

    assert report.filled >= 4          # the fields still landed
    assert report.complete is False
    reasons = [g["reason"] for g in report.required_unfilled]
    assert any("did not find it required" in r for r in reasons)
    assert not any("authoritative" in r for r in reasons)   # NOT Lever wording
    assert report.caption().endswith("NOT COMPLETE")


def test_fill_dom_sweep_cross_check_dom_only_forces_not_complete(tmp_path):
    # The opposite direction: the DOM shows a required control the schema never
    # captured (dom_only) -- the cross-check catches a field the schema missed.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeAshbyPage(fieldmap, sweep_required_labels=(
        _SAFE_REQUIRED_LABELS + ("Cover Letter",)))

    report = ashby.fill(page, fieldmap, values)

    assert report.complete is False
    reasons = [g["reason"] for g in report.required_unfilled]
    assert any("absent from the schema" in r for r in reasons)
    assert report.caption().endswith("NOT COMPLETE")


def test_fill_readback_mismatch_on_required_text_forces_not_complete(tmp_path):
    # A required text field whose value silently never takes (readback empty)
    # must NOT count as filled -> it surfaces as a genuine required gap.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeAshbyPage(fieldmap, bad_text_keys={"_systemfield_email"})

    report = ashby.fill(page, fieldmap, values)

    assert report.complete is False
    assert any(g["key"] == "_systemfield_email"
               for g in report.required_unfilled)
    assert any(m["key"] == "_systemfield_email"
               for m in report.readback_mismatches)


def test_fill_never_touches_eeo_demographic_field(tmp_path):
    # The Gender decline field is classified manual-only and skipped: fill()
    # never drives it (no get_by_role for the Gender control).
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    assert not any(fv.key == _GENDER_KEY for fv in values.fields)

    page = _FakeAshbyPage(fieldmap)
    ashby.fill(page, fieldmap, values)

    assert not any("Gender" in str(req) for req in page.requested)


def test_fill_never_send_interceptor_registered_before_any_field_access(tmp_path):
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)

    order = []

    class _OrderTrackingPage(_FakeAshbyPage):
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
    ashby.fill(page, fieldmap, values)

    assert order[0] == "route"


def test_fill_raises_on_navigation_during_fill(tmp_path):
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)

    class _NavigatingPage(_FakeAshbyPage):
        def get_by_role(self, role, name=None):
            self._url = "https://jobs.ashbyhq.com/fauxcorp/thanks"
            return super().get_by_role(role, name=name)

    page = _NavigatingPage(fieldmap)
    with pytest.raises(FillSafetyError, match="navigated during fill"):
        ashby.fill(page, fieldmap, values)


def test_fill_report_reuses_the_existing_fillreport_dataclass(tmp_path):
    from engine.fill import FillReport

    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeAshbyPage(fieldmap)
    report = ashby.fill(page, fieldmap, values)
    assert isinstance(report, FillReport)
    blob = report.to_dict()
    assert blob["vendor"] == "ashby"
    # posting_id fallback (no company slug supplied).
    assert blob["company"] == fieldmap.posting_id
