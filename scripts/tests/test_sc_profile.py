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



# W2-A1-A3-A6-A10: forme device/UNC e file privati fuori dalle data home
@pytest.mark.parametrize("raw,want", [
    (r"\\?\C:\Users\x", r"C:\Users\x"), (r"\\?\UNC\localhost\C$\Users\x", r"C:\Users\x"),
    (r"\\localhost\c$\Users\x", r"c:\Users\x"), (r"\\127.0.0.1\D$\x", r"D:\x"), ("//?/C:/Users/x", "C:/Users/x"),
    (r"\\.\C:\x", r"C:\x"), ("//LOCALHOST/C$/x", "C:/x"), ("cat '//localhost/C$/a' && type \\\\?\\C:\\b", "cat 'C:/a' && type C:\\b"),
    (r"\\server\share\x", r"\\server\share\x"), (r"\\?\UNC\server\share\x", r"\\?\UNC\server\share\x"),
    ("C:/plain", "C:/plain"), ("", ""),
])
def test_local_form(raw: str, want: str) -> None:
    assert sc_profile.local_form(raw) == want


@pytest.mark.parametrize("name,owner", [
    (".claude.json", "superclaude"), (".claude.json.backup", "superclaude"), (".claude.json.backup.123", "superclaude"),
    (".CLAUDE.JSON", "superclaude"), (".claude.jsonx", None), ("claude.json", None), (".claude.json.bak", None),
])
def test_private_file_owner(home: Path, name: str, owner: str | None) -> None:
    assert sc_profile.private_owner_of_name(name) == owner
    assert sc_profile.private_file_owner(home / name) == owner
    assert sc_profile.private_file_owner(home / "sub" / name) is None
    assert sc_profile.private_file_owner(home / ".claude-deepseek" / name) is None


def test_foreign_profile_of_private_files(home: Path) -> None:
    cj = home / ".claude.json"
    assert sc_profile.foreign_profile_of(cj, "deepseek") == "superclaude"
    assert sc_profile.foreign_profile_of(cj, "superclaude") is None
    assert sc_profile.foreign_profile_of("\\\\?\\" + str(cj), "deepseek") == "superclaude"
    ds_cj = home / ".claude-deepseek" / ".claude.json"
    assert sc_profile.foreign_profile_of(ds_cj, "superclaude") == "deepseek"
    assert sc_profile.foreign_profile_of(ds_cj, "deepseek") is None


def test_foreign_profile_of_device_forms(home: Path) -> None:
    for profile, other in (("deepseek", ".claude"), ("superclaude", ".claude-deepseek")):
        w = str(home / other / "agent-memory" / ".memory.db")
        forms = ("\\\\?\\" + w, "\\\\localhost\\" + w[0] + "$" + w[2:], "\\\\?\\UNC\\127.0.0.1\\" + w[0] + "$" + w[2:],
                 "//?/" + w.replace("\\", "/"))
        for form in forms:
            assert sc_profile.foreign_profile_of(form, profile) is not None, form


def test_profiles_built_from_data_home_dirs(home: Path) -> None:
    assert {n: p.name for n, p in sc_profile.profiles().items()} == sc_profile.DATA_HOME_DIRS
