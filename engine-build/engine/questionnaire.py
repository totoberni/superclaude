"""Missing-data questionnaire protocol: mandatory/optional items, park/resume.

One refinement mechanism, two triggers, two priorities (plan 7.6, D9/D10):

- MANDATORY / blocking (portal-driven): a required SSOT field is MISSING, so the
  affected item is parked at awaiting_input and one questionnaire item carrying
  its short stable ID is emitted. It is never guessed (I1/I6).
- OPTIONAL / non-blocking (staleness-driven): when an identikit is untouched for
  ~7 days the owner is first asked whether to refine at all; only on a yes are
  optional questions emitted. These never park work.

A reply updates the SSOT after a confirm echo and resumes the exact parked item
from where it stalled (7.6). This module is the SOLE sanctioned writer back to
the SSOT file; the SSOT object elsewhere stays read-only (7.2).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from engine.queue_sm import QueueStateMachine
from engine.ssot import SSOT
from engine.store import Store

STALENESS_DAYS = 7


@dataclass
class QItem:
    q_id: int
    item_id: str | None
    field_path: str
    prompt: str
    priority: str
    blocking: bool


@dataclass
class ReplyResult:
    echo: str
    resumed_state: str | None


class Questionnaire:
    def __init__(self, store: Store, queue_sm: QueueStateMachine,
                 ssot_path: str | Path):
        self.store = store
        self.queue_sm = queue_sm
        self.ssot_path = Path(ssot_path)

    def raise_mandatory(self, item_id: str, field_path: str, prompt: str) -> QItem:
        """Park the item (blocking) and emit one mandatory questionnaire item."""
        self.queue_sm.park(item_id, reason=f"missing {field_path}")
        q_id = self.store.add_questionnaire(item_id, field_path, prompt, "mandatory")
        return QItem(q_id, item_id, field_path, prompt, "mandatory", True)

    def raise_optional(self, field_path: str, prompt: str) -> QItem:
        """Emit a non-blocking refinement item; parks no work."""
        q_id = self.store.add_questionnaire(None, field_path, prompt, "optional")
        return QItem(q_id, None, field_path, prompt, "optional", False)

    def for_missing_required(self, ssot: SSOT, item_id: str,
                            required: list[str],
                            prompts: dict[str, str] | None = None) -> list[QItem]:
        """Park the item and raise one mandatory item per MISSING required field."""
        prompts = prompts or {}
        return [
            self.raise_mandatory(item_id, path,
                                 prompts.get(path, f"provide {path}"))
            for path in ssot.missing_required(required)
        ]

    def apply_reply(self, qitem: QItem, value, confirmed: bool = True) -> ReplyResult:
        """Confirm echo, then write SSOT and resume the parked item (7.6)."""
        echo = self._confirm_echo(qitem, value)
        if not confirmed:
            return ReplyResult(echo=echo, resumed_state=None)
        self._write_ssot(qitem.field_path, value)
        # Resume before marking answered: if resume raises (out-of-order / double
        # reply), the questionnaire stays open and the item stays parked, so the
        # reply is retryable instead of stranding a parked item with no question.
        resumed = self.queue_sm.resume(qitem.item_id) if qitem.item_id else None
        self.store.resolve_questionnaire(qitem.q_id)
        return ReplyResult(echo=echo, resumed_state=resumed)

    def is_stale(self, threshold_days: int = STALENESS_DAYS) -> bool:
        age_days = (time.time() - self.ssot_path.stat().st_mtime) / 86400
        return age_days >= threshold_days

    def refine_gate(self, owner_says_yes: bool,
                   field_prompts: dict[str, str]) -> list[QItem]:
        """Staleness-driven optional refinement: emit only on an owner yes."""
        if not owner_says_yes:
            return []
        return [self.raise_optional(path, prompt)
                for path, prompt in field_prompts.items()]

    def _confirm_echo(self, qitem: QItem, value) -> str:
        target = qitem.item_id or "profile"
        return f"Confirm: set {qitem.field_path} = {value!r} for {target}"

    def _write_ssot(self, field_path: str, value) -> None:
        data = yaml.safe_load(self.ssot_path.read_text()) or {}
        _set_dotted(data, field_path, value)
        self.ssot_path.write_text(yaml.safe_dump(data, sort_keys=False))


def _set_dotted(data: dict, dotted: str, value) -> None:
    node = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
