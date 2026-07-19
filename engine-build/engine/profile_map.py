"""Map the real seeded SSOT (schema v1.4) onto the engine matching profile (W4 3.2).

The real identikit does not use the toy fixture keys that `match.profile_from_ssot`
reads, so this adapter translates the v1.4 shape into the same profile dict
contract the Scorer expects. It is strictly read-only and NEVER guesses: a block
the SSOT omits simply does not appear in the profile, and the engine already
degrades gracefully on a missing key ("roles unset", etc.).

Mapping (W4 spec 3.2):
- roles        <- preferences.target_roles (as-is)
- locations    <- preferences.target_locations, exploded into whole-string plus
                  bare city/country tokens so substring location_fit works
- remote_ok    <- preferences.remote (True iff the string mentions "remote")
- comp_floor   <- first grouped integer parsed out of preferences.comp_floor
- skills       <- flattened skills.{programming_languages, frameworks_libraries,
                  tools_platforms, domains} (verbose, kept for cover-letter draft)
- skill_tokens <- SSOT skill_tokens (canonical short tags, for matching)
- experience_years <- SSOT experience_years / summed experience block (the
                  seniority-gate anchor; the level comes from experience, NEVER
                  from target-role names anymore, killing the self-inflation bug)
- excludes     <- preferences.excludes (as-is)
- capabilities <- only real, affirmative work-auth facts the SSOT states (a
                  region marked "no" is NOT asserted; truthiness bug fixed)
- sponsorship_required_by_region <- the owner's per-region right-to-work status,
                  read from the SSOT and never re-derived (W5.1e; see
                  `sponsorship_by_region` below). This is the fact the WORK-AUTH
                  GATE runs on.
"""

from __future__ import annotations

import re

from engine.ssot import MISSING, SSOT

_SKILL_BLOCKS = ("programming_languages", "frameworks_libraries",
                 "tools_platforms", "domains")
# Affirmative values that assert a work-authorization region. A dict-shaped
# work_authorization (region -> value) is only asserted when the value is truly
# affirmative: a string "no"/"false" is TRUTHY in Python, which is exactly the
# bug that made US/UK regions falsely assert. Everything else is not asserted.
_AFFIRMATIVE_WORDS = frozenset({
    "yes", "true", "y", "authorized", "authorised", "citizen", "permanent",
    "eligible", "granted", "1",
})
# Region words we are willing to assert as an explicit capability, matched on
# word boundaries so "neutral" never reads as "eu". Kept conservative on purpose.
_REGION_CAPABILITIES = (("eu", "work_authorization_eu"),
                        ("uk", "work_authorization_uk"),
                        ("usa", "work_authorization_us"),
                        ("us", "work_authorization_us"))


# W5.1e WORK-AUTH GATE. The owner's per-region right-to-work status is OWNER DATA:
# it lives in the SSOT and is never hardcoded here (same firewall the commute
# policy obeys). These are the region keys the SSOT states; a region it does not
# state is simply absent from the map, and the gate decides what to do about that.
_SPONSORSHIP_REGIONS = ("eu", "ch", "uk", "us", "ca", "other")
# Leading words that settle a boolean-ish SSOT value. Longest-prefix wins is not
# needed: no word here is a prefix of another with the opposite meaning.
_SPONSORSHIP_WORDS = (("true", True), ("yes", True), ("required", True),
                      ("false", False), ("no", False), ("none", False))


def sponsorship_by_region(ssot: SSOT) -> dict[str, bool]:
    """`region -> does the OWNER need visa sponsorship there` (True = needs it).

    Read from the SSOT, NEVER re-derived. Two authoritative sources, cross-checked:

    1. `work_authorization.<region>.sponsorship_required` -- the STRUCTURED fact
       (a boolean, or a "true"/"false" string). Preferred: no prose to parse.
    2. `canned_answers.sponsorship_answer_by_region.<region>` -- the owner's own
       answer, whose leading yes/no carries the same fact. This is the field the
       FORM FILLER already types into ATS sponsorship boxes (kernel/resolve.py),
       so the scorer and the filler now decide from the same statement.

    W5.1e RATIFICATION: the task-spec named source 2 as THE authoritative fact.
    Source 1 is read FIRST because the live SSOT states it as an explicit
    `sponsorship_required` boolean, which needs no natural-language reading at
    all; source 2 remains the fallback and the cross-check. Both are the owner's
    own data, so this honours the rule that matters (read the owner's answer,
    never infer it from `work_authorization` prose via `_is_affirmative`, which
    is what silently produced an EMPTY capabilities list). When the two sources
    DISAGREE the conservative reading wins (assume sponsorship IS needed): a
    false "needs sponsorship" costs one lead, a false "free to work" puts a job
    the owner cannot legally take at the top of his digest.

    A region the SSOT does not state is ABSENT from the map (never guessed). An
    SSOT with no work-auth facts at all yields {}, which leaves the gate INACTIVE
    (fail-open), exactly as a missing `location_policy` leaves the commute gate
    inactive.
    """
    structured = ssot.get("work_authorization")
    answers = ssot.get("canned_answers.sponsorship_answer_by_region")
    out: dict[str, bool] = {}
    for region in _SPONSORSHIP_REGIONS:
        primary = None
        if isinstance(structured, dict) and isinstance(structured.get(region), dict):
            primary = _sponsorship_flag(
                structured[region].get("sponsorship_required"))
        fallback = None
        if isinstance(answers, dict):
            fallback = _sponsorship_flag(answers.get(region))
        stated = [flag for flag in (primary, fallback) if flag is not None]
        if stated:
            out[region] = any(stated)  # conservative on disagreement
    return out


