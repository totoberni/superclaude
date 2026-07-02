def test_counter_is_monotonic(store):
    assert store.next_counter() == 1
    assert store.next_counter() == 2
    assert store.next_counter() == 3


def test_ledger_membership_and_dedup(store):
    key = "acme|Backend Engineer|https://x"
    assert not store.is_known(key)
    store.record_ledger(key, "j-1", "greenhouse", "acme", "Backend Engineer",
                        "https://x", "seen", 77)
    assert store.is_known(key)


def test_blacklist_makes_item_known(store):
    key = "acme|Spam Role|https://y"
    store.blacklist_add("j-9", key, "manual")
    assert store.is_known(key)


def test_domain_memory_roundtrip(store):
    store.add_domain_memory("acme", "Acme uses Greenhouse with a 9-question form")
    hits = store.search_domain_memory("Greenhouse")
    assert any("Greenhouse" in h for h in hits)


def test_queue_upsert_get_and_held_count(store):
    store.upsert_queue("j-1", "k1", "pending_review", None, 80, 1, "automatable", {})
    store.upsert_queue("j-2", "k2", "demoted", None, 40, 0, "automatable", {})
    assert store.get_queue_row("j-1")["state"] == "pending_review"
    assert store.held_count() == 1


def test_fts5_available_in_this_build(store):
    # The v1 target build ships FTS5; the store still works via LIKE if not.
    assert store.fts_enabled is True


# -- W4 3.4 additive extensions -------------------------------------------------

def _seed(store, identity_key, item_id, vendor, slug, state, visible):
    store.record_ledger(identity_key, item_id, vendor, slug, "Title",
                        "https://x", "seen", 80)
    store.upsert_queue(item_id, identity_key, state, None, 80, visible,
                       "automatable", {"posting": {}})


def test_fetch_cache_roundtrip_and_upsert(store):
    assert store.get_fetch_cache("https://boards/x") is None
    store.set_fetch_cache("https://boards/x", '"etag1"', "Mon, 01 Jan", '{"jobs":[]}')
    row = store.get_fetch_cache("https://boards/x")
    assert row["etag"] == '"etag1"'
    assert row["last_modified"] == "Mon, 01 Jan"
    assert row["body"] == '{"jobs":[]}'
    # same URL overwrites in place
    store.set_fetch_cache("https://boards/x", '"etag2"', None, '{"jobs":[1]}')
    updated = store.get_fetch_cache("https://boards/x")
    assert updated["etag"] == '"etag2"'
    assert updated["body"] == '{"jobs":[1]}'


def test_close_absent_closes_only_matching_absent_rows(store):
    _seed(store, "acme|A|u1", "j-1", "greenhouse", "acme", "pending_review", 1)
    _seed(store, "acme|B|u2", "j-2", "greenhouse", "acme", "pending_review", 1)
    _seed(store, "acme|C|u3", "j-3", "greenhouse", "acme", "demoted", 0)
    _seed(store, "globex|D|u4", "j-4", "lever", "globex", "pending_review", 1)

    closed = store.close_absent("greenhouse", "acme", {"acme|A|u1"})
    assert set(closed) == {"j-2", "j-3"}

    present = store.get_queue_row("j-1")
    assert present["visible"] == 1
    assert not present["payload"].get("closed")

    gone = store.get_queue_row("j-2")
    assert gone["visible"] == 0
    assert gone["state"] == "pending_review"  # state untouched, no new state invented
    assert gone["payload"]["closed"] is True

    demoted = store.get_queue_row("j-3")
    assert demoted["visible"] == 0
    assert demoted["state"] == "demoted"
    assert demoted["payload"]["closed"] is True

    # other vendor+slug is untouched
    assert store.get_queue_row("j-4")["visible"] == 1


def test_close_absent_spares_parked_blacklisted_submitted(store):
    for state in ("awaiting_input", "blacklisted", "submitted"):
        _seed(store, f"acme|{state}|u", f"j-{state}", "greenhouse", "acme",
              state, 1)
    # nothing present on the board this run, yet these states must be spared
    assert store.close_absent("greenhouse", "acme", set()) == []
    for state in ("awaiting_input", "blacklisted", "submitted"):
        assert not store.get_queue_row(f"j-{state}")["payload"].get("closed")


def test_held_count_excludes_closed_demoted_rows(store):
    # A demoted row closed by close_absent (board-absent) is gone for good,
    # not a held backlog awaiting promotion (w-reviewer HIGH, W4 6b finding 1).
    _seed(store, "acme|A|u1", "j-1", "greenhouse", "acme", "demoted", 0)
    _seed(store, "acme|B|u2", "j-2", "greenhouse", "acme", "demoted", 0)
    assert store.held_count() == 2

    closed = store.close_absent("greenhouse", "acme", set())
    assert set(closed) == {"j-1", "j-2"}
    assert store.held_count() == 0
