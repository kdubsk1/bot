"""
strategy_log.py - NQ CALLS 2026
=================================
Records EVERY scan decision the bot makes.

Not just what it fired — everything:
- Setups it TOOK (fired as alerts)
- Setups it REJECTED (and exactly why)
- Setups it ALMOST TOOK (close but filtered out)
- What price did AFTER — so we can see if missed setups hit

This is the raw data that drives strategy discovery.
Every day we review this with Claude to find patterns we're missing.
"""

from __future__ import annotations
import os, json, csv
from datetime import datetime, timezone
import pandas as pd

_BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR    = os.path.join(_BASE_DIR, "data")
STRATEGY_LOG = os.path.join(_DATA_DIR, "strategy_log.csv")
CANDIDATE_FILE = os.path.join(_DATA_DIR, "strategy_candidates.txt")

os.makedirs(_DATA_DIR, exist_ok=True)

COLS = [
    "timestamp", "market", "tf", "setup_type", "direction",
    "price", "entry", "stop", "target", "rr",
    "conviction", "tier", "trend", "adx", "rsi", "vol_ratio",
    "htf_bias", "news_flag", "decision", "reject_reason",
    "result", "result_checked_at"
]

# ── Decision types ─────────────────────────────────────────────────
DECISION_FIRED            = "FIRED"              # alert sent
DECISION_REJECTED         = "REJECTED"           # filtered out
DECISION_ALMOST           = "ALMOST"             # passed most filters, just missed one
DECISION_SHADOW_SUSPENDED = "REJECTED_SUSPENDED" # detected but blocked by suspension

def _ensure_csv():
    if not os.path.exists(STRATEGY_LOG):
        with open(STRATEGY_LOG, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=COLS).writeheader()

def log_scan_decision(
    market: str, tf: str, setup_type: str, direction: str,
    price: float, entry: float, stop: float, target: float, rr: float,
    conviction: int, tier: str, trend: int,
    adx: float, rsi: float, vol_ratio: float,
    htf_bias: str, news_flag: bool,
    decision: str, reject_reason: str = ""
) -> str:
    """
    Log every scan decision — fired, rejected, or almost.
    Returns the row ID so we can update result later.
    """
    _ensure_csv()
    row = {
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "market":           market,
        "tf":               tf,
        "setup_type":       setup_type,
        "direction":        direction,
        "price":            round(price, 4),
        "entry":            round(entry, 4),
        "stop":             round(stop, 4),
        "target":           round(target, 4),
        "rr":               round(rr, 2),
        "conviction":       conviction,
        "tier":             tier,
        "trend":            trend,
        "adx":              round(adx, 1),
        "rsi":              round(rsi, 1),
        "vol_ratio":        round(vol_ratio, 2),
        "htf_bias":         htf_bias,
        "news_flag":        int(news_flag),
        "decision":         decision,
        "reject_reason":    reject_reason,
        "result":           "",
        "result_checked_at":"",
    }
    with open(STRATEGY_LOG, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=COLS).writerow(row)
    return row["timestamp"]


def check_missed_setups(live_frames: dict):
    """
    Called every scan. Checks REJECTED/ALMOST setups to see if
    price hit their target or stop since the alert was logged.

    Uses candle HIGH/LOW range (not just close price) so we catch
    moves that spiked through a level between scans — same method
    as outcome_tracker uses for real trades.
    """
    if not os.path.exists(STRATEGY_LOG):
        return []

    rows = []
    with open(STRATEGY_LOG, newline="") as f:
        rows = list(csv.DictReader(f))

    updated = []
    for row in rows:
        if row.get("result"):             # already resolved
            continue
        if row.get("decision") == DECISION_FIRED:  # handled by outcome_tracker
            continue
        market = row.get("market")
        # Use frames dict {market: df} or {market: price_float} — handle both
        market_data = live_frames.get(market)
        if market_data is None:
            continue

        try:
            target    = float(row.get("target", 0))
            stop      = float(row.get("stop", 0))
            direction = row.get("direction", "LONG")
            if target == 0 or stop == 0:
                continue

            # Get candle high/low since alert — catches spikes between scans
            if isinstance(market_data, pd.DataFrame):
                # Full DataFrame passed — use recent candle range
                alert_ts = row.get("timestamp", "")
                try:
                    alert_dt = pd.Timestamp(alert_ts, tz="UTC")
                    recent = market_data[market_data.index >= alert_dt]
                    if recent.empty:
                        recent = market_data.iloc[-5:]  # fallback to last 5 candles
                except:
                    recent = market_data.iloc[-5:]
                period_high = float(recent["High"].max())
                period_low  = float(recent["Low"].min())
            elif isinstance(market_data, (int, float)):
                # Just a price float — use it as both high and low
                period_high = float(market_data)
                period_low  = float(market_data)
            else:
                continue

            hit_target = hit_stop = False
            if direction == "LONG":
                if period_high >= target: hit_target = True
                if period_low  <= stop:   hit_stop   = True
            else:
                if period_low  <= target: hit_target = True
                if period_high >= stop:   hit_stop   = True

            if hit_target:
                row["result"]           = "WOULD_WIN"
                row["result_checked_at"]= datetime.now(timezone.utc).isoformat()
                updated.append(dict(row))
            elif hit_stop:
                row["result"]           = "WOULD_LOSE"
                row["result_checked_at"]= datetime.now(timezone.utc).isoformat()
                updated.append(dict(row))
        except:
            continue

    if updated:
        # Rewrite file with updates
        with open(STRATEGY_LOG, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k,"") for k in COLS})

    return updated


