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
- File uploads are NEVER performed: `set_input_files` is not called anywhere and
  `input[type=file]` fields are skipped with reason "file-upload deferred to W5"
  (several ATSs XHR-upload the document on selection, which would transmit files
  pre-submission).
- EEO / demographic / compliance fields are never touched: they classify as
  manual-only in `fieldmap.coverage` and so never enter the fill set.

W5 DEFERRAL NOTES: the gated real submitter, file/document upload, and any
click that advances or submits a form are all deferred to W5's explicitly
owner-gated submitter. This module is operator-CLI only and is NEVER wired to
the daily timer.

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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from engine.browse import ashby_application_url, lever_apply_url
from engine.fieldmap import (
    MANUAL_ONLY,
    MISSING_STATUS,
    FieldMap,
    Locator,
    _classify_field,
    capture_greenhouse,
)
from engine.ssot import MISSING, SSOT

# The dotted path the coverage classifier assigns to any privacy/consent field
# (its `_ANSWER_MATCHERS` consent tuple resolves here); a boolean field on this
# path is the ONLY checkbox this dry run auto-ticks.
_CONSENT_PATH = "canned_answers.optional_consents"

# Field types that render as an option choice rather than free text.
_SELECT_TYPES = frozenset({
    "multi_value_single_select", "multi_value_multi_select", "yes_no",
})

# Click-name denylist: the SINGLE guard that makes a submit structurally
# impossible. Any element whose accessible name matches is refused by
# `_safe_click`, the module's only sanctioned click primitive.
_CLICK_DENYLIST = re.compile(r"submit|apply|send|finish|continue", re.I)


class FillSafetyError(RuntimeError):
    """A safety invariant of the dry run was about to be violated.

    Raised (never swallowed) when a click would hit a submit-like control, when
    the page navigated during the fill (possible submission/redirect), or when
    any other STOP-SHORT-OF-APPLYING guard trips. Distinct from a per-field fill
    error (which is fail-soft): a FillSafetyError aborts the whole fill.
    """


# -- resolved fill values ------------------------------------------------------

@dataclass
class FieldValue:
    """One concrete field to fill: the rendered value plus the locator hints and
    type needed to reach and drive the control (fill_form gets no fieldmap)."""
    key: str
    label: str
    type: str
    locator: Locator
    value: str | bool | list


@dataclass
class ResolvedValues:
    """The deterministic output of `resolve_values`: the fillable fields (with
    the metadata fill_form needs) plus the fields skipped with their reasons.

    `.values` exposes the documented `dict[str, str|bool|list]` key->value view;
    fill_form consumes the richer `.fields`/`.skipped` directly.
    """
    fields: list[FieldValue] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def values(self) -> dict[str, str | bool | list]:
        return {fv.key: fv.value for fv in self.fields}


@dataclass
class FillReport:
    vendor: str
    posting_id: str
    filled: int
    skipped: list[tuple[str, str]]
    readback_mismatches: list[dict]
    validation_errors: list[dict]
    url_unchanged: bool
    screenshot: str
    ts: str

    def to_dict(self) -> dict:
        return {
            "vendor": self.vendor,
            "posting_id": self.posting_id,
            "filled": self.filled,
            "skipped": [[key, reason] for key, reason in self.skipped],
            "readback_mismatches": self.readback_mismatches,
            "validation_errors": self.validation_errors,
            "url_unchanged": self.url_unchanged,
            "screenshot": self.screenshot,
            "ts": self.ts,
        }


# -- deterministic value resolution --------------------------------------------

