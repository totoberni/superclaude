"""Runner deterministico di /super-health: salute di superclaude in /100.

Usage: scpy health/super_health.py [--json] [--root <dir>]
  --json : stampa gli stessi dati in JSON invece del report markdown
  --root : radice dell'installazione (default ~/.claude); i test usano una radice sintetica

Contratto: tabella dei componenti, elenco dei rilievi (ogni deduzione con un
percorso concreto, `file:riga` dove ha senso), voto e, come ULTIMA riga del
report markdown, esattamente "SCORE: <int>/100". Exit 0 ogni volta che il runner
ha girato (il punteggio è un dato, non un exit code); exit 2 solo per un crash
interno.

Sola lettura sull'installazione: nessuna scrittura fuori dalle cartelle
temporanee del sistema operativo e nessun __pycache__ nel repo (compilazione in
memoria, PYTHONDONTWRITEBYTECODE per ogni processo figlio, git con
--no-optional-locks). Lo smoke test di dispatch.py gira con USERPROFILE e HOME
puntati a una HOME finta temporanea: gli handler che scrivono stato, log, backup
e snapshot scrivono lì e non nella vera ~/.claude.

Questo file è la fonte di verità di pesi, controlli e penalità;
skills/super-health/SKILL.md li documenta.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import traceback
import warnings
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

# sc_profile vive in <code home>/lib: il percorso si ricava da questo file, cosi'
# funziona sia dall'albero live sia da una copia di staging
_LIB_DIR = str(Path(__file__).resolve().parents[2] / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
import sc_profile  # noqa: E402

# --------------------------------------------------------------------------
# Dati: pesi, voti, penalità (fonte di verità)
# --------------------------------------------------------------------------
COMPONENTS = (
    ("hooks", "Hooks", 20),
    ("skills", "Skills", 15),
    ("memory", "Memory", 15),
    ("settings", "Settings + agents", 15),
    ("regression", "Regression tests", 20),
    ("drift", "Drift and hygiene", 15),
)

GRADES = ((95, "A"), (85, "B"), (70, "C"), (50, "D"), (0, "F"))

# Componente -> controllo -> (punti per occorrenza, tetto, descrizione).
# Ogni componente parte da 100 e perde punti; il tetto impedisce a una sola
# categoria rumorosa di azzerare il componente.
PENALTIES: dict[str, dict[str, tuple[int, int, str]]] = {
    "hooks": {
        "py_compile": (20, 60, "a hooks/**/*.py file does not compile"),
        "hooks_json": (40, 40, "hooks/hooks.json is missing or is not a valid JSON object"),
        "event_unregistered": (10, 40, "an event of hooks/hooks.json has no entry in settings.json"),
        "hook_target": (10, 40, "a settings.json hook entry names a missing interpreter, a missing or foreign dispatch.py, or the wrong event"),
        "smoke": (10, 40, "sandboxed dispatch.py smoke run failed: exit != 0, timeout, non-JSON stdout, or no memory injection on SessionStart/SubagentStart"),
        "handler_error": (5, 20, "a handler raised during the sandboxed smoke run (fail-open, logged to hooks.log)"),
        "shell_form": (5, 5, "settings.json hooks are shell-form while hooks/hooks.json is exec-form (bin/apply-settings.ps1 -Profile superclaude not applied)"),
    },
    "skills": {
        "frontmatter": (10, 40, "skills/*/SKILL.md has no parseable YAML frontmatter"),
        "name": (5, 20, "frontmatter name differs from the skill directory"),
        "description": (5, 20, "description missing or not starting with 'Use when'"),
        "model_invocation": (5, 20, "disable-model-invocation: true"),
        "readme": (3, 15, "skill directory not listed in skills/README.md"),
        "git_gate": (10, 10, "skills/git/SKILL.md lacks exactly one '## Unattended-context gate' heading"),
        "shared_consumers": (3, 15, "a skills/_shared/*.md block has no 'Consumed by:' line"),
        "py_compile": (10, 30, "a .py file under skills/ does not compile"),
    },
    "settings": {
        "settings_json": (50, 50, "settings.json is missing or is not a valid JSON object"),
        "agent_frontmatter": (10, 40, "agents/*.md has no parseable YAML frontmatter"),
        "agent_name": (5, 20, "agent frontmatter name differs from the file stem"),
        "agent_model": (5, 20, "agent model not in ALLOWED_MODELS (optionally with an ALLOWED_MODEL_SUFFIXES suffix)"),
        "agent_effort": (5, 20, "agent effort present but not in ALLOWED_EFFORTS"),
        "agent_tools": (5, 20, "agent frontmatter has no tools field"),
        "roster_missing": (5, 20, "a w-* agent named in rules/13 or CLAUDE.md has no agents/<name>.md"),
        "roster_unlisted": (5, 20, "an agents/w-*.md is not named in rules/13-worker-first-mandate.md"),
        "ephemeral_leftover": (2, 10, "leftover .md file in agents/_ephemeral/"),
        "bin_shim": (10, 20, "bin/mem or bin/scpy is missing"),
        "venv": (20, 20, "the superclaude venv interpreter is missing"),
    },
    "drift": {
        "dropped_term": (2, 50, "a line names a dropped subsystem (DROPPED_TERMS)"),
        "deleted_slash": (2, 50, "a line cross-references a deleted skill as /<name> (CONVENTIONS 'Deleted skills')"),
    },
}

# Componenti a rapporto: non usano PENALTIES.
RATIO_RULES = {
    "memory": "score = final 'SCORE: N/100' of scripts/memory/mem_health.py (renormalised and capped by it); 0 if missing or unparseable",
    "regression": "score = floor(passed * 100 / (passed + failed + errors)); skipped tests excluded; a suite that cannot run (missing dir, pytest exit other than 0/1, timeout) counts all its statically counted tests as failed",
}

ALLOWED_MODELS = ("haiku", "sonnet", "opus", "inherit")
# `opus[1m]` è l'alias con contesto da 1M token, valido in Claude Code
ALLOWED_MODEL_SUFFIXES = ("[1m]",)
ALLOWED_EFFORTS = ("low", "medium", "high", "xhigh", "max")

DEFAULT_SUITES = ("hooks/tests", "scripts/tests", "skills/notebook/tests")
DRIFT_SCOPE = ("rules", "agents", "skills", "CLAUDE.md")
CONVENTIONS_REL = "docs/win11-migration/CONVENTIONS.md"

# Termini distintivi dei sottosistemi eliminati (CONVENTIONS §1). Solo termini
# che non possono comparire per caso: niente `toto` (totoberni), `peer`,
# `dual-write` (scrittura doppia sul tier class), `scaffolder` (prosa di notebook).
# I comandi slash delle skill eliminate li conta solo `deleted_slash`.
DROPPED_TERMS = (
    "hcom", ".broker.db", ".comms.db", "comms_db.py", "comms_viewer.py",
    "session-timers", "/mnt/c", "tailscale", "ntfy", "tsudo", "crawl4ai",
    "firecrawl", "seal-manifest", "instrument-tripwire", "converge_auto.py",
    "claude_mem_sync.py", "infra-test.sh", "super-health.sh", "hook-health.sh",
    "skill-health.sh", "test-hooks.sh", "meta-registry.md",
)

SMOKE_SESSION = "super-health-smoke"
SMOKE_TIMEOUT_S = 10
SUITE_TIMEOUT_S = 900
MEM_TIMEOUT_S = 300
GIT_TIMEOUT_S = 30
CONTEXT_EVENTS = ("SessionStart", "SubagentStart")
# Variabili che deviano scritture o spengono guardie: fuori dallo smoke
SMOKE_ENV_DROP = (
    "CLAUDE_AGENT_NAME", "CLAUDE_CODE_SESSION_ID", "CLAUDE_COMMIT_POLICY",
    "COMPACT_SNAPSHOT_DIR", "SUPERCLAUDE_GIT_POLICY_FILE",
)
SMOKE_EXTRA: dict[str, dict] = {
    "SessionStart": {"source": "startup"},
    "SubagentStart": {"agent_type": "w-explorer", "agent_id": "smoke"},
    "PreToolUse": {"tool_name": "Bash", "tool_input": {"command": "echo super-health-smoke"}},
    "PostToolUse": {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "w-explorer", "description": "smoke", "prompt": "smoke"},
        "tool_response": {"content": [{"type": "text", "text": "STATUS: DONE"}]},
    },
    "SubagentStop": {"agent_type": "w-explorer", "agent_id": "smoke", "stop_hook_active": False},
    "PreCompact": {"trigger": "manual", "custom_instructions": ""},
    "Stop": {"stop_hook_active": False},
    "SessionEnd": {"reason": "other"},
}

SKIP_DIRS = frozenset({"__pycache__", ".venv", ".git", "node_modules"})
BINARY_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".pyc", ".db", ".zip", ".gz"})
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_FM_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_GATE_RE = re.compile(r"^## Unattended-context gate[ \t]*$", re.M)
_WORKER_RE = re.compile(r"`(w-[a-z]+(?:-[a-z]+)*)`")
_DROPPED_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    + "|".join(re.escape(t) for t in sorted(DROPPED_TERMS, key=len, reverse=True))
    + r")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_DELETED_LINE_RE = re.compile(r"^Deleted skills[^:]*:\s*(.+)$", re.M)
_LOG_ENTRY_RE = re.compile(r"^\d{4}-\d\d-\d\dT[\d:]+ (\S+): (.*)$", re.M)
_TEST_DEF_RE = re.compile(r"^\s*(?:async\s+)?def test\w*\s*\(", re.M)
_SCORE_RE = re.compile(r"^SCORE: (\d+)/100\s*$", re.M)
_SCORED_RE = re.compile(r"^Scored \d+/\d+ pts.*$", re.M)
_CRIT_RE = re.compile(r"^\| (\d+) \| ([^|]+?) \| (\d+)/(\d+) \|", re.M)
_NA_RE = re.compile(r"^\| (\d+) \| ([^|]+?) \| N/A \|", re.M)


# --------------------------------------------------------------------------
# Modello dei risultati
# --------------------------------------------------------------------------
@dataclass
class Finding:
    component: str
    check: str
    path: str
    detail: str
    points: int | None = 0   # None = componente a rapporto, punti non attribuibili al singolo rilievo


@dataclass
class Component:
    key: str
    label: str
    weight: int
    score: int
    detail: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def weighted(self) -> float:
        return self.score * self.weight / 100


@dataclass
class RunContext:
    root: Path
    python: Path             # interprete del venv, oppure quello corrente se il venv manca
    settings: dict | None
    settings_error: str | None
    data_root: Path | None = None  # data home (settings.json, agent-memory); None = root

    @property
    def data(self) -> Path:
        return self.root if self.data_root is None else self.data_root


@dataclass
class SuiteResult:
    passed: int
    failed: int
    errors: int
    findings: list[Finding]


# --------------------------------------------------------------------------
# Utilità
# --------------------------------------------------------------------------
def rel(root: Path, path: Path, line: int | None = None) -> str:
    """Percorso relativo alla radice con slash in avanti, più `:riga` se nota."""
    try:
        text = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        text = path.as_posix()
    return f"{text}:{line}" if line else text


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


def load_json(path: Path) -> tuple[object, str | None]:
    """(dati, errore): l'errore è None solo se il file esiste ed è JSON valido."""
    text = read_text(path)
    if text is None:
        return None, "missing or unreadable"
    try:
        return json.loads(text), None
    except ValueError as exc:
        return None, f"invalid JSON: {exc}"


