"""Guardia git-policy: applica il flag `/git true|false` (ex guards/26-git-policy.sh).

Il flag vive nella data home del profilo attivo, in sc_profile.GIT_POLICY_REL (sovrascrivibile con
SUPERCLAUDE_GIT_POLICY_FILE per i test, solo nel profilo di default). Profilo
superclaude (default): lettura fail-open, come nella versione bash; il blocco scatta
SOLO se il file esiste, si legge e dice `disabled` (si accettano anche `false`, `off`,
`0`, `no`); file assente o contenuto ignoto valgono `enabled`. Profilo DeepSeek (non
default): lettura fail-closed; file assente, illeggibile o ignoto valgono `disabled` e
solo un flag esplicitamente abilitante (`enabled`, `true`, `on`, `1`, `yes`) sblocca git.
La lettura del flag vive in lib/sc_gitflag.py, condivisa con il motore di sync; qui
restano solo il percorso, l'override via ambiente e il fail mode del profilo. Se quel
modulo non si carica (anche con SystemExit) o non viene da <code home>/lib, se la sua
git_disabled solleva, esce, manca o non restituisce un bool, o se sc_profile non da' il
percorso del flag, git resta bloccato in ogni profilo (fail-closed).

Con qualunque stato del flag e per ogni ruolo la guardia nega anche un comando che
contiene il token `sync` e un'opzione `--take-over` o `--allow-mass-delete`, scritta per
intero: opzioni riservate al proprietario (HUMAN GATE), da lanciare in un terminale vero.

Euristica dichiarata, non un confine di sicurezza: non modella sostituzioni di
comando, variabili, interpreti che pilotano git, alias preesistenti.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from types import ModuleType

from sc_hooks import core
from sc_hooks.handlers import _util

import sc_profile  # noqa: E402 - core ha gia' aggiunto <code home>/lib a sys.path

NAME = "git-policy"

# Difetti di una lib che la guardia assorbe: SystemExit non e' un Exception e, uscito dalla
# guardia, chiuderebbe il dispatcher con un codice che Claude Code tratta come via libera.
_LIB_DEFECTS = (Exception, SystemExit)


def _import_gitflag() -> ModuleType | None:
    """sc_gitflag da core._LIB_DIR (la <code home>/lib di sc_profile), oppure None con l'errore in hooks.log.

    Una lib rotta non deve annullare /git false: senza il modulo, con una copia trovata
    altrove in sys.path (per esempio prima di lib in PYTHONPATH), o con una git_disabled
    che solleva, esce, manca o non restituisce un bool (_flag_state), git resta bloccato
    in ogni profilo e il messaggio di diniego lo dice; le altre regole (auto-sblocco,
    HUMAN GATE di `mem sync`) restano attive.
    """
    try:
        import sc_gitflag
        where = Path(sc_gitflag.__file__).resolve().parent
    except _LIB_DEFECTS as exc:  # noqa: BLE001 - qualunque difetto della lib, non solo ImportError
        core.log_error("import/sc_gitflag", exc)
        return None
    if where != Path(core._LIB_DIR).resolve():
        core.log_error("import/sc_gitflag", ImportError(f"sc_gitflag shadowed by {sc_gitflag.__file__}"))
        return None
    return sc_gitflag


_GITFLAG = _import_gitflag()

# Opzioni di `mem sync` riservate al proprietario (HUMAN GATE), confrontate per intero
_OWNER_FLAGS = ("--take-over", "--allow-mass-delete")
# Quoting ed escape (virgolette, backtick, backslash, caret di cmd) e sintassi di sottoshell,
# blocchi ed espansioni (`$()`, `${F:-x}`, `$''`, `$null`); in coda le virgolette tipografiche
# (da U+2018 a U+201E) che PowerShell tratta come virgolette (testo incollato da Outlook o Word)
_QUOTING = "\"'`\\(){}$^\u2018\u2019\u201a\u201b\u201c\u201d\u201e"
# Separatori di token per l'HUMAN GATE: spazi, operatori di shell, la virgola degli array
# PowerShell (`a,b`), `:` di `-Param:valore` e `=` (`--take-over=1`, `F=--take-over`).
# `$'...'` e `@('...')` sono gia' coperti: quoting e parentesi fanno da bordo.
# `/` non separa: un percorso non e' un'opzione.
_GATE_SEP = r"\s,;|&<>=:"

# Verbi che cambiano la storia o la pubblicano. `reset` e `tag` hanno regole a parte.
_MUTATING = frozenset({
    "commit", "commit-tree", "push", "rebase", "merge", "cherry-pick", "revert", "am", "fast-import",
})
_ABORTABLE = frozenset({"rebase", "merge", "cherry-pick", "revert", "am"})
_TAG_READONLY = ("-l", "--list", "-n", "-v", "--verify", "--contains", "--no-contains",
                 "--points-at", "--merged", "--no-merged", "--sort", "--format", "--column")

# Bersaglio di una redirezione: `> f`, `>> f`, `>| f`, `2> f`, `&> f`; mai `>&1`.
_REDIRECT = re.compile(r""">>?\|?\s*(?!&)("[^"]*"|'[^']*'|[^\s;&|<>()]+)""")
# Comandi che scrivono (o spostano) file: bash e PowerShell, alias compresi.
_WRITERS = re.compile(
    r"""^\s*(?:&\s*)?(?:sudo\s+)?(?:tee|cp|mv|install|ln|truncate|dd|sed|rm|del|erase|copy|move|ren|rename
    |set-content|add-content|clear-content|out-file|copy-item|move-item|new-item|remove-item|rename-item
    |sc|ac|clc|cpi|mi|ni|ri|rni)(?:\.exe)?(?=\s|$)""",
    re.I | re.X,
)
_ARG_PREFIX = re.compile(r"^(?:of=|-[A-Za-z]+[:=])")     # dd of=<f>, -Path:<f>, -FilePath=<f>
# Designatori di stream che Win32 apre come il file o la cartella stessa (stessa regola di
# profile_firewall._STREAM): `f::$DATA`, `cartella::$INDEX_ALLOCATION\f`, `cartella:$I30:$INDEX_ALLOCATION\f`
_STREAM = re.compile(r"(?:::[\\`]?\$data|::[\\`]?\$index_allocation|:[\\`]?\$i30:[\\`]?\$index_allocation)"
                     r"(?=[\\/]|[. ]*$)", re.I)


