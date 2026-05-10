"""
backtest_pro.py - NQ CALLS Per-Market Deep Dive Backtest Tool
==============================================================
Wave 23 (May 9, 2026)

WHAT THIS DOES:
    Mines two data sources to build per-market actionable reports:
      1. outcomes.csv       - real fired trades + their outcomes
      2. strategy_log.csv   - ALL scan decisions (10k+ rows) including
                              REJECTED/ALMOST setups with WOULD_WIN/WOULD_LOSE
                              tags showing what would have happened if fired.

    Combined, this tells us TWO things:
      - Real bot performance (WR, EV, $/trade per market+setup)
      - What we MISSED: setups filtered out that would have won.

    The missed-winner analysis is the killer feature. Example finding from
    May 5 data: GC had 83 missed VWAP_BOUNCE_BULL winners at 100% WR,
    blocked by ADX < 18 and RR < 2.5 thresholds that are too tight for GC.

WHY THIS BEATS THE EXISTING backtest.py:
    The existing tool only sees FIRED+CLOSED trades. It can tell us how
    fired setups perform, but not what we're filtering OUT. backtest_pro
    looks at the rejected pool too. That's where the optimization gold is.

USAGE:
    python backtest_pro.py --market NQ              # NQ deep dive (last 30d default)
    python backtest_pro.py --market GC --days 60    # GC last 60d
    python backtest_pro.py --all                    # all markets summary
    python backtest_pro.py --market BTC --setup VWAP_BOUNCE_BULL  # one setup

OUTPUTS:
    Console: human-readable summary
    File:    data/backtest_pro_<MARKET>_<DATE>.md
    JSON:    data/backtest_pro_summary.json

PRE-MORTEM:
    Q1: What if strategy_log.csv has rows with conviction=0?
    A:  Those failed before scoring (e.g. volume data missing). We exclude
        them from conviction-threshold sweeps but include them in
        reject-reason analysis.

    Q2: What if WOULD_WIN was logged early and the move reversed later?
    A:  WOULD_WIN/WOULD_LOSE is checked once when target or stop hits.
        That's a real outcome, not a snapshot. Trustworthy.

    Q3: What if the same setup signal appears multiple times before
        firing (or being rejected)?
    A:  Each row is a separate scan decision. Counting them all gives a
        slightly noisy view but is conservative — our tool reports
        "this many decisions" not "this many unique setups."

    Q4: What's the unit of EV reporting?
    A:  Same as backtest.py: 1R = $100 proxy. Real dollar varies with
        sizing but RELATIVE ranking of setups is what matters.

    Q5: Could classifications conflict between this tool and the
        existing backtest.py?
    A:  Possibly, since they look at different data slices. backtest_pro
        outputs a DIFFERENT report type (per-market deep dive). Wayne
        should use both: backtest.py for fired-only stats, backtest_pro
        for "what should we change" analysis.
"""
from __future__ import annotations
import os, sys, csv, json
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
from typing import Optional, Iterable

# ============================================================
# Paths & constants
# ============================================================

_BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
OUTCOMES_CSV     = os.path.join(_BASE_DIR, "outcomes.csv")
STRATEGY_LOG_CSV = os.path.join(_BASE_DIR, "data", "strategy_log.csv")
REPORT_DIR       = os.path.join(_BASE_DIR, "data")

VALID_MARKETS = ("NQ", "GC", "BTC", "SOL")

# Conviction thresholds we'll sweep when looking for optimal MIN_CONVICTION
CONVICTION_SWEEP = [50, 55, 60, 62, 65, 68, 70, 72, 75, 80]

# Decisions counted as "filtered out" (would have been actionable but were not)
# DETECTED is when a setup matched but scored too low or RR/ADX failed.
# REJECTED is the explicit reject. ALMOST is just under threshold.
# WATCH (Wave 14+) is anticipatory and never fired - excluded.
FILTERED_DECISIONS = {"REJECTED", "ALMOST", "DETECTED"}


# ============================================================
# CLI
# ============================================================

def _parse_args(argv):
    args = {
        "days":       30,
        "market":     None,
        "setup":      None,
        "min_trades": 3,
        "all":        False,
    }
    i = 1
    while i < len(argv):
        a = argv[i]
        if   a == "--days"       and i+1 < len(argv): args["days"]   = int(argv[i+1]); i += 2
        elif a == "--market"     and i+1 < len(argv): args["market"] = argv[i+1].upper(); i += 2
        elif a == "--setup"      and i+1 < len(argv): args["setup"]  = argv[i+1].upper(); i += 2
        elif a == "--min-trades" and i+1 < len(argv): args["min_trades"] = int(argv[i+1]); i += 2
        elif a == "--all":                            args["all"]    = True; i += 1
        elif a in ("-h", "--help"):                   print(__doc__); sys.exit(0)
        else:                                         i += 1
    return args


