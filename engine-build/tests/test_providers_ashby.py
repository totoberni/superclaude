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
through the REAL (offline) `engine.providers.ashby.capture._parse_ashby` -- Ashby
HAS a real schema, so
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

import contextlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

import importlib

# `engine.providers.ashby.capture` is a submodule shadowed at package scope by the
# `capture` Provider callable, so reach the module object via importlib (the same
# sys.modules / import_module seam the package NAME NOTE documents).
ashby_capture = importlib.import_module("engine.providers.ashby.capture")
# `.fill` is shadowed at package scope by the Provider callable, same as `.capture`;
# the reconciliation tests reach `_reconcile` on the MODULE through the same seam.
ashby_fill = importlib.import_module("engine.providers.ashby.fill")
from engine.kernel.contracts import (
    Field, FieldMap, FillAssets, FillSafetyError, Locator)
# The FROZEN kernel's click-hazard set: the roles the fill hands to a human rather
# than driving. The role invariant's whole safety argument runs through it, so the
# tests read it from its one home rather than restating it.
from engine.kernel.fill_toolkit import _CLICK_HAZARD_ROLES
# The FROZEN kernel's upload primitives. The locator-resolution invariant must
# build the file-input locator the way PRODUCTION builds it, so it calls the real
# ones rather than restating their rule.
from engine.kernel import fill_toolkit as kernel_fill_toolkit
from engine.kernel.resolve import MANUAL_ONLY
from engine.kernel.capture_toolkit import CaptureShapeError
from engine.profile_map import profile_from_real_ssot
from engine.providers import _registry, ashby, base, protocol
from engine.providers.ashby.capture import ASHBY_SOURCE, capture_ashby
from engine.ssot import SSOT

_FIXTURES = Path(__file__).parent / "fixtures" / "providers" / "ashby"
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

# -- W5B-ASHBY: the geo-autocomplete Location field ---------------------------
# The live schema types it `Location` (a type `_ASHBY_TYPE_MAP` does not know)
# and the live control is a React combobox with NO accessible name, reachable
# only through its entry's `data-field-path`. FAKE places throughout.
_GEO_KEY = "custom_location"
_GEO_LABEL = "Location"
_GEO_VALUE = "Milan, Fakeland"
# The live widget fuzzy-matches its own dataset: on the 2026-07-13 probe the
# filter "Milan" offered exactly one suggestion, "Christmas Island" (a
# subsequence hit). This decoy is that trap, verbatim -- a driver that commits
# the first row it is shown writes a place the candidate never named.
_GEO_DECOY = "Christmas Island"
# What the widget legitimately offers for this candidate: its dataset is coarser
# than the SSOT's value, so the honest landing is the value's own last segment,
# reached through the sealed ladder's token-subset step.
_GEO_MATCH = "Fakeland"
# The select types Ashby's schema knows. Kept test-local (the per-vendor
# duplication note above) so the fake harness never depends on the module under
# test to decide which control to render.
_SELECT_TYPES = frozenset({
    "multi_value_single_select", "multi_value_multi_select", "yes_no"})


def _is_combobox_role(role: str) -> bool:
    """Ashby has exactly ONE combobox widget and BOTH of its comboboxes are it:
    the geo Location control and a dropdown-rendered ValueSelect (live-probed
    2026-07-13; 30 postings, 31 comboboxes, one widget). Both carry NO accessible
    name, so BOTH are served here only through their entry's `data-field-path`,
    never through `get_by_role` -- which is part of what makes this fake able to
    MISS.

    Keyed on the ROLE alone, exactly as `fill._is_ashby_combobox` is, and NOT on
    the type: the round-6 predicate excluded select TYPES, which is precisely how
    the live dropdown ValueSelect (a select type wearing the combobox control)
    stayed invisible to this harness."""
    return role == "combobox"


# -- fixture loaders -------------------------------------------------------


def _fieldmap() -> FieldMap:
    """The full fixture field map, parsed through the REAL offline graphql
    parser (`engine.providers.ashby.capture._parse_ashby`) -- Ashby's
    authoritative schema oracle."""
    raw = json.loads((_FIXTURES / "form.json").read_text())
    posting = raw["data"]["jobPosting"]
    return ashby_capture._parse_ashby(posting, "fauxcorp", "9001", now=lambda: _PINNED)


def _fieldmap_without(*keys: str) -> FieldMap:
    """The fixture field map minus the named field(s) -- models an Ashby form
    whose required set is all safely fillable (no required human-handoff)."""
    fm = _fieldmap()
    fm.fields = [f for f in fm.fields if f.key not in keys]
    return fm


def _fieldmap_with_geo() -> FieldMap:
    """The safely-fillable fixture map plus the REQUIRED geo-autocomplete
    Location field, shaped exactly as `capture._ashby_field_type` produces it
    for the live `Location` schema type (unknown type -> passed through
    lowercased; location-shaped label -> combobox role)."""
    fm = _fieldmap_without(_CONSENT_KEY)
    fm.fields.append(Field(
        key=_GEO_KEY, label=_GEO_LABEL, type="location", required=True,
        options=[], source=ASHBY_SOURCE,
        locator=Locator(role="combobox", name=_GEO_LABEL), step_index=0))
    return fm


def _fake_ssot(location: str = None, canned: dict = None) -> SSOT:
    # FAKE, invented placeholder data only -- no owner PII, matching the
    # existing real_ssot_v14.yaml fixture's convention. `current_location` is
    # what the kernel's label matcher answers a "Location" field from; it is
    # inert for the fixtures that carry no location field. `location` overrides
    # it for the single-token case (the superset-refusal test).
    #
    # `canned` supplies owner content for a specific question, keyed by the
    # resolver's normalized-label key (`canned_answers.<normalized title>`). It
    # exists because a field the SSOT cannot answer is skipped BEFORE the fill loop
    # ("missing:canned_answers.x"), so it never reaches the driver at all -- and a
    # test of the DRIVER that quietly never ran it would prove nothing.
    return SSOT({
        "identity": {
            "name": "Test Candidate",
            "email": "test.candidate@example.invalid",
            "phone": "+39 000 0000000",
            "current_location": _GEO_VALUE if location is None else location,
        },
        "canned_answers": dict({
            "visa_sponsorship_required": "no",
            "privacy_consent_default": "yes",
        }, **(canned or {})),
    })


# A REAL Ashby question the fake SSOT can answer, for the tests that must reach the
# FILL LOOP. The kernel resolver answers a field only when its label matches an
# `_ANSWER_MATCHERS` keyword row (an unmatched label is an OWNER-CONTENT gap,
# skipped as "missing:..." BEFORE the fill loop, so it never reaches the driver at
# all and a driver test on it would silently prove nothing). The live questions
# these fixtures are captured from ("How did you hear about ElevenLabs?", the
# dubbing-content multi-select, the ad-spend Number) match no row, by design: they
# are content-channel questions, not ours. So a driver test puts an ANSWERABLE
# question on the same REAL control, which is exactly the substitution the ROLE
# under test is indifferent to -- the DOM decides the role, never the label.
_SALARY_LABEL = "What is your expected salary?"


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


class _FakeCheckboxLocator:
    """A single, unambiguous boolean checkbox (Ashby's `Boolean` type): driven
    through the kernel's `ControlKind.CHECKBOX` (`drive_control`), which ticks
    via `.check()`/`.uncheck()` and confirms via `.is_checked()`, never
    `.click()` (owner ruling 2026-07-13,
    automations-automate-everything-tos-boundary: a lone checkbox is no longer
    a Turnstile hand-off)."""

    def __init__(self):
        self._checked = False

    def check(self, *args, **kwargs):
        self._checked = True

    def uncheck(self, *args, **kwargs):
        self._checked = False

    def is_checked(self):
        return self._checked

    def click(self, *args, **kwargs):
        raise AssertionError(
            "a checkbox must be driven via .check(), never .click()")


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


def _fuzzy_offers(query: str, option: str) -> bool:
    """The live Ashby autocomplete offers an option when the query is a
    SUBSEQUENCE of it (the 2026-07-13 probe: "Milan" offered "Christmas
    Island"). Modelled faithfully, because the fact that the widget offers
    JUNK is precisely why the driver may never commit what it is shown."""
    needle = [c for c in query.lower() if c.isalnum()]
    hay = [c for c in option.lower() if c.isalnum()]
    found = 0
    for char in hay:
        if found < len(needle) and char == needle[found]:
            found += 1
    return found == len(needle)


class _FakeGeoOption:
    """One rendered suggestion row (`div[role=option]`)."""

    def __init__(self, widget, text):
        self._widget = widget
        self._text = text

    def inner_text(self):
        return self._text

    def get_attribute(self, name):
        return "option" if name == "role" else None

    def click(self):
        self._widget.commit(self._text)


class _FakeGeoAutocomplete:
    """Models Ashby's React geo-autocomplete (live-probed 2026-07-13).

    Typing opens a floating listbox of fuzzily-matched suggestions; the field
    COMMITS only when a suggestion is chosen -- the typed filter alone is just
    text in the box, and the widget still reports `aria-expanded="true"`, which
    is what stops a typed-but-uncommitted value from ever reading back as
    filled. It carries NO accessible name (like the real control), so it is
    reachable ONLY through its entry's `data-field-path`: a driver that tries
    `get_by_role(name="Location")` finds nothing, exactly as it did live."""

    def __init__(self, listbox_id, suggestions=()):
        self.listbox_id = listbox_id
        self.dataset = list(suggestions)
        self.value = ""
        self.expanded = False
        self.offered = []            # every suggestion set the widget rendered
        self.clicked_option = None
        self._selected_all = False

    def click(self):                 # type_human's focus settle
        self.expanded = bool(self.value)

    def press_sequentially(self, char, delay=None):
        self.value += char
        self.expanded = True

    def press(self, key):
        if key == "Control+a":
            self._selected_all = True
            return
        if key == "Backspace":
            self.value = "" if self._selected_all else self.value[:-1]
            self._selected_all = False
            self.expanded = bool(self.value)

    def input_value(self):
        return self.value

    def get_attribute(self, name):
        if name == "aria-controls":
            return self.listbox_id if self.expanded else None
        if name == "aria-expanded":
            return "true" if self.expanded else "false"
        return None

    def options(self):
        if not (self.expanded and self.value):
            return []
        rows = [text for text in self.dataset
                if _fuzzy_offers(self.value, text)]
        self.offered.append(list(rows))
        return [_FakeGeoOption(self, text) for text in rows]

    def commit(self, text):
        self.clicked_option = text
        self.value = text
        self.expanded = False

    def fill(self, *args, **kwargs):
        raise AssertionError(
            "the geo autocomplete must be typed into (type_human), never fill()")

    def evaluate(self, *args, **kwargs):
        raise AssertionError(
            "the geo autocomplete is not a controlled-component select: it must "
            "not go through the berellevy evaluate driver")

    def select_option(self, *args, **kwargs):
        raise AssertionError("the geo autocomplete has no native <select>")


class _FakeGeoAutocompleteDeadClick(_FakeGeoAutocomplete):
    """A widget whose suggestion click NEVER registers (the row was re-rendered
    under the cursor, the handler was not yet wired): the typed filter stays in
    the box and the listbox stays open, so React holds NO location. When the
    filter text happens to equal the suggestion, the text readback ALONE reads
    as "committed" -- which is exactly why the driver also requires the listbox
    to have collapsed before it counts the field filled."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.click_attempts = 0

    def commit(self, text):
        self.click_attempts += 1      # the click landed on the row, and did nothing


class _FakeGeoAutocompleteCollapseNoCommit(_FakeGeoAutocomplete):
    """A widget that COLLAPSES its listbox on the option click without ever writing
    the suggestion back into the input: a chip/token UI that holds the selection
    somewhere else, or a collapse-on-blur. The click registers, the listbox closes
    -- and the box still holds the typed FILTER.

    It is the OTHER half of `_FakeGeoAutocompleteDeadClick`, and the fake this
    suite was missing (round-9 MAJOR 3). DeadClick leaves `expanded` True, so it can
    only ever exercise the COLLAPSE signal of the two-signal commit check; nothing
    here could express "collapsed but did not read back", so the READBACK signal
    could be deleted with the suite still green. Under that mutant the driver counts
    the field FILLED and reports the typed FILTER as the answer -- on the live seal
    target, the owner's full street address, sent as the answer to "Country you're
    currently residing in"."""

    def commit(self, text):
        self.clicked_option = text    # the click LANDED on the row...
        self.expanded = False         # ...and the listbox DID collapse...
        # ...but React never wrote the suggestion into the control: `self.value` is
        # still the filter the driver typed. ONE of the two signals, never both.


class _FakeEntry:
    """One Ashby field-entry div: `data-field-path` (the schema path), a
    `<label>` whose CSS-module class carries Ashby's required marker
    (`_required_<hash>_<n>`; never an attribute, never a visible asterisk), and
    the entry's controls, which the kernel sweep can only name by their
    PLACEHOLDER (Ashby ships no aria-label)."""

    _REQUIRED_CLASS = "_heading_f7cvd_52 _required_f7cvd_91 _label_1e3gg_42"
    _PLAIN_CLASS = "_heading_f7cvd_52 _label_1e3gg_42"

    def __init__(self, path, label, *, required=False, native_required=False,
                 placeholders=()):
        self.path = path
        self._label = _FakeSweepLocator(
            attrs={"class": (self._REQUIRED_CLASS if required
                             else self._PLAIN_CLASS)},
            text=label)
        # ONE node per control, carrying BOTH its name source (the placeholder,
        # which is all the kernel sweep can name it by) and its `required` attr
        # when it has one -- so a required control is the SAME node the sweep
        # sees, which is what makes node-counted ownership meaningful.
        self._controls = [
            _FakeSweepLocator(attrs=dict(
                {"placeholder": text},
                **({"required": ""} if native_required else {})))
            for text in placeholders]

    def get_attribute(self, name):
        return self.path if name == "data-field-path" else None

    def locator(self, css):
        from engine.providers import base
        if css == base._REQUIRED_CSS:
            return _FakeLocatorSet(
                [control for control in self._controls
                 if control.get_attribute("required") is not None])
        if "label" in css:
            return _FakeLocatorSet([self._label])
        if "input" in css:
            return _FakeLocatorSet(list(self._controls))
        return _FakeLocatorSet([])


# -- the REAL captured DOM (tests/fixtures/providers/ashby/dom.html) -----------
# POSTING REGISTRY (the one place this file states which postings its evidence
# came from, and which of them are still live):
#
#   elevenlabs/b43e388f-...  CLOSED, verified gone 2026-07-14. It was the
#                            acceptance/seal posting and it is the source of
#                            dom.html. Every citation of it in this file is a
#                            HISTORICAL attribution of where a claim was
#                            observed, and stays; none of them is a live pointer.
#                            The captured DOM itself does not expire with the
#                            posting: it is a verbatim Ashby render, and it is
#                            kept precisely because nothing edits it.
#   elevenlabs/1713bfd7-..., elevenlabs/09d54604-...  the sibling captures below
#                            (probed 2026-07-13). Liveness NOT re-verified this
#                            round; cited as history only.
#   CURRENT LIVE TARGET      axelera/17131e36-e361-45a7-8538-1033a63addd1
#                            ("Senior Platform Architect"), fallback
#                            axelera/0505a9a5-d908-4cb2-bdcb-c92b1fe8309a.
#                            Live probe 2026-07-14, headless chromium under xvfb,
#                            BROWSER LOCALE en-GB (TZ Europe/Rome): HTTP 200, 14
#                            inputs, TWO page-wide file inputs -- so the
#                            autofill-decoy hazard the fixture carries is live on
#                            it too -- and exactly one input[role=combobox] (the
#                            "Current location:" entry), which the driver COMMITS
#                            against live (see fill.py § POSTING REGISTRY for the
#                            offered rows and the readback).
#
# Read-only capture of the live elevenlabs apply form (page load, no typing, no
# click, no submit; script/style/svg stripped), so the reconciliation can be
# driven through the REAL `base.sweep_required` over the REAL key space instead
# of a fake one. This matters because the fake harness above names its swept
# controls by `aria-label`, which makes dom-name == schema-label; the real page
# ships NO aria-label anywhere, so the swept name is the PLACEHOLDER and FOUR
# required controls collide on "type here...". The nodes below expose exactly the
# sliver of the Playwright locator API the sweep and `_reconcile` call.


class _HtmlNode:
    def __init__(self, tag, attrs):
        self.tag = tag
        self.attrs = attrs
        self.parts = []          # child nodes and text, in document order

    def get_attribute(self, name):
        return self.attrs.get(name)

    def is_visible(self):
        # The fixture IS the rendered form (captured from a live page), so every
        # node in it was on screen.
        return True

    def inner_text(self):
        return "".join(
            part if isinstance(part, str) else part.inner_text()
            for part in self.parts).strip()

    def locator(self, css):
        return _FakeLocatorSet(_css_select(self, css))


