#!/usr/bin/env python3
"""ssot-lint.py : mechanical SSOT drift gate for the superclaude ~/.claude corpus.

Loads meta/concept-registry.yaml (the SSOT-of-SSOTs) and, for every registered concept:
  1. asserts canonical_home resolves (file exists; anchor, if given, is a real heading in it)
  2. if defining_marker is set, asserts it matches ONLY inside canonical_home across the corpus
     (a match elsewhere is a re-DEFINITION -- see the registry file header for the heading-line
     anchor convention that keeps legitimate prose citations from false-positiving)
  3. if forbidden_pattern is set, asserts ZERO matches outside canonical_home
  4. if ground_truth is set, runs it via `sh -c` and asserts exit 0
Plus a generic pointer resolver: every `SOT: \\`path\\`` and `see \\`path\\`` style reference found
anywhere in the corpus must resolve to a real file (report unresolved as failures).

stdlib only (no pyyaml): the registry file is a small hand-rolled YAML subset documented in its
own header comment, parsed by load_registry() below.

Usage:
    ssot-lint.py [--root DIR] [--registry PATH]

Exit 0 and a summary when clean; exit 1 and a per-failure report when any check fails.
"""
import argparse
import os
import re
import subprocess
import sys

CORPUS_DIRS = ("rules", "skills", "agents", "docs")
SKIP_DIR_NAMES = {".git", "__pycache__", "_archive"}
CORPUS_FILE_EXTS = (".md", ".yaml", ".yml")

HOME_RE = re.compile(r"^(?P<path>.+?\.(?:md|yaml|yml|py|sh|json))(?::(?P<anchor>.*))?$")
HEADING_RE = re.compile(r"^#{1,6}\s")

SOT_RE = re.compile(r"SOT:\s*`([^`]+)`")
SEE_RE = re.compile(r"\bsee\s+`([^`]+)`", re.IGNORECASE)
POINTER_PATH_RE = re.compile(r"^([~/.]?[\w][\w\-./]*\.(?:md|yaml|yml))")

# Known-benign unresolvable pointers: keyed to the exact (relative referencing file, raw
# backtick content) pair so an exemption can never widen to mask a different, real broken
# pointer in the same file. Each entry must document WHY it is intentionally unresolvable.
POINTER_IGNORES = {
    ("skills/research/references/report.md", "docs/reprod-notes.md §M3"): (
        "teaching example inside that file's own item 6, Quantitative grounding, illustrating "
        "how a downstream project would cite its own reproduction notes; the target is "
        "deliberately not a real file in this corpus, not drift"
    ),
}


class SsotLintError(Exception):
    """Malformed registry input -- distinct from a drift finding."""


# ---------------------------------------------------------------------------
# Registry loading (hand-rolled subset parser; see concept-registry.yaml header)
# ---------------------------------------------------------------------------

