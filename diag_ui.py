"""
diag_ui.py - READ ONLY diagnostic. Sends nothing, writes nothing, changes nothing.

ANSWERS TWO QUESTIONS
=====================
1. Why does the channel still look the same, when Waves 177 and 179 are live?
2. Why are the wins/losses on every alert wrong or not updating?

Both need the LIVE code and the LIVE data. The copies on the Desktop are from
April and predate every July wave, so nothing can be diagnosed from them - the
fields involved did not exist yet.

It imports the bot's own modules and reports what they actually hold. It opens
no trade, sends no Telegram message, and writes no file.

USAGE (Railway console):
    python diag_ui.py
"""

import os
import sys
import csv
import json
import traceback

SEP = "=" * 74


def section(t):
    print()
    print(SEP)
    print(t)
    print(SEP)


def safe(fn, *a, **k):
    try:
        return fn(*a, **k), None
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


# ----------------------------------------------------------------- 1. routing
def check_routing():
    section("1. WHERE ARE MESSAGES ACTUALLY GOING?")
    pub = os.getenv("CHAT_ID") or ""
    ctl = os.getenv("CONTROL_CHAT_ID") or ""
    print("  CHAT_ID (public)        : %s" % ("SET" if pub else "NOT SET"))
    print("  CONTROL_CHAT_ID (private): %s" % ("SET" if ctl else "NOT SET"))
    if not ctl:
        print()
        print("  >>> THIS IS WHY THE CHANNEL LOOKS THE SAME.")
        print("      Wave 177 routes internal messages to CONTROL_CHAT_ID, but the")
        print("      code reads:  CONTROL_CHAT_ID = os.getenv(...) or CHAT_ID")
        print("      With the variable unset it falls back to the PUBLIC channel,")
        print("      so every internal message still lands there. The split is")
        print("      deployed but dormant - that is the safety design, not a bug.")
        print()
        print("      FIX (no deploy needed): Railway -> Variables ->")
        print("      add CONTROL_CHAT_ID = <your private chat id>, then redeploy.")
    elif ctl == pub:
        print()
        print("  >>> CONTROL_CHAT_ID is set but EQUALS the public channel, so the")
        print("      split still has no effect. They must be different chats.")
    else:
        print()
        print("  >>> Split is ACTIVE. Internal messages go to the private chat.")

    for name in ("MORNING_BRIEF", "ASIA_BRIEF"):
        v = os.getenv(name)
        print("  %-22s: %s" % (name, v if v is not None else "(unset - using default)"))


# --------------------------------------------------------- 2. what a card shows
def check_alert_numbers():
    section("2. THE WIN/LOSS NUMBERS ON AN ALERT")
    try:
        import outcome_tracker as ot
    except Exception as e:
        print("  could not import outcome_tracker: %s" % e)
        return

    # find the performance/scorecard structure without assuming its name
    store = None
    for attr in ("SETUP_PERF", "setup_perf", "PERF", "_PERF", "performance",
                 "SETUP_STATS", "setup_stats"):
        if hasattr(ot, attr):
            v = getattr(ot, attr)
            if isinstance(v, dict) and v:
                store = (attr, v)
                break
    if store is None:
        for attr in dir(ot):
            if attr.startswith("__"):
                continue
            v = getattr(ot, attr, None)
            if isinstance(v, dict) and v:
                k0 = list(v)[0]
                if isinstance(v[k0], dict) and (
                        "wins" in v[k0] or "losses" in v[k0]):
                    store = (attr, v)
                    break
    if store is None:
        print("  no setup scorecard dict found on outcome_tracker.")
        print("  attributes that are dicts:")
        for attr in dir(ot):
            if not attr.startswith("__") and isinstance(getattr(ot, attr, None), dict):
                print("     %s (%d keys)" % (attr, len(getattr(ot, attr))))
        return

    name, perf = store
    print("  scorecard found: outcome_tracker.%s  (%d setups)" % (name, len(perf)))
    keys = set()
    for v in perf.values():
        if isinstance(v, dict):
            keys |= set(v)
    print("  fields present per setup: %s" % ", ".join(sorted(keys)))
    print()
    blended_only = not any(k.startswith("real") for k in keys)
    if blended_only:
        print("  >>> There are NO real_* fields. Every number shown on a card is the")
        print("      BLENDED paper+real counter. That is why the record looks wrong:")
        print("      it is mostly simulated trades, and it moves when no real trade")
        print("      happened.")
    else:
        print("  real_* fields exist, so real and blended can be told apart.")
    print()
    hdr = ("setup", "wins", "losses", "total", "real_w", "real_l", "real_tot")
    print("  %-34s %6s %6s %6s %7s %7s %8s" % hdr)
    rows = 0
    for k in sorted(perf):
        v = perf[k]
        if not isinstance(v, dict):
            continue
        print("  %-34s %6s %6s %6s %7s %7s %8s"
              % (k[:34], v.get("wins", "-"), v.get("losses", "-"),
                 v.get("total", "-"), v.get("real_wins", "-"),
                 v.get("real_losses", "-"), v.get("real_total", "-")))
        rows += 1
        if rows >= 25:
            print("  ... (%d more)" % (len(perf) - rows))
            break


