#!/usr/bin/env bash
# Bite-test for 26-git-policy (PHASE2-CONTRACT sec 6). ISOLATED unit test: sources
# lib-guard.sh + the guard directly and drives it via run_guard in a fresh subshell
# per case. No repo mutation, no network. Commands are only INSPECTED by the guard
# (the guard reads the command string; it never executes it), so the git/gh verbs
# in the cases are never run.
#
# The policy state is pointed at a TEST file under $TMPDIR via
# SUPERCLAUDE_GIT_POLICY_FILE so the real ~/.claude/config/git-policy is untouched.
# Any shell history-expansion glyph in test data would be built at runtime with
# printf; none is needed here.
#
# Cases (policy=disabled, each must BLOCK -> exit 2):
#   (a) git commit -m x
#   (b) git -C /tmp/r commit -m x            (-C global option tolerated)
#   (c) cd /tmp/r && git commit -m x         (chained compound command)
#   (d) git push origin main
#   (e) git commit-tree <oid>
#   (f) bash -c "git commit -m x"            (wrapper)
#   (g) GIT_AUTHOR_NAME=x git commit -m y    (env prefix)
#   (h) gh release create v1                 (gh push-like escalation)
# Cases (policy=enabled, must PASS -> exit 0):
#   (i) git commit -m x
#   (j) git status
# Cases (policy=disabled, read-only, must PASS -> exit 0):
#   (k) git status
#   (l) git log
# Kill-switch (policy=disabled + a commit command):
#   (m) SUPERCLAUDE_GUARDS=off -> exit 0, total silence
#
# Flag-write self-unblock cases (independent of policy state; SUPERCLAUDE_GIT_
# POLICY_FILE points at a fresh $TMPDIR target so the real flag is untouched):
#   (n) w-implementer, printf enabled > <flag>  via Bash -> block
#   (o) w-implementer, echo disabled >> <flag>              -> block
#   (p) w-implementer, sed -i s/x/y/ <flag>                 -> block
#   (q) meta,          printf enabled > <flag>              -> pass
#   (r) empty agent,   echo enabled > <flag>                -> block (default-deny)
#   (s) w-implementer, cat <flag>  (read-only)               -> pass
#
# B1 regression -- inline git alias to a mutation verb (policy=disabled). The
# `-c alias.<name>=<verb>` global defines a commit/push alias on the command line;
# defining it is the circumvention, so it must block however the aliased token is
# spelled. Benign aliases (to read-only verbs) and other `-c k=v` must NOT block:
#   (t) git -c alias.ci=commit ci -m x   -> block
#   (u) git -c alias.p=push p            -> block
#   (v) git -c alias.ci="commit" ci      -> block (quoted value)
#   (w) git -c core.editor=vim status    -> pass  (not an alias def)
#   (x) git -c alias.st=status st        -> pass  (alias to a read-only verb)
#
# B2 regression -- flag-write path SPELLINGS all resolve to the same real file and
# must block equally (worker), while meta and a different file pass. HOME is
# overridden to a $TMPDIR sandbox so the `$HOME`/`~` spellings never touch the real
# flag; the policy file is that sandbox's git-policy:
#   (y)  worker  echo enabled > $HOME/.claude/config/git-policy       -> block
#   (z)  worker  echo enabled > ${HOME}/.claude/config/git-policy     -> block
#   (aa) worker  echo enabled > ~/.claude/config/git-policy           -> block
#   (ab) worker  echo enabled > $HOME/.claude/config//git-policy      -> block (//)
#   (ac) worker  cd $HOME/.claude/config && echo enabled > git-policy -> block (cd+rel)
#   (ad) worker  printf enabled | tee $HOME/.claude/config/git-policy -> block (tee)
#   (ae) meta    echo enabled > $HOME/.claude/config/git-policy       -> pass
#   (af) worker  echo enabled > $HOME/.claude/config/other-file       -> pass (other file)
#   (ag) worker  cd /tmp && echo enabled > git-policy                 -> pass (cd elsewhere)
#
# B-a regression -- a verb (or the word git) held apart from `git` by QUOTING or
# BACKSLASH-ESCAPING must still block; benign/non-mutation forms must not:
#   (ah) git "commit"      (ai) git 'commit'     (aj) git com"m"it   -> block
#   (ak) \git commit       (al) g\it commit                          -> block
#   (am) git commit-graph write (non-mutation verb)                  -> pass
#
# B-b regression -- variable-indirection flag write must block; the command-
# substitution form is the documented residual and passes (HOME override as above):
#   (an) worker  f=<flagpath>; echo enabled > $f                     -> block
#   (ao) worker  f=<basename>; echo enabled > <flagdir>/$f           -> block
#   (ap) worker  f=<flagpath>; echo x > ${f}   (brace form)          -> block
#   (aq) worker  echo enabled > $(echo $HOME)/.../git-policy         -> pass (residual)
#   (ar) worker  f=<other-file>; echo x > $f                         -> pass (other file)
#
# B-d regression -- a literal TAB is intra-command whitespace, not a separator:
#   (as) git<TAB>commit   (at) git<TAB>push                          -> block
#   (au) git<TAB>status (benign)                                     -> pass
#   (av) worker  printf enabled >\t$HOME/.../git-policy              -> block
#   (aw) worker  printf x | tee\t$HOME/.../git-policy                -> block
#
# B-e regression -- a flag basename assembled from split literal-valued vars (the
# O(1) short-circuit runs AFTER $HOME + VAR expansion):
#   (ax) worker  b=git; > $HOME/.../${b}-policy                      -> block
#   (ay) worker  x=git; y=policy; > $HOME/.../${x}-${y}              -> block
#   (az) worker  b=git; > $HOME/.../${b}-other  (non-flag basename)  -> pass
#   (ba) git${IFS}commit (whitespace-var separator, disclosed residual) -> pass
#
# B-f regression -- a backslash-newline line continuation is elided by the shell:
#   (bb) git \<LF>commit   (bc) git \<LF>push                         -> block
#   (bd) git \<LF>status (benign)                                     -> pass
#   (be) worker  printf enabled > \<LF>$HOME/.../git-policy           -> block
#
# B-g regression -- a leading redirection before git must not defeat the anchor:
#   (bf) >f git commit  (bg) 2>f git push  (bh) &>f git commit        -> block
#   (bi) FOO=1 >f git commit                                          -> block
#   (bj) >f git status (read-only)   (bk) >f ls (no git)              -> pass
#
# B-h regression -- anti-DoS segment cap on the flag-write per-segment loop:
#   (bl) basename-present command padded beyond the cap                -> block
#   (bm) benign large command (no basename) beyond the cap             -> pass
#
# B-i regression -- a redirection only counts when the flag is its TARGET. A `2>&1`
# stderr dup merely CO-OCCURS with the flag path; it does not write it, so a pure read
# must pass. (bo)/(bp) are the control arm: the real write and the meta exemption must
# still behave, proving the target-scoping did not disarm the check:
#   (bn) worker  cat <flag> 2>&1   (read + stderr dup)                 -> pass
#   (bo) worker  printf enabled > <flag>                               -> block
#   (bp) meta    printf enabled > <flag>                               -> pass
#
# B-j regression -- agent identity resolution (guards/lib-guard.sh guard_init). The
# proc-tree walk returns "" when the hook runs detached from the claude process, which
# would default-deny META, the sanctioned writer. guard_init then falls back to the
# per-session agent marker (<timer_dir>/<session_id>.agent, written by
# hooks/modules/00-parse.sh). These cases must NOT set GUARD_AGENT after guard_init
# (run_ident_case does not), so guard_init's own resolution is what is under test;
# walk_to_agent is never defined here (hooks/lib.sh is not sourced), so the walk arm
# is empty exactly as it is live. SUPERCLAUDE_SESSION_TIMER_DIR points at a $TMPDIR
# fixture so no real session file is read:
#   (bq) marker "meta",         flag write -> pass  (fallback resolves meta)
#   (br) marker EMPTY,          flag write -> block (unresolved: default-deny kept)
#   (bs) marker "w-implementer", flag write -> block
#   (bt) no session_id in stdin, CLAUDE_CODE_SESSION_ID names a "meta" marker -> pass
#   (bu) marker file absent,    flag write -> block (fail-open resolve, default-deny)
#
# B-k regression -- the CLOBBER (`>|`) and to-a-FILE dup (`>&<f>`) redirect operators.
# Both DO write the file in bash, but their second glyph is also a separator, so the
# split tore the operator from its target and the write was never seen (under-block).
# _guard_git_policy_norm_redir_ops now rewrites them to a plain `>` before the split.
# The fd DUP forms must stay NON-writes: they name a descriptor, not a path (bash dups
# even across whitespace: `2>& 1` is verified fd-dup, not a file named 1). (by)-(cc) are
# the no-over-block arm; (bn) above is the standing `cat <flag> 2>&1` regression:
#   (bv) worker  echo enabled >| <flag>      -> block (clobber, spaced)
#   (bw) worker  echo enabled >|<flag>       -> block (clobber, unspaced)
#   (bx) worker  echo enabled >& <flag>      -> block (>& to a FILE)
#   (by) worker  echo x >&2                  -> pass  (fd dup)
#   (bz) worker  cat <flag> 2>& 1            -> pass  (fd dup across whitespace)
#   (ca) worker  cat <flag> >&-              -> pass  (close fd, not a path)
#   (cb) worker  cat <flag> >& /dev/null     -> pass  (>& to a DIFFERENT file)
#   (cc) meta    echo enabled >| <flag>      -> pass  (meta exemption holds)
#
# B-l regression -- two spellings of git that bash runs but the `^...git <verb>` anchor
# missed (under-block). A redirection is legal BETWEEN git and its verb (`git >f commit`
# runs `git commit`), and a PATH-qualified git is still git. The redirect target word is
# required, so (cg) does not false-block; the path atom requires a `/` boundary, so a
# word merely ENDING in git does not match ((ck)-(cm)). Policy=disabled throughout:
#   (cd) git >/tmp/o commit -m x             -> block (redir between git and verb)
#   (ce) git 2>/tmp/o push                   -> block (fd-qualified mid redir)
#   (cf) git >|/tmp/o commit                 -> block (clobber mid redir)
#   (cg) git > commit                        -> pass  (redirect to a FILE named commit)
#   (ch) git status > /tmp/push (read verb)  -> pass  (target named for a verb)
#   (ci) /usr/bin/git commit -m x            -> block (absolute-path git)
#   (cj) /usr/bin/git push                   -> block (absolute-path git)
#   (ck) /usr/bin/git status (read-only)     -> pass
#   (cl) legit commit -m x                   -> pass  (word ending in git)
#   (cm) mygit commit -m x                   -> pass  (word ending in git)
#   (cn) /usr/bin/mygit commit               -> pass  (path to a DIFFERENT binary)
#   (co) sudo git commit (disclosed residual) -> pass

