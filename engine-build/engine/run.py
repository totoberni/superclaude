"""Once-daily read-only pipeline runner + CLI (W4 3.5).

Wires the engine into the sequence the spec fixes: load config/sources/ssot ->
map the profile -> fetch every source politely -> close board-absent items (only
for sources that actually answered this run) -> discover net-new postings ->
score -> enqueue -> re-rank -> draft the top items still lacking material (unless
--no-draft) -> render and publish exactly ONE digest push (unless --no-push /
--dry-run) -> append a telemetry record to runs.jsonl.

`run_pipeline` takes injectable collaborators (fetcher, drafter, transport,
runs_path) so tests exercise the whole flow with fakes under the no-network
fixture; `main` builds the live collaborators from config and CLI flags. An
unhandled exception propagates and exits non-zero; a partial fetch failure (some
boards down) is expected and never fatal.
"""

from __future__ import annotations

import argparse
import itertools
import json
import subprocess
import sys
import time
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from engine.artifacts import (
    render_letter_pdf,
    render_report_pdf,
    write_txt_fallback,
)
from engine.config import Config, load_config
from engine.draft import ClaudeCliDrafter, Drafter, select_language
from engine.fetch import HttpFetcher, Source, adapter_for, fetch_all, load_sources
from engine.kernel.contracts import FieldMap
from engine.kernel.discover_base import Posting, SourceAdapter
from engine.kernel.resolve import ANSWERABLE, MANUAL_ONLY, MISSING_STATUS, coverage
from engine.match import Scorer
from engine.notify import (
    FakeTransport,
    NtfyTransport,
    Transport,
    load_credentials,
    publish_digest,
)
from engine.profile_map import profile_from_real_ssot
from engine.providers import _registry
from engine.queue_sm import QueueStateMachine
from engine.ssot import MISSING, SSOT
from engine.store import Store

_HEADER_SUBTITLE = "Computational Scientist"

_DEFAULT_RUNS_PATH = Path.home() / "automations" / "jobhunt" / "runs.jsonl"
_DEFAULT_ARTIFACTS_DIR = Path.home() / "automations" / "jobhunt" / "artifacts"
_DEFAULT_CREDENTIALS = Path.home() / "automations" / "ntfy" / "credentials"
_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read", "cache_creation")


@dataclass
class RunOptions:
    dry_run: bool = False
    no_draft: bool = False
    push: bool = True
    rescore: bool = False
    # W4 3.3: None keeps field-map capture OFF (the daily timer default); an int
    # N captures/reuses field maps for the top-N visible automatable items across
    # all three vendors (greenhouse over HTTP, ashby/lever in-browser). main()
    # resolves "flag present, no value" to config.draft_cap.
    capture_fieldmaps: int | None = None


