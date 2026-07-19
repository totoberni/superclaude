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

1. DRIVE BY THE CONTROL, NOT BY THE SCHEMA TYPE (W5B-ASHBY round 7). On Ashby the
   graphql `type` does not tell you what the applicant operates: the SAME
   `ValueSelect` renders as a native radio group on 33 of 34 live fields and as a
   nameless React dropdown on the 34th, and the schema carries no display hint at
   all. So `capture` reads each control's ARIA role off the LIVE DOM (the role
   invariant; see `capture._rendered_roles`), and `fill()` routes on that role.
   The role is an OBSERVATION of the control, not the native-vs-react ROLE GUESS
   the older wording warned against: on this page, this control really is an
   `input[role=combobox]`.

   THE NAMELESS-COMBOBOX DRIVER (F1, generalised round 7). Ashby has exactly ONE
   combobox widget and both of its comboboxes are it: the geo Location field
   (30/30 postings) and the dropdown ValueSelect (1/1). Both are React
   autocompletes with NO id, NO name and NO aria-label, so NEITHER has an
   accessible name, `base._locate`'s role+name lookup reaches NEITHER (this is
   what timed out the 2026-07-12 acceptance run), and BOTH commit only when a
   `[role=option]` row is CLICKED. `_fill_ashby_combobox` mirrors the greenhouse
   combobox PATTERN (open, type a filter, wait bounded for the suggestion list,
   commit, confirm) with NONE of its react-select selectors (Ashby has no
   `#react-select-*` DOM): it anchors on the entry's `data-field-path` and commits
   a suggestion ONLY on a unique normalized match against the intended value,
   through a FAIL-CLOSED ladder (`_unique_suggestion`: exact, or a suggestion
   whose tokens are a SUBSET of the value's, never the reverse). The widget
   fuzzy-matches (the filter "Milan" offered "Christmas Island"), so a blind
   first-option commit would write a place the candidate never named; and a
   suggestion that merely CONTAINS the value ("London" -> "London, Ontario") is a
   DIFFERENT place, so it is refused too. No unique match leaves the field
   HONESTLY UNFILLED -> required_unfilled -> NOT_COMPLETE.

   THE CONTROLLED-COMPONENT SELECT DRIVER (`_apply_ashby_controlled`, W5.3) is
   kept for a select whose live control is NOT that combobox -- a native
   `<select>`, which HAS an accessible name and which `base._locate` reaches
   correctly. Ashby serves none today. It uses the berellevy onChange/onBlur
   technique (native value setter, then the `input`/`change`/`blur` events React's
   controlled component listens for) and is readback-gated like every other path.
   It MUST NEVER be aimed at the combobox: live, it writes the value into the
   widget's FILTER box, reads that same text back, and counts the field FILLED
   while the listbox is still open and nothing is selected -- a FALSE COMPLETE.
   See its own docstring for the 2026-07-13 proof. Neither greenhouse's
   react-select driver (`base.select_react_combobox`, whose node ids Ashby lacks)
   nor Lever's `locator.select_option` (no `<select>` to option-set) is ever used.
   Text fields type through `base.type_human` (genuine key events already commit a
   React text input); a file field uploads through `base._safe_upload`.

1c. SCHEMA/DOM RECONCILIATION (W5B-ASHBY F2). The DOM sweep stays the
   authoritative cross-check, but on an Ashby page it and the schema do not
   speak the same language (React widgets carry no native `required`; the
   sweep names controls by their PLACEHOLDER because Ashby ships no aria-label;
   off-step fields are not mounted), so `_reconcile` re-expresses both sides in
   the schema's name space using Ashby's own `data-field-path` entries before
   they are diffed. It can only remove NOISE: an uncaptured required control
   still forces NOT_COMPLETE, and an unfilled required field is never reconciled
   away. See the section comment above `_reconcile` for the live evidence.

2. CHECKBOX/RADIO: A SINGLE CONTROL AND A RADIO GROUP DRIVE, A CHECKBOX GROUP
   HANDS OFF (W5.1c; radio-group adoption completed after the TB5 live run). The
   original blanket Turnstile hand-off is now narrow: `engine.kernel.
   control_toolkit.drive_control` proved (W5.1c click-policy research) that a
   native, CDP-trusted `.check()` carries the SAME anti-bot risk tier as the
   typing this module already does -- the hazard was never the click, only a
   JS-dispatched `new Event`, which this path never uses -- so a lone, boolean
   Ashby checkbox (the graphql `Boolean` type; `fv.value` is a Python bool)
   drives through the shared mechanism, and so now does a radio-rendered
   `multi_value_single_select` GROUP (33/34 live `ValueSelect` fields).

   THE RADIO GROUP (`ControlKind.RADIO`). A single boolean checkbox is resolved
   by `base._locate` (its own accessible name IS the field title). A radio group
   is N controls that share the field's entry, and each carries the OPTION's own
   accessible name, never the field title, so the field's role+name locator
   cannot pick one option. `_locate_option` builds the missing per-option locator
   the way lever's FX1 proved it: intersect a page-wide role+accessible-name
   match (`get_by_role("radio", name=<option>, exact=True)`) with a
   group-scoping locator via `Locator.and_`. Lever scopes by the group's shared
   submission NAME; ashby's radios carry no such schema-known name (they share an
   entry-id-prefixed name the graphql does not carry), so it scopes by the field
   ENTRY (`[data-field-path="<key>"] input`, the same structural anchor
   `_geo_control`/`_reconcile` use, and it IS `Field.key`). Only the option that
   is BOTH worded `<option>` AND inside THIS group's entry can match, so a page
   with the same option wording ("Other") in two groups resolves each to its own
   control (the FX1 collision class). LIVE-VERIFIED: two 2026-07-18/19 w5_accept
   runs on real ashby postings are the acceptance evidence the deferral demanded.

   Single-select semantics, fail-closed: exactly the resolved option is ticked,
   and driven through the SAME `drive_control` (`.check()` + `.is_checked()`
   readback) as a lone checkbox. An option that does not resolve to exactly one
   control inside its entry is HANDED OFF (`_GROUP_OPTION_HANDOFF_REASON`, loud
   and named); a driven option whose checked state does not read back is a GAP.
   A required group that is handed off or unconfirmed still lands in
   `required_unfilled` -> NOT_COMPLETE, never a silent skip and never a guessed
   click.

   A checkbox-rendered `multi_value_multi_select` GROUP still HANDS OFF,
   unchanged (`_control_kind` returns None for it; a list value is not a bool):
   per-option checkbox-group driving is a sibling this wave scoped out, not a
   technical impossibility (`_locate_option` would serve it structurally). It
   stays a named `required_unfilled` hand-off, never a fill-error.

   Ashby serves no DATE-kind `drive_control` target: Ashby's Date raw type
   collapses into the generic canonical `input_text` type with no surviving
   signal to tell it apart from String/Email/Phone/Number
   (`capture._ASHBY_TYPE_MAP`), so there is nothing to route to
   `ControlKind.DATE` without inventing a detection this wave was told not to
   invent.

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

