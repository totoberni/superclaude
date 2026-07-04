"""Drafter tests: canned CLI JSON, grounding prompt, fail-soft on every error.

The subprocess is faked via an injected runner, so no `claude` process ever
starts and the no-network fixture stays satisfied. The canned JSON mirrors the
real `claude -p --output-format json` schema captured in spec section 2.
"""

import json
import subprocess
from types import SimpleNamespace

from engine.draft import (
    GROUNDING_CONTRACT,
    ClaudeCliDrafter,
    build_system_prompt,
    build_user_prompt,
    select_language,
)
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
    ssot = _ssot(real_ssot_path)
    ClaudeCliDrafter(runner=runner).draft(_POSTING, _BREAKDOWN, ssot)
    cmd = runner.calls[0][0]
    # verified tool-disable flag: `--tools ""` denies all built-in tools
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--no-session-persistence" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    # the system prompt is now the full stable block (grounding contract +
    # voice rules + SSOT excerpt), built once via build_system_prompt
    system_prompt = cmd[cmd.index("--system-prompt") + 1]
    assert system_prompt == build_system_prompt(ssot)
    assert GROUNDING_CONTRACT in system_prompt
    assert "[MISSING:" in GROUNDING_CONTRACT


def test_invocation_excludes_dynamic_system_prompt_sections(real_ssot_path):
    # Stabilises the cached system-prompt prefix across calls (verified on toto:
    # cache_creation ~37k -> ~24k, enabling cross-call cache reads).
    runner = FakeRunner(_SUCCESS_JSON)
    ClaudeCliDrafter(runner=runner).draft(_POSTING, _BREAKDOWN,
                                          _ssot(real_ssot_path))
    cmd = runner.calls[0][0]
    assert "--exclude-dynamic-system-prompt-sections" in cmd


def test_user_prompt_grounded_only_in_posting():
    prompt = build_user_prompt(_POSTING, _BREAKDOWN)
    assert "Senior Backend Engineer" in prompt  # posting title
    # SSOT content now lives in the system prompt (build_system_prompt), not here
    assert "Test Candidate" not in prompt
    assert "1 month" not in prompt
    # a fact present in NEITHER the SSOT nor the posting must not be injected
    assert "Goldman Sachs" not in prompt
    assert "PhD in Physics" not in prompt


def test_system_prompt_grounded_in_ssot(real_ssot_path):
    prompt = build_system_prompt(_ssot(real_ssot_path))
    assert "Test Candidate" in prompt          # identity.name from the SSOT
    assert "1 month" in prompt                  # canned_answers.notice_period
    # a fact present in NEITHER the SSOT nor the posting must not be injected
    assert "Goldman Sachs" not in prompt
    assert "PhD in Physics" not in prompt


def test_user_prompt_contains_no_ssot_excerpt_content(real_ssot_path):
    """W4 prompt-cache cost cut: the SSOT excerpt moved entirely to the system
    prompt; the user prompt must carry none of it."""
    prompt = build_user_prompt(_POSTING, _BREAKDOWN)
    assert "SSOT EXCERPT" not in prompt
    assert "Test Candidate" not in prompt
    assert "1 month" not in prompt
    # sanity: the excerpt IS present in the system prompt for the same SSOT
    assert "SSOT EXCERPT" in build_system_prompt(_ssot(real_ssot_path))


def test_system_prompt_identical_across_items_with_different_postings(real_ssot_path):
    """W4 prompt-cache cost cut: the system prompt must be byte-identical
    across consecutive draft() calls with different postings, so calls 2..N
    in a run hit the CLI's 5-minute prompt cache."""
    runner = FakeRunner(_SUCCESS_JSON)
    drafter = ClaudeCliDrafter(runner=runner)
    ssot = _ssot(real_ssot_path)
    other_posting = dict(
        _POSTING,
        title="Staff Platform Engineer",
        company_slug="globex",
        description="Own the platform reliability stack end to end.",
    )
    drafter.draft(_POSTING, _BREAKDOWN, ssot)
    drafter.draft(other_posting, _BREAKDOWN, ssot)
    first_cmd, second_cmd = runner.calls[0][0], runner.calls[1][0]
    first_system_prompt = first_cmd[first_cmd.index("--system-prompt") + 1]
    second_system_prompt = second_cmd[second_cmd.index("--system-prompt") + 1]
    assert first_system_prompt == second_system_prompt
    # sanity: the postings genuinely differ, so the stability above isn't an
    # artifact of identical inputs
    first_user_prompt = first_cmd[first_cmd.index("-p") + 1]
    second_user_prompt = second_cmd[second_cmd.index("-p") + 1]
    assert first_user_prompt != second_user_prompt


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


def test_description_is_capped():
    posting = dict(_POSTING, description="x" * 5000)
    prompt = build_user_prompt(posting, _BREAKDOWN)
    assert "x" * 4000 in prompt
    assert "x" * 5000 not in prompt
    assert "[...]" in prompt


