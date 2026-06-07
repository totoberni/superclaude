#!/usr/bin/env python3
"""Compute cumulative cross-session Claude Code cost and write .cost-cache.json.

Streams every ~/.claude/projects/**/*.jsonl transcript line-by-line, tallies
token usage from assistant records, prices it per the table below, and buckets
into today / this-week / this-month / all-time. Atomic write on success only.
"""
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone, date, timedelta

# --------------------------------------------------------------------------- #
# PRICING TABLE  (USD per 1,000,000 tokens, per model family)                 #
# --------------------------------------------------------------------------- #
# Rates cross-checked against ryoppippi/ccusage (the LiteLLM-backed community
# reference). Patterns are matched in order; first match wins. More-specific
# families (Haiku 4.x vs Haiku 3.5, legacy Opus 4/4.1 vs Opus >=4.5) precede
# broader fallbacks.
#
# Cache token costs are derived from the INPUT rate. Cache WRITES are priced by
# the ephemeral TTL window of the cache entry:
#   cache write, 5-minute TTL : input_rate * 1.25
#   cache write, 1-hour   TTL : input_rate * 2.00
#   cache read                : input_rate * 0.10
# Newer transcripts carry a `usage.cache_creation` dict splitting the write into
# `ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`; older transcripts
# carry only the flat `cache_creation_input_tokens`, which is billed at the 5m
# rate (1.25) as a conservative fallback. Pricing the 1h writes at the correct
# 2.00 multiplier (instead of a flat 1.25) was the dominant cache underbilling.
#
# Per-version Opus rates (VERIFIED, USD/Mtok in/out):
#   Opus 4, Opus 4.1            -> (15, 75)   [legacy; not in current corpus]
#   Opus 4.5 / 4.6 / 4.7 / 4.8  -> (5, 25)
#   Sonnet 4 / 4.5 / 4.6, 3.7, 3.5 -> (3, 15)
#   Haiku >=4                   -> (1, 5)
#   Haiku 3.5                   -> (0.80, 4)
#
# Pricing is EFFECTIVE-DATED: each pattern maps to a list of
# (effective_from_date_or_None, (in_rate, out_rate)) tuples. `price_for` selects
# the entry with the latest `effective_from` that is <= the record's local date
# (None == "from the beginning of time"; the implicit floor). No model has
# re-priced yet, so every current entry is seeded with effective_from=None and
# the effective-dating changes nothing for the present corpus — it is structure
# for the future, and it also lets any legacy 4.0/4.1 record be priced correctly.
PRICING_ASOF = "2026-06-01 (cross-checked vs ryoppippi/ccusage)"
CACHE_WRITE_MULTIPLIER = 1.25      # 5-minute ephemeral cache-write rate
CACHE_WRITE_1H_MULTIPLIER = 2.00   # 1-hour ephemeral cache-write rate
CACHE_READ_MULTIPLIER = 0.10
TOKENS_PER_MILLION = 1_000_000

# Newest Opus rate, used as the fallback for any real model that matches no
# pattern (e.g. a future "Opus 5"): bill it rather than silently drop it, and
# surface the model name in the cache payload's "unpriced_models" key.
FALLBACK_RATES = (5.0, 25.0)

# Each entry: (compiled pattern, [(effective_from_date_or_None, (in, out)), ...]).
# effective_from is an ISO "YYYY-MM-DD" string or None; the list is searched for
# the latest effective_from <= record date. None sorts before all real dates.
MODEL_PRICING = [
    (re.compile(r"\bopus 4 5\b"),          [(None, (5.0, 25.0))]),   # Opus 4.5
    (re.compile(r"\bopus 4 6\b"),          [(None, (5.0, 25.0))]),   # Opus 4.6
    (re.compile(r"\bopus 4 7\b"),          [(None, (5.0, 25.0))]),   # Opus 4.7
    (re.compile(r"\bopus 4 8\b"),          [(None, (5.0, 25.0))]),   # Opus 4.8
    (re.compile(r"\bopus 4 1\b"),          [(None, (15.0, 75.0))]),  # legacy Opus 4.1
    (re.compile(r"\bopus 4\b"),            [(None, (15.0, 75.0))]),  # legacy Opus 4.0
    (re.compile(r"\bsonnet 4(?: \d+)?\b"), [(None, (3.0, 15.0))]),   # Sonnet 4.x
    (re.compile(r"\bsonnet 3 7\b"),        [(None, (3.0, 15.0))]),
    (re.compile(r"\bsonnet 3 5\b"),        [(None, (3.0, 15.0))]),
    (re.compile(r"\bhaiku 4(?: \d+)?\b"),  [(None, (1.0, 5.0))]),    # Haiku >=4
    (re.compile(r"\bhaiku 3 5\b"),         [(None, (0.8, 4.0))]),
    (re.compile(r"\bopusplan\b"),          [(None, (5.0, 25.0))]),   # enterprise aliases
    (re.compile(r"\bsonnetplan\b"),        [(None, (3.0, 15.0))]),
    (re.compile(r"\bhaikuplan\b"),         [(None, (0.8, 4.0))]),
]