import re
from dataclasses import dataclass
from typing import Any

from engine.kernel.contracts import (
    Field, FieldMap, FillAssets, FillReport, FillSafetyError, ResolvedValues)
from engine.kernel.resolve import resolve_values as _kernel_resolve_values
from engine.kernel.fill_toolkit import (
    _current_url, _fill_upload, _is_upload, _needs_human_handoff,
    _strip_fragment, _sweep_gaps)
from engine.kernel.control_toolkit import ControlKind, ControlSpec, drive_control
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
# base._safe_upload for the resume, a single checkbox and a radio group driven
# via drive_control, a checkbox multi-select group handed off),
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
        kind = _control_kind(fv)
        if kind is None and _needs_human_handoff(fv):
            # A checkbox multi-select GROUP (a list value, not a bool): still a
            # human hand-off this wave. Radio single-select groups are driven
            # per-option below (`_control_kind` -> RADIO); checkbox-group driving
            # is a scoped-out sibling. Named, and a required one falls through to
            # required_unfilled -> NOT_COMPLETE.
            extra_skips.append((fv.key, _HUMAN_HANDOFF_REASON))
            continue
        try:
            if kind is not None:
                ok, actual, reason = _fill_control(page, fv, kind)
            else:
                ok, actual, reason = _fill_field(page, fv)
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
            extra_skips.append((fv.key, reason or _READBACK_MISMATCH_REASON))

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
    #
    # RECONCILED FIRST (W5B-ASHBY F2, `_reconcile`): the raw sweep and the schema
    # do not speak the same language on an Ashby page, so the two sets are
    # brought into one name space before they are diffed. The sweep stays
    # AUTHORITATIVE over everything it can still see on its own: a required
    # control that belongs to no captured field is passed through untranslated
    # and still forces NOT_COMPLETE.
    from engine.kernel.resolve import _completeness

    schema_required = {f.label for f in fieldmap.required_fields()}
    dom_required = base.sweep_required(page)
    reconciled = _reconcile(page, fieldmap, filled_keys,
                            schema_required, dom_required)
    mismatch = base.completeness_mismatch(reconciled.schema_required,
                                          reconciled.dom_required)

    filled = len(filled_keys)
    all_skips = list(values.skipped) + extra_skips
    fillable_total, required_unfilled, justified_skips = _completeness(
        fieldmap, filled_keys, all_skips, filled, vendor_resolver=None)
    required_unfilled = _with_step_reasons(required_unfilled, reconciled)
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
    "checkbox multi-select OPTION GROUP handed off for a human-operated trusted "
    "click: radio single-select groups are now driven per-option, but "
    "multi-select checkbox-group driving is a scoped-out sibling of this wave, "
    "so this control is handed off (a required one forces NOT_COMPLETE, never a "
    "guessed click)")

# A radio group whose resolved option cannot be located to exactly ONE control
# inside its own field entry (none, or several): ashby's entry container is
# always present for a captured field, so a 0-or-2+ resolution is a genuine
# "cannot pick this option", handed off loudly rather than driving a guess.
_GROUP_OPTION_HANDOFF_REASON = (
    "radio OPTION GROUP: the resolved option did not resolve to exactly ONE "
    "control inside this field's entry (none, or several), so it is handed off "
    "for a human rather than driving a guessed control (a required one forces "
    "NOT_COMPLETE, never a guessed click)")

_READBACK_MISMATCH_REASON = "value did not commit (readback mismatch)"

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
    """True for an Ashby SELECT-TYPE field, by the vendor's own schema `type`.

    This decides only that the field is a select. It does NOT decide the DRIVER:
    what the applicant actually operates is decided by the CONTROL (`fv.locator.
    role`, which capture now reads off the LIVE DOM), because on Ashby the schema
    type does not determine the control at all -- the SAME `ValueSelect` renders
    as a radio group on 33 of 34 live fields and as a nameless React dropdown on
    the 34th."""
    return fv.type in _ASHBY_SELECT_TYPES


# RATIFICATION MARKER (W5B-ASHBY round 7). The review prescribed a narrower remedy
# for the unreachable dropdown select: "anchor the select on data-field-path",
# keeping `_apply_ashby_controlled` as its driver. THIS WAVE DEVIATES, and drives
# every combobox through the suggestion-commit driver instead.
#
# The reason is empirical, not stylistic. The prescribed remedy was run against the
# live control (elevenlabs/1713bfd7, `fe82a364-...`, 2026-07-13) before it was
# implemented, and it is a FALSE COMPLETE: the native value-set writes the text into
# the widget's FILTER box, React accepts it as a search query, the readback returns
# that same text, and `fill()` counts the required field FILLED while the listbox is
# still open and NOTHING is selected. It would have replaced an honest fill-error
# with a silent wrong answer, which is strictly worse and is the one outcome this
# campaign's doctrine forbids. The two-signal commit check (`_geo_committed`) is what
# distinguishes them, and only the suggestion driver performs it.
#
# The deviation is also the SMALLER change once the role invariant is in place: the
# geo control and the dropdown select are the same widget, so this is one driver for
# one widget, not a second mechanism for a second special case.


def _is_ashby_combobox(fv) -> bool:
    """True for Ashby's nameless React combobox, whichever field wears it.

    The role now comes from the LIVE DOM (`capture._rendered_roles`), so this is
    an OBSERVATION of the control, not the native-vs-react ROLE GUESS the module
    docstring warns about: on this page, this control really is an
    `input[role=combobox]`. That distinction is the whole point of the role
    invariant, and it is what makes routing on the role sound here.

    Ashby renders exactly ONE combobox widget, and BOTH of its comboboxes are it
    (live-probed 2026-07-13 across 30 postings): the geo Location control (30/30)
    and a dropdown-rendered ValueSelect (1/1). Both are React autocompletes with
    NO id, NO name and NO aria-label, so neither has an accessible name and
    `base._locate` can reach NEITHER; both are served only through their entry's
    `data-field-path`, and both commit only when a `[role=option]` row is CLICKED.
    One widget, one driver."""
    return fv.locator.role == _COMBOBOX_ROLE


