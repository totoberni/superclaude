"""Avviso cd-note: un cambio di directory nella shell vale per tutte le chiamate successive.

Gli strumenti Bash e PowerShell conservano la directory corrente tra una chiamata
e l'altra, quindi un `cd` di primo livello ribasa in silenzio ogni percorso
relativo usato dopo. Il proprietario non vuole bloccarlo né penalizzarlo (gli
agenti devono potersi muovere): l'handler restituisce solo `Result.note`, con la
directory di partenza e quella di arrivo, qualunque sia la modalità configurata.

- conta: `cd`, `chdir`, `pushd`, `popd`, `Set-Location`/`sl`, `Push-Location`,
  `Pop-Location` in un segmento di primo livello, anche nudi o con `-`;
- non conta: subshell `( cd x && ... )`, sostituzioni `$( ... )`, comandi dentro
  un wrapper (`bash -c "..."`, `powershell -Command "..."`);
- tace se la destinazione è la directory di partenza, la home o la radice del
  workspace: è proprio il ritorno che l'avviso chiede.
"""
from __future__ import annotations

import re

from sc_hooks import core
from sc_hooks.handlers import _util

NAME = "cd-note"

_PREVIOUS = "the previous directory"
_BACK = ("popd", "pop-location")
_PUSH = ("pushd", "push-location")
# Stesso prefisso di `_util._CD`, ma con il verbo catturato: `_CD` non riconosce il
# verbo nudo, `popd`/`Pop-Location` né argomenti in più (`cd x 2>/dev/null`).
_MOVE = re.compile(
    _util._PREFIX + r"(?P<verb>chdir|cd|pushd|popd|set-location|sl|push-location|pop-location)"
    r"(?:\s+(?P<rest>.*))?$",
    re.I | re.S,
)
_QUOTED = re.compile(r'"[^"]*"|\'[^\']*\'')
_COMMENT = re.compile(r"(?:^|(?<=\s))#[^\n]*", re.M)
_REDIRECT = re.compile(r"^&?\d*[<>]")
_UNRESOLVED = re.compile(r"[$`%]")          # variabili e sostituzioni che l'hook non sa espandere
_PROBES = ("/p/p/p/p", "/q/q/q/q")          # due basi fittizie: rivelano se un argomento dipende dalla cwd


def _first_arg(rest: str | None) -> str:
    """Primo argomento posizionale: salta opzioni (`-P`, `-Path`) e redirezioni, non il `-` di `cd -`."""
    for tok in _util.tokens(rest or ""):
        if tok == "-" or not (tok.startswith("-") or _REDIRECT.match(tok)):
            return tok
    return ""


def _is_absolute(arg: str) -> bool:
    # se il risultato non cambia con due basi diverse, l'argomento non dipende dalla cwd
    return core.norm_path(arg, _PROBES[0]) == core.norm_path(arg, _PROBES[1])


class _Shell:
    """Directory corrente stimata lungo il comando, con OLDPWD e pila di pushd.

    `here` vale None quando il testo del comando non basta a dedurla (`cd -` senza
    storia, `popd` senza `pushd`, variabili): in quel caso si mostra `label`.
    """

    def __init__(self, start: str) -> None:
        self.here: str | None = start
        self.label = start
        self.prev: str | None = None         # OLDPWD prima del comando: ignota
        self.stack: list[str | None] = []

    def _resolve(self, arg: str) -> tuple[str | None, str]:
        if self.here is None and not _is_absolute(arg):
            return None, f"{self.label}/{arg}"
        path = core.norm_path(arg, self.here)
        if _UNRESOLVED.search(path):
            return None, arg
        return path, path

    def move(self, verb: str, arg: str) -> None:
        verb = verb.lower()
        if verb in _BACK:
            target, label = (self.stack.pop() if self.stack else None), _PREVIOUS
        elif arg == "-" or (verb in _PUSH and not arg):
            # `cd -` torna a OLDPWD; `pushd` nudo scambia le prime due voci della pila
            target, label = (self.prev if arg == "-" else None), _PREVIOUS
        elif not arg:
            target = label = core.norm_path("~")
        else:
            target, label = self._resolve(arg)
            if verb in _PUSH:
                self.stack.append(self.here)
        self.prev, self.here = self.here, target
        self.label = label if target is None else target


def _paren_balance(seg: str) -> int:
    bare = _QUOTED.sub("", seg)
    return bare.count("(") - bare.count(")")


def _final_shell(cmd: str, start: str) -> _Shell | None:
    """Stato dopo i cambi di directory persistenti; None se il comando non ne contiene."""
    shell, moved, depth = _Shell(start), False, 0
    for seg in _util.segments(cmd):
        balance = _paren_balance(seg)
        # dentro `( ... )` o `$( ... )` il cambio vale solo per la subshell
        top = depth == 0 and balance <= 0 and not seg.startswith("(")
        depth = max(0, depth + balance)
        m = _MOVE.match(seg) if top else None
        if m:
            shell.move(m.group("verb"), _first_arg(m.group("rest")))
            moved = True
    return shell if moved else None


def _quotes_balanced(cmd: str) -> bool:
    # una stringa non chiusa è un errore di sintassi: la shell non esegue nulla.
    # L'apostrofo di un commento (`# it's`) non apre una stringa.
    return not re.search(r"[\"']", _COMMENT.sub("", _QUOTED.sub("", cmd)))


def _note(start: str, to: str) -> str:
    back = f'"{start}"' if " " in start else start
    text = (
        f"cd-note: this command moves the shell's working directory for every later call: {start} -> {to}. "
        f"Keep track of it: use absolute paths afterwards, or move back with `cd {back}` when done; "
        "`( cd <dir> && ... )` runs without moving."
    )
    return re.sub(r"\s*[\r\n]+\s*", " ", text)


@core.on("PreToolUse", tools=core.SHELL_TOOLS, name=NAME, default_mode="warn")
def handle(ctx: core.Ctx) -> core.Result | None:
    # Monitor gira in un processo a parte: un suo cd non sposta la shell della sessione
    if ctx.tool == "Monitor":
        return None
    cmd = _util.command_of(ctx)
    start = core.norm_path(ctx.cwd) if isinstance(ctx.cwd, str) else ""
    if not start or not _quotes_balanced(cmd):
        return None
    shell = _final_shell(cmd, start)
    restore = {start, core.norm_path("~"), core.norm_path(core.WORKSPACE_ROOT)}
    if shell is None or shell.here in restore:
        return None
    return core.Result.note(_note(start, shell.label))
