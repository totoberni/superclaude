"""Test del profilo attivo (SC_PROFILE): data home, fail closed, percorsi per profilo, Result.ask.

I controlli sulle costanti di modulo girano in un sottoprocesso con una HOME finta:
le costanti si calcolano all'import, e ricaricare `core` nel processo dei test
romperebbe il registro degli handler.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from sc_hooks import core
from sc_hooks.handlers import git_policy, write_acl

HOOKS_DIR = Path(__file__).resolve().parents[1]
DISPATCH = HOOKS_DIR / "dispatch.py"

# Costanti migrate -> percorso relativo a ~/.claude come era PRIMA della modifica
LEGACY_REL = {
    "core.CLAUDE_HOME": "",
    "core.CODE_HOME": "",
    "core.DATA_HOME": "",
    "core.STATE_DIR": "state",
    "core.LOG_DIR": "logs",
    "core.TMP_DIR": "tmp",
    "baseline_stash.CONFIG_FILE": "hooks/no-commit-projects.local",
    "context_check.CONTEXT_FILE": ".context-latest.json",
    "git_policy.policy_file()": "config/git-policy",
    "latex_warn.CACHE_DIR": ".latex-warn-cache",
    "memory_inject.DB_PATH": "agent-memory/.memory.db",
    "session_cleanup.HISTORY_LOG": "logs/session-history.log",
    "session_cleanup.HISTORY_JSONL": "history.jsonl",
    "session_cleanup.HISTORY_BACKUP_DIR": "_backups/history",
    "session_cleanup.MEMORY_DB": "agent-memory/.memory.db",
    "session_cleanup.MEMORY_BACKUP_DIR": "_backups/memory",
    "snapshots.SYSTEM_DIR": "agent-memory/_system",
    "snapshots.COMPACT_DIR": "agent-memory/_system/_compact-snapshots",
    "snapshots.STOP_DIR": "agent-memory/_system/_stop-snapshots",
    "snapshots.PLANS_DIR": "plans",
    "snapshots.SPAWN_LOG": "comms/_spawns.log",
    "snapshots.EPHEMERAL_DIR": "agents/_ephemeral",
    "spawn_log.COMMS_DIR": "comms",
}
# Restano nel codice condiviso anche con il profilo deepseek
CODE_CONSTANTS = {"core.CLAUDE_HOME", "core.CODE_HOME", "baseline_stash.CONFIG_FILE", "snapshots.EPHEMERAL_DIR"}

_PROBE = """
import json, sys
sys.path.insert(0, sys.argv[1])
from sc_hooks import core
from sc_hooks.handlers import (baseline_stash, context_check, git_policy, latex_warn, memory_inject,
                               session_cleanup, snapshots, spawn_log)
