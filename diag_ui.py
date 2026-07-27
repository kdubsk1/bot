"""
diag_ui.py v2 - READ ONLY. Sends nothing, writes nothing, changes nothing.

v1 answered the routing question (CONTROL_CHAT_ID is NOT SET) and read the real
ledger (415 closed trades across 62 files, 55 setups). It failed on one thing:
it looked for the setup scorecard as a module-level dict on outcome_tracker, and
there isn't one. The only module dicts are HOLD_BY_TIER, LEV_BY_TIER,
MIN_RISK_PCT_BY_MARKET, SETUP_RR_FLOORS, VOLUME_DIRECTION and a dedup cache.

So the performance numbers live somewhere else - a JSON file on disk, or behind
a function. v2 hunts all three places instead of assuming one.

It also checks which of the bot's own modules are actually importable, because
v1 turned up something worse than a wrong number: w179_formatters could not be
imported at all, which means the clean public alert cards from Wave 179 have
never been running.
"""

import os
import sys
import csv
import json
import inspect
import traceback

SEP = "=" * 74
BASE = os.path.dirname(os.path.abspath(__file__))


def section(t):
    print()
    print(SEP)
    print(t)
    print(SEP)


def looks_like_scorecard(obj):
    """A dict of setup -> {wins/losses/...}."""
    if not isinstance(obj, dict) or not obj:
        return False
    for k, v in list(obj.items())[:8]:
        if isinstance(v, dict) and any(
                f in v for f in ("wins", "losses", "total", "win_rate",
                                 "real_wins", "real_total")):
            return True
    return False


def dump_scorecard(label, d, limit=20):
    keys = set()
    for v in d.values():
        if isinstance(v, dict):
            keys |= set(v)
    print("  %s  (%d entries)" % (label, len(d)))
    print("     fields: %s" % ", ".join(sorted(keys)))
    has_real = any(str(k).startswith("real") for k in keys)
    print("     separates real from paper: %s" % ("YES" if has_real else "NO"))
    print()
    print("     %-32s %6s %6s %6s %7s %7s %8s"
          % ("setup", "wins", "losses", "total", "real_w", "real_l", "real_tot"))
    n = 0
    for k in sorted(d):
        v = d[k]
        if not isinstance(v, dict):
            continue
        print("     %-32s %6s %6s %6s %7s %7s %8s"
              % (str(k)[:32], v.get("wins", "-"), v.get("losses", "-"),
                 v.get("total", "-"), v.get("real_wins", "-"),
                 v.get("real_losses", "-"), v.get("real_total", "-")))
        n += 1
        if n >= limit:
            print("     ... (%d more)" % (len(d) - n))
            break
    return has_real


# --------------------------------------------------------------- 1. routing
def check_routing():
    section("1. WHERE ARE MESSAGES ACTUALLY GOING?")
    pub = os.getenv("CHAT_ID") or ""
    ctl = os.getenv("CONTROL_CHAT_ID") or ""
    print("  CHAT_ID (public)         : %s" % ("SET" if pub else "NOT SET"))
    print("  CONTROL_CHAT_ID (private): %s" % ("SET" if ctl else "NOT SET"))
    if not ctl:
        print("  >>> Split is DORMANT. Everything still goes to the public channel.")
        print("      Fix: Railway -> Variables -> CONTROL_CHAT_ID = <private chat id>")
    elif ctl == pub:
        print("  >>> Set, but EQUAL to the public channel - so it has no effect.")
    else:
        print("  >>> Split is ACTIVE.")


# ------------------------------------------------- 2. which modules even load
def check_modules():
    section("2. WHICH OF THE BOT'S MODULES ACTUALLY IMPORT?")
    mods = ["outcome_tracker", "sim_account", "crypto_sim", "strategy_log",
            "data_layer", "regime_classifier", "filter_ledger",
            "learned_overrides", "w179_formatters", "w189_levels",
            "session_projection", "conviction_boosts"]
    missing = []
    for m in mods:
        try:
            __import__(m)
            print("     %-22s OK" % m)
        except Exception as e:
            print("     %-22s FAILED: %s" % (m, e))
            missing.append(m)
    if "w179_formatters" in missing:
        print()
        print("  >>> w179_formatters IS MISSING. This is why your alerts still look")
        print("      the same. Wave 179 built the clean public cards, and the fire")
        print("      site calls format_alert_public() inside a try/except that falls")
        print("      back to the OLD full card when the import fails. It has been")
        print("      silently falling back this whole time.")
    # Wave 198: if the public cards silently fell back, say why.
    try:
        import w179_formatters as _wf
        err = getattr(_wf, "_W179_LAST_ERROR", "n/a")
        print()
        print("  w179_formatters last failure reason: %s" % (err or "none - cards rendering"))
        setup = {"direction": "LONG", "entry": 28306.5, "raw_stop": 28230.0,
                 "type": "VWAP_BOUNCE_BULL"}
        card = _wf.format_alert_public("NQ", "1h", setup, "HIGH", 28460.0, 2.0)
        print("  sample public entry card: %s"
              % ("RENDERS" if card else "RETURNS None -> caller uses the OLD card"))
        if card:
            for line in str(card).splitlines():
                print("     | %s" % line)
    except Exception as e:
        print("  (w179_formatters check skipped: %s)" % e)

    print()
    print("  files present next to bot.py:")
    try:
        for f in sorted(os.listdir(BASE)):
            if f.endswith(".py") and (f.startswith("w1") or f.startswith("diag")):
                print("     %-30s %d bytes" % (f, os.path.getsize(os.path.join(BASE, f))))
    except Exception as e:
        print("     could not list: %s" % e)