# --------------------------------------------------------------------------- #
# SUBSCRIPTION SOT — edit when a new invoice posts or the rate/plan changes.  #
# LEDGER_THROUGH must equal the last CONFIRMED invoice date.                  #
# --------------------------------------------------------------------------- #
# Confirmed invoices (IVA-incl EUR, paid in EUR, no FX).
# (invoice_date "YYYY-MM-DD", amount_eur)
SUBSCRIPTION_LEDGER = [
    ("2026-02-27", 109.80),
    ("2026-03-08", 141.91),
    ("2026-04-08", 219.60),
    ("2026-05-08", 219.60),
]
LEDGER_THROUGH = "2026-05-08"       # date of the last CONFIRMED invoice
MONTHLY_SUBSCRIPTION_EUR = 219.60   # current Max-20x recurring charge
BILLING_DAY = 8                     # day-of-month the recurring invoice posts
EUR_USD_RATE = 1.08                 # comparison only; configurable
# --------------------------------------------------------------------------- #

PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")
CACHE_PATH = os.path.join(os.path.expanduser("~"), ".claude", ".cost-cache.json")
LEDGER_PATH = os.path.join(os.path.expanduser("~"), ".claude", ".cost-ledger.json")

# ---- TIER ADVISOR CONFIG (self-calibrated ESTIMATE — Anthropic publishes no absolute weekly limits) ----
CURRENT_TIER = "X20"
TIER_RATIO_X20_OVER_X5 = 1.5   # Max20x:Max5x all-models WEEKLY capacity ratio. APPROXIMATE:
                               # Aug-2025 Opus weekly hours imply ~1.3x (Max20x 24-40h vs Max5x 15-35h),
                               # the Sonnet ratio ~1.71x; 1.5 is a conservative blend. No official number exists.
TIER_SAFETY_MARGIN = 0.85      # recommend X5 only if projected X5 utilization <= 85%
TIER_CALIB_MIN_PCT = 2.0       # ignore seven_day_pct below this (post-reset noise)
TIER_CALIB_MAX_PCT = 95.0      # ignore near-saturation readings
# Sensitivity: at steady state the 7d decision reduces to
#   util5 = TIER_RATIO_X20_OVER_X5 * (seven_day_pct/100),
# so the X5->X20 flip point is seven_day_pct ~= 100*TIER_SAFETY_MARGIN/TIER_RATIO_X20_OVER_X5 (~56.7%).
# The recommendation hinges on TIER_RATIO_X20_OVER_X5; at low pct (e.g. 7%) the X5 call has wide margin.
RATE_LATEST_PATH = os.path.join(os.path.expanduser("~"), ".claude", ".rate-latest.json")


def normalize_model(name):
    """Mirror claude-hud normalizeModelName (cost.ts)."""
    s = name.lower()
    s = re.sub(r"^claude\s+", "", s)
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[._-]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _rate_effective_at(schedule, record_date):
    """Pick the (in, out) rate from an effective-dated schedule for record_date.

    `schedule` is a list of (effective_from_or_None, (in, out)). The entry with
    the latest effective_from that is <= record_date wins; None == the implicit
    floor (applies from the beginning of time). When record_date is None (an
    unparseable timestamp) only None-dated entries are eligible.
    """
    best_key = None      # comparable sort key of the winning entry
    best_rates = None
    for eff, rates in schedule:
        # Normalize effective_from to a comparable key: None -> "" (sorts first).
        key = eff if eff is not None else ""
        if record_date is not None and eff is not None and eff > record_date:
            continue  # this rate is not yet in effect for this record
        if best_key is None or key >= best_key:
            best_key = key
            best_rates = rates
    return best_rates