def run_pipeline(config: Config, sources: list[Source], ssot: SSOT, store,
                 *, options: RunOptions, drafter: Drafter | None = None,
                 transport: Transport | None = None,
                 fetcher: HttpFetcher | None = None,
                 runs_path: str | Path | None = None,
                 artifacts_dir: str | Path | None = None,
                 capture_opener=None,
                 runner: Callable = subprocess.run) -> dict:
    started = time.monotonic()
    profile = profile_from_real_ssot(ssot)
    scorer = Scorer(config, profile, ssot=ssot)
    queue = QueueStateMachine(store, config)

    discovery, fetch_results = fetch_all(sources, store, fetcher=fetcher)
    status_counts = Counter(r.status for r in fetch_results)

    closed = _close_board_absent(store, discovery)

    new_postings = run_discovery(discovery, store)
    # Draft grounding needs the posting text for CARRYOVER items too (they were
    # discovered on an earlier run, so they are absent from new_postings). The
    # boards were fully fetched this run anyway: index every listed posting
    # present today, not just the net-new ones, so any queued item drafted
    # today gets its live description merged in.
    discovered_index = {p.identity_key(): p
                        for adapter, raw, slug in discovery
                        for p in adapter.parse(raw, slug) if p.listed}

    enqueued = 0
    discarded = 0
    for posting in new_postings:
        breakdown = scorer.score(posting)
        if breakdown.discard:
            # W4-COMMUTE-GATE: a true removal, never surfaces. Recorded to the
            # ledger (never enqueued, no item_id) so the structural no-repeat
            # guarantee covers it too.
            store.record_ledger(posting.identity_key(), None, posting.vendor,
                                posting.company_slug, posting.title,
                                posting.url, "discarded", breakdown.total)
            discarded += 1
            continue
        if breakdown.total >= 0:  # threshold gating is rerank's job, not here
            queue.enqueue(posting, breakdown)
            enqueued += 1

    rescored = (_rescore_carryover(scorer, store, discovered_index, queue)
                if options.rescore else 0)

    rerank = queue.rerank()

    fieldmap_counts = _capture_fieldmaps(
        config, queue, store, discovered_index, ssot, profile, options,
        capture_opener)

    usage_totals, cost_total, drafted_items, validation_held = _draft_top_items(
        config, queue, ssot, discovered_index, store, options, drafter)

    live = _live_transport(options, transport)
    push_sent = _publish_one_digest(config, queue, rerank, live)
    artifacts = _render_and_publish_artifacts(
        config, drafted_items, live, artifacts_dir or _DEFAULT_ARTIFACTS_DIR,
        runner, store, ssot, profile)

    record = {
        "ts": _utc_now(),
        "counts": {
            "fetched_ok": status_counts.get("ok", 0)
                          + status_counts.get("not_modified", 0),
            "invalid": status_counts.get("invalid", 0),
            "blocked": status_counts.get("blocked", 0),
            "new": len(new_postings),
            "enqueued": enqueued,
            "discarded": discarded,
            "rescored": rescored,
            "drafted": len(drafted_items),
            "validation_held": validation_held,
            "closed": len(closed),
            "demoted": len(rerank.demoted_today),
        },
        "fieldmaps": fieldmap_counts,
        "usage_totals": usage_totals,
        "cost_usd_total": round(cost_total, 6),
        "duration_s": round(time.monotonic() - started, 3),
        "push_sent": push_sent,
        "artifacts": artifacts,
    }
    _append_run_record(runs_path or _DEFAULT_RUNS_PATH, record)
    return record


def run_discovery(sources: list[tuple[SourceAdapter, object, str]],
                 store: Store) -> list[Posting]:
    """Parse every source, enforce liveness, drop already-known items.

    `sources` is a list of (adapter, raw_json, company_slug). Returns only the
    net-new live postings; carryover lives in the queue, not here (7.5).
    """
    postings: list[Posting] = []
    for adapter, raw, slug in sources:
        for posting in adapter.parse(raw, slug):
            posting.unverified = not adapter.is_authoritative
            postings.append(posting)
    live = [p for p in postings if p.listed]
    return [p for p in live if not store.is_known(p.identity_key())]


def _close_board_absent(store, discovery) -> list[str]:
    """Close items missing from boards we actually reached (ok/not_modified)."""
    closed: list[str] = []
    for adapter, raw, slug in discovery:
        present = {p.identity_key() for p in adapter.parse(raw, slug) if p.listed}
        closed.extend(store.close_absent(adapter.vendor, slug, present))
    return closed


def _rescore_carryover(scorer: Scorer, store, discovered_index: dict,
                       queue: QueueStateMachine) -> int:
    """Recompute breakdowns for still-live queued items against the LIVE board.

    Carryover items were scored on the run that discovered them; an axis-function
    change (e.g. this calibration wave) leaves their persisted scores stale. For
    every pending_review/demoted row whose identity_key is on the board today,
    re-score from the fresh posting and stage a (score, payload.breakdown) update;
    all updates are flushed in ONE store transaction (67-min per-row regression
    guard). The ledger score is updated in the same batch. Returns the count.

    W5.1e round 2, THE BACKFILL: the discard channels are applied to new_postings
    only, so without this the wave would have changed NOTHING for the rows already
    in the owner's digest -- the two impossible postings at the top of it would go
    on being served every morning. A row that the CURRENT scorer discards is
    therefore carried through the queue state machine to `demoted` (rerank then
    drops it out of the visible list on its zero score), and the discard plus its
    reason are persisted in the payload so the demotion can be explained and
    audited. A row is NEVER deleted, and the state machine is never bypassed.

    Only rows still on the board TODAY are touched (`discovered_index`), so every
    re-score reads a FRESH posting WITH its full JD text: a JD-less row cannot be
    discarded here for lacking the sponsorship language it never had.
    """
    updates = []
    now_discarded = []
    for row in store.all_queue_rows():
        if row["state"] not in ("pending_review", "demoted"):
            continue
        posting = discovered_index.get(row["identity_key"])
        if posting is None:
            continue
        breakdown = scorer.score(posting)
        payload = dict(row["payload"])
        payload["breakdown"] = {
            "total": breakdown.total,
            "matched": breakdown.matched,
            "weak": breakdown.weak,
            "ats_warnings": breakdown.ats_warnings,
            "discard": breakdown.discard,
            "discard_reason": breakdown.discard_reason,
        }
        updates.append((row["item_id"], row["identity_key"], breakdown.total,
                        payload))
        if breakdown.discard and row["state"] == "pending_review":
            now_discarded.append(row["item_id"])
    rescored = store.bulk_update_scores(updates)
    for item_id in now_discarded:
        queue.transition(item_id, "demoted")
    return rescored