def _control_kind(fv) -> str | None:
    """`ControlKind.CHECKBOX` for a single, unambiguous boolean checkbox;
    `ControlKind.RADIO` for a radio-rendered single-select GROUP; `None` for
    everything else (a checkbox-rendered multi-select GROUP), which keeps the
    human hand-off.

    Ashby's `Field.locator` is ONE role+name pair per FIELD. It resolves a lone
    boolean checkbox correctly through `base._locate` (the DOM invariant table:
    role "checkbox" is the live, correct role for a `Boolean` field, whose own
    accessible name IS the field title). It cannot resolve one specific OPTION
    inside a GROUP -- each control in a group carries the OPTION's own accessible
    name, never the field title -- so a radio group is driven a different way:
    `_locate_option` builds the per-option locator (lever's FX1 shape, scoped to
    the field entry), and `_fill_radio_group` ticks exactly the resolved option.
    A `multi_value_single_select` renders as radios on 33/34 live ValueSelect
    fields (`fv.value` is the ONE chosen option, a str); the 34th renders as the
    nameless combobox, routed by role before this is reached.

    A checkbox-rendered `multi_value_multi_select` GROUP (`fv.value` a LIST of
    chosen options, never a bool) returns None and keeps the hand-off: per-option
    checkbox-group driving is a sibling this wave scoped out, not a technical
    impossibility.

    Ashby serves no DATE-kind target: Ashby's Date raw type collapses into the
    generic canonical `input_text` type with no surviving signal to tell it apart
    from String/Email/Phone/Number (`capture._ASHBY_TYPE_MAP`), so there is
    nothing to route to `ControlKind.DATE` without inventing a detection this
    wave was told not to invent."""
    role = fv.locator.role
    if role == "checkbox" and isinstance(fv.value, bool):
        return ControlKind.CHECKBOX
    if role == "radio" and isinstance(fv.value, str):
        return ControlKind.RADIO
    return None


def _fill_control(page, fv, kind: str) -> tuple[bool, Any, str]:
    """Drive a single checkbox or a radio GROUP through the shared kernel
    mechanism (`engine.kernel.control_toolkit.drive_control`) instead of the
    Turnstile hand-off, and readback-gate the result.

    A radio GROUP (`ControlKind.RADIO`) goes through `_fill_radio_group`, which
    locates the ONE resolved option inside the field entry before driving it. A
    single checkbox is located directly by `base._locate` (its own accessible
    name IS the field title). `name` is the REAL accessible name (`fv.locator.
    name or fv.label`), exactly as `_safe_upload` passes it, so the submit
    denylist still sees it. `drive_control` is fail-soft for everything except a
    denylist/type safety error (both `FillSafetyError`, left to propagate), so a
    control that does not confirm surfaces as a skip carrying `reason`, never a
    raised exception."""
    if kind == ControlKind.RADIO:
        return _fill_radio_group(page, fv)
    outcome = drive_control(ControlSpec(
        key=fv.key, kind=kind, locator=base._locate(page, fv),
        value=fv.value, name=fv.locator.name or fv.label))
    return outcome.confirmed, outcome.actual, outcome.reason


# -- radio GROUP driving: the per-option locate (lever's FX1 shape, ashby anchor)
# A radio group is N controls sharing this field's entry, each carrying the
# OPTION's own accessible name. To tick the resolved option, its control must be
# located among the group's own radios and NEVER a sibling group's option worded
# the same. Lever's FX1 proved the shape: intersect a page-wide role+accessible-
# name match with a group-scoping locator via `Locator.and_`. Lever scopes by the
# group's shared submission NAME; ashby's radios carry no such schema-known name
# (they share an entry-id-prefixed name the graphql schema does not carry), so
# the group is scoped by its `data-field-path` field ENTRY instead -- the same
# structural anchor `_geo_control`/`_reconcile` use, and it IS `Field.key`.


# `[data-field-path="{path}"] input` matches the option INPUT nodes (descendants
# of the entry), so it intersects the role+name match at the INPUT level exactly
# as lever's `input[name="{key}"]` does.
_GROUP_SCOPE_CSS = '[data-field-path="{path}"] input'


def _group_scope_css(key: str) -> str:
    """CSS matching every option INPUT inside a group's own `data-field-path`
    field entry. Paired with a role+accessible-name locator via `.and_()` in
    `_locate_option`, it narrows the page-wide option match to the ONE option
    belonging to THIS group. The ashby analogue of lever's submission-name
    `_group_css` (FX1): ashby radios carry no group-distinguishing submission
    name to scope by, so the entry container is the anchor. Empty for a missing
    key or one that could break out of the attribute selector."""
    if not key or '"' in key:
        return ""
    return _GROUP_SCOPE_CSS.format(path=key)


def _resolves_to_one(locator) -> bool:
    """True iff this locator matches EXACTLY ONE control on the live page. Zero is
    a phantom (the drive would silently do nothing, or time out); more than one
    and the drive could tick the wrong option. Either way the option is not safely
    drivable, and the caller hands off rather than guessing."""
    try:
        return locator.count() == 1
    except Exception:
        return False


def _locate_option(page, role: str, key: str, option: str):
    """The ONE control, among every control worded `option` on the page, that
    belongs to THIS group's field entry -- the same role+accessible-name
    convention `base._locate` uses for a whole field, applied to one option of a
    group and scoped to the group's own `data-field-path` container.

    Mirrors lever's proven FX1 shape (`.and_()` intersecting a role+name match
    with a group-scoping locator) with the ashby-specific anchor, so a page with
    the SAME option wording ("Other") in two groups resolves each to its own
    control. Returns None when the intersection does not resolve to exactly one
    control: ashby's entry container is always present for a captured field, so a
    0-or-2+ result is a genuine "cannot pick this option", which the caller HANDS
    OFF. This is where ashby DEVIATES from lever's `_locate_option`, which falls
    back to a bare page-wide locator; that residual exists for a lever group whose
    primary control carries no submission name, and ashby has no such residual (a
    captured field always has its entry)."""
    css = _group_scope_css(key)
    if not css:
        return None
    scoped = page.get_by_role(role, name=option, exact=True).and_(page.locator(css))
    return scoped if _resolves_to_one(scoped) else None


