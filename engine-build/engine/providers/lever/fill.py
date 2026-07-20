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
   drives every field through the plain native path (`_locate` + `select_option`
   for a select, `base.type_human` for text, `base._safe_upload` for a file) and
   NEVER calls `base.select_react_combobox`. The control is found by its
   SUBMISSION NAME (`_locate`, this module), because a Lever question's wording is
   not the control's accessible name; the kernel's `base._locate` (role + name)
   stays the fallback. See `_locate`.

3. CHECKBOX/RADIO -> THE KERNEL'S `control_toolkit.drive_control` (W5.1c).
   Owner-ratified 2026-07-13: a native Playwright `.check()`/`.uncheck()` goes
   through CDP (`isTrusted=true`), the SAME risk tier as the `type_human`
   keystrokes this module already sends, so a programmatic tick is no longer
   deferred to a human. `fill()` drives every checkbox/radio field through
   `drive_control` (`_drive_control_field` below): a single checkbox drives one
   control; a radio group or multi-checkbox group is N controls sharing one
   submission name, each option located by its OWN accessible name (the option's
   wording IS its accessible name -- see `capture._radio_option_label` /
   `_checkbox_label`) and driven independently, and the field counts as filled
   only when every option it drove confirmed. Lever has no `date`-typed field in
   its capture (a date-shaped input falls through `capture._control_type` to
   plain `input_text` and types normally) and no picker-only calendar, so this
   module never builds a DATE `ControlSpec`. `_safe_click`'s submit denylist and
   the never-send interceptor are unchanged; `drive_control` routes every click
   through `_safe_click` itself.

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

import json
import re
import time
from typing import Any, NamedTuple

from engine.kernel.contracts import (
    FieldMap, FieldValue, FillAssets, FillReport, FillSafetyError,
    ResolvedValues)
from engine.kernel.resolve import (
    ANSWERABLE, _classify_field, resolve_values as _kernel_resolve_values)
from engine.kernel.ssot import MISSING
from engine.kernel.fill_toolkit import (
    _accessible_name, _current_url, _fill_upload, _is_upload,
    _normalize_name, _strip_fragment, _visible_locators)
