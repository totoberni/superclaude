"""Lever's adoption of the kernel's `control_toolkit.drive_control` (W5.1c).

Before this wave `lever.fill()` handed EVERY checkbox/radio field off to a
human (the hCaptcha-hazard workaround; see `lever.fill.__doc__` point 3,
pre-W5.1c). This wave replaces that with `_drive_control_field`, which routes
every checkbox/radio through the kernel's `drive_control`. These tests exercise
`_drive_control_field` / `_locate_option` / `_is_control_field` directly, with
minimal fakes, rather than rebuilding the full DOM/page fixture harness that
`tests/test_providers_lever.py` already owns (that file's own per-vendor
duplication is deliberate and self-contained; this file stays equally
self-contained rather than reaching into it).

Lever has no `date`-typed field in its capture (`capture._control_type` never
returns "date": a date-shaped input falls through to plain `input_text` and is
typed via the ordinary native path) and no picker-only calendar, so lever never
builds a DATE `ControlSpec` -- nothing here exercises that kind, by design, not
by omission.
"""

import importlib

import pytest

lever_fill = importlib.import_module("engine.providers.lever.fill")

from engine.kernel.contracts import FieldValue, FillSafetyError, Locator


# -- fakes -----------------------------------------------------------------


class _FakeToggleLocator:
    """A single checkbox/radio input: `.check()`/`.uncheck()`/`.is_checked()`,
    mirroring the real Playwright locator surface `control_toolkit._drive_toggle`
    drives. `confirm_as` lets a test make the readback disagree with the drive
    (the silent-no-op case the whole engine exists to catch)."""

    def __init__(self, *, confirm_as=None, raise_on_check=False):
        self._checked = False
        self._confirm_as = confirm_as
        self._raise_on_check = raise_on_check
        self.check_calls = 0
        self.uncheck_calls = 0

    def check(self):
        self.check_calls += 1
        if self._raise_on_check:
            raise RuntimeError("boom")
        self._checked = True

    def uncheck(self):
        self.uncheck_calls += 1
        self._checked = False

    def is_checked(self):
        if self._confirm_as is not None:
            return self._confirm_as
        return self._checked

    def count(self):
        return 1

    def and_(self, other):
        """`_locate_option` (W5.1-R2) intersects the role+name match with the
        field's own group CSS via `Locator.and_()`. A GENUINE intersection, not a
        stub: this locator (still driveable via `.check()`/`.uncheck()`) is
        returned ONLY if `other` (the group's own control set, `page.locator(
        _group_css(key))`) actually contains it; otherwise the intersection is
        genuinely empty, exactly as a real Playwright `.and_()` would report for
        an option that does not belong to the queried group."""
        return self if other.contains(self) else _FakeOptionSet([])


class _FakeOptionSet:
    """The controls `page.locator(_group_css(key))` resolves: every option
    registered on the page it was asked against. Every `_FakeRolePage` in this
    file models exactly one field's own option group (this module's docstring:
    minimal fakes, one field per page), so that is every control currently
    registered -- and `.and_()` above intersects against it by real identity,
    never by assuming the answer."""

    def __init__(self, locators):
        self._locators = list(locators)

    def count(self):
        return len(self._locators)

    def contains(self, locator) -> bool:
        return any(locator is loc for loc in self._locators)


class _FakeRolePage:
    """A fake page whose main job is `get_by_role(role, name=..., exact=...)`,
    resolved from a `{(role, name): locator}` table -- the exact shape
    `_locate_option` builds its call against. `locator(css)` answers the group-CSS
    half of the same call (W5.1-R2): the set every option's `.and_()` intersects
    against."""

    def __init__(self, controls: dict):
        self._controls = controls
        self.requested = []

    def get_by_role(self, role, name=None, exact=None):
        self.requested.append((role, name, exact))
        return self._controls[(role, name)]

    def locator(self, css):
        return _FakeOptionSet(self._controls.values())


def _fv(key, label, field_type, role, value) -> FieldValue:
    return FieldValue(key=key, label=label, type=field_type,
                      locator=Locator(role=role, name=label), value=value)


# -- _is_control_field -------------------------------------------------------


def test_is_control_field_true_for_checkbox_and_radio_roles():
    checkbox = _fv("consent[privacy]", "I agree", "boolean", "checkbox", True)
    radio = _fv("cards[a][field0]", "Level?", "multi_value_single_select",
               "radio", "Professional")
    assert lever_fill._is_control_field(checkbox) is True
    assert lever_fill._is_control_field(radio) is True


def test_is_control_field_false_for_textbox_and_combobox_roles():
    text = _fv("name", "Full name", "input_text", "textbox", "Ada Lovelace")
    select = _fv("cards[x][field0]", "Sponsorship?",
                "multi_value_single_select", "combobox", "Yes")
    assert lever_fill._is_control_field(text) is False
    assert lever_fill._is_control_field(select) is False


# -- single checkbox ----------------------------------------------------------


def test_single_checkbox_drives_and_confirms_via_drive_control(monkeypatch):
    fv = _fv("consent[privacy]", "I agree to the privacy policy", "boolean",
             "checkbox", True)
    fake_locator = _FakeToggleLocator()
    monkeypatch.setattr(lever_fill, "_locate", lambda page, f: fake_locator)

    filled_keys, mismatches, skips = set(), [], []
    lever_fill._drive_control_field(object(), fv, mismatches, skips, filled_keys)

    assert fake_locator.check_calls == 1
    assert fv.key in filled_keys
    assert mismatches == []
    assert skips == []


