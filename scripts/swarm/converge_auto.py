#!/usr/bin/env python3
"""converge_auto.py: the supervised-autonomous convergence driver.

WHY this exists: interactive /converge keeps a human in the loop (the conductor
pastes the /goal block, arms the engine, quotes verdicts). converge-auto is the
overnight/trusted variant where the owner's SINGLE consent act is launching this
process; after that no human pastes anything. Because no human is watching, every
no-pre-approval guarantee from verdict-schema.md must be enforced MECHANICALLY,
not by convention:

  - Reviewer isolation (guard 1): review/seal prompts carry artifact + diff +
    rubric ONLY. No punch list, no producer text, no history, no prior verdicts.
    Every prompt is archived to prompts/ BEFORE the session spawns, so a hostile
    gate can verify isolation by inspecting the exact bytes that were sent.
  - Producer token ban (guard 2): a producer that authors a VERDICT/SEAL line has
    self-certified; the driver refuses to count it.
  - Fresh seal (guard 3/5): reviewer and seal phases run --no-session-persistence,
    making --resume structurally impossible, so a seal can never inherit a prior
    session's approval. A mid-seal content mutation voids the seal.
  - Stale-seal immunity (guard 4): the terminal check reads only THIS round's
    hash-verified seal against a monotonic round counter.
  - No silent retry (guard 6): budget breach, timeout, FAILED, second PARTIAL,
    malformed-twice, or a non-decreasing findings trend all ESCALATE and stop.

The revision a SEAL binds to depends on mode. In a git repo the driver commits
the artifact scope (a pre-seal snapshot commit) and binds the seal to that real
commit hash (git rev-parse HEAD), quoted as commit:<12hex>; guard 3 re-verifies
HEAD unchanged AND the scope clean after the seal returns. For non-git artifacts
the binding is a content manifest (sha256 over the sorted per-file byte hashes of
artifact_paths), quoted as sha256:<12hex>, detecting any edit including
uncommitted ones. The content manifest is also the cheapest detector for the
produce-to-review window and is retained for that check in both modes.

Parallel mode (--parallel <manifest.json>) forks up to 5 CHILD PROCESSES of this
same script, one per loop config, each a normal single-loop run with its own
runtime dir, handoff, ledger, prompts, and raw returns. There is NO shared state
between children and NO cross-loop approval transfer: every loop seals independently
by construction (its own fresh seal auditor against its own bound revision), so one
loop's seal can never stand in for another. The parent validates the whole manifest
(1..5 loops, each config valid, artifact scopes disjoint, loop_ids unique) BEFORE
spawning anything, waits on all children, and writes parallel-summary.md.

Runs under ~/.claude/.venv/bin/python and system python3 alike (stdlib only).
Design SOT: ~/.claude/plans/wf-autonomy/checkpoints/w1-design.md.
Token grammar SOT: ~/.claude/skills/_shared/verdict-schema.md.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# --- Constants ---------------------------------------------------------------

DEFAULT_ALLOWED_TOOLS = ["Bash"]
READ_ONLY_DENY = "Write,Edit,NotebookEdit"
# Sentinel identifying our post-commit hook so --install-void-hook is idempotent.
HOOK_MARKER = "converge-auto-seal-void-hook"
PREFLIGHT_TIMEOUT_S = 30
NOTIFY_TIMEOUT_S = 30
# Margin added to the config-derived lock wait bound (2 * phase_timeout_s covers a
# strict sibling's two seal sessions). Env seam for tests, like CONVERGE_NO_COMMIT_FILE.
COMMIT_LOCK_MARGIN_S = int(os.environ.get("CONVERGE_LOCK_MARGIN_S", "120"))
COMMIT_LOCK_POLL_S = 0.2
GATE_TAIL_CHARS = 2000
VALID_BARS = ("default", "gate", "strict")

# Anchored token grammar (verdict-schema.md). Line 1 of an agent's final message.
STATUS_RE = re.compile(
    r"^STATUS: (?P<word>DONE|PARTIAL|FAILED) files=(?P<files>\d+) checkpoint=(?P<ckpt>\S+)$"
)
VERDICT_RE = re.compile(
    r"^VERDICT: (?P<word>REWORK|CLEAN) blocking=(?P<b>\d+) major=(?P<m>\d+) "
    r"minor=(?P<mi>\d+) round=(?P<round>\d+)$"
)
SEAL_RE = re.compile(
    r"^SEAL: (?P<word>ACCEPTED|REJECTED) blocking=(?P<b>\d+) major=(?P<m>\d+) "
    r"minor=(?P<mi>\d+) nits=(?P<nits>\d+)$"
)
# Guard 2: a producer must never author a verdict/seal line anywhere in its result.
PRODUCER_TOKEN_RE = re.compile(r"^\s*(VERDICT|SEAL):", re.MULTILINE)

# Bar thresholds echoed verbatim into reviewer/seal prompts (verdict-schema.md).
BAR_TEXT = {
    "default": "blocking=0 major=0 (minors and nits are logged, they do not gate)",
    "gate": "blocking=0 major=0 minor=0 nits=0 (everything gates; single clean SEAL)",
    "strict": "blocking=0 major=0 minor=0 nits=0 AND two consecutive fresh clean SEALs",
}

# Verbatim clauses from verdict-schema.md quoted into review/seal prompts.
EVIDENCE_BAR = (
    "Evidence bar: a finding without a file:line citation (or equivalent: DOI or "
    "arxiv id, a re-run expected-vs-actual, or a named principle plus clause) is "
    "dropped before counting. A zero-finding review is a valid outcome."
)
ANTI_HACKING = (
    "Anti-hacking sweep (automatic blocking, any scope): test-file edits that "
    "special-case, weakened assertions, harness escapes (e.g. forced exit-0), "
    "skipped or deleted coverage, diagnostic theater in infra tests or health scripts."
)
NO_PRE_APPROVAL = (
    "No pre-approval: your verdict must derive from a fresh, explicit examination "
    "of the CURRENT state of the work, with evidence gathered THIS round. Approval "
    "never transfers across rounds. A SEAL binds to a specific artifact revision; "
    "any later change to the artifact voids it. The producer's completion is a "
    "signal, not an approval; a SEAL must examine the COMPLETE final state, not a delta."
)


class ConfigError(Exception):
    """Raised on any config validation failure; the caller exits 3."""


# --- Pure helpers ------------------------------------------------------------


def iso_now() -> str:
    """UTC ISO8601 with no interior whitespace (keeps ledger headers regex-parseable)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def counts_within_bar(bar: str, blocking: int, major: int, minor: int, nits: int) -> bool:
    if blocking or major:
        return False
    if bar in ("gate", "strict"):
        return minor == 0 and nits == 0
    return True


