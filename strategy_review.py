"""
strategy_review.py - NQ CALLS Auto Strategy Review
====================================================
Reads the last 7 days of strategy_log.csv, finds patterns in what
got rejected but would have won, and outputs actionable suggestions.

Usage:
    python strategy_review.py
    python strategy_review.py --days 14
    python strategy_review.py --days 7 --verbose

Output:
    data/strategy_review.txt

What it does:
    1. Loads recent strategy log data
    2. Identifies REJECTED/ALMOST setups that WOULD_HAVE_WON
    3. Groups by reject reason, setup type, market, and indicators
    4. Finds patterns — which filters are too aggressive?
    5. Compares fired trades vs missed trades
    6. Outputs specific, actionable suggestions

This is the script that makes your bot smarter over time.
Run it weekly and paste the output to Claude to discuss changes.
"""

import os
import sys
import csv
import json
import argparse
from datetime import datetime, timedelta, timezone
from collections import defaultdict

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STRATEGY_LOG = os.path.join(_BASE_DIR, "data", "strategy_log.csv")
OUTCOMES_CSV = os.path.join(_BASE_DIR, "outcomes.csv")
REVIEW_OUT = os.path.join(_BASE_DIR, "data", "strategy_review.txt")
LEARNING_FILE = os.path.join(_BASE_DIR, "data", "setup_performance.json")


def load_strategy_log(days: int) -> list:
    if not os.path.exists(STRATEGY_LOG):
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with open(STRATEGY_LOG, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r.get("timestamp", "") >= cutoff]


def load_outcomes(days: int) -> list:
    if not os.path.exists(OUTCOMES_CSV):
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with open(OUTCOMES_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r.get("timestamp", "") >= cutoff]


