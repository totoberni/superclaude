"""Application-material drafter behind a swappable Protocol (W4 3.3).

R-WT-1 verdict (spec section 2): the Agent SDK needs a metered ANTHROPIC_API_KEY
and forbids subscription OAuth, so the SDK is NOT used. Instead the draft step
shells out to `claude -p` (print mode) under the existing subscription login,
single-shot, no tools, no session persistence. The mechanism sits behind the
Drafter Protocol so it can be swapped (a real SDK path, a local model, a test
fake) without touching the runner.

W4 4c criterion 5: the model now returns the LETTER BODY ONLY (salutation,
narrative paragraphs, sign-off + name) as plain text with NO LaTeX and NO FIELD
DATA block. Field data is assembled deterministically at artifact time
(artifacts.render_report_pdf), not by the model. The drafter carries the owner's
cover-letter voice rules plus a language directive derived from
`select_language`. Every factual claim must still be grounded in the SSOT
excerpt; anything absent is emitted as `[MISSING: <field>]` rather than invented.
Failures are soft: a bad exit, unparseable JSON, or an error result yields
`ok=False` and never raises, so one flaky draft cannot crash the run.

Tool-disable flag: verified against `claude --help` on WSL 2026-07-02. The help
documents `--tools <tools...>` with "Use \"\" to disable all tools", so passing
`--tools ""` as an explicit empty argument denies every built-in tool. This is
cleaner and more future-proof than enumerating a `--disallowedTools` deny list,
and satisfies the spec's intent (b): tool use is not possible.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Protocol

from engine.ssot import MISSING, SSOT

_DESCRIPTION_CAP = 4000

GROUNDING_CONTRACT = (
    "You draft the BODY of a job-application cover letter. Ground EVERY factual "
    "claim ONLY in the SSOT excerpt provided. If a needed fact is not in the "
    "SSOT, write [MISSING: <field>] instead of inventing it. Output ONLY the "
    "letter body as plain text: the salutation line, then 4 to 5 justified "
    "narrative paragraphs, then the sign-off line and the full name. Do NOT "
    "output LaTeX, markdown, headings, bullet lists, or a FIELD DATA block. Do "
    "NOT use em dashes or en dashes anywhere in the letter."
)

# Owner cover-letter voice rules (W4 4c criterion 5), carried in the user prompt
# so the model writes in the owner's shared-example style rather than boilerplate.
_VOICE_RULES = (
    "VOICE RULES (follow every one):\n"
    "- Paragraph 1 is a HOOK anchored to something SPECIFIC in the posting text "
    "below (a line, a claim, or the role's essence), stated as a conviction. "
    "NEVER open with 'I am writing to apply' or any boilerplate.\n"
    "- Middle paragraphs are a thematic narrative (for example a research side "
    "and an engineering side) weaving concrete evidence from the SSOT as story, "
    "not bullet lists; use specific numbers wherever the SSOT states them.\n"
    "- Near the end, ONE honest-gap paragraph with confident framing: name any "
    "tools the posting wants that the SSOT does not evidence, plainly, then the "
    "fast-ramp-up counterevidence from the SSOT.\n"
    "- Close with ONE logistics line about location fit, then a warm invitation "
    "to talk.\n"
    "- No FIELD DATA block, no lists, no headings inside the body. Ground every "
    "fact ONLY in the SSOT excerpt; mark anything absent as [MISSING: <field>]. "
    "No em dashes or en dashes."
)

# Distinctly-Italian function words vs common English ones. Whichever set is more
# frequent in the description decides the language of the posting text; a bare
# threshold is avoided because it misfires on short mixed-language snippets.
_IT_STOPWORDS = frozenset({
    "di", "e", "il", "la", "che", "per", "con", "una", "un", "del", "della",
    "dei", "delle", "sono", "come", "anche", "nostra", "nostro", "azienda",
    "ricerca", "esperienza", "sviluppo", "ruolo", "lavoro", "gli", "le", "nel",
    "nella", "alla", "non", "cerchiamo", "offriamo", "sede", "candidato",
    "competenze", "team", "in",
})
_EN_STOPWORDS = frozenset({
    "the", "and", "of", "to", "a", "in", "is", "for", "with", "you", "we",
    "our", "will", "are", "on", "as", "your", "or", "an", "be", "this",
})

# Hard-requirement English phrasings (both directions). Nice-to-have phrasings
# ("English is a plus", "nice to have") deliberately do NOT match.
_ENGLISH_HARD = re.compile(
    r"english[^.\n]{0,40}"
    r"(required|mandatory|fluen|proficien|native|madrelingua|essenziale|"
    r"obbligatori|c1|c2)"
    r"|(required|mandatory|must[- ]have|fluent|proficient|richiesto|"
    r"obbligatori|essenziale|ottima conoscenza)[^.\n]{0,40}english",
    re.IGNORECASE,
)

_ITALY_MARKERS = ("italia", "italy")
_ITALIAN_CITIES = frozenset({
    "milano", "roma", "torino", "napoli", "bologna", "firenze", "genova",
    "venezia", "palermo", "bari", "padova", "verona", "trieste", "catania",
    "pisa", "modena", "trento", "brescia", "parma", "cagliari",
})

_EMPTY_USAGE = {"input_tokens": 0, "output_tokens": 0,
                "cache_read": 0, "cache_creation": 0}


@dataclass
class DraftResult:
    material: str            # cover-letter BODY only (plain text, no LaTeX)
    usage: dict              # input_tokens, output_tokens, cache_read, cache_creation
    cost_usd: float          # notional, from total_cost_usd
    model: str
    ok: bool
    error: str | None = None


class Drafter(Protocol):
    def draft(self, posting: dict, breakdown: dict, ssot: SSOT) -> DraftResult:
        ...


class ClaudeCliDrafter:
    def __init__(self, model: str = "sonnet", effort: str = "medium",
                 claude_bin: str = "claude",
                 runner: Callable = subprocess.run, timeout_s: float = 180):
        self.model = model
        self.effort = effort
        self.claude_bin = claude_bin
        self.runner = runner
        self.timeout_s = timeout_s

    def draft(self, posting: dict, breakdown: dict, ssot: SSOT) -> DraftResult:
        prompt = build_user_prompt(posting, breakdown, ssot)
        cmd = [
            self.claude_bin, "-p", prompt,
            "--output-format", "json",
            "--model", self.model,
            "--system-prompt", GROUNDING_CONTRACT,
            # Drop the CLI's dynamic system-prompt sections so the cached prefix
            # is byte-stable across calls; verified on toto to cut cache_creation
            # from ~37k to ~24k and let subsequent drafts hit the read cache.
            "--exclude-dynamic-system-prompt-sections",
            "--no-session-persistence",
            "--tools", "",           # disable ALL built-in tools (verified flag)
            "--effort", self.effort,
        ]
        try:
            completed = self.runner(cmd, capture_output=True, text=True,
                                    timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            return _fail(f"draft timed out after {self.timeout_s}s")
        except (OSError, ValueError) as exc:
            return _fail(f"draft process error: {exc}")
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()[:200]
            return _fail(f"claude exited {completed.returncode}: {stderr}")
        return _parse_cli_json(completed.stdout, self.model)


def select_language(posting: dict) -> tuple[str, str]:
    """Decide the cover-letter language and record why (W4 4c criterion 5).

    Italian informal-personal (still professional) is chosen WHEN the posting
    description reads as Italian (stopword heuristic) AND every posting location
    is in Italy AND English is not a hard prerequisite in the description.
    Otherwise English formal. Returns `(lang, rationale)` with lang in
    {"it", "en"}; the rationale is recorded in the report for owner calibration.
    """
    description = posting.get("description", "") or ""
    locations = posting.get("locations") or []
    italian = _looks_italian(description)
    all_italy = _all_locations_in_italy(locations)
    english_hard = bool(_ENGLISH_HARD.search(description))

    if italian and all_italy and not english_hard:
        return "it", (
            f"Italian posting (stopword heuristic); all {len(locations)} "
            "location(s) in Italy; English not a hard prerequisite")

    reasons = []
    if not italian:
        reasons.append("description not detected as Italian")
    if not all_italy:
        reasons.append("locations not all in Italy")
    if english_hard:
        reasons.append("English is a hard prerequisite")
    return "en", "; ".join(reasons)


def _looks_italian(text: str) -> bool:
    tokens = re.findall(r"[a-zàèéìòù]+", text.lower())
    if not tokens:
        return False
    italian = sum(1 for t in tokens if t in _IT_STOPWORDS)
    english = sum(1 for t in tokens if t in _EN_STOPWORDS)
    return italian > english and italian >= 3


def _all_locations_in_italy(locations: list) -> bool:
    if not locations:
        return False
    for loc in locations:
        low = str(loc).lower()
        if any(marker in low for marker in _ITALY_MARKERS):
            continue
        if any(re.search(rf"\b{city}\b", low) for city in _ITALIAN_CITIES):
            continue
        return False  # a location we cannot confirm as Italy fails the whole set
    return True


def build_user_prompt(posting: dict, breakdown: dict, ssot: SSOT) -> str:
    """Assemble the grounded user prompt: language directive + voice rules +
    posting facts + score breakdown + SSOT excerpt only."""
    lang, rationale = select_language(posting)
    locations = ", ".join(posting.get("locations") or []) or "unspecified"
    lines = [
        _language_directive(lang, rationale),
        "",
        _VOICE_RULES,
        "",
        "POSTING",
        f"title: {posting.get('title', '')}",
        f"company: {posting.get('company_slug', '')}",
        f"location: {locations}",
        "description:",
        _trim(posting.get("description", ""), _DESCRIPTION_CAP),
        "",
        "SCORE BREAKDOWN",
        f"total: {breakdown.get('total', '')}",
        f"matched: {'; '.join(breakdown.get('matched') or []) or 'none'}",
        f"weak: {'; '.join(breakdown.get('weak') or []) or 'none'}",
        "",
        _ssot_excerpt(ssot),
    ]
    return "\n".join(lines)


def _language_directive(lang: str, rationale: str) -> str:
    if lang == "it":
        tone = ("Write the letter in ITALIAN. Tone: warm, personal, and direct "
                "(still fully professional); address the reader as a person, not "
                "a faceless institution.")
    else:
        tone = ("Write the letter in ENGLISH. Tone: polished and formal, as in a "
                "professional cover letter.")
    return f"LANGUAGE DIRECTIVE ({lang}: {rationale}). {tone}"


def _ssot_excerpt(ssot: SSOT) -> str:
    lines = ["SSOT EXCERPT (ground every claim only in the facts below):"]
    for label, dotted in (("name", "identity.name"),
                          ("education", "education"),
                          ("experience", "experience"),
                          ("skills", "skills"),
                          ("links", "links"),
                          ("canned_answers", "canned_answers")):
        value = ssot.get(dotted)
        if value is not MISSING:
            lines.append(f"{label}: {_compact(value)}")
    return "\n".join(lines)


def _parse_cli_json(stdout: str, fallback_model: str) -> DraftResult:
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return _fail("draft output was not valid JSON")
    if data.get("is_error"):
        reason = data.get("subtype") or data.get("result") or "unknown error"
        return _fail(f"claude reported an error: {reason}")
    subtype = data.get("subtype")
    if subtype is not None and subtype != "success":
        return _fail(f"claude returned subtype {subtype!r}")
    material = data.get("result")
    if not isinstance(material, str) or not material.strip():
        return _fail("draft result was empty")
    return DraftResult(
        material=material,
        usage=_normalize_usage(data.get("usage") or {}),
        cost_usd=float(data.get("total_cost_usd") or 0.0),
        model=_model_of(data, fallback_model),
        ok=True,
        error=None,
    )


def _normalize_usage(usage: dict) -> dict:
    return {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read": usage.get("cache_read_input_tokens", 0),
        "cache_creation": usage.get("cache_creation_input_tokens", 0),
    }


def _model_of(data: dict, fallback: str) -> str:
    model_usage = data.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        return next(iter(model_usage))
    return fallback


def _fail(message: str) -> DraftResult:
    return DraftResult(material="", usage=dict(_EMPTY_USAGE), cost_usd=0.0,
                       model="", ok=False, error=message)


def _trim(text: str, cap: int) -> str:
    text = text or ""
    return text if len(text) <= cap else text[:cap] + " [...]"


def _compact(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