def _as_arg_list(value, field: str) -> list[str]:
    """Normalize a command config value to an argv list; a bare string is one arg.

    We never shell-split (shlex is display-only per house rules) and never use
    shell=True: a string is treated as a single executable path, a list is used
    verbatim as argv. Owners needing arguments pass a JSON list or a wrapper script.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return list(value)
    raise ConfigError(f"{field} must be a string or a list of strings")


# --- Config load and validation ----------------------------------------------


def load_config(cfg_path: Path) -> dict:
    try:
        return json.loads(cfg_path.read_text())
    except (json.JSONDecodeError, ValueError) as exc:
        raise ConfigError(f"loop.json is not valid JSON: {exc}") from exc


def _require(cfg: dict, key: str, types: tuple) -> object:
    if key not in cfg or cfg[key] is None:
        raise ConfigError(f"missing required config key: {key}")
    if not isinstance(cfg[key], types):
        raise ConfigError(f"config key {key} has wrong type")
    return cfg[key]


def _validate_paths(cfg: dict) -> None:
    paths = _require(cfg, "artifact_paths", (list,))
    if not paths or not all(isinstance(p, str) and p for p in paths):
        raise ConfigError("artifact_paths must be a non-empty list of strings")
    spec = Path(_require(cfg, "task_spec_file", (str,))).expanduser()
    if not spec.is_file():
        raise ConfigError(f"task_spec_file does not exist: {spec}")
    rubric = Path(_require(cfg, "rubric_path", (str,))).expanduser()
    if not rubric.exists():
        raise ConfigError(f"rubric_path does not exist: {rubric}")


def _validate_repo(cfg: dict) -> None:
    repo = cfg.get("repo")
    if repo is None:
        return
    if not isinstance(repo, str):
        raise ConfigError("repo must be a string path or null")
    repo_dir = Path(repo).expanduser()
    if not repo_dir.is_dir():
        raise ConfigError(f"repo directory does not exist: {repo_dir}")
    if not (repo_dir / ".git").exists():
        raise ConfigError(f"repo is not a git worktree (set repo=null): {repo_dir}")


def _validate_numbers(cfg: dict) -> None:
    cap = _require(cfg, "rounds_cap", (int,))
    if isinstance(cap, bool) or cap < 1:
        raise ConfigError("rounds_cap must be an integer >= 1")
    budget = _require(cfg, "phase_budget_usd", (int, float))
    if isinstance(budget, bool) or budget <= 0:
        raise ConfigError("phase_budget_usd must be a positive number")
    timeout = _require(cfg, "phase_timeout_s", (int,))
    if isinstance(timeout, bool) or timeout <= 0:
        raise ConfigError("phase_timeout_s must be a positive integer")


def validate_config(cfg: dict) -> dict:
    """Validate and normalize; raise ConfigError (exit 3) before any phase runs."""
    for key in ("loop_id", "artifact_class", "producer_agent", "reviewer_agent", "seal_agent"):
        _require(cfg, key, (str,))
    if _require(cfg, "bar", (str,)) not in VALID_BARS:
        raise ConfigError(f"bar must be one of {VALID_BARS}")
    _validate_paths(cfg)
    _validate_repo(cfg)
    _validate_numbers(cfg)
    tools = cfg.get("allowed_tools") or DEFAULT_ALLOWED_TOOLS
    if not (isinstance(tools, list) and all(isinstance(x, str) for x in tools)):
        raise ConfigError("allowed_tools must be a list of strings")
    cfg["allowed_tools"] = tools
    cfg["test_cmd"] = _as_arg_list(cfg["test_cmd"], "test_cmd") if cfg.get("test_cmd") else None
    cfg["notify_cmd"] = _as_arg_list(cfg["notify_cmd"], "notify_cmd") if cfg.get("notify_cmd") else None
    for opt in ("producer_model", "producer_effort", "reviewer_model", "reviewer_effort",
                "seal_model", "seal_effort"):
        if cfg.get(opt) is not None and not isinstance(cfg[opt], str):
            raise ConfigError(f"{opt} must be a string or null")
    if cfg.get("allow_dirty") is not None and not isinstance(cfg["allow_dirty"], bool):
        raise ConfigError("allow_dirty must be a boolean or null")
    return cfg


# Default no-commit project list; overridable per-run so tests stay hermetic.
NO_COMMIT_LIST_DEFAULT = str(Path.home() / ".claude" / "hooks" / "no-commit-projects.local")


def detect_no_commit_policy(cfg: dict) -> bool:
    """Detect the per-project /commit false policy, mirroring the precedence in
    hooks/modules/15-baseline-stash.sh EXACTLY (that hook is the pattern SOT; the
    project list is READ from the same file, never duplicated here):

      1. CLAUDE_COMMIT_POLICY=false env wins;
      2. else the repo basename appears in the no-commit project list file
         (~/.claude/hooks/no-commit-projects.local by default, override via the
         CONVERGE_NO_COMMIT_FILE env var; blank and #comment lines ignored).

    A non-git loop (repo=null) is already in content-manifest mode, so this only
    decides the binding/diff mode for git repos.
    """
    if os.environ.get("CLAUDE_COMMIT_POLICY") == "false":
        return True
    repo = cfg.get("repo")
    if not repo:
        return False
    list_path = Path(os.environ.get("CONVERGE_NO_COMMIT_FILE", NO_COMMIT_LIST_DEFAULT)).expanduser()
    if not list_path.is_file():
        return False
    basename = Path(repo).expanduser().name
    for raw in list_path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == basename:
            return True
    return False


# --- The convergence loop ----------------------------------------------------


class Loop:
    """One artifact converged to a fresh seal, or escalated. Owns one runtime dir."""

    def __init__(self, cfg: dict, runtime_dir: Path, claude_cmd: str):
        self.cfg = cfg
        self.rt = runtime_dir
        self.claude = claude_cmd
        self.cwd = Path(cfg["repo"]).expanduser() if cfg.get("repo") else runtime_dir
        # /commit false composition: a git repo under no-commit policy behaves like
        # the repo=null path (content-manifest binding, snapshot diffs, no commits,
        # dirty-start refusal skipped). commit_mode gates every git-vs-manifest fork.
        self.no_commit = detect_no_commit_policy(cfg)
        self.commit_mode = bool(cfg.get("repo")) and not self.no_commit
        self.budget = str(cfg["phase_budget_usd"])
        self.timeout = cfg["phase_timeout_s"]
        self.open_findings = 0
        self.produce_files = 0
        self.produce_delta = ""
        self.state = {
            "loop_id": cfg["loop_id"], "round": 1, "phase": "produce", "status": "running",
            "punch_list": None, "findings_history": [], "producer_status_last": None,
            "manifest_hash_last": None, "sessions": {}, "seal": None,
            "producer_token_violations": 0,
        }

    # -- filesystem + subprocess plumbing --

    def log(self, msg: str) -> None:
        with open(self.rt / "driver.log", "a") as handle:
            handle.write(f"[{iso_now()}] {msg}\n")

    def _resolve(self, rel: str) -> Path:
        path = Path(rel).expanduser()
        return path if path.is_absolute() else self.cwd / path

    def write_handoff(self) -> None:
        tmp = self.rt / "handoff.json.tmp"
        tmp.write_text(json.dumps(self.state, indent=2))
        os.replace(tmp, self.rt / "handoff.json")

    def archive_prompt(self, round_k: int, phase: str, prompt: str) -> None:
        (self.rt / "prompts" / f"round-{round_k}-{phase}.txt").write_text(prompt)

    def append_ledger(self, round_k, phase, event, delta, token, findings, revision) -> None:
        tok = f"`{token}`" if token else "none"
        man = revision if revision else "n/a"
        row = (
            f"## R{round_k}.{phase} - {iso_now()} - {event}\n"
            f"- delta: {delta}\n"
            f"- token: {tok}\n"
            f"- findings-open: {findings}\n"
            f"- manifest: {man}\n\n"
        )
        with open(self.rt / "rounds.md", "a") as handle:
            handle.write(row)

    def notify(self, title: str, message: str) -> None:
        if not self.cfg.get("notify_cmd"):
            return
        cmd = list(self.cfg["notify_cmd"]) + [title, message]
        try:
            subprocess.run(cmd, timeout=NOTIFY_TIMEOUT_S, capture_output=True)
        except (OSError, subprocess.SubprocessError) as exc:
            self.log(f"notify_cmd failed: {exc}")

    def run_cli(self, cmd: list[str], raw_path: Path):
        """Run one headless claude phase. Returns (parsed_json_or_None, rc, timed_out)."""
        try:
            proc = subprocess.run(cmd, cwd=str(self.cwd), capture_output=True,
                                  text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raw_path.write_text(exc.stdout or "" if isinstance(exc.stdout, str) else "")
            return None, None, True
        except FileNotFoundError:
            return None, 127, False
        raw_path.write_text(proc.stdout)
        try:
            parsed = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        return (parsed if isinstance(parsed, dict) else None), proc.returncode, False

    def cli_result_or_escalate(self, parsed, rc, timed_out, round_k, phase) -> str:
        if timed_out:
            self.escalate(round_k, f"{phase} phase timeout")
        if parsed is None:
            self.escalate(round_k, f"{phase} CLI returned non-JSON (rc={rc})")
        if parsed.get("is_error") or (rc not in (0, None)):
            subtype = str(parsed.get("subtype", "")).lower()
            reason = "budget breach" if "budget" in subtype else f"CLI error ({subtype or rc})"
            self.escalate(round_k, f"{phase} {reason}")
        return parsed.get("result") or ""

    def _unique_token(self, compiled, text, phase):
        """Tolerant token extraction: find anchored full-line matches of a token
        grammar ANYWHERE in the result (re.MULTILINE), not only on line 1.

        Headless returns sometimes precede the token line with a narrative
        preamble; strict line-1 parsing refused otherwise-perfect returns. So
        EXACTLY ONE anchored match is accepted (a format warning is logged when it
        is not the first line). Zero or more-than-one matches return (None, count)
        so the caller takes its existing malformed path (retry once, then escalate).
        """
        matches = list(re.finditer(compiled.pattern, text, re.MULTILINE))
        if len(matches) != 1:
            return None, len(matches)
        match = matches[0]
        if text[:match.start()].strip():
            self.log(f"{phase} token not on line 1 (accepted, unique match): {match.group(0)}")
        return match, 1

    # -- command construction --

    def _phase_agent_overrides(self, phase: str):
        table = {
            "produce": ("producer_agent", "producer_model", "producer_effort"),
            "review": ("reviewer_agent", "reviewer_model", "reviewer_effort"),
            "seal": ("seal_agent", "seal_model", "seal_effort"),
        }
        agent_key, model_key, effort_key = table[phase]
        return self.cfg[agent_key], self.cfg.get(model_key), self.cfg.get(effort_key)

    def build_cmd(self, prompt: str, phase: str, resume_sid: str = None) -> list[str]:
        agent, model, effort = self._phase_agent_overrides(phase)
        cmd = [self.claude, "-p", prompt, "--output-format", "json",
               "--max-budget-usd", self.budget, "--permission-mode", "acceptEdits",
               "--allowedTools", ",".join(self.cfg["allowed_tools"]), "--agent", agent]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        if phase in ("review", "seal"):
            cmd += ["--no-session-persistence", "--disallowedTools", READ_ONLY_DENY]
        if resume_sid:
            cmd += ["--resume", resume_sid]
        return cmd

    # -- manifest and diff --

    def compute_manifest(self) -> str:
        entries = []
        for rel in self.cfg["artifact_paths"]:
            path = self._resolve(rel)
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "MISSING"
            entries.append(f"{rel}:{digest}")
        return sha256_text("\n".join(sorted(entries)))

    # -- revision binding (commit hash in git mode, content manifest otherwise) --

    @staticmethod
    def _rev_label(kind: str, full: str) -> str:
        """Ledger/prompt label for a bound revision: commit:<12hex> or sha256:<12hex>."""
        return f"{kind}:{full[:12]}"

    def _git_run(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.cwd), *args],
                              capture_output=True, text=True, timeout=60)

    def _git_head(self):
        try:
            proc = self._git_run("rev-parse", "HEAD")
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout.strip() if proc.returncode == 0 else None

    def _git_scope_dirty(self):
        """True/False if the artifact scope has uncommitted changes; None on git error."""
        try:
            proc = self._git_run("status", "--porcelain", "--", *self.cfg["artifact_paths"])
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return bool(proc.stdout.strip())

    def _pre_seal_commit(self, round_k: int) -> str:
        """Commit the artifact scope as a pre-seal snapshot; return the full HEAD hash.

        A clean scope (nothing to commit) is acceptable: HEAD already carries the
        current state, so the seal reuses it. Any other git failure ESCALATES.
        """
        paths = self.cfg["artifact_paths"]
        try:
            add = self._git_run("add", "--", *paths)
        except (OSError, subprocess.SubprocessError) as exc:
            self.escalate(round_k, f"pre-seal git add failed: {exc}")
        if add.returncode != 0:
            self.escalate(round_k, f"pre-seal git add failed: {add.stderr.strip()}")
        msg_path = self.rt / f"preseal-round-{round_k}.msg"
        msg_path.write_text(
            f"chore({self.cfg['loop_id']}): round {round_k} pre-seal snapshot\n\n"
            "Co-Authored-By: Claude <noreply@anthropic.com>\n"
        )
        try:
            commit = self._git_run("commit", "-F", str(msg_path))
        except (OSError, subprocess.SubprocessError) as exc:
            self.escalate(round_k, f"pre-seal git commit failed: {exc}")
        if commit.returncode != 0:
            blob = (commit.stdout + commit.stderr).lower()
            if "nothing to commit" not in blob and "no changes added" not in blob:
                self.escalate(round_k, f"pre-seal git commit failed: {commit.stderr.strip()}")
            self.log(f"pre-seal round {round_k}: nothing to commit, reusing current HEAD")
        head = self._git_head()
        if not head:
            self.escalate(round_k, "pre-seal could not resolve HEAD after commit")
        return head

    def _establish_bound_revision(self, round_k: int):
        """Return (full_revision, ledger_label) the seal binds to for this round."""
        if self.commit_mode:
            head = self._pre_seal_commit(round_k)
            return head, self._rev_label("commit", head)
        manifest = self.compute_manifest()
        return manifest, self._rev_label("sha256", manifest)

    def _revision_stable(self, bound_full: str) -> bool:
        """Guard 3: has the bound revision held since it was established?

        Git mode: HEAD unchanged AND the artifact scope clean. Non-git: the
        content manifest recomputes identically.
        """
        if self.commit_mode:
            return self._git_head() == bound_full and self._git_scope_dirty() is False
        return self.compute_manifest() == bound_full

    def snapshot_round0(self) -> None:
        if self.commit_mode:
            return
        snapdir = self.rt / "raw" / "snapshot-0"
        snapdir.mkdir(parents=True, exist_ok=True)
        for rel in self.cfg["artifact_paths"]:
            path = self._resolve(rel)
            data = path.read_bytes() if path.is_file() else b""
            (snapdir / rel.replace("/", "__").replace("\\", "__")).write_bytes(data)

    def capture_diff(self) -> str:
        if self.commit_mode:
            cmd = ["git", "-C", str(self.cwd), "diff", "--"] + self.cfg["artifact_paths"]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                return proc.stdout
            except (OSError, subprocess.SubprocessError) as exc:
                self.log(f"git diff failed, using empty diff: {exc}")
                return ""
        return self._diff_from_snapshot()

    def _diff_from_snapshot(self) -> str:
        chunks = []
        snapdir = self.rt / "raw" / "snapshot-0"
        for rel in self.cfg["artifact_paths"]:
            cur_p = self._resolve(rel)
            cur = cur_p.read_text(errors="replace") if cur_p.is_file() else ""
            snap_p = snapdir / rel.replace("/", "__").replace("\\", "__")
            old = snap_p.read_text(errors="replace") if snap_p.is_file() else ""
            diff = difflib.unified_diff(old.splitlines(keepends=True),
                                        cur.splitlines(keepends=True),
                                        fromfile=f"a/{rel}", tofile=f"b/{rel}")
            chunks.append("".join(diff))
        return "".join(chunks)

    def _artifact_dump(self) -> str:
        parts = []
        for rel in self.cfg["artifact_paths"]:
            path = self._resolve(rel)
            content = path.read_text(errors="replace") if path.is_file() else "(missing)"
            parts.append(f"----- {rel} -----\n{content}")
        return "\n\n".join(parts)

    # -- prompt construction (dispatch-contract four parts) --

    def build_producer_prompt(self, round_k: int, objective: str, checkpoint: str) -> str:
        allow = ", ".join(self.cfg["allowed_tools"])
        return (
            f"# Objective\n{objective}\n\n"
            f"# Tool guidance\nAllowed tools: {allow}. Make the minimal change set that "
            f"satisfies the objective; match existing patterns.\n\n"
            f"# Boundaries\nWrite ONLY these paths (the allowlist): "
            f"{', '.join(self.cfg['artifact_paths'])}. Touch nothing else. NEVER run git add, "
            f"git commit, or git push; version control belongs to the driver and the owner. "
            f"You are FORBIDDEN from writing a VERDICT or SEAL line anywhere; only reviewers "
            f"author those.\n\n"
            f"# Budget\nTool-call ceiling 20-40; a hard per-phase spend cap of ${self.budget} "
            f"is enforced by the harness. If you hit the ceiling, checkpoint and return "
            f"STATUS: PARTIAL rather than overrunning.\n\n"
            f"# Output contract (verdict-schema.md)\nWrite your progress and findings to the "
            f"checkpoint file BEFORE composing the final message. Your final message MUST "
            f"BEGIN with the token line, and the token line must appear EXACTLY ONCE in the "
            f"whole message:\nSTATUS: DONE|PARTIAL|FAILED files=N checkpoint={checkpoint}"
        )

    def build_reviewer_prompt(self, round_k: int, diff: str) -> str:
        return (
            f"# Objective\nReview the artifact below at its CURRENT revision against the "
            f"rubric. This is round {round_k}.\n\n"
            f"# Artifact paths\n{', '.join(self.cfg['artifact_paths'])}\n\n"
            f"# Cumulative diff\n{diff}\n\n"
            f"# Rubric\nRead and apply: {self.cfg['rubric_path']}\n\n"
            f"# Boundaries\nRead-only: you may not edit any file. {NO_PRE_APPROVAL} "
            f"Do NOT emit a SEAL line anywhere; a round reviewer emits VERDICT only.\n\n"
            f"# Budget\nA per-phase spend cap of ${self.budget} is enforced by the harness.\n\n"
            f"# Output contract (verbatim, verdict-schema.md)\n{EVIDENCE_BAR} {ANTI_HACKING} "
            f"State round={round_k}. Your final message MUST BEGIN with the token line, and "
            f"the token line must appear EXACTLY ONCE in the whole message:\n"
            f"VERDICT: REWORK|CLEAN blocking=N major=N minor=N round={round_k}"
        )

    def build_seal_prompt(self, diff: str, revision_label: str, revision12: str) -> str:
        return (
            f"# Objective\nFinal holistic acceptance audit of the COMPLETE current artifact "
            f"at revision {revision_label}. You are a fresh auditor; examine the full final "
            f"state, not a delta.\n\n"
            f"# Complete current artifact\n{self._artifact_dump()}\n\n"
            f"# Cumulative diff\n{diff}\n\n"
            f"# Rubric\nRead and apply: {self.cfg['rubric_path']}\n\n"
            f"# Boundaries\nRead-only: you may not edit any file. {NO_PRE_APPROVAL} "
            f"Do NOT emit a VERDICT line anywhere; a seal auditor emits SEAL only.\n\n"
            f"# Budget\nA per-phase spend cap of ${self.budget} is enforced by the harness.\n\n"
            f"# Output contract (verbatim, verdict-schema.md)\nSeal bar ({self.cfg['bar']}): "
            f"ACCEPT only at {BAR_TEXT[self.cfg['bar']]}. You examined revision {revision_label}; "
            f"you MUST repeat the 12-hex revision identifier {revision12} verbatim in your "
            f"return. {EVIDENCE_BAR} {ANTI_HACKING} Your final message MUST BEGIN with the "
            f"token line, and the token line must appear EXACTLY ONCE in the whole message:\n"
            f"SEAL: ACCEPTED|REJECTED blocking=N major=N minor=N nits=N"
        )

    # -- phases --

    def produce(self, round_k: int) -> str:
        if round_k == 1 or not self.state["punch_list"]:
            objective = Path(self.cfg["task_spec_file"]).expanduser().read_text()
        else:
            objective = self.state["punch_list"]["text"]
        checkpoint = str(self.rt / f"producer-round-{round_k}.md")
        prompt = self.build_producer_prompt(round_k, objective, checkpoint)
        self.archive_prompt(round_k, "produce", prompt)
        cmd = self.build_cmd(prompt, "produce")
        parsed, rc, timed_out = self.run_cli(cmd, self.rt / "raw" / f"round-{round_k}-produce.json")
        result = self.cli_result_or_escalate(parsed, rc, timed_out, round_k, "produce")
        if PRODUCER_TOKEN_RE.search(result):
            return "violation"
        self.state["sessions"][f"round-{round_k}-produce"] = parsed.get("session_id")
        return self._settle_producer_status(round_k, result, parsed.get("session_id"))

    def _settle_producer_status(self, round_k, result, session_id) -> str:
        match, count = self._unique_token(STATUS_RE, result, "produce")
        if not match:
            self.escalate(round_k, f"producer STATUS lines found={count}, need exactly 1")
        word = match.group("word")
        self.produce_files = int(match.group("files"))
        if word == "FAILED":
            self.escalate(round_k, "producer STATUS: FAILED")
        if word == "PARTIAL":
            self.resume_producer(round_k, session_id)
        self.state["producer_status_last"] = "DONE"
        delta = f"files={self.produce_files} via {self.cfg['producer_agent']}"
        if round_k == 1 and self.no_commit:
            delta += " policy=/commit-false"
        self.produce_delta = delta
        self.write_handoff()
        return "ok"

    def resume_producer(self, round_k: int, session_id: str) -> None:
        if not session_id:
            self.escalate(round_k, "producer PARTIAL but no session_id to resume")
        checkpoint = str(self.rt / f"producer-round-{round_k}.md")
        prompt = (
            f"Your previous session reported STATUS: PARTIAL. Continue exactly where you "
            f"left off and finish the remaining work. When complete, emit as line 1 of your "
            f"final message:\nSTATUS: DONE|PARTIAL|FAILED files=N checkpoint={checkpoint}"
        )
        self.archive_prompt(round_k, "produce-resume", prompt)
        cmd = self.build_cmd(prompt, "produce", resume_sid=session_id)
        parsed, rc, timed_out = self.run_cli(
            cmd, self.rt / "raw" / f"round-{round_k}-produce-resume.json")
        result = self.cli_result_or_escalate(parsed, rc, timed_out, round_k, "produce-resume")
        if PRODUCER_TOKEN_RE.search(result):
            self.escalate(round_k, "producer authored VERDICT/SEAL on resume")
        match, count = self._unique_token(STATUS_RE, result, "produce-resume")
        if not match or match.group("word") != "DONE":
            self.escalate(round_k, "producer still not DONE after one resume")
        self.produce_files = int(match.group("files"))

    def gate(self, round_k: int) -> bool:
        try:
            proc = subprocess.run(self.cfg["test_cmd"], cwd=str(self.cwd),
                                  capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.escalate(round_k, "deterministic gate timeout")
        except FileNotFoundError:
            self.escalate(round_k, "deterministic gate command not found")
        if proc.returncode == 0:
            return True
        tail = (proc.stdout + proc.stderr)[-GATE_TAIL_CHARS:]
        self.state["punch_list"] = {
            "text": f"Deterministic gate FAILED. Output tail:\n{tail}",
            "source_round": round_k, "source_phase": "gate"}
        self.open_findings = 1
        self.append_ledger(round_k, "gate", "fail", "deterministic gate FAIL", None, 1, None)
        self.write_handoff()
        return False

    def review(self, round_k: int, attempt: int = 1) -> dict:
        diff = self.capture_diff()
        prompt = self.build_reviewer_prompt(round_k, diff)
        tag = "review" if attempt == 1 else "review-retry"
        self.archive_prompt(round_k, tag, prompt)
        cmd = self.build_cmd(prompt, "review")
        parsed, rc, timed_out = self.run_cli(cmd, self.rt / "raw" / f"round-{round_k}-{tag}.json")
        result = self.cli_result_or_escalate(parsed, rc, timed_out, round_k, "review")
        seal_leak = re.search(SEAL_RE.pattern, result, re.MULTILINE) is not None
        match, count = self._unique_token(VERDICT_RE, result, "review")
        if seal_leak or not match or int(match.group("round")) != round_k:
            if seal_leak:
                reason = "reviewer emitted SEAL"
            elif not match:
                reason = f"malformed VERDICT (found {count}, need exactly 1)"
            else:
                reason = "round-mismatch VERDICT"
            if attempt == 1:
                self.log(f"review {reason}; one fresh re-dispatch")
                return self.review(round_k, attempt=2)
            self.escalate(round_k, f"reviewer {reason} twice")
        body = (result[:match.start()] + result[match.end():]).strip()
        return {"word": match.group("word"), "blocking": int(match.group("b")),
                "major": int(match.group("m")), "minor": int(match.group("mi")),
                "line": match.group(0), "body": body}

    def run_seal_once(self, round_k: int, revision_label: str, revision12: str, idx: int) -> dict:
        diff = self.capture_diff()
        for attempt in (1, 2):
            tag = f"seal-{idx}" + ("" if attempt == 1 else "-retry")
            prompt = self.build_seal_prompt(diff, revision_label, revision12)
            self.archive_prompt(round_k, tag, prompt)
            cmd = self.build_cmd(prompt, "seal")
            parsed, rc, to = self.run_cli(cmd, self.rt / "raw" / f"round-{round_k}-{tag}.json")
            result = self.cli_result_or_escalate(parsed, rc, to, round_k, "seal")
            match, count = self._unique_token(SEAL_RE, result, "seal")
            revision_ok = revision12 in result
            verdict_leak = re.search(VERDICT_RE.pattern, result, re.MULTILINE) is not None
            if match and revision_ok and not verdict_leak:
                body = (result[:match.start()] + result[match.end():]).strip()
                return {"word": match.group("word"), "blocking": int(match.group("b")),
                        "major": int(match.group("m")), "minor": int(match.group("mi")),
                        "nits": int(match.group("nits")), "line": match.group(0),
                        "body": body}
            self.log(f"seal malformed (seal_matches={count} revision={revision_ok} "
                     f"verdict_leak={verdict_leak}); attempt {attempt}")
        self.escalate(round_k, "seal auditor malformed or missing revision twice")

    # -- seal orchestration and terminal states --

    def _commit_lock_path(self) -> Path:
        return _hooks_dir(self.cwd).parent / "converge-auto-commit.lock"

    def _acquire_commit_lock(self, round_k: int) -> None:
        """Repo-scoped mkdir lock serializing a git-mode loop's pre-seal commit and
        the guard-3 window that follows, so concurrent same-repo drivers never race
        on the git index lock and never move HEAD under each other's post-seal check.
        The wait bound covers a sibling's WHOLE lock hold (seal sessions run up to
        phase_timeout_s each; strict holds two), derived from this loop's own timeout
        as the proxy. A lock whose recorded holder PID is dead is broken as stale so
        a crashed driver never wedges the repo. Released on every exit path via
        attempt_seal."""
        lock = self._commit_lock_path()
        deadline = time.monotonic() + 2 * self.timeout + COMMIT_LOCK_MARGIN_S
        last_note = time.monotonic()
        while True:
            try:
                lock.mkdir()
                (lock / "pid").write_text(str(os.getpid()))
                return
            except FileExistsError:
                self._break_lock_if_stale(lock)
                now = time.monotonic()
                if now >= deadline:
                    self.escalate(round_k, "pre-seal commit lock timeout")
                if now - last_note >= 60:
                    self.log(f"waiting for repo commit lock at {lock}")
                    last_note = now
                time.sleep(COMMIT_LOCK_POLL_S)

    def _break_lock_if_stale(self, lock: Path) -> None:
        """A crashed holder would wedge the repo forever; a dead recorded PID frees it."""
        try:
            pid = int((lock / "pid").read_text().strip())
            os.kill(pid, 0)
        except ProcessLookupError:
            # Must precede OSError: ProcessLookupError subclasses it.
            self.log(f"breaking stale commit lock at {lock} (holder pid {pid} dead)")
            self._release_lock_dir(lock)
        except (FileNotFoundError, ValueError, OSError):
            return  # no pid yet (holder mid-acquire), unreadable, or alive-other-uid

    @staticmethod
    def _release_lock_dir(lock: Path) -> None:
        try:
            (lock / "pid").unlink()
        except OSError:
            pass
        try:
            lock.rmdir()
        except OSError:
            pass

    def _release_commit_lock(self) -> None:
        self._release_lock_dir(self._commit_lock_path())

    def attempt_seal(self, round_k: int) -> str:
        """Serialize the whole git-mode seal under the repo commit lock (a no-op for a
        single uncontended loop); non-git loops need no lock (no commits, no HEAD)."""
        if not self.commit_mode:
            return self._run_seal_sequence(round_k)
        self._acquire_commit_lock(round_k)
        try:
            return self._run_seal_sequence(round_k)
        finally:
            self._release_commit_lock()

    def _run_seal_sequence(self, round_k: int) -> str:
        required = 2 if self.cfg["bar"] == "strict" else 1
        bound_full, bound_label = self._establish_bound_revision(round_k)
        seal_line = None
        for idx in range(required):
            if idx > 0 and not self._revision_stable(bound_full):
                self.escalate(round_k, "revision changed between strict seal audits")
            seal = self.run_seal_once(round_k, bound_label, bound_full[:12], idx)
            if not self._revision_stable(bound_full):
                self._record_seal(round_k, "void", seal, bound_label,
                                  seal["blocking"] + seal["major"])
                self.escalate(round_k, "artifact mutated during seal")
            accepted = seal["word"] == "ACCEPTED" and counts_within_bar(
                self.cfg["bar"], seal["blocking"], seal["major"], seal["minor"], seal["nits"])
            if not accepted:
                self._record_seal(round_k, "fail", seal, bound_label,
                                  seal["blocking"] + seal["major"] + seal["minor"] + seal["nits"])
                self.state["punch_list"] = {"text": seal["body"], "source_round": round_k,
                                            "source_phase": "seal"}
                self.state["seal"] = {"status": "REJECTED", "bound_revision": bound_full,
                                      "line": seal["line"]}
                self.write_handoff()
                return "rejected"
            self._record_seal(round_k, "ok", seal, bound_label, 0)
            # sealed_manifest is the CONTENT manifest at seal time in BOTH modes;
            # the post-commit void hook voids iff the recomputed manifest differs
            # from it (content-precise, so a byte-identical re-commit is safe).
            self.state["seal"] = {"status": "ACCEPTED", "bound_revision": bound_full,
                                  "sealed_manifest": self.compute_manifest(),
                                  "line": seal["line"]}
            self.write_handoff()
            seal_line = seal["line"]
        self.terminal(round_k, bound_full, bound_label, seal_line)

    def _record_seal(self, round_k, event, seal, bound_label, findings) -> None:
        self.append_ledger(round_k, "seal", event, f"seal by {self.cfg['seal_agent']}",
                           seal["line"], findings, bound_label)

    def terminal(self, round_k: int, bound_full: str, bound_label: str, seal_line: str) -> None:
        self.append_ledger(round_k, "terminal", "sealed", f"sealed at {bound_label}",
                           seal_line, 0, bound_label)
        self.state.update({"status": "sealed", "phase": "terminal"})
        self.write_handoff()
        self.log(f"SEALED round {round_k} at revision {bound_label}")
        self.notify(f"converge-auto {self.cfg['loop_id']} SEALED",
                    f"round {round_k} revision {bound_label}: {seal_line}")
        sys.exit(0)

    def escalate(self, round_k: int, reason: str) -> None:
        event = "escalate:" + reason.replace(" ", "_")
        last = self.state["manifest_hash_last"]
        revision = self._rev_label("sha256", last) if last else None
        self.append_ledger(round_k, "escalate", event, reason, None, self.open_findings, revision)
        self.state.update({"status": "escalated", "phase": "escalate"})
        self.write_handoff()
        self.log(f"ESCALATE round {round_k}: {reason}")
        self.notify(f"converge-auto {self.cfg['loop_id']} ESCALATE", f"round {round_k}: {reason}")
        sys.exit(2)

    def trend_stalled(self) -> bool:
        history = self.state["findings_history"]
        return len(history) >= 2 and history[-1] >= history[-2] and history[-1] > 0

    def refuse_dirty_tree_or_exit(self) -> None:
        """A seal binds to a revision; a dirty starting scope poisons diff attribution (R-2).

        Skipped under /commit false: a chronically dirty tree is that policy's normal
        state, and the round-0 snapshot (not a clean HEAD) is the attribution baseline.
        """
        if not self.commit_mode or self.cfg.get("allow_dirty"):
            return
        cmd = ["git", "-C", str(self.cwd), "status", "--porcelain", "--"] + self.cfg["artifact_paths"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.stdout.strip():
            self.log(f"artifact paths dirty at start:\n{proc.stdout}")
            print("config error: artifact_paths carry uncommitted changes at start; "
                  "commit or stash them, or set allow_dirty=true", file=sys.stderr)
            sys.exit(3)

    # -- orchestration --

    def preflight_or_exit(self) -> None:
        try:
            proc = subprocess.run([self.claude, "--version"], capture_output=True,
                                  text=True, timeout=PREFLIGHT_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as exc:
            self.log(f"preflight failed: {exc}")
            sys.exit(4)
        if proc.returncode != 0:
            self.log(f"preflight non-zero exit ({proc.returncode}); CLI absent or auth dead")
            sys.exit(4)

    def _run_round(self, round_k: int) -> None:
        if self.produce(round_k) == "violation":
            self.state["producer_token_violations"] += 1
            self.append_ledger(round_k, "produce", "fail", "producer authored VERDICT/SEAL",
                               None, self.open_findings, None)
            if self.state["producer_token_violations"] >= 2:
                self.escalate(round_k, "producer token violation twice")
            return
        if self.cfg["test_cmd"] and not self.gate(round_k):
            return
        manifest = self.compute_manifest()
        self.state["manifest_hash_last"] = manifest
        self.append_ledger(round_k, "produce", "ok", self.produce_delta, None,
                           self.open_findings, self._rev_label("sha256", manifest))
        verdict = self.review(round_k)
        if self.compute_manifest() != manifest:
            self.escalate(round_k, "artifact mutated during review")
        total = verdict["blocking"] + verdict["major"] + verdict["minor"]
        self.state["findings_history"].append(total)
        self.open_findings = total
        self.append_ledger(round_k, "review", "ok", f"review by {self.cfg['reviewer_agent']}",
                           verdict["line"], total, self._rev_label("sha256", manifest))
        if self.trend_stalled():
            self.escalate(round_k, "findings did not decrease across 2 rounds")
        self._triage(round_k, verdict)

    def _triage(self, round_k: int, verdict: dict) -> None:
        clean = verdict["word"] == "CLEAN" and counts_within_bar(
            self.cfg["bar"], verdict["blocking"], verdict["major"], verdict["minor"], 0)
        if clean:
            self.attempt_seal(round_k)  # terminates on ACCEPT, returns "rejected" otherwise
        else:
            self.state["punch_list"] = {"text": verdict["body"], "source_round": round_k,
                                        "source_phase": "review"}
            self.write_handoff()

    def run(self) -> None:
        for sub in ("prompts", "raw"):
            (self.rt / sub).mkdir(parents=True, exist_ok=True)
        self.write_handoff()
        policy = "/commit-false" if self.no_commit else "/commit-true"
        self.log(f"driver start loop_id={self.cfg['loop_id']} bar={self.cfg['bar']} "
                 f"cap={self.cfg['rounds_cap']} policy={policy}")
        self.preflight_or_exit()
        self.refuse_dirty_tree_or_exit()
        self.snapshot_round0()
        while self.state["round"] <= self.cfg["rounds_cap"]:
            round_k = self.state["round"]
            self.state["phase"] = "produce"
            self._run_round(round_k)
            self.state["round"] += 1
            self.write_handoff()
        self.escalate(self.cfg["rounds_cap"], "rounds cap reached without a seal")

    # -- dry run --

    def plan_only(self) -> None:
        cfg = self.cfg
        print(f"converge-auto DRY RUN  loop_id={cfg['loop_id']}")
        print(f"  runtime dir : {self.rt}")
        print(f"  repo        : {cfg.get('repo') or '(null; snapshot-diff mode)'}")
        print(f"  policy      : "
              + ("/commit false (snapshot-diff, no commits, sha256 binding)"
                 if self.no_commit else "/commit true"))
        print(f"  cwd         : {self.cwd}")
        print(f"  artifacts   : {', '.join(cfg['artifact_paths'])}")
        print(f"  bar         : {cfg['bar']}  ->  {BAR_TEXT[cfg['bar']]}")
        print(f"  rounds_cap  : {cfg['rounds_cap']}   budget/phase: ${self.budget}   "
              f"timeout/phase: {self.timeout}s")
        print(f"  producer    : {cfg['producer_agent']} model={cfg.get('producer_model')} "
              f"effort={cfg.get('producer_effort')}")
        print(f"  reviewer    : {cfg['reviewer_agent']} model={cfg.get('reviewer_model')} "
              f"effort={cfg.get('reviewer_effort')}")
        print(f"  seal        : {cfg['seal_agent']} model={cfg.get('seal_model')} "
              f"effort={cfg.get('seal_effort')}")
        print(f"  rubric      : {cfg['rubric_path']}")
        print(f"  test_cmd    : {cfg['test_cmd']}")
        print(f"  notify_cmd  : {cfg['notify_cmd']}")
        print("\n  phase plan per round: produce -> "
              + ("gate -> " if cfg["test_cmd"] else "") + "snapshot -> review -> triage -> seal")
        for phase in ("produce", "review", "seal"):
            demo = self.build_cmd("<PROMPT>", phase)
            print(f"\n  {phase} command template:\n    {shlex.join(demo)}")


# --- entrypoint --------------------------------------------------------------


def _hooks_dir(repo_dir: Path) -> Path:
    """Resolve the .git/hooks dir, following a worktree/submodule .git file."""
    git_marker = repo_dir / ".git"
    if git_marker.is_file():
        text = git_marker.read_text(errors="replace").strip()
        gitdir = text.split("gitdir:", 1)[1].strip() if "gitdir:" in text else ""
        base = Path(gitdir) if Path(gitdir).is_absolute() else (repo_dir / gitdir).resolve()
        return base / "hooks"
    return git_marker / "hooks"


def install_void_hook(repo: str) -> int:
    """Idempotently install seal-void-hook.sh as repo/.git/hooks/post-commit.

    Any pre-existing foreign post-commit is moved to post-commit.chained and
    exec'd by the installed hook (never clobbered). Re-running is a no-op.
    """
    repo_dir = Path(repo).expanduser()
    if not repo_dir.is_dir() or not (repo_dir / ".git").exists():
        print(f"error: not a git repo: {repo_dir}", file=sys.stderr)
        return 3
    src = Path(__file__).resolve().parent / "seal-void-hook.sh"
    if not src.is_file():
        print(f"error: seal-void-hook.sh not found beside the driver: {src}", file=sys.stderr)
        return 4
    hooks_dir = _hooks_dir(repo_dir)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    dst = hooks_dir / "post-commit"
    chained = hooks_dir / "post-commit.chained"
    if dst.exists():
        if HOOK_MARKER in dst.read_text(errors="replace"):
            print(f"post-commit already the seal-void-hook; idempotent no-op ({dst})")
            return 0
        if chained.exists():
            print(f"error: refusing to clobber existing {chained}", file=sys.stderr)
            return 1
        os.replace(str(dst), str(chained))
        os.chmod(str(chained), 0o755)
        print(f"chained existing post-commit -> {chained}")
    shutil.copyfile(str(src), str(dst))
    os.chmod(str(dst), 0o755)
    print(f"installed seal-void-hook -> {dst}")
    return 0


# --- parallel mode -----------------------------------------------------------


def resolved_artifact_paths(cfg: dict, loop_json_path: Path) -> list[Path]:
    """Absolute, normalized artifact paths for one loop, mirroring Loop._resolve
    (cwd = repo when set, else the loop.json's own directory)."""
    cwd = Path(cfg["repo"]).expanduser() if cfg.get("repo") else loop_json_path.parent
    out = []
    for rel in cfg["artifact_paths"]:
        path = Path(rel).expanduser()
        out.append((path if path.is_absolute() else cwd / path).resolve())
    return out


def load_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, ValueError) as exc:
        raise ConfigError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("manifest must be a JSON object with a loops list")
    return data


