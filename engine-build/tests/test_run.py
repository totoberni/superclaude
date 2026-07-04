"""End-to-end pipeline runner tests: one push, telemetry, cap, dry-run, guards.

Everything is faked: the fetcher wraps a scripted opener over the JSON fixtures,
the drafter and transport are in-memory. No socket is created, so the autouse
no-network fixture is satisfied throughout.
"""

import dataclasses
import json
import re

import urllib.error

from engine.discover import GreenhouseAdapter
from engine.draft import DraftResult
from engine.fetch import HttpFetcher, Source
from engine.match import Scorer
from engine.notify import FakeTransport
from engine.profile_map import profile_from_real_ssot
from engine.queue_sm import QueueItem
from engine.run import (
    RunOptions,
    _assemble_report_data,
    _build_letter_header,
    _fieldmap_targets,
    run_pipeline,
)
from engine.ssot import SSOT
from engine.store import Store
from engine.validate.checks import Violation


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class _Resp:
    def __init__(self, body):
        self._body = body
        self.headers = {}

    def read(self):
        return self._body


class _Opener:
    def __init__(self, bodies):
        self.bodies = list(bodies)

    def open(self, req, timeout=None):
        return _Resp(self.bodies.pop(0))


class _CaptureOpener:
    """Serves one questions=true body for every field-map GET (no network)."""

    def __init__(self, raw):
        self._body = json.dumps(raw).encode("utf-8")
        self.calls = 0

    def open(self, req, timeout=None):
        self.calls += 1
        return _Resp(self._body)


class _FailingCaptureOpener:
    def open(self, req, timeout=None):
        raise RuntimeError("simulated field-map capture failure")


class _ErrorOpener:
    def __init__(self, code):
        self.code = code

    def open(self, req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, self.code, "err", {}, None)


class FakeDrafter:
    def __init__(self):
        self.calls = []

    def draft(self, posting, breakdown, ssot):
        self.calls.append(posting)
        return DraftResult(
            material="COVER LETTER\n\nFIELD DATA\nnotice_period: 1 month",
            usage={"input_tokens": 10, "output_tokens": 5, "cache_read": 1,
                   "cache_creation": 0},
            cost_usd=0.002, model="claude-sonnet-4-5", ok=True)


class PartiallyPoisonedDrafter:
    """First draft() call returns a poisoned (validation_ok=False) result, as
    if the L1 anti-injection scan (draft.py's `_validate_material`) caught a
    smuggled attacker link; every later call returns a clean, explicitly
    validation_ok=True result. Exercises the held path and the normal ready
    path side by side in one run."""

    def __init__(self):
        self.calls = []

    def draft(self, posting, breakdown, ssot):
        self.calls.append(posting)
        if len(self.calls) == 1:
            return DraftResult(
                material="Dear Hiring Manager,\n\nSee more at "
                        "evil-exfil.com/steal.\n\nBest,\nTest Candidate",
                usage={"input_tokens": 10, "output_tokens": 5, "cache_read": 1,
                       "cache_creation": 0},
                cost_usd=0.002, model="claude-sonnet-4-5", ok=True,
                validation_ok=False,
                validation_violations=[Violation(
                    code="disallowed_url", field=None,
                    detail="evil-exfil.com/steal")])
        return DraftResult(
            material="COVER LETTER\n\nFIELD DATA\nnotice_period: 1 month",
            usage={"input_tokens": 10, "output_tokens": 5, "cache_read": 1,
                   "cache_creation": 0},
            cost_usd=0.002, model="claude-sonnet-4-5", ok=True,
            validation_ok=True, validation_violations=[])


def _sources():
    return [Source("greenhouse", "acme", "Acme"),
            Source("lever", "globex", "Globex"),
            Source("ashby", "initech", "Initech")]


def _fetcher(store, *raws):
    clock = _Clock()
    bodies = [json.dumps(raw).encode("utf-8") for raw in raws]
    return HttpFetcher(store, opener=_Opener(bodies), sleep=clock.sleep,
                       clock=clock)


def test_run_end_to_end_one_push_and_telemetry(tmp_path, jobhunt_config,
                                              real_ssot_path, greenhouse_raw,
                                              lever_raw, ashby_raw,
                                              fake_pdflatex):
    store = Store(tmp_path / "store.db")
    runs = tmp_path / "runs.jsonl"
    artifacts_dir = tmp_path / "artifacts"
    transport = FakeTransport()
    drafter = FakeDrafter()
    fetcher = _fetcher(store, greenhouse_raw, lever_raw, ashby_raw)

    record = run_pipeline(jobhunt_config, _sources(), SSOT.load(real_ssot_path),
                          store, options=RunOptions(), drafter=drafter,
                          transport=transport, fetcher=fetcher, runs_path=runs,
                          artifacts_dir=artifacts_dir, runner=fake_pdflatex())
    store.close()

    # exactly one DIGEST push, to the configured topic (attachments ride the
    # separate sent_files channel, so the digest count stays at 1)
    assert len(transport.sent) == 1
    topic, message = transport.sent[0]
    assert topic == jobhunt_config.topic
    assert record["push_sent"] is True

    header = message.splitlines()[0]
    assert re.fullmatch(r"\d+ ready · \d+ manual · \d+ held · \d+ demoted today",
                        header)
    ready = int(header.split(" ready")[0])

    # 2 greenhouse + 1 lever + 1 listed ashby = 4 live; unlisted ashby dropped
    assert record["counts"]["new"] == 4
    assert record["counts"]["enqueued"] == 4
    assert record["counts"]["closed"] == 0
    assert record["counts"]["fetched_ok"] == 3

    # every drafted item is a visible automatable pending_review item (the ready
    # bucket); usage + cost totals scale with the number drafted
    drafted = record["counts"]["drafted"]
    assert drafted >= 1
    assert drafted == ready == len(drafter.calls)
    assert record["usage_totals"]["input_tokens"] == 10 * drafted

    # per_item: TWO PDFs rendered (cover letter + report) and TWO attachments
    # published per drafted item, as separate messages (W4 4c criterion 4)
    assert record["artifacts"] == {"letter_pdf": drafted, "letter_txt": 0,
                                   "report_pdf": drafted, "report_txt": 0,
                                   "published": 2 * drafted, "publish_failed": 0}
    assert len(transport.sent_files) == 2 * drafted
    kinds = {"cover letter": 0, "report": 0}
    for sent_topic, _path, caption, filename in transport.sent_files:
        assert sent_topic == jobhunt_config.topic
        assert caption.startswith("[j-")
        assert (filename.endswith("-cover-letter.pdf")
                or filename.endswith("-report.pdf"))
        if "] cover letter - " in caption:
            kinds["cover letter"] += 1
        elif "] report - " in caption:
            kinds["report"] += 1
    assert kinds == {"cover letter": drafted, "report": drafted}
    # both documents really landed under artifacts/<item_id>/
    assert list(artifacts_dir.glob("*/*-cover-letter.pdf"))
    assert list(artifacts_dir.glob("*/*-report.pdf"))
    assert record["cost_usd_total"] == round(0.002 * drafted, 6)

    # runs.jsonl: exactly one parseable telemetry line carrying artifacts counts
    lines = runs.read_text().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["counts"]["new"] == 4
    assert parsed["artifacts"]["published"] == 2 * drafted


