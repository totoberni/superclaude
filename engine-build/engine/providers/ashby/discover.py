"""Ashby board-JSON discover adapter (W5.1 Stage 2e; moved verbatim from
engine.discover).

Maps the Ashby `posting-api/job-board` shape (`isRemote`, `workplaceType`,
`secondaryLocations[]`, `isListed`) onto the normalized kernel Posting. A LEAF
module: it imports ONLY the discovery-tier kernel base (`Posting`) and stdlib,
so nothing under `engine.providers` reaches back into `engine.discover` at load.
Ashby derives its remote flag structurally (`isRemote`/`workplaceType`) and so
does not need the shared `_looks_remote` heuristic. `_ashby_locations` is an
Ashby-only response helper and travels here with the adapter that uses it.
"""

from __future__ import annotations

from engine.kernel.discover_base import Posting


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


def ashby_endpoint(slug: str, region: str = "us") -> str:
    """The ONE definition of Ashby's board poll URL (region ignored). The
    provider registry registers this as `endpoint_fn` and `fetch.endpoint_for`
    delegates here; byte-identical to the old central `registry.ashby_endpoint`."""
    return ("https://api.ashbyhq.com/posting-api/job-board/"
            f"{slug}?includeCompensation=true")