def _non_default_profile() -> bool:
    # Un profilo illeggibile o ignoto conta come non default: si sceglie il lato restrittivo.
    try:
        return sc_profile.active_profile() != sc_profile.DEFAULT_PROFILE
    except sc_profile.ProfileError:
        return True


def policy_file() -> Path:
    # Fuori dal profilo di default l'override via ambiente e' ignorato: conta solo il flag
    # del profilo stesso (git resta attivabile con `/git true`).
    override = os.environ.get("SUPERCLAUDE_GIT_POLICY_FILE", "").strip()
    if override and not _non_default_profile():
        return Path(override)
    assert core.DATA_HOME is not None  # con PROFILE_ERROR gli handler non vengono caricati
    return core.DATA_HOME / sc_profile.GIT_POLICY_REL


def git_disabled() -> bool:
    # Profilo di default: flag assente o illeggibile = abilitato (fail-open).
    # Altri profili: flag assente o illeggibile = disabilitato (fail-closed); solo un
    # flag esplicitamente abilitante sblocca git. La regola sta in sc_gitflag.
    # Con sc_gitflag rotta git e' bloccato in ogni profilo: un difetto del codice
    # della guardia non e' uno stato del flag e non deve annullare /git false.
    return _flag_state()[0]


def _flag_state() -> tuple[bool, bool]:
    # (git bloccato, lib funzionanti): un difetto delle lib, anche alla chiamata, blocca git.
    # Ogni difetto finisce in hooks.log con la lib responsabile, come promette il diniego.
    if _GITFLAG is None:
        return True, False
    try:
        path, fail_closed = policy_file(), _non_default_profile()
    except _LIB_DEFECTS as exc:  # noqa: BLE001 - sc_profile: costante rinominata o errore interno
        core.log_error("call/policy_file", exc)
        return True, False
    try:
        verdict = _GITFLAG.git_disabled(path, fail_closed)
    except _LIB_DEFECTS as exc:  # noqa: BLE001 - funzione assente, firma cambiata o errore interno
        core.log_error("call/sc_gitflag", exc)
        return True, False
    if not isinstance(verdict, bool):
        core.log_error("call/sc_gitflag", TypeError(f"git_disabled returned {type(verdict).__name__}, not bool"))
        return True, False
    return verdict, True