def test_dry_run_pushes_nothing(tmp_path, jobhunt_config, real_ssot_path,
                               greenhouse_raw, lever_raw, ashby_raw,
                               fake_pdflatex):
    store = Store(tmp_path / "store.db")
    transport = FakeTransport()
    fetcher = _fetcher(store, greenhouse_raw, lever_raw, ashby_raw)

    record = run_pipeline(jobhunt_config, _sources(), SSOT.load(real_ssot_path),
                          store, options=RunOptions(dry_run=True),
                          drafter=FakeDrafter(), transport=transport,
                          fetcher=fetcher, runs_path=tmp_path / "runs.jsonl",
                          artifacts_dir=tmp_path / "artifacts",
                          runner=fake_pdflatex())
    store.close()
    # no digest AND no attachment publishes; rendering still happened locally
    assert transport.sent == []
    assert transport.sent_files == []
    assert record["push_sent"] is False
    assert record["artifacts"]["published"] == 0
    assert record["artifacts"]["letter_pdf"] >= 1
    assert record["artifacts"]["report_pdf"] >= 1


def test_draft_cap_bounds_drafts(tmp_path, jobhunt_config, real_ssot_path,
                                greenhouse_raw, lever_raw, ashby_raw,
                                fake_pdflatex):
    capped = dataclasses.replace(jobhunt_config, draft_cap=1)
    store = Store(tmp_path / "store.db")
    drafter = FakeDrafter()
    transport = FakeTransport()
    fetcher = _fetcher(store, greenhouse_raw, lever_raw, ashby_raw)

    record = run_pipeline(capped, _sources(), SSOT.load(real_ssot_path), store,
                          options=RunOptions(), drafter=drafter,
                          transport=transport, fetcher=fetcher,
                          runs_path=tmp_path / "runs.jsonl",
                          artifacts_dir=tmp_path / "artifacts",
                          runner=fake_pdflatex())
    store.close()
    assert len(drafter.calls) == 1
    assert record["counts"]["drafted"] == 1
    # the cap bounds artifacts + attachments too, not just drafts: one drafted
    # item yields one letter + one report and two attachment messages
    assert record["artifacts"]["letter_pdf"] == 1
    assert record["artifacts"]["report_pdf"] == 1
    assert len(transport.sent_files) == 2


def test_no_draft_skips_drafting(tmp_path, jobhunt_config, real_ssot_path,
                                greenhouse_raw, lever_raw, ashby_raw):
    store = Store(tmp_path / "store.db")
    drafter = FakeDrafter()
    transport = FakeTransport()
    fetcher = _fetcher(store, greenhouse_raw, lever_raw, ashby_raw)

    record = run_pipeline(jobhunt_config, _sources(), SSOT.load(real_ssot_path),
                          store, options=RunOptions(no_draft=True),
                          drafter=drafter, transport=transport,
                          fetcher=fetcher, runs_path=tmp_path / "runs.jsonl",
                          artifacts_dir=tmp_path / "artifacts")
    store.close()
    assert drafter.calls == []
    assert record["counts"]["drafted"] == 0
    # no drafts -> no artifacts rendered or published
    assert record["artifacts"] == {"letter_pdf": 0, "letter_txt": 0,
                                   "report_pdf": 0, "report_txt": 0,
                                   "published": 0, "publish_failed": 0}
    assert transport.sent_files == []


# -- anti-injection L1 enforcement (spec 6b): poisoned drafts must be held ---

