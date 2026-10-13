"""Guardia profile-firewall: i profili superclaude e deepseek non toccano i dati l'uno dell'altro.

Politica ad allowlist con negazione di default:
A. profilo superclaude: sotto ~/.claude-deepseek solo le voci di installazione
   (sc_profile.PROFILE_SETUP_ENTRIES["deepseek"]); settings*.json in sola lettura;
B. profilo deepseek: sotto ~/.claude solo LETTURA delle voci condivise
   (sc_profile.SHARED_CODE_ENTRIES), nessuna scrittura, nessun git nel repository
   ~/.claude; la shell puo' citare solo percorsi condivisi;
C. entrambi: settings.json / settings.local.json di ogni profilo sono scritti solo
   dal proprietario; il flag /git del proprio profilo (sc_profile.GIT_POLICY_REL) non
   e' scritto dai worker in nessuna grafia (forme device e UNC, giunzioni, punto finale, ::$DATA),
   meta e sessione plain lo scrivono; il confronto col flag non dipende dal tetto di realpath;
   un worker non cambia il flag nemmeno a livello di cartella: niente cancellazione, spostamento
   o rinomina della cartella config o della data home, e niente scritture di nomi ignoti nel
   testo (sorgente con caratteri jolly o `.`, estrazione di archivi con tar, Expand-Archive,
   unzip, 7z, robocopy senza filtri o con un filtro che copre il flag) dentro di esse;
D. entrambi: un comando shell che CAMBIA la variabile SC_PROFILE (assegnazione,
   export/unset/set/setx, env -u, $env:, Env:, SetEnvironmentVariable) e' negato;
   citarla soltanto (grep, pytest -k, messaggi di commit, echo) e' permesso;
E. deepseek non lancia la CLI `claude` ne' processi con ambiente svuotato (env -i,
   Start-Process -UseNewEnvironment); superclaude non lancia `claude-ds`;
F. fail closed: un transcript_path di un profilo diverso da quello attivo nega tutto;
   un comando oltre _util.MAX_CMD e' negato; un errore interno nega sotto deepseek.

E' un guardrail, non un confine di sicurezza: costruzione di stringhe, variabili
assegnate in precedenza, payload codificati e ricerche ricorsive lanciate da una
cartella antenata passano. Solo lavoro su stringhe e percorsi (realpath per le
giunzioni NTFS), nessun sottoprocesso.
"""
from __future__ import annotations

import fnmatch
import functools
import os
import re
from dataclasses import dataclass
from pathlib import Path

from sc_hooks import core
from sc_hooks.handlers import _util

import sc_profile  # noqa: E402 - core ha gia' aggiunto <code home>/lib a sys.path

NAME = "profile-firewall"

_SETTINGS = frozenset(name.lower() for name in sc_profile.SETTINGS_FILES)
_MAX_REALPATH = 64                    # tetto alle risoluzioni su disco per chiamata (heredoc lunghi)
_FLAG_DENY = "workers may not change the git policy flag; ask the owner to run /git"
_FLAG_NAME = sc_profile.GIT_POLICY_REL.rsplit("/", 1)[-1].lower()
_FLAG_PARTS = tuple(sc_profile.GIT_POLICY_REL.lower().split("/"))
# Cartelle che contengono il flag dentro la data home: "" (la home stessa) e "config"
_FLAG_DIRS = frozenset("/".join(_FLAG_PARTS[:k]) for k in range(len(_FLAG_PARTS)))
_FLAG_DIR_NAMES = frozenset(_FLAG_PARTS[:-1]) | {d.lower() for d in sc_profile.DATA_HOME_DIRS.values()}
_SHORT_MARK = re.compile(r"~\d")                                  # nome corto 8.3
# Win32 apre `nome.`, `nome ` e `nome::$DATA` come `nome`
_WIN32_TAIL = re.compile(r"[. ]*(?:::\$data)?[. ]*$", re.I)
# Designatori di stream che nominano il flusso principale o la cartella stessa: `f::$DATA`,
# `cartella::$INDEX_ALLOCATION\f`, `cartella:$I30:$INDEX_ALLOCATION\f`; non `:nome` (stream con nome).
# Il `$` puo' arrivare escapato dalla shell (`\$` in bash, `` `$ `` in PowerShell)
_STREAM = re.compile(r"(?:::[\\`]?\$data|::[\\`]?\$index_allocation|:[\\`]?\$i30:[\\`]?\$index_allocation)"
                     r"(?=[\\/]|[. ]*$)", re.I)

