"""
ui_selftest.py

Wave 180: UI SELF-TEST  (read-only - proves what actually works)

WHY
===
The bot exposes 38 slash commands and 24 menu buttons. Revamping that surface
without first knowing which parts are broken, empty, or quietly printing
placeholder text would be guesswork. This tool calls every message-building
function the bot has and reports, per builder:

    OK          produced real-looking output
    SUSPECT     produced output, but it looks like placeholder / no-data text
    EMPTY       returned nothing
    ERROR       raised (with the exception)
    SKIPPED     needs arguments this harness cannot safely supply

"Real-looking" is judged conservatively: output must contain digits, must not be
dominated by phrases like "unavailable", "no data", "not tracked", "N/A", and
must not be almost entirely zeroes. A builder that renders beautifully but shows
0.0% everywhere is reported as SUSPECT, because that is exactly the failure that
hides in a pretty UI.

SAFETY
======
Read-only. It imports modules, calls builders, and writes ONE report file:
data/ui_selftest_report.json. It sends no Telegram message, opens no trade,
writes no counter and changes no gate. Importing bot.py is safe because its
startup is guarded by `if __name__ == "__main__"`.

Every call is individually wrapped, so one broken builder cannot stop the sweep.

USAGE
=====
    python ui_selftest.py
    python ui_selftest.py --verbose      # also print the first lines of output
"""

import os
import sys
import json
import inspect
import traceback
from datetime import datetime, timezone

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(_BASE_DIR, "data")
REPORT    = os.path.join(DATA_DIR, "ui_selftest_report.json")

# Phrases that mean "this rendered, but there is nothing behind it".
_PLACEHOLDER_HINTS = (
    "unavailable", "no data", "not tracked", "n/a", "none yet", "nothing yet",
    "no trades", "no setups", "not enough", "coming soon", "todo", "error",
)

# (module, attribute, args, kwargs, note)
TARGETS = [
    ("outcome_tracker", "build_daily_report",     (), {}, "daily report"),
    ("outcome_tracker", "get_suspension_report",  (), {}, "suspended setups board"),
    ("sim_account",     "sim_status_text",        (), {}, "/simstatus"),
    ("sim_account",     "lifetime_stats_text",    (), {}, "/lifetime"),
    ("sim_account",     "eval_progression_text",  (), {}, "/eval progression"),
    ("sim_account",     "eval_trend_text",        (), {}, "/eval trend"),
    ("sim_account",     "get_edge_summary",       (), {}, "/edge"),
    ("sim_account",     "get_period_summary",     (), {}, "period summary"),
    ("crypto_sim",      "get_crypto_status_text", (), {}, "/cryptostatus"),
    ("strategy_log",    "build_strategy_analysis",(), {}, "strategy analysis"),
    ("bot",             "build_morning_brief",    (), {}, "morning brief (PUBLIC)"),
    ("bot",             "build_asia_brief",       (), {}, "asia brief (PUBLIC)"),
    ("bot",             "build_startup_state",    (), {}, "startup state"),
    ("bot",             "_build_status_text",     (), {}, "/status"),
    ("bot",             "analyze_market_bias",    ("NQ",), {}, "bias engine NQ"),
    ("bot",             "analyze_market_bias",    ("GC",), {}, "bias engine GC"),
    ("bot",             "analyze_market_bias",    ("BTC",), {}, "bias engine BTC"),
    ("bot",             "analyze_market_bias",    ("SOL",), {}, "bias engine SOL"),
    ("filter_ledger",   "build_ledger_text",      (), {}, "/ledger"),
    ("learned_overrides", "status_text",          (), {}, "/overrides"),
]


