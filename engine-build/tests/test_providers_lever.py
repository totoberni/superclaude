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
import json
from pathlib import Path

import pytest

import importlib

# `engine.providers.lever.capture` is a submodule shadowed at package scope by the
# `capture` Provider callable, so reach the module object via importlib (the same
# sys.modules / import_module seam the package NAME NOTE documents).
lever_capture = importlib.import_module("engine.providers.lever.capture")
# same shadowing for `.fill` (the package exposes the `fill` Provider callable at
# package scope): the MODULE, carrying `_locate` / `_name_css`, comes from importlib.
lever_fill = importlib.import_module("engine.providers.lever.fill")
from engine.kernel.contracts import (
    Field, FieldMap, FillAssets, FillSafetyError, Locator)
from engine.kernel.resolve import MANUAL_ONLY
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
# A FAKE 555 test number (never a real one), written the way a human types it.
_FAKE_PHONE = "+1 555 0100"
_LINKEDIN_KEY = "urls[LinkedIn]"
_RESUME_KEY = "resume"
# The sweep gap key a required-but-unswept resume WOULD produce if the schema_only
# half of _sweep_gaps ignored whether the field actually landed (the live agicap
# phantom: `dom-sweep:resume / cv` on a form whose CV had uploaded).
_RESUME_SWEEP_GAP = "dom-sweep:resume / cv"


# -- fixture loaders -------------------------------------------------------


def _dom_html() -> str:
    """The apply-DOM fixture, corrected to the LIVE Lever markup (div labels, a
    U+2731 required marker, a `required`-less CSS-hidden resume input)."""
    return (_FIXTURES / "dom.html").read_text()


def _fieldmap() -> FieldMap:
    """The full fixture field map, parsed through the REAL offline DOM parser."""
    return lever_capture._parse_lever(_dom_html(), "fauxcorp", "9001",
                                      now=lambda: _PINNED)


def _fieldmap_without(*keys: str) -> FieldMap:
    """The fixture field map minus the named field(s) -- models a Lever form
    whose required set is all safely fillable (no required human-handoff)."""
    fm = _fieldmap()
    fm.fields = [f for f in fm.fields if f.key not in keys]
    return fm


def _fieldmap_phone_required() -> FieldMap:
    """The safely-fillable field map with the phone field marked REQUIRED, so a
    phone readback verdict decides COMPLETE / NOT_COMPLETE (the live agicap shape:
    the posting made the phone mandatory)."""
    fm = _fieldmap_without(_CONSENT_KEY)
    for fld in fm.fields:
        if fld.key == "phone":
            fld.required = True
    return fm


# The geocoded 'Current location' typeahead field, appended to a safely-fillable
# map. The CAPTURED field is only the visible `location` textbox (Lever's base
# `location` name, label "Current location", from capture._LEVER_BASE_LABELS); the
# hidden selectedLocation input and the dropdown are page-RUNTIME nodes the fill
# drives, modelled by the fake page harness (`_FakeLocationLeverPage`) rather than
# captured. Marked REQUIRED so a location readback verdict decides COMPLETE.
_LOCATION_KEY = "location"
_LOCATION_LABEL = "Current location"
# A FULL postal address (fake): the street part carries a house number so the
# city-derivation heuristic must skip it and land on the city token "Bologna".
_FAKE_ADDRESS = "Via Rizzoli 4, Bologna, Emilia-Romagna, 40125, Italy"
_FAKE_CITY = "Bologna"


def _fieldmap_location_required(*, phone_required: bool = False) -> FieldMap:
    """The safely-fillable field map plus a REQUIRED geocoded location typeahead
    (optionally with the phone marked required too, for the phone-hardening path)."""
    fm = _fieldmap_without(_CONSENT_KEY)
    if phone_required:
        for fld in fm.fields:
            if fld.key == "phone":
                fld.required = True
    fm.fields.append(Field(
        key=_LOCATION_KEY, label=_LOCATION_LABEL, type="input_text",
        required=True, options=[], source=LEVER_SOURCE,
        locator=Locator(role="textbox", name=_LOCATION_LABEL)))
    return fm


def _fake_ssot(*, phone: str | None = None, location: str | None = None) -> SSOT:
    # FAKE, invented placeholder data only -- no owner PII, matching the
    # existing real_ssot_v14.yaml fixture's convention.
    identity = {
        "name": "Test Candidate",
        "email": "test.candidate@example.invalid",
    }
    if phone:
        identity["phone"] = phone
    if location:
        # the geocoded 'Current location' typeahead resolves from
        # identity.current_location (kernel.resolve's location mapping); a FULL
        # postal address on purpose, so the city-derivation heuristic is exercised.
        identity["current_location"] = location
    return SSOT({
        "identity": identity,
        "links": {
            # the slug carries digits ON PURPOSE: it lets a NON-phone field be
            # given a same-digits reformatted readback, which is how the phone
            # tolerance's scope containment is pinned (see
            # test_lever_phone_wrong_digits_still_mismatch, probe (c)).
            "linkedin": "https://www.linkedin.com/in/test-candidate-0100",
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


def _resolved_values(fieldmap, *, tmp_path, assets_kwargs=None, ssot=None):
    ssot = ssot if ssot is not None else _fake_ssot()
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

    def count(self):
        return 1              # a resolved control IS exactly one element (lever.fill._locate)

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


class _FakeReformattingLocator(_FakeTextLocator):
    """A text widget that REFORMATS what it holds (the live agicap phone
    behaviour): the keystrokes land normally (recorded in `.value` by the parent's
    press_sequentially, so a test can prove type_human drove it), but the readback
    reports `readback` instead of the raw typed string.

    Used for BOTH sides of the phone contract: a same-digits regrouping (the fill
    must count), a genuinely different or superstring number (the fill must NOT
    count), and -- on a NON-phone control -- a same-digits reformat, which must
    still be a mismatch because the digit tolerance is phone-only."""

    def __init__(self, readback):
        super().__init__()
        self._readback = readback

    def input_value(self):
        return self._readback


class _FakeSelectLocator:
    """A server-rendered native <select>: driven by select_option (NOT the
    react-select click/filter/pick dance), readback via input_value()."""

    def count(self):
        return 1              # a resolved control IS exactly one element (lever.fill._locate)

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


class _FakeCheckLocator:
    """A checkbox/radio control: driven by `.check()`/`.uncheck()`, readback via
    `is_checked()` -- the same contract `control_toolkit._drive_toggle` /
    `fill_toolkit._readback` (bool branch) exercise on a real Playwright locator.
    W5.1c drives every checkbox/radio field through this path now, so a fake
    representing one must actually hold tickable state rather than raise."""

    def __init__(self):
        self._checked = False

    def count(self):
        return 1

    def check(self, *args, **kwargs):
        self._checked = True

    def uncheck(self, *args, **kwargs):
        self._checked = False

    def is_checked(self):
        return self._checked

    def get_attribute(self, name):
        return None

    def and_(self, other):
        # `lever.fill._locate_option` (W5.1-R2) intersects a role+name match with the
        # group's submission-name CSS via `.and_()`. This fake already resolves each
        # option to exactly the one control it was asked for (indexed by dict key,
        # which is already exact), so the intersection is always itself.
        return self

    def fill(self, *args, **kwargs):
        raise AssertionError("a checkbox/radio must be driven via check(), never fill()")

    def click(self, *args, **kwargs):
        raise AssertionError("a checkbox/radio must be driven via check(), never click()")

    def select_option(self, *args, **kwargs):
        raise AssertionError("a checkbox/radio must not be select_option'd")


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


class _DomNodeLocator:
    """A Playwright-locator shim over ONE node of a real parsed apply DOM: it
    answers exactly the three calls the kernel sweep makes (`get_attribute`,
    `is_visible`, `inner_text`). Backed by the project's own HTML tree
    (`capture._build_tree`), so `base.sweep_required` runs its REAL logic --
    both arms, the real `_accessible_name` fallback chain, the real ASCII-`*`
    filter -- over the REAL markup."""

    def __init__(self, node):
        self._node = node

    def get_attribute(self, name):
        return self._node.attrs.get(name)

    def is_visible(self):
        style = (self._node.attrs.get("style") or "").replace(" ", "").lower()
        return "display:none" not in style

    def inner_text(self):
        return lever_capture._node_text(self._node)


def _dom_sweep_arms(html):
    """The two node sets the kernel sweep's CSS selectors would match on `html`:
    arm 1 `[required], [aria-required='true']`, arm 2 `label, legend`. Only the CSS
    resolution is shimmed; every downstream decision stays the kernel's own."""
    tree = lever_capture._build_tree(html)
    required = lever_capture._find_all(tree, lambda n: (
        "required" in n.attrs
        or (n.attrs.get("aria-required") or "").strip().lower() == "true"))
    labels = lever_capture._find_all(tree, lambda n: n.tag in ("label", "legend"))
    return ([_DomNodeLocator(n) for n in required],
            [_DomNodeLocator(n) for n in labels])


class _FakeLocatorSet:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items

    def count(self):
        return len(self._items)


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
                 sweep_dom=None,
                 file_inputs=None, bad_keys=()):
        self._url = url
        self.controls = {}
        # The SAME control object is reachable two ways, because the production fill
        # reaches for it two ways (W5B-LEVER round 8): by its SUBMISSION NAME
        # (`lever.fill._locate` -> `page.locator(_name_css(key))`, the path every live
        # field takes) and, as the kernel fallback, by role + accessible name
        # (`base._locate` -> `get_by_role`). Indexing one object under both keeps every
        # existing assertion on `page.controls[(role, name)]` reading the control the
        # fill actually drove.
        self.by_css = {}
        self._css_by_role_name = {}
        for fld in fieldmap.fields:
            role = fld.locator.role
            key = (role, fld.locator.name)
            if role == "textbox":
                self.controls[key] = (_FakeBadTextLocator()
                                      if fld.key in bad_keys
                                      else _FakeTextLocator())
            elif role == "combobox":
                self.controls[key] = _FakeSelectLocator()
            elif role in ("checkbox", "radio"):
                if fld.options:
                    # a radio/checkbox GROUP: N physical controls sharing one
                    # submission name, each located by its OWN option wording
                    # (lever.fill._locate_option), never by the group's own name.
                    for option in fld.options:
                        self.controls[(role, option)] = _FakeCheckLocator()
                    continue
                self.controls[key] = _FakeCheckLocator()
            else:
                continue
            css = lever_fill._name_css(fld.key)
            self.by_css[css] = self.controls[key]
            self._css_by_role_name[key] = css
        self.file_inputs = (list(file_inputs) if file_inputs is not None else
                            [_FakeFileInput(id="resume", name="resume",
                                           accept=".pdf,.doc,.docx,.txt,.rtf")])
        self.routed = []
        self.context = _PageBoundContext(self)
        self.requested = []
        self.located_css = []
        # TWO sweep modes, and only one of them is the real page:
        #
        # `sweep_dom=<html>` (the HONEST one, used by every test whose verdict turns
        # on the sweep): the two arms are resolved against the REAL parsed apply DOM,
        # so `base.sweep_required` runs its own logic over the real markup. On real
        # Lever markup that means arm 2 (`label, legend` filtered on an ASCII "*")
        # emits NOTHING -- the label is a DIV and the marker is a U+2731 -- and a
        # required control is named ONLY by its `name` attribute, with the resume
        # input named by neither arm. Rounds 1 and 2 both shipped a hand-built sweep
        # list that was friendlier than the page in exactly the dimension that
        # decides the verdict; a fake sweep cannot be trusted for those tests.
        #
        # `sweep_required_labels` (the legacy shape): a hand-built arm-1 list of
        # aria-labelled controls. Kept ONLY for the tests that are not about the
        # sweep's key spaces (they assert typing, hand-off, upload, navigation).
        if sweep_dom is not None:
            self._sweep_required, self._sweep_asterisk = _dom_sweep_arms(sweep_dom)
        else:
            self._sweep_required = [
                _FakeSweepLocator(attrs={"aria-label": label})
                for label in sweep_required_labels]
            self._sweep_asterisk = []

    @property
    def url(self):
        return self._url

    def route(self, pattern, handler):
        self.routed.append((pattern, handler))

    def swap_control(self, role_name, control):
        """Install `control` as the one the fill will drive for this (role, name) field,
        in BOTH indices at once -- so a test that swaps in a widget (a reformatting phone
        input, say) swaps the object the fill ACTUALLY reaches, whichever locator it built
        (submission-name CSS, or the kernel role+name fallback)."""
        self.controls[role_name] = control
        css = self._css_by_role_name.get(role_name)
        if css:
            self.by_css[css] = control
        return control

    def get_by_role(self, role, name=None, exact=None):
        # `exact` is accepted (and ignored) because `lever.fill._locate_option`
        # passes it for a radio/checkbox group option lookup; this fake matches
        # by dict key, which is already exact.
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
        self.requested.append(("css", css))
        from engine.providers import base
        if css == base._REQUIRED_CSS:
            return _FakeLocatorSet(self._sweep_required)
        if css == base._ASTERISK_CSS:
            return _FakeLocatorSet(self._sweep_asterisk)
        control = self.by_css.get(css)
        if control is not None:
            return control            # the submission-name locator: exactly this control
        return _FakeLocatorSet([])    # resolves to NOTHING (count 0), as a live phantom would


# -- geocoded location typeahead harness (RS-f; live probe 2026-07-19) ----------
# Modelled from the PROBE's real cross-node DOM shape, not a friendlier invented
# one: a visible `location` input, a HIDDEN `selectedLocation` input (the value
# Lever submits), and an in-field dropdown that shows geocode results OR a
# 'No location found' no-results node. Typing text alone leaves selectedLocation
# empty; a value lands only by committing a dropdown option via ArrowDown+Enter.


class _FakeLocationWidget:
    """Lever's geocoded location typeahead across its DOM nodes. Typing shows the
    configured `geocode` option names (or the no-results node); ArrowDown
    highlights; Enter commits the highlighted option -- rewriting the visible input
    to the option name AND setting selectedLocation to the JSON blob
    {"name":..,"id":..} the live widget writes. `selected_name_override` makes the
    committed selectedLocation name DIVERGE from the option text (the anti-gaming
    probe): the fill's readback gate keys on selectedLocation, never on the visible
    input or the option text.

    R5 timeline model (live probe palantir 2026-07-19): the results container AND
    the no-results node EXIST empty in the DOM from initial page load; the option
    nodes (div.dropdown-location) appear only after the remote geocode returns
    (~1.45s live). `render_after_polls` models that latency -- the option nodes stay
    ABSENT and the no-results node stays HIDDEN until that many option-poll calls
    have happened (default 0 = resolves immediately). The no-results node is served
    ALWAYS (count>0 from load), VISIBLE only once the search resolves with nothing,
    so a count-based terminal check mis-parks on it and only a visibility-based check
    survives -- the R5 container trap.

    Final-round pollution model (live gopuff): `prefill` starts the visible input
    NON-EMPTY (Lever pre-fills the geocoded input from the candidate session with a
    full address). The geocoder query is the input's whole CONTENT (prefill + typed,
    because type_human APPENDS), and it renders options ONLY for a CLEAN city query
    (`_query_clean`: no comma and no digit) -- a full postal address, or an address
    with a city appended, carries a comma/digit and geocodes to nothing, exactly as
    the engine hit while a probe typing the city into a CLEAN field got 3 options.
    `clear()` (driven by the fill's keyboard select-all + delete) empties the content
    so the typeahead's city query is clean again.

    P1-1 TRANSIENT-CLOSE model (live full-form trace, gopuff 2026-07-20). The menu
    this widget renders is TRANSIENT and the live failure is a RACE, so the fixture
    models the exact dimension every earlier location fixture omitted: the dropdown
    is POPULATED at one observation and EMPTY at the next. `focus_thief_after_polls`
    fires a focus thief (live: an embedded third-party widget iframe took focus)
    once that many option queries have happened -- the traced sequence is `count 3,
    texts [Bologna, ITA, ...]` at t=209928 followed by `count 0, texts []` 22 ms
    later at t=209950, then a keypress dispatched with an IFRAME holding focus.
    The thief does two things at once, because the live one did:
      * it CLOSES the menu (the input blurred), and
      * it takes FOCUS, so page-keyboard keystrokes no longer reach this widget
        (`_FakeLocationKeyboard` records them as dispatched-but-stolen).
    Focus alone does not bring the menu back: `focus()` restores keyboard delivery
    but leaves the menu closed, because a real blur clears the widget's results
    state -- only a fresh query (clear + type, which is how the driver recovers)
    re-establishes it. That is deliberately the LESS friendly model of the two.
    `thief_releases=False` makes the thief re-take focus after every option query
    (it never lets go), so no attempt can ever commit -- the fixture for proving the
    driver parks honestly instead of dispatching keys into the void."""

    def __init__(self, *, geocode=(), no_results=False,
                 selected_name_override=None, render_after_polls=0, prefill="",
                 focus_thief_after_polls=None, thief_releases=True):
        self.geocode = list(geocode)
        self._no_results = no_results
        self._selected_name_override = selected_name_override
        self._render_after_polls = render_after_polls
        self.content = prefill        # the visible input's whole content (prefill + typed)
        self.typed = ""               # only what type_human typed (for assertions)
        self.visible_value = prefill
        self.selected_json = ""       # the hidden selectedLocation value
        self.option_polls = 0         # how many times option_nodes was queried
        self.focused = True           # does the page keyboard reach THIS widget?
        self.searches = 0             # how many city queries were typed (attempts)
        self._thief_after_polls = focus_thief_after_polls
        self._thief_releases = thief_releases
        self._thief_fired = False
        self._highlight = -1
        self._closed = False

    def type_char(self, ch):
        self.content += ch            # type_human APPENDS to whatever is already there
        self.typed += ch
        self.visible_value = self.content
        # typing is a real keystroke on the control: it focuses the input and starts
        # a new search, so a menu a blur had closed is re-established here.
        self.focused = True
        self._closed = False

    def clear(self):
        # the fill's keyboard clear (select-all + delete) empties the input. One
        # clear precedes each city query, so this counts the driver's ATTEMPTS.
        self.searches += 1
        self.content = ""
        self.typed = ""
        self.visible_value = ""
        self.focused = True
        self._closed = False

    def focus(self):
        # an explicit .focus() puts the keyboard back on the input -- and does NOT
        # reopen the menu: the blur that closed it cleared the results state.
        self.focused = True

    def _open(self):
        # the dropdown is shown only once something is typed, and closes on
        # commit/Escape -- so nothing is read before the query or after the commit.
        return bool(self.content) and not self._closed

    def _resolved(self):
        # the remote geocode has returned: options/no-results become live only after
        # `render_after_polls` option queries (the R5 render latency).
        return self.option_polls >= self._render_after_polls

    def _query_clean(self):
        # the geocoder returns options only for a CLEAN city query; a full address
        # (or an address with a city appended onto a pre-filled field) carries a comma
        # and/or a digit and geocodes to nothing -- the live pollution symptom.
        q = self.content.strip()
        return bool(q) and "," not in q and not any(ch.isdigit() for ch in q)

    def option_nodes(self):
        # the container div.dropdown-results exists empty from load; the option nodes
        # (div.dropdown-location) appear only once the remote search resolves AND the
        # query is clean (a polluted query geocodes to nothing).
        self.option_polls += 1
        if (not self._open() or self._no_results or not self._resolved()
                or not self._query_clean()):
            self._maybe_steal_focus()
            return []
        nodes = [_FakeSweepLocator(text=name) for name in self.geocode]
        # the thief fires AFTER this read is served: the caller sees the populated
        # menu, and the very next read finds it gone (the traced 22 ms window).
        self._maybe_steal_focus()
        return nodes

    def _maybe_steal_focus(self):
        if self._thief_after_polls is None:
            return
        if self.option_polls < self._thief_after_polls:
            return
        if self._thief_fired and self._thief_releases:
            return                    # a one-shot thief: it took focus once, at load
        self._thief_fired = True
        self.focused = False
        self._closed = True

    def no_results_nodes(self):
        # the no-results node EXISTS in the DOM from load (count>0, so a count-based
        # terminal check mis-fires on it), VISIBLE only once the search resolves with
        # nothing -- the R5 trap, so the node is always served and toggles visibility.
        visible = self._open() and self._resolved() and self._no_results
        return [_FakeSweepLocator(
            text="No location found. Try entering a different location",
            visible=visible)]

    def arrow_down(self):
        if not self._open() or self._no_results or not self.geocode:
            return
        self._highlight = min(self._highlight + 1, len(self.geocode) - 1)

    def enter(self):
        if 0 <= self._highlight < len(self.geocode):
            name = self.geocode[self._highlight]
            self.visible_value = name
            committed = (self._selected_name_override
                         if self._selected_name_override is not None else name)
            self.selected_json = json.dumps(
                {"name": committed, "id": "geo-hash-abc"})
            self._closed = True

    def escape(self):
        self._closed = True


class _FakeLocationVisibleInput(_FakeTextLocator):
    """The visible `location` input: keystrokes feed the widget's search, readback
    reads the widget's visible value (rewritten to the committed option name). The
    fill's keyboard clear (`_clear_text_field`: select-all then delete) empties the
    widget content, so a pre-filled input is cleaned before the city query is typed.

    A locator-level keystroke acts on THIS element, so it focuses it first (real
    Playwright does the same); `focus()` serves the driver's explicit pre-commit
    focus re-establishment. Neither reopens a menu a blur has closed -- only a fresh
    query does (see `_FakeLocationWidget`)."""

    def __init__(self, widget):
        super().__init__()
        self._widget = widget

    def press(self, key):
        self._widget.focused = True
        if key in ("Delete", "Backspace"):
            self._widget.clear()

    def press_sequentially(self, ch, delay=None):
        self._widget.type_char(ch)

    def focus(self):
        self._widget.focus()

    def input_value(self):
        return self._widget.visible_value


class _FakeHiddenSelectedLocation:
    """The hidden `selectedLocation` input: serves the value Lever submits (empty
    until an option is committed)."""

    def __init__(self, widget):
        self._widget = widget

    def count(self):
        return 1

    def input_value(self):
        return self._widget.selected_json


class _FakeLocationKeyboard:
    """The page keyboard: ArrowDown/Enter/Escape drive the widget, exactly as the
    fill's focus-following `_press_location_key` presses them on the live page.

    FOCUS-FOLLOWING, so a key only reaches the widget while the widget holds focus.
    `presses` records what was DISPATCHED (the live trace's PRE_PRESS record) and
    `stolen` the subset that went to whatever else held focus instead -- the live
    run dispatched exactly one key, Escape, with an IFRAME as activeElement."""

    def __init__(self, widget):
        self._widget = widget
        self.presses = []
        self.stolen = []

    def press(self, key):
        self.presses.append(key)
        if not self._widget.focused:
            self.stolen.append(key)   # dispatched, but focus was elsewhere
            return
        if key == "ArrowDown":
            self._widget.arrow_down()
        elif key == "Enter":
            self._widget.enter()
        elif key == "Escape":
            self._widget.escape()


class _FakeLocationLeverPage(_FakeLeverPage):
    """The base fake plus Lever's geocoded location typeahead. Serves the three
    location-specific selectors the fill drives (referenced THROUGH the fill module,
    `lever_fill._SELECTED_LOCATION_CSS` / `_LOCATION_NO_RESULTS_CSS` /
    `_LOCATION_OPTION_CSS`, so the fake tracks the fill's own selectors and a stale
    guess is self-catching), swaps the visible `location` control for one wired to
    the widget, and exposes a page keyboard."""

    def __init__(self, fieldmap, *, location_widget, **kwargs):
        super().__init__(fieldmap, **kwargs)
        self._loc = location_widget
        self.keyboard = _FakeLocationKeyboard(location_widget)
        self.swap_control(("textbox", _LOCATION_LABEL),
                          _FakeLocationVisibleInput(location_widget))

    def locator(self, css):
        if css == lever_fill._SELECTED_LOCATION_CSS:
            self.located_css.append(css)
            return _FakeHiddenSelectedLocation(self._loc)
        if css == lever_fill._LOCATION_NO_RESULTS_CSS:
            self.located_css.append(css)
            return _FakeLocatorSet(self._loc.no_results_nodes())
        if css == lever_fill._LOCATION_OPTION_CSS:
            self.located_css.append(css)
            return _FakeLocatorSet(self._loc.option_nodes())
        return super().locator(css)


class _FakeBannerButton:
    """One button in the fixed privacy banner. Visible while the banner is up;
    clicking it dismisses the banner (and records the click). Serves its label via
    aria-label so `_accessible_name` reads it."""

    def __init__(self, page, name):
        self._page = page
        self._name = name

    def is_visible(self):
        return self._page.banner_present

    def get_attribute(self, attr):
        return self._name if attr == "aria-label" else None

    def click(self):
        self._page.banner_clicks.append(self._name)
        self._page.banner_present = False


class _FakeBannerKeyboard(_FakeLocationKeyboard):
    """The page keyboard behind a banner overlay: while the banner is UP it INTERCEPTS
    every keystroke (the fixed aria-region eats focus), so the geocode commit's
    ArrowDown/Enter never reach the widget. Once the banner is dismissed the keys pass
    through to the widget."""

    def __init__(self, widget, page):
        super().__init__(widget)
        self._page = page

    def press(self, key):
        if self._page.banner_present:
            self.presses.append(("swallowed", key))   # the banner ate it
            return
        super().press(key)


class _FakeBannerLocationPage(_FakeLocationLeverPage):
    """A location page with a fixed privacy banner up at load. The banner's buttons
    match `lever_fill._BANNER_BUTTON_CSS`; its keyboard swallows keystrokes until the
    banner is dismissed -- so the location commit succeeds ONLY if the fill dismisses
    the banner BEFORE the location drive."""

    def __init__(self, fieldmap, *, location_widget, banner_buttons, **kwargs):
        self.banner_present = True
        self.banner_clicks: list = []
        super().__init__(fieldmap, location_widget=location_widget, **kwargs)
        self.keyboard = _FakeBannerKeyboard(location_widget, self)
        self._banner_buttons = [_FakeBannerButton(self, name)
                                for name in banner_buttons]

    def locator(self, css):
        if css == lever_fill._BANNER_BUTTON_CSS:
            self.located_css.append(css)
            return _FakeLocatorSet(
                self._banner_buttons if self.banner_present else [])
        return super().locator(css)


class _FakeClearingTextLocator(_FakeTextLocator):
    """A plain text/url/phone input that PASSES its immediate readback, then a later
    dropdown interaction CLEARS it (the end-of-fill re-verify reads empty), so the
    simple-field hardening must re-drive it ONCE and the re-read must then pass.
    Models the first-pass live symptom (the RS-f phone AND the F-7 urls[LinkedIn]):
    the value landed, a subsequent typeahead interaction cleared it, and only an
    end-of-fill re-verify catches and repairs it."""

    def __init__(self):
        super().__init__()
        self._reads = 0
        self.retyped = False

    def press_sequentially(self, ch, delay=None):
        if self._reads >= 2:            # a re-drive AFTER the clear repopulates it
            self.retyped = True
        self.value += ch

    def input_value(self):
        self._reads += 1
        if self._reads == 2:            # the end-of-fill re-verify: a later interaction cleared it
            self.value = ""
        return self.value


class _FakeResidualClearTextLocator(_FakeTextLocator):
    """A text input the later dropdown interaction leaves in a NON-EMPTY RESIDUAL
    state (a partial value), not a clean empty. The re-verify re-read mismatches, and
    a BLIND re-type (type_human APPENDS) would double the residual and the value and
    STILL mismatch. Only clearing the field first (keyboard select-all then delete)
    lets the re-type REPLACE the residual and repair it. Models the R4 org regression
    ("None, final-year MEng student" doubled by the blind re-drive). `press` handles
    the select-all/delete clear; `press_sequentially` appends as a real keystroke
    would."""

    def __init__(self, residual):
        super().__init__()
        self._reads = 0
        self._residual = residual
        self.retyped = False

    def press(self, key):
        # the keyboard clear: Delete/Backspace after a select-all empties the field.
        if key in ("Delete", "Backspace"):
            self.value = ""

    def press_sequentially(self, ch, delay=None):
        if self._reads >= 2:            # typing after the residual surfaced = the re-drive
            self.retyped = True
        self.value += ch

    def input_value(self):
        self._reads += 1
        if self._reads == 2:            # the re-verify read: a residual, not empty
            self.value = self._residual
        return self.value


class _FakePrefilledTextLocator(_FakeTextLocator):
    """A plain text/url input the PAGE pre-fills from the candidate session/profile
    (live gopuff: phone pre-filled). `type_human` APPENDS, so the FIRST-PASS drive
    must CLEAR it first or the value doubles (pre-fill + intended) and the readback
    mismatches. `press` handles the keyboard clear; without it a blind type appends
    to the pre-fill."""

    def __init__(self, prefill):
        super().__init__()
        self.value = prefill

    def press(self, key):
        if key in ("Delete", "Backspace"):
            self.value = ""


class _FakeMaskedPhoneLocator(_FakeTextLocator):
    """A tel/masked phone input running an as-you-type reformat handler. A typed
    SPACE fights the mask: the handler drops it AND the caret jump duplicates the
    preceding digit (the live gopuff "+3935100000007 0000" corruption for an intended
    "+39 351 000 0000"). A DIGITS-ONLY drive formats cleanly. So typing the SPACED
    value corrupts the digit sequence and the readback mismatches, while a digits-only
    drive lands exactly the intended digits."""

    def press(self, key):
        if key in ("Delete", "Backspace"):
            self.value = ""

    def press_sequentially(self, ch, delay=None):
        if ch == " ":
            if self.value and self.value[-1].isdigit():
                self.value += self.value[-1]   # the caret jump duplicates the last digit
            return
        self.value += ch


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


def test_fill_required_checkbox_is_driven_and_confirmed_complete(tmp_path):
    # RETARGETED (W5.1c owner ruling 2026-07-13): a native `.check()` goes through
    # CDP with isTrusted=true, the same risk tier as the `type_human` keystrokes
    # this module already sends, so a required checkbox is no longer deferred to a
    # human -- `fill()` drives it through `control_toolkit.drive_control` and only
    # counts it filled once the readback (`is_checked()`) confirms.
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

    # ...and fill() DID drive it: the control was requested and ticked, and the
    # readback confirms, so it is no longer a required gap.
    consent_control = page.controls[("checkbox", consent.locator.name)]
    assert consent_control.is_checked() is True
    assert report.readback_mismatches == []
    gap = {g["key"]: g for g in report.required_unfilled}
    assert _CONSENT_KEY not in gap
    assert report.complete is True
    assert report.caption().endswith("COMPLETE")
    assert not report.caption().endswith("NOT COMPLETE")


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


# -- W5B-LEVER F1: phone-tolerant readback (plugin-side; kernel untouched) ------


def test_lever_phone_reformat_readback_counts_filled(tmp_path):
    # LIVE FAILURE (agicap 2026-07-12): the phone number LANDED, but the widget
    # reformatted it (regrouped, and re-prefixed 00.. for +..), so the kernel
    # readback -- a strip+lowercase string compare -- called it a mismatch and the
    # required phone became a false gap. The digits are identical, so the
    # plugin-side re-judge counts the field filled and the form is COMPLETE.
    fieldmap = _fieldmap_phone_required()
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(phone=_FAKE_PHONE))
    page = _FakeLeverPage(
        fieldmap, sweep_required_labels=_SAFE_REQUIRED_LABELS + ("Phone",))
    phone = page.swap_control(("textbox", "Phone"),
                              _FakeReformattingLocator("001 (555) 0100"))  # regrouped

    report = lever.fill(page, fieldmap, values)

    # the number was genuinely TYPED (human cadence), not fill()ed -- DIGITS-ONLY
    # (leading + kept) so the input's as-you-type mask cannot fight our spaces...
    assert phone.value == lever_fill._phone_type_form(_FAKE_PHONE)
    # ...and the reformatted readback no longer reads as a dropped value.
    assert report.readback_mismatches == []
    assert not any(key == "phone" for key, _ in report.skipped)
    assert report.required_unfilled == []
    assert report.complete is True


def _phone_report(tmp_path, phone_readback, *, linkedin_readback=None):
    """One fill() over the phone-required field map, with the phone control (and
    optionally the NON-phone LinkedIn control) driven by a reformatting widget."""
    fieldmap = _fieldmap_phone_required()
    sweep_labels = _SAFE_REQUIRED_LABELS + ("Phone",)
    if linkedin_readback is not None:
        for fld in fieldmap.fields:
            if fld.key == _LINKEDIN_KEY:
                fld.required = True
        sweep_labels += ("LinkedIn URL",)
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(phone=_FAKE_PHONE))
    page = _FakeLeverPage(fieldmap, sweep_required_labels=sweep_labels)
    page.swap_control(("textbox", "Phone"), _FakeReformattingLocator(phone_readback))
    if linkedin_readback is not None:
        page.swap_control(("textbox", "LinkedIn URL"),
                          _FakeReformattingLocator(linkedin_readback))
    return lever.fill(page, fieldmap, values)


