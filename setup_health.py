"""
setup_health.py

EVERY SETUP, GRADED ON WHAT ACTUALLY MATTERS - and it keeps grading, forever.

WHY NOT WIN RATE
================
Wayne asked to "fix the win rate" of the bad setups. Win rate is the wrong
target, and using it would destroy good setups. Breakeven win rate is not 50% -
it depends entirely on payoff:

    at 1.5R average win, breakeven is 40.0%
    at 2.0R average win, breakeven is 33.3%
    at 3.0R average win, breakeven is 25.0%
    at 4.0R average win, breakeven is 20.0%

From this bot's own history: BTC:BREAK_RETEST_BEAR ran 22.2% at 3.82R, which is
+0.070R per trade - PROFITABLE. Killing it for a low win rate would have thrown
away a winner. Meanwhile SOL:VWAP_BOUNCE_BULL ran 15.4% at 1.82R = -0.566R and
is genuinely bad. Win rate cannot tell those two apart. Expectancy can:

    E = win_rate x avg_win_R - (1 - win_rate) x avg_loss_R

So every setup is ranked on expectancy, with a confidence interval, and the
verdict accounts for how thin the sample is.

IT DIAGNOSES, IT DOES NOT JUST CONDEMN
======================================
A negative setup is not automatically a bad idea - it is often a bad
IMPLEMENTATION. So for each one this reports the things known to break setups in
this codebase:

  * invalid stops - stop on the wrong side of entry. 426 such rows exist and
    every one is a BREAK_RETEST. Note BTC:BREAK_RETEST_BULL is 0W/7L and
    SOL:BREAK_RETEST_BEAR is 0W/7L. That is a prime suspect, not a coincidence.
  * target too far - rr4+ measured -0.753R against rr<2 at +0.304R.
  * session - US Regular measured +0.540R against London at -0.760R.

Fixing the cause beats benching the setup, which is the standing instruction
here: make them better, do not put them away.

IT NEVER DECIDES ANYTHING
=========================
This is a REPORT. It changes no gate, disables no setup, writes no counter. It
recommends, and a human decides. The one file it writes is an APPEND-ONLY
history, so the effect of every later change is measurable against it.

USAGE
=====
    python setup_health.py
    python setup_health.py --min-trades 8
    python setup_health.py --verbose
"""

import os
import sys
import csv
import json
import math
import random
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
HISTORY = os.path.join(DATA, "setup_health_history.jsonl")
REPORT = os.path.join(DATA, "setup_health_report.json")

# Columns this tool will look for, in preference order.
COL_R = ["r_multiple", "r_realised", "rr_realised", "realised_r", "r", "R"]
COL_PNL = ["pnl", "PnL", "profit", "net", "dollars"]
COL_ENTRY = ["entry", "entry_price", "entry_p"]
COL_STOP = ["stop", "stop_price", "raw_stop"]
COL_EXIT = ["exit", "exit_price", "exit_p"]
COL_TARGET = ["target", "target_price", "tp"]
COL_SESSION = ["session", "session_label", "sess"]

_MIN_TRADES = 5          # below this, no verdict is offered at all
_BOOTSTRAP = 2000


def _f(row, names):
    for n in names:
        if n in row and str(row[n]).strip() not in ("", "None", "nan"):
            try:
                return float(row[n])
            except Exception:
                continue
    return None


def _s(row, names):
    for n in names:
        if n in row and str(row[n]).strip():
            return str(row[n]).strip()
    return None


def load_ledger():
    """Every real closed trade, from outcomes.csv and every archive."""
    paths = [os.path.join(DATA, "outcomes.csv"), os.path.join(BASE, "outcomes.csv")]
    arch = os.path.join(DATA, "archive")
    if os.path.isdir(arch):
        for f in sorted(os.listdir(arch)):
            if f.startswith("outcomes_") and f.endswith(".csv"):
                paths.append(os.path.join(arch, f))
    trades, cols, files = [], set(), 0
    for p in paths:
        if not os.path.exists(p):
            continue
        files += 1
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                for row in csv.DictReader(fh):
                    cols |= set(row)
                    res = (row.get("result") or "").strip().upper()
                    if res not in ("WIN", "LOSS"):
                        continue
                    trades.append(row)
        except Exception:
            continue
    return trades, sorted(cols), files


