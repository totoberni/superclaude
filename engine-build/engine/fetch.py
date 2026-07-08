"""Live HTTP layer feeding the parse-only discovery adapters (W4 3.1).

Turns the fixtures-only engine into a read-only poller of the three sanctioned
tier-1 ATS endpoints (Greenhouse, Lever, Ashby). stdlib `urllib` only, no new
runtime dependency. Every request is deliberately polite (R-WT-8 worker B):

- an honest, self-identifying User-Agent;
- conditional GET (`If-None-Match` / `If-Modified-Since`) so an unchanged board
  costs one 304 and reuses the cached body (cache in store.db, see store 3.4);
- at least `min_interval_s` between requests to the same host (monotonic clock);
- bounded exponential backoff on 429/5xx (max 2 retries), and a hard stop on 403
  (marked blocked for the run) or 404 (marked invalid / bad slug).

The clock and sleep are injectable so the per-host spacing and backoff can be
driven deterministically under the no-network test fixture; production wires the
real `time` functions and a real urllib opener.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from engine.kernel.discover_base import SourceAdapter
from engine.kernel.capture_toolkit import UA  # noqa: F401 (moved to kernel, W5.1 stage 5)
from engine.providers import _registry
from engine.store import Store

# _VENDORS / _ADAPTERS are thin projections of the provider registry (the SSOT for
# per-vendor wiring). Kept as module-level names for import-compat: other code and
# tests import them from engine.fetch. Deriving them keeps the fetchable-vendor list
# in exactly one place while these shims retain their original type and their
# KeyError-on-unknown behaviour. Any registered stub (supported=False or
# adapter=None) is excluded from both; with workable un-stubbed (W5.4) the four
# supported vendors are ("greenhouse", "ashby", "lever", "workable") -- the
# pinned `engine.providers.VENDOR_ORDER`.
_VENDORS: tuple[str, ...] = tuple(
    vendor for vendor, spec in _registry.PROVIDERS.items()
    if spec.supported and spec.adapter is not None
)
_MAX_RETRIES = 2
_INITIAL_BACKOFF_S = 1.0

_ADAPTERS: dict[str, Callable[[], SourceAdapter]] = {
    vendor: spec.adapter
    for vendor, spec in _registry.PROVIDERS.items()
    if spec.adapter is not None
}


@dataclass
class Source:
    vendor: str          # greenhouse | lever | ashby
    slug: str            # board token / company slug
    company: str         # display name
    region: str = "us"   # lever only: us | eu
    enabled: bool = True


@dataclass
class FetchResult:
    source: Source
    status: str          # ok | not_modified | invalid | blocked | error
    raw: object | None   # parsed JSON, or None when nothing usable came back
    from_cache: bool


def load_sources(path: str | Path) -> list[Source]:
    """Parse sources.yaml into Source rows, rejecting unknown vendors."""
    raw = yaml.safe_load(Path(path).read_text()) or {}
    entries = raw.get("sources") or []
    sources: list[Source] = []
    for entry in entries:
        vendor = entry.get("vendor")
        if vendor not in _VENDORS:
            raise ValueError(
                f"unknown vendor {vendor!r}; sources support {list(_VENDORS)} "
                "only (LinkedIn/Indeed/Glassdoor are read-avoid)"
            )
        slug = entry.get("slug")
        if not slug:
            raise ValueError(f"source for {vendor!r} is missing a slug")
        sources.append(Source(
            vendor=vendor,
            slug=slug,
            company=entry.get("company") or slug,
            region=entry.get("region", "us"),
            enabled=bool(entry.get("enabled", True)),
        ))
    return sources


def endpoint_for(source: Source) -> str:
    """Absolute request URL for a source (Lever eu routes to its eu host).

    Thin shim over the provider registry (the single source of truth for per-vendor
    poll URLs); kept as a module-level function because callers and tests import
    `endpoint_for` from engine.fetch.
    """
    spec = _registry.PROVIDERS.get(source.vendor)
    if spec is None or not spec.supported or spec.endpoint_fn is None:
        raise ValueError(f"unknown vendor: {source.vendor}")
    return spec.endpoint_fn(source.slug, source.region)


def adapter_for(vendor: str) -> SourceAdapter:
    return _ADAPTERS[vendor]()


class HttpFetcher:
    def __init__(self, store: Store, opener=None, min_interval_s: float = 2.5,
                 timeout_s: float = 20, user_agent: str = UA,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.store = store
        self.opener = opener or urllib.request.build_opener()
        self.min_interval_s = min_interval_s
        self.timeout_s = timeout_s
        self.user_agent = user_agent
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: dict[str, float] = {}
        # Per-run blocked-hosts set (W4 3.1 fix wave): a 403 anywhere on a
        # host stops every later source on that same host for the rest of
        # this fetcher's life. One HttpFetcher instance = one run, so this
        # never needs resetting mid-instance.
        self._blocked_hosts: set[str] = set()

    def fetch(self, source: Source) -> FetchResult:
        url = endpoint_for(source)
        host = urllib.parse.urlsplit(url).netloc
        if host in self._blocked_hosts:
            return FetchResult(source, "blocked", None, False)
        cache = self.store.get_fetch_cache(url)
        backoff = _INITIAL_BACKOFF_S
        for attempt in range(_MAX_RETRIES + 1):
            self._respect_spacing(host)
            try:
                response = self._open(url, cache)
            except urllib.error.HTTPError as exc:
                outcome = self._on_http_error(source, url, host, exc, cache)
                if outcome is _RETRY:
                    if attempt < _MAX_RETRIES:
                        self._sleep(backoff)
                        backoff *= 2
                        continue
                    return FetchResult(source, "error", None, False)
                return outcome
            except urllib.error.URLError:
                if attempt < _MAX_RETRIES:
                    self._sleep(backoff)
                    backoff *= 2
                    continue
                return FetchResult(source, "error", None, False)
            else:
                return self._on_ok(source, url, response)
        return FetchResult(source, "error", None, False)

    def _open(self, url: str, cache: dict | None):
        req = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        })
        if cache:
            if cache.get("etag"):
                req.add_header("If-None-Match", cache["etag"])
            if cache.get("last_modified"):
                req.add_header("If-Modified-Since", cache["last_modified"])
        return self.opener.open(req, timeout=self.timeout_s)

    def _respect_spacing(self, host: str) -> None:
        last = self._last_request_at.get(host)
        if last is not None:
            elapsed = self._clock() - last
            if elapsed < self.min_interval_s:
                self._sleep(self.min_interval_s - elapsed)
        self._last_request_at[host] = self._clock()

    def _on_ok(self, source: Source, url: str, response) -> FetchResult:
        body = _read_text(response)
        try:
            raw = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self.store.add_domain_memory(
                _topic(source), f"unparseable body from {url}")
            return FetchResult(source, "invalid", None, False)
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        self.store.set_fetch_cache(url, etag, last_modified, body)
        return FetchResult(source, "ok", raw, False)

    def _on_http_error(self, source: Source, url: str, host: str,
                       exc: urllib.error.HTTPError, cache: dict | None):
        code = exc.code
        if code == 304:
            if cache and cache.get("body"):
                return FetchResult(source, "not_modified",
                                   json.loads(cache["body"]), True)
            # 304 with no cached body should not happen (we only send validators
            # when we have one); treat as an error rather than a phantom empty.
            return FetchResult(source, "error", None, False)
        if code == 404:
            self.store.add_domain_memory(
                _topic(source), f"404 at {url}: bad slug (source marked invalid)")
            return FetchResult(source, "invalid", None, False)
        if code == 403:
            self._blocked_hosts.add(host)
            self.store.add_domain_memory(
                _topic(source),
                f"403 at {url}: blocked this run, will not retry (politeness)")
            return FetchResult(source, "blocked", None, False)
        if code == 429 or 500 <= code < 600:
            return _RETRY
        return FetchResult(source, "error", None, False)


def fetch_all(sources: list[Source], store: Store, fetcher: HttpFetcher | None = None,
              **fetcher_kwargs) -> tuple[list[tuple[SourceAdapter, object, str]],
                                         list[FetchResult]]:
    """Fetch every enabled source, returning run_discovery's input shape.

    The first element is exactly the `(adapter, raw, slug)` list that
    `discover.run_discovery` already consumes, restricted to sources whose fetch
    yielded usable board JSON (ok or not_modified). The second element is the
    full per-source status list so the runner can report invalid/blocked/error
    sources and decide which are safe to run close_absent against.
    """
    fetcher = fetcher or HttpFetcher(store, **fetcher_kwargs)
    discovery: list[tuple[SourceAdapter, object, str]] = []
    results: list[FetchResult] = []
    for source in sources:
        if not source.enabled:
            continue
        result = fetcher.fetch(source)
        results.append(result)
        if result.status in ("ok", "not_modified") and result.raw is not None:
            discovery.append((adapter_for(source.vendor), result.raw, source.slug))
    return discovery, results


class _Retry:
    """Sentinel telling fetch() the error is transient and worth a retry."""


_RETRY = _Retry()


def _read_text(response) -> str:
    body = response.read()
    return body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body


def _topic(source: Source) -> str:
    return f"{source.vendor}:{source.slug}"
