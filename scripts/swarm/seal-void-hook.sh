#!/bin/bash
# converge-auto-seal-void-hook
# Installed by: converge_auto.py --install-void-hook <repo>
#
# Post-commit hook. On every commit it scans the converge-auto runtime dirs
# ($HOME/.claude/plans/*/auto/*/handoff.json) for loops marked sealed. For a
# sealed loop bound in THIS repo, it recomputes the CONTENT manifest of the loop's
# artifact scope and voids the seal iff that manifest differs from the sealed_manifest
# recorded at seal time. Voiding appends a post-hoc void row to the loop's ledger
# (rounds.md) and drops a VOIDED marker in the runtime dir. This makes seal-voiding
# enforcement live in git itself, independent of the driver process lifetime.
#
# Content-precise (composes with /commit false): a byte-identical post-seal commit
# (a squash, an amend/message rewrite, or the owner committing an already-sealed
# tree) does NOT void, while any real content change DOES, whoever commits it, with
# the driver long gone. The manifest construction matches converge_auto.py
# compute_manifest() exactly (sha256 over the code-point-sorted, newline-joined list
# of "<rel>:<sha256(bytes)>" entries, "MISSING" for absent paths, no trailing newline).
#
# Fail-safe: this hook ALWAYS exits 0 (a post-commit hook must never block a
# commit). It requires jq and git; if either is missing it silently chains any
# pre-existing hook and exits. A sealed loop with no sealed_manifest, or a manifest
# that cannot be recomputed, is skipped (never a spurious void). Any pre-existing
# post-commit displaced at install time lives as post-commit.chained and is exec'd here.

set +e

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"

chain_and_exit() {
  if [ -n "$HOOK_DIR" ] && [ -x "$HOOK_DIR/post-commit.chained" ]; then
    "$HOOK_DIR/post-commit.chained" "$@" || true
  fi
  exit 0
}

# Recompute the content manifest EXACTLY as converge_auto.py compute_manifest():
# sha256 over the code-point-sorted, newline-joined "<rel>:<sha256(bytes)>" entries
# ("MISSING" when a path is absent), with NO trailing newline. python3 (byte-identical
# to the driver) when present; sha256sum + LC_ALL=C sort otherwise. Echoes nothing on
# error so the caller skips (fail-safe: never a spurious void).
recompute_manifest() {
  local repo="$1"; shift
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$repo" "$@" <<'PYEOF'
import hashlib, os, sys
repo = sys.argv[1]
paths = sys.argv[2:]
entries = []
for rel in paths:
    p = rel if os.path.isabs(rel) else os.path.join(repo, rel)
    try:
        with open(p, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        digest = "MISSING"
    entries.append("%s:%s" % (rel, digest))
sys.stdout.write(hashlib.sha256("\n".join(sorted(entries)).encode("utf-8")).hexdigest())
PYEOF
  else
    local rel f d sorted_entries
    sorted_entries="$(
      for rel in "$@"; do
        case "$rel" in
          /*) f="$rel" ;;
          *) f="$repo/$rel" ;;
        esac
        if [ -f "$f" ]; then
          d="$(sha256sum "$f" 2>/dev/null | cut -d' ' -f1)"
        else
          d="MISSING"
        fi
        printf '%s:%s\n' "$rel" "$d"
      done | LC_ALL=C sort
    )"
    printf '%s' "$sorted_entries" | sha256sum | cut -d' ' -f1
  fi
}

command -v jq >/dev/null 2>&1 || chain_and_exit "$@"
command -v git >/dev/null 2>&1 || chain_and_exit "$@"

REPO_TOP="$(git rev-parse --show-toplevel 2>/dev/null)" || chain_and_exit "$@"
[ -n "$REPO_TOP" ] || chain_and_exit "$@"
HEAD_HASH="$(git rev-parse HEAD 2>/dev/null)" || chain_and_exit "$@"
HEAD12="$(printf '%s' "$HEAD_HASH" | cut -c1-12)"
TS="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"

shopt -s nullglob
for handoff in "$HOME"/.claude/plans/*/auto/*/handoff.json; do
  status="$(jq -r '.status // empty' "$handoff" 2>/dev/null)"
  [ "$status" = "sealed" ] || continue
  rt_dir="$(dirname "$handoff")"
  loop_json="$rt_dir/loop.json"
  [ -f "$loop_json" ] || continue

  loop_repo="$(jq -r '.repo // empty' "$loop_json" 2>/dev/null)"
  [ -n "$loop_repo" ] || continue
  loop_repo="${loop_repo/#\~/$HOME}"
  loop_top="$(git -C "$loop_repo" rev-parse --show-toplevel 2>/dev/null)" || continue
  [ "$loop_top" = "$REPO_TOP" ] || continue

  sealed_manifest="$(jq -r '.seal.sealed_manifest // empty' "$handoff" 2>/dev/null)"
  # No content baseline recorded (pre-manifest seal) -> cannot be content-precise; skip.
  [ -n "$sealed_manifest" ] || continue

  aps=()
  while IFS= read -r ap; do
    [ -n "$ap" ] && aps+=("$ap")
  done < <(jq -r '.artifact_paths[]? // empty' "$loop_json" 2>/dev/null)
  [ "${#aps[@]}" -gt 0 ] || continue

  current_manifest="$(recompute_manifest "$loop_repo" "${aps[@]}")"
  [ -n "$current_manifest" ] || continue
  # Byte-identical sealed content (squash, amend, message rewrite, benign re-commit) -> keep.
  [ "$current_manifest" = "$sealed_manifest" ] && continue

  # Content of the sealed scope actually changed -> void the seal.
  bound="$(jq -r '.seal.bound_revision // empty' "$handoff" 2>/dev/null)"
  round="$(jq -r '.round // 0' "$handoff" 2>/dev/null)"
  bound12="$(printf '%s' "$bound" | cut -c1-12)"
  {
    printf '## R%s.escalate - %s - escalate:seal_voided_post_hoc\n' "$round" "$TS"
    printf -- '- delta: post-hoc content change in sealed scope (commit %s)\n' "$HEAD12"
    printf -- '- token: none\n'
    printf -- '- findings-open: 0\n'
    printf -- '- manifest: commit:%s\n' "$bound12"
    printf '\n'
  } >> "$rt_dir/rounds.md" 2>/dev/null || true
  printf 'voided by commit %s at %s\n' "$HEAD_HASH" "$TS" > "$rt_dir/VOIDED" 2>/dev/null || true
done

chain_and_exit "$@"
