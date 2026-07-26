"""
session_projection.py

Wave 178: SESSION PROJECTION VALIDATOR  (read-only - measures, never trades)

PURPOSE
=======
Before the bot ever posts "NQ expected 29,670 - 30,020 by 4pm" in a public
channel, that claim has to be earned. This tool measures how far each market
ACTUALLY travels during each session, builds a projection band from OLD data,
and then tests that band against NEWER data it has never seen.

WHY WALK-FORWARD MATTERS
========================
If you build a 68th-percentile band from a dataset and then report that it
contained 68% of that same dataset, you have measured nothing - the answer is
68% by construction. That is exactly the trap that made VWAP_REJECT_BEAR look
like a 65% winner on a 4-day sample when its true rate was 27.9%.

So: the band is fitted on the OLDER 60% of sessions and scored on the NEWER 40%
it never saw. The reported hit rate is out-of-sample and honest. If the band
claims 68% and delivers 45% on unseen data, this tool says so and marks the
market/session NOT SAFE TO PUBLISH.

WHAT IT MEASURES
================
Per market (NQ, GC, BTC, SOL) and per session:

    ASIA     18:00 -> 03:00 ET   (9h)
    LONDON   03:00 -> 09:30 ET   (6.5h)
    US       09:30 -> 16:00 ET   (6.5h)

  * median absolute move during the session
  * band widths at the 68 / 80 / 90th percentiles (fitted on TRAIN only)
  * out-of-sample hit rate for each band (measured on TEST only)
  * a verdict per market+session: PUBLISH or HOLD

DATA SOURCE
===========
The bot's own frames via data_layer.get_frames(market) - 501 hourly bars and
504 daily bars per market. Hourly bars are what session boundaries need. This
must run on Railway, where the market feeds are reachable.

OUTPUT
======
data/session_projection_report.json  - full numbers, per market and session
Console summary with the verdicts.

Nothing is written to any trading file. This tool cannot affect a live trade.

USAGE
=====
    python session_projection.py
    python session_projection.py --min-sessions 25 --target 68
    python session_projection.py --deep 5000     # ask TopstepX for far more history
"""

import os
import sys
import json
import math
import logging
from datetime import datetime, timezone, timedelta

_log = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(_BASE_DIR, "data")
REPORT    = os.path.join(DATA_DIR, "session_projection_report.json")

MARKETS = ["NQ", "GC", "BTC", "SOL"]

# (name, start hour ET, duration hours)
SESSIONS = [
    ("ASIA",   18, 9.0),
    ("LONDON",  3, 6.5),
    ("US",      9, 6.5),      # 09:00 ET bar is the closest hourly bar to the 09:30 open
]

# A band is only worth publishing if its out-of-sample hit rate is consistent
# with what it claims. The tolerance CANNOT be a fixed number: with only ~48 test
# sessions the sampling error alone is +/-13 points at 95% confidence, so a fixed
# +/-8 would reject perfectly good bands as noise (measured: a genuinely stable
# market scored 52.1% on one seed and 83.3% on another, purely by chance).
# So the tolerance is the binomial 95% confidence interval for the sample size,
# and a hard floor is applied on top so a band can never be published while
# badly underdelivering even if noise could technically explain it.
_MIN_SESSIONS   = 20      # below this the sample is too thin to judge at all
_HARD_FLOOR_PTS = 15.0    # never publish if actual is this far below claimed


def _tolerance_pts(claimed_q, n_test):
    """Binomial 95% CI half-width, in percentage points."""
    if n_test <= 0:
        return 100.0
    se = math.sqrt(max(1e-9, claimed_q * (1.0 - claimed_q) / n_test)) * 100.0
    return 1.96 * se


def _pct(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, int(len(sorted_vals) * q)))
    return float(sorted_vals[i])


def _to_et(ts):
    """UTC timestamp -> naive ET hour (ET = UTC-4 during EDT)."""
    return (ts - timedelta(hours=4))


