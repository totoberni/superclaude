"""Il generatore e la fonte unica: le liste deny/ask di permission-rules.json devono coincidere."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SETTINGS = Path(__file__).resolve().parents[1] / "settings"


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_permission_rules", SETTINGS / "gen_permission_rules.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GEN = _load_generator()
FILE = json.loads((SETTINGS / "permission-rules.json").read_text(encoding="utf-8"))


def test_deny_matches_file() -> None:
    assert GEN.generate()["deny"] == FILE["deny"]


def test_ask_matches_file() -> None:
    assert GEN.generate()["ask"] == FILE["ask"]


def test_check_cli_exit_zero() -> None:
    assert GEN.main(["--check"]) == 0
