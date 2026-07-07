"""Shared browser primitives every ATS provider builds on (W5.1 spine).

This module is the single home for the cross-vendor fill mechanics that the
per-vendor providers (greenhouse/lever/ashby/workable, landing in W5.2) reuse:
the four live fill primitives from `engine.fill`, a STRUCTURAL never-send network
interceptor, human-cadence typing, a DOM-sweep completeness check, and the
react-select combobox driver that W4 deferred.

LAZY-IMPORT INVARIANT (load-bearing, mirrors registry.py): the daily poller must
never load the browser stack. `engine.run` imports `engine.providers` (the package
__init__, which pulls in `registry` only), NOT this module, and this module never
imports patchright or `engine.browse` at load time. Every browser reference here is
resolved through the page/locator/route objects the caller passes in; the only
cross-module import (`engine.fill`, itself patchright-free) happens at CALL time
inside the re-export wrappers.

FILL-PRIMITIVE ACCESS -- re-export via call-time wrappers, NOT a top-level
`from engine.fill import ...` and NOT a code move out of fill.py:
- No code is moved out of the live W4 `fill.py` (the running jobhunt fills through
  it); a move would be a needless risk. The primitives stay where `fill_form` uses
  them and are surfaced here by thin pass-through wrappers.
- The wrappers look the target up on the `engine.fill` module object at call time,
  so they honour the monkeypatch seam (a test patching `engine.fill._safe_click`
  is reflected here) exactly as `registry.py` looks up `browse.capture_ashby` at
  call time. A top-level `from engine.fill import _safe_click` would bind the
  reference at import and defeat that seam.
- Importing `engine.fill` lazily (inside the wrappers) also keeps this module's own
  import cheap and dodges any import-order fragility with the providers package.

The NEW primitives (`install_never_send`, `type_human`, `sweep_required` +
`completeness_mismatch`, `select_react_combobox`) are pure-Python here: they drive
whatever page/locator/route object is handed to them, so their branching logic is
unit-tested now with fakes and their live-DOM behaviour is fixture-validated in
W5.2.
"""

from __future__ import annotations

import json
import random
import re
import time
import urllib.parse
from pathlib import Path

# never-send guard: FROZEN, single-source in the kernel (W5.1). Re-exported for back-compat.
from engine.kernel.never_send import (  # noqa: F401
    _SUBMIT_URL_PATTERNS, _SUBMIT_GRAPHQL_URL_PATTERNS, _SUBMIT_OPERATION_RE, _GRAPHQL_MUTATION_RE,
    _graphql_operation_names, _url_op_params, _all_ops_carry_inline_query, _graphql_submit_match,
    _is_submit_request, _never_send_handler, _request_post_data, install_never_send, _route_target,
)

# -- re-exported fill primitives (call-time lookup preserves the patch seam) ----


def _fill():
    """The live `engine.fill` module, imported lazily so this module stays cheap
    to load and the reference is resolved fresh on every call (patch seam)."""
    from engine import fill
    return fill


def _safe_click(*args, **kwargs):
    """Re-export of `engine.fill._safe_click` (the sole sanctioned click gateway;
    refuses any submit-like accessible name)."""
    return _fill()._safe_click(*args, **kwargs)


def _safe_upload(*args, **kwargs):
    """Re-export of `engine.fill._safe_upload` (whitelisted-asset attach; never
    submits)."""
    return _fill()._safe_upload(*args, **kwargs)


def _readback(*args, **kwargs):
    """Re-export of `engine.fill._readback` (reads a control back to confirm a
    value actually landed)."""
    return _fill()._readback(*args, **kwargs)


def _locate(*args, **kwargs):
    """Re-export of `engine.fill._locate` (role/label locator resolution)."""
    return _fill()._locate(*args, **kwargs)


# -- STRUCTURAL never-send (HOLE-FIX a): moved to engine.kernel.never_send, frozen
# byte-identical (W5.1 stage 0). Re-exported below for back-compat.


