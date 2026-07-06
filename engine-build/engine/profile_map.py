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
