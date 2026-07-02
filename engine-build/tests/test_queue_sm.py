import pytest

from engine.config import Config
from engine.discover import Posting
from engine.match import ScoreBreakdown
from engine.queue_sm import InvalidTransition, QueueStateMachine


def _config(buffer_size=2, threshold=50, terminal_state="submitted"):
    return Config(
        name="t", topic="abe-t", id_prefix="j-", threshold=threshold,
        buffer_size=buffer_size, terminal_state=terminal_state, ssot="job.yaml",
        axes={"role_fit": 1.0}, ats_rules=[], automatable_vendors=("greenhouse",),
    )


def _posting(job_id, title):
    return Posting(
        vendor="greenhouse", company_slug="acme", job_id=job_id, title=title,
        locations=["London"], remote_flag=False, comp=None, posted_ts=None,
        updated_ts=None, url=f"https://x/{job_id}",
    )


def _breakdown(total):
    return ScoreBreakdown(total=total, axis_scores={}, matched=["role: x"])


def _set_score(store, item_id, score):
    row = store.get_queue_row(item_id)
    store.upsert_queue(item_id, row["identity_key"], row["state"],
                      row["prev_state"], score, row["visible"], row["channel"],
                      row["payload"])


def test_enqueue_prefixed_id_and_pending_review(store):
    sm = QueueStateMachine(store, _config())
    item_id = sm.enqueue(_posting("1", "Backend Engineer"), _breakdown(80))
    assert item_id == "j-1"
    assert sm.get(item_id).state == "pending_review"


def test_invalid_transition_rejected(store):
    sm = QueueStateMachine(store, _config())
    item_id = sm.enqueue(_posting("1", "Role"), _breakdown(80))
    with pytest.raises(InvalidTransition):
        sm.transition(item_id, "submitted")


def test_below_threshold_item_is_held(store):
    sm = QueueStateMachine(store, _config(buffer_size=50, threshold=70))
    sm.enqueue(_posting("1", "Weak Role"), _breakdown(40))
    result = sm.rerank()
    assert result.held == 1
    assert result.demoted_today == []


def test_capacity_demotion_and_promotion_round_trip(store):
    sm = QueueStateMachine(store, _config(buffer_size=2, threshold=50))
    ids = [sm.enqueue(_posting(str(i), f"Role {i}"), _breakdown(score))
           for i, score in enumerate([90, 80, 70])]
    first = sm.rerank()
    assert first.demoted_today == [ids[2]]
    assert not sm.get(ids[2]).visible

    _set_score(store, ids[2], 99)
    sm.rerank()
    assert sm.get(ids[2]).visible
    assert sm.get(ids[2]).state == "pending_review"


def test_park_and_resume_restores_prev_state(store):
    sm = QueueStateMachine(store, _config())
    item_id = sm.enqueue(_posting("1", "Role"), _breakdown(80))
    sm.park(item_id, reason="missing notice_period")
    assert sm.get(item_id).state == "awaiting_input"
    resumed = sm.resume(item_id)
    assert resumed == "pending_review"
    assert sm.get(item_id).state == "pending_review"


def test_blacklist_sticks(store):
    sm = QueueStateMachine(store, _config())
    posting = _posting("1", "Spam Role")
    item_id = sm.enqueue(posting, _breakdown(80))
    sm.blacklist(item_id, reason="manual")
    assert sm.get(item_id).state == "blacklisted"
    assert store.is_known(posting.identity_key())


def test_non_submitting_automation_seals_submit_path(store):
    # phd/papers terminate at pending_review (7.5): approved and everything
    # downstream must be rejected, not merely absent from the happy path.
    sm = QueueStateMachine(store, _config(terminal_state="pending_review"))
    item_id = sm.enqueue(_posting("1", "PhD position"), _breakdown(80))
    with pytest.raises(InvalidTransition):
        sm.transition(item_id, "approved")
    assert sm.get(item_id).state == "pending_review"


def test_jobhunt_submit_path_still_open(store):
    sm = QueueStateMachine(store, _config())
    item_id = sm.enqueue(_posting("1", "Backend Engineer"), _breakdown(80))
    sm.transition(item_id, "approved")
    sm.transition(item_id, "submitting")
    sm.transition(item_id, "submitted")
    assert sm.get(item_id).state == "submitted"


def test_cannot_park_terminal_item(store):
    sm = QueueStateMachine(store, _config())
    item_id = sm.enqueue(_posting("1", "Role"), _breakdown(80))
    sm.blacklist(item_id, reason="manual")
    with pytest.raises(InvalidTransition):
        sm.park(item_id)
