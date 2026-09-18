"""Guardia commit-gate: disciplina di add / commit / push
(ex guards/30-commit-gate.sh e modules/25-commit-gate.sh).

- `git commit`: BLOCCA se le righe aggiunte in stage contengono una stringa a
  forma di segreto; AVVISA se l'oggetto non segue il formato convenzionale.
- `git add -A` / `git add .`: AVVISA (stage esplicito dei singoli percorsi).
- `git push`: promemoria di una riga, conferma con l'utente.

Il controllo "diff di soli permessi" della versione bash è stato tolto: qui
core.fileMode è false. Fail-open su ogni errore di git (assente, non un repo,
timeout).
"""
from __future__ import annotations

import re

from sc_hooks import core
from sc_hooks.handlers import _util

NAME = "commit-gate"

GIT_TIMEOUT_S = 5
_MAX_DIFF = 4_000_000

_BULK_ADD = frozenset({".", "-A", "--all", "-u", "--update", "-Au", "-uA", "-au", "./", "*"})
_CONVENTIONAL = re.compile(r"^\s*(feat|fix|test|docs|chore|refactor|style|ci|perf|build)(\([^)]+\))?!?:\s")

# Il ramo per parola chiave richiede un VALORE a forma di segreto (16+ caratteri
# base64/hex), non solo un nome: `MAX_TOKEN_COUNT = 100` non deve bloccare.
_SECRET = re.compile(
    r"AKIA[0-9A-Z]{16}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|[A-Z_]*(?:SECRET|TOKEN|PASSWORD|API_?KEY)[A-Z_0-9]*\s*=\s*[\"']?[A-Za-z0-9+/=_-]{16,}"
)

_MSG_FLAG = re.compile(r"""(?:^|\s)(?:-[a-zA-Z]*m|--message)(?:\s+|=)""")
_HERE_STRING = re.compile(r"""^@["']\s*\r?\n""")


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def commit_subject(segment: str) -> str:
    """Oggetto del commit da `-m "..."`, `-m '...'`, heredoc bash o here-string PowerShell."""
    m = _MSG_FLAG.search(segment)
    if not m:
        return ""
    rest = segment[m.end():]
    if _HERE_STRING.match(rest):                       # -m @' ... '@
        return _first_line(rest.split("\n", 1)[1])
    if rest[:1] in ("'", '"'):
        end = rest.find(rest[0], 1)
        body = rest[1:end] if end > 0 else rest[1:]
    else:
        body = rest.split(None, 1)[0] if rest.split() else ""
    if "<<" in body or "$(" in body:                   # -m "$(cat <<'EOF' ... EOF)"
        after = segment[m.end():].split("<<", 1)[-1]
        return _first_line(after.split("\n", 1)[1]) if "\n" in after else ""
    return _first_line(body)


def staged_additions(repo: str, include_worktree: bool = False) -> list[tuple[str, str]] | None:
    """Coppie (file, riga aggiunta) del diff in stage; None se git non risponde."""
    import subprocess  # import pigro: serve solo su `git commit`

    args = ["git", "-C", repo, "diff", "HEAD" if include_worktree else "--cached",
            "-U0", "--no-color", "--no-ext-diff"]
    try:
        proc = subprocess.run(
            args, capture_output=True, timeout=GIT_TIMEOUT_S, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out: list[tuple[str, str]] = []
    current = ""
    for line in proc.stdout[:_MAX_DIFF].decode("utf-8", errors="replace").splitlines():
        if line.startswith("+++ "):
            current = line[4:].removeprefix("b/")
        elif line.startswith("+"):
            out.append((current, line[1:]))
    return out


def _secret_files(call: _util.GitCall) -> list[str]:
    with_worktree = any(a in ("-a", "--all") or re.fullmatch(r"-[a-zA-Z]*a[a-zA-Z]*", a) for a in call.args)
    added = staged_additions(call.repo, include_worktree=with_worktree)
    if not added:
        return []
    return sorted({name or "?" for name, line in added if _SECRET.search(line)})


@core.on("PreToolUse", tools=core.SHELL_TOOLS, name=NAME)
def handle(ctx: core.Ctx) -> core.Result | None:
    cmd = _util.command_of(ctx)
    if "git" not in cmd.lower():
        return None
    cwd = ctx.cwd if isinstance(ctx.cwd, str) else ""
    notes: list[str] = []

    for call in _util.git_calls(cmd, cwd):
        if call.verb == "add":
            bulk = next((a for a in call.args if a in _BULK_ADD), None)
            if bulk:
                notes.append(f"commit-gate: bulk 'git add {bulk}' is discouraged; stage explicit pathspecs only.")
        elif call.verb == "commit":
            files = _secret_files(call)
            if files:
                return core.Result.deny(
                    "staged diff adds a secret-shaped string (AWS key shape, private key header, or a "
                    f"SECRET/TOKEN/PASSWORD/API_KEY assignment) in: {', '.join(files[:5])}. Remove it before committing"
                )
            amend_keeps_msg = "--amend" in call.args and not _MSG_FLAG.search(call.segment)
            subject = "" if amend_keeps_msg else commit_subject(call.segment)
            if subject and not _CONVENTIONAL.match(subject):
                notes.append(
                    "commit-gate: commit subject does not match the conventional format "
                    "feat|fix|test|docs|chore|refactor|style|ci|perf|build(scope)?: <text>."
                )
        elif call.verb == "push":
            notes.append("commit-gate: push detected. Confirm with the user before pushing; never push without explicit instruction.")

    if not notes:
        return None
    return core.Result(context=list(dict.fromkeys(notes)))
