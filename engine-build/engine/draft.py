"""Application-material drafter behind a swappable Protocol (W4 3.3).

R-WT-1 verdict (spec section 2): the Agent SDK needs a metered ANTHROPIC_API_KEY
and forbids subscription OAuth, so the SDK is NOT used. Instead the draft step
shells out to `claude -p` (print mode) under the existing subscription login,
single-shot, no tools, no session persistence. The mechanism sits behind the
Drafter Protocol so it can be swapped (a real SDK path, a local model, a test
fake) without touching the runner.

Every factual claim in the output must be grounded in the SSOT excerpt handed to
the model; anything absent is emitted as `[MISSING: <field>]` rather than
invented. Failures are soft: a bad exit, unparseable JSON, or an error result
yields `ok=False` and never raises, so one flaky draft cannot crash the run.

Tool-disable flag: verified against `claude --help` on WSL 2026-07-02. The help
documents `--tools <tools...>` with "Use \"\" to disable all tools", so passing
`--tools ""` as an explicit empty argument denies every built-in tool. This is
cleaner and more future-proof than enumerating a `--disallowedTools` deny list,
and satisfies the spec's intent (b): tool use is not possible.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Callable, Protocol

from engine.ssot import MISSING, SSOT

_DESCRIPTION_CAP = 4000

GROUNDING_CONTRACT = (
    "You draft job-application material. Ground EVERY factual claim ONLY in the "
    "SSOT excerpt provided. If a needed fact is not in the SSOT, write "
    "[MISSING: <field>] instead of inventing it. Output: a tailored cover letter "
    "(<= 300 words), then a FIELD DATA block mapping common ATS fields from the "
    "SSOT canned answers. No preamble."
)

_EMPTY_USAGE = {"input_tokens": 0, "output_tokens": 0,
                "cache_read": 0, "cache_creation": 0}


@dataclass
class DraftResult:
    material: str            # cover letter + field data block (D2 full material)
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


def build_user_prompt(posting: dict, breakdown: dict, ssot: SSOT) -> str:
    """Assemble the grounded user prompt: posting facts + SSOT excerpt only."""
    locations = ", ".join(posting.get("locations") or []) or "unspecified"
    lines = [
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
