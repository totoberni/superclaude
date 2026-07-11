"""Lever provider: the SECOND reference implementation of the `Provider`
contract (`engine.providers.protocol.Provider`), W5.3.

Where `greenhouse.py` proves the contract on the SCHEMA-API path, Lever proves
it on the DOM path -- the two patterns every later vendor is a variation of.
The module-scope Provider shape (a `vendor` string plus module-level `capture`
/ `apply_url` / `fill` free functions, structurally satisfying the Protocol) is
copied VERBATIM from greenhouse; only `fill()`'s field-driving mechanics and the
completeness SEMANTICS differ, for the two reasons Lever is a distinct reference:

1. NO SCHEMA -> THE DOM SWEEP IS THE PRIMARY COMPLETENESS SOURCE. Lever's
   postings API exposes no custom-question schema, so `capture_lever` derives
   the FieldMap from the rendered apply DOM (`source="lever_dom"`). There is
   therefore no INDEPENDENT schema to trust: the `fieldmap.required_fields()`
   set is itself a DOM snapshot taken at capture time, and the LIVE
   `base.sweep_required(page)` at fill time is the authoritative required-field
   oracle. For greenhouse the sweep is a cross-check against an independent
   API schema (hole-fix d); for Lever the sweep IS the schema. The completeness
   code is the same shared machinery (`kernel.resolve._completeness` + `base.
   completeness_mismatch` + `_sweep_gaps`), but for Lever a `dom_only` gap is
   not "the schema missed a field" -- it is "the live form requires a field the
   capture-time DOM did not", the ground truth winning over the snapshot. Every
   fill is still readback-gated: a value only counts once the control reads it
   back.

2. NATIVE SELECTS, NOT REACT-SELECT. Lever renders server-side native
   `<select>` and radio controls, not greenhouse's react-select comboboxes.
   A native select's fieldmap locator role is nonetheless "combobox"
   (`fieldmap._role_for_type`), so a role-based react-select inference (what
   greenhouse's `_is_react_combobox` does) would MIS-drive it. Lever therefore
   drives every field through the plain native path (`base._locate` +
   `select_option` for a select, `base.type_human` for text, `base._safe_upload`
   for a file) and NEVER calls `base.select_react_combobox`.

3. hCAPTCHA CHECKBOX/RADIO HAZARD -> HUMAN HAND-OFF (fail safe). hCaptcha
   intercepts PROGRAMMATIC checkbox/radio clicks mid-form. The shared `base.py`
   spine exposes no human-like TRUSTED-click primitive (its clicks are
   programmatic `.click()`), and this module may not add one (base.py is out of
   scope this wave). So rather than risk tripping hCaptcha with a `.check()` /
   `.click()`, `fill()` NEVER auto-clicks a checkbox/radio: it marks the control
   for human hand-off (a clear skip reason). A REQUIRED checkbox that is handed
   off therefore lands in `required_unfilled` -> NOT_COMPLETE, never a silent
   skip and never a reckless auto-click. Native `<select>` is unaffected:
   `select_option` is not a click and hCaptcha does not intercept it.

CV/PHOTO: `resolve_values` delegates to `engine.kernel.resolve.resolve_values`
-- the hole-fix e structural CV/photo choice (an image/photo upload field present
on the FORM -> the plain ATS CV and the photo attaches to that field; absent ->
the embedded-photo ATSI CV variant). That rule is vendor-agnostic (it is keyed on
`kernel.resolve._form_has_photo_field`, a form-structure signal, never posting text, per
anti-injection finding 5), so it is single-sourced in the kernel and delegated
to here rather than duplicated -- a load-bearing safety rule with one home.

OVERRIDES greenhouse: the whole `fill()` field-driving body (native path, no
react-select, checkbox/radio human hand-off) and the DOM-sweep-as-PRIMARY
completeness SEMANTICS above. Everything else -- the structural never-send
(`base.install_never_send`), the upload primitives (`base._safe_upload` /
`kernel.fill_toolkit._locate_file_input` / `kernel.fill_toolkit._upload_attached`), the DOM sweep
(`base.sweep_required`), the completeness arithmetic (`kernel.resolve._completeness`) and
the `FillReport` dataclass -- is the SAME shared base/fill spine both providers
stand on (never a reimplementation).

LAZY-IMPORT INVARIANT (mirrors greenhouse.py / base.py / _registry.py): this
module must not import patchright / a browser-capture module at load time so the daily
poller (which imports `engine.providers` eagerly: `_registry` plus the four
plugin packages, all browser-free) stays browser-free. Kernel primitives are
imported at module scope (browser-free by construction); the vendor capture
submodule is reached at CALL time via `importlib.import_module`. Dataclasses
come from their canonical kernel home (`kernel.contracts`); this module has
NO `engine.fill` import at any scope (the Stage 4 repoint is done). Lever
imports NO sibling vendor package (import-disjoint, W5.1 Stage 3a): the CV/photo
rule comes from the kernel, not from greenhouse.

SEEDED FIELD-NAME REFERENCE (workpls lever.js, Apache-2.0; W5 spec section 3):
Lever's stable native submission names are `name`, `email`, `phone`,
`urls[LinkedIn|Twitter|GitHub|Portfolio]`, the `eeo[gender|race|veteran]`
demographic block, and the resume file input `input[type=file][name=resume]`.
This module does NOT hardcode those names as selectors: the per-field
`fieldmap.Field.locator` (role + accessible name, captured from the live DOM by
`engine.providers.lever.capture._parse_lever`) is authoritative and always
preferred (`base._locate`
resolves it). The workpls names are kept here as a REFERENCE/FALLBACK note only,
for a human debugging a selector miss offline; no code path consults them.
"""

