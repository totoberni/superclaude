"""Scoring engine: weighted axes, per-item breakdown, ATS show-and-warn pre-check.

A configurable 0-100 weighted-axis score (plan 7.3, D5/D6). Every scored item
carries a breakdown (top matched criteria + missing/weak criteria) so the report
line can explain the why. The ATS pre-check is show-and-warn: a strong match that
fails a hard filter is surfaced WITH a warning, never hidden (D5).

The similarity axis is pluggable behind the Similarity protocol. v1 ships the
deterministic TokenOverlapSimilarity; Vec0Similarity is a stub for the vec0
vector-search hookup (WT-8) that lands on toto.

The concrete axis functions here implement the jobs axis set (7.3). The phd and
papers axis sets are a later design deliverable (plan 7.3: "these do not exist
yet and must be designed"); building a Scorer from a config that names an
unimplemented axis fails fast with a clear message.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Protocol

from engine.kernel.contracts import Posting
from engine.profile_map import sponsorship_by_region
from engine.ssot import MISSING, SSOT


class Similarity(Protocol):
    def score(self, query: str, document: str) -> float:
        ...


class TokenOverlapSimilarity:
    """Deterministic recall of query tokens found in the document (0..1)."""

    def score(self, query: str, document: str) -> float:
        q = _tokens(query)
        if not q:
            return 0.0
        return len(q & _tokens(document)) / len(q)


class Vec0Similarity:
    """Stub for the vec0 vector-search similarity (7.3, WT-8), toto follow-up.

    v1 ships TokenOverlapSimilarity as the deterministic default; this class
    fails loudly so a caller cannot silently depend on an absent backend.
    """

    def score(self, query: str, document: str) -> float:
        raise NotImplementedError(
            "vec0 similarity lands on toto (WT-8); use TokenOverlapSimilarity in v1"
        )


@dataclass
class ScoreBreakdown:
    total: int
    axis_scores: dict[str, float]
    matched: list[str] = field(default_factory=list)
    weak: list[str] = field(default_factory=list)
    ats_warnings: list[str] = field(default_factory=list)
    discard: bool = False
    discard_reason: str = ""


class Scorer:
    """Gated-multiplicative job matcher (W4 redesign, owner-ratified 2026-07-06).

        total = round(100 * family_mult * seniority_mult * soft_fit)

    family and seniority are GATES: the wrong family or an over-senior role
    collapses the whole score (a real discard with a reason), so a strong
    role/location can no longer swamp a fatal mismatch (the old additive bug that
    scored senior/no-overlap roles 72-74). soft_fit is the additive quality score
    over survivors. Discards (family / over-seniority / commute / excludes) set
    total to 0 and record a reason; ATS hard filters stay on the separate
    show-and-warn channel (surfaced, never hidden). Every threshold, weight, and
    keyword lives in the instance config.yaml `scoring` block: nothing here is a
    magic number.
    """

    def __init__(self, config, profile: dict, similarity: Similarity | None = None,
                ssot: SSOT | None = None):
        self.config = config
        self.profile = profile
        self.similarity = similarity or TokenOverlapSimilarity()
        scoring = getattr(config, "scoring", {}) or {}
        self._family_cfg = scoring.get("family", {})
        self._seniority_cfg = scoring.get("seniority", {})
        self._skills_cfg = scoring.get("skills", {})
        self._term_cfg = scoring.get("term_length", {})
        self._comp_cfg = scoring.get("comp", {})
        self._eligibility_cfg = scoring.get("eligibility", {})
        self._excludes_discard = scoring.get("excludes", {}).get(
            "discard_on_match", True)
        # W5.1e discard channels 5 and 6. Policy (phrases, employer allowlist,
        # role keywords) is overridable from the instance config; the module
        # defaults are complete, so an instance that says nothing still gates.
        work_auth = scoring.get("work_auth", {}) or {}
        role_cfg = scoring.get("role", {}) or {}
        self._sponsoring_employers = frozenset(
            work_auth.get("sponsoring_employers", _SPONSORING_EMPLOYERS))
        self._refusal_phrases = tuple(
            work_auth.get("refusal_phrases", _SPONSORSHIP_REFUSAL_PHRASES))
        self._offer_phrases = tuple(
            work_auth.get("offer_phrases", _SPONSORSHIP_OFFER_PHRASES))
        self._clearance_phrases = tuple(
            work_auth.get("security_clearance_phrases",
                          _SECURITY_CLEARANCE_PHRASES))
        self._non_engineering_titles = tuple(
            role_cfg.get("non_engineering_keywords",
                         _NON_ENGINEERING_TITLE_KEYWORDS))
        self._engineering_nouns = tuple(
            role_cfg.get("engineering_nouns", _ENGINEERING_TITLE_NOUNS))
        self._role_gate_on = role_cfg.get("discard_non_engineering", True)
        # The owner's per-region right-to-work status is OWNER DATA and is read
        # from the SSOT, never hardcoded (the same firewall the commute policy
        # obeys). An empty map leaves the work-auth gate INACTIVE, exactly as a
        # missing location_policy leaves the commute gate inactive: this engine
        # never invents the owner's immigration status.
        self._sponsorship_by_region = dict(
            profile.get("sponsorship_required_by_region") or {})
        if not self._sponsorship_by_region and ssot is not None:
            self._sponsorship_by_region = sponsorship_by_region(ssot)
        self._weeks_per_month = scoring.get("commute", {}).get(
            "weeks_per_month", _WEEKS_PER_MONTH)
        # Commute policy VALUES are owner data in the SSOT (preferences.
        # location_policy); ssot is optional and a MISSING/partial policy leaves
        # the gate fail-open, so every existing call site stays unaffected.
        self._location_policy = (_location_policy_from_ssot(ssot)
                                 if ssot is not None else None)
        self._axis_fns = {
            "skills_overlap": self._skills_overlap,
            "role_title_fit": self._role_title_fit,
            "location_fit": self._location_fit,
            "term_length_fit": self._term_length_fit,
            "comp_fit": self._comp_fit,
            "eligibility_fit": self._eligibility_fit,
        }
        self._require_known_axes()

    def _require_known_axes(self) -> None:
        unknown = [a for a in self.config.axes if a not in self._axis_fns]
        if unknown:
            raise ValueError(
                f"no scoring function for axes {unknown}; v1 ships the jobs axis "
                "set, phd/papers axes are a later design deliverable (plan 7.3)"
            )

    def score(self, posting: Posting) -> ScoreBreakdown:
        axis_scores, matched, weak = {}, [], []
        soft_fit = 0.0
        skills_sub = None
        for axis, weight in self.config.axes.items():
            sub, m, w = self._axis_fns[axis](posting)
            axis_scores[axis] = sub
            soft_fit += sub * weight
            matched.extend(m)
            weak.extend(w)
            if axis == "skills_overlap":
                skills_sub = sub
        # Skills floor: a role with no real overlap can never clear threshold,
        # whatever else it scores (hard cap on soft_fit, not just the axis).
        floor = self._skills_cfg.get("floor", 0.15)
        if skills_sub is not None and skills_sub < floor:
            soft_fit = min(soft_fit,
                           self._skills_cfg.get("floored_soft_fit_cap", 0.05))
            weak.append("skills overlap below floor: score capped")

        family_mult, fam_m, fam_w, fam_discard, fam_reason = self._family_gate(
            posting)
        sen_mult, sen_m, sen_w, sen_discard, sen_reason = self._seniority_gate(
            posting)
        matched.extend(fam_m + sen_m)
        weak.extend(fam_w + sen_w)

        auth_m, auth_w, auth_discard, auth_reason = self._work_auth_gate(posting)
        role_m, role_w, role_discard, role_reason = self._role_gate(posting)
        matched.extend(auth_m + role_m)
        weak.extend(auth_w + role_w)

        warnings = ats_precheck(posting, self.profile, self.config.ats_rules)
        commute_discard, commute_msg = commute_gate(
            posting, self._location_policy, self._weeks_per_month)
        if not commute_discard and commute_msg:
            warnings = warnings + [commute_msg]
        excl_discard, excl_reason = self._excludes_gate(posting)

        total = round(100 * family_mult * sen_mult * soft_fit)
        breakdown = ScoreBreakdown(total, axis_scores, matched, weak, warnings)
        reasons = [r for r in (fam_reason, sen_reason,
                               commute_msg if commute_discard else "",
                               excl_reason, auth_reason, role_reason) if r]
        if (fam_discard or sen_discard or commute_discard or excl_discard
                or auth_discard or role_discard):
            breakdown.discard = True
            breakdown.discard_reason = "; ".join(reasons)
            breakdown.total = 0  # a discard sets the total low (7.3)
        return breakdown

    # -- gates (multiplicative) ------------------------------------------------

    def _family_gate(self, posting: Posting):
        """(multiplier, matched, weak, discard, reason). Tier 1 AI/ML at full
        weight, tier 2 data/SWE-adjacent at a discount, else discard (or a
        softened show-and-warn when soften_out_of_family is set)."""
        text = f"{posting.title} {posting.description}"
        tier, mult = classify_family(text, self._family_cfg)
        if tier == 1:
            return mult, ["family: AI/ML (tier 1)"], [], False, ""
        if tier == 2:
            return mult, ["family: data/SWE-adjacent (tier 2)"], [], False, ""
        if self._family_cfg.get("soften_out_of_family", False):
            soft = self._family_cfg.get("out_of_family_multiplier", 0.35)
            return soft, [], ["role family out of scope (softened)"], False, ""
        return 0.0, [], [], True, "role family out of scope"

    def _seniority_gate(self, posting: Posting):
        """(multiplier, matched, weak, discard, reason). Penalty is the GAP
        between the posting's required band and the owner's experience-derived
        band (never the owner's target-role names); a large gap discards."""
        cfg = self._seniority_cfg
        posting_band = parse_required_band(posting.title, posting.description, cfg)
        owner_band = owner_band_from_years(
            self.profile.get("experience_years"), cfg)
        gap = max(posting_band - owner_band, 0)
        if gap >= cfg.get("discard_gap", 3):
            return 0.0, [], [], True, "required seniority far above owner level"
        mults = cfg.get("over_level_multipliers", [1.0, 0.7, 0.15])
        mult = mults[min(gap, len(mults) - 1)]
        if gap == 0:
            return mult, ["seniority: level fits"], [], False, ""
        if gap >= cfg.get("warn_gap", 2):
            return mult, [], ["likely over-level"], False, ""
        return mult, [], ["slightly over-level"], False, ""

    def _work_auth_gate(self, posting: Posting):
        """DISCARD CHANNEL 5 (W5.1e, owner ruling 9): the right to work.

        (matched, weak, discard, reason). A THREE-WAY classification, never a
        boolean, and never a weight:

        1. the posting REFUSES sponsorship or demands an existing right to work
           in a region the owner has none -> DISCARD;
        2. the posting is SILENT in a sponsorship-required region -> a WARNED
           candidate, admitted only for a FANG-like employer known to sponsor
           (the owner's carve-out); otherwise DISCARD;
        3. the posting reaches a region the owner may work in, or EXPLICITLY
           OFFERS sponsorship -> ADMIT.

        This exists because the old eligibility SOFT AXIS (weight 0.10) could
        subtract at most 8 points from 100. At a high enough score on the other
        axes, a job the owner cannot legally take still landed at the TOP of his
        digest, labelled "eligibility: EU work rights". A constraint that is
        BINARY IN REALITY must be a discard channel, never a weight.

        Refusal is checked BEFORE the offer: a generic "we do sponsor visas"
        company blurb does not override a role-specific "you must already have
        the right to work here". The conservative direction is the cheap one (a
        false discard costs one lead; a false admit costs the owner's trust).

        A SECURITY CLEARANCE requirement is its own discard reason, checked
        before both, because it is a different impossibility: a cleared US
        national-security role is closed to a non-citizen even by an employer
        that sponsors visas freely, and calling it "refuses visa sponsorship"
        (as this gate first shipped doing) told the owner something untrue about
        14 live postings. It is reached only AFTER the free-region check, so an
        EU role naming a clearance the owner could actually obtain is unaffected.
        """
        if not self._sponsorship_by_region:
            return [], [], False, ""  # no owner facts -> inactive (fail-open)
        regions = posting_regions(posting)
        # ADMIT IF ANY region is workable. `regions` is a SET (one location
        # string may name several places), and a single workable one is enough:
        # the owner takes the job in Amsterdam, not in the San Francisco the same
        # string happens to list first.
        free = sorted(r for r in regions if not self._needs_sponsorship(r))
        if free:
            return ([f"work auth: free to work ({', '.join(free)})"], [],
                    False, "")
        unplaceable = unplaceable_fragments(posting)
        if not regions or unplaceable:
            # No PLACEABLE geography (a bare "Remote"/"Hybrid", or a country the
            # region map does not know). Unjudgeable, so it is warned and never
            # discarded on a guess: absence of evidence is not evidence of
            # impossibility, and a gate that discards on silence empties the
            # digest. The note stays honest about WHY it could not judge.
            #
            # ANY unplaceable fragment fails the whole posting OPEN, even when it
            # sits beside a placeable one. 'Home based - Worldwide; Office Based -
            # Taipei, Taiwan' is not a Taiwan role: it is a WORLDWIDE role that also
            # has a Taipei office, and discarding it on the strength of the half the
            # map could read deleted a job the owner could have taken. The other 93
            # 'Home based - Worldwide' postings, which carry no second fragment, were
            # admitted all along; this one differed only in being legible enough to
            # be misjudged.
            note = "work auth: work-eligibility region not determined"
            if unplaceable and regions:
                note += (f" (unplaceable: {', '.join(sorted(unplaceable))}; "
                         f"also names {_region_names(posting, regions)})")
            return [], [note], False, ""
        named = _region_names(posting, regions)
        text = f"{posting.title} {posting.description}".lower()
        if _clearance_required(text, self._clearance_phrases):
            return ([], [], True,
                    f"requires a security clearance the owner cannot obtain "
                    f"(citizenship required; {named})")
        refusal = _first_keyword(text, self._refusal_phrases)
        if refusal:
            # The reason quotes the PHRASE, not the posting's regions: a Dubai
            # posting that demands the "right to work in the uk" was reported as
            # refusing sponsorship "(other)", naming a region the matched phrase
            # never mentioned. The evidence is the phrase; the geography is a
            # separate fact, and stapling them together made the string a lie.
            return ([], [], True,
                    f'posting refuses visa sponsorship or requires an existing '
                    f'right to work (matched: "{refusal}")')
        if _any_keyword(text, self._offer_phrases):
            return ([f"work auth: employer offers visa sponsorship ({named})"],
                    [], False, "")
        if is_sponsoring_employer(posting, self._sponsoring_employers):
            return ([], [f"work auth: needs visa sponsorship ({named}); "
                         f"{posting.company_slug} is a known sponsoring employer"],
                    False, "")
        return ([], [], True,
                f"needs visa sponsorship ({named}); employer is not a known "
                f"sponsor and the posting does not offer it")

    def _needs_sponsorship(self, region: str) -> bool:
        """The OWNER's status in a region, from the SSOT. A region the SSOT does
        not state (Canada, "other") is assumed to NEED sponsorship: the owner's
        ruling is that everywhere outside the EU/CH does, and the safe default for
        an unstated region is the one that cannot surface an impossible job."""
        return self._sponsorship_by_region.get(region, True)

    def _role_gate(self, posting: Posting):
        """DISCARD CHANNEL 6 (W5.1e, owner ruling 16): the role itself.

        (matched, weak, discard, reason). Product, design, recruiting, program
        management, executive assistance, sales and marketing roles are filtered
        OUT, with a carve-out for FANG-like employers (where the owner would take
        them). The SAME allowlist serves the work-auth carve-out: one definition,
        two consumers, so they cannot drift apart.

        Classified from the TITLE'S HEAD SEGMENT ONLY, and this is the whole
        point. The family gate matches keywords over title + DESCRIPTION, so an
        AI-flavoured job description made ANY role read as tier-1 AI/ML:
        "Technical Recruiter, AI Research" scored 84, identical to "Machine
        Learning Engineer". The description is the SUBJECT MATTER; the title is
        the ROLE. Reading only the head (the text before the first comma, colon,
        pipe, bracket or spaced dash) also stops an AI-flavoured title SUFFIX from
        promoting a non-engineering head, which is the same confusion one level
        down.
        """
        if not self._role_gate_on:
            return [], [], False, ""
        head = _title_head(posting.title)
        hits = [k for k in self._non_engineering_titles if _phrase_present(k, head)]
        if not hits:
            return [], [], False, ""
        # A genuine engineering noun in the SAME head segment wins: a "Generalist
        # Software Engineer" is an engineer, a "Safeguards Generalist" is not.
        if _any_keyword(head, self._engineering_nouns):
            return [], [], False, ""
        if is_sponsoring_employer(posting, self._sponsoring_employers):
            return ([], [f"role: non-engineering ({hits[0]}); admitted for "
                         f"{posting.company_slug} (FANG-like carve-out)"],
                    False, "")
        return [], [], True, f"non-engineering role family: {hits[0]}"

    def _excludes_gate(self, posting: Posting):
        """Owner's explicit never-show terms -> discard. Preserves the exclude
        list as a hard filter in the modern discard channel."""
        if not self._excludes_discard:
            return False, ""
        excludes = [e.lower() for e in self.profile.get("excludes", [])]
        text = f"{posting.title} {posting.description}".lower()
        hits = [e for e in excludes if e in text]
        if hits:
            return True, f"excluded term: {', '.join(hits)}"
        return False, ""

    # -- soft_fit axes (additive, weights in config, sum 1.0) ------------------

    def _skills_overlap(self, posting: Posting):
        candidate = self.profile.get("skill_tokens", [])
        text = f"{posting.title} {posting.description}"
        sub, hits = skills_overlap_sub(candidate, text, self._skills_cfg)
        if hits:
            return sub, [f"skills: {', '.join(hits)}"], []
        return sub, [], ["no canonical skill overlap"]

    def _role_title_fit(self, posting: Posting):
        roles = self.profile.get("roles", [])
        if not roles:
            return 0.5, [], ["roles unset"]
        best = max(self.similarity.score(r, posting.title) for r in roles)
        if best >= 0.5:
            return best, [f"role: {posting.title}"], []
        return best, [], ["role title match weak"]

    def _location_fit(self, posting: Posting):
        # Eligibility-aware, deterministic, pure function of posting + profile.
        # A concrete profile-location match is best (1.0). Failing that, a remote
        # posting is graded by where its remote actually lets you work: EU/EMEA/
        # global-friendly -> 1.0, US/CA/AU-only -> 0.4, unmarked -> 0.7. This
        # stops US-only "remote" roles from scoring as if EU-eligible.
        prefs = [p.lower() for p in self.profile.get("locations", [])]
        for loc in posting.locations:
            if _profile_location_hit(loc, prefs):
                return 1.0, [f"location: {loc}"], []
        if posting.remote_flag and self.profile.get("remote_ok"):
            return _remote_eligibility(posting.locations, prefs)
        return 0.3, [], ["location mismatch"]

    def _term_length_fit(self, posting: Posting):
        # NEW axis: fixed-term / contract / internship / N-month / 1-year are the
        # first-class bridge shape (the owner wants a short-term bridge role).
        text = f"{posting.title} {posting.description}"
        sub, label = term_length_sub(text, self._term_cfg)
        if sub >= self._term_cfg.get("bridge_score", 1.0):
            return sub, [f"term: {label}"], []
        if sub <= self._term_cfg.get("poor_score", 0.4):
            return sub, [], [f"term: {label}"]
        return sub, [], []  # permanent/unspecified is neutral, no note

    def _comp_fit(self, posting: Posting):
        floor = self.profile.get("comp_floor") or self._comp_cfg.get("default_floor")
        unknown = self._comp_cfg.get("unknown_score", 0.5)
        if posting.comp is None:
            return unknown, [], ["comp unknown"]
        if floor is None:
            return unknown, [], ["comp floor unset"]
        top = _max_amount(posting.comp)
        if top is not None and top >= floor:
            return self._comp_cfg.get("match_score", 1.0), \
                [f"comp: {posting.comp}"], []
        return self._comp_cfg.get("below_floor_score", 0.3), [], \
            [f"comp below floor: {posting.comp}"]

    def _eligibility_fit(self, posting: Posting):
        # Soft QUALITY signal only. The binary question ("may he legally take this
        # job?") is now the work-auth GATE above; this axis just grades what is
        # left. It keeps its own weight, but it is no longer the only thing
        # standing between the owner and a job he cannot take.
        #
        # W5.1e: the axis used to CLAIM "eligibility: EU work rights" for any
        # posting that tripped none of its keywords, including every remote US
        # role and every UK role. It now asserts EU rights only where the posting
        # actually reaches a region the owner may work in, and says nothing when
        # the geography is unstated. Never claim a right the posting does not give.
        cfg = self._eligibility_cfg
        text = f"{posting.title} {posting.description}".lower()
        caps = {c.lower() for c in self.profile.get("capabilities", [])}
        has_us = "work_authorization_us" in caps
        sub = cfg.get("eu_score", 1.0)
        matched, weak = [], []
        if (_any_keyword(text, cfg.get("us_signal_keywords", []))
                or _location_needs_sponsorship(posting)) and not has_us:
            sub = cfg.get("us_sponsorship_score", 0.2)
            weak.append("needs visa sponsorship")
        if (_any_keyword(text, cfg.get("degree_required_keywords", []))
                and not cfg.get("degree_in_hand", False)):
            weak.append(cfg.get("degree_warn", "degree not yet in hand"))
            sub = min(sub, cfg.get("degree_pending_score", sub))
        if not weak and self._free_to_work_here(posting):
            matched.append("eligibility: EU work rights")
        return sub, matched, weak

    def _free_to_work_here(self, posting: Posting) -> bool:
        """Does the posting actually reach a region the owner may work in? With no
        owner facts (gate inactive) this falls back to the location reading, so the
        axis behaves exactly as before on an SSOT that states no work-auth."""
        regions = posting_regions(posting)
        if not self._sponsorship_by_region:
            return not _location_needs_sponsorship(posting)
        return any(not self._needs_sponsorship(r) for r in regions)


def ats_precheck(posting: Posting, profile: dict, rules: list[dict]) -> list[str]:
    """show-and-warn (D5): warn when a hard filter is unmet; never hide."""
    caps = {c.lower() for c in profile.get("capabilities", [])}
    text = f"{posting.title} {posting.description}".lower()
    warnings = []
    for rule in rules:
        pattern = rule.get("pattern", "")
        capability = rule.get("capability", "")
        if pattern and re.search(pattern, text) and capability.lower() not in caps:
            warnings.append(f"may fail ATS: missing {capability}")
    return warnings


def profile_from_ssot(ssot: SSOT) -> dict:
    """Extract the matching profile from the read-only SSOT preferences block,
    plus the canonical skill_tokens and experience_years the redesign added."""
    prefs = ssot.get("preferences")
    profile: dict = {}
    if prefs is not MISSING and isinstance(prefs, dict):
        keys = ("roles", "skills", "seniority", "locations", "remote_ok",
                "comp_floor", "excludes", "capabilities")
        profile = {k: prefs[k] for k in keys if prefs.get(k) not in (None, [], "")}
    experience_years = ssot.experience_years()
    if experience_years is not None:
        profile["experience_years"] = experience_years
    skill_tokens = ssot.skill_tokens()
    if skill_tokens:
        profile["skill_tokens"] = skill_tokens
    return profile


# -- family classification (W4 redesign) -------------------------------------
# The family GATE replaces the old naive max-token role_fit gate: a posting in
# the wrong family collapses the whole score instead of being averaged away.

def classify_family(text: str, cfg: dict) -> tuple[int, float]:
    """Classify posting text into (tier, multiplier). Tier 1 (AI/ML) is checked
    before tier 2 (data/cloud/SWE-adjacent); the first matching keyword set wins.
    No match -> (0, 0.0), the caller decides discard vs softened show-and-warn."""
    low = text.lower()
    if _any_keyword(low, cfg.get("tier1_keywords", [])):
        return 1, cfg.get("tier1_multiplier", 1.0)
    if _any_keyword(low, cfg.get("tier2_keywords", [])):
        return 2, cfg.get("tier2_multiplier", 0.75)
    return 0, 0.0


# -- seniority parsing (W4 redesign; the core fix) ---------------------------
# Bands: entry(0) < mid(1) < senior(2) < principal(3). Both the owner's band
# (from experience_years) and the posting's required band (from title+JD level
# keywords AND "N+ years" phrasing) map through the same config year thresholds.

_BAND_ORDER = (("principal", 3), ("senior", 2), ("mid", 1), ("entry", 0))
_REQUIRED_YEARS_RE = re.compile(r"(\d+)\s*\+?\s*(?:-\s*\d+)?\s*years?",
                                re.IGNORECASE)


def parse_required_band(title: str, jd: str, cfg: dict) -> int:
    """Required seniority band from title+JD: the max of the level-keyword bands
    and the 'N+ years' band. Unspecified -> entry (0), per the ratified table."""
    text = f"{title} {jd}".lower()
    keywords = cfg.get("keywords", {})
    band = 0
    for name, value in _BAND_ORDER:
        if _any_keyword(text, keywords.get(name, [])):
            band = max(band, value)
    years = _parse_required_years(text)
    if years is not None:
        band = max(band, _years_to_band(years, cfg))
    return band


def owner_band_from_years(years, cfg: dict) -> int:
    """Owner seniority band from experience_years; missing -> entry (0)."""
    if years is None:
        return 0
    return _years_to_band(float(years), cfg)


def _years_to_band(years: float, cfg: dict) -> int:
    bands = cfg.get("year_bands", {})
    if years >= bands.get("principal_min", 8.0):
        return 3
    if years >= bands.get("senior_min", 5.0):
        return 2
    if years >= bands.get("mid_min", 2.0):
        return 1
    return 0


def _parse_required_years(text: str) -> int | None:
    """First N in 'N+ years' / 'N-M years' / 'minimum N years' phrasing."""
    match = _REQUIRED_YEARS_RE.search(text)
    return int(match.group(1)) if match else None


# -- skills overlap (W4 redesign; the detection fix) -------------------------

def skills_overlap_sub(candidate, text: str, cfg: dict) -> tuple[float, list[str]]:
    """(sub, matched_tags). sub = fraction of the posting's DETECTED canonical
    skills the candidate holds, with a Jaccard backstop; a posting that names no
    recognised skill is neutral (no_required_neutral). The old matcher stored 49
    verbose skill sentences and diluted the denominator so this never fired."""
    vocab = cfg.get("vocabulary", [])
    aliases = cfg.get("aliases", {})
    low = text.lower()
    required = {tag for tag in vocab
               if _skill_present(tag, aliases.get(tag, []), low)}
    have = {str(c).strip().lower() for c in candidate if str(c).strip()}
    matched = required & have
    if required:
        primary = len(matched) / len(required)
    else:
        primary = cfg.get("no_required_neutral", 0.5)
    union = required | have
    jaccard = len(matched) / len(union) if union else 0.0
    return max(primary, jaccard), sorted(matched)


def _skill_present(tag: str, aliases, low: str) -> bool:
    """A canonical tag is present if the tag OR any surface-form alias appears as
    a whole word/phrase (posting text rarely uses the bare canonical tag)."""
    return any(_phrase_present(form, low) for form in (tag, *aliases))


# -- term length (W4 redesign; NEW axis) -------------------------------------

_MONTHS_RE = re.compile(r"(\d+)\s*[-\s]?\s*month", re.IGNORECASE)
_MESI_RE = re.compile(r"(\d+)\s*mes[ei]", re.IGNORECASE)
# A year count only reads as a CONTRACT duration when a contract word follows it,
# so "5 years experience" (a seniority signal) is not mistaken for a term length.
_YEAR_TERM_RE = re.compile(
    r"(\d+)\s*[-\s]?\s*year[-\s]+"
    r"(?:contract|programme|program|fixed|placement|scheme|track|position|role)",
    re.IGNORECASE)
_ONE_YEAR_RE = re.compile(r"\b(?:one|1)\s*[-\s]?\s*year\b", re.IGNORECASE)


def term_length_sub(text: str, cfg: dict) -> tuple[float, str]:
    """(sub, label). Fixed-term/contract/internship and explicit durations in
    [min_months, max_bridge_months] are the first-class bridge; permanent /
    unspecified is neutral; multi-year or sub-min_months is a poor bridge."""
    low = text.lower()
    months = _term_months(low)
    if months is not None:
        if (months < cfg.get("min_months", 3)
                or months > cfg.get("max_bridge_months", 18)):
            return cfg.get("poor_score", 0.4), "off-bridge duration"
        return cfg.get("bridge_score", 1.0), "bridge duration"
    if _any_keyword(low, cfg.get("multi_year_keywords", [])):
        return cfg.get("poor_score", 0.4), "multi-year track"
    if _any_keyword(low, cfg.get("bridge_keywords", [])):
        return cfg.get("bridge_score", 1.0), "fixed-term/contract"
    if _any_keyword(low, cfg.get("permanent_keywords", [])):
        return cfg.get("permanent_score", 0.7), "permanent"
    return cfg.get("permanent_score", 0.7), "unspecified"


def _term_months(low: str) -> int | None:
    match = _MONTHS_RE.search(low) or _MESI_RE.search(low)
    if match:
        return int(match.group(1))
    year_term = _YEAR_TERM_RE.search(low)
    if year_term:
        return int(year_term.group(1)) * 12
    if _ONE_YEAR_RE.search(low):
        return 12
    return None


def _location_needs_sponsorship(posting: Posting) -> bool:
    """A posting whose locations are ALL non-EU implies the owner would need visa
    sponsorship.

    W5.1e: the `remote_flag` short-circuit is GONE. It returned False for every
    remote posting anywhere on earth, so a "Remote - US" role took no eligibility
    penalty at all and was credited with "EU work rights". A remote role still has
    a work-eligibility geography: judge it by its locations, exactly like an
    on-site one. A posting that states NO location stays unjudged (no locations ->
    False), which is an absence of evidence, not evidence of eligibility.
    """
    locations = posting.locations or []
    return bool(locations) and all(_marks_non_eu(loc) for loc in locations)


def _any_keyword(low: str, keywords) -> bool:
    """True when any config keyword/phrase appears as a whole word/phrase."""
    return any(_phrase_present(str(k).lower(), low) for k in keywords)


def _first_keyword(low: str, keywords) -> str:
    """The first keyword/phrase actually present, or "" when none is. The gate
    quotes it, so a discard reason states the EVIDENCE it fired on instead of
    paraphrasing it into a claim the posting never made."""
    for keyword in keywords:
        phrase = str(keyword).lower()
        if _phrase_present(phrase, low):
            return phrase
    return ""


def _clearance_required(low: str, phrases) -> bool:
    """A clearance phrase in a REQUIREMENT context, read SECTION-AWARE.

    The first cut of this read a plus-or-minus-100-character window around the
    match. That window cannot see the SOFT-QUALIFICATION HEADER, which in a real
    JD sits several bullets ABOVE the mention ("Strong candidates may also have",
    "You might thrive in this role if you"), and 4 live postings the owner wanted
    were deleted for a clearance their JD listed as a BONUS. Proximity was the
    wrong instrument; the SECTION is the evidence.

    Three readings, in order of how directly they speak:

    1. the matched PHRASE ITSELF demands ("must hold a security clearance",
       "security clearance is required") -> REQUIRED, whatever surrounds it;
    2. otherwise the phrase is a bare MENTION ("active ts/sci"), and the LINE it
       sits on decides: a soft cue on the line ("strongly preferred", "strong
       preference", "nice to have") makes it a preference, an explicit demand on
       the line ("(required)", "must have", "in order to qualify") makes it a
       requirement. The line is read with the matched span EXCLUDED, so a phrase
       cannot veto itself on its own words;
    3. otherwise the nearest SECTION HEADER above the mention decides.

    THE ASYMMETRY OF HARM DECIDES THE DEFAULT: a false admit wastes one lead, a
    false discard silently deletes a job the owner wanted. When no reading says
    "required", the mention is SOFT and the posting is ADMITTED.
    """
    text = low.replace("’", "'")  # a curly apostrophe in "you'll thrive"
    for keyword in phrases:
        phrase = str(keyword).lower()
        if not phrase:
            continue
        for hit in re.finditer(rf"(?<!\w){re.escape(phrase)}(?!\w)", text):
            if _clearance_hit_is_hard(text, phrase, hit.start(), hit.end()):
                return True
    return False


def _clearance_hit_is_hard(text: str, phrase: str, start: int, end: int) -> bool:
    """One clearance match: REQUIREMENT (True) or PREFERENCE (False)."""
    if _has_cue(phrase, _CLEARANCE_HARD_CUES):
        return True                       # the phrase IS the demand
    line_start, line_end = _line_bounds(text, start, end)
    line = text[line_start:start] + " " + text[end:line_end]
    if _has_cue(line, _CLEARANCE_SOFT_CUES):
        return False
    if _has_cue(line, _CLEARANCE_HARD_CUES):
        return True
    return _nearest_header_is_hard(text[:start])


def _line_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """The BULLET/LINE carrying the match. Capped, so a JD delivered as one
    newline-less blob is still read locally instead of whole."""
    low_edge = max(0, start - _CLEARANCE_LINE_CAP)
    cut = text.rfind("\n", low_edge, start)
    line_start = cut + 1 if cut != -1 else low_edge
    high_edge = min(len(text), end + _CLEARANCE_LINE_CAP)
    cut = text.find("\n", end, high_edge)
    return line_start, (cut if cut != -1 else high_edge)


def _nearest_header_is_hard(prefix: str) -> bool:
    """The SECTION the mention sits in, from the nearest header ABOVE it.

    No header found at all leaves the mention unjudgeable, which is an ADMIT: the
    owner loses a lead, never a job.
    """
    soft = max((prefix.rfind(h) for h in _CLEARANCE_SOFT_HEADERS), default=-1)
    hard = max((prefix.rfind(h) for h in _CLEARANCE_HARD_HEADERS), default=-1)
    return hard > soft


def _has_cue(text: str, cues) -> bool:
    return any(cue in text for cue in cues)


def _phrase_present(phrase: str, low: str) -> bool:
    """Whole-word/phrase match tolerant of special chars (c++) and spaces
    (machine learning): non-word boundaries either side, not \\b (which breaks on
    '+'). Empty phrase never matches."""
    if not phrase:
        return False
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", low) is not None


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _word_present(needle: str, haystack: str) -> bool:
    """Case-insensitive whole-word match of `needle` in `haystack`."""
    if not needle:
        return False
    return re.search(rf"\b{re.escape(needle.lower())}\b",
                     (haystack or "").lower()) is not None


# -- location eligibility classification (deterministic, no network) ----------
# Work-arrangement words that are NOT a place: excluded from the profile-location
# match so a bare "Remote" posting does not read as a concrete location hit.
_GENERIC_LOCATION_TOKENS = frozenset({
    "remote", "hybrid", "anywhere", "onsite", "on-site", "in-office", "office",
    "flexible", "worldwide", "global",
})
# W5.1e: WORK-ELIGIBILITY EUROPE, not geographic Europe. The UK left this set
# (post-Brexit an EU citizen has NO automatic right to work there), and it is the
# load-bearing correction: while "london" sat in _EU_CITIES, every other work-auth
# fix was defeated -- a UK role was affirmatively credited with EU work rights.
# EEA (norway/iceland/liechtenstein) and CH stay: free movement, no sponsorship.
# Italy is first-class per the profile; the profile's own location tokens are
# unioned in at runtime.
_EU_COUNTRIES = frozenset({
    "italy", "france", "germany", "spain", "portugal", "netherlands", "belgium",
    "luxembourg", "ireland", "austria", "greece", "poland", "czechia", "czech",
    "slovakia", "slovenia", "hungary", "romania", "bulgaria", "croatia",
    "estonia", "latvia", "lithuania", "finland", "sweden", "denmark", "malta",
    "cyprus", "norway", "iceland", "liechtenstein", "switzerland",
})
# Major EU/EEA/CH cities (whole-word, case-insensitive). A bare EU city with no
# country ("Sofia", "Eindhoven", "Tampere") used to fail to place, which cost the
# posting its "free to work" credit and left it merely warned, so the list is
# widened to the cities the live corpus actually names.
_EU_CITIES = frozenset({
    "milan", "milano", "rome", "roma", "turin", "torino", "naples", "florence",
    "bologna", "genoa", "verona", "padua", "bari", "catania", "palermo",
    "dublin", "cork", "galway", "berlin", "munich", "hamburg", "frankfurt",
    "cologne", "stuttgart", "dusseldorf", "dortmund", "leipzig", "dresden",
    "nuremberg", "hannover", "bremen", "paris", "lyon", "marseille",
    "toulouse", "bordeaux", "nantes", "lille", "strasbourg", "nice",
    "madrid", "barcelona", "valencia", "seville", "malaga", "bilbao",
    "lisbon", "porto", "braga", "amsterdam", "rotterdam", "utrecht",
    "eindhoven", "the hague", "brussels", "antwerp", "ghent", "vienna",
    "graz", "zurich", "geneva", "basel", "lausanne", "stockholm",
    "gothenburg", "malmo", "copenhagen", "aarhus", "oslo", "bergen",
    "helsinki", "espoo", "tampere", "turku", "reykjavik", "warsaw", "krakow",
    "wroclaw", "gdansk", "poznan", "lodz", "prague", "brno", "bratislava",
    "budapest", "bucharest", "cluj", "timisoara", "iasi", "sofia", "athens",
    "thessaloniki", "tallinn", "riga", "vilnius", "ljubljana", "zagreb",
    # Malta's capital, absent until now: with "MT" also a US state code, the bare
    # "Valletta, MT" form placed as US ONLY and a job the owner may legally take
    # was discarded. A city named here is what lets the set-valued classifier read
    # {eu, us} and admit on the free region.
    "valletta",
    # W5.1e round 3: the 13 live EU postings the map could not place. They failed
    # OPEN (warned, admitted), so none was lost, but each lost its "free to work"
    # credit and took the eligibility penalty: score pressure on jobs dead-centre
    # of the owner's profile ("AI Engineer - Graduate Development Program", Trento;
    # deliveroo Genova/Monza/Brescia). Italian ENDONYMS ("genova" beside the
    # already-listed English "genoa"), Italian regions used as a location, and
    # Lithuania's second city.
    "genova", "pisa", "trento", "monza", "brescia", "liguria", "lombardia",
    "kaunas",
})
# The UK: a distinct work-eligibility region (sponsorship-required for the owner).
# "northern ireland" is matched here and the Republic ("ireland"/"dublin") in the
# EU set above; the region classifier tries the UK FIRST so Belfast reads as UK.
# "u.k." is NOT in this set: a whole-word match can never fire on it (the closing
# "." has no word character after it, so `\b` fails), which left "Remote, U.K."
# unplaced. The dotted abbreviation gets its own regex, _UK_DOTTED_RE.
_UK_COUNTRIES = frozenset({
    "united kingdom", "uk", "great britain", "britain", "england",
    "scotland", "wales", "northern ireland",
})
_UK_CITIES = frozenset({
    "london", "manchester", "edinburgh", "birmingham", "glasgow", "bristol",
    "leeds", "liverpool", "belfast", "cardiff", "sheffield", "nottingham",
})
# Switzerland is inside _EU_COUNTRIES (free to work, so it needs no separate
# eligibility branch), but the SSOT states it as its own region, so the classifier
# names it to keep the gate's reason line truthful.
_CH_WORDS = frozenset({
    "switzerland", "zurich", "geneva", "basel", "lausanne", "bern", "zug",
    "lugano",
})
_CA_WORDS = frozenset({
    "canada", "toronto", "vancouver", "montreal", "ottawa", "calgary",
})
# GEOGRAPHIC Europe: the UK is in Europe for TRAVEL purposes even though it is
# not for WORK-RIGHTS purposes. Conflating the two is the root confusion this
# wave exists to undo, so the commute gate keeps its own (unchanged) notion and
# the work-auth gate gets the eligibility one. -- W5.1e
_EUROPE_COUNTRIES = _EU_COUNTRIES | _UK_COUNTRIES
_EUROPE_CITIES = _EU_CITIES | _UK_CITIES
# Region markers that place a remote posting IN EUROPE.
# W5.1e round 2: "global"/"anywhere"/"worldwide" are NOT among them. They state no
# work-eligibility geography at all, and reading them as EU told the owner "work
# auth: free to work (eu)" about 93 live 'Home based - Worldwide' postings -- a
# claim the posting never made. An unstated geography is UNPLACEABLE: warned,
# never discarded, and never credited.
_EU_REGION_MARKERS = frozenset({
    "eu", "eea", "europe", "european", "emea",
})
# Non-EU country/city names (whole-word, case-insensitive), split by region so
# the work-auth gate can NAME the region it discarded on (W5.1e). _NON_EU_WORDS
# stays the union, so `_marks_non_eu` keeps its existing meaning.
# W5.1e: extended from the LIVE corpus, never from imagination. The classifier was
# run over the 7.7k live postings and every location fragment it failed to place
# was inspected; these are the ones that actually occur. An unplaced fragment fails
# OPEN (admitted with a "region not determined" note), so a gap here is a job the
# owner cannot take reaching his digest: "Remote - California" and "Ramat Gan,
# Israel" were both sailing through as unknown geography.
_US_WORDS = frozenset({
    "united states", "new york", "san francisco", "seattle", "boston",
    "chicago", "austin", "denver", "atlanta", "los angeles", "washington",
    "mountain view", "palo alto", "menlo park", "san jose", "dallas",
    "houston", "miami", "sunnyvale", "detroit", "philadelphia", "phoenix",
    "portland", "san diego", "sacramento", "minneapolis", "pittsburgh",
    "charlotte", "raleigh", "nashville", "columbus", "indianapolis",
    "salt lake city", "las vegas", "orlando", "tampa", "santa clara",
    "redmond", "bellevue", "irvine", "cupertino",
    # Full state names: the postal-code rule only fires in "City, XX" form, so a
    # bare "Virginia" or "Remote - California" was reading as no-geography.
    "california", "texas", "virginia", "maryland", "massachusetts",
    "new jersey", "illinois", "oregon", "michigan", "ohio", "colorado",
    "arizona", "florida", "pennsylvania", "minnesota", "wisconsin", "missouri",
    "tennessee", "utah", "nevada", "oklahoma", "kansas", "iowa", "indiana",
    "kentucky", "alabama", "louisiana", "arkansas", "mississippi", "nebraska",
    "idaho", "montana", "wyoming", "vermont", "connecticut", "rhode island",
    "north carolina", "south carolina", "north dakota", "south dakota",
    "new hampshire", "new mexico", "west virginia", "hawaii", "alaska",
})
# Israel (101 live postings), Asia, the Gulf, the non-EU Balkans and South America:
# all sponsorship-required for the owner, all previously unplaced.
#
# Keyed word -> the PLACE THE OWNER WAS REJECTED FOR, because the reason line has to
# say it. "needs visa sponsorship (other)" appeared in 521 live reason strings and
# named nothing: "other" is not a place, and the owner cannot tell an Israeli role
# from a Japanese one from a reason that calls both the same. The word list is
# DERIVED from this map, so a place can never be classifiable and unnameable.
_OTHER_COUNTRY_BY_WORD = {
    "australia": "Australia", "sydney": "Australia", "melbourne": "Australia",
    "brisbane": "Australia", "perth": "Australia",
    "singapore": "Singapore",
    "india": "India", "bangalore": "India", "bengaluru": "India",
    "japan": "Japan", "tokyo": "Japan",
    "brazil": "Brazil", "sao paulo": "Brazil", "são paulo": "Brazil",
    "mexico": "Mexico",
    "israel": "Israel", "tel aviv": "Israel", "jerusalem": "Israel",
    "haifa": "Israel", "ramat gan": "Israel", "petah tikva": "Israel",
    "herzliya": "Israel",
    "south korea": "South Korea", "seoul": "South Korea",
    "china": "China", "beijing": "China", "shanghai": "China",
    "shenzhen": "China", "hong kong": "Hong Kong",
    "taiwan": "Taiwan", "taipei": "Taiwan",
    "uae": "UAE", "dubai": "UAE", "abu dhabi": "UAE",
    "qatar": "Qatar", "doha": "Qatar",
    "saudi arabia": "Saudi Arabia", "riyadh": "Saudi Arabia",
    "turkey": "Turkey", "istanbul": "Turkey",
    "serbia": "Serbia", "belgrade": "Serbia", "montenegro": "Montenegro",
    "bosnia": "Bosnia", "kosovo": "Kosovo", "albania": "Albania",
    "moldova": "Moldova", "north macedonia": "North Macedonia",
    "ukraine": "Ukraine", "kyiv": "Ukraine",
    "argentina": "Argentina", "chile": "Chile", "colombia": "Colombia",
    "peru": "Peru", "south africa": "South Africa",
    "new zealand": "New Zealand", "auckland": "New Zealand",
    "thailand": "Thailand", "bangkok": "Thailand", "vietnam": "Vietnam",
    "philippines": "Philippines", "manila": "Philippines",
    "indonesia": "Indonesia", "jakarta": "Indonesia",
    "malaysia": "Malaysia", "kuala lumpur": "Malaysia",
    "pakistan": "Pakistan", "egypt": "Egypt", "nigeria": "Nigeria",
    "kenya": "Kenya",
    # Continent/region markers that exclude Europe.
    "north america": "North America", "south america": "South America",
    "americas": "the Americas", "amer": "the Americas", "latam": "LATAM",
    "apac": "APAC", "asia pacific": "APAC",
}
_OTHER_NON_EU_WORDS = frozenset(_OTHER_COUNTRY_BY_WORD)
_NON_EU_WORDS = _US_WORDS | _CA_WORDS | _OTHER_NON_EU_WORDS
# Case-SENSITIVE standalone country abbreviations (avoid "US" matching "USte"
# / "plus" and "USA" matching itself via a word marker instead).
_US_COUNTRY_ABBREVS = frozenset({"US", "USA"})
# US state postal codes, matched case-sensitively AND only in "City, XX" form so
# ambiguous English words ("OR", "IN", "OK", "ME", "HI", "DE") do not misfire.
_US_STATE_CODES = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
})