def parse_frontmatter(text: str) -> dict | None:
    """Frontmatter YAML iniziale come dict; None se assente, non valido o non mappa."""
    m = _FM_RE.match(text.lstrip("\ufeff"))
    if not m:
        return None
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def key_line(text: str, key: str) -> int | None:
    """Riga (1-based) della prima chiave `key:` a inizio riga."""
    m = re.search(rf"^{re.escape(key)}\s*:", text, re.M)
    return text.count("\n", 0, m.start()) + 1 if m else None


def child_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra)
    return env


def venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def first_line(raw: bytes | str, limit: int = 160) -> str:
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[0][:limit] if lines else ""


def last_line(raw: bytes | str, limit: int = 160) -> str:
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1][:limit] if lines else ""


def git_out(root: Path, *args: str) -> str | None:
    """Stdout di git (sola lettura, niente lock opzionali); None se git fallisce."""
    cmd = ["git", "--no-optional-locks", "-C", str(root), *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=GIT_TIMEOUT_S,
                              stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.decode("utf-8", "replace") if proc.returncode == 0 else None


def is_git_root(root: Path) -> bool:
    """True solo se `root` è proprio la radice di un working tree git."""
    top = git_out(root, "rev-parse", "--show-toplevel")
    if not top or not top.strip():
        return False
    try:
        return os.path.samefile(top.strip(), root)
    except OSError:
        return False


def skip_path(root: Path, path: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    return bool(SKIP_DIRS.intersection(parts))


def list_files(root: Path, tops: tuple[str, ...] | list[str]) -> list[Path]:
    """File tracciati o non ignorati sotto `tops`; senza git, scansione del disco."""
    if is_git_root(root):
        out = git_out(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *tops)
        if out is not None:
            found = {root / p for p in out.split("\0") if p}
            return sorted(p for p in found if p.is_file() and not skip_path(root, p))
    files: list[Path] = []
    for top in tops:
        base = root / top
        if base.is_file():
            files.append(base)
        elif base.is_dir():
            files += [p for p in base.rglob("*") if p.is_file() and not skip_path(root, p)]
    return sorted(files)


def compile_findings(root: Path, component: str, files: list[Path]) -> list[Finding]:
    """Compila in memoria ogni .py: nessun __pycache__, nessuna scrittura."""
    out: list[Finding] = []
    for path in files:
        if path.suffix != ".py":
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
        except SyntaxError as exc:
            out.append(Finding(component, "py_compile", rel(root, path, exc.lineno), f"SyntaxError: {exc.msg}"))
        except (ValueError, OSError) as exc:
            out.append(Finding(component, "py_compile", rel(root, path), f"{type(exc).__name__}: {exc}"))
    return out


def apply_penalties(component: str, findings: list[Finding]) -> int:
    """Assegna i punti a ogni rilievo rispettando i tetti; restituisce il punteggio 0-100."""
    table = PENALTIES[component]
    spent: dict[str, int] = {}
    for f in findings:
        per, cap, _ = table[f.check]
        f.points = max(0, min(per, cap - spent.get(f.check, 0)))
        spent[f.check] = spent.get(f.check, 0) + f.points
    return max(0, 100 - sum(spent.values()))


# --------------------------------------------------------------------------
# Hooks
# --------------------------------------------------------------------------
def iter_hook_entries(config: object) -> dict[str, list[dict]]:
    """Evento -> lista delle entry hook, per la forma di settings.json['hooks'] e di hooks.json."""
    out: dict[str, list[dict]] = {}
    if not isinstance(config, dict):
        return out
    for event, groups in config.items():
        if event.startswith("_") or not isinstance(groups, list):
            continue
        for group in groups:
            hooks = group.get("hooks") if isinstance(group, dict) else None
            for hook in hooks if isinstance(hooks, list) else []:
                if isinstance(hook, dict):
                    out.setdefault(event, []).append(hook)
    return out


def parse_hook(hook: dict) -> tuple[str, list[str]] | None:
    """(interprete, argomenti) sia per la forma exec (`args`) sia per la forma shell."""
    command = hook.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    args = hook.get("args")
    if isinstance(args, list):
        return command, [str(a) for a in args]
    try:
        # I backslash dei percorsi Windows non sopravvivono a shlex POSIX: si passa agli slash
        tokens = shlex.split(command.replace("\\", "/"))
    except ValueError:
        return None
    return (tokens[0], tokens[1:]) if tokens else None


def interpreter_exists(interp: str) -> bool:
    return Path(interp).is_file() or shutil.which(interp) is not None


def target_problem(root: Path, event: str, interp: str, args: list[str]) -> str | None:
    if not interpreter_exists(interp):
        return f"interpreter not found: {interp}"
    dispatch = next((a for a in args if a.lower().endswith("dispatch.py")), None)
    if dispatch is None:
        return "no dispatch.py argument"
    if not Path(dispatch).is_file():
        return f"dispatch.py not found: {dispatch}"
    expected = root / "hooks" / "dispatch.py"
    try:
        same = os.path.samefile(dispatch, expected)
    except OSError:
        same = False
    if not same:
        return f"points at {dispatch}, not {rel(root, expected)} of this install"
    if args[-1] != event:
        return f"event argument {args[-1]!r} differs from {event}"
    return None


def resolve_targets(root: Path, registered: dict[str, list[dict]]) -> tuple[dict, list[Finding]]:
    """Primo bersaglio valido per evento, più un rilievo per ogni entry difettosa."""
    targets: dict[str, tuple[str, list[str]]] = {}
    found: list[Finding] = []
    for event in sorted(registered):
        for i, hook in enumerate(registered[event]):
            parsed = parse_hook(hook)
            problem = "unparseable command" if parsed is None else target_problem(root, event, *parsed)
            if problem:
                found.append(Finding("hooks", "hook_target", "settings.json", f"hooks.{event}[{i}]: {problem}"))
            elif event not in targets:
                targets[event] = parsed
    return targets, found


def shell_form_findings(hooks_cfg: object, registered: dict[str, list[dict]]) -> list[Finding]:
    cfg_exec = any("args" in h for hooks in iter_hook_entries(hooks_cfg).values() for h in hooks)
    shell = sorted(ev for ev, hooks in registered.items() if any("args" not in h for h in hooks))
    if not (cfg_exec and shell):
        return []
    detail = (f"{len(shell)} event(s) registered in shell form ({', '.join(shell)}) while "
              "hooks/hooks.json is exec-form: run bin/apply-settings.ps1 -Profile superclaude")
    return [Finding("hooks", "shell_form", "settings.json", detail)]


def smoke_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in SMOKE_ENV_DROP and not k.startswith("SUPERCLAUDE_GUARD")}
    env.update({"USERPROFILE": str(home), "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def smoke_payload(event: str, home: Path) -> dict:
    payload = {"session_id": SMOKE_SESSION, "hook_event_name": event, "cwd": str(home / "code")}
    payload.update(SMOKE_EXTRA.get(event, {}))
    return payload


def stdout_problem(event: str, raw: bytes) -> str | None:
    """Lo stdout di un hook è vuoto o un oggetto JSON; SessionStart/SubagentStart devono iniettare contesto."""
    text = raw.decode("utf-8", "replace").strip()
    data: object = None
    if text:
        try:
            data = json.loads(text)
        except ValueError:
            return f"stdout is not JSON: {text[:80]!r}"
    if event not in CONTEXT_EVENTS:
        return None
    specific = data.get("hookSpecificOutput") if isinstance(data, dict) else None
    if not (isinstance(specific, dict) and specific.get("additionalContext")):
        return "no additionalContext: the memory-injection handler did not run (fail-open?)"
    return None


def smoke_once(target: tuple[str, list[str]], event: str, home: Path, env: dict[str, str]) -> str | None:
    interp, args = target
    raw = json.dumps(smoke_payload(event, home)).encode("utf-8")
    try:
        proc = subprocess.run([interp, *args], input=raw, capture_output=True, env=env,
                              timeout=SMOKE_TIMEOUT_S, cwd=str(home / "code"), creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return f"no exit within {SMOKE_TIMEOUT_S}s"
    except OSError as exc:
        return f"could not start: {exc}"
    if proc.returncode != 0:
        return f"exit {proc.returncode}: {first_line(proc.stderr)}"
    return stdout_problem(event, proc.stdout)


def handler_path(root: Path, where: str) -> str:
    """Mappa l'etichetta di core.log_error al file dell'handler."""
    handlers = root / "hooks" / "sc_hooks" / "handlers"
    if where == "dispatch":
        return rel(root, root / "hooks" / "dispatch.py")
    name = where.split("/", 1)[-1].replace("-", "_")
    candidate = handlers / f"{name}.py"
    return rel(root, candidate if candidate.is_file() else handlers)


def handler_error_findings(root: Path, log_path: Path) -> list[Finding]:
    first: dict[str, str] = {}
    for where, msg in _LOG_ENTRY_RE.findall(read_text(log_path) or ""):
        first.setdefault(where, msg)
    return [Finding("hooks", "handler_error", handler_path(root, where), f"{where}: {msg[:160]}")
            for where, msg in sorted(first.items())]


def run_smokes(root: Path, targets: dict[str, tuple[str, list[str]]]) -> list[Finding]:
    """Esegue dispatch.py per ogni evento registrato dentro una HOME finta temporanea."""
    found: list[Finding] = []
    if not targets:
        return found
    with tempfile.TemporaryDirectory(prefix="super-health-", ignore_cleanup_errors=True) as td:
        home = Path(os.path.realpath(td))
        (home / "code").mkdir()
        env = smoke_env(home)
        for event in sorted(targets):
            problem = smoke_once(targets[event], event, home, env)
            if problem:
                found.append(Finding("hooks", "smoke", rel(root, root / "hooks" / "dispatch.py"), f"{event}: {problem}"))
        # Gli hook del sandbox scrivono nella data home del profilo, relativa alla HOME finta
        sandbox_data = home / sc_profile.data_home().relative_to(Path.home())
        found += handler_error_findings(root, sandbox_data / "logs" / "hooks.log")
    return found


def hooks_findings(ctx: RunContext) -> tuple[list[Finding], str]:
    root = ctx.root
    found = compile_findings(root, "hooks", [p for p in list_files(root, ["hooks"]) if p.suffix == ".py"])
    hooks_cfg, err = load_json(root / "hooks" / "hooks.json")
    if not isinstance(hooks_cfg, dict):
        found.append(Finding("hooks", "hooks_json", "hooks/hooks.json", err or "top level is not an object"))
    registered = iter_hook_entries((ctx.settings or {}).get("hooks"))
    for event in sorted(iter_hook_entries(hooks_cfg)):
        if event not in registered:
            found.append(Finding("hooks", "event_unregistered", "settings.json",
                                 f"event {event} of hooks/hooks.json has no hook in settings.json"))
    targets, target_findings = resolve_targets(root, registered)
    found += target_findings
    found += shell_form_findings(hooks_cfg, registered)
    found += run_smokes(root, targets)
    return found, f"{len(registered)} events registered, {len(targets)} smoke-run in a sandbox HOME"


# --------------------------------------------------------------------------
# Skills
# --------------------------------------------------------------------------
def skill_file_findings(root: Path, path: Path, readme: str) -> list[Finding]:
    name = path.parent.name
    text = read_text(path) or ""
    where = rel(root, path)
    out: list[Finding] = []
    if f"`{name}`" not in readme:
        out.append(Finding("skills", "readme", "skills/README.md", f"skill {name} has no `{name}` entry"))
    fm = parse_frontmatter(text)
    if fm is None:
        return out + [Finding("skills", "frontmatter", where, "no parseable YAML frontmatter")]
    if fm.get("name") != name:
        out.append(Finding("skills", "name", rel(root, path, key_line(text, "name")),
                           f"name {fm.get('name')!r} != directory {name!r}"))
    desc = fm.get("description")
    if not isinstance(desc, str) or not desc.startswith("Use when"):
        out.append(Finding("skills", "description", rel(root, path, key_line(text, "description")),
                           f"description {str(desc)[:60]!r} does not start with 'Use when'"))
    if fm.get("disable-model-invocation") is True:
        out.append(Finding("skills", "model_invocation", rel(root, path, key_line(text, "disable-model-invocation")),
                           "disable-model-invocation: true"))
    return out


def git_gate_findings(root: Path) -> list[Finding]:
    path = root / "skills" / "git" / "SKILL.md"
    text = read_text(path)
    if text is None:
        return [Finding("skills", "git_gate", rel(root, path), "file missing")]
    count = len(_GATE_RE.findall(text))
    if count == 1:
        return []
    return [Finding("skills", "git_gate", rel(root, path), f"{count} '## Unattended-context gate' headings, expected 1")]


def shared_findings(root: Path) -> list[Finding]:
    return [Finding("skills", "shared_consumers", rel(root, p), "no 'Consumed by:' line")
            for p in sorted((root / "skills" / "_shared").glob("*.md"))
            if "Consumed by:" not in (read_text(p) or "")]


def skills_findings(ctx: RunContext) -> tuple[list[Finding], str]:
    root = ctx.root
    readme = read_text(root / "skills" / "README.md") or ""
    skill_files = [p for p in sorted((root / "skills").glob("*/SKILL.md")) if p.parent.name != "_shared"]
    found: list[Finding] = []
    for path in skill_files:
        found += skill_file_findings(root, path, readme)
    found += git_gate_findings(root)
    found += shared_findings(root)
    found += compile_findings(root, "skills", [p for p in list_files(root, ["skills"]) if p.suffix == ".py"])
    return found, f"{len(skill_files)} skills"


# --------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------
def parse_mem_health(text: str) -> tuple[int | None, str]:
    """(punteggio finale, riassunto) dall'output di mem_health.py."""
    scores = _SCORE_RE.findall(text)
    if not scores:
        return None, "no 'SCORE: N/100' line"
    parts = [m.group(0) for m in _SCORED_RE.finditer(text)]
    parts += [f"#{n} {label.strip()} {got}/{mx}" for n, label, got, mx in _CRIT_RE.findall(text) if int(got) < int(mx)]
    parts += [f"#{n} {label.strip()} N/A" for n, label in _NA_RE.findall(text)]
    return int(scores[-1]), "; ".join(parts)


def memory_component(ctx: RunContext) -> tuple[int, str, list[Finding]]:
    script = ctx.root / "scripts" / "memory" / "mem_health.py"
    if not script.is_file():
        return 0, "mem_health.py missing", [Finding("memory", "mem_health", rel(ctx.root, script), "missing", 100)]
    try:
        proc = subprocess.run([str(ctx.python), "-B", str(script)], capture_output=True, timeout=MEM_TIMEOUT_S,
                              env=child_env(CLAUDE_DIR=str(ctx.data)), stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        text = proc.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as exc:
        text = f"could not run: {exc}"
    score, summary = parse_mem_health(text)
    if score is None:
        return 0, "mem_health.py output unparseable", [Finding("memory", "mem_health", rel(ctx.root, script), summary, 100)]
    score = max(0, min(100, score))
    findings = []
    if score < 100:
        db = rel(ctx.root, ctx.data / "agent-memory" / ".memory.db")
        findings.append(Finding("memory", "mem_health", db, f"mem_health.py SCORE {score}/100: {summary}", 100 - score))
    return score, f"mem_health.py {score}/100", findings


# --------------------------------------------------------------------------
# Settings + agents
# --------------------------------------------------------------------------
def model_allowed(model: object) -> bool:
    if not isinstance(model, str):
        return False
    if model in ALLOWED_MODELS:
        return True
    return any(model == f"{alias}{sfx}" for alias in ALLOWED_MODELS if alias != "inherit"
               for sfx in ALLOWED_MODEL_SUFFIXES)


def agent_findings(root: Path, path: Path) -> list[Finding]:
    text = read_text(path) or ""
    fm = parse_frontmatter(text)
    if fm is None:
        return [Finding("settings", "agent_frontmatter", rel(root, path), "no parseable YAML frontmatter")]
    out: list[Finding] = []
    if fm.get("name") != path.stem:
        out.append(Finding("settings", "agent_name", rel(root, path, key_line(text, "name")),
                           f"name {fm.get('name')!r} != file stem {path.stem!r}"))
    if not model_allowed(fm.get("model")):
        out.append(Finding("settings", "agent_model", rel(root, path, key_line(text, "model")),
                           f"model {fm.get('model')!r} not in {ALLOWED_MODELS}"))
    if "effort" in fm and fm["effort"] not in ALLOWED_EFFORTS:
        out.append(Finding("settings", "agent_effort", rel(root, path, key_line(text, "effort")),
                           f"effort {fm['effort']!r} not in {ALLOWED_EFFORTS}"))
    if fm.get("tools") in (None, ""):
        out.append(Finding("settings", "agent_tools", rel(root, path), "no tools field"))
    return out


def backticked_workers(text: str) -> dict[str, int]:
    """Nome w-* citato come identificatore tra backtick -> prima riga in cui compare."""
    out: dict[str, int] = {}
    for i, line in enumerate(text.splitlines(), 1):
        for name in _WORKER_RE.findall(line):
            out.setdefault(name, i)
    return out


def roster_findings(root: Path, agents: list[Path]) -> list[Finding]:
    rules13 = root / "rules" / "13-worker-first-mandate.md"
    named13 = backticked_workers(read_text(rules13) or "")
    existing = {p.stem for p in agents}
    out: list[Finding] = []
    sources = ((rules13, named13), (root / "CLAUDE.md", backticked_workers(read_text(root / "CLAUDE.md") or "")))
    for src, names in sources:
        out += [Finding("settings", "roster_missing", rel(root, src, line), f"{name} has no agents/{name}.md")
                for name, line in sorted(names.items()) if name not in existing]
    out += [Finding("settings", "roster_unlisted", f"agents/{stem}.md", f"not named in {rel(root, rules13)}")
            for stem in sorted(existing) if stem.startswith("w-") and stem not in named13]
    return out


def install_findings(root: Path) -> list[Finding]:
    """Residui effimeri, shim di bin/ e interprete del venv."""
    out = [Finding("settings", "ephemeral_leftover", rel(root, p), "leftover ephemeral agent definition")
           for p in sorted((root / "agents" / "_ephemeral").glob("*.md"))]
    out += [Finding("settings", "bin_shim", rel(root, root / "bin" / shim), "missing")
            for shim in ("mem", "scpy") if not (root / "bin" / shim).is_file()]
    if not venv_python(root).is_file():
        out.append(Finding("settings", "venv", rel(root, venv_python(root)), "missing"))
    return out


def settings_findings(ctx: RunContext) -> tuple[list[Finding], str]:
    root = ctx.root
    found: list[Finding] = []
    if ctx.settings_error:
        found.append(Finding("settings", "settings_json", "settings.json", ctx.settings_error))
    agents = sorted((root / "agents").glob("*.md"))
    for path in agents:
        found += agent_findings(root, path)
    found += roster_findings(root, agents)
    found += install_findings(root)
    return found, f"{len(agents)} agents"


# --------------------------------------------------------------------------
# Regression tests
# --------------------------------------------------------------------------
def discover_suites(root: Path) -> list[str]:
    """Suite predefinite più ogni cartella tests/ tracciata in git (fuori da .venv)."""
    suites = set(DEFAULT_SUITES)
    tracked = git_out(root, "ls-files", "-z") if is_git_root(root) else None
    for rel_path in (tracked or "").split("\0"):
        parts = rel_path.split("/")
        if ".venv" in parts or "tests" not in parts[:-1]:
            continue
        suites.add("/".join(parts[: parts.index("tests") + 1]))
    return sorted(suites)


def static_test_count(path: Path) -> int:
    """Numero di funzioni test_* nei file di test: stima della suite quando pytest non parte."""
    if not path.is_dir():
        return 0
    files = [p for p in path.rglob("*.py") if p.name.startswith("test_") or p.name.endswith("_test.py")]
    return sum(len(_TEST_DEF_RE.findall(read_text(p) or "")) for p in files if "__pycache__" not in p.parts)


def suite_failed(root: Path, suite: str, why: str) -> SuiteResult:
    n = max(1, static_test_count(root / suite))
    finding = Finding("regression", "suite", suite, f"{why}; whole suite counted as failing ({n} tests)", None)
    return SuiteResult(0, n, 0, [finding])


def case_path(root: Path, suite: str, case: ET.Element) -> str:
    """Percorso `file:riga` di un testcase (junit xunit1: `file` relativo al rootdir, `line` 0-based)."""
    name = case.get("file") or ""
    line = int(case.get("line")) + 1 if (case.get("line") or "").isdigit() else None
    for base in (root / suite, root):
        if name and (base / name).is_file():
            return rel(root, base / name, line)
    return f"{suite}/{name}" if name else suite


def parse_junit(root: Path, suite: str, xml_path: Path) -> SuiteResult:
    passed = failed = errors = 0
    findings: list[Finding] = []
    for case in ET.parse(xml_path).getroot().iter("testcase"):
        tags = {child.tag for child in case}
        if "failure" in tags:
            failed, kind = failed + 1, "failed"
        elif "error" in tags:
            errors, kind = errors + 1, "error"
        elif "skipped" in tags:
            continue
        else:
            passed += 1
            continue
        findings.append(Finding("regression", "test", case_path(root, suite, case),
                                f"{kind}: {case.get('classname')}::{case.get('name')}", None))
    return SuiteResult(passed, failed, errors, findings)


def run_suite(ctx: RunContext, suite: str) -> SuiteResult:
    path = ctx.root / suite
    if not path.is_dir():
        return suite_failed(ctx.root, suite, "suite directory missing")
    with tempfile.TemporaryDirectory(prefix="super-health-junit-", ignore_cleanup_errors=True) as td:
        xml_path = Path(td) / "junit.xml"
        # rootdir fissato sulla suite: l'attributo `file` del junit è relativo a lei
        cmd = [str(ctx.python), "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--rootdir={path}",
               "-o", "junit_family=xunit1", f"--junitxml={xml_path}", str(path)]
        try:
            proc = subprocess.run(cmd, cwd=str(ctx.root), capture_output=True, timeout=SUITE_TIMEOUT_S,
                                  env=child_env(), stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as exc:
            return suite_failed(ctx.root, suite, f"pytest could not run: {type(exc).__name__}")
        if proc.returncode not in (0, 1) or not xml_path.is_file():
            return suite_failed(ctx.root, suite, f"pytest exit {proc.returncode}: {last_line(proc.stdout)}")
        return parse_junit(ctx.root, suite, xml_path)


def regression_component(ctx: RunContext) -> tuple[int, str, list[Finding]]:
    suites = discover_suites(ctx.root)
    results = [run_suite(ctx, s) for s in suites]
    passed = sum(r.passed for r in results)
    failed = sum(r.failed for r in results)
    errors = sum(r.errors for r in results)
    total = passed + failed + errors
    findings = [f for r in results for f in r.findings]
    # Arrotondamento per difetto: anche un solo test rosso toglie almeno un punto
    score = passed * 100 // total if total else 0
    detail = f"{passed} passed, {failed} failed, {errors} errors in {len(suites)} suites"
    return score, detail, findings


# --------------------------------------------------------------------------
# Drift and hygiene
# --------------------------------------------------------------------------
def deleted_slash_re(root: Path) -> re.Pattern[str] | None:
    """Regex dei comandi /<skill eliminata>, dall'elenco 'Deleted skills' di CONVENTIONS.md."""
    m = _DELETED_LINE_RE.search(read_text(root / CONVENTIONS_REL) or "")
    if not m:
        return None
    names = [n.strip().rstrip(".").strip() for n in m.group(1).split(",")]
    names = sorted({n for n in names if n and n != "super-health"}, key=len, reverse=True)
    if not names:
        return None
    # Niente parola, punto, slash, tilde o trattino prima dello slash: esclude percorsi e URL
    return re.compile(r"(?<![\w./~-])/(?:" + "|".join(map(re.escape, names)) + r")(?![\w-])")


def scan_lines(root: Path, path: Path, text: str, slash_re: re.Pattern[str] | None) -> list[Finding]:
    """Un rilievo per riga e per categoria, con tutti i termini trovati sulla riga."""
    out: list[Finding] = []
    for i, line in enumerate(text.splitlines(), 1):
        terms = sorted({m.group(0) for m in _DROPPED_RE.finditer(line)})
        if terms:
            out.append(Finding("drift", "dropped_term", rel(root, path, i), ", ".join(terms)))
        slashes = sorted({m.group(0) for m in slash_re.finditer(line)}) if slash_re else []
        if slashes:
            out.append(Finding("drift", "deleted_slash", rel(root, path, i), ", ".join(slashes)))
    return out


def drift_findings(ctx: RunContext) -> tuple[list[Finding], str]:
    root = ctx.root
    files = [p for p in list_files(root, DRIFT_SCOPE) if p.suffix.lower() not in BINARY_SUFFIXES]
    slash_re = deleted_slash_re(root)
    found: list[Finding] = []
    for path in files:
        text = read_text(path)
        if text is not None:
            found += scan_lines(root, path, text, slash_re)
    detail = f"{len(files)} files swept"
    if slash_re is None:
        detail += f"; no 'Deleted skills' list in {CONVENTIONS_REL}, slash sweep skipped"
    return found, detail


def working_tree(root: Path) -> dict | None:
    """Stato del working tree git: solo informativo, non entra nel punteggio."""
    if not is_git_root(root):
        return None
    out = git_out(root, "status", "--porcelain")
    if out is None:
        return None
    entries = [ln for ln in out.splitlines() if ln.strip()]
    return {"changed": len(entries), "entries": entries}


# --------------------------------------------------------------------------
# Aggregazione e output
# --------------------------------------------------------------------------
TABLE_SCORERS = {
    "hooks": hooks_findings,
    "skills": skills_findings,
    "settings": settings_findings,
    "drift": drift_findings,
}
RATIO_SCORERS = {"memory": memory_component, "regression": regression_component}


def default_data_root(root: Path) -> Path:
    """Data home del profilo attivo se root e' l'installazione reale, altrimenti root stessa."""
    if sc_profile.is_within(root, sc_profile.code_home()) and sc_profile.is_within(sc_profile.code_home(), root):
        return sc_profile.data_home()
    return root


def build_context(root: Path, data_root: Path | None = None) -> RunContext:
    data = default_data_root(root) if data_root is None else data_root
    # settings.json e' il primo di SETTINGS_FILES; settings.local.json non porta la tabella degli hook
    settings, err = load_json(data / sc_profile.SETTINGS_FILES[0])
    if err is None and not isinstance(settings, dict):
        settings, err = None, "top level is not an object"
    py = venv_python(root)
    return RunContext(root, py if py.is_file() else Path(sys.executable), settings, err, data)


def score_component(ctx: RunContext, key: str, label: str, weight: int) -> Component:
    if key in TABLE_SCORERS:
        findings, detail = TABLE_SCORERS[key](ctx)
        return Component(key, label, weight, apply_penalties(key, findings), detail, findings)
    score, detail, findings = RATIO_SCORERS[key](ctx)
    return Component(key, label, weight, score, detail, findings)


def aggregate(components: list[Component]) -> tuple[int, str]:
    """(punteggio intero arrotondato half-up, voto) dalla somma pesata."""
    points_x100 = sum(c.score * c.weight for c in components)
    score = (points_x100 + 50) // 100
    grade = next(g for floor, g in GRADES if score >= floor)
    return score, grade


def run(root: Path, data_root: Path | None = None) -> dict:
    ctx = build_context(root, data_root)
    components = [score_component(ctx, key, label, weight) for key, label, weight in COMPONENTS]
    score, grade = aggregate(components)
    return {
        "root": root.as_posix(),
        "score": score,
        "grade": grade,
        "weighted_total": round(sum(c.weighted for c in components), 2),
        "components": [{**asdict(c), "weighted": round(c.weighted, 2)} for c in components],
        "findings": [asdict(f) for c in components for f in c.findings],
        "working_tree": working_tree(root),
        "penalties": {comp: {chk: {"per_hit": p, "cap": cap, "description": d} for chk, (p, cap, d) in table.items()}
                      for comp, table in PENALTIES.items()},
        "ratio_rules": RATIO_RULES,
    }


def cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def points_label(f: dict) -> str:
    if f["points"] is None:
        return "ratio"
    return f"-{f['points']}" if f["points"] else "0 (capped)"


def render_markdown(report: dict) -> str:
    out = ["## Superclaude Health Report", "", f"Root: `{report['root']}`", "",
           "| Component | Weight | Score | Weighted | Detail |", "|---|---:|---:|---:|---|"]
    for c in report["components"]:
        out.append(f"| {c['label']} | {c['weight']} | {c['score']}/100 | {c['weighted']:.2f} | {cell(c['detail'])} |")
    out += [f"| **Total** | **100** | | **{report['weighted_total']:.2f}** | |", ""]
    findings = report["findings"]
    out.append(f"### Findings ({len(findings)})")
    out += [f"- [{f['component']}/{f['check']}] {points_label(f)} `{f['path']}`: {f['detail']}" for f in findings]
    if not findings:
        out.append("- none")
    tree = report["working_tree"]
    out += ["", "### Working tree (informational, not scored)"]
    if tree is None:
        out.append("- not a git working tree")
    else:
        out.append(f"- {tree['changed']} changed path(s)")
        out += [f"  - `{e}`" for e in tree["entries"][:30]]
    out += ["", f"Grade: {report['grade']}", f"SCORE: {report['score']}/100"]
    return "\n".join(out)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="super_health.py", description="Superclaude health score /100.")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--root", type=Path, default=sc_profile.code_home(), help="install root (default ~/.claude)")
    parser.add_argument("--data-root", type=Path, default=None,
                        help="profile data home (default: SC_PROFILE data home for the real install, else --root)")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = parse_args(argv)
    try:
        root = args.root.expanduser()
        report = run(root) if args.data_root is None else run(root, args.data_root.expanduser())
    except Exception:  # noqa: BLE001 - crash interno: exit 2 con traceback
        traceback.print_exc()
        return 2
    sys.stdout.write((json.dumps(report, indent=2) if args.json else render_markdown(report)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
