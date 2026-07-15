# Guard: 26-git-policy: mechanical enforcement of the /git true|false policy.
#
# Owner ruling (2026-07-15): `/git false` disables commit of ANY kind and push of
# ANY kind; agents ask the owner to run `/git true`. Agents are good at finding
# circumventions, so the block must be robust. This guard replaces the old
# per-op /commit and /push toggles for the commit/push case.
#
# Fires only on GUARD_TOOL==Bash. Default mode block. The policy state lives in a
# single file (SOT written only by the /git skill):
#   ${SUPERCLAUDE_GIT_POLICY_FILE:-$HOME/.claude/config/git-policy}
# contents are the word `enabled` or `disabled`. The path is env-overridable so
# tests never touch the real file.
#
# Enforcement is one-directional and fail-open: the guard blocks ONLY when the
# file exists, reads cleanly, and says `disabled`. An absent file, a read/parse
# error, or unrecognized content is treated as `enabled` (pass) so current
# workflows are never broken. The one non-fail-open case is by design: once the
# file explicitly says `disabled`, a matched commit/push verb DOES block.
#
# What a `disabled` policy blocks (all robust to global options between `git` and
# the verb: -C <dir>, -c <k=v>, --git-dir=..., --work-tree=...; to the verb at any
# command position after ; && || | newline; to wrappers eval/sh -c/bash -c whose
# inner string carries the verb; and to env-prefixed forms VAR=val git commit):
#   commit-creating / push git verbs: commit, commit-tree, cherry-pick, revert,
#   am, rebase, merge, push, fast-import.
# Plus best-effort gh push-like escalations: `gh release create`, `gh pr merge`,
# and `gh api` with a POST method to a git-data/refs endpoint (uncertain gh api
# POSTs warn rather than block).
#
# Residuals (honest, out of robust reach for a shell-string heuristic): a git
# alias or a wrapper binary named other than git/gh, `command`/`sudo`/`xargs`
# prefixing git, an exotic unknown value-taking git global option before the verb,
# or an agent editing .git objects/refs by hand. These are noted in the report.
#
# Flag-write self-unblock check (owner ruling 2026-07-15): a blocked agent could
# defeat the policy above by simply writing `enabled` to the policy file itself
# via a Bash command (shell redirection, or a write-capable tool naming the file).
# This runs FIRST, unconditionally on every Bash call, INDEPENDENT of the current
# policy state (i.e. it fires even while the flag currently says `enabled`) --
# unlike the rest of this guard, which only fires when the flag says `disabled`.
# It BLOCKS whenever the command writes to the resolved policy-flag path AND
# GUARD_AGENT is not `meta`, with a DEFAULT-DENY on an empty/unresolved
# GUARD_AGENT (the flag is security-critical, so an unresolved identity must not
# be able to flip it -- same policy as guards/20-write-acl.sh's meta-only rule
# for the same path, which covers the Write/Edit/MultiEdit route; this is the
# Bash-tool counterpart, since the sanctioned writer -- skills/git/SKILL.md --
# itself writes via shell redirection, not a Write-class tool).

GUARD_MODE_GIT_POLICY=block

# _guard_git_policy_state: echo "disabled" only when the policy file exists, reads
# cleanly, and says disabled; otherwise "enabled" (fail-open superset covering
# absent file, read error, empty, and garbage content).
_guard_git_policy_state() {
  local pf state
  pf="${SUPERCLAUDE_GIT_POLICY_FILE:-$HOME/.claude/config/git-policy}"
  [ -f "$pf" ] || { printf 'enabled'; return 0; }
  state=$(tr -d '[:space:]' <"$pf" 2>/dev/null | tr '[:upper:]' '[:lower:]') \
    || { printf 'enabled'; return 0; }
  case "$state" in
    disabled) printf 'disabled' ;;
    *)        printf 'enabled' ;;
  esac
}