names = json.loads(sys.argv[2])
out = {n: str(eval(n)) for n in names}
out["WORKSPACE_ROOT"] = str(core.WORKSPACE_ROOT)
out["POINTER"] = memory_inject.POINTER
print(json.dumps(out))
"""


def _env(fake_home: Path, profile: str | None, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("SC_PROFILE", "SUPERCLAUDE_GIT_POLICY_FILE", "CLAUDE_AGENT_NAME", "SUPERCLAUDE_GUARDS")}
    env.update(USERPROFILE=str(fake_home), HOME=str(fake_home), PYTHONIOENCODING="utf-8")
    if profile is not None:
        env["SC_PROFILE"] = profile
    env.update(extra)
    return env


def _probe(fake_home: Path, profile: str | None) -> dict[str, str]:
    proc = subprocess.run([sys.executable, "-S", "-c", _PROBE, str(HOOKS_DIR), json.dumps(list(LEGACY_REL))],
                          env=_env(fake_home, profile), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _dispatch(fake_home: Path, profile: str | None, payload: dict, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-S", str(DISPATCH), payload["hook_event_name"]],
                          input=json.dumps(payload), env=_env(fake_home, profile, **extra),
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


# --------------------------------------------------------------------------
# Selezione della data home
# --------------------------------------------------------------------------
def test_resolve_profile_unset(home):
    assert core.resolve_profile({}) == ("superclaude", home / ".claude", "")
    assert core.resolve_profile({"SC_PROFILE": "  "}) == ("superclaude", home / ".claude", "")


def test_resolve_profile_deepseek(home):
    assert core.resolve_profile({"SC_PROFILE": "deepseek"}) == ("deepseek", home / ".claude-deepseek", "")
    assert core.resolve_profile({"SC_PROFILE": "DeepSeek"})[1] == home / ".claude-deepseek"


def test_resolve_profile_invalid_has_no_fallback(home):
    name, data, err = core.resolve_profile({"SC_PROFILE": "bogus"})
    assert (name, data) == ("", None)
    assert "bogus" in err


def test_unset_profile_paths_are_byte_identical_to_legacy(tmp_path):
    fh = tmp_path / "fh"
    fh.mkdir()
    got = _probe(fh, None)
    legacy_home = Path(str(fh)) / ".claude"
    for name, rel in LEGACY_REL.items():
        expected = str(legacy_home / rel) if rel else str(legacy_home)
        assert got[name] == expected, name
    assert got["WORKSPACE_ROOT"] == str(Path(str(fh)) / "code")
    assert "~/.claude/agent-memory/.memory.db" in got["POINTER"]


def test_deepseek_profile_moves_only_data(tmp_path):
    fh = tmp_path / "fh"
    fh.mkdir()
    got = _probe(fh, "deepseek")
    for name, rel in LEGACY_REL.items():
        root = Path(str(fh)) / (".claude" if name in CODE_CONSTANTS else ".claude-deepseek")
        assert got[name] == (str(root / rel) if rel else str(root)), name
    assert got["WORKSPACE_ROOT"] == str(Path(str(fh)) / "code")
    assert "~/.claude-deepseek/agent-memory/.memory.db" in got["POINTER"]
    assert "~/.claude/agent-memory" not in got["POINTER"]


# --------------------------------------------------------------------------
# Fail closed con SC_PROFILE ignoto
# --------------------------------------------------------------------------
def test_invalid_profile_denies_pretooluse_end_to_end(tmp_path):
    fh = tmp_path / "fh"
    fh.mkdir()
    payload = {"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": str(fh),
               "tool_name": "Bash", "tool_input": {"command": "echo hi"}}
    proc = _dispatch(fh, "bogus", payload)
    assert proc.returncode == 2
    assert proc.stderr.startswith("GUARD-BLOCK [profile] SC_PROFILE='bogus'")
    assert proc.stdout == ""
    assert list(fh.iterdir()) == []      # nessun dato scritto da nessuna parte


@pytest.mark.parametrize("event", ["SessionStart", "SubagentStart", "PostToolUse", "Stop", "SessionEnd"])
def test_invalid_profile_other_events_do_nothing(tmp_path, event):
    fh = tmp_path / "fh"
    fh.mkdir()
    payload = {"hook_event_name": event, "session_id": "s1", "cwd": str(fh), "tool_name": "Bash",
               "tool_input": {"command": "echo hi"}}
    proc = _dispatch(fh, "bogus", payload)
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    assert list(fh.iterdir()) == []


def test_profile_error_denies_in_process(home, monkeypatch, mk, capsys):
    monkeypatch.setenv("SC_PROFILE", "bogus")
    monkeypatch.setattr(core, "PROFILE_ERROR", "unknown SC_PROFILE='bogus'")
    assert core.dispatch(mk("Write", file_path=str(home / "code" / "proj" / "a.txt"), content="x")) == 2
    out = capsys.readouterr()
    assert "SC_PROFILE='bogus'" in out.err and out.out == ""
    assert core.dispatch(mk("", event="SessionStart")) == 0
    out = capsys.readouterr()
    assert (out.out, out.err) == ("", "")


# --------------------------------------------------------------------------
# memory-inject: DB del profilo deepseek, DB mancante
# --------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE memories (id INTEGER PRIMARY KEY, path TEXT UNIQUE, tier TEXT, agent TEXT, type TEXT,
  name TEXT, description TEXT, text TEXT NOT NULL, html TEXT, created TEXT, updated TEXT, hash TEXT);
CREATE VIRTUAL TABLE memories_fts USING fts5(name, description, text, content='memories', content_rowid='id');
CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, name, description, text) VALUES (new.id, new.name, new.description, new.text);
END;
"""


def _make_db(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO memories(path, tier, type, name, description, text) VALUES (?,?,?,?,?,?)",
                 (f"instance/meta/{name}.md", "instance/meta", "note", name, "",
                  "meta recovery context current state handoff"))
    conn.commit()
    conn.close()


