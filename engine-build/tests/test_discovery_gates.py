"""W5.1e: the work-auth gate (channel 5) and the role gate (channel 6).

The defect these pin: a SOFT SCORING AXIS IS NOT A GATE. Work authorization was a
0.10-weight axis that could subtract at most 8 points from 100, so a job the owner
CANNOT LEGALLY TAKE still reached the top of his digest, affirmatively labelled
"eligibility: EU work rights". Constraints that are BINARY IN REALITY (the right to
work; a role the owner will not take) must be DISCARD CHANNELS, never weights.

Each test below names the ONE guard it pins, so reverting that guard alone fails a
named test (mutation discipline). No test asserts a composition of several guards
where a single guard would do.
"""

import re
from pathlib import Path

import pytest

from engine.kernel.discover_base import Posting
from engine.match import Scorer, posting_regions, unplaceable_fragments
from engine.profile_map import profile_from_real_ssot, sponsorship_by_region
from engine.queue_sm import QueueStateMachine
from engine.run import _rescore_carryover
from engine.ssot import SSOT

ROOT = Path(__file__).parents[1]

# The owner's real axes (ruling 9), as the SSOT states them. Injected through the
# profile so no test needs the owner's private SSOT file: the engine must NEVER
# carry this as a default, and a scorer built without it leaves the gate inactive.
OWNER_AXES = {"eu": False, "ch": False, "uk": True, "us": True, "ca": True}


def _profile(**over):
    profile = {
        "roles": ["machine learning engineer"],
        "skill_tokens": ["python", "pytorch"],
        "experience_years": 1.7,
        "locations": ["milan", "italy"],
        "remote_ok": True,
        "sponsorship_required_by_region": dict(OWNER_AXES),
    }
    profile.update(over)
    return profile


def _posting(locations, *, title="Machine Learning Engineer", description="",
             remote_flag=False, slug="acme", company_name=None):
    return Posting(vendor="greenhouse", company_slug=slug, job_id="1",
                   title=title, locations=locations, remote_flag=remote_flag,
                   comp=None, posted_ts=None, updated_ts=None,
                   url="https://example.invalid/1", description=description,
                   company_name=company_name)


# -- GUARD 1: the UK is not the EU (the load-bearing split) --------------------
# While "london" sat in _EU_CITIES, EVERY other fix was defeated: a UK role was
# affirmatively credited with EU work rights and could not be gated on anything.

def test_uk_is_not_an_eu_work_eligibility_region():
    assert posting_regions(_posting(["London, England, United Kingdom"])) == {"uk"}
    assert posting_regions(_posting(["Manchester"])) == {"uk"}
    assert posting_regions(_posting(["Milan, Italy"])) == {"eu"}
    # The Republic of Ireland is the EU; Northern Ireland is the UK.
    assert posting_regions(_posting(["Dublin, Ireland"])) == {"eu"}
    assert posting_regions(_posting(["Belfast, Northern Ireland"])) == {"uk"}


def test_uk_onsite_role_is_gated_not_credited_with_eu_rights(jobhunt_config):
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(["London, England, United Kingdom"]))
    assert breakdown.discard is True
    assert "uk" in breakdown.discard_reason
    assert not any("EU work rights" in m for m in breakdown.matched)


# -- GUARD 2: a remote role still has a work-eligibility geography -------------
# `_location_needs_sponsorship` used to return False for ANY remote posting, so
# "Remote - US" was scored as if the owner could take it.

def test_remote_does_not_erase_the_postings_geography(jobhunt_config):
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(["Remote - US"], remote_flag=True))
    assert breakdown.discard is True
    assert "us" in breakdown.discard_reason
    assert not any("EU-eligible" in m for m in breakdown.matched)
    assert not any("EU work rights" in m for m in breakdown.matched)


def test_the_live_defect_j2793_shape_is_discarded(jobhunt_config):
    # The top-scoring item on the owner's real digest: remote, no EU location at
    # all (Canada or UK), and the engine told him "location: remote (EU-eligible)".
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(
        ["Remote, Canada; Remote, United Kingdom"],
        title="Senior Backend Engineer (Ruby), AI Engineering", remote_flag=True))
    assert breakdown.discard is True
    assert "ca" in breakdown.discard_reason and "uk" in breakdown.discard_reason
    assert not any("EU-eligible" in m for m in breakdown.matched)


# -- GUARD 3: the sponsorship-REFUSAL detector --------------------------------

def test_posting_that_refuses_sponsorship_is_discarded(jobhunt_config):
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(
        ["London, United Kingdom"],
        description="You must already have the existing right to work in the "
                    "United Kingdom. We are unable to sponsor visas."))
    assert breakdown.discard is True
    assert "refuses visa sponsorship" in breakdown.discard_reason


def test_refusal_beats_a_generic_company_sponsorship_blurb(jobhunt_config):
    # Live shape (n8n): "we can sponsor visas to Germany; for any other country,
    # you need to have existing right to work." A role-specific refusal must not
    # be overridden by a company-wide offer blurb.
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(
        ["Remote - US"], remote_flag=True,
        description="We can sponsor visas to Germany; for any other country you "
                    "need to have existing right to work."))
    assert breakdown.discard is True
    assert "refuses visa sponsorship" in breakdown.discard_reason


# -- GUARD 4: the sponsorship-OFFER detector ----------------------------------

def test_explicit_sponsorship_offer_admits_a_us_posting(jobhunt_config):
    # Anthropic's own JD says "We do sponsor visas!". A correct gate reads that as
    # a POSITIVE signal, not as noise. Admitted despite a sponsorship-required
    # region and an employer-agnostic check (this is NOT the allowlist path).
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(
        ["San Francisco"], slug="smallcorp", remote_flag=True,
        description="Visa sponsorship: We do sponsor visas! Come build with us."))
    assert breakdown.discard is False
    assert any("offers visa sponsorship" in m for m in breakdown.matched)


def test_offer_does_not_rescue_an_employer_that_is_silent(jobhunt_config):
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(["San Francisco"], slug="smallcorp",
                                      remote_flag=True))
    assert breakdown.discard is True
    assert "not a known sponsor" in breakdown.discard_reason


# -- GUARD 5: the SHARED sponsoring-employer allowlist (consumer 1: work auth) --

def test_uk_posting_at_a_fang_like_employer_is_warned_not_discarded(jobhunt_config):
    # Ruling 9's carve-out: a silent posting in a sponsorship-required region is
    # admitted as a WARNED candidate for a big employer known to sponsor.
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(["London, United Kingdom"], slug="google"))
    assert breakdown.discard is False
    assert any("needs visa sponsorship" in w and "known sponsoring employer" in w
               for w in breakdown.weak)


def test_the_allowlist_also_matches_the_company_name(jobhunt_config):
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(["New York City"], slug="careers-board",
                                      company_name="Microsoft"))
    assert breakdown.discard is False


# -- GUARD 6: the role gate (channel 6), and its allowlist consumer 2 ----------
# The family gate matches keywords over title + DESCRIPTION, so an AI-flavoured
# JD made ANY role read as tier-1 AI/ML: "Technical Recruiter, AI Research" scored
# 84, identical to "Machine Learning Engineer".