def resolve_values(fieldmap: FieldMap, ssot: SSOT, profile: dict) -> ResolvedValues:
    """Classify + render every field of `fieldmap` into concrete fill values.

    Reuses `fieldmap._classify_field` (the SSOT coverage classifier) per field,
    so manual-only (file upload / EEO-demographic / portal widget) and missing
    (unanswerable) fields are SKIPPED with their classifier reason. An answerable
    field is rendered by type: free text from the resolved SSOT string, an option
    label for a select when an option matches the SSOT value case-insensitively
    (else skipped), and bool True for a privacy-consent checkbox. Deterministic,
    no LLM; never writes the SSOT.
    """
    profile = profile or {}
    resolved = ResolvedValues()
    for fld in fieldmap.fields:
        classified = _classify_field(fld, ssot, profile)
        if classified.status == MANUAL_ONLY:
            resolved.skipped.append((fld.key, classified.reason or MANUAL_ONLY))
            continue
        if classified.status == MISSING_STATUS:
            resolved.skipped.append((fld.key, classified.classification()))
            continue
        value, skip_reason = _render_value(fld, classified.path, ssot)
        if skip_reason is not None:
            resolved.skipped.append((fld.key, skip_reason))
            continue
        resolved.fields.append(FieldValue(
            key=fld.key, label=fld.label, type=fld.type,
            locator=fld.locator, value=value))
    return resolved


def _render_value(fld, path: str, ssot: SSOT):
    """Render one ANSWERABLE field to (value, None) or (None, skip_reason)."""
    if fld.type == "input_file":
        return None, "file-upload deferred to W5"
    if fld.type == "boolean":
        if path == _CONSENT_PATH:
            return True, None
        return None, "non-consent checkbox not auto-checked in dry run"
    raw = ssot.get(path)
    if raw is MISSING:
        return None, f"answerable via {path} but no literal SSOT value"
    if fld.type in _SELECT_TYPES:
        return _render_select(fld, raw)
    return _render_text(raw, path)


def _render_select(fld, raw):
    if fld.type == "multi_value_multi_select":
        candidates = raw if isinstance(raw, list) else [raw]
        matched = [m for m in (_match_option(fld.options, c) for c in candidates)
                   if m is not None]
        if not matched:
            return None, f"no option matches SSOT value {_short(raw)!r}"
        return matched, None
    match = _match_option(fld.options, raw)
    if match is None:
        return None, f"no option matches SSOT value {_short(raw)!r}"
    return match, None


def _match_option(options, raw):
    """The option label equal (case-insensitively) to a scalar SSOT value."""
    if isinstance(raw, (list, dict)):
        return None
    target = str(raw).strip().lower()
    if not target:
        return None
    for option in options:
        if str(option).strip().lower() == target:
            return option
    return None


def _render_text(raw, path: str):
    if isinstance(raw, bool):
        return ("Yes" if raw else "No"), None
    if isinstance(raw, str):
        return raw, None
    if isinstance(raw, (int, float)):
        return str(raw), None
    if isinstance(raw, list) and all(
            isinstance(item, (str, int, float)) for item in raw):
        return ", ".join(str(item) for item in raw), None
    return None, f"value for {path} is not renderable as text"


def _short(value) -> str:
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."


# -- the fill itself -----------------------------------------------------------