def _session_start(fh: Path, profile: str | None) -> str:
    payload = {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": str(fh), "source": "startup"}
    proc = _dispatch(fh, profile, payload, CLAUDE_AGENT_NAME="meta")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def test_memory_inject_reads_deepseek_db_only(tmp_path):
    fh = tmp_path / "fh"
    _make_db(fh / ".claude-deepseek" / "agent-memory" / ".memory.db", "deepseek-handoff")
    _make_db(fh / ".claude" / "agent-memory" / ".memory.db", "superclaude-handoff")
    text = _session_start(fh, "deepseek")
    assert "deepseek-handoff" in text and "superclaude-handoff" not in text
    assert "~/.claude-deepseek/agent-memory/.memory.db" in text
    # Controprova: senza profilo si legge il DB di superclaude
    text = _session_start(fh, None)
    assert "superclaude-handoff" in text and "deepseek-handoff" not in text


def test_memory_inject_missing_deepseek_db_injects_pointer_only(tmp_path):
    fh = tmp_path / "fh"
    fh.mkdir()
    text = _session_start(fh, "deepseek")
    assert text.startswith("MEMORY (read this first)")
    assert "Most relevant stored memories" not in text
    assert not (fh / ".claude-deepseek" / "agent-memory" / ".memory.db").exists()
    assert not (fh / ".claude").exists()     # nessun dato finisce nella data home di superclaude


# --------------------------------------------------------------------------
# git-policy e write-acl con la data home deepseek
# --------------------------------------------------------------------------
@pytest.fixture
def deepseek_home(home, monkeypatch):
    ds = home / ".claude-deepseek"
    (ds / "config").mkdir(parents=True)
    monkeypatch.setattr(core, "DATA_HOME", ds)
    return ds


def _blocked(res) -> bool:
    return res is not None and bool(res.block)


def test_git_policy_reads_deepseek_flag(home, deepseek_home, mk):
    (home / ".claude" / "config" / "git-policy").write_text("enabled", encoding="utf-8")
    (deepseek_home / "config" / "git-policy").write_text("disabled", encoding="utf-8")
    assert git_policy.policy_file() == deepseek_home / "config" / "git-policy"
    assert _blocked(git_policy.handle(mk("Bash", command="git commit -m x")))


def test_git_policy_ignores_superclaude_flag_under_deepseek(home, deepseek_home, mk):
    (home / ".claude" / "config" / "git-policy").write_text("disabled", encoding="utf-8")
    assert git_policy.handle(mk("Bash", command="git commit -m x")) is None


def test_git_policy_env_override_still_wins(home, deepseek_home, mk, monkeypatch, tmp_path):
    (deepseek_home / "config" / "git-policy").write_text("enabled", encoding="utf-8")
    override = tmp_path / "flag"
    override.write_text("disabled", encoding="utf-8")
    monkeypatch.setenv("SUPERCLAUDE_GIT_POLICY_FILE", str(override))
    assert _blocked(git_policy.handle(mk("Bash", command="git push")))


def test_worker_cannot_write_deepseek_flag_via_shell(home, deepseek_home, mk):
    flag = deepseek_home / "config" / "git-policy"
    res = git_policy.handle(mk("PowerShell", role="worker", command=f"Set-Content '{flag}' enabled"))
    assert _blocked(res)


@pytest.mark.parametrize("leaf", ["plan.md", "context.md"])
def test_write_acl_plans_under_deepseek_data_home(home, deepseek_home, mk, leaf):
    target = deepseek_home / "plans" / "x" / leaf
    res = write_acl.handle(mk("Write", role="worker", file_path=str(target), content="x"))
    assert _blocked(res) and "~/.claude-deepseek/plans is meta-only" in res.block
    assert write_acl.handle(mk("Write", role="meta", file_path=str(target), content="x")) is None


def test_write_acl_flag_under_deepseek_data_home(home, deepseek_home, mk):
    res = write_acl.handle(mk("Edit", role="worker", file_path=str(deepseek_home / "config" / "git-policy"),
                              old_string="a", new_string="b"))
    assert _blocked(res) and "git policy flag" in res.block


def test_write_acl_code_rules_stay_on_code_home(home, deepseek_home, mk):
    for rel in ("hooks/x.py", "rules/x.md", "agents/w-x.md"):
        res = write_acl.handle(mk("Write", role="worker", file_path=str(home / ".claude" / rel), content="x"))
        assert _blocked(res), rel
    res = write_acl.handle(mk("Write", role="meta", file_path=str(home / ".claude" / "settings.json"), content="{}"))
    assert _blocked(res) and "owner-run-only" in res.block
    ok = write_acl.handle(mk("Write", role="worker", file_path=str(home / ".claude" / "agents" / "_ephemeral" / "w.md"),
                             content="x"))
    assert ok is None


@pytest.mark.parametrize("root", [".claude", ".claude-deepseek"])
@pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
def test_write_acl_settings_owner_only_with_deepseek_profile(home, deepseek_home, mk, monkeypatch, root, name):
    # Con il profilo deepseek attivo restano protetti sia i settings di ~/.claude sia i propri
    monkeypatch.setattr(core, "PROFILE", "deepseek")
    res = write_acl.handle(mk("Write", role="meta", file_path=str(home / root / name), content="{}"))
    assert _blocked(res) and f"~/{root}/{name} is owner-run-only" in res.block


# --------------------------------------------------------------------------
# Result.ask
# --------------------------------------------------------------------------
@pytest.fixture
def registry(monkeypatch):
    reg: list = []
    monkeypatch.setattr(core, "_REGISTRY", reg)
    monkeypatch.setattr(core, "_DEFAULT_MODE", {})
    monkeypatch.delenv("SUPERCLAUDE_GUARDS", raising=False)

    def add(name, fn, event="PreToolUse"):
        reg.append((event, (), name, fn))
    return add


def _json_out(capsys) -> dict:
    out = capsys.readouterr()
    assert out.err == ""
    return json.loads(out.out)["hookSpecificOutput"]


def test_result_ask_fields():
    res = core.Result.ask("why")
    assert (res.ask_reason, res.block, res.updated_input, res.context) == ("why", None, None, [])


def test_ask_emits_permission_decision(registry, mk, capsys):
    registry("t-ask", lambda ctx: core.Result.ask("confirm this"))
    assert core.dispatch(mk("Bash", command="ls")) == 0
    assert _json_out(capsys) == {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                 "permissionDecisionReason": "GUARD-ASK [t-ask] confirm this"}


def test_ask_keeps_updated_input_and_context(registry, mk, capsys):
    registry("t-fix", lambda ctx: core.Result.rewrite({"command": "ls -la"}, "fixed"))
    registry("t-ask", lambda ctx: core.Result.ask("one"))
    registry("t-ask2", lambda ctx: core.Result.ask("two"))
    registry("t-note", lambda ctx: core.Result.note("fyi"))
    assert core.dispatch(mk("Bash", command="ls")) == 0
    out = _json_out(capsys)
    assert out["permissionDecision"] == "ask"
    assert out["permissionDecisionReason"] == "GUARD-ASK [t-ask] one\n[t-ask2] two"
    assert out["updatedInput"] == {"command": "ls -la"}
    assert out["additionalContext"] == "fixed\n\nfyi"


def test_block_wins_over_ask(registry, mk, capsys):
    registry("t-ask", lambda ctx: core.Result.ask("confirm"))
    registry("t-deny", lambda ctx: core.Result.deny("no"))
    assert core.dispatch(mk("Bash", command="ls")) == 2
    out = capsys.readouterr()
    assert out.out == "" and out.err.startswith("GUARD-BLOCK [t-deny] no")


def test_ask_in_warn_mode_is_a_note(registry, mk, capsys, monkeypatch):
    monkeypatch.setenv("SUPERCLAUDE_GUARD_T_ASK", "warn")
    registry("t-ask", lambda ctx: core.Result.ask("confirm"))
    assert core.dispatch(mk("Bash", command="ls")) == 0
    out = _json_out(capsys)
    assert "permissionDecision" not in out
    assert out["additionalContext"] == "GUARD-WARN [t-ask]: confirm"


def test_ask_outside_pretooluse_is_a_note(registry, mk, capsys):
    registry("t-ask", lambda ctx: core.Result.ask("confirm"), event="PostToolUse")
    assert core.dispatch(mk("Bash", event="PostToolUse", command="ls")) == 0
    out = _json_out(capsys)
    assert "permissionDecision" not in out
    assert out["additionalContext"] == "GUARD-WARN [t-ask]: confirm"


def test_no_ask_keeps_legacy_output(registry, mk, capsys):
    registry("t-note", lambda ctx: core.Result.note("fyi"))
    assert core.dispatch(mk("Bash", command="ls")) == 0
    assert _json_out(capsys) == {"hookEventName": "PreToolUse", "additionalContext": "fyi"}
