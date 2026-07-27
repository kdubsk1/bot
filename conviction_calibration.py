"""
conviction_calibration.py - can the conviction score be turned into a real
percentage? READ ONLY.

WHAT WAYNE ASKED FOR
====================
A per-trade number: "this trade has a 62% chance". Not the setup's average - the
chance for THIS trade, from its own conditions.

That number can only come from the conviction score, because that is the only
thing the bot computes per trade. So the whole question is:

    DOES A HIGHER CONVICTION SCORE ACTUALLY WIN MORE OFTEN?

Nobody has ever checked. If a conviction of 70 and a conviction of 45 win at the
same rate, then any percentage built from conviction is decoration - a number
that looks precise and means nothing. Putting that in front of paying
subscribers would be the single most damaging thing this bot could do, because
they would size trades on it.

So this measures it, and the answer is allowed to be no.

WHAT IT TESTS
=============
1. MONOTONICITY. Sort trades into conviction buckets and check the win rate
   actually climbs. A score that predicts should climb; noise wanders.

2. SIGNIFICANCE. Compare the top third against the bottom third with a
   two-proportion z test. The bar is 95% confidence. A gap that could plausibly
   be luck does not qualify.

3. CALIBRATION. If it passes, the printed percentage per bucket IS the measured
   win rate of that bucket - not the conviction score rescaled. So when the card
   says 62%, it means "trades that looked like this won 62% of the time",
   which is a claim that can be checked.

WHAT HAPPENS WITH THE ANSWER
============================
PASS -> a calibration map is written and the entry card can show a genuine
        per-trade percentage.
FAIL -> no map is written, and the card keeps showing the setup's own record.
        That is not a failure of this tool; it is the tool doing its job.

Read-only: writes at most one report file, changes no gate and no trading logic.

USAGE:
    python conviction_calibration.py
    python conviction_calibration.py --buckets 4
"""

import os
import sys
import csv
import json
import math
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
MAP_FILE = os.path.join(DATA, "conviction_calibration.json")

CONV_COLS = ["conviction", "conviction_score", "score", "confidence"]
_MIN_TOTAL = 40          # below this, no honest test is possible
_MIN_PER_BUCKET = 12


def _num(row, names):
    for n in names:
        if n in row and str(row[n]).strip() not in ("", "None", "nan"):
            try:
                return float(row[n])
            except Exception:
                continue
    return None


def load():
    paths = [os.path.join(DATA, "outcomes.csv"), os.path.join(BASE, "outcomes.csv")]
    arch = os.path.join(DATA, "archive")
    if os.path.isdir(arch):
        for f in sorted(os.listdir(arch)):
            if f.startswith("outcomes_") and f.endswith(".csv"):
                paths.append(os.path.join(arch, f))
    rows, cols = [], set()
    for p in paths:
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                for r in csv.DictReader(fh):
                    cols |= set(r)
                    res = (r.get("result") or "").strip().upper()
                    if res not in ("WIN", "LOSS"):
                        continue
                    c = _num(r, CONV_COLS)
                    if c is None:
                        continue
                    rows.append((c, 1 if res == "WIN" else 0))
        except Exception:
            continue
    return rows, sorted(cols)


def two_prop_z(w1, n1, w2, n2):
    if n1 == 0 or n2 == 0:
        return 0.0
    p1, p2 = w1 / float(n1), w2 / float(n2)
    p = (w1 + w2) / float(n1 + n2)
    se = math.sqrt(max(1e-12, p * (1 - p) * (1.0 / n1 + 1.0 / n2)))
    return (p1 - p2) / se if se > 0 else 0.0