from engine.kernel.capture_toolkit import _utc_now_iso
from engine.kernel.control_toolkit import ControlKind, ControlSpec, drive_control
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
    so it is correct for a DOM-captured Lever field map identically.

    ONE vendor-local post-process (F-6), the same architectural shape greenhouse
    uses for its education typeaheads: the kernel's option match is EXACT-only, so
    a checkbox/radio group whose per-posting option wording differs from the bare
    SSOT concept ("English (ENG)" vs "English", "No, I do not consent" vs
    "Opt-out") is skipped "no option matches SSOT value ...".
    `_reconnect_option_phrasing` remaps the SSOT value onto that captured wording
    (see there); the mapping is a vendor concern (phrasing varies per posting), so
    it lives here, never in the shared kernel, and is never a verbatim SSOT
    reseed."""
    resolved = _kernel_resolve_values(fieldmap, ssot, profile, assets=assets,
                                      posting_lang=posting_lang)
    _reconnect_option_phrasing(fieldmap, ssot, profile or {}, resolved)
    return resolved


# -- F-6 option-phrasing reconnect: the SSOT concept -> the captured per-posting
# option WORDING the kernel could only exact-match -----------------------------
# A Lever checkbox/radio group enumerates its options in the posting's own words
# ("English (ENG)", "Yes, I consent"); the SSOT carries the bare concept
# ("English", "Opt-out"). The kernel's `_render_select` matches an option only by
# case-insensitive EQUALITY, so it SKIPS these with "no option matches SSOT value
# ...". This post-process remaps the SSOT value onto the captured wording and
# injects the driven FieldValue (dropping that skip); the existing FX1 per-option
# mechanism (`_drive_control_field`) then drives it. It is the vendor-widget
# analogue of greenhouse's education reconnect, one structural level up: the
# vendor resolver can only return an exact option, and this reconnects the ones
# whose live phrasing an exact match cannot reach. Deliberately NOT a shared
# kernel change (per-posting phrasing is a vendor concern) and NEVER a verbatim
# SSOT reseed -- the notetaker mapping is semantic-class (a consent polarity),
# not a copy of the option text into the SSOT.

# The kernel skip reason `_render_select` emits when no captured option equals the
# SSOT value: the ONLY skip this post-process ever consumes, so a field parked for
# any OTHER reason (manual-only, missing, a seeded-policy decline) is untouched.
_NO_OPTION_MATCH_PREFIX = "no option matches SSOT value"

# A consent-polarity SSOT value ("Opt-out"/"Opt-in"): the semantic class the
# notetaker radio carries. `*` (not `?`) tolerates the spaced/joined/underscored
# spellings ("opt out", "optout", "opt_out").
_OPT_OUT_RE = re.compile(r"opt[\s_-]*out", re.I)
_OPT_IN_RE = re.compile(r"opt[\s_-]*in", re.I)
# The option-side polarity patterns. Deliberately asymmetric and BARE-"consent"-
# FREE: a negative option ("No, I do not consent") also carries the word
# "consent", so keying the affirmative on a bare "consent" would match BOTH and
# defeat the exactly-one rule. The negative keys on a standalone "no" / "do not
# consent" / "decline" / "opt out"; the affirmative on "yes" / "i consent" (the
# contiguous phrase, which the negative's "i do not consent" does not contain) /
# "opt in" / "agree" / "accept".
_NEG_CONSENT_RE = re.compile(
    r"\bno\b|do(?:es)? not consent|\bdecline\b|opt[\s_-]*out|\brefuse\b", re.I)
_POS_CONSENT_RE = re.compile(
    r"\byes\b|i consent\b|opt[\s_-]*in|\bagree\b|\baccept\b", re.I)


def _reconnect_option_phrasing(fieldmap: FieldMap, ssot, profile: dict,
                               resolved: ResolvedValues) -> None:
    """Remap each checkbox/radio GROUP the kernel skipped 'no option matches'
    onto its captured option wording, injecting the driven FieldValue and dropping
    the skip. A field the kernel resolved, or skipped for any OTHER reason, is left
    exactly as-is. No-op on a field map with no such group."""
    already = {fv.key for fv in resolved.fields}
    skip_reason = dict(resolved.skipped)
    for fld in fieldmap.fields:
        if fld.key in already:
            continue                       # the kernel resolved it; never override
        if not _is_option_group(fld):
            continue
        if not skip_reason.get(fld.key, "").startswith(_NO_OPTION_MATCH_PREFIX):
            continue                       # skipped for another reason -> untouched
        raw = _field_ssot_value(fld, ssot, profile)
        if raw is MISSING:
            continue
        _map_group_value(fld, raw, resolved)


def _is_option_group(fld) -> bool:
    """A checkbox/radio GROUP carrying captured options -- the only shape whose
    per-option wording the kernel exact-matches. A bare (single) checkbox has no
    options and is resolved as a boolean, never here."""
    return (fld.locator.role in (ControlKind.CHECKBOX, ControlKind.RADIO)
            and bool(fld.options))


def _field_ssot_value(fld, ssot, profile: dict):
    """The SSOT datum the kernel's exact-match failed on, recovered through the
    SAME classifier the kernel used (no vendor_resolver, matching this module's
    `resolve_values` call), so the mapper reads exactly that value. MISSING when
    the field is not answerable from the SSOT (nothing to remap)."""
    classified = _classify_field(fld, ssot, profile)
    if classified.status != ANSWERABLE:
        return MISSING
    return ssot.get(classified.path)


def _map_group_value(fld, raw, resolved: ResolvedValues) -> None:
    """Remap `raw` onto `fld.options`; on any success inject the driven FieldValue
    and drop the kernel's exact-match skip. A RADIO (single-select) maps to ONE
    option -- by consent polarity when `raw` reads as opt-in/opt-out, else by
    exact/unambiguous-containment. A CHECKBOX group (multi-select) maps EACH value
    and drives what maps, PARKING any unmatched value by name (a partial drive is
    honest for a multi-select). Ambiguity or no match leaves the kernel skip
    untouched -- an honest gap, never a fabricated or non-matching tick."""
    if fld.locator.role == ControlKind.RADIO:
        if not isinstance(raw, str):
            return                         # a radio needs one scalar to place
        option = _map_consent_polarity(fld.options, raw)
        if option is None:
            option = _map_option_containment(fld.options, raw)
        if option is None:
            return
        _drop_skip(resolved, fld.key)
        resolved.fields.append(_option_field(fld, option))
        return
    # CHECKBOX multi-select group: map every value, drive what maps.
    values = raw if isinstance(raw, list) else [raw]
    mapped: list[str] = []
    unmatched: list = []
    for value in values:
        option = _map_option_containment(fld.options, value)
        if option is None:
            unmatched.append(value)
        elif option not in mapped:
            mapped.append(option)
    if not mapped:
        return                             # nothing mapped -> leave the honest skip
    _drop_skip(resolved, fld.key)
    resolved.fields.append(_option_field(fld, mapped))
    for value in unmatched:
        resolved.skipped.append((
            f"{fld.key}[{value}]",
            f"option value {value!r} has no unambiguous matching option "
            "(parked; the values that mapped drove)"))


def _map_consent_polarity(options, raw: str):
    """The ONE option matching the consent polarity of `raw` ("Opt-out" ->
    negative, "Opt-in" -> affirmative), ONLY when EXACTLY ONE option matches that
    polarity's pattern. None for a non-polarity value, or on ANY ambiguity (0 or
    >1 options match) -- semantic-class mapping that parks honestly rather than
    guessing among several plausible options."""
    if _OPT_OUT_RE.search(raw):
        pattern = _NEG_CONSENT_RE
    elif _OPT_IN_RE.search(raw):
        pattern = _POS_CONSENT_RE
    else:
        return None
    matched = [o for o in options if pattern.search(str(o))]
    return matched[0] if len(matched) == 1 else None


def _map_option_containment(options, value):
    """The captured option for one SSOT value: case-insensitive EXACT first, then
    UNAMBIGUOUS containment (exactly one option CONTAINS the value). None when no
    option matches or the containment is ambiguous (>1) -- so an ambiguous value
    parks rather than driving an arbitrary option."""
    want = str(value).strip().lower()
    if not want:
        return None
    for option in options:
        if str(option).strip().lower() == want:
            return option
    contains = [o for o in options if want in str(o).strip().lower()]
    return contains[0] if len(contains) == 1 else None


def _option_field(fld, value) -> FieldValue:
    """The driven FieldValue this reconnect injects: the captured option
    wording(s), carrying the field's own key/label/type/locator so the existing
    `_drive_control_field` per-option mechanism drives it unchanged."""
    return FieldValue(key=fld.key, label=fld.label, type=fld.type,
                      locator=fld.locator, value=value)


def _drop_skip(resolved: ResolvedValues, key: str) -> None:
    """Drop the kernel's exact-match skip for `key` (the field is now driven).
    Mirrors greenhouse's education reconnect: in-place so callers keep the same
    `ResolvedValues`."""
    resolved.skipped[:] = [(k, r) for k, r in resolved.skipped if k != key]


# -- fill(): the Provider contract's ordered sequence (DOM path) ----------------
# (1) never-send FIRST, (2) drive every field via base.py NATIVE primitives
# (native select / type_human / _safe_upload) or, for a checkbox/radio, via the
# kernel's control_toolkit.drive_control, (3) readback-gate what counts as
# filled, (4) DOM-sweep as the PRIMARY completeness source forces NOT_COMPLETE
# on any gap, (5) return the existing FillReport dataclass.


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

    # Snapshot the URL BEFORE any page interaction (the banner dismissal below
    # queries locators): a navigation during the fill -- including one a stray banner
    # click might trigger -- is then caught by the end-of-fill url-unchanged check.
    pre_url = _current_url(page)

    # (1b) DISMISS THE PRIVACY BANNER before any field is driven. Lever renders a
    # fixed "This website uses cookies..." overlay (DENY / ACCEPT) and is the only
    # vendor whose banner was never dismissed; a fixed aria-region overlay intercepts
    # focus/keyboard, so a keystroke meant for a field can land on the banner instead.
    # Dismissing it here -- at fill START, BEFORE the location drive -- means it can
    # never sit between a field's keystrokes and the control they were meant for.
    #
    # This banner is NOT the cause of the geocoded-location commit failure. An earlier
    # revision of this comment named it "the leading explanation"; a full-form
    # instrumented live run REFUTED that (the banner's deny button was clicked and
    # confirmed dismissed at t=16.9s, and the location still parked at t=209s). The
    # proven root cause is the CHECK-THEN-USE gap in `_drive_location_field` (see the
    # location-driver block below), which that same run measured directly. The
    # dismissal stays because a keyboard-intercepting overlay is worth removing on its
    # own merits, not because it explains the location gap.
    _dismiss_privacy_banner(page)
    readback_mismatches: list[dict] = []
    extra_skips: list[tuple[str, str]] = []
    uploads: list[dict] = []
    filled_keys: set[str] = set()

    # (2) + (3) drive + readback-gate every resolved field via the NATIVE path.
    # Two fields are DEFERRED to the end of the pass (see the location block
    # below): the geocoded location typeahead is driven LAST so its open dropdown
    # can never swallow keystrokes meant for another field, and every simple
    # text/url field that passed is re-verified afterwards in case a later dropdown
    # interaction cleared it (F-7: the live urls[LinkedIn] regression, generalized
    # from the RS-f phone case -- a typeahead blur/focus race can clear ANY plain
    # field filled before it, not just the phone). `reverify_after_location`
    # records each simple field that landed so the re-verify can re-read (and, on a
    # regression, re-drive ONCE) exactly those.
    location_fields: list = []
    reverify_after_location: list = []
    for fv in values.fields:
        if _is_upload(fv):
            _fill_upload(page, fv, uploads, extra_skips, filled_keys)
            continue
        if _is_control_field(fv):
            try:
                _drive_control_field(page, fv, readback_mismatches, extra_skips,
                                     filled_keys)
            except FillSafetyError:
                raise
            except Exception as exc:  # per-field fill error is fail-soft
                extra_skips.append((fv.key, f"fill-error: {exc}"))
            continue
        if _is_location_field(fv):
            location_fields.append(fv)   # driven LAST (see below), never inline
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
            reverify_after_location.append(fv)  # re-checked post-typeahead (F-7)
        else:
            readback_mismatches.append(
                {"key": fv.key, "intended": fv.value, "actual": actual})
            extra_skips.append(
                (fv.key, "value did not take (readback mismatch)"))

    # Drive the geocoded location typeahead(s) LAST, after every other text field
    # (ordering fix, RS-f): an OPEN geocoded dropdown swallows keystrokes, and a
    # dropdown left open over a later field is the leading explanation for the
    # first-pass phone readback mismatch. Fail-soft per field, exactly like the
    # native path above.
    location_driven = False
    for fv in location_fields:
        location_driven = True
        try:
            _drive_location_field(page, fv, readback_mismatches, extra_skips,
                                  filled_keys)
        except FillSafetyError:
            raise
        except Exception as exc:  # per-field fill error is fail-soft
            extra_skips.append((fv.key, f"fill-error: {exc}"))

    # SIMPLE-FIELD hardening (RS-f phone case, generalized in F-7): a dropdown
    # interaction can clear a plain text/url field that already readback-passed (a
    # blur/focus race). The live urls[LinkedIn] regression filled in the first pass
    # then read back empty after the location typeahead, exactly as the RS-f phone
    # did. Only after a location interaction, re-read each simple field that landed;
    # on a regression re-drive it ONCE and re-read (bounded, a single retry). The
    # live probe proves the typing primitive itself is fine, so this repairs only a
    # value a later interaction cleared and never re-drives a field that still reads
    # back (a phone keeps its digit-only tolerance; every other field the exact
    # kernel readback).
    if location_driven:
        for fv in reverify_after_location:
            if _reverify_text_field(page, fv):
                continue
            filled_keys.discard(fv.key)
            readback_mismatches.append(
                {"key": fv.key, "intended": fv.value, "actual": ""})
            extra_skips.append(
                (fv.key, "value cleared after a later interaction "
                         "(readback mismatch)"))

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
    # schema), projected into the key space the LIVE sweep used (see
    # `_reconciled_schema_required`); `dom_required` is the LIVE sweep, which
    # wins. Any mismatch forces NOT_COMPLETE via _sweep_gaps, and a checkbox/radio
    # handed off above (or any field whose readback did not confirm) surfaces
    # through kernel.resolve._completeness.
    from engine.kernel.resolve import _completeness

    dom_required = base.sweep_required(page)
    schema_required = _reconciled_schema_required(fieldmap, dom_required)
    mismatch = base.completeness_mismatch(schema_required, dom_required)

    filled = len(filled_keys)
    all_skips = list(values.skipped) + extra_skips
    fillable_total, required_unfilled, justified_skips = _completeness(
        fieldmap, filled_keys, all_skips, filled, vendor_resolver=None)
    required_unfilled = required_unfilled + _sweep_gaps(
        mismatch, _filled_alias_names(fieldmap, filled_keys))

    return FillReport(
        vendor=vendor, company=company or fieldmap.posting_id,
        posting_id=fieldmap.posting_id,
        fillable_total=fillable_total, filled=filled,
        required_unfilled=required_unfilled, justified_skips=justified_skips,
        uploads=uploads, skipped=all_skips,
        readback_mismatches=readback_mismatches, validation_errors=[],
        url_unchanged=url_unchanged, screenshot="", ts=ts)


# -- privacy/cookie banner dismissal (live gopuff, 2026-07-19) -------------------
# Lever renders a fixed "This website uses cookies..." overlay with DENY and ACCEPT
# buttons. It is an aria-region overlay that intercepts focus/keyboard, so a
# geocode-dropdown ArrowDown/Enter can land on the banner instead of the widget,
# and the uncommitted geocode text is then wiped on blur (the live location read
# EMPTY). Lever was the only vendor never dismissing its banner; the pattern mirrors
# `workable.fill._dismiss_cookie_banner` (adapted, not shared: the selectors are a
# vendor concern). PREFERENCE (owner privacy policy: application-necessary consent
# ONLY, no optional-data use): the DENY-optional button, else ACCEPT, else any
# non-settings banner button. The SETTINGS button is never a target (it opens a
# second dialog).

# Buttons living inside a cookie/consent/privacy region (class/id/aria-label). A
# contains-match, conservative: no live DOM export exists this wave (pixel evidence
# only), so this is pinned on the CONVENTION, like workable's banner selectors.
_BANNER_BUTTON_CSS = (
    '[class*="cookie" i] button, [id*="cookie" i] button, '
    '[class*="consent" i] button, [id*="consent" i] button, '
    '[class*="privacy" i] button, [id*="privacy" i] button, '
    '[aria-label*="cookie" i] button, [aria-label*="consent" i] button')
_BANNER_DENY_RE = re.compile(
    r"\bdeny\b|\bdecline\b|\breject\b|\brefuse\b|"
    r"only necessary|necessary only|only essential|essential only", re.I)
_BANNER_ACCEPT_RE = re.compile(
    r"\baccept\b|\ballow\b|\bagree\b|got it|\bok\b", re.I)
_BANNER_SETTINGS_RE = re.compile(
    r"settings|preferences|manage|customi[sz]e", re.I)


def _dismiss_privacy_banner(page) -> None:
    """Dismiss the Lever privacy/cookie banner, PREFERRING the deny-optional button.

    A no-op on a page with no banner (every ordinary apply page whose banner already
    cleared, and every offline fake without one -- `_visible_locators` treats a
    missing method as zero matches). Best-effort: an unclickable/stale button is
    skipped, but a FillSafetyError (a submit-like name) is never swallowed. The
    SETTINGS button is never targeted. Routed through the SAME `base._safe_click`
    submit-denylist primitive every other click in this module uses."""
    named = [(button, _accessible_name(button))
             for button in _visible_locators(page, _BANNER_BUTTON_CSS)]
    candidates = [(button, name) for button, name in named
                  if not _BANNER_SETTINGS_RE.search(name or "")]
    if not candidates:
        return
    for pattern in (_BANNER_DENY_RE, _BANNER_ACCEPT_RE):
        for button, name in candidates:
            if pattern.search(name or "") and _click_banner_button(button, name):
                return
    for button, name in candidates:          # fallback: any non-settings banner button
        if _click_banner_button(button, name):
            return


def _click_banner_button(button, name: str) -> bool:
    """Click one banner button through the sanctioned primitive. Returns True on a
    clean click; False on a stale/unclickable control (skip and try the next). A
    FillSafetyError (a submit-like name) is NEVER swallowed -- a genuinely ambiguous
    banner button aborts the fill, same as every other click in the module."""
    try:
        base._safe_click(button, name)
    except FillSafetyError:
        raise
    except Exception:
        return False
    return True


# per-vendor variant (differs from the kernel generic): Lever's DOM sweep is authoritative (no independent schema), so the required_unfilled reason strings differ
def _sweep_gaps(mismatch: dict, filled_names: set[str]) -> list[dict]:
    """Synthetic `required_unfilled` entries for a DOM-sweep mismatch.

    For Lever the sweep is authoritative, so a `dom_only` mismatch ALWAYS forces
    NOT_COMPLETE: the live form requires a field the capture-time DOM snapshot did
    not carry (the ground truth winning over the snapshot). This half is
    unconditional and must stay so.

    `schema_only` (the snapshot marked a field required but the live sweep did not
    find it required) is a capture/fill DRIFT detector and still blocks -- but ONLY
    for a field that is not filled. `filled_names` carries the normalized aliases of
    the required fields that DID land, and a drift entry naming one of them is
    dropped, because a field that IS filled cannot be "required_unfilled" by
    definition.

    That narrowing is what the LIVE page demands (W5B-LEVER round 3). Lever's resume
    input carries no `required` attribute and is CSS-hidden, and the sweep's asterisk
    arm never fires on a real Lever page (the marker is a U+2731 inside a DIV, while
    the kernel filters `label, legend` on an ASCII "*"), so the REQUIRED resume field
    has no sweep token on EITHER arm. Capture still marks it required (correctly),
    so it lands in `schema_only` and the old unconditional loop reported
    `dom-sweep:resume / cv` as a required gap on a form whose CV had actually
    uploaded: a phantom gap, the same class of bug as the dom_only one this wave
    removed, in the opposite direction.

    This is NOT a silencer: an UNFILLED required field the sweep cannot see still
    blocks (through this branch, and through `kernel.resolve._completeness`), and the
    dom_only half is untouched, so a required control the capture missed still forces
    NOT_COMPLETE."""
    gaps: list[dict] = []
    for name in mismatch.get("dom_only") or ():
        gaps.append({
            "key": f"dom-sweep:{name}", "label": name,
            "reason": "the live DOM requires this field but the capture-time "
                      "field map did not (DOM sweep is authoritative for Lever)"})
    for name in mismatch.get("schema_only") or ():
        if name in filled_names:
            continue      # filled: the sweep simply cannot see it, not a gap
        gaps.append({
            "key": f"dom-sweep:{name}", "label": name,
            "reason": "the captured field map marked this required but the live "
                      "DOM sweep did not find it required (capture/fill drift)"})
    return gaps


def _filled_alias_names(fieldmap: FieldMap, filled_keys: set[str]) -> set[str]:
    """The normalized aliases (label + submission name attribute) of every REQUIRED
    field that actually landed and readback-confirmed. `_sweep_gaps` uses this to
    tell a genuine capture/fill drift from a sweep blind spot on a filled field."""
    names: set[str] = set()
    for fld in fieldmap.required_fields():
        if fld.key in filled_keys:
            names.update(_normalize_name(alias)
                         for alias in _field_alias_names(fld))
    return names - {""}


# -- key-space reconciliation: label vs submission name (W5B-LEVER F2) ----------
# A Lever control answers to TWO names, and the sweep emits BOTH of them:
#   * `sweep_required` arm 1 (`[required], [aria-required='true']`) takes the
#     control's accessible name, which falls back to the `name` ATTRIBUTE when the
#     control carries no aria-label / placeholder ("name", "resume",
#     "cards[a1b2c3][field0]" -- fill_toolkit.py:88-108 + 147-157);
#   * `sweep_required` arm 2 (`label, legend` whose text shows an asterisk) takes
#     the HUMAN LABEL ("full name", "resume / cv").
# The capture-side required set, meanwhile, was built purely from human LABELS.
# Human labels never equal submission names, so on the live agicap run
# (2026-07-12) the sweep's name-attribute tokens had nothing to reconcile against
# and became spurious dom_only gaps ("dom-sweep:name", "dom-sweep:cards[...]"),
# forcing NOT_COMPLETE on a form that was in fact filled.
#
# The fix projects the capture-side set into EVERY key space the sweep itself used
# for that field. Submitting only the FIRST matching alias is NOT enough (round-1
# defect, caught in review): a base field whose label reaches the sweep through
# arm 2 would consume "full name" and leave its own arm-1 token "name" orphaned,
# so "dom-sweep:name" -- the verbatim live symptom -- kept firing. The SWEEP IS
# NEVER TOUCHED and stays the authoritative oracle.


def _field_alias_names(fld) -> tuple[str, ...]:
    """Every name the live sweep could legitimately give this captured field: its
    human label AND the control's submission `name` attribute, which Lever's
    capture records as `Field.key` (see `capture._control_name`)."""
    return tuple(name for name in (fld.label, fld.key) if name)


def _reconciled_schema_required(fieldmap: FieldMap,
                                dom_required: set[str]) -> set[str]:
    """The capture-time required set, named every way the LIVE sweep named it, so
    `completeness_mismatch` compares like with like.

    Per required field, EVERY alias (human label and/or submission name attribute)
    the sweep actually emitted is submitted to the diff -- the sweep names one
    control through both of its arms, so consuming only the first match leaves the
    other token orphaned as a phantom `dom_only` gap. When the sweep emitted
    NEITHER alias, the field keeps its label and therefore still surfaces as a
    `schema_only` gap. This reconciles a key-space difference ONLY -- it can never
    invent coverage:

      * every name added here is a name the SWEEP ITSELF emitted; a required
        control the CAPTURE genuinely missed matches no field's aliases, so it
        stays a `dom_only` gap and still forces NOT_COMPLETE (the falsification
        case, pinned by test_lever_sweep_uncaptured_required_still_blocks);
      * a captured required field the live sweep no longer requires still
        surfaces as `schema_only` (capture/fill drift), unchanged;
      * whether a field was actually FILLED is not decided here at all -- that
        stays with the readback gate + `kernel.resolve._completeness`.
    """
    dom_names = {_normalize_name(name) for name in (dom_required or set())} - {""}
    schema: set[str] = set()
    for fld in fieldmap.required_fields():
        matched = [alias for alias in _field_alias_names(fld)
                   if _normalize_name(alias) in dom_names]
        schema.update(matched or [fld.label or fld.key])
    return schema


# -- per-field driving: NATIVE path only (no react-select) ----------------------
# text/email/phone/url via type_human, a native <select> via select_option; a
# checkbox/radio never reaches `_fill_field` (see `_drive_control_field` below).


# A native (server-rendered) select or a resolved multi-select option list: the
# full _SELECT_TYPES vocabulary, all driven by select_option -- NOT react-select
# (that is the greenhouse-only override this module deliberately does not share).
_NATIVE_SELECT_TYPES = frozenset({
    "multi_value_single_select", "multi_value_multi_select", "yes_no",
})

# -- checkbox/radio driving: the kernel's control_toolkit.drive_control --------
# A single checkbox is ONE physical control (`fv.value` a bool). A radio group or
# a multi-checkbox group is N physical controls sharing one submission NAME
# (`fv.value` a str -- the one chosen option -- or a list of chosen options,
# respectively); each option is its own control, located by its own accessible
# name, because the option's own wording IS its accessible name
# (`capture._radio_option_label` / `_checkbox_label`). The field counts as
# `filled` only once EVERY option it drove has readback-confirmed -- a partial
# tick is not a silent pass, it surfaces as a required gap exactly like any other
# unconfirmed field.


def _is_control_field(fv) -> bool:
    """True for a field this module drives through `drive_control` rather than
    the native text/select path: exactly the checkbox/radio locator roles."""
    return fv.locator.role in (ControlKind.CHECKBOX, ControlKind.RADIO)


def _group_css(key: str) -> str:
    """CSS matching every physical control posting under a group's submission NAME
    `key` -- every option of a radio/checkbox group shares one `name` attribute, so
    this matches the whole group, not one option. Paired with a role+accessible-name
    locator via `.and_()` in `_locate_option`, it narrows that match down to the ONE
    option belonging to THIS group."""
    if not key:
        return ""
    name = _CSS_ESCAPE_RE.sub(r"\\\1", key)
    return f'input[name="{name}"]'


def _locate_option(page, role: str, key: str, option: str):
    """The ONE control, among every control sharing a group's submission name,
    whose accessible name is this specific OPTION's wording -- the same
    role+name convention `base._locate` uses for a whole field, applied to one
    option of a group instead of the group's own question.

    LIVE FAILURE (palantir, 2026-07-18): a bare `page.get_by_role(role, name=option,
    exact=True)` is PAGE-WIDE, so a posting with several coexisting yes/no cards --
    each rendering an option worded "No" -- strict-mode-violates: it resolved to 4
    elements (one "No" radio per card), never the one belonging to the group this
    call is actually driving. `.and_()` intersects the role+name match with the
    group's own submission-name CSS (`_group_css`), the same round-8 convention
    `_name_css`/`_locate` use for a whole field, so only the option that is BOTH
    worded `option` AND posted under `key` can ever match. Falls back to the bare
    role+name locator when that intersection does not resolve to exactly one
    control (the same residual `_locate` already tolerates, for a group whose
    primary control carries no submission name at all)."""
    css = _group_css(key)
    if css:
        scoped = page.get_by_role(role, name=option, exact=True).and_(page.locator(css))
        if _resolves_to_one(scoped):
            return scoped
    return page.get_by_role(role, name=option, exact=True)


def _drive_one_control(page, key: str, role: str, locator, value: bool, name: str):
    """One `drive_control` call for one physical checkbox/radio control."""
    return drive_control(ControlSpec(
        key=key, kind=role, locator=locator, value=value, name=name))


def _drive_control_field(page, fv, readback_mismatches: list[dict],
                         extra_skips: list[tuple[str, str]],
                         filled_keys: set[str]) -> None:
    """Drive a checkbox/radio FieldValue, single control or group, and fold the
    per-control outcome(s) into ONE verdict for this field's key."""
    role = fv.locator.role
    name = fv.locator.name or fv.label
    if isinstance(fv.value, bool):
        outcomes = [_drive_one_control(page, fv.key, role, _locate(page, fv),
                                       fv.value, name)]
    elif isinstance(fv.value, str):
        outcomes = [_drive_one_control(
            page, fv.key, role, _locate_option(page, role, fv.key, fv.value), True,
            fv.value)]
    elif isinstance(fv.value, list):
        outcomes = [_drive_one_control(
            page, fv.key, role, _locate_option(page, role, fv.key, option), True,
            option)
            for option in fv.value]
    else:
        extra_skips.append((
            fv.key, "control value must be bool, str or list, got "
                    f"{type(fv.value).__name__}"))
        return

    if outcomes and all(outcome.confirmed for outcome in outcomes):
        filled_keys.add(fv.key)
        return
    readback_mismatches.append({
        "key": fv.key, "intended": fv.value,
        "actual": [outcome.actual for outcome in outcomes]})
    reasons = "; ".join(outcome.reason for outcome in outcomes if outcome.reason)
    extra_skips.append((fv.key, reasons or "value did not take (readback mismatch)"))


