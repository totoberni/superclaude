"""Ashby provider: the THIRD reference implementation of the `Provider`
contract (`engine.providers.protocol.Provider`), W5.3.

Ashby FOLLOWS GREENHOUSE, not Lever, on completeness. Like Greenhouse it has a
REAL SCHEMA: `engine.providers.ashby.capture.capture_ashby` intercepts the non-user-graphql
`ApiJobPosting` form and maps its typed field definitions onto the FieldMap
(`source="ashby_graphql"`), so `fieldmap.required_fields()` is an INDEPENDENT,
authoritative required-field oracle. The live `base.sweep_required(page)` at
fill time is therefore a CROSS-CHECK (hole-fix d) that the schema did not miss
a field the page actually requires -- NOT the primary completeness source it is
for Lever (Lever carries no schema at all). `_sweep_gaps` reuses greenhouse's
wording ("schema missed a field" / "schema marks it required but the DOM sweep
did not"), NOT Lever's "the DOM sweep is authoritative".

The module-scope Provider shape (a `vendor` string plus module-level `capture`
/ `apply_url` / `fill` free functions structurally satisfying the Protocol) is
copied VERBATIM from greenhouse/lever; only `fill()`'s field-driving mechanics
differ, for the two reasons Ashby is a distinct reference:

1. OWN CONTROLLED-COMPONENT SELECT DRIVER. Ashby's select / combobox controls
   are React CONTROLLED COMPONENTS with NO native `<select>` element and NONE of
   react-select's `#react-select-{id}-*` DOM. So neither greenhouse's
   react-select driver (`base.select_react_combobox`, which targets those
   react-select-library node ids Ashby does not have) nor Lever's native
   `locator.select_option` (there is no `<select>` to option-set) will commit
   Ashby's state. This module drives every Ashby select through its OWN driver
   (`_apply_ashby_controlled`) using the berellevy onChange/onBlur technique:
   set the value with the element's NATIVE value setter (bypassing React's
   overridden setter) then dispatch the `input`/`change` events React's
   controlled component actually listens for (plus a `blur` to commit), and
   FINALLY readback-verify the committed value. A naive value-set with no event
   dispatch is silently ignored by the controlled component and reads back
   empty, so it is NEVER counted as filled -- the whole fill loop is
   readback-gated (a value counts only once the control reads it back).

   DECIDE THE DRIVER BY VENDOR, NOT BY LOCATOR ROLE. The role="combobox" a
   single-select carries is exactly the role a Lever NATIVE select also carries
   (`fieldmap._role_for_type`), so a role-based native-vs-react inference is the
   trap that mis-drives one or the other. Ashby sidesteps it entirely: every
   Ashby SELECT-TYPE field (the vendor's own schema `type`, unambiguous) routes
   unconditionally through the controlled-component driver; a role guess is
   never consulted. Text fields still type through `base.type_human` (genuine
   key events already commit a React text input), and a file field still
   uploads through the shared `base._safe_upload` primitives.

2. TURNSTILE CHECKBOX/RADIO HAZARD -> HUMAN HAND-OFF (fail safe). Ashby fronts
   its forms with Cloudflare TURNSTILE, which (like Lever's hCaptcha) intercepts
   PROGRAMMATIC checkbox/radio clicks mid-form. The shared `base.py` spine
   exposes no human-like TRUSTED-click primitive and this module may not add one
   (base.py is out of scope this wave), so `fill()` NEVER auto-clicks a
   checkbox/radio: it hands the control off with a clear reason (the SAME
   fail-safe pattern Lever uses). A REQUIRED checkbox that is handed off
   therefore lands in `required_unfilled` -> NOT_COMPLETE, never a silent skip
   and never a reckless auto-click that Turnstile could weaponise.

DUP-EMAIL MERGE (Part-2 forward note, no code path here): Ashby SILENTLY MERGES
a duplicate-email application into the existing candidate rather than rejecting
it. Part 1 carries no submit path (`install_never_send` aborts every submit
POST structurally), so there is nothing to guard here yet; the Part-2
idempotency ledger (key = hash(canonical url + form-schema hash + SSOT version))
must treat a re-fill of an already-applied Ashby posting as a NO-OP so a second
run cannot ride Ashby's silent merge into a duplicate touch. Recorded here as a
COMMENT so the seam is not lost; there is no submit code to attach it to.

CV/PHOTO: `resolve_values` delegates to `engine.kernel.resolve.resolve_values`
-- the hole-fix e structural CV/photo choice (a photo/image upload field present
on the FORM -> the plain ATS CV and the photo attaches to that field; absent ->
the embedded-photo ATSI CV variant). That rule keys purely on the form's own
upload-field shape (`kernel.resolve._form_has_photo_field`, never posting text, per
anti-injection finding 5), so it is single-sourced in the kernel and delegated
to here exactly as lever does, rather than duplicating a load-bearing safety
rule with one home.

OVERRIDES greenhouse: only the per-field DRIVING body (the controlled-component
select driver + the Turnstile checkbox/radio hand-off). Everything else -- the
structural never-send (`base.install_never_send`), the upload primitives
(`base._safe_upload` / `kernel.fill_toolkit._locate_file_input` / `kernel.fill_toolkit._upload_attached`), the
DOM sweep (`base.sweep_required`), the completeness arithmetic
(`kernel.resolve._completeness`) and its greenhouse cross-check SEMANTICS, and the
`FillReport` dataclass -- is the SAME shared base/fill spine every provider
stands on (never a reimplementation).

LAZY-IMPORT INVARIANT (mirrors greenhouse.py / lever.py / base.py / _registry.py):
this module must not import patchright / a browser-capture module at load time so the
daily poller (which imports `engine.providers` eagerly: `_registry` plus the
four plugin packages, all browser-free) stays browser-free. Kernel primitives
are imported at module scope (browser-free by construction); the vendor capture
submodule is reached at CALL time via `importlib.import_module`. Dataclasses
come from their canonical kernel home (`kernel.contracts`); this module has
NO `engine.fill` import at any scope (the Stage 4 repoint is done).
Ashby imports NO sibling vendor package (import-disjoint, W5.1 Stage 3a): the
CV/photo rule comes from the kernel, not from greenhouse.

SEEDED FIELD-NAME REFERENCE (neonwatty/job-apply-plugin, MIT; W5 spec section
3): Ashby's stable system-field paths are `_systemfield_name`,
`_systemfield_email`, `_systemfield_phone`, and the resume file input
`#_systemfield_resume`; custom questions carry a `custom_*` path. This module
does NOT hardcode those as selectors: the per-field `fieldmap.Field.locator`
(role + accessible name, derived from the graphql schema by
`engine.providers.ashby.capture._parse_ashby`) is authoritative and always
preferred (`base._locate` resolves it); the
resume input is found by its key stem through the shared `kernel.fill_toolkit._locate_file_
input` (the `_systemfield_resume` key yields a "resume" token). The neonwatty
paths are kept as a REFERENCE/FALLBACK note only; no code path consults them.
"""

