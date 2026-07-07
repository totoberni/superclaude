"""Discovery-tier kernel base (W5.1): the shared Posting/SourceAdapter contracts
(re-exported from kernel.contracts) plus the generic `_looks_remote` heuristic
used by more than one vendor adapter. Per-vendor discover adapters import their
base from here; vendor-specific discover helpers live in providers/<vendor>/discover.
"""
from __future__ import annotations

from engine.kernel.contracts import Posting, SourceAdapter  # noqa: F401


def _looks_remote(location: str | None) -> bool:
    return bool(location) and "remote" in location.lower()