# -- the locator: the SUBMISSION NAME, not the question (W5B-LEVER round 8) -----
# A locator is a PAIR (role, name) and BOTH halves must be true of the control.
# Earlier rounds corrected the ROLE; the NAME half was still the human QUESTION, and
# a truthful role with a name the control does not carry resolves to ZERO elements
# exactly as a fictional role does. Lever's custom questions render (gopuff, verbatim
# live 2026-07-14):
#
#   <li class="application-question custom-question"><div>
#     <div class="application-label full-width text"><div class="text">
#       Current Street Address<span class="required">*</span></div></div>
#     <div class="application-field full-width required-field">
#       <input required class="card-field-input" type="text"
#              placeholder="Type your response"
#              name="cards[d8c298f6-...][field0]"></div></div></li>
#
# The question sits OUTSIDE any `<label>` and the control is not associated with one,
# so the control's ACCESSIBLE NAME is its placeholder ("Type your response"), never the
# question. `get_by_role("textbox", name="Current Street Address")` therefore matched
# NOTHING: 14 custom-question controls across the four live postings resolved to zero,
# 11 of them REQUIRED, and each died as a silent fill error or a timeout.
#
# What the control DOES carry, uniquely, is its submission `name` attribute -- and the
# capture ALREADY records it, as `Field.key` (`capture._control_name`). So the fill
# locates by it. Verified over the four live DOMs (2026-07-14): for every field the fill
# drives that HAS a submission name -- 38 of the 39 driven controls --
# `input[name=KEY]:not([type=hidden]), select[name=KEY], textarea[name=KEY]` matches
# EXACTLY ONE control: never zero (a phantom), never several (a control the fill could
# type into by mistake). The 39th driven control has NO submission name at all and this
# CSS matches ZERO for it; it is located by the FALLBACK below, which is the residual the
# next paragraph names. Both halves are pinned offline: the invariant against the fixture
# DOM by `test_lever_every_drivable_field_locator_resolves_to_exactly_one_control`, the
# residual by
# `test_lever_locate_falls_back_to_the_kernel_for_a_control_with_no_submission_name`.
#
# `base._locate` (role + accessible name) is FROZEN kernel and stays the FALLBACK, for
# the one live shape with no submission name to locate by: swile's OPTIONAL demographic
# `<select class="candidate-location">` carries NO `name` attribute at all, so its
# captured key is a slug of the question and no name-CSS can find it. That field is
# UNCHANGED by this wave -- it took the role+name locator before and it still does; it is
# a KNOWN RESIDUAL, not a regression, and being optional it degrades fail-soft (a fill
# error is a skip, never a blocked application). Building the vendor's own locator in the
# vendor plugin is the sanctioned pattern: greenhouse's CSS-based `select_react_combobox`
# is the precedent.
# The role work of the earlier rounds STANDS -- the role is still the DOM fact that
# decides which drive path fires (`_is_control_field`, checked before either locator
# strategy runs), and it is still what the fallback locator resolves by.