def _sponsorship_flag(value) -> bool | None:
    """Boolean-ish SSOT value -> True/False, or None when it states nothing."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        for word, flag in _SPONSORSHIP_WORDS:
            if low.startswith(word):
                return flag
    return None


def profile_from_real_ssot(ssot: SSOT) -> dict:
    """Read-only v1.4 SSOT -> engine profile (same contract as profile_from_ssot)."""
    profile: dict = {}

    # Roles feed role_title_fit only. Seniority is NO LONGER derived from these
    # names (that self-inflation made "Senior X" targets score the owner as
    # senior); the seniority gate anchors on experience_years instead.
    roles = _as_str_list(ssot.get("preferences.target_roles"))
    if roles:
        profile["roles"] = roles

    experience_years = ssot.experience_years()
    if experience_years is not None:
        profile["experience_years"] = experience_years

    skill_tokens = ssot.skill_tokens()
    if skill_tokens:
        profile["skill_tokens"] = skill_tokens

    locations = _location_tokens(ssot.get("preferences.target_locations"))
    if locations:
        profile["locations"] = locations

    remote = ssot.get("preferences.remote")
    if isinstance(remote, str):
        profile["remote_ok"] = "remote" in remote.lower()

    comp_floor = _parse_comp_floor(ssot.get("preferences.comp_floor"))
    if comp_floor is not None:
        profile["comp_floor"] = comp_floor

    skills = _flatten_skills(ssot)
    if skills:
        profile["skills"] = skills

    excludes = _as_str_list(ssot.get("preferences.excludes"))
    if excludes:
        profile["excludes"] = excludes

    capabilities = _capabilities(ssot)
    if capabilities:
        profile["capabilities"] = capabilities

    sponsorship = sponsorship_by_region(ssot)
    if sponsorship:
        profile["sponsorship_required_by_region"] = sponsorship

    return profile


def _as_str_list(value) -> list[str]:
    if value is MISSING or value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _location_tokens(values) -> list[str]:
    if values is MISSING or not isinstance(values, (list, tuple)):
        return []
    tokens: list[str] = []
    for entry in values:
        text = str(entry).strip()
        if not text:
            continue
        _add_unique(tokens, text)
        for part in re.split(r"[,/()]", text):
            part = part.strip()
            if part:
                _add_unique(tokens, part)
    return tokens


def _parse_comp_floor(value):
    if value is MISSING or value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"\d[\d,.]*", str(value))
    if not match:
        return None
    digits = re.sub(r"[,.]", "", match.group())
    return int(digits) if digits.isdigit() else None


def _flatten_skills(ssot: SSOT) -> list[str]:
    flat: list[str] = []
    for block in _SKILL_BLOCKS:
        values = ssot.get(f"skills.{block}")
        if isinstance(values, (list, tuple)):
            for value in values:
                _add_unique(flat, str(value))
    return flat


def _capabilities(ssot: SSOT) -> list[str]:
    """Only capabilities the SSOT states outright; never inferred beyond that."""
    caps: list[str] = []
    auth = ssot.get("work_authorization")
    text = _authorization_text(auth)
    if text:
        low = text.lower()
        for token, capability in _REGION_CAPABILITIES:
            if re.search(rf"\b{token}\b", low):
                _add_unique(caps, capability)
    return caps


def _authorization_text(auth) -> str:
    if auth is MISSING or auth is None:
        return ""
    if isinstance(auth, str):
        return auth
    if isinstance(auth, dict):
        # A v1.4 dict form (region -> value): surface only regions whose value is
        # genuinely affirmative. NOT `if v` (a string "no" is truthy, which
        # falsely asserted US/UK rights and stopped US roles warning).
        return " ".join(k for k, v in auth.items() if _is_affirmative(v))
    return ""


def _is_affirmative(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, str):
        return value.strip().lower() in _AFFIRMATIVE_WORDS
    return False


def _add_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)
