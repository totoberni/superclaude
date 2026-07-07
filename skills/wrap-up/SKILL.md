---
name: wrap-up
description: "Post-work bundle: record outcome + /mistake + /good-idea + /remember --save + state/recovery"
category: workflow
user-invocable: true
argument-hint: "<project>"
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# /wrap-up: Post-Work Bundle

Bundles the post-work ceremony into one command: outcome record -> `/mistake` -> `/good-idea` -> `/remember --save` -> state. Prevents the failure mode where a runner finishes work but forgets retrospectives or the recovery snapshot. Meta is the primary runner (v3); orch is a legacy branch used only for the rare multi-hour handoff. `/remember --save` is invoked as an explicit named step (Step 4), so this one skill is the complete protocol; the operator never has to remember to run the recovery-save separately.

**Project**: $ARGUMENTS

## Procedure

Execute steps in order (each depends on the previous).

### Step 1: Record the Outcome

Branch by runner.

**Meta (primary)**:
1. Identify the plan: `~/.claude/plans/$ARGUMENTS/plan.md`
2. Mark the relevant task(s)/section(s) DONE in `plan.md` (status field or checkbox, per the plan's existing convention)
3. Re-render the human view: `~/.claude/.venv/bin/python ~/.claude/scripts/plan/render_plan.py ~/.claude/plans/$ARGUMENTS/plan.md`
4. Note a 2-3 line outcome summary (what was delivered, DONE/PARTIAL/BLOCKED, follow-up) for reuse in Step 4's recovery snapshot
5. Meta writes no `reports.md` entry; it has no comms directory of its own

**Orch (legacy, rare)**:
1. Read `~/.claude/comms/<your-orch>/reports.md` to find the next RPT number
2. Summarize the directive outcome (DIR ref + title, what was delivered, status, follow-up)
3. Write `## RPT-NNN` to `reports.md`:
   ```markdown
   ## RPT-NNN
   **Time**: <YYYY-MM-DD HH:MM>
   **Directive**: DIR-NNN, <title>
   **Status**: DONE | PARTIAL | BLOCKED
   **Summary**: <2-3 lines>
   **Deliverables**: <list>
   **Follow-up**: <if any>
   ```

### Step 2: Run /mistake

Invoke the `/mistake` skill for `$ARGUMENTS`:
- Review the session for errors, retries, failed approaches
- Memory is DB-only: never hand-write a `.md` memory file
- Search first (`memory_db.py search`) to check for an existing entry; prefer updating over adding a duplicate
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

Branch by runner.

**Meta (primary)**:
1. **Invoke `/remember --save`** explicitly (this step exists so the operator never has to type it separately). Per the `/remember` skill's `--save` mode: search first for the existing `meta-recovery-context` (or campaign-specific) slug to update rather than duplicate, then `memory_db.py upsert --tier instance --type user --name <slug> --description "... (<YYYY-MM-DD HH:MM>)" ...` with the Step 1 outcome summary folded into the recovery body (directive/progress/current-task/uncommitted-work/next-steps/key-findings, per `25-context-management.md` § Stash Procedure). Use `--text-stdin` with a Write-tool-authored file when the body contains `!` or other shell-history-sensitive chars (rules/20 § Shell `!` Mangling).
2. Update the master `~/.claude/plans/$ARGUMENTS/state.md` only if no orchs are currently active on that plan; otherwise leave it, orchs own their own state files while running
3. If memory changed and a peer is live, converge it: `claude_mem_sync.py --peer-host <peer> --auto newer --yes` (non-interactive agent path)

**Orch (legacy, rare)**:
1. Read your state file (`~/.claude/plans/$ARGUMENTS/state-<your-orch>.md` or `state.md`)
2. Mark completed tasks DONE, update current phase/status, write changes

## Output

After all steps:
```
## Wrap-Up Complete
- Outcome: plan.md tasks marked DONE + plan.html re-rendered (meta) | RPT-NNN written (orch)
- Mistakes: <count> recorded (or "Clean session") + <count> dual-written to class
- Wins: <count> recorded (or "Standard execution") + <count> dual-written to class
- Recovery (/remember --save): recovery memory upserted (slug, <YYYY-MM-DD HH:MM>) + peer-synced
- State: master state.md updated (meta, no orchs active) | state-<X>.md updated, <N> tasks DONE (orch)
```

## Constraints

- Execute steps sequentially: the outcome record must exist before retrospectives reference it
- If any step fails, continue with remaining steps and note the failure
- Dual-write is handled by /mistake and /good-idea (Step 0 class detection). No extra logic needed here
- Class writes are layer 2 only, never write to the `shared-global` tier directly. Use `/lt-mem` for promotions to global
- Never put PII in a memory body; keys, tokens, personal data belong nowhere in the DB
- After completing a multi-session campaign (3+ directives), consider running `/lt-mem --quick <project>` as a final consolidation step
</content>
