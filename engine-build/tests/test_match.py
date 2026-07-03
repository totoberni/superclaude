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


# -- location eligibility (calibration wave) -----------------------------------

def _posting(locations, *, remote_flag=False, title="Engineer", description=""):
    return Posting(vendor="x", company_slug="c", job_id="1", title=title,
                   locations=locations, remote_flag=remote_flag, comp=None,
                   posted_ts=None, updated_ts=None, url="https://x/1",
                   description=description)


def test_location_us_only_remote_is_weak_not_full(jobhunt_config):
    # The live-run bug: a US-only "remote" role scored location_fit 1.0 as if
    # EU-eligible. It must now be 0.4 with an eligibility caveat.
    scorer = Scorer(jobhunt_config, {"locations": ["lisbon", "italy", "eu"],
                                     "remote_ok": True})
    breakdown = scorer.score(_posting(["Remote - United States"],
                                      remote_flag=True))
    assert breakdown.axis_scores["location_fit"] == 0.4
    assert any("non-EU eligibility likely" in w for w in breakdown.weak)


def test_location_us_state_code_only_remote_is_weak(jobhunt_config):
    scorer = Scorer(jobhunt_config, {"locations": ["lisbon"], "remote_ok": True})
    breakdown = scorer.score(_posting(["Remote (San Francisco, CA)"],
                                      remote_flag=True))
    assert breakdown.axis_scores["location_fit"] == 0.4


def test_location_eu_marked_remote_is_full(jobhunt_config):
    scorer = Scorer(jobhunt_config, {"locations": ["lisbon"], "remote_ok": True})
    breakdown = scorer.score(_posting(["Remote - EU"], remote_flag=True))
    assert breakdown.axis_scores["location_fit"] == 1.0
    assert any("EU-eligible" in m for m in breakdown.matched)


def test_location_bare_remote_is_unverified(jobhunt_config):
    scorer = Scorer(jobhunt_config, {"locations": ["lisbon"], "remote_ok": True})
    breakdown = scorer.score(_posting(["Remote"], remote_flag=True))
    assert breakdown.axis_scores["location_fit"] == 0.7
    assert any("eligibility unverified" in w for w in breakdown.weak)


def test_location_italy_city_direct_match_is_full(jobhunt_config):
    scorer = Scorer(jobhunt_config, {"locations": ["lisbon", "italy"],
                                     "remote_ok": True})
    breakdown = scorer.score(_posting(["Lisbon, Portugal"], remote_flag=False))
    assert breakdown.axis_scores["location_fit"] == 1.0
    assert any("location: Lisbon, Portugal" in m for m in breakdown.matched)


def test_location_non_remote_non_matching_stays_low(jobhunt_config):
    scorer = Scorer(jobhunt_config, {"locations": ["lisbon"], "remote_ok": True})
    breakdown = scorer.score(_posting(["New York, NY"], remote_flag=False))
    assert breakdown.axis_scores["location_fit"] == 0.3


# -- seniority word boundaries + title vs description --------------------------

def test_seniority_title_match_is_full(jobhunt_config):
    scorer = Scorer(jobhunt_config, {"seniority": ["senior", "mid"]})
    breakdown = scorer.score(_posting(["Remote"], title="Senior Backend Engineer",
                                      description="We build data services."))
    assert breakdown.axis_scores["seniority_fit"] == 1.0
    assert any("seniority: senior" in m for m in breakdown.matched)


def test_seniority_description_only_is_partial(jobhunt_config):
    scorer = Scorer(jobhunt_config, {"seniority": ["senior"]})
    breakdown = scorer.score(_posting(
        ["Remote"], title="Backend Engineer",
        description="We are hiring a senior engineer for this team."))
    assert breakdown.axis_scores["seniority_fit"] == 0.6
    assert any("seniority only in description" in w for w in breakdown.weak)


def test_seniority_word_boundary_avoids_substring_false_match(jobhunt_config):
    # 'intern' must not fire inside 'internal'/'international' (live-run bug).
    scorer = Scorer(jobhunt_config, {"seniority": ["intern"]})
    breakdown = scorer.score(_posting(
        ["Remote"], title="Backend Engineer",
        description="Collaborate with international and internal teams."))
    assert breakdown.axis_scores["seniority_fit"] == 0.5
    assert any("seniority unclear" in w for w in breakdown.weak)
