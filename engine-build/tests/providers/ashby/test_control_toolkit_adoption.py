"""Ashby's adoption of the shared kernel control-driving mechanism
(`engine.kernel.control_toolkit.drive_control`, W5.1c) for checkbox controls,
narrowing the pre-W5.1c blanket Turnstile hand-off
(`engine.providers.ashby.fill._HUMAN_HANDOFF_REASON`).

Exercises `fill._control_kind` (the routing decision) and `fill._fill_control`
(the drive seam `fill()`'s loop now calls for a control `_control_kind` claims)
directly, with a minimal fake page/locator harness -- no live browser, no
network, mirroring the convention of `tests/providers/greenhouse/
test_control_toolkit_adoption.py`.

WHICH KINDS ASHBY ACTUALLY USES: CHECKBOX (a single, unambiguous boolean
control, the graphql `Boolean` type) and, since the W5.1c radio-group adoption
completed on the TB5 live run, RADIO (a radio-rendered
`multi_value_single_select` GROUP, 33/34 live ValueSelect fields). Ashby's
`Field.locator` is one role+name pair per FIELD, so a radio group is driven a
different way: `fill._locate_option` builds the per-option locator (lever's FX1
shape, scoped to the field's `data-field-path` entry) and `_fill_control` ticks
exactly the resolved option. A checkbox-rendered `multi_value_multi_select`
GROUP (a list value) is a scoped-out sibling and stays on the human hand-off, so
`_control_kind` returns None for it (see the type-group tests below and
`fill._control_kind`'s own docstring). DATE is not applicable because Ashby's
`Date` graphql type collapses into the generic canonical `input_text` type
(`capture._ASHBY_TYPE_MAP`) with no surviving signal to route it through
`ControlKind.DATE`.
"""

import importlib
import re

from engine.kernel.contracts import FieldValue, FillSafetyError, Locator
from engine.kernel.control_toolkit import ControlKind

# `engine.providers.ashby.fill` (the SUBMODULE) is shadowed at package scope by
# the `fill` Provider-callable it re-exports (see `capture.py`'s package NAME
# NOTE, which applies identically to `fill`); reached via
# `importlib.import_module`, exactly as `tests/providers/greenhouse/
# test_control_toolkit_adoption.py` does for its own vendor.
ashby_fill = importlib.import_module("engine.providers.ashby.fill")


class _FakeCheckboxLocator:
    """A native `<input type="checkbox">`: `.check()`/`.uncheck()` flip
    `checked`; `.is_checked()` is the readback `control_toolkit._confirm` reads
    via the shared kernel `_readback`."""

    def __init__(self, checked: bool | None = None):
        self.checked = checked

    def check(self):
        self.checked = True

    def uncheck(self):
        self.checked = False

    def is_checked(self):
        return bool(self.checked)


class _NoUncheckLocator:
    """A control exposing `.check()` but no `.uncheck()` -- the fail-soft path
    `control_toolkit._drive_toggle` takes for an untick it cannot perform."""

    def __init__(self):
        self.checked = None

    def check(self):
        self.checked = True

    def is_checked(self):
        return bool(self.checked)


class _FakeRolePage:
    """`page.get_by_role(role, name=...)` returns whichever fake locator was
    registered for that (role, name) pair -- mirrors `base._locate`'s own
    `page.get_by_role(role, name=name)` call exactly."""

    def __init__(self, controls: dict):
        self._controls = controls

    def get_by_role(self, role, name=None):
        return self._controls[(role, name)]


class _FakeRadioOption:
    """One native radio in a group: its OWN accessible name is the option label,
    and it shares the group's `data-field-path` entry. `.check()`/`.is_checked()`
    are the drive + readback the shared kernel mechanism uses."""

    def __init__(self, path, name):
        self.path = path
        self.name = name
        self.checked = None

    def check(self):
        self.checked = True

    def uncheck(self):
        self.checked = False

    def is_checked(self):
        return bool(self.checked)


