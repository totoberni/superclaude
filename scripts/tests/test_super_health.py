"""Test per scripts/health/super_health.py (runner di /super-health).

Ogni caso costruisce una radice sintetica sotto tmp_path e la passa come root:
nessun test tocca la vera ~/.claude e nessuno esegue le suite di regressione
reali, così il runner può includere questo file in scripts/tests senza ricorsione.
JSON e sorgenti dei processi finti sono costruiti in Python, mai via shell.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / "health" / "super_health.py"


def _load():
    spec = importlib.util.spec_from_file_location("super_health", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["super_health"] = mod
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True   # niente __pycache__ accanto al runner
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = prev
    return mod


sh = _load()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def skill_md(name: str, desc: str = "Use when testing", extra: str = "") -> str:
    return f'---\nname: {name}\ndescription: "{desc}"\n{extra}---\n\n# {name}\n'


def agent_md(name: str, model: str = "sonnet", tools: str | None = "Read, Grep", extra: str = "") -> str:
    lines = ["---", f"name: {name}", f"model: {model}"]
    if tools:
        lines.append(f"tools: {tools}")
    return "\n".join(lines) + "\n" + extra + "---\n\nbody\n"


def pairs(findings) -> list[tuple[str, str]]:
    return sorted((f.check, f.path) for f in findings)


# --------------------------------------------------------------------------
# Frontmatter, pesi, aggregazione, tetti
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ('---\nname: a\ndescription: "Use when x"\n---\nbody\n', {"name": "a", "description": "Use when x"}),
    ("\ufeff---\r\nname: a\r\n---\r\nbody\r\n", {"name": "a"}),
    ("---\nname: a\n---", {"name": "a"}),
    ("no frontmatter\n", None),
    ("---\nname: [unclosed\n---\n", None),
    ("---\n- a\n- b\n---\n", None),
])
def test_parse_frontmatter(text, expected):
    assert sh.parse_frontmatter(text) == expected


def test_weights_sum_to_100_and_every_component_has_a_scorer():
    keys = {k for k, _, _ in sh.COMPONENTS}
    assert sum(w for _, _, w in sh.COMPONENTS) == 100
    assert keys == set(sh.TABLE_SCORERS) | set(sh.RATIO_SCORERS)
    assert set(sh.TABLE_SCORERS) == set(sh.PENALTIES)
    assert set(sh.RATIO_SCORERS) == set(sh.RATIO_RULES)


def _components(scores):
    return [sh.Component(k, label, w, s, "") for (k, label, w), s in zip(sh.COMPONENTS, scores)]


def test_aggregate_is_weighted_sum_rounded_half_up():
    # 95*20 + 88*15 + 97*15 + 100*15 + 100*20 + 98*15 = 9645 -> 96.45 -> 96
    assert sh.aggregate(_components((95, 88, 97, 100, 100, 98))) == (96, "A")
    # 100*20 + 90*15 + 100*15 + 100*15 + 100*20 + 100*15 = 9850 -> 98.5 -> 99
    assert sh.aggregate(_components((100, 90, 100, 100, 100, 100))) == (99, "A")


@pytest.mark.parametrize("uniform, grade", [
    (100, "A"), (95, "A"), (94, "B"), (85, "B"), (84, "C"), (70, "C"), (69, "D"), (50, "D"), (49, "F"), (0, "F"),
])
def test_grade_boundaries(uniform, grade):
    assert sh.aggregate(_components((uniform,) * len(sh.COMPONENTS))) == (uniform, grade)


def test_penalty_cap_bounds_each_check():
    per, cap, _ = sh.PENALTIES["drift"]["dropped_term"]
    findings = [sh.Finding("drift", "dropped_term", f"rules/x.md:{i}", "hcom") for i in range(cap // per + 5)]
    assert sh.apply_penalties("drift", findings) == 100 - cap
    assert sum(f.points for f in findings) == cap
    assert findings[-1].points == 0


# --------------------------------------------------------------------------
# Skills
# --------------------------------------------------------------------------
def test_synthetic_skills_tree_yields_expected_deductions(tmp_path):
    root = tmp_path
    listed = ["good", "badname", "nouse", "flip", "nofm", "git"]
    write(root / "skills" / "README.md", "\n".join(f"| `{n}` | x |" for n in listed) + "\n")
    write(root / "skills" / "good" / "SKILL.md", skill_md("good"))
    write(root / "skills" / "good" / "tool.py", "def f(:\n    pass\n")
    write(root / "skills" / "badname" / "SKILL.md", skill_md("other"))
    write(root / "skills" / "nouse" / "SKILL.md", skill_md("nouse", desc="Scores things"))
    write(root / "skills" / "flip" / "SKILL.md", skill_md("flip", extra="disable-model-invocation: true\n"))
    write(root / "skills" / "unlisted" / "SKILL.md", skill_md("unlisted"))
    write(root / "skills" / "nofm" / "SKILL.md", "# no frontmatter\n")
    gate = "## Unattended-context gate\n"
    write(root / "skills" / "git" / "SKILL.md", skill_md("git") + gate + "text\n" + gate)
    write(root / "skills" / "_shared" / "ok.md", "# ok\n\nConsumed by: /good.\n")
    write(root / "skills" / "_shared" / "bad.md", "# bad\n\nReusable by anyone.\n")

    findings, detail = sh.skills_findings(sh.build_context(root))

    assert pairs(findings) == [
        ("description", "skills/nouse/SKILL.md:3"),
        ("frontmatter", "skills/nofm/SKILL.md"),
        ("git_gate", "skills/git/SKILL.md"),
        ("model_invocation", "skills/flip/SKILL.md:4"),
        ("name", "skills/badname/SKILL.md:2"),
        ("py_compile", "skills/good/tool.py:1"),
        ("readme", "skills/README.md"),
        ("shared_consumers", "skills/_shared/bad.md"),
    ]
    assert "unlisted" in next(f.detail for f in findings if f.check == "readme")
    assert detail == "7 skills"
    assert sh.apply_penalties("skills", findings) == 100 - (5 + 10 + 10 + 5 + 5 + 10 + 3 + 3)


# --------------------------------------------------------------------------
# Settings + agents
# --------------------------------------------------------------------------
@pytest.mark.parametrize("model, ok", [
    ("sonnet", True), ("opus", True), ("haiku", True), ("inherit", True), ("opus[1m]", True),
    ("inherit[1m]", False), ("fable", False), ("claude-opus-4", False), (None, False),
])
def test_model_allowed(model, ok):
    assert sh.model_allowed(model) is ok


def test_synthetic_agents_tree_yields_expected_deductions(tmp_path):
    root = tmp_path
    write(root / "settings.json", json.dumps({"hooks": {}}))
    write(root / "agents" / "meta.md", agent_md("meta", model="opus[1m]"))
    write(root / "agents" / "w-good.md", agent_md("w-good"))
    write(root / "agents" / "w-fable.md", agent_md("w-fable", model="fable"))
    write(root / "agents" / "w-effort.md", agent_md("w-effort", extra="effort: extreme\n"))
    write(root / "agents" / "w-notools.md", agent_md("w-notools", tools=None))
    write(root / "agents" / "w-misnamed.md", agent_md("w-other"))
    write(root / "agents" / "w-unlisted.md", agent_md("w-unlisted"))
    write(root / "agents" / "_ephemeral" / "w-tmp.md", agent_md("w-tmp"))
    cited = "`w-good` `w-fable` `w-effort` `w-notools` `w-misnamed`"
    write(root / "rules" / "13-worker-first-mandate.md", f"# Mandate\nMeta+w-swarm is prose.\n{cited}\n`w-ghost`\n")
    write(root / "CLAUDE.md", "Workers: `w-good`\n")
    write(root / "bin" / "mem", "#!/bin/sh\n")

    findings, detail = sh.settings_findings(sh.build_context(root))

    assert pairs(findings) == sorted([
        ("agent_effort", "agents/w-effort.md:5"),
        ("agent_model", "agents/w-fable.md:3"),
        ("agent_name", "agents/w-misnamed.md:2"),
        ("agent_tools", "agents/w-notools.md"),
        ("bin_shim", "bin/scpy"),
        ("ephemeral_leftover", "agents/_ephemeral/w-tmp.md"),
        ("roster_missing", "rules/13-worker-first-mandate.md:4"),
        ("roster_unlisted", "agents/w-unlisted.md"),
        ("venv", sh.rel(root, sh.venv_python(root))),
    ])
    assert detail == "7 agents"
    assert sh.apply_penalties("settings", findings) == 100 - (5 * 6 + 2 + 10 + 20)


def test_invalid_settings_json_is_a_finding(tmp_path):
    write(tmp_path / "settings.json", "{not json")
    ctx = sh.build_context(tmp_path)
    assert ctx.settings is None
    findings, _ = sh.settings_findings(ctx)
    assert ("settings_json", "settings.json") in pairs(findings)


# --------------------------------------------------------------------------
# Hooks
# --------------------------------------------------------------------------
FAKE_DISPATCH = '''\
import json
import sys
from pathlib import Path

event = sys.argv[-1]
sys.stdin.read()
if event == "Stop":
    sys.exit(3)
if event == "PreCompact":
    log = Path.home() / ".claude" / "logs" / "hooks.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("2026-01-01T00:00:00 import/broken: ImportError('boom')\\n", encoding="utf-8")
if event in ("SessionStart", "SubagentStart"):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": "MEMORY"}}))
'''


def _group(hook: dict) -> list[dict]:
    return [{"matcher": "", "hooks": [hook]}]


def test_parse_hook_handles_exec_and_shell_form():
    exec_hook = {"command": "C:/Py/python.exe", "args": ["-S", "C:/x/dispatch.py", "Stop"]}
    shell_hook = {"command": '"C:\\Py\\python.exe" -S "C:\\x y\\dispatch.py" Stop'}
    assert sh.parse_hook(exec_hook) == ("C:/Py/python.exe", ["-S", "C:/x/dispatch.py", "Stop"])
    assert sh.parse_hook(shell_hook) == ("C:/Py/python.exe", ["-S", "C:/x y/dispatch.py", "Stop"])
    assert sh.parse_hook({"command": ""}) is None


def test_synthetic_hooks_tree_yields_expected_deductions(tmp_path):
    root = tmp_path
    dispatch = write(root / "hooks" / "dispatch.py", FAKE_DISPATCH)
    write(root / "hooks" / "sc_hooks" / "handlers" / "broken.py", "X = 1\n")
    write(root / "hooks" / "bad.py", "def f(:\n")
    cfg_events = ("SessionStart", "PreToolUse", "Stop", "PreCompact", "SubagentStop")
    placeholder = {"type": "command", "command": "{{PYTHON}}", "args": ["-S", "{{DISPATCH}}"]}
    write(root / "hooks" / "hooks.json", json.dumps({"_comment": "x", **{e: _group(placeholder) for e in cfg_events}}))
    py = sys.executable

    def shell(event: str, interp: str = py) -> dict:
        return {"type": "command", "command": f'"{interp}" -S "{dispatch}" {event}'}

    settings_hooks = {e: _group(shell(e)) for e in ("SessionStart", "Stop", "PreCompact")}
    settings_hooks["PreToolUse"] = _group({"type": "command", "command": py, "args": ["-S", str(dispatch), "PreToolUse"]})
    settings_hooks["SessionEnd"] = _group(shell("SessionEnd", interp="C:/nope/python.exe"))
    write(root / "settings.json", json.dumps({"hooks": settings_hooks}))

    findings, detail = sh.hooks_findings(sh.build_context(root))

    assert pairs(findings) == sorted([
        ("event_unregistered", "settings.json"),
        ("handler_error", "hooks/sc_hooks/handlers/broken.py"),
        ("hook_target", "settings.json"),
        ("py_compile", "hooks/bad.py:1"),
        ("shell_form", "settings.json"),
        ("smoke", "hooks/dispatch.py"),
    ])
    by_check = {f.check: f.detail for f in findings}
    assert "SubagentStop" in by_check["event_unregistered"]
    assert "SessionEnd" in by_check["hook_target"] and "interpreter not found" in by_check["hook_target"]
    assert by_check["smoke"].startswith("Stop: exit 3")
    assert detail == "5 events registered, 4 smoke-run in a sandbox HOME"
    assert sh.apply_penalties("hooks", findings) == 100 - (20 + 10 + 10 + 5 + 10 + 5)


def test_placeholder_interpreter_is_a_target_problem(tmp_path):
    problem = sh.target_problem(tmp_path, "Stop", "{{PYTHON}}", ["-S", "{{DISPATCH}}", "Stop"])
    assert problem.startswith("interpreter not found")


@pytest.mark.parametrize("event, out, bad", [
    ("PreToolUse", b"", False),
    ("PreToolUse", b"not json", True),
    ("SessionStart", b"", True),
    ("SubagentStart", b"{}", True),
    ("SessionStart", json.dumps({"hookSpecificOutput": {"additionalContext": "x"}}).encode(), False),
])
def test_stdout_problem(event, out, bad):
    assert (sh.stdout_problem(event, out) is not None) is bad


def test_smoke_payloads_are_valid_json_for_every_event(tmp_path):
    for event in sh.SMOKE_EXTRA:
        payload = json.loads(json.dumps(sh.smoke_payload(event, tmp_path)))
        assert payload["hook_event_name"] == event
        assert payload["session_id"] == sh.SMOKE_SESSION


def test_smoke_env_redirects_home_and_drops_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPACT_SNAPSHOT_DIR", "C:/real")
    monkeypatch.setenv("SUPERCLAUDE_GUARDS", "off")
    env = sh.smoke_env(tmp_path)
    assert env["USERPROFILE"] == env["HOME"] == str(tmp_path)
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "COMPACT_SNAPSHOT_DIR" not in env and "SUPERCLAUDE_GUARDS" not in env


# --------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------
MEM_SAMPLE = """## Memory Health Report (mode: normal)

