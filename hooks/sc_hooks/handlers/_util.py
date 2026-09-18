"""Utilità condivise dalle guardie sui comandi shell (Bash e PowerShell).

Niente grammatica di shell: solo euristiche leggibili. Queste guardie sono dossi
rallentatori contro le abitudini di un agente, non un confine di sicurezza.
Il modulo non registra alcun handler: il dispatcher lo importa e basta.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from sc_hooks import core

MAX_CMD = 100_000            # oltre questa soglia il comando viene troncato (anti-DoS)

_CONTINUATION = re.compile(r"[\\`]\r?\n")                 # `\`+LF (bash) e backtick+LF (PowerShell)
_NAIVE_SPLIT = re.compile(r"&&|\|\||[;|&\n]")
_TOKEN = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')
_WORD = r'(?:"[^"]*"|\'[^\']*\'|\S+)'

# Prefissi innocui davanti al comando vero: parole chiave, `&` di PowerShell, VAR=val, env/sudo.
_PREFIX = r"""^\s*(?:(?:then|do|else|elif|if|while|time|!|\(|\{)\s+)*(?:&\s*)?(?:(?:env|command|sudo|exec|nohup)\s+)*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"""
_GIT_EXE = r"""(?:"[^"]*[\\/]git(?:\.exe)?"|'[^']*[\\/]git(?:\.exe)?'|(?:[^\s"']*[\\/])?git(?:\.exe)?)"""
_GIT_GLOBALS = rf"""(?P<globals>(?:\s+(?:-[Cc]\s+{_WORD}|--[a-z][a-z-]*(?:=(?:"[^"]*"|'[^']*'|\S+))?|-[pP]))*)"""
_GIT_CALL = re.compile(_PREFIX + _GIT_EXE + _GIT_GLOBALS + r"\s+(?P<verb>[a-z][a-z-]*)(?P<rest>.*)$", re.S)
_DASH_C = re.compile(rf"-C\s+({_WORD})")
_GH_CALL = re.compile(_PREFIX + r"""(?:[^\s"']*[\\/])?gh(?:\.exe)?\s+(?P<rest>.*)$""", re.S)
_WRAPPER = re.compile(
    _PREFIX + r"""(?:[^\s"']*[\\/])?(?:bash|sh|zsh|pwsh|powershell|cmd|wsl|eval|iex|invoke-expression)(?:\.exe)?\b(?P<rest>.*)$""",
    re.S | re.I,
)
_CD = re.compile(_PREFIX + rf"(?:cd|chdir|pushd|set-location|sl|push-location)\s+(?:-(?:literal)?path\s+)?(?P<dir>{_WORD})\s*$", re.I)


@dataclass
class GitCall:
    verb: str                # "commit", "push", ...
    args: list[str]          # token dopo il verbo, senza virgolette esterne
    c_dir: str               # valore grezzo di `-C <dir>` oppure ""
    cwd: str                 # directory corrente stimata (segue i `cd` precedenti)
    segment: str             # testo del segmento, per i messaggi

    @property
    def repo(self) -> str:
        """Directory del repository in forma canonica (`-C` risolto rispetto a cwd)."""
        return core.norm_path(self.c_dir or ".", self.cwd)


def unquote(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
        return tok[1:-1]
    return tok


def tokens(text: str) -> list[str]:
    return [unquote(t) for t in _TOKEN.findall(text or "")]


def clean(cmd: object) -> str:
    """Comando come stringa, con le continuazioni di riga ricucite."""
    if not isinstance(cmd, str):
        return ""
    return _CONTINUATION.sub(" ", cmd[:MAX_CMD])


def segments(cmd: object) -> list[str]:
    """Spezza sui separatori `;`, `&&`, `||`, `|`, `&`, a capo, rispettando le virgolette.

    Se le virgolette risultano sbilanciate (heredoc con apostrofi, ecc.) ripiega
    sullo split ingenuo: meglio qualche falso positivo che un comando invisibile.
    """
    text = clean(cmd)
    if not text.strip():
        return []
    out: list[str] = []
    buf: list[str] = []
    quote = ""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(text[i:i + 2])
                i += 2
                continue
            if ch == quote:
                quote = ""
            buf.append(ch)
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in ";|&\n":
            # `>|` e `>&` sono redirezioni, non separatori
            if ch in "|&" and buf and buf[-1] == ">":
                buf.append(ch)
            elif ch == "&" and i + 1 < n and text[i + 1] == ">":
                buf.append(ch)
            else:
                out.append("".join(buf))
                buf = []
                if text[i:i + 2] in ("&&", "||"):
                    i += 1
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf))
    if quote:
        out = _NAIVE_SPLIT.split(text)
    return [s for s in (seg.strip() for seg in out) if s]


def _inner_commands(seg: str) -> list[str]:
    """Stringhe tra virgolette passate a bash -c / powershell -Command / eval."""
    m = _WRAPPER.match(seg)
    if not m:
        return []
    return [unquote(t) for t in _TOKEN.findall(m.group("rest")) if t[:1] in "\"'" and len(t) > 2]


def walk(cmd: object, cwd: str = "", _depth: int = 0):
    """Genera coppie (segmento, cwd stimata), entrando di un livello nei wrapper."""
    here = cwd
    for seg in segments(cmd):
        yield seg, here
        m = _CD.match(seg)
        if m:
            here = core.norm_path(unquote(m.group("dir")), here or None)
        if _depth < 2:
            for inner in _inner_commands(seg):
                yield from walk(inner, here, _depth + 1)


def git_calls(cmd: object, cwd: str = "") -> list[GitCall]:
    calls: list[GitCall] = []
    for seg, here in walk(cmd, cwd):
        m = _GIT_CALL.match(seg)
        if not m:
            continue
        dm = _DASH_C.search(m.group("globals") or "")
        calls.append(GitCall(
            verb=m.group("verb"),
            args=tokens(m.group("rest")),
            c_dir=unquote(dm.group(1)) if dm else "",
            cwd=here,
            segment=seg,
        ))
    return calls


def gh_calls(cmd: object) -> list[list[str]]:
    """Argomenti di ogni invocazione di `gh` (es. ["pr", "merge", "12"])."""
    found: list[list[str]] = []
    for seg, _ in walk(cmd):
        m = _GH_CALL.match(seg)
        if m:
            found.append(tokens(m.group("rest")))
    return found


_SHORT_NAME = re.compile(r"~\d")


def canon(raw: object, cwd: str | None = None) -> str:
    """`core.norm_path` più l'espansione dei nomi corti 8.3 (`MARIO~1.ROS`).

    Tocca il disco solo quando il percorso contiene davvero un nome corto.
    """
    if not isinstance(raw, (str, os.PathLike)):
        return ""
    p = core.norm_path(raw, cwd if isinstance(cwd, str) and cwd else None)
    if p and _SHORT_NAME.search(p):
        try:
            p = core.norm_path(os.path.realpath(p))
        except (OSError, ValueError):
            pass
    return p


def under(path: str, root: object) -> bool:
    """Come `core.is_under`, ma con `path` già canonico e `root` espanso con `canon`."""
    r = canon(root).rstrip("/")
    return bool(path) and bool(r) and (path == r or path.startswith(r + "/"))


def rel(path: str, root: object) -> str | None:
    r = canon(root).rstrip("/")
    if not under(path, root):
        return None
    return path[len(r):].lstrip("/")


# --------------------------------------------------------------------------
# Accesso difensivo ai campi del payload
# --------------------------------------------------------------------------
def field(ctx: core.Ctx, key: str) -> object:
    ti = ctx.tool_input
    return ti.get(key) if isinstance(ti, dict) else None


def path_of(ctx: core.Ctx) -> str:
    """Percorso bersaglio dello strumento di scrittura, "" se assente o non stringa."""
    for key in ("file_path", "notebook_path", "path"):
        val = field(ctx, key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def command_of(ctx: core.Ctx) -> str:
    return clean(field(ctx, "command"))


def new_text(ctx: core.Ctx) -> str:
    """Come `ctx.new_content`, ma non solleva mai su campi di tipo sbagliato."""
    try:
        return ctx.new_content
    except Exception:  # noqa: BLE001 - payload malformato: niente da analizzare
        return ""