# -- W5.1e WORK-AUTH GATE: region classification (deterministic, no network) ---
# A posting's work-eligibility geography, derived INDEPENDENTLY of remote_flag.
# "Remote, United Kingdom" is a UK role: remote work still happens somewhere, and
# the old code threw the geography away the moment remote_flag was set, handing a
# free "EU work rights" credit to every remote US/UK/Canada posting.

# Free-to-work regions are NOT hardcoded: they come from the owner's SSOT map
# (region -> needs sponsorship). These are just the region NAMES the classifier
# can emit; an EMPTY set means the posting states no geography at all, which is
# an absence of evidence, never evidence of impossibility.
#
# One location string can list several places ("Remote, Canada; Remote, United
# Kingdom" arrives as a SINGLE element), so split before classifying. The comma is
# deliberately NOT a separator here: it would shatter "Cambridge, MA" into
# "Cambridge" (a UK city) and "MA", reading Cambridge-Massachusetts as a UK role.
# Comma-joined fragments are handled by classifying each fragment into a SET of
# regions instead (see _regions_of).
_LOCATION_SPLIT_RE = re.compile(r"[;/|]| or ")
# The dotted UK abbreviation, which no whole-word list entry can match.
_UK_DOTTED_RE = re.compile(r"(?<![a-z0-9])u\.k\.?(?![a-z0-9])")