_SHELLS = frozenset({"cmd", "bash", "sh", "zsh", "pwsh", "powershell", "wsl"})
_RUNNERS = frozenset({"npx", "start", "start-process", "saps", ".", "call", "&"})
_ARG_FLAGS = frozenset({"/c", "/k", "-c", "-command", "-f", "-file", "-filepath", "-e", "--"})
_CLAUDE_CLI = frozenset({"claude", "claude-code"})        # claude, claude.cmd/.exe/.ps1, @anthropic-ai/claude-code
_DS_LAUNCHER = frozenset({"claude-ds"})                   # claude-ds, claude-ds.ps1
_WRITE_VERBS = frozenset({
    "tee", "tee-object", "set-content", "add-content", "ac", "out-file", "new-item", "ni",
    "remove-item", "ri", "rm", "del", "erase", "rmdir", "rd", "mv", "move", "move-item", "mi",
    "ren", "rename", "rename-item", "rni", "touch", "truncate", "mkdir", "md", "ln", "mklink", "sed",
})
_COPY_VERBS = frozenset({"cp", "copy", "copy-item", "cpi", "xcopy", "robocopy", "install"})  # destinazione dopo la sorgente
_MOVE_VERBS = frozenset({"mv", "move", "move-item", "mi"})
_JOIN_VERBS = (_COPY_VERBS - {"robocopy"}) | _MOVE_VERBS   # robocopy: `src dst file...`, unione coi filtri
_DELETE_VERBS = frozenset({"remove-item", "ri", "rm", "del", "erase", "rmdir", "rd"})
_RENAME_VERBS = frozenset({"ren", "rename", "rename-item", "rni"})
_SEVEN_ZIP = frozenset({"7z", "7za", "7zr", "7zz"})
_TAR_EXTRACT = re.compile(r"-[A-Za-z]*x[A-Za-z]*|--extract|--get")
_NULL_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "nul"})

# Destinazioni con nome: PowerShell accetta ogni abbreviazione non ambigua (-Des... -Destination)
_PS_DEST = frozenset("-destination"[:k] for k in range(4, 13)) | {"-destinationpath"}
_GNU_DEST = frozenset({"-t", "--target-directory"})                 # cp/mv/install, sensibili alle maiuscole
_TAR_DIR = frozenset({"-C", "--directory"})
_OUT_FLAGS = frozenset({"-o", "--output", "--out-file", "--output-file", "-outfile", "-outputfile"})
_NO_OUT_FLAG = frozenset({"grep", "egrep", "fgrep", "rg", "ls", "ssh", "scp", "ps", "find"})  # `-o` non e' un'uscita

_HOME_SPELLING = re.compile(
    r"\$\{env:(?:userprofile|home)\}|\$env:(?:userprofile|home)(?!\w)"
    r"|%homedrive%%homepath%|%(?:userprofile|home)%"
    r"|\$\{(?:home|userprofile)\}|\$(?:home|userprofile)(?!\w)"
    r"|(?<![\w~.\-])~(?=[/\\'\"\s]|$)",
    re.I,
)
_MNT = re.compile(r"(?<![\w:/.])/mnt/([a-z])(?=/|$)", re.I)       # /mnt/c/... (WSL)
_MSYS = re.compile(r"(?<![\w:/.~*\-])/([a-z])(?=/)", re.I)        # /c/... (Git Bash) nel testo libero
_STOP = r"""\s'"`;|&<>(){}\[\],="""                                # fine di un percorso citato nel testo
_PREFIX_RUN = re.compile(r"[^" + _STOP + r"]*$")
_LEAD = re.compile(_util._PREFIX)
_OPT_VALUE = re.compile(r"^-{1,2}[A-Za-z][\w-]*[=:](?P<val>.+)$")  # --db=..., -Path:...
_ASSIGN = re.compile(r"^[A-Za-z_]\w*=(?P<val>.*)$")               # VAR=valore
_REDIRECT = re.compile(r"^(?P<pre>.*?)[12&]?>>?\|?(?P<rest>.*)$")
_NOT_PATH = frozenset("()'\"`<>|;&")                                # caratteri che un percorso non contiene
_UNEXPANDED = frozenset("$%")                                       # variabile rimasta senza espansione
_GLOB = frozenset("*?[{")

# Regola D: solo le forme che CAMBIANO la variabile (il nome del modulo sc_profile non ne fa parte)
_VAR = re.escape(sc_profile.ENV_VAR)
_CMD_START = r"(?:^|[;&|({\n\"'`])\s*(?:(?:then|do|else|sudo|command)\s+)*"
_PROFILE_CHANGE = re.compile(
    rf"(?<![\w.]){_VAR}['\"\]}}]*\s*:?=(?!=)"                       # VAR=, $env:VAR = , environ['VAR']=
    rf"|{_CMD_START}(?:export|unset|set|setx|declare|typeset|local|readonly)\s+(?:-\w+\s+)*['\"]?{_VAR}(?![\w.])"
    rf"|(?<![\w.$:-])env(?:\.exe)?\s[^\n;|&]*?(?:-u\s*|--unset[=\s]\s*)['\"]?{_VAR}(?![\w.])"
    rf"|(?:remove-item|ri|rm|del|erase|clear-item|cli|set-item|si|new-item|ni)\s+(?:-(?:literal)?path\s+)?"
    rf"['\"]?env:[\\/]?{_VAR}(?![\w.])"
    rf"|(?:setenvironmentvariable|putenv|unsetenv|setenv|pop)\s*\(\s*['\"]{_VAR}['\"]"
    rf"|\bdel\s+[^\n;]*\[\s*['\"]{_VAR}['\"]\s*\]",
    re.I,
)
# Regola E: processi lanciati senza l'ambiente (il figlio perde SC_PROFILE e torna a superclaude)
_ENV_DROP = re.compile(
    r"(?<![\w.$:-])env(?:\.exe)?\s+(?:(?:-u\s*\S+|--unset[=\s]\S+|-C\s*\S+|--chdir[=\s]\S+|-\S+|[A-Za-z_]\w*=\S*)\s+)*?"
    r"(?:-[A-Za-z0-9]*i[A-Za-z0-9]*|--ignore-environment|-)(?=\s|$)"
)
_NEW_ENV_PROCESS = re.compile(r"(?<![\w-])(?:start-process|saps|start)\b[^\n;|]*?\s-usen\w*", re.I)