AI_FLAVOURED_JD = ("Join our AI research team building machine learning systems "
                   "in Python and PyTorch on large-scale model training.")


def test_ai_flavoured_description_cannot_promote_a_recruiter_title(jobhunt_config):
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(["Milan, Italy"],
                                      title="Technical Recruiter, AI Research",
                                      description=AI_FLAVOURED_JD))
    assert breakdown.discard is True
    assert "non-engineering role family" in breakdown.discard_reason


def test_ai_flavoured_title_suffix_cannot_promote_an_assistant_title(jobhunt_config):
    # No comma to hide behind: the head IS the whole title, and "AI Research Lead"
    # in it must not rescue an executive-assistant role.
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(
        ["Milan, Italy"], title="Executive Assistant to the AI Research Lead",
        description=AI_FLAVOURED_JD))
    assert breakdown.discard is True
    assert "non-engineering role family" in breakdown.discard_reason


def test_product_role_is_discarded_but_not_at_a_fang_like_employer(jobhunt_config):
    # Ruling 16: product/design are filtered OUT, WITH a carve-out for FANG-like
    # employers. Same allowlist as the work-auth carve-out: one list, two consumers.
    #
    # The title is deliberately "Product Designer", not "Product Manager": the word
    # MANAGER lands in the `principal` SENIORITY keyword list, so a Product Manager
    # is discarded by the seniority gate whatever the role gate does. That accident
    # of vocabulary is precisely why the audit found no role filter at all, and a
    # test that leaned on it would pin nothing.
    scorer = Scorer(jobhunt_config, _profile())
    small = scorer.score(_posting(["Milan, Italy"], slug="smallcorp",
                                  title="Product Designer, Safeguards",
                                  description=AI_FLAVOURED_JD))
    assert small.discard is True
    assert small.discard_reason == "non-engineering role family: designer"

    fang = scorer.score(_posting(["Milan, Italy"], slug="google",
                                 title="Product Designer, Safeguards",
                                 description=AI_FLAVOURED_JD))
    assert fang.discard is False
    assert any("FANG-like carve-out" in w for w in fang.weak)


def test_product_manager_is_caught_by_the_role_gate_not_just_by_luck(
        jobhunt_config):
    # The audit's real posting shape. It was ALREADY discarded, but only because
    # "Manager" happens to sit in the seniority keyword list; a "Product Lead" or
    # "Safeguards Generalist" sailed straight through. The role gate must name it
    # on its own terms, so the reason now carries BOTH causes.
    #
    # (At Anthropic itself the role gate would CARVE IT OUT, ruling 16, and only
    # the seniority accident would remain. The employer here is deliberately not
    # on the allowlist, so the role gate is the one under test.)
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(["San Francisco, CA"], slug="smallcorp",
                                      title="Product Manager, Safeguards",
                                      description=AI_FLAVOURED_JD))
    assert breakdown.discard is True
    assert "required seniority far above owner level" in breakdown.discard_reason
    assert "non-engineering role family: product manager" in breakdown.discard_reason


def test_engineering_noun_in_the_title_suffix_cannot_rescue_a_recruiter_head(
        jobhunt_config):
    # The title/description confusion, one level DOWN. The head is the ROLE; the
    # suffix is the SUBJECT MATTER. "Technical Recruiter, AI Engineering" is a
    # recruiter who recruits engineers. Classifying over the WHOLE title would let
    # "engineering" in the suffix override the recruiter head and admit it, which
    # is precisely the promotion this gate exists to stop.
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(
        ["Milan, Italy"], title="Technical Recruiter, AI Engineering",
        description=AI_FLAVOURED_JD))
    assert breakdown.discard is True
    assert breakdown.discard_reason == "non-engineering role family: recruiter"


def test_engineering_title_with_a_product_flavoured_description_is_admitted(
        jobhunt_config):
    # The negative that keeps the role gate honest: the DESCRIPTION is subject
    # matter, so a product-heavy JD must not remove a genuine engineering role.
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(
        ["Milan, Italy"], title="Machine Learning Engineer",
        description="Partner with product management on the product roadmap and "
                    "product design reviews for our ML platform."))
    assert breakdown.discard is False


def test_engineering_noun_in_the_head_survives_a_non_engineering_word(
        jobhunt_config):
    # "Safeguards Generalist" is not an engineering role; a "Generalist Software
    # Engineer" is. The override is scoped to the head segment.
    scorer = Scorer(jobhunt_config, _profile())
    assert scorer.score(_posting(["Milan, Italy"],
                                 title="Generalist Software Engineer",
                                 description=AI_FLAVOURED_JD)).discard is False
    assert scorer.score(_posting(["Milan, Italy"],
                                 title="Safeguards Generalist",
                                 description=AI_FLAVOURED_JD)).discard is True


# -- GUARD 7: the owner's facts come from the SSOT, and are never invented -----

def test_eu_posting_is_admitted_and_keeps_its_eu_credit(jobhunt_config):
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(["Milan, Italy"]))
    assert breakdown.discard is False
    assert any("free to work" in m for m in breakdown.matched)


def test_switzerland_is_free_to_work(jobhunt_config):
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(["Zurich, Switzerland"]))
    assert breakdown.discard is False
    assert breakdown.discard_reason == ""


def test_gate_is_inactive_without_the_owners_ssot_facts(jobhunt_config):
    # The engine NEVER invents the owner's immigration status. With no work-auth
    # facts the gate fails OPEN, exactly as a missing location_policy leaves the
    # commute gate inactive. This is the contract that keeps owner data in the
    # SSOT and out of the public engine.
    scorer = Scorer(jobhunt_config, _profile(sponsorship_required_by_region={}))
    breakdown = scorer.score(_posting(["Remote - US"], remote_flag=True))
    assert breakdown.discard is False


def test_unplaceable_geography_is_warned_never_discarded(jobhunt_config):
    # No silent widening: a bare "Remote" states no work-eligibility geography.
    # Absence of evidence is not evidence of impossibility, and a gate that
    # discards on silence empties the digest.
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(["Remote"], remote_flag=True))
    assert breakdown.discard is False
    assert any("region not determined" in w for w in breakdown.weak)


def test_sponsorship_by_region_reads_the_structured_ssot_fact(tmp_path):
    # The SSOT states it outright; the scorer must READ it, never re-derive it
    # from `work_authorization` prose (the path that silently produced an EMPTY
    # capabilities list and left the whole engine blind).
    path = tmp_path / "ssot.yaml"
    path.write_text(
        "work_authorization:\n"
        "  eu:\n"
        "    sponsorship_required: 'false'\n"
        "    status: 'EU citizen, full right to work'\n"
        "  uk:\n"
        "    sponsorship_required: 'true, skilled worker route'\n"
        "    status: 'no right to work'\n"
        "  us:\n"
        "    sponsorship_required: 'true'\n"
        "    status: 'no'\n")
    assert sponsorship_by_region(SSOT.load(path)) == {
        "eu": False, "uk": True, "us": True}


