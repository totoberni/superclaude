"""Workable provider (engine.providers.workable): the FOURTH reference
implementation of the `Provider` contract, W5.4 -- the HYBRID path (greenhouse-
class schema CAPTURE, lever-class native-DOM FILL with a wider Turnstile hand-off).

No patchright, no network. CAPTURE is grounded in the REAL public form schemas
fetched 2026-07-06 and committed at `tests/fixtures/providers/workable/`:
`form-57CFF1B2AF.json` (powerlines; standard-only, 3 required), `form-0F5F662A46
.json` (movement-labs; QA_ custom questions incl. a required boolean, 18
required), `form-FAF4116602.json` (io-global; CA_ account attributes, 7 required).
Each is parsed through the REAL offline `fieldmap.parse_workable`. FILL is driven
through a FAKE page/locator harness (mirrors the apply DOM: native text controls,
the hidden file input, the DOM-sweep selectors). The SSOT is a hand-built FAKE (no
owner PII). A real live-browser run against apply.workable.com is a SEPARATE later
step (it needs a network host outside the WSL allowlist; it runs on toto); this
suite proves the LOGIC offline, matching test_providers_greenhouse.py /
test_providers_lever.py.
"""

import json
import re
from importlib import import_module
from pathlib import Path

import pytest

from engine.kernel.contracts import (
    FieldType, FieldValue, FillAssets, FillSafetyError, Locator, ResolvedValues,
    Section)
from engine.providers.workable.capture import capture_workable, parse_workable
from engine.profile_map import profile_from_real_ssot
from engine.providers import _registry, base, protocol, workable
from engine.ssot import SSOT

# The `.fill` SUBMODULE, not the `fill` callable the package re-exports over it
# (the package docstring's NAME NOTE: at package scope the callables win, and the
# submodules stay reachable through sys.modules / import_module).
workable_fill = import_module("engine.providers.workable.fill")

_FIXTURES = Path(__file__).parent / "fixtures" / "providers" / "workable"
_PINNED = "2026-07-03T00:00:00+00:00"

# The powerlines standard-only form's required controls, as the DOM anchors them:
# by SCHEMA KEY (only firstname/lastname/email are required there). The live page
# keys every control on its schema id, so this is the DOM-sweep required set for a
# COMPLETE run.
_POWERLINES_REQUIRED_KEYS = ("firstname", "lastname", "email")

# A FAKE international number in the live shape: a country calling code plus a
# national part (spaced, so no long digit run ever appears in this file).
_FAKE_INTL_PHONE = "+39 555 0100 123"


# -- fixture loaders -------------------------------------------------------


def _raw(filename: str):
    return json.loads((_FIXTURES / filename).read_text())


# The pinned LIVE apply-page DOM of movement-labs 0F5F662A46: the control shapes,
# the kernel sweep's alias record, the locale the session-dependent attributes were
# observed under, and the live resolution of every locator the fill code builds.
_LIVE_DOM = _raw("apply-dom-0F5F662A46.json")


def _fieldmap(filename: str, shortcode: str):
    """A field map parsed through the REAL offline Workable parser."""
    return parse_workable(_raw(filename), "fauxco", shortcode,
                          now=lambda: _PINNED)