def _fill_radio_group(page, fv) -> tuple[bool, Any, str]:
    """Drive the ONE resolved option of a radio-rendered single-select GROUP.

    Single-select semantics, fail-closed: exactly the resolved option (`fv.value`,
    the chosen option's own wording) is located among the group's radios by its
    OPTION accessible name and scoped to the field entry (`_locate_option`), then
    ticked through the SAME shared kernel mechanism as a lone checkbox
    (`drive_control`, `.check()` + `.is_checked()` readback). The `name` passed to
    `drive_control` is the option wording, so the submit denylist still guards the
    click. An option that cannot be located to exactly one control is HANDED OFF
    (named, honest); a driven option whose checked state does not read back is a
    GAP, never a silent fill. Both surface as a required gap on a required
    group -> NOT_COMPLETE."""
    option = fv.value
    control = _locate_option(page, fv.locator.role, fv.key, option)
    if control is None:
        return False, "", _GROUP_OPTION_HANDOFF_REASON
    outcome = drive_control(ControlSpec(
        key=fv.key, kind=ControlKind.RADIO, locator=control,
        value=True, name=option))
    return outcome.confirmed, outcome.actual, outcome.reason


def _fill_field(page, fv) -> tuple[bool, Any, str]:
    """Drive one non-upload, non-handoff field; returns (landed, actual-read-back,
    reason-when-not-landed).

    Routing is by the CONTROL (the live-DOM role), never by the schema type:

      combobox  -> `_fill_ashby_combobox`, the path-anchored suggestion driver.
                   Dispatched BEFORE `base._locate`, whose role+name lookup is
                   exactly what cannot reach a control with no accessible name.
      select    -> `_apply_ashby_controlled` (a select whose live control is NOT
                   a combobox: a native `<select>`, role listbox/combobox from the
                   DOM probe's `select` rungs). Ashby serves none today; see the
                   driver's own note.
      otherwise -> `base.type_human` (text/email/textarea/spinbutton: genuine key
                   events already commit a React text input).

    NEVER `base.select_react_combobox` (greenhouse's react-select widget, whose
    node ids Ashby lacks) and NEVER `locator.select_option` (Lever's native path,
    which has no `<select>` to option-set here)."""
    if _is_ashby_combobox(fv):
        return _fill_ashby_combobox(page, fv)
    locator = base._locate(page, fv)
    if _is_ashby_select(fv):
        _apply_ashby_controlled(locator, fv.value)
    else:
        base.type_human(locator, str(fv.value))
    actual, ok = base._readback(locator, fv.value)
    return ok, actual, "" if ok else _READBACK_MISMATCH_REASON


def _apply_ashby_controlled(locator, value) -> None:
    """Commit `value` into a React CONTROLLED-COMPONENT select via the berellevy
    onChange/onBlur technique (`_REACT_CONTROLLED_COMMIT_JS`): the NATIVE value
    setter plus dispatched `input`/`change`/`blur` events, run atomically in-page
    through `locator.evaluate`. NEVER `locator.fill()` (a plain value-set React
    silently ignores) and NEVER a `.click()`/`.select_option()`. The caller
    readback-verifies immediately after, so a component that ignored the change
    reads back empty and is never counted as filled.

    IT MUST NEVER BE AIMED AT A COMBOBOX, and `_fill_field` no longer can (the
    combobox route is taken first). Live proof, 2026-07-13 on the one dropdown
    ValueSelect Ashby serves (elevenlabs/1713bfd7, `fe82a364-...`): this driver
    sets the input's text and dispatches the events, React accepts the text as its
    FILTER QUERY, and `base._readback` reads that same text back -- so the field
    counts FILLED (ok=True) while the widget is still sitting there with
    `aria-expanded="true"`, its listbox open and NOTHING selected. The form would
    be submitted with no answer to a required question and the report would call
    it complete. That is a FALSE COMPLETE, and it is strictly worse than the
    fill-error it would have replaced. The suggestion driver's two-signal commit
    check (`_geo_committed`: the value reads back AND the listbox has collapsed)
    is what tells the two apart, and it is why every combobox goes there instead.

    This leaves the driver with NO live Ashby target today: every Ashby select
    renders as radios (handed off), checkboxes (handed off) or that combobox. It
    is kept, not deleted, for the select shapes the DOM probe still maps to it (a
    native `<select>` / `select[multiple]`), which HAVE an accessible name and
    which `base._locate` therefore reaches correctly."""
    locator.evaluate(_REACT_CONTROLLED_COMMIT_JS,
                     list(value) if isinstance(value, list) else str(value))