def test_lever_phone_wrong_digits_still_mismatch(tmp_path):
    # ANTI-GAMING NEGATIVE. The tolerance is on FORMATTING only, and it is
    # PHONE-ONLY. Three probes, each pinning one way the re-judge could be widened
    # into laundering a value that never landed:
    #
    #  (a) DIFFERENT digits -> still a mismatch (kills "phone fields always pass").
    #  (b) SUPERSTRING digits -> still a mismatch. The compare is exact equality,
    #      not containment: a readback carrying the typed digits PLUS more (an
    #      appended extension) is a different number, so relaxing the compare to
    #      `intended in actual_digits` must not survive.
    #  (c) SCOPE: a NON-phone required field (the LinkedIn URL, whose value carries
    #      digits) whose readback is a same-digits reformat is STILL a mismatch.
    #      The spec's "do NOT loosen readback for non-phone fields" lives here:
    #      widening `_is_phone_field` (e.g. to `return True`) makes this probe fail.
    #      The phone itself lands normally in this probe, so only the scope changed.
    for readback in ("+1 (555) 0199",          # (a) genuinely different number
                     "+1 555 0100 ext 22"):    # (b) typed digits plus an extension
        report = _phone_report(tmp_path, readback)
        assert any(m["key"] == "phone" for m in report.readback_mismatches)
        assert ("phone", "value did not take (readback mismatch)") in report.skipped
        assert any(g["key"] == "phone" for g in report.required_unfilled)
        assert report.complete is False

    # (c) non-phone containment
    report = _phone_report(
        tmp_path, "001 (555) 0100",     # phone: same digits -> counts as filled
        linkedin_readback="https://linkedin.com/in/test-candidate-0100")
    assert not any(m["key"] == "phone" for m in report.readback_mismatches)
    assert any(m["key"] == _LINKEDIN_KEY for m in report.readback_mismatches)
    assert any(g["key"] == _LINKEDIN_KEY for g in report.required_unfilled)
    assert report.complete is False


# -- RS-f: geocoded location typeahead + phone hardening ------------------------
# Live probe (palantir, 2026-07-19): the base `location` field is a GEOCODED
# typeahead. Typing the full address alone leaves the hidden `selectedLocation`
# (the value Lever submits) EMPTY while the visible input reads back the typed
# string -- a FALSE fill. A value lands only by typing a CITY query, awaiting the
# remote dropdown, and committing a matching option via ArrowDown+Enter, which sets
# selectedLocation to {"name":..,"id":..}. The fill counts ONLY when selectedLocation
# is non-empty and its name contains the city token. Live proof is a TB5-R2
# toto-gate concern; this suite pins the LOGIC offline against the probe's real DOM
# shape (visible input + hidden selectedLocation + dropdown results/no-results).


def test_lever_location_city_query_committed_sets_selected_location_and_counts(tmp_path):
    # PROBE (a): a city-level query is derived from a FULL postal address, the
    # dropdown returns matching options, and committing the first city-matching one
    # sets the hidden selectedLocation -- so the required location counts filled and
    # the form is COMPLETE. The visible input alone is NEVER the gate.
    fieldmap = _fieldmap_location_required()
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(location=_FAKE_ADDRESS))
    widget = _FakeLocationWidget(geocode=["Bologna, ITA", "Bologna, Asti, ITA"])
    page = _FakeLocationLeverPage(
        fieldmap, location_widget=widget,
        sweep_required_labels=_SAFE_REQUIRED_LABELS + (_LOCATION_LABEL,))

    report = lever.fill(page, fieldmap, values)

    # the CITY was derived from the address ("Via Rizzoli 4" carries a digit and is
    # skipped) and TYPED via human cadence, then an option was committed via the
    # keyboard (ArrowDown then Enter), never a click.
    assert widget.typed == _FAKE_CITY
    assert page.keyboard.presses[:2] == ["ArrowDown", "Enter"]
    # the hidden selectedLocation -- the value Lever submits -- is set and names the
    # city; the visible input was rewritten to the committed option name.
    assert json.loads(widget.selected_json)["name"] == "Bologna, ITA"
    assert widget.visible_value == "Bologna, ITA"
    assert not any(m["key"] == _LOCATION_KEY for m in report.readback_mismatches)
    assert not any(k == _LOCATION_KEY for k, _ in report.skipped)
    assert not any(g["key"] == _LOCATION_KEY for g in report.required_unfilled)
    assert report.complete is True


def test_lever_location_no_dropdown_match_parks_honestly_not_counted(tmp_path):
    # PROBE (b): the geocoder returns NO match (the 'No location found' node), so no
    # option is committed, selectedLocation stays EMPTY, and the REQUIRED location is
    # an honest gap -- never a silent pass, never a non-matching option committed.
    fieldmap = _fieldmap_location_required()
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(location=_FAKE_ADDRESS))
    widget = _FakeLocationWidget(no_results=True)
    page = _FakeLocationLeverPage(
        fieldmap, location_widget=widget,
        sweep_required_labels=_SAFE_REQUIRED_LABELS + (_LOCATION_LABEL,))

    report = lever.fill(page, fieldmap, values)

    # nothing was committed: selectedLocation is empty and no option-commit Enter
    # was pressed (only the defensive Escape that closes the empty dropdown).
    assert widget.selected_json == ""
    assert "Enter" not in page.keyboard.presses
    # so the required location is an honest gap and the form is NOT COMPLETE.
    assert any(g["key"] == _LOCATION_KEY for g in report.required_unfilled)
    assert any(m["key"] == _LOCATION_KEY for m in report.readback_mismatches)
    assert report.complete is False
    assert report.caption().endswith("NOT COMPLETE")


def test_lever_location_committed_option_lacking_city_is_a_mismatch(tmp_path):
    # PROBE (c), ANTI-GAMING: an option whose TEXT matches the city is committed,
    # but the SUBMITTED selectedLocation names a DIFFERENT place (a wrong/ambiguous
    # geocode). The gate keys on selectedLocation -- the value Lever submits -- not
    # on the visible input or the option text, so this is rejected as a mismatch.
    fieldmap = _fieldmap_location_required()
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(location=_FAKE_ADDRESS))
    widget = _FakeLocationWidget(geocode=["Bologna, ITA"],
                                 selected_name_override="Genoa, ITA")
    page = _FakeLocationLeverPage(
        fieldmap, location_widget=widget,
        sweep_required_labels=_SAFE_REQUIRED_LABELS + (_LOCATION_LABEL,))

    report = lever.fill(page, fieldmap, values)

    # an option WAS committed (selectedLocation is non-empty and the visible input
    # was rewritten), but its submitted name lacks the city token...
    assert json.loads(widget.selected_json)["name"] == "Genoa, ITA"
    assert widget.visible_value == "Bologna, ITA"
    # ...so it is a readback mismatch (carrying BOTH readbacks) and an honest gap,
    # never counted filled.
    mismatch = next(m for m in report.readback_mismatches
                    if m["key"] == _LOCATION_KEY)
    assert mismatch["actual"]["visible"] == "Bologna, ITA"
    assert "Genoa" in mismatch["actual"]["selectedLocation"]
    assert any(g["key"] == _LOCATION_KEY for g in report.required_unfilled)
    assert report.complete is False


def test_lever_phone_reverified_and_redriven_after_location_interaction(tmp_path):
    # PROBE (d), PHONE HARDENING: the phone lands and readback-passes, then the
    # location typeahead interaction CLEARS it (a blur/focus race). The end-of-fill
    # re-verify re-reads the phone, finds it cleared, re-drives it ONCE and re-reads
    # -- so the phone is repaired and the form is COMPLETE. The re-drive fires only
    # because a later interaction cleared a value the primitive had typed correctly.
    fieldmap = _fieldmap_location_required(phone_required=True)
    values = _resolved_values(
        fieldmap, tmp_path=tmp_path,
        ssot=_fake_ssot(phone=_FAKE_PHONE, location=_FAKE_ADDRESS))
    widget = _FakeLocationWidget(geocode=["Bologna, ITA"])
    page = _FakeLocationLeverPage(
        fieldmap, location_widget=widget,
        sweep_required_labels=_SAFE_REQUIRED_LABELS
        + (_LOCATION_LABEL, "Phone"))
    phone = page.swap_control(("textbox", "Phone"), _FakeClearingTextLocator())

    report = lever.fill(page, fieldmap, values)

    # the phone was re-driven ONCE (the clear was detected on the re-verify read)...
    assert phone.retyped is True
    assert phone.value == lever_fill._phone_type_form(_FAKE_PHONE)  # digits-only re-drive
    # ...and the location committed too, so no field is a gap and the form COMPLETEs.
    assert not any(m["key"] == "phone" for m in report.readback_mismatches)
    assert not any(g["key"] == "phone" for g in report.required_unfilled)
    assert not any(g["key"] == _LOCATION_KEY for g in report.required_unfilled)
    assert report.complete is True


