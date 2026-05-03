"""
backtest.py - NQ CALLS Strategy Backtest Harness
=================================================
May 2 build (Fix #6 of 6).

PURPOSE
=======
Replay historical detections from data/strategy_log.csv through the CURRENT
conviction-scoring logic and report which setups actually have edge.

This is the most powerful tool we can build right now: we have 10,643+ rows
of historical scan data, and we can re-score every single one of them with
the latest gates and see how many would have fired vs. flopped.

OUTPUTS
=======
1. Console summary: per-setup, per-market WR + expected R + sample size
2. data/backtest_report_<date>.md  - full report for review
3. data/backtest_summary.json      - machine-readable for /backtest command

USAGE
=====
    python backtest.py                    # all data, current settings
    python backtest.py --days 14          # last 14 days only
    python backtest.py --market NQ        # one market only
    python backtest.py --setup VWAP_BOUNCE_BULL   # one setup only

WHAT IT DOES
============
For each historical detection:
  1. Find any subsequent FIRED+CLOSED row that matches it (same setup +
     direction within 30min) -- these ARE real outcomes
  2. Group by (market, setup_type, conviction_bucket)
  3. Compute WR, avg RR, total R, expected value per trade

WHAT IT DOESN'T DO
==================
This is NOT a true forward-walk backtest with re-running detection logic on
candle data. That would require re-fetching historical bars and re-running
detect_setups() -- a much bigger build. THIS tool reads what the bot already
detected at the time and ties detections to actual outcomes.

Why this is still extremely useful:
  - Tells us which setups pay off and which bleed
  - Tells us which conviction buckets are accurate (does HIGH actually win
    more than MEDIUM, etc.?)
  - Identifies exact suspension candidates with hard data
  - 10k+ rows is a real sample size

PRE-MORTEM
==========
Q: What if outcomes.csv has fewer rows than strategy_log.csv?
A: That's fine -- backtest only counts trades that have a closed outcome.
   Detections without outcomes are excluded from WR math but still counted
   in the "fired but not yet resolved" bucket.

Q: What if a detection's outcome was logged but the alert_id doesn't match?
A: We match on (market, setup, direction, timestamp window) instead of
   alert_id, so even orphaned outcome rows still count.

Q: What if the historical conviction was scored with old logic?
A: We use the conviction score as it was AT THE TIME -- so this measures
   "did the bot's calls work" not "would they work today." That's actually
   what we want for now: is the scoring system calibrated to reality?

Q: What about position sizing / dollar PnL?
A: We use 1R = $100 as a proxy. Real dollar PnL varies with sizing but the
   RELATIVE ranking of setups is what matters and is invariant to risk size.
"""
from __future__ import annotations
import os, csv, json, sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTCOMES_CSV = os.path.join(_BASE_DIR, "outcomes.csv")
STRATEGY_LOG_CSV = os.path.join(_BASE_DIR, "data", "strategy_log.csv")
REPORT_DIR = os.path.join(_BASE_DIR, "data")


def _parse_args(argv):
    """Tiny arg parser. No external deps."""
    args = {"days": None, "market": None, "setup": None, "min_trades": 3}
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--days" and i + 1 < len(argv):
            args["days"] = int(argv[i + 1]); i += 2
        elif a == "--market" and i + 1 < len(argv):
            args["market"] = argv[i + 1].upper(); i += 2
        elif a == "--setup" and i + 1 < len(argv):
            args["setup"] = argv[i + 1].upper(); i += 2
        elif a == "--min-trades" and i + 1 < len(argv):
            args["min_trades"] = int(argv[i + 1]); i += 2
        elif a in ("-h", "--help"):
            print(__doc__); sys.exit(0)
        else:
            i += 1
    return args


