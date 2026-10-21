"""Guardia wait: impedisce agli agenti di restare appesi ad aspettare.

Il comando si esamina per segmento (separatori `;`, `&&`, `||`, `|`, `&`, a capo,
parentesi) dopo aver oscurato le stringhe tra virgolette, i corpi di heredoc e di
here-string e i commenti: un `time.sleep(60)` in un heredoc Python, un
`grep "tail -f"` o un `echo "sleep 99"` non contano.

- RISCRITTURA sul posto, quando la correzione e' meccanica:
  `tail -f|-F|--follow` perde la f (con `-n 200` se manca un conteggio);
  `Get-Content ... -Wait` perde `-Wait` (con `-Tail 200` se manca un conteggio);
  le parole di un `python [-I] -` nudo, senza heredoc, senza `<` e non a destra di
  una pipe, diventano `true` (`$null` in PowerShell): separatori e redirezioni restano.
- BLOCCO (`GUARD-BLOCK [wait]`), quando il comando aspetta per costruzione:
  cicli until/while con sleep, cicli for con sleep e break/exit 0 (attesa finche'
  qualcosa compare), sleep/Start-Sleep/`timeout /t` da 30 s in su, find sulla
  radice di un disco (oltre 2 livelli) o sulla home (oltre 4), watch, Wait-Process
  senza -Timeout, `tail -f | grep -m1 X` e simili (attesa di un marcatore). Dentro
  una shell annidata (`bash -c "..."`) anche le forme da riscrivere bloccano: li' la
  riscrittura dovrebbe passare per due livelli di quoting e non e' meccanica.
- Monitor: vietato a ogni subagent (le attese lunghe spettano a meta), libero per
  la sessione principale (meta o normale).

Non bloccano: i segmenti che invocano job.py; i comandi in background (`&`, gruppi
`( ... ) &` anche con redirezioni), dove tail e Get-Content restano anche intatti;
i comandi sotto un `timeout` di GNU sotto i 30 s; per meta e per le sessioni
normali, i comandi lanciati con run_in_background, che non si riscrivono affatto.
Errori interni: il dispatcher li registra nel log e lascia passare (fail-open).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from sc_hooks import core
from sc_hooks.handlers import _util

NAME = "wait"

_LIMIT = 30.0                 # secondi: un'attesa singola da qui in su si blocca
_TAIL_LINES = "-n 200"
_GC_TAIL = "-Tail 200"
_MAX_DEPTH = 2                # livelli di shell annidate esaminati
# find con -maxdepth fino a questi livelli resta sotto il secondo (misurato su questa macchina:
# home a profondita' 4 = 1 s, radice del disco a profondita' 3 = 8 s)
_MAXDEPTH_ROOT = 2
_MAXDEPTH_HOME = 4

_JOB_HINT = ("use `scpy job.py wait <id> --max 540` (returns on exit, crash, marker or stall) after "
             "`scpy job.py start --name <id> -- <cmd>`; meta can instead launch with Bash "
             "run_in_background and get an exit notification")
_LOOP_ESCAPE = "for a deliberately short bounded loop, wrap it in `timeout 25 bash -c '...'`"
_FIND_HINT = "scope the path or use the Glob tool"
_MONITOR_MSG = ("Monitor is reserved for the main session (meta): long waits are not a subagent's job. "
                "Do not wait here; hand back with the exact launch command (for example "
                "`scpy job.py start --name <id> -- <cmd>`) and meta will wait on it")
# Tipi di blocco che sono attese (cadono in background); "loop" aggiunge la via di fuga per i cicli brevi
_WAIT_KINDS = frozenset(("wait", "loop"))

_ESCAPE = {"bash": "\\", "ps": "`", "cmd": "^"}
# Operatore heredoc di Bash: `<<` o `<<-`, delimitatore tra apici, tra virgolette o nudo
_HEREDOC_OP = re.compile(r"""<<(-?)[ \t]*(?:'([^'\n]*)'|"([^"\n]*)"|([\w.-]+))""")
# `#` apre un commento solo a inizio parola
_WORD_START = " \t\r\n;&|(){}"
_LIST_SEPS = ("&&", "||", "|", "|&")
# Parole davanti al comando vero che non cambiano cosa viene eseguito
_PREFIX_WORDS = frozenset(("then", "do", "else", "elif", "if", "while", "until", "time", "!", "{", "}", "&",
                           "exec", "command", "builtin", "nohup", "sudo", "nice", "stdbuf", "coproc"))
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
_DURATION = re.compile(r"^(\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?([smhd]?)$", re.I)
_UNITS = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_PY_NAME = re.compile(r"^(?:python(?:3(?:\.\d+)?)?|py)$")
_REDIRECT_OUT = re.compile(r"^(?:\d*>>?|&>>?|\d*>&)(?P<target>.*)$")
# Partenze di find troppo larghe: radice MSYS, radice di un disco e cartella Users; poi la home
_ROOT_START = re.compile(r"(?:|/[a-z]|[a-z]:|(?:/[a-z]|[a-z]:)/users)")
_HOME_START = re.compile(r"(?:~|\$home|\$\{home\}|\$userprofile|\$\{userprofile\}|(?:/[a-z]|[a-z]:)/users/[^/]+)")
# Redirezioni ammesse tra la parentesi che chiude un gruppo e il suo `&`
_BG_REDIRECT = re.compile(r"[ \t]*(?:&>>?|\d*>>?|\d*<)[ \t]*(?:&\d+|&-|[^\s;&|()<>]+)")
# Consumatori di una pipe che escono al primo riscontro: `tail -f | grep -m1 X` aspetta un marcatore
_GREP_EXITS = re.compile(r"^(?:-[A-Za-z]*[qm]|--quiet$|--silent$|--max-count)")
_SED_QUIT = re.compile(r"(?:^|[;{}/\s])[qQ](?:\s*\d+)?\s*(?:$|[;}])")
_TAIL_ARG_OPTS = frozenset(("-n", "-c", "-s", "--lines", "--bytes", "--sleep-interval", "--pid",
                            "--max-unchanged-stats"))
