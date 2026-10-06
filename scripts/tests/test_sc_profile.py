"""Test unitari del contratto lib/sc_profile.py (HOME finta, mai le home reali)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import sc_profile  # noqa: E402


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Path.home() su Windows legge USERPROFILE, altrove HOME: si ridefiniscono entrambe
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(sc_profile.ENV_VAR, raising=False)
    return tmp_path


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_unset_or_blank_profile_is_superclaude(home: Path, monkeypatch: pytest.MonkeyPatch, raw: str | None) -> None:
    if raw is not None:
        monkeypatch.setenv(sc_profile.ENV_VAR, raw)
    assert sc_profile.active_profile() == "superclaude"
    assert sc_profile.data_home() == home / ".claude"


def test_deepseek_profile_is_case_insensitive(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sc_profile.ENV_VAR, " DeepSeek ")
    assert sc_profile.active_profile() == "deepseek"
    assert sc_profile.data_home() == home / ".claude-deepseek"
    assert sc_profile.memory_db_path() == home / ".claude-deepseek" / "agent-memory" / ".memory.db"


def test_unknown_profile_fails_closed(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sc_profile.ENV_VAR, "bogus")
    with pytest.raises(sc_profile.ProfileError, match="bogus"):
        sc_profile.active_profile()
    with pytest.raises(sc_profile.ProfileError):
        sc_profile.data_home()
    with pytest.raises(sc_profile.ProfileError):
        sc_profile.data_home("bogus")


def test_explicit_environ_mapping_overrides_process_env(home: Path) -> None:
    assert sc_profile.active_profile({"SC_PROFILE": "deepseek"}) == "deepseek"
    assert sc_profile.active_profile({}) == "superclaude"


def test_code_home_ignores_profile(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert sc_profile.code_home() == home / ".claude"
    monkeypatch.setenv(sc_profile.ENV_VAR, "deepseek")
    assert sc_profile.code_home() == home / ".claude"


def test_other_profiles(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert sc_profile.other_profiles() == ["deepseek"]
    assert sc_profile.other_profiles("deepseek") == ["superclaude"]
    monkeypatch.setenv(sc_profile.ENV_VAR, "deepseek")
    assert sc_profile.other_profiles() == ["superclaude"]


def test_is_within_compares_components_not_prefixes(home: Path) -> None:
    claude, deepseek = home / ".claude", home / ".claude-deepseek"
    assert sc_profile.is_within(claude / "agent-memory" / ".memory.db", claude)
    assert sc_profile.is_within(claude, claude)
    assert not sc_profile.is_within(deepseek / "agent-memory" / ".memory.db", claude)
    assert not sc_profile.is_within(claude, claude / "agent-memory")


def test_is_within_follows_platform_case_rules(home: Path) -> None:
    # Su NTFS le maiuscole non contano; altrove si', come os.path.normcase
    upper = Path(str(home / ".CLAUDE" / "Agent-Memory").upper())
    case_folding = os.path.normcase("A") == os.path.normcase("a")
    assert sc_profile.is_within(upper, home / ".claude") is case_folding


def test_profile_of_path(home: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    assert sc_profile.profile_of_path(home / ".claude" / "plans" / "x.md") == "superclaude"
    assert sc_profile.profile_of_path(home / ".claude-deepseek" / "comms") == "deepseek"
    assert sc_profile.profile_of_path(home / "code" / "proj") is None
    assert sc_profile.profile_of_path(tmp_path_factory.mktemp("elsewhere")) is None


def test_shared_code_entries_hold_code_and_model_cache_only() -> None:
    entries = set(sc_profile.SHARED_CODE_ENTRIES)
    assert {"scripts", "bin", "lib", "hooks", ".venv", ".cache"} <= entries
    assert not entries & {"agent-memory", "plans", "comms", "tmp", "logs", "settings.json", "projects"}