from __future__ import annotations

from typing import Any

from engine.kernel.contracts import (
    FieldMap, FillAssets, FillReport, FillSafetyError, ResolvedValues)
from engine.kernel.resolve import resolve_values as _kernel_resolve_values
from engine.kernel.fill_toolkit import (
    _current_url, _fill_upload, _is_upload, _needs_human_handoff,
    _strip_fragment, _sweep_gaps)
from engine.kernel.capture_toolkit import _utc_now_iso
from engine.providers import base

vendor = "ashby"


# -- capture / apply_url: the graphql schema capture + apply-page URL -----------


def capture(slug: str, job_id: str, opener: Any = None) -> FieldMap:
    """The field-map capture: `capture.capture_ashby`, the read-only
    non-user-graphql `ApiJobPosting` interception (Ashby's field map comes from
    the graphql schema, so `opener` is ignored). Reached at CALL TIME via
    `importlib.import_module("engine.providers.ashby.capture")` -- the `.capture`
    submodule name is shadowed at package scope by this Provider callable, so it
    is reached through `sys.modules` per the package __init__ NAME NOTE -- so
    importing this module never loads the browser stack and the capture-module
    monkeypatch seam still routes. No new capture logic here (the provider
    registry looks this function up lazily as `_registry.get("ashby").capture`)."""
    from importlib import import_module
    return import_module("engine.providers.ashby.capture").capture_ashby(slug, job_id)


def apply_url(slug: str, job_id: str) -> str:
    """The public apply-page URL: `capture.ashby_application_url`, reached at CALL
    TIME via `importlib.import_module` (the `.capture` submodule is shadowed at
    package scope; see __init__) so this module stays browser-free at import."""
    from importlib import import_module
    return import_module("engine.providers.ashby.capture").ashby_application_url(slug, job_id)


# -- value resolution: from the kernel (hole-fix e CV/photo choice) ------------
# The structural CV/photo rule is vendor-agnostic (keyed on the form's own
# upload-field shape via `kernel.resolve._form_has_photo_field`, never posting text), so it
# has ONE home -- the generic kernel.resolve.resolve_values -- and Ashby
# delegates to it rather than duplicating a load-bearing safety rule.


