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

# Hermetic policy env: no ambient /commit false leaks into the non-policy scenarios.
# The driver's policy detector reads CONVERGE_NO_COMMIT_FILE (default the real
# ~/.claude/hooks/no-commit-projects.local); point it at an empty temp list so only
# the policy scenarios (which override it per-run) ever see a no-commit repo.
unset CLAUDE_COMMIT_POLICY
EMPTY_NO_COMMIT="$ROOT/empty-no-commit-list"
: > "$EMPTY_NO_COMMIT"
export CONVERGE_NO_COMMIT_FILE="$EMPTY_NO_COMMIT"

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
import json, os, re, subprocess, sys, time

argv = sys.argv
MOCK_DIR = os.environ["MOCK_DIR"]
def _resolve_scenario():
    # A per-loop .mock-scenario in cwd (the loop's runtime dir / repo) wins over the
    # global MOCK_SCENARIO env, so --parallel loops sharing one env still differ.
    f = os.path.join(os.getcwd(), ".mock-scenario")
    if os.path.isfile(f):
        return open(f).read().strip()
    return os.environ.get("MOCK_SCENARIO", "happy")
SCEN = _resolve_scenario()
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
    # bound revision is labelled commit:<12hex> (git) or sha256:<12hex> (non-git)
    m = re.search(r"(?:commit|sha256):([0-9a-f]{12})", prompt)
    return m.group(1) if m else "deadbeef0000"

def _targets():
    # Single-loop tests pin MOCK_ARTIFACT; parallel loops leave it empty and let the
    # producer prompt's allowlist name the file(s), resolved against the run cwd.
    if ART:
        return [ART]
    m = re.search(r"the allowlist\): (.+?)\. Touch nothing", prompt)
    return [p.strip() for p in m.group(1).split(",")] if m else []

def write_art(txt):
    for path in _targets():
        with open(path, "w") as fh:
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

elif SCEN == "git_seal_commit":
    if role in ("produce", "produce-resume"):
        default_produce()
    if role == "review":
        default_review()
    if role == "seal":
        # move HEAD during the seal audit: guard 3 must void on the HEAD change
        repo = os.path.dirname(ART)
        with open(ART, "a") as fh:
            fh.write("mock mid-seal commit change\n")
        subprocess.run(["git", "-C", repo, "add", "artifact.txt"], capture_output=True)
        subprocess.run(["git", "-C", repo, "-c", "user.email=m@m", "-c", "user.name=m",
                        "commit", "-qm", "mock mid-seal commit"], capture_output=True)
        default_seal()

elif SCEN == "status_preamble":
    if role in ("produce", "produce-resume"):
        write_art("converged artifact body, no canary token here\n")
        emit("I completed the objective.\nSummary of changes follows.\nSTATUS: DONE files=1 checkpoint=cp.md\ntrailing note")
    if role == "review":
        default_review()
    if role == "seal":
        default_seal()

elif SCEN == "verdict_preamble":
    if role in ("produce", "produce-resume"):
        default_produce()
    if role == "review":
        emit("Here is my analysis of the diff.\nEverything checks out against the rubric.\nVERDICT: CLEAN blocking=0 major=0 minor=0 round=%s\ntrailing note" % round_of())
    if role == "seal":
        default_seal()

elif SCEN == "two_verdicts":
    if role in ("produce", "produce-resume"):
        default_produce()
    if role == "review":
        r = round_of()
        emit("VERDICT: CLEAN blocking=0 major=0 minor=0 round=%s\nVERDICT: REWORK blocking=0 major=1 minor=0 round=%s\nconflicting pair" % (r, r))
    if role == "seal":
        default_seal()

elif SCEN == "seal_preamble":
    if role in ("produce", "produce-resume"):
        default_produce()
    if role == "review":
        default_review()
    if role == "seal":
        emit("Holistic audit complete.\nNo blocking issues found.\nSEAL: ACCEPTED blocking=0 major=0 minor=0 nits=0\nexamined revision %s" % revision_of())

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

# Create a clean git repo whose artifact.txt is committed (clean start).
# $2 is written with %b, so "\n" produces a real newline (exact byte match).
mk_git_repo() {
  local repo="$1" content="$2"
  mkdir -p "$repo"
  git -C "$repo" init -q
  git -C "$repo" config user.email t@t
  git -C "$repo" config user.name t
  printf '%b' "$content" > "$repo/artifact.txt"
  git -C "$repo" add artifact.txt
  git -C "$repo" -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
}

# Content manifest of a repo's paths, mirroring converge_auto.py compute_manifest()
# and seal-void-hook.sh recompute_manifest (seeds the sealed_manifest fixture below).
manifest_of() {
  local repo="$1"; shift
  python3 - "$repo" "$@" <<'PYEOF'
import hashlib, os, sys
repo = sys.argv[1]; paths = sys.argv[2:]
entries = []
for rel in paths:
    p = rel if os.path.isabs(rel) else os.path.join(repo, rel)
    try:
        with open(p, "rb") as fh:
            d = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        d = "MISSING"
    entries.append("%s:%s" % (rel, d))
sys.stdout.write(hashlib.sha256("\n".join(sorted(entries)).encode("utf-8")).hexdigest())
PYEOF
}