_GC_NAMES = frozenset(("get-content", "gc", "cat", "type"))
_BASH_SHELLS = frozenset(("bash", "sh", "zsh", "dash", "ksh"))
_PS_SHELLS = frozenset(("pwsh", "powershell"))
# Cicli di PowerShell: parola chiave seguita dalla condizione o dal blocco
_PS_LOOP = re.compile(r"(?<![\w$.-])(while|for|foreach|do)(?![\w-])\s*", re.I)
_PS_FOREACH_OBJ = re.compile(r"(?<![\w-])(?:foreach-object|%)\s*(?:-process\s*)?(?=\{)", re.I)
# Solo in posizione di comando: `.Timeout`, `.Sleep` e le parole qualsiasi non contano
_PS_SLEEP = re.compile(r"(?:^|[;{(|&\n])\s*(?:start-sleep|sleep)(?![\w-])", re.I)
_PS_BREAK = re.compile(r"(?<![\w$-])(?:break|exit\s+0)(?![\w-])", re.I)


# --------------------------------------------------------------------------
# Oscuramento di testi e commenti (le posizioni restano quelle del comando)
# --------------------------------------------------------------------------
@dataclass
class _Mask:
    """Copia di `cmd` lunga uguale con testi e commenti oscurati.

    Il contenuto tra virgolette diventa `_` (la parola resta una sola); corpi di
    heredoc e di here-string e commenti diventano spazi, con gli a capo conservati.
    """
    cmd: str
    mode: str                                   # "bash" | "ps" | "cmd"
    out: list[str] = field(init=False)
    pending: list[tuple[str, bool]] = field(default_factory=list)   # heredoc: (delimitatore, `<<-`)

    def __post_init__(self) -> None:
        self.out = list(self.cmd)

    def run(self) -> str:
        i = 0
        while i < len(self.cmd):
            i = self._step(i)
        return "".join(self.out)

    def _fill(self, a: int, b: int, ch: str, keep_newlines: bool) -> None:
        for k in range(a, min(b, len(self.cmd))):
            if not (keep_newlines and self.out[k] == "\n"):
                self.out[k] = ch

    def _step(self, i: int) -> int:
        cmd, c = self.cmd, self.cmd[i]
        if c == "\n":
            return self._heredoc_bodies(i + 1) if self.pending else i + 1
        if c == _ESCAPE[self.mode]:
            # carattere escapato o continuazione di riga: mai separatore, mai virgoletta
            nxt = cmd[i + 1:i + 3]
            width = 3 if nxt == "\r\n" else 2
            self._fill(i, i + width, " " if nxt[:1] in ("\n", "\r") else "_", False)
            return i + width
        if c == "'" and self.mode != "cmd":
            return self._single_quote(i)
        if c == '"':
            return self._double_quote(i)
        if c == "#" and self.mode != "cmd" and (i == 0 or cmd[i - 1] in _WORD_START):
            end = _line_end(cmd, i)
            self._fill(i, end, " ", True)
            return end
        if self.mode == "ps":
            return self._ps_step(i)
        if self.mode == "bash" and cmd.startswith("<<", i):
            return self._heredoc_open(i)
        return i + 1

    def _single_quote(self, i: int) -> int:
        ansi = self.mode == "bash" and i > 0 and self.cmd[i - 1] == "$"     # $'...' accetta escape
        j = i + 1
        while j < len(self.cmd) and self.cmd[j] != "'":
            j += 2 if ansi and self.cmd[j] == "\\" else 1
        self._fill(i + 1, j, "_", False)
        return j + 1

    def _double_quote(self, i: int) -> int:
        esc = {"bash": "\\", "ps": "`"}.get(self.mode)
        j = text_start = i + 1
        while j < len(self.cmd):
            ch = self.cmd[j]
            if ch == esc:
                j += 2
                continue
            if self.mode == "bash" and self.cmd.startswith("$(", j):
                # `"$(cat <<'EOF' ... EOF)"`: dentro la sostituzione valgono le regole del codice,
                # cosi' un `"` nel corpo dell'heredoc non inverte le virgolette del resto
                self._fill(text_start, j, "_", False)
                j = text_start = self._code_until_paren(j + 2)
                continue
            if ch == '"':
                if self.mode == "ps" and self.cmd[j + 1:j + 2] == '"':      # "" di PowerShell
                    j += 2
                    continue
                break
            j += 1
        self._fill(text_start, j, "_", False)
        return j + 1

    def _code_until_paren(self, i: int) -> int:
        """Codice di una sostituzione `$( ... )` fino alla `)` che la chiude; indice dopo di essa."""
        depth = 0
        while i < len(self.cmd):
            c = self.cmd[i]
            if c == ")" and depth == 0:
                return i + 1
            if c in "()":
                depth += 1 if c == "(" else -1
                i += 1
            else:
                i = self._step(i)
        return i

    def _ps_step(self, i: int) -> int:
        cmd = self.cmd
        if cmd.startswith("<#", i):
            end = cmd.find("#>", i + 2)
            end = len(cmd) if end < 0 else end + 2
            self._fill(i, end, " ", True)
            return end
        if cmd.startswith(("@'", '@"'), i) and not cmd[i + 2:_line_end(cmd, i)].strip():
            quote, nl = cmd[i + 1], _line_end(cmd, i)
            close = cmd.find("\n" + quote + "@", nl)
            end = len(cmd) if close < 0 else close + 1
            self._fill(nl, end, " ", True)
            return len(cmd) if close < 0 else close + 3
        return i + 1

    def _heredoc_open(self, i: int) -> int:
        if self.cmd.startswith("<<<", i):        # here-string di Bash: la parola che segue e' una parola
            return i + 3
        m = _HEREDOC_OP.match(self.cmd, i)
        if m is None:
            return i + 2
        delim = next(g for g in m.group(2, 3, 4) if g is not None)
        self.pending.append((delim, m.group(1) == "-"))
        return m.end()

    def _heredoc_bodies(self, i: int) -> int:
        """`i` all'inizio del primo corpo: oscura corpi e righe delimitatrici."""
        cmd = self.cmd
        for delim, strip_tabs in self.pending:
            start = i
            while i < len(cmd):
                end = _line_end(cmd, i)
                line = cmd[i:end].rstrip("\r")
                i = end + 1
                if (line.lstrip("\t") if strip_tabs else line) == delim:
                    break
            self._fill(start, i, " ", True)
        self.pending.clear()
        return min(i, len(cmd))


