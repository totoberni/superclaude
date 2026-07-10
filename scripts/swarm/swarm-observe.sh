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
#   ^## R<round>.<phase> - <ISO8601 ts> - <event>$   phase in produce|gate|review|seal|terminal|escalate|control
#   ^- token: `<VERDICT|SEAL line>` | none$
#   ^- findings-open: <N>$
#   ^- manifest: sha256:<12hex> | commit:<12hex> | n/a$

set -uo pipefail

STALL_MIN=30
JSON=0
SELFTEST=0
WATCH=0
WATCH_INT=10

usage() {
  cat <<'EOF'
Usage: swarm-observe.sh [--json] [--stall-min N] [--watch [SECONDS]] [--self-test]
  --json          emit one JSON object (loops + workers + ephemeral) instead of tables
  --stall-min N   running driver loop with latest row older than N minutes is STALL (default 30)
  --watch [SECS]  clear-screen re-render every SECS (default 10) until Ctrl-C; not with --json
  --self-test     build tempdir fixtures, assert every classification, print pass/fail, exit 0/1
Read-only. Never mutates a watched loop. Exit 0 on scan complete, 1 only on internal error.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) JSON=1; shift ;;
    --stall-min) STALL_MIN="${2:-30}"; shift 2 ;;
    --watch)
      WATCH=1
      if [[ "${2:-}" =~ ^[0-9]+$ ]]; then WATCH_INT="$2"; shift 2; else shift; fi
      ;;
    --self-test) SELFTEST=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown arg: %s\n' "$1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ $WATCH == 1 && $JSON == 1 ]]; then
  printf 'error: --watch cannot be combined with --json\n' >&2; exit 1
fi

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

# liveness of a driver loop's process: alive | dead | nopid (W2.5 item 3).
# reads driver.pid (single loop) or parallel-driver.pid (parallel child) if present.
loop_pid_state() {
  local d=$1 pidfile pid
  for pidfile in "$d/driver.pid" "$d/parallel-driver.pid"; do
    [[ -f "$pidfile" ]] || continue
    pid=$(head -1 "$pidfile" 2>/dev/null | tr -dc '0-9')
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then echo alive; return; fi
    echo dead; return
  done
  echo nopid
}

# classify a driver loop by priority:
#   VOIDED > SEALED > ESCALATED > ABORTED > (running:) DEAD > RUNNING? > STALL > OSCILLATION > RUNNING
# DEAD = handoff running but pid dead (crashed); RUNNING? = running but no pid file (pre-W2.5 or
# crashed before pid write, kept honest rather than guessed STALL).
classify_driver() {
  local voided=$1 status=$2 age=$3 stallmin=$4 trend=$5 pidstate=$6
  if [[ $voided == 1 ]]; then echo VOIDED; return; fi
  case "$status" in
    sealed) echo SEALED; return ;;
    escalated) echo ESCALATED; return ;;
    aborted) echo ABORTED; return ;;
  esac
  if [[ $status == running ]]; then
    if [[ $pidstate == dead ]]; then echo DEAD; return; fi
    if [[ $pidstate == nopid ]]; then echo 'RUNNING?'; return; fi
    if (( age >= 0 )) && (( age > stallmin )); then echo STALL; return; fi
    if [[ $trend == flat || $trend == rise ]]; then echo OSCILLATION; return; fi
  fi
  echo RUNNING
}

# ---------- scan (reads globals ROOT, JSON, STALL_MIN, NOW, CUTOFF) ----------

