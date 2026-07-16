#!/usr/bin/env bash
# Bite-test for 30-commit-gate (PHASE2-CONTRACT sec 6, enforcement-gap-ledger.md
# Family 3 #8-#12). Self-contained, /tmp only, never touches a real repo.
#
# 30-commit-gate.sh is a Wave 1 guard, not yet wired into the real
# guard-dispatch.sh (task scope forbids editing that file). This test therefore
# generates a small ephemeral harness under $TMPD that mirrors guard-dispatch.sh's
# PreToolUse flow (source lib.sh + lib-guard.sh + the guard, guard_init,
# run_guard) but explicitly calls run_guard guard_commit_gate, since the real
# dispatcher does not yet. The harness is discarded with $TMPD; it is test
# scaffolding, not a guard-subsystem artifact.
#
# Fixture repos (all under $TMPD, throwaway):
#   REPO_GOOD   - a real (non-mode, non-secret) content change staged
#   REPO_MODE   - ONLY a chmod (755<->644) staged, zero content change
#   REPO_SECRET - a new file with an AKIA-shaped string staged
#   REPO_NONE   - a plain directory, not a git repo (fail-open check)
#
# Cases (owner-ratified severity split 2026-07-15: secret + WSL mode-only
# BLOCK; conventional-format + bulk-add WARN; co-author check removed):
#   (b) commit, heredoc-wrapped -m, conventional subject       -> pass
#   (c) commit, non-conventional subject                       -> pass + WARN
#   (d) git add .                                               -> pass + WARN
#   (e) git add path/to/file                                    -> pass
#   (f) commit, compliant message, REPO_MODE (mode-only)      -> block
#   (g) commit, compliant message, REPO_SECRET (AKIA staged)  -> block
#   (h) commit, compliant message, REPO_NONE (not a repo)     -> pass
#   (i) mode=warn, git add .                                  -> pass + WARN
#
# (j-m) SEAL-A-verdict.md M2: keyword-shaped but NOT secret-shaped constant
# assignments must pass (a short integer, header name, URL path, or TTL value
# is not a secret). (n) a real high-entropy AWS secret value must still block.
#
# (o-p) subject regex tolerates the Conventional-Commits breaking-change
# marker `!` (feat!: / fix(scope)!:); these must pass with no WARN.

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
GUARDS_DIR="$(cd "$TESTDIR/.." && pwd)"
HOOKS_DIR="$(cd "$GUARDS_DIR/.." && pwd)"

TMPD="$(mktemp -d "${TMPDIR:-/tmp}/commit-gate-bite.XXXXXX")"
trap 'rm -rf "$TMPD"' EXIT
fails=0

# ── Ephemeral harness (see header note above) ────────────────────────────────
HARNESS="$TMPD/harness.sh"
cat > "$HARNESS" <<HARNESSEOF
#!/usr/bin/env bash
set -uo pipefail
INPUT=\$(cat)
GUARD_PHASE="pre"
. "$HOOKS_DIR/lib.sh" 2>/dev/null || true
. "$GUARDS_DIR/lib-guard.sh" || { echo "harness: lib-guard.sh missing" >&2; exit 0; }
. "$GUARDS_DIR/30-commit-gate.sh" || { echo "harness: 30-commit-gate.sh missing" >&2; exit 0; }
if [ "\${SUPERCLAUDE_GUARDS:-}" = "off" ]; then exit 0; fi
guard_init "\$INPUT"
run_guard guard_commit_gate
exit 0
HARNESSEOF

# ── Fixture repos ─────────────────────────────────────────────────────────────
REPO_GOOD="$TMPD/repo-good"
REPO_MODE="$TMPD/repo-mode"
REPO_SECRET="$TMPD/repo-secret"
REPO_NONE="$TMPD/repo-none"
REPO_FP="$TMPD/repo-fp"
REPO_REALSECRET="$TMPD/repo-realsecret"

setup_git() {
  git init -q "$1"
  git -C "$1" config user.email t@example.com
  git -C "$1" config user.name test
}

mkdir -p "$REPO_NONE"

setup_git "$REPO_GOOD"
printf 'line one\n' > "$REPO_GOOD/f.txt"
git -C "$REPO_GOOD" add f.txt
git -C "$REPO_GOOD" commit -q -m "chore: init fixture"
printf 'line two\n' >> "$REPO_GOOD/f.txt"
git -C "$REPO_GOOD" add f.txt

setup_git "$REPO_MODE"
printf 'line one\n' > "$REPO_MODE/f.txt"
chmod 644 "$REPO_MODE/f.txt"
git -C "$REPO_MODE" add f.txt
git -C "$REPO_MODE" commit -q -m "chore: init fixture"
chmod 755 "$REPO_MODE/f.txt"
git -C "$REPO_MODE" add f.txt

setup_git "$REPO_SECRET"
printf 'AKIA1234567890ABCDEF\n' > "$REPO_SECRET/secret.txt"
git -C "$REPO_SECRET" add secret.txt

# M2 false-positive fixture: keyword-shaped names with NON-secret-shaped
# values (short integer, header name, URL path, TTL).
setup_git "$REPO_FP"
cat > "$REPO_FP/config.py" <<'FPEOF'
MAX_TOKEN_COUNT = 100
RESET_PASSWORD_URL = "/x"
API_KEY_HEADER = "X-Api-Key"
SESSION_TOKEN_TTL=3600
FPEOF
git -C "$REPO_FP" add config.py

