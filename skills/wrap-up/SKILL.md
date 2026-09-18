---
name: wrap-up
description: "Use when bundling post-work outcome, mistake, good-idea, and recovery."
category: workflow
user-invocable: true
argument-hint: "<project>"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# /wrap-up: Post-Work Bundle

Bundles the post-work ceremony into one command: outcome record -> `/mistake` -> `/good-idea` -> `/remember --save` -> state. Prevents the failure mode where a runner finishes work but forgets retrospectives or the recovery snapshot. Meta is the runner. `/remember --save` is invoked as an explicit named step (Step 4), so this one skill is the complete protocol; the operator never has to remember to run the recovery-save separately.

**Project**: $ARGUMENTS

## Procedure

Execute steps in order (each depends on the previous).

### Step 1: Record the Outcome

1. Identify the plan: `~/.claude/plans/$ARGUMENTS/plan.md`
2. Mark the relevant task(s)/section(s) DONE in `plan.md` (status field or checkbox, per the plan's existing convention)
3. Re-render the human view: `scpy plan/render_plan.py ~/.claude/plans/$ARGUMENTS/plan.md`
4. Note a 2-3 line outcome summary (what was delivered, DONE/PARTIAL/BLOCKED, follow-up) for reuse in Step 4's recovery snapshot

### Step 2: Run /mistake

Invoke the `/mistake` skill for `$ARGUMENTS`:
- Review the session for errors, retries, failed approaches
- Memory is DB-only: never hand-write a `.md` memory file
- Search first (`mem search`) to check for an existing entry; prefer updating over adding a duplicate
- **Primary**: upsert to the DB (`shared-projects` tier, `--agent $ARGUMENTS`)
- **Dual-write**: class-applicable mistakes also go to DB `class` tier (see /mistake Step 0)
- If no mistakes found, note "Clean session" and move on

### Step 3: Run /good-idea

Invoke the `/good-idea` skill for `$ARGUMENTS`:
- Review the session for wins, effective patterns, good decisions
- Memory is DB-only: never hand-write a `.md` memory file
- Search first to check for an existing entry; prefer updating over adding a duplicate
- **Primary**: upsert to the DB (`shared-projects` tier, `--agent $ARGUMENTS`)
- **Dual-write**: class-applicable wins also go to DB `class` tier (see /good-idea Step 0)
- If no notable wins, note "Standard execution" and move on

### Step 4: /remember --save (recovery snapshot) + state

1. **Invoke `/remember --save`** explicitly (this step exists so the operator never has to type it separately). Per the `/remember` skill's `--save` mode: search first for the existing `meta-recovery-context` (or campaign-specific) slug to update rather than duplicate, then `mem upsert --tier instance --type user --name <slug> --description "... (<YYYY-MM-DD HH:MM>)" --agent meta --text-file ~/.claude/tmp/<slug>.md` with the Step 1 outcome summary folded into the recovery body (directive/progress/current-task/uncommitted-work/next-steps/key-findings, per `25-context-management.md` § Stash Procedure). Always author the body as a file with the Write tool and pass it via `--text-file`; never inline the body on the command line.
2. Update the master `~/.claude/plans/$ARGUMENTS/state.md`: mark completed tasks DONE, update current phase/status

## Output

After all steps:
```
## Wrap-Up Complete
- Outcome: plan.md tasks marked DONE + plan.html re-rendered
- Mistakes: <count> recorded (or "Clean session") + <count> dual-written to class
- Wins: <count> recorded (or "Standard execution") + <count> dual-written to class
- Recovery (/remember --save): recovery memory upserted (slug, <YYYY-MM-DD HH:MM>)
- State: master state.md updated, <N> tasks DONE
```

## Constraints

- Execute steps sequentially: the outcome record must exist before retrospectives reference it
- If any step fails, continue with remaining steps and note the failure
- Dual-write is handled by /mistake and /good-idea (Step 0 class detection). No extra logic needed here
- Class writes are layer 2 only, never write to the `shared-global` tier directly. Use `/lt-mem` for promotions to global
- Never put PII in a memory body; keys, tokens, personal data belong nowhere in the DB
- After completing a multi-session campaign (3+ directives), consider running `/lt-mem --quick <project>` as a final consolidation step
</content>
