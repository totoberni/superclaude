from engine.discover import (
    AshbyAdapter,
    GreenhouseAdapter,
    LeverAdapter,
    run_discovery,
)


def test_greenhouse_adapter_parses_and_unescapes(greenhouse_raw):
    postings = GreenhouseAdapter().parse(greenhouse_raw, "acme")
    assert len(postings) == 2
    backend = postings[0]
    assert backend.vendor == "greenhouse"
    assert backend.title == "Senior Backend Engineer"
    assert backend.locations == ["London, UK"]
    assert "<p>" not in backend.description
    assert "SQLite" in backend.description


def test_lever_adapter_maps_workplace_and_timestamp(lever_raw):
    posting = LeverAdapter().parse(lever_raw, "globex")[0]
    assert posting.vendor == "lever"
    assert posting.remote_flag is True
    assert posting.updated_ts is not None
    assert posting.url.startswith("https://jobs.lever.co/globex/")


def test_ashby_adapter_captures_secondary_locations(ashby_raw):
    postings = AshbyAdapter().parse(ashby_raw, "initech")
    listed = next(p for p in postings if p.listed)
    assert listed.remote_flag is True
    assert "Remote - EU" in listed.locations
    assert listed.comp == "EUR 70k - 90k"
    unlisted = next(p for p in postings if not p.listed)
    assert unlisted.listed is False


def test_run_discovery_drops_unlisted_and_dedups(store, greenhouse_raw,
                                                lever_raw, ashby_raw):
    sources = [
        (GreenhouseAdapter(), greenhouse_raw, "acme"),
        (LeverAdapter(), lever_raw, "globex"),
        (AshbyAdapter(), ashby_raw, "initech"),
    ]
    first = run_discovery(sources, store)
    # 2 greenhouse + 1 lever + 1 listed ashby = 4 live; unlisted ashby dropped.
    assert len(first) == 4
    assert all(p.listed for p in first)

    for i, posting in enumerate(first):
        store.record_ledger(posting.identity_key(), f"j-{i}", posting.vendor,
                           posting.company_slug, posting.title, posting.url,
                           "seen", 0)
    # Structural no-repeat: everything already known -> nothing net-new.
    assert run_discovery(sources, store) == []


def test_authoritative_sources_are_verified(store, greenhouse_raw):
    sources = [(GreenhouseAdapter(), greenhouse_raw, "acme")]
    postings = run_discovery(sources, store)
    assert all(p.unverified is False for p in postings)