class _HtmlFixtureParser(HTMLParser):
    _VOID = frozenset({"input", "br", "img", "hr", "meta", "link", "source",
                       "col", "area", "embed", "param", "track", "wbr"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("#document", {})
        self._stack = [self.root]

    def _make(self, tag, attrs):
        node = _HtmlNode(tag, {k: (v if v is not None else "") for k, v in attrs})
        self._stack[-1].parts.append(node)
        return node

    def handle_starttag(self, tag, attrs):
        node = self._make(tag, attrs)
        if tag not in self._VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._make(tag, attrs)

    def handle_endtag(self, tag):
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data):
        self._stack[-1].parts.append(data)


def _descendants(node):
    for part in node.parts:
        if isinstance(part, str):
            continue
        yield part
        yield from _descendants(part)


def _css_matches(node, term: str) -> bool:
    # `tag`, `[attr]`, `[attr=val]`, or a compound `tag[attr=val]` (the radio
    # probe's `input[type="radio"]`); comma groups are split by the caller.
    tag, bracket, rest = term.partition("[")
    if not bracket:
        return node.tag == term
    if tag and node.tag != tag:
        return False
    name, eq, raw = rest.rstrip("]").partition("=")
    if not eq:
        return name in node.attrs
    return node.attrs.get(name) == raw.strip("'\"")


def _css_select(node, css: str):
    """The selector shapes the production code aims at a page: comma groups of
    tag names, attribute selectors, compounds (`input[type="radio"]`), and
    DESCENDANT chains (`[data-field-path="..."] input[role="combobox"]` -- the geo
    driver's entry-scoped anchor). Matches come back in DOCUMENT order,
    de-duplicated by identity, exactly as a Playwright locator resolves them.

    The descendant chain is not a convenience (round 9). It is the only way this
    fixture can answer the question the entry-scoping guard exists for -- HOW MANY
    controls does the driver's selector resolve to on this DOM -- and while it was
    unsupported, an anchor that had LOST its scope matched NOTHING here instead of
    matching every combobox on the page, so dropping the scope could not fail a
    test that used this DOM."""
    hits: list = []
    for term in css.split(","):
        term = term.strip()
        if not term:
            continue
        scopes = [node]
        for step in term.split():           # each whitespace-separated step
            scopes = [descendant for scope in scopes
                      for descendant in _descendants(scope)
                      if _css_matches(descendant, step)]
        hits.extend(scopes)
    if not hits:
        return []
    return [candidate for candidate in _descendants(node)
            if any(candidate is hit for hit in hits)]


class _HtmlLocatorSet(_FakeLocatorSet):
    """`_FakeLocatorSet` plus `.nth()`, the one extra verb the kernel's file-input
    primitive needs (`fill_toolkit._file_input_control` drives
    `page.locator("input[type=file]").nth(i)`), so the REAL `_locate_file_input`
    runs end to end against the captured DOM -- decoy included."""

    def nth(self, index):
        return self._items[index]


class _HtmlPage:
    """A page backed by the captured DOM fixture."""

    def __init__(self, html: str):
        parser = _HtmlFixtureParser()
        parser.feed(html)
        self._root = parser.root

    def locator(self, css):
        return _HtmlLocatorSet(_css_select(self._root, css))

    def query_selector_all(self, css):
        """What the kernel's `_file_inputs` enumerates a page's file inputs with.
        The captured DOM answers for itself, so the fixture's own file inputs (the
        resume field AND the autofill pane's decoy) are the candidate list the
        production upload primitive actually sees."""
        return _css_select(self._root, css)


def _real_dom() -> str:
    return (_FIXTURES / "dom.html").read_text()


# -- REAL captured DOM from SIBLING live postings (round 7) --------------------
# dom.html is the form of the posting that WAS the seal target (elevenlabs/
# b43e388f, CLOSED 2026-07-14; the live target is now axelera/17131e36 -- see the
# POSTING REGISTRY above), VERBATIM, and it stays that way: the reconciliation
# test pins its exact kernel sweep, and its fixture-equals-a-real-Ashby-render
# property is this wave's most valuable asset precisely because nothing edits it.
#
# But that posting serves only four of Ashby's control shapes, so it cannot
# express the three PHANTOM roles round 6 shipped -- which is one reason they
# survived six green rounds. These are the missing shapes, captured VERBATIM
# (element.outerHTML, read-only page load, no typing, no submit, no PII) from two
# OTHER live elevenlabs postings on 2026-07-13:
#
#   elevenlabs/1713bfd7-a63b-4dd4-b6e7-b0b8b8560a4a  "Dubbing Specialist"
#     fe82a364-...  ValueSelect  9 opts  REQUIRED -> input[role=combobox]
#                   The ONE dropdown-rendered ValueSelect on the whole board (the
#                   other 33 are radio groups). It has NO id, NO name and NO
#                   aria-label, and its <label for=...> points at the ENTRY div,
#                   not at the input -- so it has NO ACCESSIBLE NAME, and
#                   `base._locate` cannot reach it. Same widget as the geo control.
#     2726a0b3-...  MultiValueSelect  5 opts  REQUIRED -> input[type=checkbox] x5
#                   A native checkbox GROUP. Zero listbox nodes, here or anywhere.
#
#   elevenlabs/09d54604-b5eb-498e-9868-e52b25967a66  "Mobile Growth Manager"
#     c5f68ec9-...  Number    REQUIRED -> input[type=number]  (an ARIA spinbutton,
#                   and the one Ashby control that DOES carry an accessible name
#                   while wearing a phantom role)
#     0d9175ab-...  Boolean   REQUIRED -> input[type=checkbox] under a Yes/No
#                   button pair
#
# Nothing here is invented. If a control shape is absent from these fixtures, then
# Ashby does not serve it and this wave makes no claim about it.
_SIBLING_DOM = (
    # the dropdown ValueSelect
    '<div data-field-path="fe82a364-074d-47b8-b945-d411050c2913" data-field-ent'
    'ry-id="7b7f2695-1a0c-4eac-8db3-df4cb2d6bf5c_fe82a364-074d-47b8-b945-d41105'
    '0c2913"><fieldset class="_container_wz442_28 _fieldEntry_1e3gg_28"><label '
    'class="_heading_f7cvd_52 _required_f7cvd_91 _label_1e3gg_42 '
    'ashby-application-form-question-title" '
    'for="fe82a364-074d-47b8-b945-d411050c2913">How did you hear about '
    'ElevenLabs?&nbsp;</label><div class="_inputContainer_d7ago_28"><input '
    'class="_input_d7ago_28" placeholder="Start typing..." '
    'aria-autocomplete="list" aria-expanded="false" aria-haspopup="listbox" '
    'role="combobox" value=""><button class="_container_pjyt6_1 '
    '_toggleButton_d7ago_32"><svg viewBox="0 0 640 640" fill="none" '
    'height="1em"><path d="M303.5 473C312.9 482.4 328.1 482.4 337.4 473L537.4 '
    '273C546.8 263.6 546.8 248.4 537.4 239.1C528 229.8 512.8 229.7 503.5 '
    '239.1L320.5 422.1L137.5 239.1C128.1 229.7 112.9 229.7 103.6 239.1C94.3 '
    '248.5 94.2 263.7 103.6 273L303.6 '
    '473z"></path></svg></button></div></fieldset></div>'
    # the MultiValueSelect checkbox group
    '<div data-field-path="2726a0b3-d592-4917-afb9-eae9ee12bd7d" data-field-ent'
    'ry-id="7b7f2695-1a0c-4eac-8db3-df4cb2d6bf5c_2726a0b3-d592-4917-afb9-eae9ee'
    '12bd7d"><fieldset class="_container_1258i_28 _fieldEntry_1e3gg_28"><label '
    'class="_heading_f7cvd_52 _required_f7cvd_91 _label_1e3gg_42 '
    'ashby-application-form-question-title" '
    'for="2726a0b3-d592-4917-afb9-eae9ee12bd7d">Which of the following types of'
    ' content have you produced dubs or audiovisual translations for in a '
    'professional context?</label><div class="_option_1258i_34"><span class=" '
    '_container_1danv_28" data-disabled="false"><svg height="1em" viewBox="0 0 '
    '512 512" fill="none"><path d="M173.898 439.404L7.49824 273.004C-2.49876 '
    '263.007 -2.49876 246.798 7.49824 236.8L43.7012 200.596C53.6982 190.598 '
    '69.9082 190.598 79.9052 200.596L192 312.69L432.095 72.596C442.092 62.599 '
    '458.302 62.599 468.299 72.596L504.502 108.8C514.499 118.797 514.499 '
    '135.006 504.502 145.004L210.102 439.405C200.104 449.402 183.895 449.402 '
    '173.898 439.404V439.404Z"></path></svg><input type="checkbox" id="7b7f2695'
    '-1a0c-4eac-8db3-df4cb2d6bf5c_2726a0b3-d592-4917-afb9-eae9ee12bd7d-labeled-'
    'checkbox-0" name="Movies/TV shows/other dramatic content"></span><label fo'
    'r="7b7f2695-1a0c-4eac-8db3-df4cb2d6bf5c_2726a0b3-d592-4917-afb9-eae9ee12bd'
    '7d-labeled-checkbox-0" class="_label_1258i_42 ">Movies/TV shows/other '
    'dramatic content</label></div><div class="_option_1258i_34"><span class=" '
    '_container_1danv_28" data-disabled="false"><svg height="1em" viewBox="0 0 '
    '512 512" fill="none"><path d="M173.898 439.404L7.49824 273.004C-2.49876 '
    '263.007 -2.49876 246.798 7.49824 236.8L43.7012 200.596C53.6982 190.598 '
    '69.9082 190.598 79.9052 200.596L192 312.69L432.095 72.596C442.092 62.599 '
    '458.302 62.599 468.299 72.596L504.502 108.8C514.499 118.797 514.499 '
    '135.006 504.502 145.004L210.102 439.405C200.104 449.402 183.895 449.402 '
    '173.898 439.404V439.404Z"></path></svg><input type="checkbox" id="7b7f2695'
    '-1a0c-4eac-8db3-df4cb2d6bf5c_2726a0b3-d592-4917-afb9-eae9ee12bd7d-labeled-'
    'checkbox-1" name="Creator content (e.g. YouTube videos)"></span><label for'
    '="7b7f2695-1a0c-4eac-8db3-df4cb2d6bf5c_2726a0b3-d592-4917-afb9-eae9ee12bd7'
    'd-labeled-checkbox-1" class="_label_1258i_42 ">Creator content (e.g. '
    'YouTube videos)</label></div><div class="_option_1258i_34"><span class=" '
    '_container_1danv_28" data-disabled="false"><svg height="1em" viewBox="0 0 '
    '512 512" fill="none"><path d="M173.898 439.404L7.49824 273.004C-2.49876 '
    '263.007 -2.49876 246.798 7.49824 236.8L43.7012 200.596C53.6982 190.598 '
    '69.9082 190.598 79.9052 200.596L192 312.69L432.095 72.596C442.092 62.599 '
    '458.302 62.599 468.299 72.596L504.502 108.8C514.499 118.797 514.499 '
    '135.006 504.502 145.004L210.102 439.405C200.104 449.402 183.895 449.402 '
    '173.898 439.404V439.404Z"></path></svg><input type="checkbox" id="7b7f2695'
    '-1a0c-4eac-8db3-df4cb2d6bf5c_2726a0b3-d592-4917-afb9-eae9ee12bd7d-labeled-'
    'checkbox-2" name="Ads/socials"></span><label for="7b7f2695-1a0c-4eac-8db3-'
    'df4cb2d6bf5c_2726a0b3-d592-4917-afb9-eae9ee12bd7d-labeled-checkbox-2" '
    'class="_label_1258i_42 ">Ads/socials</label></div><div '
    'class="_option_1258i_34"><span class=" _container_1danv_28" '
    'data-disabled="false"><svg height="1em" viewBox="0 0 512 512" '
    'fill="none"><path d="M173.898 439.404L7.49824 273.004C-2.49876 263.007 '
    '-2.49876 246.798 7.49824 236.8L43.7012 200.596C53.6982 190.598 69.9082 '
    '190.598 79.9052 200.596L192 312.69L432.095 72.596C442.092 62.599 458.302 '
    '62.599 468.299 72.596L504.502 108.8C514.499 118.797 514.499 135.006 '
    '504.502 145.004L210.102 439.405C200.104 449.402 183.895 449.402 173.898 '
    '439.404V439.404Z"></path></svg><input type="checkbox" id="7b7f2695-1a0c-4e'
    'ac-8db3-df4cb2d6bf5c_2726a0b3-d592-4917-afb9-eae9ee12bd7d-labeled-checkbox'
    '-3" name="E-learning or informational content"></span><label for="7b7f2695'
    '-1a0c-4eac-8db3-df4cb2d6bf5c_2726a0b3-d592-4917-afb9-eae9ee12bd7d-labeled-'
    'checkbox-3" class="_label_1258i_42 ">E-learning or informational '
    'content</label></div><div class="_option_1258i_34"><span class=" '
    '_container_1danv_28" data-disabled="false"><svg height="1em" viewBox="0 0 '
    '512 512" fill="none"><path d="M173.898 439.404L7.49824 273.004C-2.49876 '
    '263.007 -2.49876 246.798 7.49824 236.8L43.7012 200.596C53.6982 190.598 '
    '69.9082 190.598 79.9052 200.596L192 312.69L432.095 72.596C442.092 62.599 '
    '458.302 62.599 468.299 72.596L504.502 108.8C514.499 118.797 514.499 '
    '135.006 504.502 145.004L210.102 439.405C200.104 449.402 183.895 449.402 '
    '173.898 439.404V439.404Z"></path></svg><input type="checkbox" id="7b7f2695'
    '-1a0c-4eac-8db3-df4cb2d6bf5c_2726a0b3-d592-4917-afb9-eae9ee12bd7d-labeled-'
    'checkbox-4" name="Documentaries"></span><label for="7b7f2695-1a0c-4eac-8db'
    '3-df4cb2d6bf5c_2726a0b3-d592-4917-afb9-eae9ee12bd7d-labeled-checkbox-4" '
    'class="_label_1258i_42 ">Documentaries</label></div></fieldset></div>'
    # the Number field
    '<div class="_fieldEntry_1e3gg_28 ashby-application-form-field-entry" '
    'data-field-path="c5f68ec9-abcc-4476-8488-5c7a9f13882c" data-field-entry-id'
    '="2d779eda-a4f1-4283-ae30-da6644fcc047_c5f68ec9-abcc-4476-8488-5c7a9f13882'
    'c"><label class="_heading_f7cvd_52 _required_f7cvd_91 _label_1e3gg_42 '
    'ashby-application-form-question-title" '
    'for="c5f68ec9-abcc-4476-8488-5c7a9f13882c">What is the largest amount '
    'you’ve scaled mobile app advertising spend per month while maintaining '
    'profitability or efficiency targets? Please answer with whole number. (Ex.'
    ' 1,000,000)</label><input required="" placeholder="Type here..." '
    'name="c5f68ec9-abcc-4476-8488-5c7a9f13882c" '
    'id="c5f68ec9-abcc-4476-8488-5c7a9f13882c" type="number" '
    'class="_input_1e3gg_32 _input_80epu_28 " value="undefined"></div>'
    # the Boolean field
    '<div class="_fieldEntry_1e3gg_28 ashby-application-form-field-entry" '
    'data-field-path="0d9175ab-55d7-4150-adf9-0c9e30d54969" data-field-entry-id'
    '="2d779eda-a4f1-4283-ae30-da6644fcc047_0d9175ab-55d7-4150-adf9-0c9e30d5496'
    '9"><label class="_heading_f7cvd_52 _required_f7cvd_91 _label_1e3gg_42 '
    'ashby-application-form-question-title" '
    'for="0d9175ab-55d7-4150-adf9-0c9e30d54969">Have you personally optimized '
    'campaigns based on post-install metrics (e.g., activation, retention, '
    'subscription revenue, LTV), not just installs or CPI?</label><div '
    'class="_container_1svni_28 _yesno_1e3gg_148 "><button '
    'class="_container_pjyt6_1 _option_1svni_32 ">Yes</button><button '
    'class="_container_pjyt6_1 _option_1svni_32 ">No</button><input '
    'type="checkbox" class="_input_1svni_78" tabindex="-1" '
    'name="0d9175ab-55d7-4150-adf9-0c9e30d54969"></div></div>'
)

# The real paths, so a test names the control it actually drives.
_DROPDOWN_PATH = "fe82a364-074d-47b8-b945-d411050c2913"   # ValueSelect, combobox
_CHECKBOX_GROUP_PATH = "2726a0b3-d592-4917-afb9-eae9ee12bd7d"  # MultiValueSelect
_NUMBER_PATH = "c5f68ec9-abcc-4476-8488-5c7a9f13882c"     # Number, spinbutton
_BOOLEAN_PATH = "0d9175ab-55d7-4150-adf9-0c9e30d54969"    # Boolean, checkbox


def _dom_with_siblings() -> str:
    """The seal posting's captured form PLUS the sibling entries that carry the
    control shapes it does not serve. Both halves are live-captured DOM."""
    return _real_dom() + _SIBLING_DOM


# -- an INDEPENDENT ARIA resolver over the captured DOM ------------------------
# The phantom sweep has to ask the DOM what roles it carries, and it must not ask
# the code that produced the roles: a sweep that reuses `capture`'s own table only
# proves the capture agrees with itself. So the role mapping below is written from
# the ARIA HTML mapping (an input[type=number] is a spinbutton, a file input is a
# button, a textarea is a textbox) and the name from Playwright's own ladder
# (aria-label, then the <label for=id>, then the placeholder), independently.
#
# It is CALIBRATED against the live browser, not trusted: the seal posting's live
# page-wide census on 2026-07-13 was textbox=6 radio=8 combobox=1 listbox=0, and
# `test_ashby_no_captured_role_is_a_phantom_on_the_real_dom` asserts this resolver
# reproduces those exact numbers over dom.html before it trusts any of its
# answers.
_ARIA_ROLE_BY_INPUT_TYPE = {
    "text": "textbox", "email": "textbox", "tel": "textbox", "url": "textbox",
    "date": "textbox", "": "textbox",
    "number": "spinbutton", "file": "button",
    "checkbox": "checkbox", "radio": "radio", "hidden": "",
}


def _aria_role(node) -> str:
    if node.attrs.get("role"):
        return node.attrs["role"]
    if node.tag == "textarea":
        return "textbox"
    if node.tag == "select":
        return "listbox" if "multiple" in node.attrs else "combobox"
    if node.tag != "input":
        return ""
    return _ARIA_ROLE_BY_INPUT_TYPE.get(
        (node.attrs.get("type") or "").lower(), "")


def _accessible_name(root, node) -> str:
    if node.attrs.get("aria-label"):
        return node.attrs["aria-label"]
    node_id = node.attrs.get("id") or ""
    if node_id:
        for other in _descendants(root):
            if other.tag == "label" and other.attrs.get("for") == node_id:
                return other.inner_text()
    return node.attrs.get("placeholder") or ""


def _norm_ws(text) -> str:
    return " ".join(str(text or "").split()).lower()


def _role_all(page, role) -> list:
    return [node for node in _descendants(page._root)
            if _aria_role(node) == role]


def _role_matches(page, role, name) -> list:
    """`page.get_by_role(role, name=...)`, resolved over the captured DOM with
    Playwright's default name semantics (case-insensitive, whitespace-normalized
    SUBSTRING of the accessible name)."""
    target = _norm_ws(name)
    return [node for node in _role_all(page, role)
            if target and target in _norm_ws(_accessible_name(page._root, node))]


class _FieldValueStub:
    """A `FieldValue`-shaped stub for the routing predicates. Its default value is
    deliberately NOT a bool: `_needs_human_handoff` hands off ANY bool value
    whatever the role, so a bool would prove nothing about the ROLE."""

    def __init__(self, fld, value="x"):
        self.key = fld.key
        self.label = fld.label
        self.type = fld.type
        self.locator = fld.locator
        self.value = value


def _real_dom_fieldmap() -> FieldMap:
    """The six REQUIRED fields the captured form carries, keyed by the graphql
    paths its `data-field-path` entries expose (the Location field is the geo
    combobox; "How did you hear" is a ValueSelect; the rest are text/file)."""
    def field(key, label, ftype, role):
        return Field(key=key, label=label, type=ftype, required=True, options=[],
                     source=ASHBY_SOURCE, locator=Locator(role=role, name=label),
                     step_index=0)

    return FieldMap(vendor="ashby", posting_id="9003", captured_at=_PINNED, fields=[
        field("_systemfield_name", "Name", "input_text", "textbox"),
        field("_systemfield_email", "Email", "input_text", "textbox"),
        field("0a131ea7-7bc4-4ff5-85d3-c14985345cf9", "Location",
              "location", "combobox"),
        field("_systemfield_resume", "Resume", "input_file", "button"),
        field("6825d01d-72c1-40e3-9617-7def67b066df",
              "How did you hear about ElevenLabs? ",
              "multi_value_single_select", "combobox"),
        field("cbd5a932-c117-4d0d-83f4-f114fadab5b2",
              "Link to your LinkedIn profile", "input_text", "textbox"),
    ])


def _entries_for(fieldmap, *, unmarked_keys=(), extra=()):
    """A live-shaped entry for every field of `fieldmap` (label = the schema
    title, required marker = the schema's requiredness), plus any `extra`
    entries the schema never captured. `unmarked_keys` models Ashby's React
    widgets, which carry NO required marker the DOM sweep can see."""
    entries = []
    for fld in fieldmap.fields:
        marked = fld.required and fld.key not in unmarked_keys
        entries.append(_FakeEntry(
            fld.key, fld.label, required=marked,
            native_required=marked and fld.locator.role == "textbox",
            placeholders=("Type here...",) if fld.locator.role == "textbox"
            else ()))
    return entries + list(extra)


class _LocatorMiss(Exception):
    """What Playwright's strict-mode locator does when a role+name matches NO
    element: the fill's `except Exception` turns it into a `fill-error:` skip."""


class _FakeOptionControl:
    """One native option control inside a GROUP -- a radio OR a checkbox, one
    class because live they are one shape: an `<input>` whose OWN accessible name
    is the option's `<label for=id>` wording, sharing the group's
    `data-field-path` entry. Driven through the shared kernel mechanism
    (`drive_control`) via `.check()`, confirmed via `.is_checked()`, never
    `.click()` -- exactly like the lone `Boolean` checkbox.

    The ONE structural difference between the two live groups is the `name`
    ATTRIBUTE, and it is the reason both must scope by `data-field-path`: a radio
    carries the GROUP's `<entry-id>_<path>` name (which the graphql schema does
    not carry, so it is not knowable in advance), while a checkbox carries the
    OPTION's own wording (live: `name="Documentaries"`, `_SIBLING_DOM`). Neither
    is a group-distinguishing name the driver could scope by, so neither is
    modelled as one here; `path` is what the entry scope resolves on.

    `checks`/`unchecks` COUNT the drives. A checkbox group needs the count, not
    just the final state: `.check()` is idempotent, so a driver that re-drove an
    already-ticked box would leave identical state and only the counter would
    show it. `.click()` RAISES because a click TOGGLES, which is the exact
    mechanism by which a second pass would untick a correct box."""

    def __init__(self, path, name, role="radio", checked=False):
        self.path = path
        self.name = name
        self.role = role
        self._checked = checked
        self.checks = 0
        self.unchecks = 0

    def check(self, *args, **kwargs):
        self.checks += 1
        self._checked = True

    def uncheck(self, *args, **kwargs):
        self.unchecks += 1
        self._checked = False

    def is_checked(self):
        return self._checked

    def click(self, *args, **kwargs):
        raise AssertionError(
            f"a {self.role} option must be driven via .check(), never .click(): "
            "a click TOGGLES and would invert an already-correct control")


class _FakeUnreadableOption(_FakeOptionControl):
    """An option whose `.check()` lands but whose readback NEVER confirms it: the
    silent-no-op `control_toolkit._confirm`'s never-confirmed bias exists to
    catch. In a multi-select it is the ingredient of a PARTIAL confirmation,
    which the convention says is a GAP and never a fill."""

    def is_checked(self):
        return False


class _FakeOptionMatch:
    """A Playwright-Locator-shaped match over option controls of EITHER group:
    `.count()` (the honest number the entry-scoping guard turns on), `.and_()`
    (the FX1 intersection `_locate_option` uses to narrow a page-wide role+name
    match to one group's own option), and the drive/readback verbs, which
    delegate to the SINGLE matched control (a strict-mode violation for 0 or 2+).
    It CAN MISS: an unscoped `.and_()` that lost the group anchor leaves >1 node,
    and `.check()` then raises exactly as live strict mode would."""

    def __init__(self, nodes):
        self.nodes = list(nodes)

    def all(self):
        return list(self.nodes)

    def count(self):
        return len(self.nodes)

    def and_(self, other):
        return _FakeOptionMatch(
            [n for n in self.nodes if any(n is o for o in other.nodes)])

    def _only(self):
        if len(self.nodes) != 1:
            raise AssertionError(
                f"strict mode: locator resolved to {len(self.nodes)} elements, "
                "not 1")
        return self.nodes[0]

    def check(self, *args, **kwargs):
        self._only().check()

    def uncheck(self, *args, **kwargs):
        self._only().uncheck()

    def is_checked(self):
        return self._only().is_checked()


class _FakeAshbyPage:
    """One fake page driving the WHOLE fill() sequence for Ashby: native text
    controls, the nameless React combobox(es), a native controlled-component
    select, the resume file input, and the sweep_required CSS selectors. Records
    every locator request so a test can assert which path was used and that no
    react-select / native-select / EEO / checkbox control was ever touched.

    IT CAN MISS, and that is the point (round-7 structural fix). Round 6's fake
    built its control table from `fld.locator.role`, so WHATEVER role the capture
    emitted, a control for it was manufactured here and the locator ALWAYS
    resolved: a phantom role -- a role no element on the live page carries -- was
    literally inexpressible, which is why three of them survived six rounds of a
    green suite. The table is now keyed on the role the modelled DOM CARRIES
    (`dom_roles`, defaulting to the captured role for the fields whose capture is
    not under test), and `get_by_role` raises `_LocatorMiss` for anything else,
    exactly as the live page did. A test can therefore hand this fake a page whose
    DOM disagrees with the capture and watch the field die."""

    def __init__(self, fieldmap, *,
                 url="https://jobs.ashbyhq.com/fauxcorp/9001/application",
                 sweep_required_labels=_SAFE_REQUIRED_LABELS,
                 file_inputs=None, bad_text_keys=(), bad_select_keys=(),
                 bad_option_names=(), entries=(), geo_suggestions=(),
                 dom_roles=None, geo_class=_FakeGeoAutocomplete,
                 geo_duplicate_keys=()):
        self._url = url
        self.controls = {}
        # An option GROUP -- a radio-rendered single-select OR a checkbox-rendered
        # multi-select -- keyed by data-field-path, each holding N distinct option
        # controls. `_option_nodes` is the page-wide flat list PER ROLE that
        # `get_by_role(<role>, ...)` searches, kept per role so a radio named
        # "Yes" and a checkbox named "Yes" cannot answer for each other. Distinct
        # controls so the fake CAN express the FX1 collision (two groups, same
        # option wording) and the entry-scoping that resolves it.
        self.option_groups = {}
        self._option_nodes = {"radio": [], "checkbox": []}
        self.geo = {}
        # A path that mounts its widget TWICE: the entry-anchored selector then
        # resolves to TWO live controls and the resolver has to choose. The
        # duplicates are real, distinct controls (not aliases), so a first-of-N
        # resolver would type into one of them and leave the other empty.
        self.geo_dupes = {}
        dom_roles = dom_roles or {}
        for fld in fieldmap.fields:
            role = dom_roles.get(fld.key, fld.locator.role)
            key = (role, fld.locator.name)
            if _is_combobox_role(role):
                # Both live comboboxes have NO accessible name, so each is served
                # ONLY through its entry's data-field-path -- never get_by_role.
                # A dropdown select's listbox offers its own enumerated options,
                # which is what the live widget renders; the geo control's offers
                # come from the widget's geo dataset.
                self.geo[fld.key] = geo_class(
                    f"combobox-listbox-{fld.key}",
                    fld.options if fld.type in _SELECT_TYPES else geo_suggestions)
            elif role in ("textbox", "spinbutton"):
                self.controls[key] = (_FakeBadTextLocator()
                                      if fld.key in bad_text_keys
                                      else _FakeTextLocator())
            elif role == "listbox":
                self.controls[key] = (_FakeAshbyReactSelectBad()
                                      if fld.key in bad_select_keys
                                      else _FakeAshbyReactSelect())
            elif role == "checkbox" and isinstance(fld.type, str) and \
                    fld.type == "boolean":
                # The LONE boolean checkbox: ONE control whose own accessible
                # name IS the field title, so `base._locate` reaches it directly.
                # Checked BEFORE the group branch, because both wear role
                # "checkbox" and only the schema type tells them apart.
                self.controls[key] = _FakeCheckboxLocator()
            elif role in ("radio", "checkbox"):
                # An option GROUP, either kind: one option control per graphql
                # option, each carrying the OPTION's own accessible name (never
                # the field title, which is why the field-level role+name locator
                # cannot reach one), all scoped to this field's data-field-path
                # entry. Mirrors both live groups: the 8-radio ValueSelect and
                # the 5-checkbox MultiValueSelect of `_SIBLING_DOM`.
                # `bad_option_names` makes ONE option a control whose `.check()`
                # lands but whose readback never confirms -- the silent-no-op the
                # readback gate exists for, and the ingredient of a PARTIALLY
                # confirmed multi-select. Same convention as `bad_text_keys` /
                # `bad_select_keys` above.
                opts = [(_FakeUnreadableOption(fld.key, opt, role=role)
                         if opt in bad_option_names
                         else _FakeOptionControl(fld.key, opt, role=role))
                        for opt in fld.options]
                self.option_groups[fld.key] = opts
                self._option_nodes[role].extend(opts)
        for key in geo_duplicate_keys:
            twin = self.geo[key]
            self.geo_dupes[key] = geo_class(f"combobox-listbox-{key}-twin",
                                            list(twin.dataset))
        self.entries = list(entries)
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

    def get_by_role(self, role, name=None, exact=None):
        self.requested.append(("role", role, name))
        if (role, name) in self.controls:
            # A FIELD-level role+name control (text, lone boolean checkbox, ...).
            # Asked FIRST so the lone checkbox keeps resolving by the field title
            # exactly as before, and only an unmatched checkbox query falls
            # through to the option-group table below.
            return self.controls[(role, name)]
        if role in self._option_nodes:
            # A group OPTION is located page-wide by its OWN accessible name (the
            # option label), exactly as `_locate_option` does before scoping.
            # `exact` is honoured by the `==` match. NEVER raises: a 0-match is a
            # real answer the caller narrows via `.and_()` and then hands off on,
            # not a locator error.
            return _FakeOptionMatch(
                [n for n in self._option_nodes[role] if n.name == name])
        raise _LocatorMiss(
            f"get_by_role({role!r}, name={name!r}) resolved to 0 elements")

    def get_by_label(self, label):
        self.requested.append(("label", label))
        if (None, label) not in self.controls:
            raise _LocatorMiss(
                f"get_by_label({label!r}) resolved to 0 elements")
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
        if css == "[data-field-path]":
            return _FakeLocatorSet(list(self.entries))
        group = self._option_group_locator(css)
        if group is not None:
            return group
        return _FakeLocatorSet(self._geo_matches(css))

    def _option_group_locator(self, css):
        """`[data-field-path="P"] input` for an option-GROUP path P (radio OR
        checkbox) -> a `_FakeOptionMatch` of that group's option controls, so
        `_locate_option`'s `.and_()` intersects at the input level. None for any
        non-group path (a path is EITHER an option group OR a combobox, never
        both), so the geo/other CSS handling below is untouched."""
        match = re.match(r'\[data-field-path="([^"]+)"\] input$', css)
        if match and match.group(1) in self.option_groups:
            return _FakeOptionMatch(self.option_groups[match.group(1)])
        return None

    # -- the geo widget's own DOM: its entry-scoped control and its listbox ----
    def _geo_matches(self, css):
        anchored = re.match(r'\[data-field-path="([^"]+)"\] input', css)
        if anchored:
            path = anchored.group(1)
            widget = self.geo.get(path)
            if widget is None:
                return []
            twin = self.geo_dupes.get(path)
            return [widget] if twin is None else [widget, twin]
        if css.endswith('[role="option"]'):
            scoped = re.match(r'\[id="([^"]+)"\] ', css)
            # A `[role=option]` query returns EVERY option node it reaches, in DOM
            # order: SCOPED to a listbox id it sees that listbox's rows only, while
            # PAGE-WIDE it sees the rows of every listbox currently mounted -- the
            # driven widget's AND any other widget's that is still open. Returning
            # the first non-empty widget's rows made the page-wide scan look
            # identical to the scoped one whenever the driven widget offered
            # anything, which is what left the scoping guard untestable here
            # (round-13 MAJOR 1).
            rows = []
            for widget in self.geo.values():
                if scoped and widget.listbox_id != scoped.group(1):
                    continue
                rows.extend(widget.options())
            return rows
        if css in ('input[role="combobox"]', "input"):
            # PAGE-WIDE and UNSCOPED. The live page carries one combobox input per
            # combobox FIELD (the Dubbing posting carries two), so a selector that
            # has lost its entry scope resolves to ALL of them in DOM order -- and
            # the driver then takes found[0] for EVERY field, typing one field's
            # answer into another field's control. The fake must be able to express
            # that (round-9 MAJOR 1): while this branch did not exist, an unscoped
            # anchor fell through to the fallback CSS, still resolved correctly, and
            # the entry-scoping guard was untestable here.
            return list(self.geo.values())
        return []


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
    # An Ashby form whose required set is all safely fillable (text + the dropdown
    # select + resume; no required checkbox): every required field lands and
    # readback-confirms -> COMPLETE.
    #
    # ROUND-7 CORRECTION. The select commits by CLICKING a suggestion row, not by
    # the controlled-component value-set this test used to assert. That is not a
    # weakening: it is what the live widget does, and the old path is a FALSE
    # COMPLETE on it (see `test_ashby_dropdown_select_value_set_is_a_false_
    # complete_so_it_commits_by_option_click`, which drives the live behaviour).
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeAshbyPage(fieldmap)

    report = ashby.fill(page, fieldmap, values)

    assert report.vendor == "ashby"
    # (1) never-send installed exactly once.
    assert len(page.routed) == 1 and page.routed[0][0] == "**"
    # (2)+(3) text via type_human, the select via the suggestion-commit driver,
    # resume via set_input_files -- every one readback-confirmed.
    assert page.controls[("textbox", "Full name")].input_value() == \
        "Test Candidate"
    assert page.controls[("textbox", "Email")].input_value() == \
        "test.candidate@example.invalid"
    visa = page.geo[_VISA_KEY]
    assert visa.clicked_option == "No"        # a suggestion row was CLICKED
    assert visa.input_value() == "No"         # and the widget committed it
    assert visa.expanded is False             # listbox collapsed = committed
    assert page.file_inputs[0].set_input_files_calls == 1
    assert report.readback_mismatches == []
    # (4) live DOM sweep agrees with the schema required set -> no gap.
    assert report.required_unfilled == []
    assert report.complete is True
    assert report.caption().endswith("COMPLETE")
    assert not report.caption().endswith("NOT COMPLETE")


def test_fill_drives_select_via_controlled_component_never_react_or_native(tmp_path):
    # The select is driven by ASHBY'S OWN protocol and NEITHER a react-select
    # locator (`#react-select-...`) NOR a native select_option is ever used -- the
    # core Ashby override, and the half of this test that was always right.
    #
    # ROUND-7 CORRECTION to the other half. Ashby's live select control is the
    # nameless React combobox, which commits ONLY on a suggestion click; the
    # controlled-component value-set this used to assert leaves the listbox open
    # with nothing selected while reading its own filter text back (a FALSE
    # COMPLETE, proven live 2026-07-13). So the driver assertion moves to the
    # protocol the control actually has, and the fake combobox RAISES on
    # `evaluate` -- so a regression to the value-set path fails loudly here.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeAshbyPage(fieldmap)

    ashby.fill(page, fieldmap, values)

    visa = page.geo[_VISA_KEY]
    assert visa.clicked_option == "No"
    assert visa.expanded is False
    assert not any("react-select" in css for css in page.located_css)
    # never located by role+name: the control has no accessible name to find.
    assert not any(_VISA_LABEL in str(req) for req in page.requested)


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
    # A required select the widget never actually commits must NOT count as
    # filled: it surfaces as a genuine required gap, with an honest reason.
    #
    # ROUND-7 CORRECTION, and the gate got STRICTER rather than looser. The
    # not-committing control modelled here is now the LIVE one: the suggestion
    # click does not register, so the listbox stays OPEN and React holds no value,
    # while the filter text sits in the box reading back EQUAL to the intent. A
    # readback alone would call that FILLED. The driver refuses it because it
    # demands the second signal too (the listbox must have collapsed), which is
    # exactly the false-COMPLETE this vendor's widget is capable of.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeAshbyPage(fieldmap, geo_class=_FakeGeoAutocompleteDeadClick)

    report = ashby.fill(page, fieldmap, values)

    visa = page.geo[_VISA_KEY]
    # the driver DID try: it typed the filter and clicked the row ...
    assert visa.click_attempts == 1
    # ... the click never registered, so the widget took NO option and React holds
    # NO value (its listbox was still open when the driver checked) ...
    assert visa.clicked_option is None
    # ... yet the TEXT read back EQUAL to the intent, because the filter box still
    # held what was typed. THAT is the trap, and it is recorded here: a driver
    # gated on the readback ALONE counts this field FILLED. The second signal (the
    # listbox must have collapsed) is the only thing that refuses it.
    mismatch = {m["key"]: m for m in report.readback_mismatches}
    assert mismatch[_VISA_KEY]["actual"] == "No" == mismatch[_VISA_KEY]["intended"]
    # ... so the field is NOT counted filled, it blocks, and the abandoned filter
    # is cleared out of the box so the JSON and the screenshot cannot disagree.
    assert report.complete is False
    gap = {g["key"]: g for g in report.required_unfilled}
    assert _VISA_KEY in gap
    assert "did not commit" in gap[_VISA_KEY]["reason"]
    assert visa.input_value() == ""


def test_fill_required_checkbox_is_driven_and_confirmed_not_handed_off(tmp_path):
    # RETARGET (owner ruling 2026-07-13, automations-automate-everything-tos-
    # boundary): the engine automates every field a vendor's ToS permits,
    # including checkboxes; Ashby's Turnstile is invisible, and ticking an
    # ordinary consent checkbox is not clicking the captcha widget. The full
    # fixture carries a REQUIRED consent checkbox; resolve_values ticks it
    # True, and fill() now DRIVES it through the kernel's ControlKind.CHECKBOX
    # (`.check()`, readback-confirmed via `.is_checked()`), so it lands like
    # any other required field instead of being handed off.
    fieldmap = _fieldmap()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    # resolve_values did resolve the consent checkbox to a boolean True value...
    consent = {fv.key: fv for fv in values.fields}[_CONSENT_KEY]
    assert consent.value is True

    page = _FakeAshbyPage(fieldmap, sweep_required_labels=(
        _SAFE_REQUIRED_LABELS + (_CONSENT_LABEL,)))
    report = ashby.fill(page, fieldmap, values)

    # ...fill() DID request a locator for it (a role+name get_by_role, its own
    # accessible name is the field title) and DROVE it via .check()...
    assert ("role", "checkbox", _CONSENT_LABEL) in page.requested
    control = page.controls[("checkbox", _CONSENT_LABEL)]
    assert control.is_checked() is True
    # ...confirmed, so it counts filled, not a required gap, and the run
    # completes.
    assert _CONSENT_KEY not in {g["key"] for g in report.required_unfilled}
    assert report.complete is True
    # "- COMPLETE", not just the "COMPLETE" suffix every "NOT COMPLETE" also
    # carries.
    assert report.caption().endswith("- COMPLETE")


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


# =============================================================================
# W5B-ASHBY F1: the geo-autocomplete Location driver
# =============================================================================


def test_ashby_location_captured_as_combobox():
    # F1, capture side. The live schema types Location as `Location` -- a type
    # _ASHBY_TYPE_MAP does not know -- so it fell through to the unknown-type
    # fallback and was typed role "textbox"; the live control is a React
    # combobox, so the textbox locator never resolved and the fill timed out.
    # A location-shaped label on a String/unknown type now carries the COMBOBOX
    # role (which is what routes it to the geo driver), while its type is left
    # untouched so the kernel keeps rendering it as the free text it is.
    posting = {
        "id": "9002",
        "applicationForm": {"sections": [{"title": "Apply", "fieldEntries": [
            {"isRequired": True, "field": {
                "path": "custom_location", "title": "Location",
                "type": "Location"}},
            {"isRequired": False, "field": {
                "path": "custom_city", "title": "City", "type": "String"}},
            {"isRequired": True, "field": {
                "path": "_systemfield_name", "title": "Full name",
                "type": "String"}},
            # The trap a substring rule falls into. The NOUN "relocation"
            # literally contains the substring "location" (re-LOCATION); the
            # VERB "relocate" does NOT, which is why the round-5 title here
            # ("Are you willing to relocate?") left the trap DISARMED and a
            # substring implementation of `_is_geo_label` survived the suite.
            # This title is a CONSTRUCTED example, not an observed Ashby label:
            # the posting dom.html was captured from (elevenlabs/b43e388f, CLOSED)
            # serves no relocation question (zero "relocat" labels in dom.html,
            # which is what the assertion below re-checks against the fixture that
            # is actually on disk). It is an ANSWERABLE question the
            # kernel resolves from canned answers (kernel/resolve.py
            # `_ANSWER_MATCHERS`, the ("relocat",) row), so it must stay a plain
            # text box: a substring rule captures it as a geo combobox, sends it
            # to the autocomplete driver, which finds no place suggestion for a
            # yes/no answer and leaves a required, fillable field unfilled.
            {"isRequired": False, "field": {
                "path": "custom_relocation",
                "title": "Do you require relocation assistance?",
                "type": "String"}},
            # A location-LABELLED select keeps its options and its
            # controlled-component driver: it is not an autocomplete.
            {"isRequired": False, "field": {
                "path": "custom_office", "title": "Preferred office location",
                "type": "ValueSelect",
                "selectableValues": [{"label": "Remote"}, {"label": "Onsite"}]}},
        ]}]},
    }
    by_key = {f.key: f for f in ashby_capture._parse_ashby(
        posting, "fauxcorp", "9002", now=lambda: _PINNED).fields}

    location = by_key["custom_location"]
    assert location.locator.role == "combobox"
    assert location.locator.name == "Location"
    assert location.type == "location"      # the schema type, passed through
    assert location.options == []           # an autocomplete has no schema options
    assert by_key["custom_city"].locator.role == "combobox"

    assert by_key["_systemfield_name"].locator.role == "textbox"

    # THE TOKEN RULE (the wave's ratified deviation from the task-spec's literal
    # "contains"; see the ratification marker at capture.py `_GEO_LABEL_TOKENS`).
    # The trap is ARMED, asserted here so it can never silently disarm again: the
    # title really does carry "location" as a SUBSTRING while NOT carrying it as
    # a TOKEN, which is the one shape that tells the two rules apart. Replace the
    # token split in `_is_geo_label` with `in` and this field captures as a geo
    # combobox and the assertion below fails.
    trap = by_key["custom_relocation"].label.lower()
    assert "location" in trap                                        # substring: YES
    assert "location" not in re.split(r"[^a-z0-9]+", trap)           # token: NO
    assert by_key["custom_relocation"].locator.role == "textbox"     # NOT combobox

    office = by_key["custom_office"]
    assert office.type == "multi_value_single_select"
    assert office.options == ["Remote", "Onsite"]


def test_ashby_geo_autocomplete_selects_unique_suggestion(tmp_path):
    # F1, fill side. The driver types a filter, reads what the widget renders,
    # and commits the ONE suggestion that uniquely matches the intended value
    # (here by the sealed ladder's token-subset step: the widget's dataset is
    # coarser than the SSOT's value). The control is reached through its
    # entry's data-field-path -- never get_by_role, which cannot name it.
    fieldmap = _fieldmap_with_geo()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    assert {fv.key: fv.value for fv in values.fields}[_GEO_KEY] == _GEO_VALUE

    page = _FakeAshbyPage(
        fieldmap,
        sweep_required_labels=_SAFE_REQUIRED_LABELS + (_GEO_LABEL,),
        geo_suggestions=(_GEO_DECOY, _GEO_MATCH))
    report = ashby.fill(page, fieldmap, values)

    widget = page.geo[_GEO_KEY]
    # it was SHOWN the fuzzy-match decoy on the way and declined it...
    assert any(_GEO_DECOY in rows for rows in widget.offered)
    assert widget.clicked_option == _GEO_MATCH     # ...and chose the true match
    assert widget.input_value() == _GEO_MATCH      # the control committed it
    assert widget.expanded is False                # listbox collapsed = committed
    # the a11y locator was never even attempted for it (it has no such name)
    assert not any(_GEO_LABEL in str(req) for req in page.requested)
    assert not any(g["key"] == _GEO_KEY for g in report.required_unfilled)
    assert report.complete is True


def test_ashby_geo_autocomplete_no_unique_match_stays_unfilled(tmp_path):
    # F1 ANTI-GAMING. The live widget fuzzy-matches: the filter "Milan" offered
    # exactly one suggestion, "Christmas Island". A first-option commit would
    # write a place the candidate never named and report the form COMPLETE. The
    # driver is SHOWN that suggestion here and must refuse it: no unique match
    # against the intended value -> the field stays HONESTLY UNFILLED and forces
    # NOT_COMPLETE, with no guessing and no blind commit.
    fieldmap = _fieldmap_with_geo()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeAshbyPage(
        fieldmap,
        sweep_required_labels=_SAFE_REQUIRED_LABELS + (_GEO_LABEL,),
        geo_suggestions=(_GEO_DECOY,))

    report = ashby.fill(page, fieldmap, values)

    widget = page.geo[_GEO_KEY]
    # the trap fired: the widget DID offer the junk suggestion...
    assert any(_GEO_DECOY in rows for rows in widget.offered)
    # ...and the driver committed nothing.
    assert widget.clicked_option is None
    assert widget.input_value() != _GEO_DECOY
    # and it left the box EMPTY: a refusal that leaves the last typed filter in
    # the input makes the JSON ("unfilled") disagree with the screenshot.
    assert widget.input_value() == ""
    gap = {g["key"]: g for g in report.required_unfilled}
    assert _GEO_KEY in gap
    assert "no suggestion uniquely matched" in gap[_GEO_KEY]["reason"]
    assert report.complete is False
    assert report.caption().endswith("NOT COMPLETE")


# =============================================================================
# W5B-ASHBY F2: schema/DOM required-set reconciliation (the sweep stays honest)
# =============================================================================


def test_ashby_filled_react_widget_not_flagged_schema_only(tmp_path):
    # F2(a). Ashby's React widgets carry no native `required` attribute, so the
    # DOM sweep is structurally blind to them and every one came back as a bogus
    # schema_only gap. A required widget that the fill FILLED and READBACK-
    # VERIFIED is accounted for by the strongest evidence there is -- the control
    # read our value back -- so it no longer re-enters required_unfilled.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeAshbyPage(
        fieldmap,
        # the sweep cannot see the React select at all (no required attr)...
        sweep_required_labels=tuple(l for l in _SAFE_REQUIRED_LABELS
                                    if l != _VISA_LABEL),
        # ...and Ashby's own entry marker is absent on it too.
        entries=_entries_for(fieldmap, unmarked_keys={_VISA_KEY}))

    report = ashby.fill(page, fieldmap, values)

    # the widget genuinely committed -- that is WHY it reconciles.
    assert page.geo[_VISA_KEY].input_value() == "No"
    assert page.geo[_VISA_KEY].expanded is False      # committed, not just typed
    assert _VISA_KEY in {fv.key for fv in values.fields}
    assert report.required_unfilled == []
    assert report.complete is True


