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
