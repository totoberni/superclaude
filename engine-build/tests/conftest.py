"""Shared test fixtures.

Includes the no-network guard mandated by the W3 gate: sockets are blocked for
every test, so any accidental live call (ntfy publish, ATS fetch) fails loudly
instead of reaching the network. Discovery and the store are fixture-fed and
file-based, so nothing legitimate needs a socket.
"""

from __future__ import annotations

import json
import shutil
import socket
import types
from pathlib import Path

import pytest

from engine.config import load_config
from engine.store import Store

ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Block all socket creation for the duration of every test (W3 gate)."""
    def _blocked(*args, **kwargs):
        raise RuntimeError("network access is blocked in tests (no-network fixture)")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    yield


def load_fixture_json(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def jobhunt_config():
    return load_config(ROOT / "instances" / "jobhunt" / "config.yaml")


@pytest.fixture
def phd_config():
    return load_config(ROOT / "instances" / "phd" / "config.yaml")


@pytest.fixture
def papers_config():
    return load_config(ROOT / "instances" / "papers" / "config.yaml")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "store.db")
    yield s
    s.close()


@pytest.fixture
def job_ssot_path(tmp_path):
    """A writable copy of the toy job SSOT (questionnaire writes back to it)."""
    dest = tmp_path / "job.yaml"
    shutil.copy(FIXTURES / "job.yaml", dest)
    return dest


@pytest.fixture
def real_ssot_path():
    """Path to the SYNTHETIC v1.4-shaped SSOT (profile_map + run tests)."""
    return FIXTURES / "real_ssot_v14.yaml"


@pytest.fixture
def fake_pdflatex():
    """Factory for a runner standing in for pdflatex (no real TeX in tests).

    `make()` writes a stub PDF next to the .tex and returns rc 0; `make(False)`
    returns rc 1 so render_pdf reports failure and the caller falls back to txt.
    """
    def make(create_pdf: bool = True):
        def runner(cmd, **kwargs):
            out_dir = None
            for i, arg in enumerate(cmd):
                if arg == "-output-directory":
                    out_dir = cmd[i + 1]
            stem = Path(cmd[-1]).stem
            if create_pdf:
                (Path(out_dir) / f"{stem}.pdf").write_bytes(
                    b"%PDF-1.4 stub\n%%EOF\n")
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return types.SimpleNamespace(returncode=1, stdout="", stderr="fail")
        return runner
    return make


@pytest.fixture
def greenhouse_raw():
    return load_fixture_json("greenhouse_acme.json")


@pytest.fixture
def lever_raw():
    return load_fixture_json("lever_globex.json")


@pytest.fixture
def ashby_raw():
    return load_fixture_json("ashby_initech.json")