def _capture_fieldmaps(config, queue, store, discovered_index, ssot, profile,
                       options, opener) -> dict:
    """Capture-or-reuse field maps for the top-N visible automatable items,
    attaching a one-line coverage summary to each (W4 3.3).

    OFF by default (all-zero dict); operator-triggered only. Greenhouse is
    browserless; ashby/lever route through their per-vendor capture modules
    (engine/providers/{ashby,lever}/capture.py; lazy playwright import).
    The cache key is vendor+posting_id+updated_at, so a posting whose board
    updated_at moved is recaptured, not reused. Every item is fail-soft: a bad
    capture (including a browser/playwright failure) is counted and never aborts
    the run.
    """
    counts = {"captured": 0, "cached": 0, "failed": 0}
    if options.capture_fieldmaps is None:
        return counts
    opener = opener or _build_capture_opener()
    for item, posting in _fieldmap_targets(queue, discovered_index,
                                            options.capture_fieldmaps):
        try:
            counts[_capture_one(item, posting, store, ssot, profile, opener)] += 1
        except Exception as exc:
            counts["failed"] += 1
            _log_capture_failure(item, exc)
    return counts


def _log_capture_failure(item, exc: Exception) -> None:
    """One-line stderr diagnostic for a fail-soft capture (round-1 live-gap
    fix): the catch above previously swallowed the exception entirely, so a
    nohup'd daily run left zero trace of WHY an item failed. Truncated to 200
    chars so one bad payload can't flood the log."""
    vendor = item.payload.get("posting", {}).get("vendor", "?")
    detail = f"{type(exc).__name__}: {exc}"[:200]
    print(f"[fieldmap] capture failed item={item.item_id} vendor={vendor} "
         f"{detail}", file=sys.stderr)


_FIELDMAP_VENDORS = ("greenhouse", "ashby", "lever")


def _fieldmap_targets(queue, discovered_index, cap) -> list[tuple]:
    """Vendor-spread top-`cap` visible automatable capture-supported items.

    All three tier-1 vendors are eligible now that browser capture landed (since
    dissolved into the per-vendor capture modules); greenhouse
    is browserless, ashby/lever go through the headless browser. A plain
    global top-N (score order across ALL vendors) previously starved ashby/
    lever whenever greenhouse's higher-scoring items filled the whole cap, so
    the browser paths went unexercised (round-1 live finding). Round-robin
    across vendors instead (score order WITHIN each vendor), which gives every
    vendor up to ceil(cap / vendor-count) items; a vendor that runs out of
    candidates is simply skipped mid-round, so its share falls back to
    whichever vendor still has candidates (global fill).
    """
    if cap <= 0:
        return []
    by_vendor: dict[str, list] = {}
    for item in queue.items():
        posting = discovered_index.get(item.identity_key)
        vendor = item.payload.get("posting", {}).get("vendor")
        if (item.visible and item.channel == "automatable" and posting is not None
                and vendor in _FIELDMAP_VENDORS):
            by_vendor.setdefault(vendor, []).append(item)
    if not by_vendor:
        return []
    for vendor_items in by_vendor.values():
        vendor_items.sort(key=lambda i: i.score, reverse=True)

    rounds = itertools.zip_longest(*by_vendor.values())
    flattened = (item for one_round in rounds for item in one_round
                if item is not None)
    selected = list(itertools.islice(flattened, cap))
    return [(item, discovered_index[item.identity_key]) for item in selected]


