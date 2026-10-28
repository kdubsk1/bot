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
  * _WAVE283_GRADER_FIX (GRADER-FIX, 27 Oct 2026): the safe_io bullet above was NOT true until this wave - the
    write handed safe_io the old snapshot, so a row appended between the read and the write was overwritten.
    Now the grades are merged into the fresh rows by row key. The history cache is refreshed per sweep, and a
    row is EXPIRED only once its whole horizon has traded (before that it stays open and is retried).
  * _WAVE285_GRADER_SOURCE (GRADER-SOURCE, 27 Oct 2026): an NQ / gold row Yahoo cannot serve (refused, empty, or
    no bar after the row) is graded from the bot's own frame (data_layer.get_frames - disk + TopstepX, ~500 bars)
    when that frame covers the row; else it waits (skip_no_history). One source per row, never mixed.
    FIX 1: the same coverage rule for BTC / SOL - a crypto frame that starts after the row does not grade it.

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
import time   # _WAVE283_GRADER_FIX: the history cache age
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

# _WAVE283_GRADER_FIX (GRADER-FIX, 27 Oct 2026).
# resolve() says WINDOW_OPEN (never written to a row) when nothing touched yet and the horizon is not over.
WINDOW_OPEN = "WINDOW_OPEN"
# One scan row. Unique in all 284,944 rows of the live log and the 11 archive parts (measured 27 Oct 2026).
_KEY = ("timestamp", "market", "tf", "setup_type", "direction", "decision")
# The only fields the merge copies into a fresh row (plus the fb:w173 tag on detection_reason).
_GRADE_FIELDS = ("result", "result_checked_at", "result_source")
# A cached history frame older than this is loaded again. The boot drain passes run 10 s apart (one load per
# frame, as before); the sweep runs every 3 h (a fresh load per frame it needs).
_HISTORY_MAX_AGE_S = 3600

# _WAVE285_GRADER_SOURCE (GRADER-SOURCE, 27 Oct 2026). The bot's own NQ / gold frames, used for a row only when Yahoo
# has no frame or no bar after the row (BTC / SOL are graded from the bot's frames already, by _load_crypto).
_BOT_MARKETS = ("NQ", "GC")


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
    # _WAVE283_GRADER_FIX: EXPIRED only once the whole horizon has traded - a bar AFTER the horizon must exist, so
    # the horizon's last bar is closed, not still forming
    full = len(window) > horizon
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
    if not full:
        return WINDOW_OPEN   # _WAVE283_GRADER_FIX: no touch yet, horizon not over - the row stays open
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
    load_history._loaded_at[key] = time.monotonic()   # _WAVE283_GRADER_FIX
    return df
load_history._cache = {}
load_history._loaded_at = {}   # _WAVE283_GRADER_FIX: (market, tf) -> when it was loaded


def _w283_refresh_history(stats, now_s=None):
    """_WAVE283_GRADER_FIX: drop every cached frame - and every cached refusal (None) - loaded more than
    _HISTORY_MAX_AGE_S ago, so the 3-hour sweep grades against candles that reach the sweep, not the last boot.
    A refused download is therefore retried at the next sweep; the row waits, it is never guessed."""
    now_s = time.monotonic() if now_s is None else now_s
    dropped = 0
    for key in list(load_history._cache):
        t = load_history._loaded_at.get(key)
        if t is None or now_s - t > _HISTORY_MAX_AGE_S:
            load_history._cache.pop(key, None)
            load_history._loaded_at.pop(key, None)
            dropped += 1
    stats["history_refreshed"] = stats.get("history_refreshed", 0) + dropped
    return dropped


def _w283_row_key(r):
    return tuple((r.get(k) or "").strip() for k in _KEY)