def test_sponsorship_by_region_falls_back_to_the_owners_canned_answer(tmp_path):
    path = tmp_path / "ssot.yaml"
    path.write_text(
        "canned_answers:\n"
        "  sponsorship_answer_by_region:\n"
        "    eu: 'No, I hold full EU work rights.'\n"
        "    uk: 'Yes, I would require visa sponsorship in the UK.'\n")
    assert sponsorship_by_region(SSOT.load(path)) == {"eu": False, "uk": True}


def test_disagreeing_sources_take_the_conservative_reading(tmp_path):
    # A false "needs sponsorship" costs one lead. A false "free to work" puts a
    # job the owner cannot legally take at the top of his digest.
    path = tmp_path / "ssot.yaml"
    path.write_text(
        "work_authorization:\n"
        "  uk:\n"
        "    sponsorship_required: 'false'\n"
        "canned_answers:\n"
        "  sponsorship_answer_by_region:\n"
        "    uk: 'Yes, I would require sponsorship.'\n")
    assert sponsorship_by_region(SSOT.load(path)) == {"uk": True}


def test_an_unstated_region_defaults_to_needing_sponsorship(jobhunt_config):
    # Israel is not a region the SSOT names. It must not therefore read as free.
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(["Tel Aviv, Israel"], slug="smallcorp"))
    assert breakdown.discard is True
    assert "other" in breakdown.discard_reason


# -- The commute gate keeps its GEOGRAPHIC notion of Europe -------------------

def test_uk_stays_geographic_europe_for_the_commute_gate(jobhunt_config,
                                                         tmp_path):
    # Removing the UK from _EU_COUNTRIES must NOT silently re-band every UK role
    # from the weekly European commute cap to the monthly rest-of-world one. The
    # commute gate asks "how far would he travel"; the work-auth gate asks "may he
    # work there". Conflating those two is the root confusion of this whole defect.
    path = tmp_path / "ssot.yaml"
    path.write_text(
        "preferences:\n"
        "  location_policy:\n"
        "    allowed_cities: ['Milan']\n"
        "    max_onsite_days_per_week_europe: 2\n"
        "    max_onsite_days_per_month_rest: 4\n")
    scorer = Scorer(jobhunt_config, _profile(), ssot=SSOT.load(path))
    # 2 days/week in London: within the EUROPEAN weekly cap. Under a rest-of-world
    # reading this would convert to ~8.7 days/month and blow the monthly cap of 4.
    breakdown = scorer.score(_posting(
        ["London, United Kingdom"], slug="google",
        description="Hybrid: 2 days per week in the office."))
    assert breakdown.discard is False
    assert breakdown.discard_reason == ""
    # ... and the gate is genuinely ACTIVE on this posting, which is what makes the
    # assertion above mean something: one more day a week in the same London office
    # trips the WEEKLY European cap of 2, by name.
    over_cap = scorer.score(_posting(
        ["London, United Kingdom"], slug="google",
        description="Hybrid: 3 days per week in the office."))
    assert over_cap.discard is True
    assert "on-site commute gate" in over_cap.discard_reason
    assert "days/week in Europe" in over_cap.discard_reason


# -- GUARD 8: ADMIT IF ANY REGION IS WORKABLE ---------------------------------
# The gate's whole semantics on a posting that names several places, and the half
# the suite never pinned. A location string may carry an EU city AFTER a US one
# ("San Francisco, Singapore, Amsterdam": 14% of the live board has this shape),
# and reading only the FIRST region deleted an Amsterdam role the owner can take.
# One test per real live shape; each FAILS on a first-match-wins classifier.

def test_a_workable_region_in_a_mixed_location_list_admits_the_posting(
        jobhunt_config):
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(["Milan, Italy", "New York, NY"]))
    assert breakdown.discard is False
    assert any("free to work (eu)" in m for m in breakdown.matched)


def test_a_comma_joined_fragment_keeps_every_region_it_names(jobhunt_config):
    # togetherai, live: an LLM inference engineer in Amsterdam, deleted as US-only.
    posting = _posting(["San Francisco, Singapore, Amsterdam "])
    assert posting_regions(posting) == {"us", "other", "eu"}
    breakdown = Scorer(jobhunt_config, _profile()).score(posting)
    assert breakdown.discard is False
    assert any("free to work (eu)" in m for m in breakdown.matched)


def test_a_comma_joined_us_uk_eu_fragment_is_admitted(jobhunt_config):
    # vercel, live.
    posting = _posting(
        ["Hybrid - San Francisco, New York City, London, Berlin"])
    assert posting_regions(posting) == {"us", "uk", "eu"}
    assert Scorer(jobhunt_config, _profile()).score(posting).discard is False


def test_a_comma_joined_uk_first_fragment_still_sees_the_eu_country(
        jobhunt_config):
    # vercel, live: the UK is named FIRST, so first-match-wins never reached
    # Germany. Note "northern ireland" is masked before the EU pass, but plain
    # "united kingdom" must not mask Germany out of the same fragment.
    posting = _posting(["Remote - United Kingdom, Germany"], remote_flag=True)
    assert posting_regions(posting) == {"uk", "eu"}
    assert Scorer(jobhunt_config, _profile()).score(posting).discard is False


def test_an_ampersand_joined_region_pair_is_admitted_on_the_free_one(
        jobhunt_config):
    # singlestore, live. The ampersand is not a separator; the SET is.
    posting = _posting(["United States & EMEA"])
    assert posting_regions(posting) == {"us", "eu"}
    assert Scorer(jobhunt_config, _profile()).score(posting).discard is False


def test_cambridge_massachusetts_is_not_a_uk_city(jobhunt_config):
    # The NEGATIVE that forbids the naive fix (splitting on the comma), which would
    # shatter a "City, XX" pair into a bare city and a bare state code. "cambridge"
    # is in no city list here, so this fragment is US on the state code alone; the
    # SHAPE that would break is a US state next to a city name Europe also uses,
    # and Birmingham/Alabama is the live one. Both discard, and no ordering decides
    # it: a comma-split reading would have kept only the leading city.
    scorer = Scorer(jobhunt_config, _profile())
    posting = _posting(["Cambridge, MA"], slug="smallcorp")
    assert posting_regions(posting) == {"us"}
    breakdown = scorer.score(posting)
    assert breakdown.discard is True
    assert "us" in breakdown.discard_reason
    assert "uk" not in breakdown.discard_reason

    shared = _posting(["Birmingham, AL"], slug="smallcorp")
    assert posting_regions(shared) == {"uk", "us"}
    assert scorer.score(shared).discard is True


def test_an_iso_country_code_cannot_make_a_european_city_american(
        jobhunt_config):
    # The US state-code rule fires on ",\s*DE" (Delaware), so "Berlin, DE" carries
    # a US signal. Berlin is still Berlin: the set holds both, and the free EU
    # region admits the posting.
    scorer = Scorer(jobhunt_config, _profile())
    posting = _posting(["Berlin, DE"])
    assert posting_regions(posting) == {"us", "eu"}
    assert scorer.score(posting).discard is False
    # The rescue only works if the city is IN the EU corpus: "MT" is Montana, so
    # "Valletta, MT" placed as US ONLY and discarded a job the owner may take.
    valletta = _posting(["Valletta, MT"])
    assert posting_regions(valletta) == {"us", "eu"}
    assert scorer.score(valletta).discard is False


