"""Greenhouse provider (engine.providers.greenhouse): the FIRST reference
implementation of the `Provider` contract, W5.2.

No patchright, no network: `fill()` is driven through a FAKE page/locator
harness mirroring the representative DOM fixture at
`tests/fixtures/providers/greenhouse/dom.html` (react-select combobox, a
required text field, a required file input, an optional image-accept file
input, and an EEOC decline field with no required marker). The captured
schema comes from `tests/fixtures/providers/greenhouse/questions.json`
parsed through the real (offline)
`engine.providers.greenhouse.capture.parse_greenhouse`. The SSOT is a
hand-built FAKE (no owner PII). Real live-browser HAR capture against the
real DOM is a SEPARATE later step (W5.2 fixture-validation promise in
providers/base.py); this suite proves the LOGIC offline, matching the
convention already established by test_providers_base.py.
"""

import importlib
import json
from pathlib import Path

import pytest

from engine import content
from engine.kernel.contracts import (
    Field, FieldMap, FieldValue, FillAssets, FillSafetyError, Locator,
    ResolvedValues)
from engine.providers.greenhouse.capture import parse_greenhouse
from engine.kernel.resolve import TOS_FORBIDDEN_SKIP_PREFIX
from engine.profile_map import profile_from_real_ssot
from engine.providers import base, greenhouse, protocol
from engine.providers._registry import PROVIDERS
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


def _assets(tmp_path, *, ats=True, atsi=True, photo=True,
           cover_letter=False) -> FillAssets:
    """`cover_letter` defaults False: no fixture field carries that key, and
    a caller must opt in explicitly to exercise the cover-letter upload."""
    def make(name, present):
        p = tmp_path / name
        if present:
            p.write_bytes(b"stub")
        return p
    return FillAssets(cv_ats=make("cv-ats.pdf", ats),
                      cv_atsi=make("cv-atsi.pdf", atsi),
                      photo=make("Me.png", photo),
                      cover_letter=make("cover-letter.pdf", cover_letter))


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
    """`rendered_confirmed` models whether GREENHOUSE'S OWN widget renders the
    attach (filename text + a remove control), independently of the native
    `input_value()`/`evaluate()` (`el.files.length`) readback: the live probe
    (HOSTILE REVIEW #1, 2026-07-06 gitlab/8503792002 run) proved the native
    FileList can be genuinely non-empty while the widget NEVER renders it, so
    these two signals are modelled as INDEPENDENT here on purpose. Defaults
    True (the widget confirms once uploaded) -- pass `rendered_confirmed=
    False` to reproduce the false-positive bug (file attached, widget never
    shows it) / the racy-render hole-fix (FIX 1/2, file attached, widget not
    YET shown).

    `attach_succeeds` (FIX 1 negative case) models the native FileList's OWN
    truthful state: True (default) means `set_input_files` genuinely lands
    the file (`evaluate()`/`input_value()` both read it back); False means
    the attach never reaches the native input at all (e.g. a widget that
    swallows the file before it lands) -- `el.files.length` stays 0 despite
    the call, so a sibling paste-textarea must NOT be satisfied.

    `widget_shows_filename` / `remove_control_name` model the container's TWO
    confirmation signals SEPARATELY, because `_upload_widget_confirmed` reads
    them separately: the filename text inside the container, and a control
    whose ACCESSIBLE NAME carries the remove/delete/clear vocabulary
    (`_REMOVE_CONTROL_NAME_RE`). A live container can carry a button that is
    NOT a remove control (e.g. a "Submit application" button in the same
    parent), and it is the NAME -- never the bare presence of a button --
    that makes a button a confirmation. `remove_control_name=None` models a
    container with no button at all."""

    def __init__(self, *, id=None, name=None, accept=None,
                 readback=_NO_OVERRIDE, rendered_confirmed=True,
                 attach_succeeds=True, widget_shows_filename=True,
                 remove_control_name="Remove"):
        self._attrs = {"id": id, "name": name, "accept": accept}
        self._readback_override = readback
        self._rendered_confirmed = rendered_confirmed
        self._attach_succeeds = attach_succeeds
        self._widget_shows_filename = widget_shows_filename
        self._remove_control_name = remove_control_name
        self.set_input_files_calls = 0
        self.uploaded = None
        self.clicks = 0

    def get_attribute(self, name):
        return self._attrs.get(name)

    def set_input_files(self, files):
        self.set_input_files_calls += 1
        if self._attach_succeeds:
            self.uploaded = files

    def click(self):
        self.clicks += 1

    def input_value(self):
        if self._readback_override is not _NO_OVERRIDE:
            return self._readback_override
        return self.uploaded or ""

    def evaluate(self, script):
        """Models `el.files.length`: the native FileList holds the file
        IMMEDIATELY once `set_input_files` genuinely lands it, INDEPENDENT of
        whether Greenhouse's own React widget has rendered a confirmation
        yet (`_rendered_confirmed` models that separate, later signal).
        `script` is ignored -- this fake only ever receives the one
        `el.files.length` probe `engine.kernel.fill_toolkit._upload_attached` sends."""
        return 1 if self.uploaded else 0

    def locator(self, css):
        """The widget's own immediate container (`xpath=..`, mirroring
        `engine.providers.base._upload_widget_container`)."""
        if css == "xpath=..":
            return _FakeUploadWidgetContainer(self)
        raise AssertionError(f"unexpected selector {css!r}")


class _FakeUploadWidgetContainer:
    """The rendered widget container `base.poll_upload_confirmed` polls:
    shows the uploaded file's stem as text, and a Remove control, ONLY once
    a file landed AND the fake models Greenhouse actually confirming it
    (`_rendered_confirmed`).

    `get_by_role` HONOURS its `name=` argument (a compiled pattern, the way
    Playwright's own accessible-name filter does), so
    `_REMOVE_CONTROL_NAME_RE` is genuinely applied here rather than assumed:
    a container whose only button is named something else confirms NOTHING
    through this signal."""

    def __init__(self, file_input: _FakeFileInput):
        self._file_input = file_input

    def _shows_confirmation(self) -> bool:
        return bool(self._file_input.uploaded) and self._file_input._rendered_confirmed

    def inner_text(self):
        if not (self._shows_confirmation()
                and self._file_input._widget_shows_filename):
            return ""
        return Path(self._file_input.uploaded).stem

    def get_by_role(self, role, name=None):
        button_name = self._file_input._remove_control_name
        # The rendered container carries exactly one BUTTON (never a link),
        # and only once greenhouse confirms the attach.
        present = (self._shows_confirmation() and role == "button"
                   and button_name is not None)
        if present and name is not None:
            matcher = getattr(name, "search", None)
            present = (bool(matcher(button_name)) if callable(matcher)
                       else name == button_name)
        return _FakeRoleQuery(present)


class _FakeRoleQuery:
    def __init__(self, present: bool):
        self._present = present

    def count(self):
        return 1 if self._present else 0


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


class _FakeKeyboard:
    """The page's OWN keyboard (`page.keyboard`), which a live Playwright page
    always exposes. It is FOCUS-FOLLOWING: it presses wherever focus currently
    sits, which is why the react-select driver commits through it rather than
    through the filter input's own `press` (react-select detaches that input on
    every keystroke, so a locator-scoped press hangs on Playwright's
    actionability wait for a stale node -- the live hang `_keyboard_press`
    exists to avoid).

    Enter commits react-select's highlighted option exactly as the filter
    input's own press does (`_FakeComboInput.press`), because the live input
    still holds the focus the keyboard follows."""

    def __init__(self, single_value=None):
        self.pressed = []
        self._single_value = single_value

    def press(self, key):
        self.pressed.append(key)
        if key == "Enter" and self._single_value is not None:
            self._single_value.commit()


class _FakeGreenhousePage:
    """One fake page driving the WHOLE fill() sequence: text controls,
    the react-select combobox, file inputs, and the sweep_required CSS
    selectors, mirroring dom.html's shape end to end.

    `with_keyboard=True` gives the page the `page.keyboard` a LIVE page always
    has, so the focus-following commit path is exercised; the default (no
    keyboard) keeps the locator-press fallback `_keyboard_press` documents for
    a page that exposes none.

    `page_rendered_filenames` models greenhouse rendering an attached file's
    name OUTSIDE the native input's own container (a sibling widget node, and
    the only confirmation left once greenhouse UNMOUNTS the `<input type=file>`
    post-upload) -- the state `_page_shows_filename` exists for. Empty by
    default: a page that renders no filename anywhere confirms nothing."""

    def __init__(self, *, url="https://boards.greenhouse.io/fakeco/jobs/7701001",
                combo_field_id="question_50001", combo_reads=("No",),
                file_inputs=None, sweep_required_labels=_REQUIRED_LABELS,
                with_keyboard=False, page_rendered_filenames=()):
        self._url = url
        self.page_rendered_filenames = list(page_rendered_filenames)
        self.controls = {
            ("textbox", "First Name"): _FakeTextLocator(),
            ("textbox", "Email"): _FakeTextLocator(),
        }
        self.combo_field_id = combo_field_id
        self.single_value = _FakeSingleValue(combo_reads)
        self.combo_input = _FakeComboInput(self.single_value)
        self.combo_control = _FakeComboControl(self.combo_input,
                                               self.single_value)
        # Focus-following: the keyboard commits the SAME react-select control
        # the filter input's own press would, since that input holds focus.
        self.keyboard = (_FakeKeyboard(self.single_value) if with_keyboard
                         else None)
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

    def get_by_role(self, role, name=None, exact=None):
        self.requested.append(("role", role, name))
        return self.controls[(role, name)]

    def get_by_label(self, label):
        self.requested.append(("label", label))
        return self.controls[(None, label)]

    def get_by_text(self, text, exact=False):
        """Page-wide text query (`_page_shows_filename`'s only reachable
        signal). Substring semantics, mirroring Playwright's `exact=False`."""
        self.requested.append(("text", text, exact))
        return _FakeRoleQuery(
            any(text in rendered for rendered in self.page_rendered_filenames))

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


def test_capture_delegates_to_registry_capture(monkeypatch):
    # PROVIDERS["greenhouse"].capture is a call-time lazy_call targeting
    # engine.providers.greenhouse:capture, which itself lazily imports and calls
    # engine.providers.greenhouse.capture.capture_greenhouse at CALL time (mirrors
    # test_providers_registry.py's own `test_collect_fieldmap_greenhouse_
    # passes_opener`), so patching that module attribute is what proves
    # capture() rides the SAME registry wiring end to end.
    from importlib import import_module
    capture_mod = import_module("engine.providers.greenhouse.capture")
    calls = []

    def fake_capture(slug, job_id, opener=None):
        calls.append((slug, job_id, opener))
        return "SENTINEL"

    monkeypatch.setattr(capture_mod, "capture_greenhouse", fake_capture)
    result = greenhouse.capture("fakeco", "7701001", opener="OPENER")
    assert result == "SENTINEL"
    assert calls == [("fakeco", "7701001", "OPENER")]
    assert PROVIDERS["greenhouse"].capture._target == ("engine.providers.greenhouse", "capture")


def test_apply_url_delegates_to_registry_apply_url():
    assert (greenhouse.apply_url("fakeco", "7701001")
           == "https://boards.greenhouse.io/fakeco/jobs/7701001")


def test_greenhouse_module_satisfies_provider_protocol():
    assert isinstance(greenhouse, protocol.Provider)
    assert greenhouse.vendor == "greenhouse"


# =============================================================================
# resolve_values: hole-fix e structural CV/photo choice
# =============================================================================


def test_cv_stays_ats_and_photo_attached_when_form_has_photo_field(
        tmp_path):
    # questions.json's fieldmap carries a "Profile picture" upload field, so the
    # structural signal fires: resume -> plain ATS CV, headshot -> the photo.
    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    by_key = {fv.key: fv for fv in values.fields}

    assert by_key["resume"].asset == "cv-ats"
    assert Path(by_key["resume"].value).name == "cv-ats.pdf"
    assert "photo field present" in by_key["resume"].upload_reason
    assert by_key["headshot"].asset == "photo"
    assert Path(by_key["headshot"].value).name == "Me.png"


def test_cv_becomes_atsi_when_form_has_no_photo_field(tmp_path):
    # A fieldmap with no photo/image upload field at all: the negative branch,
    # keyed purely on the form's own structural shape, embeds the photo via the
    # ATSI CV variant (nowhere else to carry the portrait).
    fieldmap = FieldMap(vendor="greenhouse", posting_id="1",
                        captured_at=_PINNED, fields=[
        Field(key="resume", label="Resume/CV", type="input_file",
             required=True, options=[], source="questions",
             locator=Locator(role="button", name="Resume/CV")),
    ])
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    fv = values.fields[0]
    assert fv.asset == "cv-atsi"
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


