# toto Automation Layer: Operator Reference

Standing automation runtime stood up on toto during superclaude-v4-wt (W1-W3): remote-ops wrappers, a self-hosted notification bus, a VPN-gated discovery/browsing stack, a Remote-Control coding plane, a health probe, and the W3 fixtures-only automation engine. This doc is the operator entry point; it does not duplicate the tool conventions or the campaign plan, it cross-references them.

## Table of Contents

1. [Two-Machine Split](#two-machine-split)
2. [Remote Ops](#remote-ops)
3. [ntfy](#ntfy)
4. [Discovery Egress](#discovery-egress)
5. [Remote-Control Coding Plane](#remote-control-coding-plane)
6. [Health Probe](#health-probe)
7. [W3 Engine](#w3-engine)

## Two-Machine Split

WSL (`totob@<this box>`) is the cockpit: agents, plans, comms, memory DB, this doc. toto (host TOTO-HOST, user `toto`, Tailscale IP `100.64.0.0`, CachyOS/Arch, fish login shell) is the always-on automation runtime: ntfy server, discovery stack, Remote-Control sessions, the deployed W3 engine. See `rules/12-agent-hierarchy.md` for the agent hierarchy this split sits under, and `~/.claude/plans/superclaude-v4-wt/plan.md` for the campaign that built it.

## Remote Ops

All toto command execution goes through `~/.claude/bin/tsh` (plain exec) and `~/.claude/bin/tsudo` (root via pam_ssh_agent_auth). Full wrapper contract and setup: `rules/20-tool-conventions.md` § toto Remote Ops.

Load-bearing gotchas, not covered there:
- **One command per call.** tsh/tsudo pass a single command; semicolons escape sudo's scope.
- **fish is the remote login shell.** A `$` inside a remote command string breaks fish parsing, and `{a,b}` / `{{...}}` risk brace expansion. For anything compound or containing `$`/braces, write a bash script, `scp` it over, and run it explicitly with bash: `tsh 'bash /tmp/x.sh'`.
- **scp pattern**: `env SSH_AUTH_SOCK=$HOME/.ssh/agent.sock scp -q FILE toto:/tmp/`.
- Bash-tool calls using tsh/tsudo/scp need `dangerouslyDisableSandbox: true` (ssh + agent socket are outside the sandbox allowlist).

## ntfy

Self-hosted `ntfysh-bin` on toto, bound to toto's Tailscale IP on the ntfy port (both live in the gitignored `config/toto.env`; see `config/toto.env.example`). Tailnet-only; the base URL is a raw IP because MagicDNS is broken on toto (not a hostname you can swap in later without checking).

- **Units**: `ntfy.service` (root, system-level server; check with `systemctl is-active ntfy.service`, no sudo needed to read status) and `ntfy-listener.service` (toto user, runs `ntfy subscribe --from-config` against the four topics below, writes to `~/automations/ntfy/inbox.jsonl`, acks on `abe-alerts`).
- **Health**: `curl -s http://$TOTO_TAILSCALE_IP:$NTFY_PORT/v1/health` (values from `config/toto.env`) returns `{"healthy":true}`.
- **Topics** (four separate subscriptions, each with a message-ID prefix):

  | Topic | Prefix |
  |-------|--------|
  | `abe-jobsearch` | `j-` |
  | `abe-phd` | `phd-` |
  | `abe-papers` | `p-` |
  | `abe-alerts` | server -> phone alerts |

- **Credentials**: `~/automations/ntfy/credentials` on toto, mode 0600, key=value (`url`/`user`/`password`/`token`). Location only; never print or transcribe the values into any doc, log, or health-check output.
- **iOS Option 2**: `/etc/ntfy/server.yml` sets `upstream-base-url: https://ntfy.sh` so iOS gets APNs push. Only topic hashes transit ntfy.sh; message bodies stay on toto.
- **Invariant**: ntfy push is ephemeral. The durable source of truth is each automation's own queue plus questionnaire tables on toto; a missed push is non-lossy (168h server-side cache, state-based daily digest, `awaiting_input` persistence). Never build logic that assumes a push was delivered.

## Discovery Egress

`~/automations/discovery/` on toto (dir mode 0700): `docker-compose.yml`, `.env` (0600, holds the WireGuard private key + server country list; location only, never the key), `.env.example`, `README.md`.

Stack: `gluetun` (container `discovery-gluetun`, NordVPN over WireGuard/NordLynx) fronting a Playwright container (`discovery-browser`, `network_mode: service:gluetun`), so the browser can egress only through the VPN. WireGuard was chosen over OpenVPN (fewer `AUTH_FAILED` failures); the official nordvpn-linux client was rejected (its split-tunnel conflicts with the Tailscale kill-switch). The Playwright server listens loopback-only (`127.0.0.1:9222`) inside gluetun's network namespace.

- **Bring-up**: `docker compose --project-directory ~/automations/discovery -f ~/automations/discovery/docker-compose.yml up -d`
- **Verify**: `docker ps --filter name=discovery-gluetun --filter health=healthy -q` (non-empty = VPN healthy), `docker ps --filter name=discovery-browser --filter status=running -q` (non-empty = browser up). Confirmed browser egress resolves to a Nord NL IP, host egress stays on the home IP, and Tailscale is unaffected.
- **Kill-switch**: fail-closed by construction; the browser has no network path except through gluetun, so a dead VPN tunnel means no browser egress, not a leak.
- Standing invariant: anonymous browser sessions only, never log into anything on this path, ToS-respecting.

## Remote-Control Coding Plane

`~/automations/bin/rc-project <name> <dir>` launches one `claude --remote-control <name>` per project inside its own tmux session `rc-<name>`, each bound to a **separate** working directory (never share a directory across sessions).

Live sessions: `rc-automations` (dir `~/automations`) and `rc-superclaude` (dir `~/.claude`). Health check: `tmux has-session -t rc-automations` (exit 0 = alive). A healthy pane shows `/remote-control is active` and the statusline shows `/rc active`.

**Classifier-naming note**: the Code-tab display name is derived from the first prompt sent to the session, not from the `--remote-control` argument; each session was seeded with an identity prompt so the tab label is legible.

### RE-AUTH Procedure (R-WT-6)

The toto Claude login expires roughly weekly and 401s, which silently kills **both** toto inference and Remote Control at once.

1. Detect: an RC pane shows `Remote Control failed to connect: /login` or `API Error: 401`. This is the cheap health proxy; do not run a full inference probe as a routine check, it is unnecessarily costly.
2. Re-auth on toto: `/login` -> choose the **subscription** option (never the API-billing option; the subscription path is the account-safety-correct one for Abe's Max plan).
3. Open the OAuth URL, have the owner authorize it, paste the resulting code back into the prompt.
4. Confirm: RC panes clear of `failed to connect` / `401`.

## Health Probe

`~/.claude/scripts/automations-health.sh` checks, per subsystem: ntfy `/v1/health`, `ntfy.service` + `ntfy-listener.service` unit status, the discovery gluetun/browser container health filters above, the `rc-*` tmux sessions, and the presence of the deployed W3 engine directory on toto. It is a status probe, not a fixer, and is the basis for the W7 watchdog (proactive re-login reminder + `abe-alerts` paging on 401/dark-toto).

## W3 Engine

Developed on WSL at `~/automations/engine-build/` (home level, alongside `~/.claude`, never inside it); deployed to toto `~/automations/engine-build/`. The automation CODE (engine, `bin/rc-project`, `ntfy/`, `discovery/` compose) is version-controlled on the `automations` branch of the superclaude repo; SSOT, documents, `.env`, credentials, and runtime data are gitignored and never leave toto. Runtime venv on toto is `~/automations/.venv` (Python 3.14, pyyaml + pytest). Current build is fixtures-only (v1, no live network calls) with 54 passing tests on both machines.

Run the suite on toto from the engine directory:

```bash
~/automations/.venv/bin/python -m pytest tests/ -q
```
