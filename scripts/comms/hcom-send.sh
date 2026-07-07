#!/usr/bin/env bash
# hcom-send.sh - shared HCOM dual-write helper.
#
# Extracted verbatim from the hcom_send() shell function pasted inline in:
#   - skills/handoff/SKILL.md  section "HCOM Dual-Write (Phase B)"
#   - skills/nudge/SKILL.md    section "HCOM Dual-Write (Phase B)"
# Both copies were confirmed byte-identical at extraction time (2026-07-07),
# no drift found. See checkpoint w1.3-comms-scripts.md for the comparison.
#
# Usage (sourced, function-call style, matches the original inline helper):
#   source /home/totob/.claude/scripts/comms/hcom-send.sh
#   hcom_send "$from_agent" "$to_agent" "$kind" "$seq" "$body"
#
# Usage (executed directly, same positional args, no sourcing required):
#   /home/totob/.claude/scripts/comms/hcom-send.sh <from_agent> <to_agent> <kind> <seq> <body>
#
# Args (identical to the original inline function signature):
#   from_agent - message sender identity
#   to_agent   - message recipient identity ("@name" for one agent, "*" for broadcast)
#   kind       - DIR, RPT, ESC, NUDGE, or EVENT
#   seq        - directive/report number, optional (pass "" to omit)
#   body       - message body text
#
# Behavior: this helper is the SQLite mirror only. The caller is assumed to
# have already completed the canonical flat-file write, per the dual-write
# pattern (flat-write first, SQLite-write second, fail-soft). Broker errors
# are logged to stderr and are never fatal to the caller.

hcom_send() {
  # args: from_agent  to_agent  kind  seq(optional)  body
  local from="$1" to="$2" kind="$3" seq="$4" body="$5"
  "$HOME/.claude/.venv/bin/python" "$HOME/.claude/scripts/hcom-broker.py" send \
    --from "$from" --to "$to" --kind "$kind" \
    ${seq:+--seq "$seq"} \
    --body "$body" 2>/dev/null \
    || echo "Warning: HCOM send failed (broker unavailable)" >&2
}

# When executed directly (not sourced), forward the CLI args to the function.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  hcom_send "$@"
fi
