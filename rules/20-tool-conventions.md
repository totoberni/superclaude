# Tool Conventions

Universal tool-usage patterns learned from past mistakes. Applies to ALL agents.

## Single Source of Truth Across Tool Boundaries

- When a value can be computed in two places (bash vs Python, template vs runtime config, client vs server), pick ONE site as canonical. The other must consume the canonical value directly or not exist.
- **Never mirror a computation for "early failure detection", "clarity", or "documentation"** — the mirror will drift. Sometimes silently (when both sites happen to agree), sometimes catastrophically.
- Classic case: example-project `main.py` constructs `ckpt_path = results_base_folder / dataset / model / exp_name` and creates it via `os.makedirs`. Job scripts that add a bash `CKPT_DIR` variable to pre-create the path "for early permission failure detection" inevitably diverge — and when they do, bash creates a ghost directory and Python writes checkpoints to a different location. 3 occurrences across 4 weeks cost nearly a full training run.
- Rule: if you catch yourself writing a bash formula that mirrors an application-side formula, STOP. Delete the bash mirror, let the application-side error surface at runtime with a clear traceback, fix the root cause (social — chmod/permissions — or structural — choose a distinctive leaf name to avoid the collision).
- Source: G-25 (example-project M-9 3x, session 33 retrospective).

## Superclaude ↔ Local Codebase Firewall

Bidirectional isolation rule — meta-tier agent memory must stay invisible from inside any user project.

