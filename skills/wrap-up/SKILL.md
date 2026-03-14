---
name: wrap-up
description: "Post-directive bundle: RPT + /mistake + /good-idea + state"
category: workflow
user-invocable: true
disable-model-invocation: true
argument-hint: "<project>"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# /wrap-up — Post-Directive Bundle

Bundles the 4-step post-directive ceremony into one command. Prevents the failure mode where orchs write RPT but forget retrospectives.

**Project**: $ARGUMENTS

## Procedure

Execute steps in order (each depends on the previous):

### Step 1: Write RPT

1. Read your `~/.claude/comms/<your-orch>/reports.md` to find next RPT number
2. Summarize the directive outcome:
   - What was the directive? (DIR ref + title)
   - What was delivered? (files changed, tests, key decisions)
   - What's the status? (DONE / PARTIAL / BLOCKED)
   - Any follow-up needed?
3. Write `## RPT-NNN` entry to `reports.md` with standard format:
   ```markdown
   ## RPT-NNN
   **Time**: <YYYY-MM-DD HH:MM>
   **Directive**: DIR-NNN — <title>
   **Status**: DONE | PARTIAL | BLOCKED
   **Summary**: <2-3 lines>
   **Deliverables**: <list>
   **Follow-up**: <if any>
   ```

### Step 2: Run /mistake

Invoke the `/mistake` skill for `$ARGUMENTS`:
- Review session for errors, retries, failed approaches
- **Primary**: record to `~/.claude/agent-memory/shared/projects/$ARGUMENTS.md`
- **Dual-write**: class-applicable mistakes also go to `class/<class>/mtm.md` (see /mistake Step 0)
- If no mistakes found, note "Clean session" and move on

### Step 3: Run /good-idea

Invoke the `/good-idea` skill for `$ARGUMENTS`:
- Review session for wins, effective patterns, good decisions
- **Primary**: record to `~/.claude/agent-memory/shared/projects/$ARGUMENTS.md`
- **Dual-write**: class-applicable wins also go to `class/<class>/mtm.md` (see /good-idea Step 0)
- If no notable wins, note "Standard execution" and move on

### Step 4: Update State

1. Read your state file (`~/.claude/plans/$ARGUMENTS/state-<your-orch>.md` or `state.md`)
2. Mark completed tasks as DONE
3. Update current phase/status
4. Write changes

## Output

After all 4 steps:
```
## Wrap-Up Complete
- RPT: RPT-NNN written to reports.md
- Mistakes: <count> recorded (or "Clean session") + <count> dual-written to class
- Wins: <count> recorded (or "Standard execution") + <count> dual-written to class
- State: updated, <N> tasks marked DONE
```

## Constraints

- Execute steps sequentially — RPT must exist before retrospectives reference it
- If any step fails, continue with remaining steps and note the failure
- Dual-write is handled by /mistake and /good-idea (Step 0 class detection). No extra logic needed here
- Class writes are layer 2 only — never write to `shared/global/ltm.md` (reserved for v3 /lt-mem)