def test_an_accented_city_name_is_still_placed(jobhunt_config):
    # Four live postings write it with the umlaut and a flag emoji. Unfolded, the
    # ASCII corpus list never matched and the posting placed nowhere at all.
    posting = _posting(["Düsseldorf, Germany \U0001F1E9\U0001F1EA"])
    assert posting_regions(posting) == {"eu"}
    assert posting_regions(_posting(["Düsseldorf"])) == {"eu"}
    breakdown = Scorer(jobhunt_config, _profile()).score(posting)
    assert breakdown.discard is False
    assert any("free to work (eu)" in m for m in breakdown.matched)


def test_a_worldwide_posting_is_unplaceable_not_eu(jobhunt_config):
    # 93 live "Home based - Worldwide" postings were told "free to work (eu)": a
    # claim the posting never made. Worldwide states NO work-eligibility geography,
    # so it is warned as unplaceable and never credited (nor discarded).
    posting = _posting(["Home based - Worldwide"], remote_flag=True)
    assert posting_regions(posting) == set()
    breakdown = Scorer(jobhunt_config, _profile()).score(posting)
    assert breakdown.discard is False
    assert any("region not determined" in w for w in breakdown.weak)
    assert not any("free to work" in m for m in breakdown.matched)
    assert not any("EU work rights" in m for m in breakdown.matched)


def test_switzerland_is_named_as_its_own_region(jobhunt_config):
    # The SSOT states CH separately from the EU, so the gate's reason line must be
    # able to name it. Bern is in no EU city list: it places on the CH branch alone.
    assert posting_regions(_posting(["Bern"])) == {"ch"}
    assert posting_regions(_posting(["Zurich, Switzerland"])) == {"ch", "eu"}
    assert Scorer(jobhunt_config, _profile()).score(
        _posting(["Bern"])).discard is False


def test_the_dotted_uk_abbreviation_is_placed_as_uk(jobhunt_config):
    # "u.k." can never match as a whole word (the trailing "." kills the boundary),
    # so "Remote, U.K." placed nowhere and the gate could not judge it.
    posting = _posting(["Remote, U.K."], slug="smallcorp", remote_flag=True)
    assert posting_regions(posting) == {"uk"}
    breakdown = Scorer(jobhunt_config, _profile()).score(posting)
    assert breakdown.discard is True
    assert "uk" in breakdown.discard_reason


def test_a_bare_uk_city_is_never_an_eu_city(jobhunt_config):
    # Putting the UK cities back into _EU_CITIES makes a bare "London" free to
    # work, with no country name anywhere to catch it.
    assert posting_regions(_posting(["London"])) == {"uk"}
    breakdown = Scorer(jobhunt_config, _profile()).score(
        _posting(["London"], slug="smallcorp"))
    assert breakdown.discard is True
    assert "uk" in breakdown.discard_reason


def test_a_bare_us_state_name_places_the_posting_in_the_us(jobhunt_config):
    # The postal-code rule only fires in "City, XX" form, so before the full state
    # names landed in the corpus "Remote - California" read as NO geography and
    # sailed through the gate as unjudgeable.
    posting = _posting(["Remote - California"], slug="smallcorp",
                       remote_flag=True)
    assert posting_regions(posting) == {"us"}
    breakdown = Scorer(jobhunt_config, _profile()).score(posting)
    assert breakdown.discard is True
    assert "us" in breakdown.discard_reason


# -- GUARD 9: the ELIGIBILITY AXIS half of the fix (the soft axis, not the gate) --
# The gate is only half of W5.1e. The axis must stop CLAIMING EU work rights for
# postings that do not reach a region the owner may work in, and it must keep its
# geography when the gate is inactive (no owner facts in the SSOT).

def test_remote_flag_does_not_erase_the_eligibility_penalty(jobhunt_config):
    # The headline axis correction: `if posting.remote_flag: return False` at the
    # head of _location_needs_sponsorship gave every remote role on earth a free
    # pass. With the gate INACTIVE (no owner facts) the axis is the ONLY defence,
    # so this is where the short-circuit's removal is pinned.
    elig = jobhunt_config.scoring.get("eligibility", {})
    scorer = Scorer(jobhunt_config, _profile(sponsorship_required_by_region={}))
    breakdown = scorer.score(_posting(["Remote - US"], remote_flag=True))
    assert any("needs visa sponsorship" in w for w in breakdown.weak)
    assert breakdown.axis_scores["eligibility_fit"] == elig.get(
        "us_sponsorship_score", 0.2)
    assert not any("EU work rights" in m for m in breakdown.matched)


def test_a_remote_uk_posting_takes_the_non_eu_eligibility_penalty(jobhunt_config):
    # _marks_non_eu's UK addition, pinned on the soft axis (gate inactive): a
    # remote UK role used to be graded "remote (EU-eligible)" at location_fit 1.0.
    elig = jobhunt_config.scoring.get("eligibility", {})
    scorer = Scorer(jobhunt_config, _profile(sponsorship_required_by_region={}))
    breakdown = scorer.score(_posting(["Remote, United Kingdom"],
                                      remote_flag=True))
    assert breakdown.axis_scores["location_fit"] == 0.4
    assert breakdown.axis_scores["eligibility_fit"] == elig.get(
        "us_sponsorship_score", 0.2)
    assert any("non-EU eligibility likely" in w for w in breakdown.weak)
    assert not any("EU-eligible" in m for m in breakdown.matched)


def test_unplaceable_geography_is_never_credited_with_eu_work_rights(
        jobhunt_config):
    # The axis credits EU rights only where the posting actually REACHES a region
    # the owner may work in. A bare "Remote" reaches none, so it says nothing.
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(["Remote"], remote_flag=True))
    assert not any("EU work rights" in m for m in breakdown.matched)
    assert not any("free to work" in m for m in breakdown.matched)


# -- GUARD 10: the role gate keeps hands-on engineering roles -------------------

def test_a_robotics_generalist_is_not_a_non_engineering_role(jobhunt_config):
    # Bare "generalist" deleted this real corpus title at every non-allowlisted
    # employer. The gate names product / design / recruiting / sales / marketing /
    # policy families; a hands-on robotics generalist is none of them, and a false
    # DISCARD costs the owner the job (a false admit costs one lead).
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(["Milan, Italy"], slug="smallcorp",
                                      title="Robotics Generalist",
                                      description=AI_FLAVOURED_JD))
    assert breakdown.discard is False


# -- GUARD 11: the security-clearance channel tells the TRUTH --------------------
# A cleared US national-security role is impossible for the owner, but NOT because
# the employer refuses visas: anthropic and openai sponsor freely and still cannot
# hire a non-citizen into one. It is its own channel, with its own reason, and it
# matches a REQUIREMENT, not a mention.

def test_incidental_clearance_boilerplate_does_not_discard_a_sponsoring_employer(
        jobhunt_config):
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(
        ["Washington, DC"], slug="anthropic",
        description="Some of our customers hold a security clearance; this role "
                    "does not require one."))
    assert breakdown.discard is False
    assert any("known sponsoring employer" in w for w in breakdown.weak)


