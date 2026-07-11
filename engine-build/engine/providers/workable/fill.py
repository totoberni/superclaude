"""Workable provider: the FOURTH reference implementation of the `Provider`
contract (`engine.providers.protocol.Provider`), W5.4.

Workable is a HYBRID of the two earlier reference patterns, which is exactly why
it earns its own module rather than reusing one wholesale:

1. CAPTURE is GREENHOUSE-CLASS (schema API, no browser). The per-posting apply
   schema is a public, unauthenticated GET
   (`apply.workable.com/api/v1/jobs/<SHORTCODE>/form`) returning the full typed
   field list with requiredness, so `capture()` delegates to the browser-free
   `workable.capture.capture_workable` (registered as `PROVIDERS["workable"].capture`),
   NOT a live-DOM parse. The FieldMap therefore carries an INDEPENDENT schema, so
   completeness uses GREENHOUSE semantics: the schema is the oracle and
   `base.sweep_required` is a cross-check (`_sweep_gaps` below reuses greenhouse's
   schema-oracle wording, NOT lever's "the DOM sweep is authoritative").

2. FILL is LEVER-CLASS (native DOM path, no react-select) with a WIDER hand-off.
   Workable renders no react-select combobox (that is greenhouse's widget) and no
   server-side native `<select>` this wave has sampled, so `fill()` drives text
   controls through the plain native path (`base.type_human` + readback-gate) and
   NEVER `base.select_react_combobox`. Every control whose fill would need a
   PROGRAMMATIC CLICK is HANDED OFF instead of auto-driven, the same fail-safe
   Lever uses for hCaptcha -- here the hazard is the invisible Cloudflare
   Turnstile the apply page runs. The hand-off set is WIDER than Lever's: not just
   a boolean yes/no radio (`fieldset[data-ui="QA_n"]`) and any checkbox, but ALSO
   Workable's dropdown (role "combobox") and multiple-choice (role "listbox"),
   which are CUSTOM JS widgets whose option DOM is unsampled in the seven forms
   captured this wave -- handed off rather than guessed (an unresolved wire shape,
   never auto-clicked) -- and a flattened GROUP subfield (`<group>.<sub>`), whose
   "+ Add" opener this wave never drives. A REQUIRED control that is handed off
   lands in `required_unfilled` -> NOT_COMPLETE, never a silent skip and never a
   reckless auto-click that could trip Turnstile mid-form.

CV/PHOTO: `resolve_values` delegates to `engine.kernel.resolve.resolve_values`
-- the hole-fix e structural CV/photo choice (an image/photo upload field present
on the FORM -> the plain ATS CV and the photo attaches; absent -> the
embedded-photo ATSI CV variant). Workable's `avatar` field is exactly such a
photo field, so a Workable form that exposes it takes the plain ATS CV. The rule
keys purely on the FORM's structure (`kernel.resolve._form_has_photo_field`), never posting
text, so it is single-sourced in the kernel and delegated to here rather than
duplicated -- a load-bearing safety rule with one home.

FIELD-DRIVING SPECIFICS (W5.4 spec PART B):
- text / email / paragraph / short_text / free_text / numeric -> `base.type_human`
  (human-cadence keystrokes; the SPA scores an instant value-set as bot-like).
- phone -> the SAME text path: the value is already a full international number
  (resolved from the SSOT) and the field's role+name locator resolves the
  `input[type=tel][name="phone"]` box directly, so the intl-tel-input COUNTRY
  combobox is never touched (no field maps to it, so it is never resolved/driven).
- file (resume / avatar) -> `base._safe_upload` on the real hidden
  `input[type=file]` via the shared `_fill_upload` primitive (avatar is a photo
  field, already routed to the photo asset by the inherited resolve_values).
- address -> the SAME text path fills `input[name="address"]` only; the
  city/postcode/country COMPANIONS are machine-managed (never mapped to a Field,
  so never driven), and a pre-existing `prefilledByLocation` value simply reads
  back as the confirmed value.
- boolean / checkbox / dropdown / multiple_choice / group -> HUMAN HAND-OFF.

LAZY-IMPORT INVARIANT (mirrors greenhouse.py / lever.py / base.py / _registry.py):
this module must not import patchright / a browser-capture module at load time so the daily
poller (which imports `engine.providers` eagerly: `_registry` plus the four
plugin packages, all browser-free) stays browser-free. Kernel primitives are
imported at module scope (browser-free by construction). Dataclasses come
from their canonical kernel home (`kernel.contracts`); this module has NO
`engine.fill` import at any scope. Workable imports NO sibling vendor package
(import-disjoint, W5.1 Stage 3a): the CV/photo rule comes from the kernel.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from engine.kernel.contracts import (
    FieldMap, FillAssets, FillReport, FillSafetyError, ResolvedValues)
from engine.kernel.resolve import resolve_values as _kernel_resolve_values
from engine.providers import base

vendor = "workable"


# -- capture / apply_url: the public schema GET + apply-page URL ----------------


def capture(slug: str, job_id: str, opener: Any = None) -> FieldMap:
    """The schema fetch: `capture_workable`, the public
    `.../jobs/<shortcode>/form` GET (browser-free, greenhouse-class). Reached via
    a CALL-TIME import from `engine.providers.workable.capture` so importing this
    module stays light and the test monkeypatch seam (patch `capture_workable` on
    the workable `.capture` module) still routes. No new capture logic here (the
    provider registry looks this function up lazily as `_registry.get("workable").
    capture`)."""
    from engine.providers.workable.capture import capture_workable
    return capture_workable(slug, job_id, opener)


def apply_url(slug: str, job_id: str) -> str:
    """The public apply-page URL: this vendor's own browser-free
    `capture.workable_apply_url` builder."""
    from engine.providers.workable.capture import workable_apply_url
    return workable_apply_url(slug, job_id)


# -- value resolution: from the kernel (hole-fix e CV/photo choice) ------------
# The structural CV/photo rule is vendor-agnostic (keyed on the form's own
# upload-field shape via `kernel.resolve._form_has_photo_field`, never posting text), so it
# has ONE home -- the generic kernel.resolve.resolve_values -- and Workable
# delegates to it rather than duplicating a load-bearing safety rule. Workable's
# `avatar` upload field is exactly the photo signal the rule keys on.


def resolve_values(fieldmap: FieldMap, ssot, profile: dict, *,
                   assets: FillAssets | None = None,
                   posting_lang: str = "en") -> ResolvedValues:
    """Render every field to a concrete fill value via the kernel's generic
    resolve engine. Workable has no portal-widget quirks, so no vendor_resolver
    is injected (the kernel no-op default). The owner-ratified structural
    CV/photo rule (plain ATS CV when the form has a photo field -- Workable's
    `avatar` -- embedded-photo ATSI CV otherwise) is generic in the kernel and
    keys purely on the FORM's structure."""
    return _kernel_resolve_values(fieldmap, ssot, profile, assets=assets,
                                  posting_lang=posting_lang)