# Chiave DeepSeek (entrambi i profili): helper, voce del portachiavi o di cmdkey, store-key.py eseguito
_KEY_HELPER = re.compile(r"(?<![\w-])(?:key-helper|get-storedcredential)(?![\w-])", re.I)
_KEY_SERVICE = re.compile(r"(?<![\w-])claude-deepseek(?![\w-])", re.I)
_KEYRING_GET = re.compile(r"(?<![\w-])keyring\b.*?(?<![\w-])get(?:_password|_credential)?\b", re.I | re.S)
_CMDKEY = re.compile(r"(?<![\w-])cmdkey\b", re.I)
_STORE_KEY = frozenset({"store-key", "store-key.py"})
_LAUNCHERS = frozenset({"py", "uv", "uvx", "pipx"}) | _SHELLS | _RUNNERS
_KEY_DENY = ("the DeepSeek key is never read by agents: commands that reference the key helper, the "
             "claude-deepseek keyring or cmdkey entry, Get-StoredCredential, or that run store-key.py are "
             "denied in every profile; the owner runs them in a terminal")


def _glob_regex(pattern: str) -> str:
    return "".join(r"[^" + _STOP + r"/]*" if c == "*" else re.escape(c) for c in pattern)


# File privati fuori dalle data home citati nel testo (`~/.claude.json`, `$HOME/.claude.json.backup`)
_PRIVATE_MENTION = re.compile(
    r"(?<![\w.\-])(?:" + "|".join(_glob_regex(p) for pats in sc_profile.PROFILE_PRIVATE_FILES.values()
                                  for p in pats) + r")(?![\w.\-])",
    re.I,
)


@dataclass(frozen=True)
class _Hit:
    profile: str           # profilo della data home che contiene il percorso
    rel: str               # percorso relativo a quella home, minuscolo; "" = la home stessa
    private: bool = False  # file privato del profilo fuori dalla data home: rel e' relativo alla HOME

    @property
    def first(self) -> str:
        return self.rel.split("/", 1)[0]


def _code_profile() -> str:
    """Profilo la cui data home e' anche la home del codice condiviso (superclaude)."""
    return sc_profile.profile_of_path(sc_profile.code_home()) or sc_profile.DEFAULT_PROFILE


def _code_dir() -> str:
    return sc_profile.code_home().name


# --------------------------------------------------------------------------
# Normalizzazione del testo
# --------------------------------------------------------------------------
def _expand(text: str, home: str = "") -> str:
    """Espande le grafie della HOME (~, $HOME, ${HOME}, $env:X, ${env:X}, %X%) e /mnt/c, /c."""
    home = home or str(Path.home()).replace("\\", "/")
    # \\?\C:, \\localhost\C$, ... prima di tutto: il `?` non e' un carattere jolly, `$` non e' una variabile
    text = sc_profile.local_form(text)
    text = _HOME_SPELLING.sub(lambda _m: home, text)
    text = _MNT.sub(lambda m: m.group(1) + ":", text)
    return _MSYS.sub(lambda m: m.group(1) + ":", text)


def _fold(path: str) -> str:
    """Percorso canonico come lo apre Win32: senza punti e spazi finali dei componenti ne' `::$DATA`."""
    return "/".join(c if c in ("", ".", "..") else _WIN32_TAIL.sub("", c) for c in path.split("/"))


def _may_be_flag(text: str, whole: bool = False) -> bool:
    """Il nome finale puo' essere il flag /git: lo stesso nome (anche come prodotto di `_dest_joins`)
    o un nome corto 8.3 (`GIT-PO~1`), che solo il disco riconduce al nome lungo; per una cartella
    scritta per intero (`whole`) anche `config` o il nome di una data home."""
    base = _fold(text.replace("\\", "/").rstrip("/")).rsplit("/", 1)[-1].lower()
    return (base == _FLAG_NAME or bool(_SHORT_MARK.search(base))
            or (whole and base in _FLAG_DIR_NAMES))


def _may_name_flag(tok: str) -> bool:
    """Il token puo' coprire il flag o la sua cartella? Senza jolly si'; con jolly solo se l'ultimo
    componente e' il primo con jolly e ne copre il nome (`<home>/*/__pycache__` non tocca config)."""
    parts = [p for p in re.split(r"[\\/]", tok) if p]
    for k, part in enumerate(parts):
        if any(c in _GLOB for c in part):
            low = part.lower()
            return k == len(parts) - 1 and ("{" in low or any(fnmatch.fnmatchcase(n, low) for n in _FLAG_PARTS))
    return True


def _unknown_name(src: str) -> bool:
    """Una sorgente i cui file scritti non si leggono nel testo: `.`, `..` o un glob che puo' coprire il flag."""
    name = src.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]
    return name in ("", ".", "..") or (any(c in _GLOB for c in name) and _may_name_flag(name))


def _glob_root(text: str) -> str:
    """Parte fissa di un glob: le cartelle prima del primo componente con caratteri jolly."""
    keep: list[str] = []
    for part in re.split(r"[\\/]", text):
        if any(c in _GLOB for c in part):
            break
        keep.append(part)
    return "/".join(keep)


def _is_abs(text: str) -> bool:
    return bool(re.match(r"^(?:[A-Za-z]:|[/\\])", text))