def resolve_values(fieldmap: FieldMap, ssot, profile: dict, *,
                   assets: FillAssets | None = None,
                   posting_lang: str = "en") -> ResolvedValues:
    """Render every field to a concrete fill value via the kernel's generic
    resolve engine. Ashby has no portal-widget quirks, so no vendor_resolver is
    injected (the kernel no-op default). The owner-ratified structural CV/photo
    rule (plain ATS CV when the form has a photo field, embedded-photo ATSI CV
    otherwise) is generic in the kernel and keys purely on the FORM's structure,
    so it is correct for a graphql-captured Ashby field map identically."""
    return _kernel_resolve_values(fieldmap, ssot, profile, assets=assets,
                                  posting_lang=posting_lang)


# -- fill(): the Provider contract's ordered sequence (schema + controlled-select)
# (1) never-send FIRST, (2) drive each field via the right primitive (Ashby
# controlled-component driver for selects/comboboxes, base.type_human for text,
# base._safe_upload for the resume, a checkbox/radio handed off for Turnstile),
# (3) readback-gate what counts as filled, (4) DOM-sweep CROSS-CHECK (greenhouse
# semantics -- the schema is authoritative) forces NOT_COMPLETE on any mismatch,
# (5) return the existing FillReport dataclass.


def fill(page: Any, fieldmap: FieldMap, values: ResolvedValues, *,
        dry_run: bool = True, company: str | None = None) -> FillReport:
    """Drive an ALREADY-NAVIGATED Ashby apply page, STOPPING SHORT OF APPLYING.

    `dry_run` is accepted for interface stability; Part 1 carries no submit code
    path regardless of its value (`install_never_send` is unconditional). The
    optional `company` keyword is the same documented extension greenhouse.fill /
    lever.fill carry (a `Protocol` is structural, so an extra defaulted keyword
    keeps conformance); it falls back to `fieldmap.posting_id` for the ntfy
    caption when a caller supplies no employer slug.
    """
    ts = _utc_now_iso()

    # (1) STRUCTURAL never-send FIRST: registered before any field is touched,
    # so no interaction can race ahead of the interceptor.
    base.install_never_send(page)

    pre_url = _current_url(page)
    readback_mismatches: list[dict] = []
    extra_skips: list[tuple[str, str]] = []
    uploads: list[dict] = []
    filled_keys: set[str] = set()

    # (2) + (3) drive + readback-gate every resolved field.
    for fv in values.fields:
        if _is_upload(fv):
            _fill_upload(page, fv, uploads, extra_skips, filled_keys)
            continue
        if _needs_human_handoff(fv):
            # Turnstile hazard: NEVER auto-click a checkbox/radio. Hand it off
            # with a clear reason; a required one falls through to
            # required_unfilled -> NOT_COMPLETE.
            extra_skips.append((fv.key, _HUMAN_HANDOFF_REASON))
            continue
        try:
            ok, actual = _fill_field(page, fv)
        except FillSafetyError:
            raise
        except Exception as exc:  # per-field fill error is fail-soft
            extra_skips.append((fv.key, f"fill-error: {exc}"))
            continue
        if ok:
            filled_keys.add(fv.key)
        else:
            readback_mismatches.append(
                {"key": fv.key, "intended": fv.value, "actual": actual})
            extra_skips.append(
                (fv.key, "value did not commit (readback mismatch)"))

    # Safety invariant carried over from greenhouse.fill / lever.fill /
    # fill.fill_form: a navigation during the fill is treated as a possible
    # submission/redirect, even though this module never calls page.goto()
    # itself (the page arrives already navigated).
    post_url = _current_url(page)
    url_unchanged = _strip_fragment(pre_url) == _strip_fragment(post_url)
    if not url_unchanged:
        raise FillSafetyError(
            f"page navigated during fill ({pre_url!r} -> {post_url!r}); a "
            "navigation may indicate a submission or redirect")

    # (4) DOM-sweep completeness CROSS-CHECK (hole-fix d, greenhouse semantics):
    # `schema_required` is Ashby's INDEPENDENT graphql-schema required set (the
    # authoritative oracle); `dom_required` is the live sweep, run as a
    # cross-check that the schema did not miss a field the page actually
    # requires. ANY mismatch (either direction) forces NOT_COMPLETE.
    from engine.kernel.resolve import _completeness

    schema_required = {f.label for f in fieldmap.required_fields()}
    dom_required = base.sweep_required(page)
    mismatch = base.completeness_mismatch(schema_required, dom_required)

    filled = len(filled_keys)
    all_skips = list(values.skipped) + extra_skips
    fillable_total, required_unfilled, justified_skips = _completeness(
        fieldmap, filled_keys, all_skips, filled, vendor_resolver=None)
    required_unfilled = required_unfilled + _sweep_gaps(mismatch)

    return FillReport(
        vendor=vendor, company=company or fieldmap.posting_id,
        posting_id=fieldmap.posting_id,
        fillable_total=fillable_total, filled=filled,
        required_unfilled=required_unfilled, justified_skips=justified_skips,
        uploads=uploads, skipped=all_skips,
        readback_mismatches=readback_mismatches, validation_errors=[],
        url_unchanged=url_unchanged, screenshot="", ts=ts)


