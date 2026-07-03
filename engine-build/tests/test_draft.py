"""Drafter tests: canned CLI JSON, grounding prompt, fail-soft on every error.

The subprocess is faked via an injected runner, so no `claude` process ever
starts and the no-network fixture stays satisfied. The canned JSON mirrors the
real `claude -p --output-format json` schema captured in spec section 2.
"""

import json
import subprocess
from types import SimpleNamespace

from engine.draft import GROUNDING_CONTRACT, ClaudeCliDrafter, build_user_prompt
from engine.ssot import SSOT

# Real CLI schema shape (spec section 2): result + total_cost_usd + usage.* +
# modelUsage + is_error + subtype + num_turns.
_SUCCESS_JSON = json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 1,
    "result": "Dear Hiring Manager,\n\nI am keen...\n\nFIELD DATA\n"
              "notice_period: 1 month",
    "total_cost_usd": 0.0123,
    "usage": {
        "input_tokens": 1200,
        "output_tokens": 300,
        "cache_read_input_tokens": 64,
        "cache_creation_input_tokens": 0,
    },
    "modelUsage": {"claude-sonnet-4-5": {"inputTokens": 1200, "outputTokens": 300}},
})

_POSTING = {
    "title": "Senior Backend Engineer",
    "company_slug": "acme",
    "locations": ["London, UK"],
    "description": "Own backend data services in Python with SQLite.",
}
_BREAKDOWN = {"total": 85, "matched": ["role: Senior Backend Engineer"],
              "weak": ["comp unknown"]}


class FakeRunner:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return SimpleNamespace(stdout=self.stdout, stderr=self.stderr,
                               returncode=self.returncode)


def _ssot(real_ssot_path):
    return SSOT.load(real_ssot_path)


def test_parses_success_json(real_ssot_path):
    runner = FakeRunner(_SUCCESS_JSON)
    result = ClaudeCliDrafter(runner=runner).draft(_POSTING, _BREAKDOWN,
                                                   _ssot(real_ssot_path))
    assert result.ok is True
    assert result.error is None
    assert result.cost_usd == 0.0123
    assert result.model == "claude-sonnet-4-5"
    assert result.usage == {"input_tokens": 1200, "output_tokens": 300,
                            "cache_read": 64, "cache_creation": 0}
    assert "FIELD DATA" in result.material


def test_invocation_disables_tools_and_sets_grounding(real_ssot_path):
    runner = FakeRunner(_SUCCESS_JSON)
    ClaudeCliDrafter(runner=runner).draft(_POSTING, _BREAKDOWN,
                                          _ssot(real_ssot_path))
    cmd = runner.calls[0][0]
    # verified tool-disable flag: `--tools ""` denies all built-in tools
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--no-session-persistence" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--system-prompt") + 1] == GROUNDING_CONTRACT
    assert "[MISSING:" in GROUNDING_CONTRACT


def test_invocation_excludes_dynamic_system_prompt_sections(real_ssot_path):
    # Stabilises the cached system-prompt prefix across calls (verified on toto:
    # cache_creation ~37k -> ~24k, enabling cross-call cache reads).
    runner = FakeRunner(_SUCCESS_JSON)
    ClaudeCliDrafter(runner=runner).draft(_POSTING, _BREAKDOWN,
                                          _ssot(real_ssot_path))
    cmd = runner.calls[0][0]
    assert "--exclude-dynamic-system-prompt-sections" in cmd


def test_prompt_is_grounded_only_in_ssot_and_posting(real_ssot_path):
    prompt = build_user_prompt(_POSTING, _BREAKDOWN, _ssot(real_ssot_path))
    # facts that ARE in the SSOT / posting appear
    assert "Test Candidate" in prompt          # identity.name from the SSOT
    assert "Senior Backend Engineer" in prompt  # posting title
    assert "1 month" in prompt                  # canned_answers.notice_period
    # a fact present in NEITHER the SSOT nor the posting must not be injected
    assert "Goldman Sachs" not in prompt
    assert "PhD in Physics" not in prompt


def test_error_json_fails_soft(real_ssot_path):
    err = json.dumps({"type": "result", "subtype": "error_during_execution",
                      "is_error": True, "result": "boom"})
    result = ClaudeCliDrafter(runner=FakeRunner(err)).draft(
        _POSTING, _BREAKDOWN, _ssot(real_ssot_path))
    assert result.ok is False
    assert result.error


def test_nonzero_exit_fails_soft(real_ssot_path):
    runner = FakeRunner("", returncode=1, stderr="Invalid API key / not logged in")
    result = ClaudeCliDrafter(runner=runner).draft(
        _POSTING, _BREAKDOWN, _ssot(real_ssot_path))
    assert result.ok is False
    assert "not logged in" in result.error


def test_unparseable_output_fails_soft(real_ssot_path):
    result = ClaudeCliDrafter(runner=FakeRunner("<<not json>>")).draft(
        _POSTING, _BREAKDOWN, _ssot(real_ssot_path))
    assert result.ok is False


def test_timeout_fails_soft(real_ssot_path):
    def timing_out(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 180)

    result = ClaudeCliDrafter(runner=timing_out).draft(
        _POSTING, _BREAKDOWN, _ssot(real_ssot_path))
    assert result.ok is False
    assert "timed out" in result.error


def test_description_is_capped(real_ssot_path):
    posting = dict(_POSTING, description="x" * 5000)
    prompt = build_user_prompt(posting, _BREAKDOWN, _ssot(real_ssot_path))
    assert "x" * 4000 in prompt
    assert "x" * 5000 not in prompt
    assert "[...]" in prompt