class _FakeRadioMatch:
    """A Playwright-Locator-shaped match over radio options: `.count()`, `.and_()`
    (the FX1 intersection), and the drive/readback verbs delegated to the SINGLE
    matched control (a strict-mode violation otherwise)."""

    def __init__(self, nodes):
        self.nodes = list(nodes)

    def count(self):
        return len(self.nodes)

    def and_(self, other):
        return _FakeRadioMatch(
            [n for n in self.nodes if any(n is o for o in other.nodes)])

    def _only(self):
        assert len(self.nodes) == 1, (
            f"strict mode: {len(self.nodes)} elements, not 1")
        return self.nodes[0]

    def check(self):
        self._only().check()

    def uncheck(self):
        self._only().uncheck()

    def is_checked(self):
        return self._only().is_checked()


class _FakeRadioGroupPage:
    """A page modelling one or more radio GROUPS. Serves the two calls
    `fill._locate_option` makes: `get_by_role("radio", name=<option>, exact=True)`
    (page-wide, by the option's OWN accessible name) and
    `locator('[data-field-path="<key>"] input')` (the group's entry scope), so
    `.and_()` narrows a page-wide option match to the one option inside a group."""

    def __init__(self, groups: dict):
        self.nodes = []
        self._by_path = {}
        for path, options in groups.items():
            opts = [_FakeRadioOption(path, o) for o in options]
            self._by_path[path] = opts
            self.nodes.extend(opts)

    def get_by_role(self, role, name=None, exact=None):
        assert role == "radio"
        return _FakeRadioMatch([n for n in self.nodes if n.name == name])

    def locator(self, css):
        match = re.match(r'\[data-field-path="([^"]+)"\] input$', css)
        path = match.group(1) if match else None
        return _FakeRadioMatch(self._by_path.get(path, []))


def _boolean_field(key, label, *, value, locator_name=None):
    return FieldValue(
        key=key, label=label, type="boolean",
        locator=Locator(role="checkbox", name=locator_name or label),
        value=value)


def _radio_group_field(key, label, *, value="Yes"):
    """A radio-rendered `multi_value_single_select` (33/34 live Ashby
    ValueSelect fields): ONE Field for the whole group, string value, never a
    bool."""
    return FieldValue(
        key=key, label=label, type="multi_value_single_select",
        locator=Locator(role="radio", name=label), value=value)


def _checkbox_group_field(key, label, *, value=("Documentaries",)):
    """A checkbox-rendered `multi_value_multi_select` (Ashby's native checkbox
    GROUP, per `capture._ASHBY_FALLBACK_ROLE`): ONE Field, list value."""
    return FieldValue(
        key=key, label=label, type="multi_value_multi_select",
        locator=Locator(role="checkbox", name=label), value=list(value))


def test_drives_a_boolean_checkbox_true_and_confirms():
    """A required consent-style boolean now DRIVES (never hands off): the
    fake `.check()`/`is_checked()` round-trips True, so the outcome confirms."""
    fv = _boolean_field("certify", "I certify the above is true", value=True)
    checkbox = _FakeCheckboxLocator()
    page = _FakeRolePage({("checkbox", fv.label): checkbox})

    kind = ashby_fill._control_kind(fv)
    assert kind == ControlKind.CHECKBOX
    ok, actual, reason = ashby_fill._fill_control(page, fv, kind)

    assert ok is True and reason == ""
    assert checkbox.checked is True


def test_drives_a_boolean_checkbox_false_and_confirms():
    """An unticked-intent boolean (e.g. an optional marketing consent left
    False) drives via `.uncheck()`, not just the True/tick path."""
    fv = _boolean_field("marketing_opt_in", "I want marketing emails",
                        value=False)
    checkbox = _FakeCheckboxLocator(checked=True)
    page = _FakeRolePage({("checkbox", fv.label): checkbox})

    kind = ashby_fill._control_kind(fv)
    ok, actual, reason = ashby_fill._fill_control(page, fv, kind)

    assert ok is True and reason == ""
    assert checkbox.checked is False


def test_unconfirmed_drive_is_reported_never_silently_filled():
    """A control that cannot perform the requested transition (no `.uncheck()`
    here) is `driven=False` (`control_toolkit._skip`), surfaced as an
    unconfirmed, unfilled field with a reason -- never counted as filled."""
    fv = _boolean_field("newsletter", "Subscribe to the newsletter",
                        value=False)
    page = _FakeRolePage({("checkbox", fv.label): _NoUncheckLocator()})

    kind = ashby_fill._control_kind(fv)
    ok, actual, reason = ashby_fill._fill_control(page, fv, kind)

    assert ok is False
    assert reason and "uncheck" in reason


