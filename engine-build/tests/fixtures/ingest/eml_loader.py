"""Fixture loader: parses `tests/fixtures/ingest/*.eml` into `engine.ingest`
`Message` objects, mirroring what `ImapMailbox.fetch_unread` would hand back
from imap-tools (bare `From:` address, decoded plain/html parts, no real IMAP
connection involved).
"""

from __future__ import annotations

from email import message_from_string
from email.message import Message as EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from engine.ingest.inbox import Message

FIXTURES_DIR = Path(__file__).parent


def load_message(name: str, uid: str | None = None) -> Message:
    raw = (FIXTURES_DIR / name).read_text()
    parsed = message_from_string(raw)
    plain_body, html_body = _bodies(parsed)
    date_header = parsed.get("Date")
    return Message(
        uid=uid or name,
        from_addr=parseaddr(parsed.get("From", ""))[1],
        subject=parsed.get("Subject", ""),
        plain_body=plain_body,
        html_body=html_body,
        date=parsedate_to_datetime(date_header) if date_header else None,
    )


def _bodies(parsed: EmailMessage) -> tuple[str, str]:
    if not parsed.is_multipart():
        text = _decode(parsed)
        return (text, "") if parsed.get_content_type() == "text/plain" else ("", text)
    plain, html = "", ""
    for part in parsed.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_type() == "text/plain":
            plain = _decode(part)
        elif part.get_content_type() == "text/html":
            html = _decode(part)
    return plain, html


def _decode(part: EmailMessage) -> str:
    payload = part.get_payload(decode=True) or b""
    return payload.decode(part.get_content_charset() or "utf-8")
