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

`capture` reaches this vendor's schema fetch through its own
`engine.providers.greenhouse.capture` module at call time, and `apply_url`
its own `.capture` URL builder --
this module adds NO new schema-fetch or URL-building logic, only the `fill()`
sequencing and the vendor-specific bits documented per step below (the
registry's `_registry.PROVIDERS["greenhouse"]` spec looks both up lazily).

LAZY-IMPORT INVARIANT (mirrors `providers/base.py` and `_registry.py`): this
module must not import patchright / a browser-capture module at load time, so the
daily poller (which imports `engine.providers` eagerly) stays browser-free.
Module-scope imports are the kernel (`engine.kernel.contracts` for `FieldMap`),
`providers/base`, and this package's own `.resolve` (all browser-free by
construction); this module no longer imports `engine.fill` at all.

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

The async school/degree/discipline education typeahead (education questions
Greenhouse exposes as a debounced remote-search select rather than a plain
text/select field) IS now driven, by `select_education_typeahead` -- the
async-aware sibling of the react-select combobox driver (same live-region
readback + focus-following keyboard Enter, plus a bounded settle for the
debounce). Its offline seam is pinned by test; the live debounce timing is a
TB5-R2 toto-gate confirmation (offline fixtures cannot prove it).

DEFERRED (TODO, not built this wave -- see the module-level `_TODO_*` marker
below for the exact seam a later refinement lands in):
- intl-tel-input phone-country widget (a phone field currently goes through
  the same `type_human` path as any other text field; the widget's country
  dropdown is not driven). It needs a live-DOM probe (W5.2's fixture-validation
  promise) before it can be built correctly; stubbing it now would be guessing
  at behaviour this wave does not have evidence for.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

# Dataclasses come straight from their canonical kernel home rather than via
# `engine.fill`: a top-level `from engine.fill import ...` here would re-enter a
# half-initialised `engine.fill` and raise. (Since wave 4B1 this module has NO
# `engine.fill` import at any scope; primitives come from the kernel.)
from engine.kernel.contracts import (
    FieldMap, FieldValue, FillAssets, FillReport, FillSafetyError,
    ResolvedValues)
# Generic form-driving primitives the moved react-select / upload-poll widget
# cluster (below) calls bare, exactly as it did when it lived in providers/base
# (base re-exports these same kernel names). Not monkeypatched anywhere.
from engine.kernel.fill_toolkit import (
    _current_assets, _current_url, _is_upload, _locator_text, _needs_human_handoff,
    _normalize_name, _strip_fragment, _sweep_gaps, type_human)
from engine.kernel.control_toolkit import ControlKind, ControlSpec, drive_control
from engine.kernel.capture_toolkit import _utc_now_iso
from engine.kernel.resolve import resolve_values as _kernel_resolve_values
from engine.kernel.ssot import MISSING
from engine.providers import base
from engine.providers.greenhouse.resolve import GREENHOUSE_WIDGET_RESOLVER
# The education-typeahead field type is MINTED by capture (the producer) and
# CONSUMED here (the detector). Single source of truth; capture is browser-free
# so this module-scope import keeps the lazy-import invariant (no browser code
# loaded at fill import). `capture()` below still lazily imports
# `capture_greenhouse` separately to preserve its monkeypatch seam.
from engine.providers.greenhouse.capture import EDUCATION_TYPEAHEAD_TYPE

vendor = "greenhouse"

# Greenhouse's schema exposes a `<name>_text` paste-textarea question even
# when the LIVE form is configured for file-upload mode (the textarea is then
# simply ABSENT from the DOM, e.g. `resume_text:TEXTAREA:req=True` alongside
# `resume:input_file:req=True` on the same posting): the sibling `<name>` file
# field already carries the same document, so the textarea is satisfied, never
# driven. Keys mirror `fieldmap._KEY_TEXT_PATHS`'s exact key set.
_TEXT_UPLOAD_SIBLINGS = {"resume_text": "resume",
                        "cover_letter_text": "cover_letter"}

# W5.1c (research verdict 2026-07-13): "a programmatic click is riskier than a
# programmatic keystroke" was a folk belief with no supporting evidence -- both
# go through Playwright/Patchright's native APIs (CDP -> isTrusted=true, the
# same trust tier as this vendor's own `type_human` keystrokes) as long as no JS
# dispatchEvent is used, which this path never does. `_needs_human_handoff`
# (`fill_toolkit`: a boolean tick, or a checkbox/radio locator role) still NAMES
# the click-hazard set; greenhouse no longer DEFERS it to a human, it DRIVES it
# through the shared kernel mechanism (`control_toolkit.drive_control`) via
# `_drive_click_control` below.
#
# What this buys concretely: greenhouse's `multi_value_multi_select` is a group
# of native option checkboxes (see `capture._ROLE_OVERRIDES`), and the
# QUESTION's own label resolves to no checkbox at all (only each OPTION does,
# 1-to-1, by its own label -- the live probe re-served in `tests/
# test_providers_greenhouse.py::test_fill_hands_off_a_checkbox_group_before_
# building_any_locator`'s docstring); `_drive_click_control` drives each OPTION
# by its own label/name, one `.check()` per option -- the "checkbox GROUP
# option-by-option" driving this wave named as its job. A control that does not
# CONFIRM (readback mismatch, or a locator exposing no `.check()`/`.uncheck()`)
# still books its own gap: never a silent auto-click counted as filled, never a
# laundered exemption. A group is filled ONLY when EVERY option it drove
# confirmed; a partially-confirmed group is a gap, never a fill.
#
# `_HUMAN_HANDOFF_REASON` is kept defined, though no longer produced by this
# loop, only because the pre-W5.1c suite `tests/test_providers_greenhouse.py`
# still names it; nothing else in this module references it any more.
_HUMAN_HANDOFF_REASON = (
    "checkbox/radio needs a human-operated trusted click: greenhouse scores the "
    "session with reCAPTCHA v3, so a programmatic checkbox/radio click is handed "
    "off for a human (a required one forces NOT_COMPLETE, never a silent "
    "auto-click); driving a checkbox GROUP option-by-option is W5.1c's job")

# The ONLY click-hazard role greenhouse's own vocabulary ever produces
# (`kernel.contracts._ROLE_FOR_TYPE` + `capture._ROLE_OVERRIDES`): "boolean" and
# the overridden "multi_value_multi_select" both map to "checkbox". Greenhouse
# has no field type that maps to "radio" today -- a `yes_no` question is a
# react-select COMBOBOX on the live DOM (driven by `select_react_combobox`
# below), never a radio pair. Kept as a map, not a bare "always CHECKBOX", so a
# future radio-shaped field routes correctly without a new branch here.
_ROLE_TO_CONTROL_KIND = {"checkbox": ControlKind.CHECKBOX, "radio": ControlKind.RADIO}


# -- capture / apply_url: thin delegation to the registry wiring ---------------


def capture(slug: str, job_id: str, opener: Any = None) -> FieldMap:
    """The schema fetch: the public `boards-api.../questions=true` GET. Reaches
    `engine.providers.greenhouse.capture.capture_greenhouse` via a CALL-TIME
    import, so the test monkeypatch seam (patch `capture_greenhouse` on the
    greenhouse `.capture` module) still routes and importing this module stays
    light. No new logic here (the provider registry looks this function up lazily
    as `_registry.get("greenhouse").capture`)."""
    from engine.providers.greenhouse.capture import capture_greenhouse
    return capture_greenhouse(slug, job_id, opener)


def apply_url(slug: str, job_id: str) -> str:
    """The public apply-page URL: `capture.greenhouse_apply_url`, imported at CALL
    time so this module stays browser-free at import."""
    from engine.providers.greenhouse.capture import greenhouse_apply_url
    return greenhouse_apply_url(slug, job_id)


