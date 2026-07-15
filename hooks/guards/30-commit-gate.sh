# Guard: 30-commit-gate: F3, git commit/add discipline (PHASE2-CONTRACT sec 1,
# enforcement-gap-ledger.md Family 3 #8-#12). Extends the existing warn-only
# hooks/modules/25-commit-gate.sh message-extraction logic. Default mode
# block. Fires only on GUARD_TOOL==Bash where guard_command matches a
# `git commit` or `git add` invocation (optionally prefixed with
# `git -C <path>`).
#
# Owner-ratified severity split (2026-07-15): only the two checks that guard
# an irreversible or hard-to-detect problem BLOCK; the two stylistic checks
# WARN only, since a false-block (e.g. a valid `feat!:` breaking-change
# subject) was worse than an occasional missed warning.
#
# Checks:
#   git commit -> subject matches conventional format (rules/00; WARN,
#                 hardened surface reused from hooks/modules/25-commit-gate.sh),
#                 staged diff is not mode-only (rules/00, rules/21 WSL File
#                 Permissions; BLOCK), staged diff does not add a
#                 secret-shaped string (rules/00 Security; BLOCK).
#   git add    -> bulk-add forms (., -A, --all, -u) WARN; explicit pathspecs
#                 allowed (rules/00 Minimal Changes / Git Discipline,
#                 w-committer.md:73-80 staging discipline).
#
# No Co-Authored-By trailer check: dropped per owner ruling (the /git policy
# already covers the underlying concern, and a stderr warn from this guard
# never reached the owner anyway).
#
# Staged-diff checks read the CURRENTLY staged diff of the cwd repo via
# `git -C <cwd> diff --cached` (this hook runs PreToolUse, before the commit
# lands). FAIL-OPEN: any git/parse error (not a repo, git missing) passes.

GUARD_MODE_COMMIT_GATE=block

# ── git add: bulk-add detector ────────────────────────────────────────────────
# Isolates the add invocation's own argument list (stops at the next shell
# separator so a chained `git add . && git commit ...` is scoped correctly),
# then warns on an exact bulk-add token. Explicit pathspecs (e.g. path/to/file)
# fall through untouched. WARN, not BLOCK (owner ruling 2026-07-15).
_guard_commit_gate_add() {
  local cmd="$1" seg word
  seg=$(printf '%s' "$cmd" | grep -oP -- 'git(\s+-C\s+\S+)?\s+add\s+\K[^;&|]*' | head -1)
  [ -n "$seg" ] || return 0
  for word in $seg; do
    case "$word" in
      .|-A|--all|-u|-Au|-uA|-au)
        guard_warn "bulk 'git add $word' discouraged; stage explicit pathspecs only (rules/00 Git Discipline, w-committer.md:73-80 staging discipline)"
        return 0
        ;;
    esac
  done
}

# ── git commit: subject-line extraction ─────────────────────────────────────
# Handles two forms: a simple quoted `-m "text"` / `-m 'text'`, and the
# heredoc-wrapped `-m "$(cat <<'EOF' ... EOF)"` form used throughout this
# harness's own commit convention. The simple extraction is tried first; if it
# yields heredoc boilerplate (e.g. `$(cat <<`) instead of real text, the awk
# fallback takes the first non-blank line after the `<<` opener as the subject.
_guard_commit_gate_subject() {
  local cmd="$1" subj
  subj=$(printf '%s' "$cmd" | grep -oP -- "-m\s+[\"']\K[^\"']+" | head -1)
  if [ -z "$subj" ] || printf '%s' "$subj" | grep -qE '\$\(|<<'; then
    subj=$(printf '%s\n' "$cmd" | awk '/<</{f=1;next} f && NF>0 {print; exit}')
  fi
  printf '%s' "$subj"
}