def test_a_genuine_clearance_requirement_is_discarded_with_a_truthful_reason(
        jobhunt_config):
    # 14 live postings at allowlisted employers are exactly this shape. They MUST
    # keep being discarded (the owner cannot hold a US clearance), and the reason
    # must stop claiming the employer refuses to sponsor visas, which it does not.
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(
        ["Washington, DC"], slug="anthropic",
        title="Machine Learning Engineer, National Security",
        description="This role requires an active TS/SCI clearance."))
    assert breakdown.discard is True
    assert breakdown.discard_reason == (
        "requires a security clearance the owner cannot obtain "
        "(citizenship required; us)")
    assert "refuses visa sponsorship" not in breakdown.discard_reason


def test_relocation_boilerplate_is_not_a_visa_refusal(jobhunt_config):
    # "no relocation" / "not offer relocation" are BENEFITS boilerplate. They said
    # nothing about visas, and the gate reported them as a refusal to sponsor.
    scorer = Scorer(jobhunt_config, _profile())
    fang = scorer.score(_posting(
        ["New York, NY"], slug="google",
        description="We do not offer relocation assistance for this role."))
    assert fang.discard is False
    assert any("known sponsoring employer" in w for w in fang.weak)

    small = scorer.score(_posting(
        ["New York, NY"], slug="smallcorp",
        description="We do not offer relocation assistance for this role."))
    assert small.discard is True
    assert "refuses visa sponsorship" not in small.discard_reason
    assert "not a known sponsor" in small.discard_reason


def test_no_sponsorship_available_is_a_refusal_not_an_offer(jobhunt_config):
    # The OFFER phrase "sponsorship available" matches INSIDE "no sponsorship
    # available", so the posting was admitted with the reason "employer offers visa
    # sponsorship": the exact opposite of what it said. Refusal is checked first.
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(
        ["Austin, TX"], slug="smallcorp",
        description="Unfortunately there is no sponsorship available for this "
                    "role."))
    assert breakdown.discard is True
    assert "refuses visa sponsorship" in breakdown.discard_reason
    assert not any("offers visa sponsorship" in m for m in breakdown.matched)


# -- GUARD 12: the PRODUCTION WIRING. Both paths, or the gate goes dark ---------
# run.py builds the scorer with BOTH `profile_from_real_ssot(ssot)` (which exports
# the map) and `Scorer(..., ssot=ssot)` (which falls back to reading it). Either
# alone is redundant; deleting BOTH leaves a green suite and an INACTIVE gate in
# production, which is how a mis-constructed SSOT silently disarmed it once already.

def _ssot_with_work_auth(tmp_path, real_ssot_path):
    """The synthetic production-shaped SSOT, with the STRUCTURED work-auth block
    the real one carries (the fixture states it as prose, which no map can read)."""
    block = (
        "work_authorization:\n"
        "  eu:\n    sponsorship_required: 'false'\n"
        "  ch:\n    sponsorship_required: 'false'\n"
        "  uk:\n    sponsorship_required: 'true'\n"
        "  us:\n    sponsorship_required: 'true'\n"
        "  ca:\n    sponsorship_required: 'true'"
    )
    text = re.sub(r"^work_authorization:.*$", block,
                  Path(real_ssot_path).read_text(), count=1, flags=re.M)
    path = tmp_path / "ssot_work_auth.yaml"
    path.write_text(text)
    return SSOT.load(path)


def test_the_profile_export_carries_the_owners_sponsorship_map(tmp_path,
                                                               real_ssot_path):
    ssot = _ssot_with_work_auth(tmp_path, real_ssot_path)
    profile = profile_from_real_ssot(ssot)
    assert profile["sponsorship_required_by_region"] == dict(OWNER_AXES)


def test_the_scorer_falls_back_to_the_ssot_when_the_profile_omits_the_map(
        jobhunt_config, tmp_path, real_ssot_path):
    ssot = _ssot_with_work_auth(tmp_path, real_ssot_path)
    profile = profile_from_real_ssot(ssot)
    profile.pop("sponsorship_required_by_region")
    scorer = Scorer(jobhunt_config, profile, ssot=ssot)
    assert scorer._sponsorship_by_region == dict(OWNER_AXES)


def test_the_gate_is_active_when_the_scorer_is_built_the_way_production_builds_it(
        jobhunt_config, tmp_path, real_ssot_path):
    # Exactly run.py:87-88. An empty map here is a DARK gate, and the only thing
    # that makes it loud is this test.
    ssot = _ssot_with_work_auth(tmp_path, real_ssot_path)
    profile = profile_from_real_ssot(ssot)
    scorer = Scorer(jobhunt_config, profile, ssot=ssot)
    assert scorer._sponsorship_by_region  # non-empty: the gate is ARMED
    breakdown = scorer.score(_posting(["Remote - US"], slug="smallcorp",
                                      remote_flag=True))
    assert breakdown.discard is True
    assert "us" in breakdown.discard_reason


# -- GUARD 13: THE BACKFILL. The gates must reach the rows already in the digest --
# The discard channels are applied to NET-NEW postings only, so without the
# carryover rescore this whole wave would change NOTHING for the two impossible
# postings already sitting at the top of the owner's digest.

def _seed_carryover(store, posting, score=74, item_id="j-99"):
    """A row from an earlier run: pending_review, visible, scored under the OLD
    engine that had no work-auth gate."""
    store.record_ledger(posting.identity_key(), item_id, posting.vendor,
                        posting.company_slug, posting.title, posting.url,
                        "seen", score)
    store.upsert_queue(
        item_id, posting.identity_key(), "pending_review", None, score, 1,
        "automatable",
        {"posting": {"vendor": posting.vendor, "title": posting.title,
                     "company_slug": posting.company_slug, "url": posting.url,
                     "locations": posting.locations, "remote_flag": True,
                     "comp": None, "unverified": False},
         "breakdown": {"total": score, "matched": ["eligibility: EU work rights"],
                       "weak": [], "ats_warnings": []}})
    return item_id


def test_a_carryover_the_gates_now_discard_is_demoted_out_of_the_digest(
        store, jobhunt_config):
    # The gitlab row at the top of the owner's real digest, score 74.
    posting = _posting(["Remote, Canada; Remote, United Kingdom"],
                       slug="gitlab", remote_flag=True,
                       title="Senior Backend Engineer (Ruby), AI Engineering")
    item_id = _seed_carryover(store, posting)
    queue = QueueStateMachine(store, jobhunt_config)
    scorer = Scorer(jobhunt_config, _profile())

    rescored = _rescore_carryover(scorer, store, {posting.identity_key(): posting},
                                  queue)

    assert rescored == 1
    row = store.get_queue_row(item_id)
    assert row["score"] == 0
    assert row["state"] == "demoted"       # taken out by the state machine,
    queue.rerank()                          # and off the visible list by rerank.
    assert store.get_queue_row(item_id)["visible"] == 0


