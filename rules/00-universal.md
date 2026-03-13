# Universal Operating Rules

## Read Before Edit
- Always read a file before modifying it
- Understand existing code before suggesting changes
- Check for known issues before debugging:
  - Superclaude: `~/.claude/agent-memory/shared/projects/<project>.md` (Gotchas section)
  - In-project: `docs/gotchas.md` or `.orchestrator/mistakes.md`

## Minimal Changes
- Only make changes directly requested or clearly necessary
- Don't add features, refactor, or "improve" beyond what's asked
- Don't add comments, docstrings, or type annotations to unchanged code
- Three similar lines > premature abstraction

## Git Discipline
- Conventional commits: feat:, fix:, test:, docs:, chore:, refactor:
- Never git push — the user decides when to push
- Never create/switch/merge branches without explicit instruction
- One logical commit per unit of work
- WSL permission diffs (`755 → 644`): never commit — see `20-tool-conventions.md §WSL File Permissions`

## Security
- No secrets in code (check .env, credentials before committing)
- No command injection, XSS, SQL injection
- Validate at system boundaries only

## Stop Conditions
Stop and ask the user if:
- Impact or blast radius unclear
- Architecture or data model change without plan
- Destructive or irreversible operation
- Requirements ambiguous or conflicting
- SOT or sync direction unclear

## Escalation on Repeated Failure

If you fail at the same task 3 times with different approaches:
1. Stop trying the same category of solution
2. Check project gotchas (`~/.claude/agent-memory/shared/projects/<project>.md`)
3. If a worker, report back to your orch with all 3 attempts documented
4. If an orch, write ESC-NNN to escalations.md with all 3 attempts
5. Never silently retry a 4th time — the pattern indicates a wrong mental model

## Consolidation
When the user says "funziona" or "l'ho implementato":
- Update changelogs and documentation
- Record the solution in relevant gotchas/mistakes file
