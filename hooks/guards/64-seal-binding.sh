# Guard: 64-seal-binding: F5 #21/#22, revision-binding + seal-identity
# (design/enforcement-gap-ledger.md Family 5 #21/#22; PHASE2-CONTRACT sec 8 role
# registry; skills/_shared/verdict-schema.md No pre-approval + Provenance; R-5).
#
# This file provides the #22 SEAL-IDENTITY halves. #21 REVISION-BINDING is the
# in-session sealed-manifest sidecar (staging/scripts/seal-manifest.py), which the
# conductor calls at commit time; a thin PostToolUse-on-Bash flag surfacing that
# check lives here too (guardpost_seal_binding_void, best-effort, FLAG-level).
#
# Two #22 mechanisms:
#   (A) ROLE REGISTRY WRITER (guardpost_seal_binding, PostToolUse on Agent):
#       appends one JSONL row per governed Agent spawn to
#       <SUPERCLAUDE_ROLES_DIR>/<session_id>.jsonl (default
#       ~/.claude/comms/_roles/). Row = {agentId,name,subagent_type,role,ts}.
#       role is derived from subagent_type (PHASE2-CONTRACT sec 8):
#         reviewer = {w-reviewer, w-hostile-reviewer, w-design-reviewer}
#         producer = {w-implementer, w-doc, w-refactorer, w-merger}
#       Unknown subagent_types are not registered (kept lean; fail-open). The
#       agentId (from tool_response.agentId; e.g. aREV-commit-gate-<hex>) embeds
#       the teammate name, so `name` is that field when present else derived from
#       the agentId. The registry dir is env-overridable for tests.
#   (B) SEAL-REQUEST GUARD (guard_seal_binding, PreToolUse on SendMessage):
#       BLOCKS a seal-request message sent to an agent already registered as a
#       ROUND REVIEWER this session. verdict-schema requires the final SEAL to
#       come from a FRESH auditor of a DIFFERENT identity than any round reviewer
#       (No pre-approval + Provenance). Continuing an existing round reviewer with
#       "now SEAL this" is the exact anti-pattern; the correct pattern is to spawn
#       a fresh auditor whose seal instruction is in its Agent spawn prompt (never
#       a follow-up SendMessage), so it is never registered-then-messaged.
#
# FAIL-OPEN throughout: an unresolvable tool / target / session / registry passes
# silently (returns without blocking). Only an explicit, resolved seal-request to a
# resolved registered reviewer blocks.

GUARD_MODE_SEAL_BINDING=block

# Role sets (PHASE2-CONTRACT sec 8). Echoes "reviewer" | "producer" | "" (unknown).
_guard_seal_binding_role() {
  case "$1" in
    w-reviewer|w-hostile-reviewer|w-design-reviewer) echo "reviewer" ;;
    w-implementer|w-doc|w-refactorer|w-merger)       echo "producer" ;;
    *)                                               echo "" ;;
  esac
}

# Derive a teammate name from an agentId of the form a<name>-<16hex> or a<16hex>.
# Strips the leading 'a' and a trailing (optional '-')<hex run of >=12>. Empty when
# the id carries no embedded name (auto-named spawn). Best-effort, format-derived.
_guard_seal_binding_name_from_id() {
  local id="${1:-}"
  [ -n "$id" ] || { echo ""; return 0; }
  local stripped="${id#a}"
  stripped=$(printf '%s' "$stripped" | sed -E 's/-?[0-9a-f]{12,}$//')
  printf '%s' "$stripped"
}

# Resolve the session id from the raw hook stdin. Sanitized; empty on any failure.
_guard_seal_binding_session_id() {
  [ -n "${GUARD_STDIN:-}" ] || { echo ""; return 0; }
  command -v jq >/dev/null 2>&1 || { echo ""; return 0; }
  local sid
  sid=$(printf '%s' "$GUARD_STDIN" | jq -r '.session_id // ""' 2>/dev/null) || sid=""
  printf '%s' "$sid" | tr -cd 'a-zA-Z0-9_.-'
}

