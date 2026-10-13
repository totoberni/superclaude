"""Test di scripts/settings/apply_settings.py su home finte (mai le home reali)."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "settings"))
import apply_settings  # noqa: E402

REAL_HOOKS = ROOT / "hooks" / "hooks.json"
# Nome della cartella home finta (fixture home): l'applicatore lo usa per {{HOME_USER}}
FAKE_HOME_NAME = "home"
RULES_RAW = (ROOT / "scripts" / "settings" / "permission-rules.json").read_text(encoding="utf-8")
RULES = json.loads(RULES_RAW.replace("{{HOME_USER}}", FAKE_HOME_NAME))
TEMPLATE = json.loads((ROOT / "scripts" / "settings" / "deepseek.base.json").read_text(encoding="utf-8"))
OWNER: dict[str, Any] = {
    "env": {"HF_HUB_OFFLINE": "1", "OWNER_VAR": "x"},
    "model": "opus",
    "theme": "dark",
    "tui": {"compact": True},
    "statusLine": {"type": "command", "command": "owner-status.exe"},
    "permissions": {
        "allow": ["Read", "Bash(git status:*)"],
        "deny": ["Bash(owner-deny *)"],
        "ask": ["Bash(owner-ask *)", "Bash(git reset --hard*)"],
    },
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Path.home() su Windows legge USERPROFILE, altrove HOME: si ridefiniscono entrambe
    fake = tmp_path / FAKE_HOME_NAME
    hooks_dir = fake / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    shutil.copyfile(REAL_HOOKS, hooks_dir / "hooks.json")
    (hooks_dir / "dispatch.py").write_text("", encoding="utf-8")
    local = tmp_path / "local"
    py = local / "Programs" / "Python" / "Python312" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_bytes(b"")
    (fake / ".claude-deepseek").mkdir()
    monkeypatch.setenv("USERPROFILE", str(fake))
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("SC_PROFILE", raising=False)
    return fake


def settings_file(home: Path, profile: str) -> Path:
    return home / (".claude" if profile == "superclaude" else ".claude-deepseek") / "settings.json"


def write_owner(path: Path, data: dict[str, Any]) -> bytes:
    raw = json.dumps(data, indent=4).encode("utf-8")
    path.write_bytes(raw)
    return raw


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob("settings.json.bak-*"))


def run(*argv: str) -> int:
    return apply_settings.main(list(argv))


def substituted(node: Any, values: dict[str, str]) -> Any:
    # Oracolo indipendente: sostituzione campo per campo sull'albero JSON
    if isinstance(node, dict):
        return {k: substituted(v, values) for k, v in node.items()}
    if isinstance(node, list):
        return [substituted(v, values) for v in node]
    if isinstance(node, str):
        for token, value in values.items():
            node = node.replace(token, value)
    return node


def expected_hooks(home: Path, local_python: Path) -> dict[str, Any]:
    # L'applicatore sostituisce sul testo grezzo, l'oracolo sull'albero
    values = {"{{PYTHON}}": local_python.as_posix(), "{{DISPATCH}}": (home / ".claude/hooks/dispatch.py").as_posix()}
    tree = json.loads(REAL_HOOKS.read_text(encoding="utf-8"))
    tree.pop("_comment")
    return substituted(tree, values)


def rendered_template(home: Path) -> dict[str, Any]:
    values = {"{{HOME}}": home.as_posix(), "{{DS_HOME}}": (home / ".claude-deepseek").as_posix()}
    return substituted(TEMPLATE, values)


def test_dry_run_writes_nothing(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = settings_file(home, "superclaude")
    raw = write_owner(target, OWNER)
    assert run("--profile", "superclaude", "--dry-run", "--full") == 0
    printed = json.loads(capsys.readouterr().out)
    assert "hooks" in printed and printed["permissions"]["disableBypassPermissionsMode"] == "disable"
    assert target.read_bytes() == raw
    assert backups(target) == []


def test_dry_run_on_missing_deepseek_file_creates_nothing(home: Path) -> None:
    assert run("--profile", "deepseek", "--dry-run") == 0
    assert not settings_file(home, "deepseek").exists()


def test_backup_then_write_lf_without_bom(home: Path) -> None:
    target = settings_file(home, "superclaude")
    raw = write_owner(target, OWNER)
    assert run("--profile", "superclaude") == 0
    (bak,) = backups(target)
    assert bak.read_bytes() == raw
    data = target.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf") and b"\r\n" not in data and data.endswith(b"\n")
    assert json.loads(data)["hooks"]


def test_revert_restores_latest_backup(home: Path) -> None:
    target = settings_file(home, "superclaude")
    raw = write_owner(target, OWNER)
    assert run("--profile", "superclaude") == 0
    assert target.read_bytes() != raw
    assert run("--profile", "superclaude", "--revert", "--yes") == 0
    assert target.read_bytes() == raw


def test_revert_without_backup_fails(home: Path) -> None:
    write_owner(settings_file(home, "superclaude"), OWNER)
    assert run("--profile", "superclaude", "--revert") == 1


def test_second_run_is_idempotent(home: Path) -> None:
    target = settings_file(home, "superclaude")
    write_owner(target, OWNER)
    assert run("--profile", "superclaude") == 0
    first = target.read_bytes()
    assert run("--profile", "superclaude") == 0
    assert target.read_bytes() == first
    assert len(backups(target)) == 1


@pytest.mark.parametrize("profile", ["superclaude", "deepseek"])
def test_union_keeps_owner_entries_without_duplicates(home: Path, profile: str) -> None:
    target = settings_file(home, profile)
    write_owner(target, OWNER)
    assert run("--profile", profile) == 0
    perms = json.loads(target.read_text(encoding="utf-8"))["permissions"]
    assert perms["allow"] == OWNER["permissions"]["allow"]
    for kind in ("deny", "ask"):
        assert perms[kind][: len(OWNER["permissions"][kind])] == OWNER["permissions"][kind]
        assert set(RULES[kind]) <= set(perms[kind])
        assert len(perms[kind]) == len(set(perms[kind]))
    assert perms["disableBypassPermissionsMode"] == "disable"


def test_superclaude_touches_only_hooks_and_permissions(home: Path) -> None:
    target = settings_file(home, "superclaude")
    write_owner(target, OWNER)
    assert run("--profile", "superclaude") == 0
    data = json.loads(target.read_text(encoding="utf-8"))
    for key in ("env", "model", "theme", "tui", "statusLine"):
        assert data[key] == OWNER[key]
    assert set(data) == set(OWNER) | {"hooks"}
    assert "apiKeyHelper" not in data and "skipWebFetchPreflight" not in data


def test_deepseek_created_from_template(home: Path) -> None:
    target = settings_file(home, "deepseek")
    assert run("--profile", "deepseek") == 0
    data = json.loads(target.read_text(encoding="utf-8"))
    want = rendered_template(home)
    assert data["env"] == want["env"]
    for key in ("apiKeyHelper", "skipWebFetchPreflight", "statusLine"):
        assert data[key] == want[key]
    ds = (home / ".claude-deepseek").as_posix()
    assert data["apiKeyHelper"] == f"{ds}/.venv/Scripts/python.exe {ds}/bin/key-helper.py"
    assert data["statusLine"]["command"] == f"{ds}/.venv/Scripts/python.exe {ds}/bin/ds-statusline.py"
    assert "{{DS_HOME}}" not in json.dumps(data) and "{{HOME}}" not in json.dumps(data)
    assert data["hooks"] == expected_hooks(home, home.parent / "local/Programs/Python/Python312/python.exe")
    assert set(want["permissions"]["deny"]) | set(RULES["deny"]) == set(data["permissions"]["deny"])
    assert data["permissions"]["disableBypassPermissionsMode"] == "disable"
    assert backups(target) == []


def test_deepseek_merge_leaves_unnamed_keys_alone(home: Path) -> None:
    target = settings_file(home, "deepseek")
    write_owner(target, OWNER)
    assert run("--profile", "deepseek") == 0
    data = json.loads(target.read_text(encoding="utf-8"))
    for key in ("model", "theme", "tui"):
        assert data[key] == OWNER[key]
    assert data["env"]["OWNER_VAR"] == "x"
    want = rendered_template(home)
    assert {k: data["env"][k] for k in want["env"]} == want["env"]
    assert data["statusLine"] == want["statusLine"]


@pytest.mark.parametrize("profile", ["superclaude", "deepseek"])
def test_no_secret_or_forbidden_keys_in_output(home: Path, profile: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("--profile", profile, "--dry-run", "--full") == 0
    out = capsys.readouterr().out
    for forbidden in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                      "CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_SUBAGENT_MODEL", "[1m]"):
        assert forbidden not in out


def test_hooks_block_byte_equal_to_v2_substitution(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("--profile", "superclaude", "--dry-run", "--full") == 0
    hooks = json.loads(capsys.readouterr().out)["hooks"]
    want = expected_hooks(home, home.parent / "local/Programs/Python/Python312/python.exe")
    assert json.dumps(hooks, indent=2) == json.dumps(want, indent=2)
    assert "_comment" not in hooks and "{{" not in json.dumps(hooks)


@pytest.mark.parametrize("profile", ["superclaude", "deepseek"])
def test_monitor_screened_by_hook_not_denied(home: Path, profile: str, capsys: pytest.CaptureFixture[str]) -> None:
    # Nessuna regola nativa ("Monitor", "Monitor(...)", "*") tocca Monitor e il matcher PreToolUse
    # lo include, quindi il firewall ne filtra i comandi; l'env che lo renderebbe indisponibile
    # in DeepSeek e' verificato da test_deepseek_env_keeps_monitor_and_powershell
    assert run("--profile", profile, "--dry-run", "--full") == 0
    printed = json.loads(capsys.readouterr().out)
    matchers = [g["matcher"].split("|") for g in printed["hooks"]["PreToolUse"]]
    assert any("Monitor" in m for m in matchers), matchers
    for key in ("allow", "deny", "ask"):
        rules = printed["permissions"].get(key, [])
        blanket = [r for r in rules if r in ("Monitor", "*") or r.startswith("Monitor(")]
        assert blanket == [], (key, blanket)


def test_deepseek_env_keeps_monitor_and_powershell(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # tools-reference.md § Monitor tool: Monitor non e' disponibile con DISABLE_TELEMETRY o
    # CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC impostati; DO_NOT_TRACK e DISABLE_GROWTHBOOK
    # spengono lo stesso fetch dei feature flag (env-vars.md)
    assert run("--profile", "deepseek", "--dry-run", "--full") == 0
    env = json.loads(capsys.readouterr().out)["env"]
    for var in ("DISABLE_TELEMETRY", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DO_NOT_TRACK", "DISABLE_GROWTHBOOK"):
        assert var not in env, var
    assert env["CLAUDE_CODE_USE_POWERSHELL_TOOL"] == "1"
    for var in ("DISABLE_AUTOUPDATER", "DISABLE_ERROR_REPORTING", "DISABLE_FEEDBACK_COMMAND",
                "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY"):
        assert env[var] == "1", var


def test_drop_stop_omits_stop(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("--profile", "superclaude", "--dry-run", "--full", "--drop-stop") == 0
    hooks = json.loads(capsys.readouterr().out)["hooks"]
    assert "Stop" not in hooks and "PreToolUse" in hooks


def test_unrecognised_hooks_need_force(home: Path) -> None:
    target = settings_file(home, "superclaude")
    foreign = dict(OWNER, hooks={"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "x.exe"}]}]})
    raw = write_owner(target, foreign)
    assert run("--profile", "superclaude") == 1
    assert target.read_bytes() == raw and backups(target) == []
    assert run("--profile", "superclaude", "--force") == 0
    assert "dispatch.py" in json.dumps(json.loads(target.read_text(encoding="utf-8"))["hooks"])


def test_missing_prerequisite_writes_nothing(home: Path) -> None:
    (home / ".claude" / "hooks" / "dispatch.py").unlink()
    target = settings_file(home, "superclaude")
    raw = write_owner(target, OWNER)
    assert run("--profile", "superclaude") == 1
    assert target.read_bytes() == raw


# --- B5: l'applicatore sostituisce l'insieme di voci che possiede ---

@pytest.fixture
def rules_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    path = tmp_path / "rules.json"
    monkeypatch.setattr(apply_settings, "RULES_FILE", path)
    # Isola il template superclaude reale: questi test controllano solo le regole condivise
    monkeypatch.setitem(apply_settings.TEMPLATES, "superclaude", tmp_path / "absent-superclaude.base.json")

    def write(deny: list[str], ask: list[str] | None = None) -> None:
        path.write_text(json.dumps({"_comment": "x", "deny": deny, "ask": ask or []}), encoding="utf-8")
    return write


@pytest.fixture
def sc_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    path = tmp_path / "superclaude.base.json"
    monkeypatch.setitem(apply_settings.TEMPLATES, "superclaude", path)

    def write(data: dict[str, Any]) -> None:
        path.write_text(json.dumps(data), encoding="utf-8")
    return write


def perms_of(home: Path) -> dict[str, Any]:
    return json.loads(settings_file(home, "superclaude").read_text(encoding="utf-8"))["permissions"]


def test_rules_added_changed_removed(home: Path, rules_file: Any) -> None:
    write_owner(settings_file(home, "superclaude"), OWNER)
    rules_file(["Bash(a *)", "Bash(b *)"], ["Bash(q *)"])
    assert run("--profile", "superclaude") == 0
    rules_file(["Bash(a *)", "Bash(c *)"], [])  # b cambiata in c, q rimossa
    assert run("--profile", "superclaude") == 0
    perms = perms_of(home)
    assert perms["deny"] == ["Bash(owner-deny *)", "Bash(a *)", "Bash(c *)"]
    assert perms["ask"] == OWNER["permissions"]["ask"]
    rules_file(["Bash(a *)", "Bash(c *)", "Bash(d *)"])
    assert run("--profile", "superclaude") == 0
    assert perms_of(home)["deny"] == ["Bash(owner-deny *)", "Bash(a *)", "Bash(c *)", "Bash(d *)"]


def test_owner_entry_survives_when_template_adds_then_drops_it(home: Path, rules_file: Any) -> None:
    target = settings_file(home, "superclaude")
    write_owner(target, OWNER)
    rules_file(["Bash(a *)"])
    assert run("--profile", "superclaude") == 0
    data = json.loads(target.read_text(encoding="utf-8"))
    data["permissions"]["deny"].append("Bash(mine *)")  # aggiunta a mano dopo l'applicazione
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    rules_file(["Bash(a *)", "Bash(mine *)"])
    assert run("--profile", "superclaude") == 0
    rules_file(["Bash(a *)"])
    assert run("--profile", "superclaude") == 0
    assert perms_of(home)["deny"] == ["Bash(owner-deny *)", "Bash(a *)", "Bash(mine *)"]


def test_first_run_without_sidecar_owns_template_equal_entries(home: Path, rules_file: Any) -> None:
    target = settings_file(home, "superclaude")
    write_owner(target, dict(OWNER, permissions={"deny": ["Bash(owner-deny *)", "Bash(old *)"]}))
    rules_file(["Bash(old *)"])
    assert run("--profile", "superclaude") == 0
    assert apply_settings.owned_path(target).exists()
    rules_file([])
    assert run("--profile", "superclaude") == 0
    assert perms_of(home)["deny"] == ["Bash(owner-deny *)"]


def test_template_scalars_retracted_unless_owner_changed_them(home: Path, rules_file: Any, sc_template: Any) -> None:
    target = settings_file(home, "superclaude")
    write_owner(target, {"model": "opus"})
    rules_file([])
    sc_template({"env": {"K1": "v", "K2": "w"}, "extra": {"a": 1}})
    assert run("--profile", "superclaude") == 0
    data = json.loads(target.read_text(encoding="utf-8"))
    data["env"]["K2"] = "mine"
    target.write_text(json.dumps(data), encoding="utf-8")
    sc_template({})
    assert run("--profile", "superclaude") == 0
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["env"] == {"K2": "mine"} and "extra" not in data and data["model"] == "opus"


def test_malformed_sidecar_writes_nothing(home: Path) -> None:
    target = settings_file(home, "superclaude")
    raw = write_owner(target, OWNER)
    apply_settings.owned_path(target).write_text('{"entries": [{"nopath": 1}]}', encoding="utf-8")
    assert run("--profile", "superclaude") == 1
    assert target.read_bytes() == raw and backups(target) == []


# --- B6: --revert solo sui backup dell'applicatore, anteprima e --yes ---

def test_revert_ignores_foreign_backups(home: Path) -> None:
    target = settings_file(home, "superclaude")
    raw = write_owner(target, OWNER)
    foreign = target.with_name("settings.json.bak-20261008-122914")
    foreign.write_text("{}", encoding="utf-8")
    assert run("--profile", "superclaude", "--revert", "--yes") == 1
    assert target.read_bytes() == raw
    assert run("--profile", "superclaude") == 0
    target.with_name("settings.json.bak-20991231-235959").write_text("{}", encoding="utf-8")  # estraneo piu' recente
    assert run("--profile", "superclaude", "--revert", "--yes") == 0
    assert target.read_bytes() == raw


def test_revert_preview_needs_yes(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = settings_file(home, "superclaude")
    write_owner(target, OWNER)
    assert run("--profile", "superclaude") == 0
    applied = target.read_bytes()
    capsys.readouterr()
    assert run("--profile", "superclaude", "--revert") == 0
    out = capsys.readouterr().out
    assert "nothing written" in out and "--yes" in out and "- hooks" in out
    assert target.read_bytes() == applied


def test_revert_backs_up_current_and_restores_sidecar(home: Path, rules_file: Any) -> None:
    target = settings_file(home, "superclaude")
    write_owner(target, OWNER)
    sidecar = apply_settings.owned_path(target)
    rules_file(["Bash(a *)", "Bash(b *)"])
    assert run("--profile", "superclaude") == 0
    first, first_owned = target.read_bytes(), sidecar.read_bytes()
    rules_file(["Bash(a *)"])
    assert run("--profile", "superclaude") == 0
    second = target.read_bytes()
    assert run("--profile", "superclaude", "--revert", "--yes") == 0
    assert target.read_bytes() == first and sidecar.read_bytes() == first_owned
    (pre,) = target.parent.glob("settings.json.pre-revert-*")
    assert pre.read_bytes() == second
    assert run("--profile", "superclaude", "--revert", "--yes") == 0  # ripetuto: stesso backup, idempotente
    assert target.read_bytes() == first and sidecar.read_bytes() == first_owned


def test_revert_to_backup_older_than_sidecar_removes_sidecar(home: Path) -> None:
    target = settings_file(home, "superclaude")
    raw = write_owner(target, OWNER)
    assert run("--profile", "superclaude") == 0
    sidecar = apply_settings.owned_path(target)
    assert sidecar.exists()
    assert run("--profile", "superclaude", "--revert", "--yes") == 0
    assert target.read_bytes() == raw and not sidecar.exists()


# --- B7: JSON malformato, scrittura atomica, riassunto, segnaposto ---

def test_malformed_target_is_clean_error(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = settings_file(home, "superclaude")
    target.write_bytes(b'{"model": "opus",')
    assert run("--profile", "superclaude") == 1
    assert "not valid JSON" in capsys.readouterr().err
    assert target.read_bytes() == b'{"model": "opus",' and backups(target) == []
    assert not apply_settings.owned_path(target).exists()


def test_failed_replace_leaves_original_and_no_temp(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = settings_file(home, "superclaude")
    raw = write_owner(target, OWNER)

    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")
    monkeypatch.setattr(apply_settings.os, "replace", boom)
    with pytest.raises(OSError):
        run("--profile", "superclaude")
    assert target.read_bytes() == raw
    assert list(target.parent.glob("*.tmp")) == []


def test_dry_run_prints_key_summary(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = settings_file(home, "superclaude")
    raw = write_owner(target, OWNER)
    assert run("--profile", "superclaude", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "nothing written" in out and "+ hooks" in out and "~ permissions.deny: +" in out
    assert "+ permissions.disableBypassPermissionsMode" in out
    assert "{" not in out.splitlines()[-1] and target.read_bytes() == raw


def test_dry_run_deepseek_prints_fake_home_paths(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("--profile", "deepseek", "--dry-run", "--full") == 0
    printed = json.loads(capsys.readouterr().out)
    ds = (home / ".claude-deepseek").as_posix()
    assert printed["apiKeyHelper"].startswith(f"{ds}/.venv/Scripts/python.exe ")
    assert printed["statusLine"]["command"].endswith(f"{ds}/bin/ds-statusline.py")


def test_hook_removal_names_matcher_and_command_args(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = settings_file(home, "superclaude")
    owner_hook = {"matcher": "Bash", "hooks": [{"type": "command", "command": "owner.exe", "args": ["--keep", "x"],
                                                 "env": {"SECRET": "s3cr3t"}}]}
    write_owner(target, dict(OWNER, hooks={"PreToolUse": [owner_hook], "Marker": [{"hooks": [
        {"type": "command", "command": "hooks/dispatch.py"}]}]}))
    assert run("--profile", "superclaude", "--dry-run", "--force") == 0
    out = capsys.readouterr().out
    assert "- hook matcher=Bash: owner.exe --keep x" in out
    assert "+ hook matcher=" in out and "dispatch.py" in out
    assert "s3cr3t" not in out


def test_first_run_reports_adopted_entries_dry_and_real(home: Path, rules_file: Any,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    target = settings_file(home, "superclaude")
    write_owner(target, dict(OWNER, permissions={"deny": ["Bash(owner-deny *)", "Bash(old *)"]}))
    rules_file(["Bash(old *)"])
    assert run("--profile", "superclaude", "--dry-run") == 0
    dry = capsys.readouterr().out
    assert "adopted 1 existing entries" in dry and "= permissions.deny: Bash(old *)" in dry
    assert run("--profile", "superclaude") == 0
    real = capsys.readouterr().out
    assert "written" in real and "adopted 1 existing entries" in real and "+ hooks" in real
    assert run("--profile", "superclaude", "--dry-run") == 0
    assert "existing entries" not in capsys.readouterr().out


def test_full_requires_dry_run(home: Path) -> None:
    with pytest.raises(SystemExit):
        run("--profile", "superclaude", "--full")
    with pytest.raises(SystemExit):
        run("--profile", "superclaude", "--yes")


def test_template_placeholders_rendered(home: Path, sc_template: Any, capsys: pytest.CaptureFixture[str]) -> None:
    sc_template({"permissions": {"deny": ["Read({{HOME}}/x/**)", "Edit({{DS_HOME}}/settings*.json)"]}})
    assert run("--profile", "superclaude", "--dry-run", "--full") == 0
    deny = json.loads(capsys.readouterr().out)["permissions"]["deny"]
    assert f"Read({home.as_posix()}/x/**)" in deny
    assert f"Edit({(home / 'DSDIR').as_posix()}/settings*.json)".replace("DSDIR", ".claude-" + "deepseek") in deny
    sc_template({"env": {"X": "{{NOPE}}"}})
    assert run("--profile", "superclaude", "--dry-run") == 1


def test_superclaude_template_merged_or_optional(home: Path, sc_template: Any, tmp_path: Path) -> None:
    target = settings_file(home, "superclaude")
    write_owner(target, OWNER)
    sc_template({"permissions": {"deny": ["Read(~/secret/**)"], "ask": ["Bash(sc-ask *)"]}})
    assert run("--profile", "superclaude") == 0
    perms = perms_of(home)
    assert "Read(~/secret/**)" in perms["deny"] and "Bash(sc-ask *)" in perms["ask"]
    (tmp_path / "superclaude.base.json").unlink()  # modello assente: trattato come {}
    assert run("--profile", "superclaude") == 0
    perms = perms_of(home)
    assert "Read(~/secret/**)" not in perms["deny"] and "Bash(sc-ask *)" not in perms["ask"]


# --- {{HOME_USER}}: il nome utente entra nelle regole solo al momento dell'applicazione ---

def test_home_user_rendered_from_home_name(home: Path, rules_file: Any, capsys: pytest.CaptureFixture[str]) -> None:
    rules_file(["Bash(rm -rf /c/Users/{{HOME_USER}})"], ["Bash(rm -r C:/Users/{{HOME_USER}}/*)"])
    assert run("--profile", "superclaude", "--dry-run", "--full") == 0
    perms = json.loads(capsys.readouterr().out)["permissions"]
    assert home.name == FAKE_HOME_NAME
    assert f"Bash(rm -rf /c/Users/{home.name})" in perms["deny"]
    assert f"Bash(rm -r C:/Users/{home.name}/*)" in perms["ask"]
    assert "{{" not in json.dumps(perms)
    rules_file(["Bash(rm -rf {{NOPE}})"])  # segnaposto sconosciuto nelle regole: rifiutato
    assert run("--profile", "superclaude", "--dry-run") == 1


@pytest.mark.parametrize("name", ["", "a*b", "a" + chr(92) + "b", "a/b", "a(b", "a)b"])
def test_home_user_with_matcher_characters_refused(name: str) -> None:
    with pytest.raises(apply_settings.ApplyError, match="home user name"):
        apply_settings.checked_home_user(name)


def test_home_user_plain_name_accepted() -> None:
    assert apply_settings.checked_home_user("first.last") == "first.last"


def test_home_user_refused_end_to_end(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad*name"
    monkeypatch.setenv("USERPROFILE", str(bad))
    monkeypatch.setenv("HOME", str(bad))
    before = sorted(tmp_path.rglob("*"))
    assert run("--profile", "superclaude") == 1
    assert "home user name unusable" in capsys.readouterr().err
    assert sorted(tmp_path.rglob("*")) == before
