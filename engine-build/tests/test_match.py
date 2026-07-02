import pytest

from engine.discover import GreenhouseAdapter, LeverAdapter, Posting
from engine.match import (
    Scorer,
    TokenOverlapSimilarity,
    Vec0Similarity,
    _max_amount,
    profile_from_ssot,
)
from engine.ssot import SSOT


def _profile(job_ssot_path):
    return profile_from_ssot(SSOT.load(job_ssot_path))


def _greenhouse_postings(greenhouse_raw):
    postings = GreenhouseAdapter().parse(greenhouse_raw, "acme")
    return {p.title: p for p in postings}


def test_token_overlap_similarity():
    sim = TokenOverlapSimilarity()
    assert sim.score("backend engineer", "Senior Backend Engineer") == 1.0
    assert sim.score("", "anything") == 0.0
    assert 0.0 < sim.score("machine learning engineer", "Security Engineer") < 1.0


def test_vec0_similarity_is_a_stub():
    with pytest.raises(NotImplementedError):
        Vec0Similarity().score("a", "b")


def test_backend_scores_above_threshold_with_breakdown(jobhunt_config,
                                                      job_ssot_path,
                                                      greenhouse_raw):
    scorer = Scorer(jobhunt_config, _profile(job_ssot_path))
    backend = _greenhouse_postings(greenhouse_raw)["Senior Backend Engineer"]
    breakdown = scorer.score(backend)
    assert breakdown.total == 77
    assert breakdown.total >= jobhunt_config.threshold
    assert breakdown.matched  # top matched criteria present (7.3)
    assert any("comp unknown" in w for w in breakdown.weak)


def test_ats_precheck_warns_but_does_not_hide(jobhunt_config, job_ssot_path,
                                             greenhouse_raw):
    scorer = Scorer(jobhunt_config, _profile(job_ssot_path))
    security = _greenhouse_postings(greenhouse_raw)["Security Engineer"]
    breakdown = scorer.score(security)
    assert breakdown.total < jobhunt_config.threshold
    assert any("missing clearance" in w for w in breakdown.ats_warnings)


def test_high_scorer_that_trips_ats_is_not_suppressed(jobhunt_config,
                                                     job_ssot_path):
    # D5 guarantee: a strong match failing a hard ATS filter is surfaced WITH a
    # warning, never hidden or demoted. The warning must not touch the score.
    scorer = Scorer(jobhunt_config, _profile(job_ssot_path))
    strong = Posting(
        vendor="greenhouse", company_slug="acme", job_id="99",
        title="Senior Backend Engineer",
        locations=["Remote"], remote_flag=True, comp="90000",
        posted_ts=None, updated_ts=None, url="https://x/99",
        description=("Senior backend role using python typescript sqlite pytorch "
                     "sql. Requires an active security clearance."),
    )
    breakdown = scorer.score(strong)
    assert breakdown.total >= jobhunt_config.threshold
    assert any("missing clearance" in w for w in breakdown.ats_warnings)


def test_scorer_rejects_unimplemented_axes(phd_config, job_ssot_path):
    # phd/papers axis functions are a later deliverable (plan 7.3): fail fast.
    with pytest.raises(ValueError):
        Scorer(phd_config, _profile(job_ssot_path))


def test_scores_lever_posting_with_dict_salary_range_without_raising(
        jobhunt_config, job_ssot_path, lever_raw):
    # Regression: live Lever's dict-shaped salaryRange used to reach
    # _max_amount as a dict and crash on comp.replace(); the adapter now
    # normalizes it, so scoring must complete and credit the comp match.
    scorer = Scorer(jobhunt_config, _profile(job_ssot_path))
    posting = LeverAdapter().parse(lever_raw, "globex")[0]
    breakdown = scorer.score(posting)
    assert any("comp:" in m for m in breakdown.matched)


def test_max_amount_handles_dict_shaped_comp():
    assert _max_amount({"min": 50000, "max": 70000}) == 70000
    assert _max_amount({"min": 50000}) == 50000
    assert _max_amount({}) is None
    assert _max_amount(None) is None