def validate_manifest(manifest: dict, manifest_path: Path) -> list[dict]:
    """Validate the manifest and every referenced loop.json; raise ConfigError
    (exit 3) BEFORE any child is spawned. Enforces the hard cap (1..5), per-loop
    config validity, loop_id uniqueness, and artifact-scope disjointness."""
    loops = manifest.get("loops")
    if not isinstance(loops, list) or not loops:
        raise ConfigError("manifest loops must be a non-empty list of loop.json paths")
    if not all(isinstance(x, str) and x for x in loops):
        raise ConfigError("manifest loops must be a list of path strings")
    if len(loops) > 5:
        raise ConfigError(f"manifest exceeds the hard cap of 5 loops (got {len(loops)})")
    entries, seen_ids, scope_owner = [], {}, {}
    for raw in loops:
        cfg_path = Path(raw).expanduser().resolve()
        if not cfg_path.is_file():
            raise ConfigError(f"manifest loop.json not found: {cfg_path}")
        cfg = validate_config(load_config(cfg_path))
        loop_id = cfg["loop_id"]
        if loop_id in seen_ids:
            raise ConfigError(f"duplicate loop_id across manifest loops: {loop_id}")
        seen_ids[loop_id] = cfg_path
        for path in resolved_artifact_paths(cfg, cfg_path):
            if path in scope_owner:
                raise ConfigError(f"artifact scope collision on {path}: loops "
                                  f"{scope_owner[path]} and {loop_id} share it")
            scope_owner[path] = loop_id
        entries.append({"cfg_path": cfg_path, "cfg": cfg})
    return entries