# -- value resolution: delegate to the vendor-agnostic kernel resolver ---------
# The CV/photo choice is the owner-ratified STRUCTURAL rule now living in the
# kernel (`kernel.resolve._select_cv`): a form with a dedicated photo/portrait
# upload field carries the real photo on that field, so the plain ATS CV is
# uploaded; a form with none gets the embedded-photo ATSI variant. It keys purely
# on the form's own upload-field shape (`kernel.resolve._form_has_photo_field`,
# read from the vendor schema captured at `capture()` time, never the posting's
# free text -- an attacker-independent structural signal per anti-injection
# finding 5) and is posting-language independent. Greenhouse contributes its
# portal-widget resolver (`GREENHOUSE_WIDGET_RESOLVER`) for the classification-
# time quirks, plus ONE post-process the vendor resolver structurally cannot do:
# the education typeaheads (see `_resolve_education_typeaheads`).


# The SSOT `education` is a LIST of entries; the PRIMARY (first) entry's fields
# feed the three typeaheads. School <- institution, Degree <- degree. Discipline
# has NO structured key in the education entries (v1.4 keys them as
# {degree, institution, year}; discipline lives inside the degree string), so it
# falls back to the OWNER-SEEDED `canned_answers.discipline` scalar ("Computer
# Science") -- never parsed out of the degree string (that would be guessing).
# When neither is present it stays skipped BY NAME. The degree/discipline
# VALUE-vs-option match on the live widget is a TB5-R2 concern; this maps the
# SSOT datum, and the fill readback reports honestly if it does not take.
_EDUCATION_SSOT_LIST = "education"
_EDUCATION_CONTROL_SSOT_KEY = {
    "education_school": "institution",
    "education_degree": "degree",
    "education_discipline": "discipline",
}
# The discipline datum's fallback SSOT path when the primary education entry
# carries no structured `discipline` key (the live SSOT shape).
_EDUCATION_DISCIPLINE_FALLBACK_PATH = "canned_answers.discipline"

# The LIVE react-select id root each capture-time education key drives (probed on
# canonical/4124053, 2026-07-19): the widget ids are `school--0` / `degree--0` /
# `discipline--0`, NOT the capture keys. Capture mints stable namespaced keys
# (`education_school` etc., so they never collide with a schema question literally
# named "degree"); THIS is the one place that maps a key onto the DOM id the
# react-select driver anchors its `-live-region` selector on. A key with no entry
# here falls back to itself (the pre-probe assumption).
_EDUCATION_DOM_FIELD_ID = {
    "education_school": "school--0",
    "education_degree": "degree--0",
    "education_discipline": "discipline--0",
}


def resolve_values(fieldmap: FieldMap, ssot, profile: dict, *,
                   assets: FillAssets | None = None,
                   posting_lang: str = "en") -> ResolvedValues:
    """Render every field to a concrete fill value via the vendor-agnostic kernel
    resolver, injecting Greenhouse's portal-widget resolver, then reconnect the
    async education typeaheads to the structured SSOT `education` list.

    The CV/photo choice (owner-ratified structural rule) and all other
    classification -- text/select/boolean rendering, EEO/demographic exclusion,
    missing-field skips -- live in `kernel.resolve.resolve_values`. Greenhouse's
    location-autocomplete `location` field, the paste-in `resume_text`/
    `cover_letter_text` textareas, and the `longitude`/`latitude` telemetry are
    reconnected through the injected `GREENHOUSE_WIDGET_RESOLVER`. The education
    typeaheads need a SEPARATE post-process (`_resolve_education_typeaheads`): the
    vendor resolver can only return a scalar dotted SSOT path, and `ssot.get`
    does not index a list, so it cannot reach `education[0].institution` -- the
    kernel would (and does) skip School/Degree/Discipline as
    `missing:canned_answers.<label>`. This is the vendor-widget analogue of the
    location autocomplete, one structural level deeper.
    """
    resolved = _kernel_resolve_values(
        fieldmap, ssot, profile, assets=assets, posting_lang=posting_lang,
        vendor_resolver=GREENHOUSE_WIDGET_RESOLVER)
    _resolve_education_typeaheads(fieldmap, ssot, resolved)
    return resolved


def _resolve_education_typeaheads(fieldmap: FieldMap, ssot,
                                  resolved: ResolvedValues) -> None:
    """Inject a FieldValue for each education typeahead whose datum is present in
    the structured SSOT `education` list, dropping the kernel's stale
    `missing:canned_answers.<label>` skip for it. Discipline, which the education
    entries do not carry structurally, falls back to the owner-seeded
    `canned_answers.discipline` scalar; a field whose datum is absent everywhere
    is LEFT skipped by name -- partial data fills what it has and records the
    rest, never fabricated. Idempotent: a field the kernel somehow already
    resolved is left untouched. No-op on a posting with no education typeaheads."""
    edu_fields = [f for f in fieldmap.fields
                  if f.type == EDUCATION_TYPEAHEAD_TYPE]
    if not edu_fields:
        return
    entries = ssot.get(_EDUCATION_SSOT_LIST)
    primary = (entries[0] if isinstance(entries, list) and entries
               and isinstance(entries[0], dict) else {})
    already = {fv.key for fv in resolved.fields}
    for fld in edu_fields:
        if fld.key in already:
            continue
        ssot_key = _EDUCATION_CONTROL_SSOT_KEY.get(fld.key)
        raw = primary.get(ssot_key) if ssot_key else None
        value = str(raw).strip() if raw is not None else ""
        if not value:
            value = _education_fallback_value(fld.key, ssot)
        if not value:
            continue  # datum absent -> leave the kernel's skip (recorded by name)
        resolved.skipped[:] = [(k, r) for k, r in resolved.skipped
                               if k != fld.key]
        resolved.fields.append(FieldValue(
            key=fld.key, label=fld.label, type=fld.type,
            locator=fld.locator, value=value))


def _education_fallback_value(key: str, ssot) -> str:
    """The fallback SSOT scalar for an education typeahead whose structured entry
    datum is absent. Only Discipline has one today (`canned_answers.discipline`,
    the owner-seeded degree subject); every other key returns "" so it stays
    honestly skipped by name rather than fabricated."""
    if key != "education_discipline":
        return ""
    raw = ssot.get(_EDUCATION_DISCIPLINE_FALLBACK_PATH)
    if raw is MISSING or raw is None:
        return ""
    return str(raw).strip()


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
    # native input, via `kernel.fill_toolkit._upload_attached` with NO `confirm=`
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
        if _needs_human_handoff(fv):
            # W5.1c: DRIVE the click-hazard control through the shared kernel
            # mechanism instead of handing it to a human (see the block
            # comment above `_HUMAN_HANDOFF_REASON`). A single checkbox drives
            # one control; a checkbox GROUP drives each of its selected options
            # by that option's own label. A control that does not confirm still
            # books its own gap (a group needs EVERY option to confirm); a
            # required one still forces NOT_COMPLETE. Fail-soft exactly like
            # `_fill_field` below: a locator that resolves to nothing, or a
            # `.check()` that times out, books ONE gap for ONE field rather than
            # escaping `fill()` and crashing the whole run.
            try:
                confirmed, reason = _drive_click_control(page, fv)
            except FillSafetyError:
                raise
            except Exception as exc:  # per-field drive error is fail-soft
                extra_skips.append((fv.key, f"fill-error: {exc}"))
                continue
            if confirmed:
                filled_keys.add(fv.key)
            else:
                extra_skips.append((fv.key, reason))
            continue
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
        if sibling_key is not None and not _control_rendered(page, fv):
            # The sibling upload did NOT attach (else the branch above took it)
            # AND greenhouse does not render this paste-textarea at all: on the
            # live anthropic seal posting the org is in FILE mode, so
            # `resume_text`'s own locator resolves to ZERO nodes (read-only probe
            # 2026-07-14). Driving it burns a full Playwright
            # actionability timeout hunting a control that does not exist, then
            # fail-softs anyway.
            #
            # This skip is deliberately NOT a justified one: the reason string
            # matches no predicate in `kernel.resolve._completeness`, so a
            # REQUIRED paste-textarea still books its gap and the run still forces
            # NOT_COMPLETE. The guard buys back the timeout; it never buys the
            # field an excuse. (The real finding in this state is the sibling
            # upload's own failure, which books its own gap independently.)
            extra_skips.append(
                (fv.key, f"control not rendered (vendor is in file-upload mode) "
                         f"and sibling {sibling_key} did not attach"))
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
    # and a REQUIRED upload that genuinely attached has its own widget legend
    # folded back to its bare schema label (`_reconcile_uploaded_labels`, whose
    # three-part bound is what keeps that fold from silencing a foreign gap).
    from engine.kernel.resolve import _completeness

    schema_required = {f.label for f in fieldmap.required_fields()
                       if f.key not in satisfied_by_sibling}
    dom_required = _reconcile_uploaded_labels(
        base.sweep_required(page), uploads, fieldmap, schema_required)
    mismatch = base.completeness_mismatch(schema_required, dom_required)

    filled = len(filled_keys)
    all_skips = list(values.skipped) + extra_skips
    fillable_total, required_unfilled, justified_skips = _completeness(
        fieldmap, filled_keys, all_skips, filled,
        vendor_resolver=GREENHOUSE_WIDGET_RESOLVER)
    required_unfilled = required_unfilled + _sweep_gaps(mismatch)

    return FillReport(
        vendor=vendor, company=company or fieldmap.posting_id,
        posting_id=fieldmap.posting_id,
        fillable_total=fillable_total, filled=filled,
        required_unfilled=required_unfilled, justified_skips=justified_skips,
        uploads=uploads, skipped=all_skips,
        readback_mismatches=readback_mismatches, validation_errors=[],
        url_unchanged=url_unchanged, screenshot="", ts=ts)