def _w283_merge_fn(graded, stale_rows):
    """_WAVE283_GRADER_FIX: the mutator for safe_io.safe_rewrite_csv. `graded` is [(row, rebuilt)] from
    grade_rows on the snapshot `stale_rows`. Runs on the FRESH rows read under the lock: copies only the grade
    fields into the fresh row with the same key, and only while that fresh row is still open. A new row, a row
    graded meanwhile, and a key seen twice (in the snapshot or the fresh rows) are left exactly as they are."""
    stale_n = {}
    for r in stale_rows:
        k = _w283_row_key(r)
        stale_n[k] = stale_n.get(k, 0) + 1
    want = {}
    for r, rebuilt in graded:
        want[_w283_row_key(r)] = (r, rebuilt)

    def merge(fresh):
        merge.applied = merge.dup = merge.final = 0
        seen = {}
        for r in fresh:
            k = _w283_row_key(r)
            if k in want:
                seen[k] = seen.get(k, 0) + 1
        for r in fresh:
            k = _w283_row_key(r)
            if k not in want:
                continue
            if seen[k] > 1 or stale_n.get(k, 0) > 1:
                merge.dup += 1
                continue
            if (r.get("result") or "").strip() not in _OPEN:
                merge.final += 1
                continue
            g, rebuilt = want[k]
            for f in _GRADE_FIELDS:
                if f in g and f in r:
                    r[f] = g[f]
            if rebuilt and "detection_reason" in r:
                base = (r.get("detection_reason") or "")
                if "fb:w173" not in base:
                    r["detection_reason"] = (base + " fb:w173").strip()
            merge.applied += 1
        merge.gone = sum(1 for k in want if k not in seen)
        return fresh
    merge.applied = merge.dup = merge.final = merge.gone = 0
    return merge


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


def _w285_bot_frame(market, tf, stats):
    """_WAVE285_GRADER_SOURCE: the bot's own frame for market+tf (High / Low, UTC index, sorted, one bar per time),
    or None. One data_layer.get_frames call fills all four timeframes; cached beside Yahoo's frames under
    (market, tf, "bot"), so _w283_refresh_history drops it with them."""
    cache = load_history._cache
    key = (market, tf, "bot")
    if key in cache:
        return cache[key]
    try:
        import data_layer
        frames = data_layer.get_frames(market) or {}
    except Exception as e:
        stats.setdefault("history_errors", []).append("%s bot frames: %s" % (market, e))
        frames = {}
    for t in _HORIZON_BARS:
        df = frames.get(t)
        try:
            if df is None or not len(df) or not {"High", "Low"}.issubset(df.columns):
                df = None
            else:
                df = df[["High", "Low"]].copy()
                if df.index.tz is None:
                    df.index = df.index.tz_localize("UTC")
                else:
                    df.index = df.index.tz_convert("UTC")
                df = df[~df.index.duplicated(keep="last")].sort_index()
        except Exception as e:
            stats.setdefault("history_errors", []).append("%s %s bot frame: %s" % (market, t, e))
            df = None
        cache[(market, t, "bot")] = df
        load_history._loaded_at[(market, t, "bot")] = time.monotonic()
    return cache.get(key)


def _w285_bot_candles(market, tf, ts, stats):
    """_WAVE285_GRADER_SOURCE: the bot's frame for ONE row, or None when it cannot grade it. It must cover the row
    (first bar at or before ts) - else resolve() would grade an old row from bars days later. Its last
    bar is dropped ALWAYS, not only by the clock: data_layer serves cached frames (1h up to 15 min, 4h up to 60, a
    kept frame all weekend, a stale one after a failed fetch), so a last bar closed by the clock can still be a
    snapshot taken mid-bar - a target touch whose stop touch came later. Cost: one bar of delay, never a wrong grade."""
    df = _w285_bot_frame(market, tf, stats)
    if df is None or not len(df):
        return None
    if df.index[0] > ts:
        stats["bot_frame_starts_after_row"] = stats.get("bot_frame_starts_after_row", 0) + 1
        return None
    df = df.iloc[:-1]
    return df if len(df) else None


