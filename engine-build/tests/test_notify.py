from datetime import date

import pytest

from engine.notify import (
    CredentialsError,
    FakeTransport,
    load_credentials,
    publish_digest,
    render_digest,
    render_item,
)
from engine.queue_sm import QueueItem


def _item(item_id, state, visible, channel, total=80, unverified=False,
         material=None, closed=False, locations=None, remote_flag=False,
         comp=None):
    payload = {
        "posting": {"title": "Backend Engineer", "company_slug": "acme",
                    "unverified": unverified, "locations": locations or [],
                    "remote_flag": remote_flag, "comp": comp},
        "breakdown": {"total": total, "matched": ["role: Backend Engineer"],
                      "weak": ["comp unknown"],
                      "ats_warnings": ["may fail ATS: missing clearance"]},
    }
    if material:
        payload["material"] = material
    if closed:
        payload["closed"] = True
    return QueueItem(item_id, "k", state, None, total, visible, channel, payload)


def test_digest_header_counts():
    items = [
        _item("j-1", "pending_review", True, "automatable"),
        _item("j-2", "pending_review", True, "automatable"),
        _item("j-3", "pending_review", True, "manual"),
        _item("j-4", "demoted", False, "automatable", total=40),
    ]
    message = render_digest(items, demoted_today=2, run_date="2026-07-05")
    lines = message.splitlines()
    assert lines[0] == "# 💼 JobHunt · 2026-07-05"
    assert lines[1] == "**2 ready** · 1 manual · 1 held · 2 demoted today"


def test_digest_defaults_to_todays_date_when_run_date_omitted():
    message = render_digest([_item("j-1", "pending_review", True, "automatable")])
    assert message.splitlines()[0] == f"# 💼 JobHunt · {date.today().isoformat()}"


def test_render_item_shows_score_breakdown_and_warnings():
    line = render_item(_item("j-1", "pending_review", True, "automatable"))
    assert "`j-1`" in line
    assert "score **80/100**" in line
    assert "**Match:**" in line
    assert "role: Backend Engineer" in line
    assert "**Weak/gaps:**" in line
    assert "comp unknown" in line
    assert "**Flags:**" in line
    assert "may fail ATS: missing clearance" in line


def test_render_item_header_shows_ready_emoji_id_title_and_company():
    line = render_item(_item("j-1", "pending_review", True, "automatable"))
    lines = line.splitlines()
    assert lines[0] == "### 🟢 `j-1` · Backend Engineer"
    assert lines[1] == "**acme** · score **80/100**"


def test_render_item_manual_channel_uses_manual_emoji():
    line = render_item(_item("j-3", "pending_review", True, "manual"))
    assert line.splitlines()[0].startswith("### ✋ `j-3`")


def test_render_item_held_demoted_uses_paused_emoji():
    line = render_item(_item("j-4", "demoted", False, "automatable"))
    assert line.splitlines()[0].startswith("### ⏸️ `j-4`")


def test_render_item_location_and_comp_bullets_shown_when_present():
    line = render_item(_item("j-1", "pending_review", True, "automatable",
                            locations=["London", "Remote-EU"], remote_flag=True,
                            comp="£90k-110k"))
    assert "- 📍 **Location:** London, Remote-EU (Remote)" in line
    assert "- 💰 **Comp:** £90k-110k" in line


def test_render_item_remote_with_no_locations_shows_bare_remote():
    line = render_item(_item("j-1", "pending_review", True, "automatable",
                            remote_flag=True))
    assert "- 📍 **Location:** Remote" in line


def test_render_item_omits_empty_location_and_comp_bullets():
    line = render_item(_item("j-1", "pending_review", True, "automatable"))
    assert "Location" not in line
    assert "Comp" not in line


def test_manual_item_carries_full_material_in_code_fence():
    item = _item("j-3", "pending_review", True, "manual",
                material="Dear hiring team, ...")
    message = render_digest([item])
    assert "**Copy-paste material:**" in message
    assert "```\nDear hiring team, ...\n```" in message


def test_ready_bucket_item_never_shows_material_even_if_present():
    item = _item("j-1", "pending_review", True, "automatable",
                material="Dear hiring team, ...")
    message = render_digest([item])
    assert "Copy-paste material" not in message
    assert "Dear hiring team" not in message