# -- F-5: the geocoded dropdown option selector is the LIVE node ----------------


def test_lever_location_option_selector_is_the_live_dropdown_location_node(tmp_path):
    # F-5 (live probe, palantir 2026-07-19): typing "Bologna" populates
    # <div class="dropdown-results ..."> with option nodes
    # <div class="break-word dropdown-location ..." id="location-N">Bologna, ITA</div>
    # (ids location-0/1/2...). The R3 driver's option selector guessed
    # `div.dropdown-result` (singular) and matched NOTHING against that markup ("no
    # dropdown option matched the city 'Bologna'"), so a city that WAS in the
    # dropdown never committed. The selector is pinned to the live node, and the
    # driver actually queries it while committing.
    fieldmap = _fieldmap_location_required()
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(location=_FAKE_ADDRESS))
    widget = _FakeLocationWidget(geocode=["Bologna, ITA"])
    page = _FakeLocationLeverPage(
        fieldmap, location_widget=widget,
        sweep_required_labels=_SAFE_REQUIRED_LABELS + (_LOCATION_LABEL,))

    report = lever.fill(page, fieldmap, values)

    assert (lever_fill._LOCATION_OPTION_CSS
            == "div.dropdown-results div.dropdown-location")
    assert lever_fill._LOCATION_OPTION_CSS in page.located_css
    assert json.loads(widget.selected_json)["name"] == "Bologna, ITA"
    assert report.complete is True


def test_lever_location_settle_awaits_option_nodes_over_the_empty_container(tmp_path):
    # R5 RESIDUAL (live probe palantir 2026-07-19): div.dropdown-results, the
    # no-results node, and a loading node ALL exist EMPTY in the DOM from initial
    # page load; the option nodes (div.dropdown-location) only appear ~1.3-1.5s later
    # as the remote geocode returns. The R4 settle keyed the negative terminal on the
    # no-results node's PRESENCE (count>0), so it returned on the empty container the
    # instant the field was touched and parked "no dropdown option matched the city
    # 'Bologna'". The settle now awaits OPTION NODES (or a VISIBLE no-results
    # terminal) with a >=3.5s cap, so a city whose options render LATE still commits.
    #
    # The fake serves the no-results node ALWAYS (count>0, HIDDEN) and renders the
    # options only after 5 polls -- more than the R4 4-mark schedule reached, so this
    # pins BOTH the visibility fix (a count-based no-results check parks instantly on
    # the hidden node) AND the raised cap (a shorter schedule never reaches the late
    # options and parks).
    fieldmap = _fieldmap_location_required()
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(location=_FAKE_ADDRESS))
    widget = _FakeLocationWidget(geocode=["Bologna, ITA"], render_after_polls=5)
    page = _FakeLocationLeverPage(
        fieldmap, location_widget=widget,
        sweep_required_labels=_SAFE_REQUIRED_LABELS + (_LOCATION_LABEL,))

    report = lever.fill(page, fieldmap, values)

    # the settle waited through the empty-container polls (did NOT terminate on the
    # always-present hidden no-results node) and committed the late option.
    assert widget.option_polls >= 5
    assert json.loads(widget.selected_json)["name"] == "Bologna, ITA"
    assert not any(m["key"] == _LOCATION_KEY for m in report.readback_mismatches)
    assert not any(g["key"] == _LOCATION_KEY for g in report.required_unfilled)
    assert report.complete is True


# -- P1-1: the check-then-use gap on the transient geocode menu -----------------
# Instrumented live full-form run (gopuff, 2026-07-20), the decisive three lines:
#   t=209928  option tick: count 3, texts ["Bologna, ITA", "Bologna, Asti, ITA",
#                                          "Bologna, Ferrara, ITA"]
#   t=209950  option tick: count 0, texts []                       (22 ms later)
#   t=209968  PRE_PRESS key=Escape, activeElement {tag: "IFRAME"}
# The first read was `_await_location_dropdown`'s, which proved the wanted option
# present and then threw the texts away (it returned a bare bool). The second was
# `_first_matching_option`'s INDEPENDENT re-query, which found an empty menu and
# returned None. The driver parked, and ArrowDown/Enter were never dispatched at
# all: the option the engine wanted had been on screen 22 ms earlier.
#
# Every location fixture before these ones modelled a menu that, once populated,
# STAYS populated -- friendlier than the live DOM in exactly the dimension that
# decides the verdict, which is why a green suite coexisted with an 0/4 live
# failure. These four pin the transient close itself: populated at one observation,
# empty at the next, with focus gone to another element.

def _location_report(fieldmap, values, widget):
    page = _FakeLocationLeverPage(
        fieldmap, location_widget=widget,
        sweep_required_labels=_SAFE_REQUIRED_LABELS + (_LOCATION_LABEL,))
    return page, lever.fill(page, fieldmap, values)


def _location_park_reason(report):
    return next(r for k, r in report.skipped if k == _LOCATION_KEY)


def test_lever_location_commits_when_the_menu_closes_between_observation_and_commit(tmp_path):
    # P1-1 REGRESSION (this is the live failure, offline). The menu renders the
    # matching options, and the instant after the driver observes them a third-party
    # widget takes focus and the menu closes. The pre-fix driver observed the options
    # and then re-queried an already-empty menu, so it parked without ever dispatching
    # a key. The driver must instead carry its observation to the commit, notice the
    # widget is no longer committable, re-establish the search, and commit.
    fieldmap = _fieldmap_location_required()
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(location=_FAKE_ADDRESS))
    widget = _FakeLocationWidget(
        geocode=["Bologna, ITA", "Bologna, Asti, ITA", "Bologna, Ferrara, ITA"],
        focus_thief_after_polls=1)

    page, report = _location_report(fieldmap, values, widget)

    # the option the engine wanted DID commit: selectedLocation -- the value Lever
    # submits -- names the city, so the required location counts and the form is
    # COMPLETE (the pre-fix driver parked here with the city on screen).
    assert json.loads(widget.selected_json)["name"] == "Bologna, ITA"
    assert not any(m["key"] == _LOCATION_KEY for m in report.readback_mismatches)
    assert not any(g["key"] == _LOCATION_KEY for g in report.required_unfilled)
    assert report.complete is True
    # it got there the honest way: the race DID fire (the first attempt could not
    # commit, so the driver re-established the search exactly once)...
    assert widget.searches == 2
    # ...and no keystroke was ever dispatched while focus was elsewhere. Carrying the
    # index alone would have fired ArrowDown/Enter into the iframe and committed
    # NOTHING while believing it had -- a silent wrong state, worse than the park.
    assert page.keyboard.stolen == []
    assert page.keyboard.presses[:2] == ["ArrowDown", "Enter"]


def test_lever_location_parks_by_name_when_the_menu_never_becomes_committable(tmp_path):
    # P1-1 HONESTY HALF: the focus thief never lets go, so no attempt is ever
    # committable. The driver must NOT dispatch a commit it cannot land (that would
    # leave selectedLocation empty while the driver believed it committed); it must
    # exhaust its BOUNDED attempts and park by name.
    fieldmap = _fieldmap_location_required()
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(location=_FAKE_ADDRESS))
    widget = _FakeLocationWidget(geocode=["Bologna, ITA"],
                                 focus_thief_after_polls=1, thief_releases=False)

    page, report = _location_report(fieldmap, values, widget)

    # nothing was committed and nothing was faked: no ArrowDown and no Enter was
    # DISPATCHED at all (the live run's exact shape -- one key, Escape, into an
    # element that was not the location input).
    assert widget.selected_json == ""
    assert page.keyboard.presses == ["Escape"]
    assert page.keyboard.stolen == ["Escape"]
    # the retries are BOUNDED: exactly the attempt cap, never an unbounded loop.
    assert widget.searches == lever_fill._LOCATION_COMMIT_ATTEMPTS
    # and the recorded reason says what actually happened. The old reason ("no
    # dropdown option matched the city") was true of what the driver saw on its
    # second read and false about the world; it read as proof of a geocoding failure
    # and sent four investigation rounds after a cause that did not exist.
    reason = _location_park_reason(report)
    assert "DID return a matching option" in reason
    assert "no longer the one that had been observed" in reason
    assert "no dropdown option matched" not in reason
    # the required location is an honest gap, exactly as before the fix.
    assert any(g["key"] == _LOCATION_KEY for g in report.required_unfilled)
    assert any(m["key"] == _LOCATION_KEY for m in report.readback_mismatches)
    assert report.complete is False


def test_lever_location_park_reason_separates_a_genuine_geocoder_no_match(tmp_path):
    # The other side of the same distinction: here the geocoder really did answer
    # with nothing (the no-results terminal was shown). That must NOT read like the
    # closed-menu case, and it must not burn a single retry -- retrying cannot make
    # a geocoder answer differently.
    fieldmap = _fieldmap_location_required()
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(location=_FAKE_ADDRESS))
    widget = _FakeLocationWidget(no_results=True)

    _page, report = _location_report(fieldmap, values, widget)

    reason = _location_park_reason(report)
    assert "returned NO location" in reason
    assert "DID return a matching option" not in reason
    assert widget.searches == 1                 # one search, no retries burned
    assert widget.selected_json == ""
    assert report.complete is False


def test_lever_location_park_reason_names_options_that_did_not_match_the_city(tmp_path):
    # And the third case: the geocoder answered, with places that are not the city.
    # Distinct again from both "answered nothing" and "menu closed", and the option
    # texts it DID return are recorded, so the next reader of a park log can see what
    # the widget actually offered instead of inferring it.
    fieldmap = _fieldmap_location_required()
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(location=_FAKE_ADDRESS))
    widget = _FakeLocationWidget(geocode=["Genoa, ITA", "Milan, ITA"])

    _page, report = _location_report(fieldmap, values, widget)

    reason = _location_park_reason(report)
    assert "2 option(s), none naming the city 'Bologna'" in reason
    assert "Genoa, ITA, Milan, ITA" in reason
    assert "DID return a matching option" not in reason
    # a non-matching option is NEVER committed, and no retry is burned on it.
    assert widget.selected_json == ""
    assert widget.searches == 1
    assert report.complete is False


# -- F-6: per-posting option-phrasing reconnect (checkbox multi-select + radio) --
# The kernel exact-matches an option; a Lever group words its options per posting
# ("English (ENG)", "No, I do not consent"), so the bare SSOT concept ("English",
# "Opt-out") is skipped "no option matches SSOT value ...". The vendor reconnect
# remaps onto the captured wording. Field markup below is the REAL palantir probe
# shape (2026-07-19): the language checkbox group with ISO-suffixed options and the
# AI-notetaker radio pair. Built as Field objects (like the location typeahead),
# since Lever exposes no schema and the group DOM is card-runtime.

_LANGUAGE_KEY = "cards[a69a985a-eae9-4c14-90fb-b5a4b891523e][field0]"
_LANGUAGE_LABEL = "Which languages are you proficient in?"
# The captured option WORDINGS (verbatim live probe): each language name is suffixed
# with its ISO code, so a bare SSOT "Italian" never EQUALS the option and only
# unambiguous containment can map it.
_LANGUAGE_OPTIONS = [
    "English (ENG)", "Spanish (SPA)", "French (FRA)", "Italian (ITA)",
    "Polish", "Choose not to disclose", "Other"]
# exact-slug of the label (kernel `_missing_path_guess`): the canned path the
# classifier resolves the value from, so the fixture seeds the datum there.
_LANGUAGE_CANNED = "which_languages_are_you_proficient_in"

_NOTETAKER_KEY = "cards[73796cde-fc01-4758-9002-c85155f3503d][field0]"
_NOTETAKER_LABEL = "Do you consent to an AI notetaker recording the interview?"
_NOTETAKER_OPTIONS = ["Yes, I consent", "No, I do not consent"]
_NOTETAKER_CANNED = "do_you_consent_to_an_ai_notetaker_recording_the_interview"


def _language_field(*, required=False) -> Field:
    return Field(key=_LANGUAGE_KEY, label=_LANGUAGE_LABEL,
                 type="multi_value_multi_select", required=required,
                 options=list(_LANGUAGE_OPTIONS), source=LEVER_SOURCE,
                 locator=Locator(role="checkbox", name=_LANGUAGE_LABEL))


def _notetaker_field(*, required=False) -> Field:
    return Field(key=_NOTETAKER_KEY, label=_NOTETAKER_LABEL,
                 type="multi_value_single_select", required=required,
                 options=list(_NOTETAKER_OPTIONS), source=LEVER_SOURCE,
                 locator=Locator(role="radio", name=_NOTETAKER_LABEL))


def _option_phrasing_ssot(*, languages=None, notetaker=None) -> SSOT:
    # FAKE placeholder data only; the base identity/canned mirror `_fake_ssot` so
    # the safe-required base fields fill exactly as the other fill tests.
    canned = {"visa_sponsorship_required": "no", "privacy_consent_default": "yes"}
    if languages is not None:
        canned[_LANGUAGE_CANNED] = languages
    if notetaker is not None:
        canned[_NOTETAKER_CANNED] = notetaker
    return SSOT({
        "identity": {"name": "Test Candidate",
                     "email": "test.candidate@example.invalid"},
        "canned_answers": canned,
    })


def _option_phrasing_values(fieldmap, ssot, tmp_path):
    profile = profile_from_real_ssot(ssot)
    return lever.resolve_values(fieldmap, ssot, profile, assets=_assets(tmp_path))


def test_lever_language_multiselect_maps_all_four_and_drives_the_checkboxes(tmp_path):
    # F-6(a): the SSOT carries the bare concepts ["Italian","English","Spanish",
    # "French"]; the live options suffix each with an ISO code ("Italian (ITA)"), so
    # the kernel exact-match SKIPS the group. The reconnect maps each value onto its
    # option by unambiguous containment (in SSOT order) and drives all four
    # checkboxes; the group counts filled and the form COMPLETEs.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    fieldmap.fields.append(_language_field(required=True))
    ssot = _option_phrasing_ssot(
        languages=["Italian", "English", "Spanish", "French"])
    values = _option_phrasing_values(fieldmap, ssot, tmp_path)

    lang = {fv.key: fv for fv in values.fields}[_LANGUAGE_KEY]
    assert lang.value == ["Italian (ITA)", "English (ENG)",
                          "Spanish (SPA)", "French (FRA)"]
    assert not any(k == _LANGUAGE_KEY for k, _ in values.skipped)

    page = _FakeLeverPage(
        fieldmap, sweep_required_labels=_SAFE_REQUIRED_LABELS + (_LANGUAGE_LABEL,))
    report = lever.fill(page, fieldmap, values)

    for option in ("Italian (ITA)", "English (ENG)",
                   "Spanish (SPA)", "French (FRA)"):
        assert page.controls[("checkbox", option)].is_checked() is True
    # a value that did NOT map is never driven: no ghost tick on an unrelated option.
    assert page.controls[("checkbox", "Polish")].is_checked() is False
    assert not any(g["key"] == _LANGUAGE_KEY for g in report.required_unfilled)
    assert report.complete is True


def test_lever_language_multiselect_unmatched_value_parked_by_name(tmp_path):
    # F-6(a) partial: a value with NO matching option is PARKED by name while the
    # values that map still drive -- a partial drive is honest for a multi-select,
    # never a silent drop and never a fabricated tick.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    fieldmap.fields.append(_language_field())
    ssot = _option_phrasing_ssot(languages=["Italian", "Klingon"])
    values = _option_phrasing_values(fieldmap, ssot, tmp_path)

    lang = {fv.key: fv for fv in values.fields}[_LANGUAGE_KEY]
    assert lang.value == ["Italian (ITA)"]      # the one that mapped drove
    parked = [(k, r) for k, r in values.skipped if k.startswith(_LANGUAGE_KEY)]
    assert parked == [(
        f"{_LANGUAGE_KEY}[Klingon]",
        "option value 'Klingon' has no unambiguous matching option "
        "(parked; the values that mapped drove)")]


def test_lever_notetaker_optout_drives_the_negative_consent_option(tmp_path):
    # F-6(b): the SSOT canned value "Opt-out" maps by consent POLARITY onto the one
    # option matching the negative pattern ("No, I do not consent"), never a verbatim
    # reseed of the option text into the SSOT. The mapped option drives the radio,
    # the affirmative option is never ticked, and the field counts.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    fieldmap.fields.append(_notetaker_field(required=True))
    ssot = _option_phrasing_ssot(notetaker="Opt-out")
    values = _option_phrasing_values(fieldmap, ssot, tmp_path)

    note = {fv.key: fv for fv in values.fields}[_NOTETAKER_KEY]
    assert note.value == "No, I do not consent"
    assert not any(k == _NOTETAKER_KEY for k, _ in values.skipped)

    page = _FakeLeverPage(
        fieldmap, sweep_required_labels=_SAFE_REQUIRED_LABELS + (_NOTETAKER_LABEL,))
    report = lever.fill(page, fieldmap, values)

    assert page.controls[("radio", "No, I do not consent")].is_checked() is True
    assert page.controls[("radio", "Yes, I consent")].is_checked() is False
    assert not any(g["key"] == _NOTETAKER_KEY for g in report.required_unfilled)
    assert report.complete is True


def test_lever_consent_polarity_ambiguous_options_park_honestly(tmp_path):
    # F-6(b) ambiguity: when MORE THAN ONE option matches the polarity pattern the
    # mapper parks honestly rather than guessing -- the field keeps the kernel's "no
    # option matches" skip and is never driven, never a wrong-consent tick.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    fld = _notetaker_field()
    fld.options = ["Yes, I consent", "No, I do not consent", "No, opt me out"]
    fieldmap.fields.append(fld)
    ssot = _option_phrasing_ssot(notetaker="Opt-out")
    values = _option_phrasing_values(fieldmap, ssot, tmp_path)

    assert not any(fv.key == _NOTETAKER_KEY for fv in values.fields)
    assert dict(values.skipped)[_NOTETAKER_KEY].startswith(
        "no option matches SSOT value")


# -- F-7: post-typeahead re-verify covers every simple text/url field -----------


def test_lever_linkedin_reverified_and_redriven_after_location_interaction(tmp_path):
    # F-7: urls[LinkedIn] filled in the FIRST pass, then the location typeahead
    # interaction cleared it (a blur/focus race), and the R3 run reported "value did
    # not take (readback mismatch)". The simple-field re-verify (generalized from the
    # RS-f phone-only pass to every plain text/url field filled before a typeahead)
    # re-reads it after the location drive, finds it cleared, re-drives it ONCE and
    # re-reads -- so the url is repaired and the required field COMPLETEs.
    fieldmap = _fieldmap_location_required()
    for fld in fieldmap.fields:
        if fld.key == _LINKEDIN_KEY:
            fld.required = True
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(location=_FAKE_ADDRESS))
    widget = _FakeLocationWidget(geocode=["Bologna, ITA"])
    page = _FakeLocationLeverPage(
        fieldmap, location_widget=widget,
        sweep_required_labels=_SAFE_REQUIRED_LABELS
        + (_LOCATION_LABEL, "LinkedIn URL"))
    linkedin = page.swap_control(("textbox", "LinkedIn URL"),
                                 _FakeClearingTextLocator())

    report = lever.fill(page, fieldmap, values)

    # the url was re-driven ONCE (the clear was detected on the re-verify read) and
    # repopulated; it is not a gap and the form COMPLETEs (LinkedIn now required).
    assert linkedin.retyped is True
    assert linkedin.value == _fake_ssot(location=_FAKE_ADDRESS).get("links.linkedin")
    assert not any(m["key"] == _LINKEDIN_KEY for m in report.readback_mismatches)
    assert not any(g["key"] == _LINKEDIN_KEY for g in report.required_unfilled)
    assert not any(g["key"] == _LOCATION_KEY for g in report.required_unfilled)
    assert report.complete is True


_ORG_KEY = "cards[org-uuid-1234][field0]"
_ORG_LABEL = "Current company or organization"
_ORG_VALUE = "None, final-year MEng student"
# exact-slug of the label (kernel `_missing_path_guess`): the canned path the org
# value is seeded at.
_ORG_CANNED = "current_company_or_organization"


