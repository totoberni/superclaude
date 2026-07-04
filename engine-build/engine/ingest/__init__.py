"""Inbox ingest layer: IMAP read + sender/subject classify + route (spec 7).

Self-contained W5.1-spine addition: does not modify any other engine module,
and is not yet wired into discover/store/notify (`route()` returns structured
`RoutingDecision` objects for a future wave to consume). Built + tested offline
against fixture emails (`tests/fixtures/ingest/`); no live inbox this wave.
"""

from __future__ import annotations

from engine.ingest.classify import BUCKETS, classify
from engine.ingest.inbox import (
    FakeMailbox,
    ImapMailbox,
    Mailbox,
    MailboxError,
    Message,
    load_credentials,
)
from engine.ingest.route import RoutingDecision, route
from engine.ingest.rss import HttpRssFetcher, RssItem, build_url, parse_feed

__all__ = [
    "BUCKETS",
    "classify",
    "FakeMailbox",
    "ImapMailbox",
    "Mailbox",
    "MailboxError",
    "Message",
    "load_credentials",
    "RoutingDecision",
    "route",
    "HttpRssFetcher",
    "RssItem",
    "build_url",
    "parse_feed",
]
