---
name: infra-security
description: "Use when security-reviewing ~/.claude infrastructure changes."
category: meta
user-invocable: false
---

# Infrastructure Security Checklist

Apply this checklist when reviewing changes to files under `~/.claude/` (agents, hooks, rules, skills, settings.json, scripts). Flag findings as `[INFRA-SECURITY]` with severity.

## 1. Permissions & Sandbox Integrity

- [ ] settings.json deny list: no rules removed without explicit the user approval
- [ ] settings.json allow list: no broad permissions added (`Bash(*)` instead of `Bash(cmd:*)`)
- [ ] `sandbox.enabled` remains `true`
- [ ] No new SSH/GPG/credential paths added to `allowWrite` or removed from `denyRead`
- [ ] Hook changes don't introduce new tool permissions (e.g., a hook that writes to settings.json)

## 2. Hook Safety

- [ ] All hooks exit 0 on the success path (non-zero blocks tool calls for PreToolUse)
- [ ] No unguarded commands in `set -euo pipefail` context (`grep` without `|| true`, `cat` without `2>/dev/null`)
- [ ] Hook execution time <500ms (measured, not estimated)
- [ ] No network calls in hooks (hooks run on EVERY tool call — latency kills)
- [ ] No recursive hook triggers (hook A invokes a tool that triggers hook B that triggers hook A)
- [ ] stderr/stdout separation correct (agent-visible messages to stderr, silent operation otherwise)

## 3. Implicit Execution Detection

- [ ] No code that executes on import/load without explicit invocation (module-level side effects)
- [ ] No skills/hooks that fetch or execute remote code
- [ ] No auto-update mechanisms (supply chain risk)
- [ ] SessionStart hooks don't perform destructive operations (session may start unexpectedly)

## 4. Agent Authority Compliance

- [ ] Agent's tool list matches its hierarchy tier (workers don't have Write, orchs don't have settings access)
- [ ] Agent's model field is appropriate for its role (main agents on opus[1m], workers on opus)
- [ ] No agent bypasses the comms protocol (writing directly to another agent's comms dir)
- [ ] Skills reference only tools their invoking agent has access to

## 5. Red Flag Scan

- [ ] No hardcoded paths to /tmp with predictable names (symlink attacks)
- [ ] No `eval`, `exec`, or dynamic code execution in hooks/scripts
- [ ] No commands that could leak environment variables (`env`, `printenv` in hooks)
- [ ] No `git push` in hooks or automatic scripts (push is a human decision)
- [ ] No credential file access (`.env`, `.ssh/`, `.gnupg/`, tokens)