def test_lever_org_field_reverify_clears_residual_before_re_driving(tmp_path):
    # R5 ADDENDUM: the org field ("None, final-year MEng student") filled fine in R3
    # but the R4 census skipped it "readback mismatch". Root cause: the location
    # typeahead interaction left the field in a NON-EMPTY residual (a partial value),
    # and the generalized re-verify re-drove it with type_human -- which only APPENDS
    # -- so the residual and the value doubled and the re-read still mismatched. The
    # re-drive now CLEARS the field (keyboard select-all + delete) before re-typing,
    # so a residual is REPLACED, not appended to, and the field is repaired.
    fieldmap = _fieldmap_location_required()
    fieldmap.fields.append(Field(
        key=_ORG_KEY, label=_ORG_LABEL, type="input_text", required=True,
        options=[], source=LEVER_SOURCE,
        locator=Locator(role="textbox", name=_ORG_LABEL)))
    ssot = SSOT({
        "identity": {"name": "Test Candidate",
                     "email": "test.candidate@example.invalid",
                     "current_location": _FAKE_ADDRESS},
        "canned_answers": {"visa_sponsorship_required": "no",
                           "privacy_consent_default": "yes",
                           _ORG_CANNED: _ORG_VALUE}})
    profile = profile_from_real_ssot(ssot)
    values = lever.resolve_values(fieldmap, ssot, profile, assets=_assets(tmp_path))
    assert {fv.key: fv for fv in values.fields}[_ORG_KEY].value == _ORG_VALUE

    widget = _FakeLocationWidget(geocode=["Bologna, ITA"])
    page = _FakeLocationLeverPage(
        fieldmap, location_widget=widget,
        sweep_required_labels=_SAFE_REQUIRED_LABELS + (_LOCATION_LABEL, _ORG_LABEL))
    # the location interaction leaves a PARTIAL residual (not a clean empty): a blind
    # re-type would append to it and double the value; only a clear-then-retype
    # repairs it.
    org = page.swap_control(
        ("textbox", _ORG_LABEL),
        _FakeResidualClearTextLocator(residual="None, final-y"))

    report = lever.fill(page, fieldmap, values)

    # cleared then re-typed exactly once: the value is the clean intended string, not
    # the doubled "None, final-yNone, final-year MEng student".
    assert org.retyped is True
    assert org.value == _ORG_VALUE
    assert not any(m["key"] == _ORG_KEY for m in report.readback_mismatches)
    assert not any(g["key"] == _ORG_KEY for g in report.required_unfilled)
    assert report.complete is True


# -- FINAL ROUND: page pre-fill pollution (location + phone start non-empty) -----


def test_lever_prefilled_location_is_cleared_before_the_city_query(tmp_path):
    # LIVE gopuff: Lever pre-fills the geocoded location input from the candidate
    # session with a full address. type_human APPENDS, so without a clear the geocoder
    # query is "<address><city>", which matches nothing and the location parks --
    # exactly as the engine did while a conductor probe typing the city into a CLEAN
    # field got 3 options at ~823ms. The driver now CLEARS the input before typing the
    # city, so the geocoder sees the clean query and commits.
    fieldmap = _fieldmap_location_required()
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(location=_FAKE_ADDRESS))
    widget = _FakeLocationWidget(geocode=["Bologna, ITA"], prefill=_FAKE_ADDRESS)
    page = _FakeLocationLeverPage(
        fieldmap, location_widget=widget,
        sweep_required_labels=_SAFE_REQUIRED_LABELS + (_LOCATION_LABEL,))

    report = lever.fill(page, fieldmap, values)

    # the pre-fill was cleared, so the geocoder saw ONLY the clean city query.
    assert widget.content == _FAKE_CITY
    assert json.loads(widget.selected_json)["name"] == "Bologna, ITA"
    assert not any(g["key"] == _LOCATION_KEY for g in report.required_unfilled)
    assert report.complete is True


def test_lever_location_is_excluded_from_the_generic_text_pass(tmp_path, monkeypatch):
    # Empirically falsifies the double-drive hypothesis: a field whose key normalizes
    # to "location" is routed to the geocoded typeahead ONLY, never to the generic
    # `_fill_field` pass, so the FULL address is never typed into the geocoded input by
    # a generic drive. The live pollution is therefore a page PRE-FILL (handled by the
    # clear), not a double-drive. This pins the exclusion so a future refactor cannot
    # silently start generic-driving the location field.
    fieldmap = _fieldmap_location_required()
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(location=_FAKE_ADDRESS))
    generic_keys = []
    orig_fill_field = lever_fill._fill_field
    monkeypatch.setattr(
        lever_fill, "_fill_field",
        lambda page, fv: (generic_keys.append(fv.key), orig_fill_field(page, fv))[1])
    widget = _FakeLocationWidget(geocode=["Bologna, ITA"])
    page = _FakeLocationLeverPage(
        fieldmap, location_widget=widget,
        sweep_required_labels=_SAFE_REQUIRED_LABELS + (_LOCATION_LABEL,))

    lever.fill(page, fieldmap, values)

    loc_fv = {fv.key: fv for fv in values.fields}[_LOCATION_KEY]
    assert lever_fill._is_location_field(loc_fv) is True
    assert _LOCATION_KEY not in generic_keys   # never generic-driven -> never double-driven


def test_lever_prefilled_phone_is_cleared_before_the_first_pass_drive(tmp_path):
    # LIVE gopuff: the phone (plain input, now required) ALSO readback-mismatched.
    # Lever pre-fills it from the session and the first-pass type_human APPENDED to the
    # pre-fill, doubling it. `_apply_native` now clears every text field before typing,
    # so the first-pass phone drive starts clean and reads back. No location field
    # here, so this is a pure FIRST-PASS clear (not the re-verify path).
    fieldmap = _fieldmap_phone_required()
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(phone=_FAKE_PHONE))
    page = _FakeLeverPage(
        fieldmap, sweep_required_labels=_SAFE_REQUIRED_LABELS + ("Phone",))
    phone = page.swap_control(("textbox", "Phone"),
                              _FakePrefilledTextLocator("+39 051 000000"))  # a stale session pre-fill

    report = lever.fill(page, fieldmap, values)

    # cleared then typed digits-only, NOT the doubled "+39 051 000000+1 555 0100".
    assert phone.value == lever_fill._phone_type_form(_FAKE_PHONE)
    assert not any(m["key"] == "phone" for m in report.readback_mismatches)
    assert not any(g["key"] == "phone" for g in report.required_unfilled)
    assert report.complete is True


# -- PRE-SEAL ROUND: privacy banner dismissal + phone as-you-type mask -----------


def test_lever_privacy_banner_dismissed_before_location_unblocks_the_commit(tmp_path):
    # LIVE gopuff: a fixed "This website uses cookies..." banner (DENY / ACCEPT)
    # intercepts focus/keyboard, so the geocode dropdown's ArrowDown/Enter land on the
    # banner and the uncommitted geocode text is wiped on blur -> location EMPTY. The
    # fill dismisses the banner at START (before the location drive), PREFERRING DENY
    # (owner policy: application-necessary consent only), so the commit keystrokes reach
    # the widget and the location commits.
    fieldmap = _fieldmap_location_required()
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(location=_FAKE_ADDRESS))
    widget = _FakeLocationWidget(geocode=["Bologna, ITA"])
    page = _FakeBannerLocationPage(
        fieldmap, location_widget=widget, banner_buttons=["Deny", "Accept"],
        sweep_required_labels=_SAFE_REQUIRED_LABELS + (_LOCATION_LABEL,))

    report = lever.fill(page, fieldmap, values)

    # DENY was clicked (preferred over ACCEPT) and the banner is gone BEFORE the
    # location drive, so no keystroke was swallowed and the commit landed.
    assert page.banner_clicks == ["Deny"]
    assert page.banner_present is False
    assert ("swallowed", "Enter") not in page.keyboard.presses
    assert json.loads(widget.selected_json)["name"] == "Bologna, ITA"
    assert not any(g["key"] == _LOCATION_KEY for g in report.required_unfilled)
    assert report.complete is True


def test_lever_privacy_banner_falls_back_to_accept_when_no_deny(tmp_path):
    # No DENY button on this banner variant: the fill falls back to ACCEPT (never the
    # SETTINGS button, which opens a second dialog). Still dismissed before the drive.
    fieldmap = _fieldmap_location_required()
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(location=_FAKE_ADDRESS))
    widget = _FakeLocationWidget(geocode=["Bologna, ITA"])
    page = _FakeBannerLocationPage(
        fieldmap, location_widget=widget,
        banner_buttons=["Cookies settings", "Accept all"],
        sweep_required_labels=_SAFE_REQUIRED_LABELS + (_LOCATION_LABEL,))

    report = lever.fill(page, fieldmap, values)

    assert page.banner_clicks == ["Accept all"]   # settings is never a target
    assert report.complete is True


def test_lever_phone_survives_as_you_type_reformat_via_digits_only(tmp_path):
    # LIVE gopuff: the phone rendered "+3935100000007 0000" for an intended
    # "+39 351 000 0000" -- the input's as-you-type mask fought our typed SPACES and
    # duplicated digits. Typing DIGITS-ONLY (leading + kept) lets the mask group
    # cleanly, so the value carries exactly the intended digits and the digit-tolerant
    # readback matches.
    intended = "+39 351 000 0000"
    fieldmap = _fieldmap_phone_required()
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(phone=intended))
    page = _FakeLeverPage(
        fieldmap, sweep_required_labels=_SAFE_REQUIRED_LABELS + ("Phone",))
    phone = page.swap_control(("textbox", "Phone"), _FakeMaskedPhoneLocator())

    report = lever.fill(page, fieldmap, values)

    # digits-only was typed (no spaces to fight), so the mask never duplicated a digit:
    # exactly the intended digits, no "+3935100000007 0000" corruption.
    assert phone.value == "+393510000000"
    assert lever_fill._phone_digits(phone.value) == lever_fill._phone_digits(intended)
    assert not any(m["key"] == "phone" for m in report.readback_mismatches)
    assert not any(g["key"] == "phone" for g in report.required_unfilled)
    assert report.complete is True


# -- W5B-LEVER F2: label vs name-attribute key-space reconciliation -------------


def test_lever_sweep_name_attr_reconciles_against_label(tmp_path):
    # LIVE FAILURE (agicap 2026-07-12): the sweep names a required control by its
    # `name` ATTRIBUTE ("name", "email", "cards[...][field0]"), while the capture
    # side named it by its human LABEL ("Full name"). The name-attribute tokens had
    # nothing to reconcile against, so every required control produced a spurious
    # "dom-sweep:*" gap and the form could never read COMPLETE.
    #
    # The sweep here is the REAL `base.sweep_required` running over the REAL
    # (live-corrected) fixture DOM, not a hand-built list: arm 2 is silent (div
    # labels, a U+2731 marker), so the name attribute is the ONLY channel, exactly
    # as live. Zero dom-sweep gaps must remain. RETARGETED (W5.1c): the consent
    # checkbox is no longer a hand-off -- fill() now DRIVES it via
    # `control_toolkit.drive_control`, so with the fake page registering a real
    # tickable control for it (see `_FakeLeverPage.__init__`), it readback-confirms
    # and the form is fully COMPLETE, with zero required gaps of any kind.
    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeLeverPage(fieldmap, sweep_dom=_dom_html())

    report = lever.fill(page, fieldmap, values)

    # premise: on real Lever markup the sweep speaks name attributes only
    from engine.providers import base
    dom_required = base.sweep_required(page)
    assert "name" in dom_required and "full name" not in dom_required

    assert not any(g["key"].startswith("dom-sweep:")
                   for g in report.required_unfilled)
    # the consent checkbox is DRIVEN and CONFIRMED, not left as a "fill-error" or a
    # silent skip: an unconfirmed drive would still surface here as a gap.
    consent = {fv.key: fv for fv in values.fields}[_CONSENT_KEY]
    consent_control = page.controls[("checkbox", consent.locator.name)]
    assert consent_control.is_checked() is True
    assert not any(key == _CONSENT_KEY for key, _ in report.skipped)
    assert report.required_unfilled == []
    assert report.complete is True


def test_lever_sweep_two_arm_control_consumes_every_alias_not_just_the_first(tmp_path):
    # SYNTHETIC, NON-LIVE LEVER VARIANT (W5B-LEVER round 4). The markup below is NOT
    # what the agicap posting renders, and it is not presented as it. The live page (and
    # the corrected dom.html fixture) label their fields with a DIV carrying a U+2731
    # marker, so the kernel sweep's arm 2 (`label, legend` filtered on an ASCII "*") is
    # SILENT there and every required field is named by exactly ONE alias, its `name`
    # attribute.
    #
    # That single-alias shape is exactly why this test exists. With one alias per field,
    # "consume the FIRST matching alias" and "consume EVERY matching alias" are
    # behaviourally identical, so the round-3 fixture correction -- right in itself --
    # removed the only condition under which the round-1 reconciliation defect can
    # manifest, and the fix stopped being pinned by any test (round-3 review, finding d).
    #
    # The control below is a TWO-ARM one: a <label> element bearing an ASCII "*" (the
    # classic Lever template shape) over an input whose only accessible name is its `name`
    # attribute. The REAL kernel sweep therefore names that ONE field TWICE, "full name"
    # through arm 2 and "name" through arm 1, and first-match diverges from every-match:
    # consuming only the label leaves the arm-1 token orphaned, and it reappears as the
    # verbatim live symptom, a phantom `dom-sweep:name` gap on a form that IS filled.
    #
    # Arm 2 does not fire on agicap today, so multi-alias consumption is a DEFENSIVE
    # contract rather than a live-path one. It is pinned here because Lever's markup is
    # not frozen and the reconciliation must stay correct for any posting whose labels do
    # reach arm 2. Only the sweep's CSS resolution is shimmed: the field map, the sweep,
    # the reconciliation and the gap arithmetic are all the real shipped code.
    html = (
        "<html><body><form>"
        "<ul class=\"application-fields\">"
        "<li class=\"application-field\">"
        "<label class=\"application-label\">Full name<span class=\"required\">*</span></label>"
        "<input type=\"text\" name=\"name\" required>"
        "</li>"
        "</ul>"
        "</form></body></html>")
    fieldmap = lever_capture._parse_lever(html, "fauxcorp", "9001",
                                          now=lambda: _PINNED)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeLeverPage(fieldmap, sweep_dom=html)

    # premise: the real sweep names this ONE captured field through BOTH of its arms
    from engine.providers import base
    dom_required = base.sweep_required(page)
    assert {"name", "full name"} <= dom_required
    assert [(f.key, f.label) for f in fieldmap.required_fields()] == [("name", "Full name")]

    # every alias the sweep emitted for the field is submitted to the diff, so neither
    # token is left orphaned (first-match would submit one and orphan the other).
    # `engine.providers.lever.fill` is a submodule shadowed at package scope by the
    # `fill` Provider callable, so it is reached through importlib, as `capture` is.
    lever_fill = importlib.import_module("engine.providers.lever.fill")
    assert lever_fill._reconciled_schema_required(
        fieldmap, dom_required) == {"Full name", "name"}

    report = lever.fill(page, fieldmap, values)

    # ...so the filled form carries NO phantom sweep gap in either direction: a
    # label-first match orphans "name", a key-first match orphans "full name", and both
    # surface as `dom-sweep:` entries here.
    assert page.controls[("textbox", "Full name")].input_value() == "Test Candidate"
    assert not any(g["key"].startswith("dom-sweep:") for g in report.required_unfilled)
    assert report.required_unfilled == []
    assert report.complete is True


def test_lever_sweep_uncaptured_required_still_blocks(tmp_path):
    # THE FALSIFICATION TEST for F2: the reconciliation may only close a key-space
    # difference, never manufacture coverage. The live sweep (real kernel, real
    # markup) still requires the consent control, which this field map GENUINELY
    # does not carry -- no captured field answers to that label or that name
    # attribute -- so it must remain a dom_only gap and still force NOT_COMPLETE,
    # exactly as the sweep did before the fix.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeLeverPage(fieldmap, sweep_dom=_dom_html())

    report = lever.fill(page, fieldmap, values)

    gaps = {g["key"]: g for g in report.required_unfilled}
    assert f"dom-sweep:{_CONSENT_KEY}" in gaps
    assert "DOM sweep is authoritative" in gaps[f"dom-sweep:{_CONSENT_KEY}"]["reason"]
    assert report.complete is False
    assert report.caption().endswith("NOT COMPLETE")


def test_lever_filled_required_field_the_sweep_cannot_see_is_not_a_gap(tmp_path):
    # LIVE FAILURE, ROUND 3 (agicap): the resume is REQUIRED but the sweep cannot
    # see it. Its <input type=file> carries no `required` attribute and is
    # CSS-hidden (arm 1 misses it), and arm 2 is silent on real Lever markup, so the
    # field has NO sweep token on EITHER arm. Capture still marks it required
    # (correctly: the container carries the marker span), so it lands in
    # `schema_only` -- and the old unconditional loop turned that into
    # `dom-sweep:resume / cv`, a required gap on a form whose CV HAD uploaded. Same
    # phantom-gap bug as the dom_only one, in the other direction.
    #
    # BOTH halves in one test, because the fix is only honest if it keeps biting:
    #  (a) FILLED + unswept -> NOT a gap (the sweep is blind here, the upload landed);
    #  (b) UNFILLED + unswept -> STILL BLOCKS (no file input on the page, so the CV
    #      never attaches). If the schema_only half were silenced wholesale instead
    #      of gated on "filled", (b) would pass a form with no CV attached.
    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeLeverPage(fieldmap, sweep_dom=_dom_html())

    # premise: the required resume is invisible to BOTH sweep arms
    from engine.providers import base
    dom_required = base.sweep_required(page)
    assert "resume" not in dom_required and "resume / cv" not in dom_required
    assert any(f.key == _RESUME_KEY and f.required for f in fieldmap.fields)

    report = lever.fill(page, fieldmap, values)

    # (a) the CV uploaded, so the sweep's blind spot must not invent a gap
    assert page.file_inputs[0].set_input_files_calls == 1
    # RETARGETED (W5.1c has landed): the consent checkbox is no longer a hand-off,
    # `fill()` DRIVES it via `control_toolkit.drive_control`. Earn the now-empty
    # gap set below by proving the drive actually happened and confirmed, rather
    # than asserting `set()` on faith -- an unconfirmed drive would surface as a
    # gap too, and would look identical to a silently-broken adoption otherwise.
    consent = {fv.key: fv for fv in values.fields}[_CONSENT_KEY]
    consent_control = page.controls[("checkbox", consent.locator.name)]
    assert consent_control.is_checked() is True
    assert not any(key == _CONSENT_KEY for key, _ in report.skipped)
    keys = {g["key"] for g in report.required_unfilled}
    assert _RESUME_KEY not in keys
    assert _RESUME_SWEEP_GAP not in keys
    assert _CONSENT_KEY not in keys
    assert keys == set()                   # every required field genuinely landed
    assert report.complete is True

    # (b) same form, but the CV never attaches: the required gap MUST come back,
    # through BOTH channels -- the kernel completeness census (the field's own key)
    # AND the capture/fill-drift branch of _sweep_gaps (the `dom-sweep:` alias).
    # The second assertion pins the `matched or [...]` drift fallback: drop it and
    # the drift gap vanishes, so an unfilled required field the sweep cannot see is
    # no longer flagged as drift (it still blocks via completeness, but the honest
    # drift signal is silenced).
    fieldmap_b = _fieldmap()
    values_b = _resolved_values(fieldmap_b, tmp_path=tmp_path)
    page_b = _FakeLeverPage(fieldmap_b, sweep_dom=_dom_html(), file_inputs=[])

    report_b = lever.fill(page_b, fieldmap_b, values_b)

    keys_b = {g["key"] for g in report_b.required_unfilled}
    assert _RESUME_KEY in keys_b                    # completeness census still blocks
    assert _RESUME_SWEEP_GAP in keys_b              # AND the drift branch still fires
    assert report_b.complete is False


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

        # the seam every field now goes through (round 8): the fill locates a control by
        # its SUBMISSION NAME (`lever.fill._locate` -> `page.locator`), so field access
        # must be observed HERE too or this test would watch a door nobody uses.
        def locator(self, css):
            order.append("locator")
            return super().locator(css)

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
        # BOTH locator seams navigate: the submission-name locator every live field now
        # takes (round 8), and the kernel role+name fallback. The safety invariant is
        # "a navigation DURING the fill raises", whichever door the fill went through.
        def locator(self, css):
            self._url = "https://jobs.lever.co/fauxcorp/thanks"
            return super().locator(css)

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


# -- W5B-LEVER F3: required-control capture coverage ---------------------------


def test_lever_capture_aria_required_enumerated():
    """A control marked required ONLY through `aria-required="true"` (no native
    `required` attribute, no `.required` asterisk marker) must be captured as
    REQUIRED: the live DOM sweep's selector is `[required], [aria-required='true']`,
    so a capture that read the native attribute alone marked the field optional
    while the sweep required it -- an unanswerable gap. The optional sibling card
    below pins the fix as narrow: enumerating aria-required must not make every
    control read required."""
    html = (
        "<html><body><form>"
        "<ul class=\"application-fields\">"
        "<li class=\"application-field\">"
        "<label class=\"application-label\">Full name<span class=\"required\">*</span></label>"
        "<input type=\"text\" name=\"name\" required>"
        "</li>"
        "</ul>"
        "<div class=\"application-question dropdown\">"
        "<div class=\"application-label\"><div class=\"text\">Years of experience</div></div>"
        "<select name=\"cards[b2c4][field0]\" aria-required=\"true\">"
        "<option value=\"\">Select...</option>"
        "<option value=\"0-2\">0-2 years</option>"
        "<option value=\"3-5\">3-5 years</option>"
        "</select>"
        "</div>"
        "<div class=\"application-question dropdown\">"
        "<div class=\"application-label\"><div class=\"text\">Preferred start month</div></div>"
        "<select name=\"cards[b2c4][field1]\" aria-required=\"false\">"
        "<option value=\"\">Select...</option>"
        "<option value=\"jan\">January</option>"
        "</select>"
        "</div>"
        "</form></body></html>")
    page = _FakePage(html=html)
    fm = capture_lever("globex", "req-88", _factory_for(page), now=lambda: _PINNED)

    by_key = {f.key: f for f in fm.fields}
    aria_required = by_key["cards[b2c4][field0]"]
    assert aria_required.required is True
    assert aria_required.label == "Years of experience"
    assert aria_required.type == "multi_value_single_select"
    assert aria_required.options == ["0-2 years", "3-5 years"]
    # narrow: a card with NO required signal at all stays optional
    assert by_key["cards[b2c4][field1]"].required is False


