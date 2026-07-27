"""
diag_stops.py - READ ONLY. Measures the BREAK_RETEST stop bug on real rows.

WHY MEASURE BEFORE FIXING
=========================
426 rows are known to have the stop on the WRONG SIDE of entry, and every one
is a BREAK_RETEST. Today that family traded again: 0W/3L on NQ. It is also the
prime suspect for trades closing at exactly $0.00, because when stop_pts <= 0
the sizer returns contracts=1 with reason "zero_stop" and the P&L is then
computed off a broken risk basis.

That is a coherent story, but it is still a story. Patching a story is how two
deploys got wasted today. This measures it instead:

  * how many rows really have an inverted stop, by setup and by market
  * WHICH DIRECTION the error goes - stop above entry on longs, below on
    shorts, or both. That single fact points at the fix: a sign flip, a
    long/short branch that was copied wrong, or a level lookup returning the
    opposite side.
  * whether inverted stops line up with the zero-P&L closes, which would
    confirm the two symptoms share one cause
  * what the stop distance looks like when it IS valid, so the fix has a
    target to reproduce

It writes nothing and changes nothing.

USAGE (Railway console):
    python diag_stops.py
"""

import os
import csv
import json
import glob

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
SEP = "=" * 74


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


def load_rows():
    """Every row that carries an entry and a stop, from any log we keep."""
    paths = []
    for p in (os.path.join(DATA, "outcomes.csv"), os.path.join(BASE, "outcomes.csv"),
              os.path.join(DATA, "strategy_log.csv"), os.path.join(BASE, "strategy_log.csv")):
        if os.path.exists(p):
            paths.append(p)
    paths += sorted(glob.glob(os.path.join(DATA, "archive", "outcomes_*.csv")))
    paths += sorted(glob.glob(os.path.join(DATA, "archive", "strategy_log_*.csv")))

    rows, cols = [], set()
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for r in csv.DictReader(f):
                    cols |= set(r)
                    e = _f(r, "entry", "entry_price", "entry_p")
                    s = _f(r, "stop", "raw_stop", "stop_price")
                    if e is None or s is None:
                        continue
                    r["_entry"], r["_stop"] = e, s
                    # strategy_log.py writes "setup_type", not "setup" - v1
                    # looked only for the latter, so every strategy_log row came
                    # back with a blank setup name and the whole attribution was
                    # useless. Read from the source, not from memory.
                    r["_setup"] = _s(r, "setup_type", "setup", "type", "setup_name")
                    r["_decision"] = _s(r, "decision", "status", "action").upper()
                    r["_market"] = _s(r, "market") or "?"
                    r["_side"] = _s(r, "side", "direction").upper()
                    r["_result"] = _s(r, "result").upper()
                    r["_pnl"] = _f(r, "pnl", "PnL", "profit")
                    r["_src"] = os.path.basename(p)
                    rows.append(r)
        except Exception:
            continue
    return rows, sorted(cols), paths


def is_long(r):
    s = r["_side"]
    if "LONG" in s or "BULL" in s:
        return True
    if "SHORT" in s or "BEAR" in s:
        return False
    return "BULL" in (r["_setup"] or "").upper()


