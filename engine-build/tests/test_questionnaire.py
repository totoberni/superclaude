import os
import time

import yaml

from engine.discover import Posting
from engine.match import ScoreBreakdown
from engine.queue_sm import QueueStateMachine
from engine.questionnaire import Questionnaire
from engine.ssot import SSOT


def _enqueue(store, config):
    sm = QueueStateMachine(store, config)
    posting = Posting(
        vendor="greenhouse", company_slug="acme", job_id="1",
        title="Backend Engineer", locations=["London"], remote_flag=False,
        comp=None, posted_ts=None, updated_ts=None, url="https://x/1",
    )
    item_id = sm.enqueue(posting, ScoreBreakdown(total=80, axis_scores={}))
    return sm, item_id


def test_raise_mandatory_parks_and_carries_id(store, jobhunt_config,
                                             job_ssot_path):
    sm, item_id = _enqueue(store, jobhunt_config)
    q = Questionnaire(store, sm, job_ssot_path)
    qitem = q.raise_mandatory(item_id, "preferences.notice_period",
                             "What is your notice period?")
    assert qitem.item_id == item_id
    assert qitem.blocking is True
    assert sm.get(item_id).state == "awaiting_input"


def test_for_missing_required_parks_only_missing(store, jobhunt_config,
                                                job_ssot_path):
    sm, item_id = _enqueue(store, jobhunt_config)
    q = Questionnaire(store, sm, job_ssot_path)
    ssot = SSOT.load(job_ssot_path)
    items = q.for_missing_required(
        ssot, item_id, ["identity.email", "preferences.notice_period"])
    assert [i.field_path for i in items] == ["preferences.notice_period"]


def test_apply_reply_writes_ssot_and_resumes(store, jobhunt_config,
                                            job_ssot_path):
    sm, item_id = _enqueue(store, jobhunt_config)
    q = Questionnaire(store, sm, job_ssot_path)
    qitem = q.raise_mandatory(item_id, "preferences.notice_period", "notice?")
    result = q.apply_reply(qitem, "1 month")
    assert result.resumed_state == "pending_review"
    assert sm.get(item_id).state == "pending_review"
    reloaded = yaml.safe_load(job_ssot_path.read_text())
    assert reloaded["preferences"]["notice_period"] == "1 month"


def test_unconfirmed_reply_leaves_item_parked(store, jobhunt_config,
                                             job_ssot_path):
    sm, item_id = _enqueue(store, jobhunt_config)
    q = Questionnaire(store, sm, job_ssot_path)
    qitem = q.raise_mandatory(item_id, "preferences.notice_period", "notice?")
    result = q.apply_reply(qitem, "1 month", confirmed=False)
    assert result.resumed_state is None
    assert sm.get(item_id).state == "awaiting_input"
    assert SSOT.load(job_ssot_path).is_missing("preferences.notice_period")


def test_refine_gate_only_emits_on_yes(store, jobhunt_config, job_ssot_path):
    sm, _ = _enqueue(store, jobhunt_config)
    q = Questionnaire(store, sm, job_ssot_path)
    prompts = {"preferences.german": "Willing to learn German?"}
    assert q.refine_gate(False, prompts) == []
    items = q.refine_gate(True, prompts)
    assert len(items) == 1
    assert items[0].blocking is False


def test_staleness_detection(store, jobhunt_config, job_ssot_path):
    sm, _ = _enqueue(store, jobhunt_config)
    q = Questionnaire(store, sm, job_ssot_path)
    assert q.is_stale() is False
    old = time.time() - 10 * 86400
    os.utime(job_ssot_path, (old, old))
    assert q.is_stale() is True