def test_lever_capture_outside_container_enumerated():
    """A REQUIRED control rendered outside BOTH known containers
    (`.application-field` / `.application-question`) must still be captured: the
    live sweep scans the whole page, so a control the container passes never
    walked was required by the sweep with no captured field to answer it. The
    optional stray below pins the pass as narrow: only REQUIRED strays are swept
    in, never every loose control on the page."""
    html = (
        "<html><body><form>"
        "<ul class=\"application-fields\">"
        "<li class=\"application-field\">"
        "<label class=\"application-label\">Full name<span class=\"required\">*</span></label>"
        "<input type=\"text\" name=\"name\" required>"
        "</li>"
        "</ul>"
        "<div class=\"custom-widget\">"
        "<label for=\"cl\">Cover letter</label>"
        "<textarea id=\"cl\" name=\"comments\" required></textarea>"
        "</div>"
        "<div class=\"custom-widget\">"
        "<label for=\"ref\">How did you hear about us?</label>"
        "<input type=\"text\" id=\"ref\" name=\"referral\">"
        "</div>"
        "</form></body></html>")
    page = _FakePage(html=html)
    fm = capture_lever("globex", "req-88", _factory_for(page), now=lambda: _PINNED)

    by_key = {f.key: f for f in fm.fields}
    stray = by_key["comments"]
    assert stray.required is True
    assert stray.label == "Cover letter"     # from its own <label for=...>
    assert stray.type == "textarea"
    assert stray.source == LEVER_SOURCE
    assert stray.locator.name == "Cover letter"
    # narrow: an OPTIONAL control outside the containers is still not captured
    assert "referral" not in by_key


def test_lever_required_radio_group_captured_as_radio_not_mis_driven(tmp_path):
    """LIVE FAILURE, ROUND 3 (agicap): a required custom question rendered as a
    RADIO GROUP (radios sharing one submission name) was captured as `input_text`
    with role `textbox` -- `capture._control_type` had no radio branch and fell
    through -- so `base._locate` hunted a textbox that does not exist and the
    required field died as an opaque FILL ERROR, mislabelled with the option texts
    mashed together instead of the question.

    The durable contract this test pins is CAPTURE CORRECTNESS: the group is
    recorded as a radio (role=radio, the question as the label, the option texts as
    options). What happens to it AFTER capture (whether it is auto-driven, and how)
    is deferred to the W5.1c click-policy wave and is NOT asserted here as a durable
    contract. The only current-behaviour assertion below is that the correctly
    captured radio is not mis-driven as a phantom textbox: it is NOT-YET-FILLED
    (pending W5.1c) rather than a fill error, and it still blocks while unanswered.

    ROUND-5 CORRECTION, and the reason this test failed to prevent the very bug it
    describes for two rounds: the card below now carries the LIVE nested shape. Lever
    wraps the radios in an `.application-field` div that is a SIBLING of the
    `.application-label` holding the question, all inside the
    `.application-question` card (re-derived over five live apply pages; every
    `.application-field` on every one of them is card-nested). The round-3/4 version
    of this test put the radios DIRECTLY under the card, so the base pass never saw
    them, the custom pass won by default, and the test passed while the LIVE agicap
    radio group was still captured as a textbox -- the base pass emitted a phantom
    twin for the same submission key and, being first, won the dedup. The markup was
    friendlier than the live page in precisely the dimension that decided the
    verdict. It is now the live dimension, so this test FAILS against the unfixed
    capture (verified, round 5) instead of certifying it green.

    The radios are also wrapped by their `<label>` with no `for`/`id` pairing and
    their wording is in a `<span class="application-answer-alternative">`, which is
    what Lever serves; the previous `for="fr0"`/`id="fr0"` pairing was a second
    invented friendliness, exercising a `_radio_option_label` branch the live page
    never takes.
    """
    html = (
        "<html><body><form>"
        "<ul class=\"application-fields\">"
        "<li class=\"application-field\">"
        "<div class=\"application-label\">Full name<span class=\"required\">&#10033;</span></div>"
        "<input type=\"text\" name=\"name\" required>"
        "</li>"
        "</ul>"
        "<li class=\"application-question custom-question\"><div>"
        "<div class=\"application-label full-width multiple-choice\"><div class=\"text\">"
        "What is your level of French?<span class=\"required\">&#10033;</span></div></div>"
        "<div class=\"application-field full-width required-field\">"
        "<ul data-qa=\"multiple-choice\">"
        "<li><label><input type=\"radio\" name=\"cards[7f3a][field0]\" value=\"native\""
        " required><span class=\"application-answer-alternative\">Native or bilingual"
        "</span></label></li>"
        "<li><label><input type=\"radio\" name=\"cards[7f3a][field0]\" value=\"pro\""
        " required><span class=\"application-answer-alternative\">Professional"
        "</span></label></li>"
        "<li><label><input type=\"radio\" name=\"cards[7f3a][field0]\" value=\"basic\""
        " required><span class=\"application-answer-alternative\">Beginner"
        "</span></label></li>"
        "</ul></div></div></li>"
        "</form></body></html>")
    page = _FakePage(html=html)
    fm = capture_lever("globex", "req-88", _factory_for(page), now=lambda: _PINNED)

    by_key = {f.key: f for f in fm.fields}
    radio = by_key["cards[7f3a][field0]"]
    # captured as a radio: click-hazard role, the QUESTION as the label (not a mash
    # of the option texts), and the option wordings enumerated.
    assert radio.locator.role == "radio"
    assert radio.label == "What is your level of French?"
    assert radio.required is True
    assert radio.options == ["Native or bilingual", "Professional", "Beginner"]

    # RETARGETED (W5.1c has landed): give the radio an answer, as the content
    # overlay will at seal, by injecting the resolved value directly (the
    # question is not SSOT-answerable). With the correct role the field is not
    # mis-driven as a phantom textbox, AND -- superseding the old "hand-off,
    # still blocks" expectation -- it is now DRIVEN: `_locate_option` finds the
    # one option control by its own accessible name ("Professional") and ticks
    # it, and the field counts as filled only once that readback confirms. The
    # round-2 capture (role=textbox) would instead type the value into a
    # phantom textbox and read it back as filled, so this check does not hold
    # under the bug either.
    from engine.kernel.contracts import FieldValue
    fake = _FakeLeverPage(fm, sweep_dom=html)
    ssot = _fake_ssot()
    values = lever.resolve_values(fm, ssot, profile_from_real_ssot(ssot),
                                  assets=_assets(tmp_path))
    values.skipped = [(k, r) for k, r in values.skipped if k != radio.key]
    values.fields.append(FieldValue(
        key=radio.key, label=radio.label, type=radio.type,
        locator=radio.locator, value="Professional"))

    report = lever.fill(fake, fm, values)

    # driven and readback-confirmed: the chosen option is ticked, the field is
    # no longer a required gap, and there is no fill-error skip.
    professional = fake.controls[("radio", "Professional")]
    assert professional.is_checked() is True
    assert radio.key not in {g["key"] for g in report.required_unfilled}
    assert not any("fill-error" in reason
                   for key, reason in report.skipped if key == radio.key)
    # the rest of the (tiny) form -- just "Full name" -- also lands, so the
    # whole report reads COMPLETE, not merely "this one field is unblocked".
    assert fake.controls[("textbox", "Full name")].input_value() == "Test Candidate"
    assert report.complete is True


def test_lever_required_checkbox_group_captured_as_checkbox_not_a_phantom_listbox(tmp_path):
    """LIVE REGRESSION THIS WAVE INTRODUCED, CAUGHT IN ROUND 6 (agicap, gopuff, swile):
    a custom question rendered as a CHECKBOX GROUP (N checkboxes sharing one submission
    name) is typed `multi_value_multi_select`, and the type's canonical role is
    `listbox` -- a role NO element on a Lever page has (re-derived 2026-07-13 across
    the three live apply DOMs: zero `[role=listbox]`, zero `<select multiple>`).
    `_needs_human_handoff` keys off the ROLE, so a `listbox` group is NOT handed off:
    the fill DRIVES it, `base._locate` builds `get_by_role('listbox', name=...)`, that
    resolves to ZERO live elements, and the field dies as an opaque FILL ERROR. That is
    the round-5 radio blocker's harm mechanism, one control type over.

    It is a REGRESSION, not merely an untouched gap. At baseline the live agicap group
    captured as `boolean` / `checkbox` (the single-checkbox type, right by accident) and
    was therefore handed off SAFELY. Round 5 taught the parse to read the group richly,
    which moved it off role `checkbox` onto `listbox`: its label and options improved,
    its ROLE broke. Live at HEAD the same defect hit gopuff's REQUIRED 5-box
    availability group and swile's REQUIRED consent group.

    THE DURABLE CONTRACT PINNED HERE: a control the applicant TICKS is captured with a
    role the fill hands off, never one it would TYPE into or SELECT from. The TYPE stays
    `multi_value_multi_select` (the data genuinely is a multi-choice, and the kernel type
    vocabulary is frozen); the ROLE is the DOM fact and is keyed off the CONTROL, so the
    invariant holds for every click widget at once rather than one special case at a time.
    What happens to the group AFTER capture (whether it is auto-driven, and how) is
    deferred to the W5.1c click-policy wave and is NOT asserted here as durable.

    The markup is the LIVE gopuff checkbox card, re-derived 2026-07-13 (card >
    `.application-label` > `.text` + a U+2731 marker, SIBLING `.application-field` >
    `ul[data-qa=checkboxes]` > li > label > `input[type=checkbox]` whose wording is both
    its `value` and a `<span class="application-answer-alternative">`), with a short fake
    card id. The `.application-field` is NESTED as it is live, so the base pass sees the
    container too and this exercises the real collision, not a friendlier shape.
    """
    html = (
        "<html><body><form>"
        "<ul class=\"application-fields\">"
        "<li class=\"application-field\">"
        "<div class=\"application-label\">Full name<span class=\"required\">&#10033;</span></div>"
        "<input type=\"text\" name=\"name\" required>"
        "</li>"
        "</ul>"
        "<li class=\"application-question custom-question\"><div>"
        "<div class=\"application-label full-width multiple-select\"><div class=\"text\">"
        "What is your availability (check all that apply)?"
        "<span class=\"required\">&#10033;</span></div></div>"
        "<div class=\"application-field full-width required-field\">"
        "<ul data-qa=\"checkboxes\">"
        "<li><label><input type=\"checkbox\" name=\"cards[7f3a][field0]\" value=\"Days\""
        " required><span class=\"application-answer-alternative\">Days</span></label></li>"
        "<li><label><input type=\"checkbox\" name=\"cards[7f3a][field0]\" value=\"Nights\""
        " required><span class=\"application-answer-alternative\">Nights</span></label></li>"
        "<li><label><input type=\"checkbox\" name=\"cards[7f3a][field0]\" value=\"Weekends\""
        " required><span class=\"application-answer-alternative\">Weekends</span></label></li>"
        "</ul></div></div></li>"
        "</form></body></html>")
    page = _FakePage(html=html)
    fm = capture_lever("globex", "req-92", _factory_for(page), now=lambda: _PINNED)

    group = {f.key: f for f in fm.fields}["cards[7f3a][field0]"]
    question = "What is your availability (check all that apply)?"

    # THE TYPE IS THE DATA FACT AND THE ROLE IS THE DOM FACT, and here they disagree:
    # only the role is ever built into a locator, and the type's canonical role is the
    # phantom. Deriving the role from the TYPE is exactly the defect.
    assert group.type == "multi_value_multi_select"
    assert lever_capture._role_for_type(group.type) == "listbox"   # the phantom role
    assert group.locator.role == "checkbox"                        # the DOM fact
    # LEFT AS IS, deliberately (round 8), where the same convention at the card-select
    # test was CORRECTED: `locator.name` carries the question for a checkbox group too,
    # but this control is HANDED OFF (`_needs_human_handoff` fires on the role, before any
    # locator is built), so the question-as-name is never resolved against the page and
    # cannot become a phantom. It pins the capture's label plumbing, nothing more.
    assert group.locator.name == question
    assert group.label == question          # the QUESTION, not a mash of the options
    assert group.required is True
    assert group.options == ["Days", "Nights", "Weekends"]
    # premise: nothing in this markup (nor on any of the three live pages) can answer a
    # `listbox` locator, so the phantom role can only ever resolve to zero elements.
    assert "listbox" not in html and "<select" not in html

    # RETARGETED (W5.1c has landed): answer the group as the content overlay will
    # at seal (a NON-bool value: a bool would still be a single checkbox drive
    # whatever the role, and would pin nothing about the GROUP path). Superseding
    # the old "hand-off, phantom-role-safe" expectation: the CORRECT role
    # (checkbox, pinned above) is exactly what lets `_drive_control_field` reach
    # `_locate_option` per option -- had the role stayed the `listbox` phantom, the
    # group would have died as an opaque fill error, the very bug this test's
    # docstring describes. Getting the role right is therefore now a precondition
    # for driving the group at all, not merely for a safe hand-off.
    from engine.kernel.contracts import FieldValue
    ssot = _fake_ssot()
    values = lever.resolve_values(fm, ssot, profile_from_real_ssot(ssot),
                                  assets=_assets(tmp_path))
    values.skipped = [(k, r) for k, r in values.skipped if k != group.key]
    values.fields.append(FieldValue(
        key=group.key, label=group.label, type=group.type, locator=group.locator,
        value=["Days", "Weekends"]))
    page = _FakeLeverPage(fm, sweep_dom=html)

    report = lever.fill(page, fm, values)

    # DRIVEN, not handed off: every selected option is located by its OWN
    # accessible name and ticked, and the field counts as filled only once ALL of
    # them readback-confirm -- a partial tick would still be a required gap.
    days = page.controls[("checkbox", "Days")]
    weekends = page.controls[("checkbox", "Weekends")]
    assert days.is_checked() is True
    assert weekends.is_checked() is True
    gaps = {g["key"]: g for g in report.required_unfilled}
    assert group.key not in gaps
    assert not any("fill-error" in reason
                   for key, reason in report.skipped if key == group.key)
    assert not any(g.startswith("dom-sweep:") for g in gaps)
    assert report.required_unfilled == []
    assert report.complete is True
    # ...and the rest of the form still filled.
    assert page.controls[("textbox", "Full name")].input_value() == "Test Candidate"


def test_lever_card_owned_field_container_is_not_parsed_as_a_base_field():
    """THE PRIMARY ROUND-5 FIX, pinned at its source: an `.application-field`
    container nested inside an `.application-question` card belongs to the CARD, and
    the base pass must not emit a second, phantom field for the same submission key.

    Why this test exists SEPARATELY from the radio test above, and why the fix is a
    source-level exclusion rather than only a dedup tie-break. A card `<select>`
    parses IDENTICALLY through both paths -- same type (`multi_value_single_select`),
    same locator role (`combobox`), same enumerated options -- and differs in exactly
    one thing: the LABEL. The card pass reads the question from the
    `.application-label` sibling; the base pass cannot see that sibling (it is
    OUTSIDE the container it walks), so it falls back to the container's own text,
    which is the option wordings mashed together ('Select... Yes No' here; on live
    gopuff, the entire list of US states). Two parses of equal richness therefore
    TIE, and any tie-break that arbitrates AFTER the fact must fall back to document
    order, which hands the field to the phantom. Only excluding the container at the
    source removes the duplicate, and it does so for every control type at once.

    The markup is the LIVE Lever select card (re-derived 2026-07-13 from the gopuff
    apply page): card > (`.application-label` > `.text` + required span) sibling +
    `.application-field` > `.application-dropdown` > `<select>`.
    """
    html = (
        "<html><body><form>"
        "<li class=\"application-question custom-question\"><div>"
        "<div class=\"application-label full-width dropdown\"><div class=\"text\">"
        "Do you require visa sponsorship?<span class=\"required\">&#10033;</span>"
        "</div></div>"
        "<div class=\"application-field full-width required-field\">"
        "<div class=\"application-dropdown\">"
        "<select name=\"cards[7f3a][field2]\" required>"
        "<option value=\"\">Select...</option>"
        "<option value=\"yes\">Yes</option>"
        "<option value=\"no\">No</option>"
        "</select></div></div></div></li>"
        "</form></body></html>")

    # (1) SOURCE LEVEL: the base pass emits NOTHING for a card-owned container, so
    # the collision never reaches the dedup at all.
    tree = lever_capture._build_tree(html)
    assert [f.key for f in lever_capture._lever_base_fields(tree)] == []
    owned = lever_capture._card_owned_field_containers(tree)
    assert len(owned) == 1, "the card's nested .application-field must be recognized"

    # (2) WHOLE PARSE: exactly ONE field for the key, carrying the QUESTION as its
    # label. Under the unfixed capture this label is 'Select... Yes No'.
    fm = lever_capture._parse_lever(html, "globex", "req-90", now=lambda: _PINNED)
    keys = [f.key for f in fm.fields]
    assert keys == ["cards[7f3a][field2]"]
    select = fm.fields[0]
    assert select.label == "Do you require visa sponsorship?"
    # CORRECTED IN PLACE (round 8), and this is a CORRECTION OF A PROVEN FALSEHOOD, not a
    # loosened bound. This line used to read
    #     assert select.locator.name == "Do you require visa sponsorship?"
    # which pinned the QUESTION as the value the fill would look the control up by. The
    # question is NOT the control's accessible name: it sits outside any <label> (see the
    # markup above -- the `.application-label` is a sibling DIV, and the control is
    # associated with nothing), so `get_by_role("combobox", name=<question>)` matches ZERO
    # elements. The suite was not failing to catch that phantom; it was PROTECTING it. What
    # the fill actually resolves by is the SUBMISSION NAME, which the capture records as
    # `Field.key` -- so THAT is what is pinned now, as the locator that RESOLVES:
    assert select.key == "cards[7f3a][field2]"
    page = _DomBackedPage(html)
    assert _match_count(lever_fill._locate(page, _drivable_value(select))) == 1
    # ...and the phantom, stated as a fact of this markup rather than left implicit: the
    # question-as-accessible-name locator finds NOTHING here, exactly as it finds nothing
    # live. `locator.name` still carries the question (it is the field's human label and
    # the fallback locator's name), but no live control answers to it.
    assert _match_count(page.get_by_role(select.locator.role,
                                         name=select.locator.name)) == 0
    assert select.type == "multi_value_single_select"
    assert select.locator.role == "combobox"
    assert select.required is True
    assert select.options == ["Yes", "No"]


