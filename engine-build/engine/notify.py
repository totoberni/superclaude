"""ntfy publisher + report renderers (digest header, per-item breakdown).

The single phone-facing surface (plan Section 5). A run emits one push per topic:
a Markdown digest (`# JobHunt · <date>` + a bold `N ready · M manual · K held ·
J demoted today` line), then the ready bucket (automatable, submit on your go)
and the manual/copy-paste bucket which carries the FULL tailored material (D2),
not just a link. Every item is a labelled-bullet block (score, match/weak/flags,
location/comp when present) so the owner learns what to strengthen (7.3) at a
glance on the phone, instead of a flat text blob (owner directive, legibility is
first-class).

Rendering uses ntfy Markdown formatting: publishing with the `Markdown` header
(an alias of `X-Markdown`) set to `yes` turns on headings/bold/bullets/code-fences
in the ntfy app (docs.ntfy.sh/publish, "Markdown formatting"). `publish` sends
this header by default; `publish_file` captions ride the `Message` header
instead, which does not render Markdown, so those stay plain single-line text.

Publishing rides the live toto ntfy server via NtfyTransport; tests inject
FakeTransport, so no network is touched. Credentials are read from
`~/automations/ntfy/credentials` (0600, key=value: url/user/password/token) and
loading is fail-closed: absent or world-readable credentials raise (7.8/Section 5).
"""

from __future__ import annotations

import unicodedata
from datetime import date
from email.header import Header
from pathlib import Path
from typing import Protocol

from engine.queue_sm import QueueItem

# RFC 5322's practical line-length ceiling; passed to email.header.Header so a
# long non-latin1 caption stays on ONE encoded-word (no "\n " fold, which would
# otherwise embed a literal newline in an HTTP header value).
_HEADER_MAXLINELEN = 998


class CredentialsError(RuntimeError):
    """Raised when ntfy credentials are absent or insecurely permissioned."""


class Transport(Protocol):
    def publish(self, topic: str, message: str, markdown: bool = True) -> None:
        ...

    def publish_file(self, topic: str, path: str | Path, message: str,
                     filename: str) -> None:
        ...