def _coverage_with_vendor_resolver(fieldmap: FieldMap, ssot: SSOT,
                                   profile: dict):
    """Classify a field map's coverage through the kernel, injecting the
    vendor's portal-widget resolver at the PIPELINE seam (spec 3.4).

    The registry supplies the resolver per vendor: greenhouse contributes its
    location/paste-textarea/telemetry widget resolver; every other vendor (and
    an unregistered vendor) classifies with the kernel's no-op resolver. An
    unknown vendor never crashes the run path -- `PROVIDERS.get` returns None,
    so `coverage` falls back to its no-op default.
    """
    spec = _registry.PROVIDERS.get(fieldmap.vendor)
    resolver = spec.vendor_resolver() if spec and spec.vendor_resolver else None
    return coverage(fieldmap, ssot, profile, vendor_resolver=resolver)


def _capture_one(item, posting, store, ssot, profile, opener) -> str:
    """Capture or reuse one field map for any capture-supported vendor.

    Cache key is vendor+posting_id+updated_at (never a DOM hash, R-WT-8); a
    fresh capture dispatches to the vendor's collector. Returns the count
    bucket hit.
    """
    vendor = posting.vendor
    cached = store.get_fieldmap(vendor, posting.job_id, posting.updated_ts)
    if cached is not None:
        fieldmap = FieldMap.from_dict(cached["body"])
        bucket = "cached"
    else:
        fieldmap = _collect_fieldmap(vendor, posting, opener)
        store.put_fieldmap(vendor, posting.job_id, posting.updated_ts,
                           fieldmap.to_dict(), fieldmap.captured_at)
        bucket = "captured"
    report = _coverage_with_vendor_resolver(fieldmap, ssot, profile)
    _attach_coverage(store, item, report)
    return bucket


def _collect_fieldmap(vendor: str, posting, opener):
    """Dispatch to the vendor's field-map collector.

    Greenhouse is a browserless HTTP GET (fetch.py opener conventions);
    ashby/lever need a headless browser and live in their per-vendor capture
    modules (engine/providers/{ashby,lever}/capture.py), imported LAZILY
    so the daily timer run never imports playwright. Browser capture stays
    operator-triggered (only reached under --capture-fieldmaps), never
    default-on.

    Dispatch is delegated to the provider registry (the single source of truth):
    each vendor's `capture` normalises the two capture signatures onto
    (slug, job_id, opener) and the registry keeps the browser-vendor references
    lazy, so this delegation preserves both the never-import-playwright invariant
    and the module-attribute monkeypatch seam.
    """
    spec = _registry.PROVIDERS.get(vendor)
    if spec is None or not spec.supported or spec.capture is None:
        raise ValueError(f"no field-map capture for vendor {vendor!r}")
    return spec.capture(posting.company_slug, posting.job_id, opener)


_MISSING_LABELS_CAP = 6
_MISSING_LABEL_CHARS = 80


def _attach_coverage(store, item, report) -> None:
    payload = dict(item.payload)
    payload["fieldmap_coverage"] = report.summary_line()
    payload["fieldmap_missing"] = _missing_labels(report)
    store.upsert_queue(item.item_id, item.identity_key, item.state,
                       item.prev_state, item.score, int(item.visible),
                       item.channel, payload)


def _missing_labels(report) -> list[str]:
    """Missing-field labels (capped + truncated) alongside the one-line
    summary, so a round judgment never needs an offline rerun to see WHAT was
    unanswerable."""
    labels = [f.label[:_MISSING_LABEL_CHARS] for f in report.fields
             if f.status == MISSING_STATUS]
    return labels[:_MISSING_LABELS_CAP]


def _build_capture_opener():
    import urllib.request
    return urllib.request.build_opener()