# -- human-cadence typing (reCAPTCHA v3 score protection) ----------------------


def type_human(locator, text, *, min_delay: float = 60, max_delay: float = 180):
    """Type `text` into `locator` one real keystroke at a time, random per-char delay.

    Uses `press_sequentially` (genuine key events) with a fresh random inter-key
    delay in `[min_delay, max_delay]` ms per character. NEVER `locator.fill()` and
    NEVER any JS injection: reCAPTCHA v3 scores an instant value-set as bot-like,
    so the whole point is a human keystroke cadence. `locator` is injected by the
    caller (a real Playwright locator live; a fake in tests).

    A live Greenhouse run showed the FIRST character silently dropped
    (`first_name` "Federico" landed as "ederico" in the post-fill DOM): typing
    started before the control was focus-ready, so the leading keydown raced
    the control's own focus-in handling and never registered. `_settle_focus`
    clicks the control first (a real Playwright click already waits for
    actionability), so every text field -- not just first_name -- is focused
    and settled before any keystroke is sent."""
    if not text:
        return
    _settle_focus(locator)
    for char in str(text):
        delay = random.uniform(min_delay, max_delay)
        locator.press_sequentially(char, delay=delay)


def _settle_focus(locator) -> None:
    """Click `locator` to force focus to land and settle before typing.

    Never raises: a fake/partial locator with no `.click()` (or one whose
    click fails) still falls through to typing rather than crashing the
    fill -- this is a best-effort settle, not a hard precondition."""
    clicker = getattr(locator, "click", None)
    if not callable(clicker):
        return
    try:
        clicker()
    except Exception:
        pass


# -- DOM-sweep completeness (HOLE-FIX d) ---------------------------------------
# A form is COMPLETE only when the DOM's required-field set and the schema's
# required-field set agree. Lever carries no custom-question schema at all, so the
# DOM sweep is the sole completeness oracle there; for the schema vendors it is a
# cross-check that the schema did not miss a field the page actually requires.

_REQUIRED_CSS = "[required], [aria-required='true']"
_ASTERISK_CSS = "label, legend"


def _normalize_name(text) -> str:
    """Lowercase, strip `*` required-markers, and collapse whitespace to a stable
    accessible-name key so DOM and schema names compare apples-to-apples."""
    if not text:
        return ""
    cleaned = str(text).replace("*", " ")
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def completeness_mismatch(schema_required: set[str],
                          dom_required: set[str]) -> dict:
    """Two-directional diff of the required-field sets (normalized both sides).

    Returns {"dom_only": [...], "schema_only": [...]} sorted for determinism.
    `dom_only` = required on the page but absent from the schema (the schema
    missed a field); `schema_only` = required by the schema but not found on the
    page. Any non-empty side means the form is NOT_COMPLETE.
    """
    schema = {_normalize_name(name) for name in (schema_required or set())} - {""}
    dom = {_normalize_name(name) for name in (dom_required or set())} - {""}
    return {
        "dom_only": sorted(dom - schema),
        "schema_only": sorted(schema - dom),
    }


def sweep_required(page) -> set[str]:
    """Enumerate the page's visible required-looking controls -> normalized names.

    Collects controls carrying `required` / `aria-required="true"` plus fields whose
    label/legend shows a visible asterisk, skipping aria-hidden / offscreen nodes,
    and returns their normalized accessible-name set. The live-DOM extraction is
    fixture-validated in W5.2; the normalization + diff logic (`_normalize_name`,
    `completeness_mismatch`) is unit-tested now.
    """
    names: set[str] = set()
    for locator in _visible_locators(page, _REQUIRED_CSS):
        name = _normalize_name(_accessible_name(locator))
        if name:
            names.add(name)
    for locator in _visible_locators(page, _ASTERISK_CSS):
        text = _locator_text(locator)
        if text and "*" in text:
            name = _normalize_name(text)
            if name:
                names.add(name)
    return names


