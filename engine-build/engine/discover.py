"""Source-adapter interface + ATS board JSON adapters (fixture-fed in v1).

Response parsers for the three sanctioned tier-1 ATS read endpoints (R-WT-8
worker D): Greenhouse (`jobs?content=true`, `updated_at`), Lever (`?mode=json`,
`createdAt`, `workplaceType`), Ashby (`posting-api/job-board`, `isRemote`,
`workplaceType`, `secondaryLocations[]`). Each adapter maps its vendor shape onto
one normalized Posting model. In v1 the raw JSON is handed in from fixtures; the
adapters do no network I/O.

Liveness invariant (R-WT-8): a posting is surfaceable iff present in the latest
board poll snapshot, and on Ashby iff `isListed` is true. Aggregator-sourced
postings are marked `unverified` until re-verified against the vendor endpoint.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Protocol

from engine.store import Store


@dataclass
class Posting:
    vendor: str
    company_slug: str
    job_id: str
    title: str
    locations: list[str]
    remote_flag: bool
    comp: str | None
    posted_ts: str | None
    updated_ts: str | None
    url: str
    # Matching needs posting text; the spec's minimal field list plus this one
    # description field (fed by content=true / descriptionPlain) drives match.py.
    description: str = ""
    listed: bool = True
    unverified: bool = False

    def identity_key(self) -> str:
        """`company|role|url` per 7.4 (papers would key on DOI/arXiv instead)."""
        return f"{self.company_slug}|{self.title}|{self.url}"


class SourceAdapter(Protocol):
    vendor: str
    is_authoritative: bool

    def parse(self, raw, company_slug: str) -> list[Posting]:
        ...


class GreenhouseAdapter:
    vendor = "greenhouse"
    is_authoritative = True

    def parse(self, raw: dict, company_slug: str) -> list[Posting]:
        return [self._one(job, company_slug) for job in raw.get("jobs", [])]

    def _one(self, job: dict, slug: str) -> Posting:
        location = (job.get("location") or {}).get("name")
        return Posting(
            vendor=self.vendor,
            company_slug=slug,
            job_id=str(job.get("id")),
            title=job.get("title", ""),
            locations=[location] if location else [],
            remote_flag=_looks_remote(location),
            comp=None,
            posted_ts=job.get("first_published"),
            updated_ts=job.get("updated_at"),
            url=job.get("absolute_url", ""),
            description=_plain(job.get("content", "")),
        )


class LeverAdapter:
    vendor = "lever"
    is_authoritative = True

    def parse(self, raw: list, company_slug: str) -> list[Posting]:
        return [self._one(job, company_slug) for job in raw]

    def _one(self, job: dict, slug: str) -> Posting:
        cats = job.get("categories") or {}
        location = cats.get("location")
        workplace = job.get("workplaceType", "")
        return Posting(
            vendor=self.vendor,
            company_slug=slug,
            job_id=str(job.get("id")),
            title=job.get("text", ""),
            locations=[location] if location else [],
            remote_flag=workplace == "remote" or _looks_remote(location),
            comp=job.get("salaryRange"),
            posted_ts=_ms_to_iso(job.get("createdAt")),
            updated_ts=_ms_to_iso(job.get("updatedAt") or job.get("createdAt")),
            url=job.get("hostedUrl", ""),
            description=job.get("descriptionPlain", ""),
        )


class AshbyAdapter:
    vendor = "ashby"
    is_authoritative = True

    def parse(self, raw: dict, company_slug: str) -> list[Posting]:
        return [self._one(job, company_slug) for job in raw.get("jobs", [])]

    def _one(self, job: dict, slug: str) -> Posting:
        comp = (job.get("compensation") or {}).get("compensationTierSummary")
        return Posting(
            vendor=self.vendor,
            company_slug=slug,
            job_id=str(job.get("id")),
            title=job.get("title", ""),
            locations=_ashby_locations(job),
            remote_flag=bool(job.get("isRemote")) or job.get("workplaceType") == "Remote",
            comp=comp,
            posted_ts=job.get("publishedAt"),
            updated_ts=job.get("updatedAt") or job.get("publishedAt"),
            url=job.get("jobUrl", ""),
            description=job.get("descriptionPlain", ""),
            listed=bool(job.get("isListed", True)),
        )


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


def _looks_remote(location: str | None) -> bool:
    return bool(location) and "remote" in location.lower()


def _plain(content: str) -> str:
    """Greenhouse content=true is HTML-escaped HTML; reduce to plain text."""
    unescaped = html.unescape(content or "")
    return re.sub(r"<[^>]+>", " ", unescaped).strip()


def _ms_to_iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _ashby_locations(job: dict) -> list[str]:
    locations = []
    primary = job.get("location")
    if primary:
        locations.append(primary)
    for secondary in job.get("secondaryLocations") or []:
        name = secondary.get("location")
        if name:
            locations.append(name)
    return locations
