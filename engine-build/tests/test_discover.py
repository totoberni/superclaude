from engine.providers.ashby.discover import AshbyAdapter
from engine.providers.greenhouse.discover import GreenhouseAdapter
from engine.providers.lever.discover import LeverAdapter
from engine.providers.workable.discover import WorkableAdapter
from engine.run import run_discovery


def test_greenhouse_adapter_parses_and_unescapes(greenhouse_raw):
    postings = GreenhouseAdapter().parse(greenhouse_raw, "acme")
    assert len(postings) == 2
    backend = postings[0]
    assert backend.vendor == "greenhouse"
    assert backend.title == "Senior Backend Engineer"
    assert backend.locations == ["London, UK"]
    assert "<p>" not in backend.description
    assert "SQLite" in backend.description


def test_greenhouse_adapter_parses_tos_readable_fields(greenhouse_raw):
    # W-WT-8-ext: departments/offices/requisition_id/application_deadline/
    # company_name are ToS-readable greenhouse fields that must persist onto
    # Posting. First job exercises a populated department + a null deadline;
    # second job exercises an empty department list + a populated deadline
    # (both are legitimate live shapes, so both must be null-safe).
    postings = GreenhouseAdapter().parse(greenhouse_raw, "acme")
    backend, security = postings[0], postings[1]

    assert backend.departments == ["Engineering"]
    assert backend.offices == ["London"]
    assert backend.requisition_id == "REQ-1001"
    assert backend.application_deadline is None
    assert backend.company_name == "Acme Corp"

    assert security.departments == []
    assert security.offices == ["London"]
    assert security.requisition_id == "REQ-1002"
    assert security.application_deadline == "2026-08-01"
    assert security.company_name == "Acme Corp"


def test_lever_adapter_maps_workplace_and_timestamp(lever_raw):
    posting = LeverAdapter().parse(lever_raw, "globex")[0]
    assert posting.vendor == "lever"
    assert posting.remote_flag is True
    assert posting.updated_ts is not None
    assert posting.url.startswith("https://jobs.lever.co/globex/")


def test_lever_adapter_normalizes_dict_salary_range(lever_raw):
    # Live Lever returns salaryRange as {min, max, currency, interval}, not a
    # string; the fixture predates this shape and would otherwise crash
    # match.py's _max_amount downstream (comp.replace on a dict).
    posting = LeverAdapter().parse(lever_raw, "globex")[0]
    assert posting.comp == "50000-70000 EUR per-year-salary"


def test_ashby_adapter_captures_secondary_locations(ashby_raw):
    postings = AshbyAdapter().parse(ashby_raw, "initech")
    listed = next(p for p in postings if p.listed)
    assert listed.remote_flag is True
    assert "Remote - EU" in listed.locations
    assert listed.comp == "EUR 70k - 90k"
    unlisted = next(p for p in postings if not p.listed)
    assert unlisted.listed is False


def test_discover_workable(workable_raw):
    postings = WorkableAdapter().parse(workable_raw, "powerlines")
    assert len(postings) == 5
    director = postings[0]
    assert director.vendor == "workable"
    assert director.job_id == "57CFF1B2AF"
    assert director.url == "https://apply.workable.com/powerlines/j/57CFF1B2AF/"
    assert director.locations == ["Washington, District of Columbia, United States"]


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
