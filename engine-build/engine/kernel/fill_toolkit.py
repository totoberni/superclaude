"""Generic, vendor-agnostic form-driving primitives (W5.1 kernel)."""

from __future__ import annotations

import random
import re
from typing import Callable

from engine.kernel.contracts import FieldValue, FillAssets, FillSafetyError


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


# -- fill.py closure: upload/click/locate primitives (W5.1 kernel step 4b) ----
# Moved verbatim from engine.fill (W5.1 kernel extraction). engine.fill
# re-exports every name below via a shim import so existing callers
# (fill.py internals, tests, the providers.base delegating wrappers) resolve
# them unchanged via `fill.X`.

# -- file-input location (criterion: CV upload on Greenhouse + Lever) ----------
# The fieldmap locator for an upload field is a best-effort role=button hint from
# the questions API, which does NOT reach the real <input type=file>. The file
# input is located directly on the page instead: by a key stem in its id/name,
# else by its `accept` MIME family (document for a CV, image for a photo).
_KEY_STOPWORDS = frozenset({
    "job", "application", "answers", "attributes", "field", "fields",
    "input", "the", "your", "form", "value", "name", "file", "upload",
    "attach", "document", "documents",
})
_DOC_ACCEPT_TOKENS = ("pdf", "doc", "rtf", "txt", "msword",
                      "wordprocessing", "text/", "officedocument")
_IMAGE_ACCEPT_TOKENS = ("image", "png", "jpg", "jpeg", "gif", "webp",
                        "heic", "svg", "bmp", "tiff")

# Click-name denylist: the SINGLE guard that makes a submit structurally
# impossible. Any element whose accessible name matches is refused by
# `_safe_click`, the module's only sanctioned click primitive.
_CLICK_DENYLIST = re.compile(r"submit|apply|send|finish|continue", re.I)

# Upload-trigger allowlist: the file-chooser button path may click a control
# ONLY when its accessible name matches one of these AND clears the submit
# denylist. Belt and braces on top of `_safe_click`.
_UPLOAD_BUTTON_RE = re.compile(r"attach|upload|browse", re.I)


def _is_upload_field(fld) -> bool:
    """A field the dry run now uploads to: a real file input, or a control whose
    label carries an explicit upload/attach verb (a strong file signal). A bare
    resume/cv label on a NON-file control stays with the coverage classifier
    (manual-only), so a "CV link" text box is never fed a document."""
    if "file" in (fld.type or "").lower():
        return True
    label = (fld.label or "").lower()
    return "upload" in label or "attach" in label


def _upload_attached(control, *, confirm: Callable[[], bool] | None = None) -> bool:
    """True iff the file input genuinely holds an attached file after
    `set_input_files` AND (when `confirm` is supplied) the vendor's own UI
    rendered it.

    PREFERS the input's own `files` FileList, read via `evaluate` (`el.files
    .length >= 1`): a live-DOM probe of Greenhouse's resume/CV widget proved
    `input_value()` (the `.value` string property) reads back EMPTY
    regardless of whether the attach genuinely landed -- Greenhouse's own
    widget resets `.value` once its change handler runs, to allow
    re-selecting the same file, so an empty read never distinguishes a real
    attach from one a custom widget silently swallowed. Keying off it there
    produced a structural FALSE POSITIVE: the old defensive "could not read
    -> assume attached" fallback (`except Exception: return True`, `not
    callable: return True`) guessed wrong, in the ATTACHED direction, every
    time the value read came back empty or unreadable. `el.files.length` has
    no such ambiguity: it is >=1 iff a file object is actually on the input,
    independent of anything a vendor's JS does to `.value` afterwards.

    BUT `el.files.length >= 1` alone is STILL NOT SUFFICIENT (2026-07-06
    gitlab/8503792002 acceptance run, HOSTILE REVIEW #1): a live probe showed
    the native input can genuinely hold the file while Greenhouse's own
    React-driven widget never rendered it (no filename, no remove control --
    still the empty "Attach"/"Enter manually" placeholder), because Greenhouse
    reads the change through its OWN component state, not merely the native
    FileList. `confirm`, when supplied, is an ADDITIONAL required check
    (called with no args, any exception treated as NOT confirmed): a
    vendor-specific callable that polls the vendor's own rendered
    confirmation UI (see `engine.providers.base.poll_upload_confirmed`).
    Greenhouse's `_fill_upload` supplies one; a caller that omits `confirm`
    (lever/workable/ashby, and this module's own generic `_fill_upload`) keeps
    the pre-existing FileList-only signal unchanged -- `confirm` is additive,
    never a behavior change for a caller that does not opt in.

    Falls back to `input_value()` only when the control exposes no
    `evaluate` at all (a vendor/fixture not yet carrying the file-count
    signal); that fallback is UNCONFIRMED for Greenhouse and must never be
    reinstated there. Never-attached bias throughout, mirroring the
    never-send guard's "any ambiguity blocks" philosophy elsewhere in this
    module: a control this code cannot positively confirm attached -- no
    `evaluate`, no `input_value`, or either one raising or returning
    something that is not a genuine count -- is treated as NOT attached, so
    an unreadable readback can never silently pass a required upload as
    filled. Same bias for `confirm`: it must return truthy to count; a
    missing/raising/falsy confirm means NOT attached, so a non-functional
    attach is always honestly reported as a required gap, never a silent
    false-COMPLETE."""
    evaluator = getattr(control, "evaluate", None)
    if callable(evaluator):
        try:
            count = evaluator("el => (el.files ? el.files.length : 0)")
        except Exception:
            return False
        try:
            attached = int(count) >= 1
        except (TypeError, ValueError):
            return False
    else:
        getter = getattr(control, "input_value", None)
        if not callable(getter):
            return False
        try:
            value = getter()
        except Exception:
            return False
        attached = bool(value)
    if not attached or confirm is None:
        return attached
    try:
        return bool(confirm())
    except Exception:
        return False


