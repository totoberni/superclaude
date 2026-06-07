#!/usr/bin/env python3
"""experiment-harness — multi-seed ML experiment runner + claim-provenance verifier.

Run via the dedicated venv (absolute path):
    ~/.claude/.venv/bin/python ~/.claude/scripts/experiment-harness.py <subcommand> ...

Two subcommands:

  run     Execute a target script across N seeds, capture each seed's numeric
          result(s), aggregate mean +/- std (+ min/max/n), and OPTIONALLY append a
          provenance entry to a passport.yaml under a stable --claim-id.

  verify  Given a passport.yaml and a claims list, flag every claim that LACKS a
          passport entry or whose recorded value/std is STALE relative to the
          passport. Blocks unprovenanced / drifted claims (the whole point).

------------------------------------------------------------------------------
passport.yaml schema (claim-provenance map)
------------------------------------------------------------------------------
Top-level key `claims` maps each stable claim_id -> a provenance record:

    claims:
      <claim_id>:               # stable, human-chosen key (e.g. "table2_test_acc")
        experiment: <str>       # human label for the experiment
        script:     <str>       # path/command that produced the number
        seeds:      [<int>...]  # the exact seeds aggregated
        git_commit: <str>       # commit the result was produced at (short SHA)
        date:       <str>       # ISO date the result was recorded (YYYY-MM-DD)
        metric:     <str>       # OPTIONAL: which metric, when a script emits many
        value:      <float>     # the reported central value (the mean)
        std:        <float>     # the reported spread (population std across seeds)
        n:          <int>       # number of seeds aggregated (== len(seeds))

`run --claim-id X --passport p.yaml` fills experiment/script/seeds/git_commit/
date/value/std/n automatically. `metric` is set when --metric selects one of
several emitted keys. See passport.example.yaml for a worked example.

A documented machine-readable copy of this schema lives at
    ~/.claude/scripts/passport.example.yaml
------------------------------------------------------------------------------
Exit codes
------------------------------------------------------------------------------
This is a REPORTING tool: it exits non-zero only on its OWN error (bad args,
unreadable file, target script crash that the user must fix). `verify` findings
(missing/stale claims) are reported on stdout and, by DEFAULT, still exit 0.
Pass `verify --strict` to opt into a blocking exit code (2) for CI / pre-commit
gates that must REJECT unprovenanced claims.

  0  success (run completed; or verify ran — clean, or findings w/o --strict)
  1  tool error (bad arguments, I/O failure, target-script non-zero exit)
  2  verify --strict found unprovenanced or stale claims (intentional block)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - dependency is declared in requirements.txt
    sys.stderr.write(
        "error: PyYAML not available. Install with:\n"
        "  ~/.claude/.venv/bin/pip install pyyaml\n"
    )
    sys.exit(1)


# Exit-code constants (see module docstring).
EXIT_OK = 0
EXIT_TOOL_ERROR = 1
EXIT_VERIFY_BLOCKED = 2

# Tolerance for considering two floats "the same" when checking staleness.
# Absolute floor + relative component so both tiny and large magnitudes work.
_ABS_TOL = 1e-9
_REL_TOL = 1e-6

# Matches `key=value` or `key: value` lines emitted by a target script.
_KV_RE = re.compile(r"^\s*([A-Za-z_][\w.\-]*)\s*[=:]\s*([-+]?[\d.eE+]+)\s*$")
# Matches a single bare numeric token (a script that just prints its result).
_NUM_RE = re.compile(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*$")


def _err(msg: str) -> None:
    sys.stderr.write(f"error: {msg}\n")


# --------------------------------------------------------------------------- #
# Numeric parsing + aggregation
# --------------------------------------------------------------------------- #
def parse_result(stdout: str) -> dict[str, float]:
    """Extract numeric results from a target script's stdout.

    Supported formats, tried in order:
      1. A single JSON object of {name: number} (flat, numeric values only).
      2. One or more `key=value` / `key: value` lines.
      3. A single bare number anywhere in the output -> key "value".

    Returns a {metric_name: float} dict. Raises ValueError if nothing numeric
    is found.
    """
    text = stdout.strip()
    if not text:
        raise ValueError("target script produced no stdout to parse")

    # (1) Flat JSON object.
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        obj = None
    if isinstance(obj, dict):
        out: dict[str, float] = {}
        for k, v in obj.items():
            if isinstance(v, bool):  # bool is a subclass of int; exclude it
                continue
            if isinstance(v, (int, float)):
                out[str(k)] = float(v)
        if out:
            return out
        raise ValueError("JSON object contained no numeric values")

    # (2) key=value / key: value lines (collect every match in the output).
    kv: dict[str, float] = {}
    for line in text.splitlines():
        m = _KV_RE.match(line)
        if m:
            try:
                kv[m.group(1)] = float(m.group(2))
            except ValueError:
                continue
    if kv:
        return kv

    # (3) A single bare number on its own line.
    for line in text.splitlines():
        m = _NUM_RE.match(line)
        if m:
            return {"value": float(m.group(1))}

    raise ValueError(
        "could not parse any numeric result from target stdout; "
        "emit a bare number, key=value lines, or a flat JSON object"
    )


def aggregate(values: list[float]) -> dict[str, float | int]:
    """Aggregate a list of per-seed values: mean, population std, min, max, n."""
    n = len(values)
    if n == 0:
        raise ValueError("no values to aggregate")
    mean = sum(values) / n
    # Population std (ddof=0): deterministic, well-defined for n==1 (==0.0).
    var = sum((v - mean) ** 2 for v in values) / n
    std = math.sqrt(var)
    return {
        "mean": mean,
        "std": std,
        "min": min(values),
        "max": max(values),
        "n": n,
    }


def _close(a: float, b: float) -> bool:
    """Float comparison with combined absolute + relative tolerance."""
    return abs(a - b) <= max(_ABS_TOL, _REL_TOL * max(abs(a), abs(b)))


# --------------------------------------------------------------------------- #
# Helpers: seeds, git, passport I/O
# --------------------------------------------------------------------------- #
def parse_seeds(spec: str) -> list[int]:
    """Parse a seed spec: comma list "1,2,3" or inclusive range "1-5"."""
    spec = spec.strip()
    if not spec:
        raise ValueError("empty --seeds")
    if "-" in spec and "," not in spec:
        lo_s, _, hi_s = spec.partition("-")
        lo, hi = int(lo_s), int(hi_s)
        if hi < lo:
            raise ValueError(f"range end {hi} < start {lo}")
        return list(range(lo, hi + 1))
    seeds = [int(tok) for tok in spec.split(",") if tok.strip() != ""]
    if not seeds:
        raise ValueError("no seeds parsed")
    return seeds


def git_commit(cwd: Path | None = None) -> str:
    """Short git SHA of HEAD, or 'unknown' if not a repo / git unavailable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def load_passport(path: Path) -> dict[str, Any]:
    """Load a passport.yaml. Returns {} for a missing/empty file."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return data


def save_passport(path: Path, data: dict[str, Any]) -> None:
    """Atomically write a passport.yaml (temp file + os.replace)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write("# passport.yaml — claim-provenance map.\n")
        fh.write("# Schema + worked example: ~/.claude/scripts/passport.example.yaml\n")
        fh.write("# Generated/updated by experiment-harness.py — edits preserved on re-run.\n")
        yaml.safe_dump(data, fh, sort_keys=True, default_flow_style=False)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# run subcommand