def _visible_locators(page, css: str) -> list:
    """All locators matching `css` that are visible and not aria-hidden.

    Guarded end to end: a page/locator missing a probed method is treated as
    zero matches rather than raising, so a partial fake never crashes the sweep."""
    locator_fn = getattr(page, "locator", None)
    if locator_fn is None:
        return []
    try:
        candidates = locator_fn(css).all()
    except Exception:
        return []
    visible: list = []
    for locator in candidates or []:
        if _is_visible(locator) and not _is_aria_hidden(locator):
            visible.append(locator)
    return visible


def _is_visible(locator) -> bool:
    checker = getattr(locator, "is_visible", None)
    if checker is None:
        return True
    try:
        return bool(checker())
    except Exception:
        return False


def _is_aria_hidden(locator) -> bool:
    try:
        return (locator.get_attribute("aria-hidden") or "").strip().lower() == "true"
    except Exception:
        return False


def _accessible_name(locator) -> str:
    """Best-effort accessible name: aria-label, then label text, then placeholder,
    then the control's own name attribute (live-DOM refinement is W5.2's job)."""
    for attr in ("aria-label", "placeholder", "name"):
        try:
            value = (locator.get_attribute(attr) or "").strip()
        except Exception:
            value = ""
        if value:
            return value
    return _locator_text(locator)


def _locator_text(locator) -> str:
    getter = getattr(locator, "inner_text", None) or getattr(locator, "text_content", None)
    if getter is None:
        return ""
    try:
        return (getter() or "").strip()
    except Exception:
        return ""


# -- react-select combobox driver (Greenhouse; W4-deferred) --------------------
# LIVE-DOM FIX #1 (2026-07-06, gitlab/8503792002 acceptance run): the driver used
# to click `#react-select-{field_id}-input` to open the widget -- that id DOES
# NOT EXIST on Greenhouse's react-select v5 markup, so every combobox timed out
# before a single option could be picked. The ids Greenhouse's live DOM DOES
# confirm are prefixed `react-select-{field_id}-` (e.g. `-placeholder`,
# `-live-region`); the control itself is `div.select__control` (a build-hashed
# class suffix, e.g. `remix-css-13cymwt-control`, so never matched by a full
# class-string equality), containing `div.select__value-container` >
# `div.select__placeholder` (the confirmed `-placeholder` id) and
# `div.select__input-container` > the control's own `<input>`. The driver below
# anchors on the CONFIRMED placeholder id (via Playwright's `:has()` CSS
# extension) to reach the control.
#
# LIVE-DOM FIX #2 (2026-07-06, same acceptance run, re-run after fix #1): even
# with the control correctly reached, clicking the rendered `div.select__option`
# left `.select__single-value` empty on every one of the 4 comboboxes (all read
# back "value did not take"). Clicking a react-select option div is unreliable
# under Playwright/Patchright: the click can land before the option's own click
# handler is wired, or race the menu's re-render after each filter keystroke.
# The robust, well-known react-select pattern is TYPE-TO-FILTER then ENTER --
# react-select keeps the first filtered row highlighted and commits it on
# `Enter`, exactly like a human using the widget with the keyboard alone (never
# `Escape` first, which would just close the menu with no selection). The
# driver now presses `Enter` on the control's own input instead of clicking any
# `div.select__option`, so the menu-lookup/option-click step is gone entirely.


def _combobox_control_selector(field_id: str) -> str:
    """CSS for the field's react-select control div, scoped by the per-field
    `-live-region` id, which PERSISTS across selection (unlike `-placeholder`,
    which unmounts the moment a value is picked, so a placeholder-anchored
    scope silently stops matching the control post-commit and the readback
    reads empty). The live-region node is a DIRECT child of the react-select
    container alongside `.select__control`, so anchoring on it reaches the
    control in BOTH the empty and the selected state. Live-DOM verified."""
    return (f'div:has(> [id="react-select-{field_id}-live-region"]) '
            f'div.select__control')


