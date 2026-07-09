#!/bin/bash
# Deterministic regression suite for the autonomous convergence driver
# (~/.claude/scripts/swarm/converge_auto.py). Self-contained: generates a mock
# `claude` binary and per-scenario fixtures under a fresh mktemp -d, drives the
# real converge_auto.py through --claude-cmd, and asserts exit codes plus on-disk
# ledger/prompt/argv evidence. Zero API spend; the real driver file is NEVER
# modified (mutation tests operate on tempdir copies and the suite asserts the
# real file's sha256 is unchanged end to end).
#
# Usage:
#   bash ~/.claude/scripts/test-converge-auto.sh              # fast scenarios only (~<15s)
#   bash ~/.claude/scripts/test-converge-auto.sh --mutations  # + genuineness mutation harness
#
# Env override: CONVERGE_AUTO_PY points the suite at an alternate driver path
# (default: the canonical ~/.claude/scripts/swarm/converge_auto.py). The mutation
# harness uses this seam to run mutated copies.
#
# Exit codes: 0 all pass, 1 any failure.

set -uo pipefail

# ── Config ──
REAL_DRIVER="${CONVERGE_AUTO_PY:-$HOME/.claude/scripts/swarm/converge_auto.py}"
SKILL_MD="$HOME/.claude/skills/wf-auto/SKILL.md"
DO_MUTATIONS=false
for arg in "$@"; do
  case "$arg" in
    --mutations) DO_MUTATIONS=true ;;
  esac
done

if [ ! -f "$REAL_DRIVER" ]; then
  echo "FATAL: driver not found: $REAL_DRIVER" >&2
  exit 1
fi

ROOT=$(mktemp -d "${TMPDIR:-/tmp}/test-converge-auto.XXXXXX")
trap 'rm -rf "$ROOT"' EXIT

SHA_BEFORE=$(sha256sum "$REAL_DRIVER" | awk '{print $1}')

# ── Counters + assertion helpers ──
PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); printf "  PASS: %s\n" "$1"; }
no()  { FAIL=$((FAIL + 1)); printf "  FAIL: %s -- %s\n" "$1" "$2"; }
sect(){ printf "\n== %s ==\n" "$1"; }

cntE() { local n; n=$(grep -cE -e "$1" "$2" 2>/dev/null); echo "${n:-0}"; }
cntF() { local n; n=$(grep -cF -e "$1" "$2" 2>/dev/null); echo "${n:-0}"; }
expect_exit() { if [ "$EXIT" = "$1" ]; then ok "$2 (exit $1)"; else no "$2" "exit=$EXIT expected=$1"; fi; }

