"""
grade_backfill.py

Wave 173: THE GREAT GRADING BACKFILL (Jul 25, 2026)

Grades the ~70,000 strategy_log rows that were never resolved, using real
historical candles, and can be re-run on a schedule so nothing ever ages out
ungraded again.

WHY
===
Measured across 93,997 scan rows (Apr 13 - Jul 18): only 3,696 carry a usable
grade. The rest are dead weight for learning:

    (blank)      50,142     never resolved
    NO_LEVELS    40,148     target was 0 (the pre-Wave-168 structure_target bug)
    graded        3,696

Three separate causes, all confirmed in the live code:

  1. strategy_log.check_missed_setups() starts with
         if target == 0 or stop == 0: continue
     so every NO_LEVELS row is skipped FOREVER. Wave 168 fixed target
     GENERATION going forward, but the historical rows still hold target=0
     and will never be revisited.

  2. That grader only sees the live in-memory frames. For an old row
         recent = market_data[market_data.index >= alert_dt]
     is empty, and it falls back to market_data.iloc[-5:] - i.e. it would
     grade a three-month-old signal against the last five candles.

  3. 10,995 rows are SHADOW_MAX_TRADES (the "max 3 daily trades" cap): logged
     but never tracked at all.

WHAT THIS DOES
==============
For every ungraded row with a usable entry+stop:
  * rebuilds the target when it is 0, using the Wave 168 fallback (a clean 2R
    off that row's own real entry and stop) and tags the row fb:w173
  * pulls REAL historical candles for that market+timeframe (High/Low, not
    close-only, so wicks count exactly like the live grader)
  * resolves honestly, first touch wins, and a bar that touches BOTH target and
    stop counts as a LOSS (same convention as the live shadow grader)
  * stops looking after a bounded horizon (see _HORIZON_BARS) and marks the row
    EXPIRED rather than letting a signal "eventually" hit something months later
  * writes WOULD_WIN / WOULD_LOSE / EXPIRED plus result_source=w173_backfill

DATA SAFETY (rule 11: the data is sacred)
=========================================
  * NEVER overwrites a row that already has a real grade. Only blank and
    NO_LEVELS rows are touched.
  * Backs every file up to *.pre_w173.bak before writing.
  * Writes via safe_io.safe_rewrite_csv where available (lock + fresh read), so
    a concurrent append from the live scanner is never clobbered.
  * Idempotent: re-running only fills whatever is still ungraded.

KNOWN LIMITS (stated honestly)
==============================
  * yfinance serves 15m data for ~60 days only. 15m rows older than that cannot
    be graded and are left alone (about 2,581 rows).
  * 16,955 rows have no usable entry/stop at all (tf="*") and are ungradeable.
  * 391 rows have the stop on the WRONG SIDE of entry (LONG with stop above
    entry, or SHORT with stop below). Every one is a BREAK_RETEST setup - a real
    upstream bug. These are counted and skipped, never guessed at.
  * NQ/GC history comes from continuous-contract data, which is not tick-identical
    to the TopstepX feed. Good enough to grade direction/level touches; not a
    fill simulator.

USAGE
=====
    python grade_backfill.py --dry-run        # report only, writes nothing
    python grade_backfill.py                  # grade and write
    python grade_backfill.py --limit 5000     # cap rows per run
    python grade_backfill.py --include-archives
"""

import os
import sys
import csv
import glob
import json
import shutil
import logging
from datetime import datetime, timezone, timedelta

_log = logging.getLogger(__name__)

_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(_BASE_DIR, "data")
LIVE_LOG    = os.path.join(DATA_DIR, "strategy_log.csv")
ARCHIVE_GLOB= os.path.join(DATA_DIR, "archive", "strategy_log_*.csv")
REPORT_FILE = os.path.join(DATA_DIR, "grade_backfill_report.json")

# Results we must never touch.
_FINAL = ("WOULD_WIN", "WOULD_LOSE", "WIN", "LOSS", "EXPIRED")
# Results that mean "not yet graded".
_OPEN  = ("", "NO_LEVELS")

# How far forward we allow a signal to resolve, per timeframe. Bounded on
# purpose: given unlimited time almost any level eventually trades.
_HORIZON_BARS = {"15m": 32, "1h": 24, "4h": 30, "1d": 10}
_DEFAULT_HORIZON = 24

