"""engine.ingest.inbox: Message + FakeMailbox + ImapMailbox mapping/validation.

All offline: FakeMailbox never touches the network, and the ImapMailbox test
monkeypatches `imap_tools.MailBox` itself so the field-mapping logic is
exercised without a real IMAP connection (the autouse `no_network` fixture in
conftest.py would fail loudly if a real socket were attempted).
"""

from __future__ import annotations

from datetime import datetime, timezone

import imap_tools
import pytest

from engine.ingest.inbox import FakeMailbox, ImapMailbox, MailboxError, Message


def test_fake_mailbox_returns_injected_messages_unchanged():
    seed = [Message(uid="1", from_addr="alert@indeed.com", subject="s",
                    plain_body="p", html_body="h")]
    mailbox = FakeMailbox(seed)
    fetched = mailbox.fetch_unread()
    assert fetched == seed
    # returns a copy, not the same list object, so callers can't mutate the seed
    assert fetched is not seed


def test_fake_mailbox_defaults_to_empty():
    assert FakeMailbox().fetch_unread() == []


def test_imap_mailbox_requires_user_and_password():
    with pytest.raises(MailboxError):
        ImapMailbox({"user": "me@example.com"})
    with pytest.raises(MailboxError):
        ImapMailbox({"password": "secret"})
    with pytest.raises(MailboxError):
        ImapMailbox({})


def test_imap_mailbox_defaults_host_to_gmail():
    mailbox = ImapMailbox({"user": "me@example.com", "password": "secret"})
    assert mailbox.host == "imap.gmail.com"


def test_imap_mailbox_honors_explicit_host():
    mailbox = ImapMailbox({"host": "imap.example.com", "user": "u", "password": "p"})
    assert mailbox.host == "imap.example.com"


class _FakeImapMessage:
    def __init__(self, uid, from_, subject, text, html, date):
        self.uid = uid
        self.from_ = from_
        self.subject = subject
        self.text = text
        self.html = html
        self.date = date


class _FakeMailBoxCtx:
    """Stands in for `imap_tools.MailBox`: same `login`/context-manager/`fetch`
    shape, but never opens a socket."""

    instances: list["_FakeMailBoxCtx"] = []

    def __init__(self, host):
        self.host = host
        self.credentials = None
        self.fetch_criteria = None
        _FakeMailBoxCtx.instances.append(self)

    def login(self, user, password, initial_folder="INBOX"):
        self.credentials = (user, password)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def fetch(self, criteria=None, **kwargs):
        self.fetch_criteria = criteria
        when = datetime(2026, 7, 3, 8, 0, tzinfo=timezone.utc)
        return [_FakeImapMessage("101", "jobalerts-noreply@linkedin.com",
                                 "Job alert: 3 new jobs", "plain body",
                                 "<html>body</html>", when)]


def test_imap_mailbox_fetch_unread_maps_fields(monkeypatch):
    monkeypatch.setattr(imap_tools, "MailBox", _FakeMailBoxCtx)
    mailbox = ImapMailbox({"user": "me@example.com", "password": "secret"})

    messages = mailbox.fetch_unread()

    assert len(messages) == 1
    msg = messages[0]
    assert msg.uid == "101"
    assert msg.from_addr == "jobalerts-noreply@linkedin.com"
    assert msg.subject == "Job alert: 3 new jobs"
    assert msg.plain_body == "plain body"
    assert msg.html_body == "<html>body</html>"
    assert msg.date == datetime(2026, 7, 3, 8, 0, tzinfo=timezone.utc)


def test_imap_mailbox_fetch_unread_only_requests_unseen(monkeypatch):
    monkeypatch.setattr(imap_tools, "MailBox", _FakeMailBoxCtx)
    mailbox = ImapMailbox({"user": "me@example.com", "password": "secret"})
    mailbox.fetch_unread()
    ctx = _FakeMailBoxCtx.instances[-1]
    assert ctx.credentials == ("me@example.com", "secret")
    assert isinstance(ctx.fetch_criteria, imap_tools.AND)