# ============================================================
# Loaders
# ============================================================

def _load_csv(path: str) -> list:
    if not os.path.exists(path):
        print(f"[!] missing: {path}")
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _to_dt(ts: str):
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _filter_window(rows: list, days: Optional[int], market: Optional[str], setup: Optional[str]):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    out = []
    for r in rows:
        if market:
            if r.get("market", "").upper() != market:
                continue
        if setup:
            stp = r.get("setup_type") or r.get("setup", "")
            if stp.upper() != setup:
                continue
        if cutoff:
            dt = _to_dt(r.get("timestamp", ""))
            if dt is None or dt < cutoff:
                continue
        out.append(r)
    return out


def _safe_float(v, default=0.0):
    try:
        return float(v) if v not in (None, "", "nan") else default
    except Exception:
        return default


def _safe_int(v, default=0):
    try:
        return int(float(v)) if v not in (None, "", "nan") else default
    except Exception:
        return default


# ============================================================
# Real fired-trade analysis (from outcomes.csv)
# ============================================================

def _analyze_real_trades(outcomes: list, market: Optional[str]) -> dict:
    """Compute real WR/EV/$ from CLOSED rows in outcomes.csv."""
    by_setup = defaultdict(lambda: {"wins": 0, "losses": 0, "skips": 0, "total_r": 0.0})
    total = {"wins": 0, "losses": 0, "skips": 0, "total_r": 0.0, "fired": 0}

    for r in outcomes:
        if market and r.get("market", "").upper() != market:
            continue
        if r.get("status") != "CLOSED":
            continue
        result = r.get("result", "")
        if result not in ("WIN", "LOSS", "SKIP"):
            continue

        setup = r.get("setup", "?")
        rr    = _safe_float(r.get("rr", 0))
        total["fired"] += 1

        if result == "SKIP":
            by_setup[setup]["skips"] += 1
            total["skips"] += 1
            continue

        if result == "WIN":
            by_setup[setup]["wins"] += 1
            by_setup[setup]["total_r"] += rr
            total["wins"]   += 1
            total["total_r"] += rr
        else:  # LOSS
            by_setup[setup]["losses"] += 1
            by_setup[setup]["total_r"] -= 1.0
            total["losses"] += 1
            total["total_r"] -= 1.0

    # Derived stats
    for s, v in by_setup.items():
        decided = v["wins"] + v["losses"]
        v["decided"]    = decided
        v["wr"]         = round(v["wins"] / max(1, decided) * 100, 1)
        v["avg_r"]      = round(v["total_r"] / max(1, decided), 2)
        v["dollar_per_trade"] = round(v["total_r"] / max(1, decided) * 100, 0)

    decided = total["wins"] + total["losses"]
    total["decided"]   = decided
    total["wr"]        = round(total["wins"] / max(1, decided) * 100, 1)
    total["avg_r"]     = round(total["total_r"] / max(1, decided), 2)
    total["est_dollar"]= round(total["total_r"] * 100, 0)

    return {"total": total, "by_setup": dict(by_setup)}


# ============================================================
# Missed-winners analysis (from strategy_log.csv)
# ============================================================

