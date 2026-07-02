# automations

Automation code that runs on toto (superclaude v4-wt). This is the `automations`
branch of the superclaude repo: **code only**. All secrets, SSOT identikits,
documents, the living pre-PhD plan, and runtime data live on toto and never
travel here (gitignored; invariants WT-2 / WT-3).

## Layout

| Path | What |
|------|------|
| `engine-build/` | Shared job / PhD / paper automation engine (fixtures-only v1, 54 tests). |
| `bin/rc-project` | Remote-Control coding-plane launcher (one `claude --remote-control` per project). |
| `ntfy/handle.sh` | ntfy free-text command handler (W1 skeleton; W5 replaces the body). Reads its base URL + token from the 0600 credentials file, never hardcoded. |
| `discovery/` | VPN-gated (gluetun / WireGuard) Playwright discovery-egress stack (compose + `.env.example`). |

## Deployment

Runs on toto under `~/automations/`. Machine-specific connection params (the
Tailscale IP, ntfy port) live in `~/.claude/config/toto.env` (gitignored on the
`main` branch). Operator reference: `~/.claude/docs/toto-automations.md`.

## Engine tests

From `~/automations` on toto:

```bash
~/automations/.venv/bin/python -m pytest engine-build/tests/ -q
```