# Stage a sealed converge-auto loop under a FAKE HOME so the hook scan never
# reaches the real ~/.claude/plans. Records sealed_manifest of the CURRENT artifact
# (the content-precise void hook needs it). Echoes the runtime dir it created.
write_sealed_loop() {
  local fh="$1" repo="$2" bound="$3" round="$4"
  local rt="$fh/.claude/plans/testcampaign/auto/testloop"
  mkdir -p "$rt"
  local sm; sm="$(manifest_of "$repo" artifact.txt)"
  printf '{"loop_id":"testloop","repo":"%s","artifact_paths":["artifact.txt"]}\n' "$repo" > "$rt/loop.json"
  printf '{"loop_id":"testloop","round":%s,"status":"sealed","seal":{"status":"ACCEPTED","bound_revision":"%s","sealed_manifest":"%s","line":"SEAL: ACCEPTED blocking=0 major=0 minor=0 nits=0"}}\n' "$round" "$bound" "$sm" > "$rt/handoff.json"
  printf '# ledger\n\n' > "$rt/rounds.md"
  echo "$rt"
}

BASHBIN="$(command -v bash)"

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
sect "git-mode seal (commit-hash binding)"

seal_manifest_of() { awk '/^## R.*\.seal - .* - ok$/{f=1} f&&/^- manifest:/{print $3; f=0}' "$1" | head -1; }
term_manifest_of() { awk '/^## R.*\.terminal - /{f=1} f&&/^- manifest:/{print $3; f=0}' "$1" | head -1; }

# S23: happy git seal: pre-seal snapshot commit + commit-hash binding
GREPO="$ROOT/gs23"; mk_git_repo "$GREPO" "seed content, will be overwritten\n"
S="$ROOT/s23"; setup_scen "$S" "{\"repo\":\"$GREPO\",\"bar\":\"gate\"}"
drive "$S" happy "$GREPO/artifact.txt"
expect_exit 0 "S23 git happy seal"
SUBJ=$(git -C "$GREPO" log -1 --format=%s)
if [ "$SUBJ" = "chore(testloop): round 1 pre-seal snapshot" ]; then ok "S23 pre-seal commit subject exact"; else no "S23 commit subject" "got: $SUBJ"; fi
if git -C "$GREPO" log -1 --format=%B | grep -qF 'Co-Authored-By: Claude <noreply@anthropic.com>'; then ok "S23 co-author trailer present"; else no "S23 co-author" "trailer absent"; fi
CSM=$(seal_manifest_of "$S/rounds.md"); CTM=$(term_manifest_of "$S/rounds.md")
HEAD12=$(git -C "$GREPO" rev-parse HEAD | cut -c1-12)
if [ "$CSM" = "commit:$HEAD12" ] && [ "$CSM" = "$CTM" ]; then ok "S23 seal+terminal bind commit:$HEAD12 == HEAD"; else no "S23 commit binding" "seal=$CSM terminal=$CTM head=commit:$HEAD12"; fi

# S24: git seal void: scope mutated during seal (porcelain non-empty) -> exit 2
GREPO="$ROOT/gs24"; mk_git_repo "$GREPO" "seed\n"
S="$ROOT/s24"; setup_scen "$S" "{\"repo\":\"$GREPO\"}"
drive "$S" seal_void "$GREPO/artifact.txt"
expect_exit 2 "S24 git seal void (scope dirty during seal)"
VOID_LN=$(grep -nE '^## R[0-9]+\.seal - .* - void$' "$S/rounds.md" 2>/dev/null | head -1 | cut -d: -f1)
ESC_LN=$(grep -nF 'escalate:artifact_mutated_during_seal' "$S/rounds.md" 2>/dev/null | head -1 | cut -d: -f1)
if [ -n "$VOID_LN" ] && [ -n "$ESC_LN" ] && [ "$VOID_LN" -lt "$ESC_LN" ]; then ok "S24 void row (commit-bound) precedes escalate"; else no "S24 void ordering" "void=$VOID_LN esc=$ESC_LN"; fi

# S25: git seal void on HEAD move: mock commits during seal -> exit 2
GREPO="$ROOT/gs25"; mk_git_repo "$GREPO" "seed\n"
S="$ROOT/s25"; setup_scen "$S" "{\"repo\":\"$GREPO\"}"
drive "$S" git_seal_commit "$GREPO/artifact.txt"
expect_exit 2 "S25 git seal void (HEAD moved during seal)"
if grep -qE '^## R[0-9]+\.seal - .* - void$' "$S/rounds.md" 2>/dev/null; then ok "S25 void row present on HEAD move"; else no "S25 void row" "absent"; fi

# S26: strict git bar: two seals, HEAD stable between them -> exit 0
GREPO="$ROOT/gs26"; mk_git_repo "$GREPO" "seed\n"
S="$ROOT/s26"; setup_scen "$S" "{\"repo\":\"$GREPO\",\"bar\":\"strict\"}"
drive "$S" happy "$GREPO/artifact.txt"
expect_exit 0 "S26 strict git two seals HEAD stable"
SV=$(cntF '"w-mock-seal"' "$S/mock/calls.jsonl")
SR=$(cntE '^## R[0-9]+\.seal - .* - ok$' "$S/rounds.md")
if [ "$SV" = 2 ] && [ "$SR" = 2 ]; then ok "S26 two seal invocations + two seal rows (git strict)"; else no "S26 double seal" "invocations=$SV rows=$SR"; fi