# The tokens Greenhouse's OWN upload widget contributes to the fieldset legend
# that the post-fill sweep reads. LIVE (read-only sweep 2026-07-14 of the same
# gitlab posting the LIVE-DOM comments further down cite) the legend of a
# REQUIRED Resume/CV mashes the control's label together with its chrome:
#   "resume/cv attach attach enter manually enter manually accepted file types:
#    pdf, doc, docx, txt, rtf"
# `<label for="resume">` reads only "Attach" and `<label for="resume_text">` only
# "Enter manually", so the schema's "Resume/CV" is never what arm 1 of the sweep
# names: the LEGEND is what arm 2 reads, whole. A remainder built ONLY from these
# tokens (plus the uploaded file's own name) is the upload widget talking about
# ITSELF. Anything else is a DIFFERENT question and MUST keep its gap.
_UPLOAD_LEGEND_TOKENS = frozenset({
    "attach", "attached", "enter", "manually", "accepted", "file", "files",
    "type", "types", "upload", "uploaded", "browse", "choose", "select",
    "drop", "drag", "here", "or", "remove", "delete", "clear",
    "pdf", "doc", "docx", "txt", "rtf", "odt", "pages",
})

# The punctuation the legend carries on its own words ("types:", "pdf,").
_LEGEND_PUNCT = ",:;.()[]"


def _reconcile_uploaded_labels(dom_required: set[str], uploads: list[dict],
                               fieldmap: FieldMap,
                               schema_required: set[str]) -> set[str]:
    """Fold an uploaded file field's OWN post-fill sweep label back to its bare
    schema label before the completeness diff runs (BUG 3).

    WHAT THE SWEEP ACTUALLY READS (read-only live probe 2026-07-14, the same
    gitlab posting the LIVE-DOM comments below cite).
    This supersedes the earlier account, which claimed Greenhouse "appends the
    chosen filename to the control's label" post-upload: the current job-boards
    React DOM does no such thing. The resume control's `<label>` reads "Attach",
    its paste sibling's reads "Enter manually", and the schema's "Resume/CV"
    survives only inside the upload fieldset's `<legend>` -- which arm 2 of
    `base.sweep_required` reads WHOLE, mashing label and widget chrome into
    `_UPLOAD_LEGEND_TOKENS`' example string above. The schema side says
    "Resume/CV". Unfolded, the two sides never compare equal, and EVERY
    required-resume posting books a phantom PAIR (a `dom_only` for the legend and
    a `schema_only` for the label) against a control that uploaded perfectly.

    THE INVARIANT: a dom label folds to `bare` ONLY when ALL THREE hold. (The
    W5B-WORKABLE wave hit this same class in KEY space and bounded its fold the
    same way; greenhouse diffs in LABEL space, which is why (c) is needed here
    and not there.)

    (a) PROVEN ATTACHMENT. `bare` is the label of a field in `uploads`, i.e. one
        whose own input readback AND Greenhouse's rendered confirmation both
        passed (`_fill_upload`). A required upload that FAILED to attach is
        absent from `uploads`, so the fold stays inert and its gap still bites.
    (b) SCHEMA-REQUIRED. `bare` is in `schema_required`. The fold exists solely to
        make the two REQUIRED sets compare equal; folding toward a label the
        schema does not require could only rewrite one gap into another.
    (c) THE REMAINDER IS THE WIDGET TALKING ABOUT ITSELF. Past the bare label, the
        dom label may carry ONLY `_UPLOAD_LEGEND_TOKENS` and/or the uploaded
        FILE's own name. This is the ANTI-GAMING bound and it is not optional: the
        earlier unbounded `startswith(bare + " ")` prefix fold SILENCED any
        foreign required control whose label merely began with the same words.
        PROVEN on the live gitlab DOM: with a required control the schema lacks
        ("Resume/CV Summary (250 words)") present on the page, uploading a CV made
        its gap VANISH (3 gaps -> 0). A fold that can erase the gap of a control it
        has nothing to do with is a silencer, whatever else it gets right.

    The fold REWRITES a label rather than adding one, so a label it declines to
    fold keeps its own gap: this function can never INVENT a `required_unfilled`
    entry, and (c) is what stops it from DESTROYING one."""
    labels_by_key = {f.key: f.label for f in fieldmap.fields}
    schema_bare = {base._normalize_name(name) for name in schema_required}
    folds: list[tuple[str, frozenset[str]]] = []
    for upload in uploads:                                    # (a)
        bare = base._normalize_name(labels_by_key.get(upload.get("key"), ""))
        if not bare or bare not in schema_bare:               # (b)
            continue
        folds.append((bare, _filename_tokens(upload.get("path"))))
    if not folds:
        return dom_required
    return {_fold_label(dom_label, folds) for dom_label in dom_required}


def _fold_label(dom_label: str, folds: list[tuple[str, frozenset[str]]]) -> str:
    """`dom_label` folded to a bare upload label when it IS that upload widget's
    own legend (bound (c) above); otherwise returned untouched, gap intact."""
    for bare, filename_tokens in folds:
        if dom_label == bare:
            return bare
        if not dom_label.startswith(bare + " "):
            continue
        remainder = _legend_tokens(dom_label[len(bare) + 1:])
        if remainder and remainder <= (_UPLOAD_LEGEND_TOKENS | filename_tokens):
            return bare
    return dom_label


def _legend_tokens(text: str) -> frozenset[str]:
    """The label's words, stripped of the punctuation the legend hangs on them
    ("types:", "pdf,"), so each compares against the vocabulary as a bare word."""
    return frozenset(word for word in (raw.strip(_LEGEND_PUNCT)
                                       for raw in text.split()) if word)


def _filename_tokens(path) -> frozenset[str]:
    """The uploaded file's own name as a widget could render it: the basename, its
    stem, and that stem's pieces. Greenhouse's post-upload confirmation shows the
    chosen filename, and a theme that DOES append it to the label (the behaviour
    the old docstring claimed) must still reconcile."""
    if not path:
        return frozenset()
    name = Path(str(path)).name.lower()
    stem = Path(name).stem
    tokens = {name, stem}
    tokens.update(re.split(r"[-_.\s]+", stem))
    return frozenset(token for token in tokens if token)


# -- per-field driving: text/email/phone via type_human, react-select via -----
# select_react_combobox, everything else via a native locator action ----------


