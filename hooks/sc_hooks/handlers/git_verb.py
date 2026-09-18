"""Guardia git-verb: avvisi euristici sui verbi git (ex guards/40-git-verb.sh).

Solo avvisi, mai blocchi: un hook non distingue "l'utente l'ha chiesto" da
"l'agente ha deciso da solo". Per questo l'handler restituisce sempre
`Result.note`, qualunque sia la modalità configurata.

- `-C <dir>` ripetuto dentro un pathspec: dopo `-C` i percorsi sono già relativi
  alla radice del repository.
- creazione di branch (`checkout -b`, `switch -c`, `branch <nome>`) e `merge`.

Tolti rispetto a bash: il controllo sul carattere di history expansion (non
riguarda PowerShell e il bersaglio comms_db non esiste più) e il marcatore di
race tra worktree (serviva agli orch paralleli, ruolo rimosso).
"""
from __future__ import annotations

from sc_hooks import core
from sc_hooks.handlers import _util

NAME = "git-verb"

_CONFIRM = "confirm this was explicitly instructed, never automatic."


def _looks_absolute(tok: str) -> bool:
    return tok.startswith(("/", "~", "$HOME", "${HOME}", "$env:", "%USERPROFILE%")) or (len(tok) > 2 and tok[1] == ":")


def _repeats_dir(call: _util.GitCall) -> bool:
    raw_dir = call.c_dir.replace("\\", "/").rstrip("/").lower()
    if not raw_dir or raw_dir == ".":
        return False
    repo = call.repo
    for arg in call.args:
        if arg.startswith("-"):
            continue
        low = arg.replace("\\", "/").lower()
        if low == raw_dir or low.startswith(raw_dir + "/"):
            return True
        # forme diverse dello stesso percorso assoluto (C:\repo contro /c/repo)
        if _looks_absolute(arg) and _looks_absolute(call.c_dir) and core.is_under(arg, repo):
            return True
    return False


def _branch_note(call: _util.GitCall) -> str | None:
    verb, args = call.verb, call.args
    if verb == "checkout" and any(a in ("-b", "-B") for a in args):
        return f"git-verb: 'git checkout -b' creates a branch; {_CONFIRM}"
    if verb == "switch" and any(a in ("-c", "-C", "--create", "--force-create") for a in args):
        return f"git-verb: 'git switch -c' creates a branch; {_CONFIRM}"
    if verb == "branch" and args and not args[0].startswith("-"):
        return f"git-verb: 'git branch <name>' creates a branch; {_CONFIRM}"
    if verb == "merge" and not any(a in ("--abort", "--quit") for a in args):
        return "git-verb: 'git merge' changes history; confirm this was explicitly instructed."
    return None


@core.on("PreToolUse", tools=core.SHELL_TOOLS, name=NAME)
def handle(ctx: core.Ctx) -> core.Result | None:
    cmd = _util.command_of(ctx)
    if "git" not in cmd.lower():
        return None
    cwd = ctx.cwd if isinstance(ctx.cwd, str) else ""
    notes: list[str] = []
    for call in _util.git_calls(cmd, cwd):
        if _repeats_dir(call):
            notes.append(
                f"git-verb: 'git -C {call.c_dir}' repeats that directory inside a pathspec; "
                "pathspecs after -C are already relative to the repo root."
            )
        note = _branch_note(call)
        if note:
            notes.append(note)
    if not notes:
        return None
    return core.Result(context=list(dict.fromkeys(notes)))
