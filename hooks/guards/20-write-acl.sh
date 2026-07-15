# Guard: 20-write-acl — path-scoped write ACL keyed on resolved agent identity
# (PHASE2-CONTRACT sec 2/6, enforcement-gap-ledger.md Family 2 rows #4-#7).
#
# Fires only for write-class tools (Write/Edit/MultiEdit). Resolves the target path
# (guard_file_path, ~-expanded and normalized) and the actor's role from GUARD_AGENT
# (meta; orch = orch/orch-<name>/o-<name>; worker = w-<name>; scaf = scaf), then blocks
# on the write-scope violations in rules/12-agent-hierarchy.md's Write Scope tables:
#   - plan.md: meta-only (rules/12 § Plans and State).
#   - comms/*/directives.md, comms/*/bootstrap.md: meta-only (rules/12 § Communication).
#   - comms/<other>/...: an orch may write only its OWN comms dir (rules/12 § Communication).
#   - ~/.claude/settings.json: scaf-only (scaf.md:34; sandbox already denies this, defense
#     in depth per enforcement-gap-ledger.md Family 2 row #6).
#   - ~/.claude/config/git-policy: meta-only (owner ruling 2026-07-15, guards/26-git-policy.sh
#     is the SOT enforcing the flag; this rule stops a blocked agent from just flipping it
#     back via a direct Write/Edit/MultiEdit). UNLIKE every other identity-scoped rule here,
#     an unresolved GUARD_AGENT BLOCKS instead of failing open: the flag is security-critical,
#     so an unresolved identity must not be able to enable/disable git for the whole agent
#     population.
#   - project-local .claude/ or CLAUDE.md (not rooted at ~/.claude itself): never (rules/12
#     § Global Workspace Rule "NEVER touch <project>/.claude/, <project>/CLAUDE.md"). This
#     rule is identity-independent: it fires even with an unresolved GUARD_AGENT, since it
#     does not depend on WHO is writing.
#
# An unresolved GUARD_AGENT (empty string; walk_to_agent found no `--agent` ancestor) fails
# OPEN on every identity-scoped rule above (guessing a role is worse than skipping a check) —
# except the project-local .claude rule (identity-independent) and the git-policy rule
# (default-deny by design; see above), both of which fire regardless of identity.

GUARD_MODE_WRITE_ACL=block

# _wacl_norm_path <path>: expand a leading ~ and resolve . / .. / // via `realpath -m`
# (tolerates a nonexistent target; no filesystem access needed for normalization other
# than that). Echoes "" for anything that is not absolute after ~-expansion — a relative
# path can't be ACL'd reliably without trusting a caller-supplied CWD, so the caller
# fails open on empty output.
_wacl_norm_path() {
  local p="${1:-}"
  case "$p" in
    "~")   p="$HOME" ;;
    "~/"*) p="$HOME/${p#\~/}" ;;
  esac
  case "$p" in
    /*) : ;;
    *) echo ""; return 0 ;;
  esac
  if command -v realpath >/dev/null 2>&1; then
    realpath -m -- "$p" 2>/dev/null || echo "$p"
  else
    echo "$p"
  fi
}

# _wacl_role <agent>: map a resolved GUARD_AGENT to a role. Unknown/empty -> "".
_wacl_role() {
  case "${1:-}" in
    meta)        echo "meta" ;;
    scaf)        echo "scaf" ;;
    orch|orch-*|o-*) echo "orch" ;;
    w-*)         echo "worker" ;;
    *)           echo "" ;;
  esac
}

guard_write_acl() {
  case "${GUARD_TOOL:-}" in
    Write|Edit|MultiEdit) ;;
    *) return 0 ;;
  esac

  local path
  path=$(_wacl_norm_path "$(guard_file_path)")
  [ -n "$path" ] || return 0

  local claude_home
  claude_home=$(_wacl_norm_path "$HOME/.claude")

  # Identity-independent: project-local .claude/ or CLAUDE.md, i.e. anything matching
  # */.claude/* or */CLAUDE.md that is NOT rooted at ~/.claude itself.
  case "$path" in
    "$claude_home"|"$claude_home"/*) : ;;
    */.claude/*|*/.claude)
      guard_block "project-local .claude write blocked (rules/12: never touch <project>/.claude/); path=$path"
      return 0
      ;;
    */CLAUDE.md)
      guard_block "project-local CLAUDE.md write blocked (rules/12: never touch <project>/CLAUDE.md); path=$path"
      return 0
      ;;
  esac

  # Remaining rules key on agent identity. Fail open when it did not resolve
  # -- EXCEPT the git-policy flag below, which default-denies instead.
  local role
  role=$(_wacl_role "${GUARD_AGENT:-}")

  # git-policy flag: meta-only, default-deny on unresolved identity (security-critical;
  # see the header comment). Checked before the generic fail-open return below so an
  # empty role still hits this block instead of passing through.
  if [ "$path" = "$claude_home/config/git-policy" ] && [ "$role" != "meta" ]; then
    guard_block "the /git policy flag is meta-only; workers and orchs cannot enable/disable git (owner manages git; SOT guards/26-git-policy.sh)"
    return 0
  fi

  [ -n "$role" ] || return 0

  if [ "$path" = "$claude_home/settings.json" ] && [ "$role" != "scaf" ]; then
    guard_block "settings.json write blocked (scaf.md:34, rules/12: scaf-only); agent=${GUARD_AGENT:-} path=$path"
    return 0
  fi

  case "$path" in
    "$claude_home"/plans/*/plan.md)
      if [ "$role" != "meta" ]; then
        guard_block "plan.md write blocked (rules/12 § Plans and State: meta-only SOT); agent=${GUARD_AGENT:-} path=$path"
        return 0
      fi
      ;;
  esac

  case "$path" in
    "$claude_home"/comms/*/directives.md|"$claude_home"/comms/*/bootstrap.md)
      if [ "$role" != "meta" ]; then
        guard_block "comms directives/bootstrap write blocked (rules/12 § Communication: meta-only); agent=${GUARD_AGENT:-} path=$path"
        return 0
      fi
      ;;
    "$claude_home"/comms/*)
      if [ "$role" = "orch" ]; then
        local rest dir
        rest="${path#"$claude_home"/comms/}"
        dir="${rest%%/*}"
        if [ "$dir" != "${GUARD_AGENT:-}" ]; then
          guard_block "comms cross-namespace write blocked (rules/12 § Communication: own-dir only); agent=${GUARD_AGENT:-} target_dir=$dir path=$path"
          return 0
        fi
      fi
      ;;
  esac

  return 0
}
