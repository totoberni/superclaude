"""Greenhouse provider (engine.providers.greenhouse): the FIRST reference
implementation of the `Provider` contract, W5.2.

No patchright, no network: `fill()` is driven through a FAKE page/locator
harness mirroring the representative DOM fixture at
`tests/fixtures/providers/greenhouse/dom.html` (react-select combobox, a
required text field, a required file input, an optional image-accept file
input, and an EEOC decline field with no required marker). The captured
schema comes from `tests/fixtures/providers/greenhouse/questions.json`
parsed through the real (offline) `fieldmap.parse_greenhouse`. The SSOT is a
hand-built FAKE (no owner PII). Real live-browser HAR capture against the
real DOM is a SEPARATE later step (W5.2 fixture-validation promise in
providers/base.py); this suite proves the LOGIC offline, matching the
convention already established by test_providers_base.py.
"""

import json
from pathlib import Path

import pytest

# Import engine.fill at module load (before the autouse no_network fixture
# patches socket.socket): greenhouse.py's fill()/resolve_values() import it
# lazily at call time, and a FIRST import under the socket patch would drag
# in ssl (class SSLSocket(socket)) and fail. Mirrors test_providers_base.py.
import engine.fill  # noqa: F401
from engine.fieldmap import Field, FieldMap, Locator, parse_greenhouse
from engine.fill import FillAssets, FillSafetyError
from engine.profile_map import profile_from_real_ssot
from engine.providers import base, greenhouse, protocol, registry
from engine.providers.registry import PROVIDERS
from engine.ssot import SSOT

_FIXTURES = Path(__file__).parent / "fixtures" / "providers" / "greenhouse"
_PINNED = "2026-07-03T00:00:00+00:00"


# -- fixture loaders -------------------------------------------------------


def _questions_raw() -> dict:
    return json.loads((_FIXTURES / "questions.json").read_text())


def _fieldmap() -> FieldMap:
    return parse_greenhouse(_questions_raw(), "fakeco", "7701001",
                            now=lambda: _PINNED)