def run_children(entries: list[dict], claude_cmd: str) -> list:
    """Fork one child process per loop (each a normal --config single-loop run) and
    wait on all of them. Each child owns its runtime dir; no shared state."""
    running = []
    for entry in entries:
        rt = entry["cfg_path"].parent
        out = open(rt / "parallel-child.out", "w")
        err = open(rt / "parallel-child.err", "w")
        cmd = [sys.executable, os.path.abspath(__file__),
               "--config", str(entry["cfg_path"]), "--claude-cmd", claude_cmd]
        running.append((entry, subprocess.Popen(cmd, stdout=out, stderr=err), out, err))
    results = []
    for entry, proc, out, err in running:
        rc = proc.wait()
        out.close()
        err.close()
        results.append((entry, rc))
    return results


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _terminal_bound_label(rounds_path: Path) -> str:
    """The bound-revision label (commit:/sha256:<12hex>) from the terminal ledger
    row, or n/a when the loop never reached a terminal seal."""
    if not rounds_path.is_file():
        return "n/a"
    in_terminal = False
    for line in rounds_path.read_text(errors="replace").splitlines():
        if line.startswith("## R") and ".terminal - " in line:
            in_terminal = True
        elif in_terminal and line.startswith("- manifest:"):
            return line.split("manifest:", 1)[1].strip()
    return "n/a"