def _draft_top_items(config, queue, ssot, discovered_index, store, options,
                     drafter) -> tuple[dict, float, list[dict], int]:
    usage_totals = {key: 0 for key in _USAGE_KEYS}
    if options.no_draft:
        return usage_totals, 0.0, [], 0
    drafter = drafter or _make_drafter(config)

    candidates = [item for item in queue.items()
                  if item.visible and item.state == "pending_review"
                  and not item.payload.get("material")]
    candidates.sort(key=lambda item: item.score, reverse=True)

    cost_total = 0.0
    drafted_items: list[dict] = []
    validation_held = 0
    for item in candidates[:config.draft_cap]:
        posting = dict(item.payload["posting"])
        discovered = discovered_index.get(item.identity_key)
        if discovered is not None:
            posting["description"] = discovered.description
        breakdown = item.payload["breakdown"]
        result = drafter.draft(posting, breakdown, ssot)
        if not result.ok:
            continue  # fail-soft: item stays pending_review, material unavailable
        # Generation spent real tokens regardless of what the anti-injection
        # validator decides below, so cost/usage are booked here, before the
        # validation_ok gate.
        cost_total += result.cost_usd
        for key in _USAGE_KEYS:
            usage_totals[key] += result.usage.get(key, 0)
        if not result.validation_ok:
            # Anti-injection L1 (draft.py's `_validate_material`) flagged this
            # body: a poisoned draft must never be surfaced as ready/clean.
            # Hold it at awaiting_input (the same 7.6 park/resume mechanism the
            # missing-required-field questionnaire already uses to keep a
            # not-yet-safe item off the ready bucket) instead of attaching the
            # material and adding it to `drafted_items`.
            _hold_validation_failure(store, queue, item,
                                     result.validation_violations)
            validation_held += 1
            continue
        _attach_material(store, item, result.material)
        lang, lang_rationale = select_language(posting)
        drafted_items.append({
            "item_id": item.item_id,
            "title": posting.get("title", ""),
            "company": posting.get("company_slug", ""),
            "material": result.material,
            "posting": posting,
            "breakdown": breakdown,
            "lang": lang,
            "lang_rationale": lang_rationale,
            "job_id": discovered.job_id if discovered is not None else None,
            "updated_ts": discovered.updated_ts if discovered is not None else None,
        })
    return usage_totals, cost_total, drafted_items, validation_held


def _attach_material(store, item, material: str) -> None:
    payload = dict(item.payload)
    payload["material"] = material
    store.upsert_queue(item.item_id, item.identity_key, item.state,
                       item.prev_state, item.score, int(item.visible),
                       item.channel, payload)


def _hold_validation_failure(store, queue, item, violations: list) -> None:
    """Park a poisoned draft at awaiting_input instead of leaving it a silent
    drop (spec 6b enforcement).

    The violations are written onto the payload FIRST so `queue.park()`'s
    state-preserving write (it re-reads the row and carries its payload
    through unchanged) picks them up too; the owner can then see WHY the item
    never reached pending_review/ready, not just that it didn't. `material`
    is deliberately never attached, so a later run's draft-candidate filter
    (`not item.payload.get("material")`) still picks the item back up once it
    resumes.
    """
    payload = dict(item.payload)
    payload["validation_violations"] = [_violation_dict(v) for v in violations]
    store.upsert_queue(item.item_id, item.identity_key, item.state,
                       item.prev_state, item.score, int(item.visible),
                       item.channel, payload)
    queue.park(item.item_id, reason="anti-injection L1 validation failed")


def _violation_dict(violation) -> dict:
    return {"code": violation.code, "field": violation.field,
           "detail": violation.detail}


def _live_transport(options, transport) -> Transport | None:
    """The transport to publish through, or None when publishing is suppressed.

    Resolved once so the digest push and the per-item attachments ride the same
    transport; --dry-run / --no-push short-circuit to None (no credentials read,
    nothing sent), even if a FakeTransport was injected for capture.
    """
    if options.dry_run or not options.push:
        return None
    return transport or NtfyTransport(load_credentials(_DEFAULT_CREDENTIALS))


def _publish_one_digest(config, queue, rerank, live) -> bool:
    if live is None:
        return False
    publish_digest(live, config.topic, queue.items(), len(rerank.demoted_today))
    return True


