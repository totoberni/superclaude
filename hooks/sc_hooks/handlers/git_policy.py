"""Guardia git-policy: applica il flag `/git true|false` (ex guards/26-git-policy.sh).

Il flag vive nella data home del profilo attivo, in sc_profile.GIT_POLICY_REL (sovrascrivibile con
SUPERCLAUDE_GIT_POLICY_FILE per i test). Come nella versione bash la lettura è
fail-open: il blocco scatta SOLO se il file esiste, si legge e dice `disabled`
(si accettano anche `false`, `off`, `0`, `no`); file assente o contenuto ignoto
valgono `enabled`.

Euristica dichiarata, non un confine di sicurezza: non modella sostituzioni di
comando, variabili, interpreti che pilotano git, alias preesistenti.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from sc_hooks import core
from sc_hooks.handlers import _util

import sc_profile  # noqa: E402 - core ha gia' aggiunto <code home>/lib a sys.path

NAME = "git-policy"

_DISABLED_WORDS = ("disabled", "false", "off", "0", "no")

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


def policy_file() -> Path:
    override = os.environ.get("SUPERCLAUDE_GIT_POLICY_FILE", "").strip()
    if override:
        return Path(override)
    assert core.DATA_HOME is not None  # con PROFILE_ERROR gli handler non vengono caricati
    return core.DATA_HOME / sc_profile.GIT_POLICY_REL


def git_disabled() -> bool:
    try:
        word = policy_file().read_text(encoding="utf-8-sig", errors="replace").strip().lower()
    except (OSError, ValueError):
        return False
    return word in _DISABLED_WORDS


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
        tok = _ARG_PREFIX.sub("", _util.unquote(tok))
        return bool(tok) and not tok.startswith("-") and _util.canon(tok, here or None) == flag

    for seg, here in _util.walk(cmd, cwd):
        if any(is_flag(t, here) for t in _REDIRECT.findall(seg)):
            return True
        if _WRITERS.match(seg) and any(is_flag(t, here) for t in _util.tokens(seg)[1:]):
            return True
    return False


@core.on("PreToolUse", tools=core.SHELL_TOOLS, name=NAME)
def handle(ctx: core.Ctx) -> core.Result | None:
    cmd = _util.command_of(ctx)
    if not cmd.strip():
        return None
    cwd = ctx.cwd if isinstance(ctx.cwd, str) else ""

    # 1. Auto-sblocco: vale con qualunque stato del flag. Filtro economico prima del parsing.
    if ctx.role == "worker" and policy_file().name.lower() in cmd.lower() and _writes_flag(cmd, cwd):
        return core.Result.deny("workers cannot write the git policy flag; ask the owner to run /git true")

    # 2. Verbi vietati mentre il flag dice `disabled`.
    low = cmd.lower()
    if "git" not in low and "gh" not in low:
        return None
    if not git_disabled():
        return None
    for call in _util.git_calls(cmd, cwd):
        what = _blocked_verb(call)
        if what:
            return core.Result.deny(
                f"git is disabled by /git false: '{what}' is blocked. Ask the owner to run /git true "
                "or to perform the operation manually"
            )
    for args in _util.gh_calls(cmd):
        if args[:2] in (["pr", "merge"], ["release", "create"]):
            return core.Result.deny(
                f"git is disabled by /git false: 'gh {' '.join(args[:2])}' is blocked. Ask the owner to run /git true"
            )
    return None
