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


class Scorer:
    def __init__(self, config, profile: dict, similarity: Similarity | None = None):
        self.config = config
        self.profile = profile
        self.similarity = similarity or TokenOverlapSimilarity()
        self._axis_fns = {
            "role_fit": self._role_fit,
            "skills_overlap": self._skills_overlap,
            "seniority_fit": self._seniority_fit,
            "location_fit": self._location_fit,
            "comp_fit": self._comp_fit,
            "exclusions": self._exclusions,
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
        weighted = 0.0
        for axis, weight in self.config.axes.items():
            sub, m, w = self._axis_fns[axis](posting)
            axis_scores[axis] = sub
            weighted += sub * weight
            matched.extend(m)
            weak.extend(w)
        warnings = ats_precheck(posting, self.profile, self.config.ats_rules)
        return ScoreBreakdown(round(100 * weighted), axis_scores, matched, weak,
                              warnings)

    def _role_fit(self, posting: Posting):
        roles = self.profile.get("roles", [])
        if not roles:
            return 0.5, [], ["roles unset"]
        best = max(self.similarity.score(r, posting.title) for r in roles)
        if best >= 0.5:
            return best, [f"role: {posting.title}"], []
        return best, [], ["role match weak"]

    def _skills_overlap(self, posting: Posting):
        skills = self.profile.get("skills", [])
        if not skills:
            return 0.5, [], ["skills unset"]
        text = f"{posting.title} {posting.description}"
        sub = self.similarity.score(" ".join(skills), text)
        present = [s for s in skills if _has_all_tokens(text, s)]
        if present:
            return sub, [f"skills: {', '.join(present)}"], []
        return sub, [], ["no skill overlap"]

    def _seniority_fit(self, posting: Posting):
        levels = self.profile.get("seniority", [])
        text = f"{posting.title} {posting.description}".lower()
        hit = next((lv for lv in levels if lv.lower() in text), None)
        if hit:
            return 1.0, [f"seniority: {hit}"], []
        return 0.5, [], ["seniority unclear"]

    def _location_fit(self, posting: Posting):
        prefs = [p.lower() for p in self.profile.get("locations", [])]
        if posting.remote_flag and self.profile.get("remote_ok"):
            return 1.0, ["location: remote"], []
        for loc in posting.locations:
            if any(pref in loc.lower() for pref in prefs):
                return 1.0, [f"location: {loc}"], []
        return 0.3, [], ["location mismatch"]

    def _comp_fit(self, posting: Posting):
        floor = self.profile.get("comp_floor")
        if posting.comp is None:
            return 0.5, [], ["comp unknown"]
        if floor is None:
            return 0.5, [], ["comp floor unset"]
        top = _max_amount(posting.comp)
        if top is not None and top >= floor:
            return 1.0, [f"comp: {posting.comp}"], []
        return 0.3, [], [f"comp below floor: {posting.comp}"]

    def _exclusions(self, posting: Posting):
        excludes = [e.lower() for e in self.profile.get("excludes", [])]
        text = f"{posting.title} {posting.description}".lower()
        hits = [e for e in excludes if e in text]
        if hits:
            return 0.0, [], [f"excluded term: {', '.join(hits)}"]
        return 1.0, [], []


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
    """Extract the matching profile from the read-only SSOT preferences block."""
    prefs = ssot.get("preferences")
    if prefs is MISSING or not isinstance(prefs, dict):
        return {}
    keys = ("roles", "skills", "seniority", "locations", "remote_ok",
            "comp_floor", "excludes", "capabilities")
    return {k: prefs[k] for k in keys if prefs.get(k) not in (None, [], "")}


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _has_all_tokens(text: str, phrase: str) -> bool:
    phrase_tokens = _tokens(phrase)
    return bool(phrase_tokens) and phrase_tokens <= _tokens(text)


def _max_amount(comp: str) -> int | None:
    # Strip thousands separators first so "120,000" reads as one amount, not two.
    plain = comp.replace(",", "")
    amounts = [int(num) * (1000 if suffix else 1)
               for num, suffix in re.findall(r"(\d+)\s*([kK])?", plain) if num]
    return max(amounts) if amounts else None
