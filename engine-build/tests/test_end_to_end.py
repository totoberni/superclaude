"""End-to-end dry run (W3 gate).

fixtures -> discover -> ledger dedup -> match + threshold -> queue re-rank ->
report text render -> fake-notify capture. Asserts digest counts, score
breakdown presence, the structural no-repeat guarantee, and the park/resume
round trip. No network anywhere (the autouse no_network fixture blocks sockets).
"""

import socket

import pytest

from engine.discover import (
    AshbyAdapter,
    GreenhouseAdapter,
    LeverAdapter,
    run_discovery,
)
from engine.match import Scorer, profile_from_ssot
from engine.notify import FakeTransport, publish_digest
from engine.queue_sm import QueueStateMachine
from engine.questionnaire import Questionnaire
from engine.ssot import SSOT

REQUIRED_TO_SUBMIT = ["preferences.notice_period"]


def _sources(greenhouse_raw, lever_raw, ashby_raw):
    return [
        (GreenhouseAdapter(), greenhouse_raw, "acme"),
        (LeverAdapter(), lever_raw, "globex"),
        (AshbyAdapter(), ashby_raw, "initech"),
    ]


def _run_daily_pass(sources, store, scorer, sm):
    """discover -> score -> enqueue -> rerank. Returns (new_postings, rerank)."""
    new_postings = run_discovery(sources, store)
    for posting in new_postings:
        sm.enqueue(posting, scorer.score(posting))
    return new_postings, sm.rerank()


def test_socket_is_blocked():
    with pytest.raises(RuntimeError):
        socket.socket()


def test_end_to_end_dry_run(store, jobhunt_config, job_ssot_path,
                           greenhouse_raw, lever_raw, ashby_raw):
    sources = _sources(greenhouse_raw, lever_raw, ashby_raw)
    ssot = SSOT.load(job_ssot_path)
    scorer = Scorer(jobhunt_config, profile_from_ssot(ssot))
    sm = QueueStateMachine(store, jobhunt_config)

    # -- stage A: first daily pass -----------------------------------------
    new_postings, rerank = _run_daily_pass(sources, store, scorer, sm)
    assert len(new_postings) == 4  # unlisted ashby dropped by liveness

    # score breakdown presence: every enqueued item carries matched criteria.
    assert all(item.payload["breakdown"]["matched"] for item in sm.items())

    transport = FakeTransport()
    message = publish_digest(transport, jobhunt_config.topic, sm.items(),
                            len(rerank.demoted_today))
    header = message.splitlines()[1]
    # New gated model: of the 4 enqueued, only the entry-level Machine Learning
    # Engineer (lever) clears threshold and is READY; the Senior Backend Engineer,
    # the out-of-family Security Engineer, and the tier-2 Product Engineer all
    # fall below threshold and are HELD (demoted, not visible).
    assert header == "**1 ready** · 0 manual · 3 held · 0 demoted today"
    assert "**Match:**" in message  # breakdown reaches the report line
    assert transport.sent == [(jobhunt_config.topic, message)]

    # -- stage B: structural no-repeat -------------------------------------
    repeat_postings, _ = _run_daily_pass(sources, store, scorer, sm)
    assert repeat_postings == []

    # -- stage C: park / resume round trip ---------------------------------
    target = next(i for i in sm.items()
                  if i.state == "pending_review" and i.channel == "automatable")
    questionnaire = Questionnaire(store, sm, job_ssot_path)
    raised = questionnaire.for_missing_required(ssot, target.item_id,
                                               REQUIRED_TO_SUBMIT)
    assert len(raised) == 1
    assert sm.get(target.item_id).state == "awaiting_input"

    result = questionnaire.apply_reply(raised[0], "1 month")
    assert result.resumed_state == "pending_review"
    assert sm.get(target.item_id).state == "pending_review"
    assert SSOT.load(job_ssot_path).get("preferences.notice_period") == "1 month"
