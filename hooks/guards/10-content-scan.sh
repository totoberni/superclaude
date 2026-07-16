# Guard: 10-content-scan (F1) — blocks three authored-text violation classes on
# Write/Edit/MultiEdit new content (and the Agent tool prompt). PHASE2-CONTRACT sec 2/6.
#
# Defines functions ONLY at source time; the one sanctioned top-level statement is
# the default-mode declaration (GUARD_MODE_CONTENT_SCAN), a config, not an action.
#
# Scope: guard_new_content is a TRUE diff for Edit/MultiEdit (.new_string), but NOT
# for Write, whose .content is the WHOLE file. The DASH class therefore re-derives a
# diff for Write against the file on disk (_cs_dash_text): rules/06 is forward-
# looking, so re-writing an already-present (grandfathered) dash must NOT block; only
# a dash-bearing line this call ADDS does. The FIREWALL and TRIGGER-TOKEN classes
# scan the whole text by design (they are not forward-looking rules).
#
# Violation classes (each BLOCKs, naming which fired):
#   1. EM-DASH / EN-DASH  : a U+2014 or U+2013 byte in the new text.        (rules/06)
#   2. FIREWALL           : a superclaude-internal ref inside a ~/projects   (rules/20)
#                           file that is NOT under ~/.claude.
#   3. TRIGGER-TOKEN       : one of the 3 auto-fire tokens in its LIVE        (rules/13)
#                           un-escaped form (the dot-escaped form passes).
#
# TRIGGER-TOKEN FOOTGUN (read this): the 3 live tokens auto-fire a costly run the
# instant their literal appears in text the CLI or an agent processes. This file
# therefore stores ONLY the DOT-ESCAPED forms as literals; the live token is
# reconstructed in-memory at runtime by stripping the first dot (${esc/./}). No
# live token literal ever appears in this source. The test does the same, and only
# ever pipes a runtime-built token as data into the guard (the guard is not the CLI).

GUARD_MODE_CONTENT_SCAN=block

# ── Class 1: em-dash (U+2014) / en-dash (U+2013) ──────────────────────────────
# Returns 0 if the text contains either dash. The dash bytes are built at runtime
# from their UTF-8 octal escapes (E2 80 94 / E2 80 93) so NO literal dash char is
# authored into this file (rules/06). LC_ALL=C makes the match byte-exact.
_cs_has_dash() {
  local text="$1" em en
  em=$(printf '\342\200\224')   # U+2014 em-dash
  en=$(printf '\342\200\223')   # U+2013 en-dash
  printf '%s' "$text" | LC_ALL=C grep -qF -e "$em" -e "$en"
}

# _cs_added_lines <old_file> <new_text>: prints the lines of <new_text> that do NOT
# already exist as an exact whole line anywhere in <old_file>.
#
# Line EXISTENCE, not a positional diff: an LCS diff reports a MOVED but unchanged
# line as added, which would false-block a mere section reorder of a grandfathered
# doc. An EDITED dash line has new exact text, so it IS reported; that is intended
# (rules/06: introduce no new dashes). -x whole-line keeps a blank line in the old
# file from matching everything; -F keeps old text from acting as a regex.
# rc: 0 lines added, 1 none added, >1 grep error (caller falls back to whole text).
_cs_added_lines() {
  local old="$1" new="$2"
  printf '%s\n' "$new" | LC_ALL=C grep -vxF -f "$old"
}

# _cs_dash_text <text>: the text the DASH class should scan. For a whole-file Write
# over an EXISTING file that is only the added lines; otherwise (Edit/MultiEdit,
# where the text already IS a diff, or a genuinely new file) it is the whole text.
# FAIL-OPEN per the lib-guard contract: on any doubt, return the whole text (scan
# more, never error). Assignment via '|| rc=$?' so a set -e dispatcher cannot abort.
_cs_dash_text() {
  local text="$1" fp added rc=0
  [ "${GUARD_TOOL:-}" = "Write" ] || { printf '%s' "$text"; return 0; }
  fp=$(guard_file_path)
  # A '-'-leading path would be eaten by grep -f as an option/stdin; not a real
  # absolute tool path, so scan whole. Path is only ever quoted, never eval'd.
  case "$fp" in -*) printf '%s' "$text"; return 0 ;; esac
  # Absent file => a genuinely new file: every line of it is new.
  [ -n "$fp" ] && [ -f "$fp" ] && [ -r "$fp" ] || { printf '%s' "$text"; return 0; }
  added=$(_cs_added_lines "$fp" "$text" 2>/dev/null) || rc=$?
  [ "$rc" -le 1 ] || { printf '%s' "$text"; return 0; }
  printf '%s' "$added"
}