def test_ashby_offstep_required_reported_with_step_reason(tmp_path):
    # F2(c). A required field the schema places on a step the page has not
    # mounted is not a sweep gap and not a locator bug: it is simply not on this
    # page yet. It stays a REQUIRED GAP (still NOT_COMPLETE) but is reported
    # with a reason that names the step, instead of a timeout from a control
    # that was never there.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    fieldmap.fields.append(Field(
        key="custom_portfolio", label="Portfolio URL", type="input_text",
        required=True, options=[], source=ASHBY_SOURCE,
        locator=Locator(role="textbox", name="Portfolio URL"), step_index=1))
    values = _resolved_values(fieldmap, tmp_path=tmp_path)

    # only the step-0 fields are mounted in the DOM.
    mounted = [f for f in fieldmap.fields if f.step_index == 0]
    page = _FakeAshbyPage(fieldmap, entries=_entries_for(
        FieldMap(vendor="ashby", posting_id="9001", captured_at=_PINNED,
                 fields=mounted)))
    report = ashby.fill(page, fieldmap, values)

    gap = {g["key"]: g for g in report.required_unfilled}
    assert "custom_portfolio" in gap
    reason = gap["custom_portfolio"]["reason"]
    assert "step 1" in reason and "mounted step(s) [0]" in reason
    # reported ONCE, as a step gap -- never also as a phantom DOM-sweep gap.
    assert not any(g["key"].startswith("dom-sweep:") and "portfolio" in g["key"]
                   for g in report.required_unfilled)
    assert report.complete is False


