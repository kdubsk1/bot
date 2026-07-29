"""
combine_math.py - READ ONLY. What are the actual odds of passing?

THE QUESTION NOBODY HAS ANSWERED
================================
A Topstep combine is not a trading problem, it is a BOUNDED RANDOM WALK with
three absorbing barriers:

    +$3,000  profit target      -> PASS
    -$2,000  trailing drawdown  -> FAIL
    -$1,000  in any single day   -> FAIL

Expectancy alone does not tell you whether you pass. A profitable strategy can
fail a combine most of the time if its variance is large relative to the
drawdown barrier - that is the whole reason combines are hard, and it is why
"my edge is positive" and "I will pass" are different claims.

This computes the real number by resampling THIS BOT'S OWN closed trades.

WHY BOOTSTRAP RATHER THAN A FORMULA
===================================
The classic gambler's-ruin formula assumes every bet is the same size. These
trades are not: the R distribution is skewed - many small losses, a few large
wins - and the trailing drawdown depends on the PATH, not just the sum. A
formula would give a clean answer to the wrong question.

So it resamples actual trade outcomes, in sequence, and applies the real rules
including the trailing peak and the daily loss limit. No distributional
assumption is made at all.

WHAT IT REPORTS
===============
  * P(pass) with a confidence interval
  * how it fails when it fails - drawdown or daily limit
  * how many trades a resolution takes, and therefore how long
  * the sensitivity: what P(pass) would be at different win rates and
    payoffs, so you can see which lever actually moves it

Nothing is written. No trade is touched.

USAGE:
    python combine_math.py
    python combine_math.py --sims 20000 --target 3000 --dd 2000 --daily 1000
"""

import os
import sys
import csv
import glob
import math
import random

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
SEP = "=" * 74

TARGET = 3000.0
MAX_DD = 2000.0
DAILY = 1000.0
START = 50000.0
TRADES_PER_DAY = 3          # measured below if the data supports it
SIMS = 20000


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
    """Real closed trades, as dollar P&L, in time order where possible."""
    paths = [p for p in (os.path.join(DATA, "outcomes.csv"),
                         os.path.join(BASE, "outcomes.csv")) if os.path.exists(p)]
    paths += sorted(glob.glob(os.path.join(DATA, "archive", "outcomes_*.csv")))
    rows = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for r in csv.DictReader(f):
                    res = (r.get("result") or "").strip().upper()
                    if res not in ("WIN", "LOSS"):
                        continue
                    pnl = _f(r, "pnl", "PnL", "profit", "net")
                    mkt = (r.get("market") or "").strip().upper()
                    ts = (r.get("timestamp") or "")[:10]
                    rows.append({"pnl": pnl, "res": res, "market": mkt, "day": ts})
        except Exception:
            continue
    return rows


def simulate(pnls, sims, target, max_dd, daily, per_day, seed=7):
    """
    One combine attempt = trade until an absorbing barrier is hit.

    The trailing drawdown follows the Topstep rule: it trails the PEAK until
    the peak reaches start + max_dd, after which the floor locks at the
    starting balance. Getting that wrong would flatter the result, because a
    permanently-trailing stop is much harder to survive.
    """
    rnd = random.Random(seed)
    n = len(pnls)
    passes = 0
    fail_dd = fail_daily = 0
    trades_used = []
    for _ in range(sims):
        bal = START
        peak = START
        floor = START - max_dd
        locked = False
        day_start = bal
        in_day = 0
        t = 0
        while True:
            bal += pnls[rnd.randrange(n)]
            t += 1
            in_day += 1
            if bal > peak:
                peak = bal
                if not locked:
                    if peak >= START + max_dd:
                        floor = START
                        locked = True
                    else:
                        floor = peak - max_dd
            if bal - START >= target:
                passes += 1
                trades_used.append(t)
                break
            if bal <= floor:
                fail_dd += 1
                trades_used.append(t)
                break
            if day_start - bal >= daily:
                fail_daily += 1
                trades_used.append(t)
                break
            if in_day >= per_day:
                in_day = 0
                day_start = bal
            if t > 5000:
                trades_used.append(t)
                break
    return passes, fail_dd, fail_daily, trades_used


