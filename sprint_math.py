"""
sprint_math.py - READ ONLY. Can this bot pass the combine in under a week?

THE RULES, READ FROM TOPSTEP RATHER THAN REMEMBERED
==================================================
Checked against help.topstep.com on 2026-07-29. Four things there contradict
what every earlier simulation here assumed, and all four change the answer:

  1. THE DAILY LOSS LIMIT IS NOT A FAILURE.
     "Triggering it is not a rule violation - it's a forced break for the rest
     of that session." It is also OPTIONAL in the Combine. Earlier tools here
     counted a daily-limit hit as a blown account. It is not. It costs a DAY,
     which for a speed run is the expensive part.

  2. THE MAXIMUM LOSS LIMIT TRAILS END-OF-DAY BALANCE, NOT THE INTRADAY PEAK.
     "It rises as your end-of-day balance grows." Trailing the intraday peak
     tightens the floor sooner than the real rule does.

  3. THE MLL IS CHECKED ON UNREALIZED P&L, IN REAL TIME.
     An open trade going against you can breach it before it ever closes. A
     closed-trade simulation cannot see that, so every number here is
     optimistic by exactly that much.

  4. THE CONSISTENCY TARGET RATCHETS THE PROFIT TARGET UPWARD.
     Best single day above 50% of total profit and the target becomes
     best_day / 0.50. Losses NEVER reset the best day. Nothing built here has
     ever modeled this, and for a fast pass it is the binding constraint,
     because going fast means having big days.

  Also: you cannot pass in one day. Minimum two. So "under a week" is legal.

WHAT THIS MEASURES
==================
  * P(pass within 5 sessions) across position size and trades per day
  * whether a DAILY PROFIT CAP makes the bot pass FASTER - it does, because
    it stops the ratchet
  * what cutting winners short (what "scalping" usually means) does to the
    edge, measured on the real distribution rather than argued

Nothing is written. No trade is touched.

USAGE:
    python sprint_math.py
    python sprint_math.py --days 5 --sims 20000
"""

import os
import sys
import csv
import glob
import math
import random

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
SEP = "=" * 76

START = 50000.0
MLL = 2000.0
BASE_TARGET = 3000.0
CONSIST = 0.50
DLL = 1000.0


def _f(row, *names):
    for n in names:
        v = row.get(n)
        if v not in (None, "", "None", "nan"):
            try:
                return float(v)
            except Exception:
                continue
    return None


def load_trades():
    """
    Real closed trades as R multiples, plus how many happened per day.

    outcomes.csv stores RR, not dollars - that is deliberate, and documented in
    outcome_tracker at line 654. R is the right unit anyway: 1R on NQ and 1R on
    SOL mean the same thing to the account. Dollars enter once, at the end.
    """
    paths = [p for p in (os.path.join(DATA, "outcomes.csv"),
                         os.path.join(BASE, "outcomes.csv")) if os.path.exists(p)]
    paths += sorted(glob.glob(os.path.join(DATA, "archive", "outcomes_*.csv")))
    rs, per_day, src = [], {}, {}
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for r in csv.DictReader(f):
                    res = (r.get("result") or "").strip().upper()
                    if res not in ("WIN", "LOSS"):
                        continue
                    entry = _f(r, "entry")
                    stop = _f(r, "stop", "raw_stop")
                    ex = _f(r, "exit_price", "exit")
                    rr = _f(r, "rr")
                    side = (r.get("direction") or r.get("side") or "").upper()
                    lng = "LONG" in side or "BULL" in side
                    rm = how = None
                    if None not in (entry, stop, ex) and abs(entry - stop) > 0:
                        risk = abs(entry - stop)
                        rm = ((ex - entry) if lng else (entry - ex)) / risk
                        how = "entry/stop/exit"
                    elif rr is not None:
                        rm, how = (float(rr) if res == "WIN" else -1.0), "planned rr"
                    if rm is None or abs(rm) > 50:
                        continue
                    src[how] = src.get(how, 0) + 1
                    rs.append(rm)
                    day = (r.get("timestamp") or "")[:10]
                    if day:
                        per_day[day] = per_day.get(day, 0) + 1
        except Exception:
            continue
    return rs, per_day, src


