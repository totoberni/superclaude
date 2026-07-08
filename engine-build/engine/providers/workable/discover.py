"""Workable board-JSON discover adapter (W5.1 Stage 2e; moved verbatim from
engine.discover).

Maps the Workable public account-widget shape (`shortcode`, `telecommuting`,
`locations[]`) onto the normalized kernel Posting, rebuilding the canonical
apply-page URL from slug+shortcode. A LEAF module: it imports ONLY the
discovery-tier kernel base (`Posting`) and stdlib, so nothing under
`engine.providers` reaches back into `engine.discover` at load. Workable derives
its remote flag structurally (`telecommuting`) and so does not need the shared
`_looks_remote` heuristic. `_workable_locations` is a Workable-only response
helper and travels here with the adapter that uses it.
"""

from __future__ import annotations

from engine.kernel.discover_base import Posting


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


def workable_endpoint(slug: str, region: str = "us") -> str:
    """The ONE definition of Workable's discovery board feed URL (the public
    account widget: name/description/jobs[]; region ignored). The provider
    registry registers this as `endpoint_fn` and `fetch.endpoint_for` delegates
    here; byte-identical to the old central `registry.workable_endpoint`."""
    return f"https://apply.workable.com/api/v1/widget/accounts/{slug}"