| # | Criterion | Score | Detail |
|---|-----------|-------|--------|
| 1 | Right-sized rows | 18/20 | 60/68 within 8000B; exempt: 0 |
| 2 | FTS index cohesion | 20/20 | aligned |
| 6 | Staleness managed | N/A | edit-span 4.1d < 60d |

Scored 88/90 pts (renormalized to /100; 10 pts N/A excluded; capped at 97 - incomplete assessment).
SCORE: 97/100
"""


def test_parse_mem_health():
    score, summary = sh.parse_mem_health(MEM_SAMPLE)
    assert score == 97
    assert "Scored 88/90 pts" in summary
    assert "#1 Right-sized rows 18/20" in summary
    assert "#6 Staleness managed N/A" in summary
    assert "FTS" not in summary
    assert sh.parse_mem_health("garbage")[0] is None


def test_memory_component_uses_final_score(tmp_path):
    write(tmp_path / "scripts" / "memory" / "mem_health.py", "print('SCORE: 10/100')\nprint('SCORE: 80/100')\n")
    score, _, findings = sh.memory_component(sh.build_context(tmp_path))
    assert score == 80
    assert [(f.check, f.path, f.points) for f in findings] == [("mem_health", "agent-memory/.memory.db", 20)]


def test_memory_component_missing_script_scores_zero(tmp_path):
    score, _, findings = sh.memory_component(sh.build_context(tmp_path))
    assert score == 0
    assert findings[0].path == "scripts/memory/mem_health.py"


# --------------------------------------------------------------------------
# Regression tests
# --------------------------------------------------------------------------
JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="4">
<testcase classname="test_a" name="test_ok" file="test_a.py" line="0"/>
<testcase classname="test_a" name="test_bad" file="test_a.py" line="4"><failure message="x"/></testcase>
<testcase classname="test_a" name="test_err" file="test_a.py" line="9"><error message="y"/></testcase>
<testcase classname="test_a" name="test_skip" file="test_a.py" line="12"><skipped/></testcase>
</testsuite></testsuites>
"""


