"""Single source of truth mapping an ATS vendor to its wiring (W5 consolidation).

Before this module, four call sites each re-derived the same per-vendor branch:

- `engine.fetch._ADAPTERS` + `endpoint_for` (board adapter class + poll URL),
- `engine.fetch.load_sources` (the accepted-vendor allowlist, `_VENDORS`),
- `engine.fill._apply_url` (public apply-page URL),
- `engine.run._collect_fieldmap` (field-map capture dispatch).

Each `ProviderSpec` gathers those references in one place and the call sites now
delegate here. This is a behaviour-preserving CONSOLIDATION: the capture / apply /
endpoint function BODIES stay where they live (fieldmap.py, browse.py, and the
apply-page URL builders in fill.py / browse.py); the registry only holds references.

Two properties are load-bearing and drive the lazy-reference design:

1. The daily poller must NOT import playwright. `engine.run` imports this module at
   top level, so the registry must not import `engine.browse` (or `engine.fill`,
   which pulls in browse) at module load. The browser-vendor capture / apply refs are
   therefore resolved with a function-level import on FIRST CALL.
2. Tests monkeypatch `engine.browse.capture_ashby` (and friends) as a MODULE
   attribute. The references therefore look the function up on the module object at
   call time, never binding it at import time (the classic import-alias gotcha).

Only `engine.discover` (the board adapters; a leaf module whose own imports never
loop back here) is safe to import eagerly, which also breaks the latent
fetch -> providers -> fieldmap -> fetch import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from engine.discover import AshbyAdapter, GreenhouseAdapter, LeverAdapter


# -- endpoint builders (board poll URLs) ---------------------------------------
# The ONE definition of each vendor's poll URL; `engine.fetch.endpoint_for`
# delegates here. Signature is (slug, region) so Lever's eu host stays expressible;
# greenhouse / ashby ignore region. The returned strings are byte-identical to the
# branches the old `endpoint_for` produced.

def greenhouse_endpoint(slug: str, region: str = "us") -> str:
    return ("https://boards-api.greenhouse.io/v1/boards/"
            f"{slug}/jobs?content=true")


def lever_endpoint(slug: str, region: str = "us") -> str:
    host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
    return f"https://{host}/v0/postings/{slug}?mode=json"


def ashby_endpoint(slug: str, region: str = "us") -> str:
    return ("https://api.ashbyhq.com/posting-api/job-board/"
            f"{slug}?includeCompensation=true")


# -- lazy references to capture / apply functions living in other modules ------
# Resolved at CALL time (function-level import + module-attribute lookup) so that
# (1) importing this module never pulls in playwright, and (2) tests that
# monkeypatch `engine.browse.capture_ashby` (a module attribute) are honoured.
# The capture wrappers normalise the two capture signatures onto one shape:
# greenhouse takes the urllib `opener`; the browser vendors ignore it.

def _capture_greenhouse(slug: str, job_id: str, opener=None):
    from engine.fieldmap import capture_greenhouse
    return capture_greenhouse(slug, job_id, opener)


def _capture_ashby(slug: str, job_id: str, opener=None):
    from engine import browse
    return browse.capture_ashby(slug, job_id)


def _capture_lever(slug: str, job_id: str, opener=None):
    from engine import browse
    return browse.capture_lever(slug, job_id)


def _apply_greenhouse(slug: str, job_id: str) -> str:
    from engine.fill import greenhouse_apply_url
    return greenhouse_apply_url(slug, job_id)


def _apply_ashby(slug: str, job_id: str) -> str:
    from engine import browse
    return browse.ashby_application_url(slug, job_id)


def _apply_lever(slug: str, job_id: str) -> str:
    from engine import browse
    return browse.lever_apply_url(slug, job_id)


def _workable_unsupported(*_args, **_kwargs):
    raise NotImplementedError(
        "workable is a registered STUB only; its endpoint / capture / apply wiring "
        "lands in W5.4. Do not route live traffic through it yet."
    )


@dataclass(frozen=True)
class ProviderSpec:
    """Everything the engine needs to talk to one ATS vendor, in one place.

    Fields:
        vendor        canonical vendor key ("greenhouse" | "lever" | "ashby" | ...).
        adapter       discover.py board-JSON adapter CLASS (call it for a fresh
                      instance, matching fetch.adapter_for); None for a stub.
        endpoint_fn   board poll-URL builder, (slug, region="us") -> str.
        capture_fn    field-map capture dispatch, (slug, job_id, opener=None)
                      -> FieldMap (greenhouse uses opener; browser vendors ignore it).
        apply_url_fn  public apply-page URL builder, (slug, job_id) -> str.
        supported     False for a documented stub whose fns raise NotImplementedError;
                      such a vendor is excluded from the fetch / adapter allowlists.
        hosts         host suffixes that identify this vendor from a posting URL
                      (drives detect()).
    """

    vendor: str
    adapter: type | None
    endpoint_fn: Callable[..., str] | None
    capture_fn: Callable[..., object] | None
    apply_url_fn: Callable[..., str] | None
    supported: bool = True
    hosts: tuple[str, ...] = ()


PROVIDERS: dict[str, ProviderSpec] = {
    "greenhouse": ProviderSpec(
        vendor="greenhouse",
        adapter=GreenhouseAdapter,
        endpoint_fn=greenhouse_endpoint,
        capture_fn=_capture_greenhouse,
        apply_url_fn=_apply_greenhouse,
        hosts=("greenhouse.io",),
    ),
    "lever": ProviderSpec(
        vendor="lever",
        adapter=LeverAdapter,
        endpoint_fn=lever_endpoint,
        capture_fn=_capture_lever,
        apply_url_fn=_apply_lever,
        hosts=("lever.co",),
    ),
    "ashby": ProviderSpec(
        vendor="ashby",
        adapter=AshbyAdapter,
        endpoint_fn=ashby_endpoint,
        capture_fn=_capture_ashby,
        apply_url_fn=_apply_ashby,
        hosts=("ashbyhq.com",),
    ),
    # W5.4 stub: registered so downstream code can enumerate it, but every wiring
    # slot raises NotImplementedError until W5.4 fills it in. Excluded from the
    # fetch / adapter allowlists via supported=False + adapter=None.
    "workable": ProviderSpec(
        vendor="workable",
        adapter=None,
        endpoint_fn=_workable_unsupported,
        capture_fn=_workable_unsupported,
        apply_url_fn=_workable_unsupported,
        supported=False,
        hosts=("workable.com",),
    ),
}


def resolve(vendor: str) -> ProviderSpec:
    """Return the ProviderSpec for `vendor`.

    Raises ValueError for a vendor with no registry entry at all. A registered but
    unsupported stub (e.g. workable) resolves normally; calling its wiring functions
    is what raises NotImplementedError.
    """
    try:
        return PROVIDERS[vendor]
    except KeyError:
        raise ValueError(f"unknown vendor {vendor!r}") from None


def detect(url_or_host: str) -> str | None:
    """Map a posting URL or bare host to its vendor key, or None if unrecognised.

    The ONE place a URL / host is classified to a vendor (future W5 ingest + apply
    routing). Matches on the registered host suffixes; a bare host (no scheme) is
    accepted as-is, and userinfo / port are stripped before matching.
    """
    if not url_or_host:
        return None
    host = urlsplit(url_or_host).netloc or url_or_host
    host = host.split("@")[-1].split(":")[0].strip().lower()
    if not host:
        return None
    for vendor, spec in PROVIDERS.items():
        for suffix in spec.hosts:
            if host == suffix or host.endswith("." + suffix):
                return vendor
    return None