def test_the_rescored_carryover_persists_its_discard_and_its_reason(
        store, jobhunt_config):
    # The breakdown was computed and then THROWN AWAY: the row kept its stale
    # "eligibility: EU work rights" note and nothing recorded why it went.
    posting = _posting(["Remote, Canada; Remote, United Kingdom"],
                       slug="gitlab", remote_flag=True,
                       title="Senior Backend Engineer (Ruby), AI Engineering")
    item_id = _seed_carryover(store, posting)
    _rescore_carryover(Scorer(jobhunt_config, _profile()), store,
                       {posting.identity_key(): posting},
                       QueueStateMachine(store, jobhunt_config))

    breakdown = store.get_queue_row(item_id)["payload"]["breakdown"]
    assert breakdown["discard"] is True
    assert "ca" in breakdown["discard_reason"] and "uk" in breakdown["discard_reason"]
    assert not any("EU work rights" in m for m in breakdown["matched"])


def test_a_still_workable_carryover_survives_the_rescore(store, jobhunt_config):
    # The backfill removes the impossible rows and NOTHING else: a carryover the
    # owner may still legally take keeps its place in the digest.
    posting = _posting(["Milan, Italy"], slug="acme")
    item_id = _seed_carryover(store, posting)
    queue = QueueStateMachine(store, jobhunt_config)

    _rescore_carryover(Scorer(jobhunt_config, _profile()), store,
                       {posting.identity_key(): posting}, queue)

    row = store.get_queue_row(item_id)
    assert row["payload"]["breakdown"]["discard"] is False
    assert row["state"] == "pending_review"
    assert row["visible"] == 1


def test_the_daily_launcher_runs_the_carryover_rescore():
    # The backfill exists only if the daily run actually asks for it -- and this
    # test COULD NOT FAIL as first written. It searched the launcher's whole TEXT
    # for "--rescore", and the COMMENT above the exec line contains that literal
    # token, so deleting the flag from the command left the assertion green while
    # the backfill was DISARMED in production: the single line that arms the whole
    # feature was guarded by nothing. Read the EXECUTABLE lines only, and require
    # the flag to be a whole argument on one of them.
    lines = (ROOT / "bin" / "jobhunt-daily").read_text().splitlines()
    executable = [ln for ln in lines if not ln.lstrip().startswith("#")]
    assert any("--rescore" in ln.split() for ln in executable)


# -- GUARD 13: a PREFERRED clearance is not a REQUIRED one ---------------------
# 5 live postings (cohere Ottawa x3, scaleai Los Angeles / Washington DC) named a
# clearance only to say it was PREFERRED, or that candidates merely ELIGIBLE to
# obtain one would "also be considered" -- and the gate told the owner they
# "require a security clearance ... (citizenship required)". The outcome was right
# (they are ca/us postings at employers that neither sponsor nor are allowlisted,
# so the WORK-AUTH channel takes them), but the owner READS these strings.

def test_a_preferred_clearance_does_not_enter_the_clearance_channel(jobhunt_config):
    # At an allowlisted employer this now lands where it belongs: warned and
    # ADMITTED. A preferred clearance is not a bar, so it must not discard.
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(
        ["Washington, DC"], slug="anthropic", title="Deployment Strategist",
        description=AI_FLAVOURED_JD + " An active top secret/SCI clearance is "
                    "strongly preferred. Candidates eligible and willing to "
                    "obtain clearance will also be considered."))
    assert breakdown.discard is False
    assert "security clearance" not in breakdown.discard_reason
    assert any("known sponsoring employer" in w for w in breakdown.weak)


def test_the_soft_clearance_postings_are_still_discarded_by_the_work_auth_channel(
        jobhunt_config):
    # THE REAL JD, not a synthetic sentence. As first written this test fed the gate
    # one hand-made line ("active top secret clearance strongly preferred") and went
    # GREEN while the LIVE posting it claimed to cover still fired the clearance
    # channel: the real JD says "strong preference", which "preferred" never matched.
    # A test that passes on synthetic input while the live case fails does not just
    # miss the defect, it CERTIFIES it. Every clearance test here is now built from
    # the shape the live board actually serves.
    #
    # The DISCARD stands (cohere is not allowlisted and Ottawa is a ca posting);
    # only the REASON changes, from a requirement the posting never made to the one
    # it did. The owner reads these strings.
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(
        ["Ottawa", "Canada", "Toronto", "Montreal"], slug="cohere",
        title="Technical Program Manager, AI Delivery for Public Sector & Defence",
        description=AI_FLAVOURED_JD + COHERE_OTTAWA_TPM_JD))

    # The real posting is taken by the channels that TRULY hold: it is a ca posting
    # at an employer that does not sponsor, and a program-management role. It is NOT
    # taken by a clearance requirement, because its JD never made one.
    assert breakdown.discard is True
    assert ("needs visa sponsorship (ca); employer is not a known sponsor and the "
            "posting does not offer it") in breakdown.discard_reason
    assert "security clearance" not in breakdown.discard_reason
    assert "clearance" not in breakdown.discard_reason


def test_a_hard_clearance_requirement_still_discards_with_its_own_reason(
        jobhunt_config):
    # The other side of the same guard: a JD that DEMANDS a clearance keeps its own
    # channel and its own truthful reason, even at an employer that sponsors freely.
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(
        ["Washington, DC"], slug="anthropic", title="Deployment Strategist",
        description=AI_FLAVOURED_JD + " Applicants must hold an active security "
                    "clearance."))
    assert breakdown.discard is True
    assert breakdown.discard_reason == (
        "requires a security clearance the owner cannot obtain "
        "(citizenship required; us)")


# -- GUARD 14: the engineering-noun override, actually reached -----------------
# `test_engineering_noun_in_the_head_survives_a_non_engineering_word` uses
# "Generalist Software Engineer", whose head hits NO family keyword since round 2
# removed bare "generalist": it returns before the override, which therefore
# survived deletion with a green suite. These titles hit the keyword list AND
# carry an engineering noun, so they reach it.

def test_a_family_word_beside_an_engineering_noun_is_still_an_engineering_role(
        jobhunt_config):
    scorer = Scorer(jobhunt_config, _profile())
    for title in ("Sales Engineer", "Customer Support Engineer", "UX Engineer"):
        breakdown = scorer.score(_posting(["Milan, Italy"], title=title,
                                          description=AI_FLAVOURED_JD))
        assert breakdown.discard is False, title
    # ... and the same family word WITHOUT an engineering noun still goes.
    gone = scorer.score(_posting(["Milan, Italy"], title="Sales Manager",
                                 description=AI_FLAVOURED_JD))
    assert gone.discard is True
    assert "non-engineering role family: sales" in gone.discard_reason


# -- GUARD 15: the region map's live holes ------------------------------------

def test_the_unplaced_eu_cities_are_placed(jobhunt_config):
    # 13 live postings sat dead-centre of the owner's profile (ion Trento/Pisa,
    # deliveroo Genova/Monza/Brescia, eneba Kaunas) and the map could not place
    # them: they failed OPEN (warned, admitted), but lost the "free to work" credit
    # and took the eligibility penalty. Endonyms, Italian regions used as a
    # location, and Lithuania's second city.
    scorer = Scorer(jobhunt_config, _profile())
    for location in ("Trento", "Pisa", "Genova", "Monza", "Brescia", "Liguria",
                     "Lombardia", "Kaunas"):
        assert posting_regions(_posting([location])) == {"eu"}, location
        breakdown = scorer.score(_posting([location]))
        assert breakdown.discard is False, location
        assert any("free to work (eu)" in m for m in breakdown.matched), location


