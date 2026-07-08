"""Lever board-JSON discover adapter (W5.1 Stage 2e; moved verbatim from
engine.discover).

Maps the Lever `?mode=json` board-poll shape (`createdAt`, `workplaceType`,
`salaryRange`) onto the normalized kernel Posting. A LEAF module: it imports
ONLY the discovery-tier kernel base (`Posting` plus the shared `_looks_remote`
heuristic in `engine.kernel.discover_base`) and stdlib, so nothing under
`engine.providers` reaches back into `engine.discover` at load. `_lever_comp`
and `_ms_to_iso` are Lever-only response helpers and travel here with the
adapter that uses them.
"""

from __future__ import annotations

from engine.kernel.discover_base import Posting, _looks_remote


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


def _ms_to_iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def lever_endpoint(slug: str, region: str = "us") -> str:
    """The ONE definition of Lever's board poll URL; `region="eu"` routes to the
    eu host. The provider registry registers this as `endpoint_fn` and
    `fetch.endpoint_for` delegates here; byte-identical to the old central
    `registry.lever_endpoint`."""
    host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
    return f"https://{host}/v0/postings/{slug}?mode=json"