# _guard_git_policy_hits_git <cmd>: return 0 when the command creates a commit or
# performs a push, robust to the four tolerated global-option forms, compound
# commands, shell wrappers, and env prefixes; return 1 otherwise.
#
# Method: normalize by translating shell command boundaries (separators, quotes,
# grouping, backtick, whitespace controls) to newlines, then match, per line, a
# segment that STARTS with an optional env prefix, the literal `git`, tolerated
# global options, then a mutation verb as a whole token. Anchoring at segment
# start avoids false-blocking a benign command that merely names a git verb later
# in a word (e.g. a --grep value); the boundary translation is what surfaces the
# verb to segment start inside `cd X && git commit`, `bash -c "git commit"`, and
# `VAR=v git commit`.
_guard_git_policy_hits_git() {
  local cmd="$1" seps qnorm git_re
  local envprefix='([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*'
  local globals='([[:space:]]+((-C|-c|--git-dir|--work-tree|--namespace|--super-prefix)([[:space:]]+|=)[^[:space:]]+|--[A-Za-z][A-Za-z0-9-]*|-[pP]))*'
  local verb='(commit-tree|commit|cherry-pick|revert|rebase|merge|push|am|fast-import)'
  git_re='^[[:space:]]*'"$envprefix"'git'"$globals"'[[:space:]]+'"$verb"'([[:space:]]|$)'

  # Boundary set: ; & | ( ) { } " ' backtick newline tab (octal-escaped so no
  # literal quote/backtick glyph is typed). tr maps each to newline (GNU tr
  # extends the single-char replacement set to SET1 length).
  seps=$';&|(){}\042\047\140\n\t'
  qnorm=$(printf '%s' "$cmd" | tr "$seps" '\n')
  printf '%s\n' "$qnorm" | grep -Eq "$git_re"
}

# _guard_git_policy_gh <cmd>: echo "block" | "warn" | "" for gh push-like
# escalations. Segments are split on shell separators only (not quotes) so a
# `gh api` invocation keeps its method flag and URL together for the POST check.
_guard_git_policy_gh() {
  local cmd="$1" sepnorm
  sepnorm=$(printf '%s' "$cmd" | tr $';&|\n' '\n')

  if printf '%s\n' "$sepnorm" \
       | grep -Eq '^[[:space:]]*gh[[:space:]]+(release[[:space:]]+create|pr[[:space:]]+merge)([[:space:]]|$)'; then
    printf 'block'; return 0
  fi

  if printf '%s\n' "$sepnorm" | grep -Eq '^[[:space:]]*gh[[:space:]]+api([[:space:]]|$)'; then
    if printf '%s' "$cmd" | grep -Eiq -- '(-X|--method)[[:space:]]+POST|--method=POST'; then
      if printf '%s' "$cmd" | grep -Eq 'git/(refs|commits|trees|tags)|/refs/'; then
        printf 'block'; return 0
      fi
      printf 'warn'; return 0
    fi
  fi

  printf ''
}

# _guard_git_policy_path: echo the resolved policy-flag file path. Same SOT
# resolution as the local var inside _guard_git_policy_state above, pulled out
# as its own one-liner so the flag-write check below can reuse it without
# touching that already-tested function.
_guard_git_policy_path() {
  printf '%s' "${SUPERCLAUDE_GIT_POLICY_FILE:-$HOME/.claude/config/git-policy}"
}

# _guard_git_policy_escape_re <str>: escape ERE metacharacters in <str> so it can
# be embedded literally inside a grep -E pattern (the flag path is config/env
# data, not a literal under the guard author's control).
_guard_git_policy_escape_re() {
  printf '%s' "$1" | sed -e 's/[.[\*^$()+{}?|\\]/\\&/g'
}