def test_ashby_uncaptured_required_still_blocks(tmp_path):
    # F2 ANTI-GAMING. Reconciliation may only remove NOISE, never a real gap. A
    # required control the schema never captured must still force NOT_COMPLETE,
    # from BOTH directions: an Ashby field entry the schema missed (caught under
    # its own label), and a required control outside Ashby's entry structure
    # entirely (which the alias translation must pass through UNTRANSLATED, not
    # swallow).
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeAshbyPage(
        fieldmap,
        # a required control the sweep sees but no entry owns:
        sweep_required_labels=_SAFE_REQUIRED_LABELS + ("Cover letter",),
        entries=_entries_for(fieldmap, extra=[
            # a required Ashby field the graphql schema never captured:
            _FakeEntry("custom_new_question", "New question", required=True)]))

    report = ashby.fill(page, fieldmap, values)

    reasons = {g["label"]: g["reason"] for g in report.required_unfilled}
    assert "new question" in reasons
    assert "absent from the schema" in reasons["new question"]
    assert "cover letter" in reasons
    assert "absent from the schema" in reasons["cover letter"]
    assert report.complete is False
    assert report.caption().endswith("NOT COMPLETE")


# =============================================================================
# W5B-ASHBY round 2: the review's blockers, pinned
# =============================================================================


def test_ashby_geo_refuses_a_suggestion_that_contains_the_value(tmp_path):
    # BLOCKER 2. A place that CONTAINS your place is a DIFFERENT place. With a
    # single-token SSOT location, the sealed content ladder's BIDIRECTIONAL
    # token-subset step made "Faketown, Otherstate" a unique hit for "Faketown"
    # and COMMITTED it -- a wrong city sent to a real employer, which is worse
    # than an unfilled field (unfilled is honest and recoverable; wrong is
    # neither). The geo matcher refuses the superset direction outright: the
    # field stays unfilled and blocks.
    fieldmap = _fieldmap_with_geo()
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(location="Faketown"))
    assert {fv.key: fv.value for fv in values.fields}[_GEO_KEY] == "Faketown"

    page = _FakeAshbyPage(
        fieldmap,
        sweep_required_labels=_SAFE_REQUIRED_LABELS + (_GEO_LABEL,),
        geo_suggestions=("Faketown, Otherstate",))
    report = ashby.fill(page, fieldmap, values)

    widget = page.geo[_GEO_KEY]
    # the widget DID offer the containing place, uniquely...
    assert any("Faketown, Otherstate" in rows for rows in widget.offered)
    # ...and the driver refused it rather than move the candidate to Otherstate.
    assert widget.clicked_option is None
    assert widget.input_value() == ""
    gap = {g["key"]: g for g in report.required_unfilled}
    assert _GEO_KEY in gap
    assert "no suggestion uniquely matched" in gap[_GEO_KEY]["reason"]
    assert report.complete is False


def test_ashby_geo_no_op_click_leaves_the_field_unfilled(tmp_path):
    # MINOR 2 (mutation M6). The typed FILTER is not a committed value: when the
    # suggestion click never registers, React holds no location even though the
    # input still shows the text. Here the filter equals the suggestion exactly,
    # so a text-only readback would report the field FILLED. The driver requires
    # the widget's own commit signal (the listbox collapsing) as well, so it
    # reports the field honestly unfilled -- and clears the stray text.
    fieldmap = _fieldmap_with_geo()
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(location="Faketown"))
    page = _FakeAshbyPage(
        fieldmap,
        sweep_required_labels=_SAFE_REQUIRED_LABELS + (_GEO_LABEL,),
        geo_suggestions=("Faketown",),
        geo_class=_FakeGeoAutocompleteDeadClick)

    report = ashby.fill(page, fieldmap, values)

    widget = page.geo[_GEO_KEY]
    assert widget.click_attempts == 1        # the driver DID choose the row...
    assert widget.clicked_option is None     # ...and the widget never committed it
    gap = {g["key"]: g for g in report.required_unfilled}
    assert _GEO_KEY in gap
    assert "did not commit" in gap[_GEO_KEY]["reason"]
    # the readback that a text-only check would have believed:
    mismatch = {m["key"]: m for m in report.readback_mismatches}
    assert mismatch[_GEO_KEY]["actual"] == "Faketown"
    assert widget.input_value() == ""        # stray filter text cleared
    assert report.complete is False


def test_ashby_colliding_placeholder_rogue_required_still_blocks(tmp_path):
    # BLOCKER 1. Ashby ships no aria-label, so the kernel sweep names a control
    # by its PLACEHOLDER and FOUR distinct required controls on the live page
    # share the name "type here...". Round-1 decided entry ownership by NAME, so
    # a required control OUTSIDE the field structure whose placeholder collided
    # with a captured field's was attributed to an entry that does not own it and
    # VANISHED -- the report said COMPLETE with a required control unfilled on
    # the page. Ownership is now counted by NODE: the page carries one more
    # "type here..." control than the entries own, so the name passes through
    # RAW and still blocks.
    # The second head of the same bug, pinned alongside it: an entry that OWNS a
    # required control but carries no readable label used to contribute nothing
    # to the DOM side while still suppressing its control's swept name, which
    # dissolved that control too. It now surfaces under a synthetic name built
    # from its path, which cannot collide with any schema label.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    unlabelled = _FakeEntry("custom_unlabelled", "", native_required=True,
                            placeholders=("Type here...",))
    page = _FakeAshbyPage(
        fieldmap,
        # 4 required controls named "Type here...": the three the entries own
        # (Full name, Email, the unlabelled one) plus ONE ROGUE no entry owns.
        sweep_required_labels=("Type here...", "Type here...", "Type here...",
                               "Type here...", "Resume", _VISA_LABEL),
        entries=_entries_for(fieldmap, extra=[unlabelled]))

    report = ashby.fill(page, fieldmap, values)

    gaps = {g["label"]: g for g in report.required_unfilled}
    assert "type here..." in gaps                     # the rogue still blocks
    assert "absent from the schema" in gaps["type here..."]["reason"]
    assert "data-field-path:custom_unlabelled" in gaps   # and so does the entry
    assert report.complete is False
    assert report.caption().endswith("NOT COMPLETE")


def test_ashby_unfilled_react_widget_is_not_reconciled_away(tmp_path):
    # MINOR 1 (mutation M5). Reconciling a React widget off the sweep diff is
    # EARNED by a readback-verified fill, never granted by the field's type. Same
    # page as the filled-widget test, except the widget's controlled component
    # rejects the value: it must stay on the schema side of the diff and surface
    # as a sweep gap, on top of the census gap.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeAshbyPage(
        fieldmap,
        sweep_required_labels=tuple(label for label in _SAFE_REQUIRED_LABELS
                                    if label != _VISA_LABEL),
        entries=_entries_for(fieldmap, unmarked_keys={_VISA_KEY}),
        geo_class=_FakeGeoAutocompleteDeadClick)

    report = ashby.fill(page, fieldmap, values)

    assert page.geo[_VISA_KEY].input_value() == ""      # never committed
    gaps = {g["key"]: g for g in report.required_unfilled}
    assert _VISA_KEY in gaps                       # the census caught it...
    sweep_gap = gaps.get(f"dom-sweep:{_VISA_LABEL.lower()}")
    assert sweep_gap is not None                   # ...and so did the sweep
    assert "did not find it required" in sweep_gap["reason"]
    assert report.complete is False


def test_ashby_real_dom_reconciles_through_the_kernel_sweep_and_still_bites():
    # MAJOR 1. The fake harness names its swept controls by aria-label, which
    # makes dom-name == schema-label and turns the whole name-space translation
    # into a pass-through. This drives the REAL kernel sweep over the REAL
    # captured DOM, where the sweep sees only PLACEHOLDERS and cannot see the
    # React widgets at all.
    page = _HtmlPage(_real_dom())
    fieldmap = _real_dom_fieldmap()
    schema_required = {f.label for f in fieldmap.required_fields()}
    dom_required = base.sweep_required(page)

    # what the kernel sweep ACTUALLY returns for a form with 6 required fields:
    assert dom_required == {"type here...", "hello@example.com..."}
    raw = base.completeness_mismatch(schema_required, dom_required)
    assert len(raw["dom_only"]) == 2 and len(raw["schema_only"]) == 6   # 8 phantoms

    # reconciled, with every required field filled and readback-verified:
    filled = {f.key for f in fieldmap.required_fields()}
    reconciled = ashby_fill._reconcile(page, fieldmap, filled,
                                       schema_required, dom_required)
    assert base.completeness_mismatch(reconciled.schema_required,
                                      reconciled.dom_required) == {
        "dom_only": [], "schema_only": []}

    # and the sweep STILL BITES on the same DOM: one more required control,
    # outside every [data-field-path] entry, carrying the COLLIDING placeholder.
    rogue = _HtmlPage(
        _real_dom() + '<input required placeholder="Type here...">')
    rogue_dom = base.sweep_required(rogue)
    rogue_reconciled = ashby_fill._reconcile(rogue, fieldmap, filled,
                                             schema_required, rogue_dom)
    assert base.completeness_mismatch(
        rogue_reconciled.schema_required,
        rogue_reconciled.dom_required)["dom_only"] == ["type here..."]


def test_fill_report_reuses_the_existing_fillreport_dataclass(tmp_path):
    from engine.kernel.contracts import FillReport

    fieldmap = _fieldmap_without(_CONSENT_KEY)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeAshbyPage(fieldmap)
    report = ashby.fill(page, fieldmap, values)
    assert isinstance(report, FillReport)
    blob = report.to_dict()
    assert blob["vendor"] == "ashby"
    # posting_id fallback (no company slug supplied).
    assert blob["company"] == fieldmap.posting_id


# =============================================================================
# capture path: ashby graphql interception (offline, fake-driven)
# =============================================================================
# No playwright, no network: the capture path is driven through a fake browser
# factory over fixture data (a live-confirmed ashby non-user-graphql form
# response, plus a legacy-shaped one exercising the one-release fallback probe).
# The autouse no-network guard is satisfied throughout. Schema compliance,
# source tags, and coverage() interop are asserted on the produced FieldMaps;
# shape drift is proven to raise CaptureShapeError rather than yield an empty
# map.
#
# DELIBERATE per-vendor duplication (owner-ratified 2026-07-10): vendor test files
# stay self-contained so the per-vendor development loops never co-edit a shared
# file (DRY exception: decoupling outweighs consolidation here). The fakes below
# will diverge as each vendor's capture path evolves; a shared copy would become a
# co-edit surface. Do not consolidate.


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
        # The capture-time DOM probe (`capture._rendered_roles`) reads the live
        # page through `.locator`; an empty html yields no entries, so a page with
        # no DOM contributes NO dom role for any field and every field falls back
        # to `_ashby_field_type` (the fallback path the other capture tests
        # exercise).
        self._dom = _HtmlPage(html) if html else None

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

    def locator(self, css):
        if self._dom is None:
            return _FakeLocatorSet([])
        return self._dom.locator(css)


def _factory_for(page):
    @contextlib.contextmanager
    def factory():
        yield page
    return factory


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

    # CORRECTED (round 7). This asserted `role == "listbox"`, which is what the
    # kernel's generic type map guesses for a multi-select and what NO ELEMENT ON
    # ANY ASHBY PAGE CARRIES: live, a MultiValueSelect is a native CHECKBOX group
    # (6 of 6 fields, over 30 postings re-probed 2026-07-13; page-wide listbox
    # count on every one of them: ZERO). The old role built
    # `get_by_role("listbox", name=...)`, which resolved to nothing and killed the
    # field as a fill-error. Changing it is a CORRECTION to match the live DOM,
    # not a weakening: it is the difference between a phantom and a control.
    #
    # This capture runs with NO page, so it exercises the FALLBACK role
    # (`capture._ASHBY_FALLBACK_ROLE`) rather than the DOM probe -- which is
    # exactly why the fallback had to be corrected too, and not just the probe.
    pronouns = by_key["custom_pronouns"]
    assert pronouns.type == "multi_value_multi_select"
    assert pronouns.locator.role == "checkbox"       # NOT the phantom "listbox"
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


_RADIO_PATH = "6825d01d-72c1-40e3-9617-7def67b066df"   # the radio entry in dom.html


def _radio_posting():
    """A synthetic graphql posting with TWO single-select ValueSelects of the SAME
    graphql type, on two paths that are BOTH MOUNTED in the captured DOM:
    `_RADIO_PATH`'s entry renders eight native `input[type=radio]` (the seal
    posting, dom.html) and `_DROPDOWN_PATH`'s renders the nameless React combobox
    (a sibling posting, `_SIBLING_DOM`). So the DOM, never the graphql type
    (identical for both), is what decides the role -- and BOTH poles are now REAL
    Ashby controls.

    Round 6 had no dropdown-rendered ValueSelect to point at, and borrowed a plain
    TEXT-input entry as the stand-in. That was defensible while only radios were
    read off the DOM, and it stopped being defensible the moment the DOM decides
    every role: a text input is a textbox, not a dropdown. The real one is
    captured now, so the test says what it means.

    The option LABELS come from the graphql, never from the DOM, so the synthetic
    option lists here do not have to match the real controls' wording."""
    return {"id": "9004", "applicationForm": {"sections": [{"title": "Apply",
        "fieldEntries": [
            {"isRequired": True, "field": {
                "path": _RADIO_PATH,
                "title": "How did you hear about us?", "type": "ValueSelect",
                "selectableValues": [{"label": "Job board", "value": "jb"},
                                     {"label": "Referral", "value": "ref"},
                                     {"label": "Social media", "value": "soc"}]}},
            {"isRequired": True, "field": {
                "path": _DROPDOWN_PATH, "title": "Team preference",
                "type": "ValueSelect",
                "selectableValues": [{"label": "Research", "value": "r"},
                                     {"label": "Engineering", "value": "e"}]}},
        ]}]}}


def _radio_posting_fillable():
    """A posting whose base fields fill normally plus a REQUIRED, ANSWERABLE
    ValueSelect that the DOM renders as radios (path `_RADIO_PATH`). The
    question is a sponsorship ask the SSOT answers 'No', so the resolver DOES
    produce a value for it -- which the fill now DRIVES (W5.1c radio-group
    adoption): it locates the resolved option inside the group's entry and ticks
    it, single-select."""
    return {"id": "9005", "applicationForm": {"sections": [{"title": "Apply",
        "fieldEntries": [
            {"isRequired": True, "field": {
                "path": "_systemfield_name", "title": "Full name",
                "type": "String"}},
            {"isRequired": True, "field": {
                "path": "_systemfield_email", "title": "Email", "type": "Email"}},
            {"isRequired": True, "field": {
                "path": "_systemfield_resume", "title": "Resume", "type": "File"}},
            {"isRequired": True, "field": {
                "path": _RADIO_PATH, "title": _VISA_LABEL, "type": "ValueSelect",
                "selectableValues": [{"label": "Yes", "value": "y"},
                                     {"label": "No", "value": "n"}]}},
        ]}]}}


def test_ashby_radio_group_drives_the_resolved_option_and_completes(tmp_path):
    """W5.1c radio-group adoption, now due: the deferral, now unlocked, on the
    TB5 live ashby run, satisfied twice over (2026-07-18/19 w5_accept). This is
    the sanctioned rewrite the old
    `test_ashby_radio_is_handed_off_not_driven_and_still_blocks` foresaw ("W5.1c
    teaches the engine a trusted radio click and will legitimately rewrite this
    test; that rewrite is the planned path, not a test-weakening").

    A radio-rendered single-select ValueSelect (role=radio via the DOM probe) is
    now DRIVEN, not handed off: the resolved option is located inside this group's
    own entry (`_locate_option`, lever's FX1 shape) and ticked via the shared
    `drive_control` (`.check()` + `.is_checked()` readback). The honest-census half
    SURVIVES the rewrite: the option is driven ONLY when it resolves to exactly one
    control and reads back checked, single-select semantics leave every other
    option untouched, and an unlocatable/unconfirmed option is a named required gap
    (covered by the sibling hand-off tests), never auto-filled behind the census's
    back."""
    raw = {"data": {"jobPosting": _radio_posting_fillable()}}
    page_cap = _FakePage(responses=_ashby_responses(raw), html=_real_dom())
    fieldmap = capture_ashby("fauxcorp", "9005", _factory_for(page_cap),
                             now=lambda: _PINNED)
    radio = {f.key: f for f in fieldmap.fields}[_RADIO_PATH]
    assert radio.locator.role == "radio"          # capture precondition (DOM decides)
    assert radio.options == ["Yes", "No"]

    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    # the resolver answered it (an answer exists), and the fill now DRIVES it.
    assert {fv.key: fv.value for fv in values.fields}[_RADIO_PATH] == "No"

    page = _FakeAshbyPage(fieldmap)
    report = ashby.fill(page, fieldmap, values)

    # DRIVEN: exactly the resolved option "No" was ticked, inside this group's
    # entry; the other option stayed untouched (single-select semantics).
    by_name = {o.name: o for o in page.option_groups[_RADIO_PATH]}
    assert by_name["No"].is_checked() is True
    assert by_name["Yes"].is_checked() is False
    # a `.check()` on the located option, never a `.click()` (Turnstile), and the
    # per-option locate went through `get_by_role("radio", name=<option>)`.
    assert ("role", "radio", "No") in page.requested
    assert ("role", "radio", "Yes") not in page.requested   # only the answer driven
    # counts filled: NOT a required gap, not a skip, and the run completes.
    assert _RADIO_PATH not in {g["key"] for g in report.required_unfilled}
    assert _RADIO_PATH not in {key for key, _ in report.skipped}
    assert report.readback_mismatches == []
    assert report.complete is True
    assert report.caption().endswith("- COMPLETE")
    # the base fields still filled around it.
    assert page.controls[("textbox", "Full name")].input_value() == "Test Candidate"


def test_ashby_radio_rendered_valueselect_captured_as_radio():
    # #12. A ValueSelect whose LIVE DOM entry renders native radios is captured
    # role=radio (a click-hazard the fill hands off), the QUESTION as its label,
    # and the option LABELS enumerated -- NOT role=combobox (the type-map default
    # that sent a driver at a control that does not exist, and timed out).
    #
    # The discriminating control is the sibling ValueSelect of the SAME graphql
    # type at `_DROPDOWN_PATH`, which really is a DROPDOWN on a live posting (see
    # `_SIBLING_DOM`). Same schema type, two different controls, two different
    # roles: it is the DOM that decides, and the schema cannot. A probe reduced to
    # "every mounted entry is a radio", or widened to "the page has a radio
    # somewhere", flips this sibling to radio and fails here.
    raw = {"data": {"jobPosting": _radio_posting()}}
    page = _FakePage(responses=_ashby_responses(raw), html=_dom_with_siblings())
    fm = capture_ashby("fauxcorp", "9004", _factory_for(page), now=lambda: _PINNED)
    by_key = {f.key: f for f in fm.fields}

    radio = by_key[_RADIO_PATH]
    assert radio.locator.role == "radio"                 # NOT combobox
    assert radio.label == "How did you hear about us?"    # the QUESTION
    assert radio.locator.name == "How did you hear about us?"
    assert radio.options == ["Job board", "Referral", "Social media"]  # LABELS
    assert radio.type == "multi_value_single_select"     # still a single-select
    assert radio.required is True

    # the sibling ValueSelect: same graphql type, a REAL dropdown-rendered entry.
    # It keeps the combobox role and its own options.
    dropdown = by_key[_DROPDOWN_PATH]
    assert dropdown.locator.role == "combobox"          # NOT radio
    assert dropdown.options == ["Research", "Engineering"]


# -- the FX1 collision on the REAL ashby radio shape ---------------------------
# `_locate_option` must resolve an option to the ONE control inside its OWN field
# entry even when a sibling group renders the SAME wording, and must REFUSE (None
# -> hand-off) an option that resolves to 0 or 2+ controls. Built from the live
# radio markup (`_ashby_radio_entry`, the byte structure of dom.html's radio
# entry) and resolved through an INDEPENDENT ARIA resolver (`_aria_role` /
# `_accessible_name`, the SAME ones the phantom-sweep tests calibrate against the
# live browser), so this cannot merely agree with the capture code.


def _ashby_radio_entry(path, entry_id, question, options):
    """One live-shaped ashby radio-group field entry. The SHAPE is dom.html's own
    (a `data-field-path` container, a `<fieldset>`, a `<label for=path>` question,
    and one `_option_...` div per option holding an `<input type=radio>` whose id
    is `<entry_id>_<path>-labeled-radio-<i>` and whose group `name` is
    `<entry_id>_<path>`, beside a `<label for=input-id>` that IS the option's
    accessible name); only the paths and wordings are the test's. The two facts
    `_locate_option` turns on are therefore REAL here: an option resolves by its
    own `<label>`, and the group scopes by its `data-field-path` container."""
    group_name = f"{entry_id}_{path}"
    opts = []
    for i, opt in enumerate(options):
        rid = f"{group_name}-labeled-radio-{i}"
        opts.append(
            '<div class="_option_1258i_34 false"><span class="_container_132c8_28" '
            'data-disabled="false"><span class="_circle_132c8_77"></span>'
            f'<input type="radio" id="{rid}" name="{group_name}"></span>'
            f'<label for="{rid}" class="_label_1258i_42 ">{opt}</label></div>')
    return (
        f'<div data-field-path="{path}" data-field-entry-id="{group_name}">'
        '<fieldset class="_container_1258i_28 _fieldEntry_1e3gg_28">'
        '<label class="_heading_f7cvd_52 _required_f7cvd_91 _label_1e3gg_42 '
        f'ashby-application-form-question-title" for="{path}">{question}</label>'
        + "".join(opts) + "</fieldset></div>")