def test_lever_dedup_by_key_keeps_the_richer_parse_not_the_first():
    """THE TIE-BREAK RULE ITSELF (the second line of defence), pinned directly on
    `_dedup_by_key`: when two parses collide on one submission key, the RICHER parse
    survives -- a click widget (radio/checkbox) outranks a typed parse, then the
    richer type rank, and only then document order.

    HONEST SCOPE, stated up front: these collisions are NOT produced by any live
    Lever page today, because `_card_owned_field_containers` now removes the
    base/custom duplicate at the source (see the test above, which IS the live path).
    This test pins a CODE CONTRACT, not a page shape, and it is deliberately a unit
    test on the function rather than a fabricated DOM: inventing markup Lever does
    not serve, in order to reach a defensive branch, is the failure mode that cost
    this wave two rounds. The rule is worth pinning because it is the backstop for
    the residual collision the base pass can still produce on its own (an invisible
    mirror input sharing a submission name with a visible control), and because
    "first wins" -- an ordering accident, not a decision -- is what silently
    discarded the correct parse of the live agicap radio group.
    """
    def _fld(key, label, ftype, role, required, options):
        return Field(key=key, label=label, type=ftype, required=required,
                     options=options, source=LEVER_SOURCE,
                     locator=Locator(role=role, name=label), step_index=0,
                     conditional_on=None)

    key = "cards[7f3a][field0]"
    # the phantom the base pass produces for a card-owned radio group: a textbox,
    # labelled with the option wordings mashed together, with no options.
    phantom = _fld(key, "Native Professional Beginner", "input_text", "textbox",
                   True, [])
    # the real control, as the card pass sees it.
    radio = _fld(key, "What is your level of French?", "multi_value_single_select",
                 "radio", True, ["Native", "Professional", "Beginner"])

    # (a) PHANTOM FIRST: the richer parse still wins. This is the exact ordering
    # that shipped (base fields precede custom fields in `_parse_lever`) and the
    # exact ordering under which first-wins loses the radio.
    kept = lever_capture._dedup_by_key([phantom, radio])
    assert len(kept) == 1
    assert kept[0].locator.role == "radio"
    assert kept[0].label == "What is your level of French?"
    assert kept[0].options == ["Native", "Professional", "Beginner"]

    # (b) ORDER-INDEPENDENT: reversing the inputs keeps the same winner, so the rule
    # is a decision about richness and not a second ordering accident.
    kept = lever_capture._dedup_by_key([radio, phantom])
    assert len(kept) == 1
    assert kept[0].locator.role == "radio"
    assert kept[0].label == "What is your level of French?"

    # (c) THE WIDGET-ROLE TERM IS LOAD-BEARING, not decoration: when the two parses
    # tie on type rank (both option-bearing single-selects), the CLICK WIDGET still
    # outranks the typed parse. Without that term the tie falls through to document
    # order and the radio is demoted to a combobox the fill would try to type into.
    combobox_phantom = _fld(key, "Native Professional Beginner",
                            "multi_value_single_select", "combobox", False,
                            ["Native", "Professional", "Beginner"])
    kept = lever_capture._dedup_by_key([combobox_phantom, radio])
    assert len(kept) == 1
    assert kept[0].locator.role == "radio"
    assert kept[0].label == "What is your level of French?"

    # (d) `required` is the OR across duplicates (a collapse must never NARROW
    # requiredness), and an empty label is backfilled from the loser.
    unlabelled_radio = _fld(key, "", "multi_value_single_select", "radio", False,
                            ["Native"])
    required_phantom = _fld(key, "Native", "input_text", "textbox", True, [])
    kept = lever_capture._dedup_by_key([required_phantom, unlabelled_radio])
    assert len(kept) == 1
    assert kept[0].locator.role == "radio"
    assert kept[0].required is True
    assert kept[0].label == "Native"

    # (e) KEY ORDER is preserved: collapsing a duplicate must not reshuffle the map.
    other = _fld("name", "Full name", "input_text", "textbox", True, [])
    kept = lever_capture._dedup_by_key([phantom, other, radio])
    assert [f.key for f in kept] == [key, "name"]


def test_lever_dedup_widget_role_protects_a_checkbox_group_not_just_a_radio():
    """CLAUSE 1 OF THE TIE-BREAK, FOR THE OTHER CLICK WIDGET. `_WIDGET_ROLES` is
    {radio, checkbox}, and until round 6 only its `radio` member was reachable:
    `_control_role` never emitted role `checkbox` for a GROUP, so the `checkbox` member
    protected nothing and dropping it from the set passed the whole suite. Now that a
    checkbox group is captured as a checkbox (see
    test_lever_required_checkbox_group_captured_as_checkbox_not_a_phantom_listbox), the
    member is real, and this pins it: a click widget must never be demoted to a phantom
    the fill would type into or select from, whichever click widget it is.

    HONEST SCOPE, as for the sibling tie-break test below: `_card_owned_field_containers`
    removes the base/custom duplicate AT THE SOURCE, so this collision is not produced by
    any live Lever page today. This is a CODE CONTRACT pinned as a unit test on the
    function, deliberately NOT a fabricated DOM (inventing markup Lever does not serve, to
    reach a defensive branch, is the failure mode that cost this wave two rounds). It is
    worth pinning because a tie-break that silently protects one control type and not the
    other is exactly how the round-6 regression happened one layer up.
    """
    def _fld(key, label, ftype, role, required, options):
        return Field(key=key, label=label, type=ftype, required=required,
                     options=options, source=LEVER_SOURCE,
                     locator=Locator(role=role, name=label), step_index=0,
                     conditional_on=None)

    key = "cards[7f3a][field0]"
    options = ["Days", "Nights"]
    # The two parses TIE on type rank (same type, both options-bearing), so the
    # widget-role term is the ONLY thing that can decide: without it the tie falls
    # through to document order and the phantom `listbox` -- the role no live element
    # answers to -- survives, mislabelled with the option wordings mashed together.
    phantom = _fld(key, "Days Nights", "multi_value_multi_select", "listbox",
                   True, options)
    group = _fld(key, "What is your availability?", "multi_value_multi_select",
                 "checkbox", True, options)

    kept = lever_capture._dedup_by_key([phantom, group])
    assert len(kept) == 1
    assert kept[0].locator.role == "checkbox"
    assert kept[0].label == "What is your availability?"

    # order-independent: a decision about richness, not a second ordering accident
    kept = lever_capture._dedup_by_key([group, phantom])
    assert len(kept) == 1
    assert kept[0].locator.role == "checkbox"

    # the term itself: the click widget scores on clause 1, the phantom does not, and
    # they are otherwise equal.
    assert lever_capture._field_richness(group)[0] == 1
    assert lever_capture._field_richness(phantom)[0] == 0
    assert lever_capture._field_richness(group) > lever_capture._field_richness(phantom)


def test_lever_type_rank_prefers_the_options_bearing_parse():
    """CLAUSE 2 OF THE TIE-BREAK, pinned on BOTH consumers of `_lever_type_rank`
    (`_field_richness` for the whole-map dedup, `_richer_lever_type` for the base
    mirror/twin merge): an options-bearing parse outranks a plain-text one, so a parse
    that actually SAW the control's choices is never discarded for one that read a
    hidden mirror as a textbox.

    Same honest scope as the sibling tie-break tests: a CODE CONTRACT pinned as a unit
    test on the functions, not a fabricated DOM. `_control_type` returns `input_text`
    for ANY input it does not recognize, `type=hidden` included, so rank 0 is precisely
    what a mirror parse scores and this clause is what keeps it losing.
    """
    def _fld(key, label, ftype, role, required, options):
        return Field(key=key, label=label, type=ftype, required=required,
                     options=options, source=LEVER_SOURCE,
                     locator=Locator(role=role, name=label), step_index=0,
                     conditional_on=None)

    key = "cards[7f3a][field0]"
    options = ["Yes", "No"]

    # (a) `_field_richness` / `_dedup_by_key`: NEITHER parse is a click widget, so
    # clause 1 ties at 0 and the TYPE RANK alone decides. The phantom is FIRST, which is
    # the shipped order (base fields precede custom fields in `_parse_lever`), so a rank
    # that stopped ranking options-bearing types top would hand the key to the phantom.
    phantom = _fld(key, "Select... Yes No", "input_text", "textbox", True, [])
    select = _fld(key, "Do you require visa sponsorship?", "multi_value_single_select",
                  "combobox", True, options)
    assert lever_capture._lever_type_rank(select) > lever_capture._lever_type_rank(phantom)

    kept = lever_capture._dedup_by_key([phantom, select])
    assert len(kept) == 1
    assert kept[0].type == "multi_value_single_select"
    assert kept[0].locator.role == "combobox"
    assert kept[0].label == "Do you require visa sponsorship?"
    assert kept[0].options == options

    # (b) `_richer_lever_type`: the base merge of one key's duplicates. The hidden mirror
    # is FIRST in document order (the live round-3 shape), and the visible twin is the
    # control that carries the options; the merge must take its type AND its options.
    # `_RawBaseField.container` / `.control` are the parse's own DOM nodes and are not
    # read by the merge (it ranks on `.type` / `.options` alone), so they are None here.
    def _raw(ftype, options):
        return lever_capture._RawBaseField(
            key=key, label="", type=ftype, required=False, options=options,
            container=None, control=None)

    mirror = _raw("input_text", [])                       # a hidden mirror: rank 0
    twin = _raw("multi_value_single_select", options)     # the real control: rank 4
    assert lever_capture._richer_lever_type([mirror, twin]) == (
        "multi_value_single_select", options)


def test_lever_fixture_carries_the_live_lever_card_shape():
    """THE FIXTURE'S LIVE-SHAPE CORRECTIONS, PINNED SO THEY CANNOT SILENTLY ROT.

    dom.html was corrected in round 5 to the markup Lever actually serves, and that
    correction is what finally made the suite reproduce the live base/custom key
    collision. But NOTHING asserted the corrected shape itself, so a later "cleanup"
    could un-nest a card or drop a hidden mirror, revert the fixture to a friendlier
    shape than the live page, and stay green -- re-arming the exact defect this wave
    took three rounds to close. This test asserts the two live-DOM invariants directly,
    so undoing either one FAILS rather than passes quietly.

    Both were re-derived from live Lever DOM (agicap, gopuff, swile, nium, binance;
    2026-07-13), not inferred.
    """
    tree = lever_capture._build_tree(_dom_html())
    cards = lever_capture._find_all(
        tree, lambda n: lever_capture._has_class(n, "application-question"))
    # 3 QUESTION cards (select, EEO gender, and the ONE consent card that holds BOTH
    # consent checkboxes -- round 10: live swile renders them in a single card, under two
    # DIFFERENT submission names, and that card still emits TWO fields, one per name, which
    # `test_lever_one_card_two_submission_names_emits_one_field_per_name` pins) + the 2
    # control-less FURNITURE cards the live pages carry (round 8: the LinkedIn OAuth widget
    # and the legitimate-interest privacy copy). The furniture is part of the live shape and
    # the fixture must carry it: modelling only answerable cards is exactly why the suite
    # stayed green while the shipped capture RAISED on 3 of the 4 live postings.
    assert len(cards) == 5
    furniture = [c for c in cards if not lever_capture._card_controls(c)]
    assert len(furniture) == 2
    assert any(lever_capture._has_class(c, "awli-application-row") for c in furniture)
    assert any(lever_capture._first(c, lambda n: (
        (n.attrs.get("data-qa") or "") == "legitimate-interest-copy")) is not None
        for c in furniture)

    # (1) THE NESTED CONTROL WRAPPER. Every live Lever card wraps its control in an
    # `.application-field` div that is a SIBLING of the `.application-label` holding the
    # question (agicap 11/11 containers, gopuff 29/29, swile 20/20, nium 12/12). That
    # nesting is what makes BOTH parse passes see the control, which is the collision
    # `_card_owned_field_containers` exists to prevent. Un-nest a card and the base pass
    # goes blind to it, the custom pass wins by default, and the exclusion stops being
    # exercised by that card at all.
    for card in cards:
        assert lever_capture._first(
            card, lambda n: lever_capture._has_class(n, "application-field")) is not None
    # 5, not 3: the furniture cards each wrap their button/copy in an `.application-field`
    # too (verbatim live shape). They own it, the base pass skips it, and it carries no
    # control anyway -- so the field map is unchanged either way. (One `.application-field`
    # per card, and the two consent checkboxes now share the ONE consent card's, exactly as
    # live swile renders them.)
    assert len(lever_capture._card_owned_field_containers(tree)) == 5
    # the consequence, stated as the base pass sees it: it owns ONLY the six standalone
    # base blocks, never a card's control.
    assert [f.key for f in lever_capture._lever_base_fields(tree)] == [
        "name", "email", "phone", "urls[LinkedIn]", "urls[GitHub]", "resume"]

    # (2) THE HIDDEN MIRROR. Live consent cards render a HIDDEN input carrying the
    # unticked value "0" under the SAME submission name, BEFORE the real checkbox. It is
    # not decoration: `_first(container, _is_form_control)` returns that hidden input, and
    # `_control_type` types any unrecognized input as `input_text`, which is exactly how
    # the live agicap consent box was captured as a phantom TEXTBOX. Strip the mirror and
    # the fixture stops exercising the trap.
    for key in ("consent[marketing]", _CONSENT_KEY):
        inputs = [n for n in lever_capture._find_all(tree, lever_capture._is_form_control)
                  if lever_capture._control_name(n) == key]
        assert [(n.attrs.get("type") or "").lower() for n in inputs] == [
            "hidden", "checkbox"], key
        assert [n.attrs.get("value") for n in inputs] == ["0", "1"], key

    # and the capture still comes out right THROUGH the trap: a checkbox, not a textbox.
    privacy = {f.key: f for f in _fieldmap().fields}[_CONSENT_KEY]
    assert privacy.type == "boolean"
    assert privacy.locator.role == "checkbox"
    assert privacy.required is True


def test_lever_plain_base_field_is_untouched_by_the_card_exclusion():
    """THE NEGATIVE for both halves of the round-5 fix: a key that only ONE pass
    emits must come through completely unchanged.

    Two things could have gone wrong and neither may: (1) the card exclusion must
    skip only the containers a card OWNS, so a STANDALONE `.application-field` is
    still parsed by the base pass (over-broad exclusion would silently drop base
    fields off the map, which the sweep would then report as unanswerable gaps);
    (2) the richness tie-break must not touch a key with no collision at all -- no
    relabelling, no retyping, no requiredness change.
    """
    html = (
        "<html><body><form>"
        "<ul class=\"application-fields\">"
        "<li class=\"application-field\">"
        "<div class=\"application-label\">Full name<span class=\"required\">&#10033;</span></div>"
        "<input type=\"text\" name=\"name\" required>"
        "</li>"
        "<li class=\"application-field\">"
        "<div class=\"application-label\">Current company</div>"
        "<input type=\"text\" name=\"org\">"
        "</li>"
        "</ul>"
        "<li class=\"application-question custom-question\"><div>"
        "<div class=\"application-label\"><div class=\"text\">"
        "Why Globex?<span class=\"required\">&#10033;</span></div></div>"
        "<div class=\"application-field full-width required-field\">"
        "<textarea name=\"cards[7f3a][field1]\" required></textarea>"
        "</div></div></li>"
        "</form></body></html>")

    tree = lever_capture._build_tree(html)
    # the standalone containers are NOT card-owned; only the card's nested one is
    assert len(lever_capture._card_owned_field_containers(tree)) == 1
    assert [f.key for f in lever_capture._lever_base_fields(tree)] == ["name", "org"]

    fm = lever_capture._parse_lever(html, "globex", "req-91", now=lambda: _PINNED)
    by_key = {f.key: f for f in fm.fields}
    assert set(by_key) == {"name", "org", "cards[7f3a][field1]"}

    # the uncollided base fields are byte-for-byte what the base pass parsed
    assert by_key["name"].label == "Full name"
    assert by_key["name"].type == "input_text"
    assert by_key["name"].locator.role == "textbox"
    assert by_key["name"].locator.name == "Full name"
    assert by_key["name"].required is True
    assert by_key["name"].options == []
    # and an OPTIONAL base field is not silently promoted to required by the OR
    assert by_key["org"].label == "Current company"
    assert by_key["org"].required is False

    # the card's own control is still captured, from the card, with the question
    assert by_key["cards[7f3a][field1]"].label == "Why Globex?"
    assert by_key["cards[7f3a][field1]"].type == "textarea"
    assert by_key["cards[7f3a][field1]"].required is True


def test_lever_locator_role_is_derived_from_the_control_at_every_emission_site(
        monkeypatch):
    """THE ROLE INVARIANT, PINNED AT ALL THREE OF ITS EMISSION SITES (round 7).

    Rounds 3 and 6 each fixed one instance of ONE defect: a locator role derived from
    the TYPE rather than the CONTROL, so a widget the applicant TICKS was captured
    with a role the fill would TYPE into or SELECT from, `_needs_human_handoff` (which
    keys off exactly that role) did not fire, the fill DROVE it, and
    `get_by_role(<phantom>, name=...)` resolved to ZERO live elements: an opaque FILL
    ERROR. Round 6 then declared the invariant universally -- but enforced it at only
    TWO of its THREE emission sites. `_merge_lever_base_group` still read
    `_role_for_type(field_type)`, so the SAME defect was sitting, unpinned in either
    direction, at the site nobody had looked at. It was harmless only because no live
    Lever page routes a click widget through the base pass (48/48 pages at round 6,
    18/18 re-derived at round 7: the base pass emits NOTHING live, every
    `.application-field` being card-owned). That is luck, and W5.1c will build on this
    code.

    So this test does not pin the fix, it pins the INVARIANT, and it is deliberately
    written so that a FOURTH emission site cannot be added without failing it.

    Site 1 carries the LIVE MIRROR TRAP, and it is why `_base_group_control` exists
    rather than the one-line `items[0].control`. A base group is the duplicate parses
    of ONE submission key, and Lever renders an INVISIBLE `type=hidden` mirror FIRST,
    ahead of its visible twin (round-3 finding). `items[0]` is therefore the MIRROR:
    neither a radio nor a checkbox, so the role would fall straight back through to
    `_role_for_type` and the phantom would return through the back door. The role must
    come from the control the applicant OPERATES.
    """
    html = (
        "<html><body><form>"
        "<ul class=\"application-fields\">"
        # SITE 1 -- the BASE pass: a required radio group in a STANDALONE
        # `.application-field` (no card owns it), behind its hidden mirror.
        "<li class=\"application-field\">"
        "<input type=\"hidden\" name=\"shift\" value=\"\">"
        "</li>"
        "<li class=\"application-field\">"
        "<div class=\"application-label\">Which shift?<span class=\"required\">&#10033;</span></div>"
        "<ul data-qa=\"multiple-choice\">"
        "<li><label><input type=\"radio\" name=\"shift\" value=\"day\" required>"
        "<span class=\"application-answer-alternative\">Day</span></label></li>"
        "<li><label><input type=\"radio\" name=\"shift\" value=\"night\" required>"
        "<span class=\"application-answer-alternative\">Night</span></label></li>"
        "</ul>"
        "</li>"
        "</ul>"
        # SITE 2 -- the CUSTOM pass: the live Lever card shape.
        "<li class=\"application-question custom-question\"><div>"
        "<div class=\"application-label full-width multiple-choice\"><div class=\"text\">"
        "What is your level of French?<span class=\"required\">&#10033;</span></div></div>"
        "<div class=\"application-field full-width required-field\">"
        "<ul data-qa=\"multiple-choice\">"
        "<li><label><input type=\"radio\" name=\"cards[7f3a][field0]\" value=\"native\""
        " required><span class=\"application-answer-alternative\">Native</span></label></li>"
        "<li><label><input type=\"radio\" name=\"cards[7f3a][field0]\" value=\"pro\""
        " required><span class=\"application-answer-alternative\">Professional</span>"
        "</label></li>"
        "</ul></div></div></li>"
        # SITE 3 -- the STRAY pass: a REQUIRED radio outside BOTH containers.
        "<div class=\"custom-widget\">"
        "<label for=\"ol\">Onsite only</label>"
        "<input type=\"radio\" id=\"ol\" name=\"onsite\" value=\"yes\" required>"
        "</div>"
        "</form></body></html>")

    tree = lever_capture._build_tree(html)

    # (1) THE THREE SITES, ENUMERATED as the three passes that construct a Locator.
    # Each emits exactly the one field it owns, so all three are genuinely exercised
    # below and none is silently vacant (a vacant pass would pin nothing).
    assert [f.key for f in lever_capture._lever_base_fields(tree)] == ["shift"]
    assert [f.key for f in lever_capture._lever_custom_fields(tree, "globex", "req-93")] \
        == ["cards[7f3a][field0]"]
    assert [f.key for f in lever_capture._lever_stray_required_fields(tree)] == ["onsite"]

    # (2) NO SITE MAY BYPASS `_control_role`. With it stubbed, EVERY captured role is
    # the stub's -- so a fourth emission site reaching `_role_for_type` (or hardcoding
    # a role) fails here rather than shipping the next phantom. This is the pin that
    # survives refactors: it constrains the code's SHAPE, not just today's outputs.
    monkeypatch.setattr(lever_capture, "_control_role",
                        lambda control, field_type: "SENTINEL")
    stubbed = lever_capture._parse_lever(html, "globex", "req-93", now=lambda: _PINNED)
    assert {f.locator.role for f in stubbed.fields} == {"SENTINEL"}
    monkeypatch.undo()

    # (3) THE INVARIANT ITSELF, at each site: a control the applicant TICKS is captured
    # with the role of the CONTROL, never the role its TYPE would imply -- and the real
    # FROZEN kernel therefore HANDS IT OFF instead of driving it into a phantom locator.
    from engine.kernel.contracts import FieldValue
    from engine.kernel.fill_toolkit import _needs_human_handoff

    fm = lever_capture._parse_lever(html, "globex", "req-93", now=lambda: _PINNED)
    by_key = {f.key: f for f in fm.fields}
    assert set(by_key) == {"shift", "cards[7f3a][field0]", "onsite"}

    for key, phantom in (("shift", "textbox"),                  # site 1, base
                         ("cards[7f3a][field0]", "combobox"),   # site 2, custom
                         ("onsite", "textbox")):                # site 3, stray
        fld = by_key[key]
        # the role the TYPE would have given is a role NO element on the page has:
        # that is the defect, and it is what each site emitted before it was fixed.
        assert lever_capture._role_for_type(fld.type) == phantom, key
        assert fld.locator.role == "radio", key
        # ...so the frozen kernel hands the control off. A NON-bool value on purpose:
        # a bool is handed off by `_needs_human_handoff`'s value branch whatever the
        # role, and would prove nothing about the role.
        fv = FieldValue(key=fld.key, label=fld.label, type=fld.type,
                        locator=fld.locator, value="Day")
        assert _needs_human_handoff(fv) is True, key
    assert "listbox" not in html and "<select" not in html

    # (4) SITE 1's label and requiredness survive the mirror too: the QUESTION (not the
    # option wordings mashed together), and required OR'd up from the visible twin.
    assert by_key["shift"].label == "Which shift?"
    assert by_key["shift"].required is True
    # and the mirror IS the trap this guards: `items[0]` for `shift` is the hidden
    # input, so a role read off it would fall back to the type and phantom again.
    mirrored = [n for n in lever_capture._find_all(tree, lever_capture._is_form_control)
                if lever_capture._control_name(n) == "shift"]
    assert (mirrored[0].attrs.get("type") or "").lower() == "hidden"