- **Local codebase files** (`*.sh`, `*.md`, `*.py`, `*.tex`, `*.cpp`, etc. anywhere under `~/projects/*`) must NOT reference superclaude-internal artefacts by name. Forbidden patterns inside project files include:
  - File/path: `~/.claude/`, `.claude/rules`, `agent-memory`, `shared/projects/`, `class/meta`, `MEMORY.md`, `mtm.md`, `ltm.md`, any project memory filename (`example-project.md`, `example-project.md`, etc.)
  - Superclaude memory/comms artefacts (MD or DB): `.memory.db`, `.comms.db`, `.broker.db`, `memory_db.py`, `comms_db.py`
  - Identifiers: `M-\d+`, `MM-\d+`, `GM-\d+`, `G-\d+`, `MT-\d+`, `CW-\d+`, `W-\d+` when used as agent-memory cell references (NOT when they are local section IDs like reprod-notes.md's own `C1`/`B4` etc. — those stay).
  - Phrases: "meta says", "memory.md says", "according to the gotchas file", "see the project memory".
- **Before any write to a local project file**, grep the draft for the forbidden patterns and strip them. Replace by:
  - (a) a local file reference if the content exists locally (`docs/reprod-notes.md §C4`), or
  - (b) inline paraphrase with no meta-structure reference.
- **Superclaude memory files** (`~/.claude/agent-memory/**/*.md`, `~/.claude/rules/**/*.md`) MAY freely reference local project paths and content. The flow is one-way: meta reads local, local does not read meta.
- **Why**: a teammate cloning the project repo, a reviewer reading a paper submission, or a future-you on a different machine sees the local files only. References to meta files resolve to nothing and leak internal tooling.
- Source: G-27 (example-project S33 retrospective, 4 contaminated files stripped).

## toto Remote Ops (`tsudo` / `tsh`)

- Root or plain commands on toto go through the wrappers, never the raw ssh incantation: `~/.claude/bin/tsudo <cmd>` (root via pam_ssh_agent_auth, keyed to the WSL agent) and `~/.claude/bin/tsh <cmd>` (plain exec; no args = interactive session). Both degrade gracefully when run ON toto (same name on both machines), preflight the agent with a self-describing fix message, and carry a timeout guard (`TSUDO_TIMEOUT`/`TSH_TIMEOUT`).
- NEVER pass `-n` to sudo on this channel: `sudo -n` bypasses pam_ssh_agent_auth and fails with "a password is required".
- Bash-tool calls still need `dangerouslyDisableSandbox` (ssh + agent socket). Long remote operations: launch remote-persistent (`tsudo nohup ... &` with a remote log), never tied to the local session. Full pattern + setup: memory `toto-passwordless-sudo-agent-pattern`.
- **SKM (Session Key Manager, 2026-07-05): toto access is now per-session EPHEMERAL, not one long-lived key.** The wrappers auto-route through `~/.claude/bin/skm` (`skm ensure` mints on demand) whenever `~/.ssh/skm/ENABLED` exists; no agent action needed. Each meta/orch session (SessionStart hook `mod_skm_session`) mints a fresh keypair, signs a short-TTL CA cert for **login** (sshd `TrustedUserCAKeys`), and registers an ephemeral **raw** key for **sudo** (pam_ssh_agent_auth cannot parse certs, verified; see memory `pam-ssh-agent-auth-no-cert-support-toto`) via the crypto-gated root helper `/usr/local/sbin/skm-authorize` (NOPASSWD, but refuses any key not backed by a valid CA cert). SessionEnd revokes; a toto `skm-prune.timer` + cert TTL are the backstops. The long-lived `totoserver` key is **decommissioned from sudo** (no standing root) and retained only as break-glass **login**. The one long-lived secret is the CA private key `~/.ssh/skm-ca` (WSL-only, 0600): back it up offline; its loss falls back to break-glass login + console. Manual verbs: `skm status|doctor|gc`, `sudo skm-authorize list|prune`. Full design: memory `skm-session-key-manager-toto`.

## Git with `-C`

- `git -C <dir>` sets the repo working directory. All pathspecs after it are **relative to the repo root**.
  - WRONG: `git -C /path/to/repo checkout --ours /path/to/repo/file.txt`
  - RIGHT: `git -C /path/to/repo checkout --ours file.txt`
- Always use the **full absolute path** for `-C` (e.g., `git -C $HOME/projects/workspace/example-enterprise-app`). Never use relative paths.

## Remote-Only Branches

- Many project branches exist only as remotes (never checked out locally).
- Before referencing any branch name, run `git -C <repo> branch -a` to confirm whether it's local or remote-only.
- Use `origin/` prefix for remote-only branches (e.g., `origin/codex/live` not `codex/live`).

## Parallel Tool Batches

- Claude Code cancels ALL sibling calls in a parallel batch if ANY single call errors. Applies to ALL tool types (Bash, Read, Grep, etc.), not just Bash.
- Never group uncertain calls (files that might not exist, ref lookups, commands that might fail) with safe calls in the same parallel batch.
- Common trap: a call that may fail — a `memory_db.py search` before the venv/DB is confirmed, a ref lookup, a maybe-missing file — run it separately, not batched with safe calls.
- Pattern: run safe discovery calls first, then use results to construct the next batch.

## Shell `!` Mangling in Content/DB Writes (Claude Code bash tool)

- The bash tool inserts a stray backslash before `!` (history-expansion-style escaping, byte `0x5C` before `0x21`) inside command strings, observed in BOTH double-quoted commands AND `<<'EOF'` quoted heredocs. Any string written THROUGH bash that contains `!` is silently corrupted: SQLite `UPDATE ... SET text='...!...'`, HTML-comment markers (`<!-- ... -->`), regexes, file content.
- **Self-masking trap**: a verification predicate typed in the SAME bash command receives the SAME mangling, so it appears to match the corrupted value. NEVER verify a `!`-bearing write with a bash-typed `LIKE`/grep predicate. Verify the raw bytes, e.g. `hex(substr(text,1,N))`: clean `<!--` is `3C212D2D`; a `5C` after the leading `3C` means mangled.
- **Rule**: to write a content string containing `!` (or other history-expansion-sensitive chars) into the memory DB, comms, or any file, do NOT build it in a bash command. Write a small Python file with the **Write tool** (exact bytes, no shell layer) and run it with `~/.claude/.venv/bin/python` using a parameterized query. This is load-bearing across superclaude v3: every agent that edits memories transversely (`memory_db.py` content, `<!-- budget-exempt -->` markers, recovery contexts) or comms can hit this.
- Source: meta 2026-06-13 (a `<!-- budget-exempt -->` marker on a memory row was stored as `<\!--`, defeating the line-anchored exempt predicate; cost 3 attempts incl. a quoted heredoc). Memory: `gotcha-bash-tool-bang-mangling` (shared-global tier).

## Merge Conflicts

- `git merge` returns exit code 1 when there are conflicts. This is expected workflow, not a failure.
- Proceed to conflict resolution (manual or via w-merger agent). Don't treat as a retry-able error.
- After resolution, always verify with `git -C <repo> diff --check` (detects leftover conflict markers).

## Worktree Hygiene

- **Meta** creates and deletes worktrees. **Orchs** keep them clean during use.
- **Parallel orchs in the same repo MUST use separate worktrees.** Two orchs doing `git checkout -b` in the same working directory causes a checkout race — the second checkout changes HEAD, and the first orch's next commit lands on the wrong branch. This happened twice (M-001, <project>) and required cherry-pick + force-push to fix. Meta must include worktree setup in directives when dispatching parallel orchs to the same repo.
- NEVER run generative scripts (`scan.py`, `render_html.py`, architecture pipelines) in worktrees — they produce large generated files (`model.json`, `diagram.mmd`, `index.html`) and stale `__pycache__` with worktree-specific paths.
- Stale `.pyc` from worktree runs causes 30+ spurious test isolation failures that look like real bugs.
- Before running tests in a worktree, if unsure of cleanliness: `find . -name "__pycache__" -type d -exec rm -rf {} +`

## Empirical Verification Before Prescribing

- Before writing a root-cause analysis or fix directive, run a diagnostic command that confirms the hypothesis. Never prescribe from structural reasoning alone.
- For code that constructs file paths from config/args: substitute the ACTUAL runtime values through the construction logic and verify the resulting path string. "It uses `os.path.join`" is not verification.
- Source: GM-1 (4 occurrences across <PROJECT>, VPS, <PROJECT>)

## Domain-Specific Gotchas

Moved to `21-domain-gotchas.md` (auto-loaded next). HDL/Verilog/Synopsys, Python namespace, Compose/Docker, WSL file permissions, large image handling.
