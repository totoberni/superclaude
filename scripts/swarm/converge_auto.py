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

The revision a SEAL binds to is a content manifest (sha256 over the sorted
per-file byte hashes of artifact_paths), NOT a commit hash: mid-loop trees are
uncommitted by design, and a content manifest detects ANY edit including
uncommitted ones, which is stricter than a commit hash and works for non-git
artifacts.

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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- Constants ---------------------------------------------------------------

DEFAULT_ALLOWED_TOOLS = ["Bash"]
READ_ONLY_DENY = "Write,Edit,NotebookEdit"
PREFLIGHT_TIMEOUT_S = 30
NOTIFY_TIMEOUT_S = 30
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


def first_line(text: str) -> str:
    stripped = text.lstrip()
    return stripped.splitlines()[0] if stripped else ""


def body_after_line1(text: str) -> str:
    parts = text.lstrip().split("\n", 1)
    return parts[1] if len(parts) > 1 else ""


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


# --- The convergence loop ----------------------------------------------------


class Loop:
    """One artifact converged to a fresh seal, or escalated. Owns one runtime dir."""

    def __init__(self, cfg: dict, runtime_dir: Path, claude_cmd: str):
        self.cfg = cfg
        self.rt = runtime_dir
        self.claude = claude_cmd
        self.cwd = Path(cfg["repo"]).expanduser() if cfg.get("repo") else runtime_dir
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

    def append_ledger(self, round_k, phase, event, delta, token, findings, manifest12) -> None:
        tok = f"`{token}`" if token else "none"
        man = f"sha256:{manifest12}" if manifest12 else "n/a"
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

    def snapshot_round0(self) -> None:
        if self.cfg.get("repo"):
            return
        snapdir = self.rt / "raw" / "snapshot-0"
        snapdir.mkdir(parents=True, exist_ok=True)
        for rel in self.cfg["artifact_paths"]:
            path = self._resolve(rel)
            data = path.read_bytes() if path.is_file() else b""
            (snapdir / rel.replace("/", "__").replace("\\", "__")).write_bytes(data)

    def capture_diff(self) -> str:
        if self.cfg.get("repo"):
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
            f"# Output contract\nLine 1 of your final message MUST be exactly this shape "
            f"(verdict-schema.md):\nSTATUS: DONE|PARTIAL|FAILED files=N checkpoint={checkpoint}\n"
            f"Write your progress and findings to that checkpoint file BEFORE composing the "
            f"final message. You are FORBIDDEN from writing a VERDICT or SEAL line; only "
            f"reviewers author those.\n\n"
            f"# Tool guidance\nAllowed tools: {allow}. Make the minimal change set that "
            f"satisfies the objective; match existing patterns.\n\n"
            f"# Boundaries\nWrite ONLY these paths (the allowlist): "
            f"{', '.join(self.cfg['artifact_paths'])}. Touch nothing else.\n\n"
            f"# Budget\nTool-call ceiling 20-40; a hard per-phase spend cap of ${self.budget} "
            f"is enforced by the harness. If you hit the ceiling, checkpoint and return "
            f"STATUS: PARTIAL rather than overrunning."
        )

    def build_reviewer_prompt(self, round_k: int, diff: str) -> str:
        return (
            f"# Objective\nReview the artifact below at its CURRENT revision against the "
            f"rubric. This is round {round_k}.\n\n"
            f"# Artifact paths\n{', '.join(self.cfg['artifact_paths'])}\n\n"
            f"# Cumulative diff\n{diff}\n\n"
            f"# Rubric\nRead and apply: {self.cfg['rubric_path']}\n\n"
            f"# Output contract (verbatim, verdict-schema.md)\nLine 1 of your final message "
            f"MUST be exactly:\nVERDICT: REWORK|CLEAN blocking=N major=N minor=N round={round_k}\n"
            f"State round={round_k}. {EVIDENCE_BAR} {ANTI_HACKING}\n\n"
            f"# Boundaries\nRead-only: you may not edit any file. {NO_PRE_APPROVAL} "
            f"Do NOT emit a SEAL line; a round reviewer emits VERDICT only.\n\n"
            f"# Budget\nA per-phase spend cap of ${self.budget} is enforced by the harness."
        )

    def build_seal_prompt(self, diff: str, revision12: str) -> str:
        return (
            f"# Objective\nFinal holistic acceptance audit of the COMPLETE current artifact "
            f"at revision {revision12}. You are a fresh auditor; examine the full final "
            f"state, not a delta.\n\n"
            f"# Complete current artifact\n{self._artifact_dump()}\n\n"
            f"# Cumulative diff\n{diff}\n\n"
            f"# Rubric\nRead and apply: {self.cfg['rubric_path']}\n\n"
            f"# Output contract (verbatim, verdict-schema.md)\nLine 1 of your final message "
            f"MUST be exactly:\nSEAL: ACCEPTED|REJECTED blocking=N major=N minor=N nits=N\n"
            f"Seal bar ({self.cfg['bar']}): ACCEPT only at {BAR_TEXT[self.cfg['bar']]}.\n"
            f"You examined revision {revision12}; you MUST repeat this exact revision string "
            f"in your return. {EVIDENCE_BAR} {ANTI_HACKING}\n\n"
            f"# Boundaries\nRead-only: you may not edit any file. {NO_PRE_APPROVAL} "
            f"Do NOT emit a VERDICT line; a seal auditor emits SEAL only.\n\n"
            f"# Budget\nA per-phase spend cap of ${self.budget} is enforced by the harness."
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
        match = STATUS_RE.match(first_line(result))
        if not match:
            self.escalate(round_k, "producer emitted no valid STATUS line")
        word = match.group("word")
        self.produce_files = int(match.group("files"))
        if word == "FAILED":
            self.escalate(round_k, "producer STATUS: FAILED")
        if word == "PARTIAL":
            self.resume_producer(round_k, session_id)
        self.state["producer_status_last"] = "DONE"
        self.produce_delta = f"files={self.produce_files} via {self.cfg['producer_agent']}"
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
        match = STATUS_RE.match(first_line(result))
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
        line = first_line(result)
        match = VERDICT_RE.match(line)
        if not match or int(match.group("round")) != round_k:
            reason = "reviewer emitted SEAL" if SEAL_RE.match(line) else "malformed/round-mismatch VERDICT"
            if attempt == 1:
                self.log(f"review {reason}; one fresh re-dispatch")
                return self.review(round_k, attempt=2)
            self.escalate(round_k, f"reviewer {reason} twice")
        return {"word": match.group("word"), "blocking": int(match.group("b")),
                "major": int(match.group("m")), "minor": int(match.group("mi")),
                "line": line, "body": body_after_line1(result)}

    def run_seal_once(self, round_k: int, revision12: str, idx: int) -> dict:
        diff = self.capture_diff()
        for attempt in (1, 2):
            tag = f"seal-{idx}" + ("" if attempt == 1 else "-retry")
            prompt = self.build_seal_prompt(diff, revision12)
            self.archive_prompt(round_k, tag, prompt)
            cmd = self.build_cmd(prompt, "seal")
            parsed, rc, to = self.run_cli(cmd, self.rt / "raw" / f"round-{round_k}-{tag}.json")
            result = self.cli_result_or_escalate(parsed, rc, to, round_k, "seal")
            line = first_line(result)
            match = SEAL_RE.match(line)
            revision_ok = revision12 in result
            emitted_verdict = VERDICT_RE.match(line) is not None
            if match and revision_ok and not emitted_verdict:
                return {"word": match.group("word"), "blocking": int(match.group("b")),
                        "major": int(match.group("m")), "minor": int(match.group("mi")),
                        "nits": int(match.group("nits")), "line": line,
                        "body": body_after_line1(result)}
            self.log(f"seal malformed (seal_line={bool(match)} revision={revision_ok} "
                     f"verdict_token={emitted_verdict}); attempt {attempt}")
        self.escalate(round_k, "seal auditor malformed or missing revision twice")

    # -- seal orchestration and terminal states --

    def attempt_seal(self, round_k: int) -> str:
        required = 2 if self.cfg["bar"] == "strict" else 1
        prev_hash = None
        seal_line = None
        for idx in range(required):
            h_pre = self.compute_manifest()
            if idx > 0 and h_pre != prev_hash:
                self.escalate(round_k, "manifest changed between strict seal audits")
            seal = self.run_seal_once(round_k, h_pre[:12], idx)
            if self.compute_manifest() != h_pre:
                self._record_seal(round_k, "void", seal, h_pre, seal["blocking"] + seal["major"])
                self.escalate(round_k, "artifact mutated during seal (H_post != H_pre)")
            accepted = seal["word"] == "ACCEPTED" and counts_within_bar(
                self.cfg["bar"], seal["blocking"], seal["major"], seal["minor"], seal["nits"])
            if not accepted:
                self._record_seal(round_k, "fail", seal, h_pre,
                                  seal["blocking"] + seal["major"] + seal["minor"] + seal["nits"])
                self.state["punch_list"] = {"text": seal["body"], "source_round": round_k,
                                            "source_phase": "seal"}
                self.state["seal"] = {"status": "REJECTED", "bound_hash": h_pre, "line": seal["line"]}
                self.write_handoff()
                return "rejected"
            self._record_seal(round_k, "ok", seal, h_pre, 0)
            self.state["seal"] = {"status": "ACCEPTED", "bound_hash": h_pre, "line": seal["line"]}
            self.write_handoff()
            prev_hash, seal_line = h_pre, seal["line"]
        self.terminal(round_k, prev_hash, seal_line)

    def _record_seal(self, round_k, event, seal, h_pre, findings) -> None:
        self.append_ledger(round_k, "seal", event, f"seal by {self.cfg['seal_agent']}",
                           seal["line"], findings, h_pre[:12])

    def terminal(self, round_k: int, h_final: str, seal_line: str) -> None:
        self.append_ledger(round_k, "terminal", "sealed", f"sealed at {h_final[:12]}",
                           seal_line, 0, h_final[:12])
        self.state.update({"status": "sealed", "phase": "terminal"})
        self.write_handoff()
        self.log(f"SEALED round {round_k} at revision {h_final[:12]}")
        self.notify(f"converge-auto {self.cfg['loop_id']} SEALED",
                    f"round {round_k} revision {h_final[:12]}: {seal_line}")
        sys.exit(0)

    def escalate(self, round_k: int, reason: str) -> None:
        event = "escalate:" + reason.replace(" ", "_")
        manifest12 = self.state["manifest_hash_last"][:12] if self.state["manifest_hash_last"] else None
        self.append_ledger(round_k, "escalate", event, reason, None, self.open_findings, manifest12)
        self.state.update({"status": "escalated", "phase": "escalate"})
        self.write_handoff()
        self.log(f"ESCALATE round {round_k}: {reason}")
        self.notify(f"converge-auto {self.cfg['loop_id']} ESCALATE", f"round {round_k}: {reason}")
        sys.exit(2)

    def trend_stalled(self) -> bool:
        history = self.state["findings_history"]
        return len(history) >= 2 and history[-1] >= history[-2] and history[-1] > 0

    def refuse_dirty_tree_or_exit(self) -> None:
        """A seal binds to a revision; a dirty starting scope poisons diff attribution (R-2)."""
        if not self.cfg.get("repo") or self.cfg.get("allow_dirty"):
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
                           self.open_findings, manifest[:12])
        verdict = self.review(round_k)
        if self.compute_manifest() != manifest:
            self.escalate(round_k, "artifact mutated during review")
        total = verdict["blocking"] + verdict["major"] + verdict["minor"]
        self.state["findings_history"].append(total)
        self.open_findings = total
        self.append_ledger(round_k, "review", "ok", f"review by {self.cfg['reviewer_agent']}",
                           verdict["line"], total, manifest[:12])
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
        self.log(f"driver start loop_id={self.cfg['loop_id']} bar={self.cfg['bar']} "
                 f"cap={self.cfg['rounds_cap']}")
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Supervised-autonomous convergence driver.")
    parser.add_argument("--config", required=True, help="path to loop.json")
    parser.add_argument("--claude-cmd", default="claude", help="claude CLI path (default: claude)")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate config and print the phase plan; spawn nothing")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
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