def _locate_file_input(page, fv: FieldValue):
    """Find the real <input type=file> for an upload field.

    Preference (per the live probe of Greenhouse/Lever/Ashby): an input whose
    id or name contains a meaningful token of the field key (e.g. "resume");
    else, by `accept` MIME family: an image input for a photo field, a document
    input (or an input with no `accept`) for a CV field. None if none matches.

    LIVE-DOM ROOT CAUSE (2026-07-06 gitlab/8503792002 acceptance run, HOSTILE
    REVIEW #1): `_file_inputs` enumerates candidates via `query_selector_all`,
    which returns Playwright ElementHandles -- a snapshot reference resolved
    ONCE, at scan time. Driving `set_input_files` directly on that handle put
    the file genuinely into the input's own `files` FileList (so the OLD
    `el.files.length`-only readback reported success), but Greenhouse's
    React-driven widget never rendered the attach (no filename, no remove
    control): a live probe proved a plain `page.locator('input[type=file]
    #resume').set_input_files(cv)` on the SAME input DOES make the widget
    render it. A `Locator` re-resolves its selector fresh at the exact moment
    the action runs, so it always drives whichever node is CURRENTLY mounted
    and wired to Greenhouse's own change handling; an ElementHandle captured
    earlier can silently end up acting on a stale/detached reference instead.
    `_file_input_control` returns `page.locator("input[type=file]").nth(index)`
    (the SAME positional match `_file_inputs` inspected -- no id/name CSS
    construction needed, dodging attribute-escaping edge cases entirely) so
    every caller's `set_input_files` now goes through a live Locator by
    default. Falls back to the raw handle only when `page` exposes no
    `.locator` at all (an older fixture/fake not yet carrying it)."""
    inputs = _file_inputs(page)
    if not inputs:
        return None
    tokens = _field_key_tokens(fv.key)
    for index, inp in enumerate(inputs):
        idname = _input_idname(inp)
        if idname and any(token in idname for token in tokens):
            return _file_input_control(page, index, inp)
    want_image = fv.asset == "photo"
    for index, inp in enumerate(inputs):
        accept = _input_accept(inp)
        if want_image:
            if _accept_has_image(accept):
                return _file_input_control(page, index, inp)
        elif not accept or _accept_has_doc(accept):
            return _file_input_control(page, index, inp)
    return None


def _file_input_control(page, index: int, handle):
    """The control to DRIVE the file input at `index` (same document order
    `_file_inputs` enumerated): a fresh `page.locator("input[type=file]")
    .nth(index)` when `page` supports it (re-resolves live at action time --
    see `_locate_file_input`'s docstring for why this matters), else the raw
    `handle` `_file_inputs` already inspected (a fixture/fake with no
    `.locator`, or one whose locator does not model file inputs)."""
    locator_fn = getattr(page, "locator", None)
    if not callable(locator_fn):
        return handle
    try:
        return locator_fn("input[type=file]").nth(index)
    except Exception:
        return handle


