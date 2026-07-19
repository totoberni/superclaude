"""Greenhouse's adoption of the shared kernel control-driving mechanism
(`engine.kernel.control_toolkit.drive_control`, W5.1c) for checkbox/radio
controls, replacing the pre-W5.1c human hand-off
(`engine.providers.greenhouse.fill._HUMAN_HANDOFF_REASON`).

Exercises `fill._drive_click_control` directly (the one seam this vendor's
`fill()` loop now calls instead of appending the hand-off skip), with a
minimal fake page/locator harness -- no live browser, no network, mirroring
the convention of the existing `tests/providers/greenhouse/` fixtures.

Greenhouse's own role vocabulary (`kernel.contracts._ROLE_FOR_TYPE` +
`capture._ROLE_OVERRIDES`) only ever produces "checkbox" for a click-hazard
field (a `boolean` question, or a `multi_value_multi_select` option): there is
no "radio" role and no date-picker widget anywhere in this vendor's schema or
DOM handling, so only the CHECKBOX kind is exercised here with real fixtures;
the RADIO branch is covered structurally (the role-to-kind map routes it
correctly) without a live radio fixture, and DATE is not applicable to this
vendor at all (see `fill._drive_click_control`'s own docstring).
"""

import importlib

from engine.kernel.contracts import FieldValue, FillSafetyError, Locator

# `engine.providers.greenhouse.fill` (the SUBMODULE) is shadowed at package
# scope by the `fill` Provider-callable it re-exports (see the package
# docstring's "NAME NOTE"); reached via `importlib.import_module`, exactly as
# the pre-existing `tests/test_providers_greenhouse.py` does.
greenhouse_fill = importlib.import_module("engine.providers.greenhouse.fill")


class _FakeCheckboxLocator:
    """A native `<input type="checkbox">` (or radio): `.check()`/`.uncheck()`
    flip `checked`; `.is_checked()` is the readback `control_toolkit._confirm`
    reads via the shared kernel `_readback`."""

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


def _checkbox_field(key, label, *, value, locator_name=None):
    return FieldValue(
        key=key, label=label, type="boolean",
        locator=Locator(role="checkbox", name=locator_name or label),
        value=value)


def test_drives_a_boolean_checkbox_true_and_confirms():
    """A required consent-style boolean now DRIVES (never hands off): the
    fake `.check()`/`is_checked()` round-trips True, so the outcome confirms."""
    fv = _checkbox_field("certify", "I certify the above is true", value=True)
    checkbox = _FakeCheckboxLocator()
    page = _FakeRolePage({("checkbox", fv.label): checkbox})

    confirmed, reason = greenhouse_fill._drive_click_control(page, fv)

    assert confirmed is True and reason == ""
    assert checkbox.checked is True


def test_drives_a_boolean_checkbox_false_and_confirms():
    """An unticked-intent boolean (e.g. an optional marketing consent left
    False) drives via `.uncheck()`, not just the True/tick path."""
    fv = _checkbox_field("marketing_opt_in", "I want marketing emails",
                         value=False)
    checkbox = _FakeCheckboxLocator(checked=True)
    page = _FakeRolePage({("checkbox", fv.label): checkbox})

    confirmed, reason = greenhouse_fill._drive_click_control(page, fv)

    assert confirmed is True and reason == ""
    assert checkbox.checked is False


def test_multi_value_multi_select_option_drives_by_its_own_option_label():
    """The checkbox-GROUP case this wave named as its job: each OPTION is its
    own `boolean` FieldValue with the OPTION's own label as the accessible
    name (never the question's), one `.check()` per option."""
    fv = _checkbox_field("question_langs[][python]", "Python",
                         value=True, locator_name="Python")
    checkbox = _FakeCheckboxLocator()
    page = _FakeRolePage({("checkbox", "Python"): checkbox})

    confirmed, reason = greenhouse_fill._drive_click_control(page, fv)

    assert confirmed is True
    assert checkbox.checked is True


def test_unconfirmed_drive_is_reported_never_silently_filled():
    """A control that cannot perform the requested transition (no `.uncheck()`
    here) is `driven=False` (`control_toolkit._skip`), surfaced as an
    unconfirmed, unfilled field with a reason -- never counted as filled."""
    fv = _checkbox_field("newsletter", "Subscribe to the newsletter",
                         value=False)
    page = _FakeRolePage({("checkbox", fv.label): _NoUncheckLocator()})

    confirmed, reason = greenhouse_fill._drive_click_control(page, fv)

    assert confirmed is False
    assert reason and "uncheck" in reason


def test_unexpected_role_on_a_click_hazard_field_is_a_reported_skip():
    """Defensive branch: `_needs_human_handoff` can also be tripped by a bare
    boolean value regardless of role (`fill_toolkit._needs_human_handoff`);
    a role this vendor's map does not recognize is skipped with a reason
    rather than raising or silently driving the wrong action."""
    fv = FieldValue(key="odd", label="Odd control", type="boolean",
                    locator=Locator(role="switch", name="Odd control"),
                    value=True)
    page = _FakeRolePage({})

    confirmed, reason = greenhouse_fill._drive_click_control(page, fv)

    assert confirmed is False
    assert "switch" in reason and "no drive path" in reason


def test_submit_denylist_name_still_raises_fillsafetyerror():
    """The submit-denylist guard (`control_toolkit._guard_name`) is not
    bypassed by routing checkbox/radio through this new seam: a control named
    like a submit control still aborts the fill, never a silent skip."""
    fv = _checkbox_field("apply", "Submit application", value=True)
    page = _FakeRolePage({("checkbox", fv.label): _FakeCheckboxLocator()})

    try:
        greenhouse_fill._drive_click_control(page, fv)
        assert False, "expected FillSafetyError"
    except FillSafetyError:
        pass


def test_role_to_kind_map_only_knows_checkbox_and_radio_never_date():
    """Greenhouse's own schema never exposes a date-picker widget (no field
    type maps to a date role anywhere in `kernel.contracts._ROLE_FOR_TYPE` or
    `capture._ROLE_OVERRIDES`), so this vendor's role-to-kind map deliberately
    carries no DATE entry; a date-shaped answer here is always a plain
    rendered string typed through the ordinary text path (`_apply_native`),
    never a `drive_control` DATE spec with a `day_cell` picker (no picker
    exists for this vendor to look up a calendar cell from)."""
    assert set(greenhouse_fill._ROLE_TO_CONTROL_KIND) == {"checkbox", "radio"}
