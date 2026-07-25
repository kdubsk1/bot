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


def run(min_sessions=None, target=68):
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
        "method": "walk-forward: band fitted on oldest 60%% of sessions, "
                  "hit rate measured on newest 40%% (never seen during fitting)",
        "target_confidence": target,
        "tolerance_rule": "binomial 95% CI for the test sample size; plus a hard floor of %.0f pts" % _HARD_FLOOR_PTS,
        "min_sessions": _MIN_SESSIONS,
        "markets": {},
    }

    for mkt in MARKETS:
        try:
            frames = data_layer.get_frames(mkt)
        except Exception as e:
            report["markets"][mkt] = {"error": str(e)}
            continue
        df = frames.get("1h")
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
    print("band fitted on oldest 60%% of sessions, scored on newest 40%%")
    print("target confidence: %d%%   tolerance: binomial 95%% CI per sample size\n" % target)
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
    return run(ms, tg)


if __name__ == "__main__":
    main()