def _candidate(tok: str, home: str) -> str:
    """Percorso plausibile contenuto in un token di shell, "" se il token non lo e'."""
    t = tok.strip()
    if not t or "://" in t:
        return ""
    m = _OPT_VALUE.match(t) or _ASSIGN.match(t)
    if m:
        t = _util.unquote(m.group("val"))
    elif t.startswith("-"):
        return ""
    # Il `$` di `::$DATA` non e' una variabile: senza il designatore il token resta un percorso
    t = _STREAM.sub("", t)
    if any(c.isspace() or c in _NOT_PATH for c in t):
        return ""
    t = _expand(t, home)
    if not t or t.lower() in _NULL_SINKS or any(c in _UNEXPANDED for c in t):
        return ""
    if any(c in _GLOB for c in t):
        t = _glob_root(t) or "."
    return t


def _clean_rel(rest: str) -> str | None:
    """Componenti dopo la home con `.` e `..` risolti; None se si esce dalla home."""
    parts: list[str] = []
    for seg in rest.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


def _stem(word: str) -> str:
    """Nome del programma: senza cartella, minuscolo, senza .exe/.cmd/.bat/.ps1."""
    name = _util.unquote(word.strip()).replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


@functools.lru_cache(maxsize=8)
def _mention_pattern(dir_name: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w.\-])" + re.escape(dir_name)
                      + r"(?![\w.\-])(?:/+(?P<rest>[^" + _STOP + r"]*))?")


# --------------------------------------------------------------------------
# Localizzazione dei percorsi nelle data home
# --------------------------------------------------------------------------
class _Scanner:
    """Trova percorsi e citazioni nelle data home dei profili (forme lessicali e reali)."""

    def __init__(self) -> None:
        # Path.home() si legge qui, a ogni chiamata: i test ridefiniscono USERPROFILE
        self.homes = {name: {_util.canon(f) for f in sc_profile.resolved_forms(p)}
                      for name, p in sc_profile.profiles().items()}
        self.dir_names = {name: p.name.lower() for name, p in sc_profile.profiles().items()}
        self.code_owner = _code_profile()
        self.home_fwd = str(Path.home()).replace("\\", "/")
        self.home_text = _util.canon(self.home_fwd)
        self.home_forms = {self.home_text} | {_util.canon(f) for f in sc_profile.resolved_forms(Path.home())}
        self.budget = _MAX_REALPATH
        self._cwds: dict[str, list[str]] = {}
        # Flag /git di ogni profilo con le sue forme reali, risolte una volta fuori dal budget
        self.flags = {_fold(_util.canon(f)): name for name, p in sc_profile.profiles().items()
                      for f in sc_profile.resolved_forms(p / sc_profile.GIT_POLICY_REL)}

    def locate(self, path: str) -> list[_Hit]:
        path = sc_profile.local_form(path)
        hits: list[_Hit] = []
        for name, roots in self.homes.items():
            for root in roots:
                if sc_profile.is_within(path, root):
                    hits.append(_Hit(name, path[len(root):].lstrip("/")))
                    break
        owner = sc_profile.private_file_owner(path, self.home_forms) if path else None
        if owner:
            hits.append(_Hit(owner, path.replace("\\", "/").rsplit("/", 1)[-1].lower(), private=True))
        flag = self.flags.get(_fold(path))
        if flag and not any(h.profile == flag for h in hits):
            hits.append(_Hit(flag, sc_profile.GIT_POLICY_REL))   # flag con la cartella config fuori dalla home
        return hits

    def real_forms(self, path: str, force: bool = False) -> set[str]:
        """Forma data piu' forma risolta su disco (giunzioni, symlink), entro il budget salvo `force`."""
        if self.budget <= 0 and not force:
            return {path}
        self.budget -= 1
        try:
            return {path} | {_util.canon(f) for f in sc_profile.resolved_forms(path)}
        except (OSError, ValueError):
            return {path}

    def cwd_forms(self, cwd: str) -> list[str]:
        if cwd not in self._cwds:
            base = _util.canon(sc_profile.local_form(cwd or os.getcwd()))
            self._cwds[cwd] = sorted(self.real_forms(base)) if base else []
        return self._cwds[cwd]

    def _may_hit(self, text: str, cwd: str) -> bool:
        """Senza risoluzione su disco un token tocca una home solo se ne cita il nome o la cwd sta in una home."""
        low = text.lower()
        return any(n in low for n in self.dir_names.values()) or any(self.locate(c) for c in self.cwd_forms(cwd))

    def _resolve(self, text: str, cwd: str) -> set[str]:
        if _is_abs(text):
            return {p for p in [_util.canon(text)] if p}
        return {p for base in self.cwd_forms(cwd) for p in [_util.canon(text, base)] if p}

    def path_hits(self, raw: str, cwd: str) -> list[_Hit]:
        """Bersaglio di uno strumento file: sempre risolto anche su disco."""
        forms = {f for p in self._resolve(_expand(raw), cwd) for f in self.real_forms(p, force=True)}
        return [h for f in forms for h in self.locate(f)]

    def token_hits(self, tok: str, cwd: str, force: bool = False, whole: bool = False) -> list[_Hit]:
        """Percorsi di un token; `force` (token scritti) risolve su disco a budget esaurito se puo' essere il flag
        o, con `whole` (cartella scritta per intero), una cartella che lo contiene."""
        text = _candidate(tok, self.home_fwd)
        if not text:
            return []
        # Un rm con migliaia di bersagli forzati supererebbe il timeout dell'hook: oltre il tetto
        # si risolve su disco solo un token scritto che potrebbe nominare il flag
        force = force and _may_be_flag(text, whole)
        forms: set[str] = set()
        bare = not any(c in text for c in "/\\.~:")
        # Parole nude, o budget di realpath esaurito: niente giunzioni da scoprire, basta il filtro testuale
        if (bare or (self.budget <= 0 and not force)) and not self._may_hit(text, cwd):
            return []
        for p in self._resolve(text, cwd):
            if bare:
                # Una parola nuda e' un percorso solo se esiste davvero (`ls plans` dentro una home)
                if self.locate(p) and os.path.lexists(p):
                    forms.add(p)
            else:
                forms |= self.real_forms(p, force)
        return [h for f in forms for h in self.locate(f)]

    def mention_hits(self, cmd: str, cwd: str) -> list[_Hit]:
        """Citazioni testuali di una data home, anche dentro stringhe (python -c, here-string)."""
        text = _expand(cmd.replace("\\", "/"), self.home_fwd).lower()
        at_home = self.home_text in self.cwd_forms(cwd)
        hits: list[_Hit] = []
        for name, dir_name in self.dir_names.items():
            for m in _mention_pattern(dir_name).finditer(text):
                hit = self._mention(text, m, name, at_home)
                if hit:
                    hits.append(hit)
        return hits + self._private_mentions(text, cwd, at_home)

    def _private_mentions(self, text: str, cwd: str, at_home: bool) -> list[_Hit]:
        """Citazioni dei file privati nella HOME (`~/.claude.json`): contano sotto la HOME, o nude con la cwd li'."""
        hits: list[_Hit] = []
        for m in _PRIVATE_MENTION.finditer(text):
            owner = sc_profile.private_owner_of_name(m.group(0))
            prefix = _PREFIX_RUN.search(text[max(0, m.start() - 260):m.start()]).group(0).rstrip("/")
            base = sc_profile.local_form(cwd) or None
            if owner and (_util.canon(prefix, base) in self.home_forms if prefix else at_home):
                hits.append(_Hit(owner, m.group(0).lower(), private=True))
        return hits

    def _mention(self, text: str, m: re.Match[str], profile: str, at_home: bool) -> _Hit | None:
        prefix = _PREFIX_RUN.search(text[max(0, m.start() - 260):m.start()]).group(0)
        if not prefix.strip("/"):
            prefix = ""           # `\.claude-deepseek` di una regex: la barra non e' una HOME
        # `.claude/` sotto un progetto e' un'altra cartella: conta sotto la HOME, o nuda con la cwd nella HOME
        if (profile == self.code_owner and prefix.rstrip("/") != self.home_text
                and (prefix or not at_home)):
            return None
        rel = _clean_rel(m.group("rest") or "")
        if rel is None or (not rel and not prefix):
            return None           # parola nuda senza componenti: e' solo testo
        return _Hit(profile, rel)