# -- GUARD 16: a refusal reason states the evidence it fired on ----------------

def test_the_refusal_reason_quotes_the_phrase_it_matched(jobhunt_config):
    # deliveroo "Site Manager - HOP", located in Dubai, fired on "right to work in
    # the uk" and reported "... requires an existing right to work (other)": it
    # named a REGION the matched phrase never mentioned. The discard is right; the
    # string was incoherent. It now quotes the evidence.
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(
        ["Dubai - Motor City (Hop)"], slug="deliveroo",
        description=AI_FLAVOURED_JD + " This role requires the right to work in "
                    "the UK."))
    assert breakdown.discard is True
    assert breakdown.discard_reason == (
        'posting refuses visa sponsorship or requires an existing right to work '
        '(matched: "right to work in the uk")')
    assert "(other)" not in breakdown.discard_reason


# -- GUARD 16: a clearance in a BONUS LIST is not a clearance REQUIREMENT --------
# The clearance channel read a plus-or-minus-100-character window around the match,
# and a real JD does not put its soft-qualification HEADER within 100 characters of
# the bullet it governs: it puts it several bullets ABOVE. So 4 live postings at
# SPONSORING employers, whose JDs list a clearance as a BONUS, were deleted from the
# owner's digest for a bar their JD never raised. Proximity was the wrong
# instrument; the SECTION is the evidence.
#
# Every JD text below is the LIVE text, copied from the board corpus with its real
# header, its real intervening bullets and its real separators. A synthetic sentence
# is what let this defect live through a round with a green suite.

# anthropic | Technical Cyber Threat Investigator (and, same shape, Technical CBRN-E
# Threat Investigator). Locations: Remote-Friendly (Travel-Required) | San
# Francisco, CA | Washington, DC.
ANTHROPIC_BONUS_LIST_JD = (
    " Possess excellent communication skills to collaborate with cross-functional "
    "teams and present to leadership \n \n  Strong candidates may also have  \n \n "
    "Experience working with government agencies or in regulated environments \n "
    "Background in AI safety, machine learning security, or technology abuse "
    "investigation \n Experience building and scaling threat detection systems or "
    "abuse monitoring programs \n Active Top Secret security clearance \n \n  "
    "Deadline to apply  \n None. Applications will be reviewed on a rolling basis."
)
# openai | Forward Deployed Engineer, Gov. Locations: Washington, DC | Seattle |
# San Francisco.
OPENAI_THRIVE_LIST_JD = (
    "\n\nYou might thrive in this role if you\n\n - Bring 5+ years of engineering "
    "or technical deployment experience, ideally in customer-facing or government "
    "environments.\n\n - Active TS/SCI clearance or equivalent\n\n - Have scoped "
    "and delivered complex systems in fast-moving or ambiguous contexts."
)
# openai | Industrial Security CSSO/CPSO, Washington DC. The header carries a CURLY
# apostrophe (U+2019), which no straight-quoted marker matches.
OPENAI_CURLY_THRIVE_JD = (
    "\n\nYou’ll thrive in this role if you:\n\n - Have 5+ years of experience "
    "in industrial security.\n\n - Have an active TS/SCI security clearance Full "
    "Scope Polygraph.\n\n - Have expert knowledge of the 32 CFR Part 117."
)
# anthropic | Solutions Architect, National Security. The header is SOFT ("You may
# be a good fit if you have") and the bullet is HARD: the line, not the section,
# says so.
ANTHROPIC_REQUIRED_JD = (
    " You may be a good fit if you have: \n \n \n Active TS/SCI security clearance "
    "(required) \n \n \n 2+ years of experience as a Customer Engineer, Forward "
    "Deployed Engineer or Solutions Architect."
)
# databricks | Sr Technical Solutions Engineering, McLean, Virginia.
DATABRICKS_MINIMUM_QUALS_JD = (
    " \n  What We're Looking For  \n \n  Minimum Qualifications  \n \n Active "
    "TS/SCI security clearance. \n 5+ years of experience in Systems Engineering, "
    "Cloud Infrastructure, Support."
)
# cohere | Technical Program Manager, AI Delivery for Public Sector & Defence,
# Ottawa. Three mentions, none of them a bar the owner is held to: "strongly
# preferred", a "Nice to Have" bullet, and a "Security Clearance Requirements"
# section whose text gives a STRONG PREFERENCE and then admits candidates who hold
# no clearance at all. The old soft list had "preferred" but not "preference".
COHERE_OTTAWA_TPM_JD = (
    "\n\nLocation: Ottawa (preferred) or Montreal or Toronto required (proximity "
    "to government customers)\n\nSecurity Clearance: Active Top Secret clearance "
    "strongly preferred; candidates eligible and willing to obtain clearance will "
    "also be considered.\n\n\n\nWhat You'll Do\n\n - Own the technical program.\n\n"
    " - Eligible to obtain Canadian government security clearance (Top Secret "
    "preferred)\n\nNice to Have:\n\n - Active Canadian government security "
    "clearance (Secret or Top Secret) - strong preference for active Top Secret "
    "clearance\n\n - Bilingual (English/French) proficiency\n\n\n\nSecurity "
    "Clearance Requirements\n\nThis role requires eligibility for Canadian "
    "government security clearance.\n\nStrong preference will be given to "
    "candidates who currently hold an active Top Secret (TS) clearance.\n\nHowever,"
    " we will also consider exceptional candidates who:\n\n - Do not currently hold "
    "clearance, but are eligible and willing to undergo the Top Secret clearance "
    "process"
)


def test_a_clearance_under_a_bonus_header_does_not_discard_a_sponsoring_employer(
        jobhunt_config):
    # The 2 anthropic victims. The bullet "Active Top Secret security clearance"
    # sits ~370 characters below its "Strong candidates may also have" header, so no
    # window could see it. Both employers sponsor, so with the clearance channel
    # silenced the carve-out admits them WARNED, which is where they belong.
    scorer = Scorer(jobhunt_config, _profile())
    for title in ("Technical Cyber Threat Investigator",
                  "Technical CBRN-E Threat Investigator"):
        breakdown = scorer.score(_posting(
            ["Remote-Friendly (Travel-Required) | San Francisco, CA | "
             "Washington, DC"],
            slug="anthropic", title=title,
            description=AI_FLAVOURED_JD + ANTHROPIC_BONUS_LIST_JD))
        assert breakdown.discard is False, title
        assert any("known sponsoring employer" in w for w in breakdown.weak), title