def test_validation_failed_draft_is_held_not_surfaced_ready(
        tmp_path, jobhunt_config, real_ssot_path, greenhouse_raw, lever_raw,
        ashby_raw, fake_pdflatex):
    store = Store(tmp_path / "store.db")
    runs = tmp_path / "runs.jsonl"
    artifacts_dir = tmp_path / "artifacts"
    transport = FakeTransport()
    drafter = PartiallyPoisonedDrafter()
    # Of the 4 live listed postings across the 3 sources, exactly 2 (Senior
    # Backend Engineer @ 74, Machine Learning Engineer @ 77) clear the
    # jobhunt threshold (70) and become visible pending_review draft
    # candidates -- enough to exercise the held item next to a clean one.
    fetcher = _fetcher(store, greenhouse_raw, lever_raw, ashby_raw)

    record = run_pipeline(
        jobhunt_config, _sources(), SSOT.load(real_ssot_path), store,
        options=RunOptions(), drafter=drafter, transport=transport,
        fetcher=fetcher, runs_path=runs, artifacts_dir=artifacts_dir,
        runner=fake_pdflatex())

    # both above-threshold postings were drafted: one poisoned (held), one clean
    assert len(drafter.calls) == 2
    assert record["counts"]["validation_held"] == 1
    assert record["counts"]["drafted"] == 1

    # cost/usage accounting is preserved for the held draft too: generation
    # ran (and spent real tokens) for both calls, not just the clean one
    assert record["cost_usd_total"] == round(0.002 * 2, 6)
    assert record["usage_totals"]["input_tokens"] == 10 * 2

    # only the CLEAN item's artifacts were rendered and published; the
    # poisoned draft never reaches artifact rendering
    assert record["artifacts"]["letter_pdf"] == 1
    assert record["artifacts"]["report_pdf"] == 1
    assert len(transport.sent_files) == 2  # one letter + one report

    # the digest's ready bucket must not count the held item
    header = transport.sent[0][1].splitlines()[0]
    assert re.fullmatch(r"\d+ ready · \d+ manual · \d+ held · \d+ demoted today",
                        header)
    ready = int(header.split(" ready")[0])
    assert ready == 1

    # the held item is parked at awaiting_input, never attached material, and
    # carries the violation as the reason -- not a silent drop
    parked = [r for r in store.all_queue_rows() if r["state"] == "awaiting_input"]
    assert len(parked) == 1
    held_row = parked[0]
    assert not held_row["payload"].get("material")
    violations = held_row["payload"]["validation_violations"]
    assert violations
    assert violations[0]["code"] == "disallowed_url"
    store.close()

    # runs.jsonl telemetry line carries the same held count
    parsed = json.loads(runs.read_text().splitlines()[0])
    assert parsed["counts"]["validation_held"] == 1


def test_validation_ok_draft_flows_through_normally(
        tmp_path, jobhunt_config, real_ssot_path, greenhouse_raw,
        fake_pdflatex):
    # Control: an explicit validation_ok=True result is treated exactly like
    # the pre-existing default-clean path -- no item is held.
    store = Store(tmp_path / "store.db")
    runs = tmp_path / "runs.jsonl"
    transport = FakeTransport()

    class CleanDrafter:
        def __init__(self):
            self.calls = []

        def draft(self, posting, breakdown, ssot):
            self.calls.append(posting)
            return DraftResult(
                material="COVER LETTER\n\nFIELD DATA\nnotice_period: 1 month",
                usage={"input_tokens": 10, "output_tokens": 5, "cache_read": 1,
                       "cache_creation": 0},
                cost_usd=0.002, model="claude-sonnet-4-5", ok=True,
                validation_ok=True, validation_violations=[])

    drafter = CleanDrafter()
    fetcher = _fetcher(store, greenhouse_raw)

    record = run_pipeline(
        jobhunt_config, [Source("greenhouse", "acme", "Acme")],
        SSOT.load(real_ssot_path), store, options=RunOptions(),
        drafter=drafter, transport=transport, fetcher=fetcher,
        runs_path=runs, artifacts_dir=tmp_path / "artifacts",
        runner=fake_pdflatex())

    assert record["counts"]["validation_held"] == 0
    assert record["counts"]["drafted"] == len(drafter.calls)
    assert not any(r["state"] == "awaiting_input" for r in store.all_queue_rows())
    store.close()


def test_failed_fetch_source_never_closes(tmp_path, jobhunt_config,
                                         real_ssot_path, greenhouse_raw):
    store = Store(tmp_path / "store.db")
    ssot = SSOT.load(real_ssot_path)
    sources = [Source("greenhouse", "acme", "Acme")]
    runs = tmp_path / "runs.jsonl"

    # run 1: healthy fetch enqueues the backend role (scores >= threshold, visible)
    run_pipeline(jobhunt_config, sources, ssot, store,
                 options=RunOptions(no_draft=True), transport=FakeTransport(),
                 fetcher=_fetcher(store, greenhouse_raw), runs_path=runs,
                 artifacts_dir=tmp_path / "artifacts")
    before = [r for r in store.all_queue_rows()
              if r["visible"] and r["state"] == "pending_review"]
    assert before  # at least the backend role is visible

    # run 2: the same board 404s. A failed fetch is unavailability, not absence,
    # so close_absent must NOT run and the items stay visible and unclosed.
    clock = _Clock()
    fetcher = HttpFetcher(store, opener=_ErrorOpener(404), sleep=clock.sleep,
                          clock=clock)
    run_pipeline(jobhunt_config, sources, ssot, store,
                 options=RunOptions(no_draft=True), transport=FakeTransport(),
                 fetcher=fetcher, runs_path=runs,
                 artifacts_dir=tmp_path / "artifacts")

    after = {r["item_id"]: r for r in store.all_queue_rows()}
    for row in before:
        current = after[row["item_id"]]
        assert current["visible"] == 1
        assert not current["payload"].get("closed")
    store.close()