def select_react_combobox(page, field_id: str, option_text: str, *,
                          min_delay: float = 60, max_delay: float = 180,
                          poll_ms: tuple[int, ...] = (200, 500)) -> bool:
    """Drive one react-select combobox: open, filter, commit, and confirm.

    Sequence (fresh locators at every step; react-select recycles nodes):
      1. Click the field's control (scoped via `_combobox_control_selector`,
         anchored on the confirmed `-placeholder` id) to open the menu.
      2. `type_human` the option text into the control's own input to filter
         the menu (never fill()) -- also the long-country-list path.
      3. Press `Enter` on that same input: react-select commits the
         highlighted (first-filtered) option itself -- never a `div.select__
         option` click, which does not reliably commit (see LIVE-DOM FIX #2
         above).
      4. Poll `.select__single-value` (scoped to the field's control) at
         +200/+500 ms to confirm the value landed.
      5. Dismiss with Escape -- harmless (react-select already closed the menu
         on Enter-commit) but a safe no-op net for the case Enter had nothing
         to commit (e.g. no option matched the filter). NEVER blur (a blur can
         re-open / clear the widget).

    Returns True iff the readback confirms the selection landed.
    """
    control = _combobox_control(page, field_id)
    control.click()
    combo_input = _combobox_input(page, field_id)
    type_human(combo_input, option_text, min_delay=min_delay, max_delay=max_delay)

    # Commit via a FOCUS-FOLLOWING keyboard Enter, not the filter input's own
    # press: react-select re-renders (detaches) the filter input on each
    # keystroke, so `combo_input.press("Enter")` hangs on Playwright's
    # actionability wait for a now-stale node. The live input still holds
    # focus, so the page keyboard commits the highlighted first-filtered option
    # reliably. Live-DOM verified. Falls back to the locator's own press for
    # the offline fake harness (no `page.keyboard`).
    _keyboard_press(page, combo_input, "Enter")

    landed = _poll_single_value(page, field_id, option_text, poll_ms)
    # Dismiss the still-open menu (a no-op after an Enter-commit) without a blur.
    _keyboard_press(page, combo_input, "Escape")
    return landed


def _keyboard_press(page, locator, key: str) -> None:
    """Press `key` on the PAGE keyboard (focus-following, so it survives react-
    select re-rendering/detaching its filter input mid-interaction) when the
    page exposes one; otherwise fall back to the locator's own `press` (the
    offline fake-harness path, which has no `page.keyboard`)."""
    keyboard = getattr(page, "keyboard", None)
    presser = getattr(keyboard, "press", None) if keyboard is not None else None
    if callable(presser):
        presser(key)
        return
    locator_press = getattr(locator, "press", None)
    if callable(locator_press):
        locator_press(key)


def _combobox_control(page, field_id: str):
    """A FRESH locator for the field's react-select control div."""
    return page.locator(_combobox_control_selector(field_id))


def _combobox_input(page, field_id: str):
    """A FRESH locator for the control's own text input (there is exactly one
    per control; react-select recycles the node, so this is re-resolved on
    every call rather than cached)."""
    return _combobox_control(page, field_id).locator("input")


def _poll_single_value(page, field_id: str, option_text: str,
                       poll_ms: tuple[int, ...]) -> bool:
    """Poll the rendered `.select__single-value` at the given cumulative offsets.

    Returns True as soon as the shown value contains the chosen option text. Waits
    are the cumulative deltas so `(200, 500)` reads at +200 ms then +500 ms."""
    want = _normalize_name(option_text)
    if not want:
        return False
    elapsed = 0
    for mark in poll_ms:
        _wait_timeout(page, mark - elapsed)
        elapsed = mark
        shown = _normalize_name(_single_value_text(page, field_id))
        if shown and want in shown:
            return True
    return False


