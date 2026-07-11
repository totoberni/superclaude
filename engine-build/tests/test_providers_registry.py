"""Provider registry: SSOT wiring, URL detection, and call-site equivalence.

The registry consolidates the four W4-scattered vendor-detection sites
(fetch.endpoint_for + _ADAPTERS + _VENDORS, _registry.apply_url,
run._collect_fieldmap). These tests pin two things:

1. `resolve` / `detect` behave as the single source of truth; all four vendors
   (greenhouse/lever/ashby/workable) are registered-and-supported (workable was
   un-stubbed in W5.4).
2. The three refactored call sites produce results byte-identical to the
   pre-refactor per-vendor branches (golden values), preserving every signature,
   URL, dispatch target, and error path. The lazy browser-vendor references are
   verified to keep `engine.run` free of playwright and of any reintroduced
   `engine.browse` at import.
"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine import fetch, run
from engine.providers.ashby.discover import AshbyAdapter
from engine.providers.greenhouse.discover import GreenhouseAdapter
from engine.providers.lever.discover import LeverAdapter
from engine.providers.workable.discover import WorkableAdapter
from engine.fetch import Source
from engine.providers import _registry, ashby, greenhouse, lever
from engine.providers.ashby.discover import ashby_endpoint
from engine.providers.greenhouse.discover import greenhouse_endpoint
from engine.providers.lever.discover import lever_endpoint

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _posting(slug, job_id):
    return SimpleNamespace(company_slug=slug, job_id=job_id)


# -- resolve(): the vendor -> ProviderSpec source of truth ---------------------

def test_resolve_returns_expected_wiring_for_each_real_vendor():
    gh = _registry.resolve("greenhouse")
    assert gh.adapter is GreenhouseAdapter
    assert gh.endpoint_fn is greenhouse_endpoint
    assert gh.capture._target == ("engine.providers.greenhouse", "capture")
    assert gh.apply_url is greenhouse.apply_url
    assert gh.supported is True

    lv = _registry.resolve("lever")
    assert lv.adapter is LeverAdapter
    assert lv.endpoint_fn is lever_endpoint
    assert lv.capture._target == ("engine.providers.lever", "capture")
    assert lv.apply_url is lever.apply_url

    ash = _registry.resolve("ashby")
    assert ash.adapter is AshbyAdapter
    assert ash.endpoint_fn is ashby_endpoint
    assert ash.capture._target == ("engine.providers.ashby", "capture")
    assert ash.apply_url is ashby.apply_url


def test_resolve_unknown_vendor_raises_value_error():
    with pytest.raises(ValueError, match="unknown vendor 'nope'"):
        _registry.resolve("nope")


def test_workable_is_a_registered_supported_provider():
    # W5.4 un-stubbed workable: it was formerly supported=False, adapter=None,
    # with every wiring fn raising NotImplementedError("...W5.4..."). It is now a
    # first-class supported provider with a live adapter and callable wiring.
    spec = _registry.resolve("workable")
    assert spec.supported is True
    assert spec.adapter is WorkableAdapter
    assert spec.capture._target == ("engine.providers.workable", "capture")
    # every wiring slot is a live callable now (no NotImplementedError); the
    # browser-free URL builders return their real values when called.
    assert callable(spec.capture)
    assert spec.endpoint_fn("powerlines") == \
        "https://apply.workable.com/api/v1/widget/accounts/powerlines"
    assert spec.apply_url("powerlines", "57CFF1B2AF") == \
        "https://apply.workable.com/powerlines/j/57CFF1B2AF/apply/"


# -- detect(): the one place a URL / host maps to a vendor ---------------------

@pytest.mark.parametrize("url_or_host,expected", [
    ("https://boards.greenhouse.io/acme/jobs/123", "greenhouse"),
    ("https://job-boards.greenhouse.io/acme/jobs/9", "greenhouse"),
    ("https://boards-api.greenhouse.io/v1/boards/acme/jobs", "greenhouse"),
    ("https://jobs.lever.co/globex/req-9/apply", "lever"),
    ("https://api.eu.lever.co/v0/postings/globex", "lever"),
    ("jobs.ashbyhq.com", "ashby"),
    ("https://api.ashbyhq.com/posting-api/job-board/initech", "ashby"),
    ("https://apply.workable.com/foo/j/123", "workable"),
    ("https://www.linkedin.com/jobs/view/1", None),
    ("greenhouse.io", "greenhouse"),
    ("", None),
    ("not a url", None),
])
def test_detect_maps_hosts_to_vendor(url_or_host, expected):
    assert _registry.detect(url_or_host) == expected


# -- fetch.endpoint_for: golden URLs + preserved error path --------------------

@pytest.mark.parametrize("source,expected", [
    (Source("greenhouse", "acme", "Acme"),
     "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"),
    (Source("lever", "globex", "Globex"),
     "https://api.lever.co/v0/postings/globex?mode=json"),
    (Source("lever", "globex", "Globex", region="eu"),
     "https://api.eu.lever.co/v0/postings/globex?mode=json"),
    (Source("ashby", "initech", "Initech"),
     "https://api.ashbyhq.com/posting-api/job-board/initech?includeCompensation=true"),
])
def test_endpoint_for_golden_urls(source, expected):
    assert fetch.endpoint_for(source) == expected


def test_endpoint_for_unknown_vendor_preserves_error():
    with pytest.raises(ValueError, match="unknown vendor: linkedin"):
        fetch.endpoint_for(Source("linkedin", "x", "X"))


def test_adapters_and_vendors_are_registry_projections():
    # the shims kept for import-compat mirror the registry (workable now included,
    # un-stubbed in W5.4)
    # Order is pinned to `engine.providers.VENDOR_ORDER` (registration walks
    # that tuple; a post-import check raises on divergence). The literal below
    # stays HARDCODED on purpose: it bites independently of the pin, so a
    # coordinated wrong edit of VENDOR_ORDER cannot silently pass. It is an
    # iteration order only (no functional dependency).
    assert fetch._VENDORS == ("greenhouse", "ashby", "lever", "workable")
    assert fetch._ADAPTERS == {
        "greenhouse": GreenhouseAdapter,
        "lever": LeverAdapter,
        "ashby": AshbyAdapter,
        "workable": WorkableAdapter,
    }
    # adapter_for still hands back a fresh instance and still KeyErrors unknowns
    assert isinstance(fetch.adapter_for("greenhouse"), GreenhouseAdapter)
    with pytest.raises(KeyError):
        fetch.adapter_for("linkedin")


# -- _registry.apply_url: golden URLs + preserved error path -------------------

@pytest.mark.parametrize("vendor,slug,job_id,expected", [
    ("greenhouse", "acme", "123", "https://boards.greenhouse.io/acme/jobs/123"),
    ("lever", "globex", "req-9", "https://jobs.lever.co/globex/req-9/apply"),
    ("ashby", "initech", "abc", "https://jobs.ashbyhq.com/initech/abc/application"),
])
def test_apply_url_golden(vendor, slug, job_id, expected):
    assert _registry.apply_url(vendor, slug, job_id) == expected


def test_apply_url_unknown_vendor_preserves_error():
    # workable used to be the example UNKNOWN vendor here; W5.4 made it supported
    # (_registry.apply_url now returns its real apply URL), so the error-path
    # coverage is repointed to a still-unknown vendor. "workday" is genuinely
    # unregistered.
    with pytest.raises(ValueError,
                       match=r"unknown vendor 'workday' \(expected greenhouse"):
        _registry.apply_url("workday", "x", "y")


# -- run._collect_fieldmap: golden dispatch + preserved error path -------------

def test_collect_fieldmap_greenhouse_passes_opener(monkeypatch):
    from importlib import import_module
    capture_mod = import_module("engine.providers.greenhouse.capture")
    sentinel = object()
    calls = []

    def fake_capture_greenhouse(slug, job_id, opener=None):
        calls.append((slug, job_id, opener))
        return sentinel

    monkeypatch.setattr(capture_mod, "capture_greenhouse", fake_capture_greenhouse)
    opener = object()
    result = run._collect_fieldmap("greenhouse", _posting("acme", "5501001"), opener)
    assert result is sentinel
    assert calls == [("acme", "5501001", opener)]


def test_collect_fieldmap_ashby_routes_to_vendor_capture_ignoring_opener(monkeypatch):
    calls = []

    def fake_capture_ashby(slug, job_id, browser_factory=None):
        calls.append((slug, job_id))
        return object()

    from importlib import import_module
    monkeypatch.setattr(import_module("engine.providers.ashby.capture"),
                        "capture_ashby", fake_capture_ashby)
    run._collect_fieldmap("ashby", _posting("initech", "xyz"), object())
    assert calls == [("initech", "xyz")]


def test_collect_fieldmap_lever_routes_to_vendor_capture_ignoring_opener(monkeypatch):
    calls = []

    def fake_capture_lever(slug, job_id, browser_factory=None):
        calls.append((slug, job_id))
        return object()

    from importlib import import_module
    monkeypatch.setattr(import_module("engine.providers.lever.capture"),
                        "capture_lever", fake_capture_lever)
    run._collect_fieldmap("lever", _posting("globex", "req-9"), object())
    assert calls == [("globex", "req-9")]


# workable was removed from this list in W5.4 (it is now a supported vendor with a
# real capture, so run._collect_fieldmap no longer raises for it); the error
# path is still covered for a genuinely-unsupported vendor.
@pytest.mark.parametrize("vendor", ["nope"])
def test_collect_fieldmap_unsupported_vendor_preserves_error(vendor):
    with pytest.raises(ValueError, match="no field-map capture for vendor"):
        run._collect_fieldmap(vendor, _posting("a", "b"), None)


# -- lazy-reference invariant: the daily poller stays playwright-free ----------

def test_importing_run_does_not_import_browse_or_browser_driver():
    # engine.run imports the registry at top level; the registry must keep the
    # browser-vendor refs lazy so the daily poll path never loads a browser-capture
    # module or the browser driver (patchright, or the legacy playwright name).
    # Checked in a fresh interpreter so in-process module caching cannot mask the
    # check; the `engine.browse` probe below is a tripwire against reintroducing
    # that module (dissolved in Stage 4).
    script = (
        "import sys, engine.run; "
        "print('browse' if 'engine.browse' in sys.modules else 'no-browse'); "
        "print('pw' if 'playwright' in sys.modules else 'no-pw'); "
        "print('patch' if 'patchright' in sys.modules else 'no-patch')"
    )
    out = subprocess.run([sys.executable, "-c", script], cwd=_REPO_ROOT,
                         capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["no-browse", "no-pw", "no-patch"], out.stdout