# == THE NAMELESS-COMBOBOX DRIVER (W5B-ASHBY F1; generalised round 7) =========
# Ashby has exactly ONE combobox widget, and BOTH of its comboboxes are it: the
# geo-autocomplete Location field (live on 30 of 30 postings probed) and a
# dropdown-rendered ValueSelect (live on 1 of 34 ValueSelects; the other 33 are
# radio groups). They are the same React autocomplete, with the same fatal
# property -- NO accessible name -- so `base._locate` can reach NEITHER, and both
# commit ONLY when a `[role=option]` row is clicked. One widget, one driver: this
# one. What differs between them is only which suggestions the widget offers (a
# geo dataset vs the field's own enumerated options), and the commit decision is
# made against the intended VALUE either way, so the same fail-closed ladder
# serves both.
#
# POSTING REGISTRY (kept current; read this before trusting any citation below).
# The evidence in this module was taken from live elevenlabs postings on
# 2026-07-13, and the citations keep their original attribution because that is
# what records WHERE and WHEN each claim was observed. But a posting is not a
# permanent address:
#
#   elevenlabs/b43e388f-...  CLOSED (verified gone 2026-07-14). It was the
#                            acceptance/seal posting and the source of dom.html.
#                            Every claim attributed to it below still stands as
#                            HISTORY; NOTHING may point at it as a live target.
#   CURRENT LIVE TARGET      axelera/17131e36-e361-45a7-8538-1033a63addd1
#                            ("Senior Platform Architect"). Fallback:
#                            axelera/0505a9a5-d908-4cb2-bdcb-c92b1fe8309a.
#
# WHAT THE CURRENT TARGET SHOWS (live probe 2026-07-14, headless chromium under
# xvfb, BROWSER LOCALE en-GB, navigator.languages ["en-GB"], TZ Europe/Rome; page
# load + typing only, NO submit, no owner PII). The widget below is not an
# elevenlabs artefact; it is what Ashby serves, and this driver was re-run against
# it end to end:
#
#   HTTP 200; 14 inputs; 2 page-wide `input[type=file]` (the resume field AND the
#   autofill pane's decoy -- the same hazard the fixture carries); exactly ONE
#   `input[role=combobox]`, in the entry labelled "Current location:", and that
#   entry holds exactly ONE input (so `_geo_control` resolves uniquely: 1, not N).
#
#   COMMIT, through this module's own `_fill_ashby_combobox`: filter "United
#   Kingdom" -> the widget offered FIVE fuzzy rows ["United Kingdom", "Eswatini",
#   "Kingdom of Elleore, Region Zealand, Denmark", "Kingdom City, Missouri,
#   United States", "Unity, South Sudan"] -- four of them decoys, which is the
#   anti-guessing rule's whole reason to exist -- the unique exact row was
#   clicked, and the control then read back "United Kingdom" with aria-expanded
#   "false". The value is COMMITTED, not merely typed.
#
#   REFUSAL, same page, same driver: the ambiguous value "Kinshasa, Democratic
#   Republic of the Congo" matched no row uniquely, nothing was committed, and the
#   box was left EMPTY (the filter does not outlive the attempt).
#
# The live control (probed 2026-07-13 on elevenlabs/b43e388f, the then-acceptance
# posting, CLOSED since; the dropdown ValueSelect on elevenlabs/1713bfd7 is
# byte-for-byte the same widget, with a chevron `<button>` beside it):
#
#   <div class="_fieldEntry_..." data-field-path="0a131ea7-...">
#     <label class="_heading_... _required_... ">Location</label>
#     <div class="_inputContainer_...">
#       <input class="_input_..." placeholder="Start typing..."
#              role="combobox" aria-autocomplete="list" aria-expanded="false"
#              aria-haspopup="listbox">
#
# Typing opens a floating `div[role=listbox]` (the control's `aria-controls`
# target) whose rows are `div[role=option]`; clicking a row writes its text into
# the input and collapses the listbox (`aria-expanded` -> "false"). That is the
# whole commit protocol, and it is why the old path could not work: the input
# carries NO id, NO name and NO aria-label, and its `<label>` has no `for`, so
# it has no accessible name at all -- `get_by_role("textbox"/"combobox",
# name="Location")` resolves to nothing and Playwright times out. The driver
# anchors on the entry's `data-field-path`, which IS the graphql `path` the
# FieldMap already carries as `Field.key`.
#
# This MIRRORS the greenhouse combobox PATTERN (open, type_human a filter, wait
# bounded for the suggestion list, commit, confirm) and NONE of its selectors:
# Ashby has no react-select `#react-select-*` DOM whatsoever.
#
# ANTI-GUESSING (the load-bearing rule). The widget fuzzy-matches: on the live
# probe the filter "Milan" returned exactly one suggestion, "Christmas Island"
# (a subsequence hit). A blind first-option commit would have written a location
# the candidate has no connection to and reported the field COMPLETE. A
# suggestion is committed ONLY on a UNIQUE normalized match against the intended
# value; zero hits or 2+ hits leaves the field HONESTLY UNFILLED, which lands it
# in required_unfilled -> NOT_COMPLETE.

_COMBOBOX_ROLE = "combobox"     # what capture._rendered_roles reads off the DOM
_GEO_CONTROL_CSS = '[data-field-path="{path}"] input[role="combobox"]'
_GEO_CONTROL_FALLBACK_CSS = '[data-field-path="{path}"] input'
_GEO_OPTION_CSS = '[role="option"]'
_GEO_SCOPED_OPTION_CSS = '[id="{listbox}"] [role="option"]'
_GEO_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")
# Bounded waits (ms) for the suggestion list to render / the commit to land.
# Read first, wait, read again: never an unbounded wait, never a blind sleep.
_GEO_POLL_MS = (250, 500, 1000)

_GEO_NO_CONTROL_REASON = (
    "combobox: this field's data-field-path did not resolve to exactly ONE "
    "control (none, or several) "
    "(left unfilled; never typed into a guessed control)")
_GEO_NO_MATCH_REASON = (
    "combobox: no suggestion uniquely matched the intended value "
    "(left honestly unfilled; the widget fuzzy-matches, so a first-option "
    "commit would write an answer the candidate never gave)")
_GEO_NOT_COMMITTED_REASON = (
    "combobox: the chosen suggestion did not commit (the control did "
    "not read it back with its listbox collapsed)")


def _fill_ashby_combobox(page, fv) -> tuple[bool, Any, str]:
    """Type a filter, read what the widget offers, commit ONE suggestion only if
    it uniquely matches the intended value. Returns (landed, readback, reason).

    Drives BOTH Ashby comboboxes (the geo Location and a dropdown ValueSelect):
    they are the same nameless React autocomplete, and the commit protocol is
    identical. The suggestions differ in provenance (a geo dataset vs the field's
    own enumerated options) but never in TREATMENT -- neither is trusted, and the
    match is always decided against the intended value."""
    control = _geo_control(page, fv)
    if control is None:
        return False, "", _GEO_NO_CONTROL_REASON
    for filter_text in _geo_filters(fv.value):
        _geo_clear(control)
        base.type_human(control, filter_text)
        suggestions = _geo_suggestions(page, control)
        match = _unique_suggestion([text for text, _ in suggestions], fv.value)
        if match is None:
            continue
        option = next(node for text, node in suggestions if text == match)
        # Every click still goes through the sole sanctioned gateway: a
        # suggestion whose own text reads like a submit control is REFUSED
        # (loudly, never silently clicked) by the kernel's denylist.
        base._safe_click(option, match)
        landed, actual = _geo_committed(page, control, match)
        if landed:
            return True, actual, ""
        return False, _geo_abandon(control, actual), _GEO_NOT_COMMITTED_REASON
    return False, _geo_abandon(control, _control_value(control)), \
        _GEO_NO_MATCH_REASON