class FakeTransport:
    """Test transport: captures publishes instead of sending them."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.sent_files: list[tuple[str, str, str, str]] = []

    def publish(self, topic: str, message: str, markdown: bool = True) -> None:
        self.sent.append((topic, message))

    def publish_file(self, topic: str, path: str | Path, message: str,
                     filename: str) -> None:
        self.sent_files.append((topic, str(path), message, filename))


class NtfyTransport:
    """Live transport against the self-hosted toto ntfy server (Section 5)."""

    def __init__(self, credentials: dict):
        self.url = credentials.get("url", "").rstrip("/")
        self.token = credentials.get("token")
        self.user = credentials.get("user")
        self.password = credentials.get("password")
        if not self.url:
            raise CredentialsError("ntfy credentials missing 'url'")
        if not self.token and not (self.user and self.password):
            raise CredentialsError(
                "ntfy credentials need a token or a user/password pair "
                "(fail-closed: refusing an unauthenticated transport)"
            )

    def publish(self, topic: str, message: str, markdown: bool = True) -> None:
        """Publish the digest/item body, Markdown-formatted by default.

        Verified against docs.ntfy.sh/publish ("Markdown formatting"): the
        `Markdown` header (alias of `X-Markdown`) set to `yes` enables
        headings/bold/bullets/code-fences in the ntfy app. `markdown=False`
        opts out (plain text), kept for callers that need it.
        """
        import urllib.request

        req = urllib.request.Request(f"{self.url}/{topic}",
                                     data=message.encode("utf-8"))
        if markdown:
            req.add_header("Markdown", "yes")
        self._authorize(req)
        urllib.request.urlopen(req, timeout=10)

    def publish_file(self, topic: str, path: str | Path, message: str,
                     filename: str) -> None:
        """Publish a file as an ntfy attachment (W4 3.9).

        Verified against docs.ntfy.sh/publish (Attachments): PUT the raw file
        bytes as the body with a `Filename` header for the attachment name; the
        caption rides the `Message` header (documented alias of `X-Message`),
        which does not render Markdown, so it stays plain text. Auth reuses
        the existing Bearer/Basic scheme.
        """
        import urllib.request

        req = urllib.request.Request(f"{self.url}/{topic}",
                                     data=Path(path).read_bytes(), method="PUT")
        req.add_header("Filename", _safe_filename(filename))
        if message:
            req.add_header("Message", _safe_header_value(message))
        self._authorize(req)
        urllib.request.urlopen(req, timeout=60)

    def _authorize(self, req) -> None:
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        elif self.user and self.password:
            import base64
            raw = base64.b64encode(f"{self.user}:{self.password}".encode())
            req.add_header("Authorization", f"Basic {raw.decode()}")


def _safe_header_value(value: str) -> str:
    """Guarantee a header-safe caption; never raise on content alone (W4 3.9).

    HTTP header values are sent latin-1 encoded (RFC 7230 / http.client);
    Italian accented text ("societa", "e") round-trips as-is (it IS latin-1),
    but a stray euro sign, curly quote, or emoji in a posting title would
    otherwise raise UnicodeEncodeError deep inside http.client at send time.
    Fall back to a single-line RFC 2047 encoded-word (pure ASCII) when the
    caption itself is not latin-1-safe.
    """
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        return Header(value, "utf-8", maxlinelen=_HEADER_MAXLINELEN).encode()


def _safe_filename(name: str) -> str:
    """ASCII-safe fallback attachment filename; never raise on content alone."""
    try:
        name.encode("ascii")
        return name
    except UnicodeEncodeError:
        ascii_name = unicodedata.normalize("NFKD", name).encode(
            "ascii", "ignore").decode("ascii")
        return ascii_name or "attachment"


def load_credentials(path: str | Path) -> dict:
    """Fail-closed loader for the 0600 key=value credentials file (Section 5)."""
    p = Path(path)
    if not p.exists():
        raise CredentialsError(f"ntfy credentials absent (fail-closed): {path}")
    _require_0600(p)
    creds: dict[str, str] = {}
    for line in p.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        creds[key.strip()] = value.strip()
    return creds


def _require_0600(p: Path) -> None:
    if p.stat().st_mode & 0o077:
        mode = oct(p.stat().st_mode & 0o777)
        raise CredentialsError(f"ntfy credentials must be 0600, found {mode}")


# -- status emoji (channel/state at-a-glance, own directive) ------------------
_EMOJI_READY = "🟢"    # automatable, visible, pending_review
_EMOJI_MANUAL = "✋"    # manual, visible, pending_review
_EMOJI_HELD = "⏸️"      # held/demoted (not visible, or state == demoted)


def render_digest(items: list[QueueItem], demoted_today: int = 0,
                  run_date: str | None = None) -> str:
    """Render the one-push Markdown digest: title + counts + ready/manual
    buckets (D2). `run_date` is the run's own date when the caller has one;
    defaults to today so the existing (unchanged) call site keeps working."""
    items = [i for i in items if not _is_closed(i)]
    ready = [i for i in items if _is_visible_review(i) and i.channel == "automatable"]
    manual = [i for i in items if _is_visible_review(i) and i.channel == "manual"]
    held = [i for i in items if not i.visible and i.state == "demoted"]
    date_str = run_date or date.today().isoformat()
    lines = [
        f"# 💼 JobHunt · {date_str}",
        f"**{len(ready)} ready** · {len(manual)} manual · "
        f"{len(held)} held · {demoted_today} demoted today",
    ]
    _append_bucket(lines, f"## {_EMOJI_READY} Ready (I can submit on your go)",
                   ready, full=False)
    _append_bucket(lines,
                   f"## {_EMOJI_MANUAL} Manual / copy-paste (full material below)",
                   manual, full=True)
    return "\n".join(lines)


def render_item(item: QueueItem, full: bool = False) -> str:
    """Render one posting as a Markdown block: emoji/id/title header, score
    line, one labelled bullet per non-empty field, optional full copy-paste
    material in a code fence, and a reply-by-ID hint. Every structured field
    the payload carries is surfaced; an absent/empty field is simply omitted
    (no blank labels) rather than invented.
    """
    posting = item.payload["posting"]
    breakdown = item.payload["breakdown"]
    lines = [
        f"### {_status_emoji(item)} `{item.item_id}` · {posting['title']}",
        f"**{posting['company_slug']}** · score **{breakdown['total']}/100**",
    ]
    bullets = _render_bullets(posting, breakdown)
    if bullets:
        lines.append("")
        lines.extend(bullets)
    if full and item.payload.get("material"):
        lines.append("")
        lines.append("**Copy-paste material:**")
        lines.append("```")
        lines.append(item.payload["material"])
        lines.append("```")
    lines.append("")
    lines.append(f"_Reply `{item.item_id} <instruction>` to act._")
    return "\n".join(lines)


def _status_emoji(item: QueueItem) -> str:
    if not item.visible or item.state == "demoted":
        return _EMOJI_HELD
    return _EMOJI_MANUAL if item.channel == "manual" else _EMOJI_READY


def _render_bullets(posting: dict, breakdown: dict) -> list[str]:
    bullets: list[str] = []
    _add_bullet(bullets, "✅", "Match", "; ".join(breakdown["matched"]))
    _add_bullet(bullets, "⚠️", "Weak/gaps", "; ".join(breakdown["weak"]))
    _add_bullet(bullets, "📍", "Location", _location_text(posting))
    _add_bullet(bullets, "💰", "Comp", posting.get("comp") or "")
    _add_bullet(bullets, "🚩", "Flags", "; ".join(_flags(posting, breakdown)))
    return bullets


def _add_bullet(bullets: list[str], emoji: str, label: str, value: str) -> None:
    if value:
        bullets.append(f"- {emoji} **{label}:** {value}")


def _location_text(posting: dict) -> str:
    """Locations joined with the remote flag folded in (no empty label when
    neither is present)."""
    text = ", ".join(posting.get("locations") or [])
    if posting.get("remote_flag"):
        return f"{text} (Remote)" if text else "Remote"
    return text


def _flags(posting: dict, breakdown: dict) -> list[str]:
    """ATS pre-check warnings plus the unverified marker, unified under one
    bullet (both are "watch out before you act" signals)."""
    flags = list(breakdown["ats_warnings"])
    if posting.get("unverified"):
        flags.append("unverified (re-verify against vendor endpoint)")
    return flags


def publish_digest(transport: Transport, topic: str, items: list[QueueItem],
                  demoted_today: int = 0, run_date: str | None = None,
                  markdown: bool = True) -> str:
    message = render_digest(items, demoted_today, run_date)
    transport.publish(topic, message, markdown=markdown)
    return message


def _is_visible_review(item: QueueItem) -> bool:
    return item.visible and item.state == "pending_review"


def _is_closed(item: QueueItem) -> bool:
    # Board-absent items are hidden but keep their last state (W4 3.4); the
    # payload flag is the sole marker so no new queue state is invented.
    return bool(item.payload.get("closed"))


def _append_bucket(lines: list[str], heading: str, items: list[QueueItem],
                  full: bool) -> None:
    if not items:
        return
    lines.append("")
    lines.append(heading)
    for item in items:
        lines.append("")
        lines.append(render_item(item, full=full))