from __future__ import annotations

from typing import Any

from engine.kernel.contracts import (
    FieldMap, FillAssets, FillReport, FillSafetyError, ResolvedValues)
from engine.kernel.resolve import resolve_values as _kernel_resolve_values
from engine.kernel.fill_toolkit import (
    _current_url, _fill_upload, _is_upload, _needs_human_handoff,
    _strip_fragment)
from engine.kernel.capture_toolkit import _utc_now_iso
from engine.providers import base

vendor = "lever"


# -- capture / apply_url: the read-only apply-DOM parse + apply-page URL --------


def capture(slug: str, job_id: str, opener: Any = None) -> FieldMap:
    """The field-map capture: `capture.capture_lever`, the read-only apply-DOM
    parse (Lever's field map comes from the DOM, not a schema endpoint, so
    `opener` is ignored). Reached at CALL TIME via
    `importlib.import_module("engine.providers.lever.capture")` -- the `.capture`
    submodule name is shadowed at package scope by this Provider callable, so it
    is reached through `sys.modules` per the package __init__ NAME NOTE -- so
    importing this module never loads the browser stack and the capture-module
    monkeypatch seam still routes. No new capture logic here (the provider
    registry looks this function up lazily as `_registry.get("lever").capture`)."""
    from importlib import import_module
    return import_module("engine.providers.lever.capture").capture_lever(slug, job_id)


def apply_url(slug: str, job_id: str) -> str:
    """The public apply-page URL: `capture.lever_apply_url`, reached at CALL TIME
    via `importlib.import_module` (the `.capture` submodule is shadowed at package
    scope; see __init__) so this module stays browser-free at import."""
    from importlib import import_module
    return import_module("engine.providers.lever.capture").lever_apply_url(slug, job_id)


# -- value resolution: from the kernel (hole-fix e CV/photo choice) ------------
# The structural CV/photo rule is vendor-agnostic (keyed on the form's own
# upload-field shape via `kernel.resolve._form_has_photo_field`, never posting text), so it
# has ONE home -- the generic kernel.resolve.resolve_values -- and Lever
# delegates to it rather than duplicating a load-bearing safety rule.


def resolve_values(fieldmap: FieldMap, ssot, profile: dict, *,
                   assets: FillAssets | None = None,
                   posting_lang: str = "en") -> ResolvedValues:
    """Render every field to a concrete fill value via the kernel's generic
    resolve engine. Lever has no portal-widget quirks, so no vendor_resolver is
    injected (the kernel no-op default). The owner-ratified structural CV/photo
    rule (plain ATS CV when the form has a photo field, embedded-photo ATSI CV
    otherwise) is generic in the kernel and keys purely on the FORM's structure,
    so it is correct for a DOM-captured Lever field map identically."""
    return _kernel_resolve_values(fieldmap, ssot, profile, assets=assets,
                                  posting_lang=posting_lang)


# -- fill(): the Provider contract's ordered sequence (DOM path) ----------------
# (1) never-send FIRST, (2) drive every field via base.py NATIVE primitives
# (native select / type_human / _safe_upload; a checkbox/radio is handed off,
# never auto-clicked), (3) readback-gate what counts as filled, (4) DOM-sweep as
# the PRIMARY completeness source forces NOT_COMPLETE on any gap, (5) return the
# existing FillReport dataclass.