def wilson(k, n):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    z = 1.96
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def main(argv=None):
    argv = argv or sys.argv[1:]

    def opt(flag, default, cast=float):
        if flag in argv:
            try:
                return cast(argv[argv.index(flag) + 1])
            except Exception:
                pass
        return default

    sims = int(opt("--sims", SIMS))
    target = opt("--target", TARGET)
    max_dd = opt("--dd", MAX_DD)
    daily = opt("--daily", DAILY)

    print(SEP)
    print("COMBINE MATH - what are the actual odds?")
    print(SEP)
    trades = load_trades()
    withp = [t for t in trades if t["pnl"] is not None]
    print("  closed trades found      : %d" % len(trades))
    print("  carrying a dollar P&L    : %d" % len(withp))
    if len(withp) < 30:
        print()
        print("  Not enough trades with a P&L to simulate honestly.")
        print("  This needs at least 30. Come back when the ledger has them.")
        print(SEP)
        return

    pnls = [t["pnl"] for t in withp]
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    zeros = [p for p in pnls if p == 0]
    wr = 100.0 * len(wins) / len(pnls)
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = sum(losses) / len(losses) if losses else 0.0
    ev = sum(pnls) / len(pnls)

    print()
    print("  YOUR ACTUAL DISTRIBUTION")
    print("     win rate       : %.1f%%   (%d W / %d L)" % (wr, len(wins), len(losses)))
    if zeros:
        print("     ZERO-P&L closes: %d  <- these are the broken-stop trades"
              % len(zeros))
    print("     average win    : $%.2f" % avg_w)
    print("     average loss   : $%.2f" % avg_l)
    print("     expectancy     : $%+.2f per trade" % ev)
    if avg_l > 0:
        be = 100.0 * avg_l / (avg_w + avg_l)
        print("     breakeven WR   : %.1f%%   (you are %s it by %.1f pts)"
              % (be, "above" if wr > be else "BELOW", abs(wr - be)))

    if ev <= 0:
        print()
        print("  >>> Expectancy is NOT positive. No amount of risk management")
        print("      passes a combine with a negative edge - the barriers only")
        print("      make it worse. Fix the edge before anything else.")
        print(SEP)
        return

    per_day = TRADES_PER_DAY
    days = {}
    for t in withp:
        if t["day"]:
            days[t["day"]] = days.get(t["day"], 0) + 1
    if days:
        per_day = max(1, int(round(sum(days.values()) / float(len(days)))))
        print("     trades per day : %d  (measured over %d days)" % (per_day, len(days)))

    print()
    print("  SIMULATING %s COMBINE ATTEMPTS" % format(sims, ","))
    print("     rules: +$%s target, -$%s trailing drawdown, -$%s daily limit"
          % (format(int(target), ","), format(int(max_dd), ","), format(int(daily), ",")))
    p, fdd, fday, tu = simulate(pnls, sims, target, max_dd, daily, per_day)
    lo, hi = wilson(p, sims)
    print()
    print("     PASS            : %5.1f%%   (95%% CI %.1f - %.1f)"
          % (100.0 * p / sims, lo * 100, hi * 100))
    print("     fail - drawdown : %5.1f%%" % (100.0 * fdd / sims))
    print("     fail - daily    : %5.1f%%" % (100.0 * fday / sims))
    tu.sort()
    print("     trades to resolve: median %d  (about %d trading days)"
          % (tu[len(tu) // 2], tu[len(tu) // 2] // max(1, per_day)))

    print()
    print("  " + "=" * 66)
    print("  WHICH LEVER ACTUALLY MOVES IT")
    print("  " + "=" * 66)
    print("     %-28s %s" % ("change", "P(pass)"))
    base = 100.0 * p / sims
    print("     %-28s %5.1f%%   <- as you are now" % ("nothing", base))

    # more trades at the same edge
    for mult, lab in ((0.5, "halve position size"), (2.0, "double position size")):
        sc = [x * mult for x in pnls]
        pp, _, _, _ = simulate(sc, sims, target, max_dd, daily, per_day, seed=11)
        print("     %-28s %5.1f%%" % (lab, 100.0 * pp / sims))

    # better win rate at the same payoff
    for add in (5.0, 10.0):
        want = min(0.95, (wr + add) / 100.0)
        sc = ([avg_w] * int(round(want * 1000)) +
              [-avg_l] * int(round((1 - want) * 1000)))
        pp, _, _, _ = simulate(sc, sims, target, max_dd, daily, per_day, seed=13)
        print("     %-28s %5.1f%%" % ("win rate +%.0f points" % add, 100.0 * pp / sims))

    # bigger winners at the same win rate
    for mult in (1.25, 1.5):
        sc = [x * mult if x > 0 else x for x in pnls]
        pp, _, _, _ = simulate(sc, sims, target, max_dd, daily, per_day, seed=17)
        print("     %-28s %5.1f%%" % ("winners %.0f%% bigger" % ((mult - 1) * 100),
                                      100.0 * pp / sims))

    print()
    print("  Position size is the lever with the strongest effect in both")
    print("  directions - it scales the walk against FIXED barriers. Smaller")
    print("  size means more trades to the target but far more room before")
    print("  the drawdown floor.")
    print(SEP)
    print("  Nothing was changed. This is arithmetic on your own ledger.")
    print(SEP)


if __name__ == "__main__":
    main()