# S27: nothing-to-commit reuse: producer writes identical bytes -> reuse HEAD
GREPO="$ROOT/gs27"; mk_git_repo "$GREPO" "converged artifact body, no canary token here\n"
S="$ROOT/s27"; setup_scen "$S" "{\"repo\":\"$GREPO\"}"
drive "$S" happy "$GREPO/artifact.txt"
expect_exit 0 "S27 nothing-to-commit reuse of HEAD"
NCOMMITS=$(git -C "$GREPO" rev-list --count HEAD)
if [ "$NCOMMITS" = 1 ]; then ok "S27 no pre-seal commit created (reused HEAD)"; else no "S27 commit count" "expected 1 got $NCOMMITS"; fi
CSM=$(seal_manifest_of "$S/rounds.md"); HEAD12=$(git -C "$GREPO" rev-parse HEAD | cut -c1-12)
if [ "$CSM" = "commit:$HEAD12" ]; then ok "S27 seal binds reused HEAD commit:$HEAD12"; else no "S27 reuse binding" "seal=$CSM head=commit:$HEAD12"; fi

# ═══════════════════════════════════════════════════
sect "seal-void-hook (post-commit hardening)"

# H3: install is idempotent: run twice, single hook, no spurious chain
HREPO="$ROOT/hrepo3"; mk_git_repo "$HREPO" "seed\n"
python3 "$DRIVER_PY" --install-void-hook "$HREPO" >/dev/null 2>&1
python3 "$DRIVER_PY" --install-void-hook "$HREPO" >"$ROOT/h3b.out" 2>&1
if grep -qF 'idempotent no-op' "$ROOT/h3b.out"; then ok "H3 second install is idempotent no-op"; else no "H3 idempotent" "$(cat "$ROOT/h3b.out")"; fi
if [ ! -f "$HREPO/.git/hooks/post-commit.chained" ]; then ok "H3 no spurious chain on double install"; else no "H3 chain" "post-commit.chained created without a prior hook"; fi

# H4: an existing foreign post-commit is chained and still executes on commit
HREPO="$ROOT/hrepo4"; mk_git_repo "$HREPO" "seed\n"
cat > "$HREPO/.git/hooks/post-commit" <<EOF
#!/bin/bash
touch "$ROOT/h4-foreign-ran"
EOF
chmod +x "$HREPO/.git/hooks/post-commit"
python3 "$DRIVER_PY" --install-void-hook "$HREPO" >"$ROOT/h4.out" 2>&1
if grep -qF 'chained existing post-commit' "$ROOT/h4.out" && [ -f "$HREPO/.git/hooks/post-commit.chained" ]; then ok "H4 existing post-commit moved to post-commit.chained"; else no "H4 chaining" "$(cat "$ROOT/h4.out")"; fi
rm -f "$ROOT/h4-foreign-ran"
printf 'extra\n' >> "$HREPO/artifact.txt"; git -C "$HREPO" add artifact.txt
HOME="$ROOT/fh4-empty" git -C "$HREPO" -c user.email=t@t -c user.name=t commit -qm c2 >/dev/null 2>&1
if [ -f "$ROOT/h4-foreign-ran" ]; then ok "H4 chained foreign hook still executes on commit"; else no "H4 chain exec" "foreign hook did not run"; fi

# H5: hook exits 0 (non-blocking) when jq is absent: empty PATH, absolute bash
HREPO="$ROOT/hrepo5"; mk_git_repo "$HREPO" "seed\n"
python3 "$DRIVER_PY" --install-void-hook "$HREPO" >/dev/null 2>&1
if env -i PATH="" HOME="$ROOT/fh5-empty" "$BASHBIN" "$HREPO/.git/hooks/post-commit" >/dev/null 2>&1; then ok "H5 hook exits 0 when jq is absent (non-blocking)"; else no "H5 jq-absent" "hook returned nonzero"; fi