def fill(page: Any, fieldmap: FieldMap, values: ResolvedValues, *,
        dry_run: bool = True, company: str | None = None) -> FillReport:
    """Drive an ALREADY-NAVIGATED Lever apply page, STOPPING SHORT OF APPLYING.

    `dry_run` is accepted for interface stability; Part 1 carries no submit code
    path regardless of its value (`install_never_send` is unconditional). The
    optional `company` keyword is the same documented extension greenhouse.fill
    carries (a `Protocol` is structural, so an extra defaulted keyword keeps
    conformance); it falls back to `fieldmap.posting_id` for the ntfy caption
    when a caller supplies no employer slug.
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

    # (2) + (3) drive + readback-gate every resolved field via the NATIVE path.
    for fv in values.fields:
        if _is_upload(fv):
            _fill_upload(page, fv, uploads, extra_skips, filled_keys)
            continue
        if _needs_human_handoff(fv):
            # hCaptcha hazard: NEVER auto-click a checkbox/radio. Hand it off with
            # a clear reason; a required one falls through to required_unfilled.
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
                (fv.key, "value did not take (readback mismatch)"))

    # Safety invariant carried over from greenhouse.fill / fill.fill_form: a
    # navigation during the fill is treated as a possible submission/redirect,
    # even though this module never calls page.goto() itself.
    post_url = _current_url(page)
    url_unchanged = _strip_fragment(pre_url) == _strip_fragment(post_url)
    if not url_unchanged:
        raise FillSafetyError(
            f"page navigated during fill ({pre_url!r} -> {post_url!r}); a "
            "navigation may indicate a submission or redirect")

    # (4) DOM-sweep completeness -- the PRIMARY oracle for Lever. `schema_
    # required` here is the capture-time DOM snapshot (Lever has no independent
    # schema); `dom_required` is the LIVE sweep, which wins. Any mismatch forces
    # NOT_COMPLETE via _sweep_gaps, and a checkbox/radio handed off above (or any
    # field whose readback did not confirm) surfaces through kernel.resolve._completeness.
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


# per-vendor variant (differs from the kernel generic): Lever's DOM sweep is authoritative (no independent schema), so the required_unfilled reason strings differ
def _sweep_gaps(mismatch: dict) -> list[dict]:
    """Synthetic `required_unfilled` entries for a DOM-sweep mismatch.

    For Lever the sweep is authoritative, so ANY mismatch (either direction)
    forces NOT_COMPLETE. `dom_only` = the live form requires a field the
    capture-time DOM snapshot did not carry (the ground truth winning over the
    snapshot); `schema_only` = the snapshot marked a field required but the live
    sweep no longer finds it required (a drift between capture and fill)."""
    gaps: list[dict] = []
    for name in mismatch.get("dom_only") or ():
        gaps.append({
            "key": f"dom-sweep:{name}", "label": name,
            "reason": "the live DOM requires this field but the capture-time "
                      "field map did not (DOM sweep is authoritative for Lever)"})
    for name in mismatch.get("schema_only") or ():
        gaps.append({
            "key": f"dom-sweep:{name}", "label": name,
            "reason": "the captured field map marked this required but the live "
                      "DOM sweep did not find it required (capture/fill drift)"})
    return gaps


# -- per-field driving: NATIVE path only (no react-select) ----------------------
# text/email/phone/url via type_human, a native <select> via select_option, a
# checkbox/radio never here (it is handed off before this is reached).


# A native (server-rendered) select or a resolved multi-select option list: the
# full _SELECT_TYPES vocabulary, all driven by select_option -- NOT react-select
# (that is the greenhouse-only override this module deliberately does not share).
_NATIVE_SELECT_TYPES = frozenset({
    "multi_value_single_select", "multi_value_multi_select", "yes_no",
})

_HUMAN_HANDOFF_REASON = (
    "checkbox/radio needs a human-operated trusted click: hCaptcha intercepts "
    "programmatic checkbox/radio clicks mid-form, so this control is handed off "
    "for a human (a required one forces NOT_COMPLETE, never a silent auto-click)")


def _fill_field(page, fv) -> tuple[bool, Any]:
    """Drive one non-upload, non-handoff field via the native path; returns
    (landed, actual-read-back). NEVER routes through `base.select_react_combobox`
    -- that is greenhouse's widget and would mis-drive Lever's native select."""
    locator = base._locate(page, fv)
    _apply_native(locator, fv)
    actual, ok = base._readback(locator, fv.value)
    return ok, actual


def _apply_native(locator, fv) -> None:
    """Write one value via the safe native action for its shape: `select_option`
    for a native select (single or multi) or a resolved option list, else
    `type_human` (human-cadence keystrokes, NEVER `fill()`) for the reCAPTCHA v3
    score protection the W5 spec section 3 requires. No boolean branch: a
    checkbox never reaches here (it is handed off in `fill()`), so this module
    never issues a `.check()` / `.click()` that hCaptcha could intercept."""
    value = fv.value
    if isinstance(value, list):
        locator.select_option(label=value)
    elif fv.type in _NATIVE_SELECT_TYPES:
        locator.select_option(label=value)
    else:
        base.type_human(locator, str(value))
