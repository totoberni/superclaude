# Guard: 26-git-policy: mechanical enforcement of the /git true|false policy.
#
# Owner ruling (2026-07-15): `/git false` disables commit of ANY kind and push of
# ANY kind; agents ask the owner to run `/git true`. This guard replaces the old
# per-op /commit and /push toggles for the commit/push case.
#
# BEST-EFFORT, NOT A BOUNDARY: this is a best-effort shell-string heuristic, NOT a
# shell parser and NOT a security boundary. Its purpose is to stop an agent's
# HABITUAL commit/push and self-unblock attempts, not to defeat a determined
# adversary deliberately crafting an evasion. A same-uid agent can still position
# the verb or the write target via constructs this heuristic does not model
# (command substitution, interpreter-driven git, eval, IFS / whitespace-valued
# variables, and in general arbitrary shell grammar). The real controls are the
# owner's manual review of commits and not granting agents push credentials;
# filesystem-level ownership would be the only complete mechanism. The lists below
# enumerate the SPECIFIC vectors that are tested-and-blocked (each with a bite-test
# and a seal-battery case) and the known residual CLASSES. They are NOT a
# completeness claim: a spelling not listed is a residual, not a promise.
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
# Tested-and-blocked commit/push vectors when the policy is `disabled` (each has a
# bite-test in tests/test-26-git-policy.sh and a seal-battery case):
#   - the mutation verbs: commit, commit-tree, cherry-pick, revert, am, rebase,
#     merge, push, fast-import
#   - global options between `git` and the verb: -C <dir>, -c <k=v>, --git-dir=,
#     --work-tree=, --namespace, --super-prefix
#   - the verb at a NON-leading position after a ; && || | or newline separator
#   - wrappers whose inner string carries a LITERAL verb: bash -c / sh -c / eval "..."
#   - env-prefix forms: VAR=val git commit
#   - a QUOTED or BACKSLASH-ESCAPED verb or word-git: git "commit", git com"m"it,
#     git ""commit, \git commit, g\it commit
#   - a TAB (or multi-space) between git and the verb: git<TAB>commit
#   - a BACKSLASH-NEWLINE line continuation: git \<LF>commit
#   - a LEADING REDIRECTION before git: >f git commit, 2>f git push, &>f git commit,
#     FOO=1 >f git commit
#   - an inline alias-to-mutation-verb definition: -c alias.<name>=<verb>
#   - gh push-like escalations: gh release create, gh pr merge, and gh api with a
#     POST to a git-data/refs endpoint (uncertain gh api POSTs warn, not block)
# This is an enumeration of what is tested, NOT a claim of coverage over "any"
# spelling; anything outside it is a residual (below), not a guarantee.
#
# Residuals NOT caught (honest, inherent to a shell-string heuristic): the verb or
# the word git produced at RUNTIME by command substitution (`git $(echo commit)`),
# a shell variable (`v=commit; git $v`), a whitespace-valued variable standing in
# for the separator (`git${IFS}commit`, whose value is not a literal in the string),
# or `eval`; an INTERPRETER driving git
# (`python3 -c "import subprocess; subprocess.run(['git','commit'])"`, or perl/node/
# ruby); a wrapper binary named other than git/gh, or `command`/`sudo`/`xargs`
# prefixing git; a PRE-EXISTING ~/.gitconfig alias (not inline via -c); an exotic
# unknown value-taking git global option before the verb; or an agent editing .git
# objects/refs by hand. A determined agent can commit via these; the owner's review
# is the backstop (see HONEST SCOPE above).
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
# guards/20-write-acl.sh's _wacl_norm_path), not raw-string matching. Tested-and-
# blocked flag-write spellings (each with a bite-test + seal-battery case): the
# resolved absolute path; its `~/` shorthand; `$HOME`/`${HOME}` forms; a `//` (or
# `.`/`..`) in the path; a `cd <dir> && <write> <relative>` sequence (relative
# target resolved against the cd'd dir); a trivial inline `VAR=<literal>; <write>
# $VAR` indirection and a basename assembled from split literal-valued vars
# (`b=git; ... ${b}-policy`); a TAB before the target; and a backslash-newline
# continuation before the target. HOME/`~` expansion and the VAR substitution are
# done by safe string substitution against the guard's own $HOME / the tracked
# literal (attacker strings are NEVER eval'd). The write signals covered are shell
# redirection (>, >>), tee/cp/mv/install/ln/truncate, dd of=, and sed -i.
#
# This enumerates what is tested; it is NOT complete and must not be read as such.
# A Bash-string heuristic cannot stop a Bash-capable, same-uid agent from writing an
# arbitrary file. Residual CLASSES NOT caught: a path or basename produced by command
# substitution (`echo enabled > $(echo $HOME)/.claude/config/git-policy`), by `eval`,
# or by a variable whose value itself comes from a subshell/prior call/environment;
# and any non-Bash interpreter writing the file. Two anti-DoS caps bound the cost so a
# padded command cannot hang the guard past the wired 10s hook timeout, at the price
# of two conservative dispositions: (1) the inline-VAR substitution processes at most
# a fixed cap of assignments, so a basename assembled from MORE than that many split
# assignments is not detected (passes); (2) once the flag basename IS present in the
# expanded command, a command with more segments than the cap is treated as a flag
# write and BLOCKED for a non-meta agent without scanning every segment -- so a
# very-large command that merely MENTIONS the basename without writing it is
# conservatively over-blocked for a non-meta agent. The Write/Edit/MultiEdit route is
# covered separately by guards/20-write-acl.sh (those tools name the path as
# structured data); the Bash route is inherently porous. True enforcement of
# "non-meta cannot flip the flag" would be filesystem-level (flag dir not writable by
# the agent uid); this check is a speed-bump plus the meta-only default-deny, not a
# boundary. Owner review is the backstop.

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
# performs a push, handling the tested global-option forms, compound commands,
# shell wrappers, env prefixes, leading redirections, and quote/backslash/tab/
# backslash-newline verb obfuscations (see the header's tested-and-blocked list);
# return 1 otherwise. Not a shell parser; residual classes are in the header.
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
  local cmd="$1" seps qnorm git_re assign redir prefix globals verb unq alias_re qdel
  local nl=$'\n'
  # Collapse backslash-newline line continuations FIRST, exactly as the shell does:
  # it elides a `\<LF>` and runs `git \<LF>commit` as `git commit`. Without this the
  # backslash-delete in the qdel form below leaves the LF, which the separator split
  # then breaks on, landing git and its verb on separate lines.
  cmd="${cmd//\\$nl/}"

  # Segment-start prefix tolerated before the literal `git`: zero or more atoms, each
  # either an env assignment (VAR=val) OR a redirection clause, in any order. bash
  # permits a redirection before the command word (`>/tmp/z git commit`, `2>f git
  # push`, `&>f ...`, `FOO=1 >f git commit`), which would otherwise defeat the `^...git`
  # anchor. The redir atom is: optional leading fd, optional `&`, one or more `<`/`>`,
  # optional `&`, optional fd, optional spaces, an optional target word, then space.
  assign='[A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+'
  redir='[0-9]*&?[<>]+&?[0-9]*[[:space:]]*[^[:space:]]*[[:space:]]+'
  prefix='('"$assign"'|'"$redir"')*'
  globals='([[:space:]]+((-C|-c|--git-dir|--work-tree|--namespace|--super-prefix)([[:space:]]+|=)[^[:space:]]+|--[A-Za-z][A-Za-z0-9-]*|-[pP]))*'
  verb='(commit-tree|commit|cherry-pick|revert|rebase|merge|push|am|fast-import)'
  git_re='^[[:space:]]*'"$prefix"'git'"$globals"'[[:space:]]+'"$verb"'([[:space:]]|$)'

  # Inline alias-to-mutation-verb definition: `git -c alias.<name>=<mutation-verb>`
  # defines a commit/push alias on the command line, then invokes it under a benign
  # token (e.g. `git -c alias.ci=commit ci -m x`). The `globals` pattern above
  # TOLERATES the `-c k=v` option, so it silently consumes the alias definition, and
  # the verb match never sees the aliased token. Defeat it at the source: while git
  # is disabled, defining an alias WHOSE VALUE is a mutation verb IS the
  # circumvention, so block on the definition itself, however the aliased token is
  # (or is not) later spelled. Conservative by design (defining a commit/push alias
  # while git is disabled is the circumvention): any inline `-c alias.*=<mutation-verb>`
  # is blocked. Quotes/backticks are stripped first so a quoted value
  # (`-c alias.ci="commit"`) is still caught.
  unq=$(printf '%s' "$cmd" | tr -d '\042\047\140')
  alias_re='(^|[[:space:]])-c[[:space:]]+alias\.[A-Za-z0-9._-]+='"$verb"'([^A-Za-z0-9-]|$)'
  if printf '%s' "$unq" | grep -Eq "$alias_re"; then
    return 0
  fi

  # Boundary set: ; & | ( ) { } " ' backtick newline (octal-escaped so no literal
  # quote/backtick glyph is typed). tr maps each to newline (GNU tr extends the
  # single-char replacement set to SET1 length). A literal TAB is deliberately NOT
  # in this set: tab is intra-command WHITESPACE (the shell runs `git<TAB>commit` as
  # `git commit`), so mapping it to a newline would split git from its verb and
  # defeat the block. The verb regex's own `[[:space:]]+` matches the preserved tab,
  # exactly as the gh-escalation path already relies on.
  seps=$';&|(){}\042\047\140\n'
  qnorm=$(printf '%s' "$cmd" | tr "$seps" '\n')
  if printf '%s\n' "$qnorm" | grep -Eq "$git_re"; then
    return 0
  fi

  # Second normalized form for quote / backslash verb obfuscation. The first form
  # NEWLINE-SPLITS on quotes, which surfaces a wrapped verb in `bash -c "git commit"`
  # but the SAME split defeats the block when the quote sits AROUND or BEFORE the
  # verb: `git "commit"`, `git com"m"it`, `git ""commit`, `\git commit`, `g\it commit`
  # all split git and the verb onto separate lines. So build a SECOND form that
  # DELETES quote chars and backslashes (rather than splitting on them), rejoining
  # `git "commit"` -> `git commit` and `\git` -> `git`; non-quote separators still
  # newline-split so unrelated segments never merge. Both forms feed the same anchored
  # git_re, which covers the quote/backslash/tab/backslash-newline verb obfuscations
  # tested in the suite. (A verb produced by a RUNTIME value -- `git $(echo commit)`,
  # `v=commit; git $v`, `git${IFS}commit` -- is a documented residual the header
  # lists; a shell-string heuristic cannot see a value the shell computes at runtime.)
  qdel=$(printf '%s' "$cmd" | tr -d '\042\047\140\134' | tr $';&|(){}\n' '\n')
  printf '%s\n' "$qdel" | grep -Eq "$git_re"
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
# Method: an O(1) fast-path first returns unless the flag BASENAME appears literally
# (see below; this is what keeps large commands cheap). Then `${HOME}`/`$HOME` and
# tracked inline `VAR=<literal>` assignments are expanded on the whole string, quotes
# /backticks/backslashes are stripped, and shell separators (; & | ( ) { } newline
# tab) are newline-split, so each command SEGMENT is checked independently -- a write
# signal in one chained command never pairs with an unrelated path mention in another.
# A `cd <dir>` segment updates a running cwd (it affects later segments in the same
# shell), so a `cd <flagdir> && echo x > <basename>` sequence resolves the relative
# target against the cd'd directory. Within a write-bearing segment, EVERY whitespace
# token is REALPATH-NORMALIZED (see _guard_git_policy_norm_target) and compared to the
# normalized flag path; a normalized flag-path token alongside a write signal is a
# hit. Deliberately over-inclusive (e.g. `cp <flag> /tmp/backup`, which only READS the
# flag, also matches) because for this security-critical file a false block is the
# safe failure mode. Comparing normalized targets (not raw strings) is what closes the
# STATICALLY spellable bypasses -- $HOME/${HOME}, ~/, //, ./.., cd+relative, and a
# simple `VAR=<literal>; > $VAR` indirection all resolve to the one real file. A path
# built by command substitution `$(...)`, `eval`, or a runtime-valued variable is the
# documented residual (see the header) and is NOT caught.
_guard_git_policy_hits_flagwrite() {
  local cmd="$1" flagpath flagbase seps norm line cwd="" c1 c2 crest
  local has_write clean tok ntok an av i acnt nl=$'\n' seg_cap=200 nseg
  local -a toks gp_names=() gp_vals=()

  # Collapse backslash-newline line continuations FIRST (same reason as in
  # _guard_git_policy_hits_git): the shell runs `printf enabled > \<LF><flag>` as one
  # command; without collapsing, the backslash-delete below leaves the LF, which the
  # separator split breaks on, landing the `>` write signal and the flag token on
  # separate lines so the write is never associated with the flag path.
  cmd="${cmd//\\$nl/}"

  flagpath=$(_guard_git_policy_norm_target "$(_guard_git_policy_path)")
  [ -n "$flagpath" ] || return 1

  flagbase=${flagpath##*/}
  [ -n "$flagbase" ] || return 1

  # Expand `${HOME}` / `$HOME` on the whole string FIRST, by safe substitution
  # against the guard's own $HOME. This must precede the separator split below:
  # `{` and `}` are in the separator set (shell grouping `{ ...; }`), so a `${HOME}`
  # left intact would be torn apart at its braces and never normalize. Bare `~/`
  # has no braces and survives the split (the per-token norm expands it).
  cmd="${cmd//\$\{HOME\}/$HOME}"
  cmd="${cmd//\$HOME/$HOME}"

  # Inline `VAR=value` assignment tracking + whole-string expansion (closes the
  # trivial variable-indirection flag write: `f=<flagpath>; echo x > $f`, or
  # `f=<basename>; ... /$f`). This runs BEFORE the brace-containing split for the
  # same reason as the HOME expansion: `${f}` would otherwise be torn at its braces.
  # Pass 1 collects assignments by splitting on statement separators that do NOT
  # include braces (so `${f}` survives to be recorded/looked up); the leading token
  # of each statement, if a bare `NAME=value`, is recorded (value already
  # $HOME-expanded above). Pass 2 substitutes every tracked `$VAR` / `${VAR}` on the
  # whole string. A value produced by command substitution `$(...)`, `eval`, or a
  # subshell is NOT tracked -- that is the documented residual.
  local asrc aline atok
  asrc=$(printf '%s' "$cmd" | tr -d '\042\047\140\134' | tr $';&|\n' '\n')
  while IFS= read -r aline; do
    read -r atok _ <<<"$aline"
    case "$atok" in
      [A-Za-z_]*=*)
        an="${atok%%=*}"; av="${atok#*=}"
        case "$an" in
          *[^A-Za-z0-9_]*) : ;;
          *) gp_names+=("$an"); gp_vals+=("$av") ;;
        esac
        ;;
    esac
  done <<EOF