def _regions_of(part: str) -> set[str]:
    """One location fragment -> EVERY work-eligibility region it names.

    SET-VALUED, never first-match-wins. 1,082 of the 7,714 live postings list
    several places inside ONE unsplit fragment ("San Francisco, Singapore,
    Amsterdam", "Hybrid - San Francisco, New York City, London, Berlin", "United
    States & EMEA"), and reading only the first region made every later one
    INVISIBLE to the gate: an Amsterdam inference-engineering role, dead centre of
    the owner's profile, was deleted as if it were US-only.

    Collecting every region present also dissolves the ordering hazard the
    first-match reading had to tiptoe around: "Cambridge, MA" is {uk, us}, neither
    of which is workable, so it still discards, and no ordering decides it. The
    gate's ADMIT-IF-ANY rule (_work_auth_gate) then keeps exactly the postings
    carrying at least one region the owner may work in.

    The one containment trap: "northern ireland" CONTAINS the EU country token
    "ireland", so the UK country names are masked out of the fragment before the
    EU pass (Belfast is UK; Dublin is EU).
    """
    low = _fold(part)
    regions: set[str] = set()
    if _uk_country_hit(low) or _word_hit(low, _UK_CITIES):
        regions.add("uk")
    if _looks_us(part):
        regions.add("us")
    if _word_hit(low, _CA_WORDS):
        regions.add("ca")
    if _word_hit(low, _OTHER_NON_EU_WORDS):
        regions.add("other")
    if _word_hit(low, _CH_WORDS):
        regions.add("ch")
    eu_text = _mask_phrases(low, _UK_COUNTRIES)
    if (_word_hit(eu_text, _EU_COUNTRIES) or _word_hit(eu_text, _EU_CITIES)
            or _word_hit(eu_text, _EU_REGION_MARKERS)):
        regions.add("eu")
    return regions


