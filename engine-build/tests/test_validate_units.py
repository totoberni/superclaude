"""Unit coverage for the validate package: canonicalization, detectors that must
NOT false-positive, the L1 clean-pass baseline, structural checks, the L2 judge
parser (fail-closed on every deviation), the orchestrator wiring, and render_diff.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "validate"))
import harness  # noqa: E402

from engine.validate import (  # noqa: E402
    render_diff,
    validate,
)
from engine.validate.checks import (  # noqa: E402
    canon_email,
    canon_phone,
    canon_url,
    run_l1,
)
from engine.validate.judge import (  # noqa: E402
    JUDGE_SYSTEM_PROMPT,
    ClaudeCliJudge,
    build_judge_prompt,
    parse_verdict,
)

SSOT = harness.fake_ssot()
SCHEMA = harness.FIELD_SCHEMA


# --------------------------------------------------------------------------- #
# Baseline: a clean output passes L1 with zero violations.
# --------------------------------------------------------------------------- #

def test_clean_output_passes_l1():
    result = run_l1(harness.clean_output(), SSOT, SCHEMA)
    assert result.ok is True
    assert result.violations == []


def test_clean_output_passes_with_honest_judge():
    judge = harness.StubJudge(verdict="pass")
    result = validate(harness.clean_output(), SSOT, SCHEMA, judge=judge)
    assert result.ok is True
    assert judge.calls, "the judge must actually be consulted"


def test_validate_without_judge_equals_l1():
    output = harness.clean_output()
    output["contact_email"] = "someone@evil.invalid"
    v_l1 = run_l1(output, SSOT, SCHEMA)
    v_all = validate(output, SSOT, SCHEMA)
    assert v_l1.ok == v_all.ok is False
    assert v_l1.codes == v_all.codes


# --------------------------------------------------------------------------- #
# Canonicalization.
# --------------------------------------------------------------------------- #

def test_canon_email_casefolds_and_strips_invisible():
    assert canon_email("Jordan.Fakename@Example.Invalid") == \
        canon_email("jordan.fakename@example.invalid")
    # a zero-width space must not defeat equality
    assert canon_email("jordan.fakename@e​xample.invalid") == \
        canon_email("jordan.fakename@example.invalid")


def test_canon_phone_is_e164ish():
    assert canon_phone("+1 (555) 010-0199") == "+15550100199"
    assert canon_phone("555.010.0199") == "5550100199"


def test_canon_url_normalizes_host_www_and_trailing_slash():
    a = canon_url("https://WWW.Example.Invalid/Path/")
    b = canon_url("http://example.invalid/Path")
    assert a == b == "example.invalid/path"


# --------------------------------------------------------------------------- #
# Detectors that must NOT false-positive on legitimate content.
# --------------------------------------------------------------------------- #

def test_homoglyph_detector_ignores_accented_latin():
    output = harness.clean_output()
    # genuinely accented Latin (à, è, ò, é, ï) is LATIN script, not a confusable
    output["cover_letter"] = (
        "Ho lavorato all'università di Milano; la città è però lontana. "
        "Café, naïve, résumé are all fine.")
    result = run_l1(output, SSOT, SCHEMA)
    assert "homoglyph" not in result.codes
    assert result.ok is True


def test_base64_detector_ignores_long_plain_words():
    output = harness.clean_output()
    output["cover_letter"] += (
        " Antidisestablishmentarianism and internationalization are long words.")
    result = run_l1(output, SSOT, SCHEMA)
    assert "base64_blob" not in result.codes
    assert result.ok is True


# --------------------------------------------------------------------------- #
# Structural + fail-closed L1 paths.
# --------------------------------------------------------------------------- #

def test_unknown_field_is_rejected():
    output = harness.clean_output()
    output["injected_extra"] = "surprise"
    result = run_l1(output, SSOT, SCHEMA)
    assert result.ok is False
    assert "unknown_field" in result.codes


def test_length_bound_enforced():
    output = harness.clean_output()
    output["cover_letter"] = "x" * 7000  # schema max_len is 6000
    result = run_l1(output, SSOT, SCHEMA)
    assert result.ok is False
    assert "length_exceeded" in result.codes


def test_html_or_script_rejected():
    output = harness.clean_output()
    output["cover_letter"] += " <script>steal()</script>"
    result = run_l1(output, SSOT, SCHEMA)
    assert result.ok is False
    assert "html_or_script" in result.codes


def test_identity_missing_from_ssot_fails_closed():
    # schema points an identity field at a path the SSOT does not have
    schema = dict(SCHEMA)
    schema["applicant_name"] = {"class": "identity", "identity_kind": "name",
                                "ssot": "identity.does_not_exist"}
    result = run_l1(harness.clean_output(), SSOT, schema)
    assert result.ok is False
    assert "identity_missing_ssot" in result.codes


def test_internal_error_fails_closed():
    # a None SSOT makes the policy build raise; run_l1 must fail closed, not crash
    result = run_l1(harness.clean_output(), None, SCHEMA)
    assert result.ok is False
    assert "validator_error" in result.codes


# --------------------------------------------------------------------------- #
# L2 judge: quarantine + strict parse + fail-closed on every deviation.
# --------------------------------------------------------------------------- #

def test_build_judge_prompt_carries_only_output_and_ssot():
    prompt = build_judge_prompt(harness.clean_output(), SSOT)
    assert "SSOT DATA START" in prompt
    assert "GENERATED OUTPUT DATA START" in prompt
    assert "Jordan Fakename" in prompt                      # SSOT identity
    assert "jordan.fakename@example.invalid" in prompt      # SSOT email
    assert "AUDIT" in prompt                                # datamark present


def test_judge_invocation_disables_tools_and_is_offline():
    runner = harness.FakeRunner(
        harness.cli_envelope('{"verdict": "pass", "reasons": []}'))
    verdict = ClaudeCliJudge(runner=runner).judge(harness.clean_output(), SSOT)
    assert verdict.passed is True
    cmd = runner.calls[0][0]
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--system-prompt") + 1] == JUDGE_SYSTEM_PROMPT
    assert "--no-session-persistence" in cmd


def test_parse_verdict_accepts_clean_pass_and_fail():
    passed = parse_verdict(harness.cli_envelope('{"verdict": "pass", "reasons": []}'))
    assert passed.passed is True and passed.error is None
    failed = parse_verdict(
        harness.cli_envelope('{"verdict": "fail", "reasons": ["nope"]}'))
    assert failed.passed is False
    assert failed.verdict == "fail"
    assert failed.reasons == ["nope"]


def test_parse_verdict_accepts_fenced_json():
    fenced = "```json\n{\"verdict\": \"pass\", \"reasons\": []}\n```"
    assert parse_verdict(harness.cli_envelope(fenced)).passed is True


def test_parse_verdict_fails_closed_on_non_json_result():
    v = parse_verdict(harness.cli_envelope("pass, definitely pass"))
    assert v.passed is False
    assert v.error is not None


def test_parse_verdict_fails_closed_on_bad_envelope():
    assert parse_verdict("<<not json>>").passed is False


def test_parse_verdict_fails_closed_on_error_envelope():
    import json
    envelope = json.dumps({"type": "result", "is_error": True, "result": "boom"})
    assert parse_verdict(envelope).passed is False


def test_parse_verdict_fails_closed_on_bad_verdict_value():
    v = parse_verdict(harness.cli_envelope('{"verdict": "maybe", "reasons": []}'))
    assert v.passed is False
    assert v.error is not None


def test_judge_nonzero_exit_fails_closed():
    runner = harness.FakeRunner("", returncode=1, stderr="not logged in")
    v = ClaudeCliJudge(runner=runner).judge(harness.clean_output(), SSOT)
    assert v.passed is False
    assert v.error is not None


def test_judge_timeout_fails_closed():
    import subprocess

    def timing_out(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 120)

    v = ClaudeCliJudge(runner=timing_out).judge(harness.clean_output(), SSOT)
    assert v.passed is False
    assert "timed out" in v.error


# --------------------------------------------------------------------------- #
# Orchestrator: L1 + L2 merge, raising judge fails closed.
# --------------------------------------------------------------------------- #

def test_validate_merges_l1_and_judge_violations():
    output = harness.clean_output()
    output["contact_email"] = "someone@evil.invalid"          # L1 fail
    judge = harness.StubJudge(verdict="fail", reasons=["semantic issue"])  # L2 fail
    result = validate(output, SSOT, SCHEMA, judge=judge)
    assert result.ok is False
    assert "identity_mismatch" in result.codes
    assert "judge_fail" in result.codes


def test_validate_fails_closed_when_judge_raises():
    judge = harness.StubJudge(raises=True)
    result = validate(harness.clean_output(), SSOT, SCHEMA, judge=judge)
    assert result.ok is False
    assert "judge_error" in result.codes


def test_validate_unparseable_judge_fails_closed_via_cli():
    runner = harness.FakeRunner(harness.cli_envelope("not a verdict"))
    judge = ClaudeCliJudge(runner=runner)
    result = validate(harness.clean_output(), SSOT, SCHEMA, judge=judge)
    assert result.ok is False
    assert "judge_unparseable" in result.codes


# --------------------------------------------------------------------------- #
# L3 render_diff helper.
# --------------------------------------------------------------------------- #

def test_render_diff_shows_output_and_ground_truth_and_redacts_sensitive():
    diff = render_diff(SSOT, harness.clean_output())
    assert "GENERATED OUTPUT" in diff
    assert "SSOT (ground truth)" in diff
    assert "jordan.fakename@example.invalid" in diff       # identity surfaced
    # sensitive section is acknowledged but not printed verbatim
    assert "X1234567" not in diff
    assert "redacted" in diff


# --------------------------------------------------------------------------- #
# DECODE POLICY (L2 judge): the judge process is a foreign program and can emit
# bytes that are not valid UTF-8. Under a strict decode ONE such byte raised
# UnicodeDecodeError out of subprocess, past every fail-closed handler in
# judge(), and aborted the whole run (observed in production at the sibling
# draft site, 2026-07-20). These tests pin the non-strict policy, the signal
# that keeps a degraded verdict attributable, and the fact that substitution
# can never buy a pass.
#
# They drive a REAL local process rather than a fake, because decoding is
# precisely what an injected fake cannot exercise: `claude_bin` points at a
# throwaway script that writes fixed bytes and exits. No `claude` binary and no
# socket, so the autouse no-network guard stays satisfied.
# --------------------------------------------------------------------------- #

_PASS_VERDICT = '{"verdict": "pass", "reasons": ["grounded in the SSOT"]}'


def _fake_claude_bin(tmp_path, payload: bytes, returncode: int = 0,
                     stream: str = "stdout",
                     name: str = "fake_claude") -> str:
    """An executable stand-in for `claude` that ignores its arguments and writes
    `payload` as RAW bytes, so the REAL subprocess.run performs the decode."""
    script = tmp_path / name
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.{stream}.buffer.write({payload!r})\n"
        f"sys.exit({returncode})\n"
    )
    script.chmod(0o755)
    return str(script)


def _envelope_bytes(old: bytes = b"", new: bytes = b"") -> bytes:
    """The canned pass envelope, optionally with one substring rewritten so a raw
    undecodable byte lands at a chosen position."""
    raw = harness.cli_envelope(_PASS_VERDICT).encode("utf-8")
    return raw.replace(old, new, 1) if old else raw


def test_strict_decode_of_judge_output_would_raise(tmp_path):
    """Teeth for the regressions below: the kwargs judge() used to pass DO raise
    on this exact envelope, so the passing tests that follow are not vacuous."""
    cmd = [_fake_claude_bin(tmp_path, _envelope_bytes(b"grounded",
                                                      b"gro\x88unded"))]
    with pytest.raises(UnicodeDecodeError):
        subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def test_judge_survives_undecodable_output_from_real_subprocess(tmp_path):
    """End-to-end through the DEFAULT runner (real subprocess.run), the way
    production runs: an undecodable byte inside a reason degrades one character
    instead of aborting the run."""
    judge = ClaudeCliJudge(claude_bin=_fake_claude_bin(
        tmp_path, _envelope_bytes(b"grounded", b"gro\x88unded")))
    verdict = judge.judge(harness.clean_output(), SSOT)
    assert verdict.passed is True
    assert verdict.error is None
    assert any("�" in reason for reason in verdict.reasons)


def test_judge_records_decode_replacements_and_discriminates(tmp_path):
    """The substitution must be attributable: a corrupted verdict is never handed
    back looking identical to a clean one."""
    corrupted = ClaudeCliJudge(claude_bin=_fake_claude_bin(
        tmp_path, _envelope_bytes(b"grounded", b"gro\x88unded")))
    assert corrupted.judge(harness.clean_output(),
                           SSOT).decode_replacements == 1
    # a clean verdict over the same path records nothing, so the signal
    # discriminates rather than always firing
    clean = ClaudeCliJudge(claude_bin=_fake_claude_bin(
        tmp_path, _envelope_bytes(), name="fake_claude_clean"))
    clean_verdict = clean.judge(harness.clean_output(), SSOT)
    assert clean_verdict.passed is True
    assert clean_verdict.decode_replacements == 0


def test_judge_undecodable_verdict_key_still_fails_closed(tmp_path):
    """Substitution must never buy a pass. A byte landing in the verdict KEY
    makes the object non-conforming, and non-conforming is a FAIL: replacement
    can only degrade a verdict into something the strict parser rejects, never
    spell 'pass' where the model wrote 'fail'."""
    judge = ClaudeCliJudge(claude_bin=_fake_claude_bin(
        tmp_path, _envelope_bytes(b"verdict", b"verd\x88ct")))
    verdict = judge.judge(harness.clean_output(), SSOT)
    assert verdict.passed is False
    assert verdict.error is not None
    assert verdict.decode_replacements == 1


def test_judge_undecodable_stderr_on_failure_is_also_recorded(tmp_path):
    """A non-zero exit whose stderr carries bad bytes must fail closed with the
    signal attached, not raise."""
    judge = ClaudeCliJudge(claude_bin=_fake_claude_bin(
        tmp_path, b"not logged in\x88", returncode=1, stream="stderr",
        name="fake_claude_err"))
    verdict = judge.judge(harness.clean_output(), SSOT)
    assert verdict.passed is False
    assert "claude exited 1" in verdict.error
    assert verdict.decode_replacements == 1


def test_judge_undecodable_output_is_logged(tmp_path, caplog):
    judge = ClaudeCliJudge(claude_bin=_fake_claude_bin(
        tmp_path, _envelope_bytes(b"grounded", b"gro\x88unded")))
    with caplog.at_level(logging.WARNING, logger="engine.validate.judge"):
        judge.judge(harness.clean_output(), SSOT)
    assert any(record.levelno == logging.WARNING and "undecodable" in
               record.getMessage() for record in caplog.records)


def test_judge_runner_is_called_with_non_strict_decode():
    """The injected-runner contract still gets text mode, but never the strict
    default: this kwarg pair is what stops the crash from coming back."""
    runner = harness.FakeRunner(harness.cli_envelope(_PASS_VERDICT))
    ClaudeCliJudge(runner=runner).judge(harness.clean_output(), SSOT)
    kwargs = runner.calls[0][1]
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert "text" not in kwargs
