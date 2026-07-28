"""
diag_floors.py - READ ONLY. Calibrates the minimum-stop floors before the
dormant guard is switched back on.

THE SITUATION
=============
Wave 16 (May 8, 2026) added a guard that suppresses setups whose stop is
tighter than market noise. It reads:

    _stop_p = float(s.get("stop", 0))

But all 27 setup dicts write "raw_stop", never "stop", and nothing anywhere
renames one to the other. So _stop_p is always 0, the `if _stop_p > 0` test is
always False, and THE GUARD HAS NEVER RUN. Not once since May.

That is why data/tight_stop_suppressed.jsonl is empty, why the 455 microscopic
BREAK_RETEST stops were never blocked, and why the 6 SOL trades that prompted
Wave 16 in the first place were never actually protected against.

WHY THIS IS NOT A ONE-WORD FIX
==============================
Switching the guard on with its existing floors would be worse than leaving it
off. The floors were written in May from six SOL trades and have never been
tested, because the code holding them never executed:

    market   floor    measured median stop
    NQ       0.25%    0.279%      -> floor sits just under typical
    GC       0.30%    0.616%      -> fine
    BTC      0.50%    0.381%      -> FLOOR IS ABOVE THE MEDIAN
    SOL      0.80%    0.514%      -> FLOOR IS ABOVE THE MEDIAN

On BTC and SOL the floor is higher than the typical stop those markets
actually produce, so activating it as-is would suppress more than half of all
crypto setups. That trades a silent bug for a loud one.

WHAT THIS DOES
==============
It replays the guard over every historical row at a range of candidate floors
and reports, per market:

  * how many BROKEN stops it catches (inverted, or absurdly tight)
  * how many HEALTHY stops it would destroy - the real cost
  * the resulting suppression rate

Then it recommends the floor that catches essentially all the broken ones
while costing the fewest good setups, and shows the current value beside it so
the change is visible rather than asserted.

Nothing is written and nothing is changed.

USAGE:
    python diag_floors.py
"""

import os
import csv
import glob

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
SEP = "=" * 74

# What the live code has today, from outcome_tracker.MIN_RISK_PCT_BY_MARKET.
CURRENT = {"NQ": 0.0025, "GC": 0.0030, "BTC": 0.0050, "SOL": 0.0080}

# A stop this tight is broken by any standard - used to define "broken" without
# reference to any floor, so the calibration is not circular.
_ABSURD_PCT = 0.0005          # 0.05% of price


def _f(row, *names):
    for n in names:
        v = row.get(n)
        if v not in (None, "", "None", "nan"):
            try:
                return float(v)
            except Exception:
                continue
    return None


def _s(row, *names):
    for n in names:
        v = row.get(n)
        if v:
            return str(v).strip()
    return ""


def load():
    paths = [p for p in (os.path.join(DATA, "outcomes.csv"),
                         os.path.join(DATA, "strategy_log.csv"),
                         os.path.join(BASE, "strategy_log.csv"))
             if os.path.exists(p)]
    paths += sorted(glob.glob(os.path.join(DATA, "archive", "*.csv")))
    rows = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for r in csv.DictReader(f):
                    e = _f(r, "entry", "entry_price")
                    s = _f(r, "stop", "raw_stop", "stop_price")
                    if not e or s is None or e <= 0:
                        continue
                    setup = _s(r, "setup_type", "setup", "type")
                    if "SHADOW_SCAN" in setup.upper():
                        continue          # placeholder rows, not real setups
                    side = _s(r, "side", "direction").upper()
                    long_ = ("LONG" in side or "BULL" in side
                             or (not side and "BULL" in setup.upper()))
                    risk = (e - s) if long_ else (s - e)
                    rows.append({"m": _s(r, "market") or "?",
                                 "setup": setup or "?",
                                 "pct": risk / e,          # SIGNED, on purpose
                                 "fired": "FIRED" in _s(r, "decision").upper()})
        except Exception:
            continue
    return rows, paths


def main():
    print(SEP)
    print("STOP FLOOR CALIBRATION - read only")
    print(SEP)
    rows, paths = load()
    print("  files read : %d" % len(paths))
    print("  real setup rows (SHADOW_SCAN excluded) : %d" % len(rows))
    if not rows:
        print("  nothing to calibrate")
        print(SEP)
        return

    by_mkt = {}
    for r in rows:
        by_mkt.setdefault(r["m"], []).append(r)

    print()
    print("  A stop is BROKEN if it is inverted (negative risk) or tighter")
    print("  than %.2f%% of price. That definition does not reference any" % (_ABSURD_PCT * 100))
    print("  floor, so the calibration below is not circular.")

    recommended = {}
    for m in sorted(by_mkt):
        rs = by_mkt[m]
        broken = [r for r in rs if r["pct"] <= _ABSURD_PCT]
        healthy = [r for r in rs if r["pct"] > _ABSURD_PCT]
        if not healthy:
            continue
        hp = sorted(r["pct"] for r in healthy)

        def at(q):
            return hp[min(len(hp) - 1, max(0, int(len(hp) * q)))]

        print()
        print("  " + "-" * 66)
        print("  %s   %d rows   %d broken (%.1f%%)   %d healthy"
              % (m, len(rs), len(broken), 100.0 * len(broken) / len(rs), len(healthy)))
        print("     healthy stop distances:  p1 %.3f%%   p5 %.3f%%   median %.3f%%"
              % (at(0.01) * 100, at(0.05) * 100, at(0.50) * 100))

        cands = sorted(set([CURRENT.get(m, 0.005), at(0.01), at(0.02),
                            at(0.05), at(0.10), _ABSURD_PCT * 2]))
        print()
        print("     %-10s %12s %14s %12s" % ("floor", "broken cut", "healthy LOST", "verdict"))
        best, best_cost = None, None
        for f in cands:
            cut = sum(1 for r in broken if r["pct"] < f)
            lost = sum(1 for r in healthy if r["pct"] < f)
            cut_pct = 100.0 * cut / max(1, len(broken))
            lost_pct = 100.0 * lost / len(healthy)
            tag = ""
            if abs(f - CURRENT.get(m, -1)) < 1e-9:
                tag = "<- CURRENT"
            if cut_pct >= 99.0 and (best_cost is None or lost_pct < best_cost):
                best, best_cost = f, lost_pct
            print("     %8.4f%% %11.1f%% %13.1f%% %12s"
                  % (f * 100, cut_pct, lost_pct, tag))
        if best is not None:
            recommended[m] = best
            cur = CURRENT.get(m)
            cur_lost = (100.0 * sum(1 for r in healthy if r["pct"] < cur) / len(healthy)
                        if cur else None)
            print()
            print("     -> recommend %.4f%%  (cuts >99%% of broken, loses %.1f%% of good)"
                  % (best * 100, best_cost))
            if cur_lost is not None:
                print("        current    %.4f%%  would lose %.1f%% of good setups"
                      % (cur * 100, cur_lost))
                if cur_lost > best_cost + 5:
                    print("        >>> the CURRENT floor is far too high for this market.")

    print()
    print(SEP)
    print("RECOMMENDED MIN_RISK_PCT_BY_MARKET")
    print(SEP)
    for m in sorted(recommended):
        cur = CURRENT.get(m)
        arrow = "" if cur is None else ("   (was %.4f)" % cur)
        print('    "%s": %.5f,%s' % (m, recommended[m], arrow))
    print()
    print("  These are measured from your own rows, not guessed. Paste this")
    print("  back and the fix wave will use exactly these numbers.")
    print(SEP)


if __name__ == "__main__":
    main()