def _mask_phrases(low: str, phrases) -> str:
    """Blank out whole phrases before a later pass reads a token INSIDE one."""
    for phrase in phrases:
        low = re.sub(rf"\b{re.escape(_fold(phrase))}\b", " ", low)
    return low


def _uk_country_hit(low: str) -> bool:
    """The UK by country name, including the dotted "U.K." the lists cannot hold."""
    return (_word_hit(low, _UK_COUNTRIES)
            or _UK_DOTTED_RE.search(_fold(low)) is not None)


def _looks_us(part: str) -> bool:
    """US country name/abbreviation, a US city, or a "City, XX" state code.

    The state-code rule fires on ISO country codes too ("Berlin, DE"), which is
    why the classifier is set-valued: such a fragment reads {us, eu}, and the free
    EU region admits it. A first-match reading would have deleted it as US-only.
    """
    if _word_hit(part.lower(), _US_WORDS):
        return True
    if any(re.search(rf"(?<![A-Za-z]){abbr}(?![A-Za-z])", part)
           for abbr in _US_COUNTRY_ABBREVS):
        return True
    return any(re.search(rf",\s*{code}\b", part) for code in _US_STATE_CODES)


def posting_regions(posting: Posting) -> set[str]:
    """Every work-eligibility region the posting's locations imply: the UNION over
    every fragment of every location string. Never keyed on remote_flag: a remote
    role still has a work-eligibility geography. An empty set means UNPLACEABLE."""
    return set().union(set(), *posting_region_fragments(posting).values())


