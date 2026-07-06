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
    # ToS-readable greenhouse fields (jobs/{id}?questions=true shape); safe
    # defaults so every existing construction site and the other adapters
    # (Lever/Ashby/Workable) keep working unchanged. Only GreenhouseAdapter
    # populates these today.
    departments: list[str] = field(default_factory=list)
    offices: list[str] = field(default_factory=list)
    requisition_id: str | None = None
    application_deadline: str | None = None
    company_name: str | None = None

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
            departments=_names(job.get("departments")),
            offices=_names(job.get("offices")),
            requisition_id=job.get("requisition_id"),
            application_deadline=job.get("application_deadline"),
            company_name=job.get("company_name"),
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
            comp=_lever_comp(job.get("salaryRange")),
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


class WorkableAdapter:
    vendor = "workable"
    is_authoritative = True

    def parse(self, raw: dict, company_slug: str) -> list[Posting]:
        return [self._one(job, company_slug) for job in raw.get("jobs", [])]

    def _one(self, job: dict, slug: str) -> Posting:
        # posting_id is the shortcode; the canonical apply-page URL is rebuilt
        # from slug+shortcode (the widget's own `url`/`application_url` omit the
        # account slug). Discovery only ever emits the apply.workable.com host.
        shortcode = str(job.get("shortcode", ""))
        return Posting(
            vendor=self.vendor,
            company_slug=slug,
            job_id=shortcode,
            title=job.get("title", ""),
            locations=_workable_locations(job),
            remote_flag=bool(job.get("telecommuting")),
            comp=None,
            posted_ts=job.get("published_on"),
            updated_ts=job.get("created_at") or job.get("published_on"),
            url=f"https://apply.workable.com/{slug}/j/{shortcode}/",
            description="",
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


def _lever_comp(salary_range) -> str | None:
    """Lever's live `salaryRange` is a {min, max, currency, interval} dict; the
    fixtures predate this and never exercised the field. Render it down to the
    plain string Posting.comp expects; pass through strings/None unchanged."""
    if salary_range is None or isinstance(salary_range, str):
        return salary_range
    if not isinstance(salary_range, dict):
        return None
    lo, hi = salary_range.get("min"), salary_range.get("max")
    if lo is not None and hi is not None:
        amount = f"{lo}-{hi}"
    elif lo is not None:
        amount = f"{lo}+"
    elif hi is not None:
        amount = f"up to {hi}"
    else:
        return None
    parts = [amount, salary_range.get("currency"), salary_range.get("interval")]
    return " ".join(p for p in parts if p)


def _names(entries: list[dict] | None) -> list[str]:
    """Extract `name` from a list of {id, name, ...} entries (greenhouse's
    `departments`/`offices` shape). Null-safe: missing key or empty/None list
    both yield []."""
    return [e["name"] for e in (entries or []) if e.get("name")]


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


def _workable_locations(job: dict) -> list[str]:
    """One "City, Region, Country" string per entry in the widget's
    `locations[]`, falling back to the job's top-level city/state/country when
    the array is empty."""
    out: list[str] = []
    for loc in job.get("locations") or []:
        name = ", ".join(p for p in (loc.get("city"), loc.get("region"),
                                     loc.get("country")) if p)
        if name:
            out.append(name)
    if not out:
        name = ", ".join(p for p in (job.get("city"), job.get("state"),
                                     job.get("country")) if p)
        if name:
            out.append(name)
    return out


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