_CSS_ESCAPE_RE = re.compile(r'(["\\])')


def _name_css(key: str) -> str:
    """The CSS that finds the ONE control posting under submission name `key`.

    Constrained to the three form-control tags (the same three `capture._is_form_control`
    reads) so a `<meta name=...>` / `<a name=...>` / `<button name=...>` elsewhere on the
    page can never answer a field's locator, and excluding `type=hidden` so Lever's hidden
    twins (the consent `0` mirror; the round-3 base mirrors) can never be typed into
    instead of the control the applicant sees."""
    if not key:
        return ""
    name = _CSS_ESCAPE_RE.sub(r"\\\1", key)
    return (f'input[name="{name}"]:not([type="hidden"]), '
            f'select[name="{name}"], textarea[name="{name}"]')


def _resolves_to_one(locator) -> bool:
    """True iff this locator matches EXACTLY ONE element on the live page. Zero is a
    phantom (the fill would silently do nothing, or time out); more than one and the
    fill could type into the wrong control. Either way the submission-name locator is
    not usable and the caller falls back."""
    try:
        return locator.count() == 1
    except Exception:
        return False


def _locate(page, fv):
    """The control to drive: located by its SUBMISSION NAME when that resolves to
    exactly one element, else by the kernel's role+accessible-name locator."""
    css = _name_css(fv.key)
    if css:
        by_name = page.locator(css)
        if _resolves_to_one(by_name):
            return by_name
    return base._locate(page, fv)