# ── Class 3: unescaped live trigger token ─────────────────────────────────────
# Escaped forms are stored; the live form is the escaped form with its first dot
# removed. A live occurrence is one NOT itself dot-escaped in the text, which the
# (^|[^.]) guard enforces: a dot-escaped ".workflow" (dot before the word) is NOT
# flagged; the same word stripped of that leading dot IS. The 3 tokens contain no
# ERE metacharacters, so no escaping is needed.
_cs_has_live_trigger() {
  local text="$1" esc live
  for esc in '.workflow' '/.deep-research' '.ultracode'; do
    live="${esc/./}"
    if printf '%s' "$text" | grep -Eq -- "(^|[^.])$live"; then
      return 0
    fi
  done
  return 1
}

# ── Class 2: firewall path gate + ref scan ────────────────────────────────────
# _cs_is_project_path: 0 iff the path is under ~/projects and NOT under ~/.claude.
_cs_is_project_path() {
  local p="$1"
  [ -n "$p" ] || return 1
  case "$p" in
    "~/"*) p="$HOME/${p#\~/}" ;;
  esac
  case "$p" in
    "$HOME/.claude/"*)  return 1 ;;   # global ~/.claude may reference meta freely
    "$HOME/projects/"*) return 0 ;;
    *)                  return 1 ;;
  esac
}

# _cs_firewall_hit: prints the offending pattern and returns 0 on a hit. The list
# is READ from rules/20 lines 24-28 (the firewall FORBIDDEN-PATTERN list), not
# invented: meta file/path refs, memory/comms DB tool filenames, agent-memory cell
# id refs (M-<n>/MM-<n>/GM-<n>/G-<n>/MT-<n>/CW-<n>/W-<n>), and the tell-tale phrases.
_cs_firewall_hit() {
  local text="$1" pat
  local fixed=(
    '~/.claude/' '.claude/rules' 'agent-memory' 'shared/projects/' 'class/meta'
    'MEMORY.md' 'mtm.md' 'ltm.md'
    '.memory.db' '.comms.db' '.broker.db' 'memory_db.py' 'comms_db.py'
    'meta says' 'memory.md says' 'according to the gotchas file' 'see the project memory'
  )
  for pat in "${fixed[@]}"; do
    if printf '%s' "$text" | grep -qiF -e "$pat"; then
      printf '%s' "$pat"; return 0
    fi
  done
  if printf '%s' "$text" | grep -Eq '\b(MM|GM|MT|CW|M|G|W)-[0-9]+'; then
    printf 'cell-id-ref'; return 0
  fi
  return 1
}

# ── Entry ─────────────────────────────────────────────────────────────────────
guard_content_scan() {
  local content prompt text fp hit
  content=$(guard_new_content)
  text="$content"
  if [ "${GUARD_TOOL:-}" = "Agent" ]; then
    prompt=$(guard_agent_prompt)
    text="$content
$prompt"
  fi

  # DASH class only: scan the ADDED lines (see _cs_dash_text). The two classes below
  # deliberately keep scanning "$text" whole.
  if _cs_has_dash "$(_cs_dash_text "$text")"; then
    guard_block "em-dash/en-dash in new content (rules/06 no-dash); use ; : , . or ()"
  fi

  # WARN, not block (owner-authorized 2026-07-15): the live forms of two of the
  # three tokens are the ordinary words 'workflow' and 'ultracode', which appear in
  # legitimate prose; a bare-word BLOCK would lock the owner out of the feature and
  # false-positive on normal docs. The owner explicitly accepts the residual risk
  # (a stray live token may fire an expensive run) in exchange for a non-blocking
  # nudge. Em-dash and firewall classes below stay BLOCK. (rules/13)
  if _cs_has_live_trigger "$text"; then
    guard_warn "possible unescaped auto-fire trigger token in new content/prompt (rules/13); keep it dot-escaped if it is a live invocation"
  fi

  fp=$(guard_file_path)
  if _cs_is_project_path "$fp"; then
    if hit=$(_cs_firewall_hit "$text"); then
      guard_block "firewall: superclaude-internal ref '${hit}' in a ~/projects file (rules/20 firewall)"
    fi
  fi

  return 0
}
