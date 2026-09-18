"""baseline-stash: fotografa lo stato git dei repo "no-commit" alla prima scrittura.

PreToolUse su Write/Edit/MultiEdit, una sola volta per sessione: alla prima
scrittura che cade in un repo "no-commit" (le scritture altrove non consumano il
marcatore e costano solo la lettura di un file piccolo). Un repo è "no-commit"
se `CLAUDE_COMMIT_POLICY=false` oppure se il nome del progetto è elencato in
`~/.claude/hooks/no-commit-projects.local`. In quel caso salva
`git status --porcelain` e `git diff` sotto `~/.claude/tmp/` e dice al modello
dove sono, così i reviewer non attribuiscono a questa sessione modifiche già
presenti nell'albero sporco.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from sc_hooks import core
from sc_hooks.core import Ctx, Result, on

CONFIG_FILE = core.CLAUDE_HOME / "hooks" / "no-commit-projects.local"
GIT_TIMEOUT_S = 5


def _fs(raw: object, cwd: str = "") -> Path | None:
    """Percorso utilizzabile dal filesystem Windows (accetta anche `/c/Users/...`)."""
    s = str(raw or "").strip().strip('"')
    if not s:
        return None
    m = re.match(r"^/([a-zA-Z])(/|$)", s)
    if m:
        s = f"{m.group(1)}:/{s[m.end():]}"
    p = Path(s).expanduser()
    if not p.is_absolute() and cwd:
        base = _fs(cwd)
        p = base / p if base else p
    return p


def _no_commit_names() -> set[str]:
    """Nomi di progetto (minuscoli) dal file di configurazione; vuoto se manca."""
    try:
        lines = CONFIG_FILE.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return set()
    return {ln.strip().strip("/\\").lower() for ln in lines if ln.strip() and not ln.strip().startswith("#")}


def _git(repo: Path, *args: str) -> bytes | None:
    """Output di `git -C <repo> ...` oppure None su errore, timeout o git assente."""
    import subprocess
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, timeout=GIT_TIMEOUT_S, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _candidate_dirs(ctx: Ctx) -> list[Path]:
    """Cartella del file scritto (primo antenato esistente), poi la cwd."""
    out: list[Path] = []
    target = _fs(ctx.file_path, ctx.cwd)
    if target is not None:
        for parent in target.parents:
            try:
                if parent.is_dir():
                    out.append(parent)
                    break
            except OSError:
                break
    cwd = _fs(ctx.cwd)
    if cwd is not None:
        out.append(cwd)
    return out


@on("PreToolUse", tools=("Write", "Edit", "MultiEdit"), name="baseline-stash")
def handle(ctx: Ctx) -> Result | None:
    dirs = _candidate_dirs(ctx)
    if os.environ.get("CLAUDE_COMMIT_POLICY", "").strip().lower() != "false":
        names = _no_commit_names()
        if not names:
            return None
        # Un progetto è "no-commit" se il suo nome è un segmento del percorso
        dirs = [d for d in dirs if names & set(core.norm_path(d).split("/"))]
    if not dirs or not ctx.once("baseline-stash"):
        return None                                   # una sola fotografia per sessione

    repo: Path | None = None
    for d in dirs:
        top = _git(d, "rev-parse", "--show-toplevel")
        if top and top.strip():
            repo = Path(top.decode("utf-8", "replace").strip())
            break
    if repo is None:
        return None

    sid = ctx.state_file("x").parent.name
    written: list[str] = []
    for suffix, args in (("txt", ("status", "--porcelain")), ("diff", ("diff",))):
        data = _git(repo, *args)
        if data is None:
            continue
        dest = core.TMP_DIR / f"{sid}-baseline.{suffix}"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        except OSError:
            continue
        written.append(dest.as_posix())
    if not written:
        return None
    return Result.note(
        f"BASELINE STASHED for no-commit repo {repo.as_posix()}. Pre-existing changes (NOT from this "
        f"session): see {' and '.join(written)}. Pass these paths to any w-reviewer dispatch prompt "
        "to avoid false-positive REJECTs."
    )
