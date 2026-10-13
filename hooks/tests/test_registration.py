"""Test della registrazione: hooks.json deve coprire ogni handler del registro.

Il rischio introdotto da U-1 e' che un matcher piu' stretto o un filtro `if`
tolga silenziosamente di mezzo una guardia. Qui il registro vero (gli handler
caricati come fa dispatch.py) viene confrontato con hooks.json:

- ogni coppia (evento, strumento) registrata deve essere coperta da almeno una
  entry il cui matcher accetta lo strumento;
- se l'handler nega o riscrive l'input (guardia), la copertura deve arrivare da
  una entry SENZA `if`: un filtro non puo' mai restringere una guardia;
- ogni entry deve essere in forma exec (`args`), non in forma shell;
- ogni evento presente in hooks.json deve esistere nel registro.

Le condizioni di errore sono verificate su configurazioni sintetiche costruite
con `json.dumps` (mai attraverso una shell: i backslash dei percorsi Windows non
sopravvivono a un heredoc).
"""
from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
import re
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

from sc_hooks import core  # noqa: E402

HOOKS_JSON = HOOKS_DIR / "hooks.json"

# Eventi su cui Claude Code valuta `if` (documentazione hooks). Altrove sarebbe
# lettera morta, quindi un `if` fuori da questi eventi e' un errore.
TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure",
               "PermissionRequest", "PermissionDenied")

# Un handler e' una guardia se il suo modulo nega o riscrive l'input dello
# strumento. La rilevazione e' sul sorgente, cosi' una guardia nuova risulta
# protetta senza aggiornare questo elenco.
_GUARD_MARKERS = ("deny(", "rewrite(", "updated_input")
# baseline-stash non nega nulla ma il suo effetto collaterale (R-2) deve
# avvenire per ogni scrittura: va trattato come una guardia.
_ALWAYS_GUARDS = frozenset({"baseline-stash"})

_RULE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*\([^()]*\)$")
# In forma exec `command` e' un solo eseguibile: gli spazi sono legittimi
# ("C:/Program Files/..."), le virgolette e gli operatori di shell no.
_SHELL_CHARS = re.compile(r"""["'|&><;]""")


# --------------------------------------------------------------------------
# Registro e configurazione
# --------------------------------------------------------------------------
def load_registry() -> list[tuple[str, tuple[str, ...], str, object]]:
    """Carica gli handler come dispatch.py e restituisce il registro."""
    import sc_hooks.handlers as pkg

    for mod in sorted(m.name for m in pkgutil.iter_modules(pkg.__path__)):
        importlib.import_module(f"sc_hooks.handlers.{mod}")
    return list(core._REGISTRY)


def load_hooks() -> dict:
    return json.loads(HOOKS_JSON.read_text(encoding="utf-8"))


def entries(hooks: dict, event: str) -> list[dict]:
    """Appiattisce hooks.json in una lista di entry (matcher, if, comando)."""
    out: list[dict] = []
    for group in hooks.get(event) or []:
        for hook in group.get("hooks") or []:
            out.append({
                "matcher": group.get("matcher", ""),
                "if": hook.get("if") or group.get("if"),
                "hook": hook,
            })
    return out


def matches(matcher: str, tool: str) -> bool:
    """Un matcher vuoto (o `*`) accetta tutto; altrimenti e' una regex intera."""
    if matcher in ("", "*"):
        return True
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return False


def is_guard(name: str, fn) -> bool:
    if name in _ALWAYS_GUARDS:
        return True
    try:
        src = inspect.getsource(sys.modules[fn.__module__])
    except (OSError, TypeError, KeyError):
        return True          # nel dubbio, trattalo come guardia
    return any(marker in src for marker in _GUARD_MARKERS)