def _ashby_checkbox_entry(path, entry_id, question, options):
    """One live-shaped ashby CHECKBOX-group field entry, the multi-select sibling
    of `_ashby_radio_entry`. The SHAPE is `_SIBLING_DOM`'s own (the real
    five-checkbox MultiValueSelect at `2726a0b3-...`, captured 2026-07-13): a
    `data-field-path` container, a `<fieldset>`, a `<label for=path>` question,
    and one `_option_1258i_34` div per option holding an `<input type=checkbox>`
    whose id is `<entry_id>_<path>-labeled-checkbox-<i>`, beside a
    `<label for=input-id>` that IS the option's accessible name.

    THE ONE STRUCTURAL DIFFERENCE FROM THE RADIO, reproduced faithfully because it
    is the whole reason both must scope by `data-field-path`: a radio's `name` is
    the GROUP's `<entry-id>_<path>`, but a CHECKBOX's `name` is the OPTION's own
    WORDING (live: `name="Documentaries"`). So neither group carries a
    group-distinguishing submission name, and lever's `input[name="<group>"]`
    scoping has no ashby analogue for either. A fixture that gave checkboxes a
    shared group name would have invented a friendlier world in which the bug
    class this test exists for could not occur."""
    opts = []
    for i, opt in enumerate(options):
        cid = f"{entry_id}_{path}-labeled-checkbox-{i}"
        opts.append(
            '<div class="_option_1258i_34"><span class=" _container_1danv_28" '
            'data-disabled="false">'
            f'<input type="checkbox" id="{cid}" name="{opt}"></span>'
            f'<label for="{cid}" class="_label_1258i_42 ">{opt}</label></div>')
    return (
        f'<div data-field-path="{path}" data-field-entry-id="{entry_id}_{path}">'
        '<fieldset class="_container_1258i_28 _fieldEntry_1e3gg_28">'
        '<label class="_heading_f7cvd_52 _required_f7cvd_91 _label_1e3gg_42 '
        f'ashby-application-form-question-title" for="{path}">{question}</label>'
        + "".join(opts) + "</fieldset></div>")


class _AshbyDomMatch:
    """The elements a locator matches on a real parsed ashby DOM. `.count()` is the
    honest answer (0 = a phantom, >1 = a control the drive could mistake), and
    `.and_()` is Playwright's intersection -- the FX1 pattern `_locate_option`
    uses to narrow a page-wide role+name match to the one option inside a group's
    own field entry."""

    def __init__(self, nodes):
        self.nodes = list(nodes)

    def count(self):
        return len(self.nodes)

    def and_(self, other):
        return _AshbyDomMatch(
            [n for n in self.nodes if any(n is o for o in other.nodes)])


class _AshbyRadioDomPage:
    """A page that RESOLVES locators against a REAL parsed ashby apply DOM. Serves
    the two calls `_locate_option` makes: `get_by_role` (role + exact accessible
    name, via the INDEPENDENT `_aria_role`/`_accessible_name` resolver, NOT the
    capture code) and `locator` (the entry-container CSS, via `_css_select`)."""

    def __init__(self, html):
        parser = _HtmlFixtureParser()
        parser.feed(html)
        self._root = parser.root
        self.requested = []

    def get_by_role(self, role, name=None, exact=None):
        self.requested.append(("role", role, name))
        want = _norm_ws(name)
        return _AshbyDomMatch([
            n for n in _descendants(self._root)
            if _aria_role(n) == role
            and _norm_ws(_accessible_name(self._root, n)) == want])

    def locator(self, css):
        self.requested.append(("css", css))
        return _AshbyDomMatch(_css_select(self._root, css))


def test_ashby_locate_option_scopes_each_group_and_refuses_the_unlocatable():
    """The FX1 collision class on the REAL ashby radio shape, plus the hand-off
    residual (task tests b + c).

    LEVER'S FX1 (palantir, 2026-07-18) proved a bare, PAGE-WIDE
    `get_by_role(role, name=option, exact=True)` strict-mode-violates when several
    groups render the same option wording. Ashby has the same hazard and no
    submission-name to scope by, so `_locate_option` scopes by the `data-field-
    path` field ENTRY via `.and_()`. This pins BOTH halves against real-shaped DOM:
    the bare locator IS genuinely ambiguous (the bug, reproduced), and
    `_locate_option` still resolves each group's own option to exactly the control
    inside that group's entry; and an option that resolves to 0 or 2+ controls is
    REFUSED (None), which the caller hands off loudly rather than driving a guess."""
    html = (
        "<html><body><form>"
        + _ashby_radio_entry("path-a", "entryA", "Are you a user?",
                             ["Yes", "Other"])
        + _ashby_radio_entry("path-b", "entryB", "Do you require sponsorship?",
                             ["No", "Other"])
        + _ashby_radio_entry("path-c", "entryC", "Which duplicated group?",
                             ["Same", "Same"])   # a group with duplicated wording
        + "</form></body></html>")
    page = _AshbyRadioDomPage(html)

    # sanity: the live shape parses to real radios whose accessible name is the
    # option's own `<label>`, resolved by the independent ARIA resolver.
    assert page.get_by_role("radio", name="Yes", exact=True).count() == 1
    # THE BUG, reproduced: a bare page-wide "Other" is ambiguous across the two
    # groups that share the wording.
    assert page.get_by_role("radio", name="Other", exact=True).count() == 2

    # THE FIX (c): each group's own "Other" resolves to exactly one control, and it
    # is the option inside THAT group's own entry -- never the sibling's.
    a = ashby_fill._locate_option(page, "radio", "path-a", "Other")
    b = ashby_fill._locate_option(page, "radio", "path-b", "Other")
    assert a.count() == 1 and b.count() == 1
    assert a.nodes[0] is not b.nodes[0]
    a_group = page.locator('[data-field-path="path-a"] input').nodes
    b_group = page.locator('[data-field-path="path-b"] input').nodes
    assert any(a.nodes[0] is n for n in a_group)      # a's "Other" is inside a
    assert any(b.nodes[0] is n for n in b_group)      # b's "Other" is inside b
    assert not any(a.nodes[0] is n for n in b_group)  # never the sibling's

    # HAND-OFF (b): an option resolving to ZERO controls in the group is refused.
    assert ashby_fill._locate_option(page, "radio", "path-a", "Nonexistent") is None
    # and one resolving to 2+ (a group with duplicated wording) is refused too:
    # `_resolves_to_one` sees the count is not 1 even after scoping.
    assert page.get_by_role("radio", name="Same", exact=True).and_(
        page.locator('[data-field-path="path-c"] input')).count() == 2
    assert ashby_fill._locate_option(page, "radio", "path-c", "Same") is None


def test_ashby_locate_option_scopes_checkbox_groups_and_never_crosses_roles():
    """THE FX1 COLLISION ON THE REAL ASHBY CHECKBOX SHAPE (P2-5), against parsed
    DOM built from `_SIBLING_DOM`'s own five-checkbox markup and resolved through
    the SAME INDEPENDENT ARIA resolver, so this cannot merely agree with the
    driver.

    Three things are proven, and the third is a collision class the checkbox
    adoption CREATES rather than inherits:

    (a) THE BUG, reproduced: a bare page-wide `get_by_role("checkbox",
        name="Documentaries", exact=True)` is genuinely ambiguous when two
        checkbox groups render the same wording, exactly as lever's FX1 showed
        for radios. Ashby has no submission name to scope by here either -- and
        LESS than for radios, since a live checkbox's `name` is the OPTION's own
        wording, so scoping by `input[name=...]` would match the collision rather
        than resolve it.
    (b) THE FIX: each group's own option resolves to exactly the control inside
        THAT group's `data-field-path` entry, and refuses (None -> hand-off) an
        option resolving to 0 or 2+.
    (c) NO CROSS-ROLE BLEED: a page carrying a RADIO group and a CHECKBOX group
        that share an option wording ("Yes") resolves each to its OWN control.
        Before P2-5 only radios were ever located, so this could not arise; now
        both roles are located on the same page and the role filter in
        `get_by_role` is what keeps them apart. A driver that dropped the role
        (or guessed it from the schema type, which this vendor shipped three
        phantoms of) would tick the wrong control's group entirely."""
    html = (
        "<html><body><form>"
        + _ashby_checkbox_entry("path-x", "entryX", "Which content types?",
                                ["Documentaries", "Ads/socials"])
        + _ashby_checkbox_entry("path-y", "entryY", "Which have you dubbed?",
                                ["Documentaries", "Movies/TV shows"])
        + _ashby_checkbox_entry("path-dup", "entryD", "Duplicated wording?",
                                ["Same", "Same"])
        + _ashby_checkbox_entry("path-cb", "entryC", "Consent to contact?",
                                ["Yes", "No"])
        + _ashby_radio_entry("path-rd", "entryR", "Sponsorship required?",
                             ["Yes", "No"])
        + "</form></body></html>")
    page = _AshbyRadioDomPage(html)

    # sanity: the live checkbox shape parses to real checkboxes whose accessible
    # name is the option's own `<label>`, resolved by the independent resolver.
    assert page.get_by_role("checkbox", name="Ads/socials", exact=True).count() == 1
    # (a) THE BUG: a bare page-wide "Documentaries" is ambiguous across the two
    # groups that share the wording.
    assert page.get_by_role(
        "checkbox", name="Documentaries", exact=True).count() == 2

    # (b) THE FIX: each group's own "Documentaries" is the one inside its entry.
    x = ashby_fill._locate_option(page, "checkbox", "path-x", "Documentaries")
    y = ashby_fill._locate_option(page, "checkbox", "path-y", "Documentaries")
    assert x.count() == 1 and y.count() == 1
    assert x.nodes[0] is not y.nodes[0]
    x_group = page.locator('[data-field-path="path-x"] input').nodes
    y_group = page.locator('[data-field-path="path-y"] input').nodes
    assert any(x.nodes[0] is n for n in x_group)
    assert any(y.nodes[0] is n for n in y_group)
    assert not any(x.nodes[0] is n for n in y_group)   # never the sibling's

    # the hand-off residual, same as the radio half: 0 or 2+ is refused.
    assert ashby_fill._locate_option(
        page, "checkbox", "path-x", "Nonexistent") is None
    assert ashby_fill._locate_option(page, "checkbox", "path-dup", "Same") is None

    # (c) NO CROSS-ROLE BLEED. "Yes" exists once as a checkbox and once as a
    # radio; each role sees only its own, and each locate lands in its own entry.
    assert page.get_by_role("checkbox", name="Yes", exact=True).count() == 1
    assert page.get_by_role("radio", name="Yes", exact=True).count() == 1
    cb = ashby_fill._locate_option(page, "checkbox", "path-cb", "Yes")
    rd = ashby_fill._locate_option(page, "radio", "path-rd", "Yes")
    assert cb.count() == 1 and rd.count() == 1
    assert cb.nodes[0] is not rd.nodes[0]
    assert _attr_of(cb.nodes[0], "type") == "checkbox"
    assert _attr_of(rd.nodes[0], "type") == "radio"
    # and asking for a role the group does not wear finds nothing, so a
    # role-guessing driver hands off rather than driving a sibling's control.
    assert ashby_fill._locate_option(page, "radio", "path-cb", "Yes") is None
    assert ashby_fill._locate_option(page, "checkbox", "path-rd", "Yes") is None


def _attr_of(node, name):
    return node.attrs.get(name, "")


# == THE ROLE INVARIANT ========================================================
# A control is captured with the role the LIVE DOM gives it, never a role inferred
# from its schema type. Round 6 enforced that for ONE type (a radio-rendered
# ValueSelect) through a boolean radio probe gated on a type allowlist, and left
# every other role derived from the type -- which shipped THREE PHANTOM ROLES:
# roles that ZERO elements on any live Ashby page carry (`listbox` for a
# MultiValueSelect, `textbox` for a Number, and, before this wave, `textbox` for a
# Location). A phantom role is not cosmetic: `base._locate` builds
# `get_by_role(role, name=...)`, it resolves to nothing, the fill raises, and the
# field dies as a `fill-error:` -- a capture bug and a seal REJECT.
#
# The next tests pin the invariant ITSELF rather than its instances, against REAL
# captured DOM, so the NEXT phantom cannot ship either.


def test_ashby_capture_role_is_the_live_dom_role_never_the_schema_type():
    # THE INVARIANT, pinned at the probe. Every control shape Ashby is KNOWN to
    # render (live-probed over 30 postings, 2026-07-13) appears in the captured
    # DOM, and the probe reads back the role Chrome actually exposes for it. Three
    # of these rows are exactly where the schema type would have lied:
    #
    #   MultiValueSelect -> the kernel type map says "listbox"; live: a CHECKBOX
    #                       group, and page-wide listbox count is ZERO.
    #   Number           -> the kernel type map says "textbox"; live: an ARIA
    #                       "spinbutton" (an input[type=number] is not a textbox).
    #   ValueSelect      -> the kernel type map says "combobox"; live: radios, on
    #                       33 of the 34 ValueSelects on the board.
    page = _HtmlPage(_dom_with_siblings())

    roles = ashby_capture._rendered_roles(page)

    assert roles == {
        # from the seal posting (dom.html)
        "_systemfield_name": "textbox",                  # input[type=text]
        "_systemfield_email": "textbox",                 # input[type=email]
        "_systemfield_resume": "button",                 # input[type=file]
        "0a131ea7-7bc4-4ff5-85d3-c14985345cf9": "combobox",   # the geo widget
        _RADIO_PATH: "radio",                            # 8 native radios
        "85e5d818-d02f-46c8-8171-3d0227f7c42a": "textbox",
        "cbd5a932-c117-4d0d-83f4-f114fadab5b2": "textbox",
        "7c3fc0bb-c30f-412c-9e5c-df5f81de493e": "textbox",    # textarea
        "86f1c336-4068-4c35-817f-f6525d600266": "textbox",    # textarea
        # from the sibling postings (_SIBLING_DOM)
        _DROPDOWN_PATH: "combobox",                      # the SAME React widget
        _CHECKBOX_GROUP_PATH: "checkbox",                # NOT listbox
        _NUMBER_PATH: "spinbutton",                      # NOT textbox
        _BOOLEAN_PATH: "checkbox",
    }

    # The two CLICK HAZARDS are probed FIRST, so an entry holding any tickable
    # control fails SAFE (handed to a human) rather than being typed into.
    hazards = {role for role in roles.values()} & set(_CLICK_HAZARD_ROLES)
    assert hazards == {"radio", "checkbox"}


# The REAL graphql titles of the captured entries. They are not decoration: on a
# live Ashby page the graphql `title` IS the text of the entry's `<label>`, and
# the capture uses the title as the locator NAME -- so the title and the
# accessible name are the SAME string, and that identity is exactly what makes
# `get_by_role(role, name=title)` resolve. Inventing a title here would break that
# identity and quietly turn every located role into a phantom of the test's own
# making.
_NUMBER_TITLE = ("What is the largest amount you’ve scaled mobile app "
                 "advertising spend per month while maintaining profitability "
                 "or efficiency targets? Please answer with whole number. "
                 "(Ex. 1,000,000)")
_CHECKBOX_GROUP_TITLE = ("Which of the following types of content have you "
                         "produced dubs or audiovisual translations for in a "
                         "professional context?")
_BOOLEAN_TITLE = ("Have you personally optimized campaigns based on post-install "
                  "metrics (e.g., activation, retention, subscription revenue, "
                  "LTV), not just installs or CPI?")
_HEAR_TITLE = "How did you hear about ElevenLabs?\xa0"   # the trailing &nbsp; is live


def _every_shape_posting():
    """A synthetic graphql posting over EVERY control shape Ashby is known to
    render, on the REAL paths those controls occupy in the captured DOM, with the
    REAL titles their live labels carry. The SHAPE of the posting is ours (which
    fields, in which order); every path, title and DOM entry it names is
    live-captured, which is the only way to ask "does this role exist on the page"
    and get a truthful answer."""
    def entry(path, title, raw_type, options=()):
        field = {"path": path, "title": title, "type": raw_type}
        if options:
            field["selectableValues"] = [{"label": o, "value": o} for o in options]
        return {"isRequired": True, "field": field}

    return {"id": "9008", "applicationForm": {"sections": [{"title": "Apply",
        "fieldEntries": [
            entry("_systemfield_name", "Name", "String"),
            entry("_systemfield_resume", "Resume", "File"),
            entry("0a131ea7-7bc4-4ff5-85d3-c14985345cf9", "Location", "Location"),
            entry(_RADIO_PATH, _HEAR_TITLE, "ValueSelect",
                  ("Job board", "Referral")),
            entry(_DROPDOWN_PATH, _HEAR_TITLE, "ValueSelect",
                  ("Research", "Engineering")),
            entry(_CHECKBOX_GROUP_PATH, _CHECKBOX_GROUP_TITLE,
                  "MultiValueSelect", ("Movies", "Series")),
            entry(_NUMBER_PATH, _NUMBER_TITLE, "Number"),
            entry(_BOOLEAN_PATH, _BOOLEAN_TITLE, "Boolean"),
        ]}]}}


def _realistic_value(fld):
    """The value SHAPE `resolve_values` really produces for this field type.

    The routing predicates in `fill()` turn on the value's SHAPE as well as the
    role (`_control_kind`: a bool is a lone checkbox, a list is a checkbox
    GROUP; `_is_upload`: a Path), so a stub that handed every field a string
    mirrors fill()'s routing INCORRECTLY. That is exactly how the phantom sweep
    below stayed green about the wrong thing after the P2-5 adoption: with a
    string value, `_control_kind` returned None for the checkbox GROUP and the
    sweep kept counting it as handed off, months after it started being driven."""
    if fld.type == "input_file":
        return Path("cv.pdf")
    if fld.type == "boolean":
        return True
    if fld.type == "multi_value_multi_select":
        return list(fld.options[:1]) or ["x"]
    return "x"


def test_ashby_no_captured_role_is_a_phantom_on_the_real_dom():
    # THE PHANTOM SWEEP, as a test, over EVERY shape Ashby renders. For every
    # captured field: either the fill never builds a role+name locator for it (a
    # click hazard is handed off; a combobox is reached through its
    # data-field-path; an upload through the file input), or
    # `get_by_role(role, name)` MUST resolve to exactly ONE element on the same
    # DOM. Zero phantoms, by construction, and the NEXT phantom fails here.
    #
    # This test could not have existed before round 7: `_FakeAshbyPage` built a
    # control for whatever role it was handed, so a phantom always "resolved"
    # there. Here the DOM is real and answers for itself.
    page = _HtmlPage(_dom_with_siblings())
    seal = _HtmlPage(_real_dom())

    # CALIBRATION FIRST. The resolver below is only worth as much as its agreement
    # with the real browser, so it must reproduce the live page-wide census of the
    # seal posting (Playwright, 2026-07-13) before anything is concluded from it.
    # The listbox row is the phantom's own home, and it is EMPTY -- on this page
    # and on every Ashby page probed.
    assert len(_role_all(seal, "textbox")) == 6
    assert len(_role_all(seal, "radio")) == 8
    assert len(_role_all(seal, "combobox")) == 1
    assert len(_role_all(seal, "listbox")) == 0
    assert len(_role_all(seal, "spinbutton")) == 0

    raw = {"data": {"jobPosting": _every_shape_posting()}}
    cap_page = _FakePage(responses=_ashby_responses(raw),
                         html=_dom_with_siblings())
    fm = capture_ashby("fauxcorp", "9008", _factory_for(cap_page),
                       now=lambda: _PINNED)

    assert len(fm.fields) == 8
    located, handed_off, anchored, driven_per_option = [], [], [], []
    known_nameless = []
    for fld in fm.fields:
        fv = _FieldValueStub(fld, _realistic_value(fld))
        # MIRRORS fill()'s routing, in fill()'s ORDER: upload, then
        # `_control_kind`, then the consent/EEO exclusion, then the residual
        # hand-off. A field that reaches base._locate is one whose FIELD-level
        # role+name is aimed at the live page.
        if ashby_fill._is_upload(fv):
            anchored.append(fld.locator.role)
            continue
        kind = ashby_fill._control_kind(fv)
        if kind is not None and ashby_fill._group_options(fv, kind) is not None:
            # An option GROUP, EITHER kind, is DRIVEN per OPTION
            # (`_locate_option`), never by the field's role+name -- so its
            # field-level role+name is NOT aimed at the page and there is nothing
            # to phantom-check here (the option locators are pinned by the FX1
            # collision + drive tests). The radio moved out of `handed_off` at
            # W5.1c; the checkbox group moved out at P2-5.
            driven_per_option.append(fld.locator.role)
            continue
        if ashby_fill._is_excluded_group(fv):
            handed_off.append(fld.locator.role)
            continue
        if kind is None and ashby_fill._needs_human_handoff(fv):
            handed_off.append(fld.locator.role)
            continue
        if ashby_fill._is_ashby_combobox(fv):
            anchored.append(fld.locator.role)
            continue
        n = len(_role_matches(page, fld.locator.role, fld.locator.name))
        if fld.type == "boolean":
            # KNOWN GAP, PRE-EXISTING, NOT INTRODUCED BY P2-5 AND NOT FIXED BY IT.
            # The lone Boolean IS driven through `base._locate` (`_fill_control`),
            # so its field-level role+name IS aimed at the page -- and on the real
            # captured DOM it resolves to ZERO elements, which is this test's own
            # definition of a phantom.
            #
            # The markup says why (`_SIBLING_DOM`, the 0d9175ab entry): the live
            # control is a Yes/No BUTTON PAIR, and the `<input type="checkbox">`
            # behind it carries `tabindex="-1"`, NO `id` and NO `aria-label`,
            # while the entry's `<label for="0d9175ab-...">` points at the entry
            # PATH, which is not an element id. So the checkbox has no accessible
            # name to be found by.
            #
            # This is pinned as a FINDING rather than hidden, because the previous
            # version of this loop handed every field a STRING value, which made
            # `_control_kind` return None for the Boolean and routed it into
            # `handed_off` -- so the sweep never phantom-checked it at all. It is
            # asserted, not skipped, so a fix to the Boolean path turns this RED
            # and forces this comment to be revisited. Whether Playwright's own
            # name computation agrees with this test's independent ARIA resolver
            # is a LIVE question this fixture cannot settle.
            assert n == 0, ("the Boolean checkbox's accessible name now resolves; "
                            "the known-gap carve-out below must be retired")
            known_nameless.append(fld.locator.role)
            continue
        # everything else IS aimed at the page, so it must EXIST there.
        assert n == 1, (f"PHANTOM: get_by_role({fld.locator.role!r}, "
                        f"name={fld.locator.name!r}) resolved to {n} elements")
        located.append(fld.locator.role)

    # BOTH option groups are now DRIVEN per-option; nothing in this posting is
    # handed off any more (the residual hand-off needs a wrong-shaped value, and
    # the exclusion needs a consent/EEO label, neither of which this posting
    # carries); the two nameless comboboxes and the upload are path-anchored; the
    # lone Boolean is the known nameless gap above; and the only field-level roles
    # AIMED at the page are the ones the page carries.
    assert sorted(driven_per_option) == ["checkbox", "radio"]
    assert handed_off == []
    assert sorted(anchored) == ["button", "combobox", "combobox"]
    assert known_nameless == ["checkbox"]
    assert sorted(located) == ["spinbutton", "textbox"]


