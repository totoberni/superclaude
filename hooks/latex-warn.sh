#!/usr/bin/env bash
# Hook: latex-warn
# Event: PostToolUse for Edit|Write|MultiEdit on *.tex files
# Purpose: Surface ONLY NEW LaTeX warnings (vs the previous compile state) as a
#          soft, non-blocking note. Translates common warnings to plain-English
#          hints (explain_log), detects float-stacking (FloatBarrier), and detects
#          hyperref/cleveref load-order problems (hyperref-order).
#
# Soft = warn via additionalContext, never block. Mirrors comms-schema-lint.sh.
#
# LATENCY DESIGN (non-negotiable per superclaude v3 T4.1):
#   This hook NEVER synchronously compiles. It parses the most-recent EXISTING
#   *.log file produced by the user's own latexmk/build, extracts warnings,
#   diffs them against a per-project cached prior set, and surfaces only the
#   additions. Cost is a handful of greps over one log file (single-digit ms),
#   so every .tex save stays instant. If no .log exists yet (no build run), the
#   hook is a silent no-op.
#
# FAIL-SAFE: exit 0 ALWAYS. set -uo pipefail (never -e). Missing jq/log -> no-op.
#
# Idea provenance (mined from overleaf-mcp): explain_log, FloatBarrier, hyperref-order.

set -uo pipefail

# ---------------------------------------------------------------------------
# 0. Read hook payload + filter to .tex edits. Any failure here -> silent no-op.
# ---------------------------------------------------------------------------
command -v jq >/dev/null 2>&1 || exit 0

input=$(cat)
tool_name=$(echo "$input" | jq -r '.tool_name // ""' 2>/dev/null || echo "")
file_path=$(echo "$input" | jq -r '.tool_input.file_path // ""' 2>/dev/null || echo "")

case "$tool_name" in
  Edit|Write|MultiEdit) ;;
  *) exit 0 ;;
esac

case "$file_path" in
  *.tex) ;;
  *) exit 0 ;;
esac

[ -f "$file_path" ] || exit 0

# ---------------------------------------------------------------------------
# 1. Locate the most-recent existing .log. STRATEGY: parse, never compile.
#    Search order:
#      a. Sibling <stem>.log next to the edited .tex (most common: latexmk runs
#         in the same dir, or the file is the main doc).
#      b. Any *.log in the edited file's directory - the user may edit an
#         \input-ed chapter while the main .log lives beside the master .tex.
#      c. The most-recent *.log anywhere up to 2 parent dirs (build/ or out/
#         layouts). Bounded so the hook never walks a huge tree.
#    If nothing found -> silent no-op (no build has happened yet).
# ---------------------------------------------------------------------------
tex_dir=$(dirname "$file_path")
tex_stem=$(basename "$file_path" .tex)

log_file=""
if [ -f "${tex_dir}/${tex_stem}.log" ]; then
  log_file="${tex_dir}/${tex_stem}.log"
else
  # Most-recently-modified .log in the same directory, then within 2 levels up.
  log_file=$(find "$tex_dir" -maxdepth 1 -name '*.log' -type f -printf '%T@ %p\n' 2>/dev/null \
             | sort -rn | head -1 | cut -d' ' -f2-)
  if [ -z "$log_file" ]; then
    parent=$(dirname "$tex_dir")
    log_file=$(find "$parent" -maxdepth 2 -name '*.log' -type f -printf '%T@ %p\n' 2>/dev/null \
               | sort -rn | head -1 | cut -d' ' -f2-)
  fi
fi

[ -n "$log_file" ] && [ -f "$log_file" ] || exit 0

# Confirm it is a TeX log, not some other tool's .log. pdfTeX/LuaTeX/XeTeX all
# announce themselves on line 1. If not, bail (avoid false positives on app logs).
head -1 "$log_file" 2>/dev/null | grep -qiE 'pdfTeX|XeTeX|LuaTeX|This is (pdfeTeX|e?TeX)' || exit 0