def test_parse_junit_counts_and_locates_failures(tmp_path):
    write(tmp_path / "suite" / "test_a.py", "\n")
    xml_path = write(tmp_path / "junit.xml", JUNIT)
    res = sh.parse_junit(tmp_path, "suite", xml_path)
    assert (res.passed, res.failed, res.errors) == (1, 1, 1)
    assert [(f.path, f.points) for f in res.findings] == [("suite/test_a.py:5", None), ("suite/test_a.py:10", None)]


def test_regression_runs_real_pytest_and_counts_broken_suite_whole(tmp_path, monkeypatch):
    root = tmp_path
    write(root / "pkg" / "tests" / "test_x.py", "def test_ok():\n    assert True\n\n\ndef test_fail():\n    assert 1 == 2\n")
    write(root / "broken" / "tests" / "test_y.py", "def test_a(:\n    pass\n\ndef test_b():\n    pass\n")
    monkeypatch.setattr(sh, "DEFAULT_SUITES", ("pkg/tests", "broken/tests"))
    ctx = sh.build_context(root)

    score, detail, findings = sh.regression_component(ctx)

    # 1 passato; 1 fallito in pkg; broken non raccoglie: conta i suoi 2 test come falliti
    assert detail == "1 passed, 3 failed, 0 errors in 2 suites"
    assert score == 1 * 100 // 4
    assert ("test", "pkg/tests/test_x.py:5") in pairs(findings)
    assert ("suite", "broken/tests") in pairs(findings)
    assert not list(root.rglob("__pycache__"))


