#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
NB_PROGRESS_PATH = os.path.join(HOME, ".claude", ".nb-progress.json")
COST_CACHE_PATH = os.path.join(HOME, ".claude", ".cost-cache.json")
RATE_LATEST_PATH = os.path.join(HOME, ".claude", ".rate-latest.json")
COST_REFRESH_SCRIPT = os.path.join(HOME, ".claude", "scripts", "cost-cache-refresh.sh")
# --- tunables -------------------------------------------------------------
NB_HUNG_AFTER_S = 10.0          # status==running but updated older than this => HUNG
COST_STALE_S = 15 * 60          # cost cache older than this => fire a backgrounded refresh
BAR_WIDTH_SCALE = 0.95          # shrink all proportional bars by 5%
MAX_LINES = 5                   # hard cap on rendered statusline lines
SEP = " │ "                # " | " box-drawing separator between segments on a line
SEP_DOT = "·"              # middot — intra-field separator within one agent row

# --- ANSI palette (transplanted from claude-hud render/colors.ts) ---------
RESET = "\x1b[0m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"
BRIGHT_BLUE = "\x1b[94m"
BRIGHT_MAGENTA = "\x1b[95m"
# 256-colour pink/magenta for the git repo/branch (distinct from the path's yellow
# AND from the quota bars' brightMagenta). 213 = a clear pink in the xterm-256 cube.
PINK = "\x1b[38;5;213m"

# claude-hud autocompact buffer constant (src/constants.ts AUTOCOMPACT_BUFFER_PERCENT).
AUTOCOMPACT_BUFFER_PERCENT = 0.165


def colorize(text, color):
    return "%s%s%s" % (color, text, RESET)


# --- safe stdin parse -----------------------------------------------------
def read_stdin():
    # The CC envelope is handed over via STATUSLINE_STDIN (set by the bash
    # launcher) because this interpreter's own stdin carries the heredoc script.
    raw = os.environ.get("STATUSLINE_STDIN", "")
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def gnum(d, *path):
    """Walk nested dict path; return a finite number or None. Never raises."""
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    if isinstance(cur, bool):
        return None
    if isinstance(cur, (int, float)):
        try:
            f = float(cur)
        except Exception:
            return None
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return f
    return None


def gstr(d, *path):
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    if isinstance(cur, str) and cur.strip():
        return cur.strip()
    return None


# --- terminal width -------------------------------------------------------
# The statusline command runs with stdout PIPED (non-tty), so os.get_terminal_size
# on our own fds returns the fallback and we'd never see the real width nor track
# resizes. Detection priority (most-reliable first):
#   1. CC stdin width field — does NOT exist in the 2.1.159 payload (verified
#      against the binary), but probed defensively in case a future CC adds one.
#   2. /dev/tty — the controlling terminal. Reflects the user's ACTUAL terminal
#      even when stdout is piped, and updates on resize. THE key fix.
#   3. COLUMNS env — a hint (CC may or may not export it).
#   4. conservative NARROW default LAST — when width is unknown, prefer narrower
#      so we WRAP (showing all info) rather than crop (losing info). owner's pref.
WIDTH_UNKNOWN_DEFAULT = 80  # narrow-ish: wrap-not-crop when nothing else resolves
# Sane terminal-column bounds. Reject bogus/pixel-ish values (e.g. a CC stdin field
# carrying a 1920 *pixel* width): an over-large width disables wrapping entirely and
# the terminal then CROPS the single giant line. owner's hard preference = ALWAYS wrap.
TERM_COLS_MIN = 20
TERM_COLS_MAX = 600


def _sane_cols(w):
    """w -> int column count if within a plausible terminal range, else None."""
    try:
        w = int(w)
    except (TypeError, ValueError):
        return None
    return w if TERM_COLS_MIN <= w <= TERM_COLS_MAX else None


def _width_from_stdin(stdin):
    """Probe for a width/columns field in the CC stdin envelope.

    The 2.1.159 statusline payload carries none, but newer CC builds might. We
    check the plausible locations so this picks it up for free if it ever lands.
    """
    if not isinstance(stdin, dict):
        return None
    for path in (
        ("terminal_width",),
        ("columns",),
        ("width",),
        ("terminal", "width"),
        ("terminal", "columns"),
        ("workspace", "terminal_width"),
        ("workspace", "columns"),
    ):
        c = _sane_cols(gnum(stdin, *path))
        if c:
            return c
    return None


def _width_from_tty():
    """Width of the controlling terminal via /dev/tty. None if no tty (e.g. the
    statusline running detached, or under a test harness with no terminal)."""
    fd = None
    try:
        fd = os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY)
        c = _sane_cols(os.get_terminal_size(fd).columns)
        if c:
            return c
    except Exception:
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
    return None


def _width_from_columns_env():
    return _sane_cols(os.environ.get("COLUMNS", ""))


