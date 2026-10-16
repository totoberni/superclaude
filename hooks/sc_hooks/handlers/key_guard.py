"""Guardia key-guard: nessun agente tocca per nome la chiave del vault della memoria.

La chiave (`<agent-memory>/.memsync.key`) cifra il mirror git della memoria: se finisce
nel transcript e' compromessa. La guardia nega, in ogni profilo e per ogni ruolo, la
chiamata in cui uno di questi campi contiene `memsync.key` (senza distinzione di maiuscole):

- `file_path`, `path`, `notebook_path` di qualunque strumento esaminato;
- `glob` di Grep, che sceglie i file da leggere;
- il comando di Bash, PowerShell e Monitor.

Non si esaminano il `pattern` di Grep e Glob (Grep cerca nel contenuto, Glob elenca solo
nomi), il testo scritto da Write/Edit/MultiEdit ne' i prompt di Agent: cercare il nome nel
codice resta possibile con lo strumento Grep.

Euristica dichiarata, non un confine di sicurezza: ferma solo chi scrive il nome intero in
un campo esaminato. Rischi residui accettati, perche' non nominano il file:
- caratteri jolly (`cat *.key`, `Get-Content .memsync.*`);
- nomi spezzati da virgolette o variabili (`".memsync"".key"`, `k=memsync; cat .$k.key`);
- nomi corti 8.3 di Windows (`MEMSYN~1.KEY`);
- `pwsh -EncodedCommand`: il comando in base64 non viene decodificato;
- strumenti MCP, che hooks.json non instrada a questa guardia (es. `paths` di un upload);
- Grep sulla cartella (con `glob` `*.key` o senza `glob`) prima che la chiave abbia
  l'intestazione con byte NUL, che fa saltare il file a rg.
La difesa vera e' altrove: quell'intestazione e la regola che nessun agente legge o stampa
una chiave.
"""
from __future__ import annotations

from sc_hooks import core
from sc_hooks.handlers import _util

NAME = "key-guard"

KEY_NAME = "memsync.key"
_PATH_FIELDS = ("file_path", "path", "notebook_path")
# Strumenti con un percorso o un comando; Agent resta fuori perche' i prompt non si esaminano
_TOOLS = core.SHELL_TOOLS + core.WRITE_TOOLS + ("Read", "Glob", "Grep")


def _names_key(value: object) -> bool:
    return isinstance(value, str) and KEY_NAME in value.lower()


def _scanned(ctx: core.Ctx) -> list[object]:
    """Valori dei campi esaminati per lo strumento della chiamata."""
    values = [_util.field(ctx, key) for key in _PATH_FIELDS]
    if ctx.tool == "Grep":
        values.append(_util.field(ctx, "glob"))
    if ctx.tool in core.SHELL_TOOLS:
        values.append(_util.field(ctx, "command"))
    return values


@core.on("PreToolUse", tools=_TOOLS, name=NAME)
def handle(ctx: core.Ctx) -> core.Result | None:
    if not any(_names_key(v) for v in _scanned(ctx)):
        return None
    return core.Result.deny(
        f"the memory-vault key ({KEY_NAME}) is owner-only: agents never read, copy, write or name it in a "
        "path or shell command. To find the name in code use the Grep tool with it in `pattern`; anything "
        "that needs the key is run by the owner in a real terminal"
    )