set -uo pipefail

TESTDIR="$(cd "$(dirname "$0")" && pwd)"
GUARDS_DIR="$(cd "$TESTDIR/.." && pwd)"

TMPD="$(mktemp -d "${TMPDIR:-/tmp}/git-policy-bite.XXXXXX")"
trap 'rm -rf "$TMPD"' EXIT

DISABLED="$TMPD/policy-disabled"
ENABLED="$TMPD/policy-enabled"
printf 'disabled\n' >"$DISABLED"
printf 'enabled\n'  >"$ENABLED"

fails=0

# run_case <label> <cmd> <agent> <want_rc> <stderr_must|""> <stderr_mustnot|""> [ENV=VAL ...]
# <agent> feeds GUARD_AGENT directly after guard_init (this test does not source
# hooks/lib.sh, so walk_to_agent never runs and GUARD_AGENT would otherwise stay
# "" for every case -- same isolation rationale as test-20-write-acl.sh).
run_case() {
  local label="$1" cmd="$2" agent="$3" want_rc="$4" must="$5" mustnot="$6"; shift 6
  local stdin_json err_file rc ok
  stdin_json=$(jq -nc --arg c "$cmd" '{tool_name:"Bash", tool_input:{command:$c}}')
  err_file="$TMPD/stderr.txt"

  env "$@" bash -c '
    set -uo pipefail
    . "$1/lib-guard.sh"
    . "$1/26-git-policy.sh"
    guard_init "$2"
    GUARD_AGENT="$3"
    run_guard guard_git_policy
  ' _ "$GUARDS_DIR" "$stdin_json" "$agent" >/dev/null 2>"$err_file"
  rc=$?

  assert_case "$label" "$rc" "$want_rc" "$must" "$mustnot" "$err_file"
}

