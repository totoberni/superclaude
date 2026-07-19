"""Vendor-agnostic interactive-control driving (W5.1c kernel).

ONE mechanism drives every checkbox, radio and date control, for all four
vendors. The contract is stated in terms of what the CONTROL IS (its kind and
its intended value) plus the handles the VENDOR supplies (the control's own
locator, and for a calendar widget an open handle and a day-cell resolver).
Nothing here knows a vendor name, a selector, or a DOM shape: an adopter that
needs to special-case its portal does it by choosing what it passes IN, never
by this module growing a branch.

Three properties this module deliberately does NOT own, because the kernel
already owns them elsewhere and a second copy would drift:

- READBACK. Every drive is confirmed through `fill_toolkit._readback`, the same
  primitive the text/upload paths use. Never-confirmed bias throughout, mirroring
  `_upload_attached`: a control this code cannot positively read back (no
  readback method, a raising readback, a value that does not match intent) is
  reported `confirmed=False`, so a tick the page silently swallowed can never be
  counted as filled. Callers fold a `confirmed=False` outcome into their own
  `FillReport.required_unfilled` exactly as they already do for an unconfirmed
  text fill.

- THE SUBMIT DENYLIST. Every click routes through `fill_toolkit._safe_click`, the
  module's sole sanctioned click primitive, so a submit stays structurally
  impossible on this path too.

- DATE FORMATTING. See `_drive_date`: this module refuses to format a date at
  all, on purpose.

Policy vs mechanism. `fill_toolkit._needs_human_handoff` decides WHETHER a given
checkbox/radio is driven programmatically or handed to a human (a programmatic
tick is the anti-bot hazard vendors defer on). That policy is untouched and stays
where it is. This module is only the HOW, for controls a caller has already
decided to drive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from engine.kernel.contracts import FillSafetyError
from engine.kernel.fill_toolkit import (
    _CLICK_DENYLIST, _readback, _safe_click, type_human,
)


class ControlKind:
    """What a control IS. The kind, not the vendor, selects the drive path."""

    CHECKBOX = "checkbox"
    RADIO = "radio"
    DATE = "date"


_KINDS = frozenset({ControlKind.CHECKBOX, ControlKind.RADIO, ControlKind.DATE})


@dataclass
class ControlSpec:
    """One control to drive, described semantically plus vendor-supplied handles.

    `key`/`name`: the field key (identity, echoed on the outcome) and the
    control's ACCESSIBLE NAME. `name` is what the submit denylist is checked
    against, exactly as `_safe_upload` passes `fv.locator.name or fv.label`, so
    an adopter must pass the real accessible name rather than leave it blank.

    `locator`: the control's own handle. For CHECKBOX/RADIO the tickable input;
    for DATE the box whose value is read back. Vendor-supplied and never
    constructed here.

    `value`: the INTENT. A bool for CHECKBOX (True ticks, False unticks) and for
    RADIO (True only; see `_drive_toggle`). For DATE, an ALREADY-RENDERED string
    and nothing else (see `_drive_date`).

    `day_cell`: DATE only, and the sole switch between the two date paths. None
    means the box is typed into (`type_human`). A callable means the box is
    click-only (content.py's `MECHANISM_PICKER_ONLY`): it is called with the same
    rendered date string and returns the locator of the calendar cell to click,
    or None when the widget shows no such day. The vendor owns that lookup because
    only the vendor knows its calendar's DOM.

    `open_handle`: DATE picker only. The control clicked to open the calendar,
    when opening is a separate step from the box itself. None skips the open.

    `settle`: the caller's own page-settle, called before the first interaction
    (SPA hydration) and again between every step of a multi-step drive. Never
    required and never allowed to fail the drive (see `_settle`). The kernel
    supplies no default because a page-waiting policy is the caller's, not the
    kernel's.
    """

    key: str
    kind: str
    locator: Any
    value: bool | str
    name: str = ""
    day_cell: Callable[[str], Any] | None = None
    open_handle: Any = None
    settle: Callable[[], None] | None = None


@dataclass
class ControlOutcome:
    """The evidence of one drive.

    `driven` says the interaction was performed; `confirmed` says the readback
    proved it landed. Only `confirmed` may count toward `FillReport.filled`:
    `driven=True, confirmed=False` is precisely the silent-no-op failure mode
    this engine exists to catch, and it carries `reason`.
    """

    key: str
    kind: str
    driven: bool
    confirmed: bool
    actual: Any = None
    reason: str = ""


def drive_control(spec: ControlSpec) -> ControlOutcome:
    """Drive one control to its intended value and readback-confirm it.

    Raises `FillSafetyError` (never swallowed, aborts the fill) when the control's
    name matches the submit denylist, or when a DATE value is not an
    already-rendered string. Raises `ValueError` for an unknown `kind` (a caller
    bug, not a page condition). Every other failure is fail-soft, per-control:
    it returns an outcome with `confirmed=False` and a `reason`, mirroring the
    per-field skip discipline in `_fill_upload`.
    """
    if spec.kind not in _KINDS:
        raise ValueError(
            f"unknown control kind {spec.kind!r} for {spec.key!r}: "
            f"expected one of {sorted(_KINDS)}")
    _guard_name(spec)
    _settle(spec)
    if spec.kind == ControlKind.DATE:
        return _drive_date(spec)
    return _drive_toggle(spec)


def _guard_name(spec: ControlSpec) -> None:
    """Refuse any control whose accessible name is submit-like, BEFORE touching it.

    `_safe_click` already guards the click paths, but a checkbox is driven with
    `.check()` rather than `.click()`, which would otherwise slip past the sole
    sanctioned click primitive. Same denylist constant, checked once up front, so
    both drive paths are covered by one rule.
    """
    if _CLICK_DENYLIST.search(spec.name or ""):
        raise FillSafetyError(
            f"refusing to drive control {spec.key!r} named {spec.name!r}: matches "
            "the submit denylist (submit/apply/send/finish/continue); this dry "
            "run STOPS SHORT OF APPLYING")


def _drive_toggle(spec: ControlSpec) -> ControlOutcome:
    """Tick or untick a checkbox/radio via the control's own `.check()`/
    `.uncheck()`.

    `.check()` rather than `.click()` on purpose: it is actionability-aware (it
    waits for the control to be visible, stable and able to receive events, which
    is this path's hydration settle) and it is idempotent, so a control already in
    the intended state is not toggled OUT of it. `_settle_focus` is deliberately
    NOT reused here: it settles by CLICKING, which on a checkbox would flip the
    very value being set.
    """
    desired = spec.value
    if not isinstance(desired, bool):
        return _skip(spec, f"{spec.kind} value must be a bool, got "
                           f"{type(desired).__name__}")
    if spec.kind == ControlKind.RADIO and not desired:
        return _skip(spec, "a radio is deselected only by selecting another "
                           "option in its group, never by clearing this one")
    verb = "check" if desired else "uncheck"
    driver = getattr(spec.locator, verb, None)
    if not callable(driver):
        return _skip(spec, f"control exposes no .{verb}()")
    try:
        driver()
    except Exception as exc:
        return _skip(spec, f"drive-error: {exc}")
    _settle(spec)
    return _confirm(spec, desired)


def _drive_date(spec: ControlSpec) -> ControlOutcome:
    """Put an ALREADY-RENDERED date string into a date control.

    THE KERNEL NEVER FORMATS A DATE. `value` must already be the exact text the
    control wants, rendered upstream by the ONE pinned path that owns date order:
    `engine.content` derives the control's format from its own DOM
    (`date_format_from_placeholder`), validates it (`is_supported_date_format`)
    and renders the string (`_start_date_verdict`), and the browser session that
    format was observed in is itself pinned to one locale and timezone
    (`capture_toolkit.BROWSER_LOCALE` / `BROWSER_TIMEZONE_ID`). This module is
    below `engine.content` in the import graph (content imports the kernel), so it
    could not reuse those helpers even if it wanted to, and reimplementing them
    here is exactly the drift that pinning was introduced to kill: 05/08 and 08/05
    are six months apart and both type and read back cleanly.

    Hence a non-string `value` (a `date`, a `datetime`, an int) raises
    `FillSafetyError` rather than being formatted with a guessed order. There is
    no format argument on `ControlSpec` for the same reason: a format the kernel
    cannot validate is a format the kernel must not act on. An empty or
    whitespace-only string is not a safety error but is never a confirmed fill
    (`_readback` refuses a blank intent), so it surfaces as a required gap.
    """
    text = spec.value
    if not isinstance(text, str):
        raise FillSafetyError(
            f"refusing to drive date control {spec.key!r} from a "
            f"{type(text).__name__}: this kernel never formats a date, it types "
            "an already-rendered string. Render it upstream via the pinned "
            "engine.content date path (date_format_from_placeholder / "
            "is_supported_date_format) and pass the resulting str")
    if spec.day_cell is not None:
        return _drive_date_picker(spec, text)
    return _drive_date_text(spec, text)


def _drive_date_text(spec: ControlSpec, text: str) -> ControlOutcome:
    """Type the rendered date with the shared human-cadence typist.

    `type_human` (not `locator.fill()`, not JS): the anti-bot rationale and the
    focus settle that stopped the first character being dropped both live there,
    and a date box is no more exempt from either than any other text field.
    """
    try:
        type_human(spec.locator, text)
    except Exception as exc:
        return _skip(spec, f"drive-error: {exc}")
    _settle(spec)
    return _confirm(spec, text)


def _drive_date_picker(spec: ControlSpec, text: str) -> ControlOutcome:
    """Open the vendor's calendar, click the day cell it resolves, confirm.

    The multi-step shape content.py named `MECHANISM_PICKER_ONLY` and refused to
    fill (`PICKER_NOT_DRIVEN`, "W5.1c click-policy wave"): a readonly box only a
    click can set. Both clicks route through `_safe_click`, and `settle` runs
    between every step because the calendar is rendered by the widget's own JS
    after the open click, so the cell does not exist at the moment the open
    returns.
    """
    if spec.open_handle is not None:
        try:
            _safe_click(spec.open_handle, spec.name)
        except FillSafetyError:
            raise
        except Exception as exc:
            return _skip(spec, f"picker-open-error: {exc}")
        _settle(spec)
    try:
        cell = spec.day_cell(text)
    except Exception as exc:
        return _skip(spec, f"day-cell-error: {exc}")
    if cell is None:
        return _skip(spec, f"picker shows no day cell for {text!r}")
    try:
        _safe_click(cell, spec.name)
    except FillSafetyError:
        raise
    except Exception as exc:
        return _skip(spec, f"picker-click-error: {exc}")
    _settle(spec)
    return _confirm(spec, text)


def _confirm(spec: ControlSpec, intended) -> ControlOutcome:
    """Read the control back through the shared `_readback` and grade the drive.

    Never-confirmed bias: a readback that raises is `confirmed=False`, not an
    optimistic pass. `driven=True` regardless, because the interaction did happen
    and the caller needs to know that the page was touched.
    """
    try:
        actual, ok = _readback(spec.locator, intended)
    except Exception as exc:
        return ControlOutcome(key=spec.key, kind=spec.kind, driven=True,
                              confirmed=False, reason=f"readback-error: {exc}")
    return ControlOutcome(
        key=spec.key, kind=spec.kind, driven=True, confirmed=bool(ok),
        actual=actual,
        reason="" if ok else "readback did not confirm the value")


def _skip(spec: ControlSpec, reason: str) -> ControlOutcome:
    return ControlOutcome(key=spec.key, kind=spec.kind, driven=False,
                          confirmed=False, reason=reason)


def _settle(spec: ControlSpec) -> None:
    """Best-effort caller settle: never raises, exactly like `_settle_focus`.

    A settle is a nudge for the page's own render queue to flush, never a
    precondition: a fake with no settle, or a networkidle that times out under a
    chatty page, must not fail a drive whose readback is the real gate.
    """
    if spec.settle is None:
        return
    try:
        spec.settle()
    except Exception:
        pass
