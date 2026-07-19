"""Kernel-level tests for the shared interactive-control mechanism (W5.1c).

Fakes only: no live browser, no network. The fakes model exactly the surface the
contract names (`check`/`uncheck`/`press_sequentially`/`click`/`is_checked`/
`input_value`), so a test that passes here pins the contract the four vendor
adopters code against, not a vendor's DOM.
"""

from __future__ import annotations

from datetime import date

import pytest

from engine.kernel.contracts import FillSafetyError
from engine.kernel.control_toolkit import (
    ControlKind, ControlOutcome, ControlSpec, drive_control,
)


class _FakeToggle:
    """A checkbox/radio. `swallow` models the silent-no-op failure mode: the
    interaction is accepted but the state never actually changes."""

    def __init__(self, *, checked=False, swallow=False, raises=None):
        self.checked = checked
        self._swallow = swallow
        self._raises = raises
        self.check_calls = 0
        self.uncheck_calls = 0
        self.clicks = 0

    def check(self):
        self.check_calls += 1
        if self._raises is not None:
            raise self._raises
        if not self._swallow:
            self.checked = True

    def uncheck(self):
        self.uncheck_calls += 1
        if not self._swallow:
            self.checked = False

    def click(self):
        self.clicks += 1

    def is_checked(self):
        return bool(self.checked)


class _FakeDateBox:
    """A text date box. Records real keystrokes; `readonly` models the
    picker-only box that swallows typing (content.py's MECHANISM_PICKER_ONLY)."""

    def __init__(self, *, readonly=False, value=""):
        self.value = value
        self._readonly = readonly
        self.clicks = 0

    def click(self):
        self.clicks += 1

    def press_sequentially(self, char, delay=None):
        if not self._readonly:
            self.value += char

    def input_value(self):
        return self.value

    def is_checked(self):
        raise AssertionError("a date box is not a toggle")


class _FakeDayCell:
    """A calendar cell that writes the date into the box it belongs to."""

    def __init__(self, box, text):
        self._box = box
        self._text = text
        self.clicks = 0

    def click(self):
        self.clicks += 1
        self._box.value = self._text


def _spec(**kw):
    kw.setdefault("key", "k")
    kw.setdefault("name", "Field")
    return ControlSpec(**kw)


# -- checkbox / radio ---------------------------------------------------------

def test_checkbox_tick_is_driven_and_readback_confirmed():
    box = _FakeToggle()
    out = drive_control(_spec(kind=ControlKind.CHECKBOX, locator=box, value=True))
    assert box.check_calls == 1
    assert (out.driven, out.confirmed, out.actual) == (True, True, True)
    assert out.reason == ""


def test_checkbox_untick_uses_uncheck_not_a_second_check():
    box = _FakeToggle(checked=True)
    out = drive_control(_spec(kind=ControlKind.CHECKBOX, locator=box, value=False))
    assert (box.uncheck_calls, box.check_calls) == (1, 0)
    assert out.confirmed is True
    assert out.actual is False


def test_checkbox_is_never_driven_by_a_click():
    """`.click()` would TOGGLE, so an already-correct control would be flipped
    out of its intended state. `_settle_focus`'s click settle is excluded here
    for exactly this reason."""
    box = _FakeToggle(checked=True)
    drive_control(_spec(kind=ControlKind.CHECKBOX, locator=box, value=True))
    assert box.clicks == 0
    assert box.checked is True


def test_checkbox_swallowed_tick_is_driven_but_not_confirmed():
    """The failure this engine exists to catch: the page accepted the
    interaction and the value never landed. It must never count as filled."""
    box = _FakeToggle(swallow=True)
    out = drive_control(_spec(kind=ControlKind.CHECKBOX, locator=box, value=True))
    assert (out.driven, out.confirmed) == (True, False)
    assert "readback did not confirm" in out.reason


def test_radio_selects_but_refuses_to_clear():
    box = _FakeToggle()
    assert drive_control(_spec(kind=ControlKind.RADIO, locator=box,
                               value=True)).confirmed is True
    out = drive_control(_spec(kind=ControlKind.RADIO, locator=box, value=False))
    assert (out.driven, out.confirmed) == (False, False)
    assert "another option" in out.reason
    assert box.uncheck_calls == 0