def price_for(model_name, record_date):
    """Return ((input_rate, output_rate), matched) for a model at record_date.

    `matched` is True when a pricing pattern matched the model; False means no
    pattern matched and the caller should apply FALLBACK_RATES and surface the
    model as unpriced. The rate honours the effective-dated schedule.
    """
    norm = normalize_model(model_name)
    for pat, schedule in MODEL_PRICING:
        if pat.search(norm):
            rates = _rate_effective_at(schedule, record_date)
            if rates is not None:
                return rates, True
    return FALLBACK_RATES, False


def cost_of(usage, rates):
    """USD for one usage dict given (input_rate, output_rate) per 1M."""
    in_rate, out_rate = rates

    def n(key):
        v = usage.get(key)
        return v if isinstance(v, (int, float)) and v >= 0 else 0

    inp = n("input_tokens")
    out = n("output_tokens")
    cr = n("cache_read_input_tokens")

    # Cache-write cost: prefer the per-window split from `usage.cache_creation`
    # (5-minute vs 1-hour ephemeral TTL); fall back to the flat
    # cache_creation_input_tokens at the 5m rate for older transcripts that
    # predate the dict.
    cache = usage.get("cache_creation")
    if isinstance(cache, dict):
        def nc(key):
            v = cache.get(key)
            return v if isinstance(v, (int, float)) and v >= 0 else 0
        cc5m = nc("ephemeral_5m_input_tokens")
        cc1h = nc("ephemeral_1h_input_tokens")
        cache_usd = (
            cc5m * in_rate * CACHE_WRITE_MULTIPLIER
            + cc1h * in_rate * CACHE_WRITE_1H_MULTIPLIER
        )
    else:
        cc = n("cache_creation_input_tokens")
        cache_usd = cc * in_rate * CACHE_WRITE_MULTIPLIER

    usd = (
        inp * in_rate
        + out * out_rate
        + cache_usd
        + cr * in_rate * CACHE_READ_MULTIPLIER
    )
    return usd / TOKENS_PER_MILLION


def local_date_of(ts_str):
    """Parse an ISO-8601 timestamp (e.g. '2026-06-01T16:04:57.678Z') to a LOCAL
    calendar date. Returns None if unparseable."""
    if not ts_str or not isinstance(ts_str, str):
        return None
    s = ts_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().date()  # convert to local tz, take the date


def iter_jsonl_files(root):
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn.endswith(".jsonl"):
                yield os.path.join(dirpath, fn)


def subscription_totals(today):
    """Compute confirmed + auto-accrued EUR subscription totals.

    Confirmed = sum of SUBSCRIPTION_LEDGER entries (IVA-incl invoices).
    Accrued   = number of BILLING_DAY-th-of-month dates that are strictly after
                LEDGER_THROUGH and <= today, each adding MONTHLY_SUBSCRIPTION_EUR.

    Robust to an empty ledger (returns (0.0, 0, 0.0)).
    Returns (confirmed_eur, months_accrued, total_eur).
    """
    confirmed = 0.0
    for _dt, amount in SUBSCRIPTION_LEDGER:
        if isinstance(amount, (int, float)) and amount >= 0:
            confirmed += amount
    confirmed = round(confirmed, 2)

    # Auto-accrual: find each BILLING_DAY-th after LEDGER_THROUGH and <= today.
    months_accrued = 0
    try:
        ledger_through = date.fromisoformat(LEDGER_THROUGH)
    except (ValueError, TypeError):
        ledger_through = None

    if ledger_through is not None and MONTHLY_SUBSCRIPTION_EUR > 0:
        # Walk forward month by month starting from the month after LEDGER_THROUGH.
        yr = ledger_through.year
        mo = ledger_through.month
        while True:
            # Advance to the next month.
            mo += 1
            if mo > 12:
                mo = 1
                yr += 1
            # The billing date for this calendar month.
            try:
                billing = date(yr, mo, BILLING_DAY)
            except ValueError:
                # BILLING_DAY > days in this month (e.g. day=31 in Feb); skip.
                billing = None
            if billing is None:
                # If we can't form a valid date, stop to avoid an infinite loop.
                break
            if billing > today:
                break  # not yet reached
            if billing > ledger_through:
                months_accrued += 1

    accrued_eur = round(months_accrued * MONTHLY_SUBSCRIPTION_EUR, 2)
    total_eur = round(confirmed + accrued_eur, 2)
    return confirmed, months_accrued, total_eur