def test_fill_resume_text_not_rendered_books_an_unjustified_required_gap(
        tmp_path):
    # THE OTHER `_TEXT_UPLOAD_SIBLINGS` branch (`_control_rendered`'s "not
    # rendered" skip, fill.py's `if sibling_key is not None and not
    # _control_rendered(page, fv)`): the sibling `resume` upload is ABSENT
    # here (no such field on this fieldmap at all, so `file_attached_keys`
    # can never carry it and the "satisfied by sibling" branch above it is
    # structurally unreachable), and `resume_text`'s OWN locator resolves to
    # ZERO nodes -- the live file-upload-mode state the guard exists for.
    #
    # The existing `_FakeTextLocator` fake has NO `count()` method at all, so
    # it always reads as rendered and cannot model this state; this reuses
    # `_FakeRoleQuery` (already a bare `count()`-only fake used elsewhere for
    # `get_by_role` probes) instead.
    #
    # THE ANTI-LAUNDERING PROPERTY under test (the code's own documented
    # claim): this skip's reason string must NOT match
    # `kernel.resolve._is_satisfied_by_sibling_upload` (it does not start
    # with "satisfied by sibling file upload"), so a REQUIRED resume_text
    # still books its gap in `required_unfilled`, `justified_skips` stays 0,
    # and the run stays NOT_COMPLETE -- never silently absorbed as justified.
    fieldmap = FieldMap(vendor="greenhouse", posting_id="7701098",
                        captured_at=_PINNED, fields=[
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
    values = greenhouse.resolve_values(fieldmap, ssot, profile)
    assert values.values["resume_text"] == "Test Candidate, platform engineer."

    page = _FakeGreenhousePage(sweep_required_labels=("Resume/CV",))
    page.controls[("textbox", "Resume/CV")] = _FakeRoleQuery(present=False)

    report = greenhouse.fill(page, fieldmap, values)

    assert dict(report.skipped)["resume_text"] == (
        "control not rendered (vendor is in file-upload mode) and sibling "
        "resume did not attach")
    assert [g["key"] for g in report.required_unfilled] == ["resume_text"]
    assert report.justified_skips == 0
    assert report.complete is False


def test_fill_cover_letter_text_satisfied_by_sibling_upload_is_complete(
        tmp_path):
    # This task: mirrors test_fill_resume_text_satisfied_by_sibling_upload_
    # is_complete for the cover_letter/cover_letter_text sibling pair. Once a
    # real cover-letter document asset uploads to the `cover_letter` file
    # field, `cover_letter_text` must be treated SATISFIED by the sibling
    # upload: never driven, never a required gap -- the form reads COMPLETE.
    fieldmap = FieldMap(vendor="greenhouse", posting_id="7701099",
                        captured_at=_PINNED, fields=[
        Field(key="cover_letter", label="Cover Letter", type="input_file",
             required=True, options=[], source="questions",
             locator=Locator(role="button", name="Cover Letter")),
        Field(key="cover_letter_text", label="Cover Letter", type="textarea",
             required=True, options=[], source="questions",
             locator=Locator(role="textbox", name="Cover Letter")),
    ])
    ssot = SSOT({
        "identity": {"name": "Test Candidate",
                     "email": "test.candidate@example.invalid"},
        "canned_answers": {
            "cover_letter_text": "Dear hiring manager, I am excited to apply.",
        },
    })
    profile = profile_from_real_ssot(ssot)
    assets = _assets(tmp_path, cover_letter=True)
    values = greenhouse.resolve_values(fieldmap, ssot, profile, assets=assets)
    # cover_letter_text resolved to real content (never skipped as missing),
    # and cover_letter resolved to an upload of the cover-letter asset (never
    # the CV) -- both land in values.fields, so the sibling-skip branch in
    # greenhouse.fill() is the thing under test, not an upstream resolve_
    # values skip.
    assert values.values["cover_letter_text"] == (
        "Dear hiring manager, I am excited to apply.")
    by_key = {fv.key: fv for fv in values.fields}
    assert "cover_letter" in by_key and "cover_letter_text" in by_key
    assert by_key["cover_letter"].asset == "cover-letter"

    page = _FakeGreenhousePage(
        file_inputs=[_FakeFileInput(id="cover_letter",
                                    accept=".pdf,.doc,.docx,.txt,.rtf")],
        sweep_required_labels=("Cover Letter",))

    report = greenhouse.fill(page, fieldmap, values)

    # Never driven: cover_letter_text's own (textbox, "Cover Letter") control
    # was never looked up, and it is satisfied via the documented skip
    # reason, not a fill-error.
    assert ("role", "textbox", "Cover Letter") not in page.requested
    assert dict(report.skipped)["cover_letter_text"] == (
        "satisfied by sibling file upload: cover_letter")
    assert not any(g["key"] == "cover_letter_text"
                  for g in report.required_unfilled)
    assert not any(str(g["key"]).startswith("dom-sweep:")
                  for g in report.required_unfilled)
    assert report.required_unfilled == []
    assert report.complete is True
    assert report.caption().endswith("COMPLETE")


def test_fill_reconfirms_late_rendered_upload_at_end_of_fill(
        tmp_path, monkeypatch):
    # Late-render hole-fix (2026-07-06 gitlab/8503792002 full-fill run):
    # Greenhouse's React queue can still be busy driving the OTHER fields
    # when `_fill_upload`'s OWN inline `base.poll_upload_confirmed` check
    # runs, so it can return a FALSE NEGATIVE for a resume that DOES attach
    # and IS eventually rendered by end-of-fill. `base.poll_upload_confirmed`
    # is monkeypatched to model exactly that: False on the first (inline,
    # mid-fill) call, True on the second (this fix's end-of-fill
    # re-confirmation) call -- proving greenhouse.fill's OWN orchestration
    # recovers the false negative, not re-testing base.py's real polling
    # algorithm (out of this module's scope).
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
    page = _FakeGreenhousePage(
        file_inputs=[_FakeFileInput(id="resume",
                                    accept=".pdf,.doc,.docx,.txt,.rtf")],
        sweep_required_labels=("Resume/CV",))

    calls = []

    def fake_poll_upload_confirmed(page_arg, control, filename, **kwargs):
        calls.append(filename)
        return len(calls) > 1  # False mid-fill (inline), True end-of-fill

    monkeypatch.setattr(base, "poll_upload_confirmed",
                        fake_poll_upload_confirmed)

    report = greenhouse.fill(page, fieldmap, values)

    # The inline confirm (mid-fill) and the final re-confirmation (end-of-
    # fill) each called base.poll_upload_confirmed exactly once.
    assert len(calls) == 2
    # resume: a genuine end-of-fill confirm -> filled + uploaded, never a
    # required gap.
    assert any(u["key"] == "resume" for u in report.uploads)
    # resume_text: re-evaluated satisfied-by-sibling once resume confirmed,
    # not a fill-error / required gap.
    assert dict(report.skipped)["resume_text"] == (
        "satisfied by sibling file upload: resume")
    assert not any(g["key"] in ("resume", "resume_text")
                  for g in report.required_unfilled)
    assert not any(str(g["key"]).startswith("dom-sweep:")
                  for g in report.required_unfilled)
    assert report.required_unfilled == []
    assert report.complete is True
    assert report.caption().endswith("COMPLETE")


def test_fill_resume_text_satisfied_by_sibling_when_render_not_yet_confirmed(
        tmp_path):
    # FIX 1 (racy-render decoupling, 2026-07-06 gitlab/8503792002 full-fill
    # run): Greenhouse's own rendered confirmation (filename text + a remove
    # control) is RACY under a busy full-fill load and can still be pending
    # at end-of-fill even though the file genuinely landed on the native
    # input (el.files.length>=1) immediately. `resume_text` must be treated
    # satisfied-by-sibling from that TRUTHFUL, immediate signal alone --
    # never driven -- regardless of whether `resume`'s own render-confirmed
    # promotion (a SEPARATE gate, `filled_keys`/`uploads`) ever lands within
    # this fill. `rendered_confirmed=False` models the render never
    # appearing for the whole (now-extended, FIX 2) poll window.
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
    page = _FakeGreenhousePage(
        file_inputs=[_FakeFileInput(id="resume",
                                    accept=".pdf,.doc,.docx,.txt,.rtf",
                                    rendered_confirmed=False)],
        sweep_required_labels=("Resume/CV",))

    report = greenhouse.fill(page, fieldmap, values)

    # Never driven: resume_text's own (textbox, "Resume/CV") control was
    # never looked up, satisfied purely by the file genuinely being on the
    # sibling input -- NOT by Greenhouse's (still-pending) render.
    assert ("role", "textbox", "Resume/CV") not in page.requested
    assert dict(report.skipped)["resume_text"] == (
        "satisfied by sibling file upload: resume")
    assert not any(g["key"] == "resume_text"
                  for g in report.required_unfilled)
    # resume itself: the render never confirmed within this fill, so it
    # HONESTLY stays a required gap (never falsely promoted) -- proving this
    # fix never unconditionally marks anything filled.
    assert not any(u["key"] == "resume" for u in report.uploads)
    assert any(g["key"] == "resume" for g in report.required_unfilled)


def test_fill_resume_text_not_satisfied_when_sibling_file_genuinely_absent(
        tmp_path):
    # Negative case (truthfulness half of FIX 1): the native FileList
    # genuinely NEVER holds the file (el.files.length stays 0 despite the
    # upload attempt, e.g. a widget that swallows it before it reaches the
    # input) -- resume_text must NOT be treated satisfied-by-sibling, and is
    # driven normally like any other required text field.
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
    page = _FakeGreenhousePage(
        file_inputs=[_FakeFileInput(id="resume",
                                    accept=".pdf,.doc,.docx,.txt,.rtf",
                                    attach_succeeds=False)],
        sweep_required_labels=("Resume/CV",))
    page.controls[("textbox", "Resume/CV")] = _FakeTextLocator()

    report = greenhouse.fill(page, fieldmap, values)

    # Driven normally: the sibling file genuinely never attached
    # (el.files.length==0), so resume_text is NOT satisfied-by-sibling.
    assert ("role", "textbox", "Resume/CV") in page.requested
    assert page.controls[("textbox", "Resume/CV")].input_value() == (
        "Test Candidate, platform engineer.")
    assert "resume_text" not in dict(report.skipped)
    assert not any(g["key"] == "resume_text"
                  for g in report.required_unfilled)
    # resume itself: genuinely never attached -> an honest required gap.
    assert any(g["key"] == "resume" for g in report.required_unfilled)


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


# =============================================================================
# relocation dropdown: the content overlay's option label is the ONLY value the
# react-select path lands (W5B-GREENHOUSE regression coverage)
# =============================================================================

_RELOCATION_KEY = "question_5002"
_RELOCATION_LABEL = "Are you open to relocation for this role?"
_RELOCATION_OPTIONS = ["Yes, I am willing to relocate", "No"]
# The verbose free-text relocation answer the SSOT carries (a FAKE stand-in for
# the owner's own). It opens on a place name, not on a yes/no token, so neither
# the kernel resolver's option match nor the overlay's option-fit ladder can map
# it onto a Yes/No dropdown option: exactly the shape that produced the live
# 2026-07-12 "no option matches SSOT value" gap on the anthropic posting.
_VERBOSE_RELOCATION = ("Test City and Fake Town metro areas: yes, immediately; "
                       "elsewhere case by case")


def _relocation_fieldmap() -> FieldMap:
    """A one-question posting: the REQUIRED relocation dropdown, captured as
    Greenhouse's react-select combobox (`multi_value_single_select` carries
    locator role "combobox", `contracts._ROLE_FOR_TYPE`, which is the structural
    signal `fill._is_react_combobox` keys on)."""
    return FieldMap(vendor="greenhouse", posting_id="7701002",
                    captured_at=_PINNED, fields=[
        Field(key=_RELOCATION_KEY, label=_RELOCATION_LABEL,
             type="multi_value_single_select", required=True,
             options=list(_RELOCATION_OPTIONS), source="questions",
             locator=Locator(role="combobox", name=_RELOCATION_LABEL)),
    ])


def test_greenhouse_combobox_drives_overlay_supplied_option_label():
    # The relocation dropdown's SSOT answer is a verbose sentence, so the kernel
    # resolver honestly skips the field; the content overlay (engine/content.py,
    # applied HARNESS-CENTRALLY in w5_accept.py, never by this plugin) routes
    # `canned_answers.relocation_dropdown` onto the field's own option label.
    # This test pins the GREENHOUSE half of that seam: the EXACT option label the
    # overlay produces is what select_react_combobox types, commits and
    # readback-verifies, so the required dropdown counts filled and the form
    # reads COMPLETE.
    fieldmap = _relocation_fieldmap()
    ssot = SSOT({
        "identity": {"name": "Test Candidate",
                     "email": "test.candidate@example.invalid"},
        "canned_answers": {
            "willing_to_relocate": _VERBOSE_RELOCATION,
            "relocation_dropdown": "Yes",
        },
    })
    profile = profile_from_real_ssot(ssot)
    values = greenhouse.resolve_values(fieldmap, ssot, profile)
    # Precondition (the live gap): the kernel cannot fit the verbose answer onto
    # a Yes/No option, so it reaches fill() only via the overlay.
    assert _RELOCATION_KEY not in values.values
    overlay = content.apply_content_overlay(values, fieldmap, ssot)
    assert overlay.applied == [(_RELOCATION_KEY,
                                "canned:canned_answers.relocation_dropdown")]
    # What the overlay hands fill() is an EXACT option label, never the sentence.
    assert values.values[_RELOCATION_KEY] == "Yes, I am willing to relocate"

    page = _FakeGreenhousePage(
        combo_field_id=_RELOCATION_KEY,
        combo_reads=("Yes, I am willing to relocate",),
        sweep_required_labels=(_RELOCATION_LABEL,))

    report = greenhouse.fill(page, fieldmap, values)

    # Driven as a react-select: open the control, type the option label one
    # keystroke at a time (never fill()), commit the highlighted first-filtered
    # option with Enter, dismiss with Escape (never blur).
    assert page.combo_control.clicked == 1
    assert "".join(page.combo_input.keys) == "Yes, I am willing to relocate"
    assert page.combo_input.pressed == ["Enter", "Escape"]
    # The rendered `.select__single-value` readback confirms it landed, so the
    # required dropdown closes rather than staying a gap.
    assert report.filled == 1
    assert report.readback_mismatches == []
    assert report.required_unfilled == []
    assert report.complete is True
    assert report.caption().endswith("COMPLETE")


def test_greenhouse_combobox_nonoption_value_stays_unfilled():
    # ANTI-GAMING NEGATIVE. The verbose free-text answer matches no option of the
    # Yes/No dropdown, and the sanctioned overlay REFUSES it (content.
    # NO_OPTION_MATCH: the field stays skipped and is reported unresolved,
    # nothing is guessed) -- the overlay's option-label route is the only path a
    # value legitimately reaches this combobox by.
    #
    # Should such a value reach fill() anyway (any future shortcut that bypasses
    # the overlay and types the raw SSOT text into the dropdown), the greenhouse
    # driver must NOT count it filled: react-select filters to zero options, the
    # Enter commits nothing, `.select__single-value` stays empty, and the report
    # says exactly that -- a readback mismatch plus an honest required gap that
    # forces NOT COMPLETE. A fill this vendor never achieved is never reported as
    # achieved.
    fieldmap = _relocation_fieldmap()
    ssot = SSOT({
        "identity": {"name": "Test Candidate",
                     "email": "test.candidate@example.invalid"},
        "canned_answers": {"willing_to_relocate": _VERBOSE_RELOCATION},
    })
    profile = profile_from_real_ssot(ssot)
    values = greenhouse.resolve_values(fieldmap, ssot, profile)
    overlay = content.apply_content_overlay(values, fieldmap, ssot)
    assert overlay.applied == []
    assert overlay.unresolved == [(_RELOCATION_KEY, content.NO_OPTION_MATCH)]
    assert values.fields == []          # the overlay promoted nothing

    # Force the unsanctioned value down the fill path: the FieldValue the overlay
    # itself declined to build.
    fld = fieldmap.fields[0]
    forced = ResolvedValues(fields=[FieldValue(
        key=fld.key, label=fld.label, type=fld.type, locator=fld.locator,
        value=_VERBOSE_RELOCATION)])
    page = _FakeGreenhousePage(
        combo_field_id=_RELOCATION_KEY,
        combo_reads=("",),              # no option matched: nothing to commit
        sweep_required_labels=(_RELOCATION_LABEL,))

    report = greenhouse.fill(page, fieldmap, forced)

    # The driver attempted the value truthfully (typed, Enter, Escape) and the
    # widget committed nothing.
    assert "".join(page.combo_input.keys) == _VERBOSE_RELOCATION
    assert page.combo_input.pressed == ["Enter", "Escape"]
    # NOT filled, and honestly reported as not filled.
    assert report.filled == 0
    assert [m["key"] for m in report.readback_mismatches] == [_RELOCATION_KEY]
    assert dict(report.skipped)[_RELOCATION_KEY] == (
        "value did not take (readback mismatch)")
    assert [g["key"] for g in report.required_unfilled] == [_RELOCATION_KEY]
    assert report.complete is False
    assert report.caption().endswith("NOT COMPLETE")


def test_fill_never_touches_eeo_demographic_field(tmp_path):
    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    assert not any(fv.key.startswith("demographic_") for fv in values.fields)

    page = _FakeGreenhousePage()
    greenhouse.fill(page, fieldmap, values)

    assert not any("Gender" in str(call) for call in page.requested)


def test_no_eeo_or_demographic_field_is_ever_driven_at_either_layer(tmp_path):
    """NO declined field is ever DRIVEN, and no locator is ever built for one.

    This is the safety property that makes the capture/DOM divergence in the EEO
    bucket harmless (see `capture._compliance_questions`): the boards-api serves a
    `race` question the live apply DOM does NOT render, and renders a
    `hispanic_ethnicity` the schema does not carry. `race` is therefore a PHANTOM
    LOCATOR IN WAITING: a control whose locator would resolve to zero elements, the
    exact shape that cost this vendor a 30-second timeout on the checkbox groups.

    It cannot bite for exactly one reason, and it is worth pinning rather than
    trusting: every field in this class is declined at TWO independent layers (the
    kernel resolver drops it from `values.fields`; the content overlay refuses to
    supply it a value), so the fill never reaches for it and no locator is built.
    Lift either layer and `race` becomes live phantom. The fake page raises on any
    unknown control, so a locator built for ANY of these fields fails this test.
    """
    raw = {
        "id": "9101",
        "questions": [
            {"label": "First Name", "required": True,
             "fields": [{"name": "first_name", "type": "input_text",
                         "values": []}]},
        ],
        "compliance": [
            {"type": "eeoc", "description": "OMB burden statement",
             "questions": []},
            {"type": "eeoc", "description": "",
             "questions": [
                 # `race` is the phantom: served by the API, absent from the DOM.
                 {"label": "Race", "required": False,
                  "fields": [{"name": "race", "type": "multi_value_single_select",
                              "values": [{"value": 1, "label": "Decline"}]}]},
                 {"label": "Gender", "required": True,
                  "fields": [{"name": "gender",
                              "type": "multi_value_single_select",
                              "values": [{"value": 1, "label": "Decline"}]}]},
             ]},
        ],
        "demographic_questions": {"questions": [
            {"id": 3, "label": "Which categories describe you?",
             "type": "multi_value_multi_select",
             "answer_options": [{"id": 1, "label": "Prefer not to disclose"}]},
        ]},
    }
    fieldmap = parse_greenhouse(raw, "fakeco", "9101", now=lambda: _PINNED)
    declined = {f.key for f in fieldmap.fields if f.decline_allowed}
    assert declined == {"race", "gender", "demographic_3"}   # all three CAPTURED

    ssot = _fake_ssot()
    profile = profile_from_real_ssot(ssot)
    values = greenhouse.resolve_values(fieldmap, ssot, profile,
                                       assets=_assets(tmp_path))

    # LAYER 1, the kernel resolver: not one of them carries a value to drive.
    assert declined.isdisjoint({fv.key for fv in values.fields})

    # LAYER 2, the content overlay: it may not overturn the decline either, so a
    # declined field appears in NONE of its outputs (not applied, not merely
    # deferred as ToS-forbidden, not left as an unresolved gap it might later fill).
    overlay = content.apply_content_overlay(values, fieldmap, ssot)
    assert declined.isdisjoint({key for key, _ in overlay.applied})
    assert declined.isdisjoint(set(overlay.tos_forbidden))
    assert declined.isdisjoint({key for key, _ in overlay.unresolved})
    assert declined.isdisjoint({fv.key for fv in values.fields})   # still absent

    # AND THE FILL BUILDS NO LOCATOR FOR ANY OF THEM. The fake page carries only
    # First Name, and raises KeyError on any other control, so reaching for `race`
    # (or Gender, or the demographic group) cannot pass silently.
    page = _FakeGreenhousePage(sweep_required_labels=("First Name",))
    report = greenhouse.fill(page, fieldmap, values)

    reached = {name for kind, *rest in page.requested
               for name in rest if isinstance(name, str)}
    for label in ("Race", "Gender", "Which categories describe you?"):
        assert label not in reached
    assert [fv.key for fv in values.fields] == ["first_name"]
    assert report.filled == 1
    # The declined fields are accounted for, not hidden: they are justified skips.
    assert report.justified_skips == 3


def test_fill_drives_a_checkbox_group_option_by_option_with_readback_confirmation(
        tmp_path):
    """W5.1c RETARGET (owner ruling 2026-07-13, memory
    `automations-automate-everything-tos-boundary`): a checkbox GROUP is no
    longer handed off before a locator is even built. `_drive_click_control`
    now dispatches on the resolved value's LIST shape (`kernel.resolve.
    _render_select`'s own output for a `multi_value_multi_select`) and drives
    EVERY selected option independently, each located by that OPTION's own
    name.

    THE PREMISE THIS STILL DEPENDS ON (live probe, unchanged): LIVE,
    `get_by_role("checkbox", name=<the QUESTION label>)` resolves to ZERO
    (only each OPTION checkbox resolves, 1-to-1, by its OWN label). This test
    proves the group is driven entirely through OPTION-level locators and the
    QUESTION-level locator is never even attempted.

    PROOF, not just an empty gap: each option's own fake control is checked
    for a genuine `.check()` (`is_checked() is True`), not merely that the
    field's skip vanished. An empty skip that is empty because something
    silently broke looks identical otherwise, and that is the failure mode
    this whole wave keeps surfacing.
    """
    raw = {
        "id": "9102",
        "questions": [
            {"label": "Which of these languages are you fluent in?",
             "required": True,
             "fields": [{"name": "question_langs[]",
                         "type": "multi_value_multi_select",
                         "values": [{"value": 1, "label": "Dutch"},
                                    {"value": 2, "label": "French"}]}]},
        ],
    }
    # The REAL capture, so this test also dies if the role regresses to `listbox`.
    fieldmap = parse_greenhouse(raw, "fakeco", "9102", now=lambda: _PINNED)
    fld = fieldmap.fields[0]
    assert fld.locator.role == "checkbox" and fld.required is True

    # BOTH options selected, mirroring the LIST shape `kernel.resolve.
    # _render_select` actually builds for a multi_value_multi_select (never
    # the bare scalar string the pre-W5.1c version of this test used).
    values = ResolvedValues(fields=[FieldValue(
        key=fld.key, label=fld.label, type=fld.type, locator=fld.locator,
        value=["Dutch", "French"])])

    page = _FakeGreenhousePage(sweep_required_labels=(fld.label,))
    dutch = _FakeTextLocator()
    french = _FakeTextLocator()
    page.controls[("checkbox", "Dutch")] = dutch
    page.controls[("checkbox", "French")] = french

    report = greenhouse.fill(page, fieldmap, values)

    # The QUESTION's own label was NEVER requested as a locator: only the
    # OPTIONS were, exactly matching what the live DOM resolves.
    assert not any(fld.label in str(call) for call in page.requested)
    assert any("Dutch" in str(call) for call in page.requested)
    assert any("French" in str(call) for call in page.requested)

    # Both controls were GENUINELY ticked and READBACK-CONFIRMED, not just
    # assumed: this is the real drive, not a masked fill-error.
    assert dutch.is_checked() is True
    assert french.is_checked() is True

    # The field is confirmed as a whole, never split into per-option debt:
    # no skip, no required gap, filled once.
    assert fld.key not in dict(report.skipped)
    assert [g["key"] for g in report.required_unfilled] == []
    assert report.justified_skips == 0
    assert report.filled == 1
    assert report.complete is True


def test_fill_books_a_gap_when_a_checkbox_group_partially_confirms(tmp_path):
    """The most dangerous shape in this code: a HALF-ticked group must never
    be silently reported as answered. `_drive_click_control` requires EVERY
    driven option to confirm before the field itself counts as filled; one
    option that cannot confirm still books the WHOLE field's gap, even
    though every OTHER option in the same group genuinely was ticked.
    """
    raw = {
        "id": "9102",
        "questions": [
            {"label": "Which of these languages are you fluent in?",
             "required": True,
             "fields": [{"name": "question_langs[]",
                         "type": "multi_value_multi_select",
                         "values": [{"value": 1, "label": "Dutch"},
                                    {"value": 2, "label": "French"}]}]},
        ],
    }
    fieldmap = parse_greenhouse(raw, "fakeco", "9102", now=lambda: _PINNED)
    fld = fieldmap.fields[0]

    values = ResolvedValues(fields=[FieldValue(
        key=fld.key, label=fld.label, type=fld.type, locator=fld.locator,
        value=["Dutch", "French"])])

    page = _FakeGreenhousePage(sweep_required_labels=(fld.label,))
    dutch = _FakeTextLocator()
    # French's control resolves but exposes no .check()/.uncheck(): the
    # non-confirming shape `_drive_click_control`'s own block comment names.
    french = object()
    page.controls[("checkbox", "Dutch")] = dutch
    page.controls[("checkbox", "French")] = french

    report = greenhouse.fill(page, fieldmap, values)

    # Dutch WAS genuinely ticked: the partial drive is real, never skipped
    # wholesale the moment one option is unreachable.
    assert dutch.is_checked() is True

    # The field is NOT counted as filled, and the reason names the control
    # that failed, never the retired blanket hand-off reason.
    reason = dict(report.skipped)[fld.key]
    assert reason == "control exposes no .check()"
    assert reason != greenhouse_fill._HUMAN_HANDOFF_REASON
    assert report.readback_mismatches == []

    # The REQUIRED group still forces NOT_COMPLETE: a partial tick is a gap,
    # never a justified skip and never a silent fill.
    assert [g["key"] for g in report.required_unfilled] == [fld.key]
    assert report.justified_skips == 0
    assert report.filled == 0
    assert report.complete is False


def test_fill_hands_off_every_click_hazard_role_never_auto_clicking(tmp_path):
    """W5.1c RETARGET (owner ruling 2026-07-13, memory
    `automations-automate-everything-tos-boundary`): a programmatic checkbox/
    radio click is no longer a blanket human hand-off. `_needs_human_handoff`
    still NAMES the click-hazard set (a boolean tick, or a checkbox/radio
    locator role); greenhouse now DRIVES it through the shared kernel
    mechanism (`control_toolkit.drive_control` via `_drive_click_control`) and
    only books a gap when the drive itself does not confirm. This proves both
    halves of that contract on two SINGLE (non-group) controls: a confirmed
    drive counts as filled, and a drive that cannot confirm still books its
    own NAMED gap, never a silent auto-click and never the retired blanket
    hand-off reason.

    HOSTILE FRAMING, updated: `certify`'s control DOES resolve and IS
    checkable; the fill must genuinely drive it, not skip it. `shift`'s
    control resolves too, but exposes no `.check()`/`.uncheck()` -- the exact
    non-confirming shape `_drive_click_control`'s own block comment names
    ("a locator exposing no .check()/.uncheck()"). The drive must still
    decline to count it, with the kernel's own per-control reason, never the
    retired `_HUMAN_HANDOFF_REASON`.
    """
    from engine.kernel import fill_toolkit

    # Single-sourced, not reimplemented: greenhouse drives the SAME kernel gate
    # lever and ashby do (workable's superset variant is the documented exception).
    assert greenhouse_fill._needs_human_handoff is fill_toolkit._needs_human_handoff

    certify_label = "I certify the above is accurate"
    shift_label = "Which shift can you work?"
    fieldmap = FieldMap(vendor="greenhouse", posting_id="9103",
                        captured_at=_PINNED, fields=[
        Field(key="certify", label=certify_label, type="boolean", required=True,
             options=[], source="questions",
             locator=Locator(role="checkbox", name=certify_label)),
        Field(key="shift", label=shift_label, type="unknown_radio_type",
             required=False, options=["Day", "Night"], source="questions",
             locator=Locator(role="radio", name=shift_label)),
    ])
    values = ResolvedValues(fields=[
        FieldValue(key="certify", label=certify_label, type="boolean",
                   locator=fieldmap.fields[0].locator, value=True),
        # A single radio-shaped INTENT is a bool True (select THIS control),
        # mirroring ControlSpec's own documented RADIO contract. The GROUP
        # (multi-option, string-valued) shape is a different scenario, covered
        # by test_fill_hands_off_a_checkbox_group_before_building_any_locator.
        FieldValue(key="shift", label=shift_label, type="unknown_radio_type",
                   locator=fieldmap.fields[1].locator, value=True),
    ])

    page = _FakeGreenhousePage(sweep_required_labels=(certify_label,))
    # certify's control EXISTS and is checkable: the drive must genuinely use
    # it, not skip it as a hand-off artifact.
    checkbox = _FakeTextLocator()
    # shift's control EXISTS (resolves) but exposes no .check()/.uncheck():
    # the non-confirming case the kernel books its own gap for.
    radio = object()
    page.controls[("checkbox", certify_label)] = checkbox
    page.controls[("radio", shift_label)] = radio

    report = greenhouse.fill(page, fieldmap, values)

    # certify: DRIVEN (the locator WAS reached) and CONFIRMED (the tick took).
    assert checkbox.checked is True
    assert any(certify_label in str(call) for call in page.requested)
    # shift: its locator WAS reached too (the drive genuinely tried it), but
    # nothing could be ticked since the fake control has no .check() at all.
    assert any(shift_label in str(call) for call in page.requested)

    skips = dict(report.skipped)
    # certify is no longer skipped at all: it was driven and confirmed.
    assert "certify" not in skips
    # shift's gap is the kernel's own per-control reason, never the retired
    # blanket hand-off reason (proves the hand-off path is gone, not silent).
    assert skips["shift"] == "control exposes no .check()"
    assert skips["shift"] != greenhouse_fill._HUMAN_HANDOFF_REASON

    # The REQUIRED control (certify) is now satisfied by the confirmed drive:
    # no required gap, so the fill is complete. shift stays unfilled but is
    # optional, so it never forces NOT_COMPLETE on its own.
    assert report.required_unfilled == []
    assert report.filled == 1
    assert report.complete is True


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
    from engine.kernel.contracts import FillReport

    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeGreenhousePage()
    report = greenhouse.fill(page, fieldmap, values)
    assert isinstance(report, FillReport)
    # to_dict()/caption() (the honest-caption machinery) work unmodified.
    blob = report.to_dict()
    assert blob["vendor"] == "greenhouse"
    assert "caption" in blob


# -- the live-load-bearing selector + the upload fold ---------------------------
# Both are reached through the greenhouse `fill` SUBMODULE: the PACKAGE attribute
# `fill` is the Provider CALLABLE, so the module's internals come through the
# sys.modules / import_module seam the package NAME NOTE documents (the same
# convention as test_providers_{ashby,lever}.py).

greenhouse_fill = importlib.import_module("engine.providers.greenhouse.fill")

# The EXACT string base.sweep_required reads for a REQUIRED Resume/CV on the live
# greenhouse DOM (read-only sweep of the live gitlab posting, 2026-07-14; the same
# posting engine/providers/greenhouse/fill.py's LIVE-DOM comments cite by id).
# Arm 2 of the sweep reads the upload fieldset's <legend> WHOLE, mashing the label
# together with the widget's own chrome; the `<label>`s themselves read only
# "Attach" and "Enter manually", so this legend is the ONLY place the schema's
# "Resume/CV" survives into the DOM-required set.
_LIVE_UPLOAD_LEGEND = ("resume/cv attach attach enter manually enter manually "
                       "accepted file types: pdf, doc, docx, txt, rtf")

# A REQUIRED control the schema does NOT carry, whose label merely BEGINS with the
# same words as the upload control's. Live-proven silencer: under an unbounded
# prefix fold, uploading a CV made this control's gap VANISH.
_FOREIGN_REQUIRED = "resume/cv summary (250 words)"

_PROVEN_UPLOAD = [{"key": "resume", "asset": "cv", "path": "/assets/cv-ats.pdf",
                   "reason": "resume"}]


def _upload_only_fieldmap(*, required=True) -> FieldMap:
    return FieldMap(vendor="greenhouse", posting_id="7701077",
                    captured_at=_PINNED, fields=[
        Field(key="resume", label="Resume/CV", type="input_file",
             required=required, options=[], source="questions",
             locator=Locator(role="button", name="Resume/CV")),
    ])


def _fold_mismatch(dom_required, uploads, *, required=True) -> dict:
    """The completeness diff greenhouse.fill() computes AFTER the upload fold,
    driven through the REAL `_reconcile_uploaded_labels` with the REAL call-site
    arguments."""
    fieldmap = _upload_only_fieldmap(required=required)
    schema_required = {f.label for f in fieldmap.required_fields()}
    folded = greenhouse_fill._reconcile_uploaded_labels(
        set(dom_required), uploads, fieldmap, schema_required)
    return base.completeness_mismatch(schema_required, folded)


def test_greenhouse_combobox_control_selector_anchors_on_live_region():
    """PIN the single selector every live greenhouse dropdown depends on.

    LIVE (read-only probe of the anthropic seal posting, 2026-07-14): the shipped
    `-live-region` anchor resolves exactly 1 control on each of the 6 dropdowns;
    a `-placeholder` anchor resolves ZERO on all 6, because the placeholder node
    sits BELOW the control (`div.select__placeholder < div.select__value-container
    < div.select__control`), so a `:has(> placeholder)` scope matches the
    value-container and finds no `.select__control` beneath it. It also unmounts
    the moment a value is picked.

    Re-anchoring on `-placeholder` therefore kills EVERY dropdown on the page,
    the relocation dropdown this wave exists to protect included -- and it did so
    with the whole suite green, because the fake page resolves its control by
    CALLING this function and is selector-agnostic by construction. The anchor is
    a pure string contract, so it is pinnable here despite the fake's blind spot.
    """
    selector = greenhouse_fill._combobox_control_selector("question_1")

    assert selector.startswith(
        'div:has(> [id="react-select-question_1-live-region"])')
    assert "div.select__control" in selector
    assert "placeholder" not in selector
    # fill() drives the control through base's lazy re-export shim, so that seam
    # must resolve to this very function rather than a divergent copy.
    assert base._combobox_control_selector("question_1") == selector


def test_greenhouse_combobox_readback_rejects_a_foreign_committed_option():
    """The combobox readback must confirm the INTENDED option, not merely that
    react-select committed SOMETHING.

    `_poll_single_value` gates on the chosen option's text appearing in the
    rendered `.select__single-value`. Relaxing that to "any non-empty read
    confirms" (a plausible simplification) makes the driver report an
    unachieved fill as achieved whenever react-select's type-to-filter commits a
    row other than the one asked for -- the exact class this vendor's whole
    readback gate exists to prevent. Pinned here: the widget commits "No" while
    the fill asked for "Yes", and the field must come back UNFILLED and honest.
    """
    fieldmap = _relocation_fieldmap()
    fld = fieldmap.fields[0]
    values = ResolvedValues(fields=[FieldValue(
        key=fld.key, label=fld.label, type=fld.type, locator=fld.locator,
        value="Yes, I am willing to relocate")])

    page = _FakeGreenhousePage(
        combo_field_id=_RELOCATION_KEY,
        # react-select commits a DIFFERENT option than the one asked for.
        combo_reads=("No, I am not willing to relocate",),
        sweep_required_labels=(_RELOCATION_LABEL,))

    report = greenhouse.fill(page, fieldmap, values)

    # The driver typed the option it was given ...
    assert "".join(page.combo_input.keys) == "Yes, I am willing to relocate"
    # ... and the foreign committed value is NOT accepted as the fill landing.
    assert report.filled == 0
    assert [m["key"] for m in report.readback_mismatches] == [_RELOCATION_KEY]
    assert dict(report.skipped)[_RELOCATION_KEY] == (
        "value did not take (readback mismatch)")
    assert [g["key"] for g in report.required_unfilled] == [_RELOCATION_KEY]
    assert report.complete is False


# =============================================================================
# P1-2 / LIVE-DOM FIX #3: the commit path must pick the row it ASKED FOR, never
# the row react-select happens to have focused.
#
# EVERY fixture below is built from the markup captured on the live
# canonical/5569916 posting (`auto/trackB/gh-probe.out`, analysed in
# `T12a-gh-probe-report.md`), NOT from a friendlier invented DOM:
#   - the two option class strings are verbatim from the pre-Enter menu
#     outerHTML at `:46` (re-confirmed on a second control at `:91`);
#   - the 11-row unfiltered list is `:16-:26`;
#   - the 2-row filtered list, and the fact that the focus ring sits on `1`
#     after typing `2`, are `:33-:36`;
#   - `_FakeOptionMenu.press("Enter")` commits the FOCUSED row, which is what
#     react-select actually did live (`:50` read back `1`, unchanged at `:56`).
# That last point is why the pre-existing `_FakeComboInput` could not serve
# here: it commits the intended `combo_reads` on ANY Enter, so it cannot
# express committing the WRONG row, which is the entire defect.
# =============================================================================

# Verbatim from the captured pre-Enter menu outerHTML (`gh-probe.out:46`). The
# `remix-css-*` suffix is a per-build hash and is deliberately kept in the
# fixture so any matcher that keys on a full class string fails here the way it
# would fail live.
_LIVE_OPTION_CLASS = "select__option remix-css-1yzzbro-option"
_LIVE_FOCUSED_OPTION_CLASS = (
    "select__option select__option--is-focused remix-css-1dqp7bk-option")

# The failing control's own lists (`gh-probe.out:16-:26` unfiltered, `:33-:35`
# after typing "2").
_LIVE_UNFILTERED_ROWS = ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10+")
_LIVE_FILTERED_ROWS = ("1", "2")
_LIVE_FIELD_ID = "question_55255480"


class _FakeMenuRow:
    """One rendered `div.select__option`: its visible text and its class
    string, the only two things `_menu_options` reads off a row."""

    def __init__(self, menu, index):
        self._menu = menu
        self._index = index

    def inner_text(self):
        return self._menu.texts[self._index]

    def get_attribute(self, name):
        if name != "class":
            return None
        return (_LIVE_FOCUSED_OPTION_CLASS if self._index == self._menu.focused
                else _LIVE_OPTION_CLASS)


class _FakeOptionMenu:
    """The field's OPEN react-select menu: an ordered row list plus the focus
    ring react-select keeps on exactly one row.

    Models the three behaviours the live probe demonstrated: ArrowDown/ArrowUp
    move the ring (WRAPPING at both ends, as react-select's does), `Enter`
    commits whatever row the ring is on at that instant, and the committed text
    is what `.select__single-value` then reads back. `focused=0` is react-select
    focusing the first filtered row, the state that made Enter commit `1` when
    `2` was asked for (`gh-probe.out:36`, `:50`)."""

    def __init__(self, texts, focused=0, single_value=None):
        self.texts = list(texts)
        self.focused = focused
        self.presses = []
        self._single_value = single_value

    def count(self):
        return len(self.texts)

    def nth(self, index):
        return _FakeMenuRow(self, index)

    def press(self, key):
        self.presses.append(key)
        if not self.texts:
            return
        if key == "ArrowDown":
            self.focused = (self.focused + 1) % len(self.texts)
        elif key == "ArrowUp":
            self.focused = (self.focused - 1) % len(self.texts)
        elif key == "Enter" and self._single_value is not None:
            self._single_value.commit_text(self.texts[self.focused])


class _FakeCommittedValue:
    """The `.select__single-value` node: empty until react-select commits a
    row, then reading back THAT row's text (never the intended one, unless the
    driver genuinely committed it)."""

    def __init__(self):
        self.text = ""

    def commit_text(self, text):
        self.text = text

    def inner_text(self):
        return self.text


class _FakeMenuKeyboard:
    """The page keyboard the live focus-following commit path uses; every key
    is routed into the menu's own ring/commit model."""

    def __init__(self, menu):
        self._menu = menu

    def press(self, key):
        self._menu.press(key)


class _FakeExactMatchControl:
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


class _FakeExactMatchPage:
    """The minimal page the react-select commit path touches: the field's
    live-region-anchored control, its `-listbox`-scoped option menu, a
    focus-following keyboard, and a recorded `wait_for_timeout`.

    `rows=()` models a menu that cannot be READ (the offline/unknown-theme
    case), which is a different state from a menu that renders rows none of
    which match."""

    def __init__(self, *, field_id=_LIVE_FIELD_ID, rows=_LIVE_FILTERED_ROWS,
                 focused=0):
        self.field_id = field_id
        self.single_value = _FakeCommittedValue()
        self.menu = _FakeOptionMenu(rows, focused=focused,
                                    single_value=self.single_value)
        self.combo_input = _FakeComboInput()
        self.control = _FakeExactMatchControl(self.combo_input,
                                               self.single_value)
        self.keyboard = _FakeMenuKeyboard(self.menu)
        self.timeouts = []

    def locator(self, css):
        if css == greenhouse_fill._combobox_control_selector(self.field_id):
            return self.control
        if css == greenhouse_fill._menu_option_selector(self.field_id):
            return self.menu
        raise AssertionError(f"unexpected selector {css!r}")

    def wait_for_timeout(self, ms):
        self.timeouts.append(ms)


def test_greenhouse_menu_option_selector_matches_the_captured_live_markup():
    """The option selector is checked against the REAL captured menu, so a
    fixture that drifts from live markup cannot pass unnoticed.

    Both halves of `_menu_option_selector` must appear in the outerHTML the
    probe captured immediately before Enter (`gh-probe.out:46`): the per-field
    `-listbox` id and the `select__option` row class."""
    captured_outer_html = (
        '<div class="select__menu remix-css-1oc7h4y-menu">'
        '<div class="select__menu-list remix-css-qr46ko" role="listbox" '
        'aria-multiselectable="false" '
        'id="react-select-question_55255480-listbox">'
        '<div class="select__option select__option--is-focused '
        'remix-css-1dqp7bk-option" aria-disabled="false" '
        'id="react-select-question_55255480-option-1" tabindex="-1" '
        'role="option" aria-selected="false">1</div>'
        '<div class="select__option remix-css-1yzzbro-option" '
        'aria-disabled="false" id="react-select-question_55255480-option-2" '
        'tabindex="-1" role="option" aria-selected="false">2</div>'
        '</div></div>')

    selector = greenhouse_fill._menu_option_selector(_LIVE_FIELD_ID)

    assert '[id="react-select-question_55255480-listbox"]' in selector
    assert "div.select__option" in selector
    assert f'id="react-select-{_LIVE_FIELD_ID}-listbox"' in captured_outer_html
    assert 'class="select__option' in captured_outer_html
    # The fixture class strings this suite drives the fake with are the ones
    # actually captured, focused row included.
    assert _LIVE_FOCUSED_OPTION_CLASS in captured_outer_html
    assert _LIVE_OPTION_CLASS in captured_outer_html
    assert greenhouse_fill._FOCUSED_OPTION_CLASS in _LIVE_FOCUSED_OPTION_CLASS
    assert greenhouse_fill._FOCUSED_OPTION_CLASS not in _LIVE_OPTION_CLASS


def test_greenhouse_combobox_walks_focus_to_the_requested_row_not_the_focused_one():
    """THE P1-2 regression: reproduces the live failure exactly.

    Typing `2` into `question_55255480` left the rows `["1", "2"]` with
    react-select's ring on `1` (`gh-probe.out:30-:36`). The pre-fix driver
    pressed Enter there and committed `1` (`:50`), and `select_react_combobox`
    returned False (`:59`) because the readback refused a value it had not
    asked for. The wrong value was nonetheless sitting in a REQUIRED question's
    widget, and would have been submitted.

    Against the pre-fix code this test fails on BOTH counts (no ArrowDown is
    ever pressed, and the committed value reads `1`); it can only pass if the
    driver positively identifies the `2` row and moves the ring onto it first.
    """
    page = _FakeExactMatchPage(rows=_LIVE_FILTERED_ROWS, focused=0)

    landed = greenhouse_fill.select_react_combobox(page, _LIVE_FIELD_ID, "2")

    assert landed is True
    # The ring was walked one row DOWN (from "1" to "2") before any commit ...
    assert page.menu.presses == ["ArrowDown", "Enter", "Escape"]
    # ... so what react-select committed is the row that was asked for.
    assert page.single_value.text == "2"
    assert page.menu.texts[page.menu.focused] == "2"
    # The walk itself costs no settle wait when the re-read confirms straight
    # away: the only recorded wait is the readback's own first poll mark.
    assert page.timeouts == [200]


def test_greenhouse_combobox_parks_when_no_row_matches_exactly():
    """EXACT-MATCH-OR-PARK, and the reason the rule cannot be loosened.

    Driven with the control's REAL unfiltered 11-row list (`gh-probe.out:16-
    :26`), which contains `10+`. Asking for `10` has NO exact match, but `10`
    IS a substring of `10+` -- and the readback downstream (`_poll_single_
    value`) is itself a substring test, so a near-match committed here would
    have sailed through BOTH gates and been counted as a fill.

    Nothing may be committed: no Enter, an untouched widget, and False."""
    page = _FakeExactMatchPage(rows=_LIVE_UNFILTERED_ROWS, focused=0)

    landed = greenhouse_fill.select_react_combobox(page, _LIVE_FIELD_ID, "10")

    assert landed is False
    # Escape only: the menu is closed, never committed into.
    assert page.menu.presses == ["Escape"]
    assert "Enter" not in page.menu.presses
    assert page.single_value.text == ""
    # The ring never moved either -- the park is decided before any navigation.
    assert page.menu.focused == 0


def test_greenhouse_combobox_commits_without_walking_when_match_already_focused():
    """NO REGRESSION on the paths that pass today.

    The single-option privacy control `question_44538607` filters to ONE row
    which react-select already focuses, and Enter commits it (`gh-probe.out:
    :77-:104`, returned True). That path must keep its exact prior DOM
    interaction: zero arrow keys, one Enter, one Escape."""
    page = _FakeExactMatchPage(field_id="question_44538607",
                                rows=("Acknowledge/Confirm",), focused=0)

    landed = greenhouse_fill.select_react_combobox(
        page, "question_44538607", "Acknowledge/Confirm")

    assert landed is True
    assert page.menu.presses == ["Enter", "Escape"]
    assert page.single_value.text == "Acknowledge/Confirm"


def test_greenhouse_combobox_walks_upward_when_that_is_the_shorter_route():
    """The ring wraps, so the shorter direction is taken. With the real 11-row
    list and the ring on row 0, `10+` (the last row) is 1 step UP and 10 steps
    down. Pinned because a down-only walk would spend 10 keystrokes of extra
    re-render churn on the long-list path this vendor also uses for
    countries."""
    page = _FakeExactMatchPage(rows=_LIVE_UNFILTERED_ROWS, focused=0)

    landed = greenhouse_fill.select_react_combobox(page, _LIVE_FIELD_ID, "10+")

    assert landed is True
    assert page.menu.presses == ["ArrowUp", "Enter", "Escape"]
    assert page.single_value.text == "10+"


def test_greenhouse_combobox_parks_when_two_rows_read_the_same():
    """AMBIGUITY parks. Two rows with identical visible text carry different
    underlying values and nothing visible says which was meant, so there is no
    honest way to pick one -- and picking the first would be the very
    first-row-trust this fix removes."""
    page = _FakeExactMatchPage(rows=("Yes", "Yes"), focused=0)

    landed = greenhouse_fill.select_react_combobox(page, _LIVE_FIELD_ID, "Yes")

    assert landed is False
    assert page.menu.presses == ["Escape"]
    assert page.single_value.text == ""


def test_greenhouse_combobox_unreadable_menu_keeps_the_bare_enter_fallback():
    """A menu that renders NO readable rows is IGNORANCE, not evidence that
    nothing matched, and must not be conflated with it: the driver falls back
    to the bare Enter it has always pressed and lets `_poll_single_value` gate,
    exactly as before this fix.

    Pinned deliberately, because deleting this fallback looks like tightening
    the fix and would instead break every caller whose page cannot enumerate a
    menu (`test_providers_base.py`'s combobox fakes, `_FakeGreenhousePage`, and
    any Greenhouse theme whose menu does not render under the `-listbox` id)."""
    page = _FakeExactMatchPage(rows=(), focused=0)

    landed = greenhouse_fill.select_react_combobox(page, _LIVE_FIELD_ID, "2")

    # Enter was pressed on an unreadable menu, committing nothing ...
    assert page.menu.presses == ["Enter", "Escape"]
    # ... and the readback -- still the gate -- honestly reports no fill.
    assert landed is False
    assert page.single_value.text == ""


def test_greenhouse_education_typeahead_commits_the_exact_remote_row():
    """P2-3 / seal-greenhouse NIT-2: the async education typeahead runs the
    SAME exact-match-or-park commit path, so a remote result list whose FIRST
    row is not the requested one no longer commits that first row.

    FIXTURE HONESTY: the row class strings and menu shape are live-captured
    (`gh-probe.out:46`), but the degree LABELS below are placeholders -- no
    Greenhouse degree option list has ever been captured. What this test claims
    is the commit DISCIPLINE on a result list whose first row is wrong; it
    claims nothing about Greenhouse's real degree vocabulary, and whether that
    remote list contains an exact counterpart for the SSOT degree string stays
    an open live-gate question. The shape is harsher than live, not friendlier:
    the wrong row is focused."""
    page = _FakeExactMatchPage(
        field_id="degree--0",
        rows=("Bachelor's Degree", "Master's Degree"), focused=0)

    landed = greenhouse_fill.select_education_typeahead(
        page, "degree--0", "Master's Degree")

    assert landed is True
    assert page.menu.presses == ["ArrowDown", "Enter", "Escape"]
    assert page.single_value.text == "Master's Degree"


def test_greenhouse_education_typeahead_parks_when_no_remote_row_matches():
    """The same driver must PARK rather than commit a near-miss degree. Before
    this fix the first returned row was committed and the readback rejected it
    afterwards, leaving a wrong degree in the widget (NIT-2's blank field was
    the symptom, the committed wrong row was the hazard)."""
    page = _FakeExactMatchPage(
        field_id="degree--0",
        rows=("Bachelor's Degree", "Doctorate"), focused=0)

    landed = greenhouse_fill.select_education_typeahead(
        page, "degree--0", "Master's Degree")

    assert landed is False
    assert "Enter" not in page.menu.presses
    assert page.single_value.text == ""


# =============================================================================
# RS-e: async education typeahead (School / Degree / Discipline). Greenhouse
# renders the education section as react-select selects backed by a DEBOUNCED
# remote search; the static combobox driver commits an empty menu (the live
# 2026-07-18 canonical run left them blank). These pin the async-aware branch:
# detection, the live-region readback, and the bounded debounce settle. The
# live debounce timing is a TB5-R2 toto-gate confirmation -- offline fixtures
# prove the SEAM's shape, not the real remote-search latency.
# =============================================================================

_SCHOOL_KEY = "school"
_SCHOOL_LABEL = "School"
_SCHOOL_VALUE = "University of Testville"


def _education_fieldmap(key=_SCHOOL_KEY, label=_SCHOOL_LABEL) -> FieldMap:
    """A one-field posting: an async education typeahead, carrying the synthetic
    `education_typeahead` type `fill._is_education_typeahead` keys on. OPTIONAL
    (the live canonical posting's education section is `education_optional`), so
    a value that does not take is an honestly-recorded gap, never a forced NOT
    COMPLETE."""
    return FieldMap(vendor="greenhouse", posting_id="7701003",
                    captured_at=_PINNED, fields=[
        Field(key=key, label=label,
              type=greenhouse_fill.EDUCATION_TYPEAHEAD_TYPE, required=False,
              options=[], source="education",
              locator=Locator(role="combobox", name=label)),
    ])


def _education_values(fieldmap, value=_SCHOOL_VALUE) -> ResolvedValues:
    fld = fieldmap.fields[0]
    return ResolvedValues(fields=[FieldValue(
        key=fld.key, label=fld.label, type=fld.type, locator=fld.locator,
        value=value)])


def test_greenhouse_education_typeahead_drives_and_commits_via_live_region(
        monkeypatch):
    # The async education typeahead is driven by the SAME proven react-select
    # primitives -- type-to-filter, focus-following keyboard Enter, `-live-
    # region`-anchored `.select__single-value` readback -- with only the
    # debounce settle added, NOT a third pattern. The readback confirms the SSOT
    # value landed, so the field counts filled.
    fieldmap = _education_fieldmap()
    values = _education_values(fieldmap)

    # The education branch, NOT the static react-select driver, must handle it:
    # routing an async typeahead through select_react_combobox is the mis-fire
    # this task fixes (type-then-immediate-Enter on a still-empty debounced menu).
    monkeypatch.setattr(base, "select_react_combobox", lambda *a, **k: pytest.fail(
        "an education typeahead must not use the static react-select driver"))

    page = _FakeGreenhousePage(
        combo_field_id=_SCHOOL_KEY,
        combo_reads=(_SCHOOL_VALUE,),
        sweep_required_labels=())

    report = greenhouse.fill(page, fieldmap, values)

    # Open the control, type the query one keystroke at a time (never fill()),
    # commit with Enter, dismiss with Escape (never blur).
    assert page.combo_control.clicked == 1
    assert "".join(page.combo_input.keys) == _SCHOOL_VALUE
    assert page.combo_input.pressed == ["Enter", "Escape"]
    # A bounded debounce settle ran (its offsets are recorded before the commit).
    assert page.timeouts
    # The live-region readback confirms it landed -> the field counts filled.
    assert report.filled == 1
    assert report.readback_mismatches == []
    assert report.complete is True


def test_greenhouse_education_typeahead_readback_mismatch_is_not_a_fill():
    # A committed readback that does not correspond to the SSOT value is recorded
    # as a readback mismatch (an honest gap by name), NEVER counted as a fill --
    # the async-path mirror of the react-select foreign-option gate. The widget
    # commits a DIFFERENT school than the one asked for; the driver must reject
    # it and report it, never guess it filled.
    fieldmap = _education_fieldmap()
    values = _education_values(fieldmap, value=_SCHOOL_VALUE)

    page = _FakeGreenhousePage(
        combo_field_id=_SCHOOL_KEY,
        combo_reads=("Some Other University",),
        sweep_required_labels=())

    report = greenhouse.fill(page, fieldmap, values)

    assert "".join(page.combo_input.keys) == _SCHOOL_VALUE
    assert report.filled == 0
    assert [m["key"] for m in report.readback_mismatches] == [_SCHOOL_KEY]
    assert dict(report.skipped)[_SCHOOL_KEY] == (
        "value did not take (readback mismatch)")
    # Optional: the gap is honestly recorded, but an OPTIONAL field left unfilled
    # does not by itself force NOT COMPLETE (no required gap).
    assert report.complete is True


def test_greenhouse_education_typeahead_detection_excludes_plain_fields():
    # The structural signal is the synthetic `education_typeahead` type, so a
    # PLAIN education field -- a free-text `input_text` question, or a static
    # `multi_value_single_select` -- never enters the async branch and keeps its
    # existing path (no regression). The static select still matches the
    # react-select branch, exactly as before.
    typeahead = FieldValue(
        key="school", label="School",
        type=greenhouse_fill.EDUCATION_TYPEAHEAD_TYPE,
        locator=Locator(role="combobox", name="School"),
        value=_SCHOOL_VALUE)
    plain_text = FieldValue(
        key="school_text", label="School", type="input_text",
        locator=Locator(role="textbox", name="School"), value=_SCHOOL_VALUE)
    static_select = FieldValue(
        key="degree", label="Degree", type="multi_value_single_select",
        locator=Locator(role="combobox", name="Degree"), value="BSc")

    assert greenhouse_fill._is_education_typeahead(typeahead) is True
    assert greenhouse_fill._is_education_typeahead(plain_text) is False
    assert greenhouse_fill._is_education_typeahead(static_select) is False
    # The static select is untouched -- it still routes to the react-select path.
    assert greenhouse_fill._is_react_combobox(static_select) is True


class _FakeMenuCountLocator:
    """A fresh option-menu locator per poll (mirroring `_menu_option_count`,
    which re-resolves `page.locator(...)` each time react-select recycles the
    node); `.count()` delegates to the OWNING page's shared sequence, so the
    scripted counts advance ACROSS polls rather than resetting per locator."""

    def __init__(self, page):
        self._page = page

    def count(self):
        return self._page.next_count()


class _FakeMenuPage:
    """The minimal page `_await_typeahead_options` touches: a recorded
    `wait_for_timeout` and a `locator` serving the field's option menu, whose
    scripted count sequence models a debounced menu that renders options only
    AFTER the remote search returns (0 while in flight, >0 once results arrive).
    A single-element sequence is sticky (always that count)."""

    def __init__(self, counts):
        self._counts = list(counts)
        self.timeouts = []

    def wait_for_timeout(self, ms):
        self.timeouts.append(ms)

    def locator(self, css):
        assert css == greenhouse_fill._menu_option_selector(_SCHOOL_KEY)
        return _FakeMenuCountLocator(self)

    def next_count(self):
        if len(self._counts) > 1:
            return self._counts.pop(0)
        return self._counts[0] if self._counts else 0


def test_greenhouse_education_settle_returns_when_the_menu_populates():
    # The bounded settle returns as soon as the debounced search renders an
    # option, waiting ONLY up to that poll -- never the full bound -- so a fast
    # search is not needlessly delayed before the commit.
    page = _FakeMenuPage(counts=[0, 0, 3])   # options arrive on the 3rd poll
    settled = greenhouse_fill._await_typeahead_options(
        page, _SCHOOL_KEY, (200, 500, 1000, 1800))
    assert settled is True
    # Cumulative offsets (200, 500, 1000) -> waited deltas 200, 300, 500; the 4th
    # offset (1800) is never reached because options appeared at the 3rd poll.
    assert page.timeouts == [200, 300, 500]


def test_greenhouse_education_settle_is_bounded_when_no_options_arrive():
    # A query that matches nothing exhausts the WHOLE bound and returns False
    # (never an unbounded poll), so the driver proceeds to an honest readback
    # rather than hanging on a menu that will never populate.
    page = _FakeMenuPage(counts=[0])         # the menu never populates
    settled = greenhouse_fill._await_typeahead_options(
        page, _SCHOOL_KEY, (200, 500, 1000, 1800))
    assert settled is False
    # Every cumulative delta is waited exactly once: 200, 300, 500, 800.
    assert page.timeouts == [200, 300, 500, 800]


# =============================================================================
# RS-e residual: the CAPTURE producer + the resolve reconnection. Greenhouse
# serves education as a top-level `education` TOGGLE (not a questions bucket);
# the section renders client-side. Capture keys structurally on that toggle to
# emit the typeahead fields the fill branch consumes; the greenhouse
# resolve_values post-process reconnects them to the structured SSOT `education`
# list (which the kernel's scalar ssot.get cannot index). Live react-select id /
# option matching stays a TB5-R2 toto-gate confirmation.
# =============================================================================


def _education_raw(education_toggle):
    """A minimal questions=true payload carrying the top-level `education` toggle
    plus one ordinary text + one ordinary combobox question, mirroring the real
    payload shape (the canonical anthropic posting carries `education` at the top
    level alongside `questions`)."""
    return {
        "id": "7701003",
        "education": education_toggle,
        "questions": [
            {"label": "First Name", "required": True,
             "fields": [{"name": "first_name", "type": "input_text"}]},
            {"label": "Are you open to relocation?", "required": True,
             "fields": [{"name": "q_reloc", "type": "multi_value_single_select",
                         "values": [{"label": "Yes"}, {"label": "No"}]}]},
        ],
    }


def test_greenhouse_capture_emits_education_typeaheads_when_toggle_enabled():
    # The PRODUCER the fill branch was missing. The structural signal is the
    # top-level `education` toggle (NOT label text): an enabled toggle emits the
    # three async typeaheads, each carrying EDUCATION_TYPEAHEAD_TYPE + a combobox
    # role so fill routes them to the async driver.
    fm = parse_greenhouse(_education_raw("education_optional"), "fakeco",
                          "7701003", now=lambda: _PINNED)
    edu = [f for f in fm.fields
           if f.type == greenhouse_fill.EDUCATION_TYPEAHEAD_TYPE]
    assert [(f.key, f.label, f.locator.role, f.required) for f in edu] == [
        ("education_school", "School", "combobox", False),
        ("education_degree", "Degree", "combobox", False),
        ("education_discipline", "Discipline", "combobox", False)]

    # `education_required` -> the same three, now required.
    fm_req = parse_greenhouse(_education_raw("education_required"), "fakeco", "x",
                              now=lambda: _PINNED)
    edu_req = [f for f in fm_req.fields
               if f.type == greenhouse_fill.EDUCATION_TYPEAHEAD_TYPE]
    assert len(edu_req) == 3 and all(f.required for f in edu_req)


def test_greenhouse_capture_omits_education_when_toggle_hidden_or_absent():
    # Only an ENABLED toggle produces the typeaheads; a hidden toggle, or the key
    # absent, contributes nothing -- no phantom controls on a posting whose
    # education section is off.
    hidden = parse_greenhouse(_education_raw("education_hidden"), "fakeco", "x",
                              now=lambda: _PINNED)
    assert [f for f in hidden.fields
            if f.type == greenhouse_fill.EDUCATION_TYPEAHEAD_TYPE] == []
    absent = _education_raw("education_optional")
    del absent["education"]
    fm = parse_greenhouse(absent, "fakeco", "x", now=lambda: _PINNED)
    assert [f for f in fm.fields
            if f.type == greenhouse_fill.EDUCATION_TYPEAHEAD_TYPE] == []


def test_greenhouse_capture_leaves_ordinary_controls_unchanged():
    # The education emission must not disturb any other control's type/role -- the
    # fixture-friendlier-than-live-DOM history in this codebase punishes phantom
    # role changes, so key the education detection narrowly (the toggle only).
    fm = parse_greenhouse(_education_raw("education_optional"), "fakeco", "x",
                          now=lambda: _PINNED)
    by_key = {f.key: f for f in fm.fields}
    assert by_key["first_name"].type == "input_text"
    assert by_key["q_reloc"].type == "multi_value_single_select"
    assert by_key["q_reloc"].locator.role == "combobox"
    assert by_key["q_reloc"].type != greenhouse_fill.EDUCATION_TYPEAHEAD_TYPE


def test_greenhouse_resolve_maps_education_typeaheads_from_structured_ssot():
    # The kernel resolver cannot reach the structured `education` list (its scalar
    # `ssot.get` does not index a list), so School/Degree/Discipline would skip as
    # missing:canned_answers.<label>. The greenhouse resolve_values post-process
    # reconnects them: School <- institution, Degree <- degree. Discipline has no
    # seeded key, so it stays skipped BY NAME (partial data, honestly recorded --
    # never parsed out of the degree string).
    fm = parse_greenhouse(_education_raw("education_optional"), "fakeco", "x",
                          now=lambda: _PINNED)
    ssot = SSOT({
        "identity": {"name": "Test Candidate", "email": "t@e.invalid"},
        "education": [{"degree": "BSc Computer Science",
                       "institution": "Example University", "year": 2025}],
    })
    profile = profile_from_real_ssot(ssot)
    values = greenhouse.resolve_values(fm, ssot, profile)

    edu = {v.key: v.value for v in values.fields
           if v.type == greenhouse_fill.EDUCATION_TYPEAHEAD_TYPE}
    assert edu == {"education_school": "Example University",
                   "education_degree": "BSc Computer Science"}
    # Discipline: no datum -> skipped by name, never fabricated.
    assert "education_discipline" not in edu
    assert any(k == "education_discipline" for k, _ in values.skipped)


def test_greenhouse_education_capture_to_fill_routes_into_async_branch(
        monkeypatch):
    # The WHOLE producer chain, end to end: capture emits the typeahead ->
    # resolve maps it from the structured SSOT -> fill drives it through the
    # ASYNC branch (never the static combobox driver). Proves the branch is no
    # longer dead code -- a real fieldmap+SSOT reaches it.
    fm = parse_greenhouse({"id": "x", "education": "education_optional"},
                          "fakeco", "x", now=lambda: _PINNED)
    ssot = SSOT({
        "identity": {"name": "Test Candidate", "email": "t@e.invalid"},
        "education": [{"degree": "BSc Computer Science",
                       "institution": "Example University", "year": 2025}],
    })
    profile = profile_from_real_ssot(ssot)
    values = greenhouse.resolve_values(fm, ssot, profile)

    # Isolate the School field so the one-combobox fake page drives exactly it.
    school = next(v for v in values.fields if v.key == "education_school")
    one = ResolvedValues(fields=[school])

    monkeypatch.setattr(base, "select_react_combobox", lambda *a, **k: pytest.fail(
        "an education typeahead must not use the static react-select driver"))
    # The driver anchors on the LIVE react-select id `school--0` (mapped from the
    # capture key `education_school` by `_EDUCATION_DOM_FIELD_ID`), NOT the key.
    page = _FakeGreenhousePage(
        combo_field_id="school--0",
        combo_reads=("Example University",),
        sweep_required_labels=())
    report = greenhouse.fill(page, fm, one)

    assert "".join(page.combo_input.keys) == "Example University"
    assert page.combo_input.pressed == ["Enter", "Escape"]
    assert report.filled == 1
    assert report.readback_mismatches == []


def test_greenhouse_upload_fold_collapses_the_live_widget_legend():
    # The fold's LEGITIMATE job: a REQUIRED resume that genuinely attached must
    # reconcile to ZERO gaps. Reducing the fold to an exact match (a plausible
    # "cleanup") books a phantom PAIR here -- one dom_only for the legend, one
    # schema_only for the label -- on EVERY required-resume greenhouse posting.
    assert _fold_mismatch({_LIVE_UPLOAD_LEGEND}, _PROVEN_UPLOAD) == {
        "dom_only": [], "schema_only": []}


def test_greenhouse_upload_fold_never_swallows_a_foreign_required_control():
    # THE ANTI-GAMING BOUND. A fold that can make the gap of a control it has
    # nothing to do with disappear is a silencer, whatever else it gets right.
    mismatch = _fold_mismatch({_LIVE_UPLOAD_LEGEND, _FOREIGN_REQUIRED},
                              _PROVEN_UPLOAD)
    # the upload widget's OWN legend still reconciles ...
    assert mismatch["schema_only"] == []
    # ... and the foreign required control the schema lacks KEEPS its gap.
    assert mismatch["dom_only"] == [_FOREIGN_REQUIRED]

    # Nor may the fold widen to a SUBSTRING match: a label that merely mentions
    # the bare label somewhere is not that upload widget's legend either.
    mentions = "please attach your resume/cv summary below"
    assert _fold_mismatch({_LIVE_UPLOAD_LEGEND, mentions},
                          _PROVEN_UPLOAD)["dom_only"] == [mentions]

    # ... AND THE PREFIX TEST IS THE BOUND, not merely containment. The case above
    # survives a substring-widened fold on its own (its leftover words are not
    # widget chrome, so bound (c) still rejects it); this one does NOT. Here the
    # bare label is CONTAINED but not LEADING, and everything around it IS chrome,
    # so a fold that tests containment instead of a prefix swallows a foreign
    # required control whole. A legend is a control's own label with its widget's
    # chrome AFTER it; a sentence that buries the label mid-phrase is a different
    # question, and it keeps its gap.
    wrapped = "attach resume/cv here"
    assert _fold_mismatch({_LIVE_UPLOAD_LEGEND, wrapped},
                          _PROVEN_UPLOAD)["dom_only"] == [wrapped]

    # ... AND THE UPLOAD'S ALLOWED VOCABULARY IS ITS FILENAME, NEVER ITS DIRECTORY
    # PATH. `_filename_tokens` reads the basename/stem ONLY. Widening it to every
    # component of the upload path (a plausible "be more generous" tweak) hands the
    # fold the words of the directories the CV happens to sit in -- with the real CV
    # under a `documents/` directory, the vocabulary would gain "documents" and a
    # foreign required control named below would be SILENCED by an accident of where
    # a file is stored on disk. Where the file lives is not evidence about the page.
    deep_upload = [{"key": "resume", "asset": "cv",
                    "path": "/home/agent/automations/documents/cv-ats.pdf",
                    "reason": "resume"}]
    foreign_dir_named = "resume/cv documents"
    deep = _fold_mismatch({_LIVE_UPLOAD_LEGEND, foreign_dir_named}, deep_upload)
    assert deep["dom_only"] == [foreign_dir_named]
    # and the legitimate legend still folds under that same deep path, so the
    # assertion above is discriminating rather than vacuously inert.
    assert deep["schema_only"] == []

    # ... AND THE REMAINDER MUST BE A SUBSET OF THE WIDGET'S VOCABULARY, NOT MERELY
    # OVERLAP IT. The legend is the widget talking about ITSELF, so EVERY word past
    # the label must be its own; one chrome word does not make a sentence the
    # widget's. Relaxing the subset test to an intersection lets a single shared
    # word ("upload") drag a foreign required control into the fold, and the
    # foreign control tested above does not catch that (none of its words are
    # chrome). This one shares exactly one, and must still keep its gap.
    overlapping = "resume/cv upload summary"
    assert _fold_mismatch({_LIVE_UPLOAD_LEGEND, overlapping},
                          _PROVEN_UPLOAD)["dom_only"] == [overlapping]


def test_greenhouse_upload_fold_matches_the_uploaded_files_own_filename():
    # THE POSITIVE PATH `_filename_tokens` exists for. Every fold test above is
    # NEGATIVE (pins that some dom_only entry does NOT fold), and a negative
    # assertion survives an EMPTY filename vocabulary trivially -- shrinking
    # `_filename_tokens` to `frozenset()` unconditionally only makes the fold
    # MORE conservative, so none of them catch it. This is the real post-upload
    # DOM shape the fold exists to reconcile: the uploaded file's own name,
    # folding with the bare label.
    assert _fold_mismatch({"resume/cv cv-ats.pdf"}, _PROVEN_UPLOAD) == {
        "dom_only": [], "schema_only": []}

    # ITS ANTI-GAMING TWIN: a label carrying a DIFFERENT filename must KEEP its
    # gap -- pinning that the filename vocabulary is scoped to the ACTUAL
    # upload (`_PROVEN_UPLOAD`'s own "cv-ats.pdf"), never to any word that
    # merely looks like a document filename.
    mismatch = _fold_mismatch({"resume/cv someone-elses-cv.pdf"},
                              _PROVEN_UPLOAD)
    assert mismatch["schema_only"] == ["resume/cv"]
    assert mismatch["dom_only"] == ["resume/cv someone-elses-cv.pdf"]


def test_greenhouse_upload_fold_is_gated_on_a_proven_required_upload():
    # (a) UPLOAD-GATED. With no PROVEN attachment the fold stays inert, so a
    # required resume that never attached still books its gap. This is the exact
    # state the cross-check exists to catch: a fold that fires here silences it.
    ungated = _fold_mismatch({_LIVE_UPLOAD_LEGEND}, [])
    assert ungated["dom_only"] == [_LIVE_UPLOAD_LEGEND]
    assert ungated["schema_only"] == ["resume/cv"]

    # (b) SCHEMA-REQUIRED-GATED. An OPTIONAL resume is absent from
    # `schema_required`, so the fold has nothing to reconcile toward and must
    # leave the DOM side untouched: the page marking required what the schema does
    # not is a genuine disagreement, and it stays visible under its own honest
    # name rather than being quietly rewritten to the schema's.
    optional = _fold_mismatch({_LIVE_UPLOAD_LEGEND}, _PROVEN_UPLOAD,
                              required=False)
    assert optional["dom_only"] == [_LIVE_UPLOAD_LEGEND]


def test_fill_upload_fold_does_not_silence_a_foreign_required_control(tmp_path):
    # The same anti-gaming bound, end to end through the REAL fill(), so a mutant
    # at the CALL SITE (e.g. handing the fold an empty schema_required) is caught
    # too, not just one inside the fold.
    fieldmap = _upload_only_fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeGreenhousePage(
        file_inputs=[_FakeFileInput(id="resume",
                                    accept=".pdf,.doc,.docx,.txt,.rtf")],
        sweep_required_labels=(_LIVE_UPLOAD_LEGEND, _FOREIGN_REQUIRED))

    report = greenhouse.fill(page, fieldmap, values)

    # The resume genuinely uploaded, so its own legend reconciles and books NO gap.
    assert [u["key"] for u in report.uploads] == ["resume"]
    gap_keys = [g["key"] for g in report.required_unfilled]
    assert f"dom-sweep:{_LIVE_UPLOAD_LEGEND}" not in gap_keys
    assert "dom-sweep:resume/cv" not in gap_keys
    # The FOREIGN required control still forces NOT_COMPLETE.
    assert gap_keys == [f"dom-sweep:{_FOREIGN_REQUIRED}"]
    assert report.complete is False
    assert report.caption().endswith("NOT COMPLETE")


# =============================================================================
# The live-active guards the fake harness could not previously REACH: the fold's
# vocabulary, the page-wide upload confirmation, the page-keyboard commit, the
# remove-control vocabulary, and the uploads' exclusion from the text pass.
# =============================================================================


def test_greenhouse_upload_legend_vocabulary_is_pinned_to_widget_chrome(
        monkeypatch):
    # THE ANTI-GAMING BOUND IS THE VOCABULARY, not any one label. Every other
    # fold test pins a SINGLE foreign label ("Resume/CV Summary (250 words)"),
    # so one extra word in the frozenset re-arms the silencer for every OTHER
    # question-word label without failing any of them.
    greenhouse_fill = importlib.import_module("engine.providers.greenhouse.fill")

    assert greenhouse_fill._UPLOAD_LEGEND_TOKENS == frozenset({
        "attach", "attached", "enter", "manually", "accepted", "file", "files",
        "type", "types", "upload", "uploaded", "browse", "choose", "select",
        "drop", "drag", "here", "or", "remove", "delete", "clear",
        "pdf", "doc", "docx", "txt", "rtf", "odt", "pages",
    })
    # Every token above is the upload widget's own CHROME. A QUESTION-word is
    # not: it is what a DIFFERENT required control ASKS, and the moment one
    # joins this set that control's gap can be folded away.
    for question_word in ("summary", "salary", "years", "experience",
                          "cover", "letter", "why", "notice"):
        assert question_word not in greenhouse_fill._UPLOAD_LEGEND_TOKENS

    # MEMBERSHIP IS CAUSAL, so the two assertions above are load-bearing rather
    # than decorative. As it stands, a foreign REQUIRED salary control whose
    # label leads with the bare upload label keeps its gap ...
    foreign_salary = "resume/cv salary"
    assert _fold_mismatch({_LIVE_UPLOAD_LEGEND, foreign_salary},
                          _PROVEN_UPLOAD)["dom_only"] == [foreign_salary]
    # ... and admitting ONE question-word to the vocabulary is all it takes to
    # silence it: the gap vanishes, `required_unfilled` loses an entry, and a
    # form with an unanswered required question can read COMPLETE.
    monkeypatch.setattr(greenhouse_fill, "_UPLOAD_LEGEND_TOKENS",
                        greenhouse_fill._UPLOAD_LEGEND_TOKENS | {"salary"})
    assert _fold_mismatch({_LIVE_UPLOAD_LEGEND, foreign_salary},
                          _PROVEN_UPLOAD)["dom_only"] == []


def test_greenhouse_upload_confirmed_page_wide_when_the_container_cannot():
    # `_page_shows_filename` is the ONLY confirmation path greenhouse leaves
    # when it UNMOUNTS the <input type=file> after the attach (the `control is
    # None` case `_reconfirm_late_uploads` documents and depends on), and the
    # only one when it renders the filename OUTSIDE the input's own container.
    filename = "/assets/cv-ats.pdf"
    control = _FakeFileInput(id="resume", rendered_confirmed=False)
    control.set_input_files(filename)
    page = _FakeGreenhousePage(page_rendered_filenames=["cv-ats.pdf"])

    # The container-scoped signal is mute (no filename text, no remove control) ...
    assert base._upload_widget_confirmed(control, filename) is False
    # ... yet the page RENDERS the stem, so the upload IS confirmed.
    assert base.poll_upload_confirmed(page, control, filename,
                                      poll_ms=(0,)) is True
    # And with the input UNMOUNTED (control is None), the page-wide match is the
    # only signal there is: without it a genuinely attached CV reads as a
    # non-attach and books a phantom required gap.
    assert base.poll_upload_confirmed(page, None, filename,
                                      poll_ms=(0,)) is True
    # It still FAILS CLOSED: a page that renders the name NOWHERE is a genuine
    # non-attach and must never be promoted.
    assert base.poll_upload_confirmed(_FakeGreenhousePage(), None, filename,
                                      poll_ms=(0,)) is False


def test_fill_confirms_an_upload_the_widget_container_never_shows(tmp_path):
    # The same guard end to end through the REAL fill(): greenhouse renders the
    # filename as a sibling of the widget, NOT inside the native input's own
    # parent, so `_upload_widget_confirmed` cannot see it and the page-wide stem
    # match is what stands between a genuine attach and a phantom required gap.
    fieldmap = _upload_only_fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    resume_path = {fv.key: fv.value for fv in values.fields}["resume"]
    page = _FakeGreenhousePage(
        file_inputs=[_FakeFileInput(id="resume",
                                    accept=".pdf,.doc,.docx,.txt,.rtf",
                                    rendered_confirmed=False)],
        sweep_required_labels=(_LIVE_UPLOAD_LEGEND,),
        page_rendered_filenames=[Path(resume_path).name])

    report = greenhouse.fill(page, fieldmap, values)

    # The page was genuinely asked (the signal is exercised, not incidental) ...
    assert any(r[0] == "text" for r in page.requested)
    # ... and the required resume counts uploaded, with no gap and no skip.
    assert [u["key"] for u in report.uploads] == ["resume"]
    assert "resume" not in dict(report.skipped)
    assert report.required_unfilled == []
    assert report.complete is True


def test_greenhouse_combobox_commits_through_the_page_keyboard(tmp_path):
    # THE LIVE COMMIT MECHANISM. react-select detaches its filter input on every
    # keystroke, so `combo_input.press("Enter")` hangs on Playwright's
    # actionability wait for a stale node; the driver commits through the PAGE
    # keyboard (focus-following) whenever the page exposes one -- which a live
    # page ALWAYS does. Reverting to the locator press restores the documented
    # live hang, and no other test can see it (the default fake page has no
    # keyboard, so every one of them takes the fallback).
    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeGreenhousePage(with_keyboard=True)

    report = greenhouse.fill(page, fieldmap, values)

    assert page.keyboard.pressed == ["Enter", "Escape"]
    # The detached filter input is NEVER pressed: it is typed into, and nothing
    # more.
    assert page.combo_input.pressed == []
    assert "".join(page.combo_input.keys) == "No"
    # The commit still lands (the readback confirms the option), so the guard is
    # pinned on a WORKING path, not merely on which object was called.
    assert report.readback_mismatches == []
    assert report.required_unfilled == []
    assert report.complete is True


def test_greenhouse_upload_widget_confirmation_needs_remove_vocabulary():
    # The container's SECOND signal is a control whose ACCESSIBLE NAME carries
    # the remove/delete/clear vocabulary (`_REMOVE_CONTROL_NAME_RE`). The mere
    # PRESENCE of a button is not a confirmation: a live upload widget's parent
    # can hold buttons that say nothing about whether a file attached.
    filename = "/assets/cv-ats.pdf"

    foreign = _FakeFileInput(id="resume", widget_shows_filename=False,
                             remove_control_name="Submit application")
    foreign.set_input_files(filename)
    assert base._upload_widget_confirmed(foreign, filename) is False

    # The SAME container, its only button now named in the remove vocabulary:
    # THAT is a confirmation, and here it is the only signal there is.
    removable = _FakeFileInput(id="resume", widget_shows_filename=False,
                               remove_control_name="Remove file")
    removable.set_input_files(filename)
    assert base._upload_widget_confirmed(removable, filename) is True

    # A container with no button at all confirms nothing either.
    buttonless = _FakeFileInput(id="resume", widget_shows_filename=False,
                                remove_control_name=None)
    buttonless.set_input_files(filename)
    assert base._upload_widget_confirmed(buttonless, filename) is False


def test_fill_never_drives_an_uploaded_field_a_second_time(tmp_path):
    # UPLOADS ARE EXCLUDED FROM THE NON-UPLOAD PASS. Feed them back into it and
    # every upload is driven a SECOND time through `_fill_field` ->
    # `_apply_native` -> `type_human(<the upload's BUTTON locator>, "/path/to/
    # cv.pdf")` on a LIVE employer form -- booking a fill-error / readback-
    # mismatch skip against a key that had uploaded perfectly.
    greenhouse_fill = importlib.import_module("engine.providers.greenhouse.fill")

    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    upload_keys = [fv.key for fv in values.fields
                   if greenhouse_fill._is_upload(fv)]
    assert set(upload_keys) == {"resume", "headshot"}
    page = _FakeGreenhousePage()

    report = greenhouse.fill(page, fieldmap, values)

    # Both uploaded, and NEITHER is skipped or readback-mismatched afterwards.
    assert {u["key"] for u in report.uploads} == {"resume", "headshot"}
    skipped = dict(report.skipped)
    mismatched = {m["key"] for m in report.readback_mismatches}
    for key in upload_keys:
        assert key not in skipped
        assert key not in mismatched
    # Driven exactly ONCE each, through the file input ...
    assert [fi.set_input_files_calls for fi in page.file_inputs] == [1, 1]
    # ... and the upload's own role locator (`button`, the shape an upload field
    # captures with) is never built at all: the second drive cannot even begin.
    assert not any(r[:2] == ("role", "button") for r in page.requested)


# =============================================================================
# The SEAL-CRITICAL guards: a SWALLOWED safety abort, two false completeness
# claims manufactured out of an EMPTY string, a sibling-satisfied textarea's
# phantom sweep gap, a self-contradicting report, and a skip reason that lies.
# Each is a guard whose failure mode is a PHANTOM, a FALSE COMPLETENESS CLAIM,
# or a SAFETY BREACH on a real employer's form -- never a cosmetic regression.
# =============================================================================


def test_fill_aborts_when_an_upload_asset_is_not_whitelisted(tmp_path):
    """A NON-WHITELISTED ASSET ABORTS THE FILL. It must never soften into a skip.

    `_safe_upload`'s whitelist is the one thing standing between this engine and
    ATTACHING A FILE IT WAS NEVER AUTHORISED TO SEND to a real employer. The
    whitelist is RECONSTRUCTED at fill time from the FieldValue's own asset
    provenance (`fill_toolkit._current_assets` ->
    `FillAssets.single_asset_whitelist(fv.asset, fv.value)`), so a Path that
    arrives carrying NO asset name yields an EMPTY whitelist: exactly the "this
    document was never sanctioned for upload" state, and the one the guard
    exists for. (Hostile framing, the convention this file already uses for
    `test_greenhouse_combobox_nonoption_value_stays_unfilled`: the sanctioned
    resolver never builds such a FieldValue, and a safety guard that is only
    correct because nothing currently reaches it is not a guard.)

    Both `_fill_upload` and the non-upload loop RE-RAISE `FillSafetyError` ahead
    of their fail-soft `except Exception`. Swallow it and the refusal degrades
    into a per-field skip: the fill CONTINUES driving the rest of the employer's
    form, and the report says "upload-error" about a refusal that was supposed to
    stop everything.
    """
    fieldmap = FieldMap(vendor="greenhouse", posting_id="7701055",
                        captured_at=_PINNED, fields=[
        Field(key="resume", label="Resume/CV", type="input_file",
             required=True, options=[], source="questions",
             locator=Locator(role="button", name="Resume/CV")),
        Field(key="first_name", label="First Name", type="input_text",
             required=True, options=[], source="questions",
             locator=Locator(role="textbox", name="First Name")),
    ])
    rogue = tmp_path / "passport-scan.pdf"
    rogue.write_bytes(b"stub")
    values = ResolvedValues(fields=[
        FieldValue(key="resume", label="Resume/CV", type="input_file",
                   locator=fieldmap.fields[0].locator, value=rogue),
        FieldValue(key="first_name", label="First Name", type="input_text",
                   locator=fieldmap.fields[1].locator, value="Test"),
    ])
    # It IS driven as an upload (a Path value), and it carries NO asset
    # provenance -- so the whitelist it is checked against is empty.
    assert greenhouse_fill._is_upload(values.fields[0])
    assert not values.fields[0].asset

    page = _FakeGreenhousePage(
        file_inputs=[_FakeFileInput(id="resume",
                                    accept=".pdf,.doc,.docx,.txt,.rtf")],
        sweep_required_labels=("Resume/CV", "First Name"))

    with pytest.raises(FillSafetyError, match="not in the FillAssets whitelist"):
        greenhouse.fill(page, fieldmap, values)

    # ABORTED, not degraded: the file never reached the input, and the fill never
    # went on to drive the REST of the form (a swallowed refusal would).
    assert page.file_inputs[0].set_input_files_calls == 0
    assert page.file_inputs[0].uploaded is None
    assert page.controls[("textbox", "First Name")].input_value() == ""
    assert ("role", "textbox", "First Name") not in page.requested


def test_fill_aborts_when_a_safety_error_escapes_the_field_drive_path(
        monkeypatch):
    """THE SAME ABORT, at the non-upload loop's own `except FillSafetyError`.

    The loop's fail-soft `except Exception` books a per-field "fill-error" skip
    and carries on -- correct for a flaky control, catastrophic for a SAFETY
    refusal. `FillSafetyError` is therefore re-raised ahead of it, so any safety
    primitive the drive path reaches (`_safe_click`'s submit denylist,
    `_safe_upload`'s whitelist) stops the fill dead rather than being recorded as
    a field that did not take.

    The raiser is injected at `base.select_react_combobox` -- the seam
    `_fill_field` genuinely drives a combobox through -- because no primitive on
    today's non-upload path raises it; the guard is the loop's CONTRACT with the
    kernel's safety layer, and a contract that holds only while nothing exercises
    it is exactly the one that breaks silently when something does.
    """
    fieldmap = _relocation_fieldmap()
    fld = fieldmap.fields[0]
    values = ResolvedValues(fields=[FieldValue(
        key=fld.key, label=fld.label, type=fld.type, locator=fld.locator,
        value="Yes, I am willing to relocate")])

    def refuse(*args, **kwargs):
        raise FillSafetyError("refusing to click 'Submit application': the "
                              "trigger name matches the submit denylist")

    monkeypatch.setattr(base, "select_react_combobox", refuse)
    page = _FakeGreenhousePage(combo_field_id=_RELOCATION_KEY,
                               sweep_required_labels=(_RELOCATION_LABEL,))

    with pytest.raises(FillSafetyError, match="submit denylist"):
        greenhouse.fill(page, fieldmap, values)


def test_greenhouse_combobox_empty_option_never_reports_a_landed_selection():
    """AN EMPTY OPTION TEXT IS NOT A LANDED SELECTION.

    `_poll_single_value` returns False on a blank `want` BEFORE it polls. Flip
    that to True and the driver reports a selection it never made: the engine
    believes it picked an option, the required dropdown counts FILLED, and the
    form reads COMPLETE with the control still empty -- a false completeness
    claim, on a required question, with no readback anywhere behind it.

    The widget is deliberately left showing a REAL committed value here, so the
    empty-`want` False is a decision about what was ASKED FOR, never an accident
    of an empty page.
    """
    page = _FakeGreenhousePage(combo_field_id=_RELOCATION_KEY,
                               combo_reads=("Yes, I am willing to relocate",))
    page.single_value.commit()          # the widget genuinely shows a value

    # Discriminating: a real option text DOES read back as landed here ...
    assert greenhouse_fill._poll_single_value(
        page, _RELOCATION_KEY, "Yes, I am willing to relocate", (0,)) is True
    # ... and an EMPTY / whitespace-only option text can never be one.
    assert greenhouse_fill._poll_single_value(
        page, _RELOCATION_KEY, "", (0,)) is False
    assert greenhouse_fill._poll_single_value(
        page, _RELOCATION_KEY, "   ", (0,)) is False

    # END TO END: a blank value forced down the fill path (the shape a bad
    # overlay/SSOT read produces) must come back UNFILLED and honest -- never a
    # required dropdown closed by a selection nobody made.
    fieldmap = _relocation_fieldmap()
    fld = fieldmap.fields[0]
    forced = ResolvedValues(fields=[FieldValue(
        key=fld.key, label=fld.label, type=fld.type, locator=fld.locator,
        value="")])
    blank_page = _FakeGreenhousePage(
        combo_field_id=_RELOCATION_KEY,
        combo_reads=("Yes, I am willing to relocate",),
        sweep_required_labels=(_RELOCATION_LABEL,))

    report = greenhouse.fill(blank_page, fieldmap, forced)

    assert report.filled == 0
    assert [m["key"] for m in report.readback_mismatches] == [_RELOCATION_KEY]
    assert [g["key"] for g in report.required_unfilled] == [_RELOCATION_KEY]
    assert report.complete is False
    assert report.caption().endswith("NOT COMPLETE")


def test_greenhouse_upload_confirmation_rejects_an_empty_filename():
    """AN EMPTY FILENAME CONFIRMS NO UPLOAD.

    `_page_shows_filename` is a POSITIVE signal: the vendor rendered THIS file's
    name. With no stem there is nothing to have rendered, so it returns False
    before it queries anything. Flip that to True and every `poll_upload_
    confirmed` on an empty filename confirms unconditionally -- the engine books
    an `uploads` entry, drops the required gap, and reports a document attached
    to a real employer's form when nothing was.
    """
    page = _FakeGreenhousePage(page_rendered_filenames=["cv-ats.pdf"])
    control = _FakeFileInput(id="resume")        # nothing ever attached to it

    # Discriminating: with a real filename the page-wide stem match DOES confirm.
    assert base.poll_upload_confirmed(page, control, "/assets/cv-ats.pdf",
                                      poll_ms=(0,)) is True
    # With an empty one it confirms nothing -- and does not even ask the page:
    # an empty stem matches every text node there is.
    assert greenhouse_fill._page_shows_filename(page, "") is False
    assert base.poll_upload_confirmed(page, control, "", poll_ms=(0,)) is False
    assert not any(r[0] == "text" and r[1] == "" for r in page.requested)
    # The container-scoped signal agrees: no file, no confirmation.
    assert base._upload_widget_confirmed(control, "") is False


def test_fill_sibling_satisfied_textarea_books_no_dom_sweep_gap(tmp_path):
    """A SIBLING-SATISFIED REQUIRED TEXTAREA BOOKS ZERO SWEEP GAP.

    `schema_required` excludes any key satisfied by a sibling upload, because in
    file-upload mode the vendor GENUINELY NEVER RENDERS that control: sweeping
    for it manufactures a schema/DOM disagreement out of a control that is
    working exactly as designed, and the phantom gap forces NOT_COMPLETE on a
    form that is complete.

    The paste-textarea carries its OWN label here. That is the whole point: when
    it shares the file field's label (as a single two-field Greenhouse question
    does), the schema-required LABEL SET collapses to the same set with or
    without the exclusion, so the guard is inert and NOTHING catches its removal.
    Correctness must not rest on that accident -- the exclusion is keyed on the
    KEY precisely so a separately-labelled paste question cannot book a phantom.
    """
    fieldmap = FieldMap(vendor="greenhouse", posting_id="7701097",
                        captured_at=_PINNED, fields=[
        Field(key="resume", label="Resume/CV", type="input_file",
             required=True, options=[], source="questions",
             locator=Locator(role="button", name="Resume/CV")),
        Field(key="resume_text", label="Paste your resume instead",
             type="textarea", required=True, options=[], source="questions",
             locator=Locator(role="textbox", name="Paste your resume instead")),
    ])
    ssot = SSOT({
        "identity": {"name": "Test Candidate",
                     "email": "test.candidate@example.invalid"},
        "canned_answers": {
            "resume_text": "Test Candidate, platform engineer.",
        },
    })
    profile = profile_from_real_ssot(ssot)
    values = greenhouse.resolve_values(fieldmap, ssot, profile,
                                       assets=_assets(tmp_path))
    by_key = {fv.key: fv for fv in values.fields}
    assert "resume" in by_key and "resume_text" in by_key

    # FILE MODE: the DOM renders the upload control and NOT the paste textarea.
    page = _FakeGreenhousePage(
        file_inputs=[_FakeFileInput(id="resume",
                                    accept=".pdf,.doc,.docx,.txt,.rtf")],
        sweep_required_labels=("Resume/CV",))

    report = greenhouse.fill(page, fieldmap, values)

    assert [u["key"] for u in report.uploads] == ["resume"]
    assert dict(report.skipped)["resume_text"] == (
        "satisfied by sibling file upload: resume")
    # THE PIN: the satisfied textarea's label never enters the schema-required
    # set, so the DOM sweep books NO gap against a control the vendor never
    # renders, and the form reads COMPLETE rather than carrying a phantom.
    assert not any(str(g["key"]).startswith("dom-sweep:")
                  for g in report.required_unfilled)
    assert report.required_unfilled == []
    assert report.complete is True
    assert report.caption().endswith("COMPLETE")


def test_fill_late_confirmed_upload_never_also_reported_as_skipped(
        tmp_path, monkeypatch):
    """A REPORT MAY NOT CONTRADICT ITSELF ABOUT A REAL EMPLOYER SUBMISSION.

    When `_reconfirm_late_uploads` recovers a false-negative upload it must also
    RETRACT the earlier "upload did not attach (readback)" skip that its own
    inline confirmation booked. Drop `_drop_extra_skip`/`_drop_readback_mismatch`
    and the same key is listed BOTH in `uploads` (attached) AND in `skipped`
    (did not attach): the report says two opposite things about whether a
    document reached a real employer, and every downstream reader -- the owner,
    the ntfy caption, the completeness arithmetic -- picks one at random.

    `base.poll_upload_confirmed` is stubbed to the live shape the fix exists for:
    a FALSE NEGATIVE inline (React still busy driving the other fields), a
    genuine confirm at end-of-fill.
    """
    fieldmap = FieldMap(vendor="greenhouse", posting_id="7701096",
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
    values = greenhouse.resolve_values(fieldmap, ssot, profile,
                                       assets=_assets(tmp_path))
    page = _FakeGreenhousePage(
        file_inputs=[_FakeFileInput(id="resume",
                                    accept=".pdf,.doc,.docx,.txt,.rtf")],
        sweep_required_labels=("Resume/CV",))

    calls = []

    def fake_poll_upload_confirmed(page_arg, control, filename, **kwargs):
        calls.append(filename)
        return len(calls) > 1        # False inline (mid-fill), True end-of-fill

    monkeypatch.setattr(base, "poll_upload_confirmed",
                        fake_poll_upload_confirmed)

    report = greenhouse.fill(page, fieldmap, values)

    assert len(calls) == 2           # the inline confirm DID fail first
    upload_keys = {u["key"] for u in report.uploads}
    assert upload_keys == {"resume"}
    skipped = dict(report.skipped)
    # THE PIN: an attached upload appears in NEITHER the skips nor the readback
    # mismatches. Its stale "did not attach" entry is retracted, not left to
    # contradict the attachment it now sits beside.
    assert "resume" not in skipped
    assert upload_keys.isdisjoint(set(skipped))
    assert upload_keys.isdisjoint({m["key"] for m in report.readback_mismatches})
    # ... and no key is booked TWICE (a stale entry lingering beside its
    # replacement double-counts the same control).
    skip_keys = [key for key, _ in report.skipped]
    assert len(skip_keys) == len(set(skip_keys))
    assert skipped["resume_text"] == "satisfied by sibling file upload: resume"
    assert report.required_unfilled == []
    assert report.complete is True


def test_fill_not_rendered_skip_reason_never_names_an_absent_sibling(tmp_path):
    """THE NOT-RENDERED SKIP REASON MUST BE TRUE.

    The "control not rendered (vendor is in file-upload mode) and sibling <k> did
    not attach" skip is scoped to `_TEXT_UPLOAD_SIBLINGS` paste-textareas, whose
    sibling file field is what makes both halves of that sentence true. Widen it
    past them (drop the `sibling_key is not None` half of the guard) and ANY
    control that fails to resolve -- a mis-captured locator, a field behind a
    collapsed section, a genuine vendor bug -- is written up as a file-upload-mode
    textarea whose sibling did not attach. There is no sibling. There is no
    file-upload mode. The owner and the auditor read these strings to decide
    whether a gap is real, and a reason string that invents a cause they cannot
    check is worse than no reason at all.

    The honest report for a control that does not resolve is that the fill TRIED
    and FAILED (a fill-error), which is exactly what it did.
    """
    fieldmap = FieldMap(vendor="greenhouse", posting_id="7701095",
                        captured_at=_PINNED, fields=[
        Field(key="first_name", label="First Name", type="input_text",
             required=True, options=[], source="questions",
             locator=Locator(role="textbox", name="First Name")),
    ])
    ssot = _fake_ssot()
    profile = profile_from_real_ssot(ssot)
    values = greenhouse.resolve_values(fieldmap, ssot, profile,
                                       assets=_assets(tmp_path))
    assert [fv.key for fv in values.fields] == ["first_name"]

    # A NON-SIBLING control whose own locator resolves to ZERO nodes (the
    # count()-only fake this file already uses for that state).
    page = _FakeGreenhousePage(sweep_required_labels=("First Name",))
    page.controls[("textbox", "First Name")] = _FakeRoleQuery(present=False)

    report = greenhouse.fill(page, fieldmap, values)

    reason = dict(report.skipped)["first_name"]
    # THE PIN: the reason may not invent a sibling, nor a file-upload mode, for a
    # field that has neither.
    assert "sibling" not in reason
    assert "not rendered" not in reason
    assert "file-upload mode" not in reason
    assert reason.startswith("fill-error")
    # And the required field still books its honest gap either way.
    assert [g["key"] for g in report.required_unfilled] == ["first_name"]
    assert report.justified_skips == 0
    assert report.complete is False


# =============================================================================
# R4 LIVE-DOM fixes: education 3-field drive, nationality react-select MULTI,
# privacy single-affirmative, and the ToS-forbidden census verdict (class ii).
# The fakes below model the standard remix react-select shape verbatim from the
# 2026-07-19 canonical/4124053 live probe (select__container / select-shell /
# react-select-<id>-live-region), not a friendlier invention.
# =============================================================================


class _FakeReactSelectInput:
    """A react-select control's own filter input. `type_human` clicks it once
    (`_settle_focus`) then types one char at a time; the driver commits with a
    focus-following keyboard Enter (here, the input's own `press`, since the fake
    page exposes no `page.keyboard`). SINGLE controls reveal their `-single-value`
    on Enter; MULTI controls append the typed buffer as a `-multi-value` chip and
    clear the filter, so the next option types clean. NEVER fill()/blur()."""

    def __init__(self, control):
        self._control = control
        self.keys = []
        self.pressed = []
        self._buffer = ""

    def click(self):
        pass

    def press_sequentially(self, ch, delay=None):
        self.keys.append(ch)
        self._buffer += ch

    def press(self, key):
        self.pressed.append(key)
        if key != "Enter":
            return
        if self._control.multi:
            if self._buffer:
                self._control.selected.append(self._buffer)
                self._buffer = ""
        else:
            self._control.single.commit()

    def fill(self, *args, **kwargs):
        raise AssertionError("react-select driver must never call fill()")

    def blur(self, *args, **kwargs):
        raise AssertionError("react-select driver must never blur")


class _FakeChipSet:
    """The rendered `.select__multi-value__label` chips of a multi-select (one per
    committed option), which `_multi_value_texts` counts and reads by `.nth(i)`."""

    def __init__(self, texts):
        self._texts = texts

    def count(self):
        return len(self._texts)

    def nth(self, i):
        return _FakeSweepLocator(text=self._texts[i])


class _FakeReactSelectControl:
    """One greenhouse remix react-select control, keyed by its LIVE field_id.
    `multi=False` is a single typeahead (School/Degree/Discipline, privacy);
    `multi=True` is a multi-select (nationality). `.count()==1` is the live-region
    presence signal `_is_react_multiselect` probes."""

    def __init__(self, *, single_reads=(), multi=False):
        self.multi = multi
        self.clicked = 0
        self.selected = []                       # committed chips (multi)
        self.single = _FakeSingleValue(single_reads)
        self.input = _FakeReactSelectInput(self)

    def click(self):
        self.clicked += 1

    def count(self):
        return 1

    def locator(self, css):
        if css == "input":
            return self.input
        if css.endswith(".select__single-value"):
            return self.single
        if css.endswith(".select__multi-value__label"):
            return _FakeChipSet(self.selected)
        raise AssertionError(f"unexpected control-scoped locator: {css!r}")


class _FakeReactSelectPage:
    """A page exposing one or more react-select controls by their LIVE field_id,
    plus the sweep selectors. No `page.keyboard` (so the commit rides the input's
    own press, where the fakes model it). `get_by_role`/`get_by_label` RAISE: a
    react-select control must be driven through its own live-region-anchored
    input, NEVER located as a checkbox -- reaching for one here fails loudly."""

    def __init__(self, controls, *, sweep_required_labels=()):
        self._controls = dict(controls)
        self.keyboard = None
        self.timeouts = []
        self.routed = []
        self.requested = []
        self.context = _PageBoundContext(self)
        self._url = "https://boards.greenhouse.io/canonical/jobs/4124053"
        self.file_inputs = []
        self._sweep_required = [
            _FakeSweepLocator(attrs={"aria-label": label})
            for label in sweep_required_labels]

    @property
    def url(self):
        return self._url

    def route(self, pattern, handler):
        self.routed.append((pattern, handler))

    def wait_for_timeout(self, ms):
        self.timeouts.append(ms)

    def query_selector_all(self, selector):
        return []

    def get_by_role(self, role, name=None, exact=None):
        self.requested.append(("role", role, name))
        raise KeyError((role, name))

    def get_by_label(self, label):
        self.requested.append(("label", label))
        raise KeyError(label)

    def control_for(self, field_id):
        return self._controls[field_id]

    def locator(self, css):
        for field_id, control in self._controls.items():
            if css == base._combobox_control_selector(field_id):
                return control
        if css == base._REQUIRED_CSS:
            return _FakeLocatorSet(self._sweep_required)
        if css == base._ASTERISK_CSS:
            return _FakeLocatorSet([])
        return _FakeLocatorSet([])


def test_greenhouse_education_three_field_drive_via_live_region_dom_ids():
    # F-4: capture emits education_school/degree/discipline; the LIVE react-select
    # ids are school--0 / degree--0 / discipline--0 (probe 2026-07-19). The three
    # must drive through the async live-region driver anchored on the DOM ids
    # (School+Degree from the SSOT education entry, Discipline from the seeded
    # canned_answers.discipline), never the capture keys (which anchor nothing).
    fm = parse_greenhouse({"id": "4124053", "education": "education_optional"},
                          "canonical", "4124053", now=lambda: _PINNED)
    ssot = SSOT({
        "identity": {"name": "Test Candidate", "email": "t@e.invalid"},
        "education": [{"degree": "BSc Computer Science",
                       "institution": "Example University", "year": 2025}],
        "canned_answers": {"discipline": "Computer Science"},
    })
    values = greenhouse.resolve_values(fm, ssot, {})
    by_key = {fv.key: fv for fv in values.fields}
    assert by_key["education_school"].value == "Example University"
    assert by_key["education_degree"].value == "BSc Computer Science"
    # Discipline falls back to the seeded owner datum, not the education entry.
    assert by_key["education_discipline"].value == "Computer Science"

    page = _FakeReactSelectPage({
        "school--0": _FakeReactSelectControl(single_reads=("Example University",)),
        "degree--0": _FakeReactSelectControl(single_reads=("BSc Computer Science",)),
        "discipline--0": _FakeReactSelectControl(single_reads=("Computer Science",)),
    })
    report = greenhouse.fill(page, fm, values)

    # Each control was driven by the DOM id (its capture key resolves NOTHING on
    # this page), typed its value one keystroke at a time, and readback-confirmed.
    assert "".join(page.control_for("school--0").input.keys) == "Example University"
    assert "".join(page.control_for("degree--0").input.keys) == "BSc Computer Science"
    assert "".join(page.control_for("discipline--0").input.keys) == "Computer Science"
    assert report.filled == 3
    assert report.readback_mismatches == []
    assert report.complete is True


def test_greenhouse_nationality_react_select_multi_drives_not_checkbox():
    # F-3: question_65106872[] is a multi_value_multi_select whose LIVE control is
    # a react-select MULTI (select__value-container--is-multi), NOT native
    # checkboxes. It must route to the react-select multi driver (type-to-filter +
    # Enter per value, chip readback), and the checkbox path must NEVER be reached.
    raw = {"id": "4124053", "questions": [
        {"label": "Please indicate your nationality:", "required": True,
         "fields": [{"name": "question_65106872[]",
                     "type": "multi_value_multi_select",
                     "values": [{"value": 1, "label": "Italian"},
                                {"value": 2, "label": "French"}]}]}]}
    fm = parse_greenhouse(raw, "canonical", "4124053", now=lambda: _PINNED)
    fld = fm.fields[0]
    assert fld.locator.role == "checkbox"          # captured shape unchanged
    values = ResolvedValues(fields=[FieldValue(
        key=fld.key, label=fld.label, type=fld.type, locator=fld.locator,
        value=["Italian"])])

    control = _FakeReactSelectControl(multi=True)
    page = _FakeReactSelectPage({"question_65106872[]": control},
                                sweep_required_labels=(fld.label,))
    report = greenhouse.fill(page, fm, values)

    # Driven as a react-select multi: typed the value, committed as a chip.
    assert "".join(control.input.keys) == "Italian"
    assert control.input.pressed[:1] == ["Enter"]
    assert control.selected == ["Italian"]
    # The checkbox path was NEVER taken: no get_by_role("checkbox", ...) request.
    assert not any(call[0] == "role" for call in page.requested)
    assert report.filled == 1
    assert [g["key"] for g in report.required_unfilled] == []
    assert report.complete is True


def test_greenhouse_nationality_partial_multi_is_a_gap_not_a_fill():
    # The anti-gaming bound: a multi whose readback does NOT show every intended
    # value is a GAP, never a silent fill (mirrors the checkbox-group all-or-
    # nothing verdict). Here the widget commits nothing (empty chip set).
    raw = {"id": "4124053", "questions": [
        {"label": "Please indicate your nationality:", "required": True,
         "fields": [{"name": "question_65106872[]",
                     "type": "multi_value_multi_select",
                     "values": [{"value": 1, "label": "Italian"}]}]}]}
    fm = parse_greenhouse(raw, "canonical", "4124053", now=lambda: _PINNED)
    fld = fm.fields[0]
    values = ResolvedValues(fields=[FieldValue(
        key=fld.key, label=fld.label, type=fld.type, locator=fld.locator,
        value=["Italian"])])

    class _NoCommitControl(_FakeReactSelectControl):
        def locator(self, css):
            if css.endswith(".select__multi-value__label"):
                return _FakeChipSet([])          # nothing ever committed
            return super().locator(css)

    page = _FakeReactSelectPage({"question_65106872[]": _NoCommitControl(multi=True)},
                                sweep_required_labels=(fld.label,))
    report = greenhouse.fill(page, fm, values)

    assert report.filled == 0
    assert dict(report.skipped)[fld.key].startswith("react-select multi readback")
    assert [g["key"] for g in report.required_unfilled] == [fld.key]
    assert report.complete is False


def test_greenhouse_privacy_select_fills_affirmative_on_live_ssot_shape():
    # F-2 end to end on the LIVE SSOT shape (privacy_consent_default present,
    # optional_consents ABSENT): the privacy select resolves to its sole
    # affirmative "Acknowledge/Confirm" (RS-g) and drives through the react-select
    # combobox. Before the matcher carried privacy_consent_default this classified
    # MISSING and the overlay reported the live "no option match".
    raw = {"id": "4124053", "questions": [
        {"label": "Please confirm that you have read and agree to Canonical's "
                  "Recruitment Privacy Notice and Privacy Policy.",
         "required": True,
         "fields": [{"name": "question_37455721",
                     "type": "multi_value_single_select",
                     "values": [{"value": 1, "label": "Acknowledge/Confirm"}]}]}]}
    fm = parse_greenhouse(raw, "canonical", "4124053", now=lambda: _PINNED)
    ssot = SSOT({
        "identity": {"name": "Test Candidate", "email": "t@e.invalid"},
        "policies": {"consent": {"application_privacy": "consent"}},
        "canned_answers": {"privacy_consent_default": "always tick"},
    })
    values = greenhouse.resolve_values(fm, ssot, {})
    assert values.values["question_37455721"] == "Acknowledge/Confirm"

    page = _FakeGreenhousePage(
        combo_field_id="question_37455721",
        combo_reads=("Acknowledge/Confirm",),
        sweep_required_labels=(fm.fields[0].label,))
    report = greenhouse.fill(page, fm, values)
    assert "".join(page.combo_input.keys) == "Acknowledge/Confirm"
    assert report.filled == 1
    assert report.complete is True


_ATTESTATION_LABEL = (
    "During this application process I agree to use only my own words. I "
    "understand that plagiarism, the use of AI or other generated content will "
    "disqualify my application.")


def test_greenhouse_forbid_essays_census_reads_tos_forbidden_class_ii():
    # F-1 + F-10: on a forbid-essays posting the free-texts and the AI attestation
    # must reach the census with the CLASS-II documented-ToS verdict, NOT the
    # kernel's stale data-gap reasons. Pins both live mislabel classes
    # ("skills resolved to a mapping with no usable scalar" and
    # "missing:canned_answers.*") and the attestation, then proves the overlay
    # relabels all four and the census subtracts them as justified (not gaps).
    raw = {"id": "4124053", "questions": [
        {"label": "Describe your experience with low level/system programming on "
                  "Linux.", "required": True,
         "fields": [{"name": "question_32177226", "type": "textarea",
                     "values": []}]},
        {"label": "Describe your experience with databases and/or consensus "
                  "algorithms", "required": True,
         "fields": [{"name": "question_32177227", "type": "textarea",
                     "values": []}]},
        {"label": "Please share your rationale or evidence for the high school "
                  "performance selections above.", "required": True,
         "fields": [{"name": "question_41861035", "type": "textarea",
                     "values": []}]},
        {"label": _ATTESTATION_LABEL, "required": True,
         "fields": [{"name": "question_42878852",
                     "type": "multi_value_single_select",
                     "values": [{"value": 1, "label": "Yes"},
                                {"value": 2, "label": "No"}]}]},
    ]}
    fm = parse_greenhouse(raw, "canonical", "4124053", now=lambda: _PINNED)
    keys = ["question_32177226", "question_32177227", "question_41861035",
            "question_42878852"]
    ssot = SSOT({
        "identity": {"name": "Test Candidate", "email": "t@e.invalid"},
        # A MAPPING at `skills` reproduces the live "no usable scalar" mislabel on
        # the "experience with ..." essays.
        "skills": {"languages": ["Python", "Go"], "areas": ["ML"]},
        "policies": {"consent": {"application_privacy": "consent"}},
        "canned_answers": {"privacy_consent_default": "always tick"},
    })
    values = greenhouse.resolve_values(fm, ssot, {})

    # PRE-overlay: the exact two live mislabel classes, and the attestation is
    # NEVER auto-answered (the AI-policy guard fails it closed under the policy).
    before = dict(values.skipped)
    assert "resolved to a mapping with no usable scalar" in before["question_32177226"]
    assert "resolved to a mapping with no usable scalar" in before["question_32177227"]
    assert before["question_41861035"].startswith("missing:canned_answers")
    assert "question_42878852" not in values.values
    assert "question_42878852" in before

    generated = content.GeneratedAnswers(
        vendor="greenhouse", slug="canonical", job_id="4124053", posting_lang="en",
        tos_forbidden=[
            content.TosForbidden(label=q["label"],
                                 reason="employer forbids AI-generated content")
            for q in raw["questions"][:3]
        ] + [content.TosForbidden(
            label=_ATTESTATION_LABEL,
            reason="AI-policy attestation: human handoff")])
    overlay = content.apply_content_overlay(values, fm, ssot, generated=generated)
    assert set(overlay.tos_forbidden) == set(keys)

    # POST-overlay: every forbidden field now carries the class-ii verdict, and
    # the attestation's specific per-entry reason rides with it.
    after = dict(values.skipped)
    for key in keys:
        assert after[key].startswith(TOS_FORBIDDEN_SKIP_PREFIX), key
    assert "attestation" in after["question_42878852"].lower()

    page = _FakeGreenhousePage(sweep_required_labels=tuple(q["label"]
                                                           for q in raw["questions"]),
                               file_inputs=[])
    report = greenhouse.fill(page, fm, values)

    # THE CENSUS SUBTRACTS THEM AS DOCUMENTED ToS: none is a required gap, all four
    # are justified, and the reason a human reads is the class-ii verdict.
    assert [g["key"] for g in report.required_unfilled] == []
    assert report.justified_skips >= 4
    census = dict(report.skipped)
    for key in keys:
        assert census[key].startswith(TOS_FORBIDDEN_SKIP_PREFIX), key
    assert report.complete is True
