"""IMAP inbox reader for the dedicated automation Gmail (spec 7, w5-locked).

A dedicated, non-primary @gmail.com inbox is the automation's ONLY mail access
(credential-sourcing memory, w5 spec 7): the owner sets a primary-account
auto-forward filter for job alerts + ATS verification/outcome mail, and the
automation reads ONLY this dedicated inbox via IMAP + an App Password (Workspace
would force the Gmail API, since App-Password IMAP was retired 2025-03-14, so the
dedicated inbox stays a personal Gmail). `imap-tools` wraps `imaplib`; the client
class is injected so tests never open a real socket (mirrors engine.fetch's
injectable opener / engine.notify's injectable Transport).

Credentials live at `~/automations/ingest/credentials` (0600, key=value:
`host` / `user` / `password`; `host` defaults to imap.gmail.com when omitted),
loaded via the shared fail-closed loader `engine.notify.load_credentials`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from engine.notify import load_credentials

__all__ = [
    "Message",
    "Mailbox",
    "FakeMailbox",
    "ImapMailbox",
    "MailboxError",
    "load_credentials",
]


class MailboxError(RuntimeError):
    """Raised when the IMAP mailbox is misconfigured (never on message content)."""


@dataclass
class Message:
    uid: str
    from_addr: str
    subject: str
    plain_body: str
    html_body: str
    date: datetime | None = None


class Mailbox(Protocol):
    """The interface engine.ingest needs; `ImapMailbox` below structurally
    satisfies it over a real IMAP connection. Tests inject `FakeMailbox`
    instead, so classify/route can be exercised with zero real connections."""

    def fetch_unread(self) -> list[Message]:
        ...


class FakeMailbox:
    """Test double: returns a canned list of Message, never touches the network."""

    def __init__(self, messages: list[Message] | None = None):
        self.messages = list(messages or [])

    def fetch_unread(self) -> list[Message]:
        return list(self.messages)


class ImapMailbox:
    """Live mailbox over the dedicated Gmail inbox via `imap-tools`.

    Built from a credentials dict (see module docstring / `load_credentials`).
    Only `fetch_unread` opens a real connection, so constructing this class in a
    test without calling it stays fully offline.
    """

    def __init__(self, credentials: dict):
        self.host = credentials.get("host") or "imap.gmail.com"
        self.user = credentials.get("user")
        self.password = credentials.get("password")
        if not self.user or not self.password:
            raise MailboxError(
                "imap credentials need 'user' and 'password' "
                "(~/automations/ingest/credentials)"
            )

    def fetch_unread(self) -> list[Message]:
        import imap_tools

        messages: list[Message] = []
        with imap_tools.MailBox(self.host).login(self.user, self.password) as mailbox:
            for msg in mailbox.fetch(imap_tools.AND(seen=False)):
                messages.append(Message(
                    uid=msg.uid or "",
                    from_addr=msg.from_ or "",
                    subject=msg.subject or "",
                    plain_body=msg.text or "",
                    html_body=msg.html or "",
                    date=msg.date,
                ))
        return messages
