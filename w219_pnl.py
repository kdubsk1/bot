"""
w219_pnl.py - the running record, for the alert cards.

WHY THIS READS R AND NOT DOLLARS
================================
outcomes.csv has no dollar P&L column, and that is deliberate - outcome_tracker
says so at line 654: "outcomes.csv doesn't store $ pnl directly (it stores
RR)". A previous tool asked for a "pnl" column, found 379 trades with none, and
reported it as catastrophic data loss. It was the wrong question.

So R is computed the honest way, from entry/stop/exit_price - the actual exit
against the actual risk - falling back to the planned rr only when an exit is
missing.

R is the right unit for a card anyway. 1R on NQ and 1R on GC mean the same
thing to the account; dollars do not, and the bot never sees the real account
balance. The dollar figure shown is at a stated base, so it scales.

NO UNDERSCORES IN THE OUTPUT
============================
The card escapes markdown for Telegram. Rather than guess at the exact escape
the caller applies, every string here avoids underscores entirely, so it is
safe whether or not it gets escaped.

SPEED
=====
This runs on every alert, so results are cached against the file's size and
mtime. A cold read of a few hundred rows is trivial; doing it hundreds of times
a day for no reason is not.
"""

import os
import csv
import glob
import time as _time

_CACHE = {"key": None, "line": "", "line_at": 0.0}


def _f(row, *names):
    for n in names:
        v = row.get(n)
        if v not in (None, "", "None", "nan"):
            try:
                return float(v)
            except Exception:
                continue
    return None


def _paths(base):
    out = []
    for p in (os.path.join(base, "data", "outcomes.csv"),
              os.path.join(base, "outcomes.csv")):
        if os.path.exists(p):
            out.append(p)
    out += sorted(glob.glob(os.path.join(base, "data", "archive", "outcomes_*.csv")))
    return out


def _key(paths):
    sig = []
    for p in paths:
        try:
            st = os.stat(p)
            sig.append((p, st.st_size, int(st.st_mtime)))
        except Exception:
            pass
    return tuple(sig)


def load(base):
    """Every closed trade as (R, day). Never raises."""
    rows = []
    for p in _paths(base):
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                for r in csv.DictReader(fh):
                    res = (r.get("result") or "").strip().upper()
                    if res not in ("WIN", "LOSS"):
                        continue
                    e = _f(r, "entry")
                    s = _f(r, "stop", "raw stop", "raw_stop")
                    x = _f(r, "exit_price", "exit")
                    rr = _f(r, "rr")
                    side = (r.get("direction") or r.get("side") or "").upper()
                    lng = "LONG" in side or "BULL" in side
                    val = None
                    if None not in (e, s, x) and abs(e - s) > 0:
                        val = ((x - e) if lng else (e - x)) / abs(e - s)
                    elif rr is not None:
                        val = float(rr) if res == "WIN" else -1.0
                    if val is None or abs(val) > 50:
                        continue
                    rows.append((val, (r.get("timestamp") or "")[:10], res == "WIN"))
        except Exception:
            continue
    return rows


def pnl_line(base, dollars_per_r=100.0, recent=20):
    """
    One compact line for the card. Returns "" rather than raising, ever.

        RECORD  102W-277L 26.9%  -37.5R  |  last 20: 6W-14L -2.1R  |  $100/R = -$3750
    """
    try:
        # Cheap gate first. Statting 60+ archive files on every alert measured
        # at 2ms a call, which is absurd for a line of text that changes a
        # couple of times a day. Re-check at most once a minute; between
        # checks, hand back the cached string immediately.
        now = _time.time()
        if _CACHE["line_at"] and (now - _CACHE["line_at"]) < 60.0:
            return _CACHE["line"]
        paths = _paths(base)
        if not paths:
            return ""
        k = _key(paths)
        _CACHE["line_at"] = now
        if k == _CACHE["key"]:
            return _CACHE["line"]
        rows = load(base)
        if len(rows) < 5:
            _CACHE["key"], _CACHE["line"] = k, ""
            return ""
        n = len(rows)
        w = sum(1 for r in rows if r[2])
        tot = sum(r[0] for r in rows)
        tail = rows[-recent:] if len(rows) > recent else rows
        tw = sum(1 for r in tail if r[2])
        tt = sum(r[0] for r in tail)
        cash = int(round(tot * dollars_per_r))
        money = "%s$%s" % ("-" if cash < 0 else "+", "{:,}".format(abs(cash)))
        line = ("RECORD  %dW-%dL %.0f%%  %+.1fR   |   last %d: %dW-%dL %+.1fR   |   "
                "%s at $%d/R" % (
                    w, n - w, 100.0 * w / n, tot,
                    len(tail), tw, len(tail) - tw, tt,
                    money, int(dollars_per_r)))
        _CACHE["key"], _CACHE["line"] = k, line
        return line
    except Exception:
        return ""


if __name__ == "__main__":
    import sys
    here = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    print(pnl_line(here) or "(not enough closed trades yet)")