def _gate_token(word: str, left: str = "") -> re.Pattern[str]:
    """`word` come token intero del testo: quoting, escape e continuazioni di riga ammessi
    al suo interno (`--take"-"over`, `--take\\` + a capo + `-over`, che bash ricuce), ai lati
    un separatore, un carattere di quoting o i bordi del testo; a sinistra anche i
    caratteri di `left`.

    Il quoting ai lati conta come bordo perche' PowerShell spezza `"x"--take-over` e
    `'--take-over'x` in due argomenti, mentre bash li unisce (lato sicuro per bash).
    """
    quoting = re.escape(_QUOTING)
    joiner = f"(?:[{quoting}]|(?<=[\\\\`])\\r?\\n)*"   # a capo solo dopo `\` o backtick
    inner = joiner.join(re.escape(ch) for ch in word)
    glued = f"[^{_GATE_SEP}{quoting}]"                # carattere che allungherebbe il token
    glued_left = f"[^{_GATE_SEP}{quoting}{re.escape(left)}]"
    return re.compile(f"(?<!{glued_left}){inner}(?!{glued})")


_SYNC = _gate_token("sync")
# `-` e `+` a sinistra non allungano l'opzione: `-$'-take-over'` e `${F:+...}` la consegnano intera
_OWNER_FLAG_TOKENS = tuple((flag, _gate_token(flag, "-+")) for flag in _OWNER_FLAGS)


def _owner_only_sync(cmd: str) -> list[str]:
    """Opzioni HUMAN GATE scritte nel comando, se vi compare anche il token `sync`.

    Si guarda il testo intero, non i segmenti, e il testo tra virgolette conta come
    codice: per Start-Process (saps), wt, xargs, `$'...'` o un array `@(...)` la stringa
    citata E' la riga di comando del processo lanciato. Il programma non si riconosce dal
    nome (`mem`, `mem.ps1`, `memory_db.py`, una variabile).

    Confronto esatto, senza abbreviazioni: il parser di `mem sync` usa allow_abbrev=False,
    quindi `--take`, `--all` o `--allow` non attivano nulla e passano. Un token piu' lungo
    (`--take-over-notes.md`) o dentro un percorso (`docs/--take-over`, `./--take-over`)
    non e' l'opzione per argparse e passa; `\\` invece e' un escape, quindi `.\\--take-over`
    viene negato (lato sicuro).

    `$` e `^` contano come quoting e `-`/`+` come bordo sinistro dell'opzione: espansioni
    con l'opzione scritta per intero (`${F:-...}`, `${F:+...}`, `-$'-take-over'`,
    `--take$()-over`, `...$null`) e il caret di cmd (`--take^-over`) vengono negati.
    Contano come quoting anche le virgolette tipografiche di PowerShell (da U+2018 a
    U+201E): `sync` o l'opzione racchiusi tra virgolette curve vengono negati.

    Falso positivo accettato (lato sicuro): un testo che nomina l'opzione nello stesso
    comando di `sync` (`echo "mem sync --take-over"`, un messaggio di commit) viene negato;
    quel testo va scritto in un file. Restano fuori (euristica, non un confine di sicurezza)
    un'opzione assente dal testo del comando, in un file o uno script scritto da un'altra
    chiamata; una composta con sequenze di escape (codici esadecimali in `$'...'`, `[char]45`)
    o per concatenazione (`"--take" + "-over"`); una con un nome espanso al suo interno
    (`--take$null-over`, `--take${E}-over`).
    """
    low = cmd.lower()
    if not _SYNC.search(low):
        return []
    return [flag for flag, token in _OWNER_FLAG_TOKENS if token.search(low)]


def _blocked_verb(call: _util.GitCall) -> str | None:
    """Descrizione del verbo vietato, oppure None se l'invocazione è innocua."""
    verb, args = call.verb, call.args
    if verb in _MUTATING:
        if verb in _ABORTABLE and any(a in ("--abort", "--quit") for a in args):
            return None
        return f"git {verb}"
    if verb == "reset" and "--hard" in args:
        return "git reset --hard"
    if verb == "tag":
        # senza argomenti o con un'opzione di sola lettura elenca soltanto
        if not args or any(a.split("=")[0] in _TAG_READONLY for a in args):
            return None
        return "git tag"
    return None