def posting_region_fragments(posting: Posting) -> dict[str, set[str]]:
    """Each location FRAGMENT -> the regions it names. A fragment mapping to the
    EMPTY set states a geography the map cannot place.

    The union alone cannot answer the question the gate has to ask. canonical's
    'Home based - Worldwide; Office Based - Taipei, Taiwan' unions to {other} and
    was DISCARDED as a Taiwan role, because the "Worldwide" half placed nowhere and
    the union silently swallowed it. Worldwide INCLUDES the places the owner may
    work, so that half is the one that decides. Keeping the fragments apart is what
    lets an unplaceable one FAIL OPEN (see _work_auth_gate).
    """
    fragments: dict[str, set[str]] = {}
    for loc in posting.locations or []:
        for part in _LOCATION_SPLIT_RE.split(loc):
            if part.strip():
                fragments[part.strip()] = _regions_of(part)
    return fragments


def _region_names(posting: Posting, regions) -> str:
    """The regions, as the OWNER reads them in a discard reason. "other" is not a
    place: it is the classifier's bucket, and a reason line that says "needs visa
    sponsorship (other)" (521 live strings) never told him whether it had just
    deleted an Israeli role or a Japanese one. The bucket keeps its name AND names
    the country it stands for."""
    names = []
    for region in sorted(regions):
        if region != "other":
            names.append(region)
            continue
        countries = _other_country_names(posting)
        names.append(f"other: {', '.join(countries)}" if countries else "other")
    return ", ".join(names)