# --------------------------------------------------------------------------
# Politica
# --------------------------------------------------------------------------
def _display(hit: _Hit) -> str:
    if hit.private:
        return "~/" + hit.rel
    base = "~/" + sc_profile.profiles()[hit.profile].name
    return f"{base}/{hit.rel}" if hit.rel else base


def _alternative(active: str, hit: _Hit) -> str:
    base = "~/" + sc_profile.profiles()[active].name
    alt = f"{base}/{hit.first}" if hit.first else base
    return alt + " via mem" if hit.first == "agent-memory" else alt


def _owner_only(where: str) -> str:
    return (f"{where} is owner-only (settings*.json of both profiles are written by the owner); "
            "propose the change to the user instead")


def _verdict(active: str, hit: _Hit, write: bool, role: str, *, whole: bool = False) -> str | None:
    where = _display(hit)
    if hit.private:
        return None if hit.profile == active else (
            f"{active} may not access {where}: it is a private file of the {hit.profile} profile outside "
            "its data home (claude.ai account, per-project permissions, MCP servers); read or write it "
            f"only from the {hit.profile} profile")
    code = _code_profile()
    if hit.profile == active:
        if write and hit.rel in _SETTINGS:
            return _owner_only(where)
        # /git e' una decisione del proprietario: un worker non cambia il flag del proprio profilo,
        # ne' scrivendolo ne' cancellando, spostando o riempiendo per intero una cartella che lo contiene
        rel = _fold(hit.rel)
        if role == "worker" and ((write and rel == sc_profile.GIT_POLICY_REL) or (whole and rel in _FLAG_DIRS)):
            return _FLAG_DENY
        return None
    if active == code:
        return _foreign_data_verdict(active, hit, write)
    return _code_home_verdict(active, hit, write)


def _foreign_data_verdict(active: str, hit: _Hit, write: bool) -> str | None:
    """Regola A: il profilo del codice dentro la data home di un altro profilo."""
    where = _display(hit)
    if hit.rel in _SETTINGS:
        return _owner_only(where) if write else None
    setup = sc_profile.PROFILE_SETUP_ENTRIES.get(hit.profile, ())
    if hit.first in {e.lower() for e in setup}:
        return None
    return (f"{active} may not access {where}: under {_display(_Hit(hit.profile, ''))} only the setup "
            f"entries ({', '.join(setup)}) are allowed, settings*.json read-only; "
            f"use {_alternative(active, hit)} instead")