def _fill_field(page, fv) -> tuple[bool, Any]:
    """Drive one non-upload, non-handoff field via the native path; returns
    (landed, actual-read-back). NEVER routes through `base.select_react_combobox`
    -- that is greenhouse's widget and would mis-drive Lever's native select.

    A phone field that the kernel readback called a MISMATCH is re-judged on its
    digits alone (`_phone_readback_ok`); nothing else about the readback gate
    changes. A phone is also given a BOUNDED settle after typing so an as-you-type
    reformat handler finishes rewriting the value BEFORE it is read back (live
    gopuff), avoiding a false mismatch against a half-reformatted string."""
    locator = _locate(page, fv)
    _apply_native(locator, fv)
    if _is_phone_field(fv):
        _wait_ms(page, _PHONE_SETTLE_MS)
    actual, ok = base._readback(locator, fv.value)
    if not ok and isinstance(fv.value, str) and _is_phone_field(fv):
        ok = _phone_readback_ok(actual, fv.value)
    return ok, actual


# -- phone-tolerant readback (W5B-LEVER F1; the kernel `_readback` is UNTOUCHED) -
# LIVE FAILURE (agicap, 2026-07-12): the phone field reported "value did not take
# (readback mismatch)" although the number had landed. Phone widgets REFORMAT the
# value as it is typed (regrouping, spacing, an international prefix), and the
# kernel's readback compares `_norm(actual) == _norm(value)` -- an outer-whitespace
# strip plus lowercase, nothing more (kernel/fill_toolkit.py:458-465) -- so a
# correctly-landed number reads back formatted and false-fails.
#
# The re-judge is narrow BY CONSTRUCTION and cannot launder a bad fill:
#   * it only ever re-examines a readback the kernel ALREADY called a mismatch
#     (a passing readback is never revisited, a failing one for any NON-phone
#     field is never loosened);
#   * it only fires on a phone-shaped control (tel-typed, or a label naming a
#     phone);
#   * it confirms the fill ONLY when the DIGIT SEQUENCES are identical -- genuinely
#     different digits still mismatch (pinned by
#     test_lever_phone_wrong_digits_still_mismatch), and an empty intent or an
#     empty readback never matches, so a value the page silently dropped stays an
#     honest required gap.

_PHONE_TYPES = frozenset({"tel", "phone", "input_tel", "input_phone"})
_NON_DIGITS_RE = re.compile(r"\D")
# Bounded settle (ms) after typing a phone, so an as-you-type reformat handler
# finishes before the readback (live gopuff). Never an unbounded poll -- a single
# fixed wait, so a widget that never reformats simply loses ~250ms.
_PHONE_SETTLE_MS = 250


def _is_phone_field(fv) -> bool:
    """A phone-shaped control: a tel-typed field, or one whose normalized label
    names a phone (Lever's own base phone input renders as plain text, so the
    label is the signal that fires live)."""
    if (fv.type or "").strip().lower() in _PHONE_TYPES:
        return True
    return "phone" in _normalize_name(fv.label)