# --------------------------------------------------------------------------
# Controlli riutilizzabili (usati sia sul file vero sia sui casi sintetici)
# --------------------------------------------------------------------------
def exec_form_problems(hooks: dict) -> list[str]:
    problems: list[str] = []
    for event, groups in hooks.items():
        if event.startswith("_"):
            continue
        if not isinstance(groups, list):
            problems.append(f"{event}: il valore non e' una lista di entry")
            continue
        for idx, group in enumerate(groups):
            for hook in group.get("hooks") or []:
                where = f"{event}[{idx}]"
                if hook.get("type") != "command":
                    problems.append(f"{where}: type diverso da 'command'")
                cmd = hook.get("command")
                if not isinstance(cmd, str) or not cmd:
                    problems.append(f"{where}: 'command' mancante")
                elif _SHELL_CHARS.search(cmd):
                    problems.append(f"{where}: 'command' contiene metacaratteri di shell: forma non exec")
                args = hook.get("args")
                if not isinstance(args, list) or not args:
                    problems.append(f"{where}: 'args' mancante: forma shell")
                    continue
                if not all(isinstance(a, str) for a in args):
                    problems.append(f"{where}: 'args' contiene elementi non stringa")
                elif args[-1] != event:
                    problems.append(f"{where}: l'ultimo argomento e' {args[-1]!r}, atteso {event!r}")
                if "-S" not in args:
                    problems.append(f"{where}: manca il flag -S")
    return problems


def unknown_events(hooks: dict, registry) -> list[str]:
    """Eventi dichiarati in hooks.json che nessun handler gestisce."""
    known = {event for event, _, _, _ in registry}
    return sorted(e for e in hooks if not e.startswith("_") and e not in known)


def filter_problems(hooks: dict) -> list[str]:
    problems: list[str] = []
    for event in hooks:
        if event.startswith("_"):
            continue
        for entry in entries(hooks, event):
            cond = entry["if"]
            if cond is None:
                continue
            if event not in TOOL_EVENTS:
                problems.append(f"{event}: 'if' non viene valutato su questo evento")
            if not isinstance(cond, str) or not _RULE_RE.match(cond):
                problems.append(f"{event}: 'if' {cond!r} non e' nella sintassi delle regole di permesso")
    return problems


def overlap_problems(hooks: dict) -> list[str]:
    """Due entry che accettano lo stesso strumento avvierebbero due processi."""
    problems: list[str] = []
    registry = load_registry()
    for event in [e for e in hooks if not e.startswith("_")]:
        if event not in TOOL_EVENTS:
            continue
        tools = sorted({t for ev, ts, _, _ in registry if ev == event for t in ts})
        for tool in tools:
            hit = [e for e in entries(hooks, event) if matches(e["matcher"], tool)]
            if len(hit) > 1:
                problems.append(f"{event}/{tool}: {len(hit)} entry lo accettano, un processo per ciascuna")
    return problems


def coverage_problems(hooks: dict, registry) -> list[str]:
    problems: list[str] = []
    universe: dict[str, set[str]] = {}
    for event, tools, _name, _fn in registry:
        universe.setdefault(event, set()).update(tools)

    for event, tools, name, fn in registry:
        found = entries(hooks, event)
        if not found:
            problems.append(f"{event}/{name}: l'evento non e' registrato in hooks.json")
            continue
        if event in TOOL_EVENTS:
            targets = sorted(tools) if tools else sorted(universe.get(event) or {""})
        else:
            targets = [""]
        guard = is_guard(name, fn)
        for tool in targets:
            hit = [e for e in found if matches(e["matcher"], tool)]
            if not hit:
                problems.append(f"{event}/{name}: nessuna entry accetta lo strumento {tool!r}")
            elif guard and all(e["if"] for e in hit):
                problems.append(
                    f"{event}/{name}: guardia raggiungibile solo dietro il filtro "
                    f"{hit[0]['if']!r} per lo strumento {tool!r}")
    return problems


# --------------------------------------------------------------------------
# Il file vero
# --------------------------------------------------------------------------
def test_hooks_json_exists_and_parses():
    hooks = load_hooks()
    assert isinstance(hooks, dict) and hooks


def test_every_event_exists_in_registry():
    orphans = unknown_events(load_hooks(), load_registry())
    assert orphans == [], f"eventi senza handler: {orphans}"


def test_every_registration_is_exec_form():
    assert exec_form_problems(load_hooks()) == []


def test_placeholders_are_the_only_paths():
    hooks = load_hooks()
    for event in [e for e in hooks if not e.startswith("_")]:
        for entry in entries(hooks, event):
            hook = entry["hook"]
            assert hook["command"] == "{{PYTHON}}"
            assert hook["args"][:2] == ["-S", "{{DISPATCH}}"]
            assert isinstance(hook.get("timeout"), int)