def _loop_summary_row(entry: dict, rc: int) -> str:
    rt = entry["cfg_path"].parent
    handoff = _read_json(rt / "handoff.json")
    status = handoff.get("status", "unknown")
    bound = _terminal_bound_label(rt / "rounds.md") if status == "sealed" else "n/a"
    rounds = handoff.get("round", "n/a")
    return (f"| {entry['cfg']['loop_id']} | {rc} | {status} | {bound} | "
            f"{rounds} | {rt / 'rounds.md'} |")


def write_parallel_summary(manifest_path: Path, results: list, started: str,
                           ended: str) -> Path:
    out = manifest_path.parent / "parallel-summary.md"
    worst = max((rc for _, rc in results), default=0)
    lines = ["# converge-auto parallel run summary", "",
             f"- manifest: {manifest_path}", f"- started: {started}",
             f"- ended: {ended}", f"- loops: {len(results)}",
             f"- worst child exit: {worst}", "",
             "| loop_id | exit | status | bound revision | rounds | ledger |",
             "| --- | --- | --- | --- | --- | --- |"]
    lines += [_loop_summary_row(entry, rc) for entry, rc in results]
    out.write_text("\n".join(lines) + "\n")
    return out


def run_parallel(manifest_path: Path, claude_cmd: str, dry_run: bool) -> int:
    """Validate the manifest, then either print each loop's dry-run plan (dry_run)
    or fork the children, wait, and write the aggregate summary. Return the parent
    exit code: 0 iff every child sealed (exited 0), else 2."""
    if not manifest_path.is_file():
        print(f"config error: manifest not found: {manifest_path}", file=sys.stderr)
        return 3
    try:
        entries = validate_manifest(load_manifest(manifest_path), manifest_path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 3
    if dry_run:
        for entry in entries:
            print(f"\n===== parallel loop {entry['cfg']['loop_id']} =====")
            Loop(entry["cfg"], entry["cfg_path"].parent, claude_cmd).plan_only()
        return 0
    started = iso_now()
    results = run_children(entries, claude_cmd)
    summary = write_parallel_summary(manifest_path, results, started, iso_now())
    print(f"parallel summary: {summary}")
    return 0 if all(rc == 0 for _, rc in results) else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Supervised-autonomous convergence driver.")
    parser.add_argument("--config", help="path to loop.json (required for a run)")
    parser.add_argument("--claude-cmd", default="claude", help="claude CLI path (default: claude)")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate config and print the phase plan; spawn nothing")
    parser.add_argument("--parallel", metavar="MANIFEST",
                        help="path to a parallel manifest.json ('{\"loops\": [loop.json, ...]}'); "
                             "mutually exclusive with --config")
    parser.add_argument("--install-void-hook", nargs="?", const="", metavar="REPO",
                        help="install seal-void-hook.sh as the repo's post-commit hook "
                             "(REPO from this arg, else from --config), then exit")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.install_void_hook is not None:
        repo = args.install_void_hook
        if not repo:
            if not args.config:
                print("config error: --install-void-hook needs a REPO arg or --config",
                      file=sys.stderr)
                sys.exit(3)
            try:
                cfg = validate_config(load_config(Path(args.config).expanduser().resolve()))
            except ConfigError as exc:
                print(f"config error: {exc}", file=sys.stderr)
                sys.exit(3)
            repo = cfg.get("repo")
            if not repo:
                print("config error: loop.json has no repo for --install-void-hook",
                      file=sys.stderr)
                sys.exit(3)
        sys.exit(install_void_hook(repo))
    if args.parallel:
        if args.config:
            print("config error: --parallel and --config are mutually exclusive",
                  file=sys.stderr)
            sys.exit(3)
        sys.exit(run_parallel(Path(args.parallel).expanduser().resolve(),
                              args.claude_cmd, args.dry_run))
    if not args.config:
        print("config error: --config is required", file=sys.stderr)
        sys.exit(3)
    cfg_path = Path(args.config).expanduser().resolve()
    if not cfg_path.is_file():
        print(f"config error: loop.json not found: {cfg_path}", file=sys.stderr)
        sys.exit(3)
    try:
        cfg = validate_config(load_config(cfg_path))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        sys.exit(3)
    loop = Loop(cfg, cfg_path.parent, args.claude_cmd)
    if args.dry_run:
        loop.plan_only()
        sys.exit(0)
    loop.run()


if __name__ == "__main__":
    main()
