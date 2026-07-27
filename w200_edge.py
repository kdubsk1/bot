"""
w200_edge.py - the track-record line on every entry card.

WHAT WAYNE ASKED FOR
====================
"Can we make it so you can see the percent the trade will hit on every entry?
Like if it's a 60% chance of winning it will say that."

TWO THINGS HAD TO BE SETTLED FIRST
==================================
1. WHERE DOES THE PERCENTAGE COME FROM?

   The obvious source is the conviction score - it is per-trade, which is what a
   probability should be. But conviction has never been checked against
   outcomes. If conviction 70 and conviction 45 win at the same rate, then any
   percentage derived from conviction is invented, and putting an invented
   number in front of paying subscribers is the worst thing this bot could do.

   So conviction is TESTED before it is trusted. Historical trades are split at
   the median conviction and the two halves compared. Only if the high half
   genuinely wins more - by more than sampling noise allows - is conviction used
   for a per-trade number. Otherwise the card falls back to the setup's own
   historical rate and does not pretend to be per-trade.

2. A HIT RATE ON ITS OWN IS MISLEADING.

   Measured on this bot's real trades: a 60% setup at 1.0R makes +0.200R, while
   a 40% setup at 3.0R makes +0.600R. The "worse" one is three times better.
   BTC:BREAK_RETEST_BEAR hits 22% and is profitable.

   Showing only a hit rate would teach subscribers to read it exactly backwards.
   So the line always carries the hit rate AND the average R together.

WHAT IT SHOWS
=============
    History  62% hit · +0.57R avg   ·  38 trades

and nothing at all when the evidence is too thin. Silence is a valid output
here - it is better than a number nobody should rely on.

SOURCE
======
data/setup_health_report.json, written by setup_health.py (Wave 199) from the
real closed-trade ledger. One source of truth, refreshed whenever that runs.
"""

import os
import json
import math

_REPORT = "setup_health_report.json"

# Below this many closed trades, no number is shown at all.
_MIN_TRADES = 8
# A hit rate is only called per-trade if conviction demonstrably discriminates.
_CONV_MIN_PER_SIDE = 10


def _load(base_dir):
    try:
        p = os.path.join(base_dir, "data", _REPORT)
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def conviction_discriminates(high_w, high_n, low_w, low_n):
    """
    Does a higher conviction score actually win more often?

    Split at the median and compare the halves with a two-proportion z test. The
    bar is deliberately high: a difference must clear 95% confidence before the
    score is allowed to drive a number shown to subscribers. Anything less and
    conviction is decoration, not information.

    Returns (True/False, z, detail).
    """
    if high_n < _CONV_MIN_PER_SIDE or low_n < _CONV_MIN_PER_SIDE:
        return False, None, "not enough trades on both sides of the median"
    p1 = high_w / float(high_n)
    p2 = low_w / float(low_n)
    p = (high_w + low_w) / float(high_n + low_n)
    se = math.sqrt(max(1e-12, p * (1 - p) * (1.0 / high_n + 1.0 / low_n)))
    z = (p1 - p2) / se if se > 0 else 0.0
    if z >= 1.96:
        return True, z, "high conviction wins %.0f%% vs %.0f%% (z=%.2f)" % (
            p1 * 100, p2 * 100, z)
    return False, z, "high conviction %.0f%% vs low %.0f%% - not distinguishable (z=%.2f)" % (
        p1 * 100, p2 * 100, z)


def edge_line(base_dir, market, setup, conviction=None):
    """
    One line for the entry card, or None when there is not enough evidence.

    None is a normal, expected result. The caller must treat it as "print
    nothing" rather than as an error.
    """
    rep = _load(base_dir)
    if not rep:
        return None
    key = "%s:%s" % (market, setup)
    g = (rep.get("setups") or {}).get(key)
    if not g:
        return None
    try:
        n = int(g.get("n", 0))
        if n < _MIN_TRADES:
            return None
        hit = float(g.get("win_rate", 0))
        exp = g.get("expectancy_r")
        if exp is None:
            # Without expectancy the hit rate alone would mislead - so say
            # nothing rather than half the story.
            return None
        # A setup whose record is built on broken stops has no meaningful
        # history. Do not advertise it.
        if g.get("verdict") == "FIX THE STOPS FIRST":
            return None
        return ("   History  *%.0f%%* hit  ·  *%+.2fR* avg   `%d trades`"
                % (hit, float(exp), n))
    except Exception:
        return None


def edge_summary(base_dir):
    """Plain-English summary for the control channel / a command."""
    rep = _load(base_dir)
    if not rep:
        return "No setup health report yet. Run: python setup_health.py"
    setups = rep.get("setups") or {}
    if not setups:
        return "Setup health report is empty."
    buckets = {}
    for k, v in setups.items():
        buckets.setdefault(v.get("verdict", "?"), []).append((k, v))
    order = ["PROFITABLE - REAL", "POSITIVE - UNPROVEN", "TOO THIN",
             "LOSING - UNPROVEN", "LOSING - REAL", "FIX THE STOPS FIRST",
             "UNMEASURABLE"]
    out = ["*SETUP HEALTH*", "_%d setups, %d closed trades_"
           % (len(setups), rep.get("trades", 0)), ""]
    for verdict in order:
        rows = buckets.get(verdict)
        if not rows:
            continue
        out.append("*%s*  (%d)" % (verdict, len(rows)))
        rows.sort(key=lambda kv: -(kv[1].get("expectancy_r") or -99))
        for k, v in rows[:6]:
            e = v.get("expectancy_r")
            out.append("   `%s`  %s  n=%d"
                       % (k, ("%+.2fR" % e) if e is not None else "-", v.get("n", 0)))
        if len(rows) > 6:
            out.append("   _...%d more_" % (len(rows) - 6))
        out.append("")
    return "\n".join(out).strip()


# ==================================================================
# The startup notification.
#
# The old one opened with "Bot Online - Current Market State" and then printed a
# full operator dump: trend scores, structure bias per timeframe, ADX, N/A
# markers where a frame was short. That is a debugging read-out, and with
# CONTROL_CHAT_ID unset it has been going to the PUBLIC channel.
#
# This is the same event stated the way a product would state it.
# ==================================================================

def build_startup_card(markets=None, timeframes=None, session=None,
                       when=None, version=None):
    """A short, professional 'system online' notice. Never raises."""
    try:
        bar = "━" * 22
        rows = []
        if markets:
            pretty = {"GC": "GOLD"}
            rows.append(("Markets", "  ·  ".join(
                pretty.get(m, m) for m in markets)))
        if timeframes:
            rows.append(("Scanning", "  ·  ".join(timeframes)))
        if session:
            rows.append(("Session", str(session)))
        if when:
            rows.append(("Started", str(when)))
        if version:
            rows.append(("Build", str(version)))
        if not rows:
            return "⚡ *SYSTEM ONLINE*"
        w = max(len(k) for k, _ in rows)
        body = "\n".join("`%s`   %s" % (k.ljust(w), v) for k, v in rows)
        return "\n".join([bar, "⚡ *SYSTEM ONLINE*", "", body, bar])
    except Exception:
        return "⚡ *SYSTEM ONLINE*"