# _guard_git_policy_hits_flagwrite <cmd>: return 0 (true) when <cmd> writes to
# the resolved policy-flag path by ANY of: shell redirection (>, >>), a
# write-capable tool naming the path (tee, cp, mv, install, ln, truncate),
# `dd of=<path>`, or `sed -i` / `sed --in-place` targeting the path. Matches the
# raw resolved path and, when it is rooted at $HOME, its ~/-shorthand form too.
#
# Method: quotes are stripped and other shell separators (; & | ( ) { } newline
# tab) are newline-split, same normalization idiom as _guard_git_policy_hits_git
# above, so each command SEGMENT is checked independently -- a write signal in
# one chained command never pairs with an unrelated path mention in another.
# Within a segment, presence (not strict token adjacency) of a write signal
# alongside the exact flag path is treated as a hit: this is deliberately
# over-inclusive (e.g. `cp <flag> /tmp/backup`, which only READS the flag, also
# matches) because for this one security-critical file a false block is the
# safe failure mode, and strict adjacency matching over `.*` gaps is fragile
# (shared-delimiter self-consumption bugs) for negligible precision gain.
_guard_git_policy_hits_flagwrite() {
  local cmd="$1" flagpath tilde path_re seps norm line
  local generic_re dd_word_re dd_of_re sed_word_re sed_flag_re

  flagpath=$(_guard_git_policy_path)
  path_re="($(_guard_git_policy_escape_re "$flagpath")"
  case "$flagpath" in
    "$HOME"/*)
      tilde="~/${flagpath#"$HOME"/}"
      path_re="$path_re|$(_guard_git_policy_escape_re "$tilde")"
      ;;
  esac
  path_re="$path_re)"

  generic_re='([0-9]*>>?)|((^|[[:space:]])(tee|cp|mv|install|ln|truncate)([[:space:]]|$))'
  dd_word_re='(^|[[:space:]])dd([[:space:]]|$)'
  dd_of_re='of='
  sed_word_re='(^|[[:space:]])sed([[:space:]]|$)'
  sed_flag_re='(^|[[:space:]])(-[A-Za-z]*i[A-Za-z]*|--in-place[^[:space:]]*)([[:space:]]|$)'

  seps=$';&|(){}\n\t'
  norm=$(printf '%s' "$cmd" | tr -d '\042\047\140' | tr "$seps" '\n')

  while IFS= read -r line; do
    [ -n "$line" ] || continue
    printf '%s' "$line" | grep -Eq "$path_re" || continue

    if printf '%s' "$line" | grep -Eq "$generic_re"; then
      return 0
    fi
    if printf '%s' "$line" | grep -Eq "$dd_word_re" \
       && printf '%s' "$line" | grep -Eq "$dd_of_re"; then
      return 0
    fi
    if printf '%s' "$line" | grep -Eq "$sed_word_re" \
       && printf '%s' "$line" | grep -Eq "$sed_flag_re"; then
      return 0
    fi
  done <<EOF
$norm
EOF
  return 1
}

guard_git_policy() {
  [ "${GUARD_TOOL:-}" = "Bash" ] || return 0

  local cmd
  cmd=$(guard_command)
  [ -n "$cmd" ] || return 0

  # Flag-write self-unblock check -- see header comment. Runs before the
  # disabled-state gate below and regardless of it.
  if _guard_git_policy_hits_flagwrite "$cmd" && [ "${GUARD_AGENT:-}" != "meta" ]; then
    guard_block "the /git policy flag is meta-only; use the /git skill as meta or ask the owner (SOT guards/26-git-policy.sh)"
    return 0
  fi

  [ "$(_guard_git_policy_state)" = "disabled" ] || return 0

  local gh
  if _guard_git_policy_hits_git "$cmd"; then
    guard_block "git is disabled by /git false; commit and push are blocked. Ask the owner to run /git true (rules: owner manages git manually)."
    return 0
  fi

  gh=$(_guard_git_policy_gh "$cmd")
  case "$gh" in
    block)
      guard_block "git is disabled by /git false; a gh push-like escalation (release create, pr merge, or a refs/commits write) is blocked. Ask the owner to run /git true (rules: owner manages git manually)." ;;
    warn)
      guard_warn "gh api with a POST method while git is disabled by /git false; if it writes refs or commits it is disallowed. Confirm with the owner, or ask the owner to run /git true." ;;
  esac
  return 0
}
