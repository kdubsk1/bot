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
    python session_projection.py --sweep --deep 5000   # find the tightest window that holds
    python session_projection.py --extremes --deep 5000  # project the HIGH and LOW instead
    python session_projection.py --quality  --deep 5000  # which horizon is most predictable
"""

import os
import sys
import json
import math
import logging
import time
import threading
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
    res["tolerance_pts"] = round(tol, 1)

    # Wave 187: only UNDER-delivery is a failure.
    #
    # The first version tested abs(delta) <= tolerance, which punished a band for
    # being too GOOD: Gold at 1h measured 77.2% and 2h measured 83.5% against a
    # claimed 68%, and both were marked HOLD - rejecting the two tightest, safest
    # bands on the board. That was wrong. If the band claims 68% and delivers
    # 83%, the claim is still TRUE; the band is merely wider than it needs to be,
    # which is the conservative direction.
    #
    # So the fix is also the more honest presentation: stop claiming a round
    # number and publish the MEASURED rate. `publish_pct` below is what should
    # appear in the channel - never the target.
    under_by     = -b["delta"]                       # positive means it fell short
    within_noise = under_by <= tol                   # one-sided now
    above_floor  = b["delta"] >= -_HARD_FLOOR_PTS
    res["verdict"] = "PUBLISH" if (within_noise and above_floor) else "HOLD"
    res["publish_pct"] = b["actual_pct"]             # say what was measured
    if not above_floor:
        why = "underdelivers by more than the %.0f pt hard floor" % _HARD_FLOOR_PTS
    elif not within_noise:
        why = "falls short by %.1f pts, beyond the 95%% CI of +/-%.1f for n_test=%d" % (
            under_by, tol, len(test))
    elif b["delta"] > tol:
        why = "beats its target - band is conservative, publish the measured rate"
    else:
        why = "consistent with its claim within sampling error"
    res["verdict_reason"] = (
        "out-of-sample %.1f%% vs target %d%% (delta %+.1f pts): %s"
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


# ===================================================================
# Wave 186: HORIZON SWEEP - find the tightest window that still holds
# ===================================================================
# The session view answered "can we project to the 4pm close?" and for NQ the
# answer was no: its session bands came out +/-208 to +/-428 points, which is
# not a prediction, it is a truism. Two problems caused that:
#
#   1. a full session is a long time for an index future to wander
#   2. anchoring only on session starts gave n=33-35 samples, so the
#      confidence interval was +/-16 points and almost nothing could pass
#
# This sweeps SHORTER horizons and uses NON-OVERLAPPING windows anchored on
# every Nth bar rather than only on session opens. For NQ that lifts the sample
# from ~35 to ~400 at a 2h horizon, which tightens the CI enough to give a real
# verdict. Overlapping windows would inflate n with correlated samples and make
# the CI dishonestly narrow, so they are deliberately not used.
#
# It reports band width in POINTS and as a PERCENT OF PRICE, so horizons can be
# compared on tightness rather than on raw point counts.

_SWEEP_HORIZONS = [1, 2, 3, 4, 6, 8]


def collect_horizon(df, hours):
    """
    Non-overlapping |close(t+hours) - close(t)| samples.
    Anchoring on every Nth bar keeps the samples independent.
    """
    if df is None or len(df) == 0:
        return []
    try:
        rows = []
        for ts, row in df.iterrows():
            t = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            else:
                t = t.astimezone(timezone.utc)
            rows.append((t.replace(minute=0, second=0, microsecond=0), float(row["Close"])))
    except Exception:
        return []
    rows.sort()
    closes = dict(rows)
    ordered = [t for t, _c in rows]
    out = []
    step = max(1, int(hours))
    for i in range(0, len(ordered) - step, step):
        t0 = ordered[i]
        t1 = t0 + timedelta(hours=hours)
        c0 = closes.get(t0)
        c1 = closes.get(t1)
        if c0 is None or c1 is None:
            continue
        mv = abs(c1 - c0)
        if mv <= 0:
            continue
        out.append((t0, c0, mv))
    return out


def sweep_horizons(data_layer, markets, target_q, deep=0):
    """Test every horizon on every market and return the results table."""
    results = []
    for mkt in markets:
        df = None
        if deep and mkt in ("NQ", "GC"):
            ddf, dn, _note = deep_history(data_layer, mkt, "1h", deep)
            if ddf is not None:
                df = ddf
        if df is None:
            try:
                df = data_layer.get_frames(mkt).get("1h")
            except Exception:
                df = None
        if df is None or len(df) == 0:
            continue
        try:
            ref_price = float(df["Close"].iloc[-1])
        except Exception:
            ref_price = 0.0
        for h in _SWEEP_HORIZONS:
            rows = collect_horizon(df, h)
            ev = evaluate(rows, target_q)
            if ev is None:
                results.append({"market": mkt, "hours": h, "n": len(rows),
                                "verdict": "INSUFFICIENT_DATA"})
                continue
            b = ev["bands"]["%d" % int(target_q * 100)]
            results.append({
                "market": mkt, "hours": h, "n": ev["n_total"],
                "band": b["band"],
                "band_pct": round(100.0 * b["band"] / ref_price, 3) if ref_price else None,
                "claimed": b["claimed_pct"], "actual": b["actual_pct"],
                "delta": b["delta"], "tolerance": ev.get("tolerance_pts"),
                "verdict": ev["verdict"], "ref_price": round(ref_price, 2),
            })
    return results


def print_sweep(results, target):
    print("=" * 78)
    print("HORIZON SWEEP - which prediction window is tight AND holds up")
    print("=" * 78)
    print("non-overlapping windows, walk-forward: fitted on oldest 60%, scored on newest 40%")
    print("target %d%%\n" % target)
    print("  %-5s %5s %6s %11s %8s %8s %8s  %s"
          % ("mkt", "hours", "n", "band", "band%", "claims", "actual", "verdict"))
    best = {}
    for r in results:
        if r["verdict"] == "INSUFFICIENT_DATA":
            print("  %-5s %5d %6d   INSUFFICIENT DATA" % (r["market"], r["hours"], r["n"]))
            continue
        star = ""
        if r["verdict"] == "PUBLISH":
            cur = best.get(r["market"])
            if cur is None or r["band_pct"] < cur["band_pct"]:
                best[r["market"]] = r
        print("  %-5s %5d %6d %11.2f %7.3f%% %7d%% %7.1f%%  %s%s"
              % (r["market"], r["hours"], r["n"], r["band"], r["band_pct"],
                 r["claimed"], r["actual"], r["verdict"], star))
    print()
    if best:
        print("  TIGHTEST PUBLISHABLE WINDOW PER MARKET:")
        for mkt, r in sorted(best.items()):
            lo = r["ref_price"] - r["band"]
            hi = r["ref_price"] + r["band"]
            print("     %-5s %dh  +/-%.2f (%.3f%%)  measured %.1f%% over n=%d"
                  % (mkt, r["hours"], r["band"], r["band_pct"], r["actual"], r["n"]))
            print("           from %.2f that is  %.2f - %.2f" % (r["ref_price"], lo, hi))
    else:
        print("  Nothing passed at any horizon.")
    print("=" * 78)
    return best


# ===================================================================
# Wave 188: EXTREME PROJECTION - the day's HIGH and LOW, not the close
# ===================================================================
# Wayne's insight, and it is the better question. A close-price band asks
# "where will price BE at time T", which is the hardest thing in markets to
# predict and the thing intraday manipulation destroys. How far price EXTENDS
# is far more tractable: range is bounded and mean-reverting where close
# position is closer to a random walk, it is what ATR-based projection is
# actually built on, and "expect the low near X" is a tradeable LEVEL rather
# than a guess about direction.
#
# For each non-overlapping window we measure two separate quantities from the
# window's OPEN price:
#
#     up_ext = max(High in window) - open      how far it ran UP
#     dn_ext = open - min(Low in window)       how far it ran DOWN
#
# Each side is validated independently walk-forward, because they behave
# differently - a market can have a reliable floor and a wild ceiling.
#
# The published claim is one-sided and honest:
#     "high stays below open + X"   P% of the time
#     "low stays above  open - Y"   P% of the time
# which is exactly the shape a trader can act on.

_TF_MINUTES = {"15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def collect_extremes(df, hours, bar_minutes=60):
    """
    Non-overlapping windows. Returns (up_rows, dn_rows) where each row is
    (timestamp, open_price, extension). Uses High/Low - NOT Close.

    Wave 191: `bar_minutes` is the length of ONE bar in the frame. It used to
    be hard-coded to 60 in two ways, both of which broke on any other frame:

      1. the window step was `int(hours)` bars, correct only for hourly bars;
      2. every timestamp was truncated with `.replace(minute=0)`, which on a
         15-minute frame collapses four distinct bars onto the same key. The
         sort then broke ties on the High price, silently REORDERING bars
         within each hour and scrambling every window built from them.

    The second one is the dangerous kind: no error, no warning, just quietly
    wrong numbers. Timestamps are now left intact.
    """
    if df is None or len(df) == 0:
        return [], []
    cols = set(getattr(df, "columns", []))
    if not {"High", "Low"}.issubset(cols):
        return [], []
    try:
        recs = []
        for ts, row in df.iterrows():
            t = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            else:
                t = t.astimezone(timezone.utc)
            recs.append((t.replace(second=0, microsecond=0),
                         float(row["High"]), float(row["Low"]),
                         float(row["Close"])))
    except Exception:
        return [], []
    recs.sort()
    step = max(1, int(round(float(hours) * 60.0 / float(bar_minutes or 60))))
    up_rows, dn_rows = [], []
    for i in range(0, len(recs) - step, step):
        window = recs[i:i + step + 1]
        if len(window) < 2:
            continue
        open_px = window[0][3]          # close of the anchor bar = the open of the window
        hi = max(w[1] for w in window[1:])
        lo = min(w[2] for w in window[1:])
        # Wave 191: clamp at zero, never DISCARD.
        #
        # This used to drop any window where price failed to trade above the
        # open (up <= 0), and likewise below it. That was wrong twice over.
        #
        # It threw away data - and those windows are not noise, they are real
        # observations in which the up-band held trivially. Excluding them
        # measures only the windows that already ran in that direction, which
        # biases the sample toward the violent ones and inflates every band.
        #
        # A window where price never exceeded its open has an upward extension
        # of ZERO, not a missing value. Measured on a daily crypto frame, the
        # old filter silently discarded 29% of all windows.
        up = max(0.0, hi - open_px)
        dn = max(0.0, open_px - lo)
        up_rows.append((window[0][0], open_px, up))
        dn_rows.append((window[0][0], open_px, dn))
    return up_rows, dn_rows


def evaluate_joint(paired, target_q):
    """
    Wave 189: how often did BOTH sides hold at the same time?

    Publishing "high 73% / low 70%" invites the reader to assume the RANGE holds
    about 70% of the time. It does not: two one-sided probabilities do not
    combine that way, and the joint rate is always lower than either. Rather
    than derive it (which would require assuming independence - false here,
    since a violent hour breaks both sides at once) this MEASURES it.

    Bands are fitted on the oldest 60% exactly as elsewhere, then the newest 40%
    is scored on whether BOTH the up-band and the down-band held in the same
    window. That single number is what belongs next to a published range.
    """
    n = len(paired)
    if n < _MIN_SESSIONS:
        return None
    split = int(n * 0.6)
    train, test = paired[:split], paired[split:]
    if not train or not test:
        return None
    up_band = _pct(sorted(r[2] for r in train), target_q)
    dn_band = _pct(sorted(r[3] for r in train), target_q)
    both = sum(1 for r in test if r[2] <= up_band and r[3] <= dn_band)
    rate = 100.0 * both / len(test)
    tol = _tolerance_pts(target_q, len(test))
    return {
        "n_total": n, "n_test": len(test),
        "up_band": round(up_band, 4), "dn_band": round(dn_band, 4),
        "joint_pct": round(rate, 1),
        "tolerance_pts": round(tol, 1),
    }


def collect_paired(df, hours, bar_minutes=60):
    """Windows where BOTH extensions exist, kept aligned for the joint test."""
    up, dn = collect_extremes(df, hours, bar_minutes)
    dn_map = {r[0]: r[2] for r in dn}
    out = []
    for t, px, u in up:
        d = dn_map.get(t)
        if d is not None:
            out.append((t, px, u, d))
    return out


_LADDER_QS   = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]
_MIN_PUBLISH_RATE = 65.0   # a range right less than this often is not worth printing
_MIN_TEST_WINDOWS = 30     # below this the measured rate is a coin flip dressed up


_PUBLISH_TARGET = 0.80    # the promise made to the channel


def evaluate_target(paired, target=_PUBLISH_TARGET):
    """
    Wave 192: fit the width that delivers `target` accuracy - honestly.

    THE MISTAKE THIS AVOIDS
    -----------------------
    The obvious implementation is to try widths until the TEST set scores 80%.
    That is cheating, and subtly enough that it looks rigorous: the test set
    would have been used to CHOOSE the width, so the 80% it then reports is
    fitted, not measured. It is the same error as fitting a 68th-percentile band
    and reporting that it contained 68% of the same data.

    So the width is chosen on TRAIN only - the narrowest band that contains
    `target` of the training windows on BOTH sides at once - and the test set is
    then scored on that fixed width, once. The reported rate is whatever the
    unseen data gives: sometimes above target, sometimes below. That number is
    real.

    WHY THIS REPLACED THE KNEE
    --------------------------
    The knee of the width-versus-accuracy curve is well defined only when the
    curve has a sharp bend. On live data it does not: only four rungs cleared
    the floor and the curve was smooth, so "furthest from the chord" drifted to
    the wide end and chose +/-154 points for a ONE HOUR NQ range - 1.09% of
    price, and untradeable. Measured against a fixed 80% target the knee ran
    14% to 37% wide on every market and horizon tested.

    A fixed target also makes a promise that can be stated in the channel and
    checked: "80% of the time", every day, same meaning.
    """
    n = len(paired)
    if n < _MIN_SESSIONS:
        return None
    split = int(n * 0.6)
    train, test = paired[:split], paired[split:]
    if not train or not test:
        return None

    def joint_rate(rows, up_b, dn_b):
        if not rows:
            return 0.0
        hit = sum(1 for r in rows if r[2] <= up_b and r[3] <= dn_b)
        return 100.0 * hit / len(rows)

    ups = sorted(r[2] for r in train)
    dns = sorted(r[3] for r in train)

    # Walk a fine grid of quantiles and take the FIRST that reaches the target
    # on training data - the narrowest width that keeps the promise in-sample.
    chosen_q = None
    for i in range(40, 200):
        q = i / 200.0                      # 0.200 .. 0.995
        if joint_rate(train, _pct(ups, q), _pct(dns, q)) >= target * 100.0:
            chosen_q = q
            break
    if chosen_q is None:
        return None

    up_b, dn_b = _pct(ups, chosen_q), _pct(dns, chosen_q)
    if up_b + dn_b <= 0:
        return None
    measured = joint_rate(test, up_b, dn_b)          # scored ONCE, on unseen data
    tol = _tolerance_pts(target, len(test))
    shortfall = target * 100.0 - measured
    return {
        "n_total": n, "n_train": len(train), "n_test": len(test),
        "target_pct": round(target * 100.0, 1),
        "chosen_q": round(chosen_q, 3),
        "train_pct": round(joint_rate(train, up_b, dn_b), 1),
        "up_band": round(up_b, 4), "dn_band": round(dn_b, 4),
        "width": round(up_b + dn_b, 4),
        "measured": round(measured, 1),
        "tolerance_pts": round(tol, 1),
        # Only UNDER-delivery beyond sampling noise is a failure. Over-delivery
        # means the band is merely conservative (Wave 187).
        "verdict": "PUBLISH" if shortfall <= tol else "HOLD",
    }


def evaluate_ladder(paired, price):
    """
    Wave 189: find the BEST band width instead of assuming one.

    Wayne asked for "whatever point range has the highest win rate". Taken
    literally that has a degenerate answer: a band 10,000 points wide is right
    100% of the time and says nothing. Width and hit rate are not independent -
    every extra point of width BUYS hit rate, so "highest" alone is not a target.

    What IS a real target is the KNEE of that curve: the width past which extra
    points stop buying meaningful accuracy. Below the knee you are paying width
    for very little; above it you are giving away tightness for very little.

    So this sweeps the whole ladder of percentiles, measures each one
    out-of-sample (fit on oldest 60%, scored on newest 40%), and finds the knee
    geometrically - the point furthest from the straight line joining the
    narrowest and widest candidates. That is the standard knee construction and
    it has no tunable magic number in it.

    Candidates below _MIN_PUBLISH_RATE are dropped first, because the knee of a
    curve that never gets accurate is not worth having.
    """
    cands = []
    for q in _LADDER_QS:
        jt = evaluate_joint(paired, q)
        if not jt or jt["n_test"] < _MIN_TEST_WINDOWS:
            continue
        width = jt["up_band"] + jt["dn_band"]
        if width <= 0:
            continue
        cands.append({"q": q, "up": jt["up_band"], "dn": jt["dn_band"],
                      "width": width, "rate": jt["joint_pct"],
                      "n_test": jt["n_test"], "n_total": jt["n_total"],
                      "width_pct": round(100.0 * width / price, 3) if price else None})
    if not cands:
        return None

    ok = [c for c in cands if c["rate"] >= _MIN_PUBLISH_RATE]
    if not ok:
        # Nothing on the ladder is accurate enough to publish at any width.
        return {"chosen": None, "ladder": cands,
                "reason": "no width reached %.0f%% out-of-sample" % _MIN_PUBLISH_RATE}

    ok.sort(key=lambda c: c["width"])
    if len(ok) <= 2:
        chosen = ok[0]                      # narrowest that clears the floor
        why = "narrowest width that stayed above %.0f%%" % _MIN_PUBLISH_RATE
    else:
        x0, y0 = ok[0]["width"], ok[0]["rate"]
        x1, y1 = ok[-1]["width"], ok[-1]["rate"]
        dx, dy = (x1 - x0), (y1 - y0)
        norm = math.sqrt(dx * dx + dy * dy) or 1.0
        best, best_d = ok[0], -1.0
        for c in ok:
            # perpendicular distance from the narrowest->widest chord
            d = abs(dy * (c["width"] - x0) - dx * (c["rate"] - y0)) / norm
            if d > best_d:
                best_d, best = d, c
        chosen = best
        why = "knee of the width-vs-accuracy curve"

    chosen = dict(chosen)
    chosen["why"] = why
    return {"chosen": chosen, "ladder": cands, "reason": why}


def sweep_extremes(data_layer, markets, target_q, deep=0, horizons=None):
    """Validate the UP and DOWN extension separately, per market per horizon."""
    horizons = horizons or _SWEEP_HORIZONS
    out = []
    for mkt in markets:
        df = None
        if deep and mkt in ("NQ", "GC"):
            ddf, _n, _note = deep_history(data_layer, mkt, "1h", deep)
            if ddf is not None:
                df = ddf
        if df is None:
            try:
                df = data_layer.get_frames(mkt).get("1h")
            except Exception:
                df = None
        if df is None or len(df) == 0:
            continue
        try:
            px = float(df["Close"].iloc[-1])
        except Exception:
            px = 0.0
        for h in horizons:
            up_rows, dn_rows = collect_extremes(df, h)
            # Wave 189: the RANGE row - one measured range, one measured rate.
            # Its width is CHOSEN by evaluate_ladder (knee of width vs accuracy),
            # not inherited from target_q, so the published range is the best one
            # available rather than whichever percentile we happened to ask for.
            paired = collect_paired(df, h)
            lad = evaluate_ladder(paired, px)
            tgt = evaluate_target(paired, _PUBLISH_TARGET)
            if tgt:
                out.append({"market": mkt, "hours": h, "side": "RANGE",
                            "n": tgt["n_total"], "n_test": tgt["n_test"],
                            "band": tgt["up_band"], "dn_band": tgt["dn_band"],
                            "width": tgt["width"],
                            "width_pct": (round(100.0 * tgt["width"] / px, 3)
                                          if px else None),
                            "chosen_q": int(tgt["chosen_q"] * 100),
                            "target_pct": tgt["target_pct"],
                            "train_pct": tgt["train_pct"],
                            "measured": tgt["measured"],
                            "why": ("narrowest width that held %.0f%% in training"
                                    % tgt["target_pct"]),
                            "verdict": tgt["verdict"],
                            "ladder": (lad or {}).get("ladder"),
                            "ref_price": round(px, 4)})
            elif lad:
                out.append({"market": mkt, "hours": h, "side": "RANGE",
                            "n": len(lad["ladder"]), "verdict": "HOLD",
                            "why": "could not reach the target at any width",
                            "ladder": lad["ladder"], "ref_price": round(px, 4)})
            for side, rows in (("HIGH", up_rows), ("LOW", dn_rows)):
                ev = evaluate(rows, target_q)
                if ev is None:
                    out.append({"market": mkt, "hours": h, "side": side,
                                "n": len(rows), "verdict": "INSUFFICIENT_DATA"})
                    continue
                b = ev["bands"]["%d" % int(target_q * 100)]
                out.append({
                    "market": mkt, "hours": h, "side": side, "n": ev["n_total"],
                    "band": b["band"],
                    "band_pct": round(100.0 * b["band"] / px, 3) if px else None,
                    "measured": ev["publish_pct"], "verdict": ev["verdict"],
                    "ref_price": round(px, 4),
                })
    return out


# Wave 191: which horizons to test, and which frame each one comes from.
# Futures get sub-hourly windows (TopstepX serves a 15m frame); crypto gets
# multi-day windows, because it trades 24/7 and the daily frame goes back ~300
# days where the hourly frame only reaches ~12.
# Wave 193: horizons are compared only WITHIN the frame they came from.
#
# The first version mixed frames in one comparison and the result was
# meaningless. The frames cover wildly different spans of market:
#
#     NQ  15m frame   500 bars =   5.2 days      NQ  1h frame  797 bars = 33.2 days
#     BTC 1h  frame   300 bars =  12.5 days      BTC 1d frame  300 bars =  300 days
#
# So "how does width grow with horizon" was actually measuring "how does a calm
# recent week compare with a volatile year", and reporting the difference as a
# market property. Every market came back TRENDING, SOL at 0.84, and BTC's
# 1-day range priced at 8.6% of price against a 1-hour comparison that had only
# seen twelve days. That was the confound, not an edge.
#
# Each frame now yields its own self-consistent group with its own exponent,
# fitted only across horizons measured over the SAME span of market. The 1-hour
# horizon deliberately appears in two groups where both frames support it: it is
# the overlap point, and any disagreement between the two is the confound
# showing itself instead of hiding.
_HORIZON_PLAN = {
    "NQ":  [(0.25, "15m"), (0.5, "15m"), (1, "15m"),
            (1, "1h"), (2, "1h"), (4, "1h"), (8, "1h")],
    "GC":  [(0.25, "15m"), (0.5, "15m"), (1, "15m"),
            (1, "1h"), (2, "1h"), (4, "1h"), (8, "1h")],
    "BTC": [(0.5, "15m"), (1, "15m"),
            (1, "1h"), (4, "1h"), (8, "1h"),
            (24, "1d"), (72, "1d"), (168, "1d")],
    "SOL": [(0.5, "15m"), (1, "15m"),
            (1, "1h"), (4, "1h"), (8, "1h"),
            (24, "1d"), (72, "1d"), (168, "1d")],
}

# The width used to COMPARE horizons. Comparing each horizon at its own knee
# would compare different accuracy levels against each other, which answers
# nothing. Everything is measured at one fixed accuracy so the only thing that
# varies is width.
_COMPARE_Q = 0.80

# What the scaling exponent MEASURES on a known random walk - not what theory
# says it should.
#
# Theory says 0.50. Run against six independent synthetic random walks this
# estimator returned 0.499, 0.509, 0.516, 0.520, 0.528 and 0.529: mean 0.517,
# spread 0.030. The bias is small but real, and it comes from finite samples
# plus intrabar High/Low noise that does not scale with time.
#
# Judging against the textbook 0.50 would therefore label a perfectly ordinary
# random walk as "trending" about half the time. The neutral band is the
# measured null plus a small margin, so only a market that genuinely departs
# from chance gets called.
_NULL_LO, _NULL_HI = 0.47, 0.56


def horizon_label(h):
    if h < 1:
        return "%d min" % int(round(h * 60))
    if h < 24:
        return "%d hour%s" % (int(h), "" if h == 1 else "s")
    d = h / 24.0
    return "%d day%s" % (int(d), "" if d == 1 else "s")


def sweep_quality(data_layer, markets=None, deep=0):
    """
    Which horizon is genuinely the most predictable?

    "Whichever has the highest win rate" cannot be answered directly, because
    ANY horizon reaches any win rate you like if you widen the range enough -
    and a wide range says nothing. Shorter horizons also always produce tighter
    ranges in absolute points, so "tightest" just picks the shortest and is
    equally uninformative.

    The comparison needs a baseline. For a pure random walk, how far price
    extends over a window grows with the SQUARE ROOT of time: a 4-hour range
    should be exactly twice a 1-hour range. That is the null hypothesis, and it
    involves no skill at all.

    So each horizon is measured at one fixed accuracy, and its width is divided
    by what sqrt-of-time scaling predicts from the shortest horizon:

        ratio < 1.0   price is CONTAINED better than chance at this horizon.
                      Something real (mean reversion, session structure) is
                      holding it in. This is a genuinely better horizon.
        ratio ~ 1.0   indistinguishable from a random walk. The range is
                      honest but carries no edge beyond volatility.
        ratio > 1.0   price runs FURTHER than chance here - trending or
                      gap-prone. Worst horizon to publish a range for.

    Validated on synthetic random-walk data, where it returns ~1.0 at every
    horizon as it must.
    """
    out = {}
    for mkt in (markets or MARKETS):
        try:
            frames = data_layer.get_frames(mkt)
        except Exception as e:
            out[mkt] = {"error": str(e)}
            continue
        deep_1h = None
        if deep and mkt in ("NQ", "GC"):
            try:
                ddf, _n, _note = deep_history(data_layer, mkt, "1h", deep)
                if ddf is not None:
                    deep_1h = ddf
            except Exception:
                pass

        rows = []
        for hours, tf in _HORIZON_PLAN.get(mkt, [(1, "1h"), (4, "1h")]):
            df = deep_1h if (tf == "1h" and deep_1h is not None) else frames.get(tf)
            if df is None or len(df) == 0:
                rows.append({"hours": hours, "tf": tf, "status": "NO FRAME"})
                continue
            bar_min = _TF_MINUTES.get(tf, 60)
            paired = collect_paired(df, hours, bar_min)
            try:
                px = float(df["Close"].iloc[-1])
            except Exception:
                px = 0.0
            fixed = evaluate_joint(paired, _COMPARE_Q)
            if not fixed or fixed["n_test"] < _MIN_TEST_WINDOWS:
                rows.append({"hours": hours, "tf": tf, "status": "TOO FEW WINDOWS",
                             "n": len(paired),
                             "n_test": (fixed or {}).get("n_test", 0)})
                continue
            lad = evaluate_ladder(paired, px)
            chosen = (lad or {}).get("chosen")
            width = fixed["up_band"] + fixed["dn_band"]
            try:
                span_days = (df.index[-1] - df.index[0]).total_seconds() / 86400.0
            except Exception:
                span_days = None
            rows.append({
                "hours": hours, "tf": tf, "status": "OK", "bars": len(df),
                "span_days": round(span_days, 1) if span_days else None,
                "n": fixed["n_total"], "n_test": fixed["n_test"],
                "up": fixed["up_band"], "dn": fixed["dn_band"],
                "width": width,
                "width_pct": (100.0 * width / px) if px else None,
                "rate_at_fixed": fixed["joint_pct"],
                "knee_width": (chosen or {}).get("width"),
                "knee_rate": (chosen or {}).get("rate"),
                "knee_q": (chosen or {}).get("q"),
                "ref_price": px,
            })

        ok_rows = [r for r in rows if r.get("status") == "OK"]
        groups = {}
        for r in ok_rows:
            groups.setdefault(r["tf"], []).append(r)
        exponents = {}
        for tf, grp in groups.items():
            grp.sort(key=lambda r: r["hours"])
            if len(grp) < 3:
                continue
            xs = [math.log(r["hours"]) for r in grp]
            ys = [math.log(r["width"]) for r in grp if r["width"] > 0]
            if len(ys) != len(xs):
                continue
            mx = sum(xs) / len(xs); my = sum(ys) / len(ys)
            den = sum((x - mx) ** 2 for x in xs)
            if den > 0:
                exponents[tf] = sum((x - mx) * (y - my)
                                    for x, y in zip(xs, ys)) / den
        # vs-chance is also per group: the reference is the shortest horizon
        # from the SAME frame, so no cross-frame comparison is ever made.
        for tf, grp in groups.items():
            base = grp[0]
            for r in grp:
                pred = base["width"] * math.sqrt(r["hours"] / base["hours"])
                r["vs_random_walk"] = round(r["width"] / pred, 3) if pred else None

        if ok_rows:
            # Rank on CONTAINMENT, not on tightness.
            #
            # The first version ranked by accuracy divided by width, which is
            # degenerate in the same way "highest win rate" was: width shrinks
            # faster than accuracy does, so that ratio always crowns the
            # shortest horizon on the board no matter what the data says. It
            # picked "15 min" for all four markets, which is arithmetic, not a
            # finding.
            #
            # vs_random_walk is the column that carries actual evidence: how
            # much tighter price is held than chance alone would hold it.
            # Lowest wins, and the winner can be any horizon.
            scored = [r for r in ok_rows if r.get("vs_random_walk")]
            best = (min(scored, key=lambda r: r["vs_random_walk"])
                    if scored else None)
        else:
            best = None
        out[mkt] = {"rows": rows, "best": best,
                    "scaling_exponents": exponents}
    return out


def print_quality(q):
    print()
    print("=" * 78)
    print("WHICH HORIZON IS ACTUALLY THE MOST PREDICTABLE?")
    print("=" * 78)
    print("Every horizon is measured at the SAME accuracy (%d%%), so the only thing"
          % int(_COMPARE_Q * 100))
    print("that differs is how wide the range has to be to get there.")
    print()
    print("Horizons are only ever compared against others from the SAME data feed,")
    print("because the feeds cover very different spans of market. Mixing them")
    print("compares a calm recent week with a volatile year and calls the")
    print("difference an edge. The 'days' column is there so you can see it.")
    print()
    print("'vs chance' divides the width by what a random walk predicts (extension")
    print("grows with the square root of time). Below 1.00 = held tighter than chance.")
    for mkt, d in q.items():
        print()
        if d.get("error"):
            print("  %-4s ERROR: %s" % (mkt, d["error"]))
            continue
        print("  %s" % mkt)
        rows = d.get("rows", [])
        exps = d.get("scaling_exponents", {}) or {}
        seen = []
        for r in rows:
            if r.get("tf") not in seen:
                seen.append(r.get("tf"))
        for tf in seen:
            grp = [r for r in rows if r.get("tf") == tf]
            spans = [r.get("span_days") for r in grp if r.get("span_days")]
            hdr = "   from the %s feed" % tf
            if spans:
                hdr += "  (%.0f days of market)" % max(spans)
            print(hdr)
            print("     %-9s %6s %13s %9s %10s"
                  % ("horizon", "n", "range at %d%%" % int(_COMPARE_Q * 100),
                     "of price", "vs chance"))
            for r in grp:
                lab = horizon_label(r["hours"])
                if r.get("status") != "OK":
                    extra = ""
                    if r.get("status") == "TOO FEW WINDOWS":
                        extra = "(%d windows, %d to test - need %d)" % (
                            r.get("n", 0), r.get("n_test", 0), _MIN_TEST_WINDOWS)
                    print("     %-9s   %s %s" % (lab, r.get("status"), extra))
                    continue
                star = "  <== most contained" if d.get("best") is r else ""
                print("     %-9s %6d   +/-%8.2f %8.3f%% %9.3f%s"
                      % (lab, r["n"], r["width"] / 2.0, r["width_pct"],
                         r["vs_random_walk"], star))
            e = exps.get(tf)
            if e is not None:
                if e < _NULL_LO:
                    v = "MEAN-REVERTING - ranges hold better than chance"
                elif e > _NULL_HI:
                    v = "TRENDING - price runs, ranges break more than chance"
                else:
                    v = "chance-like - honest range, no extra edge"
                print("     scaling exponent %.2f  (chance reads %.2f-%.2f)  %s"
                      % (e, _NULL_LO, _NULL_HI, v))
            else:
                print("     scaling exponent: needs 3+ horizons on this feed")
        b = d.get("best")
        if b:
            print("     -> most contained vs chance: %s - +/-%.2f (%.3f%% of "
                  "price) holding %.1f%%."
                  % (horizon_label(b["hours"]), b["width"] / 2.0,
                     b["width_pct"], b["rate_at_fixed"]))
        # the overlap check: 1 hour measured on two different feeds
        ones = [r for r in rows if r.get("status") == "OK" and r["hours"] == 1]
        if len(ones) >= 2:
            a, c = ones[0], ones[1]
            diff = abs(a["width"] - c["width"]) / max(a["width"], c["width"]) * 100.0
            print("     CROSS-CHECK  1 hour reads +/-%.2f on the %s feed and "
                  "+/-%.2f on the %s feed - %.0f%% apart."
                  % (a["width"] / 2.0, a["tf"], c["width"] / 2.0, c["tf"], diff))
            if diff > 20:
                print("                  That gap is the two feeds covering "
                      "different market, not a horizon effect. Trust the "
                      "longer-span feed.")
    print()
    print("=" * 78)
    print("A range that only matches chance is still honest - it is just volatility.")
    print("The horizons worth publishing are the ones measurably below 1.00.")
    print("=" * 78)

def write_extremes_report(res, target, base_dir=None):
    """Single writer for the extremes report - CLI and bot share it."""
    d = os.path.join(base_dir, "data") if base_dir else DATA_DIR
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, "extreme_projection_report.json")
    tmp = out + ".tmp"
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "target": target, "results": res}
    # write-then-rename: a crash mid-write can never leave a half-written
    # report that the public card would then try to read.
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    os.replace(tmp, out)
    return out


def refresh_extremes_report_if_stale(base_dir=None, max_age_days=6,
                                     target=68, deep=5000,
                                     markets=None, data_layer_mod=None):
    """
    Keep the published ranges current without anyone having to remember.

    The bands are statistics, not live quotes, so they do not need refreshing
    often - but they DO need refreshing, or the staleness guard in w189_levels
    will eventually (and correctly) silence the whole card. Called from the
    daily brief, this recomputes at most once a week and needs no scheduler of
    its own.

    Returns True if it refreshed, False if it was already fresh or could not
    run. Never raises: a stale or missing report makes the card print nothing,
    which is the safe direction.
    """
    try:
        path = EXTREMES_REPORT
        if base_dir:
            path = os.path.join(base_dir, "data", "extreme_projection_report.json")
        if os.path.exists(path):
            age = time.time() - os.path.getmtime(path)
            if age < max_age_days * 86400:
                return False
    except Exception:
        return False
    try:
        dl = data_layer_mod
        if dl is None:
            import data_layer as dl
        res = sweep_extremes(dl, markets or MARKETS, target / 100.0, deep)
        fresh_ok = sum(1 for r in res
                       if r.get("side") == "RANGE" and r.get("verdict") == "PUBLISH")

        # NEVER replace a good report with an empty one.
        #
        # sweep_extremes swallows a per-market feed error and simply returns
        # fewer rows - so a transient outage on Railway produces a perfectly
        # valid EMPTY result, which would then overwrite a report full of good
        # measured bands. Caught in test: a broken feed wiped the live report
        # and the card went silent for a week until the next refresh.
        #
        # The rule is the same one that governs every data file here: a write
        # may only ever add or improve. If this run found nothing publishable
        # and the existing report has something, the existing report wins.
        if fresh_ok == 0:
            # Write NOTHING. Not the old report overwritten, not an empty one.
            #
            # Writing an empty report would also poison the freshness check:
            # the file would look brand new, so the next six days of briefs
            # would skip the refresh and publish nothing. Declining to write
            # leaves the good report in place if there is one, and leaves no
            # report at all if there is not - and either way tomorrow's brief
            # tries again. Caught in test.
            _log.warning("w189: refresh found no publishable range "
                         "(feed problem?) - not writing; will retry")
            return False

        write_extremes_report(res, target, base_dir=base_dir)
        return True
    except Exception as e:
        _log.warning("w189: extremes refresh failed (%s) - keeping old report", e)
        return False


_REFRESH_LOCK    = threading.Lock()
_REFRESH_RUNNING = False


def refresh_extremes_report_async(base_dir=None, **kw):
    """
    Kick the refresh off in the background and return immediately.

    This matters more than it looks. The refresh pulls deep history for four
    markets and takes minutes. The morning brief is built inside the bot's
    async loop, so calling the refresh inline would freeze Telegram polling -
    the bot would go unresponsive, once a week, for no visible reason.

    So today's brief uses the report that already exists, and the refreshed one
    is picked up by tomorrow's. Bands are week-scale statistics; a day of lag in
    a weekly refresh costs nothing.

    The running flag prevents a second thread piling on if a brief is triggered
    twice (the /brief button exists), which would otherwise have two threads
    writing the same file. Returns True if a thread was started.
    """
    global _REFRESH_RUNNING
    with _REFRESH_LOCK:
        if _REFRESH_RUNNING:
            return False
        _REFRESH_RUNNING = True

    def _run():
        global _REFRESH_RUNNING
        try:
            refresh_extremes_report_if_stale(base_dir=base_dir, **kw)
        except Exception as e:
            _log.warning("w189: background refresh failed: %s", e)
        finally:
            with _REFRESH_LOCK:
                _REFRESH_RUNNING = False

    t = threading.Thread(target=_run, name="w189-refresh", daemon=True)
    t.start()
    return True


def print_ladders(results, only=("NQ", "GC")):
    """Show the full width-vs-accuracy ladder so the pick can be overridden."""
    rows = [r for r in results if r.get("side") == "RANGE" and r.get("ladder")
            and r.get("market") in only]
    if not rows:
        return
    print()
    print("=" * 74)
    print("HOW WIDE SHOULD THE RANGE BE?   (every number measured out-of-sample)")
    print("=" * 74)
    for r in rows:
        px = r.get("ref_price") or 0
        print()
        print("  %s  -  %dh window        (price %s)"
              % (r["market"], r["hours"], "{:,.2f}".format(px)))
        print("     %-9s %-22s %-8s" % ("width", "range from here", "held"))
        for c in r["ladder"]:
            chosen = (r.get("verdict") == "PUBLISH"
                      and int(c["q"] * 100) == r.get("chosen_q"))
            lo, hi = px - c["dn"], px + c["up"]
            print("     %-9.1f %-22s %5.1f%%  %s%s"
                  % (c["width"], "%s - %s" % ("{:,.0f}".format(lo), "{:,.0f}".format(hi)),
                     c["rate"], "#" * int(c["rate"] / 5),
                     "   <== PICKED" if chosen else ""))
        if r.get("verdict") == "PUBLISH":
            print("     picked: %s" % r.get("why", ""))
        else:
            print("     NOTHING PUBLISHABLE: %s" % r.get("why", ""))
    print()
    print("  Wider is always more accurate - that is arithmetic, not skill. The")
    print("  pick is the knee: past it, extra width buys very little accuracy.")
    print("=" * 74)


def print_extremes(results, target):
    print("=" * 80)
    print("EXTREME PROJECTION - how far price RUNS, not where it lands")
    print("=" * 80)
    print("non-overlapping windows, walk-forward, one-sided (only under-delivery fails)")
    print("HIGH = how far above the open it ran.  LOW = how far below.\n")
    print("  %-5s %5s %5s %6s %11s %8s %9s  %s"
          % ("mkt", "hours", "side", "n", "band", "band%", "measured", "verdict"))
    best = {}
    for r in results:
        # Wave 190: RANGE rows have their own section (print_ladders) and carry
        # different fields. They crashed this table on a KeyError for
        # 'band_pct' the first time it ran live - my fault: I added a new row
        # type to the sweep and never ran the printer over it. Skipped here,
        # and every field below is now read defensively so that a printer can
        # never again take down a run that had already done all its work.
        if r.get("side") == "RANGE":
            continue
        try:
            if r.get("verdict") == "INSUFFICIENT_DATA":
                print("  %-5s %5d %5s %6d   INSUFFICIENT DATA"
                      % (r.get("market", "?"), r.get("hours", 0),
                         r.get("side", "?"), r.get("n", 0)))
                continue
            bp = r.get("band_pct")
            print("  %-5s %5d %5s %6d %11.2f %7s %8.1f%%  %s"
                  % (r.get("market", "?"), r.get("hours", 0), r.get("side", "?"),
                     r.get("n", 0), float(r.get("band") or 0),
                     ("%.3f%%" % bp) if bp is not None else "    -  ",
                     float(r.get("measured") or 0), r.get("verdict", "?")))
            if r.get("verdict") == "PUBLISH" and bp is not None:
                k = (r.get("market"), r.get("side"))
                cur = best.get(k)
                if cur is None or bp < cur.get("band_pct", 1e9):
                    best[k] = r
        except Exception as e:
            print("  %-5s %5s %5s   (row unprintable: %s)"
                  % (r.get("market", "?"), r.get("hours", "?"),
                     r.get("side", "?"), e))
    print()
    if best:
        print("  TIGHTEST PUBLISHABLE EXTENSION PER MARKET/SIDE:")
        for (mkt, side), r in sorted(best.items()):
            try:
                px = float(r.get("ref_price") or 0)
                band = float(r.get("band") or 0)
                lvl = px + band if side == "HIGH" else px - band
                word = "stays below" if side == "HIGH" else "stays above"
                print("     %-5s %-4s %sh  %s %.2f  (+/-%.2f, %.3f%%)  %.1f%% measured, n=%d"
                      % (mkt, side, r.get("hours", "?"), word, lvl, band,
                         float(r.get("band_pct") or 0),
                         float(r.get("measured") or 0), int(r.get("n") or 0)))
            except Exception as e:
                print("     %-5s %-4s (unprintable: %s)" % (mkt, side, e))
    else:
        print("  Nothing passed.")
    print("=" * 80)
    return best


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
    if "--quality" in argv:
        try:
            import data_layer
        except Exception as e:
            print("ERROR: data_layer unavailable (%s). This must run on Railway." % e)
            return None
        q = sweep_quality(data_layer, MARKETS, dp)
        print_quality(q)
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(os.path.join(DATA_DIR, "horizon_quality_report.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
                           "compare_q": _COMPARE_Q, "null_band": [_NULL_LO, _NULL_HI],
                           "markets": q}, f, indent=2, default=str)
            print("  report: %s"
                  % os.path.join(DATA_DIR, "horizon_quality_report.json"))
        except Exception as e:
            print("  WARNING: could not write quality report: %s" % e)
        return q
    if "--extremes" in argv:
        try:
            import data_layer
        except Exception as e:
            print("ERROR: data_layer unavailable (%s). This must run on Railway." % e)
            return None
        res = sweep_extremes(data_layer, MARKETS, tg / 100.0, dp)
        best = print_extremes(res, tg)
        print_ladders(res)
        try:
            print("  report: %s" % write_extremes_report(res, tg))
        except Exception as e:
            print("  WARNING: could not write extremes report: %s" % e)
        return res
    if "--sweep" in argv:
        try:
            import data_layer
        except Exception as e:
            print("ERROR: data_layer unavailable (%s). This must run on Railway." % e)
            return None
        res = sweep_horizons(data_layer, MARKETS, tg / 100.0, dp)
        best = print_sweep(res, tg)
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(os.path.join(DATA_DIR, "horizon_sweep_report.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
                           "target": tg, "results": res,
                           "tightest_publishable": best}, f, indent=2)
            print("  report: %s" % os.path.join(DATA_DIR, "horizon_sweep_report.json"))
        except Exception as e:
            print("  WARNING: could not write sweep report: %s" % e)
        return res
    return run(ms, tg, dp)


if __name__ == "__main__":
    main()
