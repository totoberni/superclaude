#!/usr/bin/env bash
# swarm-observe.sh - read-only portfolio view over converge loops, ledgers and workers.
#
# STRICTLY read-only over everything it watches (mirrors wf-watchdog): outside --self-test
# this script opens nothing for writing and never mutates a watched loop or ledger.
#
# Sources (all under SWARM_OBSERVE_ROOT, default $HOME/.claude):
#   plans/*/auto/*/   DRIVER loops: rounds.md (R-1 structured rows) + handoff.json
#   plans/*/rounds.md HAND ledgers: tolerant last-token grep, file mtime age
#   comms/_spawns-rich.log  live workers: SPAWN rows in 24h with no same-agent_id EXIT
#   agents/_ephemeral/      ephemeral autocommissioned worker .md files
#
# _outcomes.log is DELIBERATELY never read: for background / agent-team dispatches its
# TRUNCATED classification is captured from a JSON metadata blob, not the worker's final
# text, so it misclassifies (see checkpoints/p0-ledgers.md section 4). _spawns-rich.log's
# EXIT-row outcome (derived from last_assistant_message) is the trustworthy signal.
#
# Structured ledger grammar (w1-design.md "Ledger row schema", R-1 pre-commit):
#   ^## R<round>.<phase> - <ISO8601 ts> - <event>$   phase in produce|gate|review|seal|terminal|escalate
#   ^- token: `<VERDICT|SEAL line>` | none$
#   ^- findings-open: <N>$
#   ^- manifest: sha256:<12hex> | commit:<12hex> | n/a$

set -uo pipefail

STALL_MIN=30
JSON=0
SELFTEST=0

usage() {
  cat <<'EOF'
Usage: swarm-observe.sh [--json] [--stall-min N] [--self-test]
  --json        emit one JSON object (loops + workers + ephemeral) instead of tables
  --stall-min N running driver loop with latest row older than N minutes is STALL (default 30)
  --self-test   build tempdir fixtures, assert every classification, print pass/fail, exit 0/1
Read-only. Never mutates a watched loop. Exit 0 on scan complete, 1 only on internal error.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) JSON=1; shift ;;
    --stall-min) STALL_MIN="${2:-30}"; shift 2 ;;
    --self-test) SELFTEST=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown arg: %s\n' "$1" >&2; usage >&2; exit 1 ;;
  esac
done

shopt -s nullglob

# ---------- helpers ----------

fmt_age() { # minutes -> compact age; -1 = unknown
  local m=$1
  if (( m < 0 )); then echo "-"
  elif (( m < 60 )); then echo "${m}m"
  elif (( m < 1440 )); then echo "$((m/60))h"
  else echo "$((m/1440))d"; fi
}

json_escape() { # escape backslash and doublequote for a JSON string value
  local s=${1//\\/\\\\}
  printf '%s' "${s//\"/\\\"}"
}

join_comma() { # join args with commas
  local out="" a
  for a in "$@"; do out+="${out:+,}$a"; done
  printf '%s' "$out"
}

# classify a driver loop by priority: VOIDED > SEALED > ESCALATED > STALL > OSCILLATION > RUNNING
classify_driver() {
  local voided=$1 status=$2 age=$3 stallmin=$4 trend=$5
  if [[ $voided == 1 ]]; then echo VOIDED; return; fi
  case "$status" in
    sealed) echo SEALED; return ;;
    escalated) echo ESCALATED; return ;;
  esac
  if [[ $status == running ]]; then
    if (( age >= 0 )) && (( age > stallmin )); then echo STALL; return; fi
    if [[ $trend == flat || $trend == rise ]]; then echo OSCILLATION; return; fi
  fi
  echo RUNNING
}

# ---------- scan (reads globals ROOT, JSON, STALL_MIN, NOW, CUTOFF) ----------