def test_filters_are_valid_and_on_tool_events():
    assert filter_problems(load_hooks()) == []


def test_no_tool_is_served_by_two_entries():
    assert overlap_problems(load_hooks()) == []


def test_every_registered_pair_is_covered():
    assert coverage_problems(load_hooks(), load_registry()) == []


def test_guards_are_registered_without_filters():
    """Controllo esplicito sulle guardie note: nessuna sta dietro un `if`."""
    registry = load_registry()
    hooks = load_hooks()
    names = {"write-acl", "git-policy", "commit-gate", "content-scan",
             "notebook-guard", "baseline-stash"}
    seen = set()
    for event, tools, name, fn in registry:
        if name not in names:
            continue
        seen.add(name)
        assert is_guard(name, fn), f"{name} non e' piu' riconosciuto come guardia"
        for tool in tools:
            hit = [e for e in entries(hooks, event) if matches(e["matcher"], tool)]
            assert hit, f"{name}: {tool} non e' coperto"
            assert any(e["if"] is None for e in hit), f"{name}: {tool} solo dietro un filtro"
    assert seen == names, f"guardie non piu' registrate: {sorted(names - seen)}"


# --------------------------------------------------------------------------
# Read/Glob/Grep: entrano nel matcher solo per il firewall tra profili
# --------------------------------------------------------------------------
READ_TOOLS = ("Read", "Glob", "Grep")
# Unico handler registrato senza `tools` oltre al firewall: un avviso one-shot che
# costa una sola open(), quindi puo' girare anche sugli strumenti di lettura
_TOOLLESS_OK: frozenset[str] = frozenset()


def _may_run_on_read_tools(name: str, fn) -> bool:
    # Il firewall si riconosce dal modulo, non dal nome scelto nel decoratore
    return fn.__module__.endswith(".profile_firewall") or name in _TOOLLESS_OK


def test_read_tools_reach_pretooluse_unfiltered():
    hooks = load_hooks()
    for tool in READ_TOOLS:
        hit = [e for e in entries(hooks, "PreToolUse") if matches(e["matcher"], tool)]
        assert len(hit) == 1, (tool, hit)
        assert hit[0]["if"] is None, f"{tool}: il firewall non puo' stare dietro un filtro"


# Ogni strumento nativo che esegue comandi o legge/scrive file: senza PreToolUse sarebbe
# invisibile a firewall, git-policy, content-scan e write-acl
COMMAND_AND_FILE_TOOLS = ("Bash", "PowerShell", "Monitor", "Read", "Write", "Edit", "MultiEdit",
                          "NotebookEdit", "Glob", "Grep")


@pytest.mark.parametrize("tool", COMMAND_AND_FILE_TOOLS)
def test_command_and_file_tools_reach_pretooluse_unfiltered(tool):
    hit = [e for e in entries(load_hooks(), "PreToolUse") if matches(e["matcher"], tool)]
    assert len(hit) == 1, (tool, hit)
    assert hit[0]["if"] is None, f"{tool}: una guardia non puo' stare dietro un filtro"


def test_shell_tools_include_every_command_runner():
    assert set(core.SHELL_TOOLS) == {"Bash", "PowerShell", "Monitor"}


def test_read_tools_do_not_wake_other_handlers():
    """Stesso criterio di core.dispatch: `tools` vuoto vale per ogni strumento."""
    for event, tools, name, fn in load_registry():
        if event != "PreToolUse":
            continue
        runs_on = [t for t in READ_TOOLS if not tools or t in tools]
        if runs_on:
            assert _may_run_on_read_tools(name, fn), f"{name} gira anche su {runs_on}"


@pytest.mark.parametrize("tool", READ_TOOLS)
def test_dispatch_of_read_tool_runs_only_allowed_handlers(tool, mk, monkeypatch, capsys):
    """Verifica sul dispatcher vero: ogni handler e' sostituito da un registratore."""
    called: list[str] = []
    allowed: set[str] = set()
    patched = []
    for event, tools, name, fn in load_registry():
        if _may_run_on_read_tools(name, fn):
            allowed.add(name)
        patched.append((event, tools, name, lambda ctx, n=name: called.append(n)))
    monkeypatch.setattr(core, "_REGISTRY", patched)
    assert core.dispatch(mk(tool, file_path="C:/x/y.txt", pattern="*")) == 0
    capsys.readouterr()
    assert called, "nessun handler raggiunto: il registratore non funziona"
    assert set(called) <= allowed, sorted(set(called) - allowed)