def r_of(row):
    """
    The trade's result in R - risk multiples. Tries hardest evidence first.

    Returns (r, how) or (None, reason). R is what makes setups comparable across
    markets: 1R on NQ and 1R on SOL mean the same thing to the account, where
    points and dollars do not.
    """
    r = _f(row, COL_R)
    if r is not None:
        return r, "column"
    entry, stop, exit_p = (_f(row, COL_ENTRY), _f(row, COL_STOP), _f(row, COL_EXIT))
    if None not in (entry, stop, exit_p):
        risk = abs(entry - stop)
        if risk > 0:
            side = (_s(row, ["side", "direction"]) or "").upper()
            gain = (exit_p - entry) if "LONG" in side or "BULL" in side else (entry - exit_p)
            return gain / risk, "from entry/stop/exit"
        return None, "zero risk (stop == entry)"
    return None, "no R column and no entry/stop/exit"


def invalid_stop(row):
    """Stop on the WRONG SIDE of entry - the known BREAK_RETEST corruption."""
    entry, stop = _f(row, COL_ENTRY), _f(row, COL_STOP)
    if None in (entry, stop):
        return None
    side = (_s(row, ["side", "direction"]) or "").upper()
    is_long = "LONG" in side or "BULL" in side
    if entry == stop:
        return True
    return (stop > entry) if is_long else (stop < entry)