if command -v jq >/dev/null 2>&1; then
  # H1: post-hoc commit touching a sealed loop's scope -> void row + VOIDED marker
  HREPO="$ROOT/hrepo1"; mk_git_repo "$HREPO" "seed\n"
  BOUND=$(git -C "$HREPO" rev-parse HEAD); BOUND12=$(printf '%s' "$BOUND" | cut -c1-12)
  FH="$ROOT/fh1"; RT=$(write_sealed_loop "$FH" "$HREPO" "$BOUND" 2)
  python3 "$DRIVER_PY" --install-void-hook "$HREPO" >/dev/null 2>&1
  printf 'post-hoc change\n' >> "$HREPO/artifact.txt"; git -C "$HREPO" add artifact.txt
  HOME="$FH" git -C "$HREPO" -c user.email=t@t -c user.name=t commit -qm post-hoc >/dev/null 2>&1
  if grep -qF 'escalate:seal_voided_post_hoc' "$RT/rounds.md" 2>/dev/null; then ok "H1 void row appended by hook"; else no "H1 void row" "absent"; fi
  if grep -qE '^## R[0-9]+\.escalate - [^ ]+ - escalate:seal_voided_post_hoc$' "$RT/rounds.md" 2>/dev/null; then ok "H1 void row header conforms to ledger schema"; else no "H1 header" "nonconforming"; fi
  if grep -qF "manifest: commit:$BOUND12" "$RT/rounds.md" 2>/dev/null; then ok "H1 void row names OLD bound commit:$BOUND12"; else no "H1 bound revision" "absent"; fi
  if [ -f "$RT/VOIDED" ]; then ok "H1 VOIDED marker created"; else no "H1 VOIDED marker" "absent"; fi

  # H2: a commit NOT touching the sealed scope leaves the loop untouched
  HREPO="$ROOT/hrepo2"; mk_git_repo "$HREPO" "seed\n"
  BOUND=$(git -C "$HREPO" rev-parse HEAD)
  FH="$ROOT/fh2"; RT=$(write_sealed_loop "$FH" "$HREPO" "$BOUND" 2)
  python3 "$DRIVER_PY" --install-void-hook "$HREPO" >/dev/null 2>&1
  printf 'unrelated\n' > "$HREPO/other.txt"; git -C "$HREPO" add other.txt
  HOME="$FH" git -C "$HREPO" -c user.email=t@t -c user.name=t commit -qm other >/dev/null 2>&1
  if ! grep -qF 'escalate:seal_voided_post_hoc' "$RT/rounds.md" 2>/dev/null && [ ! -f "$RT/VOIDED" ]; then ok "H2 out-of-scope commit leaves sealed loop untouched"; else no "H2 out-of-scope" "hook voided a loop it should have ignored"; fi

  # H6: content-precision. A byte-identical re-commit (amend/message rewrite) of the
  # sealed scope must NOT void (new HEAD hash, identical content manifest).
  HREPO="$ROOT/hrepo6"; mk_git_repo "$HREPO" "sealed body content\n"
  BOUND=$(git -C "$HREPO" rev-parse HEAD)
  FH="$ROOT/fh6"; RT=$(write_sealed_loop "$FH" "$HREPO" "$BOUND" 2)
  python3 "$DRIVER_PY" --install-void-hook "$HREPO" >/dev/null 2>&1
  HOME="$FH" git -C "$HREPO" -c user.email=t@t -c user.name=t commit --amend --no-edit -q >/dev/null 2>&1
  if ! grep -qF 'escalate:seal_voided_post_hoc' "$RT/rounds.md" 2>/dev/null && [ ! -f "$RT/VOIDED" ]; then
    ok "H6 byte-identical re-commit (amend) leaves the seal intact (content-precise)"
  else
    no "H6 content-precision" "byte-identical re-commit wrongly voided the seal"
  fi

  # H7: content-precision. A genuine content change to the sealed scope DOES void.
  HREPO="$ROOT/hrepo7"; mk_git_repo "$HREPO" "sealed body content\n"
  BOUND=$(git -C "$HREPO" rev-parse HEAD)
  FH="$ROOT/fh7"; RT=$(write_sealed_loop "$FH" "$HREPO" "$BOUND" 2)
  python3 "$DRIVER_PY" --install-void-hook "$HREPO" >/dev/null 2>&1
  printf 'genuine content change\n' >> "$HREPO/artifact.txt"; git -C "$HREPO" add artifact.txt
  HOME="$FH" git -C "$HREPO" -c user.email=t@t -c user.name=t commit -qm change >/dev/null 2>&1
  if grep -qF 'escalate:seal_voided_post_hoc' "$RT/rounds.md" 2>/dev/null && [ -f "$RT/VOIDED" ]; then
    ok "H7 genuine content change voids the seal (content-precise)"
  else
    no "H7 content-precision" "genuine content change failed to void"
  fi
else
  no "H1/H2 hook void detection" "jq not available on this host"
fi

# ═══════════════════════════════════════════════════
sect "tolerant token extraction (live-escalation fix)"

# S28: reviewer VERDICT behind a narrative preamble: parses and proceeds (regression)
S="$ROOT/s28"; setup_scen "$S" '{"bar":"gate"}'
drive "$S" verdict_preamble "$S/artifact.txt"
expect_exit 0 "S28 VERDICT behind preamble parses (live-escalation regression)"
RV=$(cntF '"w-mock-reviewer"' "$S/mock/calls.jsonl")
if [ "$RV" = 1 ]; then ok "S28 single review invocation (no retry needed)"; else no "S28 no retry" "found $RV review invocations, expected 1"; fi

# S29: producer STATUS behind a preamble: parses
S="$ROOT/s29"; setup_scen "$S"
drive "$S" status_preamble "$S/artifact.txt"
expect_exit 0 "S29 STATUS behind preamble parses"