# ------------------------------------------------------- 3. truth from the ledger
def check_ledger_truth():
    section("3. WHAT THE REAL TRADE LEDGER SAYS (the ground truth)")
    base = os.path.dirname(os.path.abspath(__file__))
    paths = [os.path.join(base, "data", "outcomes.csv"),
             os.path.join(base, "outcomes.csv")]
    arch = os.path.join(base, "data", "archive")
    if os.path.isdir(arch):
        for f in sorted(os.listdir(arch)):
            if f.startswith("outcomes_") and f.endswith(".csv"):
                paths.append(os.path.join(arch, f))
    found = [p for p in paths if os.path.exists(p)]
    if not found:
        print("  no outcomes.csv found. Looked in:")
        for p in paths[:2]:
            print("     %s" % p)
        return
    tally = {}
    total_rows = 0
    for p in found:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for row in csv.DictReader(f):
                    total_rows += 1
                    res = (row.get("result") or "").strip().upper()
                    if res not in ("WIN", "LOSS"):
                        continue
                    key = "%s:%s" % (row.get("market", "?"), row.get("setup", "?"))
                    d = tally.setdefault(key, {"WIN": 0, "LOSS": 0})
                    d[res] += 1
        except Exception as e:
            print("  could not read %s: %s" % (os.path.basename(p), e))
    print("  files read: %d,  rows: %d,  setups with closed trades: %d"
          % (len(found), total_rows, len(tally)))
    print()
    print("  %-34s %6s %6s %8s" % ("setup", "WIN", "LOSS", "win%"))
    tw = tl = 0
    for k in sorted(tally, key=lambda x: -(tally[x]["WIN"] + tally[x]["LOSS"]))[:25]:
        w, l = tally[k]["WIN"], tally[k]["LOSS"]
        tw += w; tl += l
        print("  %-34s %6d %6d %7.1f%%"
              % (k[:34], w, l, 100.0 * w / max(1, w + l)))
    print()
    print("  These are REAL closed trades. If the numbers in section 2 are much")
    print("  larger, the cards are showing simulated trades as if they were real.")


# --------------------------------------------------------------- 4. the card itself
def check_card():
    section("4. WHAT A LIVE ALERT CARD LOOKS LIKE RIGHT NOW")
    try:
        import w179_formatters as w
    except Exception as e:
        print("  w179_formatters not importable: %s" % e)
        return
    demo = {"market": "NQ", "setup": "VWAP_BOUNCE_BULL", "side": "LONG",
            "entry": 28306.5, "stop": 28230.0, "target": 28460.0,
            "conviction": 62, "tier": "HIGH", "rr": 2.0}
    for fn in ("format_alert_public", "format_exit_public"):
        f = getattr(w, fn, None)
        if not f:
            print("  %s: not present" % fn)
            continue
        out, err = safe(f, demo)
        print("  --- %s ---" % fn)
        if err:
            print("     ERROR %s" % err)
        elif not out:
            print("     returned nothing (caller falls back to the full card)")
        else:
            for line in str(out).splitlines():
                print("     | %s" % line)


def main():
    print(SEP)
    print("UI DIAGNOSTIC - read only. Nothing is sent, written, or changed.")
    print(SEP)
    for fn in (check_routing, check_alert_numbers, check_ledger_truth, check_card):
        try:
            fn()
        except Exception:
            print("  section failed:")
            print(traceback.format_exc()[-700:])
    print()
    print(SEP)
    print("Paste this whole output back.")
    print(SEP)


if __name__ == "__main__":
    main()