# ---------------------------------------------------------------------------
# 2. Extract warning signatures from the log into a stable, diffable form.
#    Each signature is normalised so volatile fields (page numbers, badness
#    magnitudes, byte offsets) do NOT cause spurious "new" warnings, while the
#    identifying content (ref/label/citation name, source line range) is kept.
#    grep -a forces text mode (logs can contain stray binary from font paths).
# ---------------------------------------------------------------------------
extract_signatures() {
  local log="$1"
  {
    # Undefined references / citations: key on the NAME only. Both the page and
    # the input-line number are volatile (any edit above the use-site shifts the
    # line, and the same missing \ref{X}/\cite{X} is the same defect wherever it
    # sits) - keeping them would re-flag an unchanged warning as "new" on every
    # nearby edit. Collapse to a stable "<Reference|Citation> `X' undefined".
    grep -aoE "LaTeX Warning: (Reference|Citation) \`[^']+' on page [0-9]+ undefined on input line [0-9]+" "$log" 2>/dev/null \
      | sed -E "s/' on page [0-9]+ undefined on input line [0-9]+/' undefined/"

    # Multiply-defined labels: name only (no volatile field).
    grep -aoE "LaTeX Warning: Label \`[^']+' multiply defined" "$log" 2>/dev/null

    # Overfull/Underfull boxes: keep the line range (identifies the spot); drop
    # the magnitude/badness (changes with every micro-edit, would spam "new").
    grep -aoE "(Overfull|Underfull) \\\\[hv]box \([^)]*\) (in paragraph |in alignment |detected )?at lines [0-9]+--[0-9]+" "$log" 2>/dev/null \
      | sed -E "s/\([^)]*\)/(...)/"
    grep -aoE "(Overfull|Underfull) \\\\[hv]box \([^)]*\) (has occurred while \\\\output is active)" "$log" 2>/dev/null \
      | sed -E "s/\([^)]*\)/(...)/"

    # Font substitution.
    grep -aoE "LaTeX Font Warning: Font shape \`[^']+' (undefined|in size <[^>]*> not available)" "$log" 2>/dev/null

    # Float stacking (FloatBarrier idea): the hard error LaTeX emits when floats
    # cannot be placed. Rare but high-signal.
    grep -aoE "(! )?LaTeX Error: Too many unprocessed floats" "$log" 2>/dev/null

    # hyperref/cleveref load-order (hyperref-order idea), as it surfaces in the log.
    grep -aoE "Package cleveref Error: cleveref must be loaded after hyperref" "$log" 2>/dev/null
    grep -aoE "Package hyperref Warning: Option \`[^']+' has already been used" "$log" 2>/dev/null
  } | sed -E 's/[[:space:]]+/ /g; s/[[:space:]]+$//' | sort -u
}

