# Stopgap Registry

Tools/patterns MINED into superclaude whose upstream ORIGINAL is still in use as a dependency or reference. `/better-super --update` walks this registry to catch each mined feature up to its upstream's current state, then (per the action column) works toward retiring the original.

This is the living source of truth. It was seeded from the superclaude-v3 campaign plan; `/better-super --update` Wave 2 maintains it going forward (the v3 plan's T7.3 finalizes the initial content).

| Stopgap (upstream original) | Mined into (superclaude) | `--update` action |
|---|---|---|
| wakamex/ccusage (quota %) | unified telemetry reader (quota via stdin `rate_limits`) | pull latest, then retire the dependency |
| ryoppippi/ccusage (cost) | unified telemetry reader (cost via local JSONL) | pull latest v20.x, then internalize |
| overleaf-mcp (explain_log / FloatBarrier / hyperref-order) | LaTeX compile hook (`latex-warn.sh`) | re-check for new static checks to mine |
| pedrohcgs (passport.yaml provenance) | experiment harness | re-check for new gate patterns |
| ralph-orchestrator (agent waves) | `/swarm-dispatch` scheduling | re-check parallel-scheduling design |
| superpowers (inline self-review) | reviewer topology | re-check for review-loop improvements |
| sst/opencode (multi-provider terminal REPL) | v4 console/REPL spine (FORK base) | fork base — track releases, re-sync during v4 build |
| jlowin/fastmcp (Python MCP server + middleware) | v4 asset-bridge (memory/comms/skills as MCP) | dep — track releases; mine middleware-hook patterns |
| BerriAI/litellm (multi-provider router/proxy) | v4 provider allocation/routing | dep — track releases; re-check routing/cost features |
| google-gemini/gemini-cli (headless Gemini agent) | v4 Gemini provider (full-agent path) | track releases; re-check headless/MCP surface |
| modelcontextprotocol/servers (filesystem/shell/git MCP) | v5 OS-control API (reference impls) | re-check for new OS-control servers to mine |
| nats-io/nats-server (JetStream bus) | v5 distributed comms bus + append-only audit | track releases; re-check JetStream/audit patterns |
| civicteam/mcp-hooks (passthrough MCP proxy) | v4 hook-translation (cross-client) | re-check for interceptor patterns |
| pewdiepie-archdaemon/odysseus (opencode-embedding workspace) | v4 reference (multi-provider workspace) | re-check for new integration patterns |
| open-webui/open-webui (Pipelines middleware) | v4 reference (hook/middleware patterns) | re-check Pipelines/filter patterns |
| danny-avila/LibreChat (config-driven provider routing) | v4 reference (provider abstraction) | re-check provider-routing/MCP patterns |
| onyx-dot-app/onyx (multi-provider RAG/connectors) | v4 reference (connector abstraction) | re-check connector/retrieval patterns |
| ollama/ollama (local model server) | v4 local-agent serving (dep) | track releases; re-check Intel/CPU backend state |
| a2aproject/A2A (agent-to-agent protocol) | v5 agent-to-agent comms (reference) | track spec releases; re-check A2A maturity |

<!-- v4-v5 foundation rows added 2026-06-08 (superclaude-v4v5 research campaign). pip/binary deps (fastmcp, litellm, ollama, nats) enter dependencies.yml during the v4 BUILD, not in this mostly-read phase. -->

## Maintenance (via `/better-super --update`)

- Wave 1 CHECK scans each row's upstream repo for new releases/patterns since last mined (mined-at date tracked in `mining-candidates.md`).
- After the human gate pick, Wave 2 APPLY re-mines the delta and updates the row's `--update` action with what was applied and the date.
- When an original is fully internalized and retired, mark the row DONE (with date) or move it to a "Retired" section here.