def test_rescore_refreshes_carryover_score_breakdown_and_ledger(
        tmp_path, jobhunt_config, real_ssot_path, greenhouse_raw):
    # A carryover item (scored on an earlier run, stored score deliberately
    # stale) still live on today's board must be re-scored in place with the
    # CURRENT scorer, updating queue score + payload.breakdown + ledger score.
    store = Store(tmp_path / "store.db")
    runs = tmp_path / "runs.jsonl"
    board = {"jobs": [greenhouse_raw["jobs"][0]]}  # Senior Backend Engineer only
    posting = GreenhouseAdapter().parse(board, "acme")[0]
    ikey = posting.identity_key()

    # seed the item as prior-run carryover with a stale score of 1
    store.record_ledger(ikey, "j-99", "greenhouse", "acme", posting.title,
                        posting.url, "seen", 1)
    store.upsert_queue(
        "j-99", ikey, "pending_review", None, 1, 1, "automatable",
        {"posting": {"title": posting.title, "company_slug": "acme",
                     "url": posting.url, "vendor": "greenhouse",
                     "locations": posting.locations, "remote_flag": False,
                     "comp": None, "unverified": False},
         "breakdown": {"total": 1, "matched": [], "weak": ["stale"],
                       "ats_warnings": []}})

    record = run_pipeline(
        jobhunt_config, [Source("greenhouse", "acme", "Acme")],
        SSOT.load(real_ssot_path), store,
        options=RunOptions(no_draft=True, rescore=True), transport=FakeTransport(),
        fetcher=_fetcher(store, board), runs_path=runs,
        artifacts_dir=tmp_path / "artifacts")

    fresh = Scorer(jobhunt_config,
                   profile_from_real_ssot(SSOT.load(real_ssot_path))).score(posting)
    assert fresh.total != 1  # the fresh score genuinely differs from the seed

    row = store.get_queue_row("j-99")
    assert row["score"] == fresh.total
    assert row["payload"]["breakdown"]["total"] == fresh.total
    assert row["payload"]["breakdown"]["matched"]      # real criteria, not "stale"
    assert "stale" not in row["payload"]["breakdown"]["weak"]

    ledger = store._conn.execute(
        "SELECT score FROM ledger WHERE identity_key=?", (ikey,)).fetchone()
    assert ledger["score"] == fresh.total

    assert record["counts"]["rescored"] == 1
    parsed = json.loads(runs.read_text().splitlines()[0])
    assert parsed["counts"]["rescored"] == 1
    store.close()


def test_rescore_off_by_default_records_zero(tmp_path, jobhunt_config,
                                             real_ssot_path, greenhouse_raw,
                                             lever_raw, ashby_raw):
    store = Store(tmp_path / "store.db")
    runs = tmp_path / "runs.jsonl"
    record = run_pipeline(
        jobhunt_config, _sources(), SSOT.load(real_ssot_path), store,
        options=RunOptions(no_draft=True), transport=FakeTransport(),
        fetcher=_fetcher(store, greenhouse_raw, lever_raw, ashby_raw),
        runs_path=runs, artifacts_dir=tmp_path / "artifacts")
    store.close()
    assert record["counts"]["rescored"] == 0
    parsed = json.loads(runs.read_text().splitlines()[0])
    assert parsed["counts"]["rescored"] == 0


# -- W4-COMMUTE-GATE run.py integration --------------------------------------

def test_discarded_posting_never_enqueued_and_ledger_records_discarded(
        tmp_path, jobhunt_config):
    store = Store(tmp_path / "store.db")
    runs = tmp_path / "runs.jsonl"
    raw = {"jobs": [{
        "id": 9001,
        "title": "Senior Backend Engineer",
        "updated_at": "2026-06-20T14:30:00-04:00",
        "first_published": "2026-06-12T09:00:00-04:00",
        "location": {"name": "Milan, Italy"},
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/9001",
        "content": "Backend role in Python. On-site 5 days per week in the "
                  "office, no remote option.",
    }]}
    # FIXTURE policy values (never real owner values): a Europe cap of 1
    # day/week, no allowed cities, so the 5-days/week Milan role is discarded.
    ssot = SSOT({"preferences": {"onsite_policy": {
        "allowed_cities": ["Testville"],
        "max_onsite_days_per_week_europe": 1,
        "max_onsite_days_per_month_rest": 4,
    }}})

    record = run_pipeline(
        jobhunt_config, [Source("greenhouse", "acme", "Acme")], ssot, store,
        options=RunOptions(no_draft=True), transport=FakeTransport(),
        fetcher=_fetcher(store, raw), runs_path=runs,
        artifacts_dir=tmp_path / "artifacts")

    assert record["counts"]["new"] == 1
    assert record["counts"]["enqueued"] == 0
    assert record["counts"]["discarded"] == 1
    # a true removal: the item never enters the queue at all
    assert store.all_queue_rows() == []

    posting = GreenhouseAdapter().parse(raw, "acme")[0]
    row = store._conn.execute(
        "SELECT status FROM ledger WHERE identity_key=?",
        (posting.identity_key(),)).fetchone()
    assert row["status"] == "discarded"
    store.close()

    parsed = json.loads(runs.read_text().splitlines()[0])
    assert parsed["counts"]["discarded"] == 1


def _run_with_attach_mode(tmp_path, config, real_ssot_path, raws, runner,
                          mode):
    store = Store(tmp_path / "store.db")
    transport = FakeTransport()
    fetcher = _fetcher(store, *raws)
    tuned = dataclasses.replace(config, attach_mode=mode)
    record = run_pipeline(tuned, _sources(), SSOT.load(real_ssot_path), store,
                          options=RunOptions(), drafter=FakeDrafter(),
                          transport=transport, fetcher=fetcher,
                          runs_path=tmp_path / "runs.jsonl",
                          artifacts_dir=tmp_path / "artifacts",
                          runner=runner)
    store.close()
    return record, transport


