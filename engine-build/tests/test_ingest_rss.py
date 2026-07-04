"""engine.ingest.rss: URL building + feed parsing + injectable HTTP fetch.

Mirrors the FakeOpener/FakeResponse pattern used in test_fetch.py so the live
fetcher is exercised without ever creating a real socket.
"""

from __future__ import annotations

from pathlib import Path

from engine.ingest.rss import HttpRssFetcher, build_url, parse_feed

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "ingest"


def _sample_xml() -> str:
    return (FIXTURES_DIR / "indeed_rss_sample.xml").read_text()


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body


class FakeOpener:
    def __init__(self, body: bytes):
        self.body = body
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        return FakeResponse(self.body)


def test_build_url_uses_rss_host_and_params():
    url = build_url(query="python developer", location="London, UK")
    assert url.startswith("https://rss.indeed.com/rss?")
    assert "q=python" in url or "q=python+developer" in url
    assert "sort=date" in url


def test_build_url_defaults_to_empty_query_and_location():
    url = build_url()
    assert url.startswith("https://rss.indeed.com/rss?")
    assert "sort=date" in url


def test_parse_feed_extracts_title_link_pubdate_and_job_id():
    items = parse_feed(_sample_xml())
    assert len(items) == 2

    first = items[0]
    assert first.title == "Python Developer - Example Corp - London, UK"
    assert first.link == "https://www.indeed.com/rc/clk?jk=abcdef1234567890&from=rss"
    assert first.pub_date == "Fri, 03 Jul 2026 09:00:00 GMT"
    assert first.job_id == "abcdef1234567890"

    second = items[1]
    assert second.job_id == "0123456789abcdef"


def test_http_rss_fetcher_uses_injected_opener_no_network():
    opener = FakeOpener(_sample_xml().encode("utf-8"))
    fetcher = HttpRssFetcher(opener=opener)

    items = fetcher.fetch(query="python developer", location="London, UK")

    assert len(items) == 2
    assert items[0].job_id == "abcdef1234567890"
    # confirm the request actually targeted the RSS host with our params
    assert opener.requests[0].full_url.startswith("https://rss.indeed.com/rss?")
    assert "User-Agent" in opener.requests[0].headers or \
        "User-agent" in opener.requests[0].headers