def _one_field_posting(path, title, raw_type, options=(), *, required=True):
    """The safely-fillable base form (name, email, resume) plus ONE field under
    test, on a REAL captured entry path so the live DOM decides its role."""
    def entry(p, t, rt, opts=()):
        field = {"path": p, "title": t, "type": rt}
        if opts:
            field["selectableValues"] = [{"label": o, "value": o} for o in opts]
        return {"isRequired": True, "field": field}

    subject = entry(path, title, raw_type, options)
    subject["isRequired"] = required
    return {"id": "9009", "applicationForm": {"sections": [{"title": "Apply",
        "fieldEntries": [
            entry("_systemfield_name", "Full name", "String"),
            entry("_systemfield_email", "Email", "Email"),
            entry("_systemfield_resume", "Resume", "File"),
            subject,
        ]}]}}


# The canned-answer slug of the REAL five-option question (`_missing_path_guess`
# normalizes the title). Seeding it is what makes the LIVE question answerable,
# so the drives below run on the real control AND the real wordings rather than
# on a friendlier stand-in.
_CONTENT_SLUG = ("which_of_the_following_types_of_content_have_you_produced_"
                 "dubs_or_audiovisual_translations_for_in_a_professional_"
                 "context")
_CONTENT_OPTIONS = ("Movies/TV shows/other dramatic content",
                    "Creator content (e.g. YouTube videos)",
                    "Ads/socials",
                    "E-learning or informational content",
                    "Documentaries")


def _content_group_fieldmap():
    """The REAL five-checkbox control carrying its REAL question and its REAL
    five option wordings (elevenlabs/1713bfd7, captured 2026-07-13; the entry
    `_SIBLING_DOM` holds verbatim)."""
    raw = {"data": {"jobPosting": _one_field_posting(
        _CHECKBOX_GROUP_PATH, _CHECKBOX_GROUP_TITLE, "MultiValueSelect",
        _CONTENT_OPTIONS)}}
    cap_page = _FakePage(responses=_ashby_responses(raw),
                         html=_dom_with_siblings())
    return capture_ashby("fauxcorp", "9009", _factory_for(cap_page),
                         now=lambda: _PINNED)


def test_ashby_multi_select_checkbox_group_is_driven_per_option_and_completes(tmp_path):
    """P2-5 checkbox-group adoption, end to end on the REAL five-checkbox control.
    This is the sanctioned rewrite the OLD test foresaw in its own words: it
    called the hand-off "a NAMED, temporary W5.1c click-debt" and "a subtractable
    debt instead of a capture bug", and this wave subtracts it. Exactly the
    precedent the radio adoption set at
    `test_ashby_radio_group_drives_the_resolved_option_and_completes` ("the
    sanctioned rewrite the old test foresaw ... that rewrite is the planned path,
    not a test-weakening").

    WHAT IS KEPT, because it was never about the hand-off: round 6 captured this
    REQUIRED field as `listbox`; `listbox` is not a click hazard, so the fill
    DROVE it, `base._locate` built `get_by_role("listbox", name=...)`, that
    resolved to ZERO elements on the live page, and the field died as a
    `fill-error:` -- which the seal bar calls "a CAPTURE BUG and a REJECT, never
    an exemption". The capture half below still pins role `checkbox`, and still
    pins that `listbox` exists NOWHERE on the page. Four of the six live
    MultiValueSelect fields are required, so this control's correctness is
    load-bearing either way.

    WHAT CHANGES: the control is no longer handed off. It is DRIVEN per option
    through the same `_locate_option` + `_fill_option_group` the radio group uses,
    and the run COMPLETES instead of carrying a debt. The honest-census half
    survives the rewrite intact and is pinned by the siblings below: a partial
    confirmation is still a GAP, an unlocatable option still hands the whole group
    off untouched, and a consent/EEO group still never reaches the driver."""
    # The real DOM entry, carrying the REAL question, captures as a checkbox group.
    raw = {"data": {"jobPosting": _one_field_posting(
        _CHECKBOX_GROUP_PATH, _CHECKBOX_GROUP_TITLE, "MultiValueSelect",
        ("Movies", "Series"))}}
    cap_page = _FakePage(responses=_ashby_responses(raw),
                         html=_dom_with_siblings())
    real = capture_ashby("fauxcorp", "9009", _factory_for(cap_page),
                         now=lambda: _PINNED)
    boxes = {f.key: f for f in real.fields}[_CHECKBOX_GROUP_PATH]
    assert boxes.locator.role == "checkbox"           # NOT the phantom "listbox"
    assert boxes.required is True
    # and "listbox" exists NOWHERE on the page, which is what made it a phantom.
    dom = _HtmlPage(_dom_with_siblings())
    assert _role_all(dom, "listbox") == []
    assert _role_matches(dom, "listbox", _CHECKBOX_GROUP_TITLE) == []

    # Now drive the fill. The live question is owner content the fake SSOT cannot
    # answer, and an unanswered field never reaches the driver -- so the SAME real
    # control carries an ANSWERABLE question here. That the SSOT DOES answer it is
    # the point: it proves the fill DRIVES the control, where it once handed it off.
    # THE QUESTION IS THE REAL ONE, and it must be a question the exclusion does
    # NOT catch, or this test would prove the exclusion rather than the drive. The
    # live content question is exactly that: `resolve._classify_checkbox` sorts it
    # into no consent class and it carries no demographic keyword.
    #
    # It deliberately is NOT `_VISA_LABEL`. A sponsorship ask classifies as the
    # consent class "assertion" (kernel `_classify_checkbox`), so a sponsorship
    # MULTI-SELECT is EXCLUDED from this driver by design -- pinned by
    # `test_ashby_sponsorship_multi_select_is_excluded_as_a_consent_assertion`.
    fieldmap = _content_group_fieldmap()
    assert {f.key: f for f in fieldmap.fields}[_CHECKBOX_GROUP_PATH]\
        .locator.role == "checkbox"
    values = _resolved_values(
        fieldmap, tmp_path=tmp_path,
        ssot=_fake_ssot(canned={_CONTENT_SLUG: ["Documentaries"]}))
    # a multi-select resolves to a LIST, and it is NOT a bool -- so the drive
    # below is earned by the ROLE plus the LIST shape, never by a bool shortcut.
    assert {fv.key: fv.value
            for fv in values.fields}[_CHECKBOX_GROUP_PATH] == ["Documentaries"]

    page = _FakeAshbyPage(fieldmap, sweep_required_labels=(
        "Full name", "Email", "Resume", _CHECKBOX_GROUP_TITLE))
    report = ashby.fill(page, fieldmap, values)

    # DRIVEN: exactly the resolved option was ticked, inside this group's entry,
    # and every option NOT requested stayed untouched (the combination is exact).
    by_name = {o.name: o for o in page.option_groups[_CHECKBOX_GROUP_PATH]}
    assert by_name["Documentaries"].is_checked() is True
    assert by_name["Documentaries"].checks == 1   # set ONCE, never toggled in
    for other in set(_CONTENT_OPTIONS) - {"Documentaries"}:
        assert by_name[other].is_checked() is False, other
        assert by_name[other].checks == 0, other
    # a `.check()` on the located option, never a `.click()` (Turnstile), and the
    # per-option locate went through `get_by_role("checkbox", name=<option>)`.
    assert ("role", "checkbox", "Documentaries") in page.requested
    assert ("role", "checkbox", "Ads/socials") not in page.requested
    # counts filled: NOT a required gap, not a skip, and the run completes. The
    # old test asserted the exact opposite of all four of these lines.
    assert _CHECKBOX_GROUP_PATH not in {g["key"] for g in report.required_unfilled}
    assert _CHECKBOX_GROUP_PATH not in {key for key, _ in report.skipped}
    assert not any("fill-error" in reason for _, reason in report.skipped)
    assert report.readback_mismatches == []
    assert report.complete is True
    assert report.caption().endswith("- COMPLETE")


def test_ashby_sponsorship_multi_select_is_excluded_as_a_consent_assertion(tmp_path):
    """A SPONSORSHIP question rendered as a checkbox GROUP is EXCLUDED from this
    driver, and the reason is worth pinning because it is a cross-change
    interaction rather than a rule this module wrote.

    `_is_excluded_group` imports the KERNEL's `_classify_checkbox` rather than
    restating it, so it tracks the kernel automatically. The kernel sorts a
    sponsorship ask into the consent class "assertion", whose defining property
    (the P2-2 polarity work) is that "require sponsorship" answers with the
    OPPOSITE polarity to "authorized to work" -- get it backwards and you assert
    the opposite of the truth on a legally significant question.

    For a `Boolean` the kernel has real machinery for that (a truthful-only
    disposition against the SSOT). For a MULTI-SELECT it has NONE:
    `_render_select` just matches SSOT values to option labels. So a sponsorship
    multi-select must NOT be driven, and this pins that it is not -- and that the
    exclusion tracks the kernel's classification rather than a local copy that
    could silently fall out of date."""
    raw = {"data": {"jobPosting": _one_field_posting(
        _CHECKBOX_GROUP_PATH, _VISA_LABEL, "MultiValueSelect", ("Yes", "No"))}}
    cap_page = _FakePage(responses=_ashby_responses(raw),
                         html=_dom_with_siblings())
    fieldmap = capture_ashby("fauxcorp", "9009", _factory_for(cap_page),
                             now=lambda: _PINNED)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    # the resolver DID answer it, so the exclusion is what stops the drive rather
    # than an absent answer. Without this the test could pass vacuously.
    assert {fv.key: fv.value for fv in values.fields}[_CHECKBOX_GROUP_PATH] == ["No"]

    page = _FakeAshbyPage(fieldmap, sweep_required_labels=(
        "Full name", "Email", "Resume", _VISA_LABEL))
    report = ashby.fill(page, fieldmap, values)

    gap = {g["key"]: g for g in report.required_unfilled}
    assert gap[_CHECKBOX_GROUP_PATH]["reason"] == \
        ashby_fill._SENSITIVE_GROUP_HANDOFF_REASON
    assert all(o.is_checked() is False and o.checks == 0
               for o in page.option_groups[_CHECKBOX_GROUP_PATH])
    assert not any(req[:2] == ("role", "checkbox") for req in page.requested)
    assert report.complete is False


def test_ashby_checkbox_group_with_a_wrong_shaped_value_still_hands_off(tmp_path):
    """THE RESIDUAL HAND-OFF, which the adoption narrowed but did not delete.

    Both option groups and the lone boolean now drive, so what remains on the
    hand-off path is a click-hazard control whose resolved value matches NO shape
    this module knows how to drive. Driving it would mean guessing what the value
    meant, so it goes to a human, and `_HUMAN_HANDOFF_REASON` is what says so.

    This test exists because the P2-5 rewrite of the old hand-off test removed the
    only end-to-end assertion on that constant, and a reason string no test
    reaches is a reason string that can rot.

    The wrong shape is produced by CORRUPTING a genuinely resolved value rather
    than by hand-building one: `_render_select` always returns a list for a
    multi-select, so this branch is defence in depth against a future resolve
    change, not a state the current resolve path can reach. Saying that plainly
    is better than inventing a fixture that pretends otherwise."""
    fieldmap = _content_group_fieldmap()
    values = _resolved_values(
        fieldmap, tmp_path=tmp_path,
        ssot=_fake_ssot(canned={_CONTENT_SLUG: ["Documentaries"]}))
    target = [fv for fv in values.fields if fv.key == _CHECKBOX_GROUP_PATH][0]
    target.value = "Documentaries"      # a bare string where a list belongs

    page = _FakeAshbyPage(fieldmap, sweep_required_labels=(
        "Full name", "Email", "Resume", _CHECKBOX_GROUP_TITLE))
    report = ashby.fill(page, fieldmap, values)

    gap = {g["key"]: g for g in report.required_unfilled}
    assert gap[_CHECKBOX_GROUP_PATH]["reason"] == ashby_fill._HUMAN_HANDOFF_REASON
    # a HAND-OFF, not a fill-error, and no control was touched or even located.
    assert not any("fill-error" in reason for _, reason in report.skipped)
    assert all(o.checks == 0
               for o in page.option_groups[_CHECKBOX_GROUP_PATH])
    assert not any(req[:2] == ("role", "checkbox") for req in page.requested)
    assert report.complete is False


def test_ashby_checkbox_group_ticks_only_the_requested_options_of_the_real_five(tmp_path):
    """THE MULTI-SELECT'S OWN RISK, end to end: the group must end in exactly the
    requested COMBINATION, not a superset.

    A radio is one-of-N and cannot be over-answered; a checkbox group can. Two of
    the five REAL options are requested here, through the real resolve path (a
    seeded canned answer for the real question's slug), so this exercises the
    live wordings -- slashes, parentheses, "e.g." -- that the per-option
    accessible-name match actually has to carry. The other three must be left
    untouched, and `.checks == 0` proves untouched rather than merely unticked."""
    fieldmap = _content_group_fieldmap()
    requested = ["Documentaries", "Ads/socials"]
    values = _resolved_values(
        fieldmap, tmp_path=tmp_path,
        ssot=_fake_ssot(canned={_CONTENT_SLUG: list(requested)}))
    # the resolver really did answer it, with BOTH options, matched to the real
    # option labels (`_match_option`), or the drive below would prove nothing.
    assert {fv.key: fv.value
            for fv in values.fields}[_CHECKBOX_GROUP_PATH] == requested

    page = _FakeAshbyPage(fieldmap, sweep_required_labels=(
        "Full name", "Email", "Resume", _CHECKBOX_GROUP_TITLE))
    report = ashby.fill(page, fieldmap, values)

    by_name = {o.name: o for o in page.option_groups[_CHECKBOX_GROUP_PATH]}
    for option in _CONTENT_OPTIONS:
        want = option in requested
        assert by_name[option].is_checked() is want, option
        assert by_name[option].checks == (1 if want else 0), option
        assert by_name[option].unchecks == 0, option
    assert _CHECKBOX_GROUP_PATH not in {g["key"] for g in report.required_unfilled}
    assert report.complete is True


def test_ashby_checkbox_group_partial_confirmation_is_a_gap_end_to_end(tmp_path):
    """PARTIAL CONFIRMATION IS A GAP, proven through `fill()` rather than only at
    the driver seam. Both requested options are located and driven, but one never
    reads back ticked (the silent no-op). The field must land in
    `required_unfilled` and the run must be NOT_COMPLETE, even though one option
    genuinely committed: a multi-select answered in part is a DIFFERENT answer,
    and reporting it filled would be the silent wrong answer this engine forbids.

    This is the convention that predates the adoption, and the test that proves
    the adoption did not quietly cost it."""
    fieldmap = _content_group_fieldmap()
    requested = ["Documentaries", "Ads/socials"]
    values = _resolved_values(
        fieldmap, tmp_path=tmp_path,
        ssot=_fake_ssot(canned={_CONTENT_SLUG: list(requested)}))

    page = _FakeAshbyPage(fieldmap, sweep_required_labels=(
        "Full name", "Email", "Resume", _CHECKBOX_GROUP_TITLE),
        bad_option_names=("Ads/socials",))
    report = ashby.fill(page, fieldmap, values)

    gap = {g["key"]: g for g in report.required_unfilled}
    assert _CHECKBOX_GROUP_PATH in gap          # named, not silently dropped
    assert gap[_CHECKBOX_GROUP_PATH]["reason"] == \
        ashby_fill._partial_group_reason(["Ads/socials"])
    # NOT a fill-error: the control was found and driven, it just did not confirm.
    assert not any("fill-error" in reason for _, reason in report.skipped)
    # the option that DID commit is left as it is, and reported as what landed.
    by_name = {o.name: o for o in page.option_groups[_CHECKBOX_GROUP_PATH]}
    assert by_name["Documentaries"].is_checked() is True
    mismatch = {m["key"]: m for m in report.readback_mismatches}
    assert mismatch[_CHECKBOX_GROUP_PATH]["actual"] == ["Documentaries"]
    assert report.complete is False


def test_ashby_consent_and_eeo_checkbox_groups_never_reach_the_driver(tmp_path):
    """THE EXCLUSION, end to end, on the same REAL five-checkbox control.

    Making checkbox groups drivable made consent-class and EEO/demographic
    checkbox groups drivable too, and the kernel's consent machinery does not
    reach them (`resolve._resolve_boolean` dispositions a consent checkbox through
    the seeded `policies.consent.<class>` policy, but only for the `Boolean`
    type). So the same control, carrying a CONSENT question instead of a content
    question, must be handed off by `_is_excluded_group` and must never have a
    per-option locator built for it at all.

    Asserting on `page.requested` is the load-bearing half: it proves the
    exclusion fires BEFORE any option is located, so no consent box is ever
    touched even momentarily."""
    consent_label = "I agree to the privacy policy and consent to data processing"
    raw = {"data": {"jobPosting": _one_field_posting(
        _CHECKBOX_GROUP_PATH, consent_label, "MultiValueSelect",
        ("Yes", "No"))}}
    cap_page = _FakePage(responses=_ashby_responses(raw),
                         html=_dom_with_siblings())
    fieldmap = capture_ashby("fauxcorp", "9009", _factory_for(cap_page),
                             now=lambda: _PINNED)
    slug = "i_agree_to_the_privacy_policy_and_consent_to_data_processing"
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(canned={slug: ["Yes"]}))
    # the resolver DID produce a value, so the exclusion is what stops the drive,
    # not an absent answer. Without this the test would pass vacuously.
    assert {fv.key: fv.value for fv in values.fields}[_CHECKBOX_GROUP_PATH] == ["Yes"]

    page = _FakeAshbyPage(fieldmap, sweep_required_labels=(
        "Full name", "Email", "Resume", consent_label))
    report = ashby.fill(page, fieldmap, values)

    gap = {g["key"]: g for g in report.required_unfilled}
    assert _CHECKBOX_GROUP_PATH in gap
    assert gap[_CHECKBOX_GROUP_PATH]["reason"] == \
        ashby_fill._SENSITIVE_GROUP_HANDOFF_REASON
    # NOTHING was ticked, and no option locator was ever built.
    assert all(o.is_checked() is False and o.checks == 0
               for o in page.option_groups[_CHECKBOX_GROUP_PATH])
    assert not any(req[:2] == ("role", "checkbox") for req in page.requested)
    assert report.complete is False


def test_ashby_number_is_captured_as_a_spinbutton_and_fills(tmp_path):
    # MAJOR 3, end to end, on the REAL input[type=number]. This one stings the most:
    # the control HAS an accessible name (a proper `<label for=id>`), so the engine
    # could fill it perfectly -- and round 6 killed it on a role-naming bug alone.
    # `input[type=number]` exposes ARIA role "spinbutton", never "textbox", so
    # `get_by_role("textbox", name=...)` resolved to ZERO elements while
    # `get_by_role("spinbutton", name=...)` resolved to exactly ONE (live-probed
    # 2026-07-13 on elevenlabs/09d54604, where the field is REQUIRED).
    raw = {"data": {"jobPosting": _one_field_posting(
        _NUMBER_PATH, _NUMBER_TITLE, "Number")}}
    cap_page = _FakePage(responses=_ashby_responses(raw),
                         html=_dom_with_siblings())
    fieldmap = capture_ashby("fauxcorp", "9010", _factory_for(cap_page),
                             now=lambda: _PINNED)
    number = {f.key: f for f in fieldmap.fields}[_NUMBER_PATH]

    assert number.type == "input_text"             # the schema type is untouched
    assert number.locator.role == "spinbutton"     # NOT the phantom "textbox"

    # and the role RESOLVES on the real DOM, one-to-one, while "textbox" does not.
    dom = _HtmlPage(_dom_with_siblings())
    assert len(_role_matches(dom, "spinbutton", number.locator.name)) == 1
    assert len(_role_matches(dom, "textbox", number.locator.name)) == 0

    # BOTH PATHS, because they are different code. `Number` canonicalizes to
    # `input_text`, which the kernel's generic map roles as "textbox" -- so an
    # UNMOUNTED Number (a field on a step the page has not reached, where the DOM
    # probe cannot answer) would be captured with the phantom role unless the
    # vendor-local fallback map corrects it. It does, and this pins it: reverting
    # `_ASHBY_FALLBACK_ROLE["Number"]` alone survived the rest of the suite.
    unmounted = capture_ashby(
        "fauxcorp", "9010",
        _factory_for(_FakePage(responses=_ashby_responses(raw))),
        now=lambda: _PINNED)
    assert {f.key: f for f in unmounted.fields}[_NUMBER_PATH]\
        .locator.role == "spinbutton"

    # It is not a click hazard, so the fill DRIVES it -- and now it can. The live
    # ad-spend question is owner content the fake SSOT cannot answer (and an
    # unanswered field never reaches the driver), so the SAME real spinbutton
    # entry carries an ANSWERABLE Number question here. Round 6 aimed
    # `get_by_role("textbox", ...)` at this control; the fake page now MISSES on a
    # role the modelled DOM does not carry, so the old role dies here loudly
    # instead of being manufactured a control.
    raw = {"data": {"jobPosting": _one_field_posting(
        _NUMBER_PATH, _SALARY_LABEL, "Number")}}
    cap_page = _FakePage(responses=_ashby_responses(raw),
                         html=_dom_with_siblings())
    fieldmap = capture_ashby("fauxcorp", "9010", _factory_for(cap_page),
                             now=lambda: _PINNED)
    assert {f.key: f for f in fieldmap.fields}[_NUMBER_PATH]\
        .locator.role == "spinbutton"
    values = _resolved_values(
        fieldmap, tmp_path=tmp_path,
        ssot=_fake_ssot(canned={"salary_expectation": "500000"}))

    page = _FakeAshbyPage(fieldmap, sweep_required_labels=(
        "Full name", "Email", "Resume", _SALARY_LABEL))
    report = ashby.fill(page, fieldmap, values)

    assert page.controls[("spinbutton", _SALARY_LABEL)].input_value() == "500000"
    assert not any(g["key"] == _NUMBER_PATH for g in report.required_unfilled)
    assert report.complete is True