def _line_end(cmd: str, i: int) -> int:
    j = cmd.find("\n", i)
    return len(cmd) if j < 0 else j


# --------------------------------------------------------------------------
# Segmenti
# --------------------------------------------------------------------------
@dataclass
class _Seg:
    start: int
    end: int
    before: str                          # separatore prima ("" all'inizio)
    after: str                           # separatore dopo ("" alla fine)
    before_pos: int                      # posizione del separatore prima (-1 se manca)
    after_pos: int                       # posizione del separatore dopo (len se manca)
    words: list[tuple[int, int]] = field(default_factory=list)


def _sep_at(masked: str, i: int, mode: str) -> str:
    """Separatore che inizia in `i` ("" se nessuno). Le redirezioni `>&`, `&>`, `>|` non lo sono."""
    two, c = masked[i:i + 2], masked[i]
    if two in ("&&", "||") or (mode == "bash" and two == "|&"):
        return two
    if c in ";\n()":
        return c
    if c == "|":
        return "" if i > 0 and masked[i - 1] == ">" else c
    if c == "&":
        if mode == "ps" or (i > 0 and masked[i - 1] in "<>") or masked[i + 1:i + 2] == ">":
            return ""                    # in PowerShell `&` e' l'operatore di chiamata
        return c
    if c in "{}":
        if mode == "ps":
            return "" if c == "{" and i > 0 and masked[i - 1] in "$@" else c
        # in Bash le graffe di gruppo sono parole a se'; `${HOME}` e `{a,b}` no
        alone = " \t\r\n;&|()"
        if (i == 0 or masked[i - 1] in alone) and (i + 1 >= len(masked) or masked[i + 1] in alone):
            return c
    return ""


def _split(masked: str, mode: str) -> list[_Seg]:
    segs: list[_Seg] = []
    start, before, before_pos, i = 0, "", -1, 0
    while i < len(masked):
        sep = _sep_at(masked, i, mode)
        if not sep:
            i += 1
            continue
        _add_seg(segs, masked, (start, i), (before, before_pos), (sep, i))
        before, before_pos, i = sep, i, i + len(sep)
        start = i
    _add_seg(segs, masked, (start, len(masked)), (before, before_pos), ("", len(masked)))
    return segs


def _add_seg(segs: list[_Seg], masked: str, span: tuple[int, int],
             before: tuple[str, int], after: tuple[str, int]) -> None:
    a, b = span
    words = [(a + m.start(), a + m.end()) for m in re.finditer(r"\S+", masked[a:b])]
    if words:
        segs.append(_Seg(words[0][0], words[-1][1], before[0], after[0], before[1], after[1], words))


def _groups(masked: str, mode: str) -> list[tuple[int, int]]:
    """Coppie (apertura, chiusura) di `( ... )` e `{ ...; }` (solo Bash)."""
    if mode != "bash":
        return []
    stack: list[int] = []
    pairs: list[tuple[int, int]] = []
    for i, ch in enumerate(masked):
        if ch in "({" and _sep_at(masked, i, mode) == ch:
            stack.append(i)
        elif ch in ")}" and _sep_at(masked, i, mode) == ch and stack:
            pairs.append((stack.pop(), i))
    return pairs


def _bg_after(masked: str, close: int) -> bool:
    i = close + 1
    while (m := _BG_REDIRECT.match(masked, i)) is not None:
        i = m.end()                      # `( ... ) > log 2>&1 &`: le redirezioni non contano
    rest = masked[i:].lstrip(" \t")
    return rest[:1] == "&" and rest[1:2] not in ("&", ">")