def load_learning() -> dict:
    if os.path.exists(LEARNING_FILE):
        try:
            with open(LEARNING_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def safe_float(val, default=0.0):
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def safe_int(val, default=0):
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def run_review(days: int, verbose: bool = False) -> str:
    strategy = load_strategy_log(days)
    outcomes = load_outcomes(days)
    learning = load_learning()

    lines = []
    lines.append("=" * 60)
    lines.append("  NQ CALLS AUTO STRATEGY REVIEW")
    lines.append(f"  Period: last {days} days")
    lines.append(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("=" * 60)
    lines.append("")

    if not strategy:
        lines.append("No strategy log data found for this period.")
        lines.append("Make sure the bot is running and scanning.")
        return "\n".join(lines)

    # ── Categorize decisions ──
    fired = [r for r in strategy if r.get("decision") == "FIRED"]
    rejected = [r for r in strategy if r.get("decision") == "REJECTED"]
    almost = [r for r in strategy if r.get("decision") == "ALMOST"]

    # ── Outcomes of non-fired setups ──
    missed_wins = [r for r in rejected + almost if r.get("result") == "WOULD_WIN"]
    missed_losses = [r for r in rejected + almost if r.get("result") == "WOULD_LOSE"]
    unresolved = [r for r in rejected + almost if not r.get("result")]

    # ── Outcomes of fired setups ──
    closed_outcomes = [r for r in outcomes if r.get("status") == "CLOSED"]
    real_wins = [r for r in closed_outcomes if r.get("result") == "WIN"]
    real_losses = [r for r in closed_outcomes if r.get("result") == "LOSS"]
    real_wr = round(len(real_wins) / max(1, len(real_wins) + len(real_losses)) * 100, 1)

    lines.append("OVERVIEW")
    lines.append("-" * 40)
    lines.append(f"  Total scan decisions:      {len(strategy)}")
    lines.append(f"  FIRED (alerts sent):       {len(fired)}")
    lines.append(f"  REJECTED (filtered out):   {len(rejected)}")
    lines.append(f"  ALMOST (close call):       {len(almost)}")
    lines.append(f"")
    lines.append(f"  Real trades: {len(real_wins)}W / {len(real_losses)}L ({real_wr}% WR)")
    lines.append(f"")
    lines.append(f"  Rejected that WOULD HAVE WON:   {len(missed_wins)}")
    lines.append(f"  Rejected that WOULD HAVE LOST:  {len(missed_losses)}")
    lines.append(f"  Rejected unresolved:             {len(unresolved)}")
    lines.append("")

    # Filter accuracy score
    if missed_wins or missed_losses:
        filter_accuracy = round(len(missed_losses) / max(1, len(missed_wins) + len(missed_losses)) * 100, 1)
        lines.append(f"  FILTER ACCURACY: {filter_accuracy}%")
        lines.append(f"  (% of rejected setups that correctly would have lost)")
        if filter_accuracy >= 70:
            lines.append(f"  >> Filters are working well — most rejections are correct")
        elif filter_accuracy >= 50:
            lines.append(f"  >> Filters are OK but missing some winners")
        else:
            lines.append(f"  >> FILTERS TOO AGGRESSIVE — blocking more winners than losers!")
        lines.append("")

    # ══════════════════════════════════════════════════════════════
    # SECTION 1: WHY ARE WE MISSING WINNERS?
    # ══════════════════════════════════════════════════════════════
    lines.append("=" * 60)
    lines.append("SECTION 1: MISSED WINNERS — WHY?")
    lines.append("=" * 60)
    lines.append("")

    if not missed_wins:
        lines.append("  No missed winners found — filters are not too aggressive.")
        lines.append("")
    else:
        # Group by reject reason
        by_reason = defaultdict(list)
        for r in missed_wins:
            reason = r.get("reject_reason", "unknown")
            by_reason[reason].append(r)

        lines.append("MISSED WINNERS BY REJECTION REASON:")
        lines.append("-" * 40)
        for reason, group in sorted(by_reason.items(), key=lambda x: len(x[1]), reverse=True):
            pct = round(len(group) / len(missed_wins) * 100, 1)
            lines.append(f"  [{len(group):3d}x] ({pct:5.1f}%)  {reason}")

            # Show indicator stats for this group
            if verbose and group:
                adx_vals = [safe_float(r.get("adx")) for r in group if r.get("adx")]
                rsi_vals = [safe_float(r.get("rsi")) for r in group if r.get("rsi")]
                conv_vals = [safe_int(r.get("conviction")) for r in group if r.get("conviction")]
                if adx_vals:
                    lines.append(f"           ADX range: {min(adx_vals):.0f} - {max(adx_vals):.0f} (avg {sum(adx_vals)/len(adx_vals):.1f})")
                if rsi_vals:
                    lines.append(f"           RSI range: {min(rsi_vals):.0f} - {max(rsi_vals):.0f}")
                if conv_vals:
                    lines.append(f"           Conv range: {min(conv_vals)} - {max(conv_vals)} (avg {sum(conv_vals)//len(conv_vals)})")
        lines.append("")

        # Group by market + setup type
        by_setup = defaultdict(list)
        for r in missed_wins:
            key = f"{r.get('market', '?')}:{r.get('setup_type', '?')}"
            by_setup[key].append(r)

        lines.append("MISSED WINNERS BY SETUP:")
        lines.append("-" * 40)
        for key, group in sorted(by_setup.items(), key=lambda x: len(x[1]), reverse=True):
            reasons = [r.get("reject_reason", "?") for r in group]
            most_common = max(set(reasons), key=reasons.count)
            avg_adx = sum(safe_float(r.get("adx")) for r in group) / max(1, len(group))
            avg_rsi = sum(safe_float(r.get("rsi")) for r in group) / max(1, len(group))
            avg_conv = sum(safe_int(r.get("conviction")) for r in group) / max(1, len(group))
            lines.append(f"  {key}: {len(group)} missed wins")
            lines.append(f"    Top reason: {most_common}")
            lines.append(f"    Avg ADX: {avg_adx:.1f} | RSI: {avg_rsi:.1f} | Conv: {avg_conv:.0f}")
        lines.append("")

        # Group by timeframe
        by_tf = defaultdict(int)
        for r in missed_wins:
            by_tf[r.get("tf", "?")] += 1
        lines.append("MISSED WINNERS BY TIMEFRAME:")
        lines.append("-" * 40)
        for tf, count in sorted(by_tf.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"  {tf}: {count} missed")
        lines.append("")

    # ══════════════════════════════════════════════════════════════
    # SECTION 2: ALMOST SETUPS (CLOSE CALLS)
    # ══════════════════════════════════════════════════════════════
    lines.append("=" * 60)
    lines.append("SECTION 2: ALMOST SETUPS (CLOSE CALLS)")
    lines.append("=" * 60)
    lines.append("")

    almost_wins = [r for r in almost if r.get("result") == "WOULD_WIN"]
    almost_losses = [r for r in almost if r.get("result") == "WOULD_LOSE"]

    if not almost:
        lines.append("  No ALMOST decisions recorded.")
    else:
        lines.append(f"  Total ALMOST decisions: {len(almost)}")
        lines.append(f"  Would have won:  {len(almost_wins)}")
        lines.append(f"  Would have lost: {len(almost_losses)}")
        if almost_wins:
            almost_wr = round(len(almost_wins) / max(1, len(almost_wins) + len(almost_losses)) * 100, 1)
            lines.append(f"  ALMOST win rate: {almost_wr}%")
            if almost_wr >= 55:
                lines.append(f"  >> These setups would be profitable! Consider loosening the filter that blocked them.")
            lines.append("")

            # What stopped them?
            almost_reasons = defaultdict(int)
            for r in almost_wins:
                almost_reasons[r.get("reject_reason", "?")] += 1
            lines.append("  What blocked these potential winners:")
            for reason, count in sorted(almost_reasons.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"    [{count}x] {reason}")
    lines.append("")

    # ══════════════════════════════════════════════════════════════
    # SECTION 3: INDICATOR ANALYSIS
    # ══════════════════════════════════════════════════════════════
    lines.append("=" * 60)
    lines.append("SECTION 3: INDICATOR PATTERNS")
    lines.append("=" * 60)
    lines.append("")

    # Compare indicators between fired wins vs fired losses
    fired_with_result = [r for r in fired if r.get("result") in ("WIN", "LOSS", "WOULD_WIN", "WOULD_LOSE")]
    # Also use outcomes
    if real_wins or real_losses:
        lines.append("FIRED TRADES — WINNING vs LOSING INDICATOR PROFILE:")
        lines.append("-" * 40)

        def avg_indicator(rows, field):
            vals = [safe_float(r.get(field)) for r in rows if r.get(field)]
            return round(sum(vals) / max(1, len(vals)), 1) if vals else 0

        for label, group in [("Winners", real_wins), ("Losers", real_losses)]:
            avg_adx = avg_indicator(group, "adx")
            avg_rsi = avg_indicator(group, "rsi")
            avg_conv = avg_indicator(group, "conviction")
            avg_rr = avg_indicator(group, "rr")
            avg_vol = avg_indicator(group, "vol_ratio")
            avg_trend = avg_indicator(group, "trend_score")
            lines.append(f"  {label:8s}: ADX {avg_adx:5.1f} | RSI {avg_rsi:5.1f} | Conv {avg_conv:5.0f} | "
                         f"RR {avg_rr:4.1f} | Vol {avg_vol:4.1f} | Trend {avg_trend:+5.1f}")

        # Suggestions based on differences
        win_adx = avg_indicator(real_wins, "adx")
        loss_adx = avg_indicator(real_losses, "adx")
        win_conv = avg_indicator(real_wins, "conviction")
        loss_conv = avg_indicator(real_losses, "conviction")
        win_vol = avg_indicator(real_wins, "vol_ratio")
        loss_vol = avg_indicator(real_losses, "vol_ratio")

        lines.append("")
        if win_adx > loss_adx + 3:
            lines.append(f"  >> ADX INSIGHT: Winners have ADX {win_adx:.0f} vs losers {loss_adx:.0f}")
            lines.append(f"     Consider raising MIN_ADX — higher ADX = more reliable")
        if win_vol > loss_vol + 0.3:
            lines.append(f"  >> VOLUME INSIGHT: Winners have {win_vol:.1f}x vol vs losers {loss_vol:.1f}x")
            lines.append(f"     Volume confirmation is working — keep the vol filter")
        if win_conv > loss_conv + 5:
            lines.append(f"  >> CONVICTION INSIGHT: Winners avg {win_conv:.0f} vs losers {loss_conv:.0f}")
            lines.append(f"     Higher conviction = better trades. Consider raising MIN_CONVICTION")
    lines.append("")

    # ══════════════════════════════════════════════════════════════
    # SECTION 4: MARKET-SPECIFIC ANALYSIS
    # ══════════════════════════════════════════════════════════════
    lines.append("=" * 60)
    lines.append("SECTION 4: MARKET-SPECIFIC FINDINGS")
    lines.append("=" * 60)
    lines.append("")

    markets_seen = set(r.get("market", "") for r in strategy if r.get("market"))
    for market in sorted(markets_seen):
        market_rows = [r for r in strategy if r.get("market") == market]
        m_fired = [r for r in market_rows if r.get("decision") == "FIRED"]
        m_rejected = [r for r in market_rows if r.get("decision") in ("REJECTED", "ALMOST")]
        m_missed_w = [r for r in m_rejected if r.get("result") == "WOULD_WIN"]
        m_missed_l = [r for r in m_rejected if r.get("result") == "WOULD_LOSE"]

        # Get real outcomes for this market
        m_real_wins = [r for r in real_wins if r.get("market") == market]
        m_real_losses = [r for r in real_losses if r.get("market") == market]
        m_wr = round(len(m_real_wins) / max(1, len(m_real_wins) + len(m_real_losses)) * 100, 1)

        lines.append(f"  {market}:")
        lines.append(f"    Scans: {len(market_rows)} | Fired: {len(m_fired)} | Rejected: {len(m_rejected)}")
        lines.append(f"    Real W/L: {len(m_real_wins)}W/{len(m_real_losses)}L ({m_wr}% WR)")
        lines.append(f"    Missed wins: {len(m_missed_w)} | Missed losses: {len(m_missed_l)}")

        if m_missed_w:
            reasons = [r.get("reject_reason", "?") for r in m_missed_w]
            top_reason = max(set(reasons), key=reasons.count)
            lines.append(f"    Top missed reason: {top_reason}")

        # Check learning file for this market
        for key, data in learning.items():
            if key.startswith(f"{market}:"):
                setup_name = key.split(":")[1]
                wr_val = data.get("win_rate", 0)
                total = data.get("total", 0)
                if total >= 5:
                    icon = "+" if wr_val >= 55 else "-" if wr_val < 45 else "~"
                    lines.append(f"    [{icon}] {setup_name}: {wr_val}% WR over {total} trades")

        lines.append("")

    # ══════════════════════════════════════════════════════════════
    # SECTION 5: ACTIONABLE SUGGESTIONS
    # ══════════════════════════════════════════════════════════════
    lines.append("=" * 60)
    lines.append("SECTION 5: ACTIONABLE SUGGESTIONS")
    lines.append("=" * 60)
    lines.append("")

    suggestions = []
    suggestion_num = 0

    # ADX too aggressive?
    adx_misses = [r for r in missed_wins if "ADX" in r.get("reject_reason", "").upper()]
    if len(adx_misses) >= 3:
        # Find the ADX values that were rejected
        adx_vals = [safe_float(r.get("adx")) for r in adx_misses]
        avg_adx = sum(adx_vals) / max(1, len(adx_vals))
        # Group by market:setup to see which specific filters are too tight
        adx_by_setup = defaultdict(list)
        for r in adx_misses:
            key = f"{r.get('market')}:{r.get('setup_type')}"
            adx_by_setup[key].append(safe_float(r.get("adx")))

        suggestion_num += 1
        suggestions.append(f"{suggestion_num}. LOWER ADX THRESHOLDS FOR SPECIFIC SETUPS")
        suggestions.append(f"   {len(adx_misses)} setups were rejected for low ADX but would have won.")
        suggestions.append(f"   Average ADX of missed winners: {avg_adx:.1f}")
        for key, vals in sorted(adx_by_setup.items(), key=lambda x: len(x[1]), reverse=True)[:5]:
            avg = sum(vals) / len(vals)
            suggestions.append(f"   -> {key}: {len(vals)}x missed, avg ADX {avg:.1f}")
            suggestions.append(f"      ACTION: Lower ADX_MIN_BY_SETUP['{key.split(':')[1]}'] to {max(8, int(avg) - 2)} in market_{key.split(':')[0]}.py")
        suggestions.append("")

    # Conviction too aggressive?
    conv_misses = [r for r in missed_wins if "conv" in r.get("reject_reason", "").lower()]
    if len(conv_misses) >= 3:
        conv_vals = [safe_int(r.get("conviction")) for r in conv_misses]
        avg_conv = sum(conv_vals) / max(1, len(conv_vals))
        suggestion_num += 1
        suggestions.append(f"{suggestion_num}. LOWER MINIMUM CONVICTION")
        suggestions.append(f"   {len(conv_misses)} setups rejected for low conviction but would have won.")
        suggestions.append(f"   Average conviction of missed winners: {avg_conv:.0f}")
        suggestions.append(f"   ACTION: Consider lowering MIN_CONVICTION by 3-5 points for markets with high missed count")
        suggestions.append("")

    # R:R too aggressive?
    rr_misses = [r for r in missed_wins if "RR" in r.get("reject_reason", "").upper() or "rr" in r.get("reject_reason", "").lower()]
    if len(rr_misses) >= 3:
        rr_vals = [safe_float(r.get("rr")) for r in rr_misses]
        avg_rr = sum(rr_vals) / max(1, len(rr_vals))
        suggestion_num += 1
        suggestions.append(f"{suggestion_num}. ADJUST R:R THRESHOLDS")
        suggestions.append(f"   {len(rr_misses)} setups rejected for low R:R but would have won.")
        suggestions.append(f"   Average R:R of missed winners: {avg_rr:.2f}")
        suggestions.append(f"   ACTION: Consider lowering MIN_RR by 0.5 or adjusting dynamic R:R tiers")
        suggestions.append("")

    # Specific setup types that are consistently rejected but would win
    by_setup_missed = defaultdict(lambda: {"wins": 0, "losses": 0})
    for r in missed_wins:
        key = f"{r.get('market')}:{r.get('setup_type')}"
        by_setup_missed[key]["wins"] += 1
    for r in missed_losses:
        key = f"{r.get('market')}:{r.get('setup_type')}"
        by_setup_missed[key]["losses"] += 1

    profitable_missed = [(k, v) for k, v in by_setup_missed.items()
                         if v["wins"] >= 3 and v["wins"] > v["losses"]]
    if profitable_missed:
        suggestion_num += 1
        suggestions.append(f"{suggestion_num}. THESE SETUPS ARE BEING OVER-FILTERED:")
        for key, stats in sorted(profitable_missed, key=lambda x: x[1]["wins"], reverse=True):
            wr = round(stats["wins"] / max(1, stats["wins"] + stats["losses"]) * 100, 1)
            suggestions.append(f"   {key}: {stats['wins']} would-win / {stats['losses']} would-lose ({wr}% WR)")
            suggestions.append(f"   ACTION: Loosen filters specifically for this market:setup combo")
        suggestions.append("")

    # HTF bias blocking good trades?
    htf_misses = [r for r in missed_wins if "htf" in r.get("reject_reason", "").lower()
                  or "bias" in r.get("reject_reason", "").lower()]
    if len(htf_misses) >= 3:
        suggestion_num += 1
        suggestions.append(f"{suggestion_num}. HTF BIAS FILTER MAY BE TOO STRICT")
        suggestions.append(f"   {len(htf_misses)} setups rejected due to HTF bias but would have won.")
        suggestions.append(f"   ACTION: Consider allowing setups when HTF is MIXED (not just aligned)")
        suggestions.append("")

    # Trend filter blocking good trades?
    trend_misses = [r for r in missed_wins if "trend" in r.get("reject_reason", "").lower()]
    if len(trend_misses) >= 3:
        suggestion_num += 1
        suggestions.append(f"{suggestion_num}. TREND FILTER BLOCKING WINNERS")
        suggestions.append(f"   {len(trend_misses)} setups rejected for trend alignment but would have won.")
        suggestions.append(f"   ACTION: Consider allowing counter-trend setups when conviction is HIGH (80+)")
        suggestions.append("")

    # Check which setups are working well (from learning file) — should be given MORE room
    for key, data in learning.items():
        wr_val = data.get("win_rate", 0)
        total = data.get("total", 0)
        if total >= 5 and wr_val >= 65:
            suggestion_num += 1
            suggestions.append(f"{suggestion_num}. GIVE MORE ROOM TO PROVEN WINNER: {key}")
            suggestions.append(f"   {wr_val}% WR over {total} trades")
            suggestions.append(f"   ACTION: Consider lowering ADX/conviction minimums for this setup")
            suggestions.append(f"           since it has proven itself reliable")
            suggestions.append("")

    # Check for losing setups that should be restricted more
    for key, data in learning.items():
        wr_val = data.get("win_rate", 0)
        total = data.get("total", 0)
        if total >= 5 and wr_val < 40:
            suggestion_num += 1
            suggestions.append(f"{suggestion_num}. RESTRICT LOSING SETUP: {key}")
            suggestions.append(f"   Only {wr_val}% WR over {total} trades")
            suggestions.append(f"   ACTION: Raise MIN_ADX or MIN_CONVICTION for this setup,")
            suggestions.append(f"           or add specific conditions before it can fire")
            suggestions.append("")

    if suggestions:
        for s in suggestions:
            lines.append(f"  {s}")
    else:
        lines.append("  No strong suggestions right now.")
        lines.append("  The bot's filters appear to be well-calibrated.")
        lines.append("  Keep running and collecting data for more insights.")
    lines.append("")

    # ══════════════════════════════════════════════════════════════
    # SECTION 6: QUICK SUMMARY
    # ══════════════════════════════════════════════════════════════
    lines.append("=" * 60)
    lines.append("QUICK SUMMARY")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"  Scans analyzed:        {len(strategy)}")
    lines.append(f"  Fired:                 {len(fired)}")
    lines.append(f"  Rejected:              {len(rejected) + len(almost)}")
    lines.append(f"  Real win rate:         {real_wr}%")
    lines.append(f"  Missed winners:        {len(missed_wins)}")
    lines.append(f"  Good rejections:       {len(missed_losses)}")
    lines.append(f"  Suggestions generated: {suggestion_num}")
    lines.append("")
    lines.append("=" * 60)
    lines.append("  Paste this to Claude to review and update strategy files.")
    lines.append("=" * 60)

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="NQ CALLS Auto Strategy Review")
    parser.add_argument("--days", type=int, default=7, help="Days to review (default: 7)")
    parser.add_argument("--verbose", action="store_true", help="Show detailed indicator stats")
    args = parser.parse_args()

    print(f"Running strategy review for last {args.days} days...")

    report = run_review(args.days, args.verbose)

    # Print to console
    print(f"\n{report}")

    # Save to file
    os.makedirs(os.path.dirname(REVIEW_OUT), exist_ok=True)
    with open(REVIEW_OUT, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\nSaved to: {REVIEW_OUT}")


if __name__ == "__main__":
    main()