def collect_sessions(df, start_hour_et, duration_h):
    """
    From an hourly OHLC frame, collect |close_end - close_start| for every day
    where both endpoints exist. Returns a list of (date, start_price, move).
    """
    if df is None or len(df) == 0:
        return []
    try:
        closes = {}
        for ts, row in df.iterrows():
            t = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            else:
                t = t.astimezone(timezone.utc)
            closes[t.replace(minute=0, second=0, microsecond=0)] = float(row["Close"])
    except Exception:
        return []

    out = []
    for t, c0 in closes.items():
        et = _to_et(t)
        if et.hour != start_hour_et:
            continue
        t_end = t + timedelta(hours=duration_h)
        # snap to the hour grid
        t_end = t_end.replace(minute=0, second=0, microsecond=0)
        c1 = closes.get(t_end)
        if c1 is None:
            continue
        mv = abs(c1 - c0)
        if mv <= 0:
            continue          # stale/repeat bar - excluded, same as the log analysis
        out.append((t, c0, mv))
    out.sort()
    return out


def evaluate(rows, target_q):
    """
    Walk-forward: fit the band on the older 60%, score it on the newer 40%.
    Returns a dict of measured numbers, or None if the sample is too thin.
    """
    n = len(rows)
    if n < _MIN_SESSIONS:
        return None
    split = int(n * 0.6)
    train = sorted(m for _t, _p, m in rows[:split])
    test  = [m for _t, _p, m in rows[split:]]
    if not train or not test:
        return None

    res = {"n_total": n, "n_train": len(train), "n_test": len(test),
           "median_move": round(_pct(train, 0.50), 4), "bands": {}}
    for q in (0.68, 0.80, 0.90):
        band = _pct(train, q)
        hit  = sum(1 for m in test if m <= band)
        rate = 100.0 * hit / len(test)
        res["bands"]["%d" % int(q * 100)] = {
            "band": round(band, 4),
            "claimed_pct": int(q * 100),
            "actual_pct": round(rate, 1),
            "delta": round(rate - q * 100, 1),
        }
    b = res["bands"]["%d" % int(target_q * 100)]
    tol = _tolerance_pts(target_q, len(test))
    within_noise = abs(b["delta"]) <= tol
    above_floor  = b["delta"] >= -_HARD_FLOOR_PTS
    res["tolerance_pts"] = round(tol, 1)
    res["verdict"] = "PUBLISH" if (within_noise and above_floor) else "HOLD"
    if not above_floor:
        why = "underdelivers by more than the %.0f pt hard floor" % _HARD_FLOOR_PTS
    elif not within_noise:
        why = "outside the 95%% CI (+/-%.1f pts) for n_test=%d" % (tol, len(test))
    else:
        why = "consistent with its claim within sampling error"
    res["verdict_reason"] = (
        "out-of-sample %.1f%% vs claimed %d%% (delta %+.1f pts): %s"
        % (b["actual_pct"], b["claimed_pct"], b["delta"], why)
    )
    return res


def deep_history(data_layer, market, tf, want_bars):
    """
    Ask TopstepX for MORE bars than the live scanner does.

    _TOPSTEPX_BAR_COUNT is what WE request, not a broker cap: the live scanner
    asks for 500 hourly bars and receives exactly 500. Daily already asks for 730
    and receives only ~31, which is a genuine data limit - so the two cases are
    distinguishable, and hourly has simply never been asked for more.

    500 hourly bars is ~21 days, which yields ~15 weekday sessions per session
    type - below the 20 needed to judge a band honestly. If TopstepX will serve
    5,000 hourly bars that is ~208 days and roughly 150 sessions per type, which
    turns "wait two months for data" into "validate this week".

    This raises the request, calls the RAW fetch (bypassing the cache), and
    always restores the original value in a finally block. The live scan path is
    never touched: it keeps using get_frames() with the normal 500.

    Returns (dataframe_or_None, bars_received, note).
    """
    try:
        counts = getattr(data_layer, "_TOPSTEPX_BAR_COUNT", None)
        fetch = getattr(data_layer, "_fetch_topstepx", None)
        if counts is None or fetch is None:
            return None, 0, "data_layer has no TopstepX deep-fetch path"
        original = counts.get(tf)
        try:
            counts[tf] = int(want_bars)
            df = fetch(market, tf)
        finally:
            if original is None:
                counts.pop(tf, None)
            else:
                counts[tf] = original          # always restored
        n = 0 if df is None else len(df)
        if n == 0:
            return None, 0, "deep fetch returned nothing"
        return df, n, "requested %d, received %d" % (want_bars, n)
    except Exception as e:
        return None, 0, "deep fetch failed: %s" % e