def _code_home_verdict(active: str, hit: _Hit, write: bool) -> str | None:
    """Regola B: un profilo di soli dati dentro la home del codice."""
    where = _display(hit)
    shared = hit.first in {e.lower() for e in sc_profile.SHARED_CODE_ENTRIES}
    if write:
        # Il codice condiviso non ha una controparte nel profilo: si modifica solo dal profilo del codice
        tail = (f"shared code changes are made from the {_code_profile()} profile" if shared
                else f"write under {_alternative(active, hit)} instead")
        return f"{active} may not write {where}: everything under ~/{_code_dir()} is read-only for {active}; {tail}"
    if shared:
        return None
    return (f"{active} may not access {where}: under ~/{_code_dir()} it may only read shared code "
            f"({', '.join(sc_profile.SHARED_CODE_ENTRIES)}); use {_alternative(active, hit)} instead")


def _first_verdict(active: str, hits: list[_Hit], write: bool, role: str, *, whole: bool = False) -> str | None:
    return next((r for r in (_verdict(active, h, write, role, whole=whole) for h in hits) if r), None)


# --------------------------------------------------------------------------
# Strumenti
# --------------------------------------------------------------------------
def _transcript_reason(ctx: core.Ctx, active: str, scan: _Scanner) -> str | None:
    raw = ctx.payload.get("transcript_path") if isinstance(ctx.payload, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = _util.canon(_expand(raw))
    owners = {p for f in (scan.real_forms(path) if path else ()) for p in [sc_profile.profile_of_path(f)] if p}
    if not owners or active in owners:
        return None
    owner = sorted(owners)[0]
    return (f"fail closed: transcript_path {raw} belongs to the {owner} profile but "
            f"{sc_profile.ENV_VAR} selects {active}; every tool call is denied until the session "
            f"restarts with the {owner} launcher")


def _file_targets(ctx: core.Ctx) -> list[str]:
    if ctx.tool in ("Glob", "Grep"):
        base = _util.field(ctx, "path")
        base = base if isinstance(base, str) and base.strip() else ctx.cwd
        pattern = _util.field(ctx, "pattern")
        if ctx.tool == "Glob" and isinstance(pattern, str) and pattern.strip():
            # La radice effettiva e' la base piu' la parte fissa del pattern (`bin/*.py` sotto la base)
            root = _glob_root(_expand(pattern))
            if root:
                return [root if _is_abs(root) else base.rstrip("/\\") + "/" + root]
        return [base]
    raw = _util.path_of(ctx)
    return [raw] if raw else []


def _programs(seg: str) -> set[str]:
    """Programmi invocati dal segmento, attraversando npx, start, cmd /c, pwsh -File, ..."""
    m = _LEAD.match(seg)
    toks = _util.tokens(seg[m.end():] if m else seg)
    found: set[str] = set()
    i = 0
    while i < len(toks):
        words = toks[i].split()
        if not words:
            break
        name = _stem(words[0])
        found |= {name, _stem(toks[i])}            # anche un percorso tra virgolette con spazi
        if (name not in _SHELLS and name not in _RUNNERS) or len(words) > 1:
            break                                   # stringa annidata: la visita `_util.walk`
        flag = next((k for k in range(i + 1, len(toks)) if toks[k].lower() in _ARG_FLAGS), None)
        if flag is not None:
            i = flag + 1
        else:
            i = next((k for k in range(i + 1, len(toks)) if toks[k] and toks[k][:1] not in "-/"), len(toks))
    return found


def _flag_writes(verb: str, flag: str) -> bool:
    """L'opzione `flag` di `verb` nomina un percorso che il comando scrive?"""
    low = flag.lower()
    if low in _PS_DEST:
        return True
    if (verb in _COPY_VERBS or verb in _MOVE_VERBS) and flag in _GNU_DEST:
        return True
    if verb == "tar" and flag in _TAR_DIR:
        return True
    return verb not in _NO_OUT_FLAG and low in _OUT_FLAGS


def _named_targets(verb: str, toks: list[str]) -> set[int]:
    """Indici dei valori di opzioni di scrittura: `-Destination X`, `-t X`, `--output=X`, `tar -C X`."""
    out: set[int] = set()
    for i in range(1, len(toks)):
        m = _OPT_VALUE.match(toks[i])
        flag = toks[i][:m.start("val") - 1] if m else toks[i]
        if not flag.startswith("-") or not _flag_writes(verb, flag):
            continue
        if m:
            out.add(i)
        elif i + 1 < len(toks):
            out.add(i + 1)
    return out


def _positional(toks: list[str], named: set[int]) -> list[int]:
    return [i for i in range(1, len(toks)) if toks[i][:1] != "-" and i not in named]


def _write_positions(toks: list[str]) -> set[int]:
    """Indici dei token che il comando scrive (euristica sul verbo e sulle opzioni con nome)."""
    if not toks:
        return set()
    verb = _stem(toks[0])
    named = _named_targets(verb, toks)
    positional = _positional(toks, named)
    if verb in _COPY_VERBS:
        # Con una destinazione con nome i posizionali sono tutti sorgenti
        return named or set(positional[1:])
    if verb == "sed" and not any(t.startswith("-i") or t == "--in-place" for t in toks[1:]):
        return named
    return named | (set(positional) if verb in _WRITE_VERBS else set())


def _robocopy_args(toks: list[str]) -> tuple[int | None, list[str]]:
    """Indice della destinazione e filtri sui file di `robocopy src dst [file...] [/opzioni]`."""
    positional = _positional(toks, set())
    if len(positional) < 2:
        return None, []
    filters: list[str] = []
    for i in positional[2:]:
        if toks[i].startswith("/"):
            break
        filters.append(toks[i])
    return positional[1], filters


def _robocopy_whole(toks: list[str]) -> list[int]:
    """Destinazione di robocopy riempita con nomi ignoti: senza filtri o con un filtro jolly che copre il flag."""
    dest, filters = _robocopy_args(toks)
    unknown = not filters or any(any(c in _GLOB for c in f) and _may_name_flag(f) for f in filters)
    return [dest] if dest is not None and unknown else []


def _dest_joins(toks: list[str]) -> list[str]:
    """Copia o spostamento in una cartella: il file scritto e' `<destinazione>/<nome della sorgente>`
    (per robocopy `<destinazione>/<filtro>` per ogni filtro senza caratteri jolly)."""
    if toks and _stem(toks[0]) == "robocopy":
        dest, filters = _robocopy_args(toks)
        return [] if dest is None else [toks[dest].rstrip("/\\") + "/" + f for f in filters
                                        if not any(c in _GLOB for c in f)]
    if not toks or _stem(toks[0]) not in _JOIN_VERBS:
        return []
    named = _named_targets(_stem(toks[0]), toks)
    positional = _positional(toks, named)
    dests = sorted(named) or positional[-1:]
    joins: list[str] = []
    for d in dests:
        m = _OPT_VALUE.match(toks[d])
        base = _util.unquote(m.group("val")) if m else toks[d]
        for s in (i for i in positional if i not in dests):
            name = toks[s].rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]
            if name:
                joins.append(base.rstrip("/\\") + "/" + name)
    return joins