def test_lever_stray_required_fields_skips_a_required_hidden_control():
    """`_lever_stray_required_fields`'s hidden-skip, PINNED (round 12).

    This vendor renders HIDDEN TWINS: a base field's true submission name
    often sits on an invisible `type=hidden` mirror alongside its visible
    counterpart, and that mirror can itself be marked required (natively or
    via `aria-required="true"`) while living OUTSIDE both known containers,
    exactly like the genuinely stray radio below. Without the hidden-skip,
    the sweep in `_lever_stray_required_fields` would enumerate that mirror
    as a "stray required field", polluting the completeness census with a
    control the applicant never sees or operates -- the same class of
    phantom the role invariant above guards against, at a different site.
    """
    html = (
        "<html><body><form>"
        # A REQUIRED hidden control outside BOTH known containers
        # (`.application-field` and `.application-question`). This is the
        # shape the skip exists for: caught by the stray sweep's required
        # selector, never applicant-facing.
        "<div class=\"custom-widget\">"
        "<input type=\"hidden\" name=\"internal_ref\" aria-required=\"true\""
        " value=\"x\">"
        "</div>"
        # A VISIBLE required stray control, so the sweep pass is genuinely
        # exercised and this test is not vacuously empty.
        "<div class=\"custom-widget\">"
        "<label for=\"ol\">Onsite only</label>"
        "<input type=\"radio\" id=\"ol\" name=\"onsite\" value=\"yes\" required>"
        "</div>"
        "</form></body></html>")

    tree = lever_capture._build_tree(html)
    keys = [f.key for f in lever_capture._lever_stray_required_fields(tree)]
    assert keys == ["onsite"]
    assert "internal_ref" not in keys


def test_lever_base_group_control_is_the_click_widget_not_the_hidden_mirror():
    """`_base_group_control`, BOTH ARMS -- the helper that decides WHICH of a base
    group's controls the role is read off (round 7).

    HONEST SCOPE, stated first, exactly as the sibling `_dedup_by_key` /
    `_field_richness` tests state theirs: the base pass emits NOTHING on any live
    Lever page (18/18 re-derived at round 7, 48/48 at round 6: every live
    `.application-field` is card-owned), so these collisions are a CODE CONTRACT, not
    a page shape. It is pinned as a unit test on the function rather than a fabricated
    DOM, because inventing markup Lever does not serve in order to reach a defensive
    branch is the failure mode that cost this wave two rounds.

    It is worth pinning for the reason the round-6 regression exists at all: a helper
    that protects ONE click widget and not the OTHER is how this wave broke the
    checkbox group immediately after fixing the radio. The radio arm alone is
    reachable through the whole-DOM test above; the CHECKBOX arm is reachable only
    here, and an unpinned arm protects nothing. `_WIDGET_ROLES` losing its `checkbox`
    member passed the entire suite for exactly this reason until round 6 made it real.
    """
    html = ("<form>"
            "<input type=\"hidden\" name=\"x\" value=\"0\">"
            "<input type=\"checkbox\" name=\"x\" value=\"1\">"
            "<input type=\"radio\" name=\"x\" value=\"y\">"
            "<select name=\"x\"><option value=\"a\">A</option></select>"
            "</form>")
    tree = lever_capture._build_tree(html)
    mirror, checkbox, radio, select = lever_capture._find_all(
        tree, lever_capture._is_form_control)

    def _raw(control, ftype, options):
        return lever_capture._RawBaseField(
            key="x", label="", type=ftype, required=False, options=options,
            container=None, control=control)

    m = _raw(mirror, "input_text", [])                        # the hidden mirror: FIRST
    cb = _raw(checkbox, "boolean", [])
    rd = _raw(radio, "input_text", [])
    sel = _raw(select, "multi_value_single_select", ["A"])    # the RICHEST parse

    # THE CHECKBOX ARM. The richest parse is the select, so the TYPE says `combobox`
    # -- but the control the applicant TICKS is the checkbox, so the ROLE is
    # `checkbox`. Reading the role off `items[0]` (the hidden mirror) would fall
    # through to the type and emit the phantom; dropping the checkbox arm would too.
    items = [m, cb, sel]
    field_type, _ = lever_capture._richer_lever_type(items)
    assert field_type == "multi_value_single_select"
    assert lever_capture._role_for_type(field_type) == "combobox"      # the phantom
    assert lever_capture._base_group_control(items) is checkbox
    assert lever_capture._control_role(
        lever_capture._base_group_control(items), field_type) == "checkbox"

    # THE RADIO ARM, and its precedence over the checkbox (as `_lever_custom_field`
    # picks its own `primary`: radios first, then checkboxes).
    assert lever_capture._base_group_control([m, rd]) is radio
    assert lever_capture._base_group_control([m, cb, rd]) is radio

    # THE NON-CLICK FALLTHROUGH is safe on `items[0]` even though `items[0]` is the
    # hidden mirror, and this is the whole reason the helper does not need to hunt a
    # "best" non-click control: `_control_role` answers from the TYPE there, and the
    # type is already the RICHEST parse's. A `<select>` twin behind a mirror still
    # comes out `combobox`.
    assert lever_capture._base_group_control([m, sel]) is mirror
    assert lever_capture._control_role(mirror, "multi_value_single_select") == "combobox"


def test_lever_select_options_drop_the_placeholder_but_keep_a_real_empty_value_option():
    """BOTH HALVES of `_select_options`'s placeholder rule (round 7).

    The rule the code states is "the Select... placeholder is not a real option", and
    only ONE half of it was pinned: nothing asserted that a GENUINE empty-value option
    SURVIVES. A mutation that dropped EVERY empty-value option -- silently shrinking
    the choices the resolve layer may render -- therefore passed the whole suite.

    An empty `value` alone is not a placeholder. `<option value="">None of the
    above</option>` is a real answer an applicant can pick; it is the WORDING that
    distinguishes it from `<option value="">Select...</option>` and from the bare
    `<option value=""></option>` Lever renders as a spacer. All three are here, and
    the rule must separate them.
    """
    html = (
        "<html><body><form>"
        "<div class=\"application-question dropdown\">"
        "<div class=\"application-label\"><div class=\"text\">Where are you based?"
        "<span class=\"required\">&#10033;</span></div></div>"
        "<div class=\"application-field\"><div class=\"application-dropdown\">"
        "<select name=\"cards[7f3a][field0]\" required>"
        "<option value=\"\">Select...</option>"          # the placeholder: DROPPED
        "<option value=\"\"></option>"                   # a bare spacer: DROPPED
        "<option value=\"uk\">United Kingdom</option>"   # a normal option: KEPT
        "<option value=\"\">None of the above</option>"  # a REAL empty-value option: KEPT
        "</select></div></div></div>"
        "</form></body></html>")
    fm = lever_capture._parse_lever(html, "globex", "req-94", now=lambda: _PINNED)
    options = fm.fields[0].options

    # half 1: the placeholder and the bare spacer are not real options.
    assert not any(o.lower().startswith("select") for o in options)
    assert "" not in options
    # half 2: a genuine empty-value option with real wording SURVIVES. Dropping every
    # empty-value option passes half 1 and fails here, which is the point.
    assert "None of the above" in options
    assert options == ["United Kingdom", "None of the above"]


def test_lever_card_requiredness_is_read_off_the_primary_widget_not_the_first_control(
        tmp_path):
    """CARD REQUIREDNESS FOLLOWS THE PRIMARY WIDGET, not `controls[0]` (round 7).

    The Field a card emits IS its primary widget: the key, the locator and the options
    all come from `primary`. Its `required` must therefore describe the SAME control.
    Reading it off `controls[0]` described a DIFFERENT control the moment a card's
    first control was not its primary widget, and the two readings were untested and
    interchangeable (mutant i6 swapped them and passed the whole suite) because on
    every live Lever card they COINCIDE: the group's own controls are the only
    non-hidden ones in the card, so `controls[0] is primary`.

    They diverge on a card that offers a free-text "other" box alongside its group.
    Both directions are pinned, because "read it off the primary" must be a rule, not
    a one-way ratchet that ORs everything in the card into requiredness:

      card A -- optional text box, then a REQUIRED radio group. `controls[0]` says
        OPTIONAL, so the required group would be captured OPTIONAL: a required field
        silently demoted, which is the harmful direction.
      card B -- REQUIRED text box, then an OPTIONAL radio group. `controls[0]` says
        REQUIRED, so the optional group would be captured REQUIRED: a permanent false
        gap that can never be closed.

    Neither card carries a `.required` marker span, which is what makes the two
    readings genuinely diverge (the span ORs requiredness back in regardless of which
    control `_is_required` is handed, and is why this never bit live).

    The last assertion is the honest one: card B's REQUIRED text box is captured
    NOWHERE (a card emits ONE field, and this pass has always been that way -- it is
    the known multi-control-card limitation, out of this wave's scope). This test pins
    that the LIVE DOM SWEEP still names it, so it still forces NOT_COMPLETE. Reading
    requiredness off the primary loses no coverage: the sweep is the backstop, and it
    keeps biting.
    """
    html = (
        "<html><body><form>"
        # card A: an OPTIONAL free-text box precedes a REQUIRED radio group.
        "<li class=\"application-question custom-question\"><div>"
        "<div class=\"application-label full-width multiple-choice\"><div class=\"text\">"
        "Which shift do you want?</div></div>"
        "<div class=\"application-field full-width\">"
        "<input type=\"text\" name=\"cards[7f3a][otherA]\" placeholder=\"Other\">"
        "<ul data-qa=\"multiple-choice\">"
        "<li><label><input type=\"radio\" name=\"cards[7f3a][field0]\" value=\"day\""
        " required><span class=\"application-answer-alternative\">Day</span></label></li>"
        "<li><label><input type=\"radio\" name=\"cards[7f3a][field0]\" value=\"night\""
        " required><span class=\"application-answer-alternative\">Night</span></label></li>"
        "</ul></div></div></li>"
        # card B: a REQUIRED free-text box precedes an OPTIONAL radio group.
        "<li class=\"application-question custom-question\"><div>"
        "<div class=\"application-label full-width multiple-choice\"><div class=\"text\">"
        "Anything else?</div></div>"
        "<div class=\"application-field full-width\">"
        # no placeholder and no aria-label, exactly as live: the kernel sweep's
        # accessible name for it therefore FALLS BACK to the `name` attribute (the F2
        # key-space finding), which is the alias the gap below is reported under.
        "<input type=\"text\" name=\"cards[7f3a][otherB]\" required>"
        "<ul data-qa=\"multiple-choice\">"
        "<li><label><input type=\"radio\" name=\"cards[7f3a][field1]\" value=\"yes\">"
        "<span class=\"application-answer-alternative\">Yes</span></label></li>"
        "<li><label><input type=\"radio\" name=\"cards[7f3a][field1]\" value=\"no\">"
        "<span class=\"application-answer-alternative\">No</span></label></li>"
        "</ul></div></div></li>"
        "</form></body></html>")

    # premise: on BOTH cards the first control is NOT the primary widget, and no
    # `.required` marker span papers over the difference.
    tree = lever_capture._build_tree(html)
    cards = lever_capture._find_all(
        tree, lambda n: lever_capture._has_class(n, "application-question"))
    for card in cards:
        controls = [n for n in lever_capture._find_all(card, lever_capture._is_form_control)
                    if (n.attrs.get("type") or "").lower() != "hidden"]
        assert (controls[0].attrs.get("type") or "").lower() == "text"
        assert lever_capture._is_radio(controls[1])
        assert lever_capture._first(
            card, lambda n: lever_capture._has_class(n, "required")) is None

    fm = lever_capture._parse_lever(html, "globex", "req-95", now=lambda: _PINNED)
    by_key = {f.key: f for f in fm.fields}

    # each card emits ONE field, and it is the field of its PRIMARY widget: the radio
    # group's key, the radio group's role, the radio group's options.
    assert set(by_key) == {"cards[7f3a][field0]", "cards[7f3a][field1]"}
    group_a, group_b = by_key["cards[7f3a][field0]"], by_key["cards[7f3a][field1]"]
    for group in (group_a, group_b):
        assert group.locator.role == "radio"
        assert group.type == "multi_value_single_select"
    assert group_a.options == ["Day", "Night"]

    # card A: the REQUIRED radio group is captured REQUIRED. `controls[0]` (the
    # optional text box) says otherwise, which is the demotion this pins against.
    assert group_a.required is True
    # card B: the OPTIONAL radio group stays OPTIONAL. Requiredness follows the primary
    # in BOTH directions; it is not an OR over every control the card happens to hold.
    assert group_b.required is False

    # THE BACKSTOP, unchanged: card B's required text box is in no captured field, and
    # the REAL kernel sweep over the REAL markup still names it -- so it is still an
    # unanswerable dom_only gap that forces NOT_COMPLETE. Nothing was silenced.
    from engine.providers import base
    page = _FakeLeverPage(fm, sweep_dom=html)
    dom_required = base.sweep_required(page)
    assert "cards[7f3a][otherb]" in dom_required
    assert not any(f.key == "cards[7f3a][otherB]" for f in fm.fields)

    values = _resolved_values(fm, tmp_path=tmp_path)
    report = lever.fill(page, fm, values)
    assert "dom-sweep:cards[7f3a][otherb]" in {g["key"] for g in report.required_unfilled}
    assert report.complete is False


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


# =============================================================================
# THE LOCATOR-RESOLUTION INVARIANT (W5B-LEVER round 8)
# =============================================================================
# Every fake page above BUILDS a control for whatever (role, name) the field map
# declares, so a locator aimed at a control that does not exist can never miss. That
# is why five rounds of tests stayed green over a locator that resolved to ZERO
# elements on the live page. The page below does the opposite: it RESOLVES every
# locator against the REAL parsed DOM the field map was captured from, and answers
# `.count()` honestly. A phantom counts zero here, exactly as it counts zero live.
#
# It is a shim, not a browser, and its two resolution engines are deliberately narrow:
#   * the CSS engine understands EXACTLY the grammar `lever.fill._name_css` emits and
#     ASSERTS on anything else, so a drift in the production selector FAILS here rather
#     than passing quietly;
#   * the role/accessible-name engine implements the accname order that decides this
#     bug (aria-label > associated or wrapping <label> > placeholder), which is what
#     makes a Lever custom question's control answer to its PLACEHOLDER and not to the
#     question. It is a documented approximation of Playwright's engine and is used ONLY
#     for the corroborating "the phantom finds nothing" assertions; the load-bearing
#     assertion (the production locator resolves to exactly one) goes through the CSS
#     engine and the live evidence.