def test_attach_mode_bundle_publishes_single_zip(tmp_path, jobhunt_config,
                                                real_ssot_path, greenhouse_raw,
                                                lever_raw, ashby_raw,
                                                fake_pdflatex):
    record, transport = _run_with_attach_mode(
        tmp_path, jobhunt_config, real_ssot_path,
        (greenhouse_raw, lever_raw, ashby_raw), fake_pdflatex(), "bundle")
    drafted = record["counts"]["drafted"]
    assert drafted >= 2  # a bundle is only meaningful with several PDFs
    assert record["artifacts"]["letter_pdf"] == drafted
    assert record["artifacts"]["report_pdf"] == drafted
    assert record["artifacts"]["published"] == 1  # one zip (both docs), once
    assert len(transport.sent) == 1               # digest still exactly once
    assert len(transport.sent_files) == 1
    _topic, _path, _caption, filename = transport.sent_files[0]
    assert filename.endswith(".zip")


def test_attach_mode_none_publishes_no_files(tmp_path, jobhunt_config,
                                            real_ssot_path, greenhouse_raw,
                                            lever_raw, ashby_raw,
                                            fake_pdflatex):
    record, transport = _run_with_attach_mode(
        tmp_path, jobhunt_config, real_ssot_path,
        (greenhouse_raw, lever_raw, ashby_raw), fake_pdflatex(), "none")
    drafted = record["counts"]["drafted"]
    assert len(transport.sent) == 1                 # digest only
    assert transport.sent_files == []               # zero attachments
    assert record["artifacts"]["published"] == 0
    assert record["artifacts"]["letter_pdf"] == drafted   # still rendered
    assert record["artifacts"]["report_pdf"] == drafted


def test_render_failure_falls_back_to_txt_attachments(tmp_path, jobhunt_config,
                                                     real_ssot_path,
                                                     greenhouse_raw, lever_raw,
                                                     ashby_raw, fake_pdflatex):
    # pdflatex fails for every item -> each drafted item ships letter + report
    # as .txt fallbacks, both published
    record, transport = _run_with_attach_mode(
        tmp_path, jobhunt_config, real_ssot_path,
        (greenhouse_raw, lever_raw, ashby_raw), fake_pdflatex(False), "per_item")
    drafted = record["counts"]["drafted"]
    assert record["artifacts"] == {"letter_pdf": 0, "letter_txt": drafted,
                                   "report_pdf": 0, "report_txt": drafted,
                                   "published": 2 * drafted, "publish_failed": 0}
    assert len(transport.sent_files) == 2 * drafted
    letters = sum(f.endswith("-cover-letter.txt")
                  for _t, _p, _c, f in transport.sent_files)
    reports = sum(f.endswith("-report.txt")
                  for _t, _p, _c, f in transport.sent_files)
    assert letters == drafted and reports == drafted


class _FlakyTransport(FakeTransport):
    """Publishes normally except raising on one scripted attachment call."""

    def __init__(self, fail_on_call_index):
        super().__init__()
        self._fail_on = fail_on_call_index
        self._calls = 0

    def publish_file(self, topic, path, message, filename):
        self._calls += 1
        if self._calls == self._fail_on:
            raise RuntimeError("simulated attachment publish failure")
        super().publish_file(topic, path, message, filename)


def _backend_board(greenhouse_raw):
    """Greenhouse board trimmed to just the Senior Backend Engineer (visible)."""
    return {"jobs": [greenhouse_raw["jobs"][0]]}


class _FakeQueue:
    def __init__(self, items):
        self._items = items

    def items(self):
        return self._items


def _fieldmap_item(item_id, vendor, score):
    return QueueItem(item_id=item_id, identity_key=item_id, state="pending_review",
                     prev_state=None, score=score, visible=True,
                     channel="automatable", payload={"posting": {"vendor": vendor}})


def test_fieldmap_targets_spreads_across_vendors_not_all_greenhouse():
    # Round-1 finding: a plain global top-N was all-greenhouse whenever
    # greenhouse out-scored the other vendors, so ashby/lever never got
    # exercised. cap == vendor-count must yield exactly one per vendor.
    items = [_fieldmap_item("gh1", "greenhouse", 90),
             _fieldmap_item("gh2", "greenhouse", 85),
             _fieldmap_item("gh3", "greenhouse", 80),
             _fieldmap_item("gh4", "greenhouse", 75),
             _fieldmap_item("as1", "ashby", 70),
             _fieldmap_item("lv1", "lever", 60)]
    discovered_index = {i.identity_key: object() for i in items}

    targets = _fieldmap_targets(_FakeQueue(items), discovered_index, 3)

    vendors = [item.payload["posting"]["vendor"] for item, _ in targets]
    assert len(targets) == 3
    assert set(vendors) == {"greenhouse", "ashby", "lever"}
    # score order within each vendor: greenhouse's best (gh1) wins its slot
    assert [item.item_id for item, _ in targets] == ["gh1", "as1", "lv1"]


def test_fieldmap_targets_falls_back_to_global_fill_when_vendor_short():
    # ashby/lever only have one candidate each; the leftover cap headroom
    # must fall back to the next-highest-scoring greenhouse items rather than
    # leaving slots unfilled.
    items = [_fieldmap_item("gh1", "greenhouse", 90),
             _fieldmap_item("gh2", "greenhouse", 85),
             _fieldmap_item("gh3", "greenhouse", 80),
             _fieldmap_item("gh4", "greenhouse", 75),
             _fieldmap_item("as1", "ashby", 70),
             _fieldmap_item("lv1", "lever", 60)]
    discovered_index = {i.identity_key: object() for i in items}

    targets = _fieldmap_targets(_FakeQueue(items), discovered_index, 5)

    ids = [item.item_id for item, _ in targets]
    assert len(ids) == 5
    assert set(ids) == {"gh1", "gh2", "as1", "lv1", "gh3"}


