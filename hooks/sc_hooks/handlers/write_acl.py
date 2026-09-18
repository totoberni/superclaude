"""Guardia write-acl: ACL di scrittura per percorso e ruolo (ex guards/20-write-acl.sh).

Regole:
- tutti i ruoli: mai toccare `<progetto>/.claude/` o `CLAUDE.md` / `CLAUDE.local.md`
  sotto la radice di lavoro `~/code/`;
- meta e worker: `~/.claude/settings.json` e `settings.local.json` sono del proprietario;
- solo worker: plan.md / context.md dei piani, il flag git-policy, e le cartelle
  hooks/, rules/, agents/ (tranne agents/_ephemeral/ e agents/_pending_promotion/).
"""
from __future__ import annotations

from sc_hooks import core
from sc_hooks.handlers import _util

NAME = "write-acl"

_PROJECT_FILES = {"claude.md": "CLAUDE.md", "claude.local.md": "CLAUDE.local.md"}
_SETTINGS = ("settings.json", "settings.local.json")
_WORKER_TREES = ("hooks", "rules", "agents")
_WORKER_AGENT_EXCEPTIONS = ("agents/_ephemeral/", "agents/_pending_promotion/")
_PLAN_FILES = ("plan.md", "context.md")


def _worker_denied(rel: str) -> str | None:
    """Motivo del blocco per un worker, dato il percorso relativo a ~/.claude."""
    parts = rel.split("/")
    if parts[0] == "plans" and len(parts) >= 2 and parts[-1] in _PLAN_FILES:
        return f"{parts[-1]} under ~/.claude/plans is meta-only; report the change to meta instead"
    if rel == "config/git-policy":
        return "the git policy flag is not writable by workers; ask the owner to run /git true"
    if parts[0] in _WORKER_TREES:
        if any(rel.startswith(exc) for exc in _WORKER_AGENT_EXCEPTIONS):
            return None
        return (f"~/.claude/{parts[0]}/ is read-only for workers"
                + ("; new agents go under agents/_ephemeral/ or agents/_pending_promotion/" if parts[0] == "agents" else ""))
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

    rel_home = _util.rel(path, core.CLAUDE_HOME)
    if rel_home is None or ctx.role not in ("meta", "worker"):
        return None
    if rel_home in _SETTINGS:
        return core.Result.deny(f"~/.claude/{rel_home} is owner-run-only; propose the change to the user instead")
    if ctx.role == "worker":
        reason = _worker_denied(rel_home)
        if reason:
            return core.Result.deny(f"{reason}; agent={ctx.agent or 'worker'} path={path}")
    return None