$asrc
EOF
  acnt=${#gp_names[@]}
  # Cap the substitution loop: each iteration re-scans the whole command string, so a
  # command padded with thousands of assignments would be O(n^2) and hang the guard
  # past the wired 10s hook timeout. A legitimate command has very few assignments;
  # process at most seg_cap. A basename assembled from more than seg_cap assignments
  # is a documented residual (see the header).
  [ "$acnt" -gt "$seg_cap" ] && acnt="$seg_cap"
  i=0
  while [ "$i" -lt "$acnt" ]; do
    an="${gp_names[$i]}"; av="${gp_vals[$i]}"
    cmd="${cmd//\$\{$an\}/$av}"
    cmd="${cmd//\$$an/$av}"
    i=$((i + 1))
  done

  # O(1) short-circuit (perf), applied AFTER the $HOME + VAR expansion above (NOT on
  # the raw command): a flag write MUST name the flag basename contiguously somewhere
  # in the EXPANDED command (the last path component is not itself spellable via
  # $HOME/cd/// and, once a `VAR=<literal>` assignment is substituted, the pieces are
  # joined). If the basename is still absent after expansion, this cannot be a flag
  # write, so skip the whole per-segment normalization below. Running the grep on the
  # RAW command instead would miss a basename assembled from split literal-valued vars
  # (`b=git; ... ${b}-policy`); running it here catches that class while staying flat-
  # cost (one grep on the expanded string; the assignment-collection loop above is
  # cheap and the expensive per-segment loop is still skipped for the common case).
  printf '%s' "$cmd" | grep -qF -- "$flagbase" || return 1

  # A literal TAB is NOT in this separator set (nor in the asrc set above): tab is
  # intra-segment WHITESPACE, so a `tee<TAB><flag>` or `>\t<flag>` must stay in one
  # segment; the tokenizer below folds tab to a space instead. Backslashes are
  # DELETED alongside quotes/backticks so an escaped write target (`\$f`,
  # `git-poli\cy`) normalizes like its bare form.
  seps=$';&|(){}\n'
  norm=$(printf '%s' "$cmd" | tr -d '\042\047\140\134' | tr "$seps" '\n')

  # Segment cap (anti-DoS): we only reach here when the flag basename is present in
  # the EXPANDED command (fast-path above), so this is already an unusual command. The
  # per-segment loop below runs several grep forks PER segment, so a basename-present
  # command padded to thousands of segments would exceed the wired 10s hook timeout. A
  # legitimate flag write has well under ~20 segments; if a basename-present command
  # has more than seg_cap, do NOT scan them all (that is the hang) -- treat it as a
  # flag write and BLOCK (for a non-meta agent; meta is exempt at the caller) rather
  # than hanging OR letting a buried write slip. Documented residual: a >seg_cap-segment
  # command that merely MENTIONS the basename without writing it is conservatively
  # blocked for a non-meta agent.
  nseg=$(printf '%s' "$norm" | grep -c '')
  if [ "$nseg" -gt "$seg_cap" ]; then
    return 0
  fi

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

    # Tokenize: turn redirection glyphs (and any intra-segment TAB) into spaces so
    # `>flag` / `> flag` / `>\tflag` all surface `flag` as its own token, then read
    # into an array on space+tab (read -ra never globs, so a `*`/`?` in the segment
    # cannot expand against the filesystem). Splitting IFS on tab as well as space
    # means a leading-tab target can never survive as a non-absolute token.
    clean=$(printf '%s' "$line" | tr '<>\t' '   ')
    IFS=$' \t' read -ra toks <<<"$clean"
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