def test_submit_denylist_name_still_raises_fillsafetyerror():
    """The submit-denylist guard (`control_toolkit._guard_name`) is not
    bypassed by routing the boolean checkbox through this new seam: a control
    named like a submit control still aborts the fill, never a silent skip."""
    fv = _boolean_field("apply", "Submit application", value=True)
    page = _FakeRolePage({("checkbox", fv.label): _FakeCheckboxLocator()})

    kind = ashby_fill._control_kind(fv)
    try:
        ashby_fill._fill_control(page, fv, kind)
        assert False, "expected FillSafetyError"
    except FillSafetyError:
        pass


def test_radio_group_drives_the_resolved_option_via_per_option_locate():
    """W5.1c radio-group adoption: a radio-rendered `multi_value_single_select`
    is now a `ControlKind.RADIO` target, DRIVEN, not handed off. `_control_kind`
    returns RADIO, and `_fill_control` locates the resolved option INSIDE the
    group's own entry (`_locate_option`, lever's FX1 shape) and ticks exactly it,
    single-select, readback-confirmed via the shared kernel mechanism."""
    fv = _radio_group_field("work_auth", "Are you authorized to work?",
                            value="Yes")
    page = _FakeRadioGroupPage({fv.key: ["Yes", "No"]})

    kind = ashby_fill._control_kind(fv)
    assert kind == ControlKind.RADIO

    ok, actual, reason = ashby_fill._fill_control(page, fv, kind)
    assert ok is True and reason == ""
    by_name = {o.name: o for o in page._by_path[fv.key]}
    assert by_name["Yes"].is_checked() is True     # exactly the resolved option
    assert by_name["No"].is_checked() is False     # the other stays untouched


def test_radio_group_option_that_cannot_be_located_hands_off():
    """A radio group whose resolved option resolves to NO control inside its own
    entry is handed off loudly (`_GROUP_OPTION_HANDOFF_REASON`), never a guessed
    click: the answer is not among the group's rendered options, so no control is
    ticked and the field surfaces as an honest gap."""
    fv = _radio_group_field("work_auth", "Are you authorized to work?",
                            value="Maybe")   # not a rendered option
    page = _FakeRadioGroupPage({fv.key: ["Yes", "No"]})

    ok, actual, reason = ashby_fill._fill_control(
        page, fv, ashby_fill._control_kind(fv))
    assert ok is False
    assert reason == ashby_fill._GROUP_OPTION_HANDOFF_REASON
    assert all(o.is_checked() is False for o in page._by_path[fv.key])


def test_checkbox_group_is_not_a_control_kind_target_and_still_hands_off():
    """The checkbox-rendered `multi_value_multi_select` GROUP: a LIST value on
    ONE field-level locator, so `_control_kind` returns None and the existing
    hand-off gate still catches it. Per-option checkbox-group driving is a
    scoped-out sibling of the radio-group wave (`_locate_option` would serve it
    structurally), not a technical impossibility."""
    fv = _checkbox_group_field("content_types", "Content you have worked on")

    assert ashby_fill._control_kind(fv) is None
    assert ashby_fill._needs_human_handoff(fv) is True


def test_control_kind_produces_radio_for_a_group_but_never_date():
    """WHICH KINDS ASHBY USES: CHECKBOX (a lone boolean) and RADIO (a
    single-select group). `_control_kind` has NO branch that can return
    `ControlKind.DATE` -- Ashby's `Date` type collapses into `input_text`
    (`capture._ASHBY_TYPE_MAP`) with no surviving signal to route it through
    DATE. A checkbox multi-select GROUP (list value) stays None (handed off)."""
    boolean_fv = _boolean_field("certify", "I certify", value=True)
    radio_fv = _radio_group_field("work_auth", "Are you authorized to work?")
    checkbox_group_fv = _checkbox_group_field("content_types", "Content types")

    assert ashby_fill._control_kind(boolean_fv) == ControlKind.CHECKBOX
    assert ashby_fill._control_kind(radio_fv) == ControlKind.RADIO
    assert ashby_fill._control_kind(checkbox_group_fv) is None
    for fv in (boolean_fv, radio_fv, checkbox_group_fv):
        assert ashby_fill._control_kind(fv) != ControlKind.DATE
