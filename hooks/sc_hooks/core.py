"""Nucleo condiviso degli hook superclaude (Windows nativo, solo libreria standard).

Contratto per gli handler
-------------------------
Un handler è una funzione `handle(ctx: Ctx) -> Result | None` registrata con
`@on(<evento>, tools=(...))`. Non scrive mai su stdout e non chiama mai
`sys.exit`: restituisce un `Result`. Il dispatcher aggrega i risultati di tutti
gli handler in UNA sola risposta JSON, così non esistono più le race tra hook
paralleli né gli output JSON multipli della versione bash.

Regola d'oro: un handler che solleva un'eccezione viene ignorato (fail-open) e
l'errore finisce in ~/.claude/logs/hooks.log. Un bug in una guardia non deve
mai bloccare la sessione.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

CLAUDE_HOME = Path.home() / ".claude"
STATE_DIR = CLAUDE_HOME / "state"          # marcatori per sessione (ex session-timers/)
LOG_DIR = CLAUDE_HOME / "logs"
TMP_DIR = CLAUDE_HOME / "tmp"
WORKSPACE_ROOT = Path.home() / "code"

SHELL_TOOLS = ("Bash", "PowerShell")
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

_DRIVE_MSYS = re.compile(r"^/([a-zA-Z])(/|$)")        # /c/Users/...  (Git Bash)
_DRIVE_WIN = re.compile(r"^([a-zA-Z]):[\\/]?")         # C:\Users\... o C:/Users/...
_SHORT_NAME = re.compile(r"~\d")                       # segmento 8.3, es. MARIO~1.ROS


def _expand_short_names(s: str) -> str:
    """Espande i nomi corti 8.3 risalendo al primo antenato esistente.

    `os.path.realpath` espande solo ciò che esiste: per un file ancora da creare
    si risolve la parte esistente del percorso e si riattacca il resto.
    """
    if os.name != "nt":
        return s
    probe = s.replace("/", "\\")
    m = _DRIVE_MSYS.match(s)
    if m:
        probe = f"{m.group(1)}:\\" + s[m.end():].replace("/", "\\")
    tail: list[str] = []
    try:
        while probe and not os.path.exists(probe):
            head, _, last = probe.rstrip("\\").rpartition("\\")
            if not head or head == probe:
                return s
            tail.insert(0, last)
            probe = head
        resolved = os.path.realpath(probe)
    except (OSError, ValueError):
        return s
    return "\\".join([resolved.rstrip("\\"), *tail]) if tail else resolved


# --------------------------------------------------------------------------
# Percorsi
# --------------------------------------------------------------------------
def norm_path(raw: str | os.PathLike[str] | None, cwd: str | None = None) -> str:
    """Forma canonica confrontabile: minuscolo, slash in avanti, drive `c:/...`.

    Accetta `C:\\Users\\x`, `C:/Users/x`, `/c/Users/x`, `~/x`, `$HOME/x` e
    percorsi relativi (risolti rispetto a `cwd`). NTFS è case-insensitive, quindi
    il confronto avviene sempre in minuscolo. Non tocca il filesystem, tranne che
    per espandere i nomi corti 8.3 (`C:\\Users\\MARIO~1.ROS\\...`): questa macchina
    li produce davvero e senza espansione aggirerebbero ogni regola sui percorsi.
    """
    if raw is None or isinstance(raw, (bytes, bytearray)):
        return ""
    if cwd is not None and not isinstance(cwd, str):
        cwd = None
    s = str(raw).strip().strip('"').strip("'")
    if not s:
        return ""
    if _SHORT_NAME.search(s):
        s = _expand_short_names(s)
    home = str(Path.home())
    if s == "~" or s.startswith(("~/", "~\\")):
        s = home + s[1:]
    for var in ("$HOME", "${HOME}", "$env:USERPROFILE", "%USERPROFILE%"):
        if s.startswith(var):
            s = home + s[len(var):]
    s = s.replace("\\", "/")
    m = _DRIVE_MSYS.match(s)
    if m:
        s = f"{m.group(1)}:/{s[m.end():]}"
    if not _DRIVE_WIN.match(s) and not s.startswith("//"):
        base = (cwd or os.getcwd()).replace("\\", "/")
        mb = _DRIVE_MSYS.match(base)
        if mb:
            base = f"{mb.group(1)}:/{base[mb.end():]}"
        s = f"{base.rstrip('/')}/{s}"
    # Risolve `.` e `..` senza accedere al disco
    parts: list[str] = []
    drive, _, rest = s.partition(":/") if _DRIVE_WIN.match(s) else ("", "", s)
    for seg in rest.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    out = (f"{drive}:/" if drive else "/") + "/".join(parts)
    return out.lower()


def fs_path(raw: str | os.PathLike[str] | None, cwd: str | None = None) -> Path | None:
    """Percorso utilizzabile per l'accesso ai file, con le maiuscole originali.

    `norm_path` serve per i CONFRONTI (minuscolo); questo serve per APRIRE o
    MOSTRARE un file: accetta `/c/Users/...`, `~`, percorsi relativi a `cwd`.
    """
    if raw is None or isinstance(raw, (bytes, bytearray)):
        return None
    s = str(raw).strip().strip('"').strip("'")
    if not s:
        return None
    if s == "~" or s.startswith(("~/", "~\\")):
        s = str(Path.home()) + s[1:]
    m = _DRIVE_MSYS.match(s)
    if m:
        s = f"{m.group(1).upper()}:/{s[m.end():]}"
    p = Path(s)
    if not p.is_absolute() and isinstance(cwd, str) and cwd:
        base = fs_path(cwd)
        if base is not None:
            p = base / p
    return p


def is_under(path: str | os.PathLike[str] | None, root: str | os.PathLike[str], cwd: str | None = None) -> bool:
    """True se `path` coincide con `root` o sta sotto di esso."""
    p, r = norm_path(path, cwd), norm_path(root, cwd).rstrip("/")
    return bool(p) and (p == r or p.startswith(r + "/"))


def rel_to(path: str | os.PathLike[str] | None, root: str | os.PathLike[str], cwd: str | None = None) -> str | None:
    """Percorso relativo a `root` (già normalizzato) oppure None se è fuori."""
    if not is_under(path, root, cwd):
        return None
    p, r = norm_path(path, cwd), norm_path(root, cwd).rstrip("/")
    return p[len(r):].lstrip("/")


# --------------------------------------------------------------------------
# Contesto e risultato
# --------------------------------------------------------------------------
@dataclass
class Ctx:
    event: str
    payload: dict[str, Any]
    session_id: str
    tool: str
    tool_input: dict[str, Any]
    cwd: str
    role: str            # "meta" | "worker" | "plain"
    agent: str           # nome dell'agente ("meta", "w-reviewer", ...) oppure ""

    def _str_field(self, *keys: str) -> str:
        """Primo campo stringa non vuoto tra `keys`; i tipi inattesi valgono ""."""
        for key in keys:
            val = self.tool_input.get(key)
            if isinstance(val, str) and val:
                return val
        return ""

    @property
    def file_path(self) -> str:
        return self._str_field("file_path", "notebook_path", "path")

    @property
    def command(self) -> str:
        return self._str_field("command")

    @property
    def new_content(self) -> str:
        """Testo che lo strumento sta per scrivere (Write/Edit/MultiEdit)."""
        chunks = [self._str_field("content"), self._str_field("new_string"), self._str_field("new_source")]
        edits = self.tool_input.get("edits")
        for edit in edits if isinstance(edits, (list, tuple)) else ():
            if isinstance(edit, dict) and isinstance(edit.get("new_string"), str):
                chunks.append(edit["new_string"])
        return "\n".join(c for c in chunks if c)

    def state_file(self, key: str) -> Path:
        """File di stato per (sessione, chiave), sempre DENTRO STATE_DIR/<sid>/.

        Un session_id ostile come `..` risolverebbe a ~/.claude, che la pulizia di
        fine sessione cancella: i punti iniziali vengono rimossi e un nome vuoto
        ricade su "no-session". Stessa cura per la chiave.
        """
        sid = re.sub(r"[^A-Za-z0-9_.-]", "_", self.session_id or "").lstrip(".") or "no-session"
        safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", key or "").lstrip(".") or "state"
        return STATE_DIR / sid / safe_key

    def once(self, key: str) -> bool:
        """True solo la prima volta per (sessione, chiave): per gli avvisi one-shot."""
        marker = self.state_file(f"once-{key}")
        if marker.exists():
            return False
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(int(time.time())), encoding="utf-8")
        except OSError:
            return True
        return True


@dataclass
class Result:
    block: str | None = None                 # motivo del blocco (solo PreToolUse)
    context: list[str] = field(default_factory=list)   # testo per il modello
    name: str = ""
    # Input corretto da passare allo strumento al posto dell'originale (solo
    # PreToolUse). Correggere sul posto costa molto meno di un blocco, che
    # obbliga il modello a rigenerare tutto l'output.
    updated_input: dict[str, Any] | None = None

    @staticmethod
    def deny(reason: str) -> "Result":
        return Result(block=reason)

    @staticmethod
    def rewrite(tool_input: dict[str, Any], note: str = "") -> "Result":
        return Result(updated_input=tool_input, context=[note] if note else [])

    @staticmethod
    def note(text: str) -> "Result":
        return Result(context=[text])


Handler = Callable[[Ctx], "Result | None"]
_REGISTRY: list[tuple[str, tuple[str, ...], str, Handler]] = []
_DEFAULT_MODE: dict[str, str] = {}


def on(event: str, tools: Iterable[str] = (), name: str = "",
       default_mode: str = "block") -> Callable[[Handler], Handler]:
    """Registra un handler. `tools` vuoto = tutti gli strumenti.

    `default_mode="warn"` dichiara una guardia consultiva: un suo `Result.deny`
    diventa un avviso, a meno che l'ambiente non chieda esplicitamente "block".
    """
    def deco(fn: Handler) -> Handler:
        key = name or fn.__name__
        _REGISTRY.append((event, tuple(tools), key, fn))
        if default_mode in ("warn", "block"):
            _DEFAULT_MODE.setdefault(key, default_mode)
        return fn
    return deco


def guard_mode(name: str, default: str = "block") -> str:
    """Interruttori: SUPERCLAUDE_GUARDS=off, SUPERCLAUDE_GUARD_<NOME>=off|warn|block."""
    if os.environ.get("SUPERCLAUDE_GUARDS", "").lower() == "off":
        return "off"
    key = "SUPERCLAUDE_GUARD_" + re.sub(r"[^A-Z0-9]", "_", name.upper())
    val = os.environ.get(key, "").lower()
    return val if val in ("off", "warn", "block") else default


# --------------------------------------------------------------------------
# Identità
# --------------------------------------------------------------------------
def resolve_identity(payload: dict[str, Any]) -> tuple[str, str]:
    """(ruolo, nome). Niente /proc: usa il payload, poi l'ambiente, poi il marcatore.

    - i subagent arrivano con `agent_type` (o `subagent_type`) nel payload;
    - la sessione meta è lanciata dal wrapper `meta`, che esporta CLAUDE_AGENT_NAME;
    - tutto il resto è una sessione "plain".
    """
    agent = str(payload.get("agent_type") or payload.get("subagent_type") or "").strip()
    if agent and agent != "meta":
        return "worker", agent
    env_agent = os.environ.get("CLAUDE_AGENT_NAME", "").strip()
    name = agent or env_agent
    if name == "meta":
        return "meta", "meta"
    if name:
        return "worker", name
    return "plain", ""


# --------------------------------------------------------------------------
# I/O e dispatch
# --------------------------------------------------------------------------
def log_error(where: str, exc: BaseException) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / "hooks.log").open("a", encoding="utf-8") as fh:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
            fh.write(f"{stamp} {where}: {exc!r}\n{traceback.format_exc()}\n")
    except OSError:
        pass


def read_ctx(event_arg: str | None) -> Ctx:
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw.decode("utf-8-sig")) if raw.strip() else {}
    except (ValueError, UnicodeDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    role, agent = resolve_identity(payload)
    ti = payload.get("tool_input")
    return Ctx(
        event=str(payload.get("hook_event_name") or event_arg or ""),
        payload=payload,
        session_id=str(payload.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""),
        tool=str(payload.get("tool_name") or ""),
        tool_input=ti if isinstance(ti, dict) else {},
        cwd=str(payload.get("cwd") or os.getcwd()),
        role=role,
        agent=agent,
    )


def dispatch(ctx: Ctx) -> int:
    """Esegue gli handler dell'evento e scrive UNA risposta. Ritorna l'exit code."""
    blocks: list[str] = []
    notes: list[str] = []
    rewrite_notes: list[str] = []
    rewritten = False
    for event, tools, name, fn in _REGISTRY:
        if event != ctx.event or (tools and ctx.tool not in tools):
            continue
        mode = guard_mode(name, _DEFAULT_MODE.get(name, "block"))
        if mode == "off":
            continue
        try:
            res = fn(ctx)
        except Exception as exc:  # noqa: BLE001 - fail-open per contratto
            log_error(f"{ctx.event}/{name}", exc)
            continue
        if not res:
            continue
        if isinstance(res.updated_input, dict) and ctx.event == "PreToolUse":
            # Le guardie successive devono ispezionare il testo già corretto
            ctx.tool_input = res.updated_input
            rewritten = True
            rewrite_notes.extend(res.context)
        if res.block:
            if mode == "block" and ctx.event == "PreToolUse":
                blocks.append(f"[{name}] {res.block}")
            else:
                notes.append(f"GUARD-WARN [{name}]: {res.block}")
        notes.extend(res.context)

    if blocks:
        # Exit 2 + stderr: il motivo torna al modello e lo strumento non parte
        text = "GUARD-BLOCK " + "\n".join(blocks) + "\n"
        if rewritten:
            # Il blocco scarta updatedInput: senza questa riga il modello non sa che le
            # correzioni automatiche si riapplicano da sole al nuovo tentativo
            text += ("Also: auto-fixes in this call were not applied because of the block; they reapply "
                     "automatically on retry, so change only what the block asks")
            text += (": " + "; ".join(rewrite_notes) if rewrite_notes else "") + "\n"
        sys.stderr.write(text)
        return 2
    if notes or rewritten:
        specific: dict[str, Any] = {"hookEventName": ctx.event}
        if rewritten:
            # Sempre l'input COMPLETO: vale sia se Claude Code lo sostituisce sia se lo fonde
            specific["updatedInput"] = ctx.tool_input
        if notes:
            specific["additionalContext"] = "\n\n".join(notes)
        sys.stdout.write(json.dumps({"hookSpecificOutput": specific}, ensure_ascii=False))
    return 0
