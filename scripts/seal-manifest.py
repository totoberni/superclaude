#!/usr/bin/env python3
"""seal-manifest.py: in-session sealed-manifest sidecar for F5 #21 (revision-binding).

The autonomous converge driver (scripts/swarm/converge_auto.py) records a
`sealed_manifest` in handoff.json at seal time, and scripts/swarm/seal-void-hook.sh
consumes it post-commit to void a seal whose sealed content changed. The IN-SESSION
/converge path (meta/orch driving rounds directly) never writes handoff.json or a
sealed_manifest, so --install-void-hook alone is a no-op in-session (design r1/B1).

This script is the in-session equivalent: it computes the SAME content manifest the
driver does (byte-identical to converge_auto.compute_manifest() and the void hook's
recompute_manifest) and records it, plus the bound git revision, to a sidecar
`<campaign>/seal-manifest.json`. A `check` subcommand recomputes and reports VOID
when a sealed path changed (content-precise, matching the driver) and, under
--head-binds, when HEAD moved off the bound revision.

Manifest algorithm (MUST stay byte-identical to the driver):
    entries = [ f"{rel}:{sha256_hex(bytes(rel))}"  # "MISSING" if not a file
                for rel in artifact_paths ]
    manifest = sha256_hex( "\n".join(sorted(entries)) )   # no trailing newline
`rel` is the path AS GIVEN (the label); it resolves against the repo when relative.

Stdlib only. Exit codes for `check`: 0 = OK (seal intact), 1 = VOID (seal broken),
2 = ERROR (sidecar/args unusable). A caller warns on 1 only; 2 is fail-open.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone


def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve(repo, rel):
    """Resolve rel against repo exactly as converge_auto.Loop._resolve does:
    an absolute rel stands alone; a relative rel joins the repo dir."""
    return rel if os.path.isabs(rel) else os.path.join(repo, rel)


def compute_manifest(repo, artifact_paths):
    """Byte-identical to converge_auto.compute_manifest() and the void hook."""
    entries = []
    for rel in artifact_paths:
        path = resolve(repo, rel)
        try:
            with open(path, "rb") as handle:
                digest = sha256_bytes(handle.read())
        except OSError:
            digest = "MISSING"
        entries.append("%s:%s" % (rel, digest))
    return sha256_text("\n".join(sorted(entries)))


def git_head(repo):
    """Full HEAD hash, or "" when repo is not a git work tree / git is absent."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _paths_from_rounds_md(path):
    """Best-effort extraction of sealed artifact paths named in a rounds.md.

    Two conventions are recognized (either or both may appear):
      * a line `Sealed-Paths: a b, c` (space- or comma-separated), and
      * bullet lines (`- p` / `* p`) under a heading whose text contains
        "sealed artifact" (case-insensitive), until the next heading/blank run.
    """
    out = []
    in_section = False
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            stripped = line.strip()
            low = stripped.lower()
            if low.startswith("sealed-paths:"):
                rest = stripped.split(":", 1)[1]
                for tok in rest.replace(",", " ").split():
                    out.append(tok)
                continue
            if stripped.startswith("#"):
                in_section = "sealed artifact" in low
                continue
            if in_section:
                if not stripped:
                    continue
                if stripped[0] in "-*":
                    tok = stripped[1:].strip()
                    if tok:
                        out.append(tok.split()[0])
                else:
                    in_section = False
    # de-dupe preserving order
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _collect_paths(args):
    paths = list(args.path or [])
    if args.paths_file:
        with open(args.paths_file, "r", encoding="utf-8") as handle:
            for line in handle:
                tok = line.strip()
                if tok and not tok.startswith("#"):
                    paths.append(tok)
    if args.rounds_md:
        paths.extend(_paths_from_rounds_md(args.rounds_md))
    # de-dupe preserving order
    seen, uniq = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


# ── subcommands ─────────────────────────────────────────────────────────────

def cmd_manifest(args):
    """Print just the content manifest for the given paths (byte-compat probe)."""
    repo = os.path.abspath(os.path.expanduser(args.repo))
    paths = _collect_paths(args)
    if not paths:
        sys.stderr.write("seal-manifest: no artifact paths given\n")
        return 2
    sys.stdout.write(compute_manifest(repo, paths))
    return 0


