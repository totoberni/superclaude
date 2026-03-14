#!/usr/bin/env bash
# Bash completion for `claude --agent <TAB>` and `claude -a <TAB>`
# Install: source ~/.claude/scripts/claude-completion.bash (add to .bashrc)

_claude_agent_complete() {
  local agents_dir="$HOME/.claude/agents"
  local cur="${COMP_WORDS[COMP_CWORD]}"
  local prev="${COMP_WORDS[COMP_CWORD-1]}"

  if [[ "$prev" == "--agent" || "$prev" == "-a" ]]; then
    local agents
    agents=$(find "$agents_dir" -maxdepth 1 -name '*.md' -printf '%f\n' 2>/dev/null \
      | sed 's/\.md$//' \
      | sort -u)
    COMPREPLY=($(compgen -W "$agents" -- "$cur"))
    return
  fi
}

complete -F _claude_agent_complete claude