def _control_rendered(page, fv) -> bool:
    """True unless the field's own locator resolves to ZERO nodes on the page.

    Guards the `_TEXT_UPLOAD_SIBLINGS` paste-textarea, whose control greenhouse
    does not render at all in file-upload mode (live: `resume_text` resolves to 0
    on the anthropic seal posting). BIASED TOWARD DRIVING: any page/locator that
    cannot be counted, and any exception on the way, reads as RENDERED, so this can
    only ever remove a hunt for a control that provably is not there. It must
    never become a reason to skip a control that might exist."""
    try:
        counter = getattr(base._locate(page, fv), "count", None)
        return counter() > 0 if callable(counter) else True
    except Exception:
        return True


def _is_react_combobox(fv) -> bool:
    """A Greenhouse `multi_value_single_select` / `yes_no` question renders
    as a react-select combobox in the live DOM; the fieldmap locator role
    (set at capture time, `fieldmap._ROLE_FOR_TYPE`) is the structural
    signal, not a type-string guess repeated here."""
    return fv.locator.role == "combobox"


# `EDUCATION_TYPEAHEAD_TYPE` (imported from capture, the producer that mints it)
# is the synthetic field type a Greenhouse education control (School / Degree /
# Discipline) carries when exposed as an ASYNC react-select typeahead -- a
# debounced REMOTE search, not a static-option select. It is distinct from the
# schema-API vocabulary in `contracts._ROLE_FOR_TYPE` (those education selects
# are NOT served by `questions=true`; the top-level `education` toggle drives
# them, rendered client-side -- see `capture._education_typeahead_fields`). A
# PLAIN education field -- a free-text `input_text` "which degree" question, or a
# static `multi_value_single_select` -- never carries this type, so it stays on
# its existing path (no regression).


def _is_education_typeahead(fv) -> bool:
    """True for an async education typeahead (School / Degree / Discipline),
    keyed on the `education_typeahead` type -- the structural signal, mirroring
    how `_is_react_combobox` keys on a captured structural attribute rather than
    guessing from a label. This branch must be consulted BEFORE
    `_is_react_combobox` in `_fill_field`: the widget renders as a react-select
    combobox too (`locator.role == "combobox"`), so the role check alone would
    otherwise route it to the static driver, whose type-then-immediate-Enter
    commits an empty menu before the debounced remote search has returned (the
    mis-fire the module docstring warns about)."""
    return fv.type == EDUCATION_TYPEAHEAD_TYPE


def _is_react_multiselect(page, fv) -> bool:
    """True for a `multi_value_multi_select` whose LIVE control is a react-select
    MULTI rather than a native checkbox group.

    Both shapes capture identically (role "checkbox" via `capture._ROLE_OVERRIDES`,
    a list value from `kernel.resolve._render_select`), and the schema cannot tell
    them apart, so the DOM is the authority: a remix react-select renders a
    per-field `react-select-<id>-live-region` node (the same anchor the single
    combobox driver uses), a native checkbox group does not. Probing it here keeps
    the existing option-by-option checkbox path intact for a genuine group while
    routing the live nationality control (`question_65106872[]`) to the multi
    driver. BIASED TOWARD THE CHECKBOX PATH: any page that cannot be probed reads
    as NOT a react-select, so an unprobeable page keeps its prior behaviour."""
    if fv.type != "multi_value_multi_select" or fv.locator.role != "checkbox":
        return False
    try:
        control = _combobox_control(page, fv.key)
        counter = getattr(control, "count", None)
        return counter() > 0 if callable(counter) else False
    except Exception:
        return False


def _locate_option(page, role: str, option: str):
    """The ONE checkbox, among every option checkbox of a group, whose
    accessible name is this specific OPTION's wording -- the same role+name
    convention `base._locate` uses for a whole field, applied to one option of a
    group instead of the group's own question. Mirrors `lever.fill._locate_
    option`, the proven shape for the identical DOM problem.

    The option's own label is the ONLY name that resolves for a greenhouse
    checkbox group: the QUESTION's label resolves to ZERO checkboxes, while each
    OPTION resolves 1-to-1 by its own label (live probe, re-served in
    `test_fill_hands_off_a_checkbox_group_before_building_any_locator`'s
    docstring). The option strings are never guessed here: `kernel.resolve.
    _match_option` returns the capture-time option label verbatim out of
    `Field.options` (recorded by `capture._fields_from_question` from the
    schema's own `values`), so every name this builds is a name greenhouse's own
    schema published."""
    return page.get_by_role(role, name=option, exact=True)


def _drive_one_control(key: str, kind, locator, value: bool, name: str):
    """One `drive_control` call for one physical checkbox/radio control.

    `settle=None`: `.check()`/`.uncheck()` are already actionability-aware
    (wait for visible/stable/able-to-receive-events -- see
    `control_toolkit._drive_toggle`), and there is no multi-step drive within a
    single control to need a between-step settle -- that need is DATE-picker-
    only, and greenhouse's own schema has never exposed a picker-only date
    widget (see the module docstring's DEFERRED section)."""
    return drive_control(ControlSpec(
        key=key, kind=kind, locator=locator, value=value, name=name,
        settle=None))


def _drive_click_control(page, fv) -> tuple[bool, str]:
    """Drive one checkbox/radio-shaped field -- a single control or a whole
    GROUP -- through the shared kernel mechanism (`control_toolkit.drive_
    control`) instead of the pre-W5.1c human hand-off, and fold the per-control
    outcome(s) into ONE verdict for this field's key.

    Dispatches on the resolved value's SHAPE, mirroring `lever.fill._drive_
    control_field` (the proven shape for the identical group problem, W5.1c):

      * a `bool` is ONE physical control (a single checkbox, e.g. a certify
        tick): located by the FIELD's own locator, `base._locate`, because the
        field IS the control and its own label resolves;
      * a `str` is one chosen option of a group: located by that OPTION's name;
      * a `list` is a checkbox GROUP's selected options (`kernel.resolve.
        _render_select` builds exactly this for a `multi_value_multi_select`,
        one matched capture-time option label per element): EVERY option is
        located by its OWN name and driven independently, because the group's
        own QUESTION label resolves to no checkbox at all.

    Returns `(confirmed, reason)`; `reason` is only meaningful when `confirmed`
    is False (mirrors `_fill_field`'s own `(landed, actual)` return-tuple
    convention). The field is confirmed ONLY when EVERY control it drove
    confirmed: a partially-ticked group is a GAP, never a fill, and its reason
    names each option that did not take. An option that cannot be located or
    does not confirm is never guessed past -- it books its own named reason
    through the same gate.

    A `multi_value_multi_select` whose LIVE control is a react-select MULTI (not a
    native checkbox group) is routed to `select_react_multiselect` instead: the
    two share the same capture shape (checkbox role, list value), so the DOM
    itself is probed (`_is_react_multiselect`) rather than guessed at capture
    time. Live greenhouse `question_65106872[]` (nationality) is this shape.

    `FillSafetyError` (submit-denylist name match) is never caught here: it
    propagates and aborts the fill, exactly as it does from every other drive
    path in this module."""
    if _is_react_multiselect(page, fv):
        options = fv.value if isinstance(fv.value, list) else [fv.value]
        landed = select_react_multiselect(page, fv.key, [str(o) for o in options])
        return landed, ("" if landed else
                        "react-select multi readback did not confirm every value")
    kind = _ROLE_TO_CONTROL_KIND.get(fv.locator.role)
    if kind is None:
        return False, (f"click-hazard field carries unexpected role "
                       f"{fv.locator.role!r}, no drive path for it")
    role = fv.locator.role
    value = fv.value
    if isinstance(value, bool):
        outcomes = [_drive_one_control(fv.key, kind, base._locate(page, fv),
                                       value, fv.locator.name or fv.label)]
    elif isinstance(value, str):
        outcomes = [_drive_one_control(fv.key, kind,
                                       _locate_option(page, role, value),
                                       True, value)]
    elif isinstance(value, list):
        outcomes = [_drive_one_control(fv.key, kind,
                                       _locate_option(page, role, option),
                                       True, option)
                    for option in value]
    else:
        return False, (f"control value must be bool, str or list, got "
                       f"{type(value).__name__}")

    # An EMPTY list drove nothing, so it confirmed nothing: `all(())` is True and
    # would count a group as filled without a single tick.
    if outcomes and all(outcome.confirmed for outcome in outcomes):
        return True, ""
    reasons = "; ".join(outcome.reason for outcome in outcomes if outcome.reason)
    return False, reasons or "control did not confirm (readback mismatch)"


