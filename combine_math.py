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
    """
    Real closed trades as R multiples.

    outcomes.csv has NO dollar P&L column, and that is by design - the schema
    (CSV_COLS in outcome_tracker) stores entry, stop, target, rr, result and
    exit_price, and a comment at line 654 says so explicitly:

        "outcomes.csv doesn't store $ pnl directly (it stores RR)"

    v1 of this tool asked for a "pnl" column and found 379 trades with none,
    which looked like catastrophic data loss and was actually just the wrong
    question. Reading the schema instead of assuming it is the whole lesson.

    R is the right unit anyway: 1R on NQ and 1R on SOL mean the same thing to
    the account, where dollars do not. The barriers are converted into R once,
    at the end.
    """
    paths = [p for p in (os.path.join(DATA, "outcomes.csv"),
                         os.path.join(BASE, "outcomes.csv")) if os.path.exists(p)]
    paths += sorted(glob.glob(os.path.join(DATA, "archive", "outcomes_*.csv")))
    rows, how = [], {}
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for r in csv.DictReader(f):
                    res = (r.get("result") or "").strip().upper()
                    if res not in ("WIN", "LOSS"):
                        continue
                    entry = _f(r, "entry")
                    stop = _f(r, "stop", "raw_stop")
                    exit_p = _f(r, "exit_price", "exit")
                    rr = _f(r, "rr")
                    side = (r.get("direction") or r.get("side") or "").upper()
                    long_ = "LONG" in side or "BULL" in side

                    rmult, src = None, None
                    # Best evidence: the actual exit against the actual risk.
                    if None not in (entry, stop, exit_p) and abs(entry - stop) > 0:
                        risk = abs(entry - stop)
                        gain = (exit_p - entry) if long_ else (entry - exit_p)
                        rmult, src = gain / risk, "entry/stop/exit"
                    # Fallback: the planned rr, signed by the result. Weaker,
                    # because it assumes the target was hit exactly.
                    elif rr is not None:
                        rmult = float(rr) if res == "WIN" else -1.0
                        src = "planned rr"
                    if rmult is None:
                        continue
                    how[src] = how.get(src, 0) + 1
                    rows.append({"r": rmult, "res": res,
                                 "market": (r.get("market") or "").upper(),
                                 "day": (r.get("timestamp") or "")[:10]})
        except Exception:
            continue
    return rows, how


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
    risk_dollars = opt("--risk", 0.0)

    print(SEP)
    print("COMBINE MATH - what are the actual odds?")
    print(SEP)
    rows, how = load_trades()
    print("  closed trades with a usable R : %d" % len(rows))
    for k, v in sorted(how.items(), key=lambda kv: -kv[1]):
        print("     from %-18s %d" % (k, v))
    if len(rows) < 30:
        print()
        print("  Need at least 30 to simulate honestly. Come back later.")
        print(SEP)
        return

    rs = [t["r"] for t in rows]
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r < 0]
    wr = 100.0 * len(wins) / len(rs)
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = sum(losses) / len(losses) if losses else 1.0
    ev = sum(rs) / len(rs)

    print()
    print("  YOUR ACTUAL DISTRIBUTION  (in R, the unit that compares markets)")
    print("     win rate     : %.1f%%   (%d W / %d L)" % (wr, len(wins), len(rs) - len(wins)))
    print("     average win  : %.2fR" % avg_w)
    print("     average loss : %.2fR" % avg_l)
    print("     expectancy   : %+.3fR per trade" % ev)
    if avg_w > 0:
        be = 100.0 * avg_l / (avg_w + avg_l)
        print("     breakeven WR : %.1f%%   (you are %s by %.1f pts)"
              % (be, "ABOVE it" if wr > be else "BELOW it", abs(wr - be)))

    if ev <= 0:
        print()
        print("  >>> Expectancy is NOT positive. No risk management passes a")
        print("      combine with a negative edge - barriers only make it worse.")
        print(SEP)
        return

    # How many dollars is 1R? Derive it if not supplied.
    if risk_dollars <= 0:
        risk_dollars = START * 0.0075     # 0.75% of the account, the usual sizing
        note = "assumed 0.75%% of account"
    else:
        note = "supplied"
    print()
    print("  1R = $%.2f  (%s - override with --risk N)" % (risk_dollars, note))

    tgt_r = target / risk_dollars
    dd_r = max_dd / risk_dollars
    day_r = daily / risk_dollars
    print("     so the barriers in R:  target %.1fR   drawdown %.1fR   daily %.1fR"
          % (tgt_r, dd_r, day_r))

    per_day = TRADES_PER_DAY
    days = {}
    for t in rows:
        if t["day"]:
            days[t["day"]] = days.get(t["day"], 0) + 1
    if days:
        per_day = max(1, int(round(sum(days.values()) / float(len(days)))))
        print("     trades per day : %d  (measured over %d days)" % (per_day, len(days)))

    print()
    print("  SIMULATING %s ATTEMPTS" % format(sims, ","))
    p, fdd, fday, tu = simulate(rs, sims, tgt_r, dd_r, day_r, per_day)
    lo, hi = wilson(p, sims)
    print()
    print("     PASS            : %5.1f%%   (95%% CI %.1f - %.1f)"
          % (100.0 * p / sims, lo * 100, hi * 100))
    print("     fail - drawdown : %5.1f%%" % (100.0 * fdd / sims))
    print("     fail - daily    : %5.1f%%" % (100.0 * fday / sims))
    tu.sort()
    med = tu[len(tu) // 2]
    print("     trades to resolve: median %d  (about %d trading days)"
          % (med, med // max(1, per_day)))

    print()
    print("  " + "=" * 66)
    print("  WHICH LEVER ACTUALLY MOVES IT")
    print("  " + "=" * 66)
    base = 100.0 * p / sims
    print("     %-30s %5.1f%%   <- as you are now" % ("nothing", base))
    for mult, lab in ((0.5, "halve position size"), (2.0, "double position size")):
        pp, _, _, _ = simulate([x * mult for x in rs], sims, tgt_r, dd_r, day_r,
                               per_day, seed=11)
        print("     %-30s %5.1f%%" % (lab, 100.0 * pp / sims))
    for add in (5.0, 10.0):
        want = min(0.95, (wr + add) / 100.0)
        sc = [avg_w] * int(round(want * 1000)) + [-avg_l] * int(round((1 - want) * 1000))
        pp, _, _, _ = simulate(sc, sims, tgt_r, dd_r, day_r, per_day, seed=13)
        print("     %-30s %5.1f%%" % ("win rate +%.0f points" % add, 100.0 * pp / sims))
    for mult in (1.25, 1.5):
        pp, _, _, _ = simulate([x * mult if x > 0 else x for x in rs], sims,
                               tgt_r, dd_r, day_r, per_day, seed=17)
        print("     %-30s %5.1f%%" % ("winners %.0f%% bigger" % ((mult - 1) * 100),
                                      100.0 * pp / sims))

    # ---- the efficiency frontier: P(pass) against time, by size ----
    print()
    print("  " + "=" * 66)
    print("  THE EFFICIENCY FRONTIER - P(pass) versus TIME, by position size")
    print("  " + "=" * 66)
    print("  The barriers are fixed in DOLLARS. Position size decides how many R")
    print("  fit inside them, so it decides everything. Smaller size is safer but")
    print("  slower; the question is where the trade stops being worth it.")
    print()
    print("     %-11s %8s %9s %9s %10s %s"
          % ("risk/trade", "daily", "P(pass)", "fail-day", "med trades", "sessions"))
    frontier = []
    for rk in (25, 38, 50, 75, 100, 150, 200, 300, 400):
        t_r, d_r, y_r = target / rk, max_dd / rk, daily / rk
        pp, _fd, fday, tt = simulate(rs, max(4000, sims // 4), t_r, d_r, y_r,
                                     per_day, seed=23)
        n = max(4000, sims // 4)
        tt.sort()
        med = tt[len(tt) // 2]
        pct = 100.0 * pp / n
        sess = med / float(max(1, per_day))
        frontier.append((rk, pct, med, sess))
        print("     $%-10d %7.1fR %8.1f%% %8.1f%% %10d %8.0f"
              % (rk, y_r, pct, 100.0 * fday / n, med, sess))

    # The knee: the largest size still above 90% pass.
    safe = [f for f in frontier if f[1] >= 90.0]
    if safe:
        k = max(safe, key=lambda f: f[0])
        slowest = min(frontier, key=lambda f: f[0])
        print()
        print("     Largest size still above 90%% pass: $%d per trade" % k[0])
        print("        %.1f%% pass, median %d trades, about %.0f sessions"
              % (k[1], k[2], k[3]))
        print("     Smallest size tested: $%d -> %.1f%% pass but %.0f sessions"
              % (slowest[0], slowest[1], slowest[3]))
        if slowest[3] > 0 and k[3] > 0:
            print("        so the larger size resolves about %.1fx faster for %.1f"
                  " points of pass probability"
                  % (slowest[3] / k[3], slowest[1] - k[1]))
        print()
        print("     This is a decision, not a recommendation - it is your money")
        print("     and your risk tolerance. The arithmetic is above.")

    print()
    print("  THE DAILY LIMIT IS THE BARRIER NOBODY MODELS")
    print("     $%s daily cap means this many losing trades ends the day:"
          % format(int(daily), ","))
    for rk in (50, 100, 200, 400):
        print("        $%-4d per trade -> %4.1f losses" % (rk, daily / rk))
    print("     At large size a NORMAL losing day fails the combine outright.")
    print("     That is why most simulated failures above are daily-limit, not")
    print("     drawdown - and drawdown is the one everyone plans for.")

    print()
    print("  Position size scales the walk against FIXED barriers, so it is the")
    print("  strongest lever in both directions. Smaller size means more trades")
    print("  to the target but far more room before the drawdown floor.")
    print()
    print("  HONEST LIMITS")
    print("     trades are resampled INDEPENDENTLY, but real losses cluster in")
    print("     bad regimes, and clustering raises drawdown risk - so treat the")
    print("     pass number as an upper bound rather than a promise.")
    print(SEP)
    print("  Nothing was changed. Arithmetic on your own ledger.")
    print(SEP)


if __name__ == "__main__":
    main()