# S30: two valid VERDICT lines: malformed path (retry then escalate)
S="$ROOT/s30"; setup_scen "$S"
drive "$S" two_verdicts "$S/artifact.txt"
expect_exit 2 "S30 duplicate VERDICT lines rejected"
RV=$(cntF '"w-mock-reviewer"' "$S/mock/calls.jsonl")
if [ "$RV" = 2 ]; then ok "S30 two review invocations (one retry) then escalate"; else no "S30 retry count" "found $RV, expected 2"; fi

# S31: seal SEAL behind a preamble (with revision string): parses
S="$ROOT/s31"; setup_scen "$S"
drive "$S" seal_preamble "$S/artifact.txt"
expect_exit 0 "S31 SEAL behind preamble parses"

# ═══════════════════════════════════════════════════
sect "policy composition (/commit false)"

# S32: /commit false git repo (basename in the no-commit list) -> sha256 binding,
# ZERO commits, snapshot-based reviewer diff, policy recorded in ledger + driver.log
NCREPO="$ROOT/nocommitproj"; mk_git_repo "$NCREPO" "seed body for policy-false mode\n"
NCLIST="$ROOT/no-commit-list.txt"; printf '# temp no-commit list\nnocommitproj\n' > "$NCLIST"
NBEFORE=$(git -C "$NCREPO" rev-list --count HEAD)
S="$ROOT/s32"; setup_scen "$S" "{\"repo\":\"$NCREPO\",\"bar\":\"gate\"}"
export CONVERGE_NO_COMMIT_FILE="$NCLIST"; drive "$S" happy "$NCREPO/artifact.txt"; export CONVERGE_NO_COMMIT_FILE="$EMPTY_NO_COMMIT"
expect_exit 0 "S32 /commit false seal"
CSM=$(seal_manifest_of "$S/rounds.md")
if echo "$CSM" | grep -qE '^sha256:[0-9a-f]{12}$'; then ok "S32 binds sha256 not commit ($CSM)"; else no "S32 sha256 binding" "seal manifest=$CSM"; fi
NAFTER=$(git -C "$NCREPO" rev-list --count HEAD)
if [ "$NBEFORE" = "$NAFTER" ]; then ok "S32 zero commits made (git log unchanged at $NAFTER)"; else no "S32 zero commits" "before=$NBEFORE after=$NAFTER"; fi
if grep -qF 'policy=/commit-false' "$S/rounds.md" 2>/dev/null; then ok "S32 round-1 produce delta records policy=/commit-false"; else no "S32 ledger policy" "marker absent from rounds.md"; fi
if grep -qF 'policy=/commit-false' "$S/driver.log" 2>/dev/null; then ok "S32 driver.log records policy=/commit-false"; else no "S32 driver.log policy" "marker absent from driver.log"; fi
if grep -qF 'a/artifact.txt' "$S/prompts/round-1-review.txt" 2>/dev/null && grep -qF 'b/artifact.txt' "$S/prompts/round-1-review.txt" 2>/dev/null; then
  ok "S32 reviewer diff comes from the round-0 snapshot mechanism (a/ b/ headers)"
else
  no "S32 snapshot diff" "snapshot a/ b/ headers absent from review prompt"
fi

# S33: a dirty /commit false tree does NOT trigger the dirty-start refusal
NCREPO2="$ROOT/nocommitproj2"; mk_git_repo "$NCREPO2" "committed baseline\n"
printf 'uncommitted dirty line\n' >> "$NCREPO2/artifact.txt"
NCLIST2="$ROOT/no-commit-list2.txt"; printf 'nocommitproj2\n' > "$NCLIST2"
S="$ROOT/s33"; setup_scen "$S" "{\"repo\":\"$NCREPO2\"}"
export CONVERGE_NO_COMMIT_FILE="$NCLIST2"; drive "$S" happy "$NCREPO2/artifact.txt"; export CONVERGE_NO_COMMIT_FILE="$EMPTY_NO_COMMIT"
expect_exit 0 "S33 dirty /commit false tree proceeds (dirty-start refusal skipped)"

# S34: CLAUDE_COMMIT_POLICY=false env forces /commit false even without a list entry
GREPO3="$ROOT/envpolicyrepo"; mk_git_repo "$GREPO3" "seed\n"
NBEFORE=$(git -C "$GREPO3" rev-list --count HEAD)
S="$ROOT/s34"; setup_scen "$S" "{\"repo\":\"$GREPO3\"}"
export CLAUDE_COMMIT_POLICY=false; drive "$S" happy "$GREPO3/artifact.txt"; unset CLAUDE_COMMIT_POLICY
expect_exit 0 "S34 CLAUDE_COMMIT_POLICY=false forces /commit false"
NAFTER=$(git -C "$GREPO3" rev-list --count HEAD); CSM=$(seal_manifest_of "$S/rounds.md")
if [ "$NBEFORE" = "$NAFTER" ] && echo "$CSM" | grep -qE '^sha256:'; then ok "S34 env /commit false: zero commits + sha256 binding"; else no "S34 env policy" "before=$NBEFORE after=$NAFTER seal=$CSM"; fi

# ═══════════════════════════════════════════════════
sect "parallel multi-artifact convergence (--parallel)"