def test_ashby_dropdown_select_is_reached_by_path_and_commits_by_option_click(
        tmp_path):
    # MAJOR 2, end to end, on the REAL dropdown ValueSelect (the ONE on the whole
    # board; the other 33 are radio groups). TWO defects, one control:
    #
    # (a) UNREACHABLE. The control has NO id, NO name and NO aria-label, and its
    #     `<label for=...>` points at the ENTRY div rather than the input, so it has
    #     NO ACCESSIBLE NAME. `base._locate` builds `get_by_role("combobox",
    #     name=<title>)`, which resolves to ZERO elements -- proven below against
    #     the captured DOM, and live on 2026-07-13. It is the SAME defect the wave
    #     already diagnosed and fixed for the geo control, on the very next React
    #     widget on the same page.
    #
    # (b) WORSE THAN UNREACHABLE, if "reached" naively. Path-anchoring the control
    #     and keeping the controlled-component value-set (the obvious fix) is a
    #     FALSE COMPLETE: live, the native setter writes the text into the widget's
    #     FILTER box, React accepts it as a search query, `base._readback` reads
    #     that same text back, and the field counts FILLED -- while the listbox is
    #     still open and NOTHING is selected. A required question would be submitted
    #     unanswered and reported complete. The fake combobox RAISES on `evaluate`,
    #     so that regression fails loudly right here.
    #
    # The fix is the driver the geo control already uses: anchor on
    # `data-field-path`, type a filter, and COMMIT by clicking the option row --
    # then confirm with BOTH signals (the value reads back AND the listbox has
    # collapsed), which is exactly what tells a commit from a filter.
    raw = {"data": {"jobPosting": _one_field_posting(
        _DROPDOWN_PATH, _HEAR_TITLE, "ValueSelect",
        ("Job board", "Referral", "Social media"))}}
    cap_page = _FakePage(responses=_ashby_responses(raw),
                         html=_dom_with_siblings())
    real = capture_ashby("fauxcorp", "9012", _factory_for(cap_page),
                         now=lambda: _PINNED)
    dropdown = {f.key: f for f in real.fields}[_DROPDOWN_PATH]
    assert dropdown.locator.role == "combobox"
    assert dropdown.type == "multi_value_single_select"

    # (a) the control CANNOT be reached by role+name, with its REAL live title ...
    dom = _HtmlPage(_dom_with_siblings())
    assert _role_matches(dom, "combobox", dropdown.locator.name) == []
    # ... and no title COULD reach it, because it has no accessible name at all:
    # the combobox is right there, and it is anonymous.
    entry = [n for n in dom.locator("[data-field-path]").all()
             if n.get_attribute("data-field-path") == _DROPDOWN_PATH][0]
    control = entry.locator('input[role="combobox"]').all()[0]
    assert control.get_attribute("id") is None
    assert control.get_attribute("name") is None
    assert control.get_attribute("aria-label") is None
    # its <label> points at the ENTRY, not at the input, which is the whole bug.
    label = entry.locator("label").all()[0]
    assert label.get_attribute("for") == _DROPDOWN_PATH

    # (b) so the fill never aims a locator at it: it goes through the path-anchored
    # driver. The live question is owner content the fake SSOT cannot answer (and
    # an unanswered field never reaches the driver), so the SAME real combobox
    # carries an ANSWERABLE question here.
    raw = {"data": {"jobPosting": _one_field_posting(
        _DROPDOWN_PATH, _VISA_LABEL, "ValueSelect", ("Yes", "No"))}}
    cap_page = _FakePage(responses=_ashby_responses(raw),
                         html=_dom_with_siblings())
    fieldmap = capture_ashby("fauxcorp", "9012", _factory_for(cap_page),
                             now=lambda: _PINNED)
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeAshbyPage(fieldmap, sweep_required_labels=(
        "Full name", "Email", "Resume", _VISA_LABEL))
    report = ashby.fill(page, fieldmap, values)

    widget = page.geo[_DROPDOWN_PATH]
    assert not any(_VISA_LABEL in str(req) for req in page.requested)
    # committed by an OPTION CLICK, with the listbox collapsed behind it. The fake
    # combobox RAISES on `evaluate`, so a regression to the controlled-component
    # value-set (the false COMPLETE) fails loudly right here.
    assert widget.clicked_option == "No"
    assert widget.input_value() == "No"
    assert widget.expanded is False
    assert not any(g["key"] == _DROPDOWN_PATH for g in report.required_unfilled)
    assert report.complete is True


def test_ashby_boolean_is_captured_as_a_checkbox_by_the_dom_and_by_the_fallback():
    # MINOR 1. The Boolean's checkbox role was UNPINNED: breaking it to "textbox"
    # survived the whole round-6 suite. Live harm was masked by the OTHER arm of
    # `_needs_human_handoff` (`isinstance(fv.value, bool)` fires before the role is
    # consulted), so a Boolean resolving to a Python bool was still handed off.
    # That is a SECOND LINE OF DEFENCE, not a pin: a Boolean whose resolved value
    # is a STRING would be driven into a control that is not a textbox at all.
    #
    # Pinned here on BOTH paths, because they are different code: the DOM probe
    # (mounted; 4 live Boolean fields, every one an `input[type=checkbox]`) and the
    # FALLBACK map (unmounted). The value used for the hand-off check is
    # deliberately a STRING, so it is the ROLE under test and not the bool shortcut.
    raw = {"data": {"jobPosting": _one_field_posting(
        _BOOLEAN_PATH, _BOOLEAN_TITLE, "Boolean")}}

    mounted = capture_ashby(
        "fauxcorp", "9011",
        _factory_for(_FakePage(responses=_ashby_responses(raw),
                               html=_dom_with_siblings())), now=lambda: _PINNED)
    unmounted = capture_ashby(
        "fauxcorp", "9011",
        _factory_for(_FakePage(responses=_ashby_responses(raw))),
        now=lambda: _PINNED)

    for fieldmap, how in ((mounted, "DOM probe"), (unmounted, "fallback")):
        boolean = {f.key: f for f in fieldmap.fields}[_BOOLEAN_PATH]
        assert boolean.type == "boolean"
        assert boolean.locator.role == "checkbox", how
        assert boolean.locator.role in _CLICK_HAZARD_ROLES
        # the ROLE ALONE hands it off, with a non-bool value: no bool shortcut.
        assert ashby_fill._needs_human_handoff(_FieldValueStub(boolean, "Yes"))


def _multi_select_posting():
    """A MultiValueSelect on the path of the REAL checkbox group Ashby renders for
    that type (`_CHECKBOX_GROUP_PATH`, five native checkboxes, from a live
    posting), and the SAME type on the path of the REAL radio group
    (`_RADIO_PATH`, eight native radios), so both poles of the DOM decision are
    live controls."""
    return {"id": "9006", "applicationForm": {"sections": [{"title": "Apply",
        "fieldEntries": [
            {"isRequired": True, "field": {
                "path": _CHECKBOX_GROUP_PATH,
                "title": "Which teams interest you?", "type": "MultiValueSelect",
                "selectableValues": [{"label": "Research", "value": "r"},
                                     {"label": "Engineering", "value": "e"}]}},
            {"isRequired": True, "field": {
                "path": _RADIO_PATH,
                "title": "Which teams interest you most?",
                "type": "MultiValueSelect",
                "selectableValues": [{"label": "Research", "value": "r"},
                                     {"label": "Engineering", "value": "e"}]}},
        ]}]}}


def test_ashby_multi_select_is_captured_as_its_live_checkbox_group_not_a_listbox():
    # CORRECTED (round 7), and this is the test that BLOCKED the fix. It was called
    # `test_ashby_multi_select_in_a_radio_bearing_entry_stays_listbox` and it
    # asserted `role == "listbox"` -- while its own docstring stated the true DOM
    # fact, that "a multi_value_multi_select renders as CHECKBOXES". It pinned a
    # role that ZERO elements on ANY Ashby page carry, so the correct one-line fix
    # turned the suite RED and a maintainer applying it would have reverted.
    #
    # Changing that assertion is a CORRECTION to match the live DOM, not a
    # weakening: `listbox` built `get_by_role("listbox", name=...)`, which resolves
    # to nothing and kills a REQUIRED field as a fill-error (4 of the 6 live
    # MultiValueSelect fields are required). `checkbox` is what the control IS, and
    # it is a CLICK HAZARD, so the field is handed off -- a named, temporary W5.1c
    # click-debt instead of a capture bug.
    #
    # What the old test was RIGHT about, and what is kept: the schema type alone
    # must not decide, and a multi-select must not be silently turned into a radio.
    # It is kept by asking the DOM instead of a type allowlist. Both entries below
    # carry the SAME graphql type and land on DIFFERENT roles, because their
    # CONTROLS differ -- which is the invariant, demonstrated.
    raw = {"data": {"jobPosting": _multi_select_posting()}}
    page = _FakePage(responses=_ashby_responses(raw), html=_dom_with_siblings())
    fm = capture_ashby("fauxcorp", "9006", _factory_for(page), now=lambda: _PINNED)
    by_key = {f.key: f for f in fm.fields}

    # the REAL checkbox-rendered multi-select: role checkbox, NOT the phantom.
    boxes = by_key[_CHECKBOX_GROUP_PATH]
    assert boxes.type == "multi_value_multi_select"   # the type is untouched
    assert boxes.locator.role == "checkbox"           # NOT "listbox"
    assert boxes.options == ["Research", "Engineering"]
    # and the DOM really does back that: five checkboxes, zero listbox nodes.
    dom = _HtmlPage(_dom_with_siblings())
    entry = [n for n in dom.locator("[data-field-path]").all()
             if n.get_attribute("data-field-path") == _CHECKBOX_GROUP_PATH][0]
    assert len(entry.locator('input[type="checkbox"]').all()) == 5
    assert _role_all(dom, "listbox") == []            # NOWHERE on the page

    # the SAME graphql type, on the entry that renders RADIOS, is a radio: the
    # control decides, not the type. Both are click hazards, so both are handed
    # off, and NEITHER is ever aimed at a locator.
    assert by_key[_RADIO_PATH].locator.role == "radio"
    for fld in (boxes, by_key[_RADIO_PATH]):
        assert ashby_fill._needs_human_handoff(_FieldValueStub(fld))


def test_ashby_radio_required_signal_is_the_css_module_class():
    # The radio group is REQUIRED on the live form, and Ashby's `_required_<hash>`
    # CSS-module class on its <label> is the ONLY signal that says so: the entry
    # carries no native `required`/`aria-required` anywhere, not on the group and
    # not on any of its eight radios, because React widgets hold their state
    # outside the DOM. `_entry_required` reads that class, and nothing pinned that
    # it does: strip the class from dom.html and the suite stayed green.
    #
    # The drift is FAIL-CLOSED (the entry goes required=False, the schema still
    # calls it required, and the reconciliation manufactures a PHANTOM `dom-sweep:`
    # gap while the radio still blocks), so no required control can be silenced by
    # it. It earns one assertion rather than an alarm: an Ashby CSS-module hash
    # rotation would turn the seal red with a confusing phantom gap instead of a
    # clear error, and the tempting "fix" for a phantom gap is to weaken the census
    # instead of the selector.
    page = _HtmlPage(_real_dom())
    entries = {entry.path: entry for entry in ashby_fill._ashby_entries(page)}
    radio_node = [node for node in page.locator("[data-field-path]").all()
                  if node.get_attribute("data-field-path") == _RADIO_PATH][0]

    # no native required signal anywhere inside the entry ...
    assert ashby_fill._locate_all(radio_node, base._REQUIRED_CSS) == []
    # ... yet the entry reads Ashby's marker class and reports itself required.
    assert entries[_RADIO_PATH].required is True


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


def _radio_posting_legacy():
    """`_radio_posting`, re-expressed in the PRE-MIGRATION
    `applicationFormDefinition` shape: `sections[].fields[]`, `isRequired` /
    `isHidden` on the nested `field` itself, and NO `applicationForm` key at
    all, so `_parse_ashby` takes the fallback parser. Both paths are MOUNTED in
    dom.html and only `_RADIO_PATH`'s entry holds native radios, exactly as in
    the modern-shape test."""
    return {"id": "9007", "applicationFormDefinition": {"sections": [{
        "title": "Apply", "fields": [
            {"field": {
                "path": _RADIO_PATH, "title": "How did you hear about us?",
                "type": "ValueSelect", "isRequired": True, "isHidden": False,
                "selectableValues": [{"label": "Job board", "value": "jb"},
                                     {"label": "Referral", "value": "ref"},
                                     {"label": "Social media", "value": "soc"}]}},
            {"field": {
                "path": _DROPDOWN_PATH, "title": "Team preference",
                "type": "ValueSelect", "isRequired": True, "isHidden": False,
                "selectableValues": [{"label": "Research", "value": "r"},
                                     {"label": "Engineering", "value": "e"}]}},
        ]}]}}


def test_ashby_legacy_form_definition_threads_the_dom_role_probe():
    # The LEGACY parser threads `dom_roles` too, and nothing pinned that: replacing
    # its threading with the fallback survived the whole suite, because no test
    # drove the legacy shape against a real DOM. The role invariant is a property
    # of the live PAGE, not of which graphql shape that page was served.
    #
    # Live-null on the sealed posting, which is served `applicationForm` and so
    # takes the modern path. The fallback parser exists for postings still on the
    # pre-migration shape, and on one of THOSE the radio group would capture
    # role=combobox, a driver would aim at a control that does not exist, and the
    # fill would time out: precisely the bug this wave exists to fix, reached
    # through the other parser.
    #
    # BOTH POLES ARE MOUNTED, which is what makes this pin the PROBE and not the
    # fallback: the radio entry and the REAL dropdown entry are in the same DOM,
    # they carry the SAME graphql type, and they land on different roles. If the
    # dropdown were unmounted, its combobox role would come from the fallback map
    # and this test would pass while proving nothing about the DOM.
    raw = {"data": {"jobPosting": _radio_posting_legacy()}}
    page = _FakePage(responses=_ashby_responses(raw), html=_dom_with_siblings())
    fm = capture_ashby("fauxcorp", "9007", _factory_for(page), now=lambda: _PINNED)
    by_key = {f.key: f for f in fm.fields}

    radio = by_key[_RADIO_PATH]
    assert radio.locator.role == "radio"                 # NOT combobox
    assert radio.label == "How did you hear about us?"    # the QUESTION
    assert radio.options == ["Job board", "Referral", "Social media"]  # LABELS
    assert radio.type == "multi_value_single_select"     # still a single-select
    assert radio.required is True
    assert radio.step_index == 0     # the legacy shape carries no step concept

    dropdown = by_key[_DROPDOWN_PATH]
    assert dropdown.locator.role == "combobox"           # NOT radio
    assert dropdown.options == ["Research", "Engineering"]
    # and the probe really is what decided it, on this very DOM.
    assert ashby_capture._rendered_roles(_HtmlPage(_dom_with_siblings()))[
        _DROPDOWN_PATH] == "combobox"


# =============================================================================
# ROUND 9: the guards the code's own docstrings call load-bearing, PINNED
# =============================================================================
# Every test below defends a rule the shipped code ALREADY follows correctly. Each
# one was written against a mutant that deleted its guard and passed the whole
# suite: an unpinned guard is one refactor away from being gone, and each of these
# four, once gone, sends a real answer to a real employer that the candidate never
# gave. Nothing here changes the driver's behaviour; it makes the suite able to
# NOTICE if someone changes it.

_LOCATION_PATH = "0a131ea7-7bc4-4ff5-85d3-c14985345cf9"   # the geo entry in dom.html

# A SECOND mount of that same entry path, carrying a second live combobox. It is
# CONSTRUCTED (not an observed Ashby render), and it is deliberately the SAME shape
# the real entry has -- an entry div with the path and a nameless React combobox
# inside it -- because the question it asks is about the SELECTOR, not the vendor:
# what does `[data-field-path="..."] input[role="combobox"]` resolve to when the
# page mounts that path twice, and what does the resolver do about it.
_DUPLICATE_GEO_ENTRY = (
    '<div class="_fieldEntry_1e3gg_28 ashby-application-form-field-entry" '
    f'data-field-path="{_LOCATION_PATH}">'
    '<label class="_heading_f7cvd_52 _required_f7cvd_91 _label_1e3gg_42 '
    'ashby-application-form-question-title" '
    f'for="{_LOCATION_PATH}">Location</label>'
    '<div class="_inputContainer_d7ago_28"><input class="_input_d7ago_28" '
    'placeholder="Start typing..." aria-autocomplete="list" '
    'aria-expanded="false" aria-haspopup="listbox" role="combobox" value="">'
    '</div></div>')

# The AMBIGUOUS row set, for the 2+-hits branch of the unique-match gate. Both rows
# are token-subsets of the value and they are DIFFERENT places, so neither is
# uniquely the candidate's answer and the driver must refuse both. FAKE places, in
# the fixture's own vocabulary; the LIVE shape of this trap (recorded by the round-8
# live probe of the real geo endpoint) is the value "Kinshasa, Democratic Republic
# of the Congo" being offered BOTH "Democratic Republic of the Congo" AND "Republic
# of the Congo" -- two different countries, both token-consistent with the value.
_AMBIGUOUS_VALUE = "Springfield, Lombardia, Fakeland"
_AMBIGUOUS_ROWS = ("Fakeland", "Lombardia, Fakeland")
# A value whose committing suggestion is NOT the text that was typed to find it
# (filter "Milan" -> row "Milan, Fakeland"), so the readback signal is a REAL
# signal here and not the filter reading itself back.
_COLLAPSE_VALUE = "Milan, Lombardia, Fakeland"
_COLLAPSE_ROW = "Milan, Fakeland"
_COLLAPSE_FILTER = "Milan"


def _combobox_field(key: str, label: str) -> Field:
    """A capture-shaped combobox field on a REAL captured entry path, so the DOM
    decides whether its control exists and how many of them the anchor reaches."""
    return Field(key=key, label=label, type="location", required=True, options=[],
                 source=ASHBY_SOURCE, locator=Locator(role="combobox", name=label),
                 step_index=0)


def test_ashby_geo_control_is_scoped_to_the_fields_own_entry():
    # MAJOR 1. `_geo_control`'s docstring: "Scoped to the field's OWN entry, so the
    # driver can never type into another field's control." Nothing pinned it, and
    # dropping the scope (`[data-field-path="{path}"] input[role="combobox"]` ->
    # `input[role="combobox"]`) passed the full suite -- because every fixture in it
    # carried exactly ONE combobox, so page-wide and entry-scoped could not differ.
    #
    # The live page CAN carry two: elevenlabs/1713bfd7 (Dubbing Specialist) renders
    # the geo Location control AND the one dropdown-rendered ValueSelect on the whole
    # board. That is this DOM: both entries are live-captured, verbatim. Unscoped,
    # BOTH fields resolve to found[0] -- the SAME control -- and the ValueSelect's
    # answer is typed into the Location box.
    page = _HtmlPage(_dom_with_siblings())
    assert len(_css_select(page._root, 'input[role="combobox"]')) == 2

    location = ashby_fill._geo_control(
        page, _FieldValueStub(_combobox_field(_LOCATION_PATH, _GEO_LABEL)))
    dropdown = ashby_fill._geo_control(
        page, _FieldValueStub(_combobox_field(_DROPDOWN_PATH, _HEAR_TITLE)))

    assert location is not None and dropdown is not None
    assert location is not dropdown        # THE guard: two fields, two controls
    # and each one is the control that lives INSIDE its own field entry.
    for path, control in ((_LOCATION_PATH, location), (_DROPDOWN_PATH, dropdown)):
        entries = _css_select(page._root, f'[data-field-path="{path}"]')
        assert len(entries) == 1
        own = _css_select(entries[0], 'input[role="combobox"]')
        assert len(own) == 1 and own[0] is control


def test_ashby_geo_control_refuses_a_path_that_reaches_more_than_one_control():
    # ROUND-11 MAJOR 1, at the resolver. `_geo_control` used to take found[0]: the
    # FIRST of however many controls the entry-anchor reached. The rest of this
    # module refuses to guess where a wrong answer is indistinguishable from a
    # right one (`_unique_suggestion`: "zero hits or 2+ hits leaves the field
    # HONESTLY UNFILLED"), and the resolution step is not exempt -- a first-of-N
    # pick decides WHICH LIVE CONTROL receives the owner's data on DOM order alone.
    #
    # BOTH directions are pinned here, on the real captured DOM:
    #
    #  (a) exactly ONE  -> resolves, and it is the control inside the field's entry.
    #  (b) two or more  -> None (refuse). The ambiguous DOM is CONSTRUCTED, not an
    #      observed Ashby render (same convention as capture.py's constructed
    #      relocation examples): it mounts the Location entry TWICE, which is what a
    #      re-render mid-fill, a duplicated responsive copy, or a second widget in
    #      one entry looks like to a selector. The module cannot rule those out and
    #      does not have to: it refuses instead of choosing.
    geo = _combobox_field(_LOCATION_PATH, _GEO_LABEL)

    single = _HtmlPage(_dom_with_siblings())
    assert len(_css_select(single._root, f'[data-field-path="{_LOCATION_PATH}"]')) == 1
    resolved = ashby_fill._geo_control(single, _FieldValueStub(geo))
    assert resolved is not None                       # (a) exactly one -> resolves
    entry = _css_select(single._root, f'[data-field-path="{_LOCATION_PATH}"]')[0]
    assert _css_select(entry, 'input[role="combobox"]') == [resolved]

    ambiguous = _HtmlPage(_dom_with_siblings() + _DUPLICATE_GEO_ENTRY)
    controls = _css_select(ambiguous._root,
                           f'[data-field-path="{_LOCATION_PATH}"] input[role="combobox"]')
    assert len(controls) == 2                         # the premise: the anchor is ambiguous
    # (b) two -> REFUSE. found[0] would have returned `controls[0]` here.
    assert ashby_fill._geo_control(ambiguous, _FieldValueStub(geo)) is None