def _extract_dirs(verb: str, toks: list[str], named: set[int], positional: list[int]) -> list[int]:
    """Cartelle in cui un archivio si estrae: `tar -x -C X`, `Expand-Archive -DestinationPath X`, `unzip -d X`."""
    if verb == "tar" and (any(_TAR_EXTRACT.fullmatch(t) for t in toks[1:])
                          or re.fullmatch(r"[A-Za-z]*x[A-Za-z]*", toks[1] if len(toks) > 1 else "")):
        return sorted(named)
    if verb == "expand-archive":
        return sorted(named) or positional[1:2]
    if verb == "unzip":
        return [i + 1 for i in range(1, len(toks) - 1) if toks[i] == "-d"]
    return []


def _whole_positions(toks: list[str]) -> set[int]:
    """Indici delle cartelle scritte per intero: cancellate, spostate o rinominate, o riempite con
    nomi che il testo non mostra (sorgente jolly o `.`, archivio estratto, robocopy senza filtri)."""
    if not toks:
        return set()
    verb = _stem(toks[0])
    named = _named_targets(verb, toks)
    positional = _positional(toks, named)
    if verb in _DELETE_VERBS or verb in _RENAME_VERBS:
        found = positional
    elif verb in _JOIN_VERBS:
        dests = sorted(named) or positional[-1:]
        sources = [i for i in positional if i not in dests]
        found = sources if verb in _MOVE_VERBS else []
        if any(_unknown_name(toks[s]) for s in sources):
            found = found + dests
    elif verb == "robocopy":
        found = _robocopy_whole(toks)
    else:
        found = _extract_dirs(verb, toks, named, positional)
    return {i for i in found if _may_name_flag(toks[i])}


def _whole_values(toks: list[str]) -> list[str]:
    """Cartelle di estrazione incollate all'opzione, che non sono token a se': `7z x a.7z -oX`."""
    if not toks or _stem(toks[0]) not in _SEVEN_ZIP:
        return []
    return [t[2:] for t in toks[1:] if t[:2].lower() == "-o" and len(t) > 2]


def _segment_tokens(seg: str) -> list[tuple[str, bool, bool]]:
    """(token, scritto?, cartella scritta per intero?) per un segmento: redirezioni e bersagli dei verbi."""
    m = _LEAD.match(seg)
    head, body = (seg[:m.end()], seg[m.end():]) if m else ("", seg)
    out = [(t, False, False) for t in _util.tokens(head)]
    toks = _util.tokens(body)
    targets = _write_positions(toks)
    whole = _whole_positions(toks)
    i = 0
    while i < len(toks):
        tok = toks[i]
        r = _REDIRECT.match(tok) if ">" in tok else None
        if r:
            if r.group("pre"):
                out.append((r.group("pre"), i in targets, False))
            if r.group("rest"):
                out.append((r.group("rest"), True, False))
            elif i + 1 < len(toks):
                i += 1
                out.append((toks[i], True, False))
        else:
            out.append((tok.lstrip("<"), i in targets, i in whole))
        i += 1
    return (out + [(j, True, False) for j in _dest_joins(toks)]
            + [(v, False, True) for v in _whole_values(toks)])


def _git_reason(cmd: str, cwd: str, active: str, scan: _Scanner) -> str | None:
    """Regola B: un profilo di soli dati non esegue git nel repository della home del codice."""
    for call in _util.git_calls(cmd, cwd):
        if any(h.profile == scan.code_owner for f in scan.real_forms(call.repo) for h in scan.locate(f)):
            return (f"{active} may not run git in the ~/{_code_dir()} repository (git {call.verb}): "
                    f"shared code is managed from the {scan.code_owner} profile")
    return None


