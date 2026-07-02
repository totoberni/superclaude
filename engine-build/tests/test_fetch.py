"""HTTP layer tests: politeness, conditional GET, backoff, host routing.

All network I/O is faked via an injected opener + clock, so these run cleanly
under the autouse no-network fixture (no real socket is ever created).
"""

import json

import pytest
import urllib.error

from engine.fetch import HttpFetcher, Source, endpoint_for, fetch_all, load_sources


class FakeClock:
    """Monotonic-ish clock whose only advance is an explicit fake sleep."""

    def __init__(self):
        self.t = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class FakeResponse:
    def __init__(self, body: bytes, headers: dict):
        self._body = body
        self.headers = headers

    def read(self) -> bytes:
        return self._body


class FakeOpener:
    """Programmable opener: each scripted action mirrors urllib's behaviour.

    ("ok", body_bytes, headers) returns a 200 response; ("http", code) raises the
    HTTPError urllib raises for a non-2xx status; ("url", reason) raises URLError.
    """

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        action = self.script.pop(0)
        kind = action[0]
        if kind == "ok":
            return FakeResponse(action[1], action[2])
        if kind == "http":
            headers = action[2] if len(action) > 2 else {}
            raise urllib.error.HTTPError(req.full_url, action[1],
                                         f"HTTP {action[1]}", headers, None)
        if kind == "url":
            raise urllib.error.URLError(action[1])
        raise AssertionError(f"unknown scripted action: {kind}")


def _fetcher(store, script, clock=None, min_interval_s=2.5):
    clock = clock or FakeClock()
    opener = FakeOpener(script)
    fetcher = HttpFetcher(store, opener=opener, min_interval_s=min_interval_s,
                          sleep=clock.sleep, clock=clock)
    return fetcher, opener, clock


def _body(payload) -> bytes:
    return json.dumps(payload).encode("utf-8")


def test_200_then_304_reuses_cached_body(store):
    src = Source("greenhouse", "acme", "Acme")
    payload = {"jobs": [{"id": 1, "title": "Backend Engineer"}]}
    fetcher, opener, _ = _fetcher(store, [
        ("ok", _body(payload), {"ETag": '"v1"', "Last-Modified": "yesterday"}),
        ("http", 304, {}),
    ])

    first = fetcher.fetch(src)
    assert first.status == "ok"
    assert first.from_cache is False
    assert first.raw == payload

    second = fetcher.fetch(src)
    assert second.status == "not_modified"
    assert second.from_cache is True
    assert second.raw == payload
    # the second request carried the conditional validators from the cache
    assert opener.requests[1].get_header("If-none-match") == '"v1"'
    assert opener.requests[1].get_header("If-modified-since") == "yesterday"


def test_404_marks_invalid_and_records_memory(store):
    src = Source("lever", "ghost", "Ghost")
    fetcher, opener, _ = _fetcher(store, [("http", 404)])
    result = fetcher.fetch(src)
    assert result.status == "invalid"
    assert result.raw is None
    assert len(opener.requests) == 1  # no retry on a 404
    assert store.search_domain_memory("404")


def test_429_backs_off_then_succeeds(store):
    src = Source("ashby", "initech", "Initech")
    payload = {"jobs": []}
    fetcher, opener, clock = _fetcher(store, [
        ("http", 429),
        ("ok", _body(payload), {}),
    ])
    result = fetcher.fetch(src)
    assert result.status == "ok"
    assert result.raw == payload
    assert len(opener.requests) == 2  # one retry
    assert 1.0 in clock.slept        # initial backoff slept before the retry


def test_403_blocks_and_does_not_retry(store):
    src = Source("greenhouse", "walled", "Walled")
    fetcher, opener, _ = _fetcher(store, [("http", 403)])
    result = fetcher.fetch(src)
    assert result.status == "blocked"
    assert len(opener.requests) == 1
    assert store.search_domain_memory("403")