def test_toggle_drive_error_is_fail_soft():
    box = _FakeToggle(raises=RuntimeError("detached"))
    out = drive_control(_spec(kind=ControlKind.CHECKBOX, locator=box, value=True))
    assert (out.driven, out.confirmed) == (False, False)
    assert "drive-error: detached" in out.reason


def test_toggle_without_a_check_method_is_fail_soft():
    out = drive_control(_spec(kind=ControlKind.CHECKBOX, locator=object(),
                              value=True))
    assert out.confirmed is False
    assert "no .check()" in out.reason


def test_toggle_rejects_a_non_bool_intent():
    box = _FakeToggle()
    out = drive_control(_spec(kind=ControlKind.CHECKBOX, locator=box, value="yes"))
    assert out.confirmed is False
    assert "must be a bool" in out.reason
    assert box.check_calls == 0


# -- date: the kernel never formats -------------------------------------------

@pytest.mark.parametrize("value", [date(2026, 9, 1), 20260901, None,
                                   ["01/09/2026"]])
def test_date_refuses_any_value_it_would_have_to_format(value):
    """The load-bearing guard. A `date` object reaching a date control could only
    be typed by CHOOSING an order here, which is the second date path the pinned
    locale work exists to prevent. It raises rather than guesses."""
    box = _FakeDateBox()
    with pytest.raises(FillSafetyError) as exc:
        drive_control(_spec(kind=ControlKind.DATE, locator=box, value=value))
    assert "never formats a date" in str(exc.value)
    assert box.value == ""


def test_date_types_the_rendered_string_verbatim():
    """Whatever order the caller rendered is the order typed: no normalization,
    no reformatting, byte-for-byte."""
    box = _FakeDateBox()
    out = drive_control(_spec(kind=ControlKind.DATE, locator=box,
                              value="01/09/2026"))
    assert box.value == "01/09/2026"
    assert (out.driven, out.confirmed) == (True, True)


def test_date_types_an_ambiguous_order_unchanged():
    """05/08 and 08/05 are six months apart. The kernel is not allowed an
    opinion about which one it was handed."""
    box = _FakeDateBox()
    drive_control(_spec(kind=ControlKind.DATE, locator=box, value="08/05/2026"))
    assert box.value == "08/05/2026"


def test_date_uses_real_keystrokes_not_fill():
    box = _FakeDateBox()
    assert not hasattr(box, "fill")   # type_human must not need it
    drive_control(_spec(kind=ControlKind.DATE, locator=box, value="01/09/2026"))
    assert box.clicks == 1            # type_human's focus settle
    assert box.value == "01/09/2026"


def test_date_empty_string_is_never_a_confirmed_fill():
    box = _FakeDateBox()
    out = drive_control(_spec(kind=ControlKind.DATE, locator=box, value=""))
    assert out.confirmed is False


def test_readonly_box_typed_into_is_not_confirmed():
    """A picker-only box swallows keystrokes. Without a day_cell resolver the
    kernel types, fails the readback, and honestly reports a gap."""
    box = _FakeDateBox(readonly=True)
    out = drive_control(_spec(kind=ControlKind.DATE, locator=box,
                              value="01/09/2026"))
    assert (out.driven, out.confirmed) == (True, False)


# -- date: the picker path ----------------------------------------------------

def test_picker_opens_clicks_the_day_cell_and_confirms():
    box = _FakeDateBox(readonly=True)
    cell = _FakeDayCell(box, "01/09/2026")
    opener = _FakeToggle()
    seen = []
    out = drive_control(_spec(kind=ControlKind.DATE, locator=box,
                              value="01/09/2026", open_handle=opener,
                              day_cell=lambda text: seen.append(text) or cell))
    assert opener.clicks == 1
    assert seen == ["01/09/2026"]      # resolver gets the rendered string as-is
    assert cell.clicks == 1
    assert (out.driven, out.confirmed, out.actual) == (True, True, "01/09/2026")


def test_picker_settles_between_every_step():
    """The calendar is rendered by the widget's own JS after the open click, so
    a cell resolved before a settle would not exist yet."""
    box = _FakeDateBox(readonly=True)
    cell = _FakeDayCell(box, "01/09/2026")
    calls = []
    drive_control(_spec(kind=ControlKind.DATE, locator=box, value="01/09/2026",
                        open_handle=_FakeToggle(), day_cell=lambda _: cell,
                        settle=lambda: calls.append("settle")))
    assert len(calls) == 3            # pre-interaction, post-open, post-click