def terminal_width(stdin=None):
    # 1. controlling terminal /dev/tty — the REAL renderable width (ground truth).
    #    Works even though our stdout is piped, and tracks resize. Preferred OVER a
    #    stdin-provided field whose unit/semantics we cannot trust: a too-large value
    #    there would disable wrapping and make the terminal CROP one giant line.
    w = _width_from_tty()
    if w:
        return w
    # 2. stdin width field (sanity-bounded; absent in 2.1.159, defensive for future CC).
    w = _width_from_stdin(stdin)
    if w:
        return w
    # 3. COLUMNS hint.
    w = _width_from_columns_env()
    if w:
        return w
    # 4. conservative narrow default — wrap, never crop, when width is unknown.
    return WIDTH_UNKNOWN_DEFAULT


def bar_width(cols):
    """Adaptive bar width — mirrors claude-hud getAdaptiveBarWidth breakpoints, scaled by BAR_WIDTH_SCALE."""
    if cols >= 100:
        base = 10
    elif cols >= 60:
        base = 6
    else:
        base = 4
    return max(1, int(base * BAR_WIDTH_SCALE))


def bars_fit(cols):
    """Below this width we drop the bar glyphs and show %-only (T1 abbreviation)."""
    return cols >= 60


# --- visible-length + bar rendering ---------------------------------------
def _scan_escape(s, i, n):
    """If s[i] begins an ANSI escape, return the index just past it; else i.

    Handles CSI ('\\x1b[ ... <final>') and OSC ('\\x1b] ... BEL|ST') sequences so
    that both colour codes AND OSC-8 hyperlinks are treated as zero-width.
    """
    if s[i] != "\x1b" or i + 1 >= n:
        return i
    nxt = s[i + 1]
    if nxt == "[":  # CSI: ESC [ ... <byte in @-~>
        j = i + 2
        while j < n and not ("\x40" <= s[j] <= "\x7e"):
            j += 1
        return j + 1 if j < n else n
    if nxt == "]":  # OSC: ESC ] ... (BEL | ESC \\)
        j = i + 2
        while j < n:
            if s[j] == "\x07":  # BEL terminator
                return j + 1
            if s[j] == "\x1b" and j + 1 < n and s[j + 1] == "\\":  # ST terminator
                return j + 2
            j += 1
        return n
    return i  # lone ESC; count it as 1 below


def visible_len(s):
    """Display width of s ignoring ANSI escape sequences (CSI + OSC).

    Each surviving code point counts as width 1 — correct for our glyph set
    (block/shade bars, box-vert separator, status glyphs), all of which are
    single-column. No East-Asian-wide or zero-width combining chars are emitted.
    """
    out = 0
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "\x1b":
            j = _scan_escape(s, i, n)
            if j > i:
                i = j
                continue
        out += 1
        i += 1
    return out


def hard_wrap_segment(seg, width):
    """Split a single segment that is wider than `width` into a list of chunks,
    each with visible width <= width, WITHOUT bisecting an ANSI escape sequence.

    Used as the last-resort guarantee that no emitted line ever exceeds the
    detected width (owner's wrap-not-crop preference). A trailing RESET is appended
    to any chunk that ends mid-colour so the wrapped tail doesn't bleed colour.
    """
    if width <= 0:
        return [seg]
    chunks = []
    cur = []
    cur_w = 0
    i = 0
    n = len(seg)
    while i < n:
        if seg[i] == "\x1b":
            j = _scan_escape(seg, i, n)
            if j > i:
                cur.append(seg[i:j])  # escape is zero-width: never forces a wrap
                i = j
                continue
        # a visible code point
        if cur_w + 1 > width:
            chunk = "".join(cur)
            if visible_len(chunk) and RESET not in chunk[-len(RESET):]:
                chunk += RESET
            chunks.append(chunk)
            cur = []
            cur_w = 0
        cur.append(seg[i])
        cur_w += 1
        i += 1
    if cur:
        chunks.append("".join(cur))
    return chunks or [seg]


def render_bar(percent, width, color_fn):
    """coloredBar transplant: filled '#' in color + dim empty + reset.

    color_fn maps the clamped percent -> ANSI color (context vs quota palette).
    """
    try:
        w = max(0, int(round(width)))
    except Exception:
        w = 0
    p = percent
    if p != p:  # NaN
        p = 0.0
    p = min(100.0, max(0.0, float(p)))
    filled = int(round((p / 100.0) * w))
    empty = w - filled
    color = color_fn(p)
    return "%s%s%s%s%s" % (color, "█" * filled, DIM, "░" * empty, RESET)


def context_color(percent):
    """GREEN -> YELLOW@70 -> RED@85 (claude-hud getContextColor)."""
    if percent >= 85:
        return RED
    if percent >= 70:
        return YELLOW
    return GREEN


def quota_color(percent):
    """brightBlue(<75) -> brightMagenta(>=75) -> RED(>=90) (claude-hud getQuotaColor).

    DISTINCT from the context palette so quota and context bars never alias.
    """
    if percent >= 90:
        return RED
    if percent >= 75:
        return BRIGHT_MAGENTA
    return BRIGHT_BLUE