def test_missing_suite_directory_counts_as_failing(tmp_path):
    res = sh.run_suite(sh.build_context(tmp_path), "nope/tests")
    assert (res.passed, res.failed) == (0, 1)
    assert "suite directory missing" in res.findings[0].detail


# --------------------------------------------------------------------------
# Drift and hygiene
# --------------------------------------------------------------------------
CONVENTIONS = "# C\n\nDeleted skills (remove from any list): handoff, rb, super-health, wsl-gotchas.\n"
RULE_LINES = [
    "Owner is totoberni and peers review.",
    "Messages went through hcom-broker earlier.",
    "See skills/handoff/SKILL.md and https://x.io/rb for history.",
    "Run `/handoff` then /rb.",
    "Mounts under /mnt/c/Users are gone.",
    "Use /super-health for the score.",
    "Try /handoffs or /rb-x.",
]


def test_drift_sweep_flags_only_distinctive_terms(tmp_path):
    root = tmp_path
    write(root / sh.CONVENTIONS_REL, CONVENTIONS)
    write(root / "rules" / "x.md", "\n".join(RULE_LINES) + "\n")
    write(root / "CLAUDE.md", "Old infra-test.sh runner.\n")

    findings, detail = sh.drift_findings(sh.build_context(root))

    assert pairs(findings) == [
        ("deleted_slash", "rules/x.md:4"),
        ("dropped_term", "CLAUDE.md:1"),
        ("dropped_term", "rules/x.md:2"),
        ("dropped_term", "rules/x.md:5"),
    ]
    assert next(f.detail for f in findings if f.check == "deleted_slash") == "/handoff, /rb"
    assert detail == "2 files swept"
    assert sh.apply_penalties("drift", findings) == 92


def test_drift_without_conventions_skips_slash_sweep(tmp_path):
    write(tmp_path / "rules" / "x.md", "Run /handoff now.\n")
    findings, detail = sh.drift_findings(sh.build_context(tmp_path))
    assert findings == []
    assert "slash sweep skipped" in detail


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def test_main_on_empty_root_scores_deterministically(tmp_path, capsys):
    assert sh.main(["--root", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    # hooks 60 (hooks.json), skills 90 (git gate), memory 0, settings 10, regression 0, drift 100
    assert lines[-1] == "SCORE: 42/100"
    assert lines[-2] == "Grade: F"

    assert sh.main(["--root", str(tmp_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["score"], report["grade"]) == (42, "F")
    assert [c["score"] for c in report["components"]] == [60, 90, 0, 10, 0, 100]
    assert all(re.fullmatch(r"[a-z_]+", f["check"]) for f in report["findings"])


def test_main_returns_2_on_internal_crash(tmp_path, monkeypatch, capsys):
    def boom(root):
        raise RuntimeError("boom")

    monkeypatch.setattr(sh, "run", boom)
    assert sh.main(["--root", str(tmp_path)]) == 2
    assert "RuntimeError: boom" in capsys.readouterr().err