def run(nbuckets=3):
    rows, cols = load()
    print("=" * 74)
    print("CONVICTION CALIBRATION - can it become a real percentage?")
    print("=" * 74)
    if not rows:
        print("  No trades carry a conviction score.")
        print("  Columns found in the ledger: %s" % (", ".join(cols) or "(none)"))
        print()
        print("  Looked for: %s" % ", ".join(CONV_COLS))
        print()
        print("  VERDICT: cannot test. The per-trade percentage is not possible")
        print("  until conviction is recorded alongside each closed trade.")
        print("  The entry card keeps showing the setup's own record, which is")
        print("  measured and honest.")
        print("=" * 74)
        return None

    rows.sort(key=lambda x: x[0])
    n = len(rows)
    print("  %d closed trades carry a conviction score" % n)
    print("  conviction range: %.0f to %.0f" % (rows[0][0], rows[-1][0]))
    if n < _MIN_TOTAL:
        print()
        print("  VERDICT: TOO FEW (%d, need %d). No honest test yet." % (n, _MIN_TOTAL))
        print("  The card keeps using the setup's own record.")
        print("=" * 74)
        return None

    size = n // nbuckets
    buckets = []
    for i in range(nbuckets):
        chunk = rows[i * size:(i + 1) * size] if i < nbuckets - 1 else rows[i * size:]
        if not chunk:
            continue
        w = sum(x[1] for x in chunk)
        buckets.append({"lo": chunk[0][0], "hi": chunk[-1][0], "n": len(chunk),
                        "wins": w, "win_rate": round(100.0 * w / len(chunk), 1)})

    print()
    print("  %-18s %6s %6s %8s" % ("conviction", "n", "wins", "win rate"))
    for b in buckets:
        bar = "#" * int(b["win_rate"] / 4)
        print("  %5.0f - %-10.0f %6d %6d %7.1f%%  %s"
              % (b["lo"], b["hi"], b["n"], b["wins"], b["win_rate"], bar))

    thin = [b for b in buckets if b["n"] < _MIN_PER_BUCKET]
    rates = [b["win_rate"] for b in buckets]
    monotonic = all(rates[i] <= rates[i + 1] for i in range(len(rates) - 1))
    lo_b, hi_b = buckets[0], buckets[-1]
    z = two_prop_z(hi_b["wins"], hi_b["n"], lo_b["wins"], lo_b["n"])

    print()
    print("  climbs with conviction : %s" % ("YES" if monotonic else "NO - it wanders"))
    print("  top vs bottom bucket   : %.1f%% vs %.1f%%  (z = %.2f, need 1.96)"
          % (hi_b["win_rate"], lo_b["win_rate"], z))
    if thin:
        print("  WARNING: %d bucket(s) below %d trades" % (len(thin), _MIN_PER_BUCKET))

    passed = monotonic and z >= 1.96 and not thin
    print()
    print("=" * 74)
    if passed:
        print("  VERDICT: PASS. Conviction genuinely predicts.")
        print()
        print("  The entry card can now show a per-trade percentage, and the")
        print("  number shown is the MEASURED win rate of trades in that")
        print("  conviction band - not the score rescaled. When it says %d%%,"
              % hi_b["win_rate"])
        print("  it means trades that looked like this won %d%% of the time."
              % hi_b["win_rate"])
        try:
            os.makedirs(DATA, exist_ok=True)
            with open(MAP_FILE, "w", encoding="utf-8") as f:
                json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
                           "trades": n, "z": round(z, 3), "buckets": buckets},
                          f, indent=2)
            print()
            print("  map written: %s" % MAP_FILE)
        except Exception as e:
            print("  WARNING: could not write map: %s" % e)
    else:
        why = []
        if not monotonic:
            why.append("the win rate does not climb with conviction")
        if z < 1.96:
            why.append("the top-vs-bottom gap is within noise (z=%.2f)" % z)
        if thin:
            why.append("some buckets are too thin to judge")
        print("  VERDICT: FAIL - %s." % "; and ".join(why))
        print()
        print("  Conviction does NOT currently carry enough information to")
        print("  become a percentage. No map is written, and the entry card")
        print("  keeps showing the setup's own measured record.")
        print()
        print("  This is the tool working, not failing. A per-trade number")
        print("  built on a score that does not predict would look precise")
        print("  and mean nothing - and people would size trades on it.")
    print("=" * 74)
    return {"passed": passed, "z": z, "buckets": buckets}


def main(argv=None):
    argv = argv or sys.argv[1:]
    nb = 3
    if "--buckets" in argv:
        try:
            nb = max(2, int(argv[argv.index("--buckets") + 1]))
        except Exception:
            pass
    return run(nbuckets=nb)


if __name__ == "__main__":
    main()
