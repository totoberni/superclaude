"""profile_map tests against the SYNTHETIC v1.4 SSOT fixture (no real PII)."""

from engine.match import Scorer
from engine.profile_map import profile_from_real_ssot
from engine.ssot import SSOT


def _profile(real_ssot_path):
    return profile_from_real_ssot(SSOT.load(real_ssot_path))


def test_roles_map_as_is(real_ssot_path):
    profile = _profile(real_ssot_path)
    assert profile["roles"] == [
        "Senior Backend Engineer",
        "Machine Learning Engineer",
        "Graduate Software Engineer",
    ]


def test_comp_floor_parses_grouped_integer(real_ssot_path):
    # "EUR 47,000 gross annual (RAL)" -> 47000
    assert _profile(real_ssot_path)["comp_floor"] == 47000


def test_location_tokens_include_bare_city_and_country(real_ssot_path):
    locations = _profile(real_ssot_path)["locations"]
    for token in ("Lisbon, Portugal", "Lisbon", "Portugal", "London", "UK", "Remote"):
        assert token in locations


def test_seniority_anchored_on_experience_not_roles(real_ssot_path):
    # W4 redesign: the self-inflation bug is fixed. The profile no longer derives
    # a seniority band from the owner's OWN target-role names ("Senior Backend
    # Engineer" no longer makes the owner "senior"); the seniority gate anchors
    # on an explicit experience_years instead.
    profile = _profile(real_ssot_path)
    assert "seniority" not in profile          # never derived from role names
    assert profile["experience_years"] == 1.7  # the entry-level anchor


def test_skill_tokens_mapped_for_matching(real_ssot_path):
    # canonical short tags flow through ALONGSIDE the verbose skills.
    tokens = _profile(real_ssot_path)["skill_tokens"]
    for tag in ("python", "pytorch", "cpp", "docker"):
        assert tag in tokens


def test_skills_flatten_across_all_blocks(real_ssot_path):
    skills = _profile(real_ssot_path)["skills"]
    for skill in ("Python", "TypeScript", "PyTorch", "SQLite", "machine learning"):
        assert skill in skills


def test_remote_ok_and_excludes(real_ssot_path):
    profile = _profile(real_ssot_path)
    assert profile["remote_ok"] is True
    assert profile["excludes"] == ["unpaid", "commission only"]


def test_capabilities_conservative_from_work_authorization(real_ssot_path):
    caps = _profile(real_ssot_path)["capabilities"]
    assert "work_authorization_eu" in caps
    # never guessed: the synthetic SSOT states no US/UK rights
    assert "work_authorization_us" not in caps
    assert "work_authorization_uk" not in caps


def test_missing_blocks_degrade_gracefully():
    profile = profile_from_real_ssot(SSOT({}))
    assert profile == {}
    partial = profile_from_real_ssot(SSOT({"preferences": {"target_roles":
                                                           ["Backend Engineer"]}}))
    assert partial["roles"] == ["Backend Engineer"]
    assert "comp_floor" not in partial
    assert "locations" not in partial


def test_unparseable_comp_floor_is_none():
    ssot = SSOT({"preferences": {"comp_floor": "competitive salary"}})
    assert profile_from_real_ssot(ssot) == {}


def test_profile_is_scorer_compatible(jobhunt_config, real_ssot_path):
    # same contract as match.profile_from_ssot: the Scorer accepts it unchanged.
    profile = _profile(real_ssot_path)
    scorer = Scorer(jobhunt_config, profile)
    assert scorer.profile["roles"]
