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
         material=None, closed=False):
    payload = {
        "posting": {"title": "Backend Engineer", "company_slug": "acme",
                    "unverified": unverified},
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
    message = render_digest(items, demoted_today=2)
    assert message.splitlines()[0] == "2 ready · 1 manual · 1 held · 2 demoted today"


def test_render_item_shows_score_breakdown_and_warnings():
    line = render_item(_item("j-1", "pending_review", True, "automatable"))
    assert "[j-1]" in line
    assert "score 80" in line
    assert "matched:" in line
    assert "weak:" in line
    assert "may fail ATS: missing clearance" in line


def test_manual_item_carries_full_material():
    item = _item("j-3", "pending_review", True, "manual",
                material="Dear hiring team, ...")
    message = render_digest([item])
    assert "material (copy-paste)" in message
    assert "Dear hiring team" in message


def test_unverified_flag_rendered():
    line = render_item(_item("j-1", "pending_review", True, "automatable",
                            unverified=True))
    assert "unverified" in line


# -- W4 6b finding 2: digest excludes payload-closed items (regression) --------

def test_closed_item_excluded_from_ready_bucket_and_count():
    items = [
        _item("j-1", "pending_review", True, "automatable"),
        _item("j-2", "pending_review", True, "automatable", closed=True),
    ]
    message = render_digest(items)
    assert message.splitlines()[0] == "1 ready · 0 manual · 0 held · 0 demoted today"
    assert "j-2" not in message


def test_closed_item_excluded_from_manual_bucket_and_count():
    items = [
        _item("j-1", "pending_review", True, "manual"),
        _item("j-2", "pending_review", True, "manual", closed=True),
    ]
    message = render_digest(items)
    assert message.splitlines()[0] == "0 ready · 1 manual · 0 held · 0 demoted today"
    assert "j-2" not in message


def test_closed_item_excluded_from_held_bucket_and_count():
    items = [
        _item("j-1", "demoted", False, "automatable"),
        _item("j-2", "demoted", False, "automatable", closed=True),
    ]
    message = render_digest(items)
    assert message.splitlines()[0] == "0 ready · 0 manual · 1 held · 0 demoted today"
    assert "j-2" not in message


def test_publish_digest_captured_by_fake_transport():
    transport = FakeTransport()
    items = [_item("j-1", "pending_review", True, "automatable")]
    message = publish_digest(transport, "abe-jobsearch", items)
    assert transport.sent == [("abe-jobsearch", message)]


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
