"""Queue state machine + short stable IDs.

A persistent, daily-re-ranked queue per automation (plan 7.5). States are exactly
those of the 7.5 diagram; transitions are validated against the diagram edges.
Buffer size equals the visible cap (D6): items that fall below threshold or beyond
the cap are demoted off the visible list, but the buffer stays topped up so there
is always a ranked backlog to promote from. Demotion trims what is shown; it does
not drain the buffer. PhD and papers terminate at pending_review (no submit path).

Short stable IDs (WT-10) are monotonic per automation and never re-used: the
counter lives in the store and only increments.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.discover import Posting
from engine.match import ScoreBreakdown
from engine.store import Store

# Diagram edges (7.5). awaiting_input is entered via park() and left via resume(),
# which restore the saved prev_state, so they are not listed as normal edges here.
_ALLOWED: dict[str, set[str]] = {
    "discovered": {"drafted"},
    "drafted": {"pending_review"},
    "pending_review": {"approved", "drafted", "demoted", "blacklisted"},
    "demoted": {"pending_review"},
    "approved": {"submitting"},
    "submitting": {"needs_otp", "submitted", "failed"},
    "needs_otp": {"submitting"},
    "failed": {"approved"},
    "submitted": set(),
    "blacklisted": set(),
}


class InvalidTransition(ValueError):
    """Raised on a state transition that the 7.5 diagram does not permit."""


@dataclass
class QueueItem:
    item_id: str
    identity_key: str
    state: str
    prev_state: str | None
    score: int
    visible: bool
    channel: str
    payload: dict


@dataclass
class RerankResult:
    demoted_today: list[str]
    held: int


class QueueStateMachine:
    def __init__(self, store: Store, config):
        self.store = store
        self.config = config

    def allocate_id(self) -> str:
        return f"{self.config.id_prefix}{self.store.next_counter()}"

    def enqueue(self, posting: Posting, breakdown: ScoreBreakdown) -> str:
        """Admit a scored posting: discovered -> drafted -> pending_review."""
        item_id = self.allocate_id()
        channel = self.config.channel_for(posting.vendor)
        payload = _payload(posting, breakdown)
        self.store.upsert_queue(item_id, posting.identity_key(), "discovered",
                                None, breakdown.total, 1, channel, payload)
        self.store.record_ledger(posting.identity_key(), item_id, posting.vendor,
                                 posting.company_slug, posting.title,
                                 posting.url, "seen", breakdown.total)
        self.transition(item_id, "drafted")
        self.transition(item_id, "pending_review")
        return item_id

    def transition(self, item_id: str, to_state: str) -> None:
        row = self._require_row(item_id)
        if to_state not in _ALLOWED.get(row["state"], set()):
            raise InvalidTransition(f"{row['state']} -> {to_state} not allowed")
        self._reject_submit_path(to_state)
        self._write(row, to_state, row["visible"], row["prev_state"])

    def _reject_submit_path(self, to_state: str) -> None:
        """Seal the submit path for non-submitting automations (phd/papers).

        They terminate at pending_review (7.5): approved is the sole gateway into
        the submit path (pending_review -> approved is its only inbound edge), so
        blocking approved seals every downstream submit state.
        """
        if self.config.terminal_state != "submitted" and to_state == "approved":
            raise InvalidTransition(
                f"{self.config.name} terminates at {self.config.terminal_state}; "
                "no submit path (7.5)"
            )

    def rerank(self) -> RerankResult:
        rows = [r for r in self.store.all_queue_rows()
                if r["state"] in ("pending_review", "demoted")]
        rows.sort(key=lambda r: r["score"], reverse=True)
        demoted_today: list[str] = []
        for rank, row in enumerate(rows):
            below = row["score"] < self.config.threshold
            over_cap = rank >= self.config.buffer_size
            self._apply_rank(row, below, over_cap, demoted_today)
        return RerankResult(demoted_today=demoted_today, held=self.store.held_count())

    def park(self, item_id: str, reason: str = "") -> None:
        """Park at awaiting_input (7.6), saving the state to resume to later."""
        row = self._require_row(item_id)
        if row["state"] == "awaiting_input":
            return
        if row["state"] in ("submitted", "blacklisted"):
            raise InvalidTransition(f"cannot park a {row['state']} item")
        self._write(row, "awaiting_input", row["visible"], row["state"])

    def resume(self, item_id: str) -> str:
        """Restore a parked item to exactly the state it stalled in (7.6)."""
        row = self._require_row(item_id)
        if row["state"] != "awaiting_input":
            raise InvalidTransition(f"{item_id} is not parked")
        prev = row["prev_state"] or "pending_review"
        self._write(row, prev, row["visible"], None)
        return prev

    def blacklist(self, item_id: str, reason: str = "") -> None:
        row = self._require_row(item_id)
        self._write(row, "blacklisted", 0, row["prev_state"])
        self.store.blacklist_add(item_id, row["identity_key"], reason)
        self.store.set_ledger_status(row["identity_key"], "blacklisted")

    def items(self) -> list[QueueItem]:
        return [_to_item(r) for r in self.store.all_queue_rows()]

    def get(self, item_id: str) -> QueueItem | None:
        row = self.store.get_queue_row(item_id)
        return _to_item(row) if row else None

    # -- internals -----------------------------------------------------------
    def _apply_rank(self, row: dict, below: bool, over_cap: bool,
                   demoted_today: list[str]) -> None:
        # Ledger statuses applied/skipped are written by the W5 submission path,
        # not here; rerank only ever moves items between demoted and visible.
        hide = below or over_cap
        if hide and row["visible"]:
            self._write(row, "demoted", 0, row["prev_state"])
            self.store.set_ledger_status(row["identity_key"], "demoted")
            if over_cap and not below:
                demoted_today.append(row["item_id"])
        elif not hide and not row["visible"] and row["state"] == "demoted":
            self._write(row, "pending_review", 1, row["prev_state"])
            self.store.set_ledger_status(row["identity_key"], "seen")

    def _write(self, row: dict, state: str, visible: int,
              prev_state: str | None) -> None:
        self.store.upsert_queue(row["item_id"], row["identity_key"], state,
                                prev_state, row["score"], visible,
                                row["channel"], row["payload"])

    def _require_row(self, item_id: str) -> dict:
        row = self.store.get_queue_row(item_id)
        if row is None:
            raise KeyError(f"queue item not found: {item_id}")
        return row


def _payload(posting: Posting, breakdown: ScoreBreakdown) -> dict:
    return {
        "posting": {
            "vendor": posting.vendor,
            "company_slug": posting.company_slug,
            "job_id": posting.job_id,
            "title": posting.title,
            "url": posting.url,
            "locations": posting.locations,
            "remote_flag": posting.remote_flag,
            "comp": posting.comp,
            "unverified": posting.unverified,
        },
        "breakdown": {
            "total": breakdown.total,
            "matched": breakdown.matched,
            "weak": breakdown.weak,
            "ats_warnings": breakdown.ats_warnings,
        },
    }


def _to_item(row: dict) -> QueueItem:
    return QueueItem(
        item_id=row["item_id"],
        identity_key=row["identity_key"],
        state=row["state"],
        prev_state=row["prev_state"],
        score=row["score"],
        visible=bool(row["visible"]),
        channel=row["channel"],
        payload=row["payload"],
    )