def _writes_flag(cmd: str, cwd: str) -> bool:
    flag = _util.canon(policy_file())
    if not flag:
        return False

    def is_flag(tok: str, here: str) -> bool:
        tok = _STREAM.sub("", _ARG_PREFIX.sub("", _util.unquote(tok)))
        return bool(tok) and not tok.startswith("-") and _util.canon(tok, here or None) == flag

    for seg, here in _util.walk(cmd, cwd):
        if any(is_flag(t, here) for t in _REDIRECT.findall(seg)):
            return True
        if _WRITERS.match(seg) and any(is_flag(t, here) for t in _util.tokens(seg)[1:]):
            return True
    return False


def _git_denied(what: str, hint: str, lib_ok: bool) -> core.Result:
    # Con la lib rotta /git true non servirebbe: il messaggio dice al proprietario la causa vera
    if not lib_ok:
        return core.Result.deny(
            "git-policy could not get a valid /git decision from lib/sc_gitflag.py or lib/sc_profile.py (see "
            f"hooks.log), so git stays blocked in every profile: '{what}' is blocked. Ask the owner to fix the lib "
            "or to perform the operation manually"
        )
    return core.Result.deny(f"git is disabled by /git false: '{what}' is blocked. {hint}")


@core.on("PreToolUse", tools=core.SHELL_TOOLS, name=NAME)
def handle(ctx: core.Ctx) -> core.Result | None:
    cmd = _util.command_of(ctx)
    if not cmd.strip():
        return None
    cwd = ctx.cwd if isinstance(ctx.cwd, str) else ""

    # 1. HUMAN GATE di `mem sync`: ogni ruolo, qualunque stato del flag, prima del filtro su git/gh
    # e di ogni chiamata a una lib (un difetto di sc_profile o sc_gitflag non lo raggiunge).
    # Sul testo grezzo: clean() fa di `\` + a capo uno spazio, mentre bash lo toglie e ricuce la parola.
    raw = _util.field(ctx, "command")
    owner_flags = _owner_only_sync(raw[:_util.MAX_CMD] if isinstance(raw, str) else cmd)
    if owner_flags:
        return core.Result.deny(
            f"'mem sync {' '.join(owner_flags)}' is owner-only (HUMAN GATE): agents never pass it nor launch it "
            "themselves (Start-Process, a new terminal, a script). Report the refusal and ask the owner to run "
            "the command in a real terminal. A text that only mentions the option next to 'sync' is blocked "
            "too: write that text to a file"
        )

    # 2. Auto-sblocco: vale con qualunque stato del flag. Filtro economico prima del parsing.
    # Se sc_profile non da' il percorso del flag il controllo salta, ma il passo 3 tratta git come
    # bloccato (stesso difetto, lib_ok False): l'eccezione non deve arrivare al fail-open di core.
    try:
        flag_name = policy_file().name.lower() if ctx.role == "worker" else ""
    except _LIB_DEFECTS as exc:  # noqa: BLE001 - sc_profile: costante rinominata o errore interno
        core.log_error("call/policy_file", exc)
        flag_name = ""
    if flag_name and flag_name in cmd.lower() and _writes_flag(cmd, cwd):
        return core.Result.deny("workers cannot write the git policy flag; ask the owner to run /git true")

    # 3. Verbi vietati mentre il flag dice `disabled`.
    low = cmd.lower()
    if "git" not in low and "gh" not in low:
        return None
    disabled, lib_ok = _flag_state()
    if not disabled:
        return None
    for call in _util.git_calls(cmd, cwd):
        what = _blocked_verb(call)
        if what:
            return _git_denied(what, "Ask the owner to run /git true or to perform the operation manually", lib_ok)
    for args in _util.gh_calls(cmd):
        if args[:2] in (["pr", "merge"], ["release", "create"]):
            return _git_denied(f"gh {' '.join(args[:2])}", "Ask the owner to run /git true", lib_ok)
    return None
