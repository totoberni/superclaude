# Universal Operating Rules

## Read Before Edit
- Always read a file before modifying it
- Understand existing code before suggesting changes
- Check for known issues before debugging:
  - Superclaude: `memory_db.py search '<project> gotchas mistakes'` or `list --tier shared-projects`
  - In-project: `docs/gotchas.md` or `.orchestrator/mistakes.md`

## Minimal Changes
- Only make changes directly requested or clearly necessary
- Don't add features, refactor, or "improve" beyond what's asked
- Don't add comments, docstrings, or type annotations to unchanged code
- Three similar lines > premature abstraction

## Git Discipline
- Conventional commits: feat:, fix:, test:, docs:, chore:, refactor:
- Git commit and push are gated by the `/git` policy toggle, enforced mechanically by `hooks/guards/26-git-policy.sh` (reads `config/git-policy`). `/git false` blocks commit and push for all agents; `/git true` allows them. This is the single git-permission toggle; the owner sets it. See `docs/guard-activation.md`.
- Never create/switch/merge branches without explicit instruction
- One logical commit per unit of work
- WSL permission diffs (`755 → 644`): never commit — see `21-domain-gotchas.md §WSL File Permissions`

## Commit Protocol
- **Format**: `<type>(<optional scope>): <description>` — type is one of: feat, fix, test, docs, chore, refactor, style, ci, perf, build
- **Pre-commit check**: if no tests have run this session, consider running tests before committing
- **Co-author**: all agent commits include `Co-Authored-By: Claude <noreply@anthropic.com>`
- **Hook enforcement**: `hooks/guards/30-commit-gate.sh` is the enforcement point for commit discipline (blocking: mode-only-diff, secret-shaped content; warn: conventional-subject, bulk-add); the `25-commit-gate.sh` module now carries only the push reminder.

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
2. Check project gotchas (`memory_db.py search '<project> gotchas mistakes'` or `list --tier shared-projects`)
3. If a worker, report back to your orch with all 3 attempts documented
4. If an orch, write ESC-NNN to escalations.md with all 3 attempts
5. Never silently retry a 4th time — the pattern indicates a wrong mental model

## Consolidation
When the user says "funziona" or "l'ho implementato":
- Update changelogs and documentation
- Record the solution in relevant gotchas/mistakes file