def test_single_checkbox_unconfirmed_readback_is_a_required_gap(monkeypatch):
    # the tick happened, but the page never actually reflects it (silent no-op):
    # this must NEVER count as filled.
    fv = _fv("consent[marketing]", "I want marketing emails", "boolean",
             "checkbox", True)
    fake_locator = _FakeToggleLocator(confirm_as=False)
    monkeypatch.setattr(lever_fill, "_locate", lambda page, f: fake_locator)

    filled_keys, mismatches, skips = set(), [], []
    lever_fill._drive_control_field(object(), fv, mismatches, skips, filled_keys)

    assert fake_locator.check_calls == 1
    assert fv.key not in filled_keys
    assert mismatches and mismatches[0]["key"] == fv.key
    assert skips and skips[0][0] == fv.key


# -- radio group ---------------------------------------------------------------


def test_radio_group_drives_only_the_chosen_option(monkeypatch):
    # a radio GROUP field carries the QUESTION as its own locator.name, but the
    # physical control to drive is the ONE OPTION the applicant chose, located
    # by the option's own accessible name (its own wording), never the question.
    fv = _fv("cards[7f3a][field0]", "What is your level of French?",
             "multi_value_single_select", "radio", "Professional")
    chosen = _FakeToggleLocator()
    page = _FakeRolePage({("radio", "Professional"): chosen})

    filled_keys, mismatches, skips = set(), [], []
    lever_fill._drive_control_field(page, fv, mismatches, skips, filled_keys)

    assert page.requested == [("radio", "Professional", True)]
    assert chosen.check_calls == 1
    assert fv.key in filled_keys


def test_radio_group_denylisted_option_name_raises_fill_safety_error(monkeypatch):
    # the submit denylist is checked against the OPTION's own accessible name
    # (the thing actually driven), not the group's question.
    fv = _fv("cards[abc][field0]", "How would you like to proceed?",
             "multi_value_single_select", "radio", "Continue")
    page = _FakeRolePage({("radio", "Continue"): _FakeToggleLocator()})

    with pytest.raises(FillSafetyError):
        lever_fill._drive_control_field(page, fv, [], [], set())


# -- checkbox group --------------------------------------------------------------


def test_checkbox_group_drives_every_selected_option():
    fv = _fv("cards[7f3a][field0]", "Which shifts can you work?",
             "multi_value_multi_select", "checkbox", ["Days", "Nights"])
    days = _FakeToggleLocator()
    nights = _FakeToggleLocator()
    page = _FakeRolePage({("checkbox", "Days"): days,
                          ("checkbox", "Nights"): nights})

    filled_keys, mismatches, skips = set(), [], []
    lever_fill._drive_control_field(page, fv, mismatches, skips, filled_keys)

    assert days.check_calls == 1
    assert nights.check_calls == 1
    assert fv.key in filled_keys
    assert mismatches == []


def test_checkbox_group_partial_confirmation_is_not_filled():
    # one option in the group silently does not take: the WHOLE field must not
    # count as filled, and both option outcomes are visible in the reported
    # mismatch/skip -- never a partially-successful silent pass.
    fv = _fv("cards[7f3a][field0]", "Which shifts can you work?",
             "multi_value_multi_select", "checkbox", ["Days", "Nights"])
    days = _FakeToggleLocator()
    nights = _FakeToggleLocator(confirm_as=False)
    page = _FakeRolePage({("checkbox", "Days"): days,
                          ("checkbox", "Nights"): nights})

    filled_keys, mismatches, skips = set(), [], []
    lever_fill._drive_control_field(page, fv, mismatches, skips, filled_keys)

    assert fv.key not in filled_keys
    assert mismatches and mismatches[0]["key"] == fv.key
    assert mismatches[0]["actual"] == [True, False]
    assert skips and skips[0][0] == fv.key


def test_checkbox_group_drive_error_on_one_option_is_fail_soft():
    # a raising .check() on one option is caught by the kernel's own drive path
    # (control_toolkit._drive_toggle), never propagated as an unhandled crash.
    fv = _fv("cards[7f3a][field0]", "Which shifts can you work?",
             "multi_value_multi_select", "checkbox", ["Days"])
    page = _FakeRolePage({("checkbox", "Days"):
                         _FakeToggleLocator(raise_on_check=True)})

    filled_keys, mismatches, skips = set(), [], []
    lever_fill._drive_control_field(page, fv, mismatches, skips, filled_keys)

    assert fv.key not in filled_keys
    assert "drive-error" in skips[0][1]


# -- unknown value shape --------------------------------------------------------


def test_control_field_with_unresolvable_value_shape_is_skipped_not_raised():
    fv = _fv("consent[weird]", "I agree", "boolean", "checkbox", None)
    filled_keys, mismatches, skips = set(), [], []
    lever_fill._drive_control_field(object(), fv, mismatches, skips, filled_keys)
    assert fv.key not in filled_keys
    assert skips and "control value must be bool, str or list" in skips[0][1]
