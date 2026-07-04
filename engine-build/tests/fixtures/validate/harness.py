"""Shared builders for the anti-injection validate suite.

Imported by `tests/test_validate_*.py` via a sys.path insert (the fixtures dir is
not a package, so this avoids needing __init__.py files outside the in-scope
fixtures/validate/ tree). Everything here is fabricated placeholder data: NO real
owner PII (PII firewall, spec constraints).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from engine.ssot import SSOT
from engine.validate.judge import JudgeVerdict

_SSOT_PATH = Path(__file__).with_name("ssot_fake.yaml")


def fake_ssot() -> SSOT:
    return SSOT.load(_SSOT_PATH)


# field_schema maps each generated field to its class + SSOT anchor. Identity and
# enum fields name an explicit SSOT dotted path; free-text fields inherit the
# convention-derived allowlist/sensitive policy.
FIELD_SCHEMA: dict = {
    "applicant_name": {"class": "identity", "identity_kind": "name",
                       "ssot": "identity.name"},
    "contact_email": {"class": "identity", "identity_kind": "email",
                      "ssot": "identity.email"},
    "phone": {"class": "identity", "identity_kind": "phone",
              "ssot": "identity.phone"},
    "portfolio_url": {"class": "identity", "identity_kind": "url",
                      "ssot": "links.portfolio"},
    "q_requires_sponsorship": {"class": "enum",
                               "ssot_answers": "canned_answers.requires_visa_sponsorship"},
    "cover_letter": {"class": "free_text", "max_len": 6000},
}

_CLEAN_LETTER = (
    "Dear Hiring Team,\n\n"
    "Your work on resilient backend systems is exactly the kind of problem I "
    "want to spend my days on. Over the last two years I have shipped Python "
    "services and a small machine learning model, and I care about writing code "
    "other people can maintain.\n\n"
    "You can see a couple of my projects at https://example.invalid/jordan-fakename "
    "if that is useful. I would love to talk about how I could help the team.\n\n"
    "Warm regards,\nJordan Fakename"
)


def clean_output() -> dict:
    """A baseline generated_output that passes L1 with zero violations."""
    return {
        "applicant_name": "Jordan Fakename",
        "contact_email": "jordan.fakename@example.invalid",
        "phone": "+1 555 010 0199",
        "portfolio_url": "https://portfolio.example.invalid/jordan",
        "q_requires_sponsorship": "yes",
        "cover_letter": _CLEAN_LETTER,
    }


class StubJudge:
    """Protocol-level fake Judge for offline L2 tests. Returns a canned verdict
    (or raises, to exercise the raising-judge fail-closed path)."""

    def __init__(self, verdict: str = "pass", reasons=None, error=None, raises=False):
        self._verdict = verdict
        self._reasons = list(reasons or [])
        self._error = error
        self._raises = raises
        self.calls: list[tuple[dict, object]] = []

    def judge(self, generated_output: dict, ssot) -> JudgeVerdict:
        self.calls.append((generated_output, ssot))
        if self._raises:
            raise RuntimeError("judge blew up")
        return JudgeVerdict(verdict=self._verdict, reasons=self._reasons,
                            error=self._error)


class FakeRunner:
    """Stands in for subprocess.run so ClaudeCliJudge never starts a process."""

    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return SimpleNamespace(stdout=self.stdout, stderr=self.stderr,
                               returncode=self.returncode)


def cli_envelope(result: str) -> str:
    """Wrap a raw judge `result` string in a realistic `claude -p` JSON envelope."""
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 1,
        "result": result,
        "total_cost_usd": 0.001,
        "usage": {"input_tokens": 200, "output_tokens": 20},
    })