def _other_country_names(posting: Posting) -> list[str]:
    """Every non-EU place named by the locations, by its NAME."""
    named = []
    for loc in posting.locations or []:
        low = _fold(loc)
        for word, country in _OTHER_COUNTRY_BY_WORD.items():
            if country not in named and _word_hit(low, (word,)):
                named.append(country)
    return sorted(named)


def unplaceable_fragments(posting: Posting) -> list[str]:
    """Location fragments that CLAIM A GEOGRAPHY the region map cannot place.

    A fragment is only unplaceable if something is LEFT of it once the pure
    work-MODE words are removed. This is the whole distinction: 'Remote-Friendly
    (Travel-Required)' states no geography at all and is correctly ignored (it sits
    beside "San Francisco, CA | Washington, DC", which is the real geography),
    whereas 'Home based - Worldwide' does make a geographic claim, one that INCLUDES
    the EU, and dropping it deleted a job the owner could have taken.
    """
    unplaceable = []
    for fragment, regions in posting_region_fragments(posting).items():
        if regions:
            continue
        residue = _tokens(_fold(fragment)) - _WORK_MODE_TOKENS
        if residue:
            unplaceable.append(fragment)
    return unplaceable


# Words that describe HOW the work happens, not WHERE. A fragment made only of these
# is not a location and never triggers the fail-open above.
_WORK_MODE_TOKENS = frozenset({
    "remote", "remotely", "friendly", "hybrid", "onsite", "on", "site", "in",
    "office", "based", "home", "work", "from", "travel", "required", "optional",
    "flexible", "part", "full", "time", "or", "and", "the", "a", "any",
    "multiple", "several", "various", "location", "locations", "either",
})