def test_ashby_ambiguous_geo_path_books_the_gap_and_types_into_neither_control(tmp_path):
    # ROUND-11 MAJOR 1, driven end to end: the refusal must BOOK the gap the module's
    # conventions book (required_unfilled -> NOT_COMPLETE), and it must leave BOTH
    # candidate controls untouched -- not typed into, not committed, not cleared.
    # A first-of-N resolver types the owner's location into whichever widget happens
    # to come first in DOM order and reports the field COMPLETE.
    fieldmap = _fieldmap_with_geo()
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    page = _FakeAshbyPage(
        fieldmap,
        sweep_required_labels=_SAFE_REQUIRED_LABELS + (_GEO_LABEL,),
        geo_suggestions=(_GEO_DECOY, _GEO_MATCH),
        geo_duplicate_keys=(_GEO_KEY,))

    report = ashby.fill(page, fieldmap, values)

    first, second = page.geo[_GEO_KEY], page.geo_dupes[_GEO_KEY]
    assert first is not second                    # two real controls, one path
    for control in (first, second):
        assert control.offered == []              # never typed into...
        assert control.clicked_option is None     # ...never committed
        assert control.input_value() == ""
    gap = {g["key"]: g for g in report.required_unfilled}
    assert _GEO_KEY in gap
    assert "did not resolve to exactly ONE control" in gap[_GEO_KEY]["reason"]
    assert report.complete is False
    # the OTHER combobox on the same page is unaffected: one ambiguous field is not
    # a reason to abandon a field whose own anchor is unambiguous.
    assert page.geo[_VISA_KEY].clicked_option == "No"


def test_ashby_two_comboboxes_each_field_drives_its_own_control(tmp_path):
    # MAJOR 1, at the FILL level: the same loss of scope, driven end to end. This
    # form carries two comboboxes (the ValueSelect and the geo Location), each with
    # its own widget and its own answer. Unscoped, the second field re-drives the
    # FIRST field's control: it types the location filter into the ValueSelect box,
    # finds no match there, and ABANDONS -- clearing the answer the ValueSelect had
    # already committed. One wrong answer and one destroyed answer, reported COMPLETE.
    fieldmap = _fieldmap_with_geo()
    # THREE combobox controls on this page, and the third is the reason the scope is
    # not merely tidy: the EEO demographic field wears the same nameless widget, and
    # an unscoped anchor can land a driven answer in it.
    assert {_VISA_KEY, _GEO_KEY, _GENDER_KEY} <= {
        f.key for f in fieldmap.fields if f.locator.role == "combobox"}
    values = _resolved_values(fieldmap, tmp_path=tmp_path)

    page = _FakeAshbyPage(
        fieldmap,
        sweep_required_labels=_SAFE_REQUIRED_LABELS + (_GEO_LABEL,),
        geo_suggestions=(_GEO_DECOY, _GEO_MATCH))
    report = ashby.fill(page, fieldmap, values)

    geo, visa, eeo = (page.geo[_GEO_KEY], page.geo[_VISA_KEY],
                      page.geo[_GENDER_KEY])
    assert geo.clicked_option == _GEO_MATCH        # each field committed...
    assert geo.input_value() == _GEO_MATCH
    assert visa.clicked_option == "No"             # ...its OWN answer, in its OWN box
    assert visa.input_value() == "No"
    # and neither control was ever shown the other field's answer...
    assert all(_GEO_MATCH not in rows for rows in visa.offered)
    assert all("No" not in rows for rows in geo.offered)
    # ...and the EEO control was never typed into at all.
    assert eeo.clicked_option is None
    assert eeo.input_value() == ""
    assert report.complete is True


# -- round-13 MAJOR 1: the suggestion list is scoped to the driven control's OWN
# listbox ---------------------------------------------------------------------
# What the OTHER widget was left filtering on when the geo field is driven.
_FOREIGN_FILTER = "Fake"


def test_ashby_geo_suggestions_never_reach_another_widgets_open_listbox(tmp_path):
    # ROUND-13 MAJOR 1. `_geo_suggestions` scopes the option scan to the listbox the
    # control ITSELF points at (`aria-controls`), "so a stray `[role=option]`
    # elsewhere on the page can never be mistaken for a suggestion". Nothing pinned
    # it: dropping the scope (`css = _GEO_OPTION_CSS` -- the page-wide scan) passed
    # the whole suite, because every fixture drove a page whose only OPEN listbox was
    # the driven widget's own.
    #
    # Ashby serves two of these nameless comboboxes on one page and their listboxes
    # are floating portals: one can still be mounted and OPEN while the next field is
    # driven (a refusal clears the filter; nothing guarantees the other portal
    # collapsed). Page-wide, the geo field then reads the OTHER widget's rows as its
    # own suggestions, finds a unique match among them, and clicks a row that lives
    # inside the OTHER field's listbox -- committing the location answer into the
    # ValueSelect while its own box keeps a filter. One field's data driven into
    # another field's control, on a real employer's form, is the exact harm the
    # anti-guessing doctrine exists to prevent; refusing is the only honest answer.
    fieldmap = _fieldmap_with_geo()
    page = _FakeAshbyPage(fieldmap, geo_suggestions=(_GEO_DECOY,))
    geo, foreign = page.geo[_GEO_KEY], page.geo[_VISA_KEY]
    # The FOREIGN widget's listbox is open on its own filter, and its rows (the
    # employer's own option strings) hold one that is token-consistent with the GEO
    # field's value -- nothing about a ValueSelect's dataset rules that out.
    foreign.dataset = [_GEO_MATCH]
    foreign.value = _FOREIGN_FILTER
    foreign.expanded = True
    assert [row.inner_text() for row in foreign.options()] == [_GEO_MATCH]
    foreign.offered.clear()                        # that premise read is not the driver's

    landed, _, reason = ashby_fill._fill_ashby_combobox(
        page, _FieldValueStub(_combobox_field(_GEO_KEY, _GEO_LABEL), _GEO_VALUE))

    # The geo widget's OWN listbox never offered a match, so the field is REFUSED and
    # left honestly unfilled -- the foreign row is not a suggestion, it is furniture.
    assert landed is False
    assert reason == ashby_fill._GEO_NO_MATCH_REASON
    assert geo.offered and all(_GEO_MATCH not in rows for rows in geo.offered)
    assert geo.input_value() == ""                 # and the filter does not outlive it
    # ...and the FOREIGN widget was never read as a suggestion source, never clicked,
    # never committed: page-wide, `_unique_suggestion` matches ITS row and the commit
    # lands there (foreign.clicked_option == _GEO_MATCH, foreign.value == _GEO_MATCH).
    assert foreign.offered == []
    assert foreign.clicked_option is None
    assert foreign.input_value() == _FOREIGN_FILTER
    assert foreign.expanded is True                # untouched, listbox still open


# -- round-13 MAJOR 2: the commit clicks through the sanctioned gateway --------
# A ValueSelect whose rows are the EMPLOYER's own option strings, one of which reads
# like a submit control. No malice needed: it is an ordinary CV-retention question,
# and the owner's honest answer is the row that says "Send ...".
_SUBMITTISH_KEY = "custom_cv_retention"
_SUBMITTISH_LABEL = "What should we do with your CV after this role is filled?"
_SUBMITTISH_ROWS = ("Keep it on file", "Send it to partner companies")
_SUBMITTISH_VALUE = "Send it to partner companies"


def _submittish_dropdown() -> Field:
    return Field(key=_SUBMITTISH_KEY, label=_SUBMITTISH_LABEL,
                 type="multi_value_single_select", required=True,
                 options=list(_SUBMITTISH_ROWS), source=ASHBY_SOURCE,
                 locator=Locator(role="combobox", name=_SUBMITTISH_LABEL),
                 step_index=0)


def test_ashby_combobox_commit_goes_through_the_sanctioned_click_gateway(tmp_path):
    # ROUND-13 MAJOR 2. The suggestion commit clicks through `base._safe_click` --
    # "the SOLE sanctioned click primitive: refuses any submit-like name" -- and
    # nothing pinned it: swapping the commit to a raw `option.click()` passed the
    # whole suite, because no fixture ever offered a row whose text reads like a
    # submit control. The engine would then be clicking, inside a floating portal on
    # a real employer's form, an element it was never authorised to click.
    #
    # The engine cannot tell an option row that SAYS Send from a control that SENDS,
    # and it does not try: it refuses, loudly (FillSafetyError), and stops short of
    # applying. A field left unfilled is honest and recoverable; a click the engine
    # did not understand is neither.
    fieldmap = _fieldmap_with_geo()
    fieldmap.fields.append(_submittish_dropdown())
    page = _FakeAshbyPage(fieldmap, geo_suggestions=(_GEO_DECOY, _GEO_MATCH))
    widget = page.geo[_SUBMITTISH_KEY]
    assert widget.dataset == list(_SUBMITTISH_ROWS)   # the rows ARE the employer's options
    fv = _FieldValueStub(fieldmap.fields[-1], _SUBMITTISH_VALUE)

    with pytest.raises(FillSafetyError, match="matches the submit denylist"):
        ashby_fill._fill_ashby_combobox(page, fv)

    # The premise, so this can never pass vacuously: the row really was rendered and
    # really was the UNIQUE match, so the driver DID reach the commit -- the gateway
    # is what stopped it, not a no-match refusal one rung earlier.
    assert widget.offered and widget.offered[0] == [_SUBMITTISH_VALUE]
    assert ashby_fill._unique_suggestion(
        widget.offered[0], _SUBMITTISH_VALUE) == _SUBMITTISH_VALUE
    # And the refusal is a REFUSAL: the row was never clicked, so the widget never
    # committed -- its listbox is still open on the typed filter.
    assert widget.clicked_option is None
    assert widget.expanded is True


def test_ashby_geo_refuses_two_token_subset_suggestions(tmp_path):
    # MAJOR 2. The module calls this its "load-bearing rule": ANTI-GUESSING, "zero
    # hits or 2+ hits leaves the field HONESTLY UNFILLED". Only the ZERO-hit half was
    # pinned (`..._no_unique_match_stays_unfilled` shows the driver a SINGLE decoy,
    # where `== 1` and `>= 1` behave identically). Widening the gate to `>= 1` -- a
    # blind first-option commit whenever ANYTHING matches -- passed the full suite.
    #
    # The 2+-hits branch is live-reachable and this is what it looks like: the widget
    # offers a country row AND a region-in-that-country row, BOTH token-consistent
    # with the value, and NEITHER uniquely the place the candidate named. Refusing is
    # the whole point: an unfilled required field is honest and recoverable, a place
    # the candidate never gave, sent to a real employer, is neither.
    fieldmap = _fieldmap_with_geo()
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(location=_AMBIGUOUS_VALUE))
    assert {fv.key: fv.value for fv in values.fields}[_GEO_KEY] == _AMBIGUOUS_VALUE
    # the premise: BOTH rows really are token-subsets, so this is the AMBIGUOUS
    # branch of the ladder and not the zero-hit branch the old test already covered.
    value_tokens = ashby_fill._geo_tokens(_AMBIGUOUS_VALUE)
    assert all(ashby_fill._geo_tokens(row) <= value_tokens for row in _AMBIGUOUS_ROWS)

    page = _FakeAshbyPage(
        fieldmap,
        sweep_required_labels=_SAFE_REQUIRED_LABELS + (_GEO_LABEL,),
        geo_suggestions=_AMBIGUOUS_ROWS)
    report = ashby.fill(page, fieldmap, values)

    widget = page.geo[_GEO_KEY]
    # the widget really did render BOTH rows in one round (the ambiguity was SHOWN
    # to the driver, not merely available in the dataset)...
    assert any(set(_AMBIGUOUS_ROWS) <= set(rows) for rows in widget.offered)
    # ...and the driver committed NOTHING, and left no filter text behind.
    assert widget.clicked_option is None
    assert widget.input_value() == ""
    gap = {g["key"]: g for g in report.required_unfilled}
    assert _GEO_KEY in gap
    assert "no suggestion uniquely matched" in gap[_GEO_KEY]["reason"]
    assert report.complete is False


def test_ashby_unique_suggestion_refuses_every_ambiguous_row_set():
    # MAJOR 2 + MINOR 1, at the gate itself. BOTH rungs of the ladder are
    # unique-gated, and neither uniqueness check was pinned.
    unique = ashby_fill._unique_suggestion

    # (a) the TOKEN-SUBSET rung. Two rows, both token-consistent with the value,
    # both DIFFERENT places. The live shapes of this trap, from the round-8 probe of
    # the real geo endpoint: the value "Kinshasa, Democratic Republic of the Congo"
    # is offered two different countries, and an address in Georgia, USA is offered
    # both the country and the state. A `>= 1` gate commits the first row of each.
    assert unique(["Democratic Republic of the Congo", "Republic of the Congo"],
                  "Kinshasa, Democratic Republic of the Congo") is None
    assert unique(["United States", "Georgia"],
                  "Atlanta, Georgia, United States") is None
    assert unique(list(_AMBIGUOUS_ROWS), _AMBIGUOUS_VALUE) is None
    # the refusal is about AMBIGUITY, not about the rung: ONE token-subset row among
    # junk still commits, which is what keeps a fillable field fillable.
    assert unique(["Fakeland", _GEO_DECOY], _GEO_VALUE) == "Fakeland"

    # (b) the EXACT rung (MINOR 1). Two rows whose display text normalizes to the
    # same name are two DIFFERENT places with the same label (distinct
    # providerLocationId); the readback cannot tell them apart, so committing either
    # is a guess dressed as an exact match.
    assert unique(["Fakeland", "fakeland"], "Fakeland") is None
    assert unique(["Fakeland"], "Fakeland") == "Fakeland"


def test_ashby_geo_collapse_without_readback_is_not_committed(tmp_path):
    # MAJOR 3. `_geo_committed` requires TWO signals: the control reads the
    # suggestion back AND the listbox has collapsed. Only ONE direction was pinned
    # (`..._no_op_click_leaves_the_field_unfilled` kills the readback-only mutant).
    # Deleting the READBACK and trusting the collapse alone passed the full suite,
    # because no fake could COLLAPSE WITHOUT COMMITTING.
    #
    # This one does. The click lands, the listbox closes, and the input still holds
    # the typed filter -- so a collapse-only check reports the field FILLED with the
    # FILTER as its value. On the live seal target that filter is the owner's full
    # street address, sent as the answer to "Country you're currently residing in",
    # and reported COMPLETE.
    fieldmap = _fieldmap_with_geo()
    values = _resolved_values(fieldmap, tmp_path=tmp_path,
                              ssot=_fake_ssot(location=_COLLAPSE_VALUE))
    page = _FakeAshbyPage(
        fieldmap,
        sweep_required_labels=_SAFE_REQUIRED_LABELS + (_GEO_LABEL,),
        geo_suggestions=(_COLLAPSE_ROW,),
        geo_class=_FakeGeoAutocompleteCollapseNoCommit)

    report = ashby.fill(page, fieldmap, values)

    widget = page.geo[_GEO_KEY]
    assert widget.clicked_option == _COLLAPSE_ROW   # the driver DID find its match...
    assert widget.expanded is False                 # ...and the listbox DID collapse
    # ...and the field is STILL not filled, because the control never read it back.
    gap = {g["key"]: g for g in report.required_unfilled}
    assert _GEO_KEY in gap
    assert "did not commit" in gap[_GEO_KEY]["reason"]
    mismatch = {m["key"]: m for m in report.readback_mismatches}
    # what a collapse-only check would have reported as the committed answer:
    assert mismatch[_GEO_KEY]["actual"] == _COLLAPSE_FILTER
    assert mismatch[_GEO_KEY]["intended"] == _COLLAPSE_VALUE
    assert widget.input_value() == ""     # the filter does not outlive the attempt
    assert report.complete is False


def test_ashby_fixture_carries_the_live_autofill_pane_and_its_decoy_file_input():
    # MAJOR 4. The fixture was the seal target's `.ashby-application-form-container`
    # SUBTREE, but the code under test runs PAGE-WIDE, and the live page carries one
    # more thing: the "Autofill from resume" pane, holding a SECOND file input that
    # comes FIRST in DOM order. A fixture may be HOSTILE; it may never be SOFTER than
    # the live page. Re-derived from the live DOM (read-only probe 2026-07-14,
    # elevenlabs/b43e388f: page-wide file=2, paths=9, required=4) and grafted in.
    # That posting has since CLOSED, and the decoy is NOT an artefact of it: the
    # current live target (axelera/17131e36) renders TWO page-wide file inputs
    # too, so this hazard is exactly as live as it was.
    #
    # The decoy is what makes a file-upload locator resolve to TWO elements instead
    # of one, and it is not inert: it is Ashby's resume PARSER, so a CV fed to it is
    # fed to the parser instead of to the application.
    page = _HtmlPage(_real_dom())

    files = _css_select(page._root, 'input[type="file"]')
    assert len(files) == 2
    decoy, resume = files                  # the decoy comes FIRST, exactly as live
    assert decoy.get_attribute("id") is None      # no id, no name: nothing to key on
    assert decoy.get_attribute("name") is None
    assert "pdf" in (decoy.get_attribute("accept") or "")   # and it wants the CV
    assert resume.get_attribute("id") == "_systemfield_resume"
    # the decoy belongs to NO field entry; the resume input is the only OWNED one.
    assert _css_select(page._root, '[data-field-path] input[type="file"]') == [resume]
    assert any("Autofill from resume" in node.inner_text()
               for node in _descendants(page._root) if node.tag == "h3")

    # and the pane perturbs NEITHER oracle -- it carries no required control and no
    # field entry -- so the sweep and the reconciler see exactly what they saw before
    # (this is why grafting it is a pure hardening, not a change of subject).
    assert len(_css_select(page._root, "[data-field-path]")) == 9
    assert len(_css_select(page._root, base._REQUIRED_CSS)) == 4


def test_ashby_cv_lands_on_the_resume_input_never_the_autofill_decoy(tmp_path):
    # MAJOR 4, at the FILL level. The decoy is FIRST in DOM order and it ACCEPTS the
    # CV's own MIME family, so the `accept`-family fallback alone would hand it the
    # CV. What saves the fill is the key-token preference (`_systemfield_resume` ->
    # the token "resume", which only the real resume input carries in its id), and
    # that preference is now pinned against a page that can actually punish losing it.
    fieldmap = _fieldmap_without(_CONSENT_KEY)
    resume_key = next(f.key for f in fieldmap.fields if f.type == "input_file")
    assert "resume" in resume_key
    values = _resolved_values(fieldmap, tmp_path=tmp_path)

    decoy = _FakeFileInput(          # the autofill pane's input, verbatim from live
        id=None, name=None,
        accept="application/pdf,.pdf,application/msword,.doc")
    resume = _FakeFileInput(
        id="_systemfield_resume", name=None,
        accept="application/pdf,.pdf,image/*,video/*,audio/*")
    page = _FakeAshbyPage(fieldmap, file_inputs=[decoy, resume])   # live DOM order

    report = ashby.fill(page, fieldmap, values)

    assert decoy.set_input_files_calls == 0     # the parser is never fed the CV
    assert decoy.uploaded is None
    assert resume.set_input_files_calls == 1
    assert [u["key"] for u in report.uploads] == [resume_key]
    assert report.complete is True


def test_ashby_every_driven_locator_resolves_to_exactly_one_element():
    # THE LOCATOR-RESOLUTION INVARIANT, over every control shape Ashby renders.
    #
    # For EVERY field the capture emits that is NOT handed off to a human, build the
    # locator EXACTLY as the production fill builds it -- the same three routes, in
    # fill()'s own order, through the production functions themselves -- and require
    # it to resolve to EXACTLY ONE element on the same DOM.
    #
    # Not "at least one". ZERO is a PHANTOM: the engine believes it filled a control
    # that does not exist. TWO is worse than a phantom: it may type into the WRONG
    # control, and it is silent. A locator is a PAIR, and a phantom can hide in
    # either half -- a truthful role with a fictional name resolves to zero just as
    # surely as a wrong role does.
    page = _HtmlPage(_dom_with_siblings())
    raw = {"data": {"jobPosting": _every_shape_posting()}}
    cap_page = _FakePage(responses=_ashby_responses(raw),
                         html=_dom_with_siblings())
    fm = capture_ashby("fauxcorp", "9008", _factory_for(cap_page),
                       now=lambda: _PINNED)
    assert len(fm.fields) == 8

    resolved: dict[str, str] = {}
    for fld in fm.fields:
        # the upload route keys on the VALUE being a Path, exactly as resolve_values
        # hands it over, which is why a file field's role never reaches a locator.
        value = Path("cv.pdf") if fld.type == "input_file" else "x"
        fv = _FieldValueStub(fld, value)

        if ashby_fill._is_upload(fv):                    # (1) fill()'s first branch
            # PRODUCTION's own primitive, on the real DOM (decoy included).
            control = kernel_fill_toolkit._locate_file_input(page, fv)
            assert control is not None                   # never a phantom
            assert control.get_attribute("id") == "_systemfield_resume"
            # and it is UNIQUE: exactly one of the page's file inputs is keyed by a
            # token of this field's key. Two would be a coin toss between the resume
            # field and Ashby's resume parser.
            keyed = [inp for inp in kernel_fill_toolkit._file_inputs(page)
                     if (idname := kernel_fill_toolkit._input_idname(inp))
                     and any(token in idname for token in
                             kernel_fill_toolkit._field_key_tokens(fv.key))]
            assert len(keyed) == 1
            resolved[fld.key] = "upload"
            continue

        if ashby_fill._needs_human_handoff(fv):          # (2) no locator is built
            assert fld.locator.role in _CLICK_HAZARD_ROLES
            continue

        if ashby_fill._is_ashby_combobox(fv):            # (3) the path-anchored CSS
            found = _css_select(
                page._root, ashby_fill._GEO_CONTROL_CSS.format(path=fv.key))
            assert len(found) == 1, (
                f"{fld.locator.role!r} anchor for {fld.key!r} resolved to "
                f"{len(found)} elements")
            resolved[fld.key] = "combobox"
            continue

        # (4) everything else is aimed at the page by ROLE + NAME.
        found = _role_matches(page, fld.locator.role, fld.locator.name)
        assert len(found) == 1, (
            f"get_by_role({fld.locator.role!r}, name={fld.locator.name!r}) "
            f"resolved to {len(found)} elements")
        resolved[fld.key] = "role+name"

    # every field the fill DRIVES resolved to exactly one control: the upload, both
    # nameless comboboxes, and the two role+name text-ish controls. The other three
    # are the click hazards, handed off before a locator exists.
    assert sorted(resolved.values()) == [
        "combobox", "combobox", "role+name", "role+name", "upload"]