def simulate(rs, risk, tpd, max_days, sims=12000, use_dll=True,
             daily_cap=None, seed=7):
    """One Combine attempt, day by day, under the rules as actually written."""
    rnd = random.Random(seed)
    n = len(rs)
    out = {"pass": 0, "mll": 0, "open": 0, "ratchet": 0, "pass_days": []}
    for _ in range(sims):
        bal = START
        floor = START - MLL
        locked = False
        best_day = 0.0
        done = False
        for d in range(max_days):
            day = 0.0
            for _t in range(tpd):
                day += rs[rnd.randrange(n)] * risk
                if bal + day <= floor:
                    out["mll"] += 1
                    done = True
                    break
                if use_dll and day <= -DLL:
                    break                      # forced break, NOT a failure
                if daily_cap is not None and day >= daily_cap:
                    break                      # the governor
            if done:
                break
            bal += day
            if day > best_day:
                best_day = day                 # locks at 3:10 CT, never resets
            if not locked:
                if bal - MLL >= START:
                    floor, locked = START, True
                elif bal - MLL > floor:
                    floor = bal - MLL
            profit = bal - START
            target = max(BASE_TARGET, best_day / CONSIST if best_day > 0 else 0.0)
            # d >= 1 because a one-day pass is not allowed
            if d >= 1 and profit >= target and best_day <= CONSIST * profit:
                out["pass"] += 1
                out["pass_days"].append(d + 1)
                done = True
                break
        if not done:
            out["open"] += 1
        if best_day > BASE_TARGET * CONSIST:
            out["ratchet"] += 1
    return out


