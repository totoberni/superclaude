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
   completeness uses GREENHOUSE semantics: the schema is the oracle and the live
   DOM sweep is a cross-check (`_sweep_gaps` below reuses greenhouse's
   schema-oracle wording, NOT lever's "the DOM sweep is authoritative"). The sweep
   is the kernel's own (same required-control CSS, same visibility filter) under a
   WORKABLE IDENTITY: `_workable_dom_required` keys each control on the schema id
   the control itself carries (`name`, `data-ui`, or its radiogroup's
   `fieldset[data-ui]`) and diffs in KEY space, because on this SPA the kernel's
   generic accessible-name guess cannot name a control the way the schema does and
   so cross-checked nothing (17 phantom gaps on the first live run; see that
   function for the evidence).

2. FILL is LEVER-CLASS (native DOM path, no react-select) for text controls, plus
   the SHARED KERNEL MECHANISM (`engine.kernel.control_toolkit.drive_control`,
   W5.1c) for its boolean (radio) and date controls. Workable renders no
   react-select combobox (that is greenhouse's widget) and no server-side native
   `<select>` this wave has sampled, so `fill()` drives text controls through the
   plain native path (`base.type_human` + readback-gate) and NEVER
   `base.select_react_combobox`. A boolean yes/no question renders as a RADIO
   fieldset (`fieldset[data-ui="QA_n"][role="radiogroup"]`) whose two option
   WRAPPERS (`div[data-ui="option"][role="radio"]`) each carry `aria-checked` and
   `tabindex`; the native `input[type=radio]` beneath each is aria-hidden. So
   `_drive_boolean_radio` locates the ONE wrapper whose VISIBLE TEXT matches the
   resolved intent (never its accessible name, which is the aria-labelledby
   CONCATENATION of the group question and the option word), CLICKS that wrapper
   through the submit-denylist primitive, and confirms via aria-checked -- never a
   `.check()` on the aria-hidden inner input (see that function). A date box is a
   plain typed textbox
   (`_date_control_spec`, `day_cell=None`: no picker this wave has sampled).
   Workable has NO native checkbox-typed control (its schema vocabulary carries
   no such type; see `_WORKABLE_TYPE_MAP`), so `ControlKind.CHECKBOX` is never
   used here.

   Everything else whose fill would need a PROGRAMMATIC CLICK into an UNSAMPLED
   custom widget is still HANDED OFF instead of auto-driven, the same fail-safe
   Lever uses for hCaptcha -- here the hazard is the invisible Cloudflare
   Turnstile the apply page runs. Workable's dropdown (role "combobox") and
   multiple-choice (role "listbox") are CUSTOM JS widgets whose option DOM is
   unsampled in the seven forms captured this wave -- handed off rather than
   guessed (an unresolved wire shape, never auto-clicked) -- as is a flattened
   GROUP subfield (`<group>.<sub>`), whose "+ Add" opener this wave never drives.
   A REQUIRED control that is handed off lands in `required_unfilled` ->
   NOT_COMPLETE, never a silent skip and never a reckless auto-click into an
   unsampled widget that could trip Turnstile mid-form.

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
- boolean -> CLICK the ONE yes/no option WRAPPER matching the resolved intent
  (located by its visible text, `_drive_boolean_radio`), confirmed via
  aria-checked; never a `.check()` on the aria-hidden inner radio input.
- date -> `drive_control(ControlKind.DATE, ..., day_cell=None)`: a plain typed
  textbox, never a picker this wave has sampled (`_date_control_spec`).
- dropdown / multiple_choice / group -> HUMAN HAND-OFF (unsampled widget DOM).

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

import re
from typing import Any

from engine.kernel.contracts import (
    FieldMap, FillAssets, FillReport, FillSafetyError, ResolvedValues)
from engine.kernel.control_toolkit import (
    ControlKind, ControlOutcome, ControlSpec, drive_control)
from engine.kernel.resolve import resolve_values as _kernel_resolve_values
from engine.kernel.fill_toolkit import (
    _REQUIRED_CSS, _accessible_name, _current_url, _fill_upload, _is_upload,
    _safe_click, _strip_fragment, _sweep_gaps, _visible_locators)
from engine.kernel.capture_toolkit import _utc_now_iso
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
# (text via type_human, a file via _safe_upload, a boolean by CLICKING its yes/no
# option wrapper; a dropdown/multiple/group is handed off, never auto-clicked),
# (3) readback-gate what counts
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

    # (1b) OPTIONAL defensive OVERVIEW-tab recovery (W5.1-R2 FX2): AFTER
    # never-send (so this cannot race ahead of the interceptor either), BEFORE
    # `pre_url` is captured (so this recovery's own click is never mistaken for
    # a navigation during the fill) and before any field is touched. See
    # `_maybe_recover_from_overview_tab`.
    _maybe_recover_from_overview_tab(page)

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
        if fv.type in (_BOOLEAN_TYPE, _DATE_TYPE):
            if fv.type == _BOOLEAN_TYPE and not isinstance(fv.value, bool):
                # A boolean whose resolved value is NOT a real bool cannot be
                # driven: `_drive_boolean_radio` picks the Yes/No option by
                # the TRUTHINESS of `fv.value`, and a non-bool has no honest
                # intent to read that from (the string "No" is truthy, so a
                # coerced guess would select "Yes" -- the exact inversion this
                # guard exists to prevent). Hand off BEFORE any locator is
                # built: `page.locator(...)` is never reached for this field,
                # same as the dropdown/multiple/group hand-off below.
                extra_skips.append((fv.key, _NON_BOOL_BOOLEAN_REASON))
                continue
            # W5.1c-R4: a boolean is driven by CLICKING the yes/no option
            # WRAPPER and confirming via aria-checked (`_drive_boolean_radio`),
            # never a `.check()` on the aria-hidden inner input (the R3 QA_ drive
            # timeout, 2026-07-19). A date is still driven through the shared
            # kernel mechanism (a plain typed textbox). FillSafetyError (a
            # submit-denylisted name, or a date value that is not an
            # already-rendered str) is never swallowed here.
            try:
                outcome = (_drive_boolean_radio(page, fv)
                           if fv.type == _BOOLEAN_TYPE
                           else drive_control(_date_control_spec(page, fv)))
            except FillSafetyError:
                raise
            except Exception as exc:  # per-field fill error is fail-soft
                extra_skips.append((fv.key, f"fill-error: {exc}"))
                continue
            if outcome.confirmed:
                filled_keys.add(fv.key)
            elif outcome.driven:
                # DRIVEN but not confirmed: a genuine readback mismatch (the click
                # or type landed on the page but did not stick).
                readback_mismatches.append(
                    {"key": fv.key, "intended": fv.value, "actual": outcome.actual})
                extra_skips.append(
                    (fv.key, outcome.reason or
                     "value did not take (readback mismatch)"))
            else:
                # NEVER driven (e.g. an option that could not be located to exactly
                # one wrapper): parked honestly as a skip, not a readback mismatch --
                # nothing was driven, so there is nothing to have "not taken". A
                # required one still forces NOT_COMPLETE via the completeness census.
                extra_skips.append(
                    (fv.key, outcome.reason or "control was not driven"))
            continue
        if _needs_human_handoff(fv):
            # Turnstile hazard: NEVER auto-click a dropdown / multiple / group
            # (unsampled custom-widget DOM). Hand it off with a clear reason;
            # a required one falls through to required_unfilled ->
            # NOT_COMPLETE.
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
    #
    # The two sides are diffed in KEY space, not label space (`_workable_dom_
    # required`): Workable's controls carry their schema id as their own DOM
    # anchor, so the key is the one identity BOTH sides genuinely share. See that
    # function for the live evidence.
    from engine.kernel.resolve import _completeness

    schema_required = {f.key for f in fieldmap.required_fields()}
    dom_required = _reconcile_uploaded_keys(
        _workable_dom_required(page), uploads, schema_required)
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


# -- defensive OVERVIEW-tab recovery (W5.1-R2 FX2, OPTIONAL belt-and-braces) ---
# PRODUCTION carries NO apply-URL defect here: it always navigates via
# `provider.apply_url()` (capture.py:98's `/apply/` form URL, via
# `_registry.py:157`), never the OVERVIEW base URL. A LIVE anomaly (rokt
# posting, 2026-07-18 TOTO-GATE acceptance run) nonetheless showed a page that
# can still load on the OVERVIEW tab (tab bar OVERVIEW | APPLICATION) with a
# cookie-consent banner overlaying the form, so every field locator timed out.
# This is belt-and-braces recovery ONLY, for that residual case. It is not
# pinned by a captured live DOM fixture (only the pixel-audited screenshot
# exists this wave, no DOM export), so the selectors below are a conservative,
# generic tab/banner convention, not a live-shape pin the way the rest of this
# module's selectors are.

# A tab control carries the ARIA "tab" role; "Application" is the one this
# recovery activates (matched case-insensitively: the live label case is
# unconfirmed this wave).
_TAB_ROLE_CSS = '[role="tab"]'
_APPLICATION_TAB_NAME_RE = re.compile(r"application", re.I)

# The live Workable cookie-consent MODAL is a `<div data-ui="cookie-consent"
# role="dialog" aria-modal="true">` whose classes are hash-obfuscated (styles--...)
# and whose buttons are identified ONLY by `data-ui` (probed 2026-07-19:
# `button[data-ui="cookie-consent-settings"]` "Cookies settings", with accept/
# decline on the same convention). It plus its `data-ui="backdrop"` overlay the
# whole page (aria-modal) and PERSIST across scroll, so it intercepts every field
# and QA click until it is dismissed. The class/id selectors can NEVER reach it (no
# "cookie"/"consent" appears in any class or id) -- the trap that cost two live
# rounds -- so the data-ui convention is tried FIRST.
#
# PREFERENCE (owner privacy policy 2026-07-19: application-necessary cookies only,
# no optional-data use): the DECLINE-optional button, else ACCEPT, else the
# dialog's primary (any NON-settings) button, else the legacy class/id banner. The
# SETTINGS button is NEVER a target (it opens a SECOND dialog): the decline/accept
# `data-ui` contains-matches cannot match "settings", and the primary-button
# fallback excludes it explicitly. The `data-ui` values are pinned on the CONVENTION
# with a contains-match (conservative and unambiguous), no live HTML copy existing.
_COOKIE_CLASS_CSS = (
    '[class*="cookie" i] button, [id*="cookie" i] button, '
    '[class*="consent" i] button, [id*="consent" i] button')

_COOKIE_DISMISS_SELECTORS = (
    '[data-ui="cookie-consent"] button[data-ui*="decline" i]',
    '[data-ui="cookie-consent"] button[data-ui*="accept" i]',
    '[data-ui="cookie-consent"] button:not([data-ui*="settings" i])',
    _COOKIE_CLASS_CSS,
)


def _maybe_recover_from_overview_tab(page) -> None:
    """Belt-and-braces: dismiss a cookie-consent banner (it can overlay the tab
    bar too), then click the APPLICATION tab if one is present and not already
    selected.

    A no-op whenever these elements are absent -- an ordinary /apply/-loaded
    page (no tab bar, or the Application tab already selected: the form is
    already present, so nothing is clicked) and every offline fake in this
    suite -- because `_visible_locators` treats a page/locator missing the
    probed method as zero matches rather than raising (see its own
    docstring). Never calls `page.goto`; only clicks a control already on the
    page, through the SAME `_safe_click` submit-denylist primitive the upload
    hand-off path uses, so a control whose name happens to match
    submit/apply/send/finish/continue is refused rather than blindly clicked
    -- and, per that primitive's own contract, the resulting FillSafetyError
    is never swallowed here either (a genuinely ambiguous click aborts the
    fill, same as everywhere else in this module)."""
    _dismiss_cookie_banner(page)
    _activate_application_tab(page)


def _dismiss_cookie_banner(page) -> None:
    """Dismiss the cookie-consent overlay, PREFERRING the decline-optional button.

    Tries `_COOKIE_DISMISS_SELECTORS` in preference order (the data-ui convention
    first, since the live Workable modal's hash-obfuscated classes make the class/id
    selectors match nothing), clicking the first visible control that clicks cleanly
    and stopping there. The SETTINGS button is never a target (it opens a second
    dialog). Best-effort: an unclickable/stale control is skipped, but a
    FillSafetyError (a submit-like name) is never swallowed."""
    for selector in _COOKIE_DISMISS_SELECTORS:
        for button in _visible_locators(page, selector):
            name = _accessible_name(button)
            try:
                _safe_click(button, name)
            except FillSafetyError:
                raise
            except Exception:
                continue  # best-effort: an unclickable/stale control is not fatal
            return  # one dismissal is enough


def _activate_application_tab(page) -> None:
    for tab in _visible_locators(page, _TAB_ROLE_CSS):
        name = _accessible_name(tab)
        if not _APPLICATION_TAB_NAME_RE.search(name):
            continue
        if _dom_attr(tab, "aria-selected").lower() == "true":
            continue  # already active: the form is already present, do nothing
        try:
            _safe_click(tab, name)
        except FillSafetyError:
            raise
        except Exception:
            continue  # best-effort: an unclickable/stale tab is not fatal
        break


# -- per-field driving: NATIVE text path only (no react-select, no native select)
# text/email/phone/paragraph/numeric via type_human; a boolean clicks its yes/no
# option wrapper (`_drive_boolean_radio`, W5.1c-R4), a date drives through the
# shared kernel mechanism (drive_control, W5.1c); a dropdown/multiple/group never
# here (it is handed off before this is reached).

_BOOLEAN_TYPE = "boolean"
_DATE_TYPE = "date"

# The Yes/No option text Workable renders on a boolean's two option wrappers
# (`kernel_accessible_name_sweep_aliases.option_text` in
# tests/fixtures/providers/workable/apply-dom-0F5F662A46.json pins this as the
# live shape). See `_drive_boolean_radio`.
_YES_OPTION_TEXT = "Yes"
_NO_OPTION_TEXT = "No"

# Every remaining Turnstile-hazard control Workable HANDS OFF rather than
# auto-drives (W5.1c narrowed this from the prior boolean/checkbox/dropdown/
# multiple set): a dropdown is a CUSTOM react-ish combobox (role "combobox")
# and a multiple-choice is a listbox (role "listbox") whose option DOM is
# unsampled this wave (handed off, never guessed) -- unlike Lever, whose
# "combobox" role is a server-side NATIVE <select> safely driven by select_option.
_HANDOFF_ROLES = frozenset({"combobox", "listbox"})

_HUMAN_HANDOFF_REASON = (
    "dropdown/multiple/group needs a human-operated trusted action: Workable's "
    "dropdown/multiple widgets and group '+ Add' opener are unsampled this wave, "
    "so this control is handed off for a human (a required one forces "
    "NOT_COMPLETE, never an auto-click)")

# OWNER RULING (2026-07-19): NO blanket Turnstile hand-off. A Turnstile-adjacent
# boolean radio (the QA_11599466 class) is DRIVABLE and stays agent-managed
# whenever the resolver supplies a GENUINE derived boolean (RS-d derives it from
# the commute-gate policy): `fill()`'s loop above drives it through
# `_drive_boolean_radio` (click the matching option wrapper, confirm via
# aria-checked). The hand-off below is preserved ONLY when NO genuine boolean exists --
# i.e. the value is not actually a `bool` (upstream resolution bug, malformed
# SSOT entry, etc). Picking an option by TRUTHINESS off a non-bool is a silent
# guess, not an answer -- the string "No" is truthy -- so this is handed off
# rather than coerced, the same never-guess philosophy `_date_control_spec`
# already applies to a date value. This is the ToS boundary, not caution: only a
# genuinely non-automatable interaction stays human.
_NON_BOOL_BOOLEAN_REASON = (
    "boolean question resolved to a non-boolean value: this control needs a "
    "human-operated trusted action, because choosing an option by the "
    "TRUTHINESS of a non-bool value would be a silent guess, not an answer "
    "(a required one forces NOT_COMPLETE, never an auto-click into the "
    "Turnstile-protected radio)")


def _needs_human_handoff(fv) -> bool:
    """Per-vendor variant (differs from the kernel generic
    `fill_toolkit._needs_human_handoff`, W5.1 Stage 7): Workable ALSO hands off
    its custom combobox/listbox widgets and flattened GROUP subfields, so its
    hazard set is a superset of the generic checkbox/radio one and cannot import
    the kernel helper.

    True for a control whose fill would need a PROGRAMMATIC CLICK into an
    UNSAMPLED custom widget -- a Workable dropdown/multiple (CUSTOM widgets, not
    a Lever native `<select>`) -- or a flattened GROUP subfield (`<group>.<sub>`),
    whose "+ Add" opener this wave never drives. Boolean and date fields are
    driven earlier in `fill()`'s loop via `drive_control` and never reach this
    check. A plain text/email/phone/paragraph box (role "textbox") does NOT
    qualify and is filled via type_human."""
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
    native select).

    A `phone` field takes a SECOND look when the strict readback rejects it: the
    control is an intl-tel-input widget that rewrites what it was typed
    (`_phone_landed`). Every other type keeps the strict readback verbatim."""
    locator = base._locate(page, fv)
    base.type_human(locator, str(fv.value))
    actual, ok = base._readback(locator, fv.value)
    if not ok and fv.type == _PHONE_TYPE:
        ok = _phone_landed(fv.value, actual)
    return ok, actual


# -- boolean / date: the shared kernel control mechanism (W5.1c) ---------------


# The radiogroup fieldset and its option-wrapper CSS. LIVE SHAPE (rokt /apply/
# probe 2026-07-19; movement-labs 0F5F662A46): a boolean renders as
# `fieldset[data-ui="<key>"][role="radiogroup"]` whose options are
# `div[data-ui="option"][role="radio"]` WRAPPERS -- each carries `aria-checked`,
# `aria-required` and `tabindex` and is the node a human clicks; the native
# `input[type=radio]` beneath each is aria-hidden. The wrapper ids are RANDOM per
# render (`wrapper_KlEp8o0xeFOe6Iq2` style), so they are NEVER pinned; the stable
# anchors are the fieldset's schema `data-ui` key and the option marker.
_RADIOGROUP_CSS = 'fieldset[data-ui="{key}"][role="radiogroup"]'
_OPTION_WRAPPER_CSS = 'div[data-ui="option"][role="radio"]'

_OPTION_NOT_LOCATED_REASON = (
    "boolean radio: could not locate the Yes/No option to exactly one wrapper in "
    "its radiogroup (option-text drift, or the page re-rendered the option shape); "
    "parked for a human rather than driving an ambiguous control (a required one "
    "forces NOT_COMPLETE, never a blind auto-click)")

_RADIO_READBACK_FAIL_REASON = (
    "value did not take: aria-checked did not read back 'true' on the clicked "
    "option (or its sibling stayed selected -- a broken single-select)")

# R5 residual (F-8, 2026-07-19): the locate chain resolves the wrapper live (one
# per option), but the CLICK ACTION timed out on actionability -- the radiogroup
# is LAZY-rendered ~4-5s post-load at the page BOTTOM, and the cookie-consent
# banner overlays it during the engine session. So the wrapper is scrolled into
# view and the banner dismissed before the click; a click that still cannot land
# PARKS, naming the blocker from playwright's own actionability call log.
_CLICK_BLOCKED_REASON = (
    "boolean radio: the option wrapper resolved to exactly one node but the click "
    "did not become actionable within the bounded timeout -- the radiogroup is "
    "lazy-rendered at the page bottom and can be overlaid by the cookie-consent "
    "banner. Scrolled it into view and dismissed the banner first; parked for a "
    "human because the click still could not land")

# The line of a playwright click-timeout call log that names the obstructing node.
_INTERCEPT_RE = re.compile(r".*intercepts pointer events.*", re.I)


def _radiogroup_option(page, key: str, option_text: str):
    """The ONE option wrapper for `option_text` inside the `key` radiogroup.

    LIVE ROOT CAUSE this construction replaces (R3 QA_11599465/66 drive timeout,
    2026-07-19): the old spec built `page.locator('fieldset[data-ui=<key>]')
    .get_by_role("radio", name=option_text, exact=True)` and handed it to the
    kernel's `.check()` driver (`drive_control` -> `control_toolkit._drive_toggle`
    -> `Locator.check`). That EXACT-NAME match resolved to ZERO live elements, so
    `.check()` timed out waiting on the locator: an option wrapper's accessible
    name is its `aria-labelledby` CONCATENATION of the GROUP question label and
    the option word (`aria-labelledby="<group>_label radio_label_<n>"`), never the
    bare "Yes"/"No". The fixture corroborates it -- the FIELD-LEVEL locator
    `get_by_role("radio", name=<group label>)` matched BOTH wrappers (`apply-dom-
    0F5F662A46.json::locator_resolution.fields` count 2), which is only possible
    if BOTH wrappers' accessible names carry the group label.

    So this matches the option by its own VISIBLE TEXT instead (the wrapper's
    label text is only the option word; the group label lives in a separate
    element referenced by id, never nested in the wrapper). The match is scoped to
    the ONE radiogroup by the fieldset `data-ui` key and to option nodes by the
    shared marker, then filtered to the single wrapper whose text is exactly the
    option word (`has_text` on an anchored, case-insensitive pattern, so a wrapper
    carrying extra decoration text PARKS rather than mis-driving)."""
    group = page.locator(_RADIOGROUP_CSS.format(key=key))
    return group.locator(_OPTION_WRAPPER_CSS).filter(
        has_text=re.compile(rf"^\s*{re.escape(option_text)}\s*$", re.I))


def _drive_boolean_radio(page, fv) -> ControlOutcome:
    """Drive one boolean (yes/no) question by CLICKING the option wrapper matching
    the resolved bool, then confirming via `aria-checked` (W5.1c-R4).

    `fv.value` is guaranteed to be a real `bool` here: `fill()`'s dispatch hands
    off a non-bool value BEFORE this is called, so the truthiness pick below never
    coerces a guess. The intent picks WHICH option is driven (selecting "No" IS
    selecting a different option, not clearing "Yes"), never a boolean toggle.

    The wrapper is the interactive node (it carries `tabindex` and `aria-checked`),
    so it is CLICKED through `_safe_click` -- never `.check()` on the aria-hidden
    inner input, the R3 timeout mode. `_safe_click` keeps the submit denylist in
    force and its FillSafetyError is never swallowed. When the option cannot be
    located to exactly one wrapper the control is PARKED honestly by name (driven
    is False), never driven blind. Confirmation is never-confirmed-biased: a click
    the page silently dropped leaves aria-checked "false" and surfaces as a
    required gap, exactly as an unconfirmed text fill does.

    ACTIONABILITY (R5 residual): the radiogroup is lazy-rendered at the page bottom
    and can be overlaid by the cookie-consent banner, so the resolved wrapper is
    scrolled into the viewport and the banner dismissed BEFORE the click (the
    banner can render after the fill-start recovery, e.g. once the page is
    scrolled, so this per-drive dismissal is load-bearing on top of the fill-start
    one). A click that still cannot land within its bounded timeout PARKS, naming
    the obstructing node from playwright's own call log rather than a bare
    timeout."""
    driven_text = _YES_OPTION_TEXT if fv.value else _NO_OPTION_TEXT
    name = fv.locator.name or fv.label
    option = _radiogroup_option(page, fv.key, driven_text)
    if _option_count(option) != 1:
        return ControlOutcome(key=fv.key, kind=ControlKind.RADIO, driven=False,
                              confirmed=False, reason=_OPTION_NOT_LOCATED_REASON)
    # Bring the lazy-rendered wrapper into view, THEN clear an overlay that may
    # have rendered at the page bottom on scroll, THEN click (order matters: a
    # scroll-triggered banner is only visible to the dismissal after the scroll).
    _scroll_into_view(option)
    _dismiss_cookie_banner(page)
    try:
        _safe_click(option, name)
    except FillSafetyError:
        raise
    except Exception as exc:  # actionability timeout: park, naming the blocker
        return ControlOutcome(key=fv.key, kind=ControlKind.RADIO, driven=False,
                              confirmed=False, reason=_click_blocked_reason(exc))
    actual = _dom_attr(option, "aria-checked")
    confirmed = (actual.lower() == "true"
                 and _sibling_not_selected(page, fv.key, driven_text))
    return ControlOutcome(
        key=fv.key, kind=ControlKind.RADIO, driven=True, confirmed=confirmed,
        actual=actual, reason="" if confirmed else _RADIO_READBACK_FAIL_REASON)


def _scroll_into_view(locator) -> None:
    """Best-effort scroll (never raises): the lazy-rendered radiogroup sits at the
    page bottom, so the option wrapper is brought into the viewport before the
    trusted click. A fake/locator with no such method is a no-op, and a scroll that
    itself times out does not fail the drive -- the click is the real gate."""
    scroller = getattr(locator, "scroll_into_view_if_needed", None)
    if not callable(scroller):
        return
    try:
        scroller()
    except Exception:
        pass


def _click_blocked_reason(exc) -> str:
    """The park reason for an actionability timeout, naming what BLOCKED the click
    from playwright's own call log so a census reads the overlay/off-viewport cause
    rather than a bare timeout."""
    detail = _blocking_element(str(exc))
    return f"{_CLICK_BLOCKED_REASON} [{detail}]" if detail else _CLICK_BLOCKED_REASON


def _blocking_element(message: str) -> str:
    """The most informative line of a playwright click-timeout call log: the node
    that intercepts pointer events if one is named, else the last non-empty line."""
    for line in message.splitlines():
        if _INTERCEPT_RE.search(line):
            return line.strip()[:200]
    lines = [ln.strip() for ln in message.splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""


def _sibling_not_selected(page, key: str, driven_text: str) -> bool:
    """Single-select integrity: the OTHER option must not also read back selected.

    A sibling that resolves to exactly one wrapper reading aria-checked="true" is a
    broken single-select (two options live at once) and fails the drive. A sibling
    that is false -- or that cannot be uniquely located -- does not by itself veto
    an option that already read back true (never-fail bias on this ancillary probe,
    so a flaky sibling read can never sink a genuine tick)."""
    other = _radiogroup_option(page, key, _other_option_text(driven_text))
    if _option_count(other) != 1:
        return True
    return _dom_attr(other, "aria-checked").lower() != "true"


def _other_option_text(option_text: str) -> str:
    return _NO_OPTION_TEXT if option_text == _YES_OPTION_TEXT else _YES_OPTION_TEXT


def _option_count(locator) -> int:
    """How many live elements a filtered option locator matches, guarded (a partial
    fake or a detached scope reads as zero, never a raised drive)."""
    counter = getattr(locator, "count", None)
    if not callable(counter):
        return 0
    try:
        return int(counter())
    except Exception:
        return 0


def _date_control_spec(page, fv) -> ControlSpec:
    """Build the ControlSpec for one date field.

    Workable's date box is a plain TYPED textbox (role "textbox"), never a
    picker this wave has sampled (module docstring's FIELD-DRIVING SPECIFICS;
    the live routing evidence for "When can you start?" in the same fixture's
    `locator_resolution.fields` reads `routing: "driven"`, role "textbox",
    count 1). `day_cell` stays None: there is no picker-only calendar to
    invent for. `fv.value` is passed through UNCHANGED -- it is already the
    upstream-rendered string the resolve layer produced (`engine.content`'s
    pinned date path); this module never formats a date, and passing it
    verbatim lets the kernel's own `FillSafetyError` guard catch a non-str
    value rather than this code silently coercing one."""
    locator = base._locate(page, fv)
    return ControlSpec(key=fv.key, kind=ControlKind.DATE, locator=locator,
                      value=fv.value, name=fv.locator.name or fv.label,
                      day_cell=None)


# -- phone: the intl-tel-input readback (W5B-WORKABLE live finding) -------------

_PHONE_TYPE = "phone"

# A country CALLING code is 1 to 3 digits (ITU-T E.164). Only that many leading
# digits may go missing from the readback, and only off the FRONT.
_MAX_CALLING_CODE_DIGITS = 3


def _phone_landed(intended, actual) -> bool:
    """Did the phone value land, given that the widget rewrites it?

    LIVE EVIDENCE (movement-labs 0F5F662A46, 2026-07-13): the phone box is an
    intl-tel-input. Typing a full international number moves the leading country
    CALLING code out of the text box and into the widget's own country combobox
    (which this module never drives), then reformats the national remainder with
    spaces. A 12-digit international number typed in full reads back as its 10
    national digits: correct on the page, yet `base._readback`'s exact
    string compare calls it a miss, so the live run booked a filled Phone as
    `required_unfilled` ("value did not take"). That is a FALSE NEGATIVE in the
    readback, not a fill failure.

    The retry stays NARROW so the gate keeps biting: compared on DIGITS only,
    the readback confirms ONLY when it is the whole intended number, or the
    intended number minus a 1-to-3 digit calling code taken off the FRONT. An
    EMPTY box never confirms; digits dropped off the END (a truncating maxlength,
    a value the page silently rejected) never confirm; a different number never
    confirms. Each of those still lands in `required_unfilled` -> NOT_COMPLETE.
    """
    want, got = _digits(intended), _digits(actual)
    if not want or not got:
        return False
    if got == want:
        return True
    absorbed = len(want) - len(got)
    return 1 <= absorbed <= _MAX_CALLING_CODE_DIGITS and want.endswith(got)


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value if value is not None else ""))


# -- the DOM sweep: WORKABLE keys on the control's OWN anchor -------------------
# W5B-WORKABLE DEVIATION MARKER: this vendor does NOT call `base.sweep_required`
# (which the other three vendors use). It runs the SAME sweep -- the same
# required-control CSS, the same kernel visibility filter -- and changes only the
# IDENTITY it reads off each control, because on Workable the kernel's
# accessible-name heuristic cannot name a control the way the schema does. The
# cross-check is not relaxed by this: it is made able to compare at all (see
# `_workable_dom_required`).

# `data-ui="option"` marks a radio OPTION wrapper inside a radiogroup fieldset.
# It is a role marker shared by every option on the form, NEVER a field id, so it
# must not be read as one (it would key four separate controls to one bogus
# "option" field and bite as a phantom schema gap).
_OPTION_MARKER = "option"

# The ARIA role an option wrapper carries (`div[role="radio"]`). With
# `_OPTION_MARKER` it identifies the ONLY kind of nameless control allowed to
# borrow its enclosing radiogroup's key (see `_is_option_wrapper`).
_RADIO_ROLE = "radio"

_CLOSEST_FIELDSET_JS = (
    "el => { const fs = el.closest('fieldset[data-ui]');"
    " return fs ? fs.getAttribute('data-ui') : ''; }")


def _workable_dom_required(page) -> set[str]:
    """The live required-control set, as SCHEMA KEYS (the cross-check's DOM side).

    LIVE EVIDENCE (movement-labs 0F5F662A46, 2026-07-13): every control Workable
    renders carries its schema field id as its own DOM anchor -- `name` on the
    text/textarea/date/radio inputs, `data-ui` on the resume file input, and the
    `fieldset[data-ui="QA_n"]` radiogroup around a boolean's option wrappers. The
    KEY is therefore the one identity the schema and the page genuinely share.

    `base.sweep_required` instead names a control by the kernel's generic
    accessible-name heuristic (aria-label, then placeholder, then `name`, then
    text), and Workable's controls carry no aria-label. On this form that named
    the SAME 18 fields four different ways at once -- the `name` attribute
    (`firstname`, `qa_11919114`), a placeholder for the date box (which is
    LOCALE-DEPENDENT, a property of the BROWSER SESSION and not of the page:
    `dd/mm/yyyy` under this host's browser, navigator.language=en-GB, probed
    twice on 2026-07-14, and `mm/dd/yyyy` under an en-US one -- so it is DERIVED
    from the live page at generation time, never assumed; see the fixture's
    `_locale_dependent_attributes`), an option's own text (`yes`, `no`), and the
    asterisked label with the intl-tel country code glued on (`phone +39`) --
    none of which a schema LABEL can equal. The live run booked 17 phantom `required_unfilled` gaps against
    fields that were correctly filled, while every genuinely missing field was
    ALREADY reported by the completeness census. A cross-check that fires on all
    18 fields regardless of their state is not a check.

    So the sweep keeps its teeth and loses its noise: same CSS, same visibility
    filter, keys instead of guessed names. It still bites both ways --
    a required control whose key is not in the schema is a `dom_only` gap (the
    schema GET missed a field the page requires), and a schema-required field
    with no required-marked control on the page is a `schema_only` gap -- and a
    control with NO anchor at all falls back to its accessible name, which
    matches no schema key and therefore bites too. Fixture:
    `tests/fixtures/providers/workable/apply-dom-0F5F662A46.json` pins the live
    shape this reads.
    """
    keys: set[str] = set()
    for locator in _visible_locators(page, _REQUIRED_CSS):
        key = _control_key(locator)
        if key:
            keys.add(key)
    return keys


def _reconcile_uploaded_keys(dom_required: set[str], uploads: list[dict],
                             schema_required: set[str]) -> set[str]:
    """Fold a successfully-uploaded key back into the DOM-required set before the
    cross-check diff (the WORKABLE analogue of greenhouse's
    `_reconcile_uploaded_labels`, BUG-3 class).

    LIVE EVIDENCE (movement-labs 0F5F662A46, 2026-07-13): once the resume file
    ATTACHES, Workable CLEARS the file input's `required`/`aria-required` marker
    (verified: required reads back None post-upload). The post-fill sweep then no
    longer sees `resume` as required while the schema still marks it required, so
    the diff booked a `schema_only` gap against a field that is genuinely
    satisfied. That is a benign post-upload artefact, not a missing field.

    THE INVARIANT (both halves are load-bearing): a key is folded back ONLY when
    it is BOTH (a) a PROVEN attachment and (b) SCHEMA-REQUIRED.

    (a) keeps the sweep's teeth. `uploads` carries only keys whose own input
        readback confirmed a file attached (`_upload_attached` in `_fill_upload`),
        so a required upload that FAILED to attach is absent from `uploads` and
        its `schema_only` gap still bites (the completeness census reports it
        too). A text field is never in `uploads` (`_is_upload` keys on a `Path`
        value), so the essays and the QA_ questions keep the full sweep.
    (b) stops the fold from MANUFACTURING a gap. The fold ADDS to the DOM side
        (greenhouse's analogue only REWRITES an existing label, so it cannot),
        and `completeness_mismatch` books every DOM-side key the schema does not
        REQUIRE as a `dom_only` gap. An unbounded union therefore invented a
        phantom `required_unfilled` entry against an OPTIONAL file field that had
        uploaded PERFECTLY: 2 such gaps on the powerlines form and 2 on io-global
        (both REAL sampled schemas, io-global being this wave's backup seal
        candidate), reproduced end to end through this fill(). Intersecting with
        `schema_required` first means an optional upload can never reach the
        required side, so the fold can only ever RESTORE a key the schema itself
        requires -- never add one, never remove one.
    """
    if not uploads:
        return dom_required
    uploaded_keys = {u.get("key") for u in uploads if u.get("key")}
    return dom_required | (uploaded_keys & schema_required)


def _control_key(locator) -> str:
    """One required control -> the schema key it belongs to.

    Precedence is load-bearing: `name` first (the field's own id on every input
    and textarea), then -- for an OPTION WRAPPER ONLY -- the enclosing
    radiogroup's `fieldset[data-ui]` (a boolean's visible option wrapper carries
    no name of its own), then the control's own `data-ui` (the resume file
    input's only anchor), and finally the generic accessible name for a control
    with no anchor at all -- which is no schema key, so it bites as a `dom_only`
    gap rather than passing silently. The radiogroup is consulted BEFORE
    `data-ui` precisely because an option wrapper's `data-ui` is the `option`
    MARKER, not an id.

    The option-wrapper gate on the radiogroup fallback is ANTI-SILENCER, not
    cosmetic. Ungated, the fallback keyed ANY nameless control merely NESTED in a
    known radiogroup to that group: a required control the page carries and the
    schema does NOT (exactly what the `dom_only` direction of the sweep exists to
    catch) was absorbed into an existing schema key and booked no gap. Only a
    control that IS one of the group's own options may borrow the group's key."""
    name = _dom_attr(locator, "name")
    if name:
        return name
    if _is_option_wrapper(locator):
        group = _closest_radiogroup_key(locator)
        if group:
            return group
    data_ui = _dom_attr(locator, "data-ui")
    if data_ui and data_ui != _OPTION_MARKER:
        return data_ui
    return _accessible_name(locator)


def _is_option_wrapper(locator) -> bool:
    """Is this control one of a radiogroup's own OPTION wrappers?

    LIVE SHAPE (movement-labs 0F5F662A46): `div[role="radio"][data-ui="option"]`,
    aria-required, no `name`. Either marker alone is enough (they are redundant
    on the live page, and a future Workable release that drops one leaves the
    wrapper still keyed by the other), but a control carrying NEITHER is a
    FOREIGN control that merely sits inside the fieldset, and it must keep its
    own identity so the sweep can bite on it."""
    if _dom_attr(locator, "data-ui") == _OPTION_MARKER:
        return True
    return _dom_attr(locator, "role") == _RADIO_ROLE


def _dom_attr(locator, name: str) -> str:
    """One attribute off a locator, guarded end to end (a partial fake or a
    detached node reads as absent, never as a raised sweep)."""
    getter = getattr(locator, "get_attribute", None)
    if getter is None:
        return ""
    try:
        return (getter(name) or "").strip()
    except Exception:
        return ""


def _closest_radiogroup_key(locator) -> str:
    """The `data-ui` of the nearest enclosing `fieldset[data-ui]`, or "".

    This is how a boolean's visible option wrapper (`div[role=radio]
    [data-ui=option]`, aria-required, no name) reports the QA_ key it answers.
    Guarded: a locator with no `.evaluate` (a partial fake) or a failing
    evaluation reads as no radiogroup rather than crashing the sweep."""
    evaluate = getattr(locator, "evaluate", None)
    if not callable(evaluate):
        return ""
    try:
        return (evaluate(_CLOSEST_FIELDSET_JS) or "").strip()
    except Exception:
        return ""