# -- W5.1e: sponsorship phrasing in the JD, in BOTH directions ----------------
# REFUSAL and OFFER are both real signals. Anthropic's own postings say "We do
# sponsor visas!", which a correct gate reads as a POSITIVE, not as noise.
#
# Every phrase below was frequency-checked against the live board corpus (7.7k
# postings) before being trusted. Two findings shaped the lists:
#   - bare "without sponsorship" is a TRAP: 258 of its 259 corpus hits are one
#     employer's US EXPORT-CONTROL boilerplate ("technology controlled under
#     these U.S. export laws without sponsorship for an export license"), which
#     has nothing to do with visas. Only the work-authorization-qualified forms
#     are kept.
#   - "relocation assistance" is NOT a sponsorship offer (323 hits, pure
#     benefits boilerplate), so it is not in the OFFER list.
_SPONSORSHIP_REFUSAL_PHRASES = (
    "no visa sponsorship", "not offer visa sponsorship", "unable to sponsor",
    "unable to offer visa sponsorship", "unable to provide sponsorship",
    "cannot sponsor", "can not sponsor", "do not sponsor", "does not sponsor",
    "not able to sponsor", "not provide visa sponsorship",
    "sponsorship is not available", "no sponsorship is available",
    # "no sponsorship available" (no "is") used to fall through to the OFFER list,
    # whose "sponsorship available" phrase matches INSIDE it: the posting was
    # admitted with the reason "employer offers visa sponsorship", the exact
    # opposite of what it said. Refusal is checked first, so naming the phrase
    # here is the fix.
    "no sponsorship available", "sponsorship not available",
    "without visa sponsorship", "work without sponsorship",
    "authorized to work in the us without sponsorship",
    "authorized to work in the united states without sponsorship",
    "existing right to work", "must have the right to work",
    "must already have the right to work", "already have the right to work",
    "must be authorized to work", "must be authorised to work",
    "must be legally authorized to work", "must be legally authorised to work",
    "right to work in the uk", "right to work in the united kingdom",
    "must be a us citizen", "us citizenship is required",
)
# NOT refusal phrases, and they were: "no relocation" / "not offer relocation" are
# BENEFITS boilerplate (the diff's own comment already argues this case for
# "relocation assistance" on the OFFER side), and a bare "security clearance" is a
# mention, not a refusal to sponsor a visa. Of 80 live refusal-channel discards, 46
# matched ONLY such a phrase. The clearance signal is real, but it is a DIFFERENT
# impossibility and gets its own channel and its own truthful reason below.
#
# Matched as REQUIREMENT phrasing, never as a bare mention: a JD that merely says
# its customers hold clearances is not a JD that demands one.
#
# W5.1e round 3: the list SHIPPED with soft-ELIGIBILITY phrasings in it ("ability
# to obtain", "able to obtain", "eligible to obtain a security clearance"), which
# is the OPPOSITE of a requirement: a posting that says a candidate merely able to
# obtain a clearance "will also be considered" is WIDENING its pool, not closing
# it. They are gone. "must be able to obtain a security clearance" stays, because
# the "must" makes it a demand the owner cannot meet (a US clearance is closed to
# a non-citizen), which is exactly what the reason line says.
_SECURITY_CLEARANCE_PHRASES = (
    "requires a security clearance", "requires an active security clearance",
    "requires a us security clearance", "security clearance is required",
    "active security clearance is required", "must have a security clearance",
    "must have an active security clearance", "must hold a security clearance",
    "must hold an active security clearance", "must possess a security clearance",
    "requires an active clearance", "must have an active clearance",
    "must be able to obtain a security clearance",
    "active ts/sci", "requires a ts/sci", "ts/sci clearance is required",
    "active top secret", "top secret clearance is required",
    "requires a top secret clearance",
)
# ... and the phrase alone is still not enough. "active top secret" and "active
# ts/sci" name a clearance, and live postings name one only to say it is PREFERRED
# ("active top secret clearance strongly preferred; candidates eligible and willing
# to obtain clearance will also be considered" -- cohere, scaleai). A phrase found
# in a SOFT context is not a requirement, and reporting it as one told the owner the
# posting demanded something it never demanded.
#
# Read on the LINE carrying the mention, with the matched span excluded, so a
# requirement phrase cannot veto itself. "preference" earns its place on evidence:
# the cohere Ottawa TPM says "strong preference for active top secret clearance" and
# "strong preference will be given to candidates who currently hold" -- neither of
# which "preferred" matches, so the gate called that posting a hard requirement.
_CLEARANCE_SOFT_CUES = (
    "preferred", "preferably", "preference", "eligib", "willing to obtain",
    "nice to have", "a plus", "desirable", "not required", "does not require",
    "bonus", "strongly encouraged", "also be considered",
)
# The line says it OUTRIGHT. These also classify the matched PHRASE itself: a phrase
# that contains its own demand ("must hold a security clearance") is a requirement
# wherever it appears, and only the two BARE MENTIONS ("active ts/sci", "active top
# secret") ever need a context to be judged at all.
_CLEARANCE_HARD_CUES = (
    "(required)", "required", "requires", "must have", "must hold",
    "must possess", "must be able to obtain", "must maintain", "must currently",
    "in order to qualify", "you must", "mandatory",
)
# The SECTION the mention sits in, when the line alone does not say. A real JD puts
# its soft qualifications behind a HEADER and then lists them as bare bullets:
# anthropic's "Strong candidates may also have" sits ~370 characters above the
# "Active Top Secret security clearance" bullet it governs, which is why no
# character window could ever see it. Scanned BACKWARD, nearest header wins.
_CLEARANCE_SOFT_HEADERS = (
    "strong candidates may also have", "candidates may also have",
    "you might thrive", "you'll thrive", "you will thrive", "thrive in this role",
    "nice to have", "nice-to-have", "bonus", "preferred qualifications",
    "preferred skills", "preferred experience", "a plus", "desirable",
    "strong preference", "preference will be given", "great to have",
    "even better", "additional qualifications", "what will make you stand out",
)
_CLEARANCE_HARD_HEADERS = (
    "requirements", "minimum qualifications", "minimum requirements",
    "required qualifications", "basic qualifications", "must have", "must haves",
    "must-have", "you must", "in order to qualify", "what we require", "required:",
)
_CLEARANCE_LINE_CAP = 300
# NOT a refusal phrase: "green card". Its only corpus hit was an "Immigration
# Specialist" JD describing the green-card posture of ACQUIRED WORKFORCES, i.e.
# the job's subject matter, not a requirement on the candidate. Zero true
# positives, one false positive: a phrase that never fires correctly is a bug.
_SPONSORSHIP_OFFER_PHRASES = (
    "we do sponsor visas", "we sponsor visas", "we can sponsor", "we sponsor",
    "visa sponsorship is available", "sponsorship is available",
    "sponsorship available", "we offer visa sponsorship",
    "we provide visa sponsorship", "visa sponsorship provided",
    "happy to sponsor", "will sponsor", "visa support",
)

# -- W5.1e: THE SHARED SPONSORING-EMPLOYER ALLOWLIST --------------------------
# ONE list, ONE definition, TWO consumers (owner rulings 9 and 16):
#   - work-auth gate: a silent posting in a sponsorship-required region is
#     admitted as a WARNED candidate only for these employers;
#   - role gate: a product/design role is acceptable only at these employers.
# Letting the two carve-outs drift apart is exactly the bug this prevents.
# POLICY, not owner PII, so it lives in the public engine and is overridable from
# the instance config (`scoring.work_auth.sponsoring_employers`). Matched against
# the board slug AND the company name, both normalised to bare alphanumerics.
_SPONSORING_EMPLOYERS = frozenset({
    "alphabet", "google", "googledeepmind", "deepmind", "meta", "facebook",
    "apple", "amazon", "aws", "amazonwebservices", "microsoft", "netflix",
    "nvidia", "openai", "anthropic", "ibm", "oracle", "salesforce", "adobe",
    "intel", "qualcomm", "cisco", "sap", "uber", "airbnb", "stripe", "palantir",
    "bloomberg", "spotify", "shopify", "atlassian", "linkedin", "bytedance",
    "tiktok", "samsung", "siemens", "tesla", "booking", "bookingcom",
})


# -- W5.1e ROLE GATE: non-engineering role families (owner ruling 16) ---------
# Matched as whole phrases against the TITLE'S HEAD SEGMENT only. Deliberately
# phrase-shaped, not word-shaped: bare "product" would kill a "Product Engineer"
# and bare "design" a "Design Verification Engineer", both of which are engineering
# roles the owner wants.
_NON_ENGINEERING_TITLE_KEYWORDS = (
    "product manager", "product management", "product owner", "product lead",
    "product director", "head of product", "group product manager",
    "technical product manager", "product marketing", "product analyst",
    "designer", "design lead", "design manager", "head of design", "ux", "ui",
    "user experience", "user researcher", "creative director", "art director",
    "recruiter", "recruiting", "talent acquisition", "talent partner", "sourcer",
    "people operations", "people partner", "human resources",
    "program manager", "programme manager", "project manager",
    "delivery manager", "scrum master", "agile coach", "chief of staff",
    "program coordinator", "project coordinator",
    "executive assistant", "administrative assistant", "personal assistant",
    "office manager", "receptionist",
    # BARE "generalist" is gone (round 2): it deleted "Robotics Generalist" at
    # every non-allowlisted employer, and a hands-on robotics/AI generalist is
    # none of the families this gate names (product, design, recruiting, sales,
    # marketing, policy). A false discard costs the owner the JOB; a false admit
    # costs one lead. The policy-family generalist keeps its own PHRASE entry,
    # exactly as "product" and "design" are phrase-shaped rather than word-shaped.
    "safeguards generalist",
    "sales", "account executive", "account manager", "business development",
    "partnerships", "marketing", "copywriter", "content strategist",
    "community manager", "social media",
    "customer success", "customer support", "support specialist",
    "legal counsel", "attorney", "paralegal", "compliance officer",
    "accountant", "controller", "bookkeeper", "financial analyst",
    "public policy", "policy manager", "policy lead", "policy advisor",
)
# A real engineering noun in the SAME head segment overrides a non-engineering
# hit. Kept SHORT and unambiguous on purpose: "research" is NOT here, because
# "Technical Recruiter, AI Research" and "Executive Assistant to the AI Research
# Lead" are exactly the titles this gate exists to remove.
_ENGINEERING_TITLE_NOUNS = (
    "engineer", "engineering", "developer", "programmer", "architect", "sre",
    "devops", "mlops",
)
# The head segment is the text before the first comma, colon, pipe, bracket, or
# SPACED dash (a bare hyphen would cut "Full-Stack Engineer" in half). Hyphen, en
# dash and em dash are built by codepoint so this file holds no literal dash.
_TITLE_DASHES = "-" + chr(0x2013) + chr(0x2014)
_TITLE_HEAD_RE = re.compile(
    "[,:|(\\[]|\\s[" + re.escape(_TITLE_DASHES) + "]\\s")


def _title_head(title: str) -> str:
    """The role itself, lowercased: the title's head segment. The remainder is
    subject matter ("..., AI Research") and must not reclassify the role."""
    return _TITLE_HEAD_RE.split(title or "", maxsplit=1)[0].strip().lower()