# ledger well-formedness sweep (headers + token lines), reused across P-scenarios
ledger_wellformed() {
  local rf="$1" badhdr badtok
  badhdr=$(grep -E '^## R' "$rf" 2>/dev/null | grep -cvE '^## R[0-9]+\.(produce|gate|review|seal|terminal|escalate) - [^ ]+ - [^ ]+$')
  # shellcheck disable=SC2016  # backticks are literal ledger-token delimiters
  badtok=$(grep -E '^- token:' "$rf" 2>/dev/null | grep -cvE '^- token: (none|`(VERDICT|SEAL): .*`)$')
  [ "${badhdr:-1}" = 0 ] && [ "${badtok:-1}" = 0 ]
}

write_manifest() {
  local out="$1"; shift
  python3 - "$out" "$@" <<'PYEOF'
import json, sys
json.dump({"loops": sys.argv[2:]}, open(sys.argv[1], "w"), indent=2)
PYEOF
}

# Drive the driver in --parallel mode. MOCK_ARTIFACT is empty so each child's
# producer prompt names its own artifact (resolved against that child's cwd).
drive_parallel() {
  local man="$1" md="$2" scen="$3"; shift 3
  rm -rf "$md"; mkdir -p "$md"
  MOCK_DIR="$md" MOCK_SCENARIO="$scen" MOCK_ARTIFACT="" \
    python3 "$DRIVER_PY" --parallel "$man" --claude-cmd "$MOCK" "$@" \
    >"$md/stdout.txt" 2>"$md/stderr.txt"
  EXIT=$?
}

# P1: two non-git happy loops both seal; summary has 2 sealed rows; ledgers well-formed
P1="$ROOT/p1"; mkdir -p "$P1"
setup_scen "$P1/a" '{"loop_id":"p1a"}'
setup_scen "$P1/b" '{"loop_id":"p1b"}'
write_manifest "$P1/manifest.json" "$P1/a/loop.json" "$P1/b/loop.json"
drive_parallel "$P1/manifest.json" "$P1/mock" happy
expect_exit 0 "P1 parallel happy (both non-git loops seal)"
SUM="$P1/parallel-summary.md"
if [ -f "$SUM" ]; then ok "P1 parallel-summary.md written"; else no "P1 summary" "parallel-summary.md absent"; fi
SEALED=$(cntE '^\| p1[ab] \| 0 \| sealed \|' "$SUM")
if [ "$SEALED" = 2 ]; then ok "P1 summary shows 2 sealed rows"; else no "P1 sealed rows" "found $SEALED, expected 2"; fi
if ledger_wellformed "$P1/a/rounds.md" && ledger_wellformed "$P1/b/rounds.md"; then
  ok "P1 both child ledgers well-formed (header+token sweep)"
else
  no "P1 ledger sweep" "a or b child ledger nonconforming"
fi

# P2: mixed outcome — one loop seals (happy), one escalates (trend); parent exit 2
P2="$ROOT/p2"; mkdir -p "$P2"
setup_scen "$P2/a" '{"loop_id":"p2a"}'
setup_scen "$P2/b" '{"loop_id":"p2b","rounds_cap":4}'
printf 'happy\n' > "$P2/a/.mock-scenario"
printf 'trend\n'  > "$P2/b/.mock-scenario"
write_manifest "$P2/manifest.json" "$P2/a/loop.json" "$P2/b/loop.json"
drive_parallel "$P2/manifest.json" "$P2/mock" happy
expect_exit 2 "P2 mixed outcome (one seals, one escalates)"
SUM="$P2/parallel-summary.md"
SEALED=$(cntE '^\| p2a \| 0 \| sealed \|' "$SUM")
ESCAL=$(cntE '^\| p2b \| 2 \| escalated \|' "$SUM")
if [ "$SEALED" = 1 ] && [ "$ESCAL" = 1 ]; then ok "P2 summary shows one sealed one escalated"; else no "P2 summary rows" "sealed=$SEALED escalated=$ESCAL"; fi

# P3: overlapping artifact scope -> exit 3 before any spawn (mock argv log empty)
P3="$ROOT/p3"; mkdir -p "$P3"
setup_scen "$P3/a" "{\"loop_id\":\"p3a\",\"artifact_paths\":[\"$P3/shared.txt\"]}"
setup_scen "$P3/b" "{\"loop_id\":\"p3b\",\"artifact_paths\":[\"$P3/shared.txt\"]}"
P3MAN="$P3/manifest.json"; write_manifest "$P3MAN" "$P3/a/loop.json" "$P3/b/loop.json"
P3MOCK="$P3/mock"
drive_parallel "$P3MAN" "$P3MOCK" happy
expect_exit 3 "P3 overlapping artifact scope rejected"
if [ ! -f "$P3MOCK/calls.jsonl" ]; then ok "P3 no child/mock spawned (argv log empty)"; else no "P3 no spawn" "mock was invoked"; fi
if grep -qF 'artifact scope collision' "$P3MOCK/stderr.txt" 2>/dev/null; then ok "P3 error names the collision"; else no "P3 collision message" "$(cat "$P3MOCK/stderr.txt" 2>/dev/null)"; fi