def _render_and_publish_artifacts(config, drafted_items, live, artifacts_dir,
                                  runner, store, ssot, profile) -> dict:
    """Render TWO documents per drafted item (cover letter + report) and publish
    them as SEPARATE ntfy attachment messages (W4 4c criterion 4).

    Rendering always runs; publishing honours suppression + attach_mode. The
    counts split letter/report and pdf/txt so a fallback on either document is
    visible in telemetry. The header/recipient dicts and the report field data
    are assembled deterministically from the SSOT and posting, never by the LLM.
    """
    artifacts = {"letter_pdf": 0, "letter_txt": 0, "report_pdf": 0,
                 "report_txt": 0, "published": 0, "publish_failed": 0}
    header = _build_letter_header(ssot)
    rendered: list[tuple[dict, str, Path]] = []
    for drafted in drafted_items:
        out_dir = Path(artifacts_dir) / drafted["item_id"]
        letter_path, letter_is_pdf = _render_letter(drafted, header, out_dir,
                                                     runner)
        artifacts["letter_pdf" if letter_is_pdf else "letter_txt"] += 1
        rendered.append((drafted, "cover letter", letter_path))

        report_data = _assemble_report_data(config, drafted, store, ssot, profile)
        report_path, report_is_pdf = _render_report(drafted, report_data,
                                                     out_dir, runner)
        artifacts["report_pdf" if report_is_pdf else "report_txt"] += 1
        rendered.append((drafted, "report", report_path))

    if live is None or config.attach_mode == "none" or not rendered:
        return artifacts
    if config.attach_mode == "bundle":
        published, failed = _publish_bundle(config, live, rendered,
                                            artifacts_dir)
    else:  # per_item (default)
        published, failed = _publish_per_item(config, live, rendered)
    artifacts["published"] = published
    artifacts["publish_failed"] = failed
    return artifacts


def _render_letter(drafted, header, out_dir, runner) -> tuple[Path, bool]:
    """Render one cover-letter PDF, or the verbatim .txt fallback (W4 4c 5)."""
    recipient = _build_recipient(drafted["posting"])
    subject = drafted["posting"].get("title", "") or ""
    pdf = render_letter_pdf(drafted["item_id"], drafted["material"], header,
                            recipient, subject, drafted.get("lang", "en"),
                            out_dir, runner=runner)
    if pdf is not None:
        return pdf, True
    txt = write_txt_fallback(drafted["item_id"], drafted["material"], out_dir,
                             kind="cover-letter")
    return txt, False


def _render_report(drafted, report_data, out_dir, runner) -> tuple[Path, bool]:
    """Render one report PDF, or a plain-text summary .txt fallback."""
    pdf = render_report_pdf(drafted["item_id"], report_data, out_dir,
                            runner=runner)
    if pdf is not None:
        return pdf, True
    txt = write_txt_fallback(drafted["item_id"], _report_txt(report_data),
                             out_dir, kind="report")
    return txt, False


def _build_letter_header(ssot: SSOT) -> dict:
    """Header fields from the SSOT identity/links; absent fields become
    [MISSING: <path>] so the owner sees what to fill (grounding contract)."""
    return {
        "full_name": _first_present(ssot, ["identity.full_name", "identity.name"]),
        "subtitle": _HEADER_SUBTITLE,
        "email": _first_present(ssot, ["identity.email"]),
        "phone": _first_present(ssot, ["identity.phone", "canned_answers.phone"]),
        "website": _first_present(ssot, ["links.website", "links.site"]),
        "linkedin": _first_present(
            ssot, ["links.linkedin", "canned_answers.linkedin"]),
    }


def _build_recipient(posting: dict) -> dict:
    """Recipient block from the posting: team line, company, city, country."""
    city, country = _split_city_country((posting.get("locations") or [""])[0])
    return {"team": "Hiring Team",
            "company": _title_company(posting.get("company_slug", "") or ""),
            "city": city, "country": country}


def _assemble_report_data(config, drafted, store, ssot, profile) -> dict:
    """Assemble the report tables deterministically from the item payload +
    field-data resolution (fieldmap coverage where one was captured, else the
    canned-answers surface). Never invents facts."""
    posting = drafted["posting"]
    breakdown = drafted.get("breakdown") or {}
    field_data, coverage = _resolve_field_data(drafted, store, ssot, profile,
                                               breakdown)
    return {
        "posting": {
            "vendor": posting.get("vendor", ""),
            "company": _title_company(posting.get("company_slug", "") or ""),
            "title": posting.get("title", ""),
            "locations": posting.get("locations") or [],
            "url": posting.get("url", ""),
            "score": breakdown.get("total"),
        },
        "score_rows": _score_rows(config, breakdown),
        "field_data": field_data,
        "coverage": coverage,
        "language": {"lang": drafted.get("lang", ""),
                     "rationale": drafted.get("lang_rationale", "")},
    }