# --- duration formatting --------------------------------------------------
def fmt_reset_compact(resets_at, now):
    """Compact 'resets' delta: '1h5m' / '42m' / '2d3h' (no spaces). None if unusable."""
    if resets_at is None:
        return None
    delta = resets_at - now
    if delta <= 0:
        return "due"
    mins = int(delta // 60)
    if mins < 60:
        return "%dm" % max(1, mins)
    hours = mins // 60
    rem = mins % 60
    if hours < 24:
        return "%dh%dm" % (hours, rem) if rem else "%dh" % hours
    days = hours // 24
    hrem = hours % 24
    return "%dd%dh" % (days, hrem) if hrem else "%dd" % days


def fmt_elapsed(secs):
    """Compact elapsed: 8s, 2m, 1h12m."""
    if secs is None:
        return None
    secs = int(secs)
    if secs < 60:
        return "%ds" % secs
    mins = secs // 60
    if mins < 60:
        return "%dm" % mins
    hours = mins // 60
    rem = mins % 60
    return "%dh%dm" % (hours, rem) if rem else "%dh" % hours


# ==========================================================================
# Context bar — owner's #1 ask (when-to-/compact cue)
# ==========================================================================
def get_total_tokens(stdin):
    cw = stdin.get("context_window")
    if not isinstance(cw, dict):
        return 0.0
    a = gnum(stdin, "context_window", "current_usage", "input_tokens") or 0.0
    b = gnum(stdin, "context_window", "current_usage", "cache_creation_input_tokens") or 0.0
    c = gnum(stdin, "context_window", "current_usage", "cache_read_input_tokens") or 0.0
    return a + b + c


def get_native_percent(stdin):
    """Native used_percentage (CC v2.1.6+); 0 treated as 'not yet populated'."""
    native = gnum(stdin, "context_window", "used_percentage")
    if native is not None and native > 0:
        return min(100, max(0, int(round(native))))
    return None


def get_buffered_percent(stdin):
    """Autocompact-buffered context % — transplant of claude-hud getBufferedPercent.

    Buffer means the bar hits 100% exactly at the auto-compact threshold (not at
    the raw window size), so it is a true "time to /compact" cue.

    native first (already reflects CC's own threshold). Else manual:
      rawRatio = totalTokens / size
      scale    = clamp((rawRatio - 0.05) / (0.50 - 0.05), 0, 1)   # 0 at <=5%, full at >=50%
      buffer   = size * 0.165 * scale
      pct      = round((totalTokens + buffer) / size * 100), clamped to 100
    """
    native = get_native_percent(stdin)
    if native is not None:
        return native
    size = gnum(stdin, "context_window", "context_window_size")
    if not size or size <= 0:
        return None
    total = get_total_tokens(stdin)
    raw_ratio = total / size
    low, high = 0.05, 0.50
    scale = min(1.0, max(0.0, (raw_ratio - low) / (high - low)))
    buffer = size * AUTOCOMPACT_BUFFER_PERCENT * scale
    return min(100, int(round(((total + buffer) / size) * 100)))


def seg_context(ctx):
    stdin = ctx["stdin"]
    pct = get_buffered_percent(stdin)
    if pct is None:
        return None
    val = colorize("%d%%" % pct, context_color(pct))
    if not bars_fit(ctx["cols"]):
        # very narrow: %-only, no bar (T1 abbreviation, never dropped)
        return "%s" % val
    bar = render_bar(pct, ctx["bar_w"], context_color)
    return "%s %s" % (bar, val)


# ==========================================================================
# Quota bars — 5h + 7d (distinct palette from context)
# ==========================================================================
def _quota_one(stdin, window_key, label, ctx):
    pct = gnum(stdin, "rate_limits", window_key, "used_percentage")
    if pct is None:
        return None
    pct = min(100.0, max(0.0, pct))
    ipct = int(round(pct))
    val = colorize("%d%%" % ipct, quota_color(pct))
    reset = fmt_reset_compact(gnum(stdin, "rate_limits", window_key, "resets_at"), ctx["now"])
    if not bars_fit(ctx["cols"]):
        # %-only at very narrow; keep reset only at full width
        s = "%s %s" % (label, val)
    else:
        bar = render_bar(pct, ctx["bar_w"], quota_color)
        s = "%s %s %s" % (label, bar, val)
    if reset and ctx["cols"] >= 100:
        s += " %s" % reset
    return s


def _load_rate_fallback():
    """Last-known rate-limit sample from ~/.claude/.rate-latest.json, reshaped to
    mirror stdin['rate_limits'] so the quota renderer consumes it unchanged.

    Lets the 5h/7d bars render at a FRESH REPL (before the first prompt, when CC
    has not yet populated stdin['rate_limits']) rather than vanishing until the
    agent starts. Returns None on absent/garbage/no-7d data (fail-safe -> caller
    omits the segment exactly as it did before this fallback existed)."""
    try:
        with open(RATE_LATEST_PATH) as fh:
            d = json.load(fh)
        if not isinstance(d, dict):
            return None
        seven = d.get("seven_day_pct")
        if seven is None:
            return None
        rl = {"seven_day": {"used_percentage": seven}}
        resets = d.get("seven_day_resets_at")
        if resets is not None:
            rl["seven_day"]["resets_at"] = resets
        five = d.get("five_hour_pct")
        if five is not None:
            rl["five_hour"] = {"used_percentage": five}
        return rl
    except Exception:
        return None


def seg_quota(ctx):
    stdin = ctx["stdin"]
    src = stdin
    if not isinstance(stdin.get("rate_limits"), dict):
        # Fresh REPL / API-key session: stdin carries no live rate_limits yet.
        # Fall back to the last-known persisted sample so the 5h/7d bars are
        # ALWAYS on (owner: visible at REPL launch, before the first prompt).
        fb = _load_rate_fallback()
        if fb is None:
            return None
        src = {"rate_limits": fb}
    five = _quota_one(src, "five_hour", "5h", ctx)
    seven = _quota_one(src, "seven_day", "7d", ctx)
    parts = [p for p in (five, seven) if p]
    if not parts:
        return None
    # join the two windows with a thin separator (they are one logical segment)
    return "  ".join(parts)


# ==========================================================================
# Cost — from ~/.claude/.cost-cache.json (R-1 schema). Rendered label-free as
# 'Costs: $day · $week · $month · $total · €<lifetime> · ~<1d>·<7d>·<30d>'
# (owner knows the positional encoding). The EUR lifetime subscription rides
# in-column separated by ' · ' — no box separator, no 'sub' label, no value-ratio.
# The tier-advice tail '~<1d>·<7d>·<30d>' (e.g. '~X5·X5·X5') is appended when
# cols >= 100, tier_calibrated is truthy, and tier_advice has keys 1d/7d/30d.
# Narrow fallback (<60 cols): total-only plus EUR if available
# ('Costs: $total · €<lifetime>'). The EUR field and tier tail are omitted
# gracefully when missing/garbage — fail-safe.
# ==========================================================================
def _fire_cost_refresh():
    """Background/disown the refresh; NEVER block the render. Best-effort only."""
    if not os.path.exists(COST_REFRESH_SCRIPT):
        return
    try:
        subprocess.Popen(
            ["bash", COST_REFRESH_SCRIPT],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # detach so it survives this process exit
        )
    except Exception:
        pass


def _fmt_usd(v):
    if v is None:
        return "…"  # ellipsis: value not (yet) available
    try:
        v = float(v)
    except Exception:
        return "…"
    if v < 0:
        return "…"
    if v < 10:
        return "$%.2f" % v
    if v < 1000:
        return "$%.1f" % v
    return "$%.0f" % v


def _fmt_eur(v):
    """Format a EUR amount as '€690.91'. None if missing/garbage/negative (the
    caller then OMITS the subscription field — fail-safe, never a crash). Keeps 2
    decimals for the lifetime subscription total (a precise figure, not a noisy
    running one); degrades precision only once it grows past €10k (months accrue)."""
    if v is None:
        return None
    try:
        v = float(v)
    except Exception:
        return None
    if v != v or v in (float("inf"), float("-inf")) or v < 0:
        return None
    if v < 1000:
        return "€%.2f" % v
    if v < 10000:
        return "€%.1f" % v
    return "€%.0f" % v


def load_cost_cache():
    """Read the cost cache once (read-only). None if absent/garbage. Loaded in
    main() and shared via ctx so seg_cost + seg_burn don't double-read the file."""
    try:
        with open(COST_CACHE_PATH, "r") as fh:
            loaded = json.load(fh)
        return loaded if isinstance(loaded, dict) else None
    except Exception:
        return None


def seg_cost(ctx):
    data = ctx.get("cost_cache")

    if data is None:
        # No cache yet: kick a backgrounded refresh, show a placeholder this tick.
        _fire_cost_refresh()
        return "Costs: …" if ctx["cols"] >= 60 else None

    updated = gnum(data, "updated")
    if updated is None or (ctx["now"] - updated) > COST_STALE_S:
        _fire_cost_refresh()  # stale -> refresh in background, render last-cached now

    day = _fmt_usd(gnum(data, "day_usd"))
    week = _fmt_usd(gnum(data, "week_usd"))
    month = _fmt_usd(gnum(data, "month_usd"))
    tot = _fmt_usd(gnum(data, "total_usd"))

    # EUR lifetime subscription: in-column, separated by ' · ', no box sep, no label.
    # _fmt_eur returns None if subscription_eur_total is missing/garbage -> field
    # omitted gracefully (fail-safe). The € symbol is self-explanatory.
    sub = _fmt_eur(gnum(data, "subscription_eur_total"))
    eur_tail = " · %s" % sub if sub else ""

    # Tier-advice tail: '~X5·X5·X5' (1d·7d·30d), appended when room allows and the
    # tier is calibrated. Reads from ctx["cost_cache"] (already loaded — no extra
    # file I/O). Fully defensive: any missing/wrong-type field silently drops the tail.
    adv_tail = ""
    if ctx["cols"] >= 100:
        adv = data.get("tier_advice") if isinstance(data, dict) else None
        calibrated = data.get("tier_calibrated") if isinstance(data, dict) else None
        if calibrated and isinstance(adv, dict):
            d1 = adv.get("1d")
            d7 = adv.get("7d")
            d30 = adv.get("30d")
            if isinstance(d1, str) and isinstance(d7, str) and isinstance(d30, str):
                adv_tail = " · ~%s%s%s%s%s" % (d1, SEP_DOT, d7, SEP_DOT, d30)

    # Label-free positional form. With per-figure labels gone, a middle-drop would be
    # ambiguous (reader can't tell WHICH figures remain), so the only narrow fallback
    # is TOTAL-ONLY ('Costs: $total · €<lifetime>'); otherwise show ALL FOUR USD + EUR.
    if ctx["cols"] < 60:
        return "Costs: %s%s" % (tot, eur_tail)

    return "Costs: %s · %s · %s · %s%s%s" % (day, week, month, tot, eur_tail, adv_tail)


# ==========================================================================
# dir + git — cwd tail (YELLOW) + git repo/branch (PINK, distinct from the
# path). Dirty '*' and ahead/behind ride the same pink so the whole git half is
# one visually-coherent colour, clearly separated from the yellow path.
# ==========================================================================
def _git(cwd, args):
    try:
        out = subprocess.run(
            ["git", "-C", cwd] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
            text=True,
        )
        if out.returncode != 0:
            return None
        return out.stdout.strip()
    except Exception:
        return None


def _osc8(url, text):
    """Wrap text in an OSC-8 hyperlink (zero visible width). T3 optional —
    terminals that don't grok OSC-8 simply render `text`."""
    return "\x1b]8;;%s\x1b\\%s\x1b]8;;\x1b\\" % (url, text)


def seg_dirgit(ctx):
    stdin = ctx["stdin"]
    cwd = gstr(stdin, "cwd")
    if not cwd:
        return None

    segments = [s for s in cwd.replace("\\", "/").split("/") if s]
    levels = 2 if ctx["cols"] >= 100 else 1
    tail = "/".join(segments[-levels:]) if segments else "/"
    dir_part = colorize(tail, YELLOW)
    # OSC-8 clickable dir (only at wide widths; zero visible width so packing is
    # unaffected). Links the shown tail to the absolute cwd via file://.
    if ctx["cols"] >= 100 and cwd.startswith("/"):
        dir_part = _osc8("file://%s" % cwd, dir_part)

    branch = _git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"])
    if not branch:
        return dir_part  # not a git repo (or git unavailable) -> dir only

    # Git half, rendered in PINK (distinct from the yellow path). Form: repo/branch
    # so the repo identity is visible without leaning on the path tail.
    git_text = _git_repobranch(cwd, branch, ctx["cols"])
    return "%s %s" % (dir_part, colorize(git_text, PINK))


def _git_repobranch(cwd, branch, cols):
    """Build the 'repo/branch[*][ ↑n↓n]' string (uncoloured; caller pinks it)."""
    repo = None
    top = _git(cwd, ["rev-parse", "--show-toplevel"])
    if top:
        repo = top.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or None
    head = "%s/%s" % (repo, branch) if repo else branch

    dirty = "*" if _git(cwd, ["status", "--porcelain"]) else ""

    ab = ""
    if cols >= 100:
        rev = _git(cwd, ["rev-list", "--left-right", "--count", "@{u}...HEAD"])
        if rev:
            cols2 = rev.split()
            if len(cols2) == 2:
                try:
                    behind = int(cols2[0])
                    ahead = int(cols2[1])
                except ValueError:
                    behind = ahead = 0
                if ahead > 0:
                    ab += "↑%d" % ahead
                if behind > 0:
                    ab += "↓%d" % behind
    return "%s%s%s" % (head, dirty, (" " + ab) if ab else "")


# ==========================================================================
# Notebook progress bar — ~/.claude/.nb-progress.json
# ==========================================================================
NB_GLYPH = {
    "running": "▶",   # play
    "done": "✓",      # check
    "broken": "✗",    # x
    "hung": "⚠",      # warning
    "idle": "·",      # middot
}


def seg_notebook(ctx):
    """nb progress (R-1 schema) -> 'nb 12/40 [bar] play 8s'. Self-detects HUNG."""
    try:
        with open(NB_PROGRESS_PATH, "r") as fh:
            data = json.load(fh)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    status = data.get("status")
    if not isinstance(status, str):
        return None
    status = status.strip().lower()
    if status == "idle":
        return None

    now = ctx["now"]
    updated = gnum(data, "updated")
    stale = updated is not None and (now - updated) > NB_HUNG_AFTER_S
    if status == "running" and stale:
        status = "hung"
    elif status not in ("running", "hung", "broken") and stale:
        return None

    glyph = NB_GLYPH.get(status, "·")
    cur = gnum(data, "current_index")
    total = gnum(data, "total_cells")

    bits = ["nb"]
    if cur is not None and total is not None and total > 0:
        bits.append("%d/%d" % (int(cur), int(total)))
        # a tiny inline progress bar when there is room
        if bars_fit(ctx["cols"]) and ctx["cols"] >= 100:
            frac = max(0.0, min(1.0, float(cur) / float(total)))
            bits.append(render_bar(frac * 100.0, max(4, ctx["bar_w"] - 4), lambda _p: CYAN))
    bits.append(glyph)

    if ctx["cols"] >= 60:
        elapsed = fmt_elapsed(gnum(data, "cell_elapsed_s"))
        if elapsed:
            bits.append(elapsed)
    if ctx["cols"] >= 100:
        if gstr(data, "kernel") == "dead":
            bits.append("kernel:dead")
        label = gstr(data, "current_label")
        if label and status in ("running", "hung"):
            if len(label) > 18:
                label = label[:17] + "…"
            bits.append(label)

    return " ".join(bits)


def parse_iso_z(s):
    """Parse an ISO-8601 'Z' timestamp -> epoch seconds. Tolerates fractional
    seconds (CC stamps e.g. '2026-06-01T17:08:52.913Z'). None on garbage."""
    if not isinstance(s, str) or not s.endswith("Z") or "T" not in s:
        return None
    core = s[:-1]  # drop trailing 'Z'
    if "." in core:
        core = core.split(".", 1)[0]  # drop fractional part; whole-second precision
    try:
        st = time.strptime(core, "%Y-%m-%dT%H:%M:%S")
        import calendar
        return float(calendar.timegm(st))
    except Exception:
        return None


# ==========================================================================
# Model + effort — T2 (dropped before any T1 segment)
# ==========================================================================
EFFORT_GLYPH = "⚡"  # high-voltage


def fmt_capacity(size):
    """Context-window capacity -> compact label: 1000000->'1M', 200000->'200K'.

    General rule (no special-casing): >=1M shown in M (1 decimal unless whole),
    >=1K shown in K (1 decimal unless whole), else the raw integer.
      1000000->'1M'  1048576->'1M'(rounded)  200000->'200K'  128000->'128K'
      1500000->'1.5M'  64000->'64K'
    """
    try:
        n = int(round(float(size)))
    except Exception:
        return None
    if n <= 0:
        return None
    if n >= 1_000_000:
        m = n / 1_000_000.0
        return ("%dM" % round(m)) if abs(m - round(m)) < 0.05 else ("%.1fM" % m)
    if n >= 1_000:
        k = n / 1_000.0
        return ("%dK" % round(k)) if abs(k - round(k)) < 0.05 else ("%.1fK" % k)
    return "%d" % n


def seg_model(ctx):
    stdin = ctx["stdin"]
    name = gstr(stdin, "model", "display_name")
    if not name:
        return None
    # strip a redundant "(... context)" suffix like claude-hud stripContextSuffix
    import re
    name = re.sub(r"\s*\([^)]*\bcontext\b[^)]*\)", "", name).strip() or name
    # append the model's context-window CAPACITY in parens: 'Opus 4.8 (1M)'.
    cap = fmt_capacity(gnum(stdin, "context_window", "context_window_size"))
    if cap:
        name = "%s (%s)" % (name, cap)
    s = colorize(name, CYAN)
    # effort is NESTED under effort.level in the statusline payload (the old
    # top-level read returned nothing and silently dropped the effort glyph).
    effort = gstr(stdin, "effort", "level") or gstr(stdin, "effort")
    if effort:
        s += " %s %s" % (EFFORT_GLYPH, effort)
    return s


# ==========================================================================
# T3 optionals — tier=3, dropped FIRST under width pressure
# ==========================================================================
def _fmt_tokens(n):
    """Compact token count: 940 -> '940', 12000 -> '12k', 1500000 -> '1.5M'."""
    try:
        n = int(round(float(n)))
    except Exception:
        return None
    if n < 0:
        return None
    if n >= 1_000_000:
        m = n / 1_000_000.0
        return ("%dM" % round(m)) if abs(m - round(m)) < 0.05 else ("%.1fM" % m)
    if n >= 1_000:
        return "%dk" % round(n / 1_000.0)
    return "%d" % n


def seg_tokens(ctx):
    """Session token usage from context_window.current_usage: 'tok 18k↓ 2k↑ 140k⟳'
    (in / out / cache-read). T3 — informational, dropped first."""
    stdin = ctx["stdin"]
    cu = stdin.get("context_window")
    if not isinstance(cu, dict) or not isinstance(cu.get("current_usage"), dict):
        return None
    inp = gnum(stdin, "context_window", "current_usage", "input_tokens")
    out = gnum(stdin, "context_window", "current_usage", "output_tokens")
    cache = gnum(stdin, "context_window", "current_usage", "cache_read_input_tokens")
    bits = []
    fi = _fmt_tokens(inp) if inp else None
    fo = _fmt_tokens(out) if out else None
    fc = _fmt_tokens(cache) if cache else None
    if fi:
        bits.append("%s↓" % fi)
    if fo:
        bits.append("%s↑" % fo)
    if fc and ctx["cols"] >= 100:
        bits.append("%s⟳" % fc)
    if not bits:
        return None
    return "tok " + " ".join(bits)


def seg_burn(ctx):
    """Cost burn rate + day projection, derived CHEAPLY from the cost-cache that
    seg_cost already loaded (no JSONL parse). 'burn $4.2/h ~$101/d'. T3."""
    data = ctx.get("cost_cache")
    if not isinstance(data, dict):
        return None
    day = gnum(data, "day_usd")
    if day is None or day <= 0:
        return None
    # Hours elapsed in the local day so far -> simple linear projection.
    lt = time.localtime(ctx["now"])
    hours_elapsed = lt.tm_hour + lt.tm_min / 60.0
    if hours_elapsed < 0.25:
        return None  # too early in the day for a meaningful rate
    rate = day / hours_elapsed
    proj = rate * 24.0
    if ctx["cols"] >= 100:
        return "burn %s/h ~%s/d" % (_fmt_usd(rate), _fmt_usd(proj))
    return "burn %s/h" % _fmt_usd(rate)


# ==========================================================================
# Segment registry + greedy responsive layout
# ==========================================================================
# Each segment: (key, tier, render_fn). Lower tier number = higher priority.
# T3 (tier 3) segments are dropped FIRST under width pressure.
# Line-1 order: context · quota(5h/7d) · dir+git · model · cost · tokens · burn · notebook
# Costs segment full form (cols>=100, calibrated): 'Costs: $d · $w · $m · $t · €<sub> · ~<1d>·<7d>·<30d>'
SEGMENTS = [
    ("context",  1, seg_context),
    ("quota",    1, seg_quota),
    ("dirgit",   1, seg_dirgit),
    ("model",    2, seg_model),
    ("cost",     1, seg_cost),
    ("tokens",   3, seg_tokens),
    ("burn",     3, seg_burn),
    ("notebook", 1, seg_notebook),
]


def pack_lines(rendered, cols, max_lines):
    """Greedy horizontal packing with a strict NO-CROP guarantee.

    Joins rendered segments (in priority order) with SEP until the next would
    exceed `cols`, then wraps to a new line. A single segment wider than `cols`
    is HARD-WRAPPED across multiple lines (never emitted whole and left to be
    cropped by the terminal) — owner's explicit wrap-not-crop preference.

    Returns (lines, overflow): every line in `lines` has visible_len <= cols, and
    `overflow` counts segments that could not be placed within max_lines (so the
    caller can drop the lowest-priority ones and retry).

    INVARIANT: for every returned line L, visible_len(L) <= cols.
    """
    lines = []
    cur = ""
    placed = 0
    sep_w = visible_len(SEP)
    safe_cols = max(1, cols)

    def flush():
        # append cur (if any), returns False if the line budget is exhausted.
        nonlocal cur
        if cur:
            lines.append(cur)
            cur = ""
        return len(lines) < max_lines

    for idx, seg in enumerate(rendered):
        seg_w = visible_len(seg)

        if seg_w > safe_cols:
            # Segment alone exceeds the width: flush the current line, then emit
            # the segment as hard-wrapped chunks (each <= cols). This replaces the
            # old "show it whole and let the terminal crop it" behaviour.
            if cur:
                if not flush():
                    return lines, len(rendered) - placed
            for chunk in hard_wrap_segment(seg, safe_cols):
                if len(lines) >= max_lines:
                    return lines, len(rendered) - placed
                lines.append(chunk)
            placed += 1
            continue

        if cur == "":
            cur = seg
            placed += 1
            continue
        if visible_len(cur) + sep_w + seg_w <= safe_cols:
            cur += SEP + seg
            placed += 1
        else:
            if not flush():
                return lines, len(rendered) - placed
            cur = seg
            placed += 1

    if cur:
        lines.append(cur)
    overflow = len(rendered) - placed
    if len(lines) > max_lines:
        overflow += len(lines) - max_lines
        lines = lines[:max_lines]
    return lines, overflow


def layout(ctx, reserve_lines):
    """Render all segments, then greedily pack into <= (MAX_LINES - reserve_lines)
    lines, dropping the lowest-priority (highest tier number) segments first until
    everything fits. T1 (tier 1) is never dropped — it has already self-abbreviated
    via the width-aware render fns, so at worst T1 occupies all available lines.
    """
    budget = max(1, MAX_LINES - reserve_lines)
    cols = ctx["cols"]

    # render in priority order; keep (tier, text)
    candidates = []
    for _key, tier, fn in SEGMENTS:
        try:
            text = fn(ctx)
        except Exception:
            text = None  # a misbehaving segment must never break the render
        if text:
            candidates.append((tier, text))

    # try to fit; if overflow, drop the lowest-priority segment and retry.
    while candidates:
        rendered = [t for _tier, t in candidates]
        lines, overflow = pack_lines(rendered, cols, budget)
        if overflow <= 0:
            return lines
        # drop the lowest-priority (max tier) trailing segment, then retry.
        drop_idx = None
        worst_tier = max(tier for tier, _t in candidates)
        if worst_tier <= 1:
            # only T1 left and still overflowing: accept the truncated packing
            # (T1 is never dropped — show as many lines as the budget allows).
            return lines
        for i in range(len(candidates) - 1, -1, -1):
            if candidates[i][0] == worst_tier:
                drop_idx = i
                break
        if drop_idx is None:
            return lines
        candidates.pop(drop_idx)

    return []


def _persist_rate_sample(stdin, now):
    """Write ~/.claude/.rate-latest.json with the current rate-limit sample.

    Called once per render (after read_stdin). Guards: if seven_day_pct is None
    (API-key sessions have no rate_limits at all), returns immediately without
    overwriting a good prior sample with nulls. The entire body is wrapped in a
    broad except so a filesystem hiccup can never break the render.
    """
    try:
        seven_day_pct = gnum(stdin, "rate_limits", "seven_day", "used_percentage")
        if seven_day_pct is None:
            return  # no rate data this tick; preserve whatever was written before

        five_hour_pct = gnum(stdin, "rate_limits", "five_hour", "used_percentage")

        # resets_at: prefer numeric epoch; fall back to ISO-8601 Z string.
        rr = gnum(stdin, "rate_limits", "seven_day", "resets_at")
        if rr is None:
            rs = gstr(stdin, "rate_limits", "seven_day", "resets_at")
            rr = parse_iso_z(rs) if rs else None
        resets_at = int(rr) if rr is not None else None

        payload = {
            "schema": 1,
            "ts": now,
            "five_hour_pct": five_hour_pct,
            "seven_day_pct": seven_day_pct,
            "seven_day_resets_at": resets_at,
        }
        import tempfile
        rate_dir = os.path.dirname(RATE_LATEST_PATH)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=".rate-latest.", suffix=".json.tmp", dir=rate_dir)
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh)
            os.replace(tmp, RATE_LATEST_PATH)
            tmp = None
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
    except Exception:
        pass