# ------------------------------------------------------- 3. hunt the scorecard
def check_scorecard():
    section("3. WHERE DO THE WIN/LOSS NUMBERS ON A CARD COME FROM?")
    found_any = False

    # (a) JSON files on disk
    print("  (a) JSON files under data/")
    ddir = os.path.join(BASE, "data")
    if os.path.isdir(ddir):
        for f in sorted(os.listdir(ddir)):
            if not f.endswith(".json"):
                continue
            p = os.path.join(ddir, f)
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    obj = json.load(fh)
            except Exception:
                continue
            cands = [("", obj)]
            if isinstance(obj, dict):
                for k, v in obj.items():
                    cands.append((k, v))
            for label, cand in cands:
                if looks_like_scorecard(cand):
                    print()
                    found_any = True
                    dump_scorecard("data/%s%s" % (f, ("  ->  " + label) if label else ""), cand)
                    break
        if not found_any:
            print("     (no scorecard-shaped JSON found)")
            for f in sorted(os.listdir(ddir))[:30]:
                print("     %-42s %d bytes" % (f, os.path.getsize(os.path.join(ddir, f))))
    else:
        print("     no data/ directory at %s" % ddir)

    # (b) zero-argument functions on outcome_tracker
    print()
    print("  (b) zero-argument functions on outcome_tracker that return one")
    try:
        import outcome_tracker as ot
    except Exception as e:
        print("     cannot import outcome_tracker: %s" % e)
        return
    names = [n for n in dir(ot)
             if not n.startswith("__") and callable(getattr(ot, n, None))
             and any(w in n.lower() for w in
                     ("perf", "stat", "score", "record", "setup", "load", "get"))]
    for n in names[:40]:
        fn = getattr(ot, n)
        try:
            sig = inspect.signature(fn)
            if any(p.default is inspect.Parameter.empty
                   and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                   for p in sig.parameters.values()):
                continue
        except Exception:
            continue
        try:
            out = fn()
        except Exception:
            continue
        if looks_like_scorecard(out):
            print()
            found_any = True
            dump_scorecard("outcome_tracker.%s()" % n, out)
    if not found_any:
        print("     (none returned a scorecard)")
        print("     zero-arg candidates tried: %s" % ", ".join(names[:20]))


# ------------------------------------------------------------ 4. ground truth
def check_ledger():
    section("4. GROUND TRUTH - REAL CLOSED TRADES")
    paths = [os.path.join(BASE, "data", "outcomes.csv"),
             os.path.join(BASE, "outcomes.csv")]
    arch = os.path.join(BASE, "data", "archive")
    if os.path.isdir(arch):
        for f in sorted(os.listdir(arch)):
            if f.startswith("outcomes_") and f.endswith(".csv"):
                paths.append(os.path.join(arch, f))
    tally, rows = {}, 0
    for p in [x for x in paths if os.path.exists(x)]:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for row in csv.DictReader(f):
                    rows += 1
                    res = (row.get("result") or "").strip().upper()
                    if res not in ("WIN", "LOSS"):
                        continue
                    k = "%s:%s" % (row.get("market", "?"), row.get("setup", "?"))
                    d = tally.setdefault(k, {"WIN": 0, "LOSS": 0})
                    d[res] += 1
        except Exception:
            pass
    tw = sum(v["WIN"] for v in tally.values())
    tl = sum(v["LOSS"] for v in tally.values())
    print("  %d rows, %d setups, %d wins / %d losses overall (%.1f%%)"
          % (rows, len(tally), tw, tl, 100.0 * tw / max(1, tw + tl)))
    print()
    print("  WORST PERFORMERS with 5+ trades - candidates for attention:")
    bad = [(k, v) for k, v in tally.items() if v["WIN"] + v["LOSS"] >= 5]
    bad.sort(key=lambda kv: kv[1]["WIN"] / max(1, kv[1]["WIN"] + kv[1]["LOSS"]))
    for k, v in bad[:10]:
        n = v["WIN"] + v["LOSS"]
        print("     %-32s %2dW %2dL  %5.1f%%  (n=%d)"
              % (k[:32], v["WIN"], v["LOSS"], 100.0 * v["WIN"] / n, n))


def main():
    print(SEP)
    print("UI DIAGNOSTIC v2 - read only. Nothing sent, written, or changed.")
    print(SEP)
    for fn in (check_routing, check_modules, check_scorecard, check_ledger):
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