def _command_reason(cmd: str, cwd: str, active: str, scan: _Scanner) -> str | None:
    """Controlli sull'intero comando (anche dentro stringhe annidate)."""
    if _KEY_HELPER.search(cmd) or (_KEY_SERVICE.search(cmd) and (_KEYRING_GET.search(cmd) or _CMDKEY.search(cmd))):
        return _KEY_DENY
    if _PROFILE_CHANGE.search(cmd):
        return (f"commands that change {sc_profile.ENV_VAR} are denied (a subprocess must not switch "
                f"profile); this session is {active}, start the other profile from its own launcher")
    if active == scan.code_owner:
        return None
    if _ENV_DROP.search(cmd) or _NEW_ENV_PROCESS.search(cmd):
        return (f"{active} may not start a process with an emptied environment (env -i, Start-Process "
                f"-UseNewEnvironment): without {sc_profile.ENV_VAR} the child falls back to the "
                f"{scan.code_owner} profile")
    return _git_reason(cmd, cwd, active, scan)


def _runs_store_key(seg: str) -> bool:
    """Il segmento esegue store-key.py (direttamente o tramite python, uv, una shell, ...)?"""
    m = _LEAD.match(seg)
    stems = [_stem(t) for t in _util.tokens(seg[m.end():] if m else seg)]
    if not stems:
        return False
    launcher = stems[0] in _LAUNCHERS or stems[0].startswith("python")
    return stems[0] in _STORE_KEY or (launcher and any(s in _STORE_KEY for s in stems[1:]))


def _segment_reason(seg: str, here: str, active: str, scan: _Scanner, role: str) -> str | None:
    if _runs_store_key(seg):
        return _KEY_DENY
    programs = _programs(seg)
    if active != scan.code_owner and programs & _CLAUDE_CLI:
        return (f"{active} may not launch the claude CLI: with the env scrub a child claude starts the "
                f"{scan.code_owner} profile; use subagents (Agent tool) inside this session instead")
    if active == scan.code_owner and programs & _DS_LAUNCHER:
        return (f"{active} may not launch claude-ds: a child deepseek session would mix the two "
                "profiles; the owner starts claude-ds in its own terminal")
    for tok, write, whole in _segment_tokens(seg):
        hits = scan.token_hits(tok, here, force=write or whole, whole=whole)
        reason = _first_verdict(active, hits, write, role, whole=whole)
        if reason:
            return reason
    return None


def _shell_reason(ctx: core.Ctx, active: str, scan: _Scanner) -> str | None:
    raw = _util.field(ctx, "command")
    if isinstance(raw, str) and len(raw) > _util.MAX_CMD:
        # clean() tronca a MAX_CMD: la coda resterebbe invisibile a tutte le guardie
        return (f"command of {len(raw)} characters exceeds the {_util.MAX_CMD}-character limit the "
                "guards inspect; write the content to a file and run a short command instead")
    cmd = _util.command_of(ctx)
    if not cmd.strip():
        return None
    reason = _command_reason(cmd, ctx.cwd, active, scan)
    for seg, here in ([] if reason else _util.walk(cmd, ctx.cwd)):
        reason = _segment_reason(seg, here, active, scan, ctx.role)
        if reason:
            break
    return reason or _first_verdict(active, scan.mention_hits(cmd, ctx.cwd), False, ctx.role)


def _file_reason(ctx: core.Ctx, active: str, scan: _Scanner) -> str | None:
    write = ctx.tool in core.WRITE_TOOLS
    for raw in _file_targets(ctx):
        reason = _first_verdict(active, scan.path_hits(raw, ctx.cwd), write, ctx.role)
        if reason:
            return reason
    return None


def _reason(ctx: core.Ctx, active: str) -> str | None:
    scan = _Scanner()
    reason = _transcript_reason(ctx, active, scan)
    if not reason and ctx.tool in core.SHELL_TOOLS:
        reason = _shell_reason(ctx, active, scan)
    elif not reason and ctx.tool in ("Read", "Glob", "Grep") + core.WRITE_TOOLS:
        reason = _file_reason(ctx, active, scan)
    return reason


@core.on("PreToolUse", name=NAME)
def handle(ctx: core.Ctx) -> core.Result | None:
    active = core.PROFILE
    if active not in sc_profile.profiles():
        return None
    try:
        reason = _reason(ctx, active)
    except Exception as exc:  # noqa: BLE001 - qualunque difetto della guardia
        core.log_error(NAME, exc)
        # Nel profilo predefinito un difetto della guardia non blocca il lavoro, salvo citazioni
        # testuali di un'altra data home; negli altri profili fail closed
        if active == sc_profile.DEFAULT_PROFILE:
            reason = _fail_open_reason(ctx, active, exc)
        else:
            reason = (f"fail closed: the profile firewall failed ({type(exc).__name__}) under the {active} "
                      "profile; the call is denied, see the hooks log")
    return core.Result.deny(reason) if reason else None


def _fail_open_reason(ctx: core.Ctx, active: str, exc: Exception) -> str | None:
    """Controllo di riserva che non puo' fallire: sottostringa del nome di un'altra data home nell'input grezzo."""
    raw = str(ctx.tool_input).lower()
    mark = next((d for p, d in sc_profile.DATA_HOME_DIRS.items() if p != active and d.lower() in raw), None)
    if mark is None:
        return None
    return (f"fail closed: the profile firewall failed ({type(exc).__name__}) and the tool input names "
            f"~/{mark}; the call is denied, see the hooks log")