def test_unverified_flag_rendered_under_flags_bullet():
    line = render_item(_item("j-1", "pending_review", True, "automatable",
                            unverified=True))
    assert "**Flags:**" in line
    assert "unverified (re-verify against vendor endpoint)" in line


def test_render_item_ends_with_reply_hint():
    line = render_item(_item("j-1", "pending_review", True, "automatable"))
    assert line.splitlines()[-1] == "_Reply `j-1 <instruction>` to act._"


# -- W4 6b finding 2: digest excludes payload-closed items (regression) --------

def test_closed_item_excluded_from_ready_bucket_and_count():
    items = [
        _item("j-1", "pending_review", True, "automatable"),
        _item("j-2", "pending_review", True, "automatable", closed=True),
    ]
    message = render_digest(items, run_date="2026-07-05")
    assert message.splitlines()[1] == "**1 ready** · 0 manual · 0 held · 0 demoted today"
    assert "j-2" not in message


def test_closed_item_excluded_from_manual_bucket_and_count():
    items = [
        _item("j-1", "pending_review", True, "manual"),
        _item("j-2", "pending_review", True, "manual", closed=True),
    ]
    message = render_digest(items, run_date="2026-07-05")
    assert message.splitlines()[1] == "**0 ready** · 1 manual · 0 held · 0 demoted today"
    assert "j-2" not in message


def test_closed_item_excluded_from_held_bucket_and_count():
    items = [
        _item("j-1", "demoted", False, "automatable"),
        _item("j-2", "demoted", False, "automatable", closed=True),
    ]
    message = render_digest(items, run_date="2026-07-05")
    assert message.splitlines()[1] == "**0 ready** · 0 manual · 1 held · 0 demoted today"
    assert "j-2" not in message


def test_publish_digest_captured_by_fake_transport():
    transport = FakeTransport()
    items = [_item("j-1", "pending_review", True, "automatable")]
    message = publish_digest(transport, "abe-jobsearch", items)
    assert transport.sent == [("abe-jobsearch", message)]


def test_publish_digest_passes_markdown_true_by_default_to_transport():
    calls = []

    class _SpyTransport:
        def publish(self, topic, message, markdown=True):
            calls.append(markdown)

        def publish_file(self, *args, **kwargs):
            raise AssertionError("publish_file not expected in this test")

    items = [_item("j-1", "pending_review", True, "automatable")]
    publish_digest(_SpyTransport(), "abe-jobsearch", items)
    assert calls == [True]


def test_publish_digest_markdown_false_propagates_to_transport():
    calls = []

    class _SpyTransport:
        def publish(self, topic, message, markdown=True):
            calls.append(markdown)

        def publish_file(self, *args, **kwargs):
            raise AssertionError("publish_file not expected in this test")

    items = [_item("j-1", "pending_review", True, "automatable")]
    publish_digest(_SpyTransport(), "abe-jobsearch", items, markdown=False)
    assert calls == [False]


def test_publish_file_captured_by_fake_transport(tmp_path):
    transport = FakeTransport()
    pdf = tmp_path / "j-1-acme-cover-letter.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    transport.publish_file("abe-jobsearch", pdf, "[j-1] Backend @ acme",
                           pdf.name)
    assert transport.sent == []  # attachments never land on the digest channel
    assert transport.sent_files == [
        ("abe-jobsearch", str(pdf), "[j-1] Backend @ acme",
         "j-1-acme-cover-letter.pdf")
    ]


def test_ntfy_publish_file_builds_put_with_attachment_headers(tmp_path,
                                                             monkeypatch):
    import urllib.request

    from engine.notify import NtfyTransport

    transport = NtfyTransport({"url": "https://ntfy.example", "token": "tk_abc"})
    pdf = tmp_path / "j-1-acme-cover-letter.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["data"] = req.data
        captured["headers"] = dict(req.header_items())
        return None

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    transport.publish_file("abe-jobsearch", pdf, "caption here", pdf.name)

    assert captured["url"] == "https://ntfy.example/abe-jobsearch"
    assert captured["method"] == "PUT"
    assert captured["data"] == b"%PDF-1.4\n"  # raw file bytes as the body
    # header names are stored capitalised by urllib.request.Request.add_header
    assert captured["headers"]["Filename"] == "j-1-acme-cover-letter.pdf"
    assert captured["headers"]["Message"] == "caption here"
    assert captured["headers"]["Authorization"] == "Bearer tk_abc"
    # publish_file captions never render Markdown (Message header), so no
    # Markdown header is ever attached to this request.
    assert "Markdown" not in captured["headers"]


