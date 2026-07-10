"""Eager-but-light provider auto-registry (`engine.providers._registry`, W5.1
Stage 2e-1).

These tests pin the Stage-2 keystone that DISSOLVES the old
registry <-> discover import cycle:

1. Population + light-import: importing the registry (through any entry point)
   fully populates `PROVIDERS` with all four vendors and loads NO browser module.
2. Cycle dissolution: `PROVIDERS` is fully populated no matter which module is
   imported first (registry-first, plugin-first, or `engine.run`-first) --
   the re-entrant self-registration graph resolves in every direction.
3. Per-vendor contract: the vendor_resolver duck-type (greenhouse has one, the
   others return None), the LAZY call-time capture/fill resolution, the directly
   bound apply_url/resolve_values, the discover-adapter class, and idempotent
   (replace-by-key) registration.
"""

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALL_VENDORS = {"greenhouse", "lever", "ashby", "workable"}


# -- population + light import -------------------------------------------------

def test_all_four_vendors_self_register():
    import engine.providers._registry as r
    assert set(r.all_providers()) == _ALL_VENDORS


def test_importing_registry_loads_no_browser_module():
    # A fresh interpreter so in-process module caching cannot mask the check.
    # Importing the registry populates all four vendors while
    # loading neither the browser driver (patchright / legacy playwright) nor
    # any `engine.browse` module (dissolved in Stage 4) -- the "light" half of
    # "eager-but-light".
    script = (
        "import sys, engine.providers._registry as r; "
        "print(sorted(r.all_providers())); "
        "print('pw' if 'playwright' in sys.modules else 'no-pw'); "
        "print('patch' if 'patchright' in sys.modules else 'no-patch'); "
        "print('browse' if 'engine.browse' in sys.modules else 'no-browse')"
    )
    out = subprocess.run([sys.executable, "-c", script], cwd=_REPO_ROOT,
                         capture_output=True, text=True, check=True)
    lines = out.stdout.splitlines()
    assert lines[0] == "['ashby', 'greenhouse', 'lever', 'workable']", out.stdout
    assert lines[1:] == ["no-pw", "no-patch", "no-browse"], out.stdout


@pytest.mark.parametrize("first_import", [
    "import engine.providers._registry",
    "import engine.providers.greenhouse",
    "import engine.providers.lever",
    "import engine.run",
    "import engine.fetch",
])
def test_registry_fully_populated_regardless_of_first_import(first_import):
    # The dissolve-the-cycle proof: no matter which edge of the graph is entered
    # first, the re-entrant self-registration completes and PROVIDERS holds all
    # four vendors. Run in a fresh interpreter so the "first import" is genuine.
    script = (f"{first_import}\n"
              "import engine.providers._registry as r\n"
              "print(sorted(r.all_providers()))\n")
    out = subprocess.run([sys.executable, "-c", script], cwd=_REPO_ROOT,
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "['ashby', 'greenhouse', 'lever', 'workable']", \
        (out.stdout, out.stderr)


# -- vendor_resolver contract (spec 5c) ----------------------------------------

def test_greenhouse_vendor_resolver_returns_widget_resolver_with_duck_methods():
    import engine.providers._registry as r
    resolver = r.get("greenhouse").vendor_resolver()
    assert resolver is not None
    for method in ("location_path", "key_text_path", "manual_reason",
                   "hidden_widget"):
        assert callable(getattr(resolver, method)), method


@pytest.mark.parametrize("vendor", ["lever", "ashby", "workable"])
def test_non_greenhouse_vendors_have_no_vendor_resolver(vendor):
    import engine.providers._registry as r
    assert r.get(vendor).vendor_resolver() is None


# -- lazy capture / fill (spec 5d) ---------------------------------------------

def test_lazy_capture_and_fill_resolve_to_the_real_plugin_functions():
    import importlib
    import engine.providers.greenhouse as g
    import engine.providers._registry as r
    spec = r.get("greenhouse")
    cap_mod, cap_attr = spec.capture._target
    fil_mod, fil_attr = spec.fill._target
    assert getattr(importlib.import_module(cap_mod), cap_attr) is g.capture
    assert getattr(importlib.import_module(fil_mod), fil_attr) is g.fill


def test_lazy_capture_delegates_to_the_live_plugin_function(monkeypatch):
    # The registered capture is resolved on CALL, not on registration, so a
    # monkeypatched plugin function is honoured (the same import-alias safety the
    # old registry's lazy refs gave for browse.capture_*).
    import engine.providers.greenhouse as g
    import engine.providers._registry as r
    calls = []

    def fake_capture(slug, job_id, opener=None):
        calls.append((slug, job_id, opener))
        return "SENTINEL"

    monkeypatch.setattr(g, "capture", fake_capture)
    result = r.get("greenhouse").capture("acme", "5501001", "OP")
    assert result == "SENTINEL"
    assert calls == [("acme", "5501001", "OP")]


# -- directly-bound wiring + adapter class -------------------------------------

def test_apply_url_and_resolve_values_are_bound_directly():
    import engine.providers._registry as r
    import engine.providers.greenhouse as g
    spec = r.get("greenhouse")
    assert spec.apply_url is g.apply_url
    assert spec.resolve_values is g.resolve_values


def test_registered_adapter_is_the_vendor_discover_class():
    import engine.providers._registry as r
    from engine.providers.ashby.discover import AshbyAdapter
    from engine.providers.greenhouse.discover import GreenhouseAdapter
    from engine.providers.lever.discover import LeverAdapter
    from engine.providers.workable.discover import WorkableAdapter
    assert r.get("greenhouse").adapter is GreenhouseAdapter
    assert r.get("lever").adapter is LeverAdapter
    assert r.get("ashby").adapter is AshbyAdapter
    assert r.get("workable").adapter is WorkableAdapter


# -- idempotent (replace-by-key) registration (spec 5e) ------------------------

def test_register_is_idempotent_by_replace():
    import engine.providers._registry as r

    def _stub(*args, **kwargs):
        return None

    try:
        r.register(vendor="_tmp_probe", capture=_stub, fill=_stub,
                   apply_url=_stub, resolve_values=_stub, adapter=None)
        count_after_first = len(r.PROVIDERS)
        replacement = r.register(vendor="_tmp_probe", capture=_stub, fill=_stub,
                                 apply_url=_stub, resolve_values=_stub,
                                 adapter=None, supported=False)
        # A second register for the same vendor REPLACES (last write wins); it
        # neither raises nor duplicates the entry.
        assert len(r.PROVIDERS) == count_after_first
        assert r.get("_tmp_probe") is replacement
        assert r.get("_tmp_probe").supported is False
    finally:
        r.PROVIDERS.pop("_tmp_probe", None)