def _file_inputs(page):
    getter = getattr(page, "query_selector_all", None)
    if getter is None:
        return []
    try:
        return list(getter("input[type=file]") or [])
    except Exception:
        return []


def _field_key_tokens(key: str) -> list[str]:
    return [token for token in re.split(r"[^a-z0-9]+", (key or "").lower())
            if len(token) >= 3 and token not in _KEY_STOPWORDS]


def _input_idname(inp) -> str:
    idv = _safe_get_attr(inp, "id") or ""
    namev = _safe_get_attr(inp, "name") or ""
    return f"{idv} {namev}".lower()


def _input_accept(inp) -> str:
    return (_safe_get_attr(inp, "accept") or "").lower()


def _accept_has_doc(accept: str) -> bool:
    return any(token in accept for token in _DOC_ACCEPT_TOKENS)


def _accept_has_image(accept: str) -> bool:
    return any(token in accept for token in _IMAGE_ACCEPT_TOKENS)


def _safe_click(target, name: str) -> None:
    """The SOLE sanctioned click primitive: refuses any submit-like name.

    Every click need (there are none on the happy path; a combobox open would be
    one) must route through here so a submit is structurally impossible.
    """
    if _CLICK_DENYLIST.search(name or ""):
        raise FillSafetyError(
            f"refusing to click element named {name!r}: matches the submit "
            "denylist (submit/apply/send/finish/continue); this dry run STOPS "
            "SHORT OF APPLYING")
    target.click()


def _safe_upload(control, path, assets: FillAssets, *,
                 page=None, button_name: str | None = None) -> None:
    """Attach a WHITELISTED asset to a file control without ever submitting.

    (a) Whitelist: `path` MUST resolve to one of the FillAssets (the documents/
        CVs or the profile-pics photo); anything else raises FillSafetyError.
    (b) Never clicks blindly: uploads via `set_input_files` directly on the input
        (Playwright drives even hidden inputs). Only when the control exposes no
        file input does it fall back to the OS file chooser, and then the button
        click is routed through `_safe_click` (submit denylist) ONLY when the
        trigger name also matches the attach/upload/browse allowlist.
    """
    if not assets.is_whitelisted(path):
        raise FillSafetyError(
            f"refusing to upload {str(path)!r}: not in the FillAssets whitelist "
            "(only the documents/ CVs and the profile-pics photo may be uploaded)")
    setter = getattr(control, "set_input_files", None)
    if callable(setter):
        setter(str(path))            # direct: no click, works on hidden inputs
        return
    if page is None or button_name is None:
        raise FillSafetyError(
            "cannot upload: the control exposes no file input and no file-chooser "
            "trigger is available")
    if not _UPLOAD_BUTTON_RE.search(button_name):
        raise FillSafetyError(
            f"refusing to open a file chooser via {button_name!r}: the trigger "
            "name does not match the attach/upload/browse allowlist")
    with page.expect_file_chooser() as chooser:
        _safe_click(control, button_name)     # also enforces the submit denylist
    chooser.value.set_files(str(path))


def _locate(page, fv: FieldValue):
    """Reuse the fieldmap locator hint (role+name); fall back to the label."""
    role = fv.locator.role
    name = fv.locator.name
    if role and name:
        return page.get_by_role(role, name=name)
    return page.get_by_label(fv.label)


def _readback(locator, value):
    """Read the control's current value back and decide if it matches intent.

    An empty/whitespace INTENDED text value is NEVER a confirmed fill: `fill`/
    `type_human` place nothing in the control (type_human early-returns on empty),
    so a blank control reading back "equal" to a blank intent would be a false
    match. Such a field is reported unconfirmed (ok=False) so a required field
    whose SSOT answer is empty becomes a required gap, never a silent
    false-COMPLETE. (The resolve layer already skips blank values before they
    reach here; this is the defence-in-depth guard at the readback boundary that
    covers every provider sharing `_readback`.)
    """
    if isinstance(value, bool):
        actual = locator.is_checked()
        return actual, bool(actual) == value
    if isinstance(value, list):
        actual = locator.input_value()
        haystack = str(actual).lower()
        return actual, all(str(item).strip().lower() in haystack
                           for item in value)
    actual = locator.input_value()
    if not _norm(value):
        return actual, False
    return actual, _norm(actual) == _norm(value)


def _norm(value) -> str:
    return str(value).strip().lower()


def _safe_get_attr(locator, name: str):
    getter = getattr(locator, "get_attribute", None)
    if getter is None:
        return None
    try:
        return getter(name)
    except Exception:
        return None
