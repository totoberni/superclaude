#!/usr/bin/env bash
# Superclaude shortcuts wrapper
# - Adds subcommand shortcuts to the `claude` command (e.g., `claude sr` → session-reaper)
# - Falls through to the real `claude` CLI for everything else
# - Provides tab completion for shortcuts + per-subcommand flags
# - Preserves existing `claude --agent <TAB>` completion via _claude_agent_complete
#
# Install: add this single line to ~/.bashrc (after the existing claude-completion source):
#   source ~/.claude/scripts/claude-shortcuts.sh
#
# Discover: `claude help` (or `claude help <sub>` for one synopsis)

# ============================================================================
# Wrapper function
# ============================================================================

claude() {
  case "$1" in
    sr|reap)         shift; bash "$HOME/.claude/scripts/session-reaper.sh" "$@" ;;
    ss|sstatus)      shift; bash "$HOME/.claude/scripts/session-status.sh" "$@" ;;
    hs|hcom)         shift; "$HOME/.claude/scripts/hcom-status" "$@" ;;
    sl|spawnlog)     shift; bash "$HOME/.claude/scripts/spawn-log-summary.sh" "$@" ;;
    ih|infra)        shift; bash "$HOME/.claude/scripts/infra-health.sh" "$@" ;;
    it|infratest)    shift; bash "$HOME/.claude/scripts/infra-test.sh" "$@" ;;
    arc|archive)     shift; bash "$HOME/.claude/scripts/auto-archive-stale-orchs.sh" "$@" ;;
    hb|backfill)     shift; bash "$HOME/.claude/scripts/hcom-backfill.sh" "$@" ;;
    mm|memmatrix)    shift; bash "$HOME/.claude/scripts/scan-mem-matrix.sh" "$@" ;;
    hi|hinit)        shift; bash "$HOME/.claude/scripts/hcom-init.sh" "$@" ;;
    th|testhooks)    shift; bash "$HOME/.claude/scripts/test-hooks.sh" "$@" ;;
    help|sc-help)    shift; _claude_sc_help "$@" ;;
    *)               ( [ -f "$HOME/.claude/superclaude.env" ] && . "$HOME/.claude/superclaude.env"; command claude "$@" ) ;;
  esac
}

# ============================================================================
# Help / synopsis (the "preview" substitute — claude help <sub>)
# ============================================================================

_claude_sc_help() {
  local sub="${1:-}"

  if [ -z "$sub" ] || [ "$sub" = "all" ]; then
    cat <<'EOF'
superclaude shortcuts (wrapper over the real `claude` CLI):

  claude sr  [flags]   session-reaper       (alias: reap)        clean stale session timer files
  claude ss            session-status       (alias: sstatus)     quick "what sessions are alive"
  claude hs  [args]    hcom-status          (alias: hcom)        broker inspection (summary/recent/locks/agents/json)
  claude sl  [flags]   spawn-log-summary    (alias: spawnlog)    Phase 7 telemetry from _spawns.log
  claude ih            infra-health         (alias: infra)       quick infra check
  claude it  [--full]  infra-test           (alias: infratest)   regression tests
  claude arc [flags]   auto-archive         (alias: archive)     archive registry-stale orchs
  claude hb  [flags]   hcom-backfill        (alias: backfill)    ingest historical comms into broker
  claude mm  [flags]   scan-mem-matrix      (alias: memmatrix)   memory budget audit
  claude hi  [flags]   hcom-init            (alias: hinit)       broker DB init / reset
  claude th            test-hooks           (alias: testhooks)   hook debugging
  claude help [sub]    this help (or single-subcommand synopsis)

Anything else falls through to the real `claude` CLI (e.g., `claude --agent meta`).
For per-subcommand flag synopsis: `claude help sr` (etc.)
EOF
    return 0
  fi

  case "$sub" in
    sr|reap) cat <<'EOF'
claude sr [--dry-run] [--all]
  session-reaper.sh — kill zombie session timer files
  --dry-run   preview what would be cleaned (no mutations)
  --all       also forcibly kill stale ACTIVE sessions (>53 min)
EOF
    ;;
    ss|sstatus) cat <<'EOF'
claude ss
  session-status.sh — list active claude sessions + their timer state
  (no flags currently)
EOF
    ;;
    hs|hcom) cat <<'EOF'
claude hs [mode]
  hcom-status — HCOM SQLite broker inspection (pure Python)
  modes: summary (default) | json | recent | locks | agents
  ex: claude hs recent      # last 20 messages
      claude hs agents      # agent_status table
      claude hs json        # raw JSON of broker stats
EOF
    ;;
    sl|spawnlog) cat <<'EOF'
claude sl [mode]
  spawn-log-summary.sh — analyze ~/.claude/comms/_spawns.log
  modes: (default summary) | --by-type | --by-parent | --recent
  ex: claude sl --by-type   # subagent_type histogram
      claude sl --recent    # last 20 spawn events
EOF
    ;;
    ih|infra) cat <<'EOF'
claude ih
  infra-health.sh — quick infra component check (hooks, scripts, perms)
EOF
    ;;
    it|infratest) cat <<'EOF'
claude it [--full]
  infra-test.sh — regression tests for hooks/scripts
  --full   include long-running tests
EOF
    ;;
    arc|archive) cat <<'EOF'