def test_fieldmap_targets_ignores_non_visible_and_non_automatable_and_unsupported():
    hidden = _fieldmap_item("hidden", "greenhouse", 99)
    hidden.visible = False
    other_channel = _fieldmap_item("manual", "greenhouse", 98)
    other_channel.channel = "manual"
    unsupported_vendor = _fieldmap_item("wf1", "workable", 97)
    ok = _fieldmap_item("gh1", "greenhouse", 50)
    items = [hidden, other_channel, unsupported_vendor, ok]
    discovered_index = {i.identity_key: object() for i in items}

    targets = _fieldmap_targets(_FakeQueue(items), discovered_index, 10)

    assert [item.item_id for item, _ in targets] == ["gh1"]


def test_capture_fieldmaps_off_by_default_records_zero(tmp_path, jobhunt_config,
                                                       real_ssot_path,
                                                       greenhouse_raw, lever_raw,
                                                       ashby_raw):
    store = Store(tmp_path / "store.db")
    runs = tmp_path / "runs.jsonl"
    record = run_pipeline(
        jobhunt_config, _sources(), SSOT.load(real_ssot_path), store,
        options=RunOptions(no_draft=True), transport=FakeTransport(),
        fetcher=_fetcher(store, greenhouse_raw, lever_raw, ashby_raw),
        runs_path=runs, artifacts_dir=tmp_path / "artifacts")
    store.close()
    assert record["fieldmaps"] == {"captured": 0, "cached": 0, "failed": 0}
    parsed = json.loads(runs.read_text().splitlines()[0])
    assert parsed["fieldmaps"] == {"captured": 0, "cached": 0, "failed": 0}


def test_capture_fieldmaps_captures_and_attaches_coverage(
        tmp_path, jobhunt_config, real_ssot_path, greenhouse_raw,
        greenhouse_questions_raw):
    store = Store(tmp_path / "store.db")
    runs = tmp_path / "runs.jsonl"
    board = _backend_board(greenhouse_raw)
    record = run_pipeline(
        jobhunt_config, [Source("greenhouse", "acme", "Acme")],
        SSOT.load(real_ssot_path), store,
        options=RunOptions(no_draft=True, capture_fieldmaps=5),
        transport=FakeTransport(), fetcher=_fetcher(store, board),
        capture_opener=_CaptureOpener(greenhouse_questions_raw),
        runs_path=runs, artifacts_dir=tmp_path / "artifacts")

    assert record["fieldmaps"] == {"captured": 1, "cached": 0, "failed": 0}

    # the visible greenhouse item now carries a one-line coverage summary
    row = [r for r in store.all_queue_rows()
           if r["payload"].get("posting", {}).get("vendor") == "greenhouse"][0]
    summary = row["payload"]["fieldmap_coverage"]
    assert summary == "5 answerable, 1 missing, 1 manual-only of 7 required"

    # the field map persisted under vendor+posting_id+updated_at
    stored = store.get_fieldmap("greenhouse", "5501001",
                                "2026-06-20T14:30:00-04:00")
    assert stored is not None
    assert stored["body"]["vendor"] == "greenhouse"
    store.close()

    parsed = json.loads(runs.read_text().splitlines()[0])
    assert parsed["fieldmaps"]["captured"] == 1


def test_capture_fieldmaps_reuses_cache_on_second_run(
        tmp_path, jobhunt_config, real_ssot_path, greenhouse_raw,
        greenhouse_questions_raw):
    store = Store(tmp_path / "store.db")
    runs = tmp_path / "runs.jsonl"
    board = _backend_board(greenhouse_raw)
    ssot = SSOT.load(real_ssot_path)
    source = [Source("greenhouse", "acme", "Acme")]

    first = run_pipeline(
        jobhunt_config, source, ssot, store,
        options=RunOptions(no_draft=True, capture_fieldmaps=5),
        transport=FakeTransport(), fetcher=_fetcher(store, board),
        capture_opener=_CaptureOpener(greenhouse_questions_raw),
        runs_path=runs, artifacts_dir=tmp_path / "artifacts")
    assert first["fieldmaps"] == {"captured": 1, "cached": 0, "failed": 0}

    # same board + same updated_at -> cache hit, no recapture
    opener = _CaptureOpener(greenhouse_questions_raw)
    second = run_pipeline(
        jobhunt_config, source, ssot, store,
        options=RunOptions(no_draft=True, capture_fieldmaps=5),
        transport=FakeTransport(), fetcher=_fetcher(store, board),
        capture_opener=opener, runs_path=runs,
        artifacts_dir=tmp_path / "artifacts")
    store.close()
    assert second["fieldmaps"] == {"captured": 0, "cached": 1, "failed": 0}
    assert opener.calls == 0  # a cache hit performs zero GETs


def test_capture_fieldmaps_only_greenhouse(tmp_path, jobhunt_config,
                                           real_ssot_path, greenhouse_raw,
                                           lever_raw, ashby_raw,
                                           greenhouse_questions_raw):
    store = Store(tmp_path / "store.db")
    record = run_pipeline(
        jobhunt_config, _sources(), SSOT.load(real_ssot_path), store,
        options=RunOptions(no_draft=True, capture_fieldmaps=10),
        transport=FakeTransport(),
        fetcher=_fetcher(store, greenhouse_raw, lever_raw, ashby_raw),
        capture_opener=_CaptureOpener(greenhouse_questions_raw),
        runs_path=tmp_path / "runs.jsonl", artifacts_dir=tmp_path / "artifacts")

    captured_plus_cached = (record["fieldmaps"]["captured"]
                            + record["fieldmaps"]["cached"])
    assert captured_plus_cached >= 1
    for row in store.all_queue_rows():
        vendor = row["payload"].get("posting", {}).get("vendor")
        has_summary = "fieldmap_coverage" in row["payload"]
        if vendor == "greenhouse" and row["visible"]:
            continue  # greenhouse visible items may carry a summary
        assert not (has_summary and vendor in ("lever", "ashby"))
    store.close()