def _norm_name(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def _css_clause(clause: str):
    """Parse ONE clause of the production selector: `<tag>[name="X"]` with an optional
    `:not([type="hidden"])`. Anything else is a selector this shim cannot honestly
    resolve, and it says so rather than silently matching nothing."""
    tag, _, rest = clause.strip().partition("[")
    assert tag in ("input", "select", "textarea"), f"unsupported selector tag: {clause!r}"
    assert rest.startswith('name="'), f"unsupported selector: {clause!r}"
    name, closed, tail = rest[len('name="'):].partition('"]')
    assert closed, f"unsupported selector: {clause!r}"
    assert tail in ("", ':not([type="hidden"])'), f"unsupported selector tail: {clause!r}"
    return tag, name.replace('\\"', '"').replace("\\\\", "\\"), tail != ""


def _css_matches(tree, css: str) -> list:
    matched: list = []
    for clause in css.split(", "):
        tag, name, skip_hidden = _css_clause(clause)
        for node in lever_capture._find_all(tree, lambda n: n.tag == tag):
            if (node.attrs.get("name") or "") != name:
                continue
            if skip_hidden and (node.attrs.get("type") or "").lower() == "hidden":
                continue
            if not any(node is seen for seen in matched):
                matched.append(node)
    return matched


_INPUT_TYPE_ROLES = {"checkbox": "checkbox", "radio": "radio", "file": "button",
                     "button": "button", "submit": "button"}


def _node_role(node) -> str:
    if node.tag == "select":
        return "listbox" if "multiple" in node.attrs else "combobox"
    if node.tag == "textarea":
        return "textbox"
    input_type = (node.attrs.get("type") or "text").lower()
    if input_type == "hidden":
        return ""                      # not in the accessibility tree at all
    return _INPUT_TYPE_ROLES.get(input_type, "textbox")


def _node_accessible_name(tree, node) -> str:
    """The control's ACCESSIBLE NAME, in the order that decides this bug: aria-label,
    then the <label> associated with it (`for=` or wrapping), then the placeholder. A
    Lever custom question's wording is in NEITHER of the first two -- it sits in a
    sibling DIV outside any label -- so the control answers to its placeholder."""
    aria = (node.attrs.get("aria-label") or "").strip()
    if aria:
        return aria
    node_id = (node.attrs.get("id") or "").strip()
    if node_id:
        associated = lever_capture._first(tree, lambda n: (
            n.tag == "label" and (n.attrs.get("for") or "").strip() == node_id))
        if associated is not None:
            text = _label_text(associated)
            if text:
                return text
    wrapping = lever_capture._first(tree, lambda n: (
        n.tag == "label"
        and any(c is node
                for c in lever_capture._find_all(n, lever_capture._is_form_control))))
    if wrapping is not None:
        text = _label_text(wrapping)
        if text:
            return text
    return (node.attrs.get("placeholder") or "").strip()


def _label_text(label) -> str:
    """A label's text as the ACCESSIBLE-NAME algorithm reads it: a form control EMBEDDED
    in the label contributes its VALUE, never its content -- so a label-wrapped `<select>`
    does NOT drag its whole option list into the accessible name of the field it labels
    (accname 1.2 step 2F; it is what the browser computes and what Playwright matches
    against). Reading the raw subtree text instead would make live swile's location select
    answer to "What is your location? Select... France United Kingdom" -- a fake-page
    artefact, not a DOM fact, and it would make the fallback look broken where it works."""
    if lever_capture._has_class(label, "required"):
        return ""
    if lever_capture._is_form_control(label):
        return ""
    parts = list(label.text_parts)
    parts.extend(_label_text(child) for child in label.children)
    return " ".join(part.strip() for part in parts if part and part.strip())


class _DomMatchSet:
    """The elements a locator matches on the real DOM. `.count()` is the honest answer:
    0 = a phantom the fill would never find, >1 = a control it could mistake."""

    def __init__(self, nodes):
        self.nodes = list(nodes)

    def count(self):
        return len(self.nodes)

    def and_(self, other):
        """Playwright's locator intersection: the elements BOTH locators match --
        the pattern `lever.fill._locate_option` (W5.1-R2) uses to narrow a
        page-wide role+name match down to the one option posting under a
        specific group's submission name."""
        return _DomMatchSet([n for n in self.nodes
                             if any(n is o for o in other.nodes)])


def _match_count(locator) -> int:
    return locator.count()


class _DomBackedPage:
    """A page that RESOLVES locators against a REAL parsed apply DOM (see the section
    note above). Serves the three calls the production fill path makes: `locator` (the
    submission-name CSS), `get_by_role` / `get_by_label` (the kernel fallback), and
    `query_selector_all` (the upload path's `_locate_file_input`)."""

    def __init__(self, html):
        self.tree = lever_capture._build_tree(html)
        self.requested = []

    def locator(self, css):
        self.requested.append(("css", css))
        return _DomMatchSet(_css_matches(self.tree, css))

    def get_by_role(self, role, name=None, exact=None):
        # `exact` is accepted (and ignored) because `lever.fill._locate_option`
        # passes it for a radio/checkbox group option lookup; `_norm_name` equality
        # below is already exact.
        self.requested.append(("role", role, name))
        want = _norm_name(name)
        return _DomMatchSet([
            n for n in lever_capture._find_all(self.tree,
                                               lever_capture._is_form_control)
            if _node_role(n) == role
            and _norm_name(_node_accessible_name(self.tree, n)) == want])

    def get_by_label(self, label):
        self.requested.append(("label", label))
        return self.get_by_role(None, name=label)

    def query_selector_all(self, selector):
        if "file" not in selector:
            return []
        return [_DomNodeLocator(n) for n in lever_capture._find_all(
            self.tree, lambda n: (n.tag == "input"
                                  and (n.attrs.get("type") or "").lower() == "file"))]


def _drivable_value(fld, value="x"):
    """The FieldValue the fill would drive this field with (the locator path keys off
    `key`, `locator` and the value's TYPE only)."""
    from engine.kernel.contracts import FieldValue
    return FieldValue(key=fld.key, label=fld.label, type=fld.type,
                      locator=fld.locator, value=value)


def test_lever_every_drivable_field_locator_resolves_to_exactly_one_control(tmp_path):
    """THE INVARIANT NOTHING IN THIS CODEBASE STATED, and the one that would have caught
    the phantom in rounds 3, 5, 6 AND 7 -- and catches the next one for free:

        for EVERY field the capture emits that the fill actually DRIVES, the locator the
        PRODUCTION fill code builds resolves to EXACTLY ONE element on the very DOM the
        field was captured from.

    EXACTLY one. ZERO is a phantom: the fill silently does nothing (or times out) and the
    field dies as an unexplained fill error, unfilled and unexplained -- which is what a
    truthful role with an untruthful NAME did to 14 live custom-question controls, 11 of
    them required. MORE THAN ONE is worse: the fill could type into the wrong control.

    Every field is accounted for, in the three ways the fill can treat one:
      * DRIVEN   -> `lever.fill._locate`, and the locator must resolve to exactly one;
      * HANDED OFF (a checkbox/radio: the hCaptcha hazard) -> no locator is ever built,
        so there is nothing to resolve. `_needs_human_handoff` is the production
        predicate, called here, not a re-statement of it;
      * UPLOADED  -> the kernel's `_locate_file_input`, its own locator, which must find
        the real file input.
    """
    from engine.kernel import fill_toolkit

    html = _dom_html()
    fm = lever_capture._parse_lever(html, "fauxcorp", "9001", now=lambda: _PINNED)
    page = _DomBackedPage(html)

    resolved, handed_off, uploaded = [], [], []
    for fld in fm.fields:
        fv = _drivable_value(fld, value=(tmp_path / "cv.pdf"
                                         if fld.type == "input_file" else "x"))
        if fill_toolkit._is_upload(fv):
            control = fill_toolkit._locate_file_input(page, fv)
            assert control is not None, f"{fld.key}: the upload path located no file input"
            assert control.get_attribute("name") == fld.key
            uploaded.append(fld.key)
            continue
        if fill_toolkit._needs_human_handoff(fv):
            handed_off.append(fld.key)
            continue
        count = _match_count(lever_fill._locate(page, fv))
        resolved.append((fld.key, count))
        assert count == 1, (
            f"{fld.key!r}: the production locator resolves to {count} elements on the "
            f"DOM this field was captured from (1 required: 0 is a phantom the fill "
            f"can never drive, >1 is a control it could fill by mistake)")

    # every field is classified, and the census is pinned so a field cannot quietly slip
    # out of the invariant by being reclassified into a bucket that asserts nothing.
    assert [key for key, _ in resolved] == [
        "name", "email", "phone", "urls[LinkedIn]", "urls[GitHub]",
        "cards[a1b2c3][field0]", "eeo[gender]"]
    assert handed_off == ["consent[marketing]", "consent[privacy]"]
    assert uploaded == ["resume"]
    assert len(resolved) + len(handed_off) + len(uploaded) == len(fm.fields)

    # THE PHANTOM, pinned as a fact of this same DOM: the card select's question is NOT
    # its accessible name, so the role+name locator that shipped through round 7 finds
    # NOTHING here -- while the submission-name locator finds it. This is the pair of
    # counts the fake pages above could never produce, because they build a control for
    # whatever name they are asked for.
    card = {f.key: f for f in fm.fields}["cards[a1b2c3][field0]"]
    assert _match_count(page.get_by_role(card.locator.role,
                                         name=card.locator.name)) == 0
    assert _match_count(page.locator(lever_fill._name_css(card.key))) == 1


def test_lever_control_less_question_card_is_furniture_not_a_capture_error():
    """A `.application-question` card with NO applicant-facing control is FURNITURE, and
    the capture SKIPS it -- it does not die on it.

    Until round 8 such a card raised CaptureShapeError, which killed the WHOLE capture:
    no FieldMap was produced at all, so the apply pipeline was dead before the fill was
    ever reached, on THREE of the four live postings (gopuff, swile, nium; verified by
    running the shipped `_parse_lever` over their live `page.content()`, 2026-07-14). The
    fixture modelled no such card, which is the only reason the suite stayed green.

    Both live shapes are pinned, verbatim (they are also in the fixture now):
      1. the "Apply with LinkedIn" OAuth widget (nium, swile): a card whose
         `.application-field` holds a `<button type=button>` and no control;
      2. the legitimate-interest privacy copy (gopuff, swile): a card holding only
         `<p data-qa="legitimate-interest-copy">`.
    Neither asks for a value and NEITHER CAN BE ANSWERED -- there is no control to type
    into, tick, or select from. Skipping them hides nothing: the falsification at the end
    proves a page that yields no fields AT ALL still raises, so this cannot become a
    silent empty map.
    """
    # (1) the fixture, which now carries both shapes, parses clean and emits no field for
    # the furniture -- while every real question still parses.
    fm = lever_capture._parse_lever(_dom_html(), "fauxcorp", "9001", now=lambda: _PINNED)
    keys = [f.key for f in fm.fields]
    assert "cards[a1b2c3][field0]" in keys and "consent[privacy]" in keys
    assert not any("linkedin" in k.lower() and k != "urls[LinkedIn]" for k in keys)
    assert not any("legitimate" in k.lower() or "interest" in k.lower() for k in keys)

    # (2) the VERBATIM live markup: both furniture cards beside a real custom question.
    html = (
        "<html><body><form><ul class=\"application-fields\">"
        "<li class=\"application-question awli-application-row\">"
        "<div class=\"application-label\">LinkedIn profile</div>"
        "<div class=\"application-field\"><div class=\"awli-button-container\">"
        "<button type=\"button\" class=\"template-btn-utility awli-button awli-v3 "
        "state-ready button-masked\">Apply with LinkedIn</button>"
        "</div></div></li>"
        "<li class=\"application-question custom-question\"><div>"
        "<div class=\"application-label full-width text\"><div class=\"text\">"
        "Current Street Address<span class=\"required\">&#10033;</span></div></div>"
        "<div class=\"application-field full-width required-field\">"
        "<input required=\"required\" class=\"card-field-input\" type=\"text\" "
        "placeholder=\"Type your response\" value=\"\" "
        "name=\"cards[d8c298f6][field0]\"></div></div></li>"
        "<li class=\"application-question\">"
        "<div class=\"application-field full-width\">"
        "<p data-qa=\"legitimate-interest-copy\">By applying for this position, your "
        "data will be processed as per Gopuff Privacy Policy.</p>"
        "</div></li>"
        "</ul></form></body></html>")

    fm2 = lever_capture._parse_lever(html, "gopuff", "fece6961", now=lambda: _PINNED)
    assert [f.key for f in fm2.fields] == ["cards[d8c298f6][field0]"]
    question = fm2.fields[0]
    assert question.label == "Current Street Address"
    assert question.required is True

    # ...and on that same markup, the locator invariant holds for the question that IS
    # asked. This is BLOCKING 2's live shape, stated as two counts: the control answers
    # to its PLACEHOLDER, never to the question, so the question-named locator is a
    # phantom and the submission-name locator is the one that resolves.
    page = _DomBackedPage(html)
    assert _match_count(lever_fill._locate(page, _drivable_value(question))) == 1
    assert _match_count(page.get_by_role("textbox", name="Current Street Address")) == 0
    assert _match_count(page.get_by_role("textbox", name="Type your response")) == 1

    # (3) THE FALSIFICATION: skipping furniture must never turn a broken page into a
    # silently empty field map. A page of furniture ALONE still raises.
    furniture_only = (
        "<html><body><form>"
        "<li class=\"application-question awli-application-row\">"
        "<div class=\"application-label\">LinkedIn profile</div>"
        "<div class=\"application-field\"><button type=\"button\">Apply with LinkedIn"
        "</button></div></li>"
        "</form></body></html>")
    with pytest.raises(CaptureShapeError, match="no recognizable form fields"):
        lever_capture._parse_lever(furniture_only, "nium", "2ae6fbf1", now=lambda: _PINNED)


# -- the submission-name locator's TWO GUARDS and its FALLBACK (round 10) ------------
# The invariant test above proves the locator finds exactly one control for every field of
# the fixture DOM. It does NOT prove the guards that MAKE that true are load-bearing: with
# `:not([type=hidden])` dropped from `_name_css`, and with `_resolves_to_one` weakened from
# `count() == 1` to `count() >= 1`, the whole suite stayed GREEN -- and composed, those two
# mutants let the fill drive a HIDDEN TWIN instead of the control the applicant sees. The
# fallback was dead code in the suite too: replaced by `raise AssertionError`, every test
# still passed, though it is the ONLY path by which swile's live `what_is_your_location`
# can be filled. The tests below pin each half against a LIVE Lever shape the primary
# fixture cannot show.

# The round-3 live finding, in its own DOM: a Lever base field renders TWICE, and the
# hidden mirror carries the true submission name (capture.py NOTE at :11-16).
_HIDDEN_TWIN_DOM = (
    "<html><body><form>"
    '<div class="application-field">'
    '<label for="phone-visible">Phone</label>'
    '<input type="hidden" name="phone" value="">'
    '<input type="text" id="phone-visible" name="phone">'
    "</div>"
    "</form></body></html>")

# Two APPLICANT-FACING controls answering to one submission name: neither is hidden, so no
# exclusion can separate them and the name cannot say WHICH control the fill means.
_TWO_VISIBLE_DOM = (
    "<html><body><form>"
    '<div class="application-field">'
    '<label for="phone-a">Phone</label>'
    '<input type="text" id="phone-a" name="phone">'
    '<input type="text" id="phone-b" name="phone">'
    "</div>"
    "</form></body></html>")

# LIVE swile, 2026-07-14, VERBATIM but for the country list (elided to two options): the
# demographic-survey location question. Its <select> carries NO `name` attribute at all, so
# the capture keys it by a SLUG of the question and no submission-name CSS can ever find
# it. It is the one driven control that lives or dies by the fallback.
_SWILE_LOCATION_CARD = (
    '<div class="application-question"><label>'
    '<div class="application-label">What is your location?</div>'
    '<div class="application-field"><div class="application-dropdown">'
    '<select class="candidate-location" data-qa="candidate-location-select">'
    '<option value="">Select...</option>'
    '<option value="FR">France</option>'
    '<option value="GB">United Kingdom</option>'
    "</select></div></div></label></div>")


def test_lever_name_locator_excludes_the_hidden_twin_and_drives_the_visible_control():
    """`_name_css` excludes `type=hidden`, and that exclusion is what stops the fill
    typing into a control the applicant cannot see.

    Lever renders a base field TWICE -- the visible input and a HIDDEN mirror carrying the
    SAME submission name (capture.py:11-16, a live finding, and the reason the base pass
    captured the live agicap consent box as a phantom textbox). Without the exclusion the
    name resolves to TWO elements; composed with a `_resolves_to_one` weakened to
    `count() >= 1`, the fill would take that two-element locator and drive its FIRST
    element -- the hidden twin. So this pins BOTH halves: exactly one match, and it is the
    VISIBLE control."""
    from engine.kernel.contracts import FieldValue, Locator

    page = _DomBackedPage(_HIDDEN_TWIN_DOM)
    fv = FieldValue(key="phone", label="Phone", type="input_text",
                    locator=Locator(role="textbox", name="Phone"), value="+44 7700 900001")

    matched = page.locator(lever_fill._name_css("phone"))
    assert _match_count(matched) == 1, (
        "the submission name resolves to the hidden twin as well as the control the "
        "applicant sees: the fill can no longer tell which one it means")
    assert (matched.nodes[0].attrs.get("type") or "").lower() == "text"

    located = lever_fill._locate(page, fv)
    assert _match_count(located) == 1
    assert located.nodes[0].attrs.get("id") == "phone-visible"
    # and it got there BY NAME -- no fallback was needed, so the exclusion did not merely
    # hide the ambiguity by punting to the kernel locator.
    assert [call[0] for call in page.requested] == ["css", "css"]


def test_lever_name_locator_refuses_a_name_that_two_visible_controls_answer_to():
    """`_resolves_to_one` means EXACTLY one. Two matches is not "good enough": it is a
    control the fill could type into by mistake, and the locator is refused.

    Weakened to `count() >= 1` the guard still rejects the phantom (zero matches) -- so
    only a MULTI-match DOM can tell the two apart. Here the fill must fall back to the
    kernel's role + accessible-name locator, which CAN separate the two controls."""
    from engine.kernel.contracts import FieldValue, Locator

    page = _DomBackedPage(_TWO_VISIBLE_DOM)
    fv = FieldValue(key="phone", label="Phone", type="input_text",
                    locator=Locator(role="textbox", name="Phone"), value="+44 7700 900001")

    css = lever_fill._name_css("phone")
    assert _match_count(page.locator(css)) == 2
    assert lever_fill._resolves_to_one(page.locator(css)) is False, (
        "a name TWO applicant-facing controls answer to is not a usable locator: the fill "
        "could type into the wrong one")
    # the phantom half of the same guard, pinned on the same DOM: zero is not one either.
    assert lever_fill._resolves_to_one(page.locator(lever_fill._name_css("nosuch"))) is False

    page = _DomBackedPage(_TWO_VISIBLE_DOM)
    located = lever_fill._locate(page, fv)
    assert [call[0] for call in page.requested] == ["css", "role"], (
        "the ambiguous submission-name locator was USED instead of being refused")
    assert _match_count(located) == 1


def test_lever_locate_falls_back_to_the_kernel_for_a_control_with_no_submission_name():
    """The fallback is REACHED, and it is the only thing that can fill swile's location.

    `_locate`'s `return base._locate(page, fv)` is dead code against the primary fixture --
    every field there posts under a name. Live swile's OPTIONAL demographic question does
    not: its `<select class="candidate-location">` carries NO `name` attribute, so the
    capture keys it by a SLUG of the question, the submission-name CSS matches ZERO, and
    the ONLY locator that can find it is the kernel's role + accessible name. Pinned here:
    the shape (parsed by the REAL parser, on the real fixture DOM with the live card
    grafted in), the zero-match name CSS, the fallback being taken, and the fallback
    resolving to exactly one control."""
    from engine.kernel.contracts import FieldValue

    anchor = '<button type="submit">'
    assert _dom_html().count(anchor) == 1
    html = _dom_html().replace(anchor, _SWILE_LOCATION_CARD + anchor)

    fm = lever_capture._parse_lever(html, "swile", "3f21", now=lambda: _PINNED)
    field = {f.label: f for f in fm.fields}["What is your location?"]
    assert field.key == "what_is_your_location"    # a slug: there IS no submission name
    assert field.required is False                 # optional, so it degrades fail-soft
    assert field.options == ["France", "United Kingdom"]

    page = _DomBackedPage(html)
    assert _match_count(page.locator(lever_fill._name_css(field.key))) == 0, (
        "a control with no name attribute cannot be found by submission name")

    page = _DomBackedPage(html)
    fv = FieldValue(key=field.key, label=field.label, type=field.type,
                    locator=field.locator, value="France")
    located = lever_fill._locate(page, fv)
    assert [call[0] for call in page.requested] == ["css", "role"], (
        "the fallback was NOT reached: this field can be filled by no other path")
    assert _match_count(located) == 1
    assert located.nodes[0].tag == "select"
    assert located.nodes[0].attrs.get("class") == "candidate-location"


def test_lever_locate_option_scopes_by_group_when_multiple_cards_share_an_option_wording():
    """LIVE FAILURE (palantir, 2026-07-18): `_locate_option` located an option by a bare,
    PAGE-WIDE `page.get_by_role(role, name=option, exact=True)`, with no regard for which
    GROUP the option belongs to. A posting with several coexisting yes/no cards renders the
    SAME wording ("No") as the accessible name of one radio in every card, so the locator
    strict-mode-violated: on the live posting, `get_by_role("radio", name="No", exact=True)`
    resolved to 4 elements (one "No" radio per card), never the one belonging to the field
    actually being driven.

    The fix scopes the role+name match to the field's OWN submission name via `.and_()`
    (`_group_css`), the round-8 submission-name convention `_name_css`/`_locate` already use
    for a whole field, applied one option at a time. This test builds THREE sibling yes/no
    cards -- each rendering the SAME "No" wording -- and pins both halves: the bare locator
    IS genuinely ambiguous on this DOM (the bug, reproduced), and `_locate_option` still
    resolves each card's own "No" to exactly the one control posting under that card's own
    submission name.
    """
    def card(key: str, question: str) -> str:
        return (
            f'<li class="application-question custom-question"><div>'
            f'<div class="application-label full-width multiple-choice"><div class="text">'
            f'{question}<span class="required">&#10033;</span></div></div>'
            f'<div class="application-field full-width required-field">'
            f'<ul data-qa="multiple-choice">'
            f'<li><label><input type="radio" name="cards[{key}][field0]" value="yes" '
            f'required><span class="application-answer-alternative">Yes</span></label></li>'
            f'<li><label><input type="radio" name="cards[{key}][field0]" value="no" '
            f'required><span class="application-answer-alternative">No</span></label></li>'
            f'</ul></div></div></li>')

    html = (
        "<html><body><form>"
        + card("aaa", "Are you legally authorized to work?")
        + card("bbb", "Do you require sponsorship?")
        + card("ccc", "Have you worked here before?")
        + "</form></body></html>")

    fm = lever_capture._parse_lever(html, "fauxcorp", "9001", now=lambda: _PINNED)
    by_key = {f.key: f for f in fm.fields}
    cards = [by_key[f"cards[{k}][field0]"] for k in ("aaa", "bbb", "ccc")]
    assert [c.options for c in cards] == [["Yes", "No"]] * 3

    page = _DomBackedPage(html)

    # THE BUG, reproduced: the bare page-wide locator is genuinely ambiguous -- three
    # sibling cards each render an option worded "No".
    bare = page.get_by_role("radio", name="No", exact=True)
    assert _match_count(bare) == 3

    # THE FIX: each card's own "No" resolves to exactly one control, and it is the control
    # posting under THAT card's own submission name -- never a sibling's.
    for c in cards:
        located = lever_fill._locate_option(page, "radio", c.key, "No")
        assert _match_count(located) == 1, c.key
        assert located.nodes[0].attrs.get("name") == c.key
        assert located.nodes[0].attrs.get("value") == "no"


def test_lever_one_card_two_submission_names_emits_one_field_per_name():
    """A consent card whose checkboxes post under DIFFERENT names is N questions, not one.

    LIVE swile (2026-07-14) renders ONE `.application-question` card holding TWO consent
    checkboxes with DIFFERENT submission names: one REQUIRED (data retention) and one
    optional (marketing). The `len(checkboxes) > 1` arm reads such a card as ONE checkbox
    GROUP -- "N options SHARING one submission name" -- so it emitted ONE Field, keyed by
    the first checkbox, whose `options` were the two consent sentences, and the SECOND
    control never reached the FieldMap at all (live swile: 19 fields emitted against 20
    named non-hidden controls). `capture._name_groups` splits the card by submission name,
    and each Field then describes its OWN control: its own wording, its own requiredness,
    its own single-checkbox shape."""
    tree = lever_capture._build_tree(_dom_html())
    cards = lever_capture._find_all(
        tree, lambda n: lever_capture._has_class(n, "checkbox-question"))
    assert len(cards) == 1, "the fixture's consent card is ONE card (the live swile shape)"
    assert [c.attrs.get("name") for c in lever_capture._card_controls(cards[0])] == [
        "consent[marketing]", "consent[privacy]"], "two submission names, one card"

    fm = lever_capture._parse_lever(_dom_html(), "fauxcorp", "9001", now=lambda: _PINNED)
    by_key = {f.key: f for f in fm.fields}
    assert "consent[marketing]" in by_key and "consent[privacy]" in by_key, (
        "a control the applicant sees was SUBTRACTED from the FieldMap in silence")

    marketing, privacy = by_key["consent[marketing]"], by_key["consent[privacy]"]
    # each field carries ITS OWN wording, not both sentences glued together, and its own
    # requiredness -- the optional opt-in must never inherit the REQUIRED consent's.
    assert "newsletter" in marketing.label and "privacy policy" not in marketing.label
    assert "privacy policy" in privacy.label and "newsletter" not in privacy.label
    assert marketing.required is False and privacy.required is True
    # neither is a 2-option GROUP: each is one tickable control.
    assert marketing.options == [] and privacy.options == []
    assert marketing.type == privacy.type
    # and both are still the hCaptcha hand-off, by ROLE (the wave's other invariant).
    assert marketing.locator.role == "checkbox" and privacy.locator.role == "checkbox"
