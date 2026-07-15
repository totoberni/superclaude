#!/usr/bin/env bash
# Bite-tests for seal-manifest.py (F5 #21 revision-binding, in-session sealed-manifest).
# Self-contained: builds a throwaway git repo under mktemp -d (never touches a real
# repo or ~/.claude). Prints PASS/FAIL per case; exits non-zero if any case fails.
#
# Cases:
#   (1) seal a repo scope                                   -> sidecar written, exit 0
#   (2) check, nothing changed                              -> SEAL-STATUS: OK, exit 0
#   (3) modify a sealed file                                -> SEAL-STATUS: VOID, exit 1
#   (4) commit the change (HEAD moved + content changed)    -> VOID, exit 1
#   (5) manifest byte-compat with converge_auto.compute_manifest reference
#   (6) unrelated commit moves HEAD, sealed content stable  -> OK by default; VOID under --head-binds
#   (7) seal via --rounds-md that names a sealed path       -> sidecar records that path
#   (8) check on a missing sidecar                          -> SEAL-STATUS: ERROR, exit 2 (fail-open)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEAL="$SCRIPT_DIR/../seal-manifest.py"

PASS=0
FAIL=0
pass() { echo "PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "FAIL: $1"; FAIL=$((FAIL + 1)); }

command -v git >/dev/null 2>&1 || { echo "SKIP: git unavailable"; exit 0; }
command -v python3 >/dev/null 2>&1 || { echo "SKIP: python3 unavailable"; exit 0; }

REPO="$(mktemp -d "${TMPDIR:-/tmp}/seal-manifest-repo.XXXXXX")"
CAMP="$(mktemp -d "${TMPDIR:-/tmp}/seal-manifest-camp.XXXXXX")"
trap 'rm -rf "$REPO" "$CAMP"' EXIT

git -C "$REPO" init -q
git -C "$REPO" config user.email "test@example.com"
git -C "$REPO" config user.name "seal-test"
mkdir -p "$REPO/sub"
printf 'alpha\n' > "$REPO/f1.txt"
printf 'beta\n'  > "$REPO/sub/f2.txt"
printf 'ignore me\n' > "$REPO/other.txt"
git -C "$REPO" add -A
git -C "$REPO" commit -q -m "init"

SIDE="$CAMP/seal-manifest.json"

# (1) seal
if python3 "$SEAL" seal --repo "$REPO" --campaign demo --campaign-dir "$CAMP" \
     --path f1.txt --path sub/f2.txt --round R3 --seal-line "SEAL: ACCEPTED blocking=0 major=0 minor=0 nits=0" \
     >/dev/null 2>&1 && [ -f "$SIDE" ]; then
  pass "(1) seal writes a sidecar"
else
  fail "(1) seal writes a sidecar"
fi

# (2) check unchanged -> OK
OUT="$(python3 "$SEAL" check --sidecar "$SIDE" 2>&1)"; RC=$?
if [ "$RC" -eq 0 ] && printf '%s' "$OUT" | grep -q '^SEAL-STATUS: OK'; then
  pass "(2) check unchanged -> OK (exit 0)"
else
  fail "(2) check unchanged -> OK (exit 0)  [rc=$RC out='$OUT']"
fi

# (3) modify a sealed file (uncommitted) -> VOID
printf 'alpha-modified\n' > "$REPO/f1.txt"
OUT="$(python3 "$SEAL" check --sidecar "$SIDE" 2>&1)"; RC=$?
if [ "$RC" -eq 1 ] && printf '%s' "$OUT" | grep -q '^SEAL-STATUS: VOID reason=content-changed'; then
  pass "(3) modified sealed file -> VOID (exit 1)"
else
  fail "(3) modified sealed file -> VOID (exit 1)  [rc=$RC out='$OUT']"
fi

# (4) commit the change -> still VOID (content changed AND HEAD moved)
git -C "$REPO" add -A
git -C "$REPO" commit -q -m "change f1"
OUT="$(python3 "$SEAL" check --sidecar "$SIDE" 2>&1)"; RC=$?
if [ "$RC" -eq 1 ] && printf '%s' "$OUT" | grep -q '^SEAL-STATUS: VOID'; then
  pass "(4) committed change -> VOID (exit 1)"
else
  fail "(4) committed change -> VOID (exit 1)  [rc=$RC out='$OUT']"
fi

# (5) manifest byte-compatibility with the converge_auto.compute_manifest algorithm
# (restore f1 so both sides read identical bytes)
printf 'alpha\n' > "$REPO/f1.txt"
REF="$(python3 - "$REPO" f1.txt sub/f2.txt <<'PY'
import hashlib, os, sys
repo, paths = sys.argv[1], sys.argv[2:]
entries = []
for rel in paths:
    p = rel if os.path.isabs(rel) else os.path.join(repo, rel)
    try:
        d = hashlib.sha256(open(p, "rb").read()).hexdigest()
    except OSError:
        d = "MISSING"
    entries.append("%s:%s" % (rel, d))
sys.stdout.write(hashlib.sha256("\n".join(sorted(entries)).encode("utf-8")).hexdigest())
PY
)"
GOT="$(python3 "$SEAL" manifest --repo "$REPO" --path f1.txt --path sub/f2.txt 2>/dev/null)"
if [ -n "$GOT" ] && [ "$REF" = "$GOT" ]; then
  pass "(5) manifest byte-identical to converge_auto.compute_manifest reference"