def test_picker_missing_day_cell_is_fail_soft():
    box = _FakeDateBox(readonly=True)
    out = drive_control(_spec(kind=ControlKind.DATE, locator=box,
                              value="01/09/2026", day_cell=lambda _: None))
    assert (out.driven, out.confirmed) == (False, False)
    assert "no day cell" in out.reason


def test_picker_day_cell_resolver_error_is_fail_soft():
    box = _FakeDateBox(readonly=True)

    def boom(_):
        raise RuntimeError("calendar never opened")

    out = drive_control(_spec(kind=ControlKind.DATE, locator=box,
                              value="01/09/2026", day_cell=boom))
    assert out.confirmed is False
    assert "day-cell-error: calendar never opened" in out.reason


# -- safety -------------------------------------------------------------------

@pytest.mark.parametrize("name", ["Submit application", "Apply now", "Send",
                                  "Finish", "Continue"])
def test_submit_like_name_is_refused_before_the_control_is_touched(name):
    """A checkbox is driven with `.check()`, which would otherwise bypass
    `_safe_click`'s denylist. The guard runs up front, for every kind."""
    box = _FakeToggle()
    with pytest.raises(FillSafetyError) as exc:
        drive_control(_spec(kind=ControlKind.CHECKBOX, locator=box, value=True,
                            name=name))
    assert "submit denylist" in str(exc.value)
    assert box.check_calls == 0
    assert box.checked is False


def test_submit_like_picker_open_handle_is_refused():
    box = _FakeDateBox(readonly=True)
    with pytest.raises(FillSafetyError):
        drive_control(_spec(kind=ControlKind.DATE, locator=box,
                            value="01/09/2026", name="Submit",
                            open_handle=_FakeToggle(),
                            day_cell=lambda _: None))


def test_fill_safety_error_from_a_click_is_never_swallowed():
    """`_safe_click` raising inside the picker path must abort, not degrade into
    a fail-soft skip."""
    box = _FakeDateBox(readonly=True)

    class _DenylistedCell:
        def click(self):
            raise AssertionError("must not be reached")

    spec = _spec(kind=ControlKind.DATE, locator=box, value="01/09/2026",
                 name="Continue", day_cell=lambda _: _DenylistedCell())
    with pytest.raises(FillSafetyError):
        drive_control(spec)


def test_unknown_kind_raises_rather_than_guessing_a_drive_path():
    with pytest.raises(ValueError) as exc:
        drive_control(_spec(kind="combobox", locator=_FakeToggle(), value=True))
    assert "unknown control kind" in str(exc.value)


# -- settle / readback discipline ---------------------------------------------

def test_settle_runs_before_the_first_interaction():
    order = []
    box = _FakeToggle()
    original = box.check

    def tracked():
        order.append("check")
        original()

    box.check = tracked
    drive_control(_spec(kind=ControlKind.CHECKBOX, locator=box, value=True,
                        settle=lambda: order.append("settle")))
    assert order[0] == "settle"


def test_a_raising_settle_never_fails_the_drive():
    box = _FakeToggle()

    def boom():
        raise RuntimeError("networkidle timed out")

    out = drive_control(_spec(kind=ControlKind.CHECKBOX, locator=box, value=True,
                              settle=boom))
    assert out.confirmed is True


def test_a_raising_readback_is_not_confirmed():
    """Never-confirmed bias: an unreadable control is a gap, not a pass."""

    class _Unreadable(_FakeToggle):
        def is_checked(self):
            raise RuntimeError("detached")

    out = drive_control(_spec(kind=ControlKind.CHECKBOX, locator=_Unreadable(),
                              value=True))
    assert (out.driven, out.confirmed) == (True, False)
    assert "readback-error" in out.reason


def test_outcome_echoes_key_and_kind_for_report_folding():
    out = drive_control(_spec(key="work_auth", kind=ControlKind.CHECKBOX,
                              locator=_FakeToggle(), value=True))
    assert isinstance(out, ControlOutcome)
    assert (out.key, out.kind) == ("work_auth", ControlKind.CHECKBOX)