def cmd_seal(args):
    repo = os.path.abspath(os.path.expanduser(args.repo))
    paths = _collect_paths(args)
    if not paths:
        sys.stderr.write("seal-manifest: no artifact paths given (use --path / --paths-file / --rounds-md)\n")
        return 2

    manifest = compute_manifest(repo, paths)
    head = git_head(repo)
    sidecar = {
        "campaign": args.campaign or "",
        "repo": repo,
        "artifact_paths": paths,
        "sealed_manifest": manifest,
        "bound_revision": head,
        "sealed_at": iso_now(),
        "round": args.round,
        "seal_line": args.seal_line or "",
    }

    if args.out:
        out_path = os.path.abspath(os.path.expanduser(args.out))
    elif args.campaign_dir:
        out_path = os.path.join(
            os.path.abspath(os.path.expanduser(args.campaign_dir)), "seal-manifest.json")
    else:
        sys.stderr.write("seal-manifest: seal needs --out or --campaign-dir\n")
        return 2

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(sidecar, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, out_path)

    bound = "commit:%s" % head[:12] if head else "content-only"
    sys.stdout.write(
        "SEALED %s manifest=sha256:%s bound=%s paths=%d -> %s\n"
        % (sidecar["campaign"] or "(unnamed)", manifest[:12], bound, len(paths), out_path))
    return 0


def cmd_check(args):
    sidecar_path = os.path.abspath(os.path.expanduser(args.sidecar))
    try:
        with open(sidecar_path, "r", encoding="utf-8") as handle:
            sidecar = json.load(handle)
    except (OSError, ValueError) as exc:
        sys.stdout.write("SEAL-STATUS: ERROR reason=sidecar-unreadable (%s)\n" % exc)
        return 2

    repo = args.repo or sidecar.get("repo")
    if not repo:
        sys.stdout.write("SEAL-STATUS: ERROR reason=no-repo-in-sidecar\n")
        return 2
    repo = os.path.abspath(os.path.expanduser(repo))

    paths = sidecar.get("artifact_paths") or []
    sealed_manifest = sidecar.get("sealed_manifest") or ""
    bound_revision = sidecar.get("bound_revision") or ""
    if not paths or not sealed_manifest:
        sys.stdout.write("SEAL-STATUS: ERROR reason=sidecar-missing-manifest-or-paths\n")
        return 2

    current_manifest = compute_manifest(repo, paths)
    current_head = git_head(repo)

    if current_manifest != sealed_manifest:
        sys.stdout.write(
            "SEAL-STATUS: VOID reason=content-changed "
            "sealed=sha256:%s current=sha256:%s\n"
            % (sealed_manifest[:12], current_manifest[:12]))
        return 1

    head_moved = bool(bound_revision) and bool(current_head) and current_head != bound_revision
    if head_moved and args.head_binds:
        sys.stdout.write(
            "SEAL-STATUS: VOID reason=head-moved bound=commit:%s current=commit:%s\n"
            % (bound_revision[:12], current_head[:12]))
        return 1

    note = " note=head-moved-content-stable" if head_moved else ""
    sys.stdout.write(
        "SEAL-STATUS: OK manifest=sha256:%s%s\n" % (sealed_manifest[:12], note))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="seal-manifest.py",
        description="In-session sealed-manifest sidecar for revision-binding (F5 #21).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_path_args(sp):
        sp.add_argument("--repo", required=True, help="repo root (relative paths resolve here)")
        sp.add_argument("--path", action="append", default=[], help="a sealed artifact path (repeatable)")
        sp.add_argument("--paths-file", help="file with one sealed artifact path per line")
        sp.add_argument("--rounds-md", help="rounds.md naming sealed paths (best-effort)")

    sp_seal = sub.add_parser("seal", help="record a sealed-manifest sidecar")
    add_path_args(sp_seal)
    sp_seal.add_argument("--campaign", help="campaign name (recorded in the sidecar)")
    sp_seal.add_argument("--campaign-dir", help="dir to write <dir>/seal-manifest.json")
    sp_seal.add_argument("--out", help="explicit sidecar output path (overrides --campaign-dir)")
    sp_seal.add_argument("--round", help="round label at seal time")
    sp_seal.add_argument("--seal-line", help="the SEAL: token line, for the record")
    sp_seal.set_defaults(func=cmd_seal)

    sp_check = sub.add_parser("check", help="recheck a sidecar; VOID if sealed content changed")
    sp_check.add_argument("--sidecar", required=True, help="path to the seal-manifest.json sidecar")
    sp_check.add_argument("--repo", help="override the repo recorded in the sidecar")
    sp_check.add_argument("--head-binds", action="store_true",
                          help="also VOID when HEAD moved off the bound revision (even if content is byte-identical)")
    sp_check.set_defaults(func=cmd_check)

    sp_man = sub.add_parser("manifest", help="print the content manifest for the given paths")
    add_path_args(sp_man)
    sp_man.set_defaults(func=cmd_manifest)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