def fill_form(vendor: str, slug: str, job_id: str, values: ResolvedValues,
              browser_factory=None, artifacts_dir=None, *,
              now: Callable[[], str] | None = None) -> FillReport:
    """Fill one live application page with `values`, STOPPING SHORT OF APPLYING.

    Navigates to the vendor apply page, fills each resolved value via a role/label
    locator (reusing the fieldmap locator hint, falling back to label text),
    blurs after each fill to harvest validation errors (aria-invalid + `.error`
    text), reads every filled control back to diff against intent, screenshots
    the filled page, and asserts the page URL is unchanged. Returns a FillReport.
    Never submits, never uploads a file, never touches an EEO field. A navigation
    or a submit-like click raises FillSafetyError.
    """
    ts = (now or _utc_now_iso)()
    url = _apply_url(vendor, slug, job_id)
    factory = browser_factory or _default_browser_page

    readback_mismatches: list[dict] = []
    validation_errors: list[dict] = []
    extra_skips: list[tuple[str, str]] = []
    filled = 0

    with factory() as page:
        page.goto(url, wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
        pre_url = _current_url(page)

        for fv in values.fields:
            if fv.type == "input_file":
                # Defence in depth: even a hand-crafted values must never touch
                # a file input (no set_input_files, ever).
                extra_skips.append((fv.key, "file-upload deferred to W5"))
                continue
            try:
                locator = _locate(page, fv)
                _apply(locator, fv)
                filled += 1
                locator.blur()
            except FillSafetyError:
                raise
            except Exception as exc:  # per-field fill error is fail-soft
                extra_skips.append((fv.key, f"fill-error: {exc}"))
                continue
            _harvest_field_validation(locator, fv, validation_errors)
            actual, ok = _readback(locator, fv.value)
            if not ok:
                readback_mismatches.append(
                    {"key": fv.key, "intended": fv.value, "actual": actual})

        _harvest_page_errors(page, validation_errors)
        screenshot = _screenshot(page, vendor, job_id, ts, artifacts_dir)
        post_url = _current_url(page)

    url_unchanged = _strip_fragment(pre_url) == _strip_fragment(post_url)
    if not url_unchanged:
        raise FillSafetyError(
            f"page navigated during fill ({pre_url!r} -> {post_url!r}); a "
            "navigation may indicate a submission or redirect")

    return FillReport(
        vendor=vendor, posting_id=str(job_id), filled=filled,
        skipped=list(values.skipped) + extra_skips,
        readback_mismatches=readback_mismatches,
        validation_errors=validation_errors,
        url_unchanged=url_unchanged, screenshot=str(screenshot), ts=ts)


def _safe_click(target, name: str) -> None:
    """The SOLE sanctioned click primitive: refuses any submit-like name.

    Every click need (there are none on the happy path; a combobox open would be
    one) must route through here so a submit is structurally impossible.
    """
    if _CLICK_DENYLIST.search(name or ""):
        raise FillSafetyError(
            f"refusing to click element named {name!r}: matches the submit "
            "denylist (submit/apply/send/finish/continue); this dry run STOPS "
            "SHORT OF APPLYING")
    target.click()


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


def _locate(page, fv: FieldValue):
    """Reuse the fieldmap locator hint (role+name); fall back to the label."""
    role = fv.locator.role
    name = fv.locator.name
    if role and name:
        return page.get_by_role(role, name=name)
    return page.get_by_label(fv.label)


def _readback(locator, value):
    """Read the control's current value back and decide if it matches intent."""
    if isinstance(value, bool):
        actual = locator.is_checked()
        return actual, bool(actual) == value
    if isinstance(value, list):
        actual = locator.input_value()
        haystack = str(actual).lower()
        return actual, all(str(item).strip().lower() in haystack
                           for item in value)
    actual = locator.input_value()
    return actual, _norm(actual) == _norm(value)


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
    if vendor == "greenhouse":
        return greenhouse_apply_url(slug, job_id)
    if vendor == "lever":
        return lever_apply_url(slug, job_id)
    if vendor == "ashby":
        return ashby_application_url(slug, job_id)
    raise ValueError(f"unknown vendor {vendor!r} (expected greenhouse/lever/ashby)")


def _current_url(page) -> str:
    return getattr(page, "url", "") or ""


def _strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


# -- small helpers -------------------------------------------------------------

def _norm(value) -> str:
    return str(value).strip().lower()


def _safe_get_attr(locator, name: str):
    getter = getattr(locator, "get_attribute", None)
    if getter is None:
        return None
    try:
        return getter(name)
    except Exception:
        return None


def _safe_stamp(ts: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", ts) or "run"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    store = Store(args.store)
    try:
        fieldmap = _load_fieldmap(
            store, args.vendor, args.slug, args.job_id,
            updated_at=args.updated_at, capture_first=args.capture_first)
        values = resolve_values(fieldmap, ssot, profile)
        report = fill_form(args.vendor, args.slug, args.job_id, values,
                           artifacts_dir=args.artifacts)
    finally:
        store.close()
    print(json.dumps(report.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