def _geo_abandon(control, actual):
    """Give up on this field and leave the box EMPTY, returning what was read
    back before it was cleared.

    A refusal that leaves the last typed FILTER sitting in the input is a report
    that disagrees with the page: the JSON says the field is unfilled while the
    screenshot shows text in it, and the seal's screenshot cross-check calls that
    divergence a REJECT. The filter is scaffolding, not an answer, so it does not
    outlive the attempt."""
    _geo_clear(control)
    return actual


def _geo_control(page, fv):
    """The geo input, anchored on the field entry's `data-field-path` (the
    schema `path` = `Field.key`), because the control itself has no accessible
    name to locate it by. Scoped to the field's OWN entry, so the driver can
    never type into another field's control.

    UNIQUE MATCH OR REFUSE, the same rule `_unique_suggestion` enforces one rung
    later: the anchor must resolve to EXACTLY ONE control. None otherwise, which
    leaves the field honestly unfilled (-> required_unfilled -> NOT_COMPLETE):

      zero    -> the entry is not mounted (an off-step field), or the widget the
                 path names is not there.
      several -> the path is AMBIGUOUS on this render (a re-mounted entry, a
                 duplicated responsive copy, a second widget inside the same
                 entry). Taking the first is a GUESS about which of two live
                 controls the schema meant, made on DOM order, and it types the
                 owner's data into it. The whole module refuses to guess where a
                 wrong answer would be indistinguishable from a right one; the
                 resolution step is not exempt."""
    path = fv.key or ""
    if not path or '"' in path:
        return None
    for css in (_GEO_CONTROL_CSS, _GEO_CONTROL_FALLBACK_CSS):
        found = _locate_all(page, css.format(path=path))
        if len(found) == 1:
            return found[0]
        if found:
            return None
    return None


def _geo_filters(value) -> list[str]:
    """The filter strings to type, finest first: the whole value, then its first
    comma segment (the locality), then its last (usually the country).

    Every filter is a literal slice of the SSOT's OWN value -- nothing is
    invented -- and the commit decision is always made against the FULL value,
    so a coarser filter can only widen what the widget OFFERS, never what this
    driver ACCEPTS. The ladder exists because the widget's own dataset is
    coarser than the SSOT (the 2026-07-13 probe returned no city rows at all),
    so a whole-value-only filter would leave a fillable required field unfilled.
    """
    text = str(value or "").strip()
    if not text:
        return []
    parts = [part.strip() for part in text.split(",") if part.strip()]
    filters = [text]
    if len(parts) > 1:
        filters.append(parts[0])
        filters.append(parts[-1])
    return list(dict.fromkeys(filters))


def _geo_clear(control) -> None:
    """Empty the filter box between attempts with REAL keystrokes (select-all +
    Backspace), never `fill()`: the human-cadence rule `type_human` keeps, and
    the keystrokes are what make the widget re-query its suggestions."""
    if not _control_value(control):
        return
    press = getattr(control, "press", None)
    if not callable(press):
        return
    press("Control+a")
    press("Backspace")


def _geo_suggestions(page, control) -> list[tuple[str, Any]]:
    """The suggestion rows the widget rendered, on a BOUNDED poll.

    Scoped to the listbox the control itself points at (`aria-controls` -> the
    floating `div[role=listbox]`), so a stray `[role=option]` elsewhere on the
    page can never be mistaken for a suggestion; falls back to a page-wide
    option scan only when the control exposes no `aria-controls`."""
    listbox = _attr(control, "aria-controls")
    css = (_GEO_SCOPED_OPTION_CSS.format(listbox=listbox)
           if listbox and '"' not in listbox else _GEO_OPTION_CSS)

    def rows() -> list[tuple[str, Any]]:
        found = [(base._locator_text(node), node)
                 for node in _locate_all(page, css)]
        return [(text, node) for text, node in found if text]

    for wait_ms in _GEO_POLL_MS:
        current = rows()
        if current:
            return current
        _geo_wait(page, wait_ms)
    return rows()


def _unique_suggestion(suggestions: list[str], value) -> str | None:
    """The ONE suggestion that matches `value`, or None. GEO-LOCAL and FAIL-CLOSED.

    Two steps, each requiring a UNIQUE hit; 2+ hits stop the ladder rather than
    guessing, and zero hits leave the field honestly unfilled:

      (a) exact, normalized;
      (b) the suggestion's tokens are a SUBSET of the value's -- the coarser-row
          case, and the only reason this driver needs a ladder at all: the
          widget's dataset is coarser than the SSOT, so the value "Milan,
          Fakeland" is legitimately satisfied by the offered row "Fakeland".

    IT DOES NOT USE `engine.content._fit_to_options`, and that is deliberate
    (meta ruling, W5B-ASHBY round 2). The sealed ladder's token-subset step is
    BIDIRECTIONAL (content.py:547-549: `opt_tokens <= value_tokens or
    value_tokens <= opt_tokens`), which is right for an ENUMERATED option list,
    where a verbose option ("Yes, I am willing to relocate") is the intended home
    of a terse value ("Yes"). It is UNSOUND on geo data: a place that CONTAINS
    your place is a DIFFERENT place. Against the real ladder, the value "London"
    offered ["London, Ontario"] COMMITS London, Ontario, and "Milan" offered
    ["Milan, Ohio"] COMMITS Milan, Ohio -- the same harm as the blind
    first-option commit this driver exists to prevent, reached by another route,
    and worse than an unfilled field: an unfilled required field is honest and
    recoverable, a wrong city sent to a real employer is neither.

    The superset direction is therefore REFUSED outright. The ratified cost: a
    SINGLE-TOKEN SSOT location ("London" offered "London, United Kingdom") is now
    refused too, and lands as an honestly-unfilled required field. That is the
    correct direction of failure (campaign doctrine: never guess; unfilled is
    honest, wrong is a genuine failure). content.py stays frozen and untouched."""
    if not suggestions:
        return None
    target = base._normalize_name(value)
    if not target:
        return None

    exact = [row for row in suggestions
             if base._normalize_name(row) == target]
    if exact:
        return exact[0] if len(exact) == 1 else None

    value_tokens = _geo_tokens(value)
    if not value_tokens:
        return None
    coarser = [row for row in suggestions
               if (row_tokens := _geo_tokens(row)) and row_tokens <= value_tokens]
    return coarser[0] if len(coarser) == 1 else None


def _geo_tokens(text) -> frozenset[str]:
    return frozenset(token for token
                     in _GEO_TOKEN_SPLIT_RE.split(base._normalize_name(text))
                     if token)