# assert_case <label> <rc> <want_rc> <must|""> <mustnot|""> <err_file>: score one case
# (shared by run_case and run_ident_case).
assert_case() {
  local label="$1" rc="$2" want_rc="$3" must="$4" mustnot="$5" err_file="$6" ok=1
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

# run_ident_case <label> <cmd> <session_id|""> <want_rc> <must|""> <mustnot|""> [ENV=VAL ...]
# Identity-resolution variant of run_case: it does NOT set GUARD_AGENT, so guard_init's
# own resolution (proc-tree walk, then the session-agent marker) is what is under test.
# walk_to_agent is undefined here (hooks/lib.sh is not sourced), so the walk arm yields
# "" exactly as it does live when the hook runs detached. An empty <session_id> omits
# the field from the stdin JSON, exercising the $CLAUDE_CODE_SESSION_ID fallback.
run_ident_case() {
  local label="$1" cmd="$2" sid="$3" want_rc="$4" must="$5" mustnot="$6"; shift 6
  local stdin_json err_file rc
  if [ -n "$sid" ]; then
    stdin_json=$(jq -nc --arg c "$cmd" --arg s "$sid" \
      '{tool_name:"Bash", session_id:$s, tool_input:{command:$c}}')
  else
    stdin_json=$(jq -nc --arg c "$cmd" '{tool_name:"Bash", tool_input:{command:$c}}')
  fi
  err_file="$TMPD/stderr.txt"

  env "$@" bash -c '
    set -uo pipefail
    . "$1/lib-guard.sh"
    . "$1/26-git-policy.sh"
    guard_init "$2"
    run_guard guard_git_policy
  ' _ "$GUARDS_DIR" "$stdin_json" >/dev/null 2>"$err_file"
  rc=$?

  assert_case "$label" "$rc" "$want_rc" "$must" "$mustnot" "$err_file"
}

echo "=== test-26-git-policy ==="

# policy=disabled -> BLOCK (exit 2)
run_case "(a) git commit -m x -> block"                 "git commit -m x"                    "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case "(b) git -C /tmp/r commit -> block"            "git -C /tmp/r commit -m x"          "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case "(c) cd /tmp/r && git commit -> block"         "cd /tmp/r && git commit -m x"       "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case "(d) git push origin main -> block"            "git push origin main"               "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case "(e) git commit-tree -> block"                 "git commit-tree deadbeef"           "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case "(f) bash -c wrapper -> block"                 'bash -c "git commit -m x"'          "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case "(g) env-prefixed commit -> block"             "GIT_AUTHOR_NAME=x git commit -m y"  "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case "(h) gh release create -> block"               "gh release create v1"               "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"

# policy=enabled -> PASS (exit 0)
run_case "(i) git commit (enabled) -> pass"             "git commit -m x"                    "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$ENABLED"
run_case "(j) git status (enabled) -> pass"             "git status"                         "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$ENABLED"

# policy=disabled, read-only -> PASS (exit 0)
run_case "(k) git status (disabled) -> pass"            "git status"                         "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case "(l) git log (disabled) -> pass"               "git log --oneline"                  "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"

# kill-switch -> total silence
run_case "(m) kill-switch off -> silence"               "git commit -m x"                    "" 0 ""            "GUARD" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED" SUPERCLAUDE_GUARDS=off

# Flag-write self-unblock cases: policy state is irrelevant here (ENABLED file),
# proving the check fires independent of the disabled/enabled gate.
FLAG="$TMPD/flag-target"

run_case "(n) worker printf > flag -> block"            "printf enabled > $FLAG"             "w-implementer" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"
run_case "(o) worker echo >> flag -> block"             "echo disabled >> $FLAG"             "w-implementer" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"
run_case "(p) worker sed -i flag -> block"              "sed -i s/x/y/ $FLAG"                "w-implementer" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"
run_case "(q) meta printf > flag -> pass"               "printf enabled > $FLAG"             "meta"          0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"
run_case "(r) empty agent echo > flag -> block"         "echo enabled > $FLAG"               ""              2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"
run_case "(s) worker cat flag (read-only) -> pass"      "cat $FLAG"                          "w-implementer" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"

# B1: inline git alias to a mutation verb (policy=disabled). Single-quoted cmds so
# the shell does not pre-expand anything; the guard inspects the literal string.
run_case '(t) alias.ci=commit -> block'                 'git -c alias.ci=commit ci -m x'     "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(u) alias.p=push -> block'                    'git -c alias.p=push p'              "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(v) alias.ci="commit" (quoted) -> block'      'git -c alias.ci="commit" ci'        "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(w) -c core.editor=vim (benign) -> pass'      'git -c core.editor=vim status'      "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(x) alias.st=status (read-only) -> pass'      'git -c alias.st=status st'          "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"

# B2: flag-write path spellings. HOME is overridden to a $TMPDIR sandbox so the
# `$HOME`/`~` forms resolve into it, never the real ~/.claude/config/git-policy.
# The policy file for these cases IS that sandbox flag, so every spelling below
# must resolve to the exact same real path. Single-quoted cmds keep `$HOME`/`~`
# literal for the guard to expand itself.
HHOME="$TMPD/home"
HFLAG="$HHOME/.claude/config/git-policy"
mkdir -p "$HHOME/.claude/config"

run_case '(y) worker $HOME spelling -> block'           'echo enabled > $HOME/.claude/config/git-policy'       "w-implementer" 2 "GUARD-BLOCK" "" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"
run_case '(z) worker ${HOME} spelling -> block'         'echo enabled > ${HOME}/.claude/config/git-policy'     "w-implementer" 2 "GUARD-BLOCK" "" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"
run_case '(aa) worker ~/ spelling -> block'             'echo enabled > ~/.claude/config/git-policy'           "w-implementer" 2 "GUARD-BLOCK" "" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"
run_case '(ab) worker // spelling -> block'             'echo enabled > $HOME/.claude/config//git-policy'      "w-implementer" 2 "GUARD-BLOCK" "" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"
run_case '(ac) worker cd + relative -> block'           'cd $HOME/.claude/config && echo enabled > git-policy' "w-implementer" 2 "GUARD-BLOCK" "" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"
run_case '(ad) worker tee $HOME -> block'               'printf enabled | tee $HOME/.claude/config/git-policy' "w-implementer" 2 "GUARD-BLOCK" "" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"
run_case '(ae) meta $HOME spelling -> pass'             'echo enabled > $HOME/.claude/config/git-policy'       "meta"          0 ""            "GUARD-BLOCK" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"
run_case '(af) worker other file -> pass'               'echo enabled > $HOME/.claude/config/other-file'       "w-implementer" 0 ""            "GUARD-BLOCK" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"
run_case '(ag) worker cd elsewhere + relative -> pass'  'cd /tmp && echo enabled > git-policy'                 "w-implementer" 0 ""            "GUARD-BLOCK" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"

# B-a regression: a mutation verb (or the word git) held apart from `git` by
# QUOTING or BACKSLASH-ESCAPING must still block; benign / non-mutation forms must
# not. Single-quoted cmds keep the quote/backslash glyphs literal for the guard.
run_case '(ah) git "commit" (quoted verb) -> block'     'git "commit" -m x'                  "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case "(ai) git 'commit' (squoted verb) -> block"    "git 'commit' -m x"                  "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(aj) git com"m"it (split verb) -> block'      'git com"m"it -m x'                  "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(ak) backslash-git commit -> block'           '\git commit -m x'                   "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(al) g\it commit (escaped git) -> block'      'g\it commit -m x'                   "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(am) git commit-graph write (non-mut) -> pass' 'git commit-graph write'            "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"

# B-b regression: variable-indirection flag write must block; the command-
# substitution form is the DOCUMENTED residual and correctly passes. HOME override
# as in (y)-(ag). Single-quoted cmds keep `$HOME`/`$f`/`${f}` literal for the guard.
run_case '(an) worker f=<path>; > $f -> block'          'f=$HOME/.claude/config/git-policy; echo enabled > $f'   "w-implementer" 2 "GUARD-BLOCK" "" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"
run_case '(ao) worker f=<base>; > dir/$f -> block'      'f=git-policy; echo enabled > $HOME/.claude/config/$f'   "w-implementer" 2 "GUARD-BLOCK" "" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"
run_case '(ap) worker ${f} brace indirection -> block'  'f=$HOME/.claude/config/git-policy; echo x > ${f}'       "w-implementer" 2 "GUARD-BLOCK" "" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"
run_case '(aq) worker cmd-sub path (residual) -> pass'  'echo enabled > $(echo $HOME)/.claude/config/git-policy' "w-implementer" 0 ""            "GUARD-BLOCK" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"
run_case '(ar) worker f=<other>; > $f (benign) -> pass' 'f=$HOME/.claude/config/other; echo x > $f'              "w-implementer" 0 ""            "GUARD-BLOCK" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"

# B-d regression: a literal TAB is intra-command whitespace, not a separator, so a
# tab between git and its verb (or before a flag-write target) must still block;
# git<TAB>status stays benign. Tabs are injected via ANSI-C $'...' quoting (which
# expands \t but does NOT expand $HOME, so the flag-write forms stay literal).
run_case '(as) git<TAB>commit -> block'                 $'git\tcommit -m x'                  "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(at) git<TAB>push -> block'                   $'git\tpush origin main'             "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(au) git<TAB>status (benign) -> pass'         $'git\tstatus'                       "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(av) worker >TAB flag -> block'               $'printf enabled >\t$HOME/.claude/config/git-policy'   "w-implementer" 2 "GUARD-BLOCK" "" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"
run_case '(aw) worker tee<TAB> flag -> block'           $'printf x | tee\t$HOME/.claude/config/git-policy'     "w-implementer" 2 "GUARD-BLOCK" "" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"

# B-e regression: a flag basename assembled from split literal-valued vars must
# block (the basename short-circuit runs AFTER $HOME + VAR expansion); a split-var
# building a NON-flag basename passes. Single-quoted cmds keep `$HOME`/`${b}` literal.
run_case '(ax) worker b=git; ${b}-policy -> block'      'b=git; printf enabled > $HOME/.claude/config/${b}-policy'  "w-implementer" 2 "GUARD-BLOCK" "" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"
run_case '(ay) worker x=git;y=policy; ${x}-${y} -> block' 'x=git; y=policy; echo enabled > $HOME/.claude/config/${x}-${y}' "w-implementer" 2 "GUARD-BLOCK" "" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"
run_case '(az) worker b=git; ${b}-other (benign) -> pass' 'b=git; echo enabled > $HOME/.claude/config/${b}-other'     "w-implementer" 0 ""            "GUARD-BLOCK" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"

# Disclosed residual (NOT closed by design): a whitespace-valued variable as the
# separator; its value is not a literal in the string. Must PASS (documented).
run_case '(ba) git${IFS}commit (residual) -> pass'      'git${IFS}commit -m x'               "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"

# B-f regression: a backslash-newline line continuation is elided by the shell
# (git \<LF>commit runs git commit), so it must still block; a benign continuation
# does not. Injected via ANSI-C $'...' (\\ -> backslash, \n -> newline; $HOME stays
# literal so the flag-write form resolves against the guard's HOME).
run_case '(bb) git \<LF>commit -> block'                $'git \\\ncommit -m x'               "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(bc) git \<LF>push -> block'                  $'git \\\npush origin main'          "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(bd) git \<LF>status (benign) -> pass'        $'git \\\nstatus'                    "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(be) worker > \<LF>flag -> block'             $'printf enabled > \\\n$HOME/.claude/config/git-policy' "w-implementer" 2 "GUARD-BLOCK" "" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"

# B-g regression: bash permits a redirection before the command word, so a leading
# redirect must not defeat the segment-start anchor; a leading redirect with NO git
# (or a read-only verb) stays benign.
run_case '(bf) >f git commit -> block'                  '>/tmp/zz git commit -m x'           "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(bg) 2>f git push -> block'                   '2>/tmp/zz git push'                 "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(bh) &>f git commit -> block'                 '&>/tmp/zz git commit'               "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(bi) FOO=1 >f git commit -> block'            'FOO=1 >/tmp/zz git commit'          "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(bj) >f git status (benign) -> pass'          '>/tmp/zz git status'                "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(bk) >f ls (no git, benign) -> pass'          '>/tmp/zz ls -la'                    "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"

# B-h regression (anti-DoS segment cap): a basename-present command padded beyond the
# ~200-segment cap is treated as a flag write and BLOCKED for a non-meta agent
# (bounded, no hang); a benign large command WITHOUT the basename still passes fast
# (no false-block). The commands are built here to exceed the cap.
CAP_BLOCK="printf enabled > $HFLAG"
for _p in $(seq 1 250); do CAP_BLOCK="$CAP_BLOCK; echo pad$_p"; done
run_case '(bl) basename-present >cap segs -> block'     "$CAP_BLOCK"                         "w-implementer" 2 "GUARD-BLOCK" "" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"
CAP_PASS="echo start"
for _p in $(seq 1 250); do CAP_PASS="$CAP_PASS; echo pad$_p > /tmp/pad$_p"; done
run_case '(bm) benign >cap segs, no basename -> pass'   "$CAP_PASS"                          "w-implementer" 0 ""            "GUARD-BLOCK" HOME="$HHOME" SUPERCLAUDE_GIT_POLICY_FILE="$HFLAG"

# B-i regression: a redirection must TARGET the flag to count. The `2>&1` stderr dup in
# (bn) only CO-OCCURS with the flag path -- the command reads the flag and writes
# nothing -- so it must pass, while the real write (bo) and the meta exemption (bp)
# still behave. $FLAG is the absolute $TMPDIR path (expanded here by the test shell).
run_case '(bn) worker cat flag 2>&1 (read) -> pass'     "cat $FLAG 2>&1"                     "w-implementer" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"
run_case '(bo) worker printf > flag (write) -> block'   "printf enabled > $FLAG"             "w-implementer" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"
run_case '(bp) meta printf > flag (write) -> pass'      "printf enabled > $FLAG"             "meta"          0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"

# B-j regression: agent identity resolution in guard_init. The markers mirror what
# hooks/modules/00-parse.sh writes: the agent name plus a newline, and a BARE newline
# for a session started with no --agent flag (which must stay UNRESOLVED, not resolve
# to an agent named ""). SUPERCLAUDE_SESSION_TIMER_DIR keeps every lookup inside
# $TMPDIR, so no real ~/.claude/session-timers file is ever read.
TDIR="$TMPD/session-timers"
mkdir -p "$TDIR"
printf 'meta\n'          >"$TDIR/sess-meta.agent"
printf '\n'              >"$TDIR/sess-empty.agent"
printf 'w-implementer\n' >"$TDIR/sess-worker.agent"

run_ident_case '(bq) walk empty + marker meta -> pass'      "printf enabled > $FLAG" "sess-meta"   0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG" SUPERCLAUDE_SESSION_TIMER_DIR="$TDIR"
run_ident_case '(br) walk empty + marker EMPTY -> block'    "printf enabled > $FLAG" "sess-empty"  2 "GUARD-BLOCK" ""            SUPERCLAUDE_GIT_POLICY_FILE="$FLAG" SUPERCLAUDE_SESSION_TIMER_DIR="$TDIR"
run_ident_case '(bs) walk empty + marker worker -> block'   "printf enabled > $FLAG" "sess-worker" 2 "GUARD-BLOCK" ""            SUPERCLAUDE_GIT_POLICY_FILE="$FLAG" SUPERCLAUDE_SESSION_TIMER_DIR="$TDIR"
run_ident_case '(bt) no stdin sid + env sid meta -> pass'   "printf enabled > $FLAG" ""            0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG" SUPERCLAUDE_SESSION_TIMER_DIR="$TDIR" CLAUDE_CODE_SESSION_ID="sess-meta"
run_ident_case '(bu) walk empty + no marker -> block'       "printf enabled > $FLAG" "sess-none"   2 "GUARD-BLOCK" ""            SUPERCLAUDE_GIT_POLICY_FILE="$FLAG" SUPERCLAUDE_SESSION_TIMER_DIR="$TDIR"

# B-k: the clobber (`>|`) and to-a-FILE dup (`>&<f>`) redirect operators write the flag
# in bash and must block; the fd DUP spellings name a descriptor, not a path, and must
# stay passing. $FLAG is the absolute $TMPDIR path (expanded here by the test shell);
# OTHER is a second file proving the target, not the mere operator, is what counts.
OTHER="$TMPD/other-target"

run_case '(bv) worker >| flag (clobber) -> block'       "echo enabled >| $FLAG"        "w-implementer" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"
run_case '(bw) worker >|flag (unspaced) -> block'       "echo enabled >|$FLAG"         "w-implementer" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"
run_case '(bx) worker >& flag (to a FILE) -> block'     "echo enabled >& $FLAG"        "w-implementer" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"
run_case '(by) worker echo x >&2 (fd dup) -> pass'      'echo x >&2'                   "w-implementer" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"
run_case '(bz) worker cat flag 2>& 1 (fd dup) -> pass'  "cat $FLAG 2>& 1"              "w-implementer" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"
run_case '(ca) worker cat flag >&- (close) -> pass'     "cat $FLAG >&-"                "w-implementer" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"
run_case '(cb) worker >& other file -> pass'            "cat $FLAG >& $OTHER"          "w-implementer" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"
run_case '(cc) meta >| flag -> pass'                    "echo enabled >| $FLAG"        "meta"          0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$FLAG"

# B-l: a redirection between git and its verb, and a path-qualified git. Both are run by
# bash as a plain commit/push and must block; the no-over-block arm proves a redirect
# TARGET named for a verb, a read-only verb, and a word merely ENDING in git all pass.
run_case '(cd) git >f commit (mid redir) -> block'      'git >/tmp/zz commit -m x'     "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(ce) git 2>f push (mid redir) -> block'       'git 2>/tmp/zz push'           "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(cf) git >|f commit (mid clobber) -> block'   'git >|/tmp/zz commit'         "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(cg) git > commit (file named commit) -> pass' 'git > commit'                "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(ch) git status > /tmp/push (benign) -> pass' 'git status > /tmp/push'       "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(ci) /usr/bin/git commit -> block'            '/usr/bin/git commit -m x'     "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(cj) /usr/bin/git push -> block'              '/usr/bin/git push origin main' "" 2 "GUARD-BLOCK" "" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(ck) /usr/bin/git status (benign) -> pass'    '/usr/bin/git status'          "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(cl) legit commit (word ends in git) -> pass' 'legit commit -m x'            "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(cm) mygit commit (word ends in git) -> pass' 'mygit commit -m x'            "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(cn) /usr/bin/mygit commit (other bin) -> pass' '/usr/bin/mygit commit'      "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"
run_case '(co) sudo git commit (residual) -> pass'      'sudo git commit -m x'         "" 0 ""            "GUARD-BLOCK" SUPERCLAUDE_GIT_POLICY_FILE="$DISABLED"

if [ "$fails" -eq 0 ]; then
  echo "test-26-git-policy: ALL PASS"
  exit 0
else
  echo "test-26-git-policy: $fails case(s) FAILED"
  exit 1
fi