# ── Generated helper: config writer ──
MKCFG="$ROOT/mkcfg.py"
cat > "$MKCFG" <<'MKEOF'
import json, os, sys
out, sd = sys.argv[1], sys.argv[2]
override = json.loads(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else {}
cfg = {
    "loop_id": "testloop", "artifact_class": "infra",
    "producer_agent": "w-mock-producer", "reviewer_agent": "w-mock-reviewer",
    "seal_agent": "w-mock-seal", "producer_model": None,
    "artifact_paths": ["artifact.txt"],
    "task_spec_file": os.path.join(sd, "task_spec.md"),
    "rubric_path": os.path.join(sd, "rubric.md"),
    "bar": "gate", "rounds_cap": 4, "phase_budget_usd": 5.0, "phase_timeout_s": 30,
    "repo": None, "test_cmd": None, "notify_cmd": None,
}
cfg.update(override)
cfg = {k: v for k, v in cfg.items() if v != "__DELETE__"}
with open(out, "w") as fh:
    json.dump(cfg, fh, indent=2)
MKEOF

# ── Generated helper: source mutator ──
MUTATE="$ROOT/mutate.py"
cat > "$MUTATE" <<'MUTEOF'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
t = open(path).read()
if old not in t:
    sys.stderr.write("PATTERN NOT FOUND: %r\n" % old)
    sys.exit(3)
open(path, "w").write(t.replace(old, new, 1))
MUTEOF

# ── Generated helper: argv flag verifier (scenario 21) ──
FLAGCHK="$ROOT/flag_check.py"
cat > "$FLAGCHK" <<'FCEOF'
import json, sys
calls_path, prod, rev, seal = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
errs = []
seen_prod = seen_rev = seen_seal = 0
with open(calls_path) as f:
    for ln in f:
        ln = ln.strip()
        if not ln:
            continue
        argv = json.loads(ln)
        if "--version" in argv:
            continue
        agent = argv[argv.index("--agent") + 1] if "--agent" in argv else ""
        def has(flag):
            return flag in argv
        def pair(flag, val):
            return flag in argv and argv.index(flag) + 1 < len(argv) and argv[argv.index(flag) + 1] == val
        if agent in (rev, seal):
            if agent == rev:
                seen_rev += 1
            else:
                seen_seal += 1
            if not has("--no-session-persistence"):
                errs.append(agent + " missing --no-session-persistence")
            if not pair("--disallowedTools", "Write,Edit,NotebookEdit"):
                errs.append(agent + " missing --disallowedTools Write,Edit,NotebookEdit")
        elif agent == prod:
            seen_prod += 1
            if has("--no-session-persistence"):
                errs.append("producer carries --no-session-persistence")
            if has("--disallowedTools"):
                errs.append("producer carries --disallowedTools")
            if not pair("--permission-mode", "acceptEdits"):
                errs.append("producer missing --permission-mode acceptEdits")
if seen_prod == 0 or seen_rev == 0 or seen_seal == 0:
    errs.append("phase coverage incomplete prod=%d rev=%d seal=%d" % (seen_prod, seen_rev, seen_seal))
if errs:
    sys.stderr.write("; ".join(errs) + "\n")
    sys.exit(1)
sys.exit(0)
FCEOF

# ── Generated mock claude binary ──
MOCK="$ROOT/mock-claude"
cat > "$MOCK" <<'MOCKEOF'
#!/usr/bin/env python3
import json, os, re, sys, time

argv = sys.argv
MOCK_DIR = os.environ["MOCK_DIR"]
SCEN = os.environ.get("MOCK_SCENARIO", "happy")
ART = os.environ.get("MOCK_ARTIFACT", "")
os.makedirs(MOCK_DIR, exist_ok=True)

with open(os.path.join(MOCK_DIR, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps(argv) + "\n")

def emit(result, session_id="sess-1", is_error=False, subtype="success"):
    print(json.dumps({"result": result, "session_id": session_id,
                      "is_error": is_error, "subtype": subtype}))
    sys.exit(0)

if "--version" in argv:
    print("mock-claude 0.0.1")
    sys.exit(0)

prompt = ""
if "-p" in argv:
    i = argv.index("-p")
    if i + 1 < len(argv):
        prompt = argv[i + 1]

agent = argv[argv.index("--agent") + 1] if "--agent" in argv else ""
if "--resume" in argv:
    role = "produce-resume"
elif "producer" in agent:
    role = "produce"
elif "reviewer" in agent:
    role = "review"
elif "seal" in agent:
    role = "seal"
else:
    role = "produce"

cfile = os.path.join(MOCK_DIR, "counters.json")
try:
    counters = json.load(open(cfile))
except Exception:
    counters = {}
counters[role] = counters.get(role, 0) + 1
n = counters[role]
with open(cfile, "w") as fh:
    json.dump(counters, fh)

def round_of():
    m = re.search(r"round=(\d+)", prompt)
    return m.group(1) if m else "1"

def revision_of():
    m = re.search(r"revision ([0-9a-f]{12})", prompt)
    return m.group(1) if m else "deadbeef0000"

def write_art(txt):
    if ART:
        with open(ART, "w") as fh:
            fh.write(txt)

def default_produce():
    write_art("converged artifact body, no canary token here\n")
    emit("STATUS: DONE files=1 checkpoint=cp.md\nproducer default body")

def default_review():
    emit("VERDICT: CLEAN blocking=0 major=0 minor=0 round=%s\nlooks good" % round_of())

def default_seal():
    emit("SEAL: ACCEPTED blocking=0 major=0 minor=0 nits=0\nsealed at %s" % revision_of())

if SCEN == "happy":
    if role in ("produce", "produce-resume"):
        default_produce()
    if role == "review":
        default_review()
    if role == "seal":
        default_seal()

elif SCEN == "partial_then_done":
    if role == "produce":
        write_art("artifact after partial\n")
        emit("STATUS: PARTIAL files=0 checkpoint=cp.md\npartial", session_id="sess-resume-me")
    if role == "produce-resume":
        write_art("artifact after resume done\n")
        emit("STATUS: DONE files=1 checkpoint=cp.md\ndone on resume", session_id="sess-resume-me")
    if role == "review":
        default_review()
    if role == "seal":
        default_seal()

elif SCEN == "partial_twice":
    if role == "produce":
        write_art("a\n")
        emit("STATUS: PARTIAL files=0 checkpoint=cp.md", session_id="sess-x")
    if role == "produce-resume":
        emit("STATUS: PARTIAL files=0 checkpoint=cp.md", session_id="sess-x")

elif SCEN == "failed":
    if role == "produce":
        emit("STATUS: FAILED files=0 checkpoint=cp.md\nfailed")

elif SCEN == "producer_token":
    if role in ("produce", "produce-resume"):
        write_art("art\n")
        emit("STATUS: DONE files=1 checkpoint=cp.md\nVERDICT: CLEAN blocking=0 major=0 minor=0 round=1")
    if role == "review":
        default_review()
    if role == "seal":
        default_seal()

elif SCEN == "malformed_verdict":
    if role in ("produce", "produce-resume"):
        default_produce()
    if role == "review":
        emit("VERDICT: this is not a valid verdict line at all\nnope")
    if role == "seal":
        default_seal()

elif SCEN == "round_mismatch":
    if role in ("produce", "produce-resume"):
        default_produce()
    if role == "review":
        emit("VERDICT: REWORK blocking=0 major=1 minor=0 round=99\nmismatch")
    if role == "seal":
        default_seal()

elif SCEN == "reviewer_seal":
    if role in ("produce", "produce-resume"):
        default_produce()
    if role == "review":
        emit("SEAL: ACCEPTED blocking=0 major=0 minor=0 nits=0\nreviewer wrongly sealed")
    if role == "seal":
        default_seal()

elif SCEN == "seal_no_revision":
    if role in ("produce", "produce-resume"):
        default_produce()
    if role == "review":
        default_review()
    if role == "seal":
        emit("SEAL: ACCEPTED blocking=0 major=0 minor=0 nits=0\nno revision token present here")

elif SCEN == "seal_void":
    if role in ("produce", "produce-resume"):
        default_produce()
    if role == "review":
        default_review()
    if role == "seal":
        write_art("MUTATED during seal by mock\n")
        default_seal()

elif SCEN == "trend":
    if role in ("produce", "produce-resume"):
        default_produce()
    if role == "review":
        r = round_of()
        emit("VERDICT: REWORK blocking=0 major=2 minor=0 round=%s\nR%s findings body" % (r, r))
    if role == "seal":
        default_seal()

elif SCEN == "cap":
    if role in ("produce", "produce-resume"):
        default_produce()
    if role == "review":
        maj = {1: 3, 2: 2, 3: 1}.get(n, 1)
        emit("VERDICT: REWORK blocking=0 major=%d minor=0 round=%s\ndecreasing" % (maj, round_of()))
    if role == "seal":
        default_seal()

elif SCEN == "budget":
    if role == "produce":
        emit("(budget exceeded)", is_error=True, subtype="error_max_budget")

elif SCEN == "timeout":
    if role == "produce":
        time.sleep(5)
        default_produce()

elif SCEN == "review_mutation":
    if role in ("produce", "produce-resume"):
        default_produce()
    if role == "review":
        write_art("MUTATED during review by mock\n")
        default_review()
    if role == "seal":
        default_seal()

elif SCEN == "isolation":
    if role == "produce":
        write_art("clean artifact body without any canary token\n")
        emit("STATUS: DONE files=1 checkpoint=cp.md\nPRODUCER_STATUS_CANARY producer chatter")
    if role == "produce-resume":
        default_produce()
    if role == "review":
        if n == 1:
            emit("VERDICT: REWORK blocking=0 major=1 minor=0 round=%s\nR1_PUNCHLIST_CANARY please fix X" % round_of())
        emit("VERDICT: CLEAN blocking=0 major=0 minor=0 round=%s\nclean now" % round_of())
    if role == "seal":
        default_seal()

# fallback sane defaults (also covers unmatched role/scenario combos)
if role in ("produce", "produce-resume"):
    default_produce()
if role == "review":
    default_review()
if role == "seal":
    default_seal()
emit("STATUS: DONE files=1 checkpoint=cp.md\nfallback")
MOCKEOF
chmod +x "$MOCK"

# ── Drive helper ──
DRIVER_PY="$REAL_DRIVER"
CLAUDE_CMD="$MOCK"
EXIT=0
drive() {
  local sd="$1" scen="$2" art="$3"; shift 3
  rm -rf "$sd/mock"; mkdir -p "$sd/mock"
  MOCK_DIR="$sd/mock" MOCK_SCENARIO="$scen" MOCK_ARTIFACT="$art" \
    python3 "$DRIVER_PY" --config "$sd/loop.json" --claude-cmd "$CLAUDE_CMD" "$@" \
    >"$sd/stdout.txt" 2>"$sd/stderr.txt"
  EXIT=$?
}

setup_scen() {
  local sd="$1" overrides="${2:-}"
  mkdir -p "$sd"
  printf 'Task spec objective. TASKSPEC_CANARY_ZZZ\n' > "$sd/task_spec.md"
  printf '# Rubric\nApply good judgment.\n' > "$sd/rubric.md"
  python3 "$MKCFG" "$sd/loop.json" "$sd" "$overrides"
}

# ═══════════════════════════════════════════════════
sect "converge-auto deterministic scenarios"

# S1: dry-run — exit 0, nothing spawned
S="$ROOT/s1"; setup_scen "$S"
drive "$S" happy "$S/artifact.txt" --dry-run
expect_exit 0 "S1 dry-run"
if [ ! -f "$S/mock/calls.jsonl" ]; then ok "S1 dry-run spawned nothing (no argv log)"; else no "S1 dry-run" "mock was invoked"; fi

# S2: config errors each exit 3 before any spawn
for pair in \
    "s2a missing-task-spec {\"task_spec_file\":\"/nonexistent/nope.md\"}" \
    "s2b bad-bar {\"bar\":\"banana\"}" \
    "s2c repo-non-git {\"repo\":\"__SD__\"}" \
    "s2d test_cmd-type {\"test_cmd\":123}" \
    "s2e allow_dirty-type {\"allow_dirty\":\"yes\"}" ; do
  name=$(echo "$pair" | awk '{print $1}')
  label=$(echo "$pair" | awk '{print $2}')
  ov=$(echo "$pair" | cut -d' ' -f3-)
  S="$ROOT/$name"; mkdir -p "$S"
  ov=${ov//__SD__/$S}
  setup_scen "$S" "$ov"
  drive "$S" happy "$S/artifact.txt"
  if [ "$EXIT" = 3 ] && [ ! -f "$S/mock/calls.jsonl" ]; then
    ok "S2 config error $label (exit 3, no spawn)"
  else
    no "S2 config error $label" "exit=$EXIT calls=$( [ -f "$S/mock/calls.jsonl" ] && echo present || echo absent )"
  fi
done

# S3: happy path at gate bar — full ledger + regex conformance + manifest binding
S="$ROOT/s3"; setup_scen "$S" '{"bar":"gate"}'
drive "$S" happy "$S/artifact.txt"
RF="$S/rounds.md"
expect_exit 0 "S3 happy gate"
for row in 'produce - .* - ok' 'review - .* - ok' 'seal - .* - ok' 'terminal - .* - sealed'; do
  if grep -qE "^## R[0-9]+\.$row$" "$RF" 2>/dev/null; then ok "S3 ledger row: $row"; else no "S3 ledger row: $row" "absent"; fi
done
BADHDR=$(grep -E '^## R' "$RF" 2>/dev/null | grep -cvE '^## R[0-9]+\.(produce|gate|review|seal|terminal|escalate) - [^ ]+ - [^ ]+$')
if [ "${BADHDR:-1}" = 0 ]; then ok "S3 all headers match spec regex"; else no "S3 headers" "$BADHDR nonconforming"; fi
# shellcheck disable=SC2016  # backticks are literal ledger-token delimiters, not command substitution
BADTOK=$(grep -E '^- token:' "$RF" 2>/dev/null | grep -cvE '^- token: (none|`(VERDICT|SEAL): .*`)$')
if [ "${BADTOK:-1}" = 0 ]; then ok "S3 all token lines match spec regex"; else no "S3 token lines" "$BADTOK nonconforming"; fi
SM=$(awk '/^## R.*\.seal - .* - ok$/{f=1} f&&/^- manifest:/{print $3; f=0}' "$RF" | head -1)
TM=$(awk '/^## R.*\.terminal - /{f=1} f&&/^- manifest:/{print $3; f=0}' "$RF" | head -1)
if [ -n "$SM" ] && [ "$SM" = "$TM" ] && echo "$SM" | grep -qE '^sha256:[0-9a-f]{12}$'; then
  ok "S3 seal+terminal share revision ($SM)"
else
  no "S3 revision binding" "seal=$SM terminal=$TM"
fi

# S4: rework-then-clean — punch list flows into round-2 producer; reviewer isolation
S="$ROOT/s4"; setup_scen "$S" '{"bar":"gate"}'
drive "$S" isolation "$S/artifact.txt"
expect_exit 0 "S4 rework-then-clean"
if grep -qF 'R1_PUNCHLIST_CANARY' "$S/prompts/round-2-produce.txt" 2>/dev/null; then
  ok "S4 round-1 findings flowed into round-2 producer prompt"
else
  no "S4 punch-list flow" "R1_PUNCHLIST_CANARY absent from round-2 producer prompt"
fi
if ! grep -qF 'PRODUCER_STATUS_CANARY' "$S/prompts/round-1-review.txt" 2>/dev/null; then
  ok "S4 reviewer isolation (producer canary absent from round-1 review prompt)"
else
  no "S4 reviewer isolation" "producer canary leaked into review prompt"
fi
if ! grep -qF 'TASKSPEC_CANARY_ZZZ' "$S/prompts/round-1-review.txt" 2>/dev/null; then
  ok "S4 reviewer isolation (task-spec canary absent from round-1 review prompt)"
else
  no "S4 reviewer isolation" "task-spec canary leaked into review prompt"
fi

# S5: producer token ban — 2 produce-fail rows then exit 2
S="$ROOT/s5"; setup_scen "$S" '{"rounds_cap":4}'
drive "$S" producer_token "$S/artifact.txt"
expect_exit 2 "S5 producer token ban"
PF=$(cntE '^## R[0-9]+\.produce - .* - fail$' "$S/rounds.md")
if [ "$PF" = 2 ]; then ok "S5 two produce-fail rows"; else no "S5 produce-fail rows" "found $PF, expected 2"; fi

# S6: producer STATUS FAILED -> exit 2
S="$ROOT/s6"; setup_scen "$S"
drive "$S" failed "$S/artifact.txt"
expect_exit 2 "S6 producer FAILED"
if grep -qF 'escalate:producer_STATUS:_FAILED' "$S/rounds.md" 2>/dev/null; then ok "S6 escalate row cites FAILED"; else no "S6 escalate row" "FAILED reason absent"; fi

# S7: PARTIAL then DONE on resume -> exit 0, resume argv carries session id
S="$ROOT/s7"; setup_scen "$S"
drive "$S" partial_then_done "$S/artifact.txt"
expect_exit 0 "S7 PARTIAL then DONE"
if grep -F '"--resume"' "$S/mock/calls.jsonl" 2>/dev/null | grep -qF '"sess-resume-me"'; then
  ok "S7 resume invocation carries --resume sess-resume-me"
else
  no "S7 resume argv" "--resume with session id not found"
fi

# S8: PARTIAL twice -> exit 2
S="$ROOT/s8"; setup_scen "$S"
drive "$S" partial_twice "$S/artifact.txt"
expect_exit 2 "S8 PARTIAL twice"

# S9a: malformed VERDICT -> one retry (2 review invocations) then exit 2
S="$ROOT/s9a"; setup_scen "$S"
drive "$S" malformed_verdict "$S/artifact.txt"
expect_exit 2 "S9a malformed VERDICT"
RV=$(cntF '"w-mock-reviewer"' "$S/mock/calls.jsonl")
if [ "$RV" = 2 ]; then ok "S9a two review invocations (one retry)"; else no "S9a review retries" "found $RV, expected 2"; fi

# S9b: round-mismatch VERDICT -> one retry then exit 2
S="$ROOT/s9b"; setup_scen "$S"
drive "$S" round_mismatch "$S/artifact.txt"
expect_exit 2 "S9b round-mismatch VERDICT"
RV=$(cntF '"w-mock-reviewer"' "$S/mock/calls.jsonl")
if [ "$RV" = 2 ]; then ok "S9b two review invocations (one retry)"; else no "S9b review retries" "found $RV, expected 2"; fi

# S10: reviewer emits SEAL -> malformed path, exit 2
S="$ROOT/s10"; setup_scen "$S"
drive "$S" reviewer_seal "$S/artifact.txt"
expect_exit 2 "S10 reviewer emits SEAL"
RV=$(cntF '"w-mock-reviewer"' "$S/mock/calls.jsonl")
if [ "$RV" = 2 ]; then ok "S10 two review invocations"; else no "S10 review retries" "found $RV, expected 2"; fi
if grep -qF 'emitted_SEAL' "$S/rounds.md" 2>/dev/null; then ok "S10 escalate row cites SEAL emission"; else no "S10 escalate row" "SEAL reason absent"; fi

# S11: seal missing revision -> retry (2 seal invocations) then exit 2
S="$ROOT/s11"; setup_scen "$S"
drive "$S" seal_no_revision "$S/artifact.txt"
expect_exit 2 "S11 seal missing revision"
SV=$(cntF '"w-mock-seal"' "$S/mock/calls.jsonl")
if [ "$SV" = 2 ]; then ok "S11 two seal invocations (one retry)"; else no "S11 seal retries" "found $SV, expected 2"; fi

# S12: seal void — artifact mutated during seal -> void row precedes escalate, exit 2
S="$ROOT/s12"; setup_scen "$S"
drive "$S" seal_void "$S/artifact.txt"
expect_exit 2 "S12 seal void"
VOID_LN=$(grep -nE '^## R[0-9]+\.seal - .* - void$' "$S/rounds.md" 2>/dev/null | head -1 | cut -d: -f1)
ESC_LN=$(grep -nF 'escalate:artifact_mutated_during_seal' "$S/rounds.md" 2>/dev/null | head -1 | cut -d: -f1)
if [ -n "$VOID_LN" ] && [ -n "$ESC_LN" ] && [ "$VOID_LN" -lt "$ESC_LN" ]; then
  ok "S12 seal void row precedes escalate row"
else
  no "S12 void ordering" "void_line=$VOID_LN escalate_line=$ESC_LN"
fi

# S13: strict bar — two seal invocations, two seal rows, exit 0
S="$ROOT/s13"; setup_scen "$S" '{"bar":"strict"}'
drive "$S" happy "$S/artifact.txt"
expect_exit 0 "S13 strict bar"
SV=$(cntF '"w-mock-seal"' "$S/mock/calls.jsonl")
SR=$(cntE '^## R[0-9]+\.seal - .* - ok$' "$S/rounds.md")
if [ "$SV" = 2 ] && [ "$SR" = 2 ]; then ok "S13 two seal invocations + two seal rows"; else no "S13 double seal" "invocations=$SV rows=$SR"; fi

# S14: trend guard — equal nonzero findings twice -> exit 2 citing trend
S="$ROOT/s14"; setup_scen "$S" '{"rounds_cap":4}'
drive "$S" trend "$S/artifact.txt"
expect_exit 2 "S14 trend guard"
if grep -qF 'escalate:findings_did_not_decrease_across_2_rounds' "$S/rounds.md" 2>/dev/null; then
  ok "S14 escalate row cites the trend guard"
else
  no "S14 trend citation" "trend reason absent"
fi

# S15: rounds cap — strictly decreasing findings never reach clean -> exit 2 at cap
S="$ROOT/s15"; setup_scen "$S" '{"rounds_cap":3}'
drive "$S" cap "$S/artifact.txt"
expect_exit 2 "S15 rounds cap"
PR=$(cntE '^## R[0-9]+\.produce - .* - ok$' "$S/rounds.md")
if [ "$PR" = 3 ] && grep -qF 'escalate:rounds_cap_reached_without_a_seal' "$S/rounds.md" 2>/dev/null; then
  ok "S15 three produce rows + cap escalate"
else
  no "S15 cap" "produce_rows=$PR (expected 3) or cap reason absent"
fi

# S16: budget breach -> exit 2
S="$ROOT/s16"; setup_scen "$S"
drive "$S" budget "$S/artifact.txt"
expect_exit 2 "S16 budget breach"
if grep -qF 'escalate:produce_budget_breach' "$S/rounds.md" 2>/dev/null; then ok "S16 escalate row cites budget"; else no "S16 budget citation" "budget reason absent"; fi

# S17: phase timeout -> exit 2
S="$ROOT/s17"; setup_scen "$S" '{"phase_timeout_s":2}'
drive "$S" timeout "$S/artifact.txt"
expect_exit 2 "S17 phase timeout"
if grep -qF 'escalate:produce_phase_timeout' "$S/rounds.md" 2>/dev/null; then ok "S17 escalate row cites timeout"; else no "S17 timeout citation" "timeout reason absent"; fi

# S18: preflight — /bin/false and a nonexistent path both exit 4
S="$ROOT/s18a"; setup_scen "$S"
CLAUDE_CMD="/bin/false"; drive "$S" happy "$S/artifact.txt"; CLAUDE_CMD="$MOCK"
expect_exit 4 "S18 preflight /bin/false"
S="$ROOT/s18b"; setup_scen "$S"
CLAUDE_CMD="$ROOT/does-not-exist-claude"; drive "$S" happy "$S/artifact.txt"; CLAUDE_CMD="$MOCK"
expect_exit 4 "S18 preflight nonexistent path"

# S19: dirty-start refusal on a real git repo; allow_dirty=true proceeds
REPO="$ROOT/gitrepo"; mkdir -p "$REPO"
git -C "$REPO" init -q
printf 'committed content\n' > "$REPO/artifact.txt"
git -C "$REPO" add artifact.txt
git -C "$REPO" -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
printf 'uncommitted modification\n' >> "$REPO/artifact.txt"
S="$ROOT/s19a"; setup_scen "$S" "{\"repo\":\"$REPO\"}"
drive "$S" happy "$REPO/artifact.txt"
expect_exit 3 "S19 dirty-start refusal (allow_dirty=false)"
S="$ROOT/s19b"; setup_scen "$S" "{\"repo\":\"$REPO\",\"allow_dirty\":true}"
drive "$S" happy "$REPO/artifact.txt"
expect_exit 0 "S19 allow_dirty=true proceeds past the check"

# S20: review-phase mutation -> exit 2 with mutated-during-review escalate row
S="$ROOT/s20"; setup_scen "$S"
drive "$S" review_mutation "$S/artifact.txt"
expect_exit 2 "S20 review-phase mutation"
if grep -qF 'escalate:artifact_mutated_during_review' "$S/rounds.md" 2>/dev/null; then
  ok "S20 escalate row cites review mutation"
else
  no "S20 review-mutation citation" "reason absent"
fi

# S21: flag assertions from the argv log (dedicated happy run)
S="$ROOT/s21"; setup_scen "$S" '{"bar":"gate"}'
drive "$S" happy "$S/artifact.txt"
if python3 "$FLAGCHK" "$S/mock/calls.jsonl" w-mock-producer w-mock-reviewer w-mock-seal 2>"$S/flagerr.txt"; then
  ok "S21 review/seal carry --no-session-persistence + --disallowedTools Write,Edit,NotebookEdit; produce carries neither + --permission-mode acceptEdits"
else
  no "S21 flag assertions" "$(cat "$S/flagerr.txt")"
fi

# S22: doc-code consistency (full enumeration, no head-limits)
if [ -f "$SKILL_MD" ]; then
  NSP=$(cntF '--no-session-persistence' "$SKILL_MD")
  DAT=$(cntF '--disallowedTools' "$SKILL_MD")
  DENYDOC=$(cntF 'Write,Edit,NotebookEdit' "$SKILL_MD")
  DENY=$(cntF 'Write,Edit,NotebookEdit' "$REAL_DRIVER")
  if [ "$NSP" -ge 1 ] && [ "$DAT" -ge 1 ] && [ "$DENYDOC" -ge 1 ] && [ "$DENY" -ge 1 ]; then
    ok "S22 doc-code consistency (SKILL.md documents --no-session-persistence + --disallowedTools + the literal Write,Edit,NotebookEdit deny list; driver READ_ONLY_DENY matches)"
  else
    no "S22 doc-code consistency" "skill --no-session-persistence=$NSP --disallowedTools=$DAT deny-list-in-doc=$DENYDOC driver Write,Edit,NotebookEdit=$DENY"
  fi
else
  no "S22 doc-code consistency" "wf-auto/SKILL.md not found"
fi

# ═══════════════════════════════════════════════════
# Mutation harness (genuineness proof) — behind --mutations
# ═══════════════════════════════════════════════════
run_mutation() {
  # $1 name  $2 old  $3 new  $4 scenario  $5 setup-overrides  $6 artifact-path
  local name="$1" old="$2" new="$3" scen="$4" ov="$5" art="$6"
  local mut="$ROOT/${name}.py" md="$ROOT/${name}-run"
  cp "$REAL_DRIVER" "$mut"
  if ! python3 "$MUTATE" "$mut" "$old" "$new" 2>"$ROOT/${name}.err"; then
    no "$name apply" "$(cat "$ROOT/${name}.err")"
    rm -f "$mut"; return
  fi
  if ! python3 -m py_compile "$mut" 2>"$ROOT/${name}.comp"; then
    no "$name compile" "$(cat "$ROOT/${name}.comp")"
    rm -f "$mut"; return
  fi
  setup_scen "$md" "$ov"
  DRIVER_PY="$mut"; drive "$md" "$scen" "$art"; DRIVER_PY="$REAL_DRIVER"
  MUT_MD="$md"
  rm -f "$mut"
}

if [ "$DO_MUTATIONS" = true ]; then
  sect "mutation harness (each mutation must break its paired scenario)"

  # M1: disable producer token check -> S5 loses its produce-fail rows
  run_mutation m1 \
    'if PRODUCER_TOKEN_RE.search(result):' \
    'if False and PRODUCER_TOKEN_RE.search(result):' \
    producer_token '{"rounds_cap":4}' "$ROOT/m1-run/artifact.txt"
  PF=$(cntE '^## R[0-9]+\.produce - .* - fail$' "$MUT_MD/rounds.md")
  if [ "$PF" != 2 ]; then ok "M1 producer-token guard (mutant yields $PF produce-fail rows, not 2)"; else no "M1" "guard still fired under mutation"; fi

  # M2: remove H_post recompute -> S12 no longer voids the seal
  run_mutation m2 \
    'if self.compute_manifest() != h_pre:' \
    'if False and self.compute_manifest() != h_pre:' \
    seal_void '' "$ROOT/m2-run/artifact.txt"
  if [ "$EXIT" != 2 ]; then ok "M2 seal-void guard (mutant exit=$EXIT, not 2)"; else no "M2" "seal still voided under mutation"; fi

  # M3: drop --no-session-persistence -> S21 flag_check fails
  run_mutation m3 \
    'cmd += ["--no-session-persistence", "--disallowedTools", READ_ONLY_DENY]' \
    'cmd += ["--disallowedTools", READ_ONLY_DENY]' \
    happy '{"bar":"gate"}' "$ROOT/m3-run/artifact.txt"
  if ! python3 "$FLAGCHK" "$MUT_MD/mock/calls.jsonl" w-mock-producer w-mock-reviewer w-mock-seal >/dev/null 2>&1; then
    ok "M3 session-persistence flag guard (flag_check fails under mutation)"
  else
    no "M3" "flag_check still passed under mutation"
  fi

  # M4: disable trend_stalled -> S14 escalates at cap, not on trend
  run_mutation m4 \
    'return len(history) >= 2 and history[-1] >= history[-2] and history[-1] > 0' \
    'return False' \
    trend '{"rounds_cap":4}' "$ROOT/m4-run/artifact.txt"
  if ! grep -qF 'escalate:findings_did_not_decrease_across_2_rounds' "$MUT_MD/rounds.md" 2>/dev/null; then
    ok "M4 trend guard (trend escalate row absent under mutation)"
  else
    no "M4" "trend escalate still fired under mutation"
  fi

  # M5: disable dirty-tree refusal -> S19 dirty case no longer exits 3
  run_mutation m5 \
    'if not self.cfg.get("repo") or self.cfg.get("allow_dirty"):' \
    'if True:' \
    happy "{\"repo\":\"$REPO\"}" "$REPO/artifact.txt"
  if [ "$EXIT" != 3 ]; then ok "M5 dirty-start guard (mutant exit=$EXIT, not 3)"; else no "M5" "dirty refusal still fired under mutation"; fi
fi

# ── Driver-file integrity (real file untouched end to end) ──
SHA_AFTER=$(sha256sum "$REAL_DRIVER" | awk '{print $1}')
if [ "$SHA_BEFORE" = "$SHA_AFTER" ]; then
  ok "driver file sha256 unchanged ($SHA_AFTER)"
else
  no "driver integrity" "sha256 changed: $SHA_BEFORE -> $SHA_AFTER"
fi

# ── Summary ──
echo ""
echo "======================================================="
printf "converge-auto tests: %d passed, %d failed\n" "$PASS" "$FAIL"
echo "======================================================="
[ "$FAIL" -gt 0 ] && exit 1
exit 0