run_scan() {
  local ROOT=$1
  local -a L_ID L_KIND L_STATUS L_RP L_TREND L_REV L_AGE L_TOKEN
  local d handoff rounds lid status header rest roundphase tmp ts event
  local age_min ep voided mline rev tline tok kls
  local -a RF
  local n a b trend

  # (a) DRIVER loops
  for d in "$ROOT"/plans/*/auto/*/; do
    rounds="$d/rounds.md"; handoff="$d/handoff.json"
    [[ -f "$rounds" && -f "$handoff" ]] || continue

    lid=$(grep -oE '"loop_id"[[:space:]]*:[[:space:]]*"[^"]*"' "$handoff" | head -1 | sed -E 's/.*"([^"]*)"$/\1/')
    [[ -z "$lid" ]] && lid=$(basename "$d")
    status=$(grep -oE '"status"[[:space:]]*:[[:space:]]*"[^"]*"' "$handoff" | head -1 | sed -E 's/.*"([^"]*)"$/\1/')
    [[ -z "$status" ]] && status="unknown"

    header=$(grep -E '^## R[0-9]+\.(produce|gate|review|seal|terminal|escalate) - ' "$rounds" | tail -1)
    if [[ -n "$header" ]]; then
      rest=${header#'## R'}
      roundphase=${rest%% - *}
      tmp=${rest#* - }
      ts=${tmp%% - *}
      event=${tmp#* - }
    else
      roundphase="-"; ts=""; event=""
    fi

    age_min=-1
    if [[ -n "$ts" ]]; then
      ep=$(date -d "$ts" +%s 2>/dev/null || true)
      if [[ -n "$ep" ]]; then age_min=$(( (NOW - ep) / 60 )); (( age_min < 0 )) && age_min=0; fi
    fi

    voided=0
    if find "$d" -maxdepth 1 -iname 'VOIDED*' 2>/dev/null | grep -q .; then voided=1; fi

    mline=$(grep -E '^- manifest: ' "$rounds" | tail -1 || true)
    rev=$(printf '%s' "$mline" | grep -oE '(commit|sha256):[0-9a-f]{6,}' | head -1 || true)
    [[ -z "$rev" ]] && rev="-"

    tline=$(grep -E '^- token: ' "$rounds" | tail -1 || true)
    tok=$(printf '%s' "$tline" | sed -E 's/^- token: //; s/^`//; s/`$//')
    [[ -z "$tok" ]] && tok="none"

    # findings trend from review rows (last two)
    mapfile -t RF < <(awk '
      /^## R[0-9]+\.[a-z]+ - / { ph=$0; sub(/^## R[0-9]+\./,"",ph); sub(/ - .*/,"",ph); cur=ph; next }
      cur=="review" && /^- findings-open: / { v=$0; sub(/^- findings-open: /,"",v); print v+0 }
    ' "$rounds")
    n=${#RF[@]}
    if (( n >= 2 )); then
      a=${RF[n-2]}; b=${RF[n-1]}
      if (( b < a )); then trend=fall; elif (( b == a )); then trend=flat; else trend=rise; fi
    else
      trend="-"
    fi

    kls=$(classify_driver "$voided" "$status" "$age_min" "$STALL_MIN" "$trend")

    L_ID+=("$lid"); L_KIND+=("driver"); L_STATUS+=("$kls"); L_RP+=("$roundphase")
    L_TREND+=("$trend"); L_REV+=("$rev"); L_AGE+=("$age_min"); L_TOKEN+=("$tok")
  done

  # (b) HAND ledgers (tolerant)
  local f htoken hstatus mtime hage
  for f in "$ROOT"/plans/*/rounds.md; do
    [[ -f "$f" ]] || continue
    lid=$(basename "$(dirname "$f")")
    htoken=$(grep -E '^(VERDICT|SEAL): ' "$f" | tail -1 || true)
    if [[ "$htoken" == "SEAL: ACCEPTED"* ]]; then hstatus=SEALED
    elif [[ "$htoken" == SEAL:* ]]; then hstatus="SEAL-$(printf '%s' "$htoken" | awk '{print $2}')"
    elif [[ "$htoken" == "VERDICT: CLEAN"* ]]; then hstatus=CLEAN
    elif [[ "$htoken" == "VERDICT: REWORK"* ]]; then hstatus=REWORK
    else hstatus=UNKNOWN; fi
    [[ -z "$htoken" ]] && htoken="none"
    mtime=$(date -r "$f" +%s 2>/dev/null || echo "$NOW")
    hage=$(( (NOW - mtime) / 60 )); (( hage < 0 )) && hage=0

    L_ID+=("$lid"); L_KIND+=("hand"); L_STATUS+=("$hstatus"); L_RP+=("-")
    L_TREND+=("-"); L_REV+=("-"); L_AGE+=("$hage"); L_TOKEN+=("$htoken")
  done

  # (c) LIVE workers from _spawns-rich.log
  local richlog="$ROOT/comms/_spawns-rich.log"
  local infl=0 indet=0 line k v
  local -A BYTYPE=()
  if [[ -f "$richlog" ]]; then
    while IFS= read -r line; do
      case "$line" in
        "INFLIGHT "*) infl=${line#INFLIGHT } ;;
        "INDET "*)    indet=${line#INDET } ;;
        "TYPE "*)     k=$(printf '%s' "$line" | cut -d' ' -f2); v=$(printf '%s' "$line" | cut -d' ' -f3); BYTYPE["$k"]=$v ;;
      esac
    done < <(awk -F'\t' -v cutoff="$CUTOFF" '
      FNR==NR { if ($4=="EXIT" && $2!="") exited[$2]=1; next }
      $4=="SPAWN" && $1>=cutoff {
        if ($2=="") indet++
        else if (!($2 in exited)) { infl++; bt[$3]++ }
      }
      END {
        print "INFLIGHT " (infl+0)
        print "INDET " (indet+0)
        for (t in bt) print "TYPE " t " " bt[t]
      }
    ' "$richlog" "$richlog")
  fi

  # (d) ephemeral agents
  local -a EPH
  mapfile -t EPH < <(find "$ROOT/agents/_ephemeral" -maxdepth 1 -name '*.md' -printf '%f\n' 2>/dev/null | sort)

  # ---------- render ----------
  local i
  if [[ $JSON == 1 ]]; then
    local -a lobjs=()
    for i in "${!L_ID[@]}"; do
      local agej="${L_AGE[i]}"; [[ "$agej" == "-1" ]] && agej=null
      lobjs+=("$(printf '{"loop_id":"%s","kind":"%s","status":"%s","round_phase":"%s","trend":"%s","revision":"%s","age_min":%s,"token":"%s"}' \
        "$(json_escape "${L_ID[i]}")" "${L_KIND[i]}" "${L_STATUS[i]}" "${L_RP[i]}" \
        "${L_TREND[i]}" "${L_REV[i]}" "$agej" "$(json_escape "${L_TOKEN[i]}")")")
    done
    local -a btj=()
    for k in "${!BYTYPE[@]}"; do btj+=("$(printf '"%s":%s' "$(json_escape "$k")" "${BYTYPE[$k]}")"); done
    local -a ephj=()
    for i in "${EPH[@]:-}"; do [[ -n "$i" ]] && ephj+=("$(printf '"%s"' "$(json_escape "$i")")"); done
    printf '{"loops":[%s],"workers":{"in_flight":%s,"by_type":{%s},"indeterminate_no_id":%s},"ephemeral":[%s]}\n' \
      "$(join_comma "${lobjs[@]:-}")" "$infl" "$(join_comma "${btj[@]:-}")" "$indet" "$(join_comma "${ephj[@]:-}")"
    return 0
  fi

  printf '=== DRIVER + HAND LEDGERS (%d) ===\n' "${#L_ID[@]}"
  if (( ${#L_ID[@]} > 0 )); then
    printf '%-28s %-6s %-11s %-12s %-5s %-22s %-6s\n' LOOP KIND STATUS ROUND.PHASE TREND REVISION AGE
    for i in "${!L_ID[@]}"; do
      printf '%-28.28s %-6s %-11s %-12s %-5s %-22s %-6s\n' \
        "${L_ID[i]}" "${L_KIND[i]}" "${L_STATUS[i]}" "${L_RP[i]}" \
        "${L_TREND[i]}" "${L_REV[i]}" "$(fmt_age "${L_AGE[i]}")"
    done
  else
    printf '(no loops or ledgers found under %s)\n' "$ROOT/plans"
  fi

  printf '\n=== WORKERS in-flight, last 24h (%s) ===\n' "$infl"
  if (( ${#BYTYPE[@]} > 0 )); then
    for k in "${!BYTYPE[@]}"; do printf '  %-24s %s\n' "$k" "${BYTYPE[$k]}"; done
  fi
  printf '  indeterminate (no agent_id, background/team dispatch): %s\n' "$indet"

  printf '\n=== EPHEMERAL AGENTS (%d) ===\n' "${#EPH[@]}"
  if (( ${#EPH[@]} > 0 )); then
    for i in "${EPH[@]}"; do printf '  - %s\n' "$i"; done
  else
    printf '  (none registered)\n'
  fi
  return 0
}

# ---------- self-test ----------

self_test() {
  local tmp; tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' RETURN
  local now_iso old_iso
  now_iso=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
  old_iso="2020-01-01T00:00:00+00:00"

  mkdir -p "$tmp/plans/camp/auto" "$tmp/comms" "$tmp/agents/_ephemeral"

  _mk_loop() { # dir status header_ts extra_rows
    local ld="$tmp/plans/camp/auto/$1"; mkdir -p "$ld"
    printf '{"loop_id":"%s","round":1,"phase":"produce","status":"%s"}\n' "$1" "$2" > "$ld/handoff.json"
    {
      printf '## R1.produce - %s - ok\n- delta: files=2\n- token: none\n- findings-open: 0\n- manifest: sha256:aabbccddeeff\n\n' "$3"
      [[ -n "${4:-}" ]] && printf '%b' "$4"
    } > "$ld/rounds.md"
    printf '%s' "$ld"
  }

  _mk_loop loop-sealed   sealed    "$now_iso" >/dev/null
  _mk_loop loop-escal    escalated "$now_iso" >/dev/null
  _mk_loop loop-stall    running   "$old_iso" >/dev/null
  # oscillation: running, recent, two review rows findings 2,2 (flat)
  _mk_loop loop-osc      running   "$now_iso" \
    "## R2.review - $now_iso - ok\n- delta: r\n- token: \`VERDICT: REWORK blocking=0 major=2 minor=0 round=1\`\n- findings-open: 2\n\n## R3.review - $now_iso - ok\n- delta: r\n- token: \`VERDICT: REWORK blocking=0 major=2 minor=0 round=2\`\n- findings-open: 2\n\n" >/dev/null
  # running: recent, two review rows findings 3,1 (falling) -> RUNNING not OSCILLATION
  _mk_loop loop-run      running   "$now_iso" \
    "## R2.review - $now_iso - ok\n- token: \`VERDICT: REWORK blocking=0 major=3 minor=0 round=1\`\n- findings-open: 3\n\n## R3.review - $now_iso - ok\n- token: \`VERDICT: REWORK blocking=0 major=1 minor=0 round=2\`\n- findings-open: 1\n\n" >/dev/null
  # voided: marker present, handoff sealed -> VOIDED beats SEALED
  vd=$(_mk_loop loop-void sealed "$now_iso"); : > "$vd/VOIDED"

  # hand ledger with a SEAL line
  printf '## wrap\n\nSEAL: ACCEPTED blocking=0 major=0 minor=0 nits=0\n' > "$tmp/plans/camp/rounds.md"

  # spawns-rich: 1 unmatched SPAWN (real id) -> 1 in-flight; plus a matched pair -> not counted
  {
    printf '%s\ta1111111111111111\tw-tester\tSPAWN\tmatched\t\n' "$now_iso"
    printf '%s\ta1111111111111111\tw-tester\tEXIT\t\tok\n'       "$now_iso"
    printf '%s\ta2222222222222222\tw-implementer\tSPAWN\tlive\t\n' "$now_iso"
  } > "$tmp/comms/_spawns-rich.log"

  local pass=0 fail=0
  _assert() { # label condition-already-evaluated (0=pass)
    if [[ "$2" == 0 ]]; then pass=$((pass+1)); else fail=$((fail+1)); printf '  FAIL: %s\n' "$1"; fi
  }

  local out
  NOW=$(date -u +%s); CUTOFF=$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ)
  JSON=0; out=$(run_scan "$tmp")

  grep -qE '^loop-sealed .* SEALED '     <<<"$out"; _assert "loop-sealed -> SEALED" $?
  grep -qE '^loop-escal .* ESCALATED '   <<<"$out"; _assert "loop-escal -> ESCALATED" $?
  grep -qE '^loop-stall .* STALL '       <<<"$out"; _assert "loop-stall -> STALL" $?
  grep -qE '^loop-osc .* OSCILLATION '   <<<"$out"; _assert "loop-osc -> OSCILLATION" $?
  grep -qE '^loop-run .* RUNNING '       <<<"$out"; _assert "loop-run -> RUNNING" $?
  grep -qE '^loop-void .* VOIDED '       <<<"$out"; _assert "loop-void -> VOIDED (beats sealed)" $?
  grep -qE '^camp .* hand .* SEALED '    <<<"$out"; _assert "hand ledger -> SEALED" $?
  grep -qE 'WORKERS in-flight, last 24h \(1\)' <<<"$out"; _assert "workers in-flight == 1" $?
  grep -qE 'w-implementer +1' <<<"$out"; _assert "in-flight by_type w-implementer == 1" $?

  local jout
  JSON=1; jout=$(run_scan "$tmp")
  grep -q '"loop_id":"loop-void","kind":"driver","status":"VOIDED"' <<<"$jout"; _assert "json VOIDED" $?
  grep -q '"in_flight":1' <<<"$jout"; _assert "json in_flight==1" $?
  grep -q '"w-implementer":1' <<<"$jout"; _assert "json by_type" $?
  grep -q '"status":"SEALED","round_phase":"-"' <<<"$jout"; _assert "json hand SEALED" $?

  printf '\nself-test: %d passed, %d failed\n' "$pass" "$fail"
  [[ $fail == 0 ]]
}

# ---------- main ----------

if [[ $SELFTEST == 1 ]]; then
  self_test; exit $?
fi

ROOT="${SWARM_OBSERVE_ROOT:-$HOME/.claude}"
if [[ ! -d "$ROOT" ]]; then printf 'error: root not found: %s\n' "$ROOT" >&2; exit 1; fi
NOW=$(date -u +%s)
CUTOFF=$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ)
run_scan "$ROOT"
exit 0
