"""Anti-injection validation package (spec 6b, section 4): L1 pure-code +
L2 quarantined judge, plus an L3 human-gate render helper.

Architectural principle (spec section 4): posting text NEVER determines field
VALUES. Values flow code-only from the SSOT; the posting shapes PROSE only, and
the prose is validated here. Detection is a BACKSTOP, not the primary defense.

- L1 (`checks.run_l1`): pure-code, fail-closed. Identity byte-equality, enum
  membership, free-text allowlist, cross-field exfil, invisible/homoglyph/base64/
  markdown/HTML scans, structural bounds. Defends 8 of the 10 attack classes on
  its own.
- L2 (`judge`): a quarantined `claude -p` judge that sees ONLY output + SSOT
  (never the posting) and returns a strict JSON verdict. Only needed for the two
  semantic attack classes L1 cannot check by code (judge-smuggling, persuasive
  injection). Optional: pass a `judge` to `validate()` to run it.
- L3 (`render_diff`): a human-review render of SSOT vs generated output.

Public API:
    validate(generated_output, ssot, field_schema, judge=None) -> ValidationResult
    render_diff(ssot, generated_output) -> str
"""

from __future__ import annotations

from engine.ssot import MISSING, SSOT

from engine.validate.checks import (
    DEFAULT_MAX_LEN,
    SENSITIVE_SECTION,
    ValidationResult,
    Violation,
    canon_email,
    canon_name,
    canon_phone,
    canon_url,
    run_l1,
)
from engine.validate.judge import (
    ClaudeCliJudge,
    Judge,
    JudgeVerdict,
    JUDGE_SYSTEM_PROMPT,
    build_judge_prompt,
    parse_verdict,
)

__all__ = [
    "validate",
    "render_diff",
    "ValidationResult",
    "Violation",
    "run_l1",
    "canon_email",
    "canon_name",
    "canon_phone",
    "canon_url",
    "DEFAULT_MAX_LEN",
    "SENSITIVE_SECTION",
    "Judge",
    "JudgeVerdict",
    "ClaudeCliJudge",
    "JUDGE_SYSTEM_PROMPT",
    "build_judge_prompt",
    "parse_verdict",
]


def validate(generated_output: dict, ssot: SSOT, field_schema: dict,
             judge: Judge | None = None) -> ValidationResult:
    """Run L1, then (if a judge is provided) L2, merging findings.

    FAIL-CLOSED end to end: `ok` is True only when L1 raises no violation AND
    (when a judge is supplied) the judge returns a cleanly parsed `pass`. A judge
    that errors, is unparseable, or raises is treated as a FAIL.
    """
    result = run_l1(generated_output, ssot, field_schema)
    if judge is None:
        return result

    violations = list(result.violations)
    try:
        verdict = judge.judge(generated_output, ssot)
    except Exception as exc:  # noqa: BLE001 - a raising judge is a fail-closed FAIL
        violations.append(Violation("judge_error", None,
                                    f"{type(exc).__name__}: {exc}"))
        return ValidationResult(ok=False, violations=violations)

    if not verdict.passed:
        if verdict.error:
            violations.append(Violation("judge_unparseable", None, verdict.error))
        else:
            detail = "; ".join(verdict.reasons) or "judge returned a fail verdict"
            violations.append(Violation("judge_fail", None, detail))
    return ValidationResult(ok=not violations, violations=violations)


def render_diff(ssot: SSOT, generated_output: dict) -> str:
    """L3 human-gate helper: a readable SSOT-vs-generated render for the human
    reviewer. Best-effort, schema-free; it lays the ground truth beside the
    generated values so a person can eyeball identity/enum fidelity."""
    lines = ["=== HUMAN REVIEW: SSOT (ground truth) vs GENERATED OUTPUT ===", ""]

    lines.append("GENERATED OUTPUT")
    if generated_output:
        for key, value in generated_output.items():
            lines.append(f"  {key}: {_show(value)}")
    else:
        lines.append("  (empty)")

    lines.append("")
    lines.append("SSOT (ground truth)")
    for label, dotted in (("name", "identity.name"),
                          ("email", "identity.email"),
                          ("phone", "identity.phone"),
                          ("address", "identity.address"),
                          ("links", "links"),
                          ("canned_answers", "canned_answers"),
                          (SENSITIVE_SECTION, SENSITIVE_SECTION)):
        value = ssot.get(dotted)
        if value is not MISSING:
            shown = "(present, redacted)" if dotted == SENSITIVE_SECTION else _show(value)
            lines.append(f"  {label}: {shown}")

    return "\n".join(lines)


def _show(value) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= 200 else text[:200] + " [...]"
