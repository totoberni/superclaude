"""TEST-ONLY harness plumbing for the kept generic-kernel fill integration tests.

This module is NEVER imported by engine code. It is the relocated home of the
`fill_form` orchestration (and its private helpers) that used to live in the
now-deleted `engine.fill`. `fill_form` had zero production callers (the live
per-vendor `fill()` drivers, e.g. `engine.providers.greenhouse.fill`, carry the
real fill path); its remaining value was as the harness driving ~28 generic
kernel-primitive integration tests in `tests/test_fill.py`. Relocating it here
preserves that coverage as kernel-primitive integration tests instead of
rewriting or blind-deleting them.

NOT relocated (DELETED, not moved): `engine.fill` also carried an operator-only
evidence CLI cluster (`default_assets`, `_resolve_photo`, `publish_evidence`,
`main`) with no live importer anywhere in the repo, so it was retired rather
than rehomed. `publish_evidence`/`main` drove the screenshot evidence path that
died with the harness screenshot machinery (Deviation C below; the production
per-vendor `fill()`, e.g. `engine.providers.greenhouse.fill`, hardcodes
`screenshot=""`, so there is nothing to publish). `default_assets`/`_resolve_photo`
resolved toto-layout upload assets off the filesystem; that role is superseded
by callers passing an explicit `FillAssets` into the per-vendor
`fill(page, fieldmap, values, *, assets=...)` entrypoint. No in-repo successor
is required: a future operator driver supplies `assets` directly.

DELIBERATE (owner-ratified 2026-07-10): bodies are faithful to the deleted
`engine.fill` except three declared deviations:
  A. `fill_form(..., vendor_resolver=None)`: the greenhouse-default completeness
     shim died with `engine.fill`, so the completeness denominator now takes an
     explicit `vendor_resolver` (passed straight to
     `engine.kernel.resolve._completeness`); the one greenhouse-flavored
     completeness test injects `GREENHOUSE_WIDGET_RESOLVER` itself.
  B. the apply-page URL is built via the relocated registry utility
     `engine.providers._registry.apply_url` (was `engine.fill._apply_url`).
  C. the harness no longer harvests client-side validation errors nor screenshots
     the page. The production per-vendor `fill()` reports do neither (e.g.
     `engine.providers.greenhouse.fill` hardcodes `validation_errors=[]` and
     `screenshot=""`), so the harvest/screenshot plumbing was dead here (no kept
     test asserted blur, screenshot recording, or `validation_errors` content)
     and was removed; the harness report now mirrors the production shape.
The real headless-chromium factory (`_default_browser_page`) is NOT moved: it
already lives at `engine.kernel.capture_toolkit._default_browser_page` and the
tests always pass a fake factory, so it is only referenced lazily as the default.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from engine.kernel.contracts import (
    FieldMap,
    FieldValue,
    FillAssets,
    FillReport,
    FillSafetyError,
    ResolvedValues,
)
from engine.kernel.fill_toolkit import (
    _locate,
    _locate_file_input,
    _readback,
    _safe_upload,
    _upload_attached,
)
from engine.kernel.resolve import _SELECT_TYPES
from engine.providers import _registry


# -- the fill itself -----------------------------------------------------------

def fill_form(vendor: str, slug: str, job_id: str, values: ResolvedValues,
              browser_factory=None, *,
              fieldmap: FieldMap | None = None,
              assets: FillAssets | None = None,
              vendor_resolver=None,
              now: Callable[[], str] | None = None) -> FillReport:
    """Fill one live application page with `values`, STOPPING SHORT OF APPLYING.

    Navigates to the vendor apply page, fills each resolved value via a role/label
    locator (reusing the fieldmap locator hint, falling back to label text),
    uploads any whitelisted asset via `_safe_upload` (no submit ever), reads
    every filled control back to diff against intent, and asserts the page URL is
    unchanged. A field (or upload) counts toward
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
    url = _registry.apply_url(vendor, slug, job_id)
    if browser_factory is not None:
        factory = browser_factory
    else:
        from engine.kernel.capture_toolkit import _default_browser_page
        factory = _default_browser_page

    readback_mismatches: list[dict] = []
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
            except FillSafetyError:
                raise
            except Exception as exc:  # per-field fill error is fail-soft
                extra_skips.append((fv.key, f"fill-error: {exc}"))
                continue
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

        post_url = _current_url(page)

    url_unchanged = _strip_fragment(pre_url) == _strip_fragment(post_url)
    if not url_unchanged:
        raise FillSafetyError(
            f"page navigated during fill ({pre_url!r} -> {post_url!r}); a "
            "navigation may indicate a submission or redirect")

    filled = len(filled_keys)
    all_skips = list(values.skipped) + extra_skips
    # DEVIATION A (owner-ratified 2026-07-10): the greenhouse-default completeness
    # shim died with engine.fill; the kernel completeness takes vendor_resolver
    # explicitly (None => no vendor widget classification).
    from engine.kernel.resolve import _completeness
    fillable_total, required_unfilled, justified_skips = _completeness(
        fieldmap, filled_keys, all_skips, filled, vendor_resolver=vendor_resolver)

    # DEVIATION C (2026-07-10, round 2): mirror the production per-vendor fill()
    # report shape -- no client-side validation harvest, no screenshot.
    return FillReport(
        vendor=vendor, company=slug, posting_id=str(job_id),
        fillable_total=fillable_total, filled=filled,
        required_unfilled=required_unfilled, justified_skips=justified_skips,
        uploads=uploads, skipped=all_skips,
        readback_mismatches=readback_mismatches,
        validation_errors=[],
        url_unchanged=url_unchanged, screenshot="", ts=ts)


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


_GOTO_TIMEOUT_MS = 20_000


# -- URL helpers ---------------------------------------------------------------

def _current_url(page) -> str:
    return getattr(page, "url", "") or ""


def _strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


# -- small helpers -------------------------------------------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
