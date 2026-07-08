"""Form-fill DRY RUN for the ToS-safe automatable vendors (W4 4b).

Owner /loop directive (2026-07-03), verbatim intent: this is a DRY RUN in which
the live ATS application pages "receive information on the owner's behalf, STOP
SHORT OF APPLYING". Three real pages (Greenhouse, Lever, Ashby) must actually
have their form fields populated so the owner SEES the filled forms, but no
submission ever happens in this wave.

The module is built so that submitting is not merely "not called" but STRUCTURALLY
absent: there is no code path that clicks a submit control, and several
independent safety invariants (below) fail LOUDLY (`FillSafetyError`) rather than
risk an accidental application.

SAFETY INVARIANTS (load-bearing; enforced in code AND tests):
- No submit code path exists. Every click goes through the single `_safe_click`
  gateway, which refuses any element whose accessible name matches
  `_CLICK_DENYLIST` (submit / apply / send / finish / continue). The happy-path
  fill flow deliberately uses native `fill` / `check` / `select_option` so it
  needs NO clicks at all; `_safe_click` is the sole sanctioned click primitive
  for any future need (e.g. opening a combobox).
- The page URL must be unchanged after the fill (fragment changes allowed). A
  navigation is treated as a possible submission/redirect and raises
  `FillSafetyError`.
- File uploads are WHITELISTED, never arbitrary (owner override of the W5
  deferral, 2026-07-03): the dry run now attaches the CV / profile photo /
  cover-letter document on file fields, but ONLY the paths carried by
  `FillAssets` (the documents/ CVs, the profile-pics photo, and the optional
  cover-letter document) may be uploaded. `_safe_upload` refuses any other path
  (`FillSafetyError`) and never clicks to open a chooser unless the trigger name
  clears both the submit denylist AND the attach/upload/browse allowlist. The
  owner accepts transmit-on-select for field-level uploads; submission stays
  forbidden.
- EEO / demographic / compliance fields are never touched: they classify as
  manual-only in `fieldmap.coverage` and so never enter the fill set.

W5 DEFERRAL NOTES: the gated real submitter and any click that advances or
submits a form stay deferred to W5's explicitly owner-gated submitter (field
uploads are enabled in this wave per the owner override above). This module is
operator-CLI only and is NEVER wired to the daily timer.

`resolve_values` is deterministic (no LLM): it reuses the `fieldmap` coverage
classifier's resolved dotted paths, resolves each answerable path against the
read-only SSOT, and renders a concrete fill value per field type. `fill_form`
navigates, fills via role/label locators, blurs to harvest validation errors,
reads every control back to diff against intent, screenshots the filled page,
and returns a `FillReport`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from engine.fieldmap import (
    GREENHOUSE_WIDGET_RESOLVER,
    FieldMap,
    capture_greenhouse,
)
from engine.kernel.contracts import (  # noqa: F401
    FieldValue,
    FillAssets,
    FillReport,
    FillSafetyError,
    ResolvedValues,
)
from engine.kernel.fill_toolkit import (  # noqa: F401
    _accept_has_doc,
    _accept_has_image,
    _CLICK_DENYLIST,
    _DOC_ACCEPT_TOKENS,
    _field_key_tokens,
    _file_input_control,
    _file_inputs,
    _IMAGE_ACCEPT_TOKENS,
    _input_accept,
    _input_idname,
    _is_upload_field,
    _KEY_STOPWORDS,
    _locate,
    _locate_file_input,
    _norm,
    _readback,
    _safe_click,
    _safe_get_attr,
    _safe_upload,
    _UPLOAD_BUTTON_RE,
    _upload_attached,
)
from engine.providers import _registry
from engine.ssot import SSOT

# The resolve engine (coverage classification + fill-value render) moved to
# engine.kernel.resolve (W5.1 kernel extraction). These 50 closure symbols are
# re-exported unchanged so every pre-Stage-2 importer keeps resolving them;
# `resolve_values` and `_completeness` (below) are thin shims that inject the
# Greenhouse widget resolver as the default, preserving today's behaviour for
# every caller that does not yet pass an explicit vendor_resolver.
from engine.kernel.resolve import (  # noqa: F401
    _CONSENT_RE,
    _CONSENT_SOURCE_PATHS,
    _COVERED_REGION_RE,
    _COVER_LETTER_RE,
    _COVER_LETTER_SKIP_REASON,
    _FULL_NAME_PATHS,
    _MARKETING_RE,
    _PHOTO_LABEL_RE,
    _SELECT_TYPES,
    _SPONSOR_INTENT_RE,
    _TALENT_POOL_RE,
    _UNCOVERED_REGION_RE,
    _WORK_AUTH_INTENT_RE,
    _YESNO_NEG_RE,
    _YESNO_POS_RE,
    _bool_field,
    _classify_checkbox,
    _consent_ratified,
    _empty_value_skip,
    _extract_yesno_option,
    _form_has_photo_field,
    _has_eu_work_rights,
    _is_cover_letter_field,
    _is_eeo_reason,
    _is_hidden_field,
    _is_justified_eeo_skip,
    _is_photo_field,
    _is_satisfied_by_sibling_upload,
    _is_upload_skip,
    _match_option,
    _name_part_kind,
    _pick_option,
    _questionnaire_skip,
    _region_ambiguous,
    _render_dict_value,
    _render_select,
    _render_text,
    _render_value,
    _resolve_boolean,
    _resolve_upload,
    _resolve_yes_no_select,
    _select_cv,
    _select_intent,
    _short,
    _sponsorship_needed,
    _split_full_name,
    _work_auth_text,
    _yesno,
)


# Preference order when several Me.<ext> portraits exist under a Profile Pics dir.
_PHOTO_EXT_ORDER = (".png", ".jpeg", ".jpg")


# -- deterministic value resolution (kernel shim) ------------------------------

def resolve_values(fieldmap: FieldMap, ssot: SSOT, profile: dict, *,
                   assets: FillAssets | None = None,
                   posting_lang: str = "en",
                   vendor_resolver=None) -> ResolvedValues:
    """Classify + render every field into concrete fill values (kernel shim).

    Transitional shim over `engine.kernel.resolve.resolve_values`: injects the
    Greenhouse widget resolver as the default so every pre-Stage-2 caller
    (`engine.fill.main`, `engine.providers.*`, tests calling this directly)
    keeps today's Greenhouse-widget classification (paste-in resume/cover-letter
    textareas, location autocomplete). Stage 2/3 moves callers onto the kernel +
    registry injection and drops this shim.
    """
    from engine.kernel.resolve import resolve_values as _kernel_resolve_values
    return _kernel_resolve_values(
        fieldmap, ssot, profile, assets=assets, posting_lang=posting_lang,
        vendor_resolver=(vendor_resolver if vendor_resolver is not None
                         else GREENHOUSE_WIDGET_RESOLVER))


# -- the fill itself -----------------------------------------------------------

def fill_form(vendor: str, slug: str, job_id: str, values: ResolvedValues,
              browser_factory=None, artifacts_dir=None, *,
              fieldmap: FieldMap | None = None,
              assets: FillAssets | None = None,
              now: Callable[[], str] | None = None) -> FillReport:
    """Fill one live application page with `values`, STOPPING SHORT OF APPLYING.

    Navigates to the vendor apply page, fills each resolved value via a role/label
    locator (reusing the fieldmap locator hint, falling back to label text),
    uploads any whitelisted asset via `_safe_upload` (no submit ever), blurs after
    each fill to harvest validation errors (aria-invalid + `.error` text), reads
    every filled control back to diff against intent, screenshots the filled page,
    and asserts the page URL is unchanged. A field (or upload) counts toward
    `filled` ONLY when its readback confirms the value actually landed -- a
    value the page silently rejected, or an upload a custom widget swallowed
    without wiring the native input, is excluded from `filled` and, if the
    field is required, becomes a `required_unfilled` gap (never a silent
    false-COMPLETE). When `fieldmap` is supplied the report carries the
    completeness denominator (criterion 1); without it the report degrades to
    the fields it saw. A navigation or a submit-like click raises
    FillSafetyError.
    """
    ts = (now or _utc_now_iso)()
    url = _apply_url(vendor, slug, job_id)
    factory = browser_factory or _default_browser_page

    readback_mismatches: list[dict] = []
    validation_errors: list[dict] = []
    extra_skips: list[tuple[str, str]] = []
    uploads: list[dict] = []
    filled_keys: set[str] = set()

    with factory() as page:
        page.goto(url, wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
        pre_url = _current_url(page)

        for fv in values.fields:
            if _is_upload(fv):
                _fill_upload(page, fv, assets, uploads, extra_skips, filled_keys)
                continue
            if fv.type == "input_file":
                # Defence in depth: a file field with no whitelisted asset is
                # never driven as text (no set_input_files, no fill).
                extra_skips.append(
                    (fv.key, "file-upload without a whitelisted asset"))
                continue
            try:
                locator = _locate(page, fv)
                _apply(locator, fv)
                locator.blur()
            except FillSafetyError:
                raise
            except Exception as exc:  # per-field fill error is fail-soft
                extra_skips.append((fv.key, f"fill-error: {exc}"))
                continue
            _harvest_field_validation(locator, fv, validation_errors)
            actual, ok = _readback(locator, fv.value)
            if ok:
                # Only a readback-CONFIRMED value counts as filled: a value the
                # page silently rejected (or a custom control that swallowed it)
                # must never read as done.
                filled_keys.add(fv.key)
            else:
                readback_mismatches.append(
                    {"key": fv.key, "intended": fv.value, "actual": actual})
                extra_skips.append(
                    (fv.key, "value did not take (readback mismatch)"))

        _harvest_page_errors(page, validation_errors)
        screenshot = _screenshot(page, vendor, job_id, ts, artifacts_dir)
        post_url = _current_url(page)

    url_unchanged = _strip_fragment(pre_url) == _strip_fragment(post_url)
    if not url_unchanged:
        raise FillSafetyError(
            f"page navigated during fill ({pre_url!r} -> {post_url!r}); a "
            "navigation may indicate a submission or redirect")

    filled = len(filled_keys)
    all_skips = list(values.skipped) + extra_skips
    fillable_total, required_unfilled, justified_skips = _completeness(
        fieldmap, filled_keys, all_skips, filled)

    return FillReport(
        vendor=vendor, company=slug, posting_id=str(job_id),
        fillable_total=fillable_total, filled=filled,
        required_unfilled=required_unfilled, justified_skips=justified_skips,
        uploads=uploads, skipped=all_skips,
        readback_mismatches=readback_mismatches,
        validation_errors=validation_errors,
        url_unchanged=url_unchanged, screenshot=str(screenshot), ts=ts)


def _is_upload(fv: FieldValue) -> bool:
    """An upload field carries its chosen asset as a Path value."""
    return isinstance(fv.value, Path)


def _fill_upload(page, fv: FieldValue, assets: FillAssets | None,
                 uploads: list[dict], extra_skips: list[tuple[str, str]],
                 filled_keys: set[str]) -> None:
    """Upload one whitelisted asset to the real page file input; a FillSafetyError
    still aborts the whole run, a per-field failure is fail-soft. A successful
    upload counts toward filled ONLY once `_upload_attached` confirms via
    readback that a file actually landed on the input -- a silently swallowed
    attach (e.g. a custom widget that never wires the native input) is excluded
    from filled and, if required, becomes a required gap.

    The fieldmap locator (best-effort role=button from the questions API) does
    NOT reach the actual <input type=file> on Greenhouse/Lever, so the input is
    located directly (`_locate_file_input`) and driven via `set_input_files` with
    no click. A required upload with no matching input stays required_unfilled."""
    if assets is None:
        extra_skips.append((fv.key, "upload skipped: no FillAssets provided"))
        return
    control = _locate_file_input(page, fv)
    if control is None:
        extra_skips.append((fv.key, "no file input located"))
        return
    try:
        _safe_upload(control, fv.value, assets, page=page,
                     button_name=fv.locator.name or fv.label)
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


def _completeness(fieldmap: FieldMap | None, filled_keys: set[str],
                  all_skips: list[tuple[str, str]], filled: int,
                  vendor_resolver=None):
    """Compute (fillable_total, required_unfilled, justified_skips) (kernel shim).

    Transitional shim over `engine.kernel.resolve._completeness`: injects the
    Greenhouse widget resolver as the default so hidden portal-telemetry fields
    (longitude/latitude) are excluded from the denominator for every pre-Stage-2
    caller (`fill_form`, `engine.providers.*`), preserving today's behaviour.
    """
    from engine.kernel.resolve import _completeness as _kernel_completeness
    return _kernel_completeness(
        fieldmap, filled_keys, all_skips, filled,
        vendor_resolver=(vendor_resolver if vendor_resolver is not None
                         else GREENHOUSE_WIDGET_RESOLVER))


def _apply(locator, fv: FieldValue) -> None:
    """Write one value using a native form action (no click, never a submit)."""
    value = fv.value
    if isinstance(value, bool):
        locator.check()          # ticks a checkbox; cannot submit a form
    elif isinstance(value, list):
        locator.select_option(label=value)
    elif fv.type in _SELECT_TYPES:
        locator.select_option(label=value)
    else:
        locator.fill(value)


def _harvest_field_validation(locator, fv: FieldValue, out: list[dict]) -> None:
    aria = _safe_get_attr(locator, "aria-invalid")
    if aria is not None and str(aria).strip().lower() == "true":
        out.append({"key": fv.key, "message": "aria-invalid"})


def _harvest_page_errors(page, out: list[dict]) -> None:
    """Collect any `.error` text rendered on the page after blur."""
    locator_fn = getattr(page, "locator", None)
    if locator_fn is None:
        return
    try:
        texts = locator_fn(".error").all_inner_texts()
    except Exception:
        return
    for text in texts or []:
        if text and str(text).strip():
            out.append({"message": str(text).strip()})


def _screenshot(page, vendor: str, job_id: str, ts: str, artifacts_dir) -> Path:
    base = Path(artifacts_dir) if artifacts_dir else Path.cwd() / "artifacts"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"fill-{vendor}-{job_id}-{_safe_stamp(ts)}.png"
    page.screenshot(path=str(path))
    return path


# -- playwright lifecycle (delegated to browse.py; lazy import) ----------------

def _default_browser_page():
    """The real headless-chromium page factory (imported lazily via browse.py).

    Kept as a thin indirection so this module imports cleanly without playwright
    and only reaches for it when a real fill is actually invoked with no fake
    factory. Tests always pass a fake factory and never touch this.
    """
    from engine.browse import _default_browser_page as real_factory
    return real_factory()


_GOTO_TIMEOUT_MS = 20_000


# -- URL helpers ---------------------------------------------------------------

def greenhouse_apply_url(slug: str, job_id: str) -> str:
    """The public Greenhouse apply page (the `job-boards.greenhouse.io/{slug}/
    jobs/{job_id}` host is the newer variant of the same page)."""
    return f"https://boards.greenhouse.io/{slug}/jobs/{job_id}"


def _apply_url(vendor: str, slug: str, job_id: str) -> str:
    spec = _registry.PROVIDERS.get(vendor)
    if spec is None or not spec.supported or spec.apply_url is None:
        raise ValueError(
            f"unknown vendor {vendor!r} (expected greenhouse/lever/ashby/workable)")
    return spec.apply_url(slug, job_id)


def _current_url(page) -> str:
    return getattr(page, "url", "") or ""


def _strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


# -- small helpers -------------------------------------------------------------

def _safe_stamp(ts: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", ts) or "run"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- toto assets + evidence publishing -----------------------------------------

def default_assets(*, documents_dir=None, archive_root=None,
                   cv_ats=None, cv_atsi=None, photo=None) -> FillAssets:
    """Resolve the toto upload assets, existence-checked (fail-soft, criterion 5).

    Defaults follow the toto layout: documents/cv-ats.pdf, documents/cv-atsi.pdf,
    and the first Me.<png|jpeg|jpg> under a 'Profile Pic*' directory of the
    career archive. Any explicit path overrides its default; an absent asset
    collapses to None (a later "asset missing" skip) rather than crashing.
    """
    docs = (Path(documents_dir).expanduser() if documents_dir
            else Path.home() / "automations" / "documents")
    ats = Path(cv_ats).expanduser() if cv_ats else docs / "cv-ats.pdf"
    atsi = Path(cv_atsi).expanduser() if cv_atsi else docs / "cv-atsi.pdf"
    return FillAssets(cv_ats=ats, cv_atsi=atsi,
                      photo=_resolve_photo(photo, archive_root)).verified()


def _resolve_photo(override=None, archive_root=None) -> Path | None:
    """Find the profile portrait: an explicit override, else the first existing
    Me.<ext> (png > jpeg > jpg) under a case-insensitive 'Profile Pic*' dir of
    the career archive (the exact dir name may drift, so the search globs it)."""
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.exists() else None
    root = (Path(archive_root).expanduser() if archive_root
            else Path.home() / "automations" / "career-archive")
    if not root.exists():
        return None
    matches = [p for p in root.rglob("*")
               if p.is_file()
               and p.stem.lower() == "me"
               and p.suffix.lower() in _PHOTO_EXT_ORDER
               and p.parent.name.lower().startswith("profile pic")]
    if not matches:
        return None
    matches.sort(key=lambda p: _PHOTO_EXT_ORDER.index(p.suffix.lower()))
    return matches[0]


def publish_evidence(report: FillReport, topic: str, transport) -> None:
    """Publish the fill screenshot to `topic`, captioned by FillReport.caption()
    (criterion 4). The message is EXACTLY the caption (no hand-written captions),
    so the completeness verdict rides the notification the owner reads. Operator
    only; the transport is injected (FakeTransport in tests, NtfyTransport live).
    """
    transport.publish_file(topic, report.screenshot, report.caption(),
                           Path(report.screenshot).name)


# -- operator CLI --------------------------------------------------------------

def _load_fieldmap(store, vendor: str, slug: str, job_id: str, *,
                   updated_at: str | None, capture_first: bool) -> FieldMap:
    """Load a cached field map, or (only with --capture-first) capture fresh.

    The store key is (vendor, posting_id, updated_at); `updated_at` defaults to
    "" (the same key put_fieldmap writes when a posting carries no board
    timestamp). On a miss the operator must opt into a live capture.
    """
    cached = store.get_fieldmap(vendor, job_id, updated_at or "")
    if cached is not None:
        return FieldMap.from_dict(cached["body"])
    if not capture_first:
        raise SystemExit(
            f"no cached field map for {vendor} {slug}/{job_id}; pass "
            "--capture-first to capture it live (browser/HTTP egress)")
    fieldmap = _capture(vendor, slug, job_id)
    store.put_fieldmap(vendor, job_id, updated_at or "",
                       fieldmap.to_dict(), fieldmap.captured_at)
    return fieldmap


def _capture(vendor: str, slug: str, job_id: str) -> FieldMap:
    if vendor == "greenhouse":
        return capture_greenhouse(slug, job_id)
    # Lever/Ashby need the browser; imported here so the module and the
    # --fieldmap-from-store path stay usable without playwright installed.
    from engine.browse import capture_ashby, capture_lever
    if vendor == "lever":
        return capture_lever(slug, job_id)
    if vendor == "ashby":
        return capture_ashby(slug, job_id)
    raise SystemExit(f"unknown vendor {vendor!r} (expected greenhouse/lever/ashby)")


def _parse_args(argv):
    import argparse

    parser = argparse.ArgumentParser(
        prog="engine.fill",
        description="Form-fill DRY RUN (operator only; STOPS SHORT OF APPLYING)")
    parser.add_argument("--vendor", required=True,
                        choices=["greenhouse", "lever", "ashby"])
    parser.add_argument("--slug", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--ssot", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--artifacts")
    parser.add_argument("--updated-at", default=None,
                        help="the board updated_at that keys the cached field "
                             "map (defaults to the empty key)")
    parser.add_argument("--posting-lang", default="en",
                        help="posting language hint (it selects the photo CV as "
                             "an informality proxy when no photo field exists)")
    parser.add_argument("--cv-ats",
                        help="override the default documents/cv-ats.pdf")
    parser.add_argument("--cv-atsi",
                        help="override the default documents/cv-atsi.pdf")
    parser.add_argument("--photo",
                        help="override the resolved career-archive portrait")
    parser.add_argument("--publish-topic",
                        help="ntfy topic to publish the screenshot + caption to")
    parser.add_argument("--ntfy-credentials",
                        help="path to the ntfy credentials file (defaults to "
                             "~/automations/ntfy/credentials)")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--fieldmap-from-store", action="store_true",
                        help="use only a cached field map (error on a miss)")
    source.add_argument("--capture-first", action="store_true",
                        help="capture the field map live if the cache misses")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    from engine.profile_map import profile_from_real_ssot
    from engine.store import Store

    ssot = SSOT.load(args.ssot)
    profile = profile_from_real_ssot(ssot)
    assets = default_assets(cv_ats=args.cv_ats, cv_atsi=args.cv_atsi,
                            photo=args.photo)
    store = Store(args.store)
    try:
        fieldmap = _load_fieldmap(
            store, args.vendor, args.slug, args.job_id,
            updated_at=args.updated_at, capture_first=args.capture_first)
        values = resolve_values(fieldmap, ssot, profile, assets=assets,
                                posting_lang=args.posting_lang)
        report = fill_form(args.vendor, args.slug, args.job_id, values,
                           fieldmap=fieldmap, assets=assets,
                           artifacts_dir=args.artifacts)
    finally:
        store.close()
    if args.publish_topic:
        _publish_cli(report, args.publish_topic, args.ntfy_credentials)
    print(json.dumps(report.to_dict(), indent=2))
    return 0


def _publish_cli(report: FillReport, topic: str, credentials_path) -> None:
    from engine.notify import NtfyTransport, load_credentials

    path = credentials_path or str(
        Path.home() / "automations" / "ntfy" / "credentials")
    publish_evidence(report, topic, NtfyTransport(load_credentials(path)))


if __name__ == "__main__":
    import sys

    sys.exit(main())