def test_a_clearance_under_a_thrive_header_does_not_discard_a_sponsoring_employer(
        jobhunt_config):
    # The 2 openai victims, including the CURLY apostrophe in "You'll thrive".
    scorer = Scorer(jobhunt_config, _profile())
    gov = scorer.score(_posting(
        ["Washington, DC", "Seattle", "San Francisco"], slug="openai",
        title="Forward Deployed Engineer, Gov",
        description=AI_FLAVOURED_JD + OPENAI_THRIVE_LIST_JD))
    assert gov.discard is False
    assert any("known sponsoring employer" in w for w in gov.weak)

    csso = scorer.score(_posting(
        ["Washington, DC"], slug="openai",
        title="Industrial Security CSSO/CPSO, Washington DC",
        description=AI_FLAVOURED_JD + OPENAI_CURLY_THRIVE_JD))
    assert csso.discard is False
    assert any("known sponsoring employer" in w for w in csso.weak)


def test_the_bullet_marked_required_outranks_its_soft_header(jobhunt_config):
    # The other side, and the reason the section is not read alone: anthropic's
    # National Security JD lists the clearance under "You may be a good fit if you
    # have" and then marks that one bullet "(required)". The LINE says it outright,
    # so the section never gets a vote. This posting MUST stay discarded.
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(
        ["Washington, DC"], slug="anthropic",
        title="Solutions Architect, National Security",
        description=AI_FLAVOURED_JD + ANTHROPIC_REQUIRED_JD))
    assert breakdown.discard is True
    assert breakdown.discard_reason == (
        "requires a security clearance the owner cannot obtain "
        "(citizenship required; us)")


def test_a_clearance_under_a_minimum_qualifications_header_still_discards(
        jobhunt_config):
    # databricks states the same bare mention ("Active TS/SCI security clearance")
    # as the anthropic bonus list, with NOTHING on the line to tell them apart. The
    # header is the whole difference, and it is a REQUIREMENT header.
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(
        ["McLean, Virginia"], slug="databricks",
        title="Sr Technical Solutions Engineering",
        description=AI_FLAVOURED_JD + DATABRICKS_MINIMUM_QUALS_JD))
    assert breakdown.discard is True
    assert "requires a security clearance" in breakdown.discard_reason


def test_a_hard_requirement_sentence_needs_no_header_at_all(jobhunt_config):
    # cohere's Public Sector SA says it in one sentence, under no header. A phrase
    # that CARRIES its own demand is a requirement wherever it appears.
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(
        ["Washington, DC"], slug="cohere", title="Solutions Architect",
        description=AI_FLAVOURED_JD + "\n\nIn order to qualify for this exciting "
                    "career opportunity, you must have a Security Clearance.\n\n"))
    assert breakdown.discard is True
    assert "requires a security clearance" in breakdown.discard_reason


# -- GUARD 17: an UNPLACEABLE fragment fails the posting OPEN --------------------
# posting_regions UNIONS the placeable fragments, so an unplaceable one beside a
# placeable one was silently DROPPED and the posting judged on the half the map
# could read. "Home based - Worldwide" INCLUDES the places the owner may work.

def test_an_unplaceable_fragment_beside_a_placeable_one_fails_open(jobhunt_config):
    # canonical | Linux Devices Software Engineer, the live location string verbatim.
    # It is not a Taiwan role: it is a WORLDWIDE role that also has a Taipei office,
    # and the other 93 "Home based - Worldwide" postings were admitted all along.
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(
        ["Home based - Worldwide; Office Based - Taipei, Taiwan"],
        slug="canonical", title="Linux Devices Software Engineer",
        description=AI_FLAVOURED_JD))
    # The UNION is {other}: placeable, non-empty, and exactly why the old gate never
    # reached its fail-open branch. The FRAGMENTS are what tell the truth.
    posting = _posting(["Home based - Worldwide; Office Based - Taipei, Taiwan"])
    assert posting_regions(posting) == {"other"}
    assert unplaceable_fragments(posting) == ["Home based - Worldwide"]
    assert breakdown.discard is False
    assert any("region not determined" in w for w in breakdown.weak)
    assert any("Home based - Worldwide" in w for w in breakdown.weak)


def test_a_work_mode_fragment_is_not_an_unplaceable_geography(jobhunt_config):
    # The fail-open must not swallow the gate. "Remote-Friendly (Travel-Required)"
    # states no geography AT ALL (it sits beside the real one), so it is ignored and
    # the posting is still judged on San Francisco and Washington.
    scorer = Scorer(jobhunt_config, _profile())
    breakdown = scorer.score(_posting(
        ["Remote-Friendly (Travel-Required) | San Francisco, CA | Washington, DC"],
        slug="smallcorp", description=AI_FLAVOURED_JD))
    assert breakdown.discard is True
    assert breakdown.discard_reason == (
        "needs visa sponsorship (us); employer is not a known sponsor "
        "and the posting does not offer it")


# -- GUARD 18: "(other)" is not a place ----------------------------------------

def test_the_discard_reason_names_the_country_behind_other(jobhunt_config):
    # 521 live reason strings said "needs visa sponsorship (other)" and named
    # nothing. The owner cannot tell an Israeli role from a Japanese one from a
    # reason that calls both the same.
    scorer = Scorer(jobhunt_config, _profile())
    israel = scorer.score(_posting(["Ramat Gan, Israel"], slug="smallcorp",
                                   description=AI_FLAVOURED_JD))
    assert israel.discard is True
    assert "other: Israel" in israel.discard_reason

    tokyo = scorer.score(_posting(["Tokyo"], slug="smallcorp",
                                  description=AI_FLAVOURED_JD))
    assert "other: Japan" in tokyo.discard_reason


# -- GUARD 19: what ARMING --rescore does to the digest the owner reads ----------
# `--rescore` is one flag on one line of bin/jobhunt-daily, and it is the ONLY thing
# that carries this wave's gates back to the rows already in the digest. Nothing
# tested what it does to the VISIBLE list: the backfill tests all read the queue row
# they had just re-scored, never the digest.

def _visible_digest(queue):
    """Exactly what run.py serves the owner (_draft_top_items): the visible
    pending_review rows, best score first."""
    rows = [item for item in queue.items()
            if item.visible and item.state == "pending_review"]
    return [item.item_id for item in sorted(rows, key=lambda i: i.score,
                                            reverse=True)]


def test_arming_the_rescore_takes_the_impossible_row_out_of_the_visible_digest(
        store, jobhunt_config):
    impossible = _posting(["Remote, Canada; Remote, United Kingdom"],
                          slug="gitlab", remote_flag=True,
                          title="Senior Backend Engineer (Ruby), AI Engineering")
    workable = _posting(["Milan, Italy"], slug="acme")
    _seed_carryover(store, impossible, score=74, item_id="j-99")
    _seed_carryover(store, workable, score=61, item_id="j-98")
    queue = QueueStateMachine(store, jobhunt_config)

    # DISARMED (run.py runs no backfill): the impossible row is at the TOP of the
    # digest the owner reads, which is the defect this wave exists to fix.
    assert _visible_digest(queue) == ["j-99", "j-98"]

    _rescore_carryover(Scorer(jobhunt_config, _profile()), store,
                       {impossible.identity_key(): impossible,
                        workable.identity_key(): workable}, queue)
    queue.rerank()

    # ARMED: it is gone from the digest, and the workable carryover is untouched.
    assert _visible_digest(QueueStateMachine(store, jobhunt_config)) == ["j-98"]
