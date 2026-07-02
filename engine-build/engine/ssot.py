"""Read-only SSOT loader + MISSING-field detection.

The engine treats the SSOT identikit (job.yaml / academic.yaml) as read-only
(plan 7.2): a field the SSOT lacks is MISSING and is NEVER guessed (I1/I6). A
required field that resolves to MISSING parks the affected item at awaiting_input
and drives one questionnaire item. The questionnaire module is the sole
sanctioned writer back to the SSOT file (7.6); this object never mutates it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

# Sentinel for an absent or empty SSOT field. Distinct object so callers can test
# identity (`value is MISSING`) rather than guessing from falsy values.
MISSING = object()


class SSOT:
    def __init__(self, data: dict):
        self._data = data or {}

    @classmethod
    def load(cls, path: str | Path) -> "SSOT":
        return cls(yaml.safe_load(Path(path).read_text()) or {})

    def get(self, dotted: str):
        """Resolve a dotted path (`preferences.notice_period`) or MISSING."""
        node = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return MISSING
            node = node[part]
        return MISSING if _is_empty(node) else node

    def is_missing(self, dotted: str) -> bool:
        return self.get(dotted) is MISSING

    def missing_required(self, required: list[str]) -> list[str]:
        """Return the subset of required dotted paths that resolve to MISSING."""
        return [path for path in required if self.is_missing(path)]


def _is_empty(value) -> bool:
    """Empty string, empty collection, or None all count as MISSING data."""
    if value is None:
        return True
    if isinstance(value, (str, list, dict, tuple, set)):
        return len(value) == 0
    return False