def _geo_committed(page, control, match: str) -> tuple[bool, Any]:
    """Confirm the widget COMMITTED the chosen suggestion, on a bounded poll.

    TWO signals, both required (never-committed bias, mirroring the kernel's
    upload readback): the control reads the suggestion's own text back, AND the
    widget reports its listbox collapsed (`aria-expanded` back to "false" -- the
    live commit signal). The text alone is NOT sufficient: the filter box still
    holds whatever was typed, so a filter that happened to equal its own
    suggestion would read back as "committed" even if the click never registered
    and the form's React state holds no location at all -- a false COMPLETE. A
    control that cannot positively confirm both signals is treated as NOT
    committed."""
    for wait_ms in _GEO_POLL_MS:
        actual = _control_value(control)
        if (base._normalize_name(actual) == base._normalize_name(match)
                and _attr(control, "aria-expanded") == "false"):
            return True, actual
        _geo_wait(page, wait_ms)
    return False, _control_value(control)


def _geo_wait(page, ms: int) -> None:
    waiter = getattr(page, "wait_for_timeout", None)
    if not callable(waiter):
        return
    try:
        waiter(ms)
    except Exception:
        pass


def _control_value(control) -> Any:
    getter = getattr(control, "input_value", None)
    if not callable(getter):
        return ""
    try:
        return getter()
    except Exception:
        return ""


# == SCHEMA/DOM REQUIRED-SET RECONCILIATION (W5B-ASHBY F2) =====================
# The DOM sweep is a CROSS-CHECK of the graphql schema (greenhouse semantics),
# but on an Ashby page the two oracles do not speak the same language, so the
# raw diff was almost entirely NOISE. Live evidence (elevenlabs/b43e388f,
# 2026-07-13; that posting is CLOSED since 2026-07-14 -- see the POSTING REGISTRY
# above for the current live target -- and this remains the historical record of
# where the divergence was measured), with 6 schema-required fields on the page:
#
#   kernel sweep  -> {"type here...", "hello@example.com..."}
#   schema        -> {"Name", "Email", "Location", "Resume",
#                     "How did you hear about ElevenLabs?",
#                     "Link to your LinkedIn profile"}
#
# Three independent divergence sources, all of them DOM artefacts rather than
# real gaps:
#   (a) Ashby's React widgets (the geo combobox, the controlled-component
#       selects) carry NO native `required` attribute, so the sweep cannot see
#       them at all -> every one came back as a bogus `schema_only` gap;
#   (b) the sweep names a control by aria-label -> placeholder -> name attr, and
#       Ashby gives its inputs no aria-label and a raw uuid path for `name`, so
#       the names it does produce are PLACEHOLDERS ("type here...") -> bogus
#       `dom_only` gaps, and the same placeholder is shared by several fields;
#   (c) a field on a not-yet-mounted step is not in the DOM at all.
#
# `_reconcile` re-expresses BOTH sides in the schema's name space using Ashby's
# OWN structure: every field entry carries `data-field-path` (the schema path),
# its `<label>` carries the human title, and required-ness is marked by a
# CSS-module class on that label (`_required_<hash>_<n>`) -- never by an
# attribute, and never by an asterisk in the label TEXT (it is a ::after
# pseudo-element, invisible to `inner_text`).
#
# WHAT IT MUST NEVER DO, and does not:
#   * A swept required control that belongs to NO captured entry is NOT
#     translated and NOT dropped: it passes through raw, lands in `dom_only`
#     and still forces NOT_COMPLETE. That is the sweep still biting. Ownership is
#     counted by NODE, so this holds even when the rogue control's accessible
#     name COLLIDES with a captured field's (on the live page four required
#     controls share the placeholder "type here...", so name-based ownership --
#     the round-1 implementation -- silently dissolved exactly this case).
#   * A required field that was not FILLED is never reconciled away: only
#     readback-VERIFIED fills (`filled_keys`, the readback gate) are taken off
#     the diff.
#   * With no Ashby entry structure on the page (an unknown DOM variant), NO
#     reconciliation happens at all: the raw sweep stands exactly as before,
#     which is the strict direction.

_ASHBY_ENTRY_CSS = "[data-field-path]"
_ASHBY_ENTRY_LABEL_CSS = "label, legend"
_ASHBY_REQUIRED_CLASS_RE = re.compile(r"(?:^|\s)_required(?:_\S*)?(?=\s|$)")
# The controls Ashby renders as React widgets: they hold their value in React
# state and carry no native `required`, so the sweep is structurally blind to
# them. Everything else (text inputs, the file input) the sweep sees natively.
_REACT_WIDGET_ROLES = frozenset({"combobox", "listbox"})


@dataclass
class _AshbyEntry:
    """One MOUNTED Ashby field entry, read from the live DOM."""
    path: str            # data-field-path == the graphql path == Field.key
    label: str           # normalized <label> text (the schema's name space)
    required: bool       # Ashby's own required marker (class, or native attr)
    owned: list[str]     # sweep-equivalent names of the REQUIRED controls it OWNS


@dataclass
class _Reconciled:
    schema_required: set[str]
    dom_required: set[str]
    offstep: list[Field]
    mounted_steps: set[int]