def test_403_blocks_host_for_later_sources_without_a_request(store):
    # Two sources on the same vendor host: the first 403s, and the fetcher
    # must remember the HOST is blocked for the rest of this run so the
    # second source never even issues a request (W4 6b finding 3).
    fetcher, opener, _ = _fetcher(store, [("http", 403)])
    first = fetcher.fetch(Source("greenhouse", "walled", "Walled"))
    assert first.status == "blocked"
    assert len(opener.requests) == 1

    second = fetcher.fetch(Source("greenhouse", "other", "Other"))
    assert second.status == "blocked"
    assert second.raw is None
    assert len(opener.requests) == 1  # no new request issued for the second


def test_per_host_spacing_sleeps_between_same_host_requests(store):
    payload = {"jobs": []}
    fetcher, _, clock = _fetcher(store, [
        ("ok", _body(payload), {}),
        ("ok", _body(payload), {}),
    ])
    fetcher.fetch(Source("greenhouse", "acme", "Acme"))
    fetcher.fetch(Source("greenhouse", "globex", "Globex"))
    # both hit boards-api.greenhouse.io, so the second waits the full interval
    assert clock.slept == [2.5]


def test_lever_eu_routes_to_eu_host(store):
    src = Source("lever", "globex", "Globex", region="eu")
    assert "api.eu.lever.co" in endpoint_for(src)
    fetcher, opener, _ = _fetcher(store, [("ok", _body([]), {})])
    fetcher.fetch(src)
    assert "api.eu.lever.co" in opener.requests[0].full_url


def test_5xx_exhausts_retries_and_reports_error(store):
    src = Source("ashby", "flaky", "Flaky")
    fetcher, opener, _ = _fetcher(store, [
        ("http", 503), ("http", 503), ("http", 503),
    ])
    result = fetcher.fetch(src)
    assert result.status == "error"
    assert len(opener.requests) == 3  # initial + 2 retries, then give up


def test_fetch_all_returns_discovery_shape(store, greenhouse_raw):
    sources = [Source("greenhouse", "acme", "Acme")]
    fetcher, _, _ = _fetcher(store, [("ok", _body(greenhouse_raw), {})])
    discovery, results = fetch_all(sources, store, fetcher=fetcher)
    assert len(discovery) == 1
    adapter, raw, slug = discovery[0]
    assert adapter.vendor == "greenhouse"
    assert slug == "acme"
    assert raw == greenhouse_raw
    assert results[0].status == "ok"


def test_fetch_all_skips_disabled_and_excludes_failed(store, greenhouse_raw):
    sources = [
        Source("greenhouse", "acme", "Acme"),
        Source("lever", "ghost", "Ghost"),
        Source("ashby", "off", "Off", enabled=False),
    ]
    fetcher, _, _ = _fetcher(store, [
        ("ok", _body(greenhouse_raw), {}),
        ("http", 404),  # lever ghost is invalid, must not reach discovery
    ])
    discovery, results = fetch_all(sources, store, fetcher=fetcher)
    assert [slug for _, _, slug in discovery] == ["acme"]
    assert len(results) == 2  # disabled source never fetched
    assert {r.status for r in results} == {"ok", "invalid"}


def test_load_sources_validates_vendor(tmp_path):
    good = tmp_path / "sources.yaml"
    good.write_text(
        "sources:\n"
        "  - vendor: greenhouse\n    slug: acme\n    company: Acme\n"
        "  - vendor: lever\n    slug: globex\n    company: Globex\n    region: eu\n"
    )
    sources = load_sources(good)
    assert [s.vendor for s in sources] == ["greenhouse", "lever"]
    assert sources[1].region == "eu"

    bad = tmp_path / "bad.yaml"
    bad.write_text("sources:\n  - vendor: linkedin\n    slug: x\n    company: X\n")
    with pytest.raises(ValueError):
        load_sources(bad)