def _single_value_text(page, field_id: str) -> str:
    """The field's currently-shown `.select__single-value` text, scoped to
    its own control. A control that no longer matches the `-placeholder`-
    anchored scope (e.g. the placeholder unmounts once a value is selected --
    UNVERIFIED live, flagged for the owner's live iteration) degrades to a
    fast empty read via `.count()` rather than hanging on Playwright's
    default actionability wait for a selector that will never resolve."""
    locator_fn = getattr(page, "locator", None)
    if locator_fn is None:
        return ""
    try:
        single_value = _combobox_control(page, field_id).locator(
            ".select__single-value")
        counter = getattr(single_value, "count", None)
        if callable(counter) and counter() == 0:
            return ""
        return _locator_text(single_value)
    except Exception:
        return ""


def _wait_timeout(page, ms: int) -> None:
    if ms <= 0:
        return
    waiter = getattr(page, "wait_for_timeout", None)
    if callable(waiter):
        waiter(ms)
    else:
        time.sleep(ms / 1000.0)


# -- upload rendered-confirmation poll (Greenhouse resume-upload false ---------
# positive fix, 2026-07-06 gitlab/8503792002 acceptance run, HOSTILE REVIEW #1)
#
# `engine.fill._upload_attached`'s `el.files.length >= 1` check is NECESSARY
# but NOT SUFFICIENT for Greenhouse: a live probe showed the engine's own
# upload path (an ElementHandle captured once via `query_selector_all`,
# fixed in `engine.fill._locate_file_input`/`_file_input_control`) left the
# file genuinely sitting in the native input's FileList while Greenhouse's
# React-driven widget never rendered it -- still the empty "Attach"/"Enter
# manually" placeholder, no filename, no remove control. A DIRECT
# `page.locator('input[type=file]#resume').set_input_files(cv)` on the SAME
# input DID make the widget render the uploaded filename AND a remove
# control, proving Greenhouse keys its own confirmed-attached UI off actually
# receiving the change through React, never off the native FileList alone.
#
# `poll_upload_confirmed` is the truthful signal this gap needs: it polls the
# file input's own immediate container (its parent element -- Greenhouse
# renders the filename text and the remove control as SIBLINGS of the native
# input inside that shared parent in every layout observed so far) for
# EITHER the uploaded file's basename appearing in the container's text, OR a
# visible remove/delete control. Best-effort selector, UNVERIFIED against
# every possible Greenhouse theme -- same caveat as `_single_value_text`
# above, flagged for the owner's live iteration.

_REMOVE_CONTROL_NAME_RE = re.compile(r"remove|delete|clear", re.I)


def poll_upload_confirmed(
        page, control, filename: str, *,
        poll_ms: tuple[int, ...] = (500, 1500, 3000, 5000, 7500, 10000,
                                    12500, 15000)) -> bool:
    """Poll `_upload_widget_confirmed` at the given cumulative offsets
    (mirrors `_poll_single_value`'s pattern): True as soon as it confirms, up
    to ~15s total. LIVE-DOM finding (2026-07-06 gitlab full-fill): greenhouse's
    React re-render of the attached-file widget completes within ~1s when the
    page is IDLE (an isolated upload) but takes several seconds MID-FILL (the
    React queue is busy processing the other field updates), so the original
    ~7.5s window still reported a structural FALSE NEGATIVE under a busy
    full-fill load -- the CV was genuinely attached and rendered by end-of-fill
    (get_by_text(stem) count 1) yet the too-short poll missed it, reporting
    the required upload as unfilled. The window is extended to ~15s
    cumulative (FIX 2, same finding) AND `_settle_event_loop` is given a
    chance to nudge the page's own event loop / render queue BETWEEN every
    poll, so a genuinely-attached file's render is caught rather than missed
    even when React is busy driving the rest of a full fill. Returns as soon
    as it confirms, so the longer tail only costs time on a genuine
    non-attach. Never raises: any DOM-query failure along the way reads as
    NOT confirmed (never-attached bias, mirroring `_upload_attached`'s own
    philosophy) -- `_settle_event_loop` can only help a positive confirm land
    sooner, it can never manufacture one, so a genuinely non-attached file
    still exhausts the full window and returns False."""
    elapsed = 0
    raw_stem = Path(filename).stem if filename else ""
    for mark in poll_ms:
        _wait_timeout(page, mark - elapsed)
        elapsed = mark
        _settle_event_loop(page)
        if _upload_widget_confirmed(control, filename) or _page_shows_filename(page, raw_stem):
            return True
    return False


