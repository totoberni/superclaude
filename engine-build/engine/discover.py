"""Source-adapter interface + ATS board JSON adapters (fixture-fed in v1).

Response parsers for the three sanctioned tier-1 ATS read endpoints (R-WT-8
worker D): Greenhouse (`jobs?content=true`, `updated_at`), Lever (`?mode=json`,
`createdAt`, `workplaceType`), Ashby (`posting-api/job-board`, `isRemote`,
`workplaceType`, `secondaryLocations[]`). Each adapter maps its vendor shape onto
one normalized Posting model. In v1 the raw JSON is handed in from fixtures; the
adapters do no network I/O.

The four board-JSON adapters were moved to their per-vendor plugin homes
(`engine.providers.<vendor>.discover`, leaf modules importing only
`engine.kernel.*`) in W5.1 Stage 2e; they are re-exported here at load so every
`from engine.discover import <Adapter>` consumer keeps resolving unchanged. This
re-export edge is SAFE (it does not re-create the old cycle) because
`engine.providers.registry` no longer imports `engine.discover`: nothing under
`engine.providers` reaches back here at load, so importing these plugin leaves
never re-enters a half-initialised `engine.discover`.

Liveness invariant (R-WT-8): a posting is surfaceable iff present in the latest
board poll snapshot, and on Ashby iff `isListed` is true. Aggregator-sourced
postings are marked `unverified` until re-verified against the vendor endpoint.
"""

from __future__ import annotations

from engine.kernel.discover_base import Posting, SourceAdapter  # noqa: F401
from engine.providers.ashby.discover import AshbyAdapter  # noqa: F401
from engine.providers.greenhouse.discover import GreenhouseAdapter  # noqa: F401
from engine.providers.lever.discover import LeverAdapter  # noqa: F401
from engine.providers.workable.discover import WorkableAdapter  # noqa: F401
from engine.store import Store


def run_discovery(sources: list[tuple[SourceAdapter, object, str]],
                 store: Store) -> list[Posting]:
    """Parse every source, enforce liveness, drop already-known items.

    `sources` is a list of (adapter, raw_json, company_slug). Returns only the
    net-new live postings; carryover lives in the queue, not here (7.5).
    """
    postings: list[Posting] = []
    for adapter, raw, slug in sources:
        for posting in adapter.parse(raw, slug):
            posting.unverified = not adapter.is_authoritative
            postings.append(posting)
    live = [p for p in postings if p.listed]
    return [p for p in live if not store.is_known(p.identity_key())]