# ----------------------------------------------------------------------------
def grade_rows(rows, stats, limit=None, now=None, graded_out=None):
    """Mutate rows in place. Returns number graded. _WAVE283_GRADER_FIX: each graded row is also appended to
    graded_out as (row, rebuilt) when a list is given."""
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
        horizon = _HORIZON_BARS.get(tf, _DEFAULT_HORIZON)
        # _WAVE285_GRADER_SOURCE: Yahoo first, as before; the bot's frame only when Yahoo has no frame or no bar
        # after the row. One source per row. BTC / SOL come from the bot's frames already (_load_crypto).
        source = "yahoo" if market in _BOT_MARKETS else "bot"
        have = candles is not None and len(candles) > 0
        if have and market not in _BOT_MARKETS:
            # _WAVE285_GRADER_SOURCE FIX 1 - one rule for every market: a frame grades a row only if it covers
            # it (first bar at or before the row), else an old BTC / SOL row is graded from bars days later.
            try:
                starts_after = bool(candles.index[0] > ts)
            except Exception:
                starts_after = False   # no usable index: today's behaviour for this row
            if starts_after:
                stats["bot_frame_starts_after_row"] = stats.get("bot_frame_starts_after_row", 0) + 1
                stats["skip_no_history"] += 1
                continue
        verdict = resolve(candles, ts, entry, stop, target, direction, horizon) if have else None
        if verdict is None and market in _BOT_MARKETS:
            bot = _w285_bot_candles(market, tf, ts, stats)
            if bot is not None:
                have, source = True, "bot"
                verdict = resolve(bot, ts, entry, stop, target, direction, horizon)
        if not have:
            stats["skip_no_history"] += 1
            continue
        if verdict is None:
            stats["skip_no_window"] += 1
            continue
        if verdict == WINDOW_OPEN:   # _WAVE283_GRADER_FIX: retried at the next sweep
            stats["skip_window_open"] = stats.get("skip_window_open", 0) + 1
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
        stats["history_source_" + source] = stats.get("history_source_" + source, 0) + 1   # _WAVE285_GRADER_SOURCE
        if source == "bot" and market in _BOT_MARKETS:
            stats["history_source_bot_futures"] = stats.get("history_source_bot_futures", 0) + 1
        graded += 1
        if graded_out is not None:   # _WAVE283_GRADER_FIX
            graded_out.append((row, rebuilt))
    return graded


def process_file(path, stats, dry_run=True, limit=None):
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        rows = list(reader)
    stats["rows_scanned"] += len(rows)
    graded = []   # _WAVE283_GRADER_FIX: [(row, rebuilt)] for the merge
    n = grade_rows(rows, stats, limit=limit, graded_out=graded)
    if n and not dry_run:
        bak = path + ".pre_w173.bak"
        if not os.path.exists(bak):
            shutil.copy2(path, bak)
        wrote = False
        merge = _w283_merge_fn(graded, rows)   # _WAVE283_GRADER_FIX: grades into the FRESH rows, by row key
        try:
            import safe_io
            if hasattr(safe_io, "safe_rewrite_csv"):
                safe_io.safe_rewrite_csv(path, cols, merge)
                wrote = True
        except Exception as e:
            _log.warning("grade_backfill: safe_io path failed (%s), using atomic fallback", e)
        if not wrote:
            with open(path, newline="", encoding="utf-8", errors="replace") as f:   # _WAVE283_GRADER_FIX: fresh
                fresh = merge(list(csv.DictReader(f)))
            tmp = path + ".tmp"
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                for r in fresh:
                    w.writerow({k: r.get(k, "") for k in cols})
            os.replace(tmp, path)
        for _k, _v in (("merge_applied", merge.applied), ("merge_dup_key", merge.dup),
                       ("merge_already_final", merge.final), ("merge_row_gone", merge.gone)):
            stats[_k] = stats.get(_k, 0) + _v
        n = merge.applied   # the grades actually written (the boot drain stops at 0)
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
                            "skip_no_target", "skip_no_history", "skip_no_window",
                            "history_source_yahoo", "history_source_bot",   # _WAVE285_GRADER_SOURCE
                            "history_source_bot_futures", "bot_frame_starts_after_row")}

    _w283_refresh_history(stats)   # _WAVE283_GRADER_FIX: the sweep grades against candles that reach today
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
