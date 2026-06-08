# cockpit — WSL→peer memory-sync + SSH wrapper

Runs `claude_mem_sync.py` to reconcile the WSL and peer memory DBs, then opens an SSH session to peer. The SSH login is **never gated** on the sync result — skip, conflict-abort, or error all proceed to login anyway. If peer is unreachable the sync tool prints `SKIP` and exits 0 immediately (it has its own `ConnectTimeout`), so the wrapper is safe to keep installed even when peer is offline.

### What this is (and isn't)

- **Memory only.** This reconciles the `.memory.db` rows via the `memory_db.py` API. It does **not** touch git and does not move code. (Code is a separate flow: authored-on-peer → WSL pulls from peer over the same one-way ssh → WSL pushes to GitHub.)
- **One-way channel, by design.** The whole exchange runs over the **WSL→peer** ssh that this wrapper opens. peer never connects back to WSL — so a compromised peer cannot reach your laptop. "Pulling peer's memories to WSL" is data flowing over the *WSL-initiated* connection, not peer dialling out.
- **Eventual consistency — one login behind.** The sync runs *before* the interactive session. So edits you make **on peer during a session** propagate to WSL only on your **next** `cockpit` login (which reconciles both ways before the session opens). If the same memory was also edited on WSL in between, you'll get the conflict dialogue — no data is lost, you just choose.

> **⚠ GATE — do NOT install into the login path yet.**
> Conflict UX review (campaign gate **HG-9**) is not signed off. Until owner clears HG-9, invoke `cockpit` **manually** when you want a synced session.

## Setup

| Shell | Install |
|-------|---------|
| **fish** | Add `source ~/.claude/scripts/cockpit/cockpit.fish` to `~/.config/fish/config.fish`, **or** symlink: `ln -s ~/.claude/scripts/cockpit/cockpit.fish ~/.config/fish/functions/cockpit.fish` |
| **bash** | Add `source ~/.claude/scripts/cockpit/cockpit.bash` to `~/.bashrc` |

## Usage

| Command | Effect |
|---------|--------|
| `cockpit` | Interactive sync (prompts on conflicts), then SSH |
| `cockpit --auto newer` | Non-interactive sync (auto-resolves to newer side), then SSH |
| `cockpit --dry-run` | Preview sync diff without mutating either DB, then SSH |

At each conflict the dialogue shows a git-style diff (`−` = WSL/local, `+` = peer/remote) and prompts: **`w`** keep WSL · **`t`** keep peer · **`b`** keep both (renames one) · **`s`** skip (re-prompts next run) · **`q`** quit (applies nothing).