CONTEXT_LATEST_PATH = os.path.join(HOME, ".claude", ".context-latest.json")


def _persist_context_sample(stdin):
    """Write ~/.claude/.context-latest.json with the current context-window sample.

    Mirrors the atomic temp-file write pattern of _persist_rate_sample. Guards:
    if context_window is absent we return immediately without overwriting a good
    prior sample. The entire body is wrapped in a broad except so a filesystem
    hiccup can never break the render.
    """
    try:
        cw = stdin.get("context_window") if isinstance(stdin, dict) else None
        if not isinstance(cw, dict):
            return  # no context_window this tick; preserve whatever was written before

        used_pct = gnum(stdin, "context_window", "used_percentage") or 0.0
        window_size = int(gnum(stdin, "context_window", "context_window_size") or 0)
        inp = gnum(stdin, "context_window", "current_usage", "input_tokens") or 0.0
        cache_read = gnum(stdin, "context_window", "current_usage", "cache_read_input_tokens") or 0.0
        cache_create = gnum(stdin, "context_window", "current_usage", "cache_creation_input_tokens") or 0.0
        used_tokens = int(inp + cache_read + cache_create)

        payload = {
            "schema": 1,
            "ts": int(time.time()),
            "used_pct": float(used_pct),
            "window_size": window_size,
            "used_tokens": used_tokens,
        }
        import tempfile
        ctx_dir = os.path.dirname(CONTEXT_LATEST_PATH)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=".context-latest.", suffix=".json.tmp", dir=ctx_dir)
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh)
            os.replace(tmp, CONTEXT_LATEST_PATH)
            tmp = None
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
    except Exception:
        pass


def main():
    now = time.time()
    stdin = read_stdin()
    _persist_rate_sample(stdin, now)
    _persist_context_sample(stdin)
    cols = terminal_width(stdin)

    ctx = {
        "stdin": stdin,
        "now": now,
        "cols": cols,
        "bar_w": bar_width(cols),
        "cost_cache": load_cost_cache(),
    }

    lines = layout(ctx, 0)

    if not lines:
        # Fully degraded but never blank: show whatever single hint we have.
        fallback = gstr(stdin, "model", "display_name") or "telemetry: no signal"
        print(fallback)
        return

    print("\n".join(lines[:MAX_LINES]))


if __name__ == "__main__":
    main()