_AXIS_STEMS = {
    "role_fit": ("role",),
    "skills_overlap": ("skill",),
    "seniority_fit": ("seniority",),
    "location_fit": ("location", "remote"),
    "comp_fit": ("comp",),
    "exclusions": ("exclud",),
}


def _score_rows(config, breakdown: dict) -> list[dict]:
    matched = breakdown.get("matched") or []
    weak = breakdown.get("weak") or []
    axis_scores = breakdown.get("axis_scores") or {}
    rows = []
    for axis, weight in config.axes.items():
        subscore = axis_scores.get(axis)
        rows.append({
            "axis": axis,
            "weight": f"{float(weight):.2f}",
            "subscore": "-" if subscore is None else f"{float(subscore):.2f}",
            "notes": _notes_for_axis(axis, matched, weak) or "-",
        })
    return rows


def _notes_for_axis(axis: str, matched: list, weak: list) -> str:
    stems = _AXIS_STEMS.get(axis, ())
    notes = [str(n) for n in matched if _stem_hit(stems, n)]
    notes += [f"weak: {n}" for n in weak if _stem_hit(stems, n)]
    return "; ".join(notes)


def _stem_hit(stems, note) -> bool:
    low = str(note).lower()
    return any(stem in low for stem in stems)


def _resolve_field_data(drafted, store, ssot, profile, breakdown):
    """Deterministic ATS field -> value resolution + coverage summary.

    Reuse the captured field map's coverage paths when one exists in the store
    for this posting; otherwise surface the canned-answers/identity fields.
    """
    fieldmap = _lookup_fieldmap(store, drafted)
    if fieldmap is not None:
        report = _coverage_with_vendor_resolver(fieldmap, ssot, profile)
        return _field_data_from_coverage(report, ssot), {
            "summary": report.summary_line(),
            "warnings": breakdown.get("ats_warnings") or [],
            "missing": report.missing_paths(),
        }
    return _field_data_from_canned(ssot), {
        "summary": "no field map captured for this posting",
        "warnings": breakdown.get("ats_warnings") or [],
        "missing": [],
    }


def _lookup_fieldmap(store, drafted) -> FieldMap | None:
    vendor = drafted["posting"].get("vendor")
    job_id = drafted.get("job_id")
    if not vendor or not job_id:
        return None
    cached = store.get_fieldmap(vendor, str(job_id), drafted.get("updated_ts"))
    return FieldMap.from_dict(cached["body"]) if cached is not None else None


def _field_data_from_coverage(report, ssot: SSOT) -> list[dict]:
    rows = []
    for fc in report.fields:
        if fc.status == ANSWERABLE:
            value = _fmt_value(ssot.get(fc.path))
        elif fc.status == MANUAL_ONLY:
            value = f"(manual-only: {fc.reason})"
        else:
            value = f"[MISSING: {fc.path}]"
        rows.append({"field": fc.label, "value": value})
    return rows


_CANNED_SURFACE = (
    ("Full name", "identity.name"),
    ("Email", "identity.email"),
    ("Current location", "identity.current_location"),
    ("Website", "links.site"),
    ("GitHub", "links.github"),
    ("Work authorization", "work_authorization"),
)


def _field_data_from_canned(ssot: SSOT) -> list[dict]:
    rows = []
    for label, path in _CANNED_SURFACE:
        value = ssot.get(path)
        if value is not MISSING:
            rows.append({"field": label, "value": _fmt_value(value)})
    canned = ssot.get("canned_answers")
    if isinstance(canned, dict):
        for key, value in canned.items():
            rows.append({"field": f"canned: {key}", "value": _fmt_value(value)})
    return rows


def _report_txt(report_data: dict) -> str:
    """A plain-text rendering of the report for the .txt fallback (no LaTeX)."""
    posting = report_data.get("posting") or {}
    coverage = report_data.get("coverage") or {}
    lines = [
        f"vendor: {posting.get('vendor', '')}",
        f"company: {posting.get('company', '')}",
        f"title: {posting.get('title', '')}",
        f"score: {posting.get('score', '')}",
        f"coverage: {coverage.get('summary', '')}",
        "",
        "field data:",
    ]
    for row in report_data.get("field_data") or []:
        lines.append(f"  {row.get('field', '')}: {row.get('value', '')}")
    return "\n".join(lines)