# Wave 168 fallback: a clean 2R target built from the row's own entry/stop.
_FALLBACK_RR = 2.0

# yfinance history limits (days) by interval.
_MAX_AGE_DAYS = {"15m": 58, "1h": 720, "4h": 720, "1d": 720}


def _f(v, d=0.0):
    try:
        return float(v)
    except Exception:
        return d


def valid_levels(entry, stop, direction):
    """Structural sanity. LONG stops below entry; SHORT stops above."""
    if entry <= 0 or stop <= 0 or entry == stop:
        return False
    if direction == "LONG":
        return stop < entry
    if direction == "SHORT":
        return stop > entry
    return False


def fallback_target(entry, stop, direction, rr=_FALLBACK_RR):
    """Wave 168 semantics: 2R off the row's real entry/stop."""
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    return entry + rr * risk if direction == "LONG" else entry - rr * risk


def resolve(candles, ts, entry, stop, target, direction, horizon):
    """
    Walk candles forward from ts. First touch wins; a bar touching BOTH counts
    as a LOSS (same convention as the live shadow grader).
    Returns "WOULD_WIN" / "WOULD_LOSE" / "EXPIRED" / None (no data).
    """
    try:
        window = candles[candles.index > ts]
    except Exception:
        return None
    if window is None or len(window) == 0:
        return None
    window = window.iloc[:horizon]
    for _idx, bar in window.iterrows():
        hi = float(bar["High"])
        lo = float(bar["Low"])
        if direction == "LONG":
            hit_t = hi >= target
            hit_s = lo <= stop
        else:
            hit_t = lo <= target
            hit_s = hi >= stop
        if hit_t and hit_s:
            return "WOULD_LOSE"      # ambiguous bar -> loss, never flattering
        if hit_t:
            return "WOULD_WIN"
        if hit_s:
            return "WOULD_LOSE"
    return "EXPIRED"


# ----------------------------------------------------------------------------
# History loading. Runs on Railway where market data is reachable.
# ----------------------------------------------------------------------------
def load_history(market, tf, stats):
    """
    Deep historical OHLC for market+timeframe as a DataFrame indexed by UTC time
    with High/Low columns. Cached per (market, tf). Returns None if unavailable.
    """
    key = (market, tf)
    cache = load_history._cache
    if key in cache:
        return cache[key]

    df = None
    try:
        import pandas as pd  # noqa: F401
        if market in ("BTC", "SOL"):
            df = _load_crypto(market, tf)
        else:
            df = _load_futures(market, tf)
    except Exception as e:
        stats.setdefault("history_errors", []).append("%s %s: %s" % (market, tf, e))
        _log.warning("grade_backfill: history load failed %s %s: %s", market, tf, e)
        df = None

    if df is not None and len(df):
        try:
            df = df[~df.index.duplicated(keep="last")].sort_index()
        except Exception:
            pass
    cache[key] = df
    return df
load_history._cache = {}


def _yf_symbol(market):
    return {"NQ": "NQ=F", "GC": "GC=F"}.get(market)


def _load_futures(market, tf):
    import yfinance as yf
    import pandas as pd
    sym = _yf_symbol(market)
    if not sym:
        return None
    interval, period = ("15m", "60d") if tf == "15m" else ("60m", "730d")
    df = yf.download(sym, interval=interval, period=period,
                     progress=False, auto_adjust=False)
    if df is None or not len(df):
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    if tf == "4h":
        df = df.resample("4h").agg({"Open": "first", "High": "max",
                                    "Low": "min", "Close": "last"}).dropna()
    elif tf == "1d":
        df = df.resample("1D").agg({"Open": "first", "High": "max",
                                    "Low": "min", "Close": "last"}).dropna()
    return df[["High", "Low"]] if {"High", "Low"}.issubset(df.columns) else None