def test_capture_fieldmaps_is_fail_soft_per_item(tmp_path, jobhunt_config,
                                                 real_ssot_path, greenhouse_raw,
                                                 lever_raw, ashby_raw):
    # The capture GET raises for every item; the run still completes and the
    # failures are counted, never fatal (W4 3.3 fail-soft).
    store = Store(tmp_path / "store.db")
    runs = tmp_path / "runs.jsonl"
    record = run_pipeline(
        jobhunt_config, _sources(), SSOT.load(real_ssot_path), store,
        options=RunOptions(no_draft=True, capture_fieldmaps=10),
        transport=FakeTransport(),
        fetcher=_fetcher(store, greenhouse_raw, lever_raw, ashby_raw),
        capture_opener=_FailingCaptureOpener(),
        runs_path=runs, artifacts_dir=tmp_path / "artifacts")
    store.close()
    assert record["fieldmaps"]["captured"] == 0
    assert record["fieldmaps"]["cached"] == 0
    assert record["fieldmaps"]["failed"] >= 1
    # the rest of the record is intact (the run did not abort)
    assert record["counts"]["enqueued"] == 4


def test_capture_fieldmaps_routes_ashby_to_browse_capture(
        tmp_path, jobhunt_config, real_ssot_path, ashby_raw, monkeypatch):
    # An ashby item must dispatch to browse.capture_ashby (NOT the greenhouse
    # HTTP path). A fake stands in for the browser capture so no playwright or
    # network is touched; threshold=0 guarantees the ashby posting is visible.
    import engine.browse as browse
    from engine.fieldmap import Field, FieldMap, Locator

    calls = []

    def fake_capture_ashby(slug, job_id, browser_factory=None):
        calls.append((slug, job_id))
        return FieldMap(
            vendor="ashby", posting_id=job_id, captured_at="2026-07-03T00:00:00+00:00",
            fields=[Field(key="_systemfield_name", label="Full name",
                          type="input_text", required=True, options=[],
                          source="ashby_graphql",
                          locator=Locator(role="textbox", name="Full name"),
                          step_index=0, conditional_on=None)])

    monkeypatch.setattr(browse, "capture_ashby", fake_capture_ashby)

    tuned = dataclasses.replace(jobhunt_config, threshold=0)
    store = Store(tmp_path / "store.db")
    record = run_pipeline(
        tuned, [Source("ashby", "initech", "Initech")],
        SSOT.load(real_ssot_path), store,
        options=RunOptions(no_draft=True, capture_fieldmaps=5),
        transport=FakeTransport(), fetcher=_fetcher(store, ashby_raw),
        runs_path=tmp_path / "runs.jsonl", artifacts_dir=tmp_path / "artifacts")

    # the browser capture ran for the listed ashby posting (slug + job_id routed)
    assert calls == [("initech", "f7e6d5c4-aaaa-bbbb-cccc-ddddeeeeffff")]
    assert record["fieldmaps"] == {"captured": 1, "cached": 0, "failed": 0}

    # the field map persisted under the ashby vendor + posting_id + updated_at key
    stored = store.get_fieldmap("ashby", "f7e6d5c4-aaaa-bbbb-cccc-ddddeeeeffff",
                                "2026-06-22T08:00:00.000Z")
    assert stored is not None and stored["body"]["vendor"] == "ashby"
    row = [r for r in store.all_queue_rows()
           if r["payload"].get("posting", {}).get("vendor") == "ashby"][0]
    assert "fieldmap_coverage" in row["payload"]
    store.close()


def test_attachment_publish_failure_is_fail_soft_and_recorded(
        tmp_path, jobhunt_config, real_ssot_path, greenhouse_raw, lever_raw,
        ashby_raw, fake_pdflatex):
    # Lower the threshold so every discovered posting is visible/drafted,
    # guaranteeing enough attachments for a mid-batch failure to have both a
    # predecessor and a successor (proving the rest still publish, W4 3.9).
    tuned = dataclasses.replace(jobhunt_config, threshold=0)
    store = Store(tmp_path / "store.db")
    transport = _FlakyTransport(fail_on_call_index=2)
    fetcher = _fetcher(store, greenhouse_raw, lever_raw, ashby_raw)

    record = run_pipeline(tuned, _sources(), SSOT.load(real_ssot_path), store,
                          options=RunOptions(), drafter=FakeDrafter(),
                          transport=transport, fetcher=fetcher,
                          runs_path=tmp_path / "runs.jsonl",
                          artifacts_dir=tmp_path / "artifacts",
                          runner=fake_pdflatex())
    store.close()

    drafted = record["counts"]["drafted"]
    assert drafted >= 3  # a predecessor and a successor around the failed call

    # the digest already went out; the mid-batch attachment failure never
    # aborted it, nor the remaining attachments. Two attachments per item, so
    # one failed call leaves (2 * drafted - 1) published.
    attachments = 2 * drafted
    assert len(transport.sent) == 1
    assert len(transport.sent_files) == attachments - 1
    assert record["artifacts"]["published"] == attachments - 1
    assert record["artifacts"]["publish_failed"] == 1
    # rendering itself is untouched by a downstream publish failure
    assert record["artifacts"]["letter_pdf"] == drafted
    assert record["artifacts"]["report_pdf"] == drafted