# --------------------------------------------------------------------------
# I controlli hanno denti: configurazioni sintetiche che DEVONO fallire
# --------------------------------------------------------------------------
def _write_json(tmp_path: Path, payload: dict) -> dict:
    """Passa dal testo JSON vero e proprio, come fa scripts/settings/apply_settings.py."""
    target = tmp_path / "hooks.json"
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return json.loads(target.read_text(encoding="utf-8"))


def _entry(event: str, matcher: str, cond: str | None = None, shell: bool = False) -> dict:
    hook: dict = {"type": "command", "timeout": 10}
    if shell:
        hook["command"] = '"C:/py/python.exe" -S "C:/h/dispatch.py" ' + event
    else:
        hook["command"] = "C:/py/python.exe"
        hook["args"] = ["-S", "C:/h/dispatch.py", event]
    if cond:
        hook["if"] = cond
    return {"matcher": matcher, "hooks": [hook]}


def test_shell_form_is_rejected(tmp_path):
    hooks = _write_json(tmp_path, {"PreToolUse": [_entry("PreToolUse", "Bash", shell=True)]})
    assert exec_form_problems(hooks), "la forma shell deve essere segnalata"


def test_wrong_event_argument_is_rejected(tmp_path):
    bad = _entry("PostToolUse", "Bash")
    hooks = _write_json(tmp_path, {"PreToolUse": [bad]})
    assert exec_form_problems(hooks)


def test_filter_on_a_non_tool_event_is_rejected(tmp_path):
    hooks = _write_json(tmp_path, {"SessionStart": [_entry("SessionStart", "", cond="Edit(*.tex)")]})
    assert filter_problems(hooks)


def test_malformed_filter_is_rejected(tmp_path):
    hooks = _write_json(tmp_path, {"PreToolUse": [_entry("PreToolUse", "Bash", cond="*.tex")]})
    assert filter_problems(hooks)


def test_filter_over_a_guard_is_rejected(tmp_path):
    """Una PreToolUse filtrata su Edit(*.tex) nasconderebbe write-acl."""
    hooks = _write_json(tmp_path, {
        "PreToolUse": [_entry("PreToolUse", "Bash|PowerShell|Write|Edit|MultiEdit|NotebookEdit|Agent",
                              cond="Edit(*.tex)")],
        "PostToolUse": [_entry("PostToolUse", "Agent|Write|Edit|MultiEdit")],
    })
    problems = coverage_problems(hooks, load_registry())
    assert any("write-acl" in p for p in problems), problems


def test_narrowed_matcher_is_rejected(tmp_path):
    """Togliere NotebookEdit dal matcher lascia scoperte content-scan e write-acl."""
    hooks = _write_json(tmp_path, {
        "PreToolUse": [_entry("PreToolUse", "Bash|PowerShell|Write|Edit|MultiEdit|Agent")],
        "PostToolUse": [_entry("PostToolUse", "Agent|Write|Edit|MultiEdit")],
    })
    problems = coverage_problems(hooks, load_registry())
    assert any("NotebookEdit" in p for p in problems), problems


def test_missing_event_is_rejected(tmp_path):
    hooks = _write_json(tmp_path, {"PreToolUse": [_entry("PreToolUse", "Bash|PowerShell|Write|Edit|MultiEdit|NotebookEdit|Agent")]})
    problems = coverage_problems(hooks, load_registry())
    assert any("latex-warn" in p for p in problems), problems


def test_unknown_event_is_rejected(tmp_path):
    """Un evento inventato deve essere segnalato dallo stesso controllo del file vero."""
    hooks = _write_json(tmp_path, {
        "PreToolUse": [_entry("PreToolUse", "Bash")],
        "NotAnEvent": [_entry("NotAnEvent", "")],
    })
    assert unknown_events(hooks, load_registry()) == ["NotAnEvent"]