run_scan() {
  local ROOT=$1
  local -a L_ID L_KIND L_STATUS L_RP L_TREND L_REV L_AGE L_TOKEN L_PID
  local d handoff rounds lid status header rest roundphase tmp ts event
  local age_min ep voided mline rev tline tok kls hround hphase pidstate
  local -a RF
  local n a b trend

  # (a) DRIVER loops. handoff.json is the entry criterion; rounds.md is optional so a loop
  # in its first phase (no ledger rows yet) is still visible (W2.5 item 7, first-phase gap).
  for d in "$ROOT"/plans/*/auto/*/; do
    handoff="$d/handoff.json"
    [[ -f "$handoff" ]] || continue
    rounds="$d/rounds.md"

    lid=$(grep -oE '"loop_id"[[:space:]]*:[[:space:]]*"[^"]*"' "$handoff" | head -1 | sed -E 's/.*"([^"]*)"$/\1/')
    [[ -z "$lid" ]] && lid=$(basename "$d")
    status=$(grep -oE '"status"[[:space:]]*:[[:space:]]*"[^"]*"' "$handoff" | head -1 | sed -E 's/.*"([^"]*)"$/\1/')
    [[ -z "$status" ]] && status="unknown"
    hround=$(grep -oE '"round"[[:space:]]*:[[:space:]]*[0-9]+' "$handoff" | head -1 | grep -oE '[0-9]+' || true)
    hphase=$(grep -oE '"phase"[[:space:]]*:[[:space:]]*"[^"]*"' "$handoff" | head -1 | sed -E 's/.*"([^"]*)"$/\1/')

    header=""
    [[ -f "$rounds" ]] && header=$(grep -E '^## R[0-9]+\.(produce|gate|review|seal|terminal|escalate|control) - ' "$rounds" | tail -1)
    if [[ -n "$header" ]]; then
      rest=${header#'## R'}
      roundphase=${rest%% - *}
      tmp=${rest#* - }
      ts=${tmp%% - *}
      event=${tmp#* - }
    else
      # handoff-only: no ledger rows yet, take round.phase from driver state
      roundphase="${hround:-?}.${hphase:-?}"; ts=""; event=""
    fi

    age_min=-1
    if [[ -n "$ts" ]]; then
      ep=$(date -d "$ts" +%s 2>/dev/null || true)
      if [[ -n "$ep" ]]; then age_min=$(( (NOW - ep) / 60 )); (( age_min < 0 )) && age_min=0; fi
    fi

    voided=0
    if find "$d" -maxdepth 1 -iname 'VOIDED*' 2>/dev/null | grep -q .; then voided=1; fi

    rev="-"; tok="none"; trend="-"
    if [[ -f "$rounds" ]]; then
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
      fi
    fi

    pidstate=$(loop_pid_state "$d")
    kls=$(classify_driver "$voided" "$status" "$age_min" "$STALL_MIN" "$trend" "$pidstate")

    L_ID+=("$lid"); L_KIND+=("driver"); L_STATUS+=("$kls"); L_RP+=("$roundphase")
    L_TREND+=("$trend"); L_REV+=("$rev"); L_AGE+=("$age_min"); L_TOKEN+=("$tok"); L_PID+=("$pidstate")
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
    L_TREND+=("-"); L_REV+=("-"); L_AGE+=("$hage"); L_TOKEN+=("$htoken"); L_PID+=("n-a")
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
      lobjs+=("$(printf '{"loop_id":"%s","kind":"%s","status":"%s","round_phase":"%s","trend":"%s","revision":"%s","age_min":%s,"token":"%s","pid":"%s"}' \
        "$(json_escape "${L_ID[i]}")" "${L_KIND[i]}" "${L_STATUS[i]}" "${L_RP[i]}" \
        "${L_TREND[i]}" "${L_REV[i]}" "$agej" "$(json_escape "${L_TOKEN[i]}")" "${L_PID[i]}")")
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

  local deadpid alivepid
  deadpid=$(cat /proc/sys/kernel/pid_max 2>/dev/null || echo 4194304); deadpid=$((deadpid + 1))
  alivepid=$$

  _mk_loop() { # dir status header_ts extra_rows pid
    local ld="$tmp/plans/camp/auto/$1"; mkdir -p "$ld"
    printf '{"loop_id":"%s","round":1,"phase":"produce","status":"%s"}\n' "$1" "$2" > "$ld/handoff.json"
    {
      printf '## R1.produce - %s - ok\n- delta: files=2\n- token: none\n- findings-open: 0\n- manifest: sha256:aabbccddeeff\n\n' "$3"
      [[ -n "${4:-}" ]] && printf '%b' "$4"
    } > "$ld/rounds.md"
    [[ -n "${5:-}" ]] && printf '%s\n' "$5" > "$ld/driver.pid"
    printf '%s' "$ld"
  }

  _mk_loop loop-sealed   sealed    "$now_iso" >/dev/null
  _mk_loop loop-escal    escalated "$now_iso" >/dev/null
  # running fixtures carry a live driver.pid so the W2.5 pid check does not flip them to RUNNING?/DEAD
  _mk_loop loop-stall    running   "$old_iso" "" "$alivepid" >/dev/null
  # oscillation: running, recent, two review rows findings 2,2 (flat)
  _mk_loop loop-osc      running   "$now_iso" \
    "## R2.review - $now_iso - ok\n- delta: r\n- token: \`VERDICT: REWORK blocking=0 major=2 minor=0 round=1\`\n- findings-open: 2\n\n## R3.review - $now_iso - ok\n- delta: r\n- token: \`VERDICT: REWORK blocking=0 major=2 minor=0 round=2\`\n- findings-open: 2\n\n" "$alivepid" >/dev/null
  # running: recent, two review rows findings 3,1 (falling) -> RUNNING not OSCILLATION
  _mk_loop loop-run      running   "$now_iso" \
    "## R2.review - $now_iso - ok\n- token: \`VERDICT: REWORK blocking=0 major=3 minor=0 round=1\`\n- findings-open: 3\n\n## R3.review - $now_iso - ok\n- token: \`VERDICT: REWORK blocking=0 major=1 minor=0 round=2\`\n- findings-open: 1\n\n" "$alivepid" >/dev/null
  # voided: marker present, handoff sealed -> VOIDED beats SEALED
  vd=$(_mk_loop loop-void sealed "$now_iso"); : > "$vd/VOIDED"

  # --- W2.5 fixtures (item 3+7+8) ---
  # handoff-only RUNNING: no rounds.md, alive pid, round.phase from handoff (first-phase gap)
  mkdir -p "$tmp/plans/camp/auto/loop-hoff"
  printf '{"loop_id":"loop-hoff","round":1,"phase":"produce","status":"running"}\n' > "$tmp/plans/camp/auto/loop-hoff/handoff.json"
  printf '%s\n' "$alivepid" > "$tmp/plans/camp/auto/loop-hoff/driver.pid"

  # DEAD: handoff running but driver.pid is a definitely-dead pid (pid_max+1, never allocatable)
  mkdir -p "$tmp/plans/camp/auto/loop-dead"
  printf '{"loop_id":"loop-dead","round":1,"phase":"review","status":"running"}\n' > "$tmp/plans/camp/auto/loop-dead/handoff.json"
  printf '## R1.review - %s - ok\n- token: none\n- findings-open: 0\n- manifest: sha256:aabbccddeeff\n\n' "$now_iso" > "$tmp/plans/camp/auto/loop-dead/rounds.md"
  printf '%s\n' "$deadpid" > "$tmp/plans/camp/auto/loop-dead/driver.pid"

  # ABORTED: handoff status aborted (control/ABORT graceful stop, exit 5)
  mkdir -p "$tmp/plans/camp/auto/loop-abort"
  printf '{"loop_id":"loop-abort","round":2,"phase":"control","status":"aborted"}\n' > "$tmp/plans/camp/auto/loop-abort/handoff.json"
  printf '## R2.control - %s - aborted\n- token: none\n- findings-open: 0\n- manifest: sha256:aabbccddeeff\n\n' "$now_iso" > "$tmp/plans/camp/auto/loop-abort/rounds.md"

  # control paused row: running, alive pid, latest ledger row is a control/paused entry (must parse cleanly)
  mkdir -p "$tmp/plans/camp/auto/loop-control"
  printf '{"loop_id":"loop-control","round":2,"phase":"produce","status":"running"}\n' > "$tmp/plans/camp/auto/loop-control/handoff.json"
  {
    printf '## R1.review - %s - ok\n- token: `VERDICT: REWORK blocking=0 major=1 minor=0 round=1`\n- findings-open: 1\n- manifest: sha256:aabbccddeeff\n\n' "$now_iso"
    printf '## R2.control - %s - paused\n- delta: paused by control/PAUSE\n- token: none\n- findings-open: 1\n- manifest: sha256:aabbccddeeff\n\n' "$now_iso"
  } > "$tmp/plans/camp/auto/loop-control/rounds.md"
  printf '%s\n' "$alivepid" > "$tmp/plans/camp/auto/loop-control/driver.pid"

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
  # W2.5 additions
  grep -qE '^loop-hoff .* RUNNING .* 1.produce'    <<<"$out"; _assert "handoff-only -> RUNNING R1.produce (first-phase gap)" $?
  grep -qE '^loop-dead .* DEAD '                   <<<"$out"; _assert "running + dead pid -> DEAD" $?
  grep -qE '^loop-abort .* ABORTED '               <<<"$out"; _assert "handoff aborted -> ABORTED" $?
  grep -qE '^loop-control .* RUNNING .* 2.control' <<<"$out"; _assert "control paused row parses -> 2.control" $?

  local jout
  JSON=1; jout=$(run_scan "$tmp")
  grep -q '"loop_id":"loop-void","kind":"driver","status":"VOIDED"' <<<"$jout"; _assert "json VOIDED" $?
  grep -q '"in_flight":1' <<<"$jout"; _assert "json in_flight==1" $?
  grep -q '"w-implementer":1' <<<"$jout"; _assert "json by_type" $?
  grep -q '"status":"SEALED","round_phase":"-"' <<<"$jout"; _assert "json hand SEALED" $?
  # W2.5 additions
  grep -q '"loop_id":"loop-dead","kind":"driver","status":"DEAD"' <<<"$jout"; _assert "json DEAD" $?
  grep -q '"loop_id":"loop-abort","kind":"driver","status":"ABORTED"' <<<"$jout"; _assert "json ABORTED" $?
  grep -q '"loop_id":"loop-hoff","kind":"driver","status":"RUNNING","round_phase":"1.produce","trend":"-","revision":"-","age_min":null,"token":"none","pid":"alive"' <<<"$jout"; _assert "json handoff-only RUNNING (pid alive, age null)" $?

  printf '\nself-test: %d passed, %d failed\n' "$pass" "$fail"
  [[ $fail == 0 ]]
}

# ---------- main ----------

if [[ $SELFTEST == 1 ]]; then
  self_test; exit $?
fi

ROOT="${SWARM_OBSERVE_ROOT:-$HOME/.claude}"
if [[ ! -d "$ROOT" ]]; then printf 'error: root not found: %s\n' "$ROOT" >&2; exit 1; fi

if [[ $WATCH == 1 ]]; then
  # clear-screen re-render loop until Ctrl-C (W2.5 item 7); plain tables only, never --json
  while true; do
    clear 2>/dev/null || printf '\033[2J\033[H'
    printf 'swarm-observe --watch (every %ss, Ctrl-C to stop)  %s\n\n' "$WATCH_INT" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    NOW=$(date -u +%s)
    CUTOFF=$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ)
    run_scan "$ROOT"
    sleep "$WATCH_INT"
  done
fi

NOW=$(date -u +%s)
CUTOFF=$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ)
run_scan "$ROOT"
exit 0