# -- fill(): the Provider contract's ordered sequence (native DOM path) ----------
# (1) never-send FIRST, (2) drive every field via base.py NATIVE primitives
# (text via type_human, a file via _safe_upload; a boolean/checkbox/dropdown/
# multiple/group is handed off, never auto-clicked), (3) readback-gate what counts
# as filled, (4) DOM-sweep cross-check (GREENHOUSE semantics: the schema is the
# oracle, the sweep confirms it) forces NOT_COMPLETE on any mismatch, (5) return
# the existing FillReport dataclass.


def fill(page: Any, fieldmap: FieldMap, values: ResolvedValues, *,
        dry_run: bool = True, company: str | None = None) -> FillReport:
    """Drive an ALREADY-NAVIGATED Workable apply page, STOPPING SHORT OF APPLYING.

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

    # (2) + (3) drive + readback-gate every resolved field via the NATIVE path.
    for fv in values.fields:
        if _is_upload(fv):
            _fill_upload(page, fv, uploads, extra_skips, filled_keys)
            continue
        if _needs_human_handoff(fv):
            # Turnstile hazard: NEVER auto-click a boolean radio / checkbox /
            # dropdown / multiple / group. Hand it off with a clear reason; a
            # required one falls through to required_unfilled -> NOT_COMPLETE.
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

    # Safety invariant carried over from greenhouse.fill / lever.fill: a
    # navigation during the fill is treated as a possible submission/redirect,
    # even though this module never calls page.goto() itself.
    post_url = _current_url(page)
    url_unchanged = _strip_fragment(pre_url) == _strip_fragment(post_url)
    if not url_unchanged:
        raise FillSafetyError(
            f"page navigated during fill ({pre_url!r} -> {post_url!r}); a "
            "navigation may indicate a submission or redirect")

    # (4) DOM-sweep completeness cross-check -- GREENHOUSE semantics. Workable
    # HAS an independent schema (the public form endpoint), so `schema_required`
    # is that trustworthy oracle and `dom_required` is the LIVE cross-check; any
    # mismatch forces NOT_COMPLETE via _sweep_gaps, and a boolean/checkbox/etc
    # handed off above (or any field whose readback did not confirm) surfaces
    # through kernel.resolve._completeness.
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


def _current_url(page) -> str:
    return getattr(page, "url", "") or ""


def _strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


def _sweep_gaps(mismatch: dict) -> list[dict]:
    """Synthetic `required_unfilled` entries for a DOM-sweep mismatch.

    GREENHOUSE semantics (Workable has an independent schema, unlike Lever): the
    schema is the trusted oracle and the sweep is a cross-check, so ANY mismatch
    (either direction) forces NOT_COMPLETE. `dom_only` = the page requires a field
    the schema did not carry (the schema missed it); `schema_only` = the schema
    marked a field required but the live sweep did not find it required."""
    gaps: list[dict] = []
    for name in mismatch.get("dom_only") or ():
        gaps.append({
            "key": f"dom-sweep:{name}", "label": name,
            "reason": "DOM shows this field as required but it is absent "
                      "from the schema"})
    for name in mismatch.get("schema_only") or ():
        gaps.append({
            "key": f"dom-sweep:{name}", "label": name,
            "reason": "schema marks this field required but the DOM sweep "
                      "did not find it required"})
    return gaps


# -- per-field driving: NATIVE text path only (no react-select, no native select)
# text/email/phone/paragraph/numeric via type_human; a boolean/checkbox/dropdown/
# multiple/group never here (it is handed off before this is reached).


# Every Turnstile-hazard control Workable HANDS OFF rather than auto-drives. A
# boolean yes/no question renders as a radio fieldset (role "radio"); a consent
# box is a checkbox; a dropdown is a CUSTOM react-ish combobox (role "combobox")
# and a multiple-choice is a listbox (role "listbox") whose option DOM is
# unsampled this wave (handed off, never guessed) -- unlike Lever, whose
# "combobox" role is a server-side NATIVE <select> safely driven by select_option.
_HANDOFF_ROLES = frozenset({"checkbox", "radio", "combobox", "listbox"})

_HUMAN_HANDOFF_REASON = (
    "boolean/checkbox/dropdown/multiple/group needs a human-operated trusted "
    "action: the invisible Cloudflare Turnstile intercepts programmatic "
    "checkbox/radio/option clicks mid-form, and Workable's dropdown/multiple "
    "widgets and group '+ Add' opener are unsampled this wave, so this control is "
    "handed off for a human (a required one forces NOT_COMPLETE, never an "
    "auto-click)")


def _is_upload(fv) -> bool:
    from pathlib import Path
    return isinstance(fv.value, Path)


def _needs_human_handoff(fv) -> bool:
    """True for a control whose fill would need a PROGRAMMATIC CLICK -- the
    Turnstile hazard. A boolean tick (a bool value) qualifies, as does any field
    the fieldmap typed with a checkbox/radio/combobox/listbox locator role
    (Workable's dropdown/multiple are CUSTOM widgets, not a Lever native
    `<select>`), as does a flattened GROUP subfield (`<group>.<sub>`), whose
    "+ Add" opener this wave never drives. A plain text/email/phone/paragraph box
    (role "textbox") does NOT qualify and is filled via type_human."""
    if isinstance(fv.value, bool):
        return True
    if fv.locator.role in _HANDOFF_ROLES:
        return True
    return _is_group_subfield(fv)


def _is_group_subfield(fv) -> bool:
    """A flattened Workable group subfield (education/experience), keyed
    `<group>.<sub>` by `fieldmap.parse_workable`. The group container's "+ Add"
    opener is a Part-1 hand-off (never opened), so a group subfield is never
    auto-filled either. No fixed/custom Workable id carries a dot, so the dotted
    key uniquely marks a group subfield."""
    return "." in (fv.key or "")


def _fill_field(page, fv) -> tuple[bool, Any]:
    """Drive one non-upload, non-handoff (text-class) field via the native path;
    returns (landed, actual-read-back). Uses `base.type_human` (human-cadence
    keystrokes, NEVER `fill()`) for the Turnstile score protection, and NEVER
    `base.select_react_combobox` (greenhouse's widget) nor `select_option` (a
    Workable dropdown is a custom widget handed off before this is reached, not a
    native select)."""
    locator = base._locate(page, fv)
    base.type_human(locator, str(fv.value))
    actual, ok = base._readback(locator, fv.value)
    return ok, actual


def _fill_upload(page, fv, uploads: list[dict],
                 extra_skips: list[tuple[str, str]],
                 filled_keys: set[str]) -> None:
    """Attach a whitelisted asset via the reused `base._safe_upload` /
    `kernel.fill_toolkit._locate_file_input` primitives (the real hidden `input[type=file]`; the
    fieldmap's role=button hint never reaches it). Counts as filled ONLY once the
    input's own readback confirms a file attached, mirroring greenhouse's / lever's
    upload path exactly (the SAME base/fill primitives, not a reimplementation)."""
    from engine.kernel.fill_toolkit import _locate_file_input, _upload_attached

    control = _locate_file_input(page, fv)
    if control is None:
        extra_skips.append((fv.key, "no file input located"))
        return
    try:
        base._safe_upload(control, fv.value, _current_assets(fv),
                          page=page, button_name=fv.locator.name or fv.label)
    except FillSafetyError:
        raise
    except Exception as exc:  # per-field upload error is fail-soft
        extra_skips.append((fv.key, f"upload-error: {exc}"))
        return
    if not _upload_attached(control):
        extra_skips.append((fv.key, "upload did not attach (readback)"))
        return
    filled_keys.add(fv.key)
    uploads.append({"key": fv.key, "asset": fv.asset,
                    "path": str(fv.value), "reason": fv.upload_reason})


def _current_assets(fv):
    """Reconstruct a single-path `FillAssets` whitelist for `base._safe_upload`
    from the already-resolved `fv.value`/`fv.asset` (the value is itself one of
    the upstream whitelist's paths), without threading the original FillAssets
    through the Provider contract's `fill(page, fieldmap, values)` signature."""
    kwargs = {"cv_ats": None, "cv_atsi": None, "photo": None}
    slot = {"cv-ats": "cv_ats", "cv-atsi": "cv_atsi",
           "photo": "photo"}.get(fv.asset)
    if slot is not None:
        kwargs[slot] = fv.value
    return FillAssets(**kwargs)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