_guard_seal_binding_roles_dir() {
  printf '%s' "${SUPERCLAUDE_ROLES_DIR:-$HOME/.claude/comms/_roles}"
}

# ── (A) Role registry writer: PostToolUse on Agent ──────────────────────────
guardpost_seal_binding() {
  guard_kill_switch && return 0
  [ "${GUARD_TOOL:-}" = "Agent" ] || return 0
  command -v jq >/dev/null 2>&1 || return 0

  local subtype role
  subtype=$(guard_field "subagent_type")
  role=$(_guard_seal_binding_role "$subtype")
  [ -n "$role" ] || return 0

  local sid
  sid=$(_guard_seal_binding_session_id)
  [ -n "$sid" ] || return 0

  local agentid
  agentid=$(printf '%s' "${GUARD_STDIN:-}" | jq -r '.tool_response.agentId // ""' 2>/dev/null) || agentid=""
  agentid=$(printf '%s' "$agentid" | tr -cd 'a-zA-Z0-9_.-')

  local name
  name=$(guard_field "name")
  [ -n "$name" ] || name=$(_guard_seal_binding_name_from_id "$agentid")

  local dir
  dir=$(_guard_seal_binding_roles_dir)
  mkdir -p "$dir" 2>/dev/null || return 0

  local ts row
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  row=$(jq -nc \
    --arg a "$agentid" --arg n "$name" --arg s "$subtype" --arg r "$role" --arg t "$ts" \
    '{agentId:$a, name:$n, subagent_type:$s, role:$r, ts:$t}' 2>/dev/null) || return 0

  printf '%s\n' "$row" >> "$dir/$sid.jsonl" 2>/dev/null || true
  return 0
}

# ── (B) Seal-request guard: PreToolUse on SendMessage ───────────────────────
# The continuation-tool set (the tool that messages an existing agent). Primary:
# SendMessage. Env-overridable for other continuation tools / future environments.
_guard_seal_binding_is_cont_tool() {
  local tool="${1:-}" set t
  set="${SUPERCLAUDE_SEAL_CONT_TOOLS:-SendMessage}"
  for t in $set; do [ "$tool" = "$t" ] && return 0; done
  return 1
}

# A seal / final-audit request. Requires SEAL adjacent to an instruction, not a
# bare mention (SEAL-A m2 fix): the prior `\bSEAL\b` arm matched ANY uppercase
# mention of the word, so "no SEAL yet, keep reviewing" and "hold the SEAL
# until round 3" both blocked despite being deferrals, not requests. Tightened
# to two arms: (1) `SEAL:` immediately followed by a colon — the verdict-schema
# SEAL: line format, an explicit instruction to emit one; (2) an imperative
# seal-request phrase (case-insensitive) — "seal this", "emit a SEAL", "final
# audit", "produce a SEAL", etc. A message merely mentioning uppercase SEAL in
# passing, with no colon and no imperative phrasing, does NOT match.
_guard_seal_binding_is_seal_request() {
  local text="${1:-}"
  printf '%s' "$text" | grep -qE '\bSEAL:' && return 0
  printf '%s' "$text" | grep -qiE \
    'seal this|emit an? seal|emit the seal|now seal|please seal|go ahead and seal|final audit|final seal|seal the (artifact|work|campaign|change|diff|pr|branch|release|result|thing)|produce an? seal|produce the seal|give( me)? an? seal|give( me)? the seal' \
    && return 0
  return 1
}

