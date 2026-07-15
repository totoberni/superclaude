#!/usr/bin/env python3
"""instrument-tripwire.py - R-6 instrument-tripwire accumulator (design/DECISION-DOC.md
section 4, Half B; rules/40-swarm-quality-gates.md R-6 "the rule of two").

Per-session accumulator of gating check-CLASSES drawn from two sources:
  (1) reviewer `TOOLING:` lines (fed via --tooling, or scanned from --ledger with
      --scan-ledger), and
  (2) normalized signatures of gating check-COMMANDS (fed via --command).

When the SAME check-class is recorded >=2 times in a session the tripwire FIRES ONCE
for that class: it prints the guidance message to stdout (the guard relays it as a
FLAG/warn) and, when --ledger names a writable file, appends `INSTRUMENT-TRIPWIRE:
<class>` to it. Otherwise stdout is empty. This is the mechanical form of R-6: the
second time the same class of manual/gating friction recurs, build the deterministic
check for it, or record why none is buildable (the only legal bypass).

FLAG-level only: using manual verification twice where a deterministic check would
serve is an overridable judgement, never a block. State lives in
<state-dir>/<session>.tripwire.json; a missing or corrupt state file is treated as
empty (fail-open). No em-dashes/en-dashes anywhere by house style.

Counting semantics:
  - TOOLING lines are deduped by RAW-line hash, so re-feeding the identical physical
    line (same reviewer response seen twice, or a re-scan of the same ledger) never
    double-counts. Two DIFFERENT lines that name the SAME class (any word order) do
    accumulate and fire.
  - Commands are counted per invocation: each run of a gating command is a distinct
    friction event.
Exit code is always 0 (advisory).
"""

import argparse
import hashlib
import json
import os
import re
import sys

# Minimal stopword set for TOOLING-line class signatures. Kept small on purpose:
# over-stripping would collapse distinct instruments into one class.
_STOPWORDS = {
    "a", "an", "the", "for", "of", "to", "and", "in", "on", "with", "is", "it",
    "this", "that", "be", "by", "as", "or",
}

# Gating check-command whitelist: (regex, class). First match wins. This is the
# SINGLE classifier of "what is a gating command" (rules/20 Single Source of Truth
# Across Tool Boundaries): the shell guard never re-implements this list.
_GATING = [
    (re.compile(r"\bpytest\b"), "pytest"),
    (re.compile(r"\bpython[0-9.]*\s+-m\s+pytest\b"), "pytest"),
    (re.compile(r"\brun-all\.sh\b"), "run-all"),
    (re.compile(r"\btest-[^\s/]*\.sh\b"), "test-script"),
    (re.compile(r"\bbats\b"), "bats"),
    (re.compile(r"\bshellcheck\b"), "shellcheck"),
    (re.compile(r"\bruff\b"), "ruff"),
    (re.compile(r"\bmypy\b"), "mypy"),
    (re.compile(r"\bflake8\b"), "flake8"),
    (re.compile(r"\b(?:npm|yarn|pnpm)\s+(?:run\s+)?test\b"), "js-test"),
    (re.compile(r"\bcargo\s+test\b"), "cargo-test"),
    (re.compile(r"\bgo\s+test\b"), "go-test"),
    (re.compile(r"\bmake\s+(?:test|check|lint)\b"), "make-check"),
]

_TOOLING_PREFIX = re.compile(r"(?i)^\s*(?:[-*]\s*)?TOOLING:\s*")


def classify_tooling(line):
    """A word-order-independent class signature for a reviewer TOOLING line, or None."""
    text = _TOOLING_PREFIX.sub("", line).strip()
    toks = [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t and t not in _STOPWORDS]
    if not toks:
        return None
    return "-".join(sorted(set(toks)))[:64]


def classify_command(cmd):
    """The gating check-class for a shell command, or None if it is not a gating check."""
    for rx, cls in _GATING:
        if rx.search(cmd):
            return cls
    return None


def _state_path(state_dir, session):
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session) or "unknown"
    return os.path.join(state_dir, "%s.tripwire.json" % safe)


def load_state(state_dir, session):
    path = _state_path(state_dir, session)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        counts = data.get("counts", {})
        fired = data.get("fired", [])
        seen = data.get("seen_lines", [])
        if isinstance(counts, dict) and isinstance(fired, list) and isinstance(seen, list):
            return {"counts": dict(counts), "fired": list(fired), "seen_lines": list(seen)}
    except (OSError, ValueError):
        pass
    return {"counts": {}, "fired": [], "seen_lines": []}


def save_state(state_dir, session, state):
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(_state_path(state_dir, session), "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError:
        pass  # advisory scratch; fail-open


def read_ledger_tooling(ledger):
    lines = []
    try:
        with open(ledger, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                if _TOOLING_PREFIX.search(raw):
                    lines.append(raw.rstrip("\n"))
    except OSError:
        pass
    return lines


def append_ledger(ledger, cls):
    if not ledger:
        return
    try:
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write("INSTRUMENT-TRIPWIRE: %s\n" % cls)
    except OSError:
        pass  # fail-open; the stdout warn still reaches the guard


def _line_hash(line):
    return hashlib.sha1(line.strip().encode("utf-8", "replace")).hexdigest()


def record_tooling(state, line, threshold, ledger, fired_out):
    h = _line_hash(line)
    if h in state["seen_lines"]:
        return
    state["seen_lines"].append(h)
    cls = classify_tooling(line)
    _tally(state, cls, threshold, ledger, fired_out)


def record_command(state, cmd, threshold, ledger, fired_out):
    _tally(state, classify_command(cmd), threshold, ledger, fired_out)


def _tally(state, cls, threshold, ledger, fired_out):
    if not cls:
        return
    state["counts"][cls] = state["counts"].get(cls, 0) + 1
    if state["counts"][cls] >= threshold and cls not in state["fired"]:
        state["fired"].append(cls)
        append_ledger(ledger, cls)
        fired_out.append(cls)


def main(argv=None):
    ap = argparse.ArgumentParser(description="R-6 instrument-tripwire accumulator")
    ap.add_argument("--session", required=True)
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--ledger", default="")
    ap.add_argument("--tooling", default="")
    ap.add_argument("--command", default="")
    ap.add_argument("--scan-ledger", action="store_true",
                    help="also fold existing TOOLING lines from --ledger into the count")
    ap.add_argument("--threshold", type=int, default=2)
    args = ap.parse_args(argv)

    state = load_state(args.state_dir, args.session)
    fired = []

    if args.scan_ledger and args.ledger and os.path.isfile(args.ledger):
        for line in read_ledger_tooling(args.ledger):
            record_tooling(state, line, args.threshold, args.ledger, fired)
    if args.tooling:
        record_tooling(state, args.tooling, args.threshold, args.ledger, fired)
    if args.command:
        record_command(state, args.command, args.threshold, args.ledger, fired)

    save_state(args.state_dir, args.session, state)

    for cls in fired:
        sys.stdout.write(
            "instrument tripwire (R-6): check-class '%s' recurred; build the "
            "deterministic check for it, or record why none is buildable (the only "
            "legal bypass, rules/40 R-6)\n" % cls
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