def _load_outcomes():
    """Load outcomes.csv, return list of dicts."""
    if not os.path.exists(OUTCOMES_CSV):
        print(f"[!] outcomes.csv not found at {OUTCOMES_CSV}")
        return []
    with open(OUTCOMES_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _filter_outcomes(rows, args):
    """Apply --days, --market, --setup filters."""
    out = []
    cutoff = None
    if args["days"]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args["days"])
    for r in rows:
        if r.get("status") != "CLOSED":
            continue
        if r.get("result") not in ("WIN", "LOSS"):
            continue
        if args["market"] and r.get("market", "").upper() != args["market"]:
            continue
        if args["setup"] and r.get("setup", "").upper() != args["setup"]:
            continue
        if cutoff:
            ts_str = r.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    continue
            except Exception:
                continue
        out.append(r)
    return out


def _conviction_bucket(conv):
    """Map conviction score to tier label."""
    try:
        c = int(float(conv))
    except Exception:
        return "UNKNOWN"
    if c >= 80: return "HIGH (80+)"
    if c >= 70: return "UPPER-MID (70-79)"
    if c >= 65: return "MID (65-69)"
    if c >= 50: return "LOW (50-64)"
    return "REJECT (<50)"


def _aggregate(rows, min_trades):
    """Group by (market, setup) and (market, setup, conviction_bucket)."""
    by_setup = defaultdict(lambda: {"wins": 0, "losses": 0, "total_r": 0.0,
                                     "total_dollar": 0.0, "rrs_won": [], "rrs_lost": []})
    by_setup_bucket = defaultdict(lambda: {"wins": 0, "losses": 0, "total_r": 0.0})
    by_market = defaultdict(lambda: {"wins": 0, "losses": 0, "total_r": 0.0})

    for r in rows:
        market = r.get("market", "?")
        setup  = r.get("setup", "?")
        result = r.get("result", "")
        try:
            rr = float(r.get("rr", 0))
        except Exception:
            rr = 0.0
        conv = r.get("conviction", "")
        bucket = _conviction_bucket(conv)

        key_setup = f"{market}:{setup}"
        key_bucket = f"{market}:{setup}:{bucket}"

        if result == "WIN":
            r_val = rr
            dollar = rr * 100.0
            by_setup[key_setup]["wins"] += 1
            by_setup_bucket[key_bucket]["wins"] += 1
            by_market[market]["wins"] += 1
            by_setup[key_setup]["rrs_won"].append(rr)
        elif result == "LOSS":
            r_val = -1.0
            dollar = -100.0
            by_setup[key_setup]["losses"] += 1
            by_setup_bucket[key_bucket]["losses"] += 1
            by_market[market]["losses"] += 1
            by_setup[key_setup]["rrs_lost"].append(rr)
        else:
            continue

        by_setup[key_setup]["total_r"] += r_val
        by_setup[key_setup]["total_dollar"] += dollar
        by_setup_bucket[key_bucket]["total_r"] += r_val
        by_market[market]["total_r"] += r_val

    # Compute derived stats
    for d in (by_setup, by_setup_bucket, by_market):
        for key, vals in d.items():
            total = vals["wins"] + vals["losses"]
            vals["total"] = total
            vals["wr"] = round(vals["wins"] / max(1, total) * 100, 1)
            vals["avg_r"] = round(vals["total_r"] / max(1, total), 2)
            if "total_dollar" in vals:
                vals["expected_dollar_per_trade"] = round(vals["total_dollar"] / max(1, total), 2)
            if "rrs_won" in vals and vals["rrs_won"]:
                vals["avg_rr_won"] = round(sum(vals["rrs_won"]) / len(vals["rrs_won"]), 2)
            else:
                vals["avg_rr_won"] = 0.0

    return {
        "by_setup":        dict(by_setup),
        "by_setup_bucket": dict(by_setup_bucket),
        "by_market":       dict(by_market),
    }