def _reconcile(page, fieldmap: FieldMap, filled_keys: set[str],
               schema_required: set[str], dom_required: set[str]) -> _Reconciled:
    """Bring the schema's required set and the live sweep into ONE name space."""
    schema = {base._normalize_name(name) for name in schema_required} - {""}
    dom = {base._normalize_name(name) for name in dom_required} - {""}
    entries = _ashby_entries(page)
    if not entries:
        return _Reconciled(schema, dom, [], set())

    mounted = {entry.path for entry in entries}
    schema_labels = {f.key: base._normalize_name(f.label) for f in fieldmap.fields}

    # (b) name space. OWNERSHIP IS DECIDED BY NODE, NEVER BY NAME (round-2
    # BLOCKER 1). Ashby gives four distinct required controls the SAME accessible
    # name on the live page (the placeholder "type here...", because it ships no
    # aria-label), so "this swept NAME also appears somewhere inside an entry" is
    # not evidence that the entry OWNS the control the sweep saw. Counting the
    # nodes is: a name is translated to entry labels only when EVERY page-wide
    # required control carrying it is accounted for by an entry that owns one.
    # The moment the page carries one more control with that name than the
    # entries own, the name passes through RAW -- so a required control outside
    # the field structure lands in dom_only and still forces NOT_COMPLETE, even
    # when its name collides with a captured field's.
    owned = _name_counts(entry.owned for entry in entries)
    page_wide = _name_counts([_swept_names(page)])
    reconciled_dom = {_entry_name(entry, schema_labels) for entry in entries
                      if entry.required}
    reconciled_dom |= {name for name in dom
                       if not (owned.get(name, 0) >= 1
                               and page_wide.get(name, 0) <= owned.get(name, 0))}
    reconciled_dom -= {""}

    # (c) off-step: a required field the schema places on a step the page has not
    # mounted is not a sweep gap; it stays a REQUIRED GAP with a step reason
    # (`_with_step_reasons`), so the report is honest and still NOT_COMPLETE.
    # Only decidable when at least one captured field IS mounted; otherwise no
    # field is reclassified and the raw diff stands.
    mounted_steps = {f.step_index for f in fieldmap.fields
                     if f.key in mounted and f.step_index is not None}
    offstep: list[Field] = []
    if mounted_steps:
        offstep = [f for f in fieldmap.required_fields()
                   if f.key not in mounted and f.step_index is not None
                   and f.step_index not in mounted_steps]
    reconciled_schema = schema - {base._normalize_name(f.label) for f in offstep}

    # (a) a React widget that WAS filled and readback-verified is accounted for
    # by the strongest evidence there is (the control read our value back), so it
    # leaves BOTH sides of the diff: the sweep is structurally blind to its
    # required-ness and can only manufacture a false gap for it. An unfilled one
    # is untouched and still bites.
    verified = {base._normalize_name(f.label) for f in fieldmap.required_fields()
                if f.key in filled_keys and f.locator.role in _REACT_WIDGET_ROLES}
    return _Reconciled(reconciled_schema - verified, reconciled_dom - verified,
                       offstep, mounted_steps)


def _with_step_reasons(required_unfilled: list[dict],
                       reconciled: _Reconciled) -> list[dict]:
    """Re-word the required gaps of OFF-STEP fields so the report names the step
    they are waiting on, instead of a locator error from a control that was
    never on the page. They REMAIN required gaps (still NOT_COMPLETE)."""
    if not reconciled.offstep:
        return required_unfilled
    mounted = sorted(reconciled.mounted_steps)
    reasons = {
        f.key: (f"not on the mounted step: the schema places this field on step "
                f"{f.step_index}, the page has mounted step(s) {mounted}; it "
                "stays a required gap until that step is reached")
        for f in reconciled.offstep}
    return [dict(gap, reason=reasons[gap["key"]]) if gap["key"] in reasons else gap
            for gap in required_unfilled]


def _ashby_entries(page) -> list[_AshbyEntry]:
    """Every mounted Ashby field entry, read through Playwright locators (never a
    page.evaluate blob, so the extraction itself is unit-testable against a real
    captured DOM). A page with no `data-field-path` entries yields [] -> no
    reconciliation.

    `owned` is the sweep-equivalent name of each REQUIRED control the entry
    actually contains (the same aria-label -> placeholder -> name derivation the
    kernel sweep uses, single-sourced through `base._accessible_name`), which is
    what lets `_reconcile` count nodes instead of trusting names."""
    entries: list[_AshbyEntry] = []
    for node in _locate_all(page, _ASHBY_ENTRY_CSS):
        path = _attr(node, "data-field-path")
        if not path:
            continue
        labels = _locate_all(node, _ASHBY_ENTRY_LABEL_CSS)
        label_node = labels[0] if labels else None
        label = (base._normalize_name(base._locator_text(label_node))
                 if label_node is not None else "")
        entries.append(_AshbyEntry(
            path=path, label=label, required=_entry_required(node, label_node),
            owned=_swept_names(node)))
    return entries


def _swept_names(scope) -> list[str]:
    """The normalized accessible name of every REQUIRED control under `scope`
    (page or entry), one per NODE, duplicates kept: the multiplicity is the whole
    point (`_reconcile`, BLOCKER 1). Mirrors the kernel sweep's own arm-1
    enumeration (`fill_toolkit._REQUIRED_CSS` + `_accessible_name`)."""
    names = [base._normalize_name(base._accessible_name(control))
             for control in _locate_all(scope, base._REQUIRED_CSS)]
    return [name for name in names if name]


def _name_counts(name_lists) -> dict[str, int]:
    counts: dict[str, int] = {}
    for names in name_lists:
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    return counts


def _entry_name(entry: _AshbyEntry, schema_labels: dict[str, str]) -> str:
    """The name an entry contributes to the DOM-required set, in the schema's
    name space: its own `<label>` text, else the captured field's label (the
    entry is keyed by the schema path, so the map is exact), else a synthetic
    name built from the path.

    The last rung is what closes the second head of BLOCKER 1: an entry with a
    required control but NO readable label used to contribute NOTHING to the DOM
    side while still suppressing its control's swept name, which silently
    dissolved a required control. A synthetic name can never collide with a
    schema label, so such an entry now surfaces as a dom_only gap and blocks."""
    if entry.label:
        return entry.label
    schema_label = schema_labels.get(entry.path, "")
    if schema_label:
        return schema_label
    return f"data-field-path:{entry.path}"


def _entry_required(entry, label_node) -> bool:
    """Ashby's own required signal for one field entry: the CSS-module marker
    class on its `<label>` (`_required_<hash>_<n>`), or a native `required` /
    `aria-required` on any control inside it (the two text inputs carry one; the
    React widgets carry neither)."""
    if label_node is not None and _ASHBY_REQUIRED_CLASS_RE.search(
            _attr(label_node, "class")):
        return True
    return bool(_locate_all(entry, base._REQUIRED_CSS))


def _locate_all(scope, css: str) -> list:
    """`scope.locator(css).all()`, fully guarded: a page/locator that cannot
    answer yields no matches rather than raising, so a partial fake or an
    unexpected DOM degrades to "no reconciliation", never to a crash."""
    locator_fn = getattr(scope, "locator", None)
    if not callable(locator_fn):
        return []
    try:
        return list(locator_fn(css).all() or [])
    except Exception:
        return []


def _attr(node, name: str) -> str:
    getter = getattr(node, "get_attribute", None)
    if not callable(getter):
        return ""
    try:
        return (getter(name) or "").strip()
    except Exception:
        return ""
