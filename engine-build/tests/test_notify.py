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
         material=None):
    payload = {
        "posting": {"title": "Backend Engineer", "company_slug": "acme",
                    "unverified": unverified},
        "breakdown": {"total": total, "matched": ["role: Backend Engineer"],
                      "weak": ["comp unknown"],
                      "ats_warnings": ["may fail ATS: missing clearance"]},
    }
    if material:
        payload["material"] = material
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


def test_publish_digest_captured_by_fake_transport():
    transport = FakeTransport()
    items = [_item("j-1", "pending_review", True, "automatable")]
    message = publish_digest(transport, "abe-jobsearch", items)
    assert transport.sent == [("abe-jobsearch", message)]


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