def _phone_digits(value) -> str:
    """The bare digit sequence of a phone value, with a leading `00` international
    prefix folded away so it compares equal to the `+` form (`+`, spaces, dashes,
    brackets carry no digits; `0044 20 ...` and `+44 20 ...` are one number)."""
    digits = _NON_DIGITS_RE.sub("", str(value or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def _phone_readback_ok(actual, value) -> bool:
    """True iff a reformatted phone readback carries EXACTLY the digits that were
    typed. An empty intent, or an empty/different readback, is never a match."""
    intended = _phone_digits(value)
    return bool(intended) and intended == _phone_digits(actual)


# LIVE FAILURE (gopuff, 2026-07-19): the phone rendered "+3935100000007 0000" for an
# intended "+39 351 000 0000" -- a genuine ON-PAGE corruption. Lever's phone input
# runs an as-you-type reformat handler; typing our OWN spaces fights its mask (the
# handler moves the caret to manage grouping between keystrokes, so a following digit
# lands out of position and digits duplicate/reorder). Typing DIGITS-ONLY (with a
# leading + kept) lets the mask insert its own spaces cleanly, and the digit-tolerant
# readback matches whatever grouping it renders.
def _phone_type_form(value) -> str:
    """The string to TYPE into a phone input: the leading + (when present) plus the
    bare digits, with NO spaces/dashes/brackets. A masked/tel widget manages its own
    grouping; typing our formatting interleaves with its reformatter and corrupts the
    value, so we type only what the mask cannot fight."""
    raw = str(value or "")
    plus = "+" if raw.lstrip().startswith("+") else ""
    return plus + _NON_DIGITS_RE.sub("", raw)


def _apply_native(locator, fv) -> None:
    """Write one value via the safe native action for its shape: `select_option`
    for a native select (single or multi) or a resolved option list, else
    `type_human` (human-cadence keystrokes, NEVER `fill()`) for the reCAPTCHA v3
    score protection the W5 spec section 3 requires. No boolean branch: a
    checkbox never reaches here (it is handed off in `fill()`), so this module
    never issues a `.check()` / `.click()` that hCaptcha could intercept.

    Every text drive CLEARS the field first (`_clear_text_field`). `type_human` only
    ever APPENDS keystrokes, and a Lever field is not always empty when it is driven:
    the page can PRE-FILL a base field (name/email/phone/location) from the
    candidate's session/profile (the live gopuff run: phone pre-filled, so the
    first-pass type appended and the readback mismatched), and a re-drive fires on a
    field a dropdown interaction left with a residual. Typing onto either doubles the
    content and the readback fails. Clearing first makes the drive REPLACE, so it is
    correct whether the field started empty, pre-filled, or residual. A native select
    needs no clear (`select_option` replaces the selection).

    A phone field is typed DIGITS-ONLY (`_phone_type_form`): its as-you-type mask
    fights typed spaces and corrupts the value (live gopuff), so we type only the
    digits (and a leading +) and let the mask group them."""
    value = fv.value
    if isinstance(value, list):
        locator.select_option(label=value)
    elif fv.type in _NATIVE_SELECT_TYPES:
        locator.select_option(label=value)
    else:
        _clear_text_field(locator)
        text = _phone_type_form(value) if _is_phone_field(fv) else str(value)
        base.type_human(locator, text)


# -- simple-field re-verify: the end-of-fill hardening pass (RS-f, F-7) ----------
# A dropdown interaction (the geocoded location typeahead is the only one Lever
# renders) can clear a plain text/url/phone field that already readback-passed,
# via a blur/focus race. `fill()` re-verifies every simple field it filled AFTER
# the location drive: a re-read that still passes leaves the fill untouched (the
# common case; the probe proves the typing primitive works), and a regression is
# re-driven ONCE and re-read. Bounded to a single retry: a field that does not read
# back after one re-drive is an honest readback mismatch, never an unbounded retype
# loop. Generalized in F-7 from the RS-f phone-only pass to cover the live
# urls[LinkedIn] regression (a url field cleared by the same typeahead race); the
# phone keeps its digit-only tolerance, every other field uses the exact kernel
# readback.


def _reverify_text_field(page, fv) -> bool:
    """Re-read a simple text/url/phone field that already passed; on a regression
    re-drive it ONCE and re-read. Returns True iff it still (or again) reads back.
    The locator is re-resolved fresh (the module's fresh-locator discipline), so
    the re-read sees the control's current live state, not a stale handle from the
    first pass.

    The re-drive goes through `_apply_native`, which CLEARS the field before
    typing. A dropdown interaction does not always empty a field it disturbs -- it
    can leave a PARTIAL residual -- and `type_human` only ever APPENDS keystrokes, so
    re-typing onto a residual would DOUBLE it and the re-read would mismatch again
    (the R4 org regression: "None, final-year MEng student" filled in R3, then the
    location typeahead left a residual and a blind re-type doubled it). Clearing in
    `_apply_native` makes the re-drive REPLACE the content, so it repairs a field
    whatever state the interaction left it in (empty OR residual). A phone re-drive
    gets the SAME bounded reformat settle the first pass uses before the re-read, so
    an as-you-type mask is not read mid-reformat."""
    locator = _locate(page, fv)
    if _text_reads_back(locator, fv):
        return True
    _apply_native(locator, fv)             # re-type once (clears first, then types)
    if _is_phone_field(fv):
        _wait_ms(page, _PHONE_SETTLE_MS)
    return _text_reads_back(locator, fv)


def _clear_text_field(locator) -> None:
    """Empty a text field via genuine keyboard events (select-all then delete), so
    a re-type REPLACES the content instead of appending to a residual a dropdown
    interaction left behind. Keyboard-only, never `fill("")`/JS injection -- the
    same reCAPTCHA v3 discipline `type_human` documents (an instant value-set scores
    as bot-like). Best-effort: a locator without `.press` (a partial fake, or the
    already-empty case) degrades to a plain re-type, which still repairs an
    empty-cleared field."""
    presser = getattr(locator, "press", None)
    if not callable(presser):
        return
    for key in ("Control+a", "Delete"):
        try:
            presser(key)
        except Exception:
            return


def _text_reads_back(locator, fv) -> bool:
    """True iff `fv` reads back its intended value, applying the SAME readback
    `_fill_field` uses: the exact kernel compare, with the digit-only tolerance
    added ONLY for a phone field (a reformatting phone that passed via the
    tolerance on the first pass must still pass on the re-read). Never loosens a
    non-phone readback."""
    actual, ok = base._readback(locator, fv.value)
    if not ok and isinstance(fv.value, str) and _is_phone_field(fv):
        ok = _phone_readback_ok(actual, fv.value)
    return ok


# -- geocoded location typeahead (RS-f; live probe 2026-07-19, palantir) --------
# Lever's base `location` field ("Current location") is a GEOCODED typeahead, not
# a plain text input. The visible `<input name="location">` is paired with a
# HIDDEN `<input name="selectedLocation">` -- the value Lever actually SUBMITS --
# and an in-field dropdown (`div.dropdown-container` > `div.dropdown-results`, plus
# a `div.dropdown-no-results` "No location found" node). Typing text alone leaves
# selectedLocation EMPTY: the value never takes, though the visible input reads
# back the typed string (so the plain native path would count a FALSE fill --
# exactly the "counted an attempt, not verified state" failure class). A value
# lands ONLY by typing a CITY-level query, awaiting the remote dropdown, and
# committing an option via the keyboard, which rewrites the visible input to the
# option name AND sets selectedLocation to a JSON blob {"name":..,"id":..}.
#
# So this driver derives a city query, types it, awaits the dropdown (a BOUNDED
# settle, mirroring the greenhouse education-typeahead shape -- never an unbounded
# poll), and commits the FIRST option whose text contains the city token via
# ArrowDown+Enter; a no-match parks HONESTLY (never commits a non-matching option).
# The readback GATE is the hidden selectedLocation, the authoritative submitted
# value: the fill counts only when it is non-empty AND its committed name contains
# the city token.
#
# P1-1, THE CHECK-THEN-USE GAP (root cause, measured on a full-form instrumented
# live run, 2026-07-20). The menu this widget renders is TRANSIENT: it closes the
# moment the input blurs. The previous driver awaited the dropdown, THREW AWAY the
# option texts the awaiter had just proved present (it returned a bare bool), and
# then issued an INDEPENDENT SECOND DOM query to resolve the option index. The live
# run measured 3 matching options on the first read and ZERO on the second, 22 ms
# later, with an IFRAME holding focus at the next sampled instant: the index
# resolved to None, the driver took the park branch, and ArrowDown/Enter were NEVER
# DISPATCHED AT ALL. The city the engine wanted was on screen 22 ms before it
# looked. The discriminator against an isolated probe that passes is ELAPSED TIME
# on the page (~15 s vs ~208 s), by which time the form's third-party widgets have
# loaded and can take focus; the bug is a race the easy path always wins.
#
# The fix has TWO halves, and the second is the load-bearing one:
#   (a) the awaiter RETURNS what it observed (`_LocationObservation`) and the index
#       is resolved from THAT observation by a PURE function (`_match_option_index`,
#       no DOM access), so one read decides one outcome;
#   (b) knowing the index is NOT sufficient to COMMIT. ArrowDown/Enter go through
#       the FOCUS-FOLLOWING page keyboard, so if the menu has closed and focus has
#       moved to an iframe they commit nothing while the driver believes it acted.
#       Carrying the index alone would therefore convert an honest park into a
#       SILENT WRONG STATE. `_location_committable` re-establishes focus on the
#       input and re-verifies the live option set is STILL the observed one
#       immediately before dispatch; when it is not, the attempt is abandoned and
#       the search is re-established from scratch (BOUNDED, `_LOCATION_COMMIT_
#       ATTEMPTS`). The authoritative post-condition is unchanged and unrelaxed:
#       `_selected_location_ok` on the hidden selectedLocation. A residual
#       sub-millisecond window between that re-verify and the keypress is
#       irreducible for a focus-following keyboard, so it is GATED rather than
#       eliminated -- it can only ever produce an honest park, never a false fill.
#
# OPTION SELECTOR (F-5, live probe palantir 2026-07-19). Typing "Bologna" populates
# `<div class="dropdown-results ...">` with option nodes
# `<div class="break-word dropdown-location ..." id="location-N">Bologna, ITA</div>`
# (ids location-0/1/2...). The R3 driver guessed `div.dropdown-result` (singular)
# and matched NOTHING against that markup ("no dropdown option matched the city
# 'Bologna'"), so a city that WAS in the dropdown never committed. The selector is
# now pinned to the live node, `div.dropdown-results div.dropdown-location` (the
# location-N id pattern corroborates the shape). selectedLocation, on Lever's own
# stable submission name, remains what the gate turns on -- so even a future option
# selector miss parks the field (an honest gap), never a false fill.

# The value Lever submits (a stable submission NAME, not a guessed widget class).
_SELECTED_LOCATION_CSS = 'input[name="selectedLocation"]'
# The 'No location found' node. R5 (live probe palantir 2026-07-19): this node, the
# results container, and a loading node ALL exist EMPTY in the DOM from initial page
# load, so its mere PRESENCE (count>0) is NOT a terminal -- it is the negative
# terminal only once it becomes VISIBLE (see `_location_no_results`).
_LOCATION_NO_RESULTS_CSS = "div.dropdown-no-results"
# The dropdown option node (F-5, probe-verified: div.dropdown-location, id location-N).
_LOCATION_OPTION_CSS = "div.dropdown-results div.dropdown-location"
# Bounded settle schedule (cumulative ms), mirroring the greenhouse education
# typeahead: poll early and often. R5: the cap is raised to 4.0s (was 1.8s) because
# the live geocode rendered its options at ~1.45s UNDER PROBE LOAD and engine
# sessions run heavier; a query matching NOTHING still terminates on the VISIBLE
# no-results node or, failing both, at the cap (falling through to an honest
# readback), so it can never hang the fill.
_LOCATION_SETTLE_MS: tuple[int, ...] = (
    200, 500, 900, 1300, 1700, 2200, 2800, 3400, 4000)
# BOUNDED commit attempts (P1-1). Attempt 1 is the ordinary drive; the retries exist
# ONLY for the observed-but-not-committable race (the transient menu closed between
# the observation and the keypress), and are never spent on a genuine geocoder
# non-match -- a no-match breaks out immediately and parks by name, so a query the
# geocoder answers with nothing still costs exactly one search. Worst case is 3
# searches, each itself bounded by `_LOCATION_SETTLE_MS`; never an unbounded loop.
_LOCATION_COMMIT_ATTEMPTS = 3


class _LocationObservation(NamedTuple):
    """What ONE await of the geocoded dropdown actually SAW, carried across the
    check-then-use boundary instead of being discarded (P1-1). `texts` is the option
    set as rendered at the observing instant; `no_results` records that the widget's
    VISIBLE no-results node was the terminal that ended the wait. An empty `texts`
    with `no_results` False means neither terminal was ever observed within the
    bound -- a materially different fact from "the geocoder returned nothing", and
    the park reason keeps the two apart (`_location_park_reason`)."""

    texts: tuple[str, ...]
    no_results: bool


def _is_location_field(fv) -> bool:
    """True for Lever's geocoded 'Current location' typeahead: the base field
    posting under submission name `location` (Lever's stable base name; see
    `capture._LEVER_BASE_LABELS` and the module docstring's seeded reference). A
    geocoded typeahead must NOT go through the plain text path, which would type
    the full address, match no dropdown option, and leave selectedLocation empty
    while the visible input reads back the typed text -- a false fill. Guarded to a
    string-valued textbox so a hypothetical non-text `location` control is never
    mis-routed here."""
    return (_normalize_name(fv.key) == "location"
            and fv.locator.role == "textbox"
            and isinstance(fv.value, str))


def _location_city_query(value) -> str:
    """Derive a CITY-level query for the geocoded typeahead from a full postal
    address. Conservative and documented: split on commas, then take the FIRST
    part that carries no digit and is not the trailing country part -- a street
    line carries a house number (a digit) and is skipped, a region+postcode part
    carries a postcode (a digit) and is skipped, and the last part is the country.
    Falls back to the WHOLE string when no such part exists (a bare city, a
    country-only value, or an unparseable address), so a value the heuristic cannot
    split still drives the typeahead and reports its result honestly.

    Examples: "Via Rizzoli 4, Bologna, Emilia-Romagna, 40125, Italy" -> "Bologna"
    (the street part carries a digit and is skipped); "Lisbon, Portugal" ->
    "Lisbon"; "Portugal" -> "Portugal" (single part, the fallback)."""
    parts = [p.strip() for p in str(value or "").split(",")]
    parts = [p for p in parts if p]
    if len(parts) >= 2:
        for part in parts[:-1]:            # every part but the trailing country
            if not any(ch.isdigit() for ch in part):
                return part
    return str(value or "").strip()


def _drive_location_field(page, fv, readback_mismatches: list[dict],
                          extra_skips: list[tuple[str, str]],
                          filled_keys: set[str]) -> None:
    """Drive Lever's geocoded location typeahead and readback-gate it on the hidden
    selectedLocation value (the value Lever submits). CLEARS the input, types a city
    query, awaits the dropdown, commits the first city-matching option via the
    keyboard, and counts the fill ONLY when selectedLocation is non-empty and names
    the city.

    The clear is load-bearing (live gopuff run). The geocoded input is not always
    empty when the typeahead reaches it: Lever can PRE-FILL it from the candidate's
    session/profile with a full address. `type_human` APPENDS, so without the clear
    the geocoder searches "<pre-filled address><city>" -- a query it matches to
    nothing -- and the location parks, exactly as the engine did while a probe typing
    the city into a CLEAN field got results. Clearing first (this is NOT reached via
    `_apply_native`, so the clear is explicit here) makes the geocoder see the clean
    city query. The location field is ALSO excluded from the generic text pass
    (`_is_location_field` routes it here, never to `_fill_field`), so it is never
    double-driven; the pollution is the page pre-fill, which the clear handles.

    The search-and-commit runs as BOUNDED ATTEMPTS (P1-1, see the block comment
    above): each attempt observes the dropdown ONCE, resolves the option index from
    THAT observation rather than from a second independent query, and commits only
    while the widget is still in a committable state. An attempt whose menu closed
    under it is abandoned and the search re-established; a genuine non-match breaks
    out on the FIRST attempt (retrying cannot make a geocoder answer differently)
    and parks by name."""
    city = _location_city_query(fv.value)
    visible = _locate(page, fv)
    observation = _LocationObservation((), False)
    # The option set of the last attempt that DID contain the city. Sticky across
    # attempts: it is what separates "the geocoder had no match" from "the geocoder
    # had a match and the menu closed before it could be committed" in the park
    # reason, and a later attempt observing nothing must not erase that fact.
    matched_texts: tuple[str, ...] = ()
    for _ in range(_LOCATION_COMMIT_ATTEMPTS):
        observation = _location_search(page, visible, city)
        index = _match_option_index(observation.texts, city)
        if index is None:
            break                          # a genuine non-match: park, never retry
        matched_texts = observation.texts
        if not _location_committable(page, visible, observation.texts):
            continue                       # the menu closed under us: re-establish
        _commit_location_option(page, visible, index)
        selected = _selected_location_value(page)
        if _selected_location_ok(selected, city):
            filled_keys.add(fv.key)
            return
        # An option was committed but the SUBMITTED value disagrees (empty, or a
        # committed name that does not contain the city token): an honest mismatch,
        # not a fill -- the visible input and the option text are never the gate.
        # NOT retried: the widget answered, and it answered with the wrong place.
        _record_location_gap(
            fv, readback_mismatches, extra_skips, selected,
            _readback_visible(visible),
            "geocoded location did not commit: the submitted selectedLocation is "
            "empty or does not name the city")
        return
    # Nothing was committed: park HONESTLY, never commit a non-matching option.
    # selectedLocation stays empty, so a REQUIRED location surfaces as a genuine gap
    # through kernel.resolve._completeness.
    _press_location_key(page, visible, "Escape")
    _record_location_gap(
        fv, readback_mismatches, extra_skips,
        _selected_location_value(page), _readback_visible(visible),
        _location_park_reason(city, observation, matched_texts))


def _record_location_gap(fv, readback_mismatches: list[dict],
                         extra_skips: list[tuple[str, str]],
                         selected, visible_value, reason: str) -> None:
    """Book one honest location gap: a readback mismatch carrying BOTH the
    submitted selectedLocation and the visible input's post-commit value (the
    spec's dual readback), plus the skip that surfaces the field as unfilled."""
    readback_mismatches.append({
        "key": fv.key, "intended": fv.value,
        "actual": {"selectedLocation": selected, "visible": visible_value}})
    extra_skips.append((fv.key, reason))


def _location_search(page, visible, city: str) -> _LocationObservation:
    """ONE search of the geocoded typeahead: clear the input, type the city query,
    await the dropdown, and return WHAT THE AWAIT OBSERVED.

    Re-typing is also the deterministic RECOVERY from a menu that closed under a
    previous attempt (P1-1). A bare re-focus is NOT enough: the widget clears its
    results state when the input blurs, so the menu has to be re-established by a
    fresh query, not merely re-shown. `_clear_text_field` and `type_human` both drive
    the control through its own locator, which re-focuses it as a side effect of the
    keystrokes -- so an attempt that lost focus to another element gets the keyboard
    back here, before anything is observed."""
    _clear_text_field(visible)             # start clean: a page pre-fill would pollute the geocoder query
    base.type_human(visible, city)
    return _await_location_dropdown(page)


def _await_location_dropdown(page, settle_ms: tuple[int, ...] = _LOCATION_SETTLE_MS
                             ) -> _LocationObservation:
    """BOUNDED wait for the geocoded remote search to RESOLVE. Polls at the
    cumulative offsets (the same bounded shape `select_react_combobox`/the education
    typeahead use) and terminates on the POSITIVE render -- OPTION NODES appearing
    (`_location_option_texts`, count>0 on `div.dropdown-location`) or a VISIBLE
    no-results node (`_location_no_results`), the two real terminals.

    R5 residual fix: it must NOT terminate on the mere presence of the results
    container or the no-results node. Both exist EMPTY in the DOM from initial page
    load (live probe), so the R4 settle -- which keyed the negative terminal on the
    no-results node's COUNT -- returned instantly on the empty container and parked
    every location before any option rendered (~1.45s later). Awaiting the OPTION
    NODES (or the no-results node becoming VISIBLE) is what waits through the empty
    container. Returns an EMPTY observation once the whole bound is exhausted --
    never an unbounded poll, so a query the geocoder never answers falls through to
    the readback (which reports the empty result honestly) rather than hanging.

    P1-1: it returns the option texts it OBSERVED rather than a bare bool. The
    caller resolves the option index from this return value, so the observation that
    decides the outcome is the one that was actually made, not a second independent
    read taken an unbounded number of milliseconds later against a menu that may
    already have closed."""
    elapsed = 0
    for mark in settle_ms:
        _wait_ms(page, mark - elapsed)
        elapsed = mark
        texts = _location_option_texts(page)
        if texts:
            return _LocationObservation(tuple(texts), False)
        if _location_no_results(page):
            return _LocationObservation((), True)
    return _LocationObservation((), False)


def _match_option_index(texts: tuple[str, ...], city: str):
    """The index of the FIRST observed option whose text contains the city token, or
    None when none does. PURE: it reads the observation it is handed and touches no
    DOM, which is precisely what closes the check-then-use gap (P1-1) -- the widget
    cannot change under a decision made from an already-captured list. None means
    park (never commit a non-matching option); the returned index is navigated to by
    keyboard in `_commit_location_option`.

    The no-results terminal needs no separate check here: it is recorded in the
    observation, and an observation carrying it carries no option texts, so it
    yields None through the ordinary path."""
    want = _normalize_name(city)
    if not want:
        return None
    for i, text in enumerate(texts):
        if want in _normalize_name(text):
            return i
    return None


def _location_committable(page, visible, texts: tuple[str, ...]) -> bool:
    """True iff the widget is in a state where a focus-following keystroke can still
    commit the option `texts` was observed at (P1-1, requirement (b)).

    Knowing the index is NOT enough to commit. `_commit_location_option` dispatches
    ArrowDown/Enter on the PAGE keyboard, which follows focus: if the transient menu
    has closed and focus has moved (the live run measured an IFRAME holding focus
    22 ms after a populated read), those keys land somewhere else and commit nothing
    while this driver believes it committed. Acting on the carried index alone would
    turn today's honest park into a SILENT WRONG STATE, which is strictly worse.

    So: re-establish focus on the location input, THEN re-read the live option set
    and require it to be IDENTICAL to what was observed. Establishing focus is
    preferred over merely inspecting `document.activeElement`: it is deterministic
    (it puts the keyboard back where the keys must go) rather than diagnostic, and it
    needs no page-script evaluation. Reading the option set AFTER focusing is what
    proves the menu survived both the interference and the focus call.

    IDENTICAL, not merely non-empty: ArrowDown navigates the LIVE list, so an index
    resolved against a different list would highlight a different place. A changed
    list is treated as not-committable and the caller re-establishes the search
    rather than guessing.

    This re-read is NOT a second reintroduction of the check-then-use gap. It cannot
    decide to PARK -- a failure here sends the caller into a bounded re-search, and
    only the observation from `_await_location_dropdown` ever decides whether an
    option matched. It is a guard on the carried decision, not a second decider."""
    _focus_location_input(visible)
    return tuple(_location_option_texts(page)) == tuple(texts)


def _focus_location_input(visible) -> None:
    """Put the keyboard back on the location input immediately before a commit. The
    live failure trigger is another element (an embedded third-party widget iframe)
    taking focus mid-drive; a focus-following keyboard then drives that element
    instead. Best-effort: a locator without `.focus()` degrades to a no-op, and the
    committability re-read still gates the commit, so a partial fake or a locator
    that cannot focus can never turn into a false fill."""
    focuser = getattr(visible, "focus", None)
    if not callable(focuser):
        return
    try:
        focuser()
    except Exception:
        return


def _location_park_reason(city: str, observation: _LocationObservation,
                          matched_texts: tuple[str, ...]) -> str:
    """The recorded reason for an uncommitted location, distinguishing the four
    materially different things that can have happened.

    This matters beyond tidiness. The previous single reason -- "no dropdown option
    matched the city X" -- was honest about what the driver SAW and actively
    misleading about what HAPPENED: on the run that produced it, three options
    naming the city were on screen 22 ms earlier. It read as proof that the geocoder
    had returned nothing, and it sent four investigation rounds after a geocoding
    problem that did not exist. A reader of a park log must be able to tell a
    geocoder that answered NOTHING from a menu that closed before the commit."""
    if matched_texts:
        # Deliberately covers BOTH ways the widget can stop being committable: the
        # menu closing (the live case) and the menu re-rendering into a different
        # option set (which would make the observed index name a different place).
        # `_location_committable` does not distinguish them, so this reason must not
        # claim to either.
        return (f"geocoded location typeahead: the geocoder DID return a matching "
                f"option for the city {city!r} ({', '.join(matched_texts)}), but at "
                f"each of the {_LOCATION_COMMIT_ATTEMPTS} commit attempts the "
                f"dropdown was no longer the one that had been observed (closed, or "
                f"re-rendered), so no option could be committed without guessing "
                f"which one the keyboard would land on (selectedLocation left empty)")
    if observation.no_results:
        return (f"geocoded location typeahead: the geocoder returned NO location for "
                f"the city {city!r} (the widget's no-results terminal was shown; "
                f"selectedLocation left empty)")
    if observation.texts:
        return (f"geocoded location typeahead: the geocoder returned "
                f"{len(observation.texts)} option(s), none naming the city {city!r} "
                f"({', '.join(observation.texts)}; selectedLocation left empty)")
    return (f"geocoded location typeahead: the dropdown never resolved for the city "
            f"{city!r} within {_LOCATION_SETTLE_MS[-1]}ms -- no option and no "
            f"no-results terminal was ever observed, so the geocoder's answer is "
            f"UNKNOWN rather than empty (selectedLocation left empty)")


def _location_option_texts(page) -> list[str]:
    """The visible text of each rendered dropdown option (0 while the remote search
    is still in flight, or on any selector miss). Degrades to [] on a missing
    method / query failure -- never a hang or a raise."""
    locator_fn = getattr(page, "locator", None)
    if locator_fn is None:
        return []
    try:
        options = locator_fn(_LOCATION_OPTION_CSS)
        all_fn = getattr(options, "all", None)
        rows = all_fn() if callable(all_fn) else []
    except Exception:
        return []
    texts: list[str] = []
    for row in rows or []:
        getter = (getattr(row, "inner_text", None)
                  or getattr(row, "text_content", None))
        if not callable(getter):
            continue
        try:
            text = (getter() or "").strip()
        except Exception:
            text = ""
        if text:
            texts.append(text)
    return texts


def _location_no_results(page) -> bool:
    """True iff the widget's 'No location found' node is VISIBLE -- the negative
    terminal. R5 residual: the node EXISTS empty in the DOM from initial page load
    (alongside the empty results container and a loading node, live probe), so a
    COUNT check reads 'no results' the instant the field is touched, before the
    remote search has run -- which parked every location on the empty container. The
    node is SHOWN only once a search resolves with nothing, so VISIBILITY is the
    real terminal signal. Degrades to False on any missing method / query failure
    (biased toward 'still searching', so the bounded settle only ever ends early on a
    POSITIVE, VISIBLE render)."""
    locator_fn = getattr(page, "locator", None)
    if locator_fn is None:
        return False
    try:
        node = locator_fn(_LOCATION_NO_RESULTS_CSS)
    except Exception:
        return False
    return _any_node_visible(node)


def _any_node_visible(node) -> bool:
    """True iff any element the locator matches is VISIBLE. Handles both a live
    Playwright locator (its `.all()` yields one locator per match, each with
    `is_visible`) and a locator that answers `is_visible` directly (the offline
    fake). Degrades to False on any missing method / failure -- a hidden or
    unqueryable node is never a terminal."""
    all_fn = getattr(node, "all", None)
    if callable(all_fn):
        try:
            rows = all_fn()
        except Exception:
            return False
        for row in rows or []:
            checker = getattr(row, "is_visible", None)
            try:
                if callable(checker) and checker():
                    return True
            except Exception:
                continue
        return False
    checker = getattr(node, "is_visible", None)
    try:
        return bool(callable(checker) and checker())
    except Exception:
        return False


def _commit_location_option(page, visible, index: int) -> None:
    """Highlight the option at `index` and commit it via the keyboard: ArrowDown
    (index+1) times to reach it from the un-highlighted state, then Enter. A
    keyboard commit (not an option click) mirrors the react-select driver's own
    proven mechanism.

    CALL ONLY BEHIND `_location_committable`. This dispatches on the FOCUS-FOLLOWING
    page keyboard, so it drives whatever holds focus. "The visible input holds focus
    after typing" was the assumption this driver used to make, and the live full-form
    run refuted it: another element held focus by the time the commit was reached.
    The keys are therefore aimed by the caller's focus re-establishment, and the
    result is still gated on selectedLocation afterwards."""
    for _ in range(index + 1):
        _press_location_key(page, visible, "ArrowDown")
    _press_location_key(page, visible, "Enter")


def _press_location_key(page, locator, key: str) -> None:
    """Press `key` on the PAGE keyboard (focus-following, so it survives the widget
    re-rendering its own nodes) when the page exposes one; otherwise fall back to
    the locator's own `press` (the offline fake-harness path)."""
    keyboard = getattr(page, "keyboard", None)
    presser = getattr(keyboard, "press", None) if keyboard is not None else None
    if callable(presser):
        presser(key)
        return
    locator_press = getattr(locator, "press", None)
    if callable(locator_press):
        locator_press(key)


def _selected_location_value(page) -> str:
    """The hidden selectedLocation input's value (the value Lever submits), read on
    Lever's own stable submission name. Empty string when the field is absent /
    unreadable, so an un-committed typeahead reads back empty and never counts."""
    locator_fn = getattr(page, "locator", None)
    if locator_fn is None:
        return ""
    try:
        loc = locator_fn(_SELECTED_LOCATION_CSS)
        getter = getattr(loc, "input_value", None)
        return (getter() or "").strip() if callable(getter) else ""
    except Exception:
        return ""


def _selected_location_ok(selected: str, city: str) -> bool:
    """True iff selectedLocation is non-empty AND its committed name contains the
    city token. An empty selectedLocation (typed-but-not-committed), or a committed
    name for a different place, is never a fill."""
    if not selected:
        return False
    want = _normalize_name(city)
    return bool(want) and want in _normalize_name(_selected_location_name(selected))


def _selected_location_name(selected: str) -> str:
    """The committed place name inside selectedLocation. Lever sets a JSON blob
    {"name":..,"id":..}; a non-JSON value is treated as the name itself, so the
    city-token check stays meaningful whatever shape the widget wrote."""
    try:
        blob = json.loads(selected)
    except (ValueError, TypeError):
        return str(selected)
    if isinstance(blob, dict):
        return str(blob.get("name") or blob.get("label") or "")
    return str(blob)


def _readback_visible(visible) -> str:
    """The visible input's current value, recorded alongside selectedLocation for
    an honest dual readback. Best-effort: empty on any missing method / failure."""
    getter = getattr(visible, "input_value", None)
    if not callable(getter):
        return ""
    try:
        return (getter() or "").strip()
    except Exception:
        return ""


def _wait_ms(page, ms: int) -> None:
    """Best-effort settle: `page.wait_for_timeout` when the page exposes it (the
    live path and the fake), else `time.sleep`. No-op for a non-positive delta."""
    if ms <= 0:
        return
    waiter = getattr(page, "wait_for_timeout", None)
    if callable(waiter):
        waiter(ms)
    else:
        time.sleep(ms / 1000.0)