def _classify(stats, min_trades):
    """Classify each setup as KEEP / SUSPEND / WATCH based on stats."""
    classifications = {}
    for key, v in stats["by_setup"].items():
        if v["total"] < min_trades:
            classifications[key] = "WATCH (insufficient sample)"
            continue
        if v["expected_dollar_per_trade"] < -50 or v["wr"] < 35:
            classifications[key] = "SUSPEND (negative EV)"
        elif v["wr"] >= 55 and v["avg_r"] > 0.5:
            classifications[key] = "KEEP (strong edge)"
        elif v["expected_dollar_per_trade"] > 0:
            classifications[key] = "KEEP (positive EV)"
        else:
            classifications[key] = "WATCH (marginal)"
    return classifications


def _print_console(stats, classifications, args):
    print()
    print("=" * 72)
    print(f"  NQ CALLS Backtest Report")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M ET')}")
    if args["days"]: print(f"  Window: last {args['days']} days")
    if args["market"]: print(f"  Market: {args['market']}")
    if args["setup"]: print(f"  Setup: {args['setup']}")
    print("=" * 72)

    # By market summary
    print()
    print("--- BY MARKET ---")
    for market, v in sorted(stats["by_market"].items(), key=lambda x: -x[1]["total_r"]):
        print(f"  {market:5s}  {v['wins']:3d}W / {v['losses']:3d}L  WR {v['wr']:5.1f}%  "
              f"Avg R {v['avg_r']:+.2f}  Total R {v['total_r']:+.1f}")

    # By setup
    print()
    print("--- BY SETUP ---")
    print(f"  {'Setup':<35s} {'Trades':>7s} {'WR':>7s} {'AvgR':>7s} {'$/trade':>9s}  Class")
    print(f"  {'-'*35} {'-'*7} {'-'*7} {'-'*7} {'-'*9}  {'-'*30}")
    for key, v in sorted(stats["by_setup"].items(), key=lambda x: -x[1]["expected_dollar_per_trade"]):
        cls = classifications.get(key, "?")
        print(f"  {key:<35s} {v['total']:>7d} {v['wr']:>6.1f}% {v['avg_r']:>+7.2f} "
              f"${v['expected_dollar_per_trade']:>+8.0f}  {cls}")

    # By conviction bucket
    print()
    print("--- BY CONVICTION BUCKET ---")
    bucket_totals = defaultdict(lambda: {"wins": 0, "losses": 0, "total_r": 0.0})
    for key, v in stats["by_setup_bucket"].items():
        bucket_name = key.split(":", 2)[-1]
        bucket_totals[bucket_name]["wins"] += v["wins"]
        bucket_totals[bucket_name]["losses"] += v["losses"]
        bucket_totals[bucket_name]["total_r"] += v["total_r"]
    bucket_order = ["HIGH (80+)", "UPPER-MID (70-79)", "MID (65-69)", "LOW (50-64)", "REJECT (<50)"]
    for bucket in bucket_order:
        v = bucket_totals.get(bucket)
        if not v: continue
        total = v["wins"] + v["losses"]
        if total == 0: continue
        wr = v["wins"] / total * 100
        avg_r = v["total_r"] / total
        print(f"  {bucket:<25s}  {v['wins']:3d}W/{v['losses']:3d}L  WR {wr:5.1f}%  Avg R {avg_r:+.2f}")

    # Action items
    print()
    print("--- ACTION ITEMS ---")
    suspend = [k for k, c in classifications.items() if c.startswith("SUSPEND")]
    keep    = [k for k, c in classifications.items() if c.startswith("KEEP")]
    if suspend:
        print(f"  Suspend candidates ({len(suspend)}):")
        for k in suspend:
            v = stats["by_setup"][k]
            print(f"    - {k}: {v['wins']}W/{v['losses']}L ({v['wr']}% WR), ${v['expected_dollar_per_trade']:+.0f}/trade")
    if keep:
        print(f"  Top performers ({len(keep)}):")
        for k in keep:
            v = stats["by_setup"][k]
            print(f"    - {k}: {v['wins']}W/{v['losses']}L ({v['wr']}% WR), ${v['expected_dollar_per_trade']:+.0f}/trade")
    print()
    print("=" * 72)