# -- per-field driving ---------------------------------------------------------
# text/email/phone via base.type_human; an Ashby select/combobox via the OWN
# controlled-component driver (NEVER react-select, NEVER native select_option);
# a checkbox/radio never here (it is handed off before this is reached).


# The Ashby SELECT-TYPE fields, decided by the vendor's own schema `type` (NOT by
# locator role -- see the module docstring's role="combobox" trap). Every one is
# driven by the controlled-component driver, never react-select or select_option.
_ASHBY_SELECT_TYPES = frozenset({
    "multi_value_single_select", "multi_value_multi_select", "yes_no",
})

_HUMAN_HANDOFF_REASON = (
    "checkbox/radio needs a human-operated trusted click: Cloudflare Turnstile "
    "intercepts programmatic checkbox/radio clicks mid-form, so this control is "
    "handed off for a human (a required one forces NOT_COMPLETE, never a silent "
    "auto-click)")

# The berellevy React controlled-component commit technique (BSD-3
# job_app_filler). React overrides the input/textarea `value` setter to track
# component state, so a plain `el.value = x` is ignored: the fix is to call the
# element prototype's NATIVE value setter, then dispatch the `input` + `change`
# events React's onChange handler listens for (and a `blur` for onBlur commit).
# Applied via a single `locator.evaluate` so the whole native-set + event
# dispatch runs atomically in-page; the driver then readback-verifies the
# committed value, so a controlled component that ignored the value never counts
# as filled.
_REACT_CONTROLLED_COMMIT_JS = """
(el, value) => {
    const proto = (el instanceof window.HTMLTextAreaElement)
        ? window.HTMLTextAreaElement.prototype
        : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
}
"""


def _is_ashby_select(fv) -> bool:
    """True for an Ashby select/combobox field, decided BY VENDOR SCHEMA TYPE
    (`fv.type` in `_ASHBY_SELECT_TYPES`), NOT by locator role. A single-select
    carries role "combobox" -- the same role a Lever NATIVE select carries -- so
    a role-based test is the native-vs-react trap; the schema `type` is the
    unambiguous vendor signal, and every such field routes through the
    controlled-component driver."""
    return fv.type in _ASHBY_SELECT_TYPES


def _fill_field(page, fv) -> tuple[bool, Any]:
    """Drive one non-upload, non-handoff field; returns (landed, actual-read-back).

    An Ashby select routes through the OWN controlled-component driver
    (`_apply_ashby_controlled`); everything else (text/email/phone/textarea)
    types through `base.type_human` (genuine key events already commit a React
    text input). NEVER `base.select_react_combobox` (greenhouse's react-select
    widget, whose node ids Ashby lacks) and NEVER `locator.select_option`
    (Lever's native path, which has no `<select>` to option-set here)."""
    locator = base._locate(page, fv)
    if _is_ashby_select(fv):
        _apply_ashby_controlled(locator, fv.value)
    else:
        base.type_human(locator, str(fv.value))
    actual, ok = base._readback(locator, fv.value)
    return ok, actual


def _apply_ashby_controlled(locator, value) -> None:
    """Commit `value` into an Ashby React CONTROLLED-COMPONENT select via the
    berellevy onChange/onBlur technique (`_REACT_CONTROLLED_COMMIT_JS`): the
    NATIVE value setter plus dispatched `input`/`change`/`blur` events. Runs the
    whole set-and-dispatch atomically in-page through `locator.evaluate`. NEVER
    `locator.fill()` (a plain value-set React silently ignores) and NEVER a
    `.click()`/`.select_option()`. The caller (`_fill_field`) readback-verifies
    the committed value immediately after, so a controlled component that
    ignored the change reads back empty and is never counted as filled."""
    locator.evaluate(_REACT_CONTROLLED_COMMIT_JS,
                     list(value) if isinstance(value, list) else str(value))