# W4 4c criterion 5: language/tone discriminant.
_IT_DESCRIPTION = (
    "Cerchiamo un ingegnere del backend per lo sviluppo dei nostri servizi in "
    "Python. La nostra azienda offre un ruolo nel team di ricerca con esperienza "
    "richiesta nel lavoro di sviluppo software."
)
_IT_POSTING = {
    "title": "Ingegnere Backend",
    "company_slug": "acme-it",
    "locations": ["Milano, Italia"],
    "description": _IT_DESCRIPTION,
}


def test_select_language_italian_all_italy_no_english_hard():
    lang, rationale = select_language(_IT_POSTING)
    assert lang == "it"
    assert "Italy" in rationale


def test_select_language_english_hard_forces_english():
    posting = dict(_IT_POSTING,
                   description=_IT_DESCRIPTION + " Fluent English is required.")
    lang, rationale = select_language(posting)
    assert lang == "en"
    assert "English is a hard prerequisite" in rationale


def test_select_language_non_italian_text_is_english():
    lang, rationale = select_language(_POSTING)  # English description, UK location
    assert lang == "en"
    assert "not detected as Italian" in rationale


def test_select_language_italian_text_but_location_outside_italy_is_english():
    posting = dict(_IT_POSTING, locations=["Milano, Italia", "London, UK"])
    lang, rationale = select_language(posting)
    assert lang == "en"
    assert "not all in Italy" in rationale


def test_system_prompt_carries_voice_rules_and_no_field_data_instruction(real_ssot_path):
    prompt = build_system_prompt(_ssot(real_ssot_path))
    # the owner's voice rules are present
    assert "HOOK" in prompt
    assert "honest-gap" in prompt
    assert "warm invitation" in prompt
    # the drafter is told NOT to emit a FIELD DATA block (assembled deterministically)
    assert "No FIELD DATA block" in prompt


def test_user_prompt_carries_language_directive_but_not_voice_rules():
    prompt = build_user_prompt(_POSTING, _BREAKDOWN)
    # a language directive rides the (per-item) user prompt
    assert "LANGUAGE DIRECTIVE" in prompt
    assert "ENGLISH" in prompt
    # voice rules now live in the system prompt, not here
    assert "HOOK" not in prompt
    assert "VOICE RULES" not in prompt


def test_prompt_italian_directive_when_posting_is_italian():
    prompt = build_user_prompt(_IT_POSTING, _BREAKDOWN)
    assert "LANGUAGE DIRECTIVE (it" in prompt
    assert "ITALIAN" in prompt


# --------------------------------------------------------------------------- #
# WIRE-IN: L1 anti-injection validation of the generated body against the SSOT.
# The layer was previously inert (nothing called it outside tests); draft() now
# validates the cover-letter BODY before returning it as clean.
# --------------------------------------------------------------------------- #

def _poisoned_json(letter_body: str) -> str:
    return json.dumps({
        "type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
        "result": letter_body,
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    })


def test_scheme_less_attacker_link_marks_draft_not_clean(real_ssot_path):
    # Simulate a drafter whose output smuggles a scheme-less attacker link into
    # the body. Generation succeeds, but the wired-in L1 validation catches it.
    poisoned = _poisoned_json(
        "Dear Hiring Manager,\n\nI would be a great fit for this role. You can "
        "see more of my work at evil-exfil.com/steal.\n\nBest regards,\n"
        "Test Candidate")
    result = ClaudeCliDrafter(runner=FakeRunner(poisoned)).draft(
        _POSTING, _BREAKDOWN, _ssot(real_ssot_path))
    # generation itself succeeded ...
    assert result.ok is True
    # ... but the poisoned draft is NOT returned as clean.
    assert result.validation_ok is False
    assert result.validation_violations
    assert any(v.code == "disallowed_url" for v in result.validation_violations)


def test_clean_draft_passes_wired_in_validation(real_ssot_path):
    # The default path runs pure-code L1 only (no live judge) and a clean body
    # passes: validation_ok True, no violations.
    result = ClaudeCliDrafter(runner=FakeRunner(_SUCCESS_JSON)).draft(
        _POSTING, _BREAKDOWN, _ssot(real_ssot_path))
    assert result.ok is True
    assert result.validation_ok is True
    assert result.validation_violations == []


def test_failed_draft_is_not_touched_by_validation(real_ssot_path):
    # A generation failure has no body to validate; it passes through with the
    # default (vacuously clean) validation fields, ok already False.
    result = ClaudeCliDrafter(runner=FakeRunner("<<not json>>")).draft(
        _POSTING, _BREAKDOWN, _ssot(real_ssot_path))
    assert result.ok is False
    assert result.validation_ok is True
    assert result.validation_violations == []