def main():
    print(SEP)
    print("STOP PLACEMENT DIAGNOSTIC - read only")
    print(SEP)
    rows, cols, paths = load_rows()
    print("  files read : %d" % len(paths))
    print("  rows with entry AND stop : %d" % len(rows))
    if not rows:
        print("  Nothing to measure - no row carries both an entry and a stop.")
        print("  columns seen: %s" % ", ".join(cols))
        print(SEP)
        return

    # THE question: are the broken rows real trades, or scan noise?
    # strategy_log records every SCAN, most of which never fire. A zero-risk
    # stop on a rejected scan costs nothing. On a FIRED row it costs money.
    fired = [r for r in rows if "FIRED" in (r.get("_decision") or "")]
    print()
    print("  rows marked FIRED : %d" % len(fired))
    print("  everything else   : %d  (scans, rejects, shadows)" % (len(rows) - len(fired)))

    bad, good, zero_risk = [], [], []
    for r in rows:
        e, s = r["_entry"], r["_stop"]
        if e == s:
            zero_risk.append(r)
        elif (s > e) if is_long(r) else (s < e):
            bad.append(r)
        else:
            good.append(r)

    def split(rs):
        b = z = g = 0
        for r in rs:
            if r["_entry"] == r["_stop"]:
                z += 1
            elif (r["_stop"] > r["_entry"]) if is_long(r) else (r["_stop"] < r["_entry"]):
                b += 1
            else:
                g += 1
        return g, b, z

    if fired:
        fg, fb, fz = split(fired)
        n = len(fired)
        print()
        print("  " + "=" * 62)
        print("  FIRED ROWS ONLY - these are the ones that cost real money")
        print("  " + "=" * 62)
        print("     valid stop        : %d  (%.1f%%)" % (fg, 100.0 * fg / n))
        print("     INVERTED stop     : %d  (%.1f%%)" % (fb, 100.0 * fb / n))
        print("     ZERO RISK         : %d  (%.1f%%)" % (fz, 100.0 * fz / n))
        if fz + fb == 0:
            print("     >>> EVERY FIRED TRADE HAS A VALID STOP.")
            print("         The broken rows are scan/reject noise and cost nothing.")
        else:
            print("     >>> %d fired trades went out with a broken stop." % (fz + fb))
            print("         Those are real money. This is the fix that matters.")
        bysetup = {}
        for r in fired:
            if r["_entry"] == r["_stop"] or (
                    (r["_stop"] > r["_entry"]) if is_long(r) else (r["_stop"] < r["_entry"])):
                k = "%s:%s" % (r["_market"], r["_setup"] or "?")
                bysetup[k] = bysetup.get(k, 0) + 1
        if bysetup:
            print()
            print("     which FIRED setups:")
            for k in sorted(bysetup, key=lambda x: -bysetup[x])[:12]:
                print("        %-34s %d" % (k[:34], bysetup[k]))

    print()
    print("  ALL ROWS (scans included)")
    print("  valid stop        : %d" % len(good))
    print("  INVERTED stop     : %d   (stop on the wrong side of entry)" % len(bad))
    print("  zero risk (s==e)  : %d" % len(zero_risk))

    if not bad and not zero_risk:
        print()
        print("  No inverted stops in the data available here.")
        print(SEP)
        return

    # ---- which setups ----
    by_setup = {}
    for r in bad + zero_risk:
        k = "%s:%s" % (r["_market"], r["_setup"])
        by_setup[k] = by_setup.get(k, 0) + 1
    print()
    print("  WHICH SETUPS ARE AFFECTED")
    for k in sorted(by_setup, key=lambda x: -by_setup[x])[:15]:
        print("     %-34s %d" % (k[:34], by_setup[k]))
    fams = set(k.split(":")[1].replace("_BULL", "").replace("_BEAR", "")
               for k in by_setup if ":" in k)
    print()
    print("     distinct setup families affected: %s" % ", ".join(sorted(fams)))
    if len(fams) == 1:
        print("     >>> ONE family only. That points at that setup's own stop")
        print("         calculation, not at shared sizing or logging code.")

    # ---- WHICH DIRECTION - this is the fix pointer ----
    long_bad = sum(1 for r in bad if is_long(r))
    short_bad = len(bad) - long_bad
    long_all = sum(1 for r in rows if is_long(r))
    short_all = len(rows) - long_all
    print()
    print("  WHICH DIRECTION IS BROKEN  (this is what names the fix)")
    print("     LONGs  with stop ABOVE entry : %d of %d (%.1f%%)"
          % (long_bad, long_all, 100.0 * long_bad / max(1, long_all)))
    print("     SHORTs with stop BELOW entry : %d of %d (%.1f%%)"
          % (short_bad, short_all, 100.0 * short_bad / max(1, short_all)))
    if long_bad and not short_bad:
        print("     >>> LONGS ONLY. The long branch has the sign the wrong way,")
        print("         or is reusing the short branch's level.")
    elif short_bad and not long_bad:
        print("     >>> SHORTS ONLY. The short branch has the sign the wrong way.")
    elif long_bad and short_bad:
        print("     >>> BOTH sides. That is one shared expression, not two")
        print("         branches - most likely entry and stop swapped, or a")
        print("         level lookup returning the opposite side.")

    # ---- do inverted stops explain the zero-PnL closes? ----
    closed_bad = [r for r in bad + zero_risk if r["_result"] in ("WIN", "LOSS")]
    closed_good = [r for r in good if r["_result"] in ("WIN", "LOSS")]
    def zero_rate(rs):
        withp = [r for r in rs if r["_pnl"] is not None]
        if not withp:
            return None, 0
        z = sum(1 for r in withp if abs(r["_pnl"]) < 0.005)
        return 100.0 * z / len(withp), len(withp)
    zb, nb = zero_rate(closed_bad)
    zg, ng = zero_rate(closed_good)
    print()
    print("  DO INVERTED STOPS EXPLAIN THE $0.00 CLOSES?")
    if zb is None or zg is None:
        print("     not enough closed rows carrying a P&L to compare")
    else:
        print("     closed with INVERTED stop : %.1f%% closed at exactly $0  (n=%d)" % (zb, nb))
        print("     closed with valid stop    : %.1f%% closed at exactly $0  (n=%d)" % (zg, ng))
        if zb > zg + 20:
            print("     >>> YES. The zero-P&L closes are concentrated in the broken")
            print("         rows. One root cause, two symptoms.")
        elif zb <= zg + 5:
            print("     >>> NO. Zero-P&L closes happen at a similar rate either way,")
            print("         so that is a SEPARATE bug and needs its own fix.")

    # ---- what a healthy stop looks like, so the fix has a target ----
    print()
    print("  WHAT A VALID STOP LOOKS LIKE  (the fix should reproduce this)")
    per_mkt = {}
    for r in good:
        d = abs(r["_entry"] - r["_stop"])
        if r["_entry"]:
            per_mkt.setdefault(r["_market"], []).append(100.0 * d / abs(r["_entry"]))
    for m in sorted(per_mkt):
        v = sorted(per_mkt[m])
        if v:
            print("     %-5s median stop distance %.3f%% of price   (n=%d)"
                  % (m, v[len(v) // 2], len(v)))

    print()
    print("  EXAMPLES OF BROKEN ROWS")
    for r in (bad + zero_risk)[:6]:
        print("     %-4s %-24s %-5s entry %-12.4f stop %-12.4f  %s"
              % (r["_market"], (r["_setup"] or "?")[:24],
                 "LONG" if is_long(r) else "SHORT",
                 r["_entry"], r["_stop"], r["_src"]))
    print(SEP)
    print("  Nothing was changed. Paste this back.")
    print(SEP)


if __name__ == "__main__":
    main()
