"""Generic, vendor-agnostic form-driving primitives (W5.1 kernel)."""

from __future__ import annotations

import random
import re


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
