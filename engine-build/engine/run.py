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

from engine.artifacts import render_pdf, write_txt_fallback
from engine.config import Config, load_config
from engine.discover import run_discovery
from engine.draft import ClaudeCliDrafter, Drafter
from engine.fetch import HttpFetcher, Source, adapter_for, fetch_all, load_sources
from engine.fieldmap import FieldMap, capture_greenhouse
from engine.match import Scorer
from engine.notify import (
    FakeTransport,
    NtfyTransport,
    Transport,
    load_credentials,
    publish_digest,
)
from engine.profile_map import profile_from_real_ssot
from engine.queue_sm import QueueStateMachine
from engine.ssot import SSOT

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
    scorer = Scorer(config, profile)
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
    for posting in new_postings:
        breakdown = scorer.score(posting)
        if breakdown.total >= 0:  # threshold gating is rerank's job, not here
            queue.enqueue(posting, breakdown)
            enqueued += 1

    rescored = (_rescore_carryover(scorer, store, discovered_index)
                if options.rescore else 0)

    rerank = queue.rerank()

    fieldmap_counts = _capture_fieldmaps(
        config, queue, store, discovered_index, ssot, profile, options,
        capture_opener)

    usage_totals, cost_total, drafted_items = _draft_top_items(
        config, queue, ssot, discovered_index, store, options, drafter)

    live = _live_transport(options, transport)
    push_sent = _publish_one_digest(config, queue, rerank, live)
    artifacts = _render_and_publish_artifacts(
        config, drafted_items, live, artifacts_dir or _DEFAULT_ARTIFACTS_DIR,
        runner)

    record = {
        "ts": _utc_now(),
        "counts": {
            "fetched_ok": status_counts.get("ok", 0)
                          + status_counts.get("not_modified", 0),
            "invalid": status_counts.get("invalid", 0),
            "blocked": status_counts.get("blocked", 0),
            "new": len(new_postings),
            "enqueued": enqueued,
            "rescored": rescored,
            "drafted": len(drafted_items),
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


def _close_board_absent(store, discovery) -> list[str]:
    """Close items missing from boards we actually reached (ok/not_modified)."""
    closed: list[str] = []
    for adapter, raw, slug in discovery:
        present = {p.identity_key() for p in adapter.parse(raw, slug) if p.listed}
        closed.extend(store.close_absent(adapter.vendor, slug, present))
    return closed


def _rescore_carryover(scorer: Scorer, store, discovered_index: dict) -> int:
    """Recompute breakdowns for still-live queued items against the LIVE board.

    Carryover items were scored on the run that discovered them; an axis-function
    change (e.g. this calibration wave) leaves their persisted scores stale. For
    every pending_review/demoted row whose identity_key is on the board today,
    re-score from the fresh posting and stage a (score, payload.breakdown) update;
    all updates are flushed in ONE store transaction (67-min per-row regression
    guard). The ledger score is updated in the same batch. Returns the count.
    """
    updates = []
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
        }
        updates.append((row["item_id"], row["identity_key"], breakdown.total,
                        payload))
    return store.bulk_update_scores(updates)


def _capture_fieldmaps(config, queue, store, discovered_index, ssot, profile,
                       options, opener) -> dict:
    """Capture-or-reuse field maps for the top-N visible automatable items,
    attaching a one-line coverage summary to each (W4 3.3).

    OFF by default (all-zero dict); operator-triggered only. Greenhouse is
    browserless; ashby/lever route through browse.py (lazy playwright import).
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
        except Exception:
            counts["failed"] += 1
    return counts


_FIELDMAP_VENDORS = ("greenhouse", "ashby", "lever")


def _fieldmap_targets(queue, discovered_index, cap) -> list[tuple]:
    """Top-`cap` visible automatable capture-supported items still on the board.

    All three tier-1 vendors are eligible now that browse.py lands; greenhouse
    is browserless, ashby/lever go through the headless browser.
    """
    candidates = []
    for item in queue.items():
        posting = discovered_index.get(item.identity_key)
        vendor = item.payload.get("posting", {}).get("vendor")
        if (item.visible and item.channel == "automatable" and posting is not None
                and vendor in _FIELDMAP_VENDORS):
            candidates.append(item)
    candidates.sort(key=lambda i: i.score, reverse=True)
    return [(item, discovered_index[item.identity_key])
            for item in candidates[:cap]]


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
    report = fieldmap.coverage(ssot, profile)
    _attach_coverage(store, item, report.summary_line())
    return bucket


def _collect_fieldmap(vendor: str, posting, opener):
    """Dispatch to the vendor's field-map collector.

    Greenhouse is a browserless HTTP GET (fetch.py opener conventions);
    ashby/lever need a headless browser and live in browse.py, imported LAZILY
    so the daily timer run never imports playwright. Browser capture stays
    operator-triggered (only reached under --capture-fieldmaps), never
    default-on.
    """
    if vendor == "greenhouse":
        return capture_greenhouse(posting.company_slug, posting.job_id, opener)
    from engine import browse
    if vendor == "ashby":
        return browse.capture_ashby(posting.company_slug, posting.job_id)
    if vendor == "lever":
        return browse.capture_lever(posting.company_slug, posting.job_id)
    raise ValueError(f"no field-map capture for vendor {vendor!r}")


def _attach_coverage(store, item, summary: str) -> None:
    payload = dict(item.payload)
    payload["fieldmap_coverage"] = summary
    store.upsert_queue(item.item_id, item.identity_key, item.state,
                       item.prev_state, item.score, int(item.visible),
                       item.channel, payload)


def _build_capture_opener():
    import urllib.request
    return urllib.request.build_opener()


def _draft_top_items(config, queue, ssot, discovered_index, store, options,
                     drafter) -> tuple[dict, float, list[dict]]:
    usage_totals = {key: 0 for key in _USAGE_KEYS}
    if options.no_draft:
        return usage_totals, 0.0, []
    drafter = drafter or _make_drafter(config)

    candidates = [item for item in queue.items()
                  if item.visible and item.state == "pending_review"
                  and not item.payload.get("material")]
    candidates.sort(key=lambda item: item.score, reverse=True)

    cost_total = 0.0
    drafted_items: list[dict] = []
    for item in candidates[:config.draft_cap]:
        posting = dict(item.payload["posting"])
        discovered = discovered_index.get(item.identity_key)
        if discovered is not None:
            posting["description"] = discovered.description
        result = drafter.draft(posting, item.payload["breakdown"], ssot)
        if not result.ok:
            continue  # fail-soft: item stays pending_review, material unavailable
        _attach_material(store, item, result.material)
        cost_total += result.cost_usd
        for key in _USAGE_KEYS:
            usage_totals[key] += result.usage.get(key, 0)
        drafted_items.append({
            "item_id": item.item_id,
            "title": item.payload["posting"].get("title", ""),
            "company": item.payload["posting"].get("company_slug", ""),
            "material": result.material,
        })
    return usage_totals, cost_total, drafted_items


def _attach_material(store, item, material: str) -> None:
    payload = dict(item.payload)
    payload["material"] = material
    store.upsert_queue(item.item_id, item.identity_key, item.state,
                       item.prev_state, item.score, int(item.visible),
                       item.channel, payload)


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
                                  runner) -> dict:
    """Render one PDF (or .txt fallback) per drafted item, then publish per
    attach_mode. Rendering always runs; publishing honours suppression + mode."""
    artifacts = {"rendered_pdf": 0, "fallback_txt": 0, "published": 0,
                "publish_failed": 0}
    rendered: list[tuple[dict, Path]] = []
    for drafted in drafted_items:
        out_dir = Path(artifacts_dir) / drafted["item_id"]
        pdf = render_pdf(drafted["item_id"], drafted["material"], out_dir,
                         company_slug=drafted["company"], runner=runner)
        if pdf is not None:
            artifacts["rendered_pdf"] += 1
            rendered.append((drafted, pdf))
        else:
            txt = write_txt_fallback(drafted["item_id"], drafted["material"],
                                     out_dir, company_slug=drafted["company"])
            artifacts["fallback_txt"] += 1
            rendered.append((drafted, txt))

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


def _publish_per_item(config, live, rendered) -> tuple[int, int]:
    """Publish one attachment per drafted item, fail-soft (W4 3.9 fix wave).

    A single bad attachment (unrenderable caption, transient transport error)
    must not abort the digest already sent nor the remaining attachments in
    the batch; it is only counted as a failure.
    """
    published = 0
    failed = 0
    for drafted, path in rendered:
        caption = (f"[{drafted['item_id']}] {drafted['title']} "
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
        for drafted, path in rendered:
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