def _settle_event_loop(page) -> None:
    """Best-effort nudge for the page's own JS event loop / React render
    queue to flush BETWEEN polls (FIX 2, 2026-07-06 gitlab/8503792002
    full-fill run): tries `page.wait_for_load_state("networkidle")` first
    (Playwright's own idle signal -- yields until no in-flight network
    activity), falling back to a cheap `page.evaluate("1")` round-trip
    (forces a JS event-loop tick) when networkidle is unavailable or raises.
    Never raises and never blocks the poll on a genuinely non-attached file:
    any failure here is swallowed and the poll simply proceeds to its own DOM
    check on schedule."""
    waiter = getattr(page, "wait_for_load_state", None)
    if callable(waiter):
        try:
            waiter("networkidle")
            return
        except Exception:
            pass
    evaluator = getattr(page, "evaluate", None)
    if callable(evaluator):
        try:
            evaluator("1")
        except Exception:
            pass


def _page_shows_filename(page, raw_stem: str) -> bool:
    """Positive page-scoped confirmation the vendor RENDERED the attached
    file's name. Greenhouse renders the filename / remove control OUTSIDE the
    input's immediate parent (a sibling widget node), so the container-only
    `_upload_widget_confirmed` (xpath=.. one level up) misses it. The file stem
    is a distinctive name (never job-posting prose), so a page-wide text match
    is a reliable positive signal. Live-verified 2026-07-06 gitlab/8503792002:
    a genuine attach makes `get_by_text(stem)` count>0; a non-attach does not."""
    if not raw_stem:
        return False
    getter = getattr(page, "get_by_text", None)
    if not callable(getter):
        return False
    try:
        loc = getter(raw_stem, exact=False)
        counter = getattr(loc, "count", None)
        return bool(callable(counter) and counter() > 0)
    except Exception:
        return False


def _upload_widget_confirmed(control, filename: str) -> bool:
    """One-shot DOM check: True iff the file input's own widget container
    shows the uploaded file's name and/or a remove/delete control. Never
    raises: a control/container missing a probed method, or any DOM query
    failing, reads as NOT confirmed."""
    container = _upload_widget_container(control)
    if container is None:
        return False
    stem = _normalize_name(Path(filename).stem) if filename else ""
    text = _normalize_name(_locator_text(container))
    if stem and stem in text:
        return True
    return _has_remove_control(container)


def _upload_widget_container(control):
    """The file input's own immediate container (its parent element), scoped
    via `xpath=..`. None when the control exposes no `.locator` (a fixture/
    fake not modelling one, or a bare ElementHandle fallback with no
    container reachable this way)."""
    locator_fn = getattr(control, "locator", None)
    if not callable(locator_fn):
        return None
    try:
        return locator_fn("xpath=..")
    except Exception:
        return None


def _has_remove_control(container) -> bool:
    getter = getattr(container, "get_by_role", None)
    if not callable(getter):
        return False
    for role in ("button", "link"):
        try:
            candidate = getter(role, name=_REMOVE_CONTROL_NAME_RE)
            counter = getattr(candidate, "count", None)
            if callable(counter) and counter() > 0:
                return True
        except Exception:
            continue
    return False