# --------------------------------------------------------------------------
# Un livello di analisi (il comando dello strumento o il testo di `bash -c`)
# --------------------------------------------------------------------------
@dataclass
class _Found:
    blocks: list[tuple[str, str]] = field(default_factory=list)     # (tipo, motivo): wait | find | fix
    edits: list[tuple[int, int, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class _Cmd:
    cmd: str
    mode: str
    depth: int
    masked: str = field(init=False)
    segs: list[_Seg] = field(init=False)
    groups: list[tuple[int, int]] = field(init=False)

    def __post_init__(self) -> None:
        self.masked = _Mask(self.cmd, self.mode).run()
        self.segs = _split(self.masked, self.mode)
        self.groups = _groups(self.masked, self.mode)

    def orig(self, seg: _Seg) -> list[str]:
        return [self.cmd[a:b] for a, b in seg.words]

    def mk(self, seg: _Seg) -> list[str]:
        return [self.masked[a:b] for a, b in seg.words]

    def text(self, a: int, b: int) -> str:
        return _shown(self.cmd[a:b])

    def backgrounded(self, k: int) -> bool:
        """Vero se il segmento `k` gira in background (`... &` o dentro `( ... ) &`)."""
        if self.mode != "bash":
            return False
        j = k
        while j < len(self.segs) - 1 and self.segs[j].after in _LIST_SEPS:
            j += 1
        if self.segs[j].after == "&":
            return True
        pos = self.segs[k].start
        return any(a < pos < b and _bg_after(self.masked, b) for a, b in self.groups)


@dataclass
class _Loop:
    kind: str                    # "while" (anche until) | "for" (anche select e while read)
    seg: int
    body: bool = False
    sleeps: bool = False
    brk: bool = False


@dataclass
class _Head:
    keywords: list[str]
    idx: int | None              # indice della parola di comando, None se manca
    bound: float | None = None   # durata di un `timeout` di GNU davanti al comando


def _head(c: _Cmd, seg: _Seg) -> _Head:
    """Parole chiave e prefissi davanti al comando vero (il confronto usa il testo oscurato)."""
    mk, kws, i = c.mk(seg), [], 0
    while i < len(mk):
        w = mk[i]
        if w in ("for", "select") and c.mode == "bash":
            kws.append(w)
            return _Head(kws, None)
        if w in _PREFIX_WORDS:
            kws.append(w)
            i += 1
        elif w == "env":
            i += 1
            while i < len(mk) and mk[i].startswith("-"):
                i += 1
        elif _ASSIGN.match(w):
            i += 1
        elif w == "timeout" and c.mode == "bash" and (nxt := _gnu_timeout(mk, i)) is not None:
            return _Head(kws, nxt[0] if nxt[0] < len(mk) else None, nxt[1])
        else:
            return _Head(kws, i)
    return _Head(kws, None)


def _gnu_timeout(mk: list[str], i: int) -> tuple[int, float] | None:
    """(indice del comando, durata) dopo `timeout [opzioni] DURATA`; None se non e' il timeout di GNU."""
    j = i + 1
    while j < len(mk) and mk[j].startswith("-"):
        j += 2 if mk[j] in ("-s", "-k", "--signal", "--kill-after") else 1
    secs = _gnu_seconds(mk[j]) if j < len(mk) else None
    return None if secs is None else (j + 1, secs)


def _base(tok: str) -> str:
    """Nome del comando: senza virgolette, senza cartella, minuscolo, senza `.exe`."""
    name = _util.unquote(tok).replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _shown(text: str, limit: int = 80) -> str:
    flat = re.sub(r"\s+", " ", text).strip().replace("`", "'")
    return flat if len(flat) <= limit else flat[:limit - 3] + "..."


def _analyze(cmd: str, mode: str, depth: int, found: _Found) -> None:
    c = _Cmd(cmd, mode, depth)
    loops: list[_Loop] = []
    for k, seg in enumerate(c.segs):
        head = _head(c, seg)
        if mode == "bash":
            _loop_keywords(loops, head.keywords, k)
        if head.idx is None:
            continue
        orig = c.orig(seg)
        name = _base(orig[head.idx])
        if mode == "bash" and _loop_command(c, loops, k, head, name, found):
            continue
        if any(_base(t) == "job.py" for t in orig):
            continue                     # l'aiutante delle attese e' la via giusta
        if head.bound is not None and head.bound < _LIMIT:
            continue                     # sotto un timeout breve il comando non puo' appendersi
        _check(c, k, head, name, found)
    if mode == "ps":
        _ps_loops(c, found)


# --------------------------------------------------------------------------
# Cicli di Bash: until/while con sleep; for con sleep e break/exit 0
# --------------------------------------------------------------------------
def _loop_keywords(loops: list[_Loop], keywords: list[str], k: int) -> None:
    for kw in keywords:
        if kw in ("while", "until"):
            loops.append(_Loop("while", k))
        elif kw in ("for", "select"):
            loops.append(_Loop("for", k))
        elif kw == "do" and loops:
            loops[-1].body = True


def _loop_command(c: _Cmd, loops: list[_Loop], k: int, head: _Head, name: str, found: _Found) -> bool:
    """Aggiorna i cicli aperti con il comando `name`; True se il segmento chiude un ciclo."""
    args = c.mk(c.segs[k])[head.idx + 1:]
    if name == "done" and c.mk(c.segs[k])[head.idx] == "done":
        if loops:
            _close_loop(c, loops, k, found)
        return True
    if not loops:
        return False
    top = loops[-1]
    if name == "read" and head.keywords[-1:] == ["while"] and top.seg == k and not top.body:
        top.kind = "for"                 # `while read` scorre righe: si giudica come un for
    if name in ("sleep", "start-sleep") or (name == "timeout" and args[:1] in (["/t"], ["/T"])):
        top.sleeps = True
    elif name == "break":
        top.brk = True
    elif name == "exit" and args[:1] == ["0"]:
        for loop in loops:
            loop.brk = True
    return False


def _close_loop(c: _Cmd, loops: list[_Loop], k: int, found: _Found) -> None:
    loop = loops.pop()
    if loops:
        loops[-1].sleeps |= loop.sleeps
    if not loop.sleeps or (loop.kind == "for" and not loop.brk):
        return                           # un for con sleep e senza uscita e' solo un ritmo
    if c.backgrounded(k) or c.backgrounded(loop.seg):
        return
    what = ("`until`/`while` loop with sleep" if loop.kind == "while"
            else "`for` loop that sleeps until a check breaks out")
    found.blocks.append(("loop", f"{what} polls and keeps you waiting "
                                 f"(`{c.text(c.segs[loop.seg].start, c.segs[k].end)}`)"))


def _ps_loops(c: _Cmd, found: _Found) -> None:
    """while/do/for/foreach di PowerShell e ForEach-Object: stesse regole dei cicli di Bash."""
    m_text = c.masked
    for m in _PS_LOOP.finditer(m_text):
        span = _ps_loop_body(m_text, m.group(1).lower(), m.end())
        if span is not None:
            kind = "while" if m.group(1).lower() in ("while", "do") else "for"
            _judge_ps_body(c, kind, m.start(), span, found)
    for m in _PS_FOREACH_OBJ.finditer(m_text):
        close = _match(m_text, m.end(), "{", "}")
        if close > 0:
            _judge_ps_body(c, "for", m.start(), (m.end() + 1, close), found)


def _ps_loop_body(text: str, kw: str, i: int) -> tuple[int, int] | None:
    """(inizio, fine) del corpo del ciclo che parte in `i` (dopo la parola chiave)."""
    if kw == "do":
        if text[i:i + 1] != "{":
            return None
        close = _match(text, i, "{", "}")
        after = text[close + 1:].lstrip() if close > 0 else ""
        return (i + 1, close) if re.match(r"(?i)(while|until)\b", after) else None
    if text[i:i + 1] != "(":
        return None
    close = _match(text, i, "(", ")")
    if close < 0:
        return None
    j = close + 1
    while j < len(text) and text[j] in " \t\r\n":
        j += 1
    if text[j:j + 1] != "{":
        return None
    end = _match(text, j, "{", "}")
    return (j + 1, end) if end > 0 else None


def _match(text: str, i: int, open_ch: str, close_ch: str) -> int:
    """Indice della parentesi che chiude quella in `i`, -1 se manca."""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == open_ch:
            depth += 1
        elif text[j] == close_ch:
            depth -= 1
            if depth == 0:
                return j
    return -1


def _judge_ps_body(c: _Cmd, kind: str, start: int, span: tuple[int, int], found: _Found) -> None:
    body = c.masked[span[0]:span[1]]
    if not _PS_SLEEP.search(body) or (kind == "for" and not _PS_BREAK.search(body)):
        return
    what = "`while`/`do` loop with Start-Sleep" if kind == "while" else "loop that sleeps until a check breaks out"
    found.blocks.append(("loop", f"{what} polls and keeps you waiting (`{c.text(start, span[1] + 1)}`)"))


# --------------------------------------------------------------------------
# Controlli per comando
# --------------------------------------------------------------------------
def _check(c: _Cmd, k: int, head: _Head, name: str, found: _Found) -> None:
    seg = c.segs[k]
    args = c.orig(seg)[head.idx + 1:]
    shown = c.text(seg.start, seg.end)
    if name in ("sleep", "start-sleep", "timeout"):
        secs = _sleep_seconds(name, args, c.mode)
        if secs is not None and secs >= _LIMIT and not c.backgrounded(k):
            found.blocks.append(("wait", f"`{shown}` waits {_secs_text(secs)} "
                                         f"(single waits of {_LIMIT:g} s or more are blocked)"))
    elif name == "watch":
        found.blocks.append(("wait", f"`{shown}` reruns a command forever and never returns"))
    elif name == "wait-process" and not any(_is_param(a, "timeout", 1) for a in args):
        found.blocks.append(("wait", f"`{shown}` without -Timeout can wait forever"))
    elif name == "find" and c.mode == "bash":
        reason = _find_reason(args)
        if reason:
            found.blocks.append(("find", reason))
    elif name == "tail":
        _tail_check(c, k, head.idx, found)
    elif name in _GC_NAMES and c.mode == "ps":
        if not c.backgrounded(k):
            _fix(c, _gc_edits(c, seg, head.idx), _FIX_TEXT["gc"], found)
    elif _PY_NAME.match(name):
        _fix(c, _python_edits(c, k, head.idx), _FIX_TEXT["python"], found)
    else:
        _nested(c, k, head.idx, name, found)


# Correzioni: (nota per la riscrittura, motivo del blocco dentro una shell annidata)
_FIX_TEXT = {
    "tail": ("`tail -f` -> `tail` without the follow flag (`-n 200` when no count was given)",
             "`tail -f` never returns (drop the follow flag, use `tail -n 200`)"),
    "gc": ("`Get-Content -Wait` -> `-Tail 200`", "`Get-Content -Wait` never returns (drop -Wait, use -Tail 200)"),
    "python": ("replaced a bare `python -` that waits on stdin forever with a no-op",
               "a bare `python -` waits on stdin forever (feed it a heredoc or a file)"),
}


def _tail_check(c: _Cmd, k: int, idx: int, found: _Found) -> None:
    """`tail -f`: in background resta com'e'; davanti a un consumatore che esce al riscontro blocca."""
    edits = _tail_edits(c, c.segs[k], idx)
    if not edits or c.backgrounded(k):
        return
    consumer = _marker_consumer(c, k)
    if consumer is not None:
        found.blocks.append(("wait", f"`{c.text(c.segs[k].start, c.segs[consumer].end)}` waits for a marker that may "
                                     "never come; use `scpy job.py wait <id> --marker <regex> --max 540`"))
        return
    _fix(c, edits, _FIX_TEXT["tail"], found)


def _marker_consumer(c: _Cmd, k: int) -> int | None:
    """Indice del segmento della pipe dopo `k` che esce al primo riscontro (grep -m/-q, head, awk exit, sed q)."""
    j = k
    while c.segs[j].after in ("|", "|&") and j + 1 < len(c.segs):
        j += 1
        head = _head(c, c.segs[j])
        if head.idx is None:
            continue
        orig = c.orig(c.segs[j])
        name = _base(orig[head.idx])
        args = [_util.unquote(a) for a in orig[head.idx + 1:]]
        if _exits_on_match(name, args):
            return j
    return None


def _exits_on_match(name: str, args: list[str]) -> bool:
    if name in ("grep", "egrep", "fgrep", "rg"):
        return any(_GREP_EXITS.match(a) for a in args)
    if name == "head":
        return True
    if name in ("awk", "gawk", "mawk"):
        return any(re.search(r"\bexit\b", a) for a in args)
    if name == "sed":
        return any(_SED_QUIT.search(a) for a in args if not a.startswith("-"))
    return False


def _fix(c: _Cmd, edits: list[tuple[int, int, str]], text: tuple[str, str], found: _Found) -> None:
    """Al primo livello le correzioni si applicano; dentro una shell annidata diventano un blocco."""
    if not edits:
        return
    note, problem = text
    if c.depth > 0:
        found.blocks.append(("fix", problem))
        return
    found.edits.extend(edits)
    if note not in found.notes:
        found.notes.append(note)


def _secs_text(secs: float) -> str:
    return "forever" if math.isinf(secs) else f"{secs:g} s"


def _gnu_seconds(tok: str) -> float | None:
    tok = _util.unquote(tok)
    if tok.lower() in ("inf", "infinity"):
        return math.inf
    m = _DURATION.match(tok)
    if not m:
        return None
    return float(tok[:len(tok) - len(m.group(2))]) * _UNITS[m.group(2).lower()]


def _sleep_seconds(name: str, args: list[str], mode: str) -> float | None:
    """Durata dell'attesa; None se non si sa calcolare (variabili, forme ignote)."""
    if name == "timeout":
        return _cmd_timeout(args, mode)
    if name == "start-sleep" or mode == "ps":
        return _ps_sleep(args)
    total, known = 0.0, False
    for a in args:                       # il sleep di GNU somma i suoi argomenti
        secs = _gnu_seconds(a)
        if secs is not None:
            total, known = total + secs, True
    return total if known else None


def _number(text: str) -> float | None:
    try:
        return float(_util.unquote(text))
    except ValueError:
        return None


def _ps_sleep(args: list[str]) -> float | None:
    """Start-Sleep N | -Seconds N | -Milliseconds N (con abbreviazioni e `-Seconds:N`)."""
    secs, i = None, 0
    while i < len(args):
        tok = _util.unquote(args[i])
        if not tok.startswith("-"):
            num = _number(tok)
            secs = num if num is not None and secs is None else secs
            i += 1
            continue
        pname, _, inline = tok[1:].partition(":")
        low = pname.lower()
        if low and "duration".startswith(low):
            return None
        unit = 0.001 if low == "ms" or (low and "milliseconds".startswith(low)) else (
            1.0 if low and "seconds".startswith(low) else None)
        if unit is not None:
            value = inline or (args[i + 1] if i + 1 < len(args) else "")
            num = _number(value)
            secs = num * unit if num is not None else secs
            i += 0 if inline else 1
        i += 1
    return secs


def _cmd_timeout(args: list[str], mode: str) -> float | None:
    """`timeout /t N` di cmd (N = -1: per sempre). Fuori da Bash vale anche `timeout N` da solo."""
    flags = ("/t",) if mode == "bash" else ("/t", "-t")
    for i, a in enumerate(args):
        if a.lower() in flags and i + 1 < len(args):
            return _int_wait(args[i + 1])
    rest = [a for a in args if a.lower() != "/nobreak"]
    return _int_wait(rest[0]) if mode != "bash" and len(rest) == 1 else None


def _int_wait(tok: str) -> float | None:
    try:
        n = int(_util.unquote(tok))
    except ValueError:
        return None
    return math.inf if n < 0 else float(n)


def _is_param(tok: str, full: str, min_len: int) -> bool:
    """Vero se `tok` e' il parametro PowerShell `-<full>` o una sua abbreviazione (anche `-Nome:valore`)."""
    tok = _util.unquote(tok)
    if not tok.startswith("-"):
        return False
    pname = tok[1:].partition(":")[0].lower()
    return len(pname) >= min_len and full.startswith(pname)


def _wide_limit(start: str) -> int | None:
    """Profondita' massima ammessa per una partenza troppo larga; None se la partenza e' circoscritta."""
    p = _util.unquote(start).replace("\\", "/")
    p = (p.rstrip("/") if p.strip("/") else "").lower()   # "/" e "//" valgono la radice
    if _ROOT_START.fullmatch(p):
        return _MAXDEPTH_ROOT
    return _MAXDEPTH_HOME if _HOME_START.fullmatch(p) else None


def _find_reason(args: list[str]) -> str | None:
    i = 0
    while i < len(args) and (args[i] in ("-H", "-L", "-P", "-D") or args[i].startswith("-O")):
        i += 2 if args[i] == "-D" else 1
    starts: list[str] = []
    while i < len(args) and not _util.unquote(args[i]).startswith(("-", "(", "!", ",", "\\(", "\\!")):
        starts.append(args[i])
        i += 1
    wide = [(s, lim) for s in starts if (lim := _wide_limit(s)) is not None]
    if not wide:
        return None
    rest = [_util.unquote(a) for a in args[i:]]
    if "-maxdepth" in rest:
        j = rest.index("-maxdepth")
        depth = _int_wait(rest[j + 1]) if j + 1 < len(rest) else None
        if depth is not None and depth <= min(lim for _, lim in wide):
            return None                  # pochi livelli: una semplice elencazione
    return f"`find {_shown(_util.unquote(wide[0][0]), 40)}` walks a whole drive or profile and can run for minutes"


# --------------------------------------------------------------------------
# Riscritture
# --------------------------------------------------------------------------
def _tail_options(c: _Cmd, seg: _Seg, idx: int) -> list[tuple[int, int, str]]:
    """Opzioni di tail come (inizio, fine, testo), fino a `--`; gli argomenti di -n/-c/-s restano attaccati."""
    opts: list[tuple[int, int, str]] = []
    skip = False
    for a, b in seg.words[idx + 1:]:
        tok = c.cmd[a:b]
        if skip:
            skip = False
        elif tok == "--":
            break
        else:
            skip = tok in _TAIL_ARG_OPTS
            opts.append((a, b, tok))
    return opts


def _tail_has_count(tokens: list[str]) -> bool:
    """Vero se tail ha gia' un conteggio: -n/-c (anche nei gruppi), --lines/--bytes, -NUM, +NUM."""
    return any(re.match(r"-[A-Za-z]*[nc]|--(?:lines|bytes)(?:=|$)|[-+]\d", t) for t in tokens)


def _tail_edits(c: _Cmd, seg: _Seg, idx: int) -> list[tuple[int, int, str]]:
    opts = _tail_options(c, seg, idx)
    has_count = _tail_has_count([t for _, _, t in opts])
    edits: list[tuple[int, int, str]] = []
    for a, b, tok in opts:
        new = _tail_token(tok, has_count)
        if new == "":
            edits.append((_space_before(c.cmd, a), b, ""))   # con un conteggio si toglie solo la f
        elif new is not None:
            edits.append((a, b, new))
    return edits


def _tail_token(tok: str, has_count: bool) -> str | None:
    """Sostituto di un'opzione di tail che segue il file ("" = toglierla); None se `tok` non segue."""
    add = "" if has_count else _TAIL_LINES
    if tok == "--follow" or tok.startswith("--follow="):
        return add
    m = re.fullmatch(r"([-+]\d+[bcl]?)f", tok)          # forma obsoleta: tail -100f
    if m:
        return m.group(1)
    if not re.fullmatch(r"-[A-Za-z]+", tok):
        return None
    letters = tok[1:]
    # n, c e s prendono il resto del gruppo come argomento: una f dopo di loro non e' -f
    cut = min((letters.find(x) for x in "ncs" if x in letters), default=len(letters))
    head, rest = letters[:cut], letters[cut:]
    if "f" not in head and "F" not in head:
        return None
    left = head.replace("f", "").replace("F", "") + rest
    if not left:
        return add
    return f"-{left}" if has_count else f"{_TAIL_LINES} -{left}"


def _gc_edits(c: _Cmd, seg: _Seg, idx: int) -> list[tuple[int, int, str]]:
    spans = seg.words[idx + 1:]
    toks = [c.cmd[a:b] for a, b in spans]
    waits = [span for span, t in zip(spans, toks) if _is_wait_flag(t)]
    if not waits:
        return []
    has_count = any(_is_param(t, p, 2) for t in toks for p in ("tail", "last", "totalcount", "first", "head"))
    edits = []
    for n, (a, b) in enumerate(waits):
        if n == 0 and not has_count:
            edits.append((a, b, _GC_TAIL))
        else:
            edits.append((_space_before(c.cmd, a), b, ""))
    return edits


def _is_wait_flag(tok: str) -> bool:
    if not _is_param(tok, "wait", 1):
        return False
    value = tok.partition(":")[2].lower()
    return value not in ("$false", "0")


def _space_before(cmd: str, a: int) -> int:
    while a > 0 and cmd[a - 1] in " \t":
        a -= 1
    return a


def _bare_python(c: _Cmd, k: int, idx: int) -> bool:
    """`python [-I] -` con sole redirezioni d'uscita: senza stdin aspetta per sempre."""
    seg = c.segs[k]
    args = c.mk(seg)[idx + 1:]
    if args[:1] == ["-I"]:
        args = args[1:]
    if args[:1] != ["-"] or "<" in c.masked[seg.start:seg.end]:
        return False
    want_target = False
    for a in args[1:]:
        if want_target:
            want_target = False
            continue
        m = _REDIRECT_OUT.match(a)
        if not m:
            return False
        want_target = not m.group("target")
    return not want_target and seg.before not in ("|", "|&")


def _python_edits(c: _Cmd, k: int, idx: int) -> list[tuple[int, int, str]]:
    """Sostituisce solo le parole `python [-I] -` con un comando nullo; separatori e redirezioni restano.

    Con lo stdin vuoto `python -` esce con 0, quindi `true` (o `$null`) vale lo stesso in ogni contesto.
    """
    if not _bare_python(c, k, idx):
        return []
    words, mk = c.segs[k].words, c.mk(c.segs[k])
    start = words[idx][0]
    if c.mode == "ps" and idx > 0 and mk[idx - 1] == "&":
        start = words[idx - 1][0]        # `& "python.exe" -`: anche l'operatore di chiamata
    end = words[idx + (2 if mk[idx + 1] == "-I" else 1)][1]
    return [(start, end, "$null" if c.mode == "ps" else "true")]


# --------------------------------------------------------------------------
# Shell annidate: bash -c "...", powershell -Command "...", cmd /c ..., eval
# --------------------------------------------------------------------------
def _nested(c: _Cmd, k: int, idx: int, name: str, found: _Found) -> None:
    if c.depth >= _MAX_DEPTH or name not in _BASH_SHELLS | _PS_SHELLS | {"cmd", "eval"}:
        return
    inner = _inner_command(c, c.segs[k], idx, name)
    if inner is None:
        return
    text, mode = inner
    sub = _Found()
    _analyze(text, mode, c.depth + 1, sub)
    background = c.backgrounded(k)
    for kind, reason in sub.blocks:
        if background and kind in _WAIT_KINDS:
            continue                     # la shell annidata gira in background: non aspetta nessuno
        tail = "; it is inside a nested shell, so change it yourself" if kind == "fix" else ""
        found.blocks.append((kind, f"{reason}, inside `{name}`{tail}"))


def _inner_command(c: _Cmd, seg: _Seg, idx: int, name: str) -> tuple[str, str] | None:
    spans = seg.words[idx + 1:]
    toks = [c.cmd[a:b] for a, b in spans]
    if name == "eval":
        return (" ".join(_inner_text(t, c.mode) for t in toks), "bash") if toks else None
    for j, tok in enumerate(toks[:-1]):
        if name in _BASH_SHELLS and re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", tok):
            return _inner_text(toks[j + 1], c.mode), "bash"
        if name in _PS_SHELLS and _is_param(tok, "command", 1):
            rest = c.cmd[spans[j + 1][0]:seg.end]
            return (_inner_text(rest, c.mode) if len(toks) == j + 2 else rest), "ps"
        if name == "cmd" and tok.lower() in ("/c", "/k"):
            rest = c.cmd[spans[j + 1][0]:seg.end]
            return (_inner_text(rest, c.mode) if len(toks) == j + 2 else rest), "cmd"
    return None


def _inner_text(tok: str, outer: str) -> str:
    """Testo che la shell annidata riceve da una parola tra virgolette."""
    if len(tok) < 2 or tok[0] != tok[-1] or tok[0] not in "\"'":
        return tok
    body = tok[1:-1]
    if tok[0] == "'":
        return body.replace("''", "'") if outer == "ps" else body
    if outer == "bash":
        return re.sub(r'\\([$`"\\\n])', r"\1", body)
    if outer == "ps":
        return re.sub(r"`(.)", r"\1", body, flags=re.S).replace('""', '"')
    return body


# --------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------
def _apply(cmd: str, edits: list[tuple[int, int, str]]) -> str:
    pieces, pos = [], 0
    for a, b, new in sorted(set(edits)):
        if a < pos:
            continue                     # sovrapposta a una modifica gia' applicata
        pieces += [cmd[pos:a], new]
        pos = b
    pieces.append(cmd[pos:])
    return "".join(pieces)


def _deny_text(blocks: list[tuple[str, str]]) -> str:
    reasons: list[str] = []
    for _, reason in blocks:
        if reason not in reasons:
            reasons.append(reason)
    kinds = {kind for kind, _ in blocks}
    text = "; ".join(reasons)
    if kinds & _WAIT_KINDS:
        text += f". To wait for a long job, {_JOB_HINT}"
    if "loop" in kinds:
        text += f"; {_LOOP_ESCAPE}"
    if "find" in kinds:
        text += f". For find, {_FIND_HINT}"
    return text


@core.on("PreToolUse", tools=core.SHELL_TOOLS, name=NAME)
def handle(ctx: core.Ctx) -> core.Result | None:
    if ctx.tool == "Monitor":
        # le attese lunghe spettano a meta: `worker` e' ogni subagent (core.resolve_identity:
        # agent_type nel payload); la sessione principale (meta o normale) usa Monitor liberamente
        if ctx.role == "worker":
            return core.Result.deny(_MONITOR_MSG)
        return None
    raw = _util.field(ctx, "command")
    if not isinstance(raw, str) or not raw.strip():
        return None
    found = _Found()
    _analyze(raw[:_util.MAX_CMD], "ps" if ctx.tool == "PowerShell" else "bash", 0, found)
    if _util.field(ctx, "run_in_background") is True and ctx.role != "worker":
        # in background non resta fermo nessuno, e il comando va lanciato com'e' (niente riscritture);
        # i worker invece non devono fare da balia ai job
        found.blocks = [(kind, reason) for kind, reason in found.blocks if kind not in _WAIT_KINDS]
        found.edits.clear()
    if found.blocks:
        return core.Result.deny(_deny_text(found.blocks))
    if not found.edits or len(raw) > _util.MAX_CMD:
        return None
    fixed = _apply(raw, found.edits)
    if fixed == raw:
        return None
    return core.Result.rewrite(
        {**ctx.tool_input, "command": fixed},
        f"{NAME} guard: rewrote the command so it returns ({'; '.join(found.notes)}); "
        "the command that runs differs from your draft.",
    )