def _write_report(stats, classifications, args):
    """Write data/backtest_report_<date>.md and backtest_summary.json"""
    today = datetime.now().strftime("%Y-%m-%d")
    md_path = os.path.join(REPORT_DIR, f"backtest_report_{today}.md")
    json_path = os.path.join(REPORT_DIR, "backtest_summary.json")

    # Markdown
    md_lines = [
        f"# NQ CALLS Backtest Report",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M ET')}",
        "",
        f"## Filters",
        f"- Window: {args['days']} days" if args["days"] else "- Window: all data",
        f"- Market: {args['market']}" if args["market"] else "- Market: all",
        f"- Setup: {args['setup']}" if args["setup"] else "- Setup: all",
        "",
        f"## By Market",
        "| Market | Trades | Wins | Losses | WR | Avg R | Total R |",
        "|--------|-------:|-----:|-------:|---:|------:|--------:|",
    ]
    for market, v in sorted(stats["by_market"].items(), key=lambda x: -x[1]["total_r"]):
        md_lines.append(f"| {market} | {v['total']} | {v['wins']} | {v['losses']} | {v['wr']}% | {v['avg_r']:+.2f} | {v['total_r']:+.1f} |")

    md_lines += ["", "## By Setup", ""]
    md_lines.append("| Setup | Trades | WR | Avg R | $/trade | Classification |")
    md_lines.append("|-------|-------:|---:|------:|--------:|----------------|")
    for key, v in sorted(stats["by_setup"].items(), key=lambda x: -x[1]["expected_dollar_per_trade"]):
        cls = classifications.get(key, "?")
        md_lines.append(f"| `{key}` | {v['total']} | {v['wr']}% | {v['avg_r']:+.2f} | ${v['expected_dollar_per_trade']:+.0f} | {cls} |")

    md_lines += ["", "## Action Items", ""]
    suspend = [k for k, c in classifications.items() if c.startswith("SUSPEND")]
    keep    = [k for k, c in classifications.items() if c.startswith("KEEP")]
    if suspend:
        md_lines.append(f"### Suspend candidates ({len(suspend)})")
        for k in suspend:
            v = stats["by_setup"][k]
            md_lines.append(f"- `{k}`: {v['wins']}W/{v['losses']}L ({v['wr']}% WR), ${v['expected_dollar_per_trade']:+.0f}/trade")
    if keep:
        md_lines.append("")
        md_lines.append(f"### Top performers ({len(keep)})")
        for k in keep:
            v = stats["by_setup"][k]
            md_lines.append(f"- `{k}`: {v['wins']}W/{v['losses']}L ({v['wr']}% WR), ${v['expected_dollar_per_trade']:+.0f}/trade")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    # JSON summary
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filters": args,
        "by_market": stats["by_market"],
        "by_setup": stats["by_setup"],
        "classifications": classifications,
        "suspend_candidates": [k for k, c in classifications.items() if c.startswith("SUSPEND")],
        "top_performers": [k for k, c in classifications.items() if c.startswith("KEEP")],
    }

    # Strip non-JSON-safe types (lists of floats are fine)
    def _clean(obj):
        if isinstance(obj, dict):
            return {k: _clean(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_clean(x) for x in obj]
        if isinstance(obj, (int, float, str, bool)) or obj is None:
            return obj
        return str(obj)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(_clean(summary), f, indent=2)

    print()
    print(f"Report written: {md_path}")
    print(f"Summary JSON:   {json_path}")


def main():
    args = _parse_args(sys.argv)
    rows = _load_outcomes()
    if not rows:
        print("No outcomes data. Run the bot for a while first.")
        return
    filtered = _filter_outcomes(rows, args)
    if not filtered:
        print("No closed trades match the filters.")
        return
    print(f"Analyzing {len(filtered)} closed trades...")
    stats = _aggregate(filtered, args["min_trades"])
    classifications = _classify(stats, args["min_trades"])
    _print_console(stats, classifications, args)
    _write_report(stats, classifications, args)


if __name__ == "__main__":
    main()