# --------------------------------------------------------------------------- #
def run_one_seed(
    cmd: list[str], seed: int, timeout: int
) -> tuple[dict[str, float], str]:
    """Run the target command once for a given seed. Returns (metrics, raw_stdout).

    The seed is exposed two ways so the target can consume whichever it prefers:
      - appended as CLI args:  <cmd> --seed <seed>
      - env var:               EXPERIMENT_SEED=<seed>
    """
    env = dict(os.environ)
    env["EXPERIMENT_SEED"] = str(seed)
    full = list(cmd) + ["--seed", str(seed)]
    proc = subprocess.run(
        full, capture_output=True, text=True, env=env, timeout=timeout
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"target exited {proc.returncode} for seed {seed}\n"
            f"--- stderr ---\n{proc.stderr.strip()}"
        )
    return parse_result(proc.stdout), proc.stdout


def cmd_run(args: argparse.Namespace) -> int:
    try:
        seeds = parse_seeds(args.seeds)
    except ValueError as exc:
        _err(f"--seeds: {exc}")
        return EXIT_TOOL_ERROR

    # The target command: everything after --script, split on whitespace, OR a
    # quoted single string. We run it with the venv python only if it's a .py.
    cmd = args.script
    if len(cmd) == 1 and cmd[0].endswith(".py"):
        cmd = [sys.executable, cmd[0]]
    elif len(cmd) >= 1 and cmd[0].endswith(".py"):
        cmd = [sys.executable] + cmd

    script_label = " ".join(args.script)
    print(f"[harness] running '{script_label}' across {len(seeds)} seed(s): {seeds}")

    # metric_name -> list of per-seed values (aligned with `seeds` order).
    series: dict[str, list[float]] = {}
    for seed in seeds:
        try:
            metrics, _raw = run_one_seed(cmd, seed, args.timeout)
        except subprocess.TimeoutExpired:
            _err(f"target timed out after {args.timeout}s for seed {seed}")
            return EXIT_TOOL_ERROR
        except (RuntimeError, ValueError, OSError) as exc:
            _err(str(exc))
            return EXIT_TOOL_ERROR
        for k, v in metrics.items():
            series.setdefault(k, []).append(v)
        shown = ", ".join(f"{k}={v:g}" for k, v in metrics.items())
        print(f"  seed {seed}: {shown}")

    # If the user pinned a metric, keep only that one (error if absent).
    if args.metric is not None:
        if args.metric not in series:
            _err(
                f"--metric '{args.metric}' not found in target output; "
                f"available: {sorted(series)}"
            )
            return EXIT_TOOL_ERROR
        series = {args.metric: series[args.metric]}

    # Aggregate + report each metric.
    print("\n[harness] aggregate (mean +/- std [min, max], n):")
    aggs: dict[str, dict[str, float | int]] = {}
    for name in sorted(series):
        agg = aggregate(series[name])
        aggs[name] = agg
        print(
            f"  {name}: {agg['mean']:.6g} +/- {agg['std']:.6g} "
            f"[{agg['min']:.6g}, {agg['max']:.6g}], n={agg['n']}"
        )

    # Optionally write a passport entry.
    if args.claim_id is not None:
        if args.passport is None:
            _err("--claim-id given but --passport not specified")
            return EXIT_TOOL_ERROR
        # Choose which metric backs the claim: --metric, else the sole metric,
        # else require disambiguation.
        if args.metric is not None:
            chosen = args.metric
        elif len(aggs) == 1:
            chosen = next(iter(aggs))
        else:
            _err(
                "target emitted multiple metrics; pass --metric to choose which "
                f"one backs claim '{args.claim_id}'. available: {sorted(aggs)}"
            )
            return EXIT_TOOL_ERROR

        agg = aggs[chosen]
        passport_path = Path(args.passport).expanduser()
        try:
            data = load_passport(passport_path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            _err(f"reading passport: {exc}")
            return EXIT_TOOL_ERROR

        claims = data.setdefault("claims", {})
        entry: dict[str, Any] = {
            "experiment": args.experiment or script_label,
            "script": script_label,
            "seeds": seeds,
            "git_commit": git_commit(),
            "date": _dt.date.today().isoformat(),
            "value": round(float(agg["mean"]), 12),
            "std": round(float(agg["std"]), 12),
            "n": int(agg["n"]),
        }
        if len(series) > 1 or args.metric is not None:
            entry["metric"] = chosen
        claims[args.claim_id] = entry

        try:
            save_passport(passport_path, data)
        except OSError as exc:
            _err(f"writing passport: {exc}")
            return EXIT_TOOL_ERROR
        print(
            f"\n[harness] recorded claim '{args.claim_id}' "
            f"(value={entry['value']:.6g}, std={entry['std']:.6g}, n={entry['n']}) "
            f"-> {passport_path}"
        )

    return EXIT_OK


# --------------------------------------------------------------------------- #
# verify subcommand
# --------------------------------------------------------------------------- #
def _read_claims_file(path: Path) -> list[dict[str, Any]]:
    """Read a claims list. Supports two formats:

      .txt/.list : one entry per line:
                     <claim_id>                         (presence-only check)
                     <claim_id> <value>                 (value freshness)
                     <claim_id> <value> <std>           (value + std freshness)
                   '#' starts a comment; blank lines ignored.
      .yaml/.yml : a list, OR a mapping `claims: {id: {value:, std:}}`,
                   OR a flat mapping `{id: value}`.
    """
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return _normalise_yaml_claims(raw)

    claims: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        toks = line.split()
        rec: dict[str, Any] = {"claim_id": toks[0]}
        if len(toks) >= 2:
            try:
                rec["value"] = float(toks[1])
            except ValueError as exc:
                raise ValueError(f"{path}:{lineno}: bad value '{toks[1]}'") from exc
        if len(toks) >= 3:
            try:
                rec["std"] = float(toks[2])
            except ValueError as exc:
                raise ValueError(f"{path}:{lineno}: bad std '{toks[2]}'") from exc
        claims.append(rec)
    return claims


def _normalise_yaml_claims(raw: Any) -> list[dict[str, Any]]:
    """Coerce assorted YAML claim shapes into a list of {claim_id, value?, std?}."""
    out: list[dict[str, Any]] = []
    if raw is None:
        return out
    if isinstance(raw, dict) and "claims" in raw and isinstance(raw["claims"], dict):
        raw = raw["claims"]
    if isinstance(raw, dict):
        for cid, body in raw.items():
            rec: dict[str, Any] = {"claim_id": str(cid)}
            if isinstance(body, dict):
                if "value" in body:
                    rec["value"] = float(body["value"])
                if "std" in body:
                    rec["std"] = float(body["std"])
            elif isinstance(body, (int, float)):
                rec["value"] = float(body)
            out.append(rec)
        return out
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                out.append({"claim_id": item})
            elif isinstance(item, dict) and "claim_id" in item:
                rec = {"claim_id": str(item["claim_id"])}
                if "value" in item:
                    rec["value"] = float(item["value"])
                if "std" in item:
                    rec["std"] = float(item["std"])
                out.append(rec)
        return out
    raise ValueError("unrecognised YAML claims shape")


def cmd_verify(args: argparse.Namespace) -> int:
    passport_path = Path(args.passport).expanduser()
    try:
        data = load_passport(passport_path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        _err(f"reading passport: {exc}")
        return EXIT_TOOL_ERROR
    if not passport_path.exists():
        _err(f"passport not found: {passport_path}")
        return EXIT_TOOL_ERROR

    passport_claims = data.get("claims", {})
    if not isinstance(passport_claims, dict):
        _err(f"{passport_path}: 'claims' must be a mapping")
        return EXIT_TOOL_ERROR

    claims_path = Path(args.claims).expanduser()
    if not claims_path.exists():
        _err(f"claims file not found: {claims_path}")
        return EXIT_TOOL_ERROR
    try:
        claims = _read_claims_file(claims_path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        _err(f"reading claims: {exc}")
        return EXIT_TOOL_ERROR

    missing: list[str] = []
    stale: list[str] = []
    ok: list[str] = []

    for rec in claims:
        cid = rec["claim_id"]
        entry = passport_claims.get(cid)
        if entry is None:
            missing.append(cid)
            continue
        # Freshness check only for the fields the claim actually asserts.
        issues = []
        if "value" in rec and "value" in entry:
            if not _close(float(rec["value"]), float(entry["value"])):
                issues.append(f"value {rec['value']:g} != passport {float(entry['value']):g}")
        if "std" in rec and "std" in entry:
            if not _close(float(rec["std"]), float(entry["std"])):
                issues.append(f"std {rec['std']:g} != passport {float(entry['std']):g}")
        if issues:
            stale.append(f"{cid}: {'; '.join(issues)}")
        else:
            ok.append(cid)

    # Report.
    print(f"[verify] passport: {passport_path}")
    print(f"[verify] claims:   {claims_path}  ({len(claims)} claim(s))")
    print(f"[verify] provenanced + fresh: {len(ok)}")
    for cid in ok:
        print(f"  OK     {cid}")
    if missing:
        print(f"[verify] UNPROVENANCED (no passport entry): {len(missing)}")
        for cid in missing:
            print(f"  MISSING {cid}")
    if stale:
        print(f"[verify] STALE (value/std drifted vs passport): {len(stale)}")
        for s in stale:
            print(f"  STALE   {s}")

    blocked = bool(missing or stale)
    if not blocked:
        print("[verify] PASS — every claim is provenanced and fresh.")
    else:
        print(
            f"[verify] FOUND {len(missing)} unprovenanced + {len(stale)} stale claim(s)."
        )

    if blocked and args.strict:
        return EXIT_VERIFY_BLOCKED
    return EXIT_OK


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="experiment-harness.py",
        description="Multi-seed experiment runner + passport claim-provenance verifier.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    pr = sub.add_parser(
        "run",
        help="run a target script across N seeds and aggregate mean +/- std.",
        description=(
            "Run a target script once per seed, parse its numeric stdout, and "
            "aggregate. Optionally record the result as a passport claim."
        ),
    )
    pr.add_argument(
        "--script",
        nargs="+",
        required=True,
        metavar="CMD",
        help="target script/command. A lone .py is run with the venv python. "
        "Extra tokens are passed as args. --seed N and EXPERIMENT_SEED=N are "
        "appended/exported per run.",
    )
    pr.add_argument(
        "--seeds",
        required=True,
        help="comma list '1,2,3' or inclusive range '1-5'.",
    )
    pr.add_argument(
        "--metric",
        default=None,
        help="pin one metric name when the script emits several (else all are "
        "aggregated; required to disambiguate a passport claim).",
    )
    pr.add_argument(
        "--claim-id",
        default=None,
        help="stable claim id to record in the passport (requires --passport).",
    )
    pr.add_argument(
        "--passport",
        default=None,
        help="path to passport.yaml to create/update with the claim entry.",
    )
    pr.add_argument(
        "--experiment",
        default=None,
        help="human experiment label for the passport entry (default: the command).",
    )
    pr.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="per-seed wall-clock timeout in seconds (default 600).",
    )
    pr.set_defaults(func=cmd_run)

    pv = sub.add_parser(
        "verify",
        help="flag claims lacking a passport entry or stale vs the passport.",
        description=(
            "Cross-check a claims list against a passport.yaml. Reports MISSING "
            "(unprovenanced) and STALE (value/std drifted) claims. Default exit "
            "is 0 (reporting); pass --strict to exit 2 when findings exist."
        ),
    )
    pv.add_argument("--passport", required=True, help="path to passport.yaml.")
    pv.add_argument(
        "--claims",
        required=True,
        help="claims list: .txt (one '<id> [value] [std]' per line) or "
        ".yaml (list / mapping / passport-shaped).",
    )
    pv.add_argument(
        "--strict",
        action="store_true",
        help="exit 2 when any claim is unprovenanced or stale (for CI gates).",
    )
    pv.set_defaults(func=cmd_verify)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