def boot_ci(vals, iters=_BOOTSTRAP, conf=0.95):
    """Bootstrap CI for the mean. Used because R distributions are skewed -
    a few big wins and many small losses - so the normal approximation
    understates the uncertainty on small samples."""
    if len(vals) < 2:
        return (None, None)
    rnd = random.Random(12345)
    means = []
    n = len(vals)
    for _ in range(iters):
        means.append(sum(vals[rnd.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[int((1 - conf) / 2 * iters)]
    hi = means[int((1 - (1 - conf) / 2) * iters) - 1]
    return (lo, hi)


def grade(setup_rows, min_trades=_MIN_TRADES):
    rs, hows, bad_stops, sessions, targets = [], {}, 0, {}, []
    wins = losses = 0
    for row in setup_rows:
        res = (row.get("result") or "").upper()
        if res == "WIN":
            wins += 1
        else:
            losses += 1
        r, how = r_of(row)
        hows[how] = hows.get(how, 0) + 1
        if r is not None:
            rs.append(r)
        bs = invalid_stop(row)
        if bs:
            bad_stops += 1
        sess = _s(row, COL_SESSION) or "unknown"
        d = sessions.setdefault(sess, [0, 0])
        d[0 if res == "WIN" else 1] += 1
        entry, stop, tgt = _f(row, COL_ENTRY), _f(row, COL_STOP), _f(row, COL_TARGET)
        if None not in (entry, stop, tgt) and abs(entry - stop) > 0:
            targets.append(abs(tgt - entry) / abs(entry - stop))

    n = wins + losses
    out = {"n": n, "wins": wins, "losses": losses,
           "win_rate": round(100.0 * wins / max(1, n), 1),
           "r_available": len(rs), "r_sources": hows,
           "invalid_stops": bad_stops,
           "invalid_stop_pct": round(100.0 * bad_stops / max(1, n), 1),
           "sessions": {k: {"W": v[0], "L": v[1]} for k, v in sessions.items()},
           "median_target_rr": round(sorted(targets)[len(targets) // 2], 2) if targets else None}

    if rs:
        wr = [x for x in rs if x > 0]
        lr = [-x for x in rs if x <= 0]
        out["expectancy_r"] = round(sum(rs) / len(rs), 3)
        out["avg_win_r"] = round(sum(wr) / len(wr), 2) if wr else None
        out["avg_loss_r"] = round(sum(lr) / len(lr), 2) if lr else None
        if out["avg_win_r"]:
            out["breakeven_wr"] = round(100.0 / (1.0 + out["avg_win_r"]), 1)
        lo, hi = boot_ci(rs)
        out["exp_ci_low"] = round(lo, 3) if lo is not None else None
        out["exp_ci_high"] = round(hi, 3) if hi is not None else None
    else:
        out["expectancy_r"] = None

    # ---- verdict ----
    e, lo, hi = out.get("expectancy_r"), out.get("exp_ci_low"), out.get("exp_ci_high")
    if n < min_trades:
        out["verdict"] = "TOO THIN"
        out["why"] = "only %d closed trades - no verdict is honest yet" % n
    elif e is None:
        out["verdict"] = "UNMEASURABLE"
        out["why"] = ("no R multiple available on any trade (%s) - cannot judge "
                      "without knowing risk taken" % ", ".join(hows))
    elif bad_stops and out["invalid_stop_pct"] >= 50:
        out["verdict"] = "FIX THE STOPS FIRST"
        out["why"] = ("%.0f%% of these trades have the stop on the WRONG SIDE of "
                      "entry. The result is meaningless until that is fixed - the "
                      "setup has never been tested properly."
                      % out["invalid_stop_pct"])
    elif hi is not None and hi < 0:
        out["verdict"] = "LOSING - REAL"
        out["why"] = ("expectancy %+.3fR and the whole 95%% interval is below zero "
                      "(%+.3f to %+.3f). This is not a thin-sample artefact."
                      % (e, lo, hi))
    elif lo is not None and lo > 0:
        out["verdict"] = "PROFITABLE - REAL"
        out["why"] = ("expectancy %+.3fR with the whole 95%% interval above zero "
                      "(%+.3f to %+.3f)." % (e, lo, hi))
    elif e < 0:
        out["verdict"] = "LOSING - UNPROVEN"
        out["why"] = ("expectancy %+.3fR but the interval straddles zero "
                      "(%+.3f to %+.3f) - more trades needed before acting."
                      % (e, lo or 0, hi or 0))
    else:
        out["verdict"] = "POSITIVE - UNPROVEN"
        out["why"] = ("expectancy %+.3fR, interval straddles zero (%+.3f to %+.3f)."
                      % (e, lo or 0, hi or 0))

    # ---- what to try, rather than just benching it ----
    fixes = []
    if bad_stops:
        fixes.append("%d trade(s) have an invalid stop - fix the stop calculation"
                     % bad_stops)
    if out.get("median_target_rr") and out["median_target_rr"] >= 4:
        fixes.append("median target is %.1fR - measured rr4+ runs -0.753R; cap nearer 2-3R"
                     % out["median_target_rr"])
    good = [(k, v) for k, v in out["sessions"].items()
            if v["W"] + v["L"] >= 3 and v["W"] / max(1, v["W"] + v["L"]) >= 0.5]
    poor = [(k, v) for k, v in out["sessions"].items()
            if v["W"] + v["L"] >= 3 and v["W"] == 0]
    if good and poor:
        fixes.append("works in %s, never in %s - gate by session"
                     % (", ".join(k for k, _ in good), ", ".join(k for k, _ in poor)))
    out["suggested_fixes"] = fixes
    return out


def run(min_trades=_MIN_TRADES, verbose=False):
    trades, cols, files = load_ledger()
    print("=" * 78)
    print("SETUP HEALTH - every setup graded on EXPECTANCY, not win rate")
    print("=" * 78)
    print("  %d closed trades from %d file(s)" % (len(trades), files))
    print("  columns available: %s" % ", ".join(cols))
    if not trades:
        print("  no closed trades found - nothing to grade.")
        return None

    by = {}
    for t in trades:
        k = "%s:%s" % (t.get("market", "?"), t.get("setup", "?"))
        by.setdefault(k, []).append(t)

    graded = {k: grade(v, min_trades) for k, v in by.items()}

    order = {"FIX THE STOPS FIRST": 0, "LOSING - REAL": 1, "LOSING - UNPROVEN": 2,
             "UNMEASURABLE": 3, "TOO THIN": 4, "POSITIVE - UNPROVEN": 5,
             "PROFITABLE - REAL": 6}
    print()
    print("  %-30s %4s %6s %9s %16s  %s"
          % ("setup", "n", "WR", "expect", "95% interval", "verdict"))
    print("  " + "-" * 92)
    for k in sorted(graded, key=lambda x: (order.get(graded[x]["verdict"], 9),
                                           graded[x].get("expectancy_r") or 0)):
        g = graded[k]
        e = g.get("expectancy_r")
        ci = ("%+.2f to %+.2f" % (g["exp_ci_low"], g["exp_ci_high"])
              if g.get("exp_ci_low") is not None else "-")
        print("  %-30s %4d %5.1f%% %9s %16s  %s"
              % (k[:30], g["n"], g["win_rate"],
                 ("%+.3fR" % e) if e is not None else "-", ci, g["verdict"]))

    print()
    print("=" * 78)
    print("WHAT TO DO ABOUT IT")
    print("=" * 78)
    acted = 0
    for k in sorted(graded, key=lambda x: order.get(graded[x]["verdict"], 9)):
        g = graded[k]
        if g["verdict"] in ("PROFITABLE - REAL", "TOO THIN"):
            continue
        acted += 1
        print()
        print("  %s   [%s]" % (k, g["verdict"]))
        print("     %s" % g["why"])
        for f in g.get("suggested_fixes", []):
            print("     -> %s" % f)
        if not g.get("suggested_fixes"):
            print("     -> no implementation cause found; the edge itself is the problem")
        if acted >= 15:
            print()
            print("  (showing the worst 15)")
            break

    stars = [k for k in graded if graded[k]["verdict"] == "PROFITABLE - REAL"]
    if stars:
        print()
        print("  PROVEN PROFITABLE - protect these, do not touch them:")
        for k in sorted(stars, key=lambda x: -(graded[x].get("expectancy_r") or 0)):
            g = graded[k]
            print("     %-30s %+.3fR  (n=%d, %.0f%% WR, avg win %.1fR)"
                  % (k[:30], g["expectancy_r"], g["n"], g["win_rate"],
                     g.get("avg_win_r") or 0))

    # ---- append-only history, so improvement is measurable later ----
    try:
        os.makedirs(DATA, exist_ok=True)
        rec = {"at": datetime.now(timezone.utc).isoformat(),
               "trades": len(trades),
               "setups": {k: {"n": v["n"], "expectancy_r": v.get("expectancy_r"),
                              "verdict": v["verdict"]} for k, v in graded.items()}}
        with open(HISTORY, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        with open(REPORT, "w", encoding="utf-8") as f:
            json.dump({"generated_at": rec["at"], "columns": cols,
                       "trades": len(trades), "setups": graded}, f, indent=2)
        print()
        print("  history appended: %s  (never overwritten)" % HISTORY)
        print("  full report     : %s" % REPORT)
    except Exception as e:
        print("  WARNING: could not write report: %s" % e)

    print("=" * 78)
    print("  Nothing was changed. No gate moved, no setup disabled.")
    print("=" * 78)
    return graded


def main(argv=None):
    argv = argv or sys.argv[1:]
    mt = _MIN_TRADES
    if "--min-trades" in argv:
        try:
            mt = int(argv[argv.index("--min-trades") + 1])
        except Exception:
            pass
    return run(min_trades=mt, verbose="--verbose" in argv)


if __name__ == "__main__":
    main()