def load_ledger():
    """Load the persistent cost ledger from LEDGER_PATH.

    Returns the `days` dict {YYYY-MM-DD: usd_float} from the ledger, or an
    empty dict if the file is missing, unreadable, or contains garbage. Never
    raises — callers must handle the empty-dict fallback path.
    """
    try:
        with open(LEDGER_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        days = raw.get("days")
        if not isinstance(days, dict):
            return {}
        # Validate each entry: key must be a YYYY-MM-DD string, value a number.
        cleaned = {}
        for k, v in days.items():
            if isinstance(k, str) and len(k) == 10 and isinstance(v, (int, float)):
                cleaned[k] = float(v)
        return cleaned
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def merge_ledger(existing_days, computed, today_iso):
    """Merge per-day computed costs into the existing ledger days dict.

    Rules:
      - today  → overwrite (today is still accumulating)
      - past   → max(existing, computed)  (high-water mark; JSONL deletion
                  only lowers a recompute, so max() preserves the full value)
      - aged-out (in existing but not in computed) → kept as-is

    Returns a NEW dict (does not mutate either input).
    """
    merged = dict(existing_days)  # start from existing (includes aged-out days)
    for date_iso, usd in computed.items():
        if date_iso == today_iso:
            merged[date_iso] = usd  # today: overwrite
        else:
            # Past day: high-water mark.
            merged[date_iso] = max(merged.get(date_iso, 0.0), usd)
    return merged


def write_ledger_atomic(days_dict):
    """Atomically write the ledger to LEDGER_PATH.

    Uses tempfile + fsync + os.replace — same pattern as the cache writer.
    Raises on failure (caller must handle and degrade gracefully).
    """
    payload = {
        "schema": 1,
        "days": days_dict,
        "updated": round(time.time(), 3),
    }
    ledger_dir = os.path.dirname(LEDGER_PATH)
    fd, tmp = tempfile.mkstemp(
        prefix=".cost-ledger.", suffix=".json.tmp", dir=ledger_dir
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, LEDGER_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def trailing_window_sum(days_dict, today, n_days):
    """Sum ledger values for the N calendar days [today-(n_days-1) .. today] inclusive."""
    total = 0.0
    for i in range(n_days):
        d = today - timedelta(days=i)
        total += days_dict.get(d.isoformat(), 0.0)
    return total


def compute_tier_advice(ledger_days, trailing, today, prev_m20):
    """Self-calibrated tier advisor (ESTIMATE — Anthropic publishes no absolute limits).

    Args:
        ledger_days: {date_iso: usd} from the persistent ledger.
        trailing:    {"1d": usd, "7d": usd, "30d": usd} trailing window sums.
        today:       date object for the current local date.
        prev_m20:    last calibrated M20 cap (float|None), reused when no fresh sample.

    Returns:
        (advice, calibrated, m20, m5)
        advice:      {"1d": "X5"|"X20", "7d": ..., "30d": ...} or None
        calibrated:  bool — True if a fresh telemetry sample was used
        m20:         float|None — estimated Max20x weekly capacity in USD
        m5:          float|None — estimated Max5x weekly capacity in USD
    """
    m20 = None
    calibrated_fresh = False

    try:
        try:
            with open(RATE_LATEST_PATH, "r", encoding="utf-8") as _f:
                _raw = json.load(_f)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            _raw = {}

        pct = _raw.get("seven_day_pct") if isinstance(_raw, dict) else None
        resets_at = _raw.get("seven_day_resets_at") if isinstance(_raw, dict) else None

        pct_ok = (
            isinstance(pct, (int, float))
            and TIER_CALIB_MIN_PCT <= pct <= TIER_CALIB_MAX_PCT
        )
        resets_ok = isinstance(resets_at, (int, float)) and resets_at > 0

        if pct_ok and resets_ok:
            window_start = date.fromtimestamp(resets_at - 7 * 86400)
            usage_since = sum(
                usd
                for d_iso, usd in ledger_days.items()
                if date.fromisoformat(d_iso) >= window_start
            )
            if usage_since > 0:
                m20 = usage_since / (pct / 100.0)
                calibrated_fresh = True

        if not calibrated_fresh:
            m20 = prev_m20

    except Exception:
        m20 = prev_m20

    if m20 is None or m20 <= 0:
        return (None, False, None, None)

    m5 = m20 / TIER_RATIO_X20_OVER_X5

    advice = {}
    for h, days_n in (("1d", 1), ("7d", 7), ("30d", 30)):
        u = trailing[h] * (7.0 / days_n)
        util5 = u / m5
        advice[h] = "X5" if util5 <= TIER_SAFETY_MARGIN else "X20"

    return (advice, calibrated_fresh, m20, m5)


def main():
    if not os.path.isdir(PROJECTS_DIR):
        print(f"cost-cache-refresh: no projects dir at {PROJECTS_DIR}", file=sys.stderr)
        return 1

    # Read prev_m20 BEFORE overwriting the cache (resilient; None if absent/garbage).
    prev_m20 = None
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as _cf:
            _old = json.load(_cf)
        _v = _old.get("tier_m20_usd")
        if isinstance(_v, (int, float)) and _v > 0:
            prev_m20 = float(_v)
    except (OSError, json.JSONDecodeError, TypeError, ValueError, AttributeError):
        pass

    today = datetime.now().astimezone().date()
    iso_year, iso_week, _ = today.isocalendar()  # ISO week of "today"
    cur_month = (today.year, today.month)

    cutoff_30d = today - timedelta(days=30)  # trailing-30d window (inclusive)

    n_files = 0
    n_unpriced = 0     # real records that matched no pattern (billed at fallback)
    n_raw_dedupable = 0  # priced records that carried a (message.id, requestId)
    unpriced_models = {}  # {model_name: count} — surfaced, never silently dropped

    # Global dedup, matching ryoppippi/ccusage. The same API response is logged
    # to the transcript 2-3x (dual-logging), and the SAME (message.id, requestId)
    # pair can recur non-consecutively and across files (e.g. a session resumed
    # into a second transcript). Keying globally on (message.id, requestId) and
    # letting the LAST occurrence win collapses every such duplicate to one
    # record carrying the final (complete) streamed usage counts. A per-file
    # consecutive-only dedup (the previous approach) missed the cross-file and
    # non-consecutive duplicates and inflated the totals.
    #
    # `deduped[(message_id, request_id)] = (usd, local_date)` — last write wins.
    # Records missing either id (cannot be safely deduped) are always counted
    # via `undeduped`. Sidechain (subagent) records ARE counted: that token
    # usage is real spend, and ccusage includes it too.
    deduped = {}
    undeduped = []

    for path in iter_jsonl_files(PROJECTS_DIR):
        n_files += 1
        try:
            fh = open(path, "r", encoding="utf-8")
        except OSError:
            # An unreadable file must not corrupt the whole run; skip it.
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                # Fast pre-filter: only assistant records carry usage. Skipping
                # the json.loads on the ~90% of lines without "usage" keeps the
                # full 256MB scan to a few seconds.
                if '"usage"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue

                model_name = msg.get("model", "")
                # Synthetic records (e.g. local placeholder turns) carry no real
                # spend and are never priced.
                if model_name == "<synthetic>":
                    continue

                d = local_date_of(obj.get("timestamp"))
                rates, matched = price_for(model_name, d)
                if not matched:
                    # Real model we don't have a rate for (e.g. a future "Opus
                    # 5"): bill it at the fallback rate so it is NOT silently
                    # dropped, and surface the model name in the payload.
                    n_unpriced += 1
                    unpriced_models[model_name] = unpriced_models.get(model_name, 0) + 1

                usd = cost_of(usage, rates)

                msg_id = msg.get("id")
                req_id = obj.get("requestId")
                if msg_id is not None and req_id is not None:
                    n_raw_dedupable += 1
                    deduped[(msg_id, req_id)] = (usd, d)  # last occurrence wins
                else:
                    undeduped.append((usd, d))

    day_usd = 0.0
    week_usd = 0.0
    month_usd = 0.0
    n_records = 0  # unique usage records counted (post-dedup)

    # Per-day cost map from the current JSONL corpus (None-dated records excluded
    # from the ledger but still counted toward period buckets as today).
    computed = {}  # {YYYY-MM-DD: usd_sum}
    today_iso = today.isoformat()

    for usd, d in list(deduped.values()) + undeduped:
        n_records += 1
        if d is None:
            # Cannot bucket by calendar date — count toward today in the ledger.
            computed[today_iso] = computed.get(today_iso, 0.0) + usd
            # Do NOT count toward period buckets (unchanged behaviour).
            continue
        d_iso = d.isoformat()
        computed[d_iso] = computed.get(d_iso, 0.0) + usd
        if d == today:
            day_usd += usd
        iy, iw, _ = d.isocalendar()
        if iy == iso_year and iw == iso_week:
            week_usd += usd
        if (d.year, d.month) == cur_month:
            month_usd += usd

    # --------------------------------------------------------------------- #
    # PERSISTENT LEDGER: load → merge → write atomically (fail-safe).       #
    # --------------------------------------------------------------------- #
    ledger_ok = False
    ledger_days = {}

    try:
        existing_days = load_ledger()
        merged_days = merge_ledger(existing_days, computed, today_iso)
        write_ledger_atomic(merged_days)
        ledger_days = merged_days
        ledger_ok = True
    except Exception as ledger_exc:
        print(
            f"cost-cache-refresh: WARNING ledger write failed ({ledger_exc!r}); "
            "falling back to JSONL-only total (non-cumulative)",
            file=sys.stderr,
        )

    if ledger_ok:
        total_usd = round(sum(ledger_days.values()), 6)
        trailing_1d_usd = round(trailing_window_sum(ledger_days, today, 1), 6)
        trailing_7d_usd = round(trailing_window_sum(ledger_days, today, 7), 6)
        trailing_30d_usd = round(trailing_window_sum(ledger_days, today, 30), 6)
    else:
        # Degrade gracefully: fall back to JSONL-only sum (old behaviour).
        total_usd = round(sum(computed.values()), 6)
        trailing_30d_usd_records = 0.0
        cutoff_30d = today - timedelta(days=29)  # exactly 30 days inclusive
        for d_iso, usd in computed.items():
            try:
                d = date.fromisoformat(d_iso)
            except ValueError:
                continue
            if d >= cutoff_30d:
                trailing_30d_usd_records += usd
        trailing_1d_usd = round(computed.get(today_iso, 0.0), 6)
        trailing_7d_usd = round(
            sum(
                computed.get((today - timedelta(days=i)).isoformat(), 0.0)
                for i in range(7)
            ),
            6,
        )
        trailing_30d_usd = round(trailing_30d_usd_records, 6)

    # --------------------------------------------------------------------- #
    # TIER ADVISOR: self-calibrated estimate (fail-safe wrapper).           #
    # --------------------------------------------------------------------- #
    tier_advice = None
    tier_calibrated = False
    tier_m20_usd = None
    tier_m5_usd = None
    tier_asof = None
    try:
        _trailing = {
            "1d": trailing_1d_usd,
            "7d": trailing_7d_usd,
            "30d": trailing_30d_usd,
        }
        tier_advice, tier_calibrated, tier_m20_usd, tier_m5_usd = compute_tier_advice(
            ledger_days, _trailing, today, prev_m20
        )
        if tier_calibrated:
            tier_asof = time.time()
    except Exception as _tier_exc:
        print(
            f"cost-cache-refresh: WARNING tier advisor failed ({_tier_exc!r}); skipped",
            file=sys.stderr,
        )

    sub_eur_confirmed, sub_months_accrued, sub_eur_total = subscription_totals(today)

    # Monthly overspending signal: trailing-30d ledger value vs subscription cost.
    # >1.0 = API value delivered exceeds the flat subscription price (subscription
    # is "winning"); <1.0 = overspending relative to value delivered.
    # None when the denominator is zero.
    denom = EUR_USD_RATE * MONTHLY_SUBSCRIPTION_EUR
    if denom > 0:
        value_ratio_30d = round((trailing_30d_usd / EUR_USD_RATE) / MONTHLY_SUBSCRIPTION_EUR, 2)
    else:
        value_ratio_30d = None

    payload = {
        "schema": 1,
        "day_usd": round(day_usd, 6),
        "week_usd": round(week_usd, 6),
        "month_usd": round(month_usd, 6),
        "total_usd": total_usd,
        # Subscription — lifetime (PRIMARY metric)
        "subscription_eur_confirmed": sub_eur_confirmed,
        "subscription_months_accrued": sub_months_accrued,
        "subscription_eur_total": sub_eur_total,
        "monthly_subscription_eur": MONTHLY_SUBSCRIPTION_EUR,
        # Trailing-window sums from the persistent ledger (monotonic / cumulative).
        "trailing_1d_usd": trailing_1d_usd,
        "trailing_7d_usd": trailing_7d_usd,
        "trailing_30d_usd": trailing_30d_usd,
        "eur_usd_rate": EUR_USD_RATE,
        "value_ratio_30d": value_ratio_30d,
        "unpriced_models": unpriced_models,
        "pricing_asof": PRICING_ASOF,
        # Tier advisor (self-calibrated ESTIMATE)
        "tier_advice": tier_advice,
        "tier_calibrated": tier_calibrated,
        "tier_m20_usd": tier_m20_usd,
        "tier_m5_usd": tier_m5_usd,
        "tier_asof": tier_asof,
        "updated": round(time.time(), 3),
    }

    # Atomic write: temp file in the SAME directory (so os.replace is atomic on
    # the same filesystem), fsync, then os.replace over the live cache. Any
    # exception before os.replace leaves the previous cache fully intact.
    cache_dir = os.path.dirname(CACHE_PATH)
    fd, tmp = tempfile.mkstemp(
        prefix=".cost-cache.", suffix=".json.tmp", dir=cache_dir
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CACHE_PATH)
    except BaseException:
        # Never leave a partial temp file behind, and never clobber the cache.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    # Diagnostics to stderr (stdout stays clean for any caller capturing JSON).
    vr = payload["value_ratio_30d"]
    vr_str = f"{vr:.2f}" if vr is not None else "n/a"
    ledger_tag = "ledger=ok" if ledger_ok else "ledger=DEGRADED"
    # Tier diagnostics
    if tier_advice is not None:
        _adv = tier_advice
        tier_tag = (
            f"tier={_adv.get('1d','?')}·{_adv.get('7d','?')}·{_adv.get('30d','?')} "
            f"m20=${round(tier_m20_usd)}"
        )
    else:
        tier_tag = "tier=n/a"
    print(
        f"cost-cache-refresh: files={n_files} records={n_records} "
        f"dup_skipped={n_raw_dedupable - len(deduped)} unpriced={n_unpriced} "
        f"day=${payload['day_usd']:.4f} week=${payload['week_usd']:.4f} "
        f"month=${payload['month_usd']:.4f} total=${payload['total_usd']:.4f} "
        f"sub_lifetime=€{payload['subscription_eur_total']:.2f} "
        f"(confirmed=€{payload['subscription_eur_confirmed']:.2f} accrued={payload['subscription_months_accrued']}mo) "
        f"t1d=${payload['trailing_1d_usd']:.4f} t7d=${payload['trailing_7d_usd']:.4f} "
        f"t30d=${payload['trailing_30d_usd']:.4f} value_ratio_30d={vr_str} {ledger_tag} {tier_tag}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # fail-safe: report, leave cache untouched, exit nonzero
        print(f"cost-cache-refresh: FAILED ({exc!r}); cache left intact", file=sys.stderr)
        sys.exit(1)