def _analyze_missed_winners(decisions: list) -> dict:
    """For filtered (REJECTED/ALMOST/DETECTED) rows that have WOULD_WIN/WOULD_LOSE,
    figure out what gates we should loosen."""
    by_setup     = defaultdict(lambda: {"would_win": 0, "would_lose": 0})
    by_reason    = Counter()
    by_reason_w  = Counter()  # reasons attached to WOULD_WIN
    by_reason_l  = Counter()  # reasons attached to WOULD_LOSE
    by_hour      = defaultdict(lambda: {"would_win": 0, "would_lose": 0})
    convs_won    = []
    convs_lost   = []
    rrs_won      = []

    for r in decisions:
        decision = r.get("decision", "")
        if decision not in FILTERED_DECISIONS:
            continue
        result = r.get("result", "")
        if result not in ("WOULD_WIN", "WOULD_LOSE"):
            continue

        setup = r.get("setup_type") or r.get("setup", "?")
        reason = r.get("reject_reason", "").strip()
        conv   = _safe_int(r.get("conviction", 0))
        rr     = _safe_float(r.get("rr", 0))
        dt     = _to_dt(r.get("timestamp", ""))
        hour   = dt.hour if dt else -1

        if result == "WOULD_WIN":
            by_setup[setup]["would_win"]  += 1
            if reason: by_reason_w[reason] += 1
            convs_won.append(conv)
            rrs_won.append(rr)
            by_hour[hour]["would_win"] += 1
        else:
            by_setup[setup]["would_lose"] += 1
            if reason: by_reason_l[reason] += 1
            convs_lost.append(conv)
            by_hour[hour]["would_lose"] += 1

        if reason: by_reason[reason] += 1

    # Derived: WR per setup, EV per setup
    for s, v in by_setup.items():
        total = v["would_win"] + v["would_lose"]
        v["total"] = total
        v["would_wr"] = round(v["would_win"] / max(1, total) * 100, 1)
        v["est_r"]   = v["would_win"] * 2.0 - v["would_lose"]  # rough proxy
        v["est_dollar"] = round(v["est_r"] * 100, 0)

    return {
        "by_setup":      dict(by_setup),
        "reasons":       by_reason,
        "reasons_won":   by_reason_w,
        "reasons_lost":  by_reason_l,
        "convs_won":     convs_won,
        "convs_lost":    convs_lost,
        "rrs_won":       rrs_won,
        "by_hour":       dict(by_hour),
    }


# ============================================================
# Threshold sweep — what if MIN_CONVICTION were N?
# ============================================================

def _sweep_conviction(decisions: list) -> list:
    """For each candidate threshold, count: of all decisions with WOULD_WIN/LOSE,
    how many would have fired and what's the resulting WR."""
    rows_with_outcome = [
        r for r in decisions
        if r.get("result") in ("WOULD_WIN", "WOULD_LOSE", "WIN", "LOSS")
        and _safe_int(r.get("conviction", 0)) > 0
    ]
    out = []
    for thresh in CONVICTION_SWEEP:
        wins = sum(1 for r in rows_with_outcome
                   if _safe_int(r.get("conviction", 0)) >= thresh
                   and r.get("result") in ("WOULD_WIN", "WIN"))
        losses = sum(1 for r in rows_with_outcome
                     if _safe_int(r.get("conviction", 0)) >= thresh
                     and r.get("result") in ("WOULD_LOSE", "LOSS"))
        total = wins + losses
        wr   = round(wins / max(1, total) * 100, 1)
        ev_r = wins * 2.0 - losses  # rough proxy at avg 2R per win
        out.append({
            "threshold": thresh, "fires": total,
            "wins": wins, "losses": losses, "wr": wr, "ev_r": ev_r,
        })
    return out


# ============================================================
# Recommendations engine
# ============================================================

def _build_recommendations(market: str, real: dict, missed: dict,
                            sweep: list) -> list:
    """Generate concrete config-change recommendations."""
    recs = []

    # Recommendation 1: Top missed-winner setups (>= 5 wins, >= 60% WR)
    big_misses = [
        (s, v) for s, v in missed["by_setup"].items()
        if v["would_win"] >= 5 and v["would_wr"] >= 60
    ]
    big_misses.sort(key=lambda x: -x[1]["would_win"])
    for setup, v in big_misses[:5]:
        recs.append({
            "type":     "loosen_filter",
            "priority": "HIGH",
            "setup":    setup,
            "detail":   f"{v['would_win']} missed wins, {v['would_wr']}% WR — filters too strict",
        })

    # Recommendation 2: Top reject reasons attached to WOULD_WINs
    top_killers = missed["reasons_won"].most_common(5)
    for reason, count in top_killers:
        if count >= 5:
            recs.append({
                "type":     "investigate_gate",
                "priority": "HIGH" if count >= 15 else "MEDIUM",
                "reason":   reason[:120],
                "detail":   f"This gate killed {count} winners on {market}",
            })

    # Recommendation 3: Optimal conviction threshold
    valid_sweep = [s for s in sweep if s["fires"] >= 5]
    if valid_sweep:
        best_ev = max(valid_sweep, key=lambda x: x["ev_r"])
        recs.append({
            "type":     "set_threshold",
            "priority": "MEDIUM",
            "field":    "MIN_CONVICTION",
            "value":    best_ev["threshold"],
            "detail":   (f"Best EV at threshold {best_ev['threshold']}: "
                         f"{best_ev['fires']} fires, {best_ev['wr']}% WR, +{best_ev['ev_r']:.0f}R"),
        })

    # Recommendation 4: Real-trade laggards
    for setup, v in real["by_setup"].items():
        if v["decided"] >= 3 and v["wr"] < 30:
            recs.append({
                "type":     "suspend_setup",
                "priority": "HIGH",
                "setup":    setup,
                "detail":   f"{v['wins']}W/{v['losses']}L ({v['wr']}% WR) — losing money",
            })

    # Recommendation 5: Best-performing real setups (KEEP these)
    for setup, v in real["by_setup"].items():
        if v["decided"] >= 3 and v["wr"] >= 60 and v["avg_r"] >= 0.5:
            recs.append({
                "type":     "keep_setup",
                "priority": "INFO",
                "setup":    setup,
                "detail":   f"{v['wins']}W/{v['losses']}L ({v['wr']}% WR), +{v['avg_r']:.2f}R/trade — top performer",
            })

    return recs