# M2 real-secret fixture: a genuine high-entropy AWS secret value must still
# block (only the non-secret-shaped VALUE case is relaxed).
setup_git "$REPO_REALSECRET"
printf 'AWS_SECRET_ACCESS_KEY="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"\n' > "$REPO_REALSECRET/config.py"
git -C "$REPO_REALSECRET" add config.py

# ── stdin + run helpers ───────────────────────────────────────────────────────
mk_stdin() {
  jq -nc --arg c "$1" '{tool_name:"Bash", tool_input:{command:$c}}'
}

# run_case <label> <repo_dir> <command_string> <expected_rc> <stderr_must_match|""> <stderr_must_not_match|""> [ENV=VAL ...]
run_case() {
  local label="$1" repo="$2" content="$3" want_rc="$4" must="$5" mustnot="$6"; shift 6
  local stdin_file="$TMPD/stdin.json" err_file="$TMPD/stderr.txt"
  mk_stdin "$content" > "$stdin_file"
  ( cd "$repo" && env "$@" bash "$HARNESS" < "$stdin_file" > /dev/null 2> "$err_file" )
  local rc=$?
  local ok=1
  [ "$rc" -eq "$want_rc" ] || { ok=0; echo "    rc=$rc want=$want_rc"; }
  if [ -n "$must" ] && ! grep -q "$must" "$err_file"; then
    ok=0; echo "    stderr missing: '$must'"; echo "    stderr was: $(cat "$err_file")"
  fi
  if [ -n "$mustnot" ] && grep -q "$mustnot" "$err_file"; then
    ok=0; echo "    stderr unexpectedly matched: '$mustnot'"; echo "    stderr was: $(cat "$err_file")"
  fi
  if [ "$ok" -eq 1 ]; then
    echo "  PASS: $label"
  else
    echo "  FAIL: $label"
    fails=$((fails + 1))
  fi
}

# ── Command strings ────────────────────────────────────────────────────────────
CMD_B=$(cat <<'CMDEOF'
git commit -m "$(cat <<'EOF'
fix: x

Co-Authored-By: Claude <n@a>
EOF
)"
CMDEOF
)

CMD_C='git commit -m "bad message"'
CMD_D='git add .'
CMD_E='git add path/to/file'
CMD_COMPLIANT='git commit -m "fix: x"'
CMD_BANG='git commit -m "feat!: x"'
CMD_BANG_SCOPED='git commit -m "fix(api)!: x"'

echo "=== test-30-commit-gate ==="
run_case "(b) commit, heredoc -m, conventional subject -> pass" \
  "$REPO_GOOD" "$CMD_B" 0 "" "GUARD-BLOCK"
run_case "(c) commit, non-conventional subject -> pass + WARN" \
  "$REPO_GOOD" "$CMD_C" 0 "conventional format" "GUARD-BLOCK"
run_case "(d) git add . -> pass + WARN" \
  "$REPO_GOOD" "$CMD_D" 0 "bulk 'git add" "GUARD-BLOCK"
run_case "(e) git add path/to/file -> pass" \
  "$REPO_GOOD" "$CMD_E" 0 "" "GUARD-BLOCK"
run_case "(f) commit, compliant message, mode-only staged -> block" \
  "$REPO_MODE" "$CMD_COMPLIANT" 2 "mode-only" ""
run_case "(g) commit, compliant message, secret staged -> block" \
  "$REPO_SECRET" "$CMD_COMPLIANT" 2 "secret-shaped" ""
run_case "(h) commit, compliant message, not a repo -> pass (fail-open)" \
  "$REPO_NONE" "$CMD_COMPLIANT" 0 "" "GUARD-BLOCK"
run_case "(i) mode=warn, git add . -> pass + WARN" \
  "$REPO_GOOD" "$CMD_D" 0 "WARN" "" SUPERCLAUDE_GUARD_COMMIT_GATE=warn
run_case "(j) commit, MAX_TOKEN_COUNT = 100 staged -> pass (not secret-shaped)" \
  "$REPO_FP" "$CMD_COMPLIANT" 0 "" "GUARD-BLOCK"
run_case "(k) commit, API_KEY_HEADER = \"X-Api-Key\" staged -> pass (not secret-shaped)" \
  "$REPO_FP" "$CMD_COMPLIANT" 0 "" "GUARD-BLOCK"
run_case "(l) commit, RESET_PASSWORD_URL = \"/x\" staged -> pass (not secret-shaped)" \
  "$REPO_FP" "$CMD_COMPLIANT" 0 "" "GUARD-BLOCK"
run_case "(m) commit, SESSION_TOKEN_TTL=3600 staged -> pass (not secret-shaped)" \
  "$REPO_FP" "$CMD_COMPLIANT" 0 "" "GUARD-BLOCK"
run_case "(n) commit, real high-entropy AWS_SECRET_ACCESS_KEY staged -> block" \
  "$REPO_REALSECRET" "$CMD_COMPLIANT" 2 "secret-shaped" ""
run_case "(o) commit, feat!: x (breaking-change marker) -> pass, no WARN" \
  "$REPO_GOOD" "$CMD_BANG" 0 "" "conventional format"
run_case "(p) commit, fix(api)!: x (scoped breaking-change marker) -> pass, no WARN" \
  "$REPO_GOOD" "$CMD_BANG_SCOPED" 0 "" "conventional format"

if [ "$fails" -eq 0 ]; then
  echo "test-30-commit-gate: ALL PASS"
  exit 0
else
  echo "test-30-commit-gate: $fails case(s) FAILED"
  exit 1
fi
