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
# Also blocks an INLINE alias-to-mutation-verb definition, i.e. any
# `-c alias.<name>=<mutation-verb>` on the command line: defining a commit/push
# alias while git is disabled is itself the circumvention (the aliased token then
# runs the verb under a benign name), so the definition is blocked regardless of
# how the aliased token is later spelled.
# Plus best-effort gh push-like escalations: `gh release create`, `gh pr merge`,
# and `gh api` with a POST method to a git-data/refs endpoint (uncertain gh api
# POSTs warn rather than block).
#
# Residuals (honest, out of robust reach for a shell-string heuristic): a
# PRE-EXISTING gitconfig alias (defined in ~/.gitconfig, not inline via -c) or a
# wrapper binary named other than git/gh, `command`/`sudo`/`xargs` prefixing git,
# an exotic unknown value-taking git global option before the verb, an agent
# editing .git objects/refs by hand, or an INTERPRETER driving git (e.g.
# `python3 -c "import subprocess; subprocess.run(['git','commit'])"`, or the same
# via perl/node/ruby) -- an interpreter builds the argv internally, so no
# shell-string heuristic can see the verb. These are noted in the report.
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
#
# The path comparison is done on REALPATH-NORMALIZED targets (mirroring
# guards/20-write-acl.sh's _wacl_norm_path), not raw-string matching, so all
# spellings that resolve to the same real file are caught equally: the resolved
# absolute path, its `~/` shorthand, `$HOME`/`${HOME}` forms, a `//` (or `.`/`..`)
# in the path, and a `cd <dir> && <write> <relative>` sequence where the relative
# target resolves against the cd'd directory. HOME/`~` expansion is done by safe
# string substitution against the guard's own $HOME (attacker-supplied strings are
# NEVER eval'd).

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
  local cmd="$1" seps qnorm git_re unq alias_re
  local envprefix='([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*'
  local globals='([[:space:]]+((-C|-c|--git-dir|--work-tree|--namespace|--super-prefix)([[:space:]]+|=)[^[:space:]]+|--[A-Za-z][A-Za-z0-9-]*|-[pP]))*'
  local verb='(commit-tree|commit|cherry-pick|revert|rebase|merge|push|am|fast-import)'
  git_re='^[[:space:]]*'"$envprefix"'git'"$globals"'[[:space:]]+'"$verb"'([[:space:]]|$)'

  # Inline alias-to-mutation-verb definition: `git -c alias.<name>=<mutation-verb>`
  # defines a commit/push alias on the command line, then invokes it under a benign
  # token (e.g. `git -c alias.ci=commit ci -m x`). The `globals` pattern above
  # TOLERATES the `-c k=v` option, so it silently consumes the alias definition, and
  # the verb match never sees the aliased token. Defeat it at the source: while git
  # is disabled, defining an alias WHOSE VALUE is a mutation verb IS the
  # circumvention, so block on the definition itself, however the aliased token is
  # (or is not) later spelled. Conservative by design per the owner "must be robust"
  # ruling: any inline `-c alias.*=<mutation-verb>` is blocked. Quotes/backticks are
  # stripped first so a quoted value (`-c alias.ci="commit"`) is still caught.
  unq=$(printf '%s' "$cmd" | tr -d '\042\047\140')
  alias_re='(^|[[:space:]])-c[[:space:]]+alias\.[A-Za-z0-9._-]+='"$verb"'([^A-Za-z0-9-]|$)'
  if printf '%s' "$unq" | grep -Eq "$alias_re"; then
    return 0
  fi

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