else
  fail "(5) manifest byte-identical to converge_auto.compute_manifest reference  [ref='$REF' got='$GOT']"
fi

# (6) re-seal cleanly, then move HEAD with an UNRELATED commit (sealed content stable).
python3 "$SEAL" seal --repo "$REPO" --campaign demo --out "$SIDE" \
  --path f1.txt --path sub/f2.txt >/dev/null 2>&1
git -C "$REPO" add -A && git -C "$REPO" commit -q -m "restore f1"
printf 'unrelated\n' > "$REPO/other.txt"
git -C "$REPO" add -A && git -C "$REPO" commit -q -m "touch unrelated file"
OUT="$(python3 "$SEAL" check --sidecar "$SIDE" 2>&1)"; RC=$?
if [ "$RC" -eq 0 ] && printf '%s' "$OUT" | grep -q '^SEAL-STATUS: OK'; then
  pass "(6a) unrelated commit, sealed content stable -> OK by default"
else
  fail "(6a) unrelated commit, sealed content stable -> OK by default  [rc=$RC out='$OUT']"
fi
OUT="$(python3 "$SEAL" check --sidecar "$SIDE" --head-binds 2>&1)"; RC=$?
if [ "$RC" -eq 1 ] && printf '%s' "$OUT" | grep -q '^SEAL-STATUS: VOID reason=head-moved'; then
  pass "(6b) same state under --head-binds -> VOID reason=head-moved"
else
  fail "(6b) same state under --head-binds -> VOID reason=head-moved  [rc=$RC out='$OUT']"
fi

# (7) seal via a rounds.md that names sealed paths
ROUNDS="$CAMP/rounds.md"
{
  printf '# rounds ledger\n\n'
  printf '## Sealed artifacts\n'
  printf -- '- f1.txt\n'
  printf -- '- sub/f2.txt\n'
} > "$ROUNDS"
SIDE2="$CAMP/seal-manifest-rounds.json"
python3 "$SEAL" seal --repo "$REPO" --campaign demo --out "$SIDE2" --rounds-md "$ROUNDS" >/dev/null 2>&1
if python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d['artifact_paths']==['f1.txt','sub/f2.txt'] else 1)" "$SIDE2"; then
  pass "(7) --rounds-md extracts the sealed paths into the sidecar"
else
  fail "(7) --rounds-md extracts the sealed paths into the sidecar  [$(cat "$SIDE2" 2>/dev/null)]"
fi

# (8) check on a missing sidecar -> ERROR, exit 2 (fail-open, not a spurious VOID)
OUT="$(python3 "$SEAL" check --sidecar "$CAMP/nope.json" 2>&1)"; RC=$?
if [ "$RC" -eq 2 ] && printf '%s' "$OUT" | grep -q '^SEAL-STATUS: ERROR'; then
  pass "(8) missing sidecar -> ERROR exit 2 (fail-open)"
else
  fail "(8) missing sidecar -> ERROR exit 2 (fail-open)  [rc=$RC out='$OUT']"
fi

echo "----"
echo "test-seal-manifest: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