def _fill_field(page, fv) -> tuple[bool, Any]:
    """Drive one non-upload field; returns (landed, actual-or-None).

    `actual` is only meaningful on a text/native-locator path (it is what
    `base._readback` read back); the react-select and education-typeahead
    paths report their own landed bool with no separate `actual` value to
    surface (their readback is internal to the driver)."""
    if _is_education_typeahead(fv):
        # BEFORE the react-select check: an education typeahead is a combobox
        # too, so `_is_react_combobox` would otherwise grab it for the static
        # driver. `select_education_typeahead` is a module-local name (unlike
        # `base.select_react_combobox`): the `base` re-export shim would need a
        # new entry in `base._GREENHOUSE_WIDGET_NAMES`, which is out of this
        # change's scope, so it is called directly here and patched on this
        # module in tests. The driver anchors on the LIVE react-select id
        # (`school--0` etc.), NOT the capture key (`education_school`); the two
        # differ, so `_EDUCATION_DOM_FIELD_ID` maps between them here.
        field_id = _EDUCATION_DOM_FIELD_ID.get(fv.key, fv.key)
        landed = select_education_typeahead(page, field_id, str(fv.value))
        return landed, None
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


# `multi_value_multi_select` is NOT a native `<select multiple>` on the live
# greenhouse DOM (it is a group of native checkboxes: see `capture._ROLE_OVERRIDES`),
# and since it now captures with a checkbox role it is driven OPTION-BY-OPTION by
# `_drive_click_control` in `fill()` before `_apply_native` is ever reached, so this
# `select_option` path no longer runs for it. The set is kept because the action is
# still the right one for a genuine native select (a resolved `list` value takes the
# branch above it either way); what must not come back is a locator built from a role
# the page does not carry.
_NATIVE_SELECT_TYPES = frozenset({"multi_value_multi_select"})