# ============================================================
# Output: console + markdown + json
# ============================================================

def _print_console(market: str, real: dict, missed: dict, sweep: list,
                    recs: list, args: dict):
    print()
    print("=" * 78)
    print(f"  Backtest Pro — {market or 'ALL MARKETS'}")
    print(f"  Window: last {args['days']} days  |  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M ET')}")
    print("=" * 78)

    t = real["total"]
    print()
    print(f"REAL TRADES (from outcomes.csv)")
    print(f"  Fired: {t['fired']}  |  Decided: {t['decided']} ({t['wins']}W/{t['losses']}L, {t['skips']} skips)")
    print(f"  WR: {t['wr']}%  |  Avg R: {t['avg_r']:+.2f}  |  Total R: {t['total_r']:+.1f}  |  Est $: ${t['est_dollar']:+,.0f}")

    # Wave 23: alpha-at-risk summary
    big_misses_count = sum(v["would_win"]  for v in missed["by_setup"].values())
    big_loses_count  = sum(v["would_lose"] for v in missed["by_setup"].values())
    overall_would_wr = round(big_misses_count / max(1, big_misses_count + big_loses_count) * 100, 1)
    est_alpha_r = sum(v["est_r"] for v in missed["by_setup"].values() if v["would_wr"] >= 60 and v["would_win"] >= 5)
    print()
    print(f"ALPHA AT RISK (filtered setups never fired)")
    print(f"  Missed: {big_misses_count} would-have-won, {big_loses_count} would-have-lost ({overall_would_wr}% WR on rejects)")
    print(f"  Recoverable alpha (high-WR setups only): est +{est_alpha_r:.0f}R = ~${est_alpha_r*100:+,.0f}")

    # Real perf by setup
    if real["by_setup"]:
        print()
        print("  By setup (decided trades only):")
        for setup, v in sorted(real["by_setup"].items(), key=lambda x: -x[1]["dollar_per_trade"]):
            if v["decided"] == 0:
                continue
            print(f"    {setup:30s}  {v['wins']:3d}W/{v['losses']:3d}L ({v['wr']:5.1f}% WR)  "
                  f"avgR {v['avg_r']:+.2f}  ${v['dollar_per_trade']:+.0f}/trade")

    # Missed winners
    print()
    print("MISSED WINNERS (filtered setups that WOULD have won)")
    big_misses = [(s, v) for s, v in missed["by_setup"].items() if v["total"] >= 3]
    if not big_misses:
        print("  No filtered setups with sufficient WOULD_WIN/LOSE data.")
    else:
        big_misses.sort(key=lambda x: -x[1]["would_win"])
        for setup, v in big_misses[:10]:
            print(f"  {setup:30s}  {v['would_win']:3d}W/{v['would_lose']:3d}L "
                  f"({v['would_wr']:5.1f}% WR on filtered)  est R: {v['est_r']:+.0f}")

    # Top killers
    print()
    print("TOP REJECT REASONS THAT KILLED WINS")
    for reason, count in missed["reasons_won"].most_common(8):
        if reason and count >= 3:
            print(f"  {count:3d}x  {reason[:90]}")

    # Conviction sweep
    print()
    print("CONVICTION THRESHOLD SWEEP")
    print(f"  {'Threshold':>10s} {'Fires':>7s} {'Wins':>6s} {'Losses':>7s} {'WR':>7s} {'EV(R)':>8s}")
    for s in sweep:
        if s["fires"] > 0:
            mark = " <-- best EV" if s["ev_r"] == max(x["ev_r"] for x in sweep if x["fires"] >= 5) else ""
            print(f"  {s['threshold']:>10d} {s['fires']:>7d} {s['wins']:>6d} {s['losses']:>7d} "
                  f"{s['wr']:>6.1f}% {s['ev_r']:>+7.1f}R{mark}")

    # Recommendations
    print()
    print("RECOMMENDATIONS")
    if not recs:
        print("  No actionable recommendations from this dataset.")
    for i, r in enumerate(recs, 1):
        prio = r.get("priority", "INFO")
        typ  = r.get("type", "?")
        extra = ""
        if "setup" in r:
            extra = f" {r['setup']}"
        elif "field" in r:
            extra = f" {r['field']}={r.get('value', '?')}"
        elif "reason" in r:
            extra = f" '{r['reason'][:50]}...'"
        print(f"  [{prio:6s}] {typ:20s}{extra}")
        print(f"            {r.get('detail', '')}")

    print()
    print("=" * 78)