def median(xs):
    if not xs:
        return 0
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def main(argv=None):
    argv = argv or sys.argv[1:]

    def opt(flag, default, cast=float):
        if flag in argv:
            try:
                return cast(argv[argv.index(flag) + 1])
            except Exception:
                pass
        return default

    days = int(opt("--days", 5))
    sims = int(opt("--sims", 12000))

    print(SEP)
    print("SPRINT MATH - can this pass in under a week?")
    print(SEP)

    rs, per_day, src = load_trades()
    if len(rs) < 25:
        print("  Only %d closed trades found - not enough to say anything honest." % len(rs))
        print("  Need about 25 minimum. Let it trade, then run this again.")
        print(SEP)
        return

    w = [r for r in rs if r > 0]
    l = [r for r in rs if r <= 0]
    wr = len(w) / len(rs)
    avgw = sum(w) / len(w) if w else 0.0
    avgl = -sum(l) / len(l) if l else 1.0
    E = sum(rs) / len(rs)

    print("  closed trades   : %d" % len(rs))
    for k, v in sorted(src.items(), key=lambda x: -x[1]):
        print("     %-16s %d" % (k, v))
    print("  win rate        : %.1f%%" % (100 * wr))
    print("  average win     : %+.2fR" % avgw)
    print("  average loss    : %+.2fR" % (-avgl))
    print("  EXPECTANCY      : %+.3fR per trade" % E)
    if E <= 0:
        print()
        print("  >>> Expectancy is NEGATIVE. No position size and no speed fixes")
        print("      this. Everything below would only lose money faster.")
        print(SEP)
        return

    if per_day:
        cnt = sorted(per_day.values())
        actual = sum(cnt) / float(len(cnt))
        print()
        print("  trading days    : %d" % len(per_day))
        print("  trades per day  : %.2f average, %d median, %d busiest"
              % (actual, median(cnt), max(cnt)))
    else:
        actual = 2.0

    # ---------- 1. the grid ----------
    print()
    print(SEP)
    print("1. P(PASS WITHIN %d SESSIONS) - size against fire rate" % days)
    print(SEP)
    print("  %7s %7s | %10s %10s | %8s %8s"
          % ("risk/R", "tr/day", "P(<=%dd)" % days, "P(MLL)", "med days", "ratchet"))
    print("  " + "-" * 62)
    grid = []
    for risk in (50, 100, 150, 200, 300):
        for tpd in (2, 4, 8, 12):
            a = simulate(rs, risk, tpd, days, sims=sims)
            b = simulate(rs, risk, tpd, 10, sims=sims)
            p5 = a["pass"] / float(sims)
            pm = b["mll"] / float(sims)
            md = median(b["pass_days"])
            rt = b["ratchet"] / float(sims)
            grid.append((p5, risk, tpd, pm, md, rt))
            print("  %7s %7d | %9.1f%% %9.1f%% | %8s %7.1f%%"
                  % ("$%d" % risk, tpd, 100 * p5, 100 * pm,
                     ("%.0f" % md) if md else "-", 100 * rt))
        print("  " + "-" * 62)

    grid.sort(reverse=True)
    top = grid[0]
    print()
    print("  BEST CONFIGURATION FOUND: $%d per R at %d trades/day" % (top[1], top[2]))
    print("     %.1f%% chance of passing within %d sessions" % (100 * top[0], days))
    print("     %.1f%% chance of blowing the account on the way" % (100 * top[3]))
    print("     needs %.1fx your current fire rate of %.2f trades/day"
          % (top[2] / max(actual, 0.01), actual))
    if top[0] < 0.60:
        print()
        print("  >>> NOTHING TESTED PASSES IN %d SESSIONS RELIABLY." % days)
        print("      The best case is close to a coin flip, and it needs a fire")
        print("      rate this bot has never produced. Under a week is possible.")
        print("      It is not something to plan around.")

    # ---------- 2. the governor ----------
    print()
    print(SEP)
    print("2. THE DAILY PROFIT CAP - stopping early to finish sooner")
    print(SEP)
    print("  A big day raises the profit target to best_day/0.50, and losses")
    print("  never reset it. So an enormous session is not a free gift - it")
    print("  moves the finish line away from you.")
    print()
    risk, tpd = top[1], top[2]
    print("  measured at $%d per R, %d trades/day:" % (risk, tpd))
    print("  %-12s %10s %9s %8s   %s"
          % ("daily cap", "P(<=%dd)" % days, "P(MLL)", "ratchet", "vs no cap"))
    base = None
    rows = []
    for cap in (None, 1500, 1200, 900, 750, 600):
        a = simulate(rs, risk, tpd, days, sims=sims, daily_cap=cap)
        b = simulate(rs, risk, tpd, 10, sims=sims, daily_cap=cap)
        p5 = a["pass"] / float(sims)
        pm = b["mll"] / float(sims)
        rt = b["ratchet"] / float(sims)
        if base is None:
            base = p5
        rows.append((p5, cap))
        tag = "none (now)" if cap is None else "$%d" % cap
        mk = "" if cap is None else "%+5.1f points" % (100 * (p5 - base))
        print("  %-12s %9.1f%% %8.1f%% %7.1f%%   %s"
              % (tag, 100 * p5, 100 * pm, 100 * rt, mk))
    rows.sort(reverse=True)
    if rows[0][1] is not None and rows[0][0] > base:
        print()
        print("  >>> A $%d DAILY PROFIT CAP IS WORTH %+.1f POINTS OF PASS RATE."
              % (rows[0][1], 100 * (rows[0][0] - base)))
        print("      Trading LESS on good days finishes the combine SOONER.")
        print("      This costs no new edge. TopstepX has it built in:")
        print("      Settings -> Risk Settings -> Personal Daily Profit Target.")

    # ---------- 3. scalping ----------
    print()
    print(SEP)
    print("3. WOULD SCALPING WORK? - measured, not argued")
    print(SEP)
    ws = sorted(w, reverse=True)
    tot = sum(ws) or 1.0
    run = 0.0
    print("  where the profit actually comes from:")
    for i, r in enumerate(ws, 1):
        run += r
        if i in (1, 3, max(1, len(ws) // 10), max(1, len(ws) // 4), len(ws)):
            print("     top %3d of %d winners (%4.1f%%) = %5.1f%% of gross profit"
                  % (i, len(ws), 100.0 * i / len(ws), 100.0 * run / tot))
    print()
    print("  if every winner were cut short at a cap:")
    print("  %-14s %12s %13s  %s" % ("win cap", "E/trade", "breakeven WR", "verdict"))
    for cap in (0.5, 1.0, 1.5, 2.0, 3.0):
        e = sum(min(r, cap) if r > 0 else r for r in rs) / len(rs)
        be = avgl / (cap + avgl)
        print("  %-14s %+11.3fR %12.1f%%  %s"
              % ("cut at %.1fR" % cap, e, 100 * be,
                 "profitable" if e > 0 else ">>> LOSES MONEY"))
    print("  %-14s %+11.3fR %12.1f%%  %s"
          % ("uncapped (now)", E, 100 * (avgl / (avgw + avgl)), "profitable"))
    lo, hi = 0.05, 20.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if sum(min(r, mid) if r > 0 else r for r in rs) / len(rs) > 0:
            hi = mid
        else:
            lo = mid
    print()
    print("  >>> BREAKEVEN WIN CAP: %.2fR" % hi)
    print("      Cut winners shorter than that and the edge goes NEGATIVE.")
    be1 = avgl / (1.0 + avgl)
    print("      Scalping at 1R would need a %.1f%% win rate. The bot wins %.1f%%."
          % (100 * be1, 100 * wr))
    print("      That is a %+.1f point jump - a different strategy, not a setting."
          % (100 * (be1 - wr)))
    print()
    print("  THE DISTINCTION THAT MATTERS:")
    print("     'scalp' = cut winners short   -> kills the edge outright")
    print("     'scalp' = trade more often    -> exactly what is needed")
    print("     Same targets, more setups. That is the fix, and it is already")
    print("     queued: Waves 209 and 210 unlock setups that were rejecting")
    print("     themselves.")

    print()
    print(SEP)
    print("WHAT THIS CANNOT SEE")
    print(SEP)
    print("  * Trades are resampled INDEPENDENTLY. Real losses cluster, and")
    print("    clustering is what actually breaks accounts. Every number above")
    print("    is an UPPER BOUND.")
    print("  * The MLL is checked on UNREALIZED P&L in real time. This only")
    print("    sees closed trades, so real breaches happen sooner than modeled.")
    print("  * Fill quality, slippage and commissions are not modeled at all.")
    print("    At high trade counts commissions matter a great deal.")
    print(SEP)


if __name__ == "__main__":
    main()