claude arc [--dry-run] [--exclude <name>] [--orch <name>]
  auto-archive-stale-orchs.sh — move registry-Decommissioned orks to _archive
  --dry-run        preview moves; no filesystem changes
  --exclude NAME   skip the named orch (e.g., the current session's orch)
  --orch NAME      only consider this one orch
EOF
    ;;
    hb|backfill) cat <<'EOF'
claude hb [--dry-run] [--apply] [--archive] [--orch <name>]
  hcom-backfill.sh — ingest historical comms into HCOM SQLite broker
  --dry-run    default; preview only
  --apply      actually insert
  --archive    also process ~/.claude/comms/_archive/
  --orch NAME  only this orch (incremental backfill / testing)
  Idempotent via backfill_audit table; safe to re-run.
EOF
    ;;
    mm|memmatrix) cat <<'EOF'
claude mm [mode]
  scan-mem-matrix.sh — agent-memory file LOC vs budget audit
  modes: (default --budgets table) | --json
EOF
    ;;
    hi|hinit) cat <<'EOF'
claude hi [--reset|--status]
  hcom-init.sh — initialize the HCOM SQLite broker DB
  (no flags) create DB if missing, apply schema (idempotent)
  --reset     DESTRUCTIVE drop + recreate (prompts for confirmation)
  --status    show current DB state + counts
EOF
    ;;
    th|testhooks) cat <<'EOF'
claude th
  test-hooks.sh — exercise hook scripts with synthetic inputs
EOF
    ;;
    *) echo "Unknown subcommand: $sub. Run 'claude help' for the full list." ;;
  esac
}

# ============================================================================
# Tab completion
# ============================================================================

_claude_shortcuts_complete() {
  local cur="${COMP_WORDS[COMP_CWORD]}"
  local prev="${COMP_WORDS[COMP_CWORD-1]}"
  local first="${COMP_WORDS[1]}"

  # If --agent or -a was the previous word: delegate to existing agent completion
  if [[ "$prev" == "--agent" || "$prev" == "-a" ]]; then
    if declare -f _claude_agent_complete >/dev/null 2>&1; then
      _claude_agent_complete
      return 0
    fi
  fi

  # First-arg completion: suggest shortcuts (real claude CLI args also accepted via fall-through pattern)
  if [ "$COMP_CWORD" -eq 1 ]; then
    local shortcuts="sr reap ss sstatus hs hcom sl spawnlog ih infra it infratest arc archive hb backfill mm memmatrix hi hinit th testhooks help"
    # Also include common claude CLI flags that follow `claude` directly
    local cli_flags="--agent -a --print -p --model --resume --session --version --help"
    COMPREPLY=( $(compgen -W "$shortcuts $cli_flags" -- "$cur") )
    return 0
  fi

  # Per-subcommand flag completion
  case "$first" in
    sr|reap)
      COMPREPLY=( $(compgen -W "--dry-run --all" -- "$cur") )
      ;;
    hb|backfill)
      # If the previous word is --orch, complete with available orch names
      if [[ "$prev" == "--orch" ]]; then
        local orchs
        orchs=$(ls -d "$HOME/.claude/comms"/*/ 2>/dev/null | xargs -n1 basename 2>/dev/null \
          | grep -vE '^(_archive|_template)$')
        COMPREPLY=( $(compgen -W "$orchs" -- "$cur") )
      else
        COMPREPLY=( $(compgen -W "--dry-run --apply --archive --orch" -- "$cur") )
      fi
      ;;
    arc|archive)
      if [[ "$prev" == "--orch" || "$prev" == "--exclude" ]]; then
        local orchs
        orchs=$(ls -d "$HOME/.claude/comms"/*/ 2>/dev/null | xargs -n1 basename 2>/dev/null \
          | grep -vE '^(_archive|_template)$')
        COMPREPLY=( $(compgen -W "$orchs" -- "$cur") )
      else
        COMPREPLY=( $(compgen -W "--dry-run --exclude --orch" -- "$cur") )
      fi
      ;;
    hs|hcom)
      COMPREPLY=( $(compgen -W "summary json recent locks agents" -- "$cur") )
      ;;
    hi|hinit)
      COMPREPLY=( $(compgen -W "--reset --status" -- "$cur") )
      ;;
    sl|spawnlog)
      COMPREPLY=( $(compgen -W "--by-type --by-parent --recent" -- "$cur") )
      ;;
    it|infratest)
      COMPREPLY=( $(compgen -W "--full" -- "$cur") )
      ;;
    mm|memmatrix)
      COMPREPLY=( $(compgen -W "--budgets --json" -- "$cur") )
      ;;
    help|sc-help)
      # Suggest known subcommand names so `claude help <TAB>` shows the synopsis targets
      local helpcmds="all sr reap ss sstatus hs hcom sl spawnlog ih infra it infratest arc archive hb backfill mm memmatrix hi hinit th testhooks"
      COMPREPLY=( $(compgen -W "$helpcmds" -- "$cur") )
      ;;
    *)
      # Not one of our shortcuts — fall through to existing claude completion if any
      if declare -f _claude_agent_complete >/dev/null 2>&1; then
        _claude_agent_complete
      fi
      ;;
  esac
  return 0
}

# Register our completion (overrides any prior registration on `claude`).
# The fall-through inside _claude_shortcuts_complete preserves agent-name completion.
complete -F _claude_shortcuts_complete claude