# per-vendor variant (differs from the kernel generic): adds file_attached_keys + an upload-confirm widget gate (spec 3.3)
def _fill_upload(page, fv, uploads: list[dict],
                 extra_skips: list[tuple[str, str]],
                 filled_keys: set[str],
                 file_attached_keys: set[str]) -> None:
    """Attach a whitelisted asset via the reused `base._safe_upload` /
    `kernel.fill_toolkit._locate_file_input` (the real `<input type=file>` locator;
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
    length>=1` signal alone -- `kernel.fill_toolkit._upload_attached(control)` with
    NO `confirm=` argument, read right after `set_input_files` -- recorded
    independently of whether the render-confirmed gate below succeeds. This
    is the ONLY signal a `<name>_text` paste-textarea sibling is satisfied
    by (see `fill()`'s `_TEXT_UPLOAD_SIBLINGS` branch): the render can lag
    (or, under a busy full-fill load, not land within even the extended
    poll window) while the file is already genuinely on the input, and the
    textarea must never be driven in that state regardless."""
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
    if _upload_attached(control):
        file_attached_keys.add(fv.key)
    confirm = lambda: base.poll_upload_confirmed(page, control, str(fv.value))
    if not _upload_attached(control, confirm=confirm):
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
    skip (`kernel.resolve._is_upload_skip`), so re-polling it would spend time
    without changing the report's verdict.

    On a genuine re-confirm: the key joins `filled_keys`/`uploads` (mirrors
    `_fill_upload`'s own success bookkeeping exactly) and its earlier
    "did not attach" skip is dropped. Its paste-text sibling (`_TEXT_
    UPLOAD_SIBLINGS`, e.g. `resume_text` for `resume`), if it was driven and
    marked unsatisfied while the sibling upload still looked unfilled, is
    re-marked satisfied-by-sibling -- the SAME skip-reason string `fill.
    _is_satisfied_by_sibling_upload` recognizes, so completeness stays
    single-sourced with the inline sibling-skip branch above."""
    from engine.kernel.fill_toolkit import _locate_file_input

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
        control = _locate_file_input(page, fv)
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


# -- DEFERRED (see module docstring): intl-tel-input phone-country widget -------
# `_TODO_PHONE_COUNTRY_WIDGET` is a documentation-only marker (no dead branch is
# left in the fill loop): the generic text/select path already handles the phone
# field today, just not with the widget-specific driving a live-DOM probe would
# earn. (The async education typeahead this block once also deferred is now BUILT
# -- see `_is_education_typeahead` / `select_education_typeahead`.)

_TODO_PHONE_COUNTRY_WIDGET = (
    "A Greenhouse phone question rendered via intl-tel-input needs its "
    "country-code dropdown driven before the number is typed (else the "
    "number lands under the wrong country code); undrivable without a "
    "live-DOM probe of the widget's markup. Seam: _fill_field's non-"
    "combobox branch, keyed on a phone norm_type once the widget's real "
    "selector is captured.")


# ============================================================================
# GREENHOUSE WIDGET CLUSTER (moved here from engine.providers.base, W5.1 Stage
# 2a). These drive Greenhouse's react-select combobox and its resume-upload
# rendered-confirmation, which are greenhouse-only DOM widgets. `providers.base`
# keeps a lazy re-export shim for every public/private name below so existing
# callers (this module's own fill() orchestration via `base.X`, and the base
# unit tests that still reach them as `base.select_react_combobox` /
# `base.poll_upload_confirmed` / `base._settle_event_loop` etc.) keep working
# unchanged; `fill()` above deliberately still calls `base.poll_upload_confirmed`
# / `base.select_react_combobox` (NOT the local names) so the test monkeypatch
# seam `monkeypatch.setattr(base, "poll_upload_confirmed", ...)` still routes.
# ============================================================================


# -- react-select combobox driver (Greenhouse; W4-deferred) --------------------
# LIVE-DOM FIX #1 (2026-07-06, gitlab/8503792002 acceptance run): the driver used
# to click `#react-select-{field_id}-input` to open the widget -- that id DOES
# NOT EXIST on Greenhouse's react-select v5 markup, so every combobox timed out
# before a single option could be picked. The ids Greenhouse's live DOM DOES
# confirm are prefixed `react-select-{field_id}-` (e.g. `-placeholder`,
# `-live-region`); the control itself is `div.select__control` (a build-hashed
# class suffix, e.g. `remix-css-13cymwt-control`, so never matched by a full
# class-string equality), containing `div.select__value-container` >
# `div.select__placeholder` and `div.select__input-container` > the control's
# own `<input>`. The driver below anchors on the `-live-region` id (via
# Playwright's `:has()` CSS extension) to reach the control.
#
# DO NOT RE-ANCHOR THIS ON `-placeholder` (read-only live probe of the anthropic
# seal posting, 2026-07-14; earlier revisions of THIS comment told you to, and
# they were wrong). Two independent reasons, either one fatal:
#   1. The placeholder node sits BELOW the control, not above it. Live ancestry:
#      `div.select__placeholder < div.select__value-container < div.select__control`.
#      So `div:has(> [id="...-placeholder"]) div.select__control` scopes to the
#      VALUE-CONTAINER and then finds no `.select__control` DESCENDANT of it: it
#      resolves to ZERO controls, on all 6 live dropdowns, even BEFORE anything is
#      picked. The `-live-region` node IS a direct child of `div.select-shell`,
#      which does contain the control, so it resolves 1-to-1 on all 6.
#   2. The placeholder also UNMOUNTS the moment a value is picked, so even a
#      correctly-shaped placeholder scope would stop matching post-commit.
# Pinned by test_greenhouse_combobox_control_selector_anchors_on_live_region.
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
#
# LIVE-DOM FIX #3 (2026-07-20, canonical/5569916 read-only probe; raw log
# `auto/trackB/gh-probe.out`, analysis `T12a-gh-probe-report.md`). FIX #2 above
# is RIGHT that react-select commits the FOCUSED row on Enter, and WRONG in the
# assumption it silently carried: that the first filtered row IS the requested
# option. On the live REQUIRED control `question_55255480` (intended value "2")
# the menu filtered 11 rows down to 2 (`:12` vs `:30`) and the filter input did
# hold the query (`:31`), so type-to-filter works. But the survivors were `1`
# AND `2` IN THAT ORDER, so `select__option--is-focused` sat on `1` (`:34`,
# `:36`, pre-Enter outerHTML `:46`), Enter committed `1` (`:50`, still `1` at
# +250 ms `:56`), and the readback correctly refused it (`:59`).
#
# WHY `1` survives a filter for `2` is NOT established (the leading hypothesis
# is react-select's default `createFilter` stringifying label plus value, so a
# short numeric query matches an unrelated row through its numeric VALUE; an
# attempt to confirm it against the boards API returned non-JSON). Nothing
# below depends on that mechanism: the driver stops reasoning about WHICH row
# react-select chose to focus and instead POSITIVELY IDENTIFIES the row whose
# visible text matches, walks the focus ring onto it, re-reads to confirm it
# got there, and only then presses Enter.
#
# The severity this closes is NOT an accounting error. The readback already
# stopped a wrong value being COUNTED as filled; it cannot stop the wrong value
# being COMMITTED. Live, `1` was left sitting in a REQUIRED question's widget
# and would have been SUBMITTED. Exact-match-or-park keeps the wrong value out
# of the form in the first place; a park is always better than a guess.
#
# The single-option controls that pass today (e.g. `question_44538607`,
# `:69-:104`) filter to ONE row which is already focused, so the walk is zero
# steps and their DOM interaction is byte-for-byte what it was before.


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
    """Drive one react-select combobox: open, filter, commit the EXACT match,
    and confirm.

    Sequence (fresh locators at every step; react-select recycles nodes):
      1. Click the field's control (scoped via `_combobox_control_selector`,
         anchored on the `-live-region` id -- NEVER `-placeholder`, which
         resolves to zero controls live; see the block comment above) to open
         the menu.
      2. `type_human` the option text into the control's own input to filter
         the menu (never fill()) -- also the long-country-list path.
      3. `_commit_and_confirm`: identify the rendered row whose VISIBLE TEXT
         exactly matches `option_text`, walk react-select's focus ring onto
         THAT row, confirm on a fresh read that it got there, and only then
         press `Enter` (still never a `div.select__option` click, which does
         not reliably commit -- LIVE-DOM FIX #2). A menu that renders rows but
         carries no exact match commits NOTHING and parks -- LIVE-DOM FIX #3.
      4. Poll `.select__single-value` (scoped to the field's control) at
         +200/+500 ms to confirm the value landed.
      5. Dismiss with Escape -- harmless (react-select already closed the menu
         on Enter-commit) but a safe no-op net for the case Enter had nothing
         to commit, and the deliberate exit for the parked case. NEVER blur (a
         blur can re-open / clear the widget).

    Returns True iff the readback confirms the selection landed.
    """
    control = _combobox_control(page, field_id)
    control.click()
    combo_input = _combobox_input(page, field_id)
    type_human(combo_input, option_text, min_delay=min_delay, max_delay=max_delay)

    landed = _commit_and_confirm(page, field_id, combo_input, option_text,
                                 poll_ms)
    # Dismiss the menu (a no-op after an Enter-commit; a genuine close after a
    # park) without a blur.
    _keyboard_press(page, combo_input, "Escape")
    return landed


def select_react_multiselect(page, field_id: str, options: list[str], *,
                             min_delay: float = 60, max_delay: float = 180,
                             poll_ms: tuple[int, ...] = (200, 500)) -> bool:
    """Drive one react-select MULTI: pick EVERY option, then confirm all landed.

    Reuses the exact single-combobox primitives (live-region-anchored control,
    `type_human` filter, focus-following keyboard Enter -- NOT a third pattern);
    the only difference is the readback, which counts `.select__multi-value`
    chips instead of the single `.select__single-value`. Per option, in order:
    open the control, type-to-filter, commit the highlighted first-filtered row
    with Enter (react-select appends it as a chip and clears the filter, so the
    next option starts clean). After all options: poll the rendered chips and
    return True ONLY when EVERY intended value is present -- a partial multi is a
    gap, never a fill (mirrors `_drive_click_control`'s all-or-nothing group
    verdict). Dismiss with Escape (never blur). Live greenhouse
    `question_65106872[]` (nationality) resolves `['Italian']` through here."""
    for option_text in options:
        control = _combobox_control(page, field_id)
        control.click()
        combo_input = _combobox_input(page, field_id)
        type_human(combo_input, option_text,
                   min_delay=min_delay, max_delay=max_delay)
        _keyboard_press(page, combo_input, "Enter")
    landed = _poll_multi_values(page, field_id, options, poll_ms)
    _keyboard_press(page, _combobox_input(page, field_id), "Escape")
    return landed


def _poll_multi_values(page, field_id: str, options: list[str],
                       poll_ms: tuple[int, ...]) -> bool:
    """Poll the rendered multi-value chips at the given cumulative offsets; True as
    soon as EVERY intended option's text appears among the shown chips. An empty
    option list confirms nothing (never a vacuous pass)."""
    wanted = [_normalize_name(o) for o in options]
    wanted = [w for w in wanted if w]
    if not wanted:
        return False
    elapsed = 0
    for mark in poll_ms:
        _wait_timeout(page, mark - elapsed)
        elapsed = mark
        shown = [_normalize_name(text) for text in _multi_value_texts(page, field_id)]
        if all(any(want in chip for chip in shown) for want in wanted):
            return True
    return False


def _multi_value_texts(page, field_id: str) -> list[str]:
    """The texts of the field's rendered `.select__multi-value__label` chips (one
    per committed option), scoped to its own `-live-region`-anchored control.
    Degrades to an empty list on any missing method / query failure -- never a
    hang or a raise -- so the readback is biased toward "not landed yet"."""
    locator_fn = getattr(page, "locator", None)
    if locator_fn is None:
        return []
    try:
        chips = _combobox_control(page, field_id).locator(
            ".select__multi-value__label")
        counter = getattr(chips, "count", None)
        count = counter() if callable(counter) else 0
        texts: list[str] = []
        for i in range(count):
            texts.append(_locator_text(chips.nth(i)))
        return texts
    except Exception:
        return []


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


# The class react-select puts on the ONE row its focus ring currently sits on;
# `Enter` commits that row and no other. Live-captured on two independent
# controls of the same posting (`auto/trackB/gh-probe.out:46` and `:91`). The
# sibling `remix-css-*-option` class carries a per-build hash and is never
# matched (same reason `_combobox_control_selector` never matches a full class
# string).
_FOCUSED_OPTION_CLASS = "select__option--is-focused"

# The most ArrowDown/ArrowUp presses `_focus_exact_option` will spend reaching
# the matching row. react-select's focus ring WRAPS, so the walk is at most half
# the rendered rows in the better direction; a filtered menu that still leaves
# more than this between the focused row and the match is not a list a keyboard
# walk should be brute-forcing, and parking is the honest outcome. It is never
# reached on any control observed so far: the passing controls filter to a
# single row (`gh-probe.out:77`) and the failing one to two (`:30`).
_MAX_OPTION_NAV_STEPS = 40

# Cumulative ms offsets for the post-walk focus re-read (same bounded-offset
# shape as `_poll_single_value`). The first read is at +0: react-select moves
# its focus ring synchronously on the keydown, so the common case costs no wait
# at all, and the later marks only pay out when a render lags.
_FOCUS_SETTLE_MS: tuple[int, ...] = (0, 60, 150)


def _commit_and_confirm(page, field_id: str, combo_input, option_text: str,
                        poll_ms: tuple[int, ...]) -> bool:
    """Commit the option whose visible text matches `option_text`, then confirm.

    Never commits a row it has not positively identified (LIVE-DOM FIX #3):
    when the menu renders rows, `Enter` is pressed ONLY after the focus ring has
    been walked onto the exactly-matching row and a fresh read confirms it is
    there. Rows with no exact match press nothing at all and return False, so
    the caller books an honest gap and the wrong value never enters the form.

    UNENUMERABLE FALLBACK: an EMPTY row list means the menu could not be read
    (an offline fake page, or a theme whose menu does not render under this
    field's `-listbox` id), which is IGNORANCE, not evidence of "no option
    matched" -- the two must never be conflated. That case falls through to the
    bare `Enter` the driver has always pressed, and `_poll_single_value` remains
    the gate, exactly as before this fix. Live that Enter has nothing to commit
    anyway (no rendered row means no focused row), and the selector it depends
    on is live-confirmed on the only markup we have (`_menu_option_selector`).
    """
    rows = _menu_options(page, field_id)
    if rows and not _focus_exact_option(page, field_id, combo_input,
                                        option_text, rows):
        return False
    # Commit via a FOCUS-FOLLOWING keyboard Enter, not the filter input's own
    # press: react-select re-renders (detaches) the filter input on each
    # keystroke, so `combo_input.press("Enter")` hangs on Playwright's
    # actionability wait for a now-stale node. The live input still holds
    # focus, so the page keyboard commits the FOCUSED option reliably.
    # Live-DOM verified. Falls back to the locator's own press for the offline
    # fake harness (no `page.keyboard`).
    _keyboard_press(page, combo_input, "Enter")
    return _poll_single_value(page, field_id, option_text, poll_ms)


def _focus_exact_option(page, field_id: str, combo_input, option_text: str,
                        rows: list[tuple[str, bool]]) -> bool:
    """Walk react-select's focus ring onto the row whose visible text exactly
    matches `option_text`; True only once a FRESH read confirms it is focused.

    False (commit nothing) when: no row matches exactly, the match is ambiguous,
    no row is focused at all (so the walk has no known origin and every step
    count would be a guess), the walk is longer than `_MAX_OPTION_NAV_STEPS`, or
    the re-read never shows the intended row focused. Every one of those is a
    state in which pressing `Enter` would commit a row this driver cannot name.
    """
    target = _exact_option_index([text for text, _ in rows], option_text)
    if target is None:
        return False
    focused = next((i for i, (_, is_focused) in enumerate(rows) if is_focused),
                   None)
    if focused is None:
        return False
    # The ring wraps at both ends, so both directions are legal; take the
    # shorter one to keep the keypress count (and the re-render churn) minimal.
    down = (target - focused) % len(rows)
    up = (focused - target) % len(rows)
    key, steps = ("ArrowDown", down) if down <= up else ("ArrowUp", up)
    if steps > _MAX_OPTION_NAV_STEPS:
        return False
    for _ in range(steps):
        _keyboard_press(page, combo_input, key)
    return _focused_text_matches(page, field_id, option_text)


def _focused_text_matches(page, field_id: str, option_text: str) -> bool:
    """Bounded re-read of the focus ring: True as soon as the row react-select
    reports as FOCUSED carries exactly the intended text.

    This is the step that makes the walk verified rather than assumed -- the
    whole defect being fixed is a step that trusted react-select's own choice of
    focused row without ever reading it back. Biased toward False: a menu that
    cannot be re-read returns no rows and therefore no match, so the caller
    parks rather than committing blind."""
    want = _normalize_name(option_text)
    if not want:
        return False
    elapsed = 0
    for mark in _FOCUS_SETTLE_MS:
        _wait_timeout(page, mark - elapsed)
        elapsed = mark
        for text, is_focused in _menu_options(page, field_id):
            if is_focused and _normalize_name(text) == want:
                return True
    return False


def _exact_option_index(texts: list[str], option_text: str) -> int | None:
    """The index of the ONE row whose visible text matches `option_text`; None
    when nothing matches or the match is AMBIGUOUS (two rows reading the same).

    MATCHING RULE: equality after `_normalize_name` -- lowercase, `*`
    required-marker stripped, internal whitespace collapsed -- and EXACT, never
    substring. Justification for each half:

    - EXACT is load-bearing. The live failure is a menu whose FILTER is loose
      (querying `2` left `1` AND `2`: `gh-probe.out:30-35`), so a loose MATCH
      layered on a loose filter is how `2` picks `12`, or `10` picks `10+` out
      of the live 11-row list at `:16-:26`. Substring matching cannot be made
      safe on a numeric option list, and the readback downstream is itself a
      substring test (`_poll_single_value`), so exactness here is what stops a
      near-match sailing through BOTH gates.
    - The `_normalize_name` normalisation is safe because it cannot merge two
      DISTINCT rows: rows differing only in case, in required-marker asterisks,
      or in how much whitespace the markup indents them with are not rows a
      human reading the menu could tell apart either. It is also the SAME
      normalisation `_poll_single_value` applies, so the commit gate and the
      readback gate agree on what a name is, and a rule stricter than the
      readback's (raw equality) would park fields the readback confirms today.
    - AMBIGUITY parks. Two rows with identical visible text carry different
      underlying values, and nothing visible says which was meant, so there is
      no honest way to pick one.
    """
    want = _normalize_name(option_text)
    if not want:
        return None
    hits = [i for i, text in enumerate(texts) if _normalize_name(text) == want]
    return hits[0] if len(hits) == 1 else None


def _menu_options(page, field_id: str) -> list[tuple[str, bool]]:
    """`(visible text, is-focused)` for every rendered row of the field's OPEN
    menu, in DOM order.

    Live-captured shape (`auto/trackB/gh-probe.out:46`, re-confirmed on a second
    control at `:91`): each row is a `div.select__option` inside
    `[id="react-select-<field_id>-listbox"]`, and the focused row carries the
    additional `_FOCUSED_OPTION_CLASS`.

    Reads text and focus in ONE pass (rather than a second query for the focused
    row) so the two can never disagree about an index, which is what makes a
    duplicate-text menu detectable instead of silently mis-navigated. Degrades
    to an empty list on any missing method / query failure -- never a hang or a
    raise; see `_commit_and_confirm` for what an empty list is taken to mean.
    Kept separate from `_menu_option_count`, which answers a different question
    (has the debounced search rendered ANYTHING yet) against pages that expose
    only `.count()`."""
    locator_fn = getattr(page, "locator", None)
    if locator_fn is None:
        return []
    try:
        rows = locator_fn(_menu_option_selector(field_id))
        counter = getattr(rows, "count", None)
        count = counter() if callable(counter) else 0
        options: list[tuple[str, bool]] = []
        for index in range(count):
            row = rows.nth(index)
            attr = getattr(row, "get_attribute", None)
            classes = (attr("class") or "") if callable(attr) else ""
            options.append((_locator_text(row),
                            _FOCUSED_OPTION_CLASS in classes))
        return options
    except Exception:
        return []


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
    its own control.

    The scope is `_combobox_control_selector`'s `-live-region` anchor, which
    resolves in BOTH the empty and the selected state (live-verified 2026-07-14:
    1 control on each of the 6 anthropic dropdowns), so the post-commit readback
    genuinely reads the committed value rather than an empty string. Should a
    control nonetheless fail to match, this degrades to a fast empty read via
    `.count()` rather than hanging on Playwright's default actionability wait for
    a selector that will never resolve."""
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


# -- async education typeahead driver (School / Degree / Discipline) ------------
# Greenhouse renders the education section (driven by the posting's top-level
# `education` toggle, NOT by the `questions=true` schema) as react-select
# selects that search a REMOTE database with a DEBOUNCED query: the option list
# is empty until the keystrokes settle and the fetch returns. So the static
# `select_react_combobox` -- which types then presses Enter immediately -- would
# commit an empty menu and the value would never land (the live 2026-07-18
# canonical run left School/Degree/Discipline BLANK though the SSOT carried the
# data). This driver is that driver's async-aware sibling: it reuses the SAME
# proven primitives (live-region-anchored control, focus-following keyboard
# Enter, `-live-region` `.select__single-value` readback -- NOT a third
# react-select pattern) and adds exactly one step -- a BOUNDED wait for the
# debounced option list to render before it commits.
#
# The debounce timing itself is UNPROVABLE offline (a fixture cannot reproduce a
# real remote search's latency); this seam's SHAPE is what the offline suite
# pins, and its live behaviour is a TB5-R2 toto-gate confirmation. The menu /
# option selectors below are best-effort and UNVERIFIED against every Greenhouse
# theme -- the same caveat `_single_value_text` / `poll_upload_confirmed` carry.

# The bounded settle schedule (cumulative ms, mirroring `_poll_single_value`'s
# offsets): a debounced remote search typically returns within a few hundred ms
# of the last keystroke, so poll early and often, capped at ~1.8s total -- long
# enough for a slow round-trip, bounded so a query that matches NOTHING can
# never hang the fill (it falls through to the readback, which reports the empty
# result honestly).
_EDUCATION_SETTLE_MS: tuple[int, ...] = (200, 500, 1000, 1800)


def select_education_typeahead(page, field_id: str, option_text: str, *,
                               min_delay: float = 60, max_delay: float = 180,
                               settle_ms: tuple[int, ...] = _EDUCATION_SETTLE_MS,
                               poll_ms: tuple[int, ...] = (200, 500)) -> bool:
    """Drive one Greenhouse async education typeahead: open, filter, WAIT for the
    debounced remote options, commit, and confirm.

    Identical to `select_react_combobox` except for step 3 (the bounded settle):

      1. Click the field's control (live-region-anchored `_combobox_control_
         selector`, NEVER `-placeholder`; see that function's block comment) to
         open the menu.
      2. `type_human` the query into the control's own input (never fill()) to
         drive the debounced remote search.
      3. `_await_typeahead_options`: BOUNDED wait for that search to render at
         least one option, so the commit lands on a real (highlighted) row
         rather than an empty/stale menu. Returns as soon as options appear;
         falls through when the bound is exhausted, so a query matching nothing
         still proceeds to an honest readback rather than hanging.
      4. `_commit_and_confirm`, the SAME commit path as the static driver
         (LIVE-DOM FIX #3): walk react-select's focus ring onto the returned row
         whose visible text EXACTLY matches the SSOT value, confirm it got
         there, and only then press a FOCUS-FOLLOWING keyboard Enter. A result
         list carrying no exact match commits NOTHING.
      5. `_poll_single_value` readback via the persistent `-live-region`-anchored
         `.select__single-value`: True ONLY when the committed value CONTAINS the
         intended SSOT text (normalized). A value that did not take -- an empty
         readback, OR a committed option that does not correspond to the SSOT
         value -- returns False, so `fill()` records a readback mismatch /
         honest gap and NEVER counts it filled. Never guesses an option.
      6. Dismiss with Escape (never blur).

    SEAL-GREENHOUSE NIT-2 (`education_degree` readback-mismatched on BOTH
    censuses and was left blank) is fixed HERE, by step 4, and by the same named
    root cause as P1-2 rather than a second theory: the settle in step 3 only
    ever guaranteed that SOME row had rendered, and Enter then committed
    whichever row the remote search happened to return FIRST. When that row was
    not the SSOT degree, a wrong degree was committed into the widget and the
    readback rejected it after the fact. Now nothing is committed unless it is
    the row asked for. Whether Greenhouse's remote degree list contains an exact
    counterpart for the SSOT degree string is UNPROVEN offline (no degree option
    list has ever been captured) and stays a live-gate question; if it does not,
    this parks by name instead of committing a near-miss, and the degree datum
    still reaches the form through the free-text degree-result answer.

    Partial data across the three education fields is handled by the per-field
    fill loop, not here: each of School / Degree / Discipline is a separate
    FieldValue driven independently, so a school present in the SSOT fills while
    a discipline the SSOT lacks is skipped-by-name upstream (the resolver) or
    booked as a readback gap here -- what one field achieves never speaks for
    another.

    Returns True iff the readback confirms the intended value landed.
    """
    control = _combobox_control(page, field_id)
    control.click()
    combo_input = _combobox_input(page, field_id)
    type_human(combo_input, option_text, min_delay=min_delay, max_delay=max_delay)
    _await_typeahead_options(page, field_id, settle_ms)
    landed = _commit_and_confirm(page, field_id, combo_input, option_text,
                                 poll_ms)
    _keyboard_press(page, combo_input, "Escape")
    return landed


def _await_typeahead_options(page, field_id: str,
                             settle_ms: tuple[int, ...]) -> bool:
    """BOUNDED wait for a debounced typeahead's remote search to render at least
    one option into the field's own menu.

    Polls `_menu_option_count` at the given cumulative offsets (the same
    bounded-offset shape as `_poll_single_value`): returns True the moment an
    option is present, and False once the WHOLE bound is exhausted without one --
    never an unbounded poll, so a query that matches nothing proceeds to the
    readback (which reports the empty result honestly) rather than hanging.
    Never raises: any DOM-query failure reads as "no options yet" (the
    not-committed-yet bias, mirroring the readback's never-attached philosophy),
    so the wait can only ever end early on a POSITIVE render, never manufacture
    one."""
    elapsed = 0
    for mark in settle_ms:
        _wait_timeout(page, mark - elapsed)
        elapsed = mark
        if _menu_option_count(page, field_id) > 0:
            return True
    return False


def _menu_option_selector(field_id: str) -> str:
    """CSS for the field's OPEN react-select menu options, scoped by the
    per-field `-listbox` id so it counts only THIS field's options, never a
    sibling education select's.

    LIVE-CONFIRMED 2026-07-20 (this selector was written best-effort and is no
    longer unverified): the captured pre-Enter menu markup on canonical/5569916
    is `<div class="select__menu-list ..." role="listbox" id="react-select-
    question_55255480-listbox"><div class="select__option ...">1</div>...`
    (`auto/trackB/gh-probe.out:46`), re-confirmed on a second control of the
    same posting at `:91`. Both the id shape and the option class match what
    this builds. Still unverified against every Greenhouse THEME -- the same
    caveat `_single_value_text` / `poll_upload_confirmed` carry -- which is why
    `_commit_and_confirm` treats an unreadable menu as ignorance rather than as
    evidence that nothing matched."""
    return f'[id="react-select-{field_id}-listbox"] div.select__option'


def _menu_option_count(page, field_id: str) -> int:
    """The number of rendered option rows in the field's react-select menu (0
    while the debounced search is still in flight). Degrades to 0 on any missing
    method / query failure -- never a hang or a raise -- so the settle wait is
    biased toward "not ready yet"."""
    locator_fn = getattr(page, "locator", None)
    if locator_fn is None:
        return 0
    try:
        options = locator_fn(_menu_option_selector(field_id))
        counter = getattr(options, "count", None)
        return counter() if callable(counter) else 0
    except Exception:
        return 0


# -- upload rendered-confirmation poll (Greenhouse resume-upload false ---------
# positive fix, 2026-07-06 gitlab/8503792002 acceptance run, HOSTILE REVIEW #1)
#
# `kernel.fill_toolkit._upload_attached`'s `el.files.length >= 1` check is NECESSARY
# but NOT SUFFICIENT for Greenhouse: a live probe showed the engine's own
# upload path (an ElementHandle captured once via `query_selector_all`,
# fixed in `kernel.fill_toolkit._locate_file_input`/`_file_input_control`) left the
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