def build_strategy_analysis() -> str:
    """
    Analyzes the strategy log to find:
    - Patterns we're missing (ALMOST/REJECTED that would have won)
    - Setups that are being filtered too aggressively
    - New potential strategy candidates
    Returns a text report.
    """
    if not os.path.exists(STRATEGY_LOG):
        return "No strategy log data yet."

    with open(STRATEGY_LOG, newline="") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        return "Strategy log is empty."

    fired     = [r for r in rows if r.get("decision") == DECISION_FIRED]
    rejected  = [r for r in rows if r.get("decision") == DECISION_REJECTED]
    almost    = [r for r in rows if r.get("decision") == DECISION_ALMOST]

    # Missed winners — setups we rejected that would have won
    missed_wins  = [r for r in rejected + almost if r.get("result") == "WOULD_WIN"]
    missed_losses= [r for r in rejected + almost if r.get("result") == "WOULD_LOSE"]

    lines = [
        "STRATEGY LOG ANALYSIS",
        "=" * 50,
        f"Total scan decisions: {len(rows)}",
        f"  FIRED:    {len(fired)}",
        f"  REJECTED: {len(rejected)}",
        f"  ALMOST:   {len(almost)}",
        "",
        "MISSED OPPORTUNITIES:",
        "-" * 30,
        f"Setups we rejected that WOULD HAVE WON:  {len(missed_wins)}",
        f"Setups we rejected that WOULD HAVE LOST: {len(missed_losses)}",
        "",
    ]

    if missed_wins:
        lines.append("TOP MISSED WINNERS (setups to investigate):")
        by_type = {}
        for r in missed_wins:
            key = f"{r.get('market')}:{r.get('setup_type')}:{r.get('tf')}"
            by_type.setdefault(key, []).append(r)
        for key, group in sorted(by_type.items(), key=lambda x: len(x[1]), reverse=True):
            reasons = [r.get("reject_reason","?") for r in group]
            most_common_reason = max(set(reasons), key=reasons.count)
            avg_conv = round(sum(int(r.get("conviction",0)) for r in group) / max(1,len(group)))
            lines.append(f"  {key}: {len(group)} missed wins")
            lines.append(f"    Avg conviction: {avg_conv} | Most filtered by: {most_common_reason}")
        lines.append("")

    # Pattern discovery — look for indicator combos that correlate with wins
    if len(fired) >= 10:
        wins  = [r for r in fired if "WIN" in r.get("result","")]
        losses= [r for r in fired if "LOSS" in r.get("result","")]
        if wins and losses:
            lines.append("INDICATOR PATTERNS IN WINNING TRADES:")
            lines.append("-" * 30)
            try:
                avg_adx_win  = round(sum(float(r.get("adx",0)) for r in wins)  / max(1,len(wins)),  1)
                avg_adx_loss = round(sum(float(r.get("adx",0)) for r in losses) / max(1,len(losses)), 1)
                avg_rsi_win  = round(sum(float(r.get("rsi",0)) for r in wins)  / max(1,len(wins)),  1)
                avg_rsi_loss = round(sum(float(r.get("rsi",0)) for r in losses) / max(1,len(losses)), 1)
                avg_trend_win = round(sum(int(r.get("trend",0)) for r in wins) / max(1,len(wins)),  1)
                lines.append(f"  Winning trades avg ADX:   {avg_adx_win}  (losing: {avg_adx_loss})")
                lines.append(f"  Winning trades avg RSI:   {avg_rsi_win}  (losing: {avg_rsi_loss})")
                lines.append(f"  Winning trades avg Trend: {avg_trend_win}")
                lines.append("")
                if avg_adx_win > avg_adx_loss + 3:
                    lines.append(f"  💡 INSIGHT: Wins have higher ADX — consider raising MIN_ADX")
                if avg_trend_win > 3:
                    lines.append(f"  💡 INSIGHT: Strong trend correlation — trend filter is working")
            except:
                pass

    # Candidate strategies — setups appearing in missed wins consistently
    lines += [
        "",
        "STRATEGY CANDIDATES FOR REVIEW:",
        "-" * 30,
    ]
    candidate_setups = {}
    for r in missed_wins:
        key = f"{r.get('market')}:{r.get('setup_type')}"
        candidate_setups.setdefault(key, 0)
        candidate_setups[key] += 1

    if candidate_setups:
        for key, count in sorted(candidate_setups.items(), key=lambda x: x[1], reverse=True):
            if count >= 2:
                lines.append(f"  ⚡ {key}: appeared {count}x as missed winner — consider loosening filter")
    else:
        lines.append("  Not enough data yet. Keep running.")

    lines += [
        "",
        "=" * 50,
        "END OF ANALYSIS",
        "Paste this to Claude to review and update strategy files.",
    ]

    report = "\n".join(lines)

    # Save candidate file
    with open(CANDIDATE_FILE, "w", encoding="utf-8") as f:
        f.write(report)

    return report
