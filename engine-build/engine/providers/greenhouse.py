"""Greenhouse provider: the FIRST reference implementation of the `Provider`
contract (`engine.providers.protocol.Provider`), W5.2.

Greenhouse is the schema-API vendor (spec section 3): `capture()` reads the
public `boards-api.../questions=true` endpoint (no browser), so its FieldMap
already carries required/options/section for every field, including the
EEOC/demographic questions (`decline_allowed=True, required=False` set at
capture time in `fieldmap.py`). `fill()` is therefore mostly schema-driven;
the DOM-sweep cross-check (`base.sweep_required` / `base.completeness_
mismatch`) still runs so a schema/DOM divergence is never silently missed
(hole-fix d is load-bearing everywhere, not only for the DOM-only vendors).

`capture` and `apply_url` are thin delegations to the registry wiring already
registered for "greenhouse" (`registry.PROVIDERS["greenhouse"]`) -- this
module adds NO new schema-fetch or URL-building logic, only the `fill()`
sequencing and the vendor-specific bits documented per step below.

LAZY-IMPORT INVARIANT (mirrors `providers/base.py` and `registry.py`): this
module must not import patchright / `engine.browse` at load time, so the
daily poller (which imports `engine.providers` eagerly) stays browser-free.
`engine.fill` (patchright-free itself, but not on the poller's hot path
either) is imported lazily inside the functions that need its private
helpers, matching `base.py`'s own `_fill()` accessor pattern.

SEEDED FIELD-NAME REFERENCE (workpls greenhouse.js, Apache-2.0; W5 spec
section 3): Greenhouse's stable native field names are
`job_application[first_name]`, `job_application[last_name]`,
`job_application[email]`, `job_application[phone]`, `job_application[resume]`
(input id commonly `#resume` / `#first_name` etc. on the legacy embed) plus
`urls[]` for link questions. This module does NOT hardcode those names as
selectors: `fieldmap.Field.locator` (role + name, captured from the live
schema per posting) is authoritative and always preferred (`base._locate`
resolves it directly). The workpls names are kept here as a REFERENCE/
FALLBACK comment only, for a human debugging a selector miss offline; they
are not consulted by any code path in this wave.

DEFERRED (TODO, not built this wave -- see the module-level `_TODO_*`
markers below for the exact seams a later refinement lands in):
- intl-tel-input phone-country widget (a phone field currently goes through
  the same `type_human` path as any other text field; the widget's country
  dropdown is not driven).
- async school/degree typeahead (education questions, when Greenhouse
  exposes them as a typeahead rather than a plain text/select field, are not
  specially handled; they fall through to the generic text/select path,
  which will usually mis-fire on a typeahead's debounce).
Both need a live-DOM probe (W5.2's fixture-validation promise) before they
can be built correctly; stubbing them now would be guessing at behaviour this
wave does not have evidence for.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from engine.fieldmap import FieldMap
from engine.fill import FillAssets, FillReport, FillSafetyError, ResolvedValues
from engine.providers import base, registry

vendor = "greenhouse"

# Greenhouse's schema exposes a `<name>_text` paste-textarea question even
# when the LIVE form is configured for file-upload mode (the textarea is then
# simply ABSENT from the DOM, e.g. `resume_text:TEXTAREA:req=True` alongside
# `resume:input_file:req=True` on the same posting): the sibling `<name>` file
# field already carries the same document, so the textarea is satisfied, never
# driven. Keys mirror `fieldmap._KEY_TEXT_PATHS`'s exact key set.
_TEXT_UPLOAD_SIBLINGS = {"resume_text": "resume",
                        "cover_letter_text": "cover_letter"}


# -- capture / apply_url: thin delegation to the registry wiring ---------------


def capture(slug: str, job_id: str, opener: Any = None) -> FieldMap:
    """The schema fetch: delegates to `registry.PROVIDERS["greenhouse"].
    capture_fn` (itself `fieldmap.capture_greenhouse`, the public
    `boards-api.../questions=true` GET). No new logic here."""
    return registry.resolve(vendor).capture_fn(slug, job_id, opener)


def apply_url(slug: str, job_id: str) -> str:
    """The public apply-page URL: delegates to `registry.PROVIDERS[
    "greenhouse"].apply_url_fn` (`fill.greenhouse_apply_url`)."""
    return registry.resolve(vendor).apply_url_fn(slug, job_id)


# -- value resolution: hole-fix e (structural CV/photo choice) -----------------
# `fill.resolve_values` already renders every field to a concrete FieldValue
# (including a CV upload, per the W4 language-proxy rule). The W5 spec's
# hole-fix e SUPERSEDES that language proxy with a purely structural signal:
# if the form exposes an image/photo upload field (a form property, read from
# the schema -- never posting text), the company is judged formal/large, so
# the ATSI (CV + photo) variant is used and the photo is attached; otherwise
# the plain ATS CV is used. This function calls the existing `fill.
# resolve_values` unchanged (all its other classification stays authoritative:
# text/select/boolean rendering, EEO/demographic exclusion, missing-field
# skips) and then overrides ONLY the already-resolved CV FieldValue(s) to
# match the structural rule. No fill.py code is modified for this: the
# override is a pure post-process of the ResolvedValues this function itself
# produces, using the FillAssets abstraction fill.py already exposes.


def resolve_values(fieldmap: FieldMap, ssot, profile: dict, *,
                   assets: FillAssets | None = None,
                   posting_lang: str = "en") -> ResolvedValues:
    """`fill.resolve_values` plus the hole-fix e structural CV/photo override.

    A field is judged an image/photo upload the same way `fill.
    _form_has_photo_field` already judges it: an upload-type field whose
    LABEL reads as a portrait ask (EN + IT). The label is part of the vendor
    FORM SCHEMA (captured at `capture()` time from Greenhouse's own question
    definitions), never the job posting's free text, so this stays an
    attacker-independent structural signal per anti-injection finding 5.
    """
    from engine import fill as _fill

    resolved = _fill.resolve_values(fieldmap, ssot, profile, assets=assets,
                                    posting_lang=posting_lang)
    if assets is None:
        return resolved
    has_photo_field = _fill._form_has_photo_field(fieldmap)
    wanted = (("cv-atsi", assets.cv_atsi,
              "photo field present on the form (ATSI variant, hole-fix e)")
             if has_photo_field else
             ("cv-ats", assets.cv_ats,
              "no photo field on the form (plain ATS variant, hole-fix e)"))
    wanted_asset, wanted_path, wanted_reason = wanted
    for fv in resolved.fields:
        if fv.asset in ("cv-ats", "cv-atsi") and wanted_path is not None:
            fv.value, fv.asset, fv.upload_reason = (
                wanted_path, wanted_asset, wanted_reason)
    return resolved


# -- fill(): the Provider contract's ordered sequence ---------------------------
# (1) never-send FIRST, (2) drive every field via base.py primitives,
# (3) readback-gate what counts as filled, (4) DOM-sweep cross-check forces
# NOT_COMPLETE on any mismatch, (5) return the existing FillReport dataclass.


def fill(page: Any, fieldmap: FieldMap, values: ResolvedValues, *,
        dry_run: bool = True, company: str | None = None) -> FillReport:
    """Drive an ALREADY-NAVIGATED Greenhouse apply page, STOPPING SHORT OF
    APPLYING. `dry_run` is accepted for interface stability; Part 1 carries
    no submit code path regardless of its value (`install_never_send` is
    unconditional, matching the W5 spec's "no submit code path" hard gate).

    `company` is an ADDITIONAL optional keyword beyond the Protocol's
    mandated `(page, fieldmap, values, *, dry_run=True)` shape (a `Protocol`
    is structural, not signature-exact: an extra defaulted keyword does not
    break conformance). The Protocol carries no company/employer slug --
    `FieldMap` only has `vendor` (the ATS vendor, e.g. "greenhouse") and
    `posting_id`, neither of which is the employer's board slug -- so
    `FillReport.company` (feeds the ntfy caption, "<Vendor> (<company>): ...")
    falls back to `fieldmap.posting_id` when the caller does not supply the
    real slug. This is a genuine gap in the mandated contract (documented,
    not silently papered over): a real orchestration call site should pass
    `company=<slug>` explicitly once one exists.
    """
    ts = _utc_now_iso()

    # (1) STRUCTURAL never-send FIRST: registered before any field is
    # touched, so no interaction can race ahead of the interceptor.
    base.install_never_send(page)

    pre_url = _current_url(page)
    readback_mismatches: list[dict] = []
    extra_skips: list[tuple[str, str]] = []
    uploads: list[dict] = []
    filled_keys: set[str] = set()
    satisfied_by_sibling: set[str] = set()
    # The TRUTHFUL, immediate sibling-satisfaction signal (racy-render
    # hole-fix, 2026-07-06 gitlab/8503792002 full-fill run): a key lands
    # here the moment `_fill_upload` confirms `el.files.length>=1` on the
    # native input, via `engine.fill._upload_attached` with NO `confirm=`
    # argument -- independent of Greenhouse's own (racy) React re-render.
    # `filled_keys` stays the RENDER-CONFIRMED signal (gated on `confirm=`,
    # used for the report's `uploads`/completeness); `file_attached_keys` is
    # the separate, earlier "genuinely on the input" signal the `<name>_text`
    # sibling check below is now based on, so a delayed or still-pending
    # render can never force a paste-textarea to be (mis)driven.
    file_attached_keys: set[str] = set()

    # Uploads FIRST, order-independent of the schema's own field ordering: a
    # `<name>_text` paste textarea's sibling `<name>` file field (hole-fix,
    # BUG 3) must already be in `file_attached_keys` by the time the
    # text-field pass below checks it, regardless of which one the schema
    # lists first.
    upload_fields = [fv for fv in values.fields if _is_upload(fv)]
    other_fields = [fv for fv in values.fields if not _is_upload(fv)]

    for fv in upload_fields:
        _fill_upload(page, fv, uploads, extra_skips, filled_keys,
                    file_attached_keys)
        # Let greenhouse's React render THIS attachment's confirmation before
        # the next upload starts: two file uploads back-to-back saturate the
        # React queue so NEITHER renders its filename in time (live-verified),
        # but a settle between them lets each render independently. Only pays
        # the cost when there is a further upload still to drive.
        if fv is not upload_fields[-1]:
            _settle_page(page)

    # (2) + (3) drive + readback-gate every resolved non-upload field.
    for fv in other_fields:
        sibling_key = _TEXT_UPLOAD_SIBLINGS.get(fv.key)
        if sibling_key is not None and sibling_key in file_attached_keys:
            # Greenhouse's schema exposes BOTH the file field and its
            # paste-text alternative even though the LIVE form renders only
            # one (whichever input mode the org configured); the textarea
            # is simply ABSENT from the DOM when file mode is active, so
            # driving it always times out. The sibling file already carries
            # the same document (genuinely on the input, `file_attached_
            # keys`) -- satisfied, never attempted, regardless of whether
            # Greenhouse's own widget has rendered a visible confirmation
            # yet (that render is `filled_keys`/`uploads`'s OWN separate
            # gate, untouched by this decision).
            extra_skips.append(
                (fv.key, f"satisfied by sibling file upload: {sibling_key}"))
            satisfied_by_sibling.add(fv.key)
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

    # Safety invariant carried over from fill.fill_form: a navigation during
    # the fill is treated as a possible submission/redirect, even though
    # this module never calls page.goto() itself (the page arrives already
    # navigated) -- a stray submit-like interaction could still redirect it.
    post_url = _current_url(page)
    url_unchanged = _strip_fragment(pre_url) == _strip_fragment(post_url)
    if not url_unchanged:
        raise FillSafetyError(
            f"page navigated during fill ({pre_url!r} -> {post_url!r}); a "
            "navigation may indicate a submission or redirect")

    # (3b) FINAL upload re-confirmation (late-render hole-fix, 2026-07-06
    # gitlab/8503792002 full-fill run): a REQUIRED upload's INLINE confirm
    # (above, in `_fill_upload`) can be a FALSE NEGATIVE while Greenhouse's
    # React queue is still busy driving the OTHER fields; by now every field
    # has been driven, React has settled, and a fresh confirm gets a genuine
    # positive where the mid-fill one timed out. Must run BEFORE the
    # completeness/DOM-sweep computation below, so a late confirmation is
    # reflected in `filled_keys`/`uploads`/`satisfied_by_sibling` in time.
    _reconfirm_late_uploads(page, fieldmap, values, filled_keys, uploads,
                           extra_skips, readback_mismatches,
                           satisfied_by_sibling)

    # (4) DOM-sweep completeness cross-check (hole-fix d): a schema/DOM
    # required-field mismatch forces NOT_COMPLETE regardless of what the
    # per-field fill loop achieved. A field satisfied by a sibling upload
    # (BUG 3) is excluded from `schema_required` -- the DOM genuinely never
    # renders it, so it must never be swept as a schema/DOM disagreement --
    # and an uploaded field's OWN label is reconciled against the filename
    # Greenhouse appends to it post-upload (`_reconcile_uploaded_labels`).
    from engine import fill as _fill

    schema_required = {f.label for f in fieldmap.required_fields()
                       if f.key not in satisfied_by_sibling}
    dom_required = _reconcile_uploaded_labels(
        base.sweep_required(page), uploads, fieldmap)
    mismatch = base.completeness_mismatch(schema_required, dom_required)

    filled = len(filled_keys)
    all_skips = list(values.skipped) + extra_skips
    fillable_total, required_unfilled, justified_skips = _fill._completeness(
        fieldmap, filled_keys, all_skips, filled)
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

    Hole-fix d requires ANY mismatch (either direction) to force
    NOT_COMPLETE; these entries make that concrete regardless of whether the
    per-field fill loop otherwise landed every schema-known field."""
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


def _reconcile_uploaded_labels(dom_required: set[str], uploads: list[dict],
                               fieldmap: FieldMap) -> set[str]:
    """Fold a successfully-uploaded file field's POST-FILL DOM-sweep label
    back to its bare schema label before the completeness diff runs (BUG 3).

    Once a resume/cover-letter FILE is attached, Greenhouse appends the
    chosen filename to the control's own accessible label (observed live:
    "Resume/CV" becomes "resume/cv cv-ats.pdf" post-upload), so
    `base.sweep_required`'s POST-FILL scan reads a label the schema's
    PRE-FILL label no longer matches -- a pure upload-confirmation artefact,
    never a genuine schema/DOM disagreement. For every field this run
    actually uploaded, any DOM label that equals or starts with (on a word
    boundary) its normalized schema label is folded back to the bare label,
    so the two sides compare equal again."""
    labels_by_key = {f.key: f.label for f in fieldmap.fields}
    bare_labels = {base._normalize_name(labels_by_key[u["key"]])
                  for u in uploads if u.get("key") in labels_by_key}
    bare_labels.discard("")
    if not bare_labels:
        return dom_required
    reconciled: set[str] = set()
    for dom_label in dom_required:
        folded = dom_label
        for bare in bare_labels:
            if dom_label == bare or dom_label.startswith(bare + " "):
                folded = bare
                break
        reconciled.add(folded)
    return reconciled


# -- per-field driving: text/email/phone via type_human, react-select via -----
# select_react_combobox, everything else via a native locator action ----------


def _is_upload(fv) -> bool:
    from pathlib import Path
    return isinstance(fv.value, Path)


def _is_react_combobox(fv) -> bool:
    """A Greenhouse `multi_value_single_select` / `yes_no` question renders
    as a react-select combobox in the live DOM; the fieldmap locator role
    (set at capture time, `fieldmap._ROLE_FOR_TYPE`) is the structural
    signal, not a type-string guess repeated here."""
    return fv.locator.role == "combobox"


def _fill_field(page, fv) -> tuple[bool, Any]:
    """Drive one non-upload field; returns (landed, actual-or-None).

    `actual` is only meaningful on a text/native-locator path (it is what
    `base._readback` read back); the react-select path reports its own
    landed bool with no separate `actual` value to surface (the combobox
    driver's readback is internal to `select_react_combobox`)."""
    if _is_react_combobox(fv):
        landed = base.select_react_combobox(page, fv.key, str(fv.value))
        return landed, None
    locator = base._locate(page, fv)
    _apply_native(locator, fv)
    actual, ok = base._readback(locator, fv.value)
    return ok, actual


def _apply_native(locator, fv) -> None:
    """Write one value via the safe native action for its shape: `check()`
    for a boolean, `select_option` for a native (non-react) select or a
    resolved option list, `type_human` (human-cadence keystrokes, NEVER
    `fill()`) for everything else -- the reCAPTCHA v3 score protection the
    W5 spec section 3 requires for every human-cadence field."""
    value = fv.value
    if isinstance(value, bool):
        locator.check()
    elif isinstance(value, list):
        locator.select_option(label=value)
    elif fv.type in _NATIVE_SELECT_TYPES:
        locator.select_option(label=value)
    else:
        base.type_human(locator, str(value))


_NATIVE_SELECT_TYPES = frozenset({"multi_value_multi_select"})


def _fill_upload(page, fv, uploads: list[dict],
                 extra_skips: list[tuple[str, str]],
                 filled_keys: set[str],
                 file_attached_keys: set[str]) -> None:
    """Attach a whitelisted asset via the reused `base._safe_upload` /
    `engine.fill._locate_file_input` (the real `<input type=file>` locator;
    the fieldmap's best-effort role=button hint never reaches it). A
    successful upload counts as filled (`filled_keys`/`uploads`) ONLY once
    BOTH the input's own readback confirms a file actually attached AND
    Greenhouse's own rendered widget shows it (`confirm=`, see `base.
    poll_upload_confirmed`): a live probe proved the native FileList alone
    can be non-empty while Greenhouse's React-driven widget never rendered
    the attach (HOSTILE REVIEW #1, 2026-07-06 gitlab/8503792002 run), so
    `el.files.length` on its own is a structural false positive for THAT
    signal. This mirrors `fill._fill_upload`'s contract otherwise exactly
    (this module drives the SAME primitives, not a reimplementation of the
    attach-confirmation logic).

    `file_attached_keys` (FIX 1, racy-render decoupling, 2026-07-06
    gitlab/8503792002 full-fill run): the TRUTHFUL, immediate `el.files.
    length>=1` signal alone -- `engine.fill._upload_attached(control)` with
    NO `confirm=` argument, read right after `set_input_files` -- recorded
    independently of whether the render-confirmed gate below succeeds. This
    is the ONLY signal a `<name>_text` paste-textarea sibling is satisfied
    by (see `fill()`'s `_TEXT_UPLOAD_SIBLINGS` branch): the render can lag
    (or, under a busy full-fill load, not land within even the extended
    poll window) while the file is already genuinely on the input, and the
    textarea must never be driven in that state regardless."""
    from engine import fill as _fill

    control = _fill._locate_file_input(page, fv)
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
    if _fill._upload_attached(control):
        file_attached_keys.add(fv.key)
    confirm = lambda: base.poll_upload_confirmed(page, control, str(fv.value))
    if not _fill._upload_attached(control, confirm=confirm):
        extra_skips.append((fv.key, "upload did not attach (readback)"))
        return
    filled_keys.add(fv.key)
    uploads.append({"key": fv.key, "asset": fv.asset,
                    "path": str(fv.value), "reason": fv.upload_reason})


def _settle_page(page) -> None:
    """Best-effort: wait for the page to reach networkidle then a short fixed
    settle, so React flushes any pending re-render (e.g. an attached-file
    confirmation) before a readback runs. Never raises: a page/fake missing
    either method, or a networkidle timeout under a still-chatty page, simply
    falls through (the subsequent poll is still gated on a real confirmation)."""
    waiter = getattr(page, "wait_for_load_state", None)
    if callable(waiter):
        try:
            waiter("networkidle", timeout=12000)
        except Exception:
            pass
    tw = getattr(page, "wait_for_timeout", None)
    if callable(tw):
        try:
            tw(8000)
        except Exception:
            pass


def _reconfirm_late_uploads(page, fieldmap: FieldMap, values: ResolvedValues,
                            filled_keys: set[str], uploads: list[dict],
                            extra_skips: list[tuple[str, str]],
                            readback_mismatches: list[dict],
                            satisfied_by_sibling: set[str]) -> None:
    """End-of-fill second chance for a REQUIRED upload whose inline
    `_fill_upload` confirmation was a FALSE NEGATIVE (late-render hole-fix,
    2026-07-06 gitlab/8503792002 full-fill run).

    A live probe proved the resume file GENUINELY attaches and Greenhouse
    EVENTUALLY renders the filename confirmation, but not always within
    `_fill_upload`'s own poll window while its React queue is still busy
    driving the REST of the form's fields (an isolated single-field fill
    confirms quickly; a full multi-field fill can take until every other
    field is done). By this function's call site -- after every field has
    been driven -- React has settled, so a FRESH `base.poll_upload_
    confirmed` re-check is a cheap, high-signal second chance. It is gated
    on the exact same real vendor-rendered confirmation `_fill_upload`
    already required: never an unconditional mark-filled.

    Restricted to REQUIRED upload fields still missing from `filled_keys`:
    an optional upload's false negative already resolves to a justified
    skip (`_fill._is_upload_skip`), so re-polling it would spend time
    without changing the report's verdict.

    On a genuine re-confirm: the key joins `filled_keys`/`uploads` (mirrors
    `_fill_upload`'s own success bookkeeping exactly) and its earlier
    "did not attach" skip is dropped. Its paste-text sibling (`_TEXT_
    UPLOAD_SIBLINGS`, e.g. `resume_text` for `resume`), if it was driven and
    marked unsatisfied while the sibling upload still looked unfilled, is
    re-marked satisfied-by-sibling -- the SAME skip-reason string `fill.
    _is_satisfied_by_sibling_upload` recognizes, so completeness stays
    single-sourced with the inline sibling-skip branch above."""
    from engine import fill as _fill

    # Under a busy acceptance harness (per-request audit callbacks on the
    # context keep the event loop churning), Greenhouse's attached-file React
    # render lags PAST even the inline poll and does not flush until the page
    # actually goes quiet. Wait for networkidle plus a short settle HERE, so
    # React has flushed the filename/remove confirmation before this
    # second-chance re-poll runs. Best-effort, never raises.
    _settle_page(page)

    text_sibling_of = {upload_key: text_key
                       for text_key, upload_key in _TEXT_UPLOAD_SIBLINGS.items()}
    # Re-confirm EVERY attempted upload still missing from filled_keys, required
    # AND optional: with two file uploads on one form (resume + cover_letter),
    # BOTH greenhouse React renders lag past the inline poll, so the optional
    # cover_letter needs this second chance too, not just the required resume.
    # `poll_upload_confirmed` still gates on a real vendor-rendered confirmation,
    # so an upload that never actually attached is never promoted here.
    for fv in values.fields:
        if not _is_upload(fv) or fv.key in filled_keys:
            continue
        control = _fill._locate_file_input(page, fv)
        # `control` can be None here precisely BECAUSE the upload SUCCEEDED:
        # greenhouse unmounts the <input type=file> once it re-renders the
        # widget to a filename + remove control, so a None control is NOT a
        # reason to skip. `poll_upload_confirmed` falls back to a page-wide
        # rendered-filename check (needs no control) and returns False only
        # when the filename genuinely is not shown (a real non-attach).
        if not base.poll_upload_confirmed(page, control, str(fv.value)):
            continue
        filled_keys.add(fv.key)
        uploads.append({"key": fv.key, "asset": fv.asset,
                        "path": str(fv.value), "reason": fv.upload_reason})
        _drop_extra_skip(extra_skips, fv.key)
        _drop_readback_mismatch(readback_mismatches, fv.key)
        text_key = text_sibling_of.get(fv.key)
        if text_key is not None and text_key not in filled_keys:
            _drop_extra_skip(extra_skips, text_key)
            _drop_readback_mismatch(readback_mismatches, text_key)
            extra_skips.append(
                (text_key, f"satisfied by sibling file upload: {fv.key}"))
            satisfied_by_sibling.add(text_key)


def _drop_extra_skip(extra_skips: list[tuple[str, str]], key: str) -> None:
    """Remove any existing `extra_skips` entries for `key` IN PLACE: the late
    re-confirmation above supersedes an earlier "did not attach"/fill-error
    entry for the same key with a genuine fill (or a satisfied-by-sibling
    justification), so the stale entry must not linger and double-count."""
    extra_skips[:] = [(k, r) for k, r in extra_skips if k != key]


def _drop_readback_mismatch(readback_mismatches: list[dict], key: str) -> None:
    """Remove any existing `readback_mismatches` entries for `key` IN PLACE
    (mirrors `_drop_extra_skip`; a field the late re-confirmation now counts
    as filled/satisfied must not also be reported as a readback mismatch)."""
    readback_mismatches[:] = [m for m in readback_mismatches
                              if m.get("key") != key]


def _current_assets(fv):
    """`base._safe_upload` requires a `FillAssets` whitelist; `fv.value` is
    already ONE of that whitelist's resolved paths (produced by `resolve_
    values`/`_fill.resolve_values` upstream), so a single-path FillAssets
    keyed to whichever asset slot `fv.asset` names reconstructs an
    equivalent whitelist without threading the original object through the
    Provider contract's `fill(page, fieldmap, values)` signature."""
    kwargs = {"cv_ats": None, "cv_atsi": None, "photo": None,
             "cover_letter": None}
    slot = {"cv-ats": "cv_ats", "cv-atsi": "cv_atsi", "photo": "photo",
           "cover-letter": "cover_letter"}.get(fv.asset)
    if slot is not None:
        kwargs[slot] = fv.value
    return FillAssets(**kwargs)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- DEFERRED (see module docstring): intl-tel-input + async school typeahead --
# Both TODOs below are documentation-only markers (no dead branch is left in
# the fill loop): the generic text/select path already handles these fields
# today, just not with the widget-specific driving a live-DOM probe would
# earn. `_TODO_PHONE_COUNTRY_WIDGET` / `_TODO_SCHOOL_TYPEAHEAD` name the
# exact seam a later change lands in.

_TODO_PHONE_COUNTRY_WIDGET = (
    "A Greenhouse phone question rendered via intl-tel-input needs its "
    "country-code dropdown driven before the number is typed (else the "
    "number lands under the wrong country code); undrivable without a "
    "live-DOM probe of the widget's markup. Seam: _fill_field's non-"
    "combobox branch, keyed on a phone norm_type once the widget's real "
    "selector is captured.")

_TODO_SCHOOL_TYPEAHEAD = (
    "A Greenhouse education question exposed as an async school/degree "
    "typeahead (debounced remote search, not a plain select) is not "
    "specially handled; it falls through to _apply_native's generic "
    "type_human path, which will not wait for or select a suggestion. "
    "Seam: _fill_field, a new branch keyed on the typeahead's structural "
    "signal once captured live (mirrors the react-select branch's role-"
    "based detection).")