def _fake_ssot() -> SSOT:
    # FAKE, invented placeholder data only -- no owner PII, matching the
    # existing real_ssot_v14.yaml fixture's convention.
    return SSOT({
        "identity": {
            "name": "Test Candidate",
            "email": "test.candidate@example.invalid",
        },
        "canned_answers": {
            "visa_sponsorship_required": "no",
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


# -- fake DOM harness (mirrors dom.html) ------------------------------------


class _FakeTextLocator:
    """A plain text/email input: type_human -> press_sequentially, readback
    via input_value(). Raises if the forbidden fill()/type() path is used
    (reCAPTCHA v3 human-cadence invariant)."""

    def __init__(self):
        self.value = ""
        self.checked = None
        self.selected = None
        self.blurred = False

    def press_sequentially(self, ch, delay=None):
        self.value += ch

    def input_value(self):
        if self.selected is not None:
            return self.selected
        return self.value

    def is_checked(self):
        return bool(self.checked)

    def check(self):
        self.checked = True

    def select_option(self, label=None):
        self.selected = label

    def get_attribute(self, name):
        return None

    def blur(self):
        self.blurred = True

    def fill(self, *args, **kwargs):
        raise AssertionError("must never call fill() (type_human only)")


class _FakeComboInput:
    """The react-select control's OWN filter input: `_settle_focus` clicks it
    once before typing (first-char-focus fix), `type_human` then types the
    option text one keystroke at a time, `press("Enter")` commits the
    highlighted (first-filtered) option -- never a `div.select__option`
    click, mirroring real react-select -- and the driver finally dismisses
    the (already-closed) menu with Escape (never blur). Enter is modeled here
    by flipping the paired `_FakeSingleValue` from uncommitted (always reads
    "") to revealing its `reads` sequence."""

    def __init__(self, single_value=None):
        self.clicked = 0
        self.keys = []
        self.pressed = []
        self._single_value = single_value

    def click(self):
        self.clicked += 1

    def press_sequentially(self, text, delay=None):
        self.keys.append(text)

    def press(self, key):
        self.pressed.append(key)
        if key == "Enter" and self._single_value is not None:
            self._single_value.commit()

    def fill(self, *args, **kwargs):
        raise AssertionError("react-select driver must never call fill()")

    def blur(self, *args, **kwargs):
        raise AssertionError("react-select driver must never blur")


class _FakeComboControl:
    """The field's `div.select__control:has([id="react-select-<id>-
    placeholder"])` container: `.click()` opens the menu; `.locator("input")`
    reaches the control's own filter input; `.locator(".select__single-
    value")` reaches the post-selection readback node. One instance persists
    for the field's whole lifetime (react-select recycles the node; a fresh
    `page.locator(...)` call still resolves to this same fake element)."""

    def __init__(self, combo_input, single_value):
        self.clicked = 0
        self._combo_input = combo_input
        self._single_value = single_value

    def click(self):
        self.clicked += 1

    def locator(self, css):
        if css == "input":
            return self._combo_input
        if css.endswith(".select__single-value"):
            return self._single_value
        raise AssertionError(f"unexpected control-scoped locator: {css!r}")


class _FakeSingleValue:
    """Reads the rendered value. Uncommitted (before `_FakeComboInput` presses
    `Enter`) always reads "" -- no real react-select selection has landed yet.
    Once committed, a multi-element `reads` list pops one per poll so a value
    that appears only on the +500 ms read is expressible."""

    def __init__(self, reads):
        self._reads = list(reads)
        self._committed = False

    def commit(self):
        self._committed = True

    def inner_text(self):
        if not self._committed:
            return ""
        if len(self._reads) > 1:
            return self._reads.pop(0)
        return self._reads[0] if self._reads else ""


_NO_OVERRIDE = object()


class _FakeFileInput:
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


_REQUIRED_LABELS = ("First Name", "Email", "Resume/CV",
                    "Will you now or in the future require visa "
                    "sponsorship for employment?")


class _PageBoundContext:
    """The fake page's owning BrowserContext. `install_never_send` now targets
    the CONTEXT (so the guard covers every page/popup the context opens, not just
    the one page); the registration is recorded here AND mirrored onto the page
    so the existing page-level assertions and the route-ordering check keep
    working unchanged."""

    def __init__(self, page):
        self._page = page
        self.routed = []

    def route(self, pattern, handler):
        self.routed.append((pattern, handler))
        self._page.route(pattern, handler)


class _FakeGreenhousePage:
    """One fake page driving the WHOLE fill() sequence: text controls,
    the react-select combobox, file inputs, and the sweep_required CSS
    selectors, mirroring dom.html's shape end to end."""

    def __init__(self, *, url="https://boards.greenhouse.io/fakeco/jobs/7701001",
                combo_field_id="question_50001", combo_reads=("No",),
                file_inputs=None, sweep_required_labels=_REQUIRED_LABELS):
        self._url = url
        self.controls = {
            ("textbox", "First Name"): _FakeTextLocator(),
            ("textbox", "Email"): _FakeTextLocator(),
        }
        self.combo_field_id = combo_field_id
        self.single_value = _FakeSingleValue(combo_reads)
        self.combo_input = _FakeComboInput(self.single_value)
        self.combo_control = _FakeComboControl(self.combo_input,
                                               self.single_value)
        self.timeouts = []
        self.file_inputs = (list(file_inputs) if file_inputs is not None else
                            [_FakeFileInput(id="resume",
                                           accept=".pdf,.doc,.docx,.txt,.rtf"),
                             _FakeFileInput(id="headshot",
                                           accept="image/png,image/jpeg")])
        self.routed = []
        self.context = _PageBoundContext(self)
        self.requested = []
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

    def wait_for_timeout(self, ms):
        self.timeouts.append(ms)

    def locator(self, css):
        if css == base._combobox_control_selector(self.combo_field_id):
            return self.combo_control
        if css == "div.select__menu div.select__option":
            return self.option_menu
        if css == base._REQUIRED_CSS:
            return _FakeLocatorSet(self._sweep_required)
        if css == base._ASTERISK_CSS:
            return _FakeLocatorSet([])
        return _FakeLocatorSet([])


def _resolved_values(fieldmap, *, tmp_path, assets_kwargs=None):
    ssot = _fake_ssot()
    profile = profile_from_real_ssot(ssot)
    assets = _assets(tmp_path, **(assets_kwargs or {}))
    return greenhouse.resolve_values(fieldmap, ssot, profile, assets=assets)


# =============================================================================
# capture / apply_url: thin delegation to the registry
# =============================================================================


def test_capture_delegates_to_registry_capture_fn(monkeypatch):
    # registry._capture_greenhouse (== PROVIDERS["greenhouse"].capture_fn,
    # bound at registry module-load time) itself lazily imports and calls
    # engine.fieldmap.capture_greenhouse at CALL time (mirrors
    # test_providers_registry.py's own `test_collect_fieldmap_greenhouse_
    # passes_opener`), so patching that module attribute is what proves
    # capture() rides the SAME registry wiring end to end.
    import engine.fieldmap as fieldmap_module
    calls = []

    def fake_capture(slug, job_id, opener=None):
        calls.append((slug, job_id, opener))
        return "SENTINEL"

    monkeypatch.setattr(fieldmap_module, "capture_greenhouse", fake_capture)
    result = greenhouse.capture("fakeco", "7701001", opener="OPENER")
    assert result == "SENTINEL"
    assert calls == [("fakeco", "7701001", "OPENER")]
    assert registry.resolve("greenhouse").capture_fn is registry._capture_greenhouse


def test_apply_url_delegates_to_registry_apply_url_fn():
    assert (greenhouse.apply_url("fakeco", "7701001")
           == "https://boards.greenhouse.io/fakeco/jobs/7701001")


def test_greenhouse_module_satisfies_provider_protocol():
    assert isinstance(greenhouse, protocol.Provider)
    assert greenhouse.vendor == "greenhouse"


# =============================================================================
# resolve_values: hole-fix e structural CV/photo choice
# =============================================================================


def test_cv_becomes_atsi_and_photo_attached_when_form_has_photo_field(
        tmp_path):
    # questions.json's fieldmap carries a "Profile picture" upload field, so
    # the structural signal fires: resume -> cv-atsi, headshot -> photo.
    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    by_key = {fv.key: fv for fv in values.fields}

    assert by_key["resume"].asset == "cv-atsi"
    assert Path(by_key["resume"].value).name == "cv-atsi.pdf"
    assert "photo field present" in by_key["resume"].upload_reason
    assert by_key["headshot"].asset == "photo"
    assert Path(by_key["headshot"].value).name == "Me.png"


def test_cv_stays_ats_when_form_has_no_photo_field(tmp_path):
    # A fieldmap with no photo/image upload field at all: hole-fix e's
    # negative branch, keyed purely on the form's own structural shape.
    fieldmap = FieldMap(vendor="greenhouse", posting_id="1",
                        captured_at=_PINNED, fields=[
        Field(key="resume", label="Resume/CV", type="input_file",
             required=True, options=[], source="questions",
             locator=Locator(role="button", name="Resume/CV")),
    ])
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    fv = values.fields[0]
    assert fv.asset == "cv-ats"
    assert "no photo field" in fv.upload_reason


def test_structural_rule_is_a_noop_with_no_assets(tmp_path):
    # No assets supplied: greenhouse.resolve_values must degrade to plain
    # fill.resolve_values (the pre-override file-upload skip), never crash.
    fieldmap = _fieldmap()
    ssot = _fake_ssot()
    profile = profile_from_real_ssot(ssot)
    values = greenhouse.resolve_values(fieldmap, ssot, profile)
    assert values.fields == [] or all(
        fv.key not in ("resume", "headshot") for fv in values.fields)
    assert dict(values.skipped)["resume"] == "file-upload"


# =============================================================================
# fill(): the ordered Provider-contract sequence
# =============================================================================


def test_fill_happy_path_all_required_land_is_complete(tmp_path):
    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeGreenhousePage()

    report = greenhouse.fill(page, fieldmap, values)

    assert report.vendor == "greenhouse"
    # (1) never-send installed.
    assert len(page.routed) == 1
    assert page.routed[0][0] == "**"
    # (2)+(3) every field landed and readback-confirmed. The SSOT carries only a
    # combined identity.name, so a First Name field is split to the first token
    # (gap #2): the discrete field lands "Test", not the whole name.
    assert page.controls[("textbox", "First Name")].input_value() == \
        "Test"
    assert page.controls[("textbox", "Email")].input_value() == \
        "test.candidate@example.invalid"
    # The react-select driver: click the control to open the menu, type_human
    # the option text into the control's OWN input (settle-focus click first,
    # per the first-char-drop fix), commit via Enter (react-select commits the
    # highlighted first-filtered option itself -- never a `div.select__option`
    # click), then dismiss with Escape -- never the stale #react-select-<id>-
    # input / -listbox ids the old driver used.
    assert page.combo_control.clicked == 1
    assert page.combo_input.clicked == 1              # _settle_focus click
    assert "".join(page.combo_input.keys) == "No"
    assert page.combo_input.pressed == ["Enter", "Escape"]
    resume_input = page.file_inputs[0]
    headshot_input = page.file_inputs[1]
    assert resume_input.set_input_files_calls == 1
    assert headshot_input.set_input_files_calls == 1
    assert report.readback_mismatches == []
    # (4) DOM sweep agrees with schema -> no forced gap.
    assert report.required_unfilled == []
    assert report.complete is True
    assert report.caption().endswith("COMPLETE")
    assert not report.caption().endswith("NOT COMPLETE")


def test_fill_dom_sweep_mismatch_forces_not_complete(tmp_path):
    # Every field lands successfully, but the DOM sweep does not carry
    # "Resume/CV" as required (schema says required, DOM disagrees):
    # hole-fix d must force NOT_COMPLETE regardless of the per-field result.
    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    mismatched_labels = tuple(label for label in _REQUIRED_LABELS
                              if label != "Resume/CV")
    page = _FakeGreenhousePage(sweep_required_labels=mismatched_labels)

    report = greenhouse.fill(page, fieldmap, values)

    assert report.filled >= 4          # the fields still landed
    assert report.complete is False
    gap_keys = [g["key"] for g in report.required_unfilled]
    assert any(k.startswith("dom-sweep:") for k in gap_keys)
    assert report.caption().endswith("NOT COMPLETE")


def test_fill_dom_sweep_extra_required_field_forces_not_complete(tmp_path):
    # The DOM shows a required control the schema never captured
    # (dom_only): also a hole-fix d gap, the opposite direction.
    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    extra_labels = _REQUIRED_LABELS + ("Cover Letter",)
    page = _FakeGreenhousePage(sweep_required_labels=extra_labels)

    report = greenhouse.fill(page, fieldmap, values)

    assert report.complete is False
    reasons = [g["reason"] for g in report.required_unfilled]
    assert any("absent from the schema" in r for r in reasons)


def test_fill_resume_text_satisfied_by_sibling_upload_is_complete(tmp_path):
    # BUG 3: Greenhouse's schema exposes a `resume_text` paste-textarea
    # question ALONGSIDE the `resume` file-upload question (same label,
    # "Resume/CV") even when the live form is configured for file-upload
    # mode, where the textarea is simply ABSENT from the DOM. Once `resume`
    # uploads, `resume_text` must be treated SATISFIED by the sibling upload:
    # never driven (no fill attempt, no fill-error), never a required gap,
    # and never a dom-sweep mismatch -- the form must read COMPLETE.
    fieldmap = FieldMap(vendor="greenhouse", posting_id="7701099",
                        captured_at=_PINNED, fields=[
        Field(key="resume", label="Resume/CV", type="input_file",
             required=True, options=[], source="questions",
             locator=Locator(role="button", name="Resume/CV")),
        Field(key="resume_text", label="Resume/CV", type="textarea",
             required=True, options=[], source="questions",
             locator=Locator(role="textbox", name="Resume/CV")),
    ])
    ssot = SSOT({
        "identity": {"name": "Test Candidate",
                     "email": "test.candidate@example.invalid"},
        "canned_answers": {
            "resume_text": "Test Candidate, platform engineer.",
        },
    })
    profile = profile_from_real_ssot(ssot)
    assets = _assets(tmp_path)
    values = greenhouse.resolve_values(fieldmap, ssot, profile, assets=assets)
    # resume_text resolved to real content (never skipped as missing), and
    # resume resolved to an upload -- both land in values.fields, so the
    # sibling-skip branch in greenhouse.fill() is the thing under test, not
    # an upstream resolve_values skip.
    assert values.values["resume_text"] == "Test Candidate, platform engineer."
    by_key = {fv.key: fv for fv in values.fields}
    assert "resume" in by_key and "resume_text" in by_key

    page = _FakeGreenhousePage(
        file_inputs=[_FakeFileInput(id="resume",
                                    accept=".pdf,.doc,.docx,.txt,.rtf")],
        sweep_required_labels=("Resume/CV",))

    report = greenhouse.fill(page, fieldmap, values)

    # Never driven: resume_text's own (textbox, "Resume/CV") control was
    # never looked up, and it is satisfied via the documented skip reason,
    # not a fill-error.
    assert ("role", "textbox", "Resume/CV") not in page.requested
    assert dict(report.skipped)["resume_text"] == (
        "satisfied by sibling file upload: resume")
    assert not any(g["key"] == "resume_text" for g in report.required_unfilled)
    assert not any(str(g["key"]).startswith("dom-sweep:")
                  for g in report.required_unfilled)
    assert report.required_unfilled == []
    assert report.complete is True
    assert report.caption().endswith("COMPLETE")


def test_fill_missing_required_ssot_answer_forces_not_complete(tmp_path):
    # The SSOT carries no sponsorship answer at all: resolve_values skips
    # the required combobox field, and it must surface as a genuine gap.
    fieldmap = _fieldmap()
    ssot = SSOT({"identity": {"name": "Test Candidate",
                              "email": "test.candidate@example.invalid"}})
    profile = profile_from_real_ssot(ssot)
    assets = _assets(tmp_path)
    values = greenhouse.resolve_values(fieldmap, ssot, profile, assets=assets)
    assert "question_50001" not in values.values

    page = _FakeGreenhousePage()
    report = greenhouse.fill(page, fieldmap, values)

    assert report.complete is False
    assert any(g["key"] == "question_50001" for g in report.required_unfilled)


def test_fill_never_touches_eeo_demographic_field(tmp_path):
    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    assert not any(fv.key.startswith("demographic_") for fv in values.fields)

    page = _FakeGreenhousePage()
    greenhouse.fill(page, fieldmap, values)

    assert not any("Gender" in str(call) for call in page.requested)


def test_fill_never_send_interceptor_registered_before_any_field_access(
        tmp_path):
    # install_never_send is called before the field-driving loop: prove it
    # by handing a page whose .route() call happens strictly before any
    # .get_by_role/.get_by_label/.locator call is recorded.
    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)

    order = []

    class _OrderTrackingPage(_FakeGreenhousePage):
        def route(self, pattern, handler):
            order.append("route")
            super().route(pattern, handler)

        def get_by_role(self, role, name=None):
            order.append("get_by_role")
            return super().get_by_role(role, name=name)

        def locator(self, css):
            order.append(f"locator:{css}")
            return super().locator(css)

    page = _OrderTrackingPage()
    greenhouse.fill(page, fieldmap, values)

    assert order[0] == "route"


def test_fill_installs_never_send_at_context_scope(tmp_path):
    # The never-send interceptor is installed on the CONTEXT, not the bare page,
    # so a submit from a popup / new tab the context opens mid-fill is aborted
    # too (a page-scoped route would let it escape). base.install_never_send
    # targets page.context; the fake context records the catch-all registration.
    # (The handler's abort behaviour is exercised in test_providers_base.py.)
    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeGreenhousePage()

    greenhouse.fill(page, fieldmap, values)

    patterns = [pattern for pattern, _ in page.context.routed]
    assert patterns == ["**"]                    # exactly one catch-all route
    assert page.context.routed[0][1] is not None  # a real handler was registered


def test_fill_raises_on_navigation_during_fill(tmp_path):
    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)

    class _NavigatingPage(_FakeGreenhousePage):
        def get_by_role(self, role, name=None):
            self._url = "https://boards.greenhouse.io/fakeco/thanks"
            return super().get_by_role(role, name=name)

    page = _NavigatingPage()
    with pytest.raises(FillSafetyError, match="navigated during fill"):
        greenhouse.fill(page, fieldmap, values)


def test_fill_report_reuses_the_existing_fillreport_dataclass(tmp_path):
    from engine.fill import FillReport

    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeGreenhousePage()
    report = greenhouse.fill(page, fieldmap, values)
    assert isinstance(report, FillReport)
    # to_dict()/caption() (the honest-caption machinery) work unmodified.
    blob = report.to_dict()
    assert blob["vendor"] == "greenhouse"
    assert "caption" in blob