def _normalise_employer(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def is_sponsoring_employer(posting: Posting, allowlist) -> bool:
    """Is this employer a FANG-like big company (the owner's shared carve-out)?"""
    known = {_normalise_employer(e) for e in allowlist}
    candidates = (posting.company_slug,
                  getattr(posting, "company_name", None) or "")
    return any(_normalise_employer(c) in known for c in candidates if c)


def _profile_location_hit(loc: str, prefs: list[str]) -> bool:
    """Whole-word match of a concrete (non-generic) profile token in `loc`."""
    low = loc.lower()
    return any(_word_present(pref, low)
               for pref in prefs if pref not in _GENERIC_LOCATION_TOKENS)


def _remote_eligibility(locations: list[str], prefs: list[str]):
    """Grade a remote posting by the eligibility its location strings imply."""
    eu_extra = {p for p in prefs if p not in _GENERIC_LOCATION_TOKENS}
    if any(_marks_eu(loc, eu_extra) for loc in locations):
        return 1.0, ["location: remote (EU-eligible)"], []
    if locations and all(_marks_non_eu(loc) for loc in locations):
        return 0.4, [], [
            f"remote but non-EU eligibility likely ({', '.join(locations)})"]
    return 0.7, [], ["remote, eligibility unverified"]


def _marks_eu(loc: str, eu_extra: set[str]) -> bool:
    low = loc.lower()
    return (_word_hit(low, _EU_REGION_MARKERS)
            or _word_hit(low, _EU_COUNTRIES)
            or _word_hit(low, _EU_CITIES)
            or _word_hit(low, eu_extra))


def _marks_non_eu(loc: str) -> bool:
    """Does this location imply the owner would need visa sponsorship? The UK now
    counts (W5.1e): a remote UK role used to be credited "EU-eligible"."""
    low = loc.lower()
    if _word_hit(low, _NON_EU_WORDS) or _uk_country_hit(low):
        return True
    if _word_hit(low, _UK_CITIES) and not _word_hit(low, _EU_CITIES):
        return True
    if any(re.search(rf"(?<![A-Za-z]){abbr}(?![A-Za-z])", loc)
           for abbr in _US_COUNTRY_ABBREVS):
        return True
    return any(re.search(rf",\s*{code}\b", loc) for code in _US_STATE_CODES)


def _word_hit(text_lower: str, words) -> bool:
    """Whole-word match, ACCENT-FOLDED on both sides."""
    folded = _fold(text_lower)
    return any(re.search(rf"\b{re.escape(_fold(w))}\b", folded) for w in words)


def _fold(text: str) -> str:
    """Lowercase and strip diacritics: "Düsseldorf" -> "dusseldorf".

    The boards write EU cities with their native diacritics (and often a flag
    emoji: 'Düsseldorf, Germany 🇩🇪'), while every corpus list here is ASCII, so an
    unfolded match failed to PLACE the posting at all -- the fail-open direction,
    which costs an EU role its free-to-work credit and lets a non-EU one through
    as unjudgeable."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def _max_amount(comp) -> int | None:
    """`comp` should be str|None (Posting.comp), but a future vendor shape that
    slips an un-normalized dict past the adapter boundary must degrade to
    "comp unknown" rather than crash the whole scoring pass."""
    if isinstance(comp, dict):
        numeric = [v for v in (comp.get("max"), comp.get("min"))
                  if isinstance(v, (int, float))]
        return int(max(numeric)) if numeric else None
    if not isinstance(comp, str):
        return None
    # Strip thousands separators first so "120,000" reads as one amount, not two.
    plain = comp.replace(",", "")
    amounts = [int(num) * (1000 if suffix else 1)
               for num, suffix in re.findall(r"(\d+)\s*([kK])?", plain) if num]
    return max(amounts) if amounts else None


# -- W4-COMMUTE-GATE (D5): hard discard for excessive on-site presence --------
# Policy VALUES are never hardcoded (public branch): they load from the SSOT at
# `preferences.location_policy`. A MISSING or partial policy leaves the gate
# INACTIVE (fail-open), matching current behaviour with no crash.
_LOCATION_POLICY_KEYS = ("allowed_cities", "max_onsite_days_per_week_europe",
                        "max_onsite_days_per_month_rest")
_WEEKS_PER_MONTH = 4.33

_FULLY_REMOTE_RE = re.compile(r"fully[\s-]*remote", re.IGNORECASE)

# Tried in order, first match wins. Each entry is (compiled pattern, cadence).
# English + Italian phrasing (spec examples: "on-site", "in office",
# "hybrid (3 days", "3 days per week in the office", "in sede", "N giorni in
# ufficio"). Explicit monthly/weekly cadence words are checked before the
# cadence-less fallbacks, which default to a weekly cadence (the common case
# for "hybrid"/"giorni in ufficio" phrasing with no cadence word attached).
_ONSITE_AMOUNT_PATTERNS = (
    (re.compile(r"(\d+)\s*days?\s*(?:per|a|/)\s*month", re.IGNORECASE), "month"),
    (re.compile(r"(\d+)\s*days?\s*(?:per|a|/)\s*week", re.IGNORECASE), "week"),
    (re.compile(r"(\d+)\s*giorni\s*(?:al\s*mese|/\s*mese|per\s*mese)",
               re.IGNORECASE), "month"),
    (re.compile(r"(\d+)\s*giorni\s*(?:a\s*settimana|alla\s*settimana|"
               r"/\s*settimana|per\s*settimana)", re.IGNORECASE), "week"),
    (re.compile(r"(\d+)\s*giorni\s*in\s*(?:ufficio|sede)", re.IGNORECASE), "week"),
    (re.compile(r"hybrid\D{0,20}?(\d+)\s*days?", re.IGNORECASE), "week"),
    (re.compile(r"(\d+)\s*days?\s*in\s*(?:the\s*)?office", re.IGNORECASE), "week"),
)


def commute_gate(posting: Posting, policy: dict | None,
                 weeks_per_month: float = _WEEKS_PER_MONTH) -> tuple[bool, str | None]:
    """Hard DISCARD gate for roles requiring too much on-site presence outside
    the owner's allowed cities (plan D5: show-and-warn, never guess).

    Returns `(discard, message)`. When `discard` is True, `message` is the
    discard reason (detected day count + region classification, for the report
    "why" line). When `discard` is False and `message` is not None, it is a
    show-and-warn line for an undetectable on-site amount (the caller routes it
    to `ats_warnings`, never hidden). A `None`/partial policy (SSOT
    `preferences.location_policy` absent or missing a required key) leaves the
    gate permanently INACTIVE: fail-open, never discards, never raises.

    `weeks_per_month` is the calendar-conversion constant used to reconcile
    weekly vs monthly on-site cadences; the caller passes it from config
    (`scoring.commute.weeks_per_month`) so the number stays tunable and is not
    hardcoded at the one site that consumes it.
    """
    if policy is None:
        return False, None
    if posting.remote_flag or _is_fully_remote_text(posting.description):
        return False, None
    if _matches_allowed_city(posting.locations, policy["allowed_cities"]):
        return False, None
    text = f"{posting.description} {' '.join(posting.locations)}"
    amount, unit = _detect_onsite_amount(text)
    if amount is None:
        return False, "on-site presence unclear"
    europe = _location_is_europe(posting.locations)
    if europe:
        detected = amount if unit == "week" else amount / weeks_per_month
        threshold = policy["max_onsite_days_per_week_europe"]
    else:
        detected = amount * weeks_per_month if unit == "week" else amount
        threshold = policy["max_onsite_days_per_month_rest"]
    if detected > threshold:
        return True, _discard_reason(amount, unit, europe, threshold)
    return False, None


def _location_policy_from_ssot(ssot: SSOT) -> dict | None:
    """Read+validate `preferences.location_policy`; None if MISSING or partial."""
    raw = ssot.get("preferences.location_policy")
    if raw is MISSING or not isinstance(raw, dict):
        return None
    if any(key not in raw for key in _LOCATION_POLICY_KEYS):
        return None
    week_cap = raw.get("max_onsite_days_per_week_europe")
    month_cap = raw.get("max_onsite_days_per_month_rest")
    if (isinstance(week_cap, bool) or isinstance(month_cap, bool)
            or not isinstance(week_cap, (int, float))
            or not isinstance(month_cap, (int, float))):
        return None
    cities = raw.get("allowed_cities")
    allowed_cities = ([str(c) for c in cities]
                      if isinstance(cities, (list, tuple)) else [])
    return {"allowed_cities": allowed_cities,
            "max_onsite_days_per_week_europe": float(week_cap),
            "max_onsite_days_per_month_rest": float(month_cap)}


def _is_fully_remote_text(description: str) -> bool:
    return bool(_FULLY_REMOTE_RE.search(description or ""))


def _matches_allowed_city(locations: list[str], allowed_cities: list[str]) -> bool:
    return any(_word_present(city, loc) for loc in locations for city in allowed_cities)


def _location_is_europe(locations: list[str]) -> bool:
    """GEOGRAPHIC Europe (travel distance), which still includes the UK. Distinct
    from work-eligibility Europe (`_EU_*`, which the UK left in W5.1e): the commute
    gate asks "how far would he travel", not "may he work there". Keeping the two
    apart is why removing the UK from `_EU_COUNTRIES` did not silently re-band
    every UK role from the weekly European cap to the monthly rest-of-world one."""
    return any(_word_hit(loc.lower(), _EUROPE_COUNTRIES)
               or _word_hit(loc.lower(), _EUROPE_CITIES)
               for loc in locations)


def _detect_onsite_amount(text: str) -> tuple[float | None, str | None]:
    """Heuristic day-count + cadence parse over description + location text."""
    for pattern, unit in _ONSITE_AMOUNT_PATTERNS:
        match = pattern.search(text or "")
        if match:
            return float(match.group(1)), unit
    return None, None


def _discard_reason(amount: float, unit: str, europe: bool, threshold: float) -> str:
    region = "Europe" if europe else "non-Europe"
    cap_unit = "week" if europe else "month"
    return (f"on-site commute gate: detected {amount:g} days/{unit} in {region} "
           f"exceeds policy cap of {threshold:g} days/{cap_unit}")
