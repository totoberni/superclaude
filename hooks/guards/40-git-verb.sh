# ~/.claude/hooks/guards/40-git-verb.sh: Family 4 git-verb gate (PreToolUse on Bash).
#
# Advisory home: design/enforcement-gap-ledger.md Family 4 (#13-#16). Fires only when
# GUARD_TOOL == Bash. Every check below is a HEURISTIC regex over the raw command
# string, not a git parser: false positives are expected and acceptable because every
# finding here is a guard_warn, NEVER guard_block. A hook cannot tell "the user
# explicitly instructed this" from "the agent decided on its own", so the whole family
# defaults to warn (the safe superset per the ledger note on #13); blocking outright
# would false-positive on legitimate, explicitly-instructed ops.
#
# Checks:
#   #14 -C PATHSPEC   : `git -C <dir> ... <pathspec>` repeats <dir> inside a later
#                        pathspec argument in the same command (rules/20 Git with -C:
#                        pathspecs after -C are already repo-root-relative).
#   #16 BANG MANGLING : a command string contains the shell history-expansion
#                        character AND targets memory_db.py/comms_db.py or writes a
#                        file via redirection (rules/20 Shell Mangling in Content/DB
#                        Writes: the bash tool can corrupt that character before it
#                        reaches the target; use the Write tool instead). The character
#                        itself is built at RUNTIME inside the helper below, never typed
#                        as a literal glyph in this file.
#   #13 BRANCH OP     : `git checkout -b`, `git switch -c`, `git branch <name>`
#                        (creation form, not list/delete), `git merge` (rules/00 Git
#                        Discipline: never create/switch/merge a branch without
#                        explicit instruction; a hook cannot verify "explicit", so this
#                        warns rather than blocks).
#   #15 WORKTREE RACE : a checkout -b / switch -c against a repo dir already touched by
#                        this same check within the race window warns about a possible
#                        parallel-orch checkout race (rules/12 Multi-Orch, rules/20
#                        Worktree Hygiene: same-repo parallel orchs need separate
#                        worktrees). Best-effort, a plain marker file under $TMPDIR.

GUARD_MODE_GIT_VERB=warn

# _guard_git_verb_dash_c_dir <cmd>: echo the first `-C <dir>` argument value, or "".
_guard_git_verb_dash_c_dir() {
  local cmd="$1"
  if [[ "$cmd" =~ -C[[:space:]]+([^[:space:]]+) ]]; then
    printf '%s' "${BASH_REMATCH[1]}"
  fi
}

# #14: -C <dir> repeated inside a later pathspec argument in the SAME command. A
# fixed-string occurrence count >= 2 means the dir string shows up again after its
# own -C slot (the exact anti-pattern from rules/20: `git -C /repo ... /repo/file`).
_guard_git_verb_pathspec() {
  local cmd="$1" dir hits
  dir=$(_guard_git_verb_dash_c_dir "$cmd")
  [ -n "$dir" ] || return 0
  hits=$(printf '%s' "$cmd" | grep -Fo "$dir" | wc -l)
  [ "$hits" -ge 2 ] || return 0
  guard_warn "git -C $dir repeats '$dir' inside a pathspec argument; pathspecs after -C are already repo-root-relative (rules/20 Git with -C)"
}

# #16: the shell history-expansion character (built at runtime, never a literal glyph
# in this file) alongside a memory_db.py/comms_db.py target or a file-write redirect.
_guard_git_verb_bang() {
  local cmd="$1" bang
  bang=$(printf '\41')
  printf '%s' "$cmd" | grep -qF "$bang" || return 0
  printf '%s' "$cmd" | grep -Eq 'memory_db\.py|comms_db\.py|(>>?|<<-?)[[:space:]]*.?[A-Za-z0-9_./-]+' \
    || return 0
  guard_warn "command carries the shell history-expansion character while targeting memory_db.py/comms_db.py or a file write; the bash tool can corrupt it before it lands, use the Write tool instead (rules/20 Shell Mangling in Content/DB Writes)"
}

# #15: best-effort per-repo lock under $TMPDIR. A second checkout -b/switch -c against
# the same repo dir inside the race window warns about a possible parallel-orch race.
_guard_git_verb_race() {
  local cmd="$1" dir key marker now mtime age window
  window=30
  dir=$(_guard_git_verb_dash_c_dir "$cmd")
  [ -n "$dir" ] || dir="no-explicit--C-dir"
  key=$(printf '%s' "$dir" | tr -c '[:alnum:]' '_')
  marker="${TMPDIR:-/tmp}/guard-git-verb-worktree-${key}.marker"
  now=$(date +%s 2>/dev/null || echo 0)
  if [ -f "$marker" ]; then
    mtime=$(stat -c %Y "$marker" 2>/dev/null || stat -f %m "$marker" 2>/dev/null || echo 0)
    age=$((now - mtime))
    if [ "$age" -ge 0 ] && [ "$age" -lt "$window" ]; then
      guard_warn "another checkout -b/switch -c against $dir ran ${age}s ago; confirm a dedicated worktree rather than a same-repo parallel checkout race (rules/12 Multi-Orch, rules/20 Worktree Hygiene)"
    fi
  fi
  : >"$marker" 2>/dev/null || true
}

# #13: branch create / switch / merge. Independent ifs (not elif) so a compound
# command matching more than one verb still gets every applicable warning.
_guard_git_verb_branch() {
  local cmd="$1"
  # An optional `-C <dir>` may sit between `git` and the verb (rules/20 Git with -C is
  # the documented form); match with or without it so `git -C <dir> checkout -b ...`
  # is recognized the same as `git checkout -b ...`.
  local pre='git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+'

  if printf '%s' "$cmd" | grep -Eq "${pre}checkout[[:space:]]+-b([[:space:]]|\$)"; then
    guard_warn "git checkout -b creates a branch; confirm this was explicitly instructed, never automatic (rules/00 Git Discipline)"
    _guard_git_verb_race "$cmd"
  fi
  if printf '%s' "$cmd" | grep -Eq "${pre}switch[[:space:]]+-c([[:space:]]|\$)"; then
    guard_warn "git switch -c creates a branch; confirm this was explicitly instructed, never automatic (rules/00 Git Discipline)"
    _guard_git_verb_race "$cmd"
  fi
  if printf '%s' "$cmd" | grep -Eq "${pre}branch[[:space:]]+[^-[:space:]]"; then
    guard_warn "git branch <name> creates a branch; confirm this was explicitly instructed, never automatic (rules/00 Git Discipline)"
  fi
  if printf '%s' "$cmd" | grep -Eq "${pre}merge([[:space:]]|\$)"; then
    guard_warn "git merge changes history; confirm this was explicitly instructed (rules/00 Git Discipline)"
  fi
}

guard_git_verb() {
  [ "${GUARD_TOOL:-}" = "Bash" ] || return 0
  local cmd
  cmd=$(guard_command)
  [ -n "$cmd" ] || return 0

  _guard_git_verb_pathspec "$cmd"
  _guard_git_verb_bang "$cmd"
  _guard_git_verb_branch "$cmd"
  return 0
}