# ── git commit: staged-diff checks (mode-only, secret-shaped content) ───────
_guard_commit_gate_staged() {
  local cwd
  cwd=$(pwd 2>/dev/null) || return 0
  git -C "$cwd" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 0

  local summary shortstat
  summary=$(git -C "$cwd" diff --cached --summary 2>/dev/null)
  shortstat=$(git -C "$cwd" diff --cached --shortstat 2>/dev/null)
  if printf '%s' "$summary" | grep -q 'mode change' \
     && printf '%s' "$shortstat" | grep -qE '0 insertions?\(\+\), 0 deletions?\(-\)'; then
    guard_block "staged diff is mode-only (chmod, e.g. 755 to 644, zero content change); WSL/NTFS strips the exec bit spuriously (rules/00 Git Discipline, rules/21 WSL File Permissions)"
    return 0
  fi

  # The keyword arm requires a SECRET-SHAPED VALUE (>=16 chars of a
  # base64/hex-ish alphabet, quoted or bare), not just a keyword-shaped name.
  # Without this, everyday constants like `MAX_TOKEN_COUNT = 100`,
  # `API_KEY_HEADER = "X-Api-Key"`, or `SESSION_TOKEN_TTL=3600` false-blocked
  # every commit that touched them (SEAL-A-verdict.md M2). AKIA and
  # PRIVATE-KEY shapes are already secret-shaped and are left as-is.
  if git -C "$cwd" diff --cached 2>/dev/null | grep -E '^\+' | grep -v '^\+\+\+ ' \
       | grep -qP -- "AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|[A-Z_]*(SECRET|TOKEN|PASSWORD|API_?KEY)[A-Z_0-9]*\s*=\s*[\"']?[A-Za-z0-9+/=_-]{16,}"; then
    guard_block "staged diff adds a secret-shaped string (AWS-key shape, private-key header, or SECRET/TOKEN/PASSWORD/API_KEY assignment); rules/00 Security: no secrets in code"
  fi
}

# ── git commit: message checks + dispatch to staged-diff checks ─────────────
_guard_commit_gate_commit() {
  local cmd="$1"

  # --amend with no new -m keeps the prior (already-checked) message; skip the
  # subject check only, mirroring the amend-skip in the existing warn-only
  # hooks/modules/25-commit-gate.sh. Staged-diff checks still apply.
  if ! { printf '%s' "$cmd" | grep -qE -- '--amend' \
         && ! printf '%s' "$cmd" | grep -qE -- '-m[[:space:]]'; }; then
    local subj
    subj=$(_guard_commit_gate_subject "$cmd")
    if [ -n "$subj" ] \
       && ! printf '%s' "$subj" | grep -qE -- '^[[:space:]]*(feat|fix|test|docs|chore|refactor|style|ci|perf|build)(\([^)]+\))?:[[:space:]]'; then
      guard_warn "commit subject does not match conventional format feat|fix|test|docs|chore|refactor|style|ci|perf|build(scope)?: <text> (rules/00 Commit Protocol)"
    fi
  fi

  _guard_commit_gate_staged
}

guard_commit_gate() {
  local cmd
  cmd=$(guard_command)
  [ -n "$cmd" ] || return 0

  # Anchor at command/line start or after a shell separator (;, &&, ||), never a
  # bare substring match: mirrors hooks/modules/25-commit-gate.sh's line-start
  # anchor (extended with the separator alternation for same-line chains, e.g.
  # `git add . && git commit ...`). Without this anchor a command like
  # `echo "please run git commit -m foo"` would false-block on its own string
  # argument.
  printf '%s' "$cmd" | grep -qP -- '(^|;|&&|\|\|)[[:space:]]*git(\s+-C\s+\S+)?\s+add\s' && _guard_commit_gate_add "$cmd"
  printf '%s' "$cmd" | grep -qP -- '(^|;|&&|\|\|)[[:space:]]*git(\s+-C\s+\S+)?\s+commit(\s|$)' && _guard_commit_gate_commit "$cmd"
  return 0
}