def parse_scalar(raw):
    raw = raw.strip()
    if raw.startswith("'"):
        out = []
        i = 1
        while i < len(raw):
            if raw[i] == "'":
                if i + 1 < len(raw) and raw[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                break
            out.append(raw[i])
            i += 1
        return "".join(out)
    return raw


def load_registry(path):
    if not os.path.isfile(path):
        raise SsotLintError(f"registry not found: {path}")
    records = []
    current = None
    seen_ids = set()
    with open(path, encoding="utf-8") as f:
        for lineno, raw_line in enumerate(f, 1):
            line = raw_line.rstrip("\n")
            if not line.strip():
                continue
            if line.lstrip().startswith("#"):
                continue
            m = re.match(r"^- id:\s*(.+)$", line)
            if m:
                if current is not None:
                    records.append(current)
                rec_id = parse_scalar(m.group(1))
                if rec_id in seen_ids:
                    raise SsotLintError(f"{path}:{lineno}: duplicate concept id {rec_id!r}")
                seen_ids.add(rec_id)
                current = {"id": rec_id, "_lineno": lineno}
                continue
            m = re.match(r"^\s+([a-zA-Z_]+):\s*(.*)$", line)
            if m:
                if current is None:
                    raise SsotLintError(f"{path}:{lineno}: field before any '- id:' record")
                current[m.group(1)] = parse_scalar(m.group(2))
                continue
            raise SsotLintError(f"{path}:{lineno}: unparseable registry line: {line!r}")
    if current is not None:
        records.append(current)
    if not records:
        raise SsotLintError(f"{path}: registry is empty")
    return records


def is_true(value):
    return str(value).strip().lower() == "true"


# ---------------------------------------------------------------------------
# Corpus access
# ---------------------------------------------------------------------------

def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def iter_corpus_files(root):
    paths = []
    claude_md = os.path.join(root, "CLAUDE.md")
    if os.path.isfile(claude_md):
        paths.append(claude_md)
    for d in CORPUS_DIRS:
        base = os.path.join(root, d)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [dn for dn in dirnames if dn not in SKIP_DIR_NAMES]
            for fn in filenames:
                if fn.endswith(CORPUS_FILE_EXTS):
                    paths.append(os.path.join(dirpath, fn))
    return sorted(paths)


def line_of(text, offset):
    return text.count("\n", 0, offset) + 1


# ---------------------------------------------------------------------------
# Per-concept checks
# ---------------------------------------------------------------------------

def split_home(value):
    m = HOME_RE.match(value)
    if not m:
        return value, None
    return m.group("path"), m.group("anchor")


def check_home(root, rec, failures):
    raw = rec.get("canonical_home")
    if not raw:
        failures.append(f"[{rec['id']}] missing required field canonical_home")
        return None
    rel_path, anchor = split_home(raw)
    abspath = os.path.join(root, rel_path)
    if not os.path.isfile(abspath):
        failures.append(f"[{rec['id']}] canonical_home file not found: {rel_path}")
        return None
    if anchor:
        text = read_text(abspath)
        found = any(
            HEADING_RE.match(line) and anchor.lower() in line.lower()
            for line in text.splitlines()
        )
        if not found:
            failures.append(
                f"[{rec['id']}] anchor {anchor!r} not found as a heading in {rel_path}"
            )
    return rel_path


def check_defining_marker(root, rec, home_rel, corpus_paths, failures):
    pattern = rec.get("defining_marker")
    if not pattern or home_rel is None:
        return
    try:
        rx = re.compile(pattern, re.MULTILINE)
    except re.error as e:
        failures.append(f"[{rec['id']}] defining_marker is not a valid regex: {e}")
        return
    home_abs = os.path.normpath(os.path.join(root, home_rel))
    found_in_home = False
    hits = set()  # (rel_path, lineno) deduped -- an alternation can match the same line twice
    for p in corpus_paths:
        text = read_text(p)
        for m in rx.finditer(text):
            if os.path.normpath(p) == home_abs:
                found_in_home = True
            else:
                hits.add((os.path.relpath(p, root), line_of(text, m.start())))
    for rel, lineno in sorted(hits):
        failures.append(
            f"[{rec['id']}] defining_marker re-defined outside canonical_home: {rel}:{lineno}"
        )
    if not found_in_home:
        failures.append(
            f"[{rec['id']}] defining_marker not found in its own canonical_home {home_rel}"
        )


def check_forbidden_pattern(root, rec, home_rel, corpus_paths, failures):
    pattern = rec.get("forbidden_pattern")
    if not pattern or home_rel is None:
        return
    try:
        rx = re.compile(pattern, re.MULTILINE)
    except re.error as e:
        failures.append(f"[{rec['id']}] forbidden_pattern is not a valid regex: {e}")
        return
    home_abs = os.path.normpath(os.path.join(root, home_rel))
    hits = set()  # (rel_path, lineno) deduped -- an alternation can match the same line twice
    for p in corpus_paths:
        if os.path.normpath(p) == home_abs:
            continue
        text = read_text(p)
        for m in rx.finditer(text):
            hits.add((os.path.relpath(p, root), line_of(text, m.start())))
    for rel, lineno in sorted(hits):
        failures.append(
            f"[{rec['id']}] forbidden_pattern hit outside canonical_home: {rel}:{lineno}"
        )


def check_ground_truth(rec, failures):
    cmd = rec.get("ground_truth")
    if not cmd:
        return
    result = subprocess.run(
        ["sh", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        detail = detail[0] if detail else ""
        suffix = f" ({detail})" if detail else ""
        failures.append(
            f"[{rec['id']}] ground_truth failed (exit {result.returncode}): {cmd}{suffix}"
        )


def check_concept(root, rec, corpus_paths, failures):
    home_rel = check_home(root, rec, failures)
    if is_true(rec.get("pointer_only", "false")):
        return
    check_defining_marker(root, rec, home_rel, corpus_paths, failures)
    check_forbidden_pattern(root, rec, home_rel, corpus_paths, failures)
    check_ground_truth(rec, failures)


# ---------------------------------------------------------------------------
# Generic pointer resolver: every `SOT: \`path\`` / `see \`path\`` must resolve
# ---------------------------------------------------------------------------

def build_basename_index(corpus_paths):
    idx = {}
    for p in corpus_paths:
        idx.setdefault(os.path.basename(p), []).append(p)
    return idx


def resolve_pointer_target(root, token, referencing_path, basename_index):
    if token.startswith("~/.claude/"):
        cand = os.path.join(root, token[len("~/.claude/"):])
        return cand if os.path.isfile(cand) else None
    if token.startswith("~/"):
        cand = os.path.expanduser(token)
        return cand if os.path.isfile(cand) else None
    if token.startswith("/"):
        return token if os.path.isfile(token) else None
    if "/" in token:
        cand = os.path.join(root, token)
        if os.path.isfile(cand):
            return cand
        cand2 = os.path.join(os.path.dirname(referencing_path), token)
        return cand2 if os.path.isfile(cand2) else None
    sibling = os.path.join(os.path.dirname(referencing_path), token)
    if os.path.isfile(sibling):
        return sibling
    matches = basename_index.get(token, [])
    return matches[0] if len(matches) == 1 else None


def check_generic_pointers(root, corpus_paths, failures, stats):
    basename_index = build_basename_index(corpus_paths)
    for p in corpus_paths:
        text = read_text(p)
        for rx in (SOT_RE, SEE_RE):
            for m in rx.finditer(text):
                raw = m.group(1).strip()
                pm = POINTER_PATH_RE.match(raw)
                if not pm:
                    continue
                token = pm.group(1)
                target = resolve_pointer_target(root, token, p, basename_index)
                rel = os.path.relpath(p, root)
                lineno = line_of(text, m.start())
                if target is not None:
                    stats["resolved"] += 1
                    continue
                if (rel, raw) in POINTER_IGNORES:
                    stats["ignored"] += 1
                    continue
                stats["unresolved"] += 1
                is_bare = "/" not in token and not token.startswith(("~", "/"))
                matches = basename_index.get(token, []) if is_bare else []
                if len(matches) > 1:
                    failures.append(
                        f"[pointer] {rel}:{lineno}: ambiguous target {raw!r} "
                        f"({len(matches)} basename matches in corpus)"
                    )
                else:
                    failures.append(f"[pointer] {rel}:{lineno}: unresolved target {raw!r}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", default="~/.claude", help="corpus root (default ~/.claude)"
    )
    parser.add_argument(
        "--registry",
        default=None,
        help="path to concept-registry.yaml (default <root>/meta/concept-registry.yaml)",
    )
    args = parser.parse_args(argv)

    root = os.path.abspath(os.path.expanduser(args.root))
    registry_path = (
        os.path.abspath(os.path.expanduser(args.registry))
        if args.registry
        else os.path.join(root, "meta", "concept-registry.yaml")
    )

    try:
        records = load_registry(registry_path)
    except SsotLintError as e:
        print(f"SSOT-LINT ERROR: {e}", file=sys.stderr)
        return 1

    corpus_paths = iter_corpus_files(root)
    if not corpus_paths:
        print(f"SSOT-LINT ERROR: no corpus files found under {root}", file=sys.stderr)
        return 1

    failures = []
    for rec in records:
        check_concept(root, rec, corpus_paths, failures)

    pointer_stats = {"resolved": 0, "unresolved": 0, "ignored": 0}
    check_generic_pointers(root, corpus_paths, failures, pointer_stats)

    if failures:
        print(f"SSOT-LINT: {len(failures)} failure(s) against {len(records)} concept(s)")
        print(f"root={root} registry={registry_path}")
        print()
        for f in failures:
            print(f"  FAIL {f}")
        print()
        print(
            f"pointer resolver: {pointer_stats['resolved']} resolved, "
            f"{pointer_stats['unresolved']} unresolved, "
            f"{pointer_stats['ignored']} ignored"
        )
        return 1

    print(
        f"SSOT-LINT: clean. {len(records)} concept(s) checked, "
        f"{len(corpus_paths)} corpus file(s) scanned."
    )
    print(
        f"pointer resolver: {pointer_stats['resolved']} resolved, "
        f"{pointer_stats['unresolved']} unresolved, "
        f"{pointer_stats['ignored']} ignored"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
