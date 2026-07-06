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
from dataclasses import dataclass, field
from typing import Protocol

from engine.discover import Posting
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
                               excl_reason) if r]
        if fam_discard or sen_discard or commute_discard or excl_discard:
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
        # first-class bridge shape (the owner wants ~1 year pre-PhD).
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
        # NEW axis: EU/Italy work rights -> full; a role that needs sponsorship
        # the owner lacks is penalised + warned; a degree-in-hand requirement is
        # warned against (the owner's degree pending).
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
        if not weak:
            matched.append("eligibility: EU work rights")
        return sub, matched, weak


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
    """A non-remote posting whose locations are all non-EU implies the owner (EU
    only) would need visa sponsorship. Remote roles are left to explicit
    us_signal_keywords, since remote-EU is eligible without sponsorship."""
    if getattr(posting, "remote_flag", False):
        return False
    locations = posting.locations or []
    return bool(locations) and all(_marks_non_eu(loc) for loc in locations)


def _any_keyword(low: str, keywords) -> bool:
    """True when any config keyword/phrase appears as a whole word/phrase."""
    return any(_phrase_present(str(k).lower(), low) for k in keywords)


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
# EU/EEA/CH/UK country names (whole-word, case-insensitive). Italy is first-class
# per the profile; the profile's own location tokens are unioned in at runtime.
_EU_COUNTRIES = frozenset({
    "italy", "france", "germany", "spain", "portugal", "netherlands", "belgium",
    "luxembourg", "ireland", "austria", "greece", "poland", "czechia", "czech",
    "slovakia", "slovenia", "hungary", "romania", "bulgaria", "croatia",
    "estonia", "latvia", "lithuania", "finland", "sweden", "denmark", "malta",
    "cyprus", "norway", "iceland", "liechtenstein", "switzerland",
    "united kingdom", "britain", "scotland", "wales",
})
# Major EU/UK/CH cities (whole-word, case-insensitive).
_EU_CITIES = frozenset({
    "milan", "rome", "turin", "naples", "florence", "bologna", "london",
    "manchester", "edinburgh", "dublin", "berlin", "munich", "hamburg",
    "frankfurt", "cologne", "paris", "lyon", "madrid", "barcelona", "lisbon",
    "porto", "amsterdam", "rotterdam", "brussels", "vienna", "zurich", "geneva",
    "stockholm", "copenhagen", "oslo", "helsinki", "warsaw", "prague", "athens",
})
# Region markers that make a remote posting EU/EMEA/global-friendly.
_EU_REGION_MARKERS = frozenset({
    "eu", "eea", "europe", "european", "emea", "global", "anywhere", "worldwide",
})
# Non-EU country/city names (whole-word, case-insensitive).
_NON_EU_WORDS = frozenset({
    "united states", "canada", "australia", "singapore", "india", "japan",
    "brazil", "mexico", "toronto", "vancouver", "montreal", "ottawa", "sydney",
    "melbourne", "brisbane", "perth", "bangalore", "bengaluru", "tokyo",
    "new york", "san francisco", "seattle", "boston", "chicago", "austin",
    "denver", "atlanta", "los angeles", "washington", "mountain view",
    "palo alto", "menlo park", "san jose", "dallas", "houston", "miami",
})
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
    if _word_hit(loc.lower(), _NON_EU_WORDS):
        return True
    if any(re.search(rf"(?<![A-Za-z]){abbr}(?![A-Za-z])", loc)
           for abbr in _US_COUNTRY_ABBREVS):
        return True
    return any(re.search(rf",\s*{code}\b", loc) for code in _US_STATE_CODES)


def _word_hit(text_lower: str, words) -> bool:
    return any(re.search(rf"\b{re.escape(w)}\b", text_lower) for w in words)


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
    return any(_word_hit(loc.lower(), _EU_COUNTRIES) or _word_hit(loc.lower(), _EU_CITIES)
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