# -- markdown-enabled digest push (owner directive: legible phone formatting) --

def test_ntfy_publish_sets_markdown_header_by_default(monkeypatch):
    import urllib.request

    from engine.notify import NtfyTransport

    transport = NtfyTransport({"url": "https://ntfy.example", "token": "tk_abc"})

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data
        captured["headers"] = dict(req.header_items())
        return None

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    transport.publish("abe-jobsearch", "# hello")

    assert captured["headers"]["Markdown"] == "yes"
    assert captured["data"] == b"# hello"


def test_ntfy_publish_markdown_false_omits_header(monkeypatch):
    import urllib.request

    from engine.notify import NtfyTransport

    transport = NtfyTransport({"url": "https://ntfy.example", "token": "tk_abc"})

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        return None

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    transport.publish("abe-jobsearch", "plain text", markdown=False)

    assert "Markdown" not in captured["headers"]


# -- W4 6b finding 4a: publish_file caption/filename safety --------------------

def test_publish_file_caption_with_non_latin1_chars_does_not_raise(tmp_path,
                                                                  monkeypatch):
    import urllib.request

    from engine.notify import NtfyTransport

    transport = NtfyTransport({"url": "https://ntfy.example", "token": "tk_abc"})
    pdf = tmp_path / "j-1-acme-cover-letter.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        # reproduce the real http.client send-time boundary: header values
        # are latin-1 encoded, so a bad value would still fail loud here.
        for value in captured["headers"].values():
            value.encode("latin-1")
        return None

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    caption = "[j-1] Ingegnere davvero interessante € bonus @ società"
    transport.publish_file("abe-jobsearch", pdf, caption, pdf.name)

    message = captured["headers"]["Message"]
    assert message.encode("latin-1")  # never raises
    assert message.startswith("=?utf-8?")  # RFC 2047 encoded-word
    assert "\n" not in message  # single line, no header-folding newline


def test_publish_file_caption_with_latin1_accents_passes_through(tmp_path,
                                                                 monkeypatch):
    import urllib.request

    from engine.notify import NtfyTransport

    transport = NtfyTransport({"url": "https://ntfy.example", "token": "tk_abc"})
    pdf = tmp_path / "j-1-acme-cover-letter.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        return None

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    caption = "[j-1] Ingegnere software è perfetto per società Acme"
    transport.publish_file("abe-jobsearch", pdf, caption, pdf.name)

    # pure latin-1-safe Italian text needs no encoding, rides as-is
    assert captured["headers"]["Message"] == caption


def test_publish_file_filename_falls_back_to_ascii_safe_name(tmp_path,
                                                             monkeypatch):
    import urllib.request

    from engine.notify import NtfyTransport

    transport = NtfyTransport({"url": "https://ntfy.example", "token": "tk_abc"})
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        return None

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    filename = "società-è-fantàstica-cover-letter.pdf"
    transport.publish_file("abe-jobsearch", pdf, "caption", filename)

    safe_name = captured["headers"]["Filename"]
    assert safe_name.isascii()
    assert safe_name != filename
    assert safe_name.endswith("-cover-letter.pdf")


def test_credentials_absent_fails_closed(tmp_path):
    with pytest.raises(CredentialsError):
        load_credentials(tmp_path / "nope")


def test_credentials_reject_loose_permissions(tmp_path):
    creds = tmp_path / "credentials"
    creds.write_text("url=https://ntfy.example\ntoken=tk_abc\n")
    creds.chmod(0o644)
    with pytest.raises(CredentialsError):
        load_credentials(creds)


def test_credentials_parse_when_secure(tmp_path):
    creds = tmp_path / "credentials"
    creds.write_text("# ntfy\nurl=https://ntfy.example\ntoken=tk_abc\n")
    creds.chmod(0o600)
    parsed = load_credentials(creds)
    assert parsed["url"] == "https://ntfy.example"
    assert parsed["token"] == "tk_abc"