# _guard_git_policy_norm_target <token> [cwd]: echo the REALPATH-NORMALIZED
# absolute path for a write-target token, or "" when it cannot be resolved to an
# absolute path. Steps: strip a leading `of=` (dd form) so its target normalizes
# like a bare path; expand `${HOME}` / `$HOME` / a leading `~` against the guard's
# own $HOME by SAFE STRING SUBSTITUTION (attacker input is never eval'd); resolve a
# relative token against <cwd> when one is known (from a preceding cd), else give
# up (relative with unknown cwd is unresolvable); then `realpath -m` to collapse
# `//`, `.`, `..` without touching the filesystem. Mirrors 20-write-acl's
# _wacl_norm_path so the Bash route and the Write route normalize identically.
_guard_git_policy_norm_target() {
  local t="${1:-}" cwd="${2:-}"
  [ -n "$t" ] || { printf ''; return 0; }
  t="${t#of=}"
  t="${t//\$\{HOME\}/$HOME}"
  t="${t//\$HOME/$HOME}"
  case "$t" in
    "~")   t="$HOME" ;;
    "~/"*) t="$HOME/${t#\~/}" ;;
  esac
  case "$t" in
    /*) : ;;
    *)  if [ -n "$cwd" ]; then t="$cwd/$t"; else printf ''; return 0; fi ;;
  esac
  if command -v realpath >/dev/null 2>&1; then
    realpath -m -- "$t" 2>/dev/null || printf '%s' "$t"
  else
    printf '%s' "$t"
  fi
}

# _guard_git_policy_hits_flagwrite <cmd>: return 0 (true) when <cmd> writes to the
# resolved policy-flag path by ANY of: shell redirection (>, >>), a write-capable
# tool naming the path (tee, cp, mv, install, ln, truncate), `dd of=<path>`, or
# `sed -i` / `sed --in-place` targeting the path.
#
# Method: quotes/backticks are stripped and shell separators (; & | ( ) { } newline
# tab) are newline-split, so each command SEGMENT is checked independently -- a
# write signal in one chained command never pairs with an unrelated path mention in
# another. A `cd <dir>` segment updates a running cwd (it affects later segments in
# the same shell), so a `cd <flagdir> && echo x > <basename>` sequence resolves the
# relative target against the cd'd directory. Within a write-bearing segment, EVERY
# whitespace token is REALPATH-NORMALIZED (see _guard_git_policy_norm_target) and
# compared to the normalized flag path; presence of a normalized flag-path token
# alongside a write signal is a hit. This is deliberately over-inclusive (e.g.
# `cp <flag> /tmp/backup`, which only READS the flag, also matches) because for this
# one security-critical file a false block is the safe failure mode. Comparing
# normalized targets (not raw strings) is what closes the path-spelling bypasses:
# $HOME/${HOME}, ~/, //, ./.., and cd+relative all resolve to the one real file.
_guard_git_policy_hits_flagwrite() {
  local cmd="$1" flagpath seps norm line cwd="" c1 c2 crest
  local has_write clean tok ntok
  local -a toks

  flagpath=$(_guard_git_policy_norm_target "$(_guard_git_policy_path)")
  [ -n "$flagpath" ] || return 1

  # Expand `${HOME}` / `$HOME` on the whole string FIRST, by safe substitution
  # against the guard's own $HOME. This must precede the separator split below:
  # `{` and `}` are in the separator set (shell grouping `{ ...; }`), so a `${HOME}`
  # left intact would be torn apart at its braces and never normalize. Bare `~/`
  # has no braces and survives the split (the per-token norm expands it).
  cmd="${cmd//\$\{HOME\}/$HOME}"
  cmd="${cmd//\$HOME/$HOME}"

  seps=$';&|(){}\n\t'
  norm=$(printf '%s' "$cmd" | tr -d '\042\047\140' | tr "$seps" '\n')

  while IFS= read -r line; do
    [ -n "$line" ] || continue

    # cd context: a `cd <dir>` shifts the cwd for every later segment. No arg or
    # `~` -> $HOME; otherwise normalize <dir> against the current cwd.
    read -r c1 c2 crest <<<"$line"
    if [ "$c1" = "cd" ]; then
      if [ -z "$c2" ] || [ "$c2" = "~" ]; then
        cwd="$HOME"
      else
        local d
        d=$(_guard_git_policy_norm_target "$c2" "$cwd")
        [ -n "$d" ] && cwd="$d"
      fi
    fi

    # Write-signal detection on the raw segment (before redirection glyphs are
    # neutralized for tokenizing).
    has_write=1
    if   printf '%s' "$line" | grep -Eq '>>?'; then :
    elif printf '%s' "$line" | grep -Eq '(^|[[:space:]])(tee|cp|mv|install|ln|truncate)([[:space:]]|$)'; then :
    elif printf '%s' "$line" | grep -Eq '(^|[[:space:]])dd([[:space:]]|$)' \
         && printf '%s' "$line" | grep -Eq 'of='; then :
    elif printf '%s' "$line" | grep -Eq '(^|[[:space:]])sed([[:space:]]|$)' \
         && printf '%s' "$line" | grep -Eq '(^|[[:space:]])(-[A-Za-z]*i[A-Za-z]*|--in-place[^[:space:]]*)([[:space:]]|$)'; then :
    else
      has_write=0
    fi
    [ "$has_write" -eq 1 ] || continue

    # Tokenize: turn redirection glyphs into spaces so `>flag` / `> flag` both
    # surface `flag` as its own token, then read into an array (read -ra never
    # globs, so a `*`/`?` in the segment cannot expand against the filesystem).
    clean=$(printf '%s' "$line" | tr '<>' '  ')
    IFS=' ' read -ra toks <<<"$clean"
    for tok in "${toks[@]}"; do
      ntok=$(_guard_git_policy_norm_target "$tok" "$cwd")
      [ -n "$ntok" ] || continue
      if [ "$ntok" = "$flagpath" ]; then
        return 0
      fi
    done
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
