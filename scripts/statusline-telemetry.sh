#!/usr/bin/env bash
# statusline-telemetry.sh — unified Claude Code statusline telemetry reader (superclaude v3)
#
# CONSUMER half of a producer/consumer pair. Aggregates and renders, on every CC
# statusline render, a claude-hud-grade responsive multi-line telemetry display.
#
# SEGMENTS (registry-driven; see SEGMENTS list in python below):
#   T1 (base — never dropped, abbreviated instead) — packed left->right in this
#   order: USAGE STATS FIRST, then CWD+Git, then Model:
#     CTX     context bar w/ autocompact buffer  (stdin.context_window)
#     5h/7d   quota bars                          (stdin.rate_limits)
#     Costs   $day·$week·$month·$total·€<lifetime-sub>·~<1d>·<7d>·<30d>  (~/.claude/.cost-cache.json)
#             (in-column EUR, no box-sep, no "sub" label, no value-ratio;
#             the ~X5·X5·X5 tail = self-calibrated tier advice for 1d/7d/30d horizons)
#     dir+git cwd tail (yellow) + repo/branch (pink)  (stdin.cwd + git subprocess)
#   T2 (recommended — dropped before T1):
#     model   display name + effort               (stdin.model + stdin.effort)
#   T3 (optional — dropped first): tok / burn.
#
# SUBAGENT MONITOR (own dedicated row — NEVER inlined with the stats):
#   Disk-based liveness from the per-subagent transcript files that CC writes at
#   ~/.claude/projects/<SLUG>/<SESSION>/subagents/agent-<id>.{meta.json,jsonl}
#   where <SESSION> = stdin.session_id and <SLUG> = stdin.cwd with every '/'->'-'
#   (CC slugify). An agent is ACTIVE iff its meta.json exists AND its sibling
#   .jsonl mtime is fresh (within ~90s of now); staler => done (evicted, not shown).
#   This replaces the old _spawns-rich.log SPAWN/EXIT correlation, which could not
#   tell a live agent from a finished one. The monitor reads NO log file now.
#
# RESPONSIVE LAYOUT: stat/cwd/model segments are placed in priority order (all T1,
# then T2/T3) and packed HORIZONTALLY — joined with " │ " until the next would
# exceed COLUMNS, then wrapped to a new line. The subagent monitor is FORCED onto
# its own line(s) below the stats (thematic separation + room for per-agent info).
# Hard cap: <=5 lines. If output would exceed 5 lines, the lowest-priority (T3 then
# T2) segments are dropped first; T1 is never dropped, it abbreviates (shorter
# labels, narrower bars, %-only at very narrow widths).
#
# CONTRACT (must NOT crash, must always print >=1 line):
#   * FAIL-SAFE: every input is optional; a missing/garbage input omits its
#     segment, never aborts. Final fallback prints a minimal degraded line.
#   * FAST: single python stdlib process (<100ms), no network. The only I/O is the
#     git subprocess (1s timeout) + small cache reads; the cost-cache refresh is
#     backgrounded/disowned and NEVER awaited.
#
# This is a thin bash launcher that passes the CC stdin envelope to the python
# module (run via ~/.claude/.venv/bin/python; falls back to python3 on PATH) via
# the STATUSLINE_STDIN env var.
#
# stdin field paths confirmed from the CC 2.1.159 binary statusline payload
# builder (function cZz) — NOT guessed, NOT from claude-hud:
#   context_window.used_percentage           (number 0-100 | null)  <- native ctx %
#   context_window.context_window_size       (number)  <- ctx fallback + capacity
#   context_window.current_usage.{input_tokens,cache_creation_input_tokens,cache_read_input_tokens}
#   rate_limits.five_hour.{used_percentage,resets_at}   (number | null)
#   rate_limits.seven_day.{used_percentage,resets_at}   (number | null)
#   cost.{total_cost_usd,total_duration_ms}   (float | null)  <- optional session $
#   model.{id,display_name}                   (str)
#   effort.level                              (str: max|high|medium|low|...) <- NESTED
#   cwd                                       (str absolute path)
#   session_id                                (str uuid)  <- subagent-dir key
# WIDTH SOURCE: the 2.1.159 statusline payload carries NO width/columns/
# terminal_width field (verified: every `terminalWidth` literal in the binary is
# internal Ink/yoga TUI renderer plumbing, never serialized to the statusline
# command; the payload's `workspace` object is {current_dir,project_dir,
# added_dirs,git_worktree?,repo?} with no width). The statusline command also
# runs with stdout PIPED (non-tty), so os.get_terminal_size(stdout) FAILS and a
# naive default (100) drops segments on wide monitors and crops on resize.
# FIX: detect width via the controlling terminal /dev/tty (reflects the user's
# REAL terminal + tracks resize even when stdout is piped), then COLUMNS env as a
# hint, then a conservative narrow default LAST. Bar-width breakpoints
# (>=100->10 / >=60->6 / <60->4) still mirror claude-hud getAdaptiveBarWidth.
# WRAP-NOT-CROP: no rendered line may exceed the detected width; we wrap to extra
# lines (<=5) rather than letting the terminal crop. owner's explicit preference:
# multi-line beats cropping (wrapping shows all info; cropping loses it).

set -uo pipefail

PYTHON="$HOME/.claude/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3 2>/dev/null || true)"

# Capture stdin once so we can both feed python and fall back if python is absent.
STDIN_ENVELOPE="$(cat 2>/dev/null || true)"

if [ -z "$PYTHON" ]; then
  # No interpreter at all: degraded one-liner so the statusline is never blank.
  printf '%s\n' "telemetry: python unavailable"
  exit 0
fi

# Hand the envelope to python via env (stdin is consumed by the heredoc script).
# COLUMNS is passed through as a HINT only; the python prefers /dev/tty (the real
# controlling terminal) because the statusline runs with stdout piped (non-tty).
STATUSLINE_STDIN="$STDIN_ENVELOPE" COLUMNS="${COLUMNS:-}" "$PYTHON" "$HOME/.claude/scripts/statusline_telemetry.py" 2>/dev/null || printf '%s\n' "telemetry: degraded"