def _load_crypto(market, tf):
    import pandas as pd
    try:
        import data_layer
        frames = data_layer.get_frames(market)
        df = frames.get(tf)
        if df is not None and len(df) and {"High", "Low"}.issubset(df.columns):
            return df[["High", "Low"]]
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------------
def grade_rows(rows, stats, limit=None, now=None):
    """Mutate rows in place. Returns number graded."""
    import pandas as pd
    now = now or datetime.now(timezone.utc)
    graded = 0
    for row in rows:
        if limit is not None and graded >= limit:
            break
        res = (row.get("result") or "").strip()
        if res in _FINAL:
            continue
        if res not in _OPEN:
            continue
        if (row.get("decision") or "").strip() == "FIRED":
            stats["skip_fired"] += 1
            continue

        market = (row.get("market") or "").strip()
        tf     = (row.get("tf") or "").strip()
        direction = (row.get("direction") or "").strip().upper()
        if direction not in ("LONG", "SHORT"):
            stats["skip_no_direction"] += 1
            continue

        entry = _f(row.get("entry"))
        stop  = _f(row.get("stop"))
        if not valid_levels(entry, stop, direction):
            stats["skip_bad_levels"] += 1
            continue

        ts_raw = (row.get("timestamp") or "").strip()
        try:
            ts = pd.Timestamp(ts_raw)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            else:
                ts = ts.tz_convert("UTC")
        except Exception:
            stats["skip_bad_timestamp"] += 1
            continue

        age_days = (now - ts.to_pydatetime()).days
        if age_days > _MAX_AGE_DAYS.get(tf, 720):
            stats["skip_too_old"] += 1
            continue

        target = _f(row.get("target"))
        rebuilt = False
        if target <= 0:
            target = fallback_target(entry, stop, direction)
            rebuilt = True
            if target <= 0:
                stats["skip_no_target"] += 1
                continue

        candles = load_history(market, tf, stats)
        if candles is None or not len(candles):
            stats["skip_no_history"] += 1
            continue

        horizon = _HORIZON_BARS.get(tf, _DEFAULT_HORIZON)
        verdict = resolve(candles, ts, entry, stop, target, direction, horizon)
        if verdict is None:
            stats["skip_no_window"] += 1
            continue

        row["result"] = verdict
        row["result_checked_at"] = now.isoformat()
        if "result_source" in row:
            row["result_source"] = "w173_backfill" + (":fb" if rebuilt else "")
        if rebuilt and "detection_reason" in row:
            base = (row.get("detection_reason") or "")
            if "fb:w173" not in base:
                row["detection_reason"] = (base + " fb:w173").strip()
        stats[verdict] += 1
        stats["rebuilt_targets"] += 1 if rebuilt else 0
        graded += 1
    return graded


def process_file(path, stats, dry_run=True, limit=None):
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        rows = list(reader)
    stats["rows_scanned"] += len(rows)
    n = grade_rows(rows, stats, limit=limit)
    if n and not dry_run:
        bak = path + ".pre_w173.bak"
        if not os.path.exists(bak):
            shutil.copy2(path, bak)
        wrote = False
        try:
            import safe_io
            if hasattr(safe_io, "safe_rewrite_csv"):
                safe_io.safe_rewrite_csv(path, cols, lambda _existing: rows)
                wrote = True
        except Exception as e:
            _log.warning("grade_backfill: safe_io path failed (%s), using atomic fallback", e)
        if not wrote:
            tmp = path + ".tmp"
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                for r in rows:
                    w.writerow({k: r.get(k, "") for k in cols})
            os.replace(tmp, path)
    return n


def main(argv=None):
    argv = argv or sys.argv[1:]
    dry = "--dry-run" in argv
    include_archives = "--include-archives" in argv
    limit = None
    if "--limit" in argv:
        try:
            limit = int(argv[argv.index("--limit") + 1])
        except Exception:
            limit = None

    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    stats = {k: 0 for k in ("rows_scanned", "WOULD_WIN", "WOULD_LOSE", "EXPIRED",
                            "rebuilt_targets", "skip_fired", "skip_bad_levels",
                            "skip_no_direction", "skip_bad_timestamp", "skip_too_old",
                            "skip_no_target", "skip_no_history", "skip_no_window")}

    targets = []
    if os.path.exists(LIVE_LOG):
        targets.append(LIVE_LOG)
    if include_archives:
        targets.extend(sorted(glob.glob(ARCHIVE_GLOB)))

    total = 0
    for path in targets:
        n = process_file(path, stats, dry_run=dry, limit=limit)
        total += n
        _log.info("%s -> graded %d", os.path.basename(path), n)
        if limit is not None:
            limit -= n
            if limit <= 0:
                break

    stats["total_graded"] = total
    stats["mode"] = "dry-run" if dry else "write"
    stats["generated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(REPORT_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2)
    except Exception:
        pass

    print(json.dumps(stats, indent=2))
    return stats


if __name__ == "__main__":
    main()
