"""Guardia write-acl: ACL di scrittura per percorso e ruolo (ex guards/20-write-acl.sh).

Regole:
- tutti i ruoli: mai toccare `<progetto>/.claude/` o `CLAUDE.md` / `CLAUDE.local.md`
  sotto la radice di lavoro `~/code/`;
- meta e worker: i settings (sc_profile.SETTINGS_FILES) di ~/.claude e della data home
  attiva sono del proprietario;
- solo worker: plan.md / context.md dei piani, il flag git-policy, e le cartelle
  hooks/, rules/, agents/ (tranne agents/_ephemeral/ e agents/_pending_promotion/).

Piani e flag git-policy sono DATI e si cercano nella data home del profilo attivo;
gli alberi hooks/rules/agents sono CODICE condiviso e restano in ~/.claude.
"""
from __future__ import annotations

from sc_hooks import core
from sc_hooks.handlers import _util

import sc_profile  # noqa: E402 - core ha gia' aggiunto <code home>/lib a sys.path

NAME = "write-acl"

_PROJECT_FILES = {"claude.md": "CLAUDE.md", "claude.local.md": "CLAUDE.local.md"}
_WORKER_TREES = ("hooks", "rules", "agents")
_WORKER_AGENT_EXCEPTIONS = ("agents/_ephemeral/", "agents/_pending_promotion/")
_PLAN_FILES = ("plan.md", "context.md")


def _worker_denied(rel_code: str | None, rel_data: str | None) -> str | None:
    """Motivo del blocco per un worker, dati i percorsi relativi a code home e data home."""
    if rel_data is not None:
        parts = rel_data.split("/")
        if parts[0] == "plans" and len(parts) >= 2 and parts[-1] in _PLAN_FILES:
            assert core.DATA_HOME is not None  # rel_data esiste solo se la data home e' nota
            return f"{parts[-1]} under ~/{core.DATA_HOME.name}/plans is meta-only; report the change to meta instead"
        if rel_data == sc_profile.GIT_POLICY_REL:
            return "the git policy flag is not writable by workers; ask the owner to run /git true"
    if rel_code is None:
        return None
    parts = rel_code.split("/")
    if parts[0] in _WORKER_TREES:
        if any(rel_code.startswith(exc) for exc in _WORKER_AGENT_EXCEPTIONS):
            return None
        return (f"~/.claude/{parts[0]}/ is read-only for workers"
                + ("; new agents go under agents/_ephemeral/ or agents/_pending_promotion/" if parts[0] == "agents" else ""))
    return None


def _owner_settings(path: str) -> str | None:
    """Forma `~/<home>/<file>` del file settings colpito da `path`, oppure None.

    Si proteggono i settings del profilo attivo e quelli di ~/.claude (profilo
    superclaude) anche da deepseek; senza SC_PROFILE i due insiemi coincidono.
    """
    for profile in {core.PROFILE, sc_profile.DEFAULT_PROFILE}:
        for target in sc_profile.settings_paths(profile):
            if path == _util.canon(target):
                return f"~/{target.parent.name}/{target.name}"
    return None


@core.on("PreToolUse", tools=core.WRITE_TOOLS, name=NAME)
def handle(ctx: core.Ctx) -> core.Result | None:
    raw = _util.path_of(ctx)
    if not raw:
        return None
    path = _util.canon(raw, ctx.cwd)
    if not path:
        return None

    rel_ws = _util.rel(path, core.WORKSPACE_ROOT)
    if rel_ws is not None:
        parts = rel_ws.split("/")
        if ".claude" in parts:
            return core.Result.deny(f"project-local .claude/ is never written by agents; path={path}")
        if parts[-1] in _PROJECT_FILES:
            return core.Result.deny(f"project-local {_PROJECT_FILES[parts[-1]]} is never written by agents; path={path}")
        return None

    rel_code = _util.rel(path, core.CODE_HOME)
    rel_data = _util.rel(path, core.DATA_HOME)
    if (rel_code is None and rel_data is None) or ctx.role not in ("meta", "worker"):
        return None
    settings = _owner_settings(path)
    if settings:
        return core.Result.deny(f"{settings} is owner-run-only; propose the change to the user instead")
    if ctx.role == "worker":
        reason = _worker_denied(rel_code, rel_data)
        if reason:
            return core.Result.deny(f"{reason}; agent={ctx.agent or 'worker'} path={path}")
    return None