def _first_present(ssot: SSOT, paths: list[str]) -> str:
    for path in paths:
        value = ssot.get(path)
        if value is not MISSING:
            return _fmt_value(value)
    return f"[MISSING: {paths[0]}]"


def _fmt_value(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}={v}" for k, v in value.items())
    return str(value)


def _title_company(company_slug: str) -> str:
    return company_slug.replace("_", " ").replace("-", " ").title()


def _split_city_country(location: str) -> tuple[str, str]:
    parts = [p.strip() for p in str(location or "").split(",") if p.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def _publish_per_item(config, live, rendered) -> tuple[int, int]:
    """Publish one attachment per rendered document (letter + report each),
    fail-soft (W4 3.9 + 4c 4).

    A single bad attachment (unrenderable caption, transient transport error)
    must not abort the digest already sent nor the remaining attachments in
    the batch; it is only counted as a failure.
    """
    published = 0
    failed = 0
    for drafted, kind, path in rendered:
        caption = (f"[{drafted['item_id']}] {kind} - {drafted['title']} "
                   f"@ {drafted['company']}")
        try:
            live.publish_file(config.topic, path, caption, path.name)
            published += 1
        except Exception:
            failed += 1
    return published, failed


def _publish_bundle(config, live, rendered, artifacts_dir) -> tuple[int, int]:
    zip_path = Path(artifacts_dir) / f"jobhunt-drafts-{_stamp()}.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for drafted, _kind, path in rendered:
            archive.write(path, arcname=f"{drafted['item_id']}/{path.name}")
    caption = f"{len(rendered)} drafted application artifacts"
    try:
        live.publish_file(config.topic, zip_path, caption, zip_path.name)
        return 1, 0
    except Exception:
        return 0, 1


def _make_drafter(config: Config) -> Drafter:
    return ClaudeCliDrafter(model=config.drafter.get("model", "sonnet"),
                            effort=config.drafter.get("effort", "medium"))


def _append_run_record(path: str | Path, record: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="engine.run", description="JobHunt daily read-only pipeline")
    parser.add_argument("--config", required=True)
    parser.add_argument("--sources")
    parser.add_argument("--ssot", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-draft", action="store_true")
    parser.add_argument("--rescore", action="store_true",
                        help="re-score carryover queue items from the live board "
                             "before rerank (default off)")
    parser.add_argument("--capture-fieldmaps", nargs="?", type=int, const=-1,
                        default=None, metavar="N",
                        help="capture/reuse field maps for the top-N visible "
                             "automatable greenhouse items and attach a coverage "
                             "summary (N defaults to draft_cap); off by default")
    parser.add_argument("--push", action=argparse.BooleanOptionalAction,
                        default=True)
    return parser.parse_args(argv)


def _resolve_sources_path(args, config: Config) -> Path:
    if args.sources:
        return Path(args.sources)
    if config.sources:
        return Path(args.config).parent / config.sources
    raise SystemExit("no sources path: pass --sources or set 'sources' in config")


def main(argv=None) -> int:
    args = _parse_args(argv)
    config = load_config(args.config)
    sources = load_sources(_resolve_sources_path(args, config))
    ssot = SSOT.load(args.ssot)
    store = _open_store(args.store)
    capture_n = _resolve_capture_n(args.capture_fieldmaps, config)
    options = RunOptions(dry_run=args.dry_run, no_draft=args.no_draft,
                        push=args.push, rescore=args.rescore,
                        capture_fieldmaps=capture_n)
    transport = FakeTransport() if args.dry_run else None
    capture_opener = _build_capture_opener() if capture_n is not None else None
    try:
        run_pipeline(config, sources, ssot, store, options=options,
                     transport=transport, capture_opener=capture_opener)
    finally:
        store.close()
    return 0


def _resolve_capture_n(value: int | None, config: Config) -> int | None:
    """None keeps capture off; the const sentinel (<0) means use draft_cap."""
    if value is None:
        return None
    return config.draft_cap if value < 0 else value


def _open_store(path: str):
    from engine.store import Store
    return Store(path)


if __name__ == "__main__":
    sys.exit(main())