def _stringify(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        parts = [p for p in v if isinstance(p, str)]
        if parts:
            return "\n".join(parts)
        return json.dumps(v, default=str)[:4000]
    if isinstance(v, dict):
        return json.dumps(v, default=str)[:4000]
    return str(v)


def _judge(text):
    """Classify output as OK / SUSPECT / EMPTY, conservatively."""
    t = (text or "").strip()
    if not t:
        return "EMPTY", "returned nothing"
    low = t.lower()
    hits = [h for h in _PLACEHOLDER_HINTS if h in low]
    digits = sum(1 for c in t if c.isdigit())
    if digits == 0:
        return "SUSPECT", "no digits at all - nothing measured"
    # count how much of the numeric content is zero
    import re
    nums = re.findall(r"-?\d+\.?\d*", t)
    zeros = sum(1 for n in nums if float(n or 0) == 0)
    if nums and zeros / len(nums) > 0.85:
        return "SUSPECT", "%d of %d numbers are zero" % (zeros, len(nums))
    if hits and len(t) < 400:
        return "SUSPECT", "placeholder wording: %s" % ", ".join(hits[:3])
    return "OK", "%d chars, %d numbers" % (len(t), len(nums))


def run(verbose=False):
    results = []
    modcache = {}
    for modname, attr, args, kwargs, note in TARGETS:
        entry = {"module": modname, "function": attr, "note": note,
                 "args": [str(a) for a in args]}
        try:
            if modname not in modcache:
                modcache[modname] = __import__(modname)
            mod = modcache[modname]
        except Exception as e:
            entry.update(status="ERROR", detail="import failed: %s" % e)
            results.append(entry)
            continue

        fn = getattr(mod, attr, None)
        if fn is None:
            entry.update(status="MISSING", detail="function does not exist")
            results.append(entry)
            continue
        if not callable(fn):
            entry.update(status="MISSING", detail="attribute is not callable")
            results.append(entry)
            continue

        # refuse to call anything needing arguments we were not given
        try:
            sig = inspect.signature(fn)
            required = [p for p in sig.parameters.values()
                        if p.default is inspect.Parameter.empty
                        and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
            if len(required) > len(args):
                entry.update(status="SKIPPED",
                             detail="needs %d arg(s): %s"
                                    % (len(required), ", ".join(p.name for p in required)))
                results.append(entry)
                continue
        except (TypeError, ValueError):
            pass

        try:
            out = fn(*args, **kwargs)
            text = _stringify(out)
            status, detail = _judge(text)
            entry.update(status=status, detail=detail, chars=len(text))
            if verbose:
                entry["preview"] = "\n".join(text.splitlines()[:6])
        except Exception as e:
            entry.update(status="ERROR",
                         detail="%s: %s" % (type(e).__name__, e),
                         traceback=traceback.format_exc()[-800:])
        results.append(entry)

    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": counts,
        "results": results,
        "note": "read-only: no Telegram message sent, no trade opened, no counter written",
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(REPORT, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
    except Exception as e:
        print("WARNING: could not write report: %s" % e)

    print("=" * 74)
    print("UI SELF-TEST  -  what actually works")
    print("=" * 74)
    order = {"ERROR": 0, "MISSING": 1, "SUSPECT": 2, "EMPTY": 3, "SKIPPED": 4, "OK": 5}
    for r in sorted(results, key=lambda x: (order.get(x["status"], 9), x["module"])):
        arg = ("(%s)" % ",".join(r["args"])) if r["args"] else ""
        print("  %-8s %-18s %-24s %s"
              % (r["status"], r["module"], r["function"] + arg, r["detail"][:60]))
        if verbose and r.get("preview"):
            for line in r["preview"].splitlines():
                print("             | %s" % line[:80])
    print()
    print("  " + "  ".join("%s=%d" % (k, v) for k, v in sorted(counts.items())))
    print("  report: %s" % REPORT)
    print("=" * 74)
    print("  ERROR/MISSING = broken. SUSPECT = renders but the data behind it")
    print("  looks empty or all-zero. Those two groups are the revamp list.")
    print("=" * 74)
    return report


def main(argv=None):
    argv = argv or sys.argv[1:]
    return run(verbose="--verbose" in argv)


if __name__ == "__main__":
    main()