def run(min_sessions=None, target=68, deep=0):
    global _MIN_SESSIONS
    if min_sessions:
        _MIN_SESSIONS = int(min_sessions)
    target_q = target / 100.0

    try:
        import data_layer
    except Exception as e:
        print("ERROR: data_layer unavailable (%s). This must run on Railway." % e)
        return None

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "walk-forward: band fitted on oldest 60% of sessions, "
                  "hit rate measured on newest 40% (never seen during fitting)",
        "target_confidence": target,
        "tolerance_rule": ("binomial 95%% CI for the test sample size, plus a hard "
                           "floor of %.0f pts" % _HARD_FLOOR_PTS),
        "min_sessions": _MIN_SESSIONS,
        "markets": {},
    }

    report["deep_requested"] = int(deep or 0)
    report["bar_supply"] = {}

    for mkt in MARKETS:
        df = None
        supply = {}
        # Deep history first (NQ/GC only - crypto comes from ccxt, which caps low).
        if deep and mkt in ("NQ", "GC"):
            ddf, dn, dnote = deep_history(data_layer, mkt, "1h", deep)
            supply["deep"] = dnote
            if ddf is not None:
                df = ddf
                supply["bars_used"] = dn
                supply["source"] = "deep TopstepX"
        if df is None:
            try:
                frames = data_layer.get_frames(mkt)
            except Exception as e:
                report["markets"][mkt] = {"error": str(e)}
                report["bar_supply"][mkt] = supply
                continue
            df = frames.get("1h")
            supply["bars_used"] = 0 if df is None else len(df)
            supply["source"] = "normal get_frames (500)"
        report["bar_supply"][mkt] = supply
        m_out = {}
        for name, h0, dur in SESSIONS:
            rows = collect_sessions(df, h0, dur)
            ev = evaluate(rows, target_q)
            if ev is None:
                m_out[name] = {"n_total": len(rows), "verdict": "INSUFFICIENT_DATA"}
            else:
                # express the band as a percentage of price too
                try:
                    px = rows[-1][1]
                    for k, b in ev["bands"].items():
                        b["band_pct_of_price"] = round(100.0 * b["band"] / px, 3)
                    ev["reference_price"] = round(px, 4)
                except Exception:
                    pass
                m_out[name] = ev
        report["markets"][mkt] = m_out

    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(REPORT, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
    except Exception as e:
        print("WARNING: could not write report: %s" % e)

    # ---- console summary ----
    print("=" * 74)
    print("SESSION PROJECTION VALIDATION  (walk-forward, out-of-sample)")
    print("=" * 74)
    print("band fitted on oldest 60% of sessions, scored on newest 40%")
    print("target confidence: %d%%   tolerance: binomial 95%% CI per sample size\n" % target)
    if report.get("deep_requested"):
        print("  DEEP HISTORY PROBE (requested %d hourly bars):" % report["deep_requested"])
        for mkt, sup in report.get("bar_supply", {}).items():
            print("     %-5s %-22s %s" % (mkt, sup.get("source", "?"), sup.get("deep", "")))
        print()
    publishable = 0
    for mkt, sess in report["markets"].items():
        if "error" in sess:
            print("  %-5s ERROR: %s" % (mkt, sess["error"]))
            continue
        print("  %s" % mkt)
        for name, ev in sess.items():
            if ev.get("verdict") == "INSUFFICIENT_DATA":
                print("     %-7s n=%-3d  INSUFFICIENT DATA" % (name, ev.get("n_total", 0)))
                continue
            b = ev["bands"][str(target)]
            flag = "PUBLISH" if ev["verdict"] == "PUBLISH" else "HOLD   "
            if ev["verdict"] == "PUBLISH":
                publishable += 1
            print("     %-7s n=%-3d  band +/-%9.2f  claims %d%%  actual %5.1f%%  (%+.1f)  %s"
                  % (name, ev["n_total"], b["band"], b["claimed_pct"],
                     b["actual_pct"], b["delta"], flag))
        print()
    print("  %d market/session combinations are safe to publish." % publishable)
    print("  Report written: %s" % REPORT)
    print("=" * 74)
    return report


def main(argv=None):
    argv = argv or sys.argv[1:]
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    ms = None
    tg = 68
    if "--min-sessions" in argv:
        try:
            ms = int(argv[argv.index("--min-sessions") + 1])
        except Exception:
            pass
    if "--target" in argv:
        try:
            tg = int(argv[argv.index("--target") + 1])
        except Exception:
            pass
    dp = 0
    if "--deep" in argv:
        try:
            dp = int(argv[argv.index("--deep") + 1])
        except Exception:
            dp = 5000
    return run(ms, tg, dp)


if __name__ == "__main__":
    main()
