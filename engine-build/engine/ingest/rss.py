"""Indeed RSS helper (spec 7, w5-locked).

Indeed via RSS (`rss.indeed.com/rss?q=&l=&sort=date`, swap `www` -> `rss` on the
normal search URL) is the primary, health-checked, no-login sourcing channel for
job alerts; LinkedIn/Glassdoor have no RSS and stay alert-email-only (see
`engine.ingest.classify` / `engine.ingest.route`). The HTTP call is injectable
(mirrors `engine.fetch.HttpFetcher`'s opener), so tests never touch the network.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from xml.etree import ElementTree

UA = "abe-automations-jobhunt/0.1 (personal job-search; polite reader)"

_BASE_URL = "https://rss.indeed.com/rss"
_JK_RE = re.compile(r"[?&]jk=([0-9a-zA-Z]+)")


@dataclass
class RssItem:
    title: str
    link: str
    pub_date: str | None
    job_id: str | None


def build_url(query: str = "", location: str = "", sort: str = "date") -> str:
    """`rss.indeed.com/rss?q=&l=&sort=date` (spec 7): no login required."""
    params = {"q": query, "l": location, "sort": sort}
    return f"{_BASE_URL}?{urllib.parse.urlencode(params)}"


def parse_feed(xml_text: str) -> list[RssItem]:
    """Parse an Indeed RSS 2.0 body into `RssItem` rows (title/link/pubDate + jk=)."""
    root = ElementTree.fromstring(xml_text)
    items = []
    for item in root.iter("item"):
        title = _text(item, "title") or ""
        link = _text(item, "link") or ""
        pub_date = _text(item, "pubDate")
        items.append(RssItem(title=title, link=link, pub_date=pub_date,
                             job_id=_extract_jk(link)))
    return items


def _text(item, tag: str) -> str | None:
    el = item.find(tag)
    return el.text.strip() if el is not None and el.text else None


def _extract_jk(link: str) -> str | None:
    match = _JK_RE.search(link or "")
    return match.group(1) if match else None


class HttpRssFetcher:
    """Live fetcher; the opener is injectable (mirrors `engine.fetch.HttpFetcher`)."""

    def __init__(self, opener=None, timeout_s: float = 20, user_agent: str = UA):
        self.opener = opener or urllib.request.build_opener()
        self.timeout_s = timeout_s
        self.user_agent = user_agent

    def fetch(self, query: str = "", location: str = "", sort: str = "date") -> list[RssItem]:
        url = build_url(query, location, sort)
        req = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent,
            "Accept": "application/rss+xml, application/xml, text/xml",
        })
        response = self.opener.open(req, timeout=self.timeout_s)
        return parse_feed(_read_text(response))


def _read_text(response) -> str:
    body = response.read()
    return body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body
