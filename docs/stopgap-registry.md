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

## Maintenance (via `/better-super --update`)

- Wave 1 CHECK scans each row's upstream repo for new releases/patterns since last mined (mined-at date tracked in `mining-candidates.md`).
- After the human gate pick, Wave 2 APPLY re-mines the delta and updates the row's `--update` action with what was applied and the date.
- When an original is fully internalized and retired, mark the row DONE (with date) or move it to a "Retired" section here.