def _write_markdown(market: str, real: dict, missed: dict, sweep: list,
                     recs: list, args: dict) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    suffix = market or "ALL"
    md_path = os.path.join(REPORT_DIR, f"backtest_pro_{suffix}_{today}.md")

    lines = []
    lines.append(f"# Backtest Pro Report — {market or 'ALL MARKETS'}")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M ET')}")
    lines.append(f"Window: last {args['days']} days")
    lines.append("")
    lines.append("## Executive Summary")
    t = real["total"]
    lines.append(f"- **Real fired**: {t['fired']} setups, {t['decided']} decided "
                 f"({t['wins']}W / {t['losses']}L, {t['wr']}% WR)")
    lines.append(f"- **Real R**: {t['total_r']:+.1f} total, {t['avg_r']:+.2f} avg/trade")
    lines.append(f"- **Est dollar**: ${t['est_dollar']:+,.0f} (1R = $100 proxy)")
    big_misses_count = sum(v["would_win"] for v in missed["by_setup"].values())
    big_loses_count  = sum(v["would_lose"] for v in missed["by_setup"].values())
    overall_would_wr = round(big_misses_count / max(1, big_misses_count + big_loses_count) * 100, 1)
    lines.append(f"- **Filtered-out winners**: {big_misses_count} would-have-won, "
                 f"{big_loses_count} would-have-lost ({overall_would_wr}% WR on rejects)")
    lines.append("")

    # Real perf
    lines.append("## Real Trade Performance")
    lines.append("| Setup | Wins | Losses | WR | AvgR | $/trade |")
    lines.append("|-------|-----:|-------:|---:|-----:|--------:|")
    for setup, v in sorted(real["by_setup"].items(), key=lambda x: -x[1]["dollar_per_trade"]):
        if v["decided"] == 0:
            continue
        lines.append(f"| `{setup}` | {v['wins']} | {v['losses']} | {v['wr']}% | "
                     f"{v['avg_r']:+.2f} | ${v['dollar_per_trade']:+.0f} |")
    lines.append("")

    # Missed winners
    lines.append("## Missed Winners (filtered setups that would have won)")
    if not missed["by_setup"]:
        lines.append("_No data._")
    else:
        lines.append("| Setup | Would-Win | Would-Lose | WR | Est R |")
        lines.append("|-------|----------:|-----------:|---:|------:|")
        ranked = sorted(missed["by_setup"].items(), key=lambda x: -x[1]["would_win"])
        for setup, v in ranked:
            if v["total"] < 3:
                continue
            lines.append(f"| `{setup}` | {v['would_win']} | {v['would_lose']} | "
                         f"{v['would_wr']}% | {v['est_r']:+.0f} |")
    lines.append("")

    # Reject reasons that killed wins
    lines.append("## Top Reject Reasons That Killed Winners")
    if missed["reasons_won"]:
        lines.append("| Count | Reject Reason |")
        lines.append("|------:|---------------|")
        for reason, count in missed["reasons_won"].most_common(15):
            if reason and count >= 2:
                clean_reason = reason.replace("|", "\\|").replace("\n", " ")
                lines.append(f"| {count} | {clean_reason[:120]} |")
    else:
        lines.append("_No reject-reason data._")
    lines.append("")

    # Conviction sweep
    lines.append("## Conviction Threshold Sweep")
    lines.append("What if `MIN_CONVICTION` were set to N? (Based on actual WOULD_WIN/LOSE data)")
    lines.append("")
    lines.append("| Threshold | Fires | Wins | Losses | WR | EV (R) |")
    lines.append("|----------:|------:|-----:|-------:|---:|-------:|")
    best_ev = max((s["ev_r"] for s in sweep if s["fires"] >= 5), default=None)
    for s in sweep:
        if s["fires"] == 0:
            continue
        marker = " ⭐" if best_ev is not None and s["ev_r"] == best_ev else ""
        lines.append(f"| {s['threshold']} | {s['fires']} | {s['wins']} | {s['losses']} | "
                     f"{s['wr']}% | {s['ev_r']:+.1f}R{marker} |")
    lines.append("")

    # Hour-of-day
    if missed["by_hour"]:
        lines.append("## Hour-of-Day WR (UTC) on Filtered Setups")
        lines.append("| Hour | Would-Win | Would-Lose | WR |")
        lines.append("|-----:|----------:|-----------:|---:|")
        for h in range(24):
            v = missed["by_hour"].get(h)
            if not v:
                continue
            tot = v["would_win"] + v["would_lose"]
            if tot < 3:
                continue
            wr = round(v["would_win"] / max(1, tot) * 100, 1)
            lines.append(f"| {h:02d}:00 | {v['would_win']} | {v['would_lose']} | {wr}% |")
        lines.append("")

    # Recommendations
    lines.append("## Recommendations")
    if not recs:
        lines.append("_No actionable recommendations._")
    else:
        lines.append("| Priority | Type | Detail |")
        lines.append("|----------|------|--------|")
        for r in recs:
            prio = r.get("priority", "INFO")
            typ  = r.get("type", "?")
            det  = r.get("detail", "")
            extra = ""
            if "setup" in r:    extra = f" `{r['setup']}`"
            if "field" in r:    extra = f" `{r['field']}` -> {r.get('value', '?')}"
            lines.append(f"| {prio} | {typ}{extra} | {det} |")

    md = "\n".join(lines)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    return md_path