def _fake_ssot() -> SSOT:
    # FAKE, invented placeholder data only -- no owner PII, matching the existing
    # real_ssot fixture convention and test_providers_lever.py.
    return SSOT({
        "identity": {
            "name": "Test Candidate",
            "email": "test.candidate@example.invalid",
        },
        "canned_answers": {
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


def _resolved_values(fieldmap, *, tmp_path=None, assets_kwargs=None):
    ssot = _fake_ssot()
    profile = profile_from_real_ssot(ssot)
    assets = (_assets(tmp_path, **(assets_kwargs or {}))
              if tmp_path is not None else None)
    return workable.resolve_values(fieldmap, ssot, profile, assets=assets)


# -- fake opener (real capture path, no network) ---------------------------


class _Resp:
    def __init__(self, body):
        self._body = body
        self.headers = {}

    def read(self):
        return self._body


class _CaptureOpener:
    def __init__(self, raw):
        self._body = json.dumps(raw).encode("utf-8")
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        return _Resp(self._body)


# -- fake DOM harness (mirrors the apply form) -----------------------------


class _FakeTextLocator:
    """A plain text/email/paragraph/phone input: type_human -> press_sequentially,
    readback via input_value(). Raises on the forbidden fill()/click()/check()
    paths (the Turnstile human-cadence + no-auto-click invariants)."""

    def __init__(self):
        self.value = ""

    def press_sequentially(self, ch, delay=None):
        self.value += ch

    def input_value(self):
        return self.value

    def get_attribute(self, name):
        return None

    def fill(self, *args, **kwargs):
        raise AssertionError("Workable text fields must use type_human, never fill()")

    def check(self, *args, **kwargs):
        raise AssertionError("Workable must never programmatically .check() (Turnstile)")

    def click(self, *args, **kwargs):
        raise AssertionError("Workable must never programmatically .click() a field")

    def select_option(self, *args, **kwargs):
        raise AssertionError("a text field must not be select_option'd")


class _FakeBadTextLocator(_FakeTextLocator):
    """A text input whose value silently never takes (readback always empty):
    exercises the readback-gate rejecting a value the page dropped."""

    def input_value(self):
        return ""


class _FakeIntlTelLocator(_FakeTextLocator):
    """The LIVE phone box: an intl-tel-input widget, not a plain text input.

    Pinned from the live run (movement-labs, 2026-07-13): it accepts every
    keystroke, then moves the leading country CALLING code out of the text box
    and into its own country combobox (which fill() never drives) and reformats
    the national remainder. So it reads back FEWER digits than were typed -- 12
    typed, 10 read back -- and an exact string readback calls a landed number a
    miss. `drop_trailing` / `blank` model the two ways the value genuinely does
    NOT land (the page truncated it; the page took nothing), which must still be
    rejected."""

    def __init__(self, *, calling_code="+39", drop_trailing=0, blank=False):
        super().__init__()
        self._calling_code = calling_code
        self._drop_trailing = drop_trailing
        self._blank = blank

    def input_value(self):
        if self._blank:
            return ""
        national = self.value
        if national.startswith(self._calling_code):
            national = national[len(self._calling_code):]
        national = national.strip()
        if self._drop_trailing:
            national = national[:-self._drop_trailing]
        return national


class _FakeDigitRewritingLocator(_FakeTextLocator):
    """A phone box whose readback is rewritten DIGIT-EXACTLY: `absorb_leading`
    digits taken off the FRONT (what the intl-tel widget does with a country
    calling code), `drop_trailing` digits taken off the END (what a truncating
    maxlength does), or a wholly different number via `readback`.

    Why this exists alongside `_FakeIntlTelLocator`: `_phone_landed` guards the
    readback in TWO independent ways (a LENGTH bound of 1-to-3 absorbed digits,
    and a FRONT-ONLY suffix check), and a fake that breaches BOTH at once pins
    NEITHER -- the length bound alone rejects the value, so the suffix check is
    never exercised and can be deleted with the suite still green.
    `_FakeIntlTelLocator(drop_trailing=N)` is exactly that fake: it strips the
    calling code AND the tail. This one counts DIGITS, so a test can hold the
    length bound satisfied while breaching the front-only guard, and vice versa.
    """

    def __init__(self, *, absorb_leading=0, drop_trailing=0, readback=None):
        super().__init__()
        self._absorb = absorb_leading
        self._drop = drop_trailing
        self._readback = readback

    def input_value(self):
        if self._readback is not None:
            return self._readback
        digits = re.sub(r"\D", "", self.value)
        return digits[self._absorb:len(digits) - self._drop]


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


class _FakeDeadFileInput(_FakeFileInput):
    """A file input that records the attach call but never holds the file (its
    readback stays empty): models an upload the page silently rejected, so the
    key never enters `uploads` and its required gap must still bite."""

    def input_value(self):
        return ""


class _FakeSweepControl:
    """One required control as the LIVE apply page renders it.

    Shape pinned by `apply-dom-0F5F662A46.json` (read off the real posting on
    2026-07-13, re-derived 2026-07-14): a control carries its SCHEMA KEY as its
    own anchor -- `name` on the inputs/textareas, `data-ui` on the resume file
    input -- while a boolean's visible option WRAPPER carries neither (only
    `role=radio` and the shared marker `data-ui=option`) and reports its key
    through the enclosing `fieldset[data-ui]`, which `.evaluate()` answers for.

    `aria_hidden` is NOT decorative. The kernel sweep reads a control only when it
    is CSS-visible AND NOT aria-hidden (`fill_toolkit._visible_locators`), and the
    live page marks all four native `input[type=radio][name=QA_n]` aria-hidden
    (it paints the option wrappers on top of them). A fake that cannot model the
    attribute hands the code four anchors the live page never shows the sweep, so
    it must model it: `get_attribute("aria-hidden")` answers here exactly as the
    live DOM answers.
    """

    def __init__(self, *, name="", data_ui="", role="", closest_fieldset="",
                 visible=True, aria_hidden="", text="", aria_label="",
                 placeholder=""):
        self._attrs = {"name": name, "data-ui": data_ui, "role": role,
                      "aria-label": aria_label, "placeholder": placeholder,
                      "aria-hidden": aria_hidden}
        self._closest = closest_fieldset
        self._visible = visible
        self._text = text

    def get_attribute(self, name):
        return self._attrs.get(name) or None

    def is_visible(self):
        return self._visible

    def inner_text(self):
        return self._text

    def evaluate(self, js):
        # The sweep asks exactly one thing of the DOM: the enclosing radiogroup.
        assert "fieldset[data-ui]" in js
        return self._closest


def _live_dom_controls():
    """Every required control of the LIVE movement-labs apply page, from the
    pinned DOM fixture (structure only; the page it was read from was unfilled).

    All 24 of them, INCLUDING the four the sweep drops: the fixture is a map of
    the page, not of the sweep's input, and `_visible_locators` is what narrows
    the one to the other. Feeding the sweep only the 20 survivors here would move
    the filter out of the code under test and into the harness."""
    raw = json.loads((_FIXTURES / "apply-dom-0F5F662A46.json").read_text())
    return [_FakeSweepControl(
        name=c["name"], data_ui=c["data_ui"], role=c["role"],
        closest_fieldset=c["closest_fieldset_data_ui"], visible=c["visible"],
        aria_hidden=c.get("aria_hidden", ""),
        placeholder=c.get("placeholder", "")) for c in raw["controls"]]


class _FakeLocatorSet:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class _PageBoundContext:
    """The fake page's owning BrowserContext. `install_never_send` targets the
    CONTEXT (so the guard covers every page/popup it opens); the registration is
    recorded here AND mirrored onto the page so the page-level assertions work."""

    def __init__(self, page):
        self._page = page
        self.routed = []

    def route(self, pattern, handler):
        self.routed.append((pattern, handler))
        self._page.route(pattern, handler)


def _has_text_match(pattern, text):
    """Model playwright's `filter(has_text=...)`: a regex is matched against the
    element's normalized-whitespace text; a plain string is a case-insensitive
    substring. `_radiogroup_option` passes an anchored, case-insensitive regex."""
    normalized = " ".join(str(text).split())
    if hasattr(pattern, "search"):
        return bool(pattern.search(normalized))
    return str(pattern).strip().lower() in normalized.lower()


class _FakeActionabilityTimeout(Exception):
    """A playwright-style click actionability timeout: the wrapper never became
    visible/stable/unobstructed within the bounded click wait. NOT a
    FillSafetyError, so `_drive_boolean_radio` catches it and PARKS."""


class _FakeLazyCookieBanner:
    """A cookie-consent banner that renders LATE -- absent at fill-start, present
    once the lazy radiogroup at the page bottom is SCROLLED into view (models the
    live rokt session: the banner overlays the form area). The fill-start recovery
    cannot see it (not visible yet), so ONLY the per-drive dismissal (which runs
    AFTER the scroll) catches it -- that is what unblocks the option click."""

    def __init__(self):
        self.rendered = False
        self.clicked = 0

    def is_covering(self):
        return self.rendered and self.clicked == 0

    def is_visible(self):
        return self.rendered and self.clicked == 0

    def get_attribute(self, attr):
        return None

    def inner_text(self):
        return "Accept"

    def click(self):
        self.clicked += 1


class _FakeCookieButton:
    """One button of the cookie-consent modal, identified ONLY by its `data-ui`
    (its classes/id are hash-obfuscated, so class/id lookups read None). Clicking
    it dismisses the whole modal."""

    def __init__(self, modal, data_ui, text):
        self._modal = modal
        self._data_ui = data_ui
        self.text = text
        self.clicked = 0

    def get_attribute(self, attr):
        return {"data-ui": self._data_ui}.get(attr)

    def is_visible(self):
        return not self._modal.dismissed

    def inner_text(self):
        return self.text

    def click(self):
        self.clicked += 1
        self._modal.dismissed = True


class _FakeCookieConsentModal:
    """The LIVE Workable cookie-consent MODAL: `<div data-ui="cookie-consent"
    role="dialog" aria-modal="true">` with hash-obfuscated classes, buttons
    identified ONLY by `data-ui`, and a `data-ui="backdrop"` that overlays the whole
    page (aria-modal) and PERSISTS across scroll -- so it intercepts the QA click
    until dismissed. The class/id cookie selectors match ZERO buttons here (no
    "cookie"/"consent" in any class or id) -- the trap that cost two live rounds --
    so only the data-ui selectors can dismiss it. SETTINGS is listed FIRST (a
    realistic Settings|Decline|Accept layout), so a naive first-button click would
    hit SETTINGS: the fix must prefer decline and never settings."""

    def __init__(self, *, has_decline=True, has_accept=True, has_settings=True):
        self.dismissed = False
        self.buttons = []
        if has_settings:
            self.buttons.append(
                _FakeCookieButton(self, "cookie-consent-settings", "Cookies settings"))
        if has_decline:
            self.buttons.append(
                _FakeCookieButton(self, "cookie-consent-decline",
                                  "Decline optional cookies"))
        if has_accept:
            self.buttons.append(
                _FakeCookieButton(self, "cookie-consent-accept", "Accept all cookies"))

    def is_covering(self):
        return not self.dismissed

    def button(self, token):
        return next(b for b in self.buttons if token in b._data_ui)

    def match(self, selector):
        """The buttons a dismissal selector resolves to, modeling CSS matching over
        the modal's data-ui buttons. A class/id selector (no `[data-ui="cookie-
        consent"]` scope) matches NOTHING -- the hash-obfuscated-class trap."""
        if self.dismissed:
            return []
        sel = selector.lower()
        if '[data-ui="cookie-consent"]' not in sel:
            return []

        def keep(button):
            dataui = (button.get_attribute("data-ui") or "").lower()
            if 'data-ui*="decline"' in sel:
                return "decline" in dataui
            if 'data-ui*="accept"' in sel:
                return "accept" in dataui
            if ":not(" in sel:                 # the non-settings primary-button fallback
                return "settings" not in dataui
            return False

        return [b for b in self.buttons if keep(b)]


class _FakeRadioWrapper:
    """One boolean OPTION wrapper as the LIVE apply page renders it
    (`div[data-ui="option"][role="radio"]`, rokt /apply/ probe 2026-07-19): the
    node a human CLICKS. It carries `aria-checked` and `tabindex`; its VISIBLE text
    is only the option word ("Yes"/"No"), while its ACCESSIBLE NAME is the
    aria-labelledby CONCATENATION of the group question and the option word -- the
    exact live shape that made the old exact-name `.check()` locator resolve to
    ZERO and time out (R3, QA_11599465/66).

    Clicking it flips `aria-checked` to "true" and, per single-select semantics, its
    group siblings to "false". `lands=False` models a click the page silently drops
    (aria-checked stays "false"); `sticky_sibling=True` models a BROKEN single-select
    (the click sets self true but leaves the sibling as it was).

    ACTIONABILITY (R5 residual trap): the wrapper starts NON-actionable and only a
    scroll+dismiss makes it clickable. `requires_scroll=True` -> a click before
    `scroll_into_view_if_needed` raises an off-viewport timeout; `overlay=<banner>`
    -> a click while the banner is covering raises an intercepts-pointer-events
    timeout; `click_error=<msg>` -> a click ALWAYS raises (a persistent blocker),
    to pin the park-with-blocker path. `.check()`/`.fill()` are FORBIDDEN: the
    driver CLICKS the wrapper, never `.check()`s the aria-hidden inner input."""

    def __init__(self, group, option_text, *, accessible_name=None,
                 lands=True, aria_checked="false", sticky_sibling=False,
                 requires_scroll=False, overlay=None, click_error=None):
        self._group = group
        self.option_text = option_text
        self.accessible_name = accessible_name or option_text
        self._lands = lands
        self._sticky_sibling = sticky_sibling
        self.aria_checked = aria_checked
        self._requires_scroll = requires_scroll
        self._overlay = overlay
        self._click_error = click_error
        self.scrolled = 0
        self.clicked = 0

    def get_attribute(self, name):
        # A live wrapper carries NO aria-label and NO name; its accessible name is
        # computed from aria-labelledby, which get_attribute cannot flatten -- so a
        # name-based locator has nothing to match, exactly as live.
        return {"aria-checked": self.aria_checked, "role": "radio",
                "data-ui": "option", "tabindex": "0",
                "aria-hidden": None, "aria-label": None, "name": None}.get(name)

    def inner_text(self):
        return self.option_text

    def scroll_into_view_if_needed(self):
        self.scrolled += 1
        # a lazy overlay renders once the page is scrolled to the bottom form.
        if self._overlay is not None:
            self._overlay.rendered = True

    def click(self):
        # Actionability, exactly as playwright reports it: a persistent blocker, an
        # off-viewport node (not yet scrolled), or a live overlay intercepts the
        # click. The click never lands (clicked stays 0) -- distinct from a click
        # that lands but whose state the page drops (`lands=False`).
        if self._click_error is not None:
            raise _FakeActionabilityTimeout(self._click_error)
        if self._requires_scroll and not self.scrolled:
            raise _FakeActionabilityTimeout(
                "locator.click: Timeout 30000ms exceeded.\nCall log:\n  - "
                "element is outside of the viewport")
        if self._overlay is not None and self._overlay.is_covering():
            raise _FakeActionabilityTimeout(
                "locator.click: Timeout 30000ms exceeded.\nCall log:\n  - "
                '<div class="cookie-consent-banner">Accept</div> intercepts '
                "pointer events")
        self.clicked += 1
        if not self._lands:
            return
        if not self._sticky_sibling:
            for wrapper in self._group.wrappers:
                wrapper.aria_checked = "false"
        self.aria_checked = "true"

    def check(self, *args, **kwargs):
        raise AssertionError(
            "the boolean radio must be CLICKED on its wrapper, never .check()'d "
            "(the R3 timeout mode; the inner input is aria-hidden)")

    def fill(self, *args, **kwargs):
        raise AssertionError("a boolean option is never .fill()'d")


class _FakeRadioGroup:
    """`fieldset[data-ui="<key>"][role="radiogroup"]`: a SCOPE holding its option
    wrappers. `.locator('div[data-ui="option"][role="radio"]')` returns them as a
    filterable set, mirroring `_radiogroup_option`'s own two-step construction."""

    def __init__(self):
        self.wrappers = []

    def locator(self, css):
        assert css == workable_fill._OPTION_WRAPPER_CSS, css
        return _FakeOptionSet(self.wrappers)


class _FakeOptionSet:
    """A live set of option wrappers, narrowable by `.filter(has_text=...)` and
    countable by `.count()` -- the same Locator surface `_radiogroup_option` drives.
    `.filter` matches each wrapper's VISIBLE TEXT (never its accessible name),
    exactly as playwright's `has_text` does; a name-based filter would find nothing
    here, which is the whole point of the fix. `.get_attribute`/`.click` require the
    set to have narrowed to exactly one wrapper (playwright strict mode), so a driver
    that reads or clicks an ambiguous locator fails loudly rather than guessing."""

    def __init__(self, items):
        self._items = list(items)

    def filter(self, has_text=None):
        if has_text is None:
            return _FakeOptionSet(self._items)
        return _FakeOptionSet(
            [w for w in self._items if _has_text_match(has_text, w.inner_text())])

    def count(self):
        return len(self._items)

    def get_attribute(self, name):
        assert len(self._items) == 1, (
            f"get_attribute on a non-unique locator ({len(self._items)} matches)")
        return self._items[0].get_attribute(name)

    def scroll_into_view_if_needed(self):
        assert len(self._items) == 1, (
            f"scroll on a non-unique locator ({len(self._items)} matches)")
        self._items[0].scroll_into_view_if_needed()

    def click(self):
        assert len(self._items) == 1, (
            f"click on a non-unique locator ({len(self._items)} matches)")
        self._items[0].click()

    def check(self, *args, **kwargs):
        raise AssertionError("the boolean radio must be CLICKED, never .check()'d")


def _radio_group(key, *, yes_lands=True, no_lands=True, yes_checked="false",
                 no_checked="false", yes_sticky=False,
                 group_question="A yes/no question",
                 requires_scroll=False, overlay=None, yes_click_error=None):
    """Build one boolean radiogroup with a Yes and a No option wrapper in the LIVE
    shape: each wrapper's ACCESSIBLE NAME is the group question CONCATENATED with
    the option word (via aria-labelledby), while its VISIBLE TEXT is only the
    option word -- so a name-exact locator resolves to zero and the visible-text
    filter resolves to exactly one. `requires_scroll`/`overlay`/`yes_click_error`
    arm the R5 actionability trap (the wrapper starts non-actionable)."""
    group = _FakeRadioGroup()
    group.wrappers = [
        _FakeRadioWrapper(group, "Yes", accessible_name=f"{group_question} Yes",
                          lands=yes_lands, aria_checked=yes_checked,
                          sticky_sibling=yes_sticky, requires_scroll=requires_scroll,
                          overlay=overlay, click_error=yes_click_error),
        _FakeRadioWrapper(group, "No", accessible_name=f"{group_question} No",
                          lands=no_lands, aria_checked=no_checked,
                          requires_scroll=requires_scroll, overlay=overlay),
    ]
    return group


class _FakeTabButton:
    """One tab-bar control (`[role="tab"]`), the W5.1-R2 FX2 defensive-recovery
    fixture: models the live OVERVIEW|APPLICATION tab bar (rokt posting,
    2026-07-18 TOTO-GATE acceptance run) enough to prove the recovery path. An
    accessible name (read back via `inner_text`, the same fallback
    `_accessible_name` uses for a control with no aria-label), an
    `aria-selected` state, and a `.click()` that flips it once `lands` allows
    -- driven through the SAME `_safe_click` primitive as everywhere else in
    this module, never a raw `.check()`/`.fill()`."""

    def __init__(self, *, name, selected=False, lands=True):
        self._name = name
        self._selected = selected
        self._lands = lands
        self.clicked = 0

    def get_attribute(self, attr):
        if attr == "aria-selected":
            return "true" if self._selected else "false"
        return None

    def is_visible(self):
        return True

    def inner_text(self):
        return self._name

    def click(self):
        self.clicked += 1
        if self._lands:
            self._selected = True


class _FakeCookieBanner:
    """One cookie-consent banner accept/dismiss control, the W5.1-R2 FX2
    fixture's other half: an accessible name and a `.click()` count, nothing
    more -- the recovery never reads any other state off it."""

    def __init__(self, *, name="Accept"):
        self._name = name
        self.clicked = 0

    def get_attribute(self, attr):
        return None

    def is_visible(self):
        return True

    def inner_text(self):
        return self._name

    def click(self):
        self.clicked += 1


class _FakeWorkablePage:
    """One fake page driving the WHOLE fill() sequence for Workable: native TEXT
    controls only (auto-built from the field map's textbox-role fields), the hidden
    file input(s), and the sweep_required CSS selectors. A boolean/checkbox/
    dropdown/multiple/group control is deliberately NOT built: fill() must hand it
    off (never request a locator for it), so any such request KeyErrors -- proving
    the no-auto-click invariant.

    The swept required controls mirror the LIVE DOM (`_FakeSweepControl`): each
    one carries its SCHEMA KEY as its own anchor, which is what
    `_workable_dom_required` reads. `sweep_required_keys` says which required
    controls the page renders (default: every required field of the map, i.e. a
    page that agrees with its schema); `sweep_controls` overrides that with
    hand-shaped controls (the pinned live DOM, an unanchored control, ...). A
    `phone` field gets the intl-tel widget the live page renders, not a plain
    text box."""

    def __init__(self, fieldmap, *,
                 url="https://apply.workable.com/fauxco/j/57CFF1B2AF/apply/",
                 sweep_required_keys=None, sweep_controls=None,
                 file_inputs=None, bad_keys=(), phone_locator=None,
                 boolean_groups=None, tab_controls=None,
                 cookie_banner_button=None, cookie_modal=None):
        self._url = url
        # W5.1-R2 FX2: the OVERVIEW|APPLICATION tab bar + cookie-consent
        # banner the defensive recovery probes for. Empty/None by default (no
        # tests here render either), so every OTHER test's `fill()` call
        # exercises the recovery as a true no-op, exactly as an ordinary
        # /apply/-loaded page does. `cookie_modal` is the live data-ui MODAL
        # (matched only by the data-ui selectors); `cookie_banner_button` is the
        # legacy class/id single-button banner (matched by the class fallback).
        self._tab_controls = list(tab_controls) if tab_controls is not None else []
        self._cookie_banner_button = cookie_banner_button
        self._cookie_modal = cookie_modal
        self.boolean_groups = dict(boolean_groups or {})
        self.controls = {}
        for fld in fieldmap.fields:
            if fld.locator.role == "textbox":
                key = (fld.locator.role, fld.locator.name)
                if fld.key in bad_keys:
                    self.controls[key] = _FakeBadTextLocator()
                elif fld.type == "phone":
                    self.controls[key] = (phone_locator or _FakeIntlTelLocator())
                else:
                    self.controls[key] = _FakeTextLocator()
        self.file_inputs = (list(file_inputs) if file_inputs is not None else
                            [_FakeFileInput(id="resume", name="resume",
                                           accept=".pdf,.doc,.docx"),
                             _FakeFileInput(id="avatar", name="avatar",
                                           accept="image/*")])
        self.routed = []
        self.context = _PageBoundContext(self)
        self.requested = []
        self.located_css = []
        if sweep_controls is not None:
            self._sweep_required = list(sweep_controls)
        else:
            keys = (sweep_required_keys if sweep_required_keys is not None
                    else [f.key for f in fieldmap.required_fields()])
            self._sweep_required = [_FakeSweepControl(name=key) for key in keys]

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
        if css == base._REQUIRED_CSS:
            return _FakeLocatorSet(self._sweep_required)
        if css == workable_fill._TAB_ROLE_CSS:
            return _FakeLocatorSet(self._tab_controls)
        if css in workable_fill._COOKIE_DISMISS_SELECTORS:
            return _FakeLocatorSet(self._match_cookie_selector(css))
        fieldset = re.match(
            r'^fieldset\[data-ui="([^"]+)"\]\[role="radiogroup"\]$', css)
        if fieldset:
            # A key the test never wired is a KeyError, deliberately: the
            # production code must never scope to a radiogroup this harness was
            # not told to expect.
            return self.boolean_groups[fieldset.group(1)]
        return _FakeLocatorSet([])

    def _match_cookie_selector(self, css):
        # The data-ui MODAL (hash-obfuscated classes) is matched ONLY by the
        # data-ui selectors -- its `match` returns [] for the class/id fallback
        # (the trap). The legacy single-button banner is matched by the class
        # fallback instead.
        if self._cookie_modal is not None:
            return self._cookie_modal.match(css)
        if css == workable_fill._COOKIE_CLASS_CSS and self._cookie_banner_button:
            return [self._cookie_banner_button]
        return []


# =============================================================================
# capture / apply_url: thin delegation to the registry
# =============================================================================


def test_capture_delegates_to_registry_capture(monkeypatch):
    # _registry.get("workable").capture is a call-time lazy_call targeting
    # engine.providers.workable:capture, which lazily imports and calls
    # engine.providers.workable.capture.capture_workable at CALL time; patching
    # that module attribute proves capture() rides the SAME registry wiring.
    from importlib import import_module
    capture_mod = import_module("engine.providers.workable.capture")
    calls = []

    def fake_capture(slug, job_id, opener=None):
        calls.append((slug, job_id, opener))
        return "SENTINEL"

    monkeypatch.setattr(capture_mod, "capture_workable", fake_capture)
    result = workable.capture("powerlines", "57CFF1B2AF", opener="OPENER")
    assert result == "SENTINEL"
    assert calls == [("powerlines", "57CFF1B2AF", "OPENER")]
    assert _registry.get("workable").capture._target == ("engine.providers.workable", "capture")


def test_apply_url_delegates_to_registry_apply_url():
    assert (workable.apply_url("foo", "123")
           == "https://apply.workable.com/foo/j/123/apply/")


def test_workable_module_satisfies_provider_protocol():
    # The load-bearing conformance check: the module-scope shape structurally
    # satisfies the SAME Provider Protocol greenhouse / lever do.
    assert isinstance(workable, protocol.Provider)
    assert workable.vendor == "workable"


# =============================================================================
# capture: the real form fixtures -> canonical FieldMap
# =============================================================================


@pytest.mark.parametrize("filename,shortcode,req_count,custom_id", [
    ("form-57CFF1B2AF.json", "57CFF1B2AF", 3, None),
    ("form-0F5F662A46.json", "0F5F662A46", 18, "QA_11919111"),
    ("form-FAF4116602.json", "FAF4116602", 7, "CA_6419"),
])
def test_capture_parses_real_fixture(filename, shortcode, req_count, custom_id):
    fm = _fieldmap(filename, shortcode)
    assert fm.vendor == "workable"
    assert fm.posting_id == shortcode
    assert len(fm.required_fields()) == req_count
    by_key = {f.key: f for f in fm.fields}
    if custom_id is None:
        # standard-only form: no QA_/CA_ custom-section field
        assert not any(f.section == Section.CUSTOM for f in fm.fields)
    else:
        assert custom_id in by_key
        assert by_key[custom_id].section == Section.CUSTOM


def test_capture_workable_rides_the_real_opener_get():
    # The end-to-end HTTP-only capture path (capture_workable -> parse_workable)
    # against a FAKE opener: one GET to the public form endpoint, no browser.
    opener = _CaptureOpener(_raw("form-0F5F662A46.json"))
    fm = capture_workable("movement-labs", "0F5F662A46", opener,
                          now=lambda: _PINNED)
    assert len(opener.requests) == 1
    assert opener.requests[0].full_url == \
        "https://apply.workable.com/api/v1/jobs/0F5F662A46/form"
    assert fm.vendor == "workable" and fm.posting_id == "0F5F662A46"
    assert len(fm.required_fields()) == 18


def test_parse_workable_dropdown_multiple_and_number_types_and_choice_labels():
    # SYNTHETIC payload: none of the three real fixtures samples a dropdown,
    # multiple-choice, or number field (see `_workable_choice_labels`'s own
    # docstring), so this hand-built sections payload pins the type->role
    # mapping and the choices[].body choice-label extraction that would
    # otherwise be defensive-only, untested code.
    raw = [{
        "name": "Custom questions",
        "fields": [
            {
                "id": "QA_1001", "required": True,
                "label": "Which office would you prefer?",
                "type": "dropdown",
                "choices": [{"id": 1, "body": "London"},
                           {"id": 2, "body": "Remote"}],
            },
            {
                "id": "QA_1002", "required": False,
                "label": "Which languages do you speak?",
                "type": "multiple",
                "choices": [{"id": 3, "body": "English"},
                           {"id": 4, "body": "Italian"}],
            },
            {
                "id": "QA_1003", "required": True,
                "label": "Years of professional experience",
                "type": "number",
            },
        ],
    }]

    fm = parse_workable(raw, "fauxco", "SYNTHETIC", now=lambda: _PINNED)
    by_key = {f.key: f for f in fm.fields}

    dropdown = by_key["QA_1001"]
    assert dropdown.locator.role == "combobox"
    assert dropdown.norm_type == FieldType.SINGLE_SELECT
    assert dropdown.options == ["London", "Remote"]

    multiple = by_key["QA_1002"]
    assert multiple.locator.role == "listbox"
    assert multiple.norm_type == FieldType.MULTI_SELECT
    assert multiple.options == ["English", "Italian"]

    number = by_key["QA_1003"]
    assert number.locator.role == "textbox"
    assert number.norm_type == FieldType.NUMBER
    assert number.options == []


# =============================================================================
# resolve_values: INHERITED hole-fix e structural CV/photo choice (FAKE ssot)
# =============================================================================


def test_resolve_values_inherits_cv_ats_and_photo_when_avatar_present(tmp_path):
    # powerlines exposes an `avatar` (Photo) upload field -> the structural
    # signal fires: resume is the plain ATS CV and the photo attaches.
    fieldmap = _fieldmap("form-57CFF1B2AF.json", "57CFF1B2AF")
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    by_key = {fv.key: fv for fv in values.fields}
    assert by_key["resume"].asset == "cv-ats"
    assert "photo field present" in by_key["resume"].upload_reason
    assert by_key["avatar"].asset == "photo"
    assert Path(by_key["avatar"].value).name == "Me.png"


def test_resolve_values_inherits_cv_atsi_when_no_photo_field(tmp_path):
    # Strip the avatar field -> no photo signal -> the negative branch embeds
    # the photo via the ATSI CV for the resume upload.
    fieldmap = _fieldmap("form-57CFF1B2AF.json", "57CFF1B2AF")
    fieldmap.fields = [f for f in fieldmap.fields if f.key != "avatar"]
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    resume = {fv.key: fv for fv in values.fields}["resume"]
    assert resume.asset == "cv-atsi"
    assert "no photo field" in resume.upload_reason


# =============================================================================
# fill(): the ordered Provider-contract sequence (native DOM path)
# =============================================================================


def test_fill_completes_when_all_safe_required_land():
    # The powerlines standard-only form's required set is firstname/lastname/email
    # (all text). With no assets the optional uploads skip; every required text
    # field lands and readback-confirms and the live sweep agrees -> COMPLETE.
    fieldmap = _fieldmap("form-57CFF1B2AF.json", "57CFF1B2AF")
    values = _resolved_values(fieldmap)
    page = _FakeWorkablePage(fieldmap)

    report = workable.fill(page, fieldmap, values)

    assert report.vendor == "workable"
    # (1) never-send installed exactly once, at CONTEXT scope.
    assert len(page.context.routed) == 1 and page.context.routed[0][0] == "**"
    # (2)+(3) text via type_human, readback-confirmed. Combined identity.name is
    # split to the first token for a First name field (gap #2), so it lands "Test".
    assert page.controls[("textbox", "First name")].input_value() == "Test"
    assert page.controls[("textbox", "Email")].input_value() == \
        "test.candidate@example.invalid"
    assert report.readback_mismatches == []
    # (4) live sweep agrees with the schema required set -> no gap.
    assert report.required_unfilled == []
    assert report.complete is True
    assert report.caption().endswith("COMPLETE")
    assert not report.caption().endswith("NOT COMPLETE")


def test_fill_required_boolean_qa_is_driven_by_clicking_the_option_wrapper():
    # RETARGETED (W5.1c-R4, F-8). movement-labs QA_11919111 is a REQUIRED boolean
    # rendered as a yes/no radio fieldset. It is DRIVEN by CLICKING the option
    # WRAPPER matching the resolved intent (`_drive_boolean_radio`) and confirming
    # via aria-checked -- NOT by `.check()` on the aria-hidden inner input, the R3
    # QA_11599465/66 drive-timeout mode. The wrapper is located by its VISIBLE TEXT
    # scoped to `fieldset[data-ui="<key>"][role="radiogroup"]`, never by the
    # ambiguous field-level role+label locator (which resolves to BOTH wrappers --
    # see the locator-resolution invariant below).
    #
    # Two halves, equally load-bearing: (a) a resolved bool that LANDS is DRIVEN
    # and readback-confirmed via aria-checked -- no gap; (b) a click the page
    # silently drops (aria-checked stays false) is STILL a gap, never a silent
    # COMPLETE -- the never-confirmed-bias readback gate applies here too.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    qa = {f.key: f for f in fieldmap.fields}["QA_11919111"]
    assert qa.required and qa.type == "boolean" and qa.locator.role == "radio"
    values = ResolvedValues(fields=[FieldValue(
        key=qa.key, label=qa.label, type=qa.type, locator=qa.locator, value=True)])

    # (a) CONFIRMED: the "Yes" wrapper is CLICKED and reads back aria-checked=true,
    # the "No" wrapper false (single-select).
    group = _radio_group(qa.key)
    page = _FakeWorkablePage(fieldmap, boolean_groups={qa.key: group})

    report = workable.fill(page, fieldmap, values)

    yes, no = group.wrappers
    gap = {g["key"]: g for g in report.required_unfilled}
    assert qa.key not in gap                     # driven + confirmed: no gap
    assert report.readback_mismatches == []
    assert yes.clicked == 1 and yes.aria_checked == "true"    # CLICKED, not checked
    assert no.clicked == 0 and no.aria_checked == "false"     # sibling deselected
    # It went through the fieldset-SCOPED, radiogroup construction: no field-level
    # role+label request (the one that would resolve to both options) was ever made.
    assert not any(req == ("role", "radio", qa.label) for req in page.requested)
    assert f'fieldset[data-ui="{qa.key}"][role="radiogroup"]' in page.located_css

    # (b) UNCONFIRMED: the SAME drive, but the click never sticks (aria-checked
    # stays false) -- still a required gap, never a silent pass.
    group_unconfirmed = _radio_group(qa.key, yes_lands=False)
    page_unconfirmed = _FakeWorkablePage(
        fieldmap, boolean_groups={qa.key: group_unconfirmed})

    report2 = workable.fill(page_unconfirmed, fieldmap, values)

    assert report2.complete is False
    gap2 = {g["key"]: g for g in report2.required_unfilled}
    assert qa.key in gap2
    assert group_unconfirmed.wrappers[0].clicked == 1        # it WAS clicked ...
    assert group_unconfirmed.wrappers[0].aria_checked == "false"  # ... but did not stick
    assert any(m["key"] == qa.key for m in report2.readback_mismatches)
    assert report2.caption().endswith("NOT COMPLETE")


def test_fill_boolean_radio_without_genuine_bool_stays_handed_off():
    # RS-g / owner ruling 2026-07-19 (W4): NO blanket Turnstile hand-off -- a
    # boolean radio is DRIVEN when a GENUINE bool exists (proven by the test
    # above). The hand-off is preserved ONLY when no genuine bool exists: a
    # value that is not a real `bool` (the string "No" is truthy, so a coerced
    # pick would silently guess "Yes"). fill() hands it off with
    # `_NON_BOOL_BOOLEAN_REASON` and never builds the radio locator.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    qa = {f.key: f for f in fieldmap.fields}["QA_11919111"]
    assert qa.type == "boolean" and qa.locator.role == "radio"
    values = ResolvedValues(fields=[FieldValue(
        key=qa.key, label=qa.label, type=qa.type, locator=qa.locator,
        value="No")])                        # NOT a real bool

    group = _radio_group(qa.key)
    page = _FakeWorkablePage(fieldmap, boolean_groups={qa.key: group})

    report = workable.fill(page, fieldmap, values)

    assert (qa.key, workable_fill._NON_BOOL_BOOLEAN_REASON) in report.skipped
    # Never driven: neither wrapper was clicked, and no radiogroup locator was built.
    assert all(w.clicked == 0 for w in group.wrappers)
    assert all(w.aria_checked == "false" for w in group.wrappers)
    assert f'fieldset[data-ui="{qa.key}"][role="radiogroup"]' not in page.located_css
    # A required non-bool boolean is a gap, never a silent COMPLETE.
    assert report.complete is False
    assert qa.key in {g["key"] for g in report.required_unfilled}


def test_fill_boolean_radio_locates_by_visible_text_not_the_concatenated_accessible_name():
    # THE R3 ROOT CAUSE, pinned (QA_11599465/66 drive-timeout, 2026-07-19). The old
    # spec built get_by_role("radio", name=<option>, exact=True) and .check()'d it;
    # that resolved to ZERO live elements and Locator.check timed out, because an
    # option wrapper's ACCESSIBLE NAME is its aria-labelledby CONCATENATION of the
    # GROUP question and the option word, never the bare "Yes"/"No". The fix filters
    # by the wrapper's own VISIBLE TEXT (only the option word) instead.
    key = "QA_11919111"
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    group = _radio_group(key, group_question="Are you legally authorised to work")
    page = _FakeWorkablePage(fieldmap, boolean_groups={key: group})

    # (a) the visible-text construction resolves to EXACTLY ONE wrapper per option.
    assert workable_fill._option_count(
        workable_fill._radiogroup_option(page, key, "Yes")) == 1
    assert workable_fill._option_count(
        workable_fill._radiogroup_option(page, key, "No")) == 1

    # (b) the accessible name is NOT the bare option word -- it carries the group
    # question -- so the R3 exact-name match on "Yes"/"No" would have found zero.
    for wrapper in group.wrappers:
        assert wrapper.accessible_name != wrapper.option_text
        assert wrapper.option_text in wrapper.accessible_name
        assert "Are you legally authorised to work" in wrapper.accessible_name
        assert wrapper.get_attribute("aria-label") is None    # name is via aria-labelledby
        assert wrapper.get_attribute("name") is None

    # (c) the LIVE fixture corroborates the concatenation: the FIELD-LEVEL locator
    # keyed on the group question label resolved to BOTH wrappers (count 2), which
    # is only possible if each wrapper's accessible name carries that group label.
    field_rows = {r["key"]: r for r in _LIVE_DOM["locator_resolution"]["fields"]}
    assert field_rows[key]["count"] == 2
    assert field_rows[key]["matched"] == ["option", "option"]


def test_fill_boolean_radio_single_select_violation_is_not_confirmed():
    # SINGLE-SELECT INTEGRITY (the sibling aria-checked="false" half of the readback).
    # A broken group leaves the sibling selected after the click -- two options live
    # at once. The driven "Yes" reads back true, but "No" ALSO reads true, so the
    # drive is NOT confirmed and the required field stays a gap, never a silent COMPLETE.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    qa = {f.key: f for f in fieldmap.fields}["QA_11919111"]
    values = ResolvedValues(fields=[FieldValue(
        key=qa.key, label=qa.label, type=qa.type, locator=qa.locator, value=True)])
    # "No" starts selected, and clicking "Yes" does NOT deselect it (sticky sibling).
    group = _radio_group(qa.key, yes_sticky=True, no_checked="true")
    page = _FakeWorkablePage(fieldmap, boolean_groups={qa.key: group})

    report = workable.fill(page, fieldmap, values)

    yes, no = group.wrappers
    assert yes.aria_checked == "true" and no.aria_checked == "true"   # BOTH selected
    assert report.complete is False
    assert qa.key in {g["key"] for g in report.required_unfilled}
    assert any(m["key"] == qa.key for m in report.readback_mismatches)


def test_fill_boolean_radio_option_not_uniquely_located_parks_by_name():
    # PARK HONESTLY. When the Yes/No option cannot be narrowed to exactly one wrapper
    # (option-text drift, a re-rendered shape), the control is PARKED for a human --
    # never a blind click into an ambiguous locator, and never a false readback
    # mismatch (nothing was driven). A required one stays a gap -> NOT_COMPLETE.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    qa = {f.key: f for f in fieldmap.fields}["QA_11919111"]
    values = ResolvedValues(fields=[FieldValue(
        key=qa.key, label=qa.label, type=qa.type, locator=qa.locator, value=True)])
    # A group whose option words are BOTH "Maybe": the "Yes" filter matches neither,
    # so the driven-option locator resolves to zero.
    group = _FakeRadioGroup()
    group.wrappers = [_FakeRadioWrapper(group, "Maybe"),
                      _FakeRadioWrapper(group, "Maybe")]
    page = _FakeWorkablePage(fieldmap, boolean_groups={qa.key: group})

    report = workable.fill(page, fieldmap, values)

    assert all(w.clicked == 0 for w in group.wrappers)          # nothing was clicked
    assert (qa.key, workable_fill._OPTION_NOT_LOCATED_REASON) in report.skipped
    assert report.complete is False
    assert qa.key in {g["key"] for g in report.required_unfilled}
    # Parked, not driven: no false "value did not take" readback mismatch.
    assert not any(m["key"] == qa.key for m in report.readback_mismatches)


def test_fill_boolean_radio_scrolls_the_lazy_wrapper_into_view_before_clicking():
    # R5 residual: the radiogroup is lazy-rendered at the page BOTTOM, so the wrapper
    # is off-viewport and a click times out on actionability UNTIL it is scrolled into
    # view. `_drive_boolean_radio` scrolls first; drop that scroll and the click never
    # lands (the wrapper models a real off-viewport actionability timeout).
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    qa = {f.key: f for f in fieldmap.fields}["QA_11919111"]
    values = ResolvedValues(fields=[FieldValue(
        key=qa.key, label=qa.label, type=qa.type, locator=qa.locator, value=True)])
    group = _radio_group(qa.key, requires_scroll=True)
    page = _FakeWorkablePage(fieldmap, boolean_groups={qa.key: group})

    report = workable.fill(page, fieldmap, values)

    yes = group.wrappers[0]
    assert yes.scrolled >= 1                       # scrolled into view before the click
    assert yes.clicked == 1 and yes.aria_checked == "true"
    assert qa.key not in {g["key"] for g in report.required_unfilled}
    assert report.readback_mismatches == []


def test_fill_boolean_radio_dismisses_a_lazy_banner_that_renders_on_scroll():
    # R5 residual: the cookie-consent banner renders AFTER the fill-start recovery
    # (once the page is scrolled to the bottom form), so it overlays the wrapper and
    # intercepts the click. The fill-start dismissal cannot see it (not rendered yet);
    # the PER-DRIVE dismissal, which runs after the scroll, is what clears it. Drop
    # the per-drive dismissal and the click is intercepted -> parked.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    qa = {f.key: f for f in fieldmap.fields}["QA_11919111"]
    values = ResolvedValues(fields=[FieldValue(
        key=qa.key, label=qa.label, type=qa.type, locator=qa.locator, value=True)])
    banner = _FakeLazyCookieBanner()
    group = _radio_group(qa.key, requires_scroll=True, overlay=banner)
    page = _FakeWorkablePage(fieldmap, boolean_groups={qa.key: group},
                             cookie_banner_button=banner)

    report = workable.fill(page, fieldmap, values)

    yes = group.wrappers[0]
    # invisible at fill-start (never dismissed then), rendered on scroll, then
    # dismissed by the per-drive recovery BEFORE the click landed.
    assert banner.rendered is True and banner.clicked == 1
    assert yes.scrolled >= 1
    assert yes.clicked == 1 and yes.aria_checked == "true"
    assert qa.key not in {g["key"] for g in report.required_unfilled}


def test_fill_boolean_radio_parks_naming_the_blocking_element_on_click_timeout():
    # R5 residual: when the click STILL cannot land after scroll+dismiss (a persistent
    # overlay), the control is PARKED and the reason NAMES the obstructing node from
    # playwright's own call log -- so a census reads the overlay, not a bare timeout.
    # A required one stays a gap; nothing is falsely confirmed, no false readback miss.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    qa = {f.key: f for f in fieldmap.fields}["QA_11919111"]
    values = ResolvedValues(fields=[FieldValue(
        key=qa.key, label=qa.label, type=qa.type, locator=qa.locator, value=True)])
    group = _radio_group(qa.key, yes_click_error=(
        "locator.click: Timeout 30000ms exceeded.\nCall log:\n  - "
        '<div id="turnstile-overlay">...</div> intercepts pointer events'))
    page = _FakeWorkablePage(fieldmap, boolean_groups={qa.key: group})

    report = workable.fill(page, fieldmap, values)

    assert group.wrappers[0].clicked == 0          # the click never landed
    assert qa.key in {g["key"] for g in report.required_unfilled}
    qa_skips = [r for k, r in report.skipped if k == qa.key]
    assert len(qa_skips) == 1
    # The dedicated actionability park (NOT the generic "fill-error:" fallback):
    # the reason opens with the lazy/overlay context and then names the node.
    assert qa_skips[0].startswith(workable_fill._CLICK_BLOCKED_REASON)
    assert "intercepts pointer events" in qa_skips[0]     # the blocker is named ...
    assert "turnstile-overlay" in qa_skips[0]             # ... down to the node
    # parked (never driven): no false "value did not take" readback mismatch.
    assert not any(m["key"] == qa.key for m in report.readback_mismatches)
    assert report.complete is False


def test_fill_dismisses_the_dataui_cookie_modal_and_the_qa_click_lands():
    # FINAL residual (2026-07-19): the live cookie-consent MODAL is
    # <div data-ui="cookie-consent" role="dialog" aria-modal="true"> with hashed
    # classes; its buttons are identified ONLY by data-ui. The class/id selectors
    # can NEVER reach it (the trap that cost two live rounds), and its backdrop
    # overlays the page (aria-modal) and intercepts the QA click. The data-ui
    # dismissal removes it -- preferring DECLINE-optional (owner privacy policy),
    # never SETTINGS -- the backdrop goes invisible, and the QA click lands.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    qa = {f.key: f for f in fieldmap.fields}["QA_11919111"]
    values = ResolvedValues(fields=[FieldValue(
        key=qa.key, label=qa.label, type=qa.type, locator=qa.locator, value=True)])
    modal = _FakeCookieConsentModal()            # decline + accept + settings
    group = _radio_group(qa.key, overlay=modal)  # the modal covers the wrapper
    page = _FakeWorkablePage(fieldmap, boolean_groups={qa.key: group},
                             cookie_modal=modal)

    report = workable.fill(page, fieldmap, values)

    assert modal.button("decline").clicked == 1   # DECLINE preferred
    assert modal.button("accept").clicked == 0    # accept not needed when decline present
    assert modal.button("settings").clicked == 0  # settings NEVER clicked
    assert modal.dismissed is True and modal.is_covering() is False   # backdrop gone
    yes = group.wrappers[0]
    assert yes.clicked == 1 and yes.aria_checked == "true"            # QA click LANDS
    assert qa.key not in {g["key"] for g in report.required_unfilled}


def test_cookie_modal_is_immune_to_class_selectors_but_caught_by_dataui():
    # Pin the TRAP directly: the modal's hash-obfuscated classes mean the legacy
    # class/id selector matches ZERO buttons, while the data-ui selectors resolve
    # the decline/accept buttons -- and NONE of the selectors ever resolves the
    # settings button. If the fix regressed to class-only, dismissal would silently
    # do nothing (two live rounds lost exactly here).
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    modal = _FakeCookieConsentModal()
    page = _FakeWorkablePage(fieldmap, cookie_modal=modal)

    # the CLASS/ID fallback selector: zero matches against the hashed modal.
    assert base._visible_locators(page, workable_fill._COOKIE_CLASS_CSS) == []
    # the data-ui decline/accept selectors: each resolves exactly its own button.
    decline = base._visible_locators(page, workable_fill._COOKIE_DISMISS_SELECTORS[0])
    accept = base._visible_locators(page, workable_fill._COOKIE_DISMISS_SELECTORS[1])
    assert [b.get_attribute("data-ui") for b in decline] == ["cookie-consent-decline"]
    assert [b.get_attribute("data-ui") for b in accept] == ["cookie-consent-accept"]
    # NONE of the dismissal selectors ever resolves the settings button.
    for sel in workable_fill._COOKIE_DISMISS_SELECTORS:
        assert not any(b.get_attribute("data-ui") == "cookie-consent-settings"
                       for b in base._visible_locators(page, sel))


def test_cookie_modal_falls_back_to_accept_when_no_decline_button():
    # If the modal offers no decline-optional button, dismissal falls back to
    # ACCEPT (still never settings), so the backdrop still clears and the QA lands.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    qa = {f.key: f for f in fieldmap.fields}["QA_11919111"]
    values = ResolvedValues(fields=[FieldValue(
        key=qa.key, label=qa.label, type=qa.type, locator=qa.locator, value=True)])
    modal = _FakeCookieConsentModal(has_decline=False)   # accept + settings only
    group = _radio_group(qa.key, overlay=modal)
    page = _FakeWorkablePage(fieldmap, boolean_groups={qa.key: group},
                             cookie_modal=modal)

    report = workable.fill(page, fieldmap, values)

    assert modal.button("accept").clicked == 1
    assert modal.button("settings").clicked == 0
    assert modal.dismissed is True
    assert group.wrappers[0].clicked == 1
    assert qa.key not in {g["key"] for g in report.required_unfilled}


def test_fill_unrecognised_type_is_handed_off_not_typed():
    # A field type outside the known Workable vocabulary (a future SPA widget
    # this wave never sampled) must be HANDED OFF, never guessed as free text:
    # never-send bias means an unrecognised control is safer handed to a human
    # than blindly typed. `_workable_role_for_type` defaults an unrecognised
    # native type to "combobox" (a HAND-OFF role) precisely so this holds.
    raw = [{
        "name": "Custom questions",
        "fields": [{
            "id": "QA_9999", "required": True, "label": "Mystery widget",
            "type": "shiny_new_widget",
        }],
    }]
    fm = parse_workable(raw, "fauxco", "SYNTHETIC2", now=lambda: _PINNED)
    mystery = {f.key: f for f in fm.fields}["QA_9999"]
    assert mystery.required and mystery.locator.role == "combobox"

    # Feed a concrete guessed value directly (bypassing resolve_values' own
    # SSOT-match policy) to prove fill() itself refuses to auto-drive an
    # unrecognised-type control, mirroring the boolean hand-off test above.
    values = ResolvedValues(fields=[FieldValue(
        key=mystery.key, label=mystery.label, type=mystery.type,
        locator=mystery.locator, value="a sneaky guessed answer")])
    page = _FakeWorkablePage(fm)

    report = workable.fill(page, fm, values)

    assert report.complete is False
    gap = {g["key"]: g for g in report.required_unfilled}
    assert mystery.key in gap
    assert "handed off" in gap[mystery.key]["reason"]
    # fill() never requested a locator for it -- no false readback-fill: the
    # guessed value was never typed anywhere, so there is nothing to read back.
    assert not any(mystery.key in str(req) or mystery.label in str(req)
                   for req in page.requested)
    assert report.readback_mismatches == []
    assert report.caption().endswith("NOT COMPLETE")


def test_fill_readback_mismatch_on_required_field_forces_not_complete():
    # A required field whose value silently never takes (readback empty) must NOT
    # count as filled -> it surfaces as a genuine required gap + a readback mismatch.
    fieldmap = _fieldmap("form-57CFF1B2AF.json", "57CFF1B2AF")
    values = _resolved_values(fieldmap)
    page = _FakeWorkablePage(fieldmap, bad_keys={"email"})

    report = workable.fill(page, fieldmap, values)

    assert report.complete is False
    assert any(g["key"] == "email" for g in report.required_unfilled)
    assert any(m["key"] == "email" for m in report.readback_mismatches)


def test_fill_dom_sweep_extra_required_field_forces_not_complete():
    # GREENHOUSE semantics (Workable has an independent schema): the live sweep
    # shows a required control the schema did not carry. That dom_only mismatch
    # forces NOT_COMPLETE even though every schema-required field landed, and the
    # reason reads with the SCHEMA-oracle wording (not Lever's "authoritative").
    fieldmap = _fieldmap("form-57CFF1B2AF.json", "57CFF1B2AF")
    values = _resolved_values(fieldmap)
    page = _FakeWorkablePage(fieldmap, sweep_required_keys=(
        _POWERLINES_REQUIRED_KEYS + ("cover_letter",)))

    report = workable.fill(page, fieldmap, values)

    assert report.complete is False
    reasons = [g["reason"] for g in report.required_unfilled]
    assert any("absent from the schema" in r for r in reasons)
    assert report.caption().endswith("NOT COMPLETE")


def test_fill_never_send_interceptor_registered_before_any_field_access():
    fieldmap = _fieldmap("form-57CFF1B2AF.json", "57CFF1B2AF")
    values = _resolved_values(fieldmap)

    order = []

    class _OrderTrackingPage(_FakeWorkablePage):
        def route(self, pattern, handler):
            order.append("route")
            super().route(pattern, handler)

        def get_by_role(self, role, name=None):
            order.append("get_by_role")
            return super().get_by_role(role, name=name)

    page = _OrderTrackingPage(fieldmap)
    workable.fill(page, fieldmap, values)

    assert order[0] == "route"


def test_fill_raises_on_navigation_during_fill():
    fieldmap = _fieldmap("form-57CFF1B2AF.json", "57CFF1B2AF")
    values = _resolved_values(fieldmap)

    class _NavigatingPage(_FakeWorkablePage):
        def get_by_role(self, role, name=None):
            self._url = "https://apply.workable.com/fauxco/thanks"
            return super().get_by_role(role, name=name)

    page = _NavigatingPage(fieldmap)
    with pytest.raises(FillSafetyError, match="navigated during fill"):
        workable.fill(page, fieldmap, values)


# =============================================================================
# defensive OVERVIEW-tab recovery (W5.1-R2 FX2, OPTIONAL belt-and-braces)
#
# NO production apply-URL defect (production always navigates via
# provider.apply_url() = capture.py:98's /apply/ form URL). These pin the
# DEFENSIVE path only: a page still loaded on OVERVIEW (rokt posting,
# 2026-07-18 TOTO-GATE acceptance run: tab bar OVERVIEW | APPLICATION, plus a
# cookie-consent banner overlaying the form) reaches the APPLICATION form
# through `_maybe_recover_from_overview_tab`, and an already-on-APPLICATION
# page is left untouched (every OTHER test in this file proves that half
# implicitly, since none of them render a tab bar or banner at all).
# =============================================================================


def test_fill_recovers_from_overview_tab_and_dismisses_cookie_banner():
    # Models the live rokt anomaly: the page loads on OVERVIEW with the
    # APPLICATION tab present but unselected, and a cookie-consent banner
    # overlaying the form. fill() must dismiss the banner, activate the
    # APPLICATION tab, and then drive the required text fields exactly as it
    # does on an ordinary /apply/-loaded page -- reaching the SAME COMPLETE
    # outcome test_fill_completes_when_all_safe_required_land pins.
    fieldmap = _fieldmap("form-57CFF1B2AF.json", "57CFF1B2AF")
    values = _resolved_values(fieldmap)
    overview_tab = _FakeTabButton(name="Overview", selected=True)
    application_tab = _FakeTabButton(name="Application", selected=False)
    banner = _FakeCookieBanner()
    page = _FakeWorkablePage(fieldmap, tab_controls=[overview_tab, application_tab],
                             cookie_banner_button=banner)

    report = workable.fill(page, fieldmap, values)

    assert banner.clicked == 1
    assert application_tab.clicked == 1
    assert application_tab._selected is True
    assert report.complete is True
    assert report.required_unfilled == []
    assert report.caption().endswith("COMPLETE")


def test_fill_recovery_is_a_noop_when_application_tab_already_selected():
    # Belt-and-braces: "if the form is already present, do nothing." An
    # already-selected APPLICATION tab (the ordinary /apply/-loaded page) must
    # never be re-clicked.
    fieldmap = _fieldmap("form-57CFF1B2AF.json", "57CFF1B2AF")
    values = _resolved_values(fieldmap)
    application_tab = _FakeTabButton(name="Application", selected=True)
    page = _FakeWorkablePage(fieldmap, tab_controls=[application_tab])

    workable.fill(page, fieldmap, values)

    assert application_tab.clicked == 0


# =============================================================================
# never-send guard: the workable submit endpoint set (no live submit)
# =============================================================================


def test_never_send_aborts_the_real_apply_post():
    assert base._is_submit_request(
        "POST", "https://apply.workable.com/api/v1/jobs/57CFF1B2AF/apply",
        None) is True


def test_never_send_hardening_covers_custom_domain_and_eeoc():
    # (a) custom-domain / redirect tenant apply POST (host-agnostic shortcode path).
    assert base._is_submit_request(
        "POST", "https://careers.example.com/api/v2/jobs/ABCDEF/apply",
        None) is True
    # (b) the post-submit eeoc send (a second application-data POST after apply).
    assert base._is_submit_request(
        "POST", "https://apply.workable.com/api/v1/eeoc/send", None) is True


@pytest.mark.parametrize("method,url", [
    # the public schema read (a GET, never a submit)
    ("GET", "https://apply.workable.com/api/v1/jobs/57CFF1B2AF/form"),
    # the resume upload POST (an asset attach, not the application submit)
    ("POST", "https://apply.workable.com/api/v1/jobs/57CFF1B2AF/form/upload/resume"),
    # the S3 asset PUT (not even a POST)
    ("PUT", "https://workablehr.s3.amazonaws.com/uploads/abc123"),
    # the account job listing POST (discovery/read, not a submit)
    ("POST", "https://apply.workable.com/api/v3/accounts/powerlines/jobs"),
])
def test_never_send_allows_non_submit_workable_traffic(method, url):
    assert base._is_submit_request(method, url, None) is False


# =============================================================================
# DOM sweep: the LIVE control-anchor identity (W5B-WORKABLE first live contact)
#
# Live run 2026-07-13 (movement-labs 0F5F662A46): the kernel's accessible-name
# sweep named the SAME 18 controls four different ways at once -- the `name`
# attribute, a placeholder, an option's own text, an asterisked label with the
# intl-tel country code glued on -- so 17 PHANTOM dom-sweep gaps were booked
# against fields that were correctly filled. These tests pin the live DOM shape
# (apply-dom-0F5F662A46.json) and prove the key-space sweep reads it correctly
# WITHOUT losing its teeth: a genuinely missed required control still bites.
# =============================================================================


def test_dom_sweep_keys_on_live_control_anchors_not_the_kernel_name_guess():
    # The live DOM, control for control. Every required control resolves to its
    # SCHEMA KEY, so the cross-check compares like with like and finds no gap --
    # the 17 phantom gaps of the first live run are gone. What is left in
    # required_unfilled is only what is GENUINELY unfilled (the offline fake SSOT
    # answers no QA_ question), never a naming artefact.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    values = _resolved_values(fieldmap)
    page = _FakeWorkablePage(fieldmap, sweep_controls=_live_dom_controls())

    swept = workable_fill._workable_dom_required(page)
    assert swept == {f.key for f in fieldmap.required_fields()}
    assert len(swept) == 18

    # None of the kernel heuristic's alias families survives as a key. The aliases
    # are read from the fixture's OWN record of what base.sweep_required returned
    # on the live run, so the assertion cannot drift from the observation it claims
    # to check (round-4 m-1: the hardcoded tuple could assert a string the live page
    # never produced, and pass vacuously, without anything noticing).
    #
    # The placeholder family is LOCALE-DEPENDENT -- it is a property of the BROWSER
    # SESSION, not of the page. The live control served "DD/MM/YYYY" under the seal
    # host's browser (navigator.language=en-GB, probed twice on 2026-07-14, bare and
    # locale-pinned) and serves "MM/DD/YYYY" to an en-US browser. BOTH lowercased
    # forms are asserted, so this bites whatever locale a run resolves to.
    #
    # The `name_attribute` family is deliberately NOT asserted: two of its members
    # (`firstname`, `lastname`) ARE the schema keys, so there the kernel's guess
    # coincides with the key by accident. Every OTHER family is a name no schema key
    # can equal, and not one of them may survive.
    aliases = _LIVE_DOM["kernel_accessible_name_sweep_aliases"]
    noise = [a for family, items in aliases.items()
             if isinstance(items, list) and family != "name_attribute"
             for a in items]
    assert "dd/mm/yyyy" in noise and "mm/dd/yyyy" in noise
    assert set(noise) >= {"yes", "no", "phone +39"}
    for alias in noise + ["option", "first name", "Phone +39"]:
        assert alias not in swept

    report = workable.fill(page, fieldmap, values)
    assert [g for g in report.required_unfilled
            if str(g["key"]).startswith("dom-sweep:")] == []
    # The sweep is quiet, but the census is NOT: the unanswered QA_ questions and
    # the handed-off booleans still block completeness.
    assert report.complete is False
    assert any(g["key"] == "QA_11919111" for g in report.required_unfilled)


def test_dom_sweep_reads_only_the_twenty_controls_the_live_page_shows_it():
    # THE LIVE TOPOLOGY (re-derived off the page 2026-07-14, W5B-WORKABLE round 3).
    # 24 controls match the kernel's required CSS and ALL 24 are CSS-visible, but
    # four carry aria-hidden="true" -- and every one of those four is a native
    # input[type=radio][name=QA_n], which Workable hides from the accessibility tree
    # and paints its div[role=radio][data-ui=option] wrapper on top of.
    # `_visible_locators` drops an aria-hidden node, so the sweep reads 20, not 24.
    # The fixture omitted the attribute for two rounds, which handed the code four
    # `name` anchors the live page never shows the sweep: a friendlier world than the
    # real one, and the most dangerous kind (offline redundancy that live lacks).
    controls = _live_dom_controls()
    assert len(controls) == 24

    hidden = [c for c in controls if base._is_aria_hidden(c)]
    assert sorted(c.get_attribute("name") for c in hidden) == [
        "QA_11919111", "QA_11919111", "QA_11919112", "QA_11919112"]
    # They are the NATIVE radios (no role of their own), not the option wrappers.
    assert all(c.get_attribute("role") is None for c in hidden)

    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    page = _FakeWorkablePage(fieldmap, sweep_controls=controls)
    assert len(base._visible_locators(page, base._REQUIRED_CSS)) == 20


def test_dom_sweep_ignores_an_aria_hidden_or_invisible_required_control():
    # The sweep's VISIBILITY FILTER, isolated. A required control the page hides --
    # from the accessibility tree (aria-hidden) or from the eye (not visible) -- is
    # not a control a human can answer or the engine can drive, and the kernel drops
    # it before `_control_key` ever sees it.
    #
    # Pinning this needs a control whose key is NOT otherwise in the set. The four
    # aria-hidden natives on the real page SHARE their keys with the option wrappers,
    # so dropping the filter changes no key there and no assertion on the live
    # fixture could feel it. These ghosts carry keys the schema never had, so a sweep
    # that read them would book PHANTOM dom_only gaps against controls that are not
    # on the page in any sense that matters.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    values = _resolved_values(fieldmap)
    ghosts = [_FakeSweepControl(name="QA_99999999", aria_hidden="true"),
              _FakeSweepControl(name="QA_99999998", visible=False)]
    page = _FakeWorkablePage(fieldmap,
                             sweep_controls=_live_dom_controls() + ghosts)

    swept = workable_fill._workable_dom_required(page)
    report = workable.fill(page, fieldmap, values)

    assert "QA_99999999" not in swept and "QA_99999998" not in swept
    assert swept == {f.key for f in fieldmap.required_fields()}
    assert [g for g in report.required_unfilled
            if str(g["key"]).startswith("dom-sweep:")] == []

    # CONTRAST: the SAME control, neither hidden nor aria-hidden, DOES bite. So it
    # is the FILTER that excluded the ghosts above, not some other accident of the
    # key ladder swallowing them.
    page_seen = _FakeWorkablePage(fieldmap, sweep_controls=(
        _live_dom_controls() + [_FakeSweepControl(name="QA_99999999")]))
    seen_report = workable.fill(page_seen, fieldmap, values)
    assert any(g["key"] == "dom-sweep:qa_99999999"
               for g in seen_report.required_unfilled)


def test_dom_sweep_boolean_keys_hang_on_the_option_wrapper_anchor_alone():
    # THE CONSEQUENCE of the aria-hidden topology, and the map W5.1c inherits.
    # Live, each boolean key is anchored ONCE, by its option WRAPPERS. The native
    # input[type=radio] that carries the matching `name` is aria-hidden, so the sweep
    # never reads it: the `name` anchor is DEAD on this page for QA_11919111 and
    # QA_11919112. Break the wrapper path and both keys VANISH -- there is no second
    # anchor to cover for it, and the census loses two required fields. (Against the
    # pre-2026-07-14 fixture, which omitted aria-hidden, the phantom `name` anchor
    # DID cover for it, so this test could not have failed and the wrapper path was
    # pinned by nothing on the live shape.)
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    controls = _live_dom_controls()
    page = _FakeWorkablePage(fieldmap, sweep_controls=controls)

    swept = workable_fill._workable_dom_required(page)
    assert {"QA_11919111", "QA_11919112"} <= swept

    # Not one control the sweep can SEE carries either boolean's `name`.
    visible_to_sweep = [c for c in controls
                        if c.is_visible() and not base._is_aria_hidden(c)]
    assert not [c for c in visible_to_sweep
                if c.get_attribute("name") in ("QA_11919111", "QA_11919112")]

    # Drop the WRAPPERS (the sole live anchor) and both keys vanish, exactly as they
    # would live. They fail CLOSED: two schema_only gaps, never a silent pass.
    no_wrappers = [c for c in controls
                   if c.get_attribute("data-ui") != "option"]
    page_bare = _FakeWorkablePage(fieldmap, sweep_controls=no_wrappers)
    assert not ({"QA_11919111", "QA_11919112"}
                & workable_fill._workable_dom_required(page_bare))

    report = workable.fill(page_bare, fieldmap,
                           _resolved_values(fieldmap))
    gaps = {g["key"] for g in report.required_unfilled}
    assert "dom-sweep:qa_11919111" in gaps and "dom-sweep:qa_11919112" in gaps
    assert report.complete is False


def test_dom_sweep_option_wrapper_keys_to_its_radiogroup_not_the_option_marker():
    # A boolean's VISIBLE control is a div[role=radio][data-ui=option] wrapper: it
    # carries no name, and its data-ui is the marker "option" -- shared by every
    # option on the form, never a field id. Reading that marker as a key would
    # collapse four separate controls onto one bogus "option" field and bite as a
    # phantom gap, so the sweep asks the enclosing fieldset[data-ui] instead.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    wrappers = [_FakeSweepControl(data_ui="option", role="radio",
                                 closest_fieldset="QA_11919111"),
                _FakeSweepControl(data_ui="option", role="radio",
                                 closest_fieldset="QA_11919111")]
    page = _FakeWorkablePage(fieldmap, sweep_controls=wrappers)

    swept = workable_fill._workable_dom_required(page)

    assert swept == {"QA_11919111"}
    assert "option" not in swept


def test_dom_sweep_still_bites_on_a_required_control_absent_from_the_schema():
    # FALSIFICATION (anti-gaming): the page requires a control the schema GET
    # never carried -- a question the engine would otherwise leave blank without
    # noticing. The key-space sweep must still force NOT_COMPLETE.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    values = _resolved_values(fieldmap)
    page = _FakeWorkablePage(fieldmap, sweep_controls=(
        _live_dom_controls() + [_FakeSweepControl(name="QA_99999999")]))

    report = workable.fill(page, fieldmap, values)

    gaps = {g["key"]: g for g in report.required_unfilled}
    assert "dom-sweep:qa_99999999" in gaps
    assert "absent from the schema" in gaps["dom-sweep:qa_99999999"]["reason"]
    assert report.complete is False


def test_dom_sweep_still_bites_when_the_dom_does_not_require_a_schema_field():
    # FALSIFICATION (anti-gaming), the other direction: the schema says a field is
    # required but the live page renders no required control for it (selector
    # drift, a conditional the engine mis-read). Still NOT_COMPLETE.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    values = _resolved_values(fieldmap)
    kept = [c for c in _live_dom_controls()
            if c.get_attribute("name") != "QA_11919114"]
    page = _FakeWorkablePage(fieldmap, sweep_controls=kept)

    report = workable.fill(page, fieldmap, values)

    gaps = {g["key"]: g for g in report.required_unfilled}
    assert "dom-sweep:qa_11919114" in gaps
    assert "did not find it required" in gaps["dom-sweep:qa_11919114"]["reason"]
    assert report.complete is False


def test_dom_sweep_reconciles_an_uploaded_resume_that_dropped_its_required_marker(tmp_path):
    # LIVE artefact (movement-labs 0F5F662A46, 2026-07-13): once the resume
    # ATTACHES, Workable CLEARS the file input's required marker, so the post-fill
    # sweep no longer sees "resume" as required while the schema still does -- a
    # benign schema_only disagreement for a field that is genuinely satisfied. A
    # successfully-uploaded key is reconciled back, so it does not book a gap.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    values = _resolved_values(fieldmap, tmp_path=tmp_path)  # a CV asset -> resume uploads
    # The post-upload live DOM: every required control EXCEPT resume (its marker
    # was cleared once the file attached).
    post_upload = [c for c in _live_dom_controls()
                   if c.get_attribute("data-ui") != "resume"]
    page = _FakeWorkablePage(fieldmap, sweep_controls=post_upload)

    report = workable.fill(page, fieldmap, values)

    assert any(u["key"] == "resume" for u in report.uploads)   # it DID attach
    assert not any(g["key"] == "dom-sweep:resume"
                   for g in report.required_unfilled)           # ... so no phantom gap
    assert not any(str(g["key"]).startswith("dom-sweep:")
                   for g in report.required_unfilled)


def test_dom_sweep_still_bites_on_a_required_upload_that_did_not_attach(tmp_path):
    # FALSIFICATION (anti-gaming): the reconcile is UPLOAD-GATED. A required
    # resume whose file never attached is absent from `uploads`, so its
    # schema_only gap still bites -- the reconcile can never hide a real gap.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    dead = _FakeDeadFileInput(id="resume", name="resume", accept=".pdf")
    post_upload = [c for c in _live_dom_controls()
                   if c.get_attribute("data-ui") != "resume"]
    page = _FakeWorkablePage(fieldmap, sweep_controls=post_upload,
                             file_inputs=[dead])

    report = workable.fill(page, fieldmap, values)

    assert not any(u["key"] == "resume" for u in report.uploads)  # did NOT attach
    gaps = {g["key"]: g for g in report.required_unfilled}
    assert "dom-sweep:resume" in gaps
    assert "did not find it required" in gaps["dom-sweep:resume"]["reason"]
    assert report.complete is False


def test_dom_sweep_still_bites_on_a_missing_schema_field_when_an_upload_attached(tmp_path):
    # FALSIFICATION (anti-gaming): the fold's PROVEN-ATTACHMENT half, isolated.
    #
    # The invariant has two halves -- a key is folded back ONLY when it is (a) a
    # PROVEN attachment AND (b) SCHEMA-REQUIRED. Half (b) is pinned by the
    # optional-upload test below. Half (a) was pinned ONLY through the `if not
    # uploads` early return, i.e. only on forms where NOTHING uploaded, and that
    # leaves a hole: widen the fold from `uploaded_keys & schema_required` to
    # `schema_required` and the early return still covers the no-upload tests, while
    # on ANY form where a file DID attach, EVERY schema-required field the page does
    # not require is folded back and its schema_only gap is SILENCED. That is a
    # fail-open on the exact axis the sweep exists to guard, and it slipped the whole
    # suite until this test (W5B-WORKABLE round 3).
    #
    # Here the resume attaches WHILE the page stops requiring an essay the schema
    # requires. The essay's gap must still bite: a file landing elsewhere on the form
    # is no evidence whatever about a text field.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    values = _resolved_values(fieldmap, tmp_path=tmp_path)  # a CV asset -> resume uploads
    kept = [c for c in _live_dom_controls()
            if c.get_attribute("name") != "QA_11919114"]
    page = _FakeWorkablePage(fieldmap, sweep_controls=kept)

    report = workable.fill(page, fieldmap, values)

    assert any(u["key"] == "resume" for u in report.uploads)   # the upload DID attach
    gaps = {g["key"]: g for g in report.required_unfilled}
    assert "dom-sweep:qa_11919114" in gaps                     # ... and the essay STILL bites
    assert "did not find it required" in gaps["dom-sweep:qa_11919114"]["reason"]
    assert report.complete is False


def test_dom_sweep_optional_upload_that_attached_books_no_phantom_gap(tmp_path):
    # THE OTHER HALF of the reconcile invariant (round-2 finding, proved on the
    # REAL sampled powerlines and io-global schemas): the fold ADDS to the DOM
    # side, and `completeness_mismatch` books any DOM-side key the schema does not
    # REQUIRE as a `dom_only` gap. So an unbounded union invented a phantom
    # required_unfilled entry against a file field that uploaded PERFECTLY and
    # that the schema marks OPTIONAL -- the very defect class (a phantom gap on a
    # correctly-filled field) the key-space sweep was written to kill. powerlines
    # carries exactly that shape: BOTH its file fields are optional.
    fieldmap = _fieldmap("form-57CFF1B2AF.json", "57CFF1B2AF")
    by_key = {f.key: f for f in fieldmap.fields}
    assert not by_key["resume"].required and not by_key["avatar"].required
    values = _resolved_values(fieldmap, tmp_path=tmp_path)  # assets -> both upload
    # The post-upload live DOM: only the three required TEXT controls carry a
    # required marker (an attached file input drops its own, and neither file
    # field is schema-required here anyway).
    page = _FakeWorkablePage(fieldmap, sweep_required_keys=_POWERLINES_REQUIRED_KEYS)

    report = workable.fill(page, fieldmap, values)

    assert {u["key"] for u in report.uploads} == {"resume", "avatar"}  # both ATTACHED
    assert [g for g in report.required_unfilled
            if str(g["key"]).startswith("dom-sweep:")] == []
    assert report.complete is True


def test_dom_sweep_still_bites_on_a_nameless_required_control_in_a_radiogroup():
    # FALSIFICATION (anti-gaming), the absorption case: a required control the
    # page carries and the SCHEMA DOES NOT, with no `name` of its own, nested
    # inside a known radiogroup fieldset. An ungated fieldset fallback keyed it to
    # THAT GROUP -- a key the schema already has -- so a foreign required control
    # was swallowed whole and booked no gap. Only the group's own OPTION WRAPPERS
    # may borrow the group's key; anything else keeps its own identity and bites.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    values = _resolved_values(fieldmap)
    intruder = _FakeSweepControl(aria_label="Mystery consent",
                                 closest_fieldset="QA_11919111")
    page = _FakeWorkablePage(fieldmap, sweep_controls=(
        _live_dom_controls() + [intruder]))

    report = workable.fill(page, fieldmap, values)

    gaps = {g["key"]: g for g in report.required_unfilled}
    assert "dom-sweep:mystery consent" in gaps
    assert "absent from the schema" in gaps["dom-sweep:mystery consent"]["reason"]
    assert report.complete is False


def test_dom_sweep_still_bites_on_a_role_bearing_foreign_control_in_a_radiogroup():
    # The option-wrapper gate's PRECISION, not just its existence.
    # `_is_option_wrapper` admits a control carrying the `option` MARKER or the
    # `radio` ROLE, and nothing else. A foreign required control that merely SITS
    # inside the fieldset while carrying some OTHER role (here a textbox) is not one
    # of the group's options: widen the gate to "any control with a role" and it
    # borrows the group's key -- a key the schema already has -- so a required
    # control the page carries and the schema does NOT is swallowed whole and books
    # no gap. That is precisely the absorption the `dom_only` direction exists to
    # catch. The existing nameless-intruder test cannot see this: its intruder
    # carries no role at all, so the widened gate rejects it for the wrong reason.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    values = _resolved_values(fieldmap)
    intruder = _FakeSweepControl(aria_label="Mystery textbox", role="textbox",
                                 closest_fieldset="QA_11919111")
    page = _FakeWorkablePage(fieldmap, sweep_controls=(
        _live_dom_controls() + [intruder]))

    swept = workable_fill._workable_dom_required(page)
    report = workable.fill(page, fieldmap, values)

    # It keeps its OWN identity (its accessible name), never the radiogroup's key.
    assert "Mystery textbox" in swept
    gaps = {g["key"]: g for g in report.required_unfilled}
    assert "dom-sweep:mystery textbox" in gaps
    assert "absent from the schema" in gaps["dom-sweep:mystery textbox"]["reason"]
    assert report.complete is False


def test_dom_sweep_control_with_no_anchor_at_all_still_bites():
    # A required control carrying NO anchor the schema could name (no name, no
    # data-ui, no radiogroup) falls back to its accessible name -- which is no
    # schema key, so it bites as a dom_only gap. An unidentifiable required
    # control must never pass silently.
    fieldmap = _fieldmap("form-57CFF1B2AF.json", "57CFF1B2AF")
    values = _resolved_values(fieldmap)
    page = _FakeWorkablePage(fieldmap, sweep_controls=(
        [_FakeSweepControl(name=key) for key in _POWERLINES_REQUIRED_KEYS]
        + [_FakeSweepControl(aria_label="Mystery consent")]))

    report = workable.fill(page, fieldmap, values)

    gaps = {g["key"]: g for g in report.required_unfilled}
    assert "dom-sweep:mystery consent" in gaps
    assert report.complete is False


# =============================================================================
# phone: the intl-tel-input readback (W5B-WORKABLE first live contact)
#
# Live run 2026-07-13: the phone box moved the typed "+39" country calling code
# into its own country combobox and reformatted the rest, so 12 typed digits read
# back as 10. The exact-string readback called the landed number a miss and the
# run booked a filled Phone as required_unfilled ("value did not take").
# =============================================================================


def _phone_fieldmap():
    """A one-field synthetic Workable form whose only field is the phone box."""
    raw = [{"name": "Personal information", "fields": [
        {"id": "phone", "required": True, "label": "Phone", "type": "phone"}]}]
    return parse_workable(raw, "fauxco", "PHONE01", now=lambda: _PINNED)


def _phone_values(fieldmap, value=_FAKE_INTL_PHONE):
    fld = {f.key: f for f in fieldmap.fields}["phone"]
    return ResolvedValues(fields=[FieldValue(
        key=fld.key, label=fld.label, type=fld.type, locator=fld.locator,
        value=value)])


def test_phone_readback_accepts_the_intl_tel_calling_code_absorption():
    # The LIVE shape: the whole number is typed, the widget keeps the national
    # part and moves the calling code to its own combobox (never driven). The
    # number DID land, so it must count as filled -- no gap, no mismatch.
    fieldmap = _phone_fieldmap()
    values = _phone_values(fieldmap)
    page = _FakeWorkablePage(fieldmap)

    report = workable.fill(page, fieldmap, values)

    control = page.controls[("textbox", "Phone")]
    assert control.value == _FAKE_INTL_PHONE          # every keystroke was sent
    assert control.input_value() != _FAKE_INTL_PHONE  # the widget rewrote it
    assert report.filled == 1
    assert report.required_unfilled == []
    assert report.readback_mismatches == []
    assert report.complete is True


def test_phone_readback_rejects_an_empty_phone_control():
    # ANTI-GAMING: the tolerance is for a REWRITTEN number, never for a missing
    # one. A phone box that took nothing is still a required gap.
    fieldmap = _phone_fieldmap()
    values = _phone_values(fieldmap)
    page = _FakeWorkablePage(fieldmap,
                             phone_locator=_FakeIntlTelLocator(blank=True))

    report = workable.fill(page, fieldmap, values)

    assert report.filled == 0
    assert any(g["key"] == "phone" for g in report.required_unfilled)
    assert any(m["key"] == "phone" for m in report.readback_mismatches)
    assert report.complete is False


def test_phone_readback_rejects_an_empty_box_for_a_short_intended_number():
    # ANTI-GAMING, the EMPTY-READBACK guard ISOLATED. `_phone_landed` rejects an
    # empty box TWICE over: the explicit `not got` guard, and -- for a real 7-to-15
    # digit international number -- the LENGTH BOUND, since every digit went missing
    # and that is far more than a 1-to-3 digit calling code. The existing empty-box
    # test types a full international number, so the length bound alone rejects it
    # and the guard the test is named for is never exercised: delete `not got` and
    # that test stays green.
    #
    # A SHORT intended number (3 digits, exactly the width of a calling code) holds
    # the length bound SATISFIED, so only `not got` can reject it. Without the guard,
    # an EMPTY phone box confirms as LANDED: the page would carry no number at all
    # while the census called the field filled.
    fieldmap = _phone_fieldmap()
    values = _phone_values(fieldmap, value="555")
    page = _FakeWorkablePage(fieldmap,
                             phone_locator=_FakeIntlTelLocator(blank=True))

    report = workable.fill(page, fieldmap, values)

    assert report.filled == 0
    assert any(g["key"] == "phone" for g in report.required_unfilled)
    assert any(m["key"] == "phone" for m in report.readback_mismatches)
    assert report.complete is False


def test_phone_readback_rejects_a_phone_the_page_truncated():
    # ANTI-GAMING: digits missing off the END (a truncating maxlength, a value the
    # page partly swallowed) are NOT a calling-code absorption. Only a 1-to-3
    # digit prefix may go missing, and only off the front.
    fieldmap = _phone_fieldmap()
    values = _phone_values(fieldmap)
    page = _FakeWorkablePage(
        fieldmap, phone_locator=_FakeIntlTelLocator(drop_trailing=3))

    report = workable.fill(page, fieldmap, values)

    assert report.filled == 0
    assert any(g["key"] == "phone" for g in report.required_unfilled)
    assert report.complete is False


def test_phone_readback_accepts_exactly_three_absorbed_leading_digits():
    # BOUNDARY, the PASS edge. A country calling code is 1 to 3 digits (ITU-T
    # E.164), so 3 absorbed off the FRONT is the WIDEST readback the tolerance may
    # ever confirm. Paired with its FAIL twin below, this is what holds
    # _MAX_CALLING_CODE_DIGITS at 3: on its own, neither test pins the bound.
    fieldmap = _phone_fieldmap()
    values = _phone_values(fieldmap)
    page = _FakeWorkablePage(
        fieldmap, phone_locator=_FakeDigitRewritingLocator(absorb_leading=3))

    report = workable.fill(page, fieldmap, values)

    assert report.filled == 1
    assert report.required_unfilled == []
    assert report.readback_mismatches == []
    assert report.complete is True


def test_phone_readback_rejects_four_absorbed_leading_digits():
    # BOUNDARY, the FAIL edge (ANTI-GAMING). No calling code is 4 digits long, so
    # a box missing 4 leading digits lost part of the NUMBER, not just its
    # country code, and must never confirm. This is the ONLY case that distinguishes
    # the shipped bound of 3 from a widened one: at 4 absorbed the value is still a
    # true suffix of what was typed, so the front-only guard passes it and the
    # LENGTH BOUND ALONE is what rejects it.
    fieldmap = _phone_fieldmap()
    values = _phone_values(fieldmap)
    page = _FakeWorkablePage(
        fieldmap, phone_locator=_FakeDigitRewritingLocator(absorb_leading=4))

    report = workable.fill(page, fieldmap, values)

    assert report.filled == 0
    assert any(g["key"] == "phone" for g in report.required_unfilled)
    assert any(m["key"] == "phone" for m in report.readback_mismatches)
    assert report.complete is False


def test_phone_readback_rejects_a_different_number_of_absorbable_length():
    # ANTI-GAMING, the FRONT-ONLY guard ISOLATED. The readback here is exactly as
    # long as a LEGAL calling-code absorption (2 digits short of the intended
    # number), so the length bound is SATISFIED and lets it through; only the
    # `want.endswith(got)` suffix check can reject it. Without that check the
    # engine books a COMPLETELY DIFFERENT phone number as successfully filled.
    fieldmap = _phone_fieldmap()
    values = _phone_values(fieldmap)
    page = _FakeWorkablePage(fieldmap, phone_locator=_FakeDigitRewritingLocator(
        readback="555 0199 456"))

    report = workable.fill(page, fieldmap, values)

    assert report.filled == 0
    assert any(g["key"] == "phone" for g in report.required_unfilled)
    assert any(m["key"] == "phone" for m in report.readback_mismatches)
    assert report.complete is False


def test_phone_readback_rejects_digits_dropped_off_the_end_alone():
    # ANTI-GAMING, the END-ABSORPTION guard ISOLATED. The widget keeps the calling
    # code and drops 2 digits off the END -- a truncating maxlength, a value the
    # page partly swallowed. Exactly 2 digits go missing, so the length bound is
    # SATISFIED (an absorption of that size is legal off the FRONT) and only the
    # direction of the check rejects it. A tolerance that absorbed off the end too
    # would call a truncated number landed, and the page would send a phone number
    # that is not the owner's.
    fieldmap = _phone_fieldmap()
    values = _phone_values(fieldmap)
    page = _FakeWorkablePage(
        fieldmap, phone_locator=_FakeDigitRewritingLocator(drop_trailing=2))

    report = workable.fill(page, fieldmap, values)

    assert report.filled == 0
    assert any(g["key"] == "phone" for g in report.required_unfilled)
    assert any(m["key"] == "phone" for m in report.readback_mismatches)
    assert report.complete is False


def test_phone_tolerance_never_leaks_to_a_non_phone_field():
    # ANTI-GAMING: the retry is gated on the `phone` TYPE. A plain text field
    # whose control rewrites the value exactly the same way still fails its
    # readback -- no other field inherits a loose comparison.
    raw = [{"name": "Details", "fields": [
        {"id": "QA_2001", "required": True, "label": "Reference code",
         "type": "text"}]}]
    fieldmap = parse_workable(raw, "fauxco", "TEXT01", now=lambda: _PINNED)
    fld = {f.key: f for f in fieldmap.fields}["QA_2001"]
    values = ResolvedValues(fields=[FieldValue(
        key=fld.key, label=fld.label, type=fld.type, locator=fld.locator,
        value=_FAKE_INTL_PHONE)])
    page = _FakeWorkablePage(fieldmap)
    # Same rewriting widget, on a field the schema types `text`, not `phone`.
    page.controls[("textbox", "Reference code")] = _FakeIntlTelLocator()

    report = workable.fill(page, fieldmap, values)

    assert report.filled == 0
    assert any(g["key"] == "QA_2001" for g in report.required_unfilled)
    assert any(m["key"] == "QA_2001" for m in report.readback_mismatches)
    assert report.complete is False


# =============================================================================
# THE HUMAN HAND-OFF, clause by clause (W5B-WORKABLE round 4)
#
# `_needs_human_handoff` is the campaign's core Turnstile fail-safe and it has
# THREE independent clauses: a bool VALUE, a click-hazard ROLE, and a flattened
# GROUP subfield. Every boolean test above feeds `value=True`, so the BOOL clause
# covers for the other two: a round-3 mutation battery deleted the "radio" role
# entry (M21) and neutered `_is_group_subfield` (M24), SEPARATELY AND TOGETHER,
# with the suite green at 729 passed. These tests close that.
# =============================================================================


def test_needs_human_handoff_each_hazard_clause_is_load_bearing_alone():
    # RETARGETED (W5.1c): `_needs_human_handoff`'s hazard set NARROWED. The bool-
    # value clause is GONE from this function entirely -- fill()'s own dispatch
    # now decides a boolean's route on `fv.type`, BEFORE this check is ever
    # reached (pinned by the boolean-drive tests above and the locator-
    # resolution invariant below), and `_HANDOFF_ROLES` dropped "radio" and
    # "checkbox" (pinned end to end by the untouched
    # test_fill_radio_role_with_a_non_bool_value_is_still_handed_off_never_typed,
    # a round-3 mutant survivor this file must not re-break).
    #
    # What remains is TWO clauses -- a ROLE in {combobox, listbox}, and a DOTTED
    # group-subfield key -- and the SAME "each clause is load-bearing alone"
    # rigor applies to them: each row activates EXACTLY ONE clause and holds the
    # other inert, so deleting either clause, or narrowing `_HANDOFF_ROLES` to
    # just ONE of its two remaining members, fails a row here.
    cases = [
        ("workable's CUSTOM dropdown widget (unsampled option DOM): the ROLE "
         "clause, its combobox member alone",
         "QA_4", "combobox", "London", True),
        ("workable's CUSTOM multiple-choice widget: the ROLE clause, its "
         "listbox member alone",
         "QA_5", "listbox", "English", True),
        ("a flattened GROUP subfield: its role is textbox (NOT a hazard role), "
         "so the DOTTED-KEY clause is the only thing standing between it and "
         "type_human typing into a group whose '+ Add' opener was never driven",
         "education.school", "textbox", "A School", True),
        ("the NEGATIVE control: a plain text box on a plain key is DRIVEN, "
         "never handed off -- neither clause fires",
         "firstname", "textbox", "Test", False),
    ]
    for why, key, role, value, expected in cases:
        fv = FieldValue(key=key, label="Label", type="text",
                        locator=Locator(role=role, name="Label"), value=value)
        assert workable_fill._needs_human_handoff(fv) is expected, why


def test_fill_radio_role_with_a_non_bool_value_is_still_handed_off_never_typed():
    # THE ROLE PATH, end to end (round-3 mutant M21, a survivor). Delete "radio"
    # from `_HANDOFF_ROLES` and the shipped code hands this field off while the
    # mutant TYPES it: a programmatic keystroke into a Turnstile-protected radio on
    # the live seal target. The existing boolean test cannot see it, because a
    # `True` value is caught by the bool clause first.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    qa = {f.key: f for f in fieldmap.fields}["QA_11919111"]
    assert qa.required and qa.locator.role == "radio"
    values = ResolvedValues(fields=[FieldValue(
        key=qa.key, label=qa.label, type=qa.type, locator=qa.locator,
        value="Yes")])
    assert not isinstance(values.fields[0].value, bool)  # the bool clause is INERT
    page = _FakeWorkablePage(fieldmap)

    report = workable.fill(page, fieldmap, values)

    assert report.complete is False
    gap = {g["key"]: g for g in report.required_unfilled}
    assert qa.key in gap
    assert "Turnstile" in gap[qa.key]["reason"]   # handed off, not a fill-error
    # No locator was ever requested for it: nothing was clicked, nothing typed,
    # nothing read back.
    assert not any(qa.key in str(req) or qa.label in str(req)
                   for req in page.requested)
    assert report.readback_mismatches == []


def test_fill_group_subfield_is_handed_off_never_typed():
    # THE GROUP PATH, end to end (round-3 mutant M24, a survivor). A group
    # subfield's role is "textbox", which is NOT a hand-off role, so
    # `_is_group_subfield` is the ONLY guard here. Group fields are REAL and already
    # in the corpus: powerlines carries education + experience, io-global carries
    # education. The guard survived untested only because no test ever resolved a
    # value for one -- and W5.1d, which authors canned answers, is very plausibly
    # the wave that first does (an education subfield is exactly what an SSOT can
    # answer).
    fieldmap = _fieldmap("form-57CFF1B2AF.json", "57CFF1B2AF")
    sub = next(f for f in fieldmap.fields
               if "." in f.key and f.locator.role == "textbox")
    assert sub.locator.role not in workable_fill._HANDOFF_ROLES  # the role DRIVES

    values = ResolvedValues(fields=[FieldValue(
        key=sub.key, label=sub.label, type=sub.type, locator=sub.locator,
        value="an answer a canned SSOT could plausibly supply")])
    page = _FakeWorkablePage(fieldmap)
    # The fake DOES build a text control for it (its role is textbox), so a fill
    # that failed to hand it off would type into it cleanly and read it back
    # cleanly: exactly the SILENT success this guard exists to prevent.
    control = page.controls[(sub.locator.role, sub.locator.name)]

    report = workable.fill(page, fieldmap, values)

    assert (sub.key, workable_fill._HUMAN_HANDOFF_REASON) in report.skipped
    assert report.filled == 0
    assert control.value == ""                    # not one keystroke was sent
    assert not any(sub.key in str(req) or sub.locator.name in str(req)
                   for req in page.requested)
    assert report.readback_mismatches == []


def test_dom_sweep_orphan_option_marker_never_becomes_a_schema_key():
    # The `data_ui != _OPTION_MARKER` guard in `_control_key`, pinned (round-3
    # mutant M10, a survivor). "option" is a ROLE MARKER shared by every option on
    # the form, never a field id. A control carrying it with no name and no
    # enclosing radiogroup -- an option torn out of its fieldset by a Workable
    # release, a stray marker -- must fall through to its own accessible name. Drop
    # the guard and it keys to "option": a bogus field id that every such control on
    # the form would share.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    values = _resolved_values(fieldmap)
    orphan = _FakeSweepControl(data_ui="option", role="radio",
                               closest_fieldset="", aria_label="Stray option")
    page = _FakeWorkablePage(fieldmap,
                             sweep_controls=_live_dom_controls() + [orphan])

    swept = workable_fill._workable_dom_required(page)

    assert "option" not in swept        # the shared MARKER is never a key ...
    assert "Stray option" in swept      # ... the control keeps its own identity ...

    report = workable.fill(page, fieldmap, values)
    gaps = {g["key"] for g in report.required_unfilled}
    assert "dom-sweep:stray option" in gaps      # ... and bites as a dom_only gap
    assert "dom-sweep:option" not in gaps
    assert report.complete is False


# =============================================================================
# THE LOCATOR-RESOLUTION INVARIANT (W5B-WORKABLE round 4)
#
# For EVERY field the capture emits that fill() does not hand off, the locator the
# PRODUCTION code builds must resolve to EXACTLY ONE element on the live page. Not
# "at least one". ZERO is a PHANTOM: the engine believes it filled a control that
# does not exist. MORE THAN ONE is an ambiguity: it may type into the wrong
# control. A locator is a PAIR (role, name) and a phantom can hide in EITHER half.
#
# The resolution is not re-implemented offline (that would be an approximation of
# playwright's accessible-name matching). It is REPLAYED from the live probe
# recorded in `apply-dom-0F5F662A46.json::locator_resolution` (2026-07-14, en-GB,
# read-only, two browser contexts in agreement), keyed on the exact (role, name)
# pair the production locator asks for. The DECISIONS -- upload, hand-off, drive --
# are taken by the REAL shipped code, not by the harness.
# =============================================================================


_LIVE_LOCATORS = _LIVE_DOM["locator_resolution"]


class _LiveResolvedLocator:
    """What `page.get_by_role(role, name=...)` matched on the LIVE page: how many
    elements, and the own DOM anchor of each."""

    def __init__(self, role, name, row):
        self.role, self.name = role, name
        self.count = row["count"]
        self.matched = list(row["matched"])


class _LiveResolvingPage:
    """A page whose `get_by_role` REPLAYS the live resolution rather than guessing
    it. A (role, name) pair the live probe never resolved is a KeyError, deliberately:
    a locator the fill code builds that nobody has ever resolved against the real
    page is exactly what must not pass silently.

    `.locator(radiogroup_css)` supports the boolean branch's OWN two-step
    construction (`_radiogroup_option`): the live probe recorded only the
    FIELD-LEVEL construction (see `_LiveOptionSet` below for why the fieldset-
    scoped, visible-text resolution is DERIVED rather than probed)."""

    def __init__(self):
        self._table = {(r["role"], r["locator_name"]): r
                       for r in _LIVE_LOCATORS["fields"]}
        self.requested = []

    def get_by_role(self, role, name=None):
        self.requested.append((role, name))
        return _LiveResolvedLocator(role, name, self._table[(role, name)])

    def get_by_label(self, label):
        raise AssertionError(
            "the workable fill path must locate by ROLE + NAME; falling back to "
            "get_by_label means the fieldmap lost its locator hint")

    def locator(self, css):
        fieldset = re.match(
            r'^fieldset\[data-ui="([^"]+)"\]\[role="radiogroup"\]$', css)
        assert fieldset, f"unexpected locator scope css: {css!r}"
        return _LiveRadioGroup(fieldset.group(1))


class _LiveRadioGroup:
    """What `page.locator('fieldset[data-ui="<key>"][role="radiogroup"]')` returns:
    a SCOPE, resolved no further until `.locator(option_css)` narrows it to the
    group's option wrappers, mirroring `_radiogroup_option`'s own construction."""

    def __init__(self, key):
        self._key = key

    def locator(self, css):
        assert css == workable_fill._OPTION_WRAPPER_CSS, css
        return _LiveOptionSet(self._key)


# The two distinct option words the LIVE page carries anywhere on it (recorded by
# the kernel sweep's alias record): a boolean fieldset's two wrappers are one of
# each, so a Yes/No `has_text` filter over them keeps exactly one.
_LIVE_OPTION_TEXTS = _LIVE_DOM["kernel_accessible_name_sweep_aliases"]["option_text"]


class _LiveOptionSet:
    """The option wrappers of ONE radiogroup, DERIVED (not probed) from the
    fixture. `locator_resolution._how` records the live probe resolved only the
    FIELD-LEVEL locator (`get_by_role(role, name=label)`), which matched BOTH
    wrappers (count 2) because each wrapper's accessible name carries the GROUP
    label -- the exact reason the fieldset-scoped, VISIBLE-TEXT construction exists
    and the old exact-name one timed out on zero matches.

    `controls` pins exactly TWO option wrappers per boolean fieldset (role=radio,
    data-ui=option, closest_fieldset==key). The fixture carries no per-wrapper
    text, but the page-wide `option_text` alias pins exactly the two DISTINCT texts
    {yes, no} and no third value anywhere on the page, so the two wrappers are one
    of each. `.filter(has_text=...)` over those two therefore keeps exactly ONE for
    a Yes/No pattern: never zero, never both."""

    def __init__(self, key, texts=None):
        wrappers = [c for c in _LIVE_DOM["controls"]
                    if c.get("role") == "radio" and c.get("data_ui") == "option"
                    and c.get("closest_fieldset_data_ui") == key]
        assert len(wrappers) == 2, (
            f"{key}: expected exactly 2 option wrappers in the fieldset scope, "
            f"found {len(wrappers)}")
        self._key = key
        self._texts = list(_LIVE_OPTION_TEXTS) if texts is None else list(texts)

    def filter(self, has_text=None):
        if has_text is None:
            return _LiveOptionSet(self._key, self._texts)
        return _LiveOptionSet(
            self._key, [t for t in self._texts if _has_text_match(has_text, t)])

    def count(self):
        return len(self._texts)


def _live_field_values(fieldmap):
    """One FieldValue per schema field, carrying the locator name the LIVE page was
    probed with (the label the schema served in the SAME session the DOM was read).

    A file field gets a `Path`, so `_is_upload` routes it exactly as a real run
    does. A required BOOLEAN gets a genuine `bool` (W5.1c): fill()'s own dispatch
    decides a boolean's route on `fv.type`, BEFORE `_needs_human_handoff` is ever
    reached, and only a real bool exercises the DRIVE path
    (`_drive_boolean_radio`, which clicks the option wrapper) this invariant means
    to check --
    a boolean resolved to a non-bool value is a SEPARATE hazard, pinned by its
    own dedicated test above, not by this one. EVERY other field gets a plain
    STRING, never a bool."""
    labels = {r["key"]: r["locator_name"] for r in _LIVE_LOCATORS["fields"]}
    return [FieldValue(
        key=f.key, label=f.label, type=f.type,
        locator=Locator(role=f.locator.role, name=labels[f.key]),
        value=(Path("/nonexistent/cv-ats.pdf") if f.type == "file"
               else True if f.type == workable_fill._BOOLEAN_TYPE
               else "an answer")) for f in fieldmap.fields]


def test_locator_resolution_invariant_every_driven_field_resolves_to_exactly_one():
    # RETARGETED (W5.1c). Live, 2026-07-14, en-GB: 18 fields. This form carries no
    # dropdown/multiple/group field, so the shipped code UPLOADS 1 (resume) and
    # DRIVES the remaining 17: 15 plain text-class fields (+ the date box) via
    # the field-level `base._locate`, and the 2 required booleans via the
    # fieldset-SCOPED, visible-text `_radiogroup_option`. EVERY one of the 17
    # resolves to EXACTLY ONE element on the live page, whichever construction
    # fill() actually uses for it.
    #
    # THE MODEL WAS STALE: pre-W5.1c it routed every non-upload, non-hand-off
    # field through `base._locate` alone -- exactly what fill() itself did before
    # the shared kernel control mechanism existed, and exactly what fill() no
    # longer does for a boolean (that branch runs BEFORE `_needs_human_handoff`
    # is ever reached; `engine/providers/workable/fill.py`'s own dispatch order).
    # Mirroring that order here makes the invariant STRONGER, not weaker: the
    # boolean's own scoped locator is now held to the SAME "exactly one" bar as
    # every other driven field, where before it was never checked at all.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    page = _LiveResolvingPage()

    # The DOM fixture and the capture fixture must describe the SAME form: same
    # keys, same roles. (A LABEL may drift, and one has -- see _label_drift and the
    # test below. A KEY or a ROLE drifting means they are no longer the same page.)
    recorded = {r["key"]: r for r in _LIVE_LOCATORS["fields"]}
    assert set(recorded) == {f.key for f in fieldmap.fields}
    assert all(recorded[f.key]["role"] == f.locator.role for f in fieldmap.fields)

    def _assert_resolves_uniquely(key, locator):
        assert locator.count == 1, (
            f"{key}: the production locator (role={locator.role!r}) resolved to "
            f"{locator.count} elements on the live page, not 1. ZERO is a "
            "PHANTOM (the engine would believe it filled a control that does "
            "not exist); more than one may drive the wrong control.")
        assert locator.matched == [key], (
            f"{key}: the locator resolved to {locator.matched}, whose own DOM "
            "anchor is not this field's schema key -- it targets another "
            "control.")

    driven, handed_off, uploaded = [], [], []
    for fv in _live_field_values(fieldmap):
        # THE REAL ROUTING: fill()'s OWN dispatch order, exactly as it decides.
        if workable_fill._is_upload(fv):
            uploaded.append(fv.key)
            continue
        if fv.type in (workable_fill._BOOLEAN_TYPE, workable_fill._DATE_TYPE):
            # `_live_field_values` feeds every boolean a genuine True, so the
            # non-bool hand-off guard (pinned separately, see the dedicated
            # boolean-drive tests above) stays inert here and the driven option
            # is the "Yes" wrapper.
            driven.append(fv.key)
            if fv.type == workable_fill._BOOLEAN_TYPE:
                option = workable_fill._radiogroup_option(
                    page, fv.key, workable_fill._YES_OPTION_TEXT)
                assert workable_fill._option_count(option) == 1, (
                    f"{fv.key}: the boolean option locator resolved to "
                    f"{workable_fill._option_count(option)} wrappers on the live "
                    "page, not 1 -- ZERO is the R3 phantom (the drive would time "
                    "out); more than one may drive the wrong option.")
            else:
                _assert_resolves_uniquely(
                    fv.key, workable_fill._date_control_spec(page, fv).locator)
            continue
        if workable_fill._needs_human_handoff(fv):
            handed_off.append(fv.key)
            continue
        # THE REAL CONSTRUCTION: base._locate, not a re-implementation of it.
        locator = base._locate(page, fv)
        driven.append(fv.key)
        _assert_resolves_uniquely(fv.key, locator)

    # Every field is classified by the shipped code; none escapes into a third path.
    assert len(driven) + len(handed_off) + len(uploaded) == len(fieldmap.fields)
    assert uploaded == ["resume"]
    assert handed_off == []    # this form carries no dropdown/multiple/group field
    assert len(driven) == 17
    assert {"QA_11919111", "QA_11919112"} <= set(driven)
    assert len(page.requested) == 15   # the 15 field-level (base._locate)
                                       # requests; the 2 booleans resolve through
                                       # the fieldset scope, never page.get_by_role

    # ... and the routing the fixture records for the 15 NON-boolean fields still
    # matches what the code performs.
    non_boolean_driven = set(driven) - {"QA_11919111", "QA_11919112"}
    assert {k for k, r in recorded.items() if r["routing"] == "driven"} == non_boolean_driven
    assert {k for k, r in recorded.items() if r["routing"] == "upload"} == set(uploaded)

    # The fixture's OWN "handed-off" label for the two booleans PREDATES W5.1c
    # (the probe recorded the decision the shipped code took AT THE TIME, before
    # the shared kernel control mechanism existed): it is SUPERSEDED by the
    # boolean-drive branch's own routing asserted above, not contradicted by it.
    # What it still proves, undiminished: the FIELD-LEVEL locator (fv.locator.
    # role='radio', name=label) fill() deliberately never builds for a boolean
    # anymore is ambiguous on the live page -- 2 matches, both option wrappers --
    # which is exactly why the scoped construction exists.
    for key in ("QA_11919111", "QA_11919112"):
        assert recorded[key]["routing"] == "handed-off"
        assert recorded[key]["count"] == 2
        assert recorded[key]["matched"] == ["option", "option"]

    # The resume upload: as a ROLE locator it is ALSO ambiguous (2 matches), and
    # safe only because `_is_upload` routes it to `_locate_file_input`, which
    # reads the single live input[type=file].
    assert recorded["resume"]["count"] == 2
    assert _LIVE_LOCATORS["_live_file_inputs"] == ["resume"]


def test_locator_resolution_a_drifted_label_resolves_to_zero_and_never_fills():
    # THE PHANTOM, live-verified, and the reason the invariant above insists on
    # EXACTLY one rather than at least one.
    #
    # A locator is a PAIR (role, name). Here the ROLE is right and the NAME is one
    # word stale: the label QA_11919118 carried in the 2026-07-06 capture ("Even you
    # haven't used any...") is not the label the live page carries ("Even IF you
    # haven't used any..."). The employer edited the question between capture and
    # fill. Resolved against the live page on 2026-07-14, that locator matched ZERO
    # elements.
    #
    # Two things are pinned. (a) The phantom is real: a name that is not the live
    # accessible name resolves to nothing, while the live name resolves 1:1 to the
    # right control. (b) It fails LOUD: a field whose locator resolves to nothing
    # raises on the first keystroke, and fill() books it as a required gap, never as
    # filled -- so capture-to-fill label drift costs a required field, but never
    # sends wrong data.
    drift = _LIVE_LOCATORS["_label_drift"]
    stale, live = (drift["committed_label_2026_07_06"],
                   drift["live_label_2026_07_14"])
    assert stale != live

    # (a) THE PHANTOM.
    probe = drift["live_resolution_of_the_committed_label"]
    assert probe["count"] == 0 and probe["matched"] == []
    good = {r["key"]: r for r in _LIVE_LOCATORS["fields"]}[drift["key"]]
    assert good["locator_name"] == live
    assert good["count"] == 1 and good["matched"] == [drift["key"]]

    # (b) THE LOUD FAILURE, through the real fill(): the control the locator names
    # is not on the page, so nothing is typed and the required field books a gap.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    fld = {f.key: f for f in fieldmap.fields}[drift["key"]]
    assert fld.required
    page = _FakeWorkablePage(fieldmap)
    del page.controls[(fld.locator.role, fld.locator.name)]   # the phantom

    report = workable.fill(page, fieldmap, ResolvedValues(fields=[FieldValue(
        key=fld.key, label=fld.label, type=fld.type, locator=fld.locator,
        value="an answer")]))

    assert report.filled == 0
    gaps = {g["key"]: g for g in report.required_unfilled}
    assert fld.key in gaps
    assert "fill-error" in gaps[fld.key]["reason"]
    assert report.complete is False


def test_date_field_is_driven_by_the_strict_readback_with_no_format_protection():
    # THE LATENT PATH, pinned WITHOUT changing the fill code. The live date box
    # (QA_11919121) has NEVER run: the resolve layer skipped it for want of a canned
    # answer, so an owner-content gap was hiding an unexercised code path. It
    # executes for the first time the moment a computed start date arrives.
    #
    # (a) a date is DRIVEN, never handed off (its role is textbox), and typed as
    #     human-cadence keystrokes, verbatim;
    # (b) its ONLY gate is `base._readback`, a string compare. The intl-tel
    #     tolerance is gated on `fv.type == "phone"` and does not reach it, and the
    #     live control carries no pattern and no maxlength;
    # (c) therefore the engine has NO FORMAT PROTECTION on a date: any value that
    #     round-trips is confirmed, whatever it says. The format is a property of
    #     the BROWSER LOCALE (live 2026-07-14, twice, navigator.language=en-GB:
    #     placeholder DD/MM/YYYY; an en-US browser gets MM/DD/YYYY), so whoever
    #     authors the answer must DERIVE the format from the live placeholder at
    #     generation time and never assume it. This test puts that absence of a
    #     guard on the record, so it is a decision rather than a surprise.
    raw = [{"name": "Custom questions", "fields": [
        {"id": "QA_D1", "required": True, "label": "When can you start?",
         "type": "date"}]}]
    fieldmap = parse_workable(raw, "fauxco", "DATE01", now=lambda: _PINNED)
    fld = {f.key: f for f in fieldmap.fields}["QA_D1"]
    assert fld.locator.role == "textbox"
    fv = FieldValue(key=fld.key, label=fld.label, type=fld.type,
                    locator=fld.locator, value="01/09/2026")
    assert workable_fill._needs_human_handoff(fv) is False      # (a) DRIVEN
    page = _FakeWorkablePage(fieldmap)

    report = workable.fill(page, fieldmap, ResolvedValues(fields=[fv]))

    assert page.controls[("textbox", "When can you start?")].value == "01/09/2026"
    assert report.filled == 1
    assert report.readback_mismatches == []
    assert report.complete is True

    # (b) the strict readback is the whole gate: a control that REWRITES the value
    # the way the phone widget does is a MISMATCH here, because the tolerance is
    # gated on the phone type. A date gets no second look.
    page2 = _FakeWorkablePage(fieldmap)
    page2.controls[("textbox", "When can you start?")] = \
        _FakeDigitRewritingLocator(absorb_leading=2)

    report2 = workable.fill(page2, fieldmap, ResolvedValues(fields=[fv]))

    assert report2.filled == 0
    assert any(m["key"] == "QA_D1" for m in report2.readback_mismatches)
    assert any(g["key"] == "QA_D1" for g in report2.required_unfilled)
    assert report2.complete is False