# P4: manifest with 6 loops -> exit 3 (hard cap 5)
P4="$ROOT/p4"; mkdir -p "$P4"
P4LOOPS=()
for i in 1 2 3 4 5 6; do
  setup_scen "$P4/l$i" "{\"loop_id\":\"p4l$i\"}"
  P4LOOPS+=("$P4/l$i/loop.json")
done
write_manifest "$P4/manifest.json" "${P4LOOPS[@]}"
drive_parallel "$P4/manifest.json" "$P4/mock" happy
expect_exit 3 "P4 six loops exceed the hard cap of 5"
if grep -qF 'hard cap of 5' "$P4/mock/stderr.txt" 2>/dev/null; then ok "P4 error cites the cap"; else no "P4 cap message" "$(cat "$P4/mock/stderr.txt" 2>/dev/null)"; fi

# P5: two git-mode loops in ONE repo, disjoint files -> both seal, both pre-seal
# commits present, no lock-timeout escalation (repo commit lock serializes them)
GREPO5="$ROOT/gp5"; mkdir -p "$GREPO5"
git -C "$GREPO5" init -q
git -C "$GREPO5" config user.email t@t
git -C "$GREPO5" config user.name t
printf 'seed a\n' > "$GREPO5/artifact_a.txt"
printf 'seed b\n' > "$GREPO5/artifact_b.txt"
git -C "$GREPO5" add artifact_a.txt artifact_b.txt
git -C "$GREPO5" -c user.email=t@t -c user.name=t commit -qm init >/dev/null 2>&1
P5="$ROOT/p5"; mkdir -p "$P5"
setup_scen "$P5/a" "{\"loop_id\":\"p5a\",\"repo\":\"$GREPO5\",\"artifact_paths\":[\"artifact_a.txt\"]}"
setup_scen "$P5/b" "{\"loop_id\":\"p5b\",\"repo\":\"$GREPO5\",\"artifact_paths\":[\"artifact_b.txt\"]}"
write_manifest "$P5/manifest.json" "$P5/a/loop.json" "$P5/b/loop.json"
drive_parallel "$P5/manifest.json" "$P5/mock" happy
expect_exit 0 "P5 same-repo serialization (both git loops seal)"
CA=$(git -C "$GREPO5" log --format=%s | grep -cF 'chore(p5a): round 1 pre-seal snapshot')
CB=$(git -C "$GREPO5" log --format=%s | grep -cF 'chore(p5b): round 1 pre-seal snapshot')
if [ "$CA" = 1 ] && [ "$CB" = 1 ]; then ok "P5 both pre-seal commits present in git log"; else no "P5 commits" "p5a=$CA p5b=$CB"; fi
if ! grep -qF 'pre-seal_commit_lock_timeout' "$P5/a/rounds.md" 2>/dev/null && ! grep -qF 'pre-seal_commit_lock_timeout' "$P5/b/rounds.md" 2>/dev/null; then
  ok "P5 no lock-timeout escalation in either loop"
else
  no "P5 lock timeout" "a loop escalated on the commit lock"
fi

# P6: parallel dry-run validates + prints plans, spawns nothing, writes no summary, exit 0
P6="$ROOT/p6"; mkdir -p "$P6"
setup_scen "$P6/a" '{"loop_id":"p6a"}'
setup_scen "$P6/b" '{"loop_id":"p6b"}'
write_manifest "$P6/manifest.json" "$P6/a/loop.json" "$P6/b/loop.json"
drive_parallel "$P6/manifest.json" "$P6/mock" happy --dry-run
expect_exit 0 "P6 parallel dry-run"
if [ ! -f "$P6/mock/calls.jsonl" ]; then ok "P6 dry-run spawned nothing"; else no "P6 dry-run" "mock invoked"; fi
if [ ! -f "$P6/parallel-summary.md" ]; then ok "P6 dry-run wrote no summary"; else no "P6 dry-run summary" "summary written on dry-run"; fi
if grep -qF 'p6a' "$P6/mock/stdout.txt" 2>/dev/null && grep -qF 'p6b' "$P6/mock/stdout.txt" 2>/dev/null; then ok "P6 dry-run printed both loop plans"; else no "P6 dry-run plans" "loop plans absent from stdout"; fi