def _write_json(market: str, real: dict, missed: dict, sweep: list,
                 recs: list, args: dict) -> str:
    json_path = os.path.join(REPORT_DIR, "backtest_pro_summary.json")
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market":       market or "ALL",
        "filters":      args,
        "real":         real,
        "missed":       {
            "by_setup":   missed["by_setup"],
            "reasons":    dict(missed["reasons"].most_common(50)),
            "reasons_won": dict(missed["reasons_won"].most_common(50)),
            "by_hour":    missed["by_hour"],
        },
        "sweep":         sweep,
        "recommendations": recs,
    }
    def _clean(o):
        if isinstance(o, dict):  return {k: _clean(v) for k, v in o.items()}
        if isinstance(o, list):  return [_clean(x) for x in o]
        if isinstance(o, (int, float, str, bool)) or o is None: return o
        return str(o)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(_clean(summary), f, indent=2)
    return json_path


# ============================================================
# Main
# ============================================================

def run_one_market(market: Optional[str], outcomes: list, decisions: list, args: dict):
    """Run analysis for a single market or all."""
    real     = _analyze_real_trades(outcomes,  market)
    fdec     = _filter_window(decisions, args["days"], market, args["setup"])
    missed   = _analyze_missed_winners(fdec)
    sweep    = _sweep_conviction(fdec)
    recs     = _build_recommendations(market or "ALL", real, missed, sweep)

    _print_console(market, real, missed, sweep, recs, args)
    md_path   = _write_markdown(market, real, missed, sweep, recs, args)
    json_path = _write_json(market, real, missed, sweep, recs, args)
    print(f"\nReport saved: {md_path}")
    print(f"JSON saved:   {json_path}")


def main():
    args = _parse_args(sys.argv)

    print("Loading data...")
    outcomes  = _load_csv(OUTCOMES_CSV)
    decisions = _load_csv(STRATEGY_LOG_CSV)
    print(f"  outcomes.csv:     {len(outcomes):,} rows")
    print(f"  strategy_log.csv: {len(decisions):,} rows")

    if args["all"]:
        for market in VALID_MARKETS:
            run_one_market(market, outcomes, decisions, args)
        return

    market = args["market"]
    if market and market not in VALID_MARKETS:
        print(f"[!] Unknown market: {market}. Valid: {VALID_MARKETS}")
        sys.exit(1)
    run_one_market(market, outcomes, decisions, args)


if __name__ == "__main__":
    main()
