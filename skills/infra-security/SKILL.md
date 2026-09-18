---
name: infra-security
description: "Use when security-reviewing ~/.claude infrastructure changes."
category: meta
user-invocable: false
---

# Infrastructure Security Checklist

Apply this checklist when reviewing changes to files under `~/.claude/` (agents, hooks, rules, skills, settings.json, scripts). Flag findings as `[INFRA-SECURITY]` with severity.

What to inspect: the Python hook dispatchers under `~/.claude/hooks/` (one dispatcher per event, plus the guard and module code they import), the hook registrations in `settings.json` (`hooks` block: event, matcher, command), and `permissions.deny` / `permissions.allow` in `settings.json`. There is no OS-level isolation layer on native Windows: enforcement is the write-ACL guard plus `permissions.deny`, so a weakening of either is a weakening of the whole perimeter.

## 1. Permissions & Enforcement Integrity

- [ ] settings.json `permissions.deny`: no rules removed or narrowed without explicit the user approval
- [ ] settings.json `permissions.allow`: no broad permissions added (`Bash(*)` instead of `Bash(cmd:*)`, and likewise `PowerShell(*)`)
- [ ] Deny rules cover both shells: a command denied for the Bash tool is not reachable through the PowerShell tool (and vice versa)
- [ ] The write-ACL guard is still registered on PreToolUse and its protected-path list has not shrunk
- [ ] No SSH/GPG/credential paths removed from `permissions.deny`
- [ ] Hook registrations in `settings.json` invoke the expected dispatcher under `~/.claude/hooks/`; no registration removed, re-pointed, or given a matcher that silently excludes a tool
- [ ] Hook changes don't introduce new tool permissions (e.g., a hook that writes to settings.json)

## 2. Hook Safety

- [ ] All hooks exit 0 on the success path (non-zero blocks tool calls for PreToolUse)
- [ ] No unhandled exception on the hot path: malformed or missing stdin JSON, a missing file, or an unexpected tool name must not crash the dispatcher. Each guard states explicitly whether it fails open or fails closed
- [ ] Hook execution time <500ms (measured, not estimated); no heavy imports at module level in a dispatcher that runs on every tool call
- [ ] No network calls in hooks (hooks run on EVERY tool call — latency kills)
- [ ] No recursive hook triggers (hook A invokes a tool that triggers hook B that triggers hook A)
- [ ] stderr/stdout separation correct (agent-visible messages to stderr, silent operation otherwise)
- [ ] Path checks normalise before comparing (resolved, case-folded, both separator styles): a guard that matches only forward slashes or only one letter case is bypassable on Windows
- [ ] If a validation step is essential, the file compiles: `scpy -m py_compile <file>`

## 3. Implicit Execution Detection

- [ ] No code that executes on import/load without explicit invocation (module-level side effects)
- [ ] No skills/hooks that fetch or execute remote code
- [ ] No auto-update mechanisms (supply chain risk)
- [ ] SessionStart hooks don't perform destructive operations (session may start unexpectedly)

## 4. Agent Authority Compliance

- [ ] Agent's tool list matches its hierarchy tier (read-only workers don't have Write or Edit; no worker has settings access)
- [ ] Agent's model field is appropriate for its role (main agents on opus[1m], workers on opus)
- [ ] No agent writes outside its declared file scope
- [ ] Skills reference only tools their invoking agent has access to

## 5. Red Flag Scan

- [ ] No hardcoded temp paths with predictable names outside `~/.claude/tmp/`
- [ ] No `eval`, `exec`, `subprocess` with `shell=True`, or other dynamic code execution in hooks/scripts
- [ ] No commands that could leak environment variables (dumping `os.environ`, `env`, `printenv` in hooks)
- [ ] No `git push` in hooks or automatic scripts (push is a human decision)
- [ ] No credential file access (`.env`, `.ssh/`, `.gnupg/`, tokens)
