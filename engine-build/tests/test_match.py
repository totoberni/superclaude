import pytest

from engine.discover import GreenhouseAdapter, LeverAdapter, Posting
from engine.match import (
    Scorer,
    TokenOverlapSimilarity,
    Vec0Similarity,
    _max_amount,
    owner_band_from_years,
    parse_required_band,
    profile_from_ssot,
    skills_overlap_sub,
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


def test_senior_backend_no_overlap_drops_below_threshold(jobhunt_config,
                                                        job_ssot_path,
                                                        greenhouse_raw):
    # AUDIT REGRESSION (was 77 under the old additive matcher): a Senior role
    # far above the owner's entry level must now collapse via the seniority
    # GATE (gap 2 -> 0.15 multiplier) and land well below threshold, even with
    # strong skill overlap and a good location. This is the whole point of the
    # gated-multiplicative redesign: a fatal seniority mismatch can no longer be
    # averaged away by role_fit + location.
    scorer = Scorer(jobhunt_config, _profile(job_ssot_path))
    backend = _greenhouse_postings(greenhouse_raw)["Senior Backend Engineer"]
    breakdown = scorer.score(backend)
    assert breakdown.total < jobhunt_config.threshold
    assert breakdown.matched  # top matched criteria still present (7.3)
    assert any("over-level" in w for w in breakdown.weak)


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
    # warning, never hidden or demoted. The ats_precheck warning is appended to
    # ats_warnings and never subtracted from the total. Under the gated model a
    # "strong match" must be ENTRY-level and in-family (a Senior title would now
    # collapse via the seniority gate), so the posting is an entry AI/ML role
    # with real skill overlap that still clears threshold despite the ATS flag.
    scorer = Scorer(jobhunt_config, _profile(job_ssot_path))
    strong = Posting(
        vendor="greenhouse", company_slug="acme", job_id="99",
        title="Machine Learning Engineer",
        locations=["Remote - EU"], remote_flag=True, comp="90000",
        posted_ts=None, updated_ts=None, url="https://x/99",
        description=("Entry-level AI/ML role using python pytorch sql docker. "
                     "Requires an active security clearance."),
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

def _posting(locations, *, remote_flag=False, title="Machine Learning Engineer",
             description=""):
    # Default title is in-family (tier 1 AI/ML) so the family gate does not
    # discard: these helpers isolate the location / commute gates, and a bare
    # "Engineer" title would now collapse the whole score via the family gate.
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


# -- seniority parser + gate (W4 redesign; replaces the old seniority_fit axis) -
# The old model scored seniority as a soft_fit axis derived from the owner's OWN
# target-role names (self-inflation). The redesign makes seniority a GATE: the
# posting's required band (title + "N+ years") vs the owner's experience-derived
# band; the gap drives an over-level multiplier and, when large, a discard.

def _seniority_cfg(config):
    return config.scoring["seniority"]


def _entry_owner_profile():
    # ~1.7 yrs of experience anchors the owner at the entry band (0); NEVER
    # derived from role names anymore.
    return {"experience_years": 1.7,
            "skill_tokens": ["python", "pytorch", "sql", "docker"]}


def test_seniority_parser_reads_title_level_keywords(jobhunt_config):
    cfg = _seniority_cfg(jobhunt_config)
    assert parse_required_band("Machine Learning Engineer", "", cfg) == 0
    assert parse_required_band("Junior Data Scientist", "", cfg) == 0
    assert parse_required_band("Mid-level ML Engineer", "", cfg) == 1
    assert parse_required_band("Senior Backend Engineer", "", cfg) == 2
    assert parse_required_band("Staff Research Engineer", "", cfg) == 2
    assert parse_required_band("Principal Engineer", "", cfg) == 3
    assert parse_required_band("Director of AI", "", cfg) == 3


def test_seniority_parser_reads_years_phrasing(jobhunt_config):
    cfg = _seniority_cfg(jobhunt_config)
    # "N+ years" / "N-M years" / "minimum N years" map through the same year
    # thresholds (mid>=2, senior>=5, principal>=8) as the title keywords.
    assert parse_required_band("Engineer", "1 year of experience", cfg) == 0
    assert parse_required_band("Engineer", "3+ years of experience", cfg) == 1
    assert parse_required_band("Engineer", "5+ years building systems", cfg) == 2
    assert parse_required_band("Engineer", "3-5 years required", cfg) == 1
    assert parse_required_band("Engineer", "minimum 8 years", cfg) == 3
    # the max of the keyword band and the years band wins.
    assert parse_required_band("Junior Engineer", "8+ years", cfg) == 3


def test_owner_band_from_experience_years_is_entry(jobhunt_config):
    cfg = _seniority_cfg(jobhunt_config)
    assert owner_band_from_years(1.7, cfg) == 0     # the owner (~1.7 yrs)
    assert owner_band_from_years(None, cfg) == 0    # unknown -> entry
    assert owner_band_from_years(3.0, cfg) == 1
    assert owner_band_from_years(6.0, cfg) == 2


def test_seniority_gate_entry_role_fits_entry_owner(jobhunt_config):
    scorer = Scorer(jobhunt_config, _entry_owner_profile())
    breakdown = scorer.score(_posting(
        ["Remote - EU"], remote_flag=True, title="Machine Learning Engineer",
        description="Entry-level role building models in python and pytorch."))
    assert breakdown.discard is False
    assert any("level fits" in m for m in breakdown.matched)


def test_seniority_gate_senior_role_penalized_and_warned(jobhunt_config):
    # gap 2 (owner entry vs senior posting) -> steep 0.15 multiplier + warn,
    # not a discard: it stays surfaced but scores far below threshold.
    scorer = Scorer(jobhunt_config, _entry_owner_profile())
    breakdown = scorer.score(_posting(
        ["Remote - EU"], remote_flag=True, title="Senior ML Engineer",
        description="Senior role building models in python and pytorch."))
    assert breakdown.discard is False
    assert breakdown.total < jobhunt_config.threshold
    assert any("over-level" in w for w in breakdown.weak)


def test_seniority_gate_principal_role_discards(jobhunt_config):
    # gap 3 >= discard_gap -> a true removal with a reason.
    scorer = Scorer(jobhunt_config, _entry_owner_profile())
    breakdown = scorer.score(_posting(
        ["Remote - EU"], remote_flag=True, title="Principal ML Engineer",
        description="Principal role, minimum 8 years, in python and pytorch."))
    assert breakdown.discard is True
    assert "seniority" in breakdown.discard_reason


# -- W4-COMMUTE-GATE (7.3 D5): hard discard for excessive on-site presence -----

def _onsite_ssot(allowed_cities, week_cap, month_cap):
    return SSOT({"preferences": {"location_policy": {
        "allowed_cities": allowed_cities,
        "max_onsite_days_per_week_europe": week_cap,
        "max_onsite_days_per_month_rest": month_cap,
    }}})


def test_commute_gate_inactive_when_policy_missing(jobhunt_config):
    # No ssot passed at all -> gate off (current-behaviour default).
    scorer = Scorer(jobhunt_config, {})
    breakdown = scorer.score(_posting(
        ["Somewhere, Overthere"], description="5 days per week in office"))
    assert breakdown.discard is False
    assert breakdown.discard_reason == ""

    # ssot passed but preferences.location_policy absent -> still off.
    scorer_empty = Scorer(jobhunt_config, {}, ssot=SSOT({}))
    breakdown_empty = scorer_empty.score(_posting(
        ["Somewhere, Overthere"], description="5 days per week in office"))
    assert breakdown_empty.discard is False

    # ssot with a PARTIAL policy (missing a required key) -> still off.
    partial = SSOT({"preferences": {"location_policy": {
        "allowed_cities": ["Testville"],
        "max_onsite_days_per_week_europe": 1,
    }}})
    scorer_partial = Scorer(jobhunt_config, {}, ssot=partial)
    breakdown_partial = scorer_partial.score(_posting(
        ["Somewhere, Overthere"], description="5 days per week in office"))
    assert breakdown_partial.discard is False


def test_commute_gate_allowed_city_passes_full_time_onsite(jobhunt_config):
    # Rule 2 (allowed city) bypasses the amount check entirely: any on-site
    # amount, including full-time, is fine in an allowed city.
    ssot = _onsite_ssot(["Testville"], 1, 4)
    scorer = Scorer(jobhunt_config, {}, ssot=ssot)
    breakdown = scorer.score(_posting(
        ["Testville, Nowhereland"],
        description="Full-time on-site role, 5 days per week in the office."))
    assert breakdown.discard is False
    assert breakdown.discard_reason == ""


def test_commute_gate_europe_discards_over_weekly_cap(jobhunt_config):
    ssot = _onsite_ssot(["Testville"], 1, 4)
    scorer = Scorer(jobhunt_config, {}, ssot=ssot)
    breakdown = scorer.score(_posting(
        ["Milan, Italy"], description="Hybrid, 3 days per week in office."))
    assert breakdown.discard is True
    assert "3" in breakdown.discard_reason
    assert "week" in breakdown.discard_reason


def test_commute_gate_europe_passes_within_weekly_cap(jobhunt_config):
    ssot = _onsite_ssot(["Testville"], 1, 4)
    scorer = Scorer(jobhunt_config, {}, ssot=ssot)
    breakdown = scorer.score(_posting(
        ["Milan, Italy"], description="Hybrid, 1 day per week in office."))
    assert breakdown.discard is False


def test_commute_gate_non_europe_passes_within_monthly_cap(jobhunt_config):
    ssot = _onsite_ssot(["Testville"], 1, 4)
    scorer = Scorer(jobhunt_config, {}, ssot=ssot)
    breakdown = scorer.score(_posting(
        ["Austin, TX"], description="2 days per month on-site."))
    assert breakdown.discard is False


def test_commute_gate_non_europe_discards_weekly_converted_over_monthly_cap(
        jobhunt_config):
    # 3 days/week * 4.33 wk/mo = 12.99 days/mo, over the 4 days/mo cap.
    ssot = _onsite_ssot(["Testville"], 1, 4)
    scorer = Scorer(jobhunt_config, {}, ssot=ssot)
    breakdown = scorer.score(_posting(
        ["Austin, TX"], description="3 days per week on-site."))
    assert breakdown.discard is True
    assert "non-Europe" in breakdown.discard_reason


def test_commute_gate_fully_remote_non_europe_passes(jobhunt_config):
    ssot = _onsite_ssot(["Testville"], 1, 4)
    scorer = Scorer(jobhunt_config, {}, ssot=ssot)
    breakdown = scorer.score(_posting(
        ["Austin, TX"], remote_flag=True, description="Fully remote position."))
    assert breakdown.discard is False
    assert breakdown.discard_reason == ""


def test_commute_gate_ambiguous_amount_warns_not_discards(jobhunt_config):
    # Required on-site presence (not remote, not an allowed city) but no
    # detectable day count: show-and-warn (D5), never a guessed discard.
    ssot = _onsite_ssot(["Testville"], 1, 4)
    scorer = Scorer(jobhunt_config, {}, ssot=ssot)
    breakdown = scorer.score(_posting(
        ["Milan, Italy"], description="This is an on-site role in our office."))
    assert breakdown.discard is False
    assert breakdown.discard_reason == ""
    assert any("on-site presence unclear" in w for w in breakdown.ats_warnings)


def test_commute_gate_italian_phrasing_detection(jobhunt_config):
    # "N giorni in ufficio" (Italian, implicit weekly cadence) must be parsed
    # the same as its English equivalent.
    ssot = _onsite_ssot(["Testville"], 1, 4)
    scorer = Scorer(jobhunt_config, {}, ssot=ssot)
    breakdown = scorer.score(_posting(
        ["Milan, Italy"],
        description="Richiediamo 3 giorni in ufficio a settimana."))
    assert breakdown.discard is True
    assert "3" in breakdown.discard_reason


# -- skills canonical-token overlap + floor (W4 redesign; the detection fix) ----

def test_skills_overlap_fraction_of_detected_required(jobhunt_config):
    cfg = jobhunt_config.scoring["skills"]
    # candidate holds python+pytorch; the posting names python, pytorch AND sql
    # -> 2 of the 3 detected required skills -> 2/3, matched list is the overlap.
    sub, matched = skills_overlap_sub(
        ["python", "pytorch"], "We use Python, PyTorch and SQL daily.", cfg)
    assert matched == ["python", "pytorch"]  # skills_overlap_sub returns sorted
    assert abs(sub - 2 / 3) < 1e-9


def test_skills_overlap_no_named_skill_is_neutral(jobhunt_config):
    cfg = jobhunt_config.scoring["skills"]
    sub, matched = skills_overlap_sub(
        ["python"], "A generalist role with no named tools.", cfg)
    assert matched == []
    assert sub == cfg["no_required_neutral"]


def test_skills_floor_caps_soft_fit_below_threshold(jobhunt_config):
    # A role whose required skills the candidate lacks falls below the floor, so
    # soft_fit is hard-capped: no other axis can lift it over threshold. This is
    # the "no real overlap cannot clear threshold" guarantee.
    floor = jobhunt_config.scoring["skills"]["floor"]
    scorer = Scorer(jobhunt_config,
                    {"experience_years": 1.7, "skill_tokens": ["java", "go"]})
    breakdown = scorer.score(_posting(
        ["Remote - EU"], remote_flag=True, title="Machine Learning Engineer",
        description="Build ML systems in python, pytorch, cuda and sql."))
    assert breakdown.axis_scores["skills_overlap"] < floor
    assert any("below floor" in w for w in breakdown.weak)
    assert breakdown.total < jobhunt_config.threshold


# -- family tiering (W4 redesign; the gate that replaces naive role_fit) --------

def test_family_tier1_ai_outscores_equivalent_tier2_swe(jobhunt_config):
    # All else equal (same entry level, same skills, same location), an AI/ML
    # role (tier 1, 1.0) must outscore an equivalent SWE role (tier 2, 0.75).
    scorer = Scorer(jobhunt_config, _entry_owner_profile())
    ai = scorer.score(_posting(
        ["Remote - EU"], remote_flag=True, title="Machine Learning Engineer",
        description="Build models in python and pytorch."))
    swe = scorer.score(_posting(
        ["Remote - EU"], remote_flag=True, title="Backend Software Engineer",
        description="Build services in python and pytorch."))
    assert ai.discard is False and swe.discard is False
    assert ai.total > swe.total


def test_out_of_family_role_is_discarded(jobhunt_config):
    scorer = Scorer(jobhunt_config, _entry_owner_profile())
    breakdown = scorer.score(_posting(
        ["Milan, Italy"], title="Registered Nurse",
        description="Provide bedside patient care on the hospital ward."))
    assert breakdown.discard is True
    assert "role family out of scope" in breakdown.discard_reason


# -- audit regression cases (spec: these bad cases must now score correctly) ----

def test_senior_ai_engineer_no_overlap_below_threshold(jobhunt_config):
    # AUDIT case (was 72): a Senior AI role whose named skills the candidate
    # lacks. BOTH gates bite: seniority gap 2 (0.15) and the skills floor.
    scorer = Scorer(jobhunt_config,
                    {"experience_years": 1.7, "skill_tokens": ["java"]})
    breakdown = scorer.score(_posting(
        ["Remote - EU"], remote_flag=True, title="Senior AI Engineer",
        description="Senior AI role. Requires python, pytorch and cuda."))
    assert breakdown.total < jobhunt_config.threshold


def test_rome_five_days_onsite_is_discarded(jobhunt_config):
    # AUDIT case: a non-allowed-city (Rome) role demanding 5 on-site days/week
    # over the owner's 2-day Europe cap is a commute DISCARD, not a 66 score.
    ssot = _onsite_ssot(["Milan", "Bologna"], 2, 4)
    scorer = Scorer(jobhunt_config, _entry_owner_profile(), ssot=ssot)
    breakdown = scorer.score(_posting(
        ["Rome, Italy"], title="Machine Learning Engineer",
        description="On-site 5 days per week in our Rome office."))
    assert breakdown.discard is True
    assert "commute" in breakdown.discard_reason


def test_us_role_warns_on_sponsorship(jobhunt_config):
    # AUDIT case: a US role the EU-only owner cannot take without sponsorship
    # must WARN (the old region-dict truthiness bug asserted US rights falsely).
    scorer = Scorer(jobhunt_config, _entry_owner_profile())
    breakdown = scorer.score(_posting(
        ["New York, NY"], remote_flag=False, title="Machine Learning Engineer",
        description=("ML role. Must be authorized to work in the United States "
                     "with python and pytorch.")))
    assert any("visa sponsorship" in w for w in breakdown.weak)


def test_entry_ml_remote_fixed_term_with_overlap_scores_high(jobhunt_config):
    # POSITIVE case (spec): entry AI/ML, EU-remote, fixed-term, real overlap ->
    # high. The gated model rewards exactly the bridge shape the owner wants.
    scorer = Scorer(jobhunt_config, _entry_owner_profile())
    breakdown = scorer.score(_posting(
        ["Remote - EU"], remote_flag=True, title="Machine Learning Engineer",
        description=("Entry-level ML engineer, 12-month fixed-term contract, "
                     "EU-remote. Build models in python, pytorch and docker.")))
    assert breakdown.discard is False
    assert breakdown.total >= jobhunt_config.threshold
    assert any("skills:" in m for m in breakdown.matched)