# P8: a stale commit lock (recorded holder PID is dead) is broken and the loop
# still seals. Small margin via the CONVERGE_LOCK_MARGIN_S env seam so a
# NON-broken lock would escalate fast (exactly what mutation M8 asserts).
GREPO8="$ROOT/gp8"; mk_git_repo "$GREPO8" "seed p8\n"
mkdir -p "$GREPO8/.git/converge-auto-commit.lock"
sleep 0.01 & P8DEAD=$!
wait "$P8DEAD" 2>/dev/null
printf '%s' "$P8DEAD" > "$GREPO8/.git/converge-auto-commit.lock/pid"
S="$ROOT/p8s"; setup_scen "$S" "{\"loop_id\":\"p8loop\",\"repo\":\"$GREPO8\",\"phase_timeout_s\":5}"
export CONVERGE_LOCK_MARGIN_S=2
drive "$S" happy "$GREPO8/artifact.txt"
unset CONVERGE_LOCK_MARGIN_S
expect_exit 0 "P8 stale commit lock broken, loop seals"
if grep -qF 'breaking stale commit lock' "$S/driver.log" 2>/dev/null; then ok "P8 stale-break logged"; else no "P8 stale-break log" "log line absent"; fi
if [ ! -d "$GREPO8/.git/converge-auto-commit.lock" ]; then ok "P8 lock released after run"; else no "P8 lock release" "lock dir persists"; fi

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

  # M2: neutralize the git-mode seal post-check -> paired git seal-void no longer voids
  GREPO_M2="$ROOT/m2-gitrepo"; mk_git_repo "$GREPO_M2" "seed\n"
  run_mutation m2 \
    'return self._git_head() == bound_full and self._git_scope_dirty() is False' \
    'return True' \
    seal_void "{\"repo\":\"$GREPO_M2\"}" "$GREPO_M2/artifact.txt"
  if [ "$EXIT" != 2 ]; then ok "M2 git seal-void guard (mutant exit=$EXIT, not 2)"; else no "M2" "seal still voided under mutation"; fi

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
    'if not self.commit_mode or self.cfg.get("allow_dirty"):' \
    'if True:' \
    happy "{\"repo\":\"$REPO\"}" "$REPO/artifact.txt"
  if [ "$EXIT" != 3 ]; then ok "M5 dirty-start guard (mutant exit=$EXIT, not 3)"; else no "M5" "dirty refusal still fired under mutation"; fi

  # M6: accept the first token even when duplicates exist -> S30 no longer rejects
  run_mutation m6 \
    'if len(matches) != 1:' \
    'if len(matches) == 0:' \
    two_verdicts '' "$ROOT/m6-run/artifact.txt"
  if [ "$EXIT" != 2 ]; then ok "M6 unique-token guard (mutant exit=$EXIT, not 2)"; else no "M6" "duplicate VERDICT still rejected under mutation"; fi

  # M7: disable the artifact-scope disjointness check -> P3 overlap no longer rejected.
  # Driven in --parallel mode (not run_mutation, which is --config-only).
  MUT7="$ROOT/m7.py"; cp "$REAL_DRIVER" "$MUT7"
  if python3 "$MUTATE" "$MUT7" 'if path in scope_owner:' 'if False and path in scope_owner:' 2>"$ROOT/m7.err" \
     && python3 -m py_compile "$MUT7" 2>"$ROOT/m7.comp"; then
    M7MOCK="$ROOT/m7-mock"; rm -rf "$M7MOCK"; mkdir -p "$M7MOCK"
    MOCK_DIR="$M7MOCK" MOCK_SCENARIO=happy MOCK_ARTIFACT="" \
      python3 "$MUT7" --parallel "$P3MAN" --claude-cmd "$MOCK" >/dev/null 2>&1
    M7EXIT=$?
    if [ "$M7EXIT" != 3 ]; then ok "M7 disjointness guard (mutant exit=$M7EXIT, not 3)"; else no "M7" "overlap still rejected under mutation"; fi
  else
    no "M7 apply/compile" "$(cat "$ROOT/m7.err" "$ROOT/m7.comp" 2>/dev/null)"
  fi
  rm -f "$MUT7"

  # M8: disable the stale-lock break -> a P8-style run wedges on the dead-holder
  # lock and escalates on the (margin-shrunk) bound instead of sealing.
  MUT8="$ROOT/m8.py"; cp "$REAL_DRIVER" "$MUT8"
  if python3 "$MUTATE" "$MUT8" 'self._break_lock_if_stale(lock)' 'pass  # M8: stale-break disabled' 2>"$ROOT/m8.err" \
     && python3 -m py_compile "$MUT8" 2>"$ROOT/m8.comp"; then
    GREPOM8="$ROOT/gm8"; mk_git_repo "$GREPOM8" "seed m8\n"
    mkdir -p "$GREPOM8/.git/converge-auto-commit.lock"
    sleep 0.01 & M8DEAD=$!
    wait "$M8DEAD" 2>/dev/null
    printf '%s' "$M8DEAD" > "$GREPOM8/.git/converge-auto-commit.lock/pid"
    SM8="$ROOT/m8s"; setup_scen "$SM8" "{\"loop_id\":\"m8loop\",\"repo\":\"$GREPOM8\",\"phase_timeout_s\":2}"
    export CONVERGE_LOCK_MARGIN_S=1
    DRIVER_PY="$MUT8"; drive "$SM8" happy "$GREPOM8/artifact.txt"; DRIVER_PY="$REAL_DRIVER"
    unset CONVERGE_LOCK_MARGIN_S
    if [ "$EXIT" = 2 ] && grep -qF 'pre-seal_commit_lock_timeout' "$SM8/rounds.md" 2>/dev/null; then
      ok "M8 stale-break guard (mutant wedges on the stale lock and escalates)"
    else
      no "M8 stale-break guard" "exit=$EXIT (expected 2 with lock-timeout escalate)"
    fi
  else
    no "M8 apply/compile" "$(cat "$ROOT/m8.err" "$ROOT/m8.comp" 2>/dev/null)"
  fi
  rm -f "$MUT8"
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