# Is <target> tagged role=reviewer in THIS session's registry? Matches the target
# (a SendMessage `to` value: a name, a raw agentId, or a subagent_type) against a
# reviewer row by exact agentId / name / subagent_type, plus the a<name>-<hex>
# embedding (messaging by short name when the id embeds it). Returns 0 (true) only
# on a confirmed match; any resolution failure returns 1 (fail-open: no block).
_guard_seal_binding_target_is_reviewer() {
  local target="${1:-}"
  [ -n "$target" ] || return 1
  command -v jq >/dev/null 2>&1 || return 1

  local sid dir file
  sid=$(_guard_seal_binding_session_id)
  [ -n "$sid" ] || return 1
  dir=$(_guard_seal_binding_roles_dir)
  file="$dir/$sid.jsonl"
  [ -f "$file" ] || return 1

  jq -e -s --arg tgt "$target" '
    any(.[];
      .role == "reviewer"
      and ( (.agentId == $tgt)
            or (.name == $tgt)
            or (.subagent_type == $tgt)
            or (((.agentId // "") | startswith("a" + $tgt + "-"))) ))
  ' "$file" >/dev/null 2>&1
}

guard_seal_binding() {
  _guard_seal_binding_is_cont_tool "${GUARD_TOOL:-}" || return 0

  local target
  target=$(guard_field "to")
  [ -n "$target" ] || target=$(guard_field "recipient")
  [ -n "$target" ] || return 0

  local text
  text=$(guard_field "message")
  [ -n "$text" ] || text=$(guard_field "content")
  [ -n "$text" ] || return 0

  _guard_seal_binding_is_seal_request "$text" || return 0
  _guard_seal_binding_target_is_reviewer "$target" || return 0

  guard_block "the seal auditor must be a FRESH agent of a different identity than any round reviewer; '$target' already served as a reviewer this campaign (verdict-schema No pre-approval + Provenance, R-5). Spawn a fresh auditor and put the seal instruction in its Agent prompt, not a follow-up message."
}

# ── (#21 surface) Best-effort commit-time seal-void FLAG: PostToolUse on Bash ─
# When the conductor commits, if a seal-manifest sidecar exists for a campaign and
# staging/scripts/seal-manifest.py check reports VOID (a sealed artifact changed
# after its SEAL), surface a WARN. FLAG-level by design: blocking a commit on
# seal-void would be too strong (a PostToolUse guard cannot block anyway); the
# mechanical value is that the void is DETECTED and surfaced, not silently ignored
# (verdict-schema revision-binding, R-5). Fail-open: no sidecar, no python, or a
# non-git-commit Bash call passes silently. Sidecars are discovered under
# SUPERCLAUDE_SEAL_SIDECARS (colon-separated globs) or the default campaign tree.
# Distinct guard name SEAL_BINDING_VOID (run_guard strips the guardpost_ prefix), so
# it is controlled independently of the #22 block via SUPERCLAUDE_GUARD_SEAL_BINDING_VOID.
GUARD_MODE_SEAL_BINDING_VOID=warn
guardpost_seal_binding_void() {
  guard_kill_switch && return 0
  [ "${GUARD_TOOL:-}" = "Bash" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0

  local cmd
  cmd=$(guard_command)
  printf '%s' "$cmd" | grep -qE 'git( +-C +[^ ]+)? +commit\b' || return 0

  local checker="${SUPERCLAUDE_SEAL_MANIFEST:-$HOME/.claude/scripts/seal-manifest.py}"
  [ -f "$checker" ] || return 0

  local globs="${SUPERCLAUDE_SEAL_SIDECARS:-$HOME/.claude/plans/*/seal-manifest.json:$HOME/.claude/plans/*/*/seal-manifest.json}"
  local IFS=':' g sidecar rc
  for g in $globs; do
    for sidecar in $g; do
      [ -f "$sidecar" ] || continue
      # seal-manifest.py check contract: 0 = OK, 1 = VOID (warn), 2 = ERROR
      # (sidecar/args unusable, fail-open, no warn). SEAL-A m3 fix: the prior
      # `if ! ...` treated any non-zero exit as VOID, so an unreadable/argless
      # sidecar (exit 2) emitted a spurious "sealed artifact changed" warn.
      python3 "$checker" check --sidecar "$sidecar" >/dev/null 2>&1
      rc=$?
      if [ "$rc" -eq 1 ]; then
        guard_warn "a sealed artifact changed after its SEAL; the SEAL is VOID, a fresh seal is required ($sidecar; verdict-schema revision-binding, R-5)"
      fi
    done
  done
  return 0
}