class _StubQueue:
    def __init__(self, items):
        self._items = items

    def items(self):
        return self._items


def _target_item(item_id, vendor, score, visible=True, channel="automatable"):
    return QueueItem(item_id=item_id, identity_key=f"k-{item_id}", state="pending_review",
                     prev_state=None, score=score, visible=visible, channel=channel,
                     payload={"posting": {"vendor": vendor}})


def test_fieldmap_targets_round_robin_spreads_vendors():
    """Round-1 live finding: global top-N starved ashby/lever; spread must not."""
    items = [_target_item("g1", "greenhouse", 90), _target_item("g2", "greenhouse", 80),
             _target_item("g3", "greenhouse", 70), _target_item("g4", "greenhouse", 60),
             _target_item("a1", "ashby", 85), _target_item("a2", "ashby", 75),
             _target_item("l1", "lever", 65),
             _target_item("hidden", "greenhouse", 99, visible=False),
             _target_item("manual", "lever", 99, channel="manual"),
             _target_item("offboard", "ashby", 99)]
    index = {f"k-{i}": object() for i in
             ("g1", "g2", "g3", "g4", "a1", "a2", "l1", "hidden", "manual")}
    picked = [item.item_id for item, _ in _fieldmap_targets(_StubQueue(items), index, 6)]
    assert set(picked) >= {"a1", "l1"}, "every vendor with candidates gets exercised"
    assert picked[0] == "g1" and "g2" in picked, "score order preserved within vendor"
    assert len(picked) == 6
    assert not {"hidden", "manual", "offboard"} & set(picked)


def test_fieldmap_targets_zero_cap_and_empty_pool():
    assert _fieldmap_targets(_StubQueue([]), {}, 6) == []
    assert _fieldmap_targets(_StubQueue([_target_item("g1", "greenhouse", 90)]),
                             {"k-g1": object()}, 0) == []


# -- report-data assembly (W4 4c criterion 4) --------------------------------

def _drafted_stub(job_id=None, updated_ts=None):
    return {
        "item_id": "j-1", "title": "Senior Backend Engineer", "company": "Acme",
        "material": "body", "lang": "en", "lang_rationale": "english default",
        "job_id": job_id, "updated_ts": updated_ts,
        "posting": {"vendor": "greenhouse", "company_slug": "acme",
                    "title": "Senior Backend Engineer",
                    "url": "https://x.invalid/j/1", "locations": ["London, UK"]},
        "breakdown": {"total": 72, "matched": ["role: Senior Backend Engineer"],
                      "weak": ["comp unknown"],
                      "ats_warnings": ["may fail ATS: missing work_authorization"]},
    }


def test_assemble_report_data_uses_canned_surface_without_fieldmap(
        tmp_path, jobhunt_config, real_ssot_path):
    store = Store(tmp_path / "store.db")
    ssot = SSOT.load(real_ssot_path)
    profile = profile_from_real_ssot(ssot)

    report = _assemble_report_data(jobhunt_config, _drafted_stub(), store, ssot,
                                   profile)
    store.close()

    assert report["posting"]["company"] == "Acme"
    assert report["posting"]["score"] == 72
    assert report["coverage"]["summary"] == "no field map captured for this posting"
    # the canned-answers surface carries identity + canned_answers fields
    fields = {row["field"]: row["value"] for row in report["field_data"]}
    assert fields["Email"] == "test.candidate@example.invalid"
    assert fields["canned: notice_period"] == "1 month"
    # one score row per configured axis, weights from the config
    assert {r["axis"] for r in report["score_rows"]} == set(jobhunt_config.axes)


def test_assemble_report_data_reuses_captured_fieldmap_coverage(
        tmp_path, jobhunt_config, real_ssot_path):
    from engine.fieldmap import Field, FieldMap, Locator

    store = Store(tmp_path / "store.db")
    ssot = SSOT.load(real_ssot_path)
    profile = profile_from_real_ssot(ssot)
    fieldmap = FieldMap(
        vendor="greenhouse", posting_id="555",
        captured_at="2026-07-03T00:00:00+00:00",
        fields=[
            Field(key="email", label="Email", type="input_text", required=True,
                  options=[], source="questions",
                  locator=Locator("textbox", "Email"), step_index=0,
                  conditional_on=None),
            Field(key="resume", label="Resume", type="input_file", required=True,
                  options=[], source="questions",
                  locator=Locator("button", "Resume"), step_index=0,
                  conditional_on=None),
        ])
    store.put_fieldmap("greenhouse", "555", "2026-06-20T14:30:00-04:00",
                       fieldmap.to_dict(), fieldmap.captured_at)

    drafted = _drafted_stub(job_id="555", updated_ts="2026-06-20T14:30:00-04:00")
    report = _assemble_report_data(jobhunt_config, drafted, store, ssot, profile)
    store.close()

    fields = {row["field"]: row["value"] for row in report["field_data"]}
    assert fields["Email"] == "test.candidate@example.invalid"   # answerable
    assert fields["Resume"] == "(manual-only: file-upload)"      # never auto-filled
    assert report["coverage"]["summary"].endswith("of 2 required")


def test_build_letter_header_marks_absent_fields_missing(real_ssot_path):
    header = _build_letter_header(SSOT.load(real_ssot_path))
    assert header["full_name"] == "Test Candidate"      # identity.name fallback
    assert header["subtitle"] == "Computational Scientist"
    assert header["email"] == "test.candidate@example.invalid"
    assert header["website"] == "https://example.invalid"  # links.site fallback
    # the synthetic SSOT lacks phone + linkedin -> grounding marker, not invented
    assert header["phone"] == "[MISSING: identity.phone]"
    assert header["linkedin"] == "[MISSING: links.linkedin]"
