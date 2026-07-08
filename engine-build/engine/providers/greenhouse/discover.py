"""Greenhouse board-JSON discover adapter (W5.1 Stage 2e; moved verbatim from
engine.discover).

Maps the Greenhouse `jobs?content=true` board-poll shape onto the normalized
kernel Posting. A LEAF module: it imports ONLY the discovery-tier kernel base
(`Posting` plus the shared `_looks_remote` heuristic, which already lives in
`engine.kernel.discover_base`) and stdlib, so nothing under `engine.providers`
reaches back into `engine.discover` at load. `_plain` and `_names` are
Greenhouse-only response helpers and travel here with the adapter that uses them.
"""

from __future__ import annotations

import html
import re

from engine.kernel.discover_base import Posting, _looks_remote


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


def _names(entries: list[dict] | None) -> list[str]:
    """Extract `name` from a list of {id, name, ...} entries (greenhouse's
    `departments`/`offices` shape). Null-safe: missing key or empty/None list
    both yield []."""
    return [e["name"] for e in (entries or []) if e.get("name")]


def _plain(content: str) -> str:
    """Greenhouse content=true is HTML-escaped HTML; reduce to plain text."""
    unescaped = html.unescape(content or "")
    return re.sub(r"<[^>]+>", " ", unescaped).strip()


def greenhouse_endpoint(slug: str, region: str = "us") -> str:
    """The ONE definition of Greenhouse's board poll URL (region ignored). The
    provider registry registers this as `endpoint_fn` and `fetch.endpoint_for`
    delegates here; the returned string is byte-identical to the old central
    `registry.greenhouse_endpoint`."""
    return ("https://boards-api.greenhouse.io/v1/boards/"
            f"{slug}/jobs?content=true")
