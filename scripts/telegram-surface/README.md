# Telegram factory surface v0 — RETIRED 2026-07-02

**RETIRED (superclaude-v4-wt W1, WT-4): replaced by self-hosted ntfy on toto behind
Tailscale.** HG-F3 is moot: no token was ever created, the surface never went live.
Kept in-tree as a pattern library only: AllowlistMiddleware (sender gate), fail-closed
env loading, and the read-only SQLite surfacing in `surfaces.py` are reused by the ntfy
dispatcher. Do not enable; do not create a bot token.

Read-only remote visibility into the superclaude factory (plan superclaude-v4, F3).
**OFF by default** — no token exists until owner clears HG-F3
(security packet: `~/.claude/docs/telegram-surface-security.md`).

## Setup

1. Create a bot via @BotFather; get your numeric user ID via @userinfobot.
2. `cp .env.example .env`, fill both values, `chmod 600 .env`.
3. Run once interactively to verify: `~/.claude/.venv/bin/python bot.py`
4. Install the systemd user unit (commands at the top of `telegram-surface.service`).
   For the bot to survive logout: `loginctl enable-linger user` (WSL note: Windows
   still suspends WSL when no session is open — keep a terminal open or configure
   a Windows-side keep-alive if 24/7 uptime is wanted).

## Usage (whitelisted commands only)

| Command | Source |
|---|---|
| `/status` | live sessions (`~/.claude/session-timers/` + PID check) + latest RPT per agent (`comms/.broker.db`) |
| `/plans` | active `plan-index-*` cards (`agent-memory/.memory.db`) |
| `/report <agent>` | latest full RPT body from that agent, truncated to fit Telegram |
| `/help`, `/start` | command list |

## Configuration

| Var | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather token. Missing → bot exits (fail-closed). |
| `ALLOWED_USER_IDS` | comma-separated numeric IDs. Empty → bot exits (fail-closed). |

Runtime choice: systemd `--user` unit (verified `running` on this WSL 2026-06-12);
no run-loop fallback script needed.