# ---------------------------------------------------------------------------
# 2b. hyperref-order from SOURCE (.tex). The log only shows an order problem
#     when it already errored; a wrong-but-tolerated order (hyperref loaded too
#     early, or cleveref before hyperref) often compiles with subtle breakage.
#     Detect structurally from \usepackage order in the edited file.
#     Emitted as a synthetic signature so it participates in NEW-only diffing.
# ---------------------------------------------------------------------------
source_order_signatures() {
  local tex="$1"
  # Line numbers of the relevant \usepackage / \RequirePackage calls.
  local hyp clv n_after_hyp
  hyp=$(grep -nE '^\s*\\(usepackage|RequirePackage)(\[[^]]*\])?\{[^}]*\bhyperref\b' "$tex" 2>/dev/null | head -1 | cut -d: -f1)
  clv=$(grep -nE '^\s*\\(usepackage|RequirePackage)(\[[^]]*\])?\{[^}]*\bcleveref\b' "$tex" 2>/dev/null | head -1 | cut -d: -f1)

  [ -z "$hyp" ] && return 0  # hyperref not used in this file -> nothing to check.

  # cleveref MUST load AFTER hyperref.
  if [ -n "$clv" ] && [ "$clv" -lt "$hyp" ]; then
    echo "SOURCE hyperref-order: cleveref loaded before hyperref"
  fi

  # hyperref should load LATE. Count \usepackage lines that appear AFTER hyperref
  # (excluding the known late-loaders that are designed to follow it). If many
  # packages still load after hyperref, the order is suspect.
  n_after_hyp=$(awk -v h="$hyp" '
    NR>h && /^[[:space:]]*\\(usepackage|RequirePackage)/ {
      if ($0 ~ /cleveref|bookmark|glossaries|algorithm2e|hypcap|hyperxmp/) next
      c++
    }
    END { print c+0 }' "$tex" 2>/dev/null)
  if [ "${n_after_hyp:-0}" -ge 3 ]; then
    echo "SOURCE hyperref-order: ${n_after_hyp} packages load after hyperref (hyperref should load near-last)"
  fi
}

# ---------------------------------------------------------------------------
# 3. Build the current signature set (log warnings + source-order checks).
# ---------------------------------------------------------------------------
current=$(
  {
    extract_signatures "$log_file"
    source_order_signatures "$file_path"
  } | grep -v '^$' | sort -u
)

# ---------------------------------------------------------------------------
# 4. NEW-only diff against the per-project cache.
#    Cache key = sha of the resolved log path (stable across runs for one doc).
#    First-ever sighting of this doc's log: seed the baseline (which may be the
#    EMPTY set if the current build is clean) and stay SILENT - we cannot know
#    what was "already there" before the hook existed, so surfacing everything
#    on first contact would be noise, not signal. Subsequent runs diff against
#    this baseline. Seeding happens even when `current` is empty, so that the
#    FIRST build to introduce a warning is correctly diffed as NEW (not mistaken
#    for a first sighting and silently re-seeded).
# ---------------------------------------------------------------------------
cache_dir="${HOME}/.claude/.latex-warn-cache"
mkdir -p "$cache_dir" 2>/dev/null || exit 0

key=$(printf '%s' "$log_file" | sha1sum 2>/dev/null | cut -d' ' -f1)
[ -n "$key" ] || exit 0
cache_file="${cache_dir}/${key}"

if [ ! -f "$cache_file" ]; then
  # Seed silently; establish the baseline (may legitimately be empty).
  printf '%s\n' "$current" > "$cache_file" 2>/dev/null || true
  exit 0
fi

prior=$(cat "$cache_file" 2>/dev/null || echo "")

# Lines present in current but NOT in prior = newly-introduced warnings.
new_warnings=$(comm -13 <(printf '%s\n' "$prior" | sort -u) <(printf '%s\n' "$current" | sort -u) 2>/dev/null | grep -v '^$' || true)

# Always refresh the cache to the latest state (so a fixed-then-reintroduced
# warning is correctly re-flagged, and resolved ones stop being "prior").
printf '%s\n' "$current" > "$cache_file" 2>/dev/null || true

# No additions -> silent no-op (the core "NEW-only" contract).
[ -z "$new_warnings" ] && exit 0

# ---------------------------------------------------------------------------
# 5. explain_log - translate each NEW warning into a plain-English hint.
#    NOTE: use `printf '%s\n'`, never `echo`. The hint strings contain LaTeX
#    control sequences (\cite, \clearpage, \-, \url …) and bash's builtin `echo`
#    interprets backslash escapes - `\c` in `\cite`/`\clearpage` means "stop all
#    output" and would silently truncate the hint. printf '%s' is escape-safe.
# ---------------------------------------------------------------------------
explain() {
  case "$1" in
    *"Reference"*"undefined"*)
      printf '%s' 'undefined \ref - the label is misspelled or not yet defined; rerun the build after the label exists' ;;
    *"Citation"*"undefined"*)
      printf '%s' 'undefined \cite - the bibkey is missing from your .bib, or BibTeX/biber has not been re-run' ;;
    *"multiply defined"*)
      printf '%s' 'duplicate \label - two \label{...} share one key; rename one (cross-refs will point to the wrong target)' ;;
    *Overfull*hbox*)
      printf '%s' 'overfull hbox - text runs past the margin; reword, add \- hyphenation, or wrap a long token (e.g. \url, \texttt)' ;;
    *Underfull*hbox*)
      printf '%s' 'underfull hbox - a line is too loose; usually cosmetic, often from a forced \\ or a narrow column' ;;
    *Overfull*vbox*|*Underfull*vbox*)
      printf '%s' 'over/underfull vbox - vertical overflow on a page; check large floats or \vspace near a page break' ;;
    *"Font shape"*)
      printf '%s' 'font substitution - requested shape unavailable; TeX picked a fallback (italic/bold may look off)' ;;
    *"Too many unprocessed floats"*)
      printf '%s' 'FLOAT STACKING - too many figures/tables queued unplaced; add \clearpage or \FloatBarrier (placeins pkg) to flush them' ;;
    *"cleveref must be loaded after hyperref"*|*"cleveref loaded before hyperref"*)
      printf '%s' 'HYPERREF ORDER - load cleveref AFTER hyperref (cleveref hooks into hyperref reference machinery)' ;;
    *"packages load after hyperref"*)
      printf '%s' 'HYPERREF ORDER - hyperref should load near-last; packages after it can clobber its redefinitions (cleveref/bookmark are the intended exceptions)' ;;
    *"has already been used"*)
      printf '%s' 'hyperref option conflict - an option was set twice (often \hypersetup vs \usepackage[...]); consolidate' ;;
    *)
      printf '%s' 'new LaTeX warning' ;;
  esac
}

n_new=$(printf '%s\n' "$new_warnings" | grep -c .)

# Assemble the human-readable block with REAL newlines (no \n placeholders, no
# printf %b - the warning text and hints contain backslashes that %b mangles).
body=$(
  while IFS= read -r w; do
    [ -z "$w" ] && continue
    # Trim the verbose log prefix for readability in the note.
    short=$(printf '%s' "$w" | sed -E 's/^LaTeX (Font )?Warning: //; s/^(! )?LaTeX Error: //; s/^Package //')
    printf '  - %s\n      hint: %s\n' "$short" "$(explain "$w")"
  done <<HOOK_EOF
$new_warnings
HOOK_EOF
)

rel_log="$(basename "$(dirname "$log_file")")/$(basename "$log_file")"
header=$(printf 'latex-warn: %d NEW LaTeX warning(s) since the last build of %s (parsed from %s, no recompile triggered):' \
  "$n_new" "$tex_stem.tex" "$rel_log")
footer='These are NEW vs the previous compile state; pre-existing warnings are not repeated. Soft note only, your tool call was not affected.'

msg=$(printf '%s\n%s\n%s' "$header" "$body" "$footer")

jq -nc --arg msg "$msg" \
  '{hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext: $msg}}' 2>/dev/null || true

exit 0
