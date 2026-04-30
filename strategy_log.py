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
from typing import Optional
import pandas as pd
import safe_io  # data-loss fix: atomic writes + cross-process locks

_BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR    = os.path.join(_BASE_DIR, "data")
STRATEGY_LOG = os.path.join(_DATA_DIR, "strategy_log.csv")
CANDIDATE_FILE = os.path.join(_DATA_DIR, "strategy_candidates.txt")

os.makedirs(_DATA_DIR, exist_ok=True)

COLS = [
    # ── EXISTING — KEEP THIS ORDER EXACTLY ──
    "timestamp", "market", "tf", "setup_type", "direction",
    "price", "entry", "stop", "target", "rr",
    "conviction", "tier", "trend", "adx", "rsi", "vol_ratio",
    "htf_bias", "news_flag", "decision", "reject_reason",
    "result", "result_checked_at",
    # ── NEW: scoring transparency ──
    "score_breakdown",       # JSON dict of conviction factors and their points
    "confidence_factors",    # JSON dict: BB position, Stoch signal, MACD signal, etc.
    "detection_reason",      # Human-readable sentence explaining what the bot saw
    # ── NEW: indicator snapshot at decision time ──
    "atr", "vwap", "ema20", "ema50", "ema200", "ema21",
    "bb_upper", "bb_middle", "bb_lower", "bb_width_pct",
    "stoch_k", "stoch_d", "macd_line", "macd_signal", "macd_hist",
    # ── NEW: market context ──
    "close_price", "regime", "session_name",
    "swing_high_30", "swing_low_30", "volume_raw", "volume_20ma",
]

# ── Decision types ─────────────────────────────────────────────────
DECISION_FIRED            = "FIRED"              # alert sent
DECISION_REJECTED         = "REJECTED"           # filtered out
DECISION_ALMOST           = "ALMOST"             # passed most filters, just missed one
DECISION_SHADOW_SUSPENDED = "REJECTED_SUSPENDED" # detected but blocked by suspension
DECISION_DETECTED         = "DETECTED"           # raw detection before any filter
DECISION_CLOSED_WIN       = "CLOSED_WIN"         # trade closed as a win
DECISION_CLOSED_LOSS      = "CLOSED_LOSS"        # trade closed as a loss

# Pre-Batch 2026-04-20: Shadow log for signals that WOULD have fired but were
# blocked by a gate we've since removed (e.g., the 2-loss halt). These rows
# let us measure whether the gate saved money or cost money — CRITICAL for
# validating filter logic later.
DECISION_SHADOW_HALTED    = "SHADOW_HALTED"      # signal fired anyway despite old halt

# Pre-Batch Follow-up Part A 2026-04-20: Additional shadow-log decision types.
# Each represents "this signal/scan fired anyway; old gate would have blocked
# for this specific reason." Separate constants let us filter strategy_log.csv
# by gate type later in weekly reviews and gate-value analysis.
DECISION_SHADOW_PROFIT_LOCK    = "SHADOW_PROFIT_LOCK"     # +$150 profit lock
DECISION_SHADOW_MAX_TRADES     = "SHADOW_MAX_TRADES"      # 4th+ trade of session
DECISION_SHADOW_CORRELATION    = "SHADOW_CORRELATION"     # BTC/SOL 30-min correlation
DECISION_SHADOW_ZONE_LOCK      = "SHADOW_ZONE_LOCK"       # loss zone lockout
DECISION_SHADOW_FAMILY_CD      = "SHADOW_FAMILY_CD"       # setup family cooldown
DECISION_SHADOW_MARKET_HALT    = "SHADOW_MARKET_HALT"     # 3-loss per-market halt
DECISION_SHADOW_COOLDOWN       = "SHADOW_COOLDOWN"        # per-setup cooldown

def _ensure_csv():
    """
    Ensure strategy_log.csv exists with the current COLS schema.
    If an old-schema file exists (22 cols), migrate it in place by adding
    empty values for the new columns. Never loses data.
    """
    if not os.path.exists(STRATEGY_LOG):
        with open(STRATEGY_LOG, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=COLS).writeheader()
        return

    # File exists — check if header matches current COLS
    try:
        with open(STRATEGY_LOG, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            existing_header = next(reader, [])
    except Exception:
        existing_header = []

    if existing_header == COLS:
        return  # already migrated

    # Migration needed. Back up, then rewrite with new schema.
    backup_path = STRATEGY_LOG + ".pre_batch2a.bak"
    try:
        import shutil
        shutil.copy2(STRATEGY_LOG, backup_path)
    except Exception:
        pass

    try:
        with open(STRATEGY_LOG, newline="", encoding="utf-8") as f:
            old_rows = list(csv.DictReader(f))
    except Exception:
        old_rows = []

    with open(STRATEGY_LOG, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLS)
        writer.writeheader()
        for row in old_rows:
            # Fill missing columns with empty string
            clean = {k: row.get(k, "") for k in COLS}
            writer.writerow(clean)


def update_fired_row_result(market: str, setup_type: str, direction: str,
                            entry: float, result: str) -> bool:
    """
    Apr 30 fix: update the most recent FIRED row in strategy_log.csv to record
    the trade's WIN/LOSS outcome. This makes the 9k+ scan decisions queryable
    by win rate later — previously the 'result' column for FIRED rows stayed
    empty because outcomes were only written to outcomes.csv.

    Matches by market+setup_type+direction+entry (rounded). Updates the
    most recent unresolved FIRED row only. Safe and idempotent.
    """
    if not os.path.exists(STRATEGY_LOG):
        return False

    def _mut(rows):
        # Find the most recent FIRED row that matches and has empty result.
        # Walk in reverse; update the first match.
        for r in reversed(rows):
            if r.get("decision") != DECISION_FIRED:
                continue
            if r.get("result"):
                continue
            if r.get("market") != market:
                continue
            if r.get("setup_type") != setup_type:
                continue
            if r.get("direction") != direction:
                continue
            try:
                if abs(float(r.get("entry", 0)) - float(entry)) > 0.01 * abs(float(entry)):
                    continue
            except Exception:
                continue
            r["result"] = result
            r["result_checked_at"] = datetime.now(timezone.utc).isoformat()
            break
        return rows

    try:
        safe_io.safe_rewrite_csv(STRATEGY_LOG, COLS, _mut)
        return True
    except Exception:
        return False

def log_scan_decision(
    market: str, tf: str, setup_type: str, direction: str,
    price: float, entry: float, stop: float, target: float, rr: float,
    conviction: int, tier: str, trend: int,
    adx: float, rsi: float, vol_ratio: float,
    htf_bias: str, news_flag: bool,
    decision: str, reject_reason: str = "",
    # ── NEW optional keyword-only params (Batch 2A) ──
    *,
    context: Optional[dict] = None,
    detection_reason: str = "",
    score_breakdown: Optional[dict] = None,
    confidence_factors: Optional[dict] = None,
    result: str = "",
) -> str:
    """
    Log every scan decision — fired, rejected, almost, or detected.
    Returns the row timestamp so we can update result later.
    """
    _ensure_csv()
    row = {
        "timestamp":         datetime.now(timezone.utc).isoformat(),
        "market":            market,
        "tf":                tf,
        "setup_type":        setup_type,
        "direction":         direction,
        "price":             round(float(price), 4)    if price     not in ("", None) else "",
        "entry":             round(float(entry), 4)    if entry     not in ("", None) else "",
        "stop":              round(float(stop), 4)     if stop      not in ("", None) else "",
        "target":            round(float(target), 4)   if target    not in ("", None) else "",
        "rr":                round(float(rr), 2)       if rr        not in ("", None) else "",
        "conviction":        conviction,
        "tier":              tier,
        "trend":             trend,
        "adx":               round(float(adx), 1)      if adx       not in ("", None) else "",
        "rsi":               round(float(rsi), 1)      if rsi       not in ("", None) else "",
        "vol_ratio":         round(float(vol_ratio),2) if vol_ratio not in ("", None) else "",
        "htf_bias":          htf_bias,
        "news_flag":         int(bool(news_flag)),
        "decision":          decision,
        "reject_reason":     reject_reason or "",
        "result":            result or "",
        "result_checked_at": "",
        # NEW fields
        "score_breakdown":     json.dumps(score_breakdown, default=str)    if score_breakdown    else "",
        "confidence_factors":  json.dumps(confidence_factors, default=str) if confidence_factors else "",
        "detection_reason":    detection_reason or "",
    }

    # Pull indicator snapshot from context (all optional)
    ctx = context or {}
    for key in ("atr", "vwap", "ema20", "ema50", "ema200", "ema21",
                "bb_upper", "bb_middle", "bb_lower", "bb_width_pct",
                "stoch_k", "stoch_d", "macd_line", "macd_signal", "macd_hist",
                "close_price", "regime", "session_name",
                "swing_high_30", "swing_low_30", "volume_raw", "volume_20ma"):
        val = ctx.get(key, "")
        if isinstance(val, float):
            row[key] = round(val, 4)
        else:
            row[key] = val if val not in (None,) else ""

    # Ensure every COLS key is present
    for k in COLS:
        row.setdefault(k, "")

    # Locked atomic append. Prevents check_missed_setups from clobbering
    # this row by rewriting the file with a stale snapshot.
    safe_io.safe_append_csv(STRATEGY_LOG, COLS, row)

    return row["timestamp"]


def check_missed_setups(live_frames: dict):
    """
    Called every scan. Checks REJECTED/ALMOST setups to see if
    price hit their target or stop since the alert was logged.

    Uses candle HIGH/LOW range (not just close price) so we catch
    moves that spiked through a level between scans — same method
    as outcome_tracker uses for real trades.

    DATA-LOSS FIX (2026-04-27): the old version read the file, mutated
    rows in-memory, then truncate-rewrote with `open("w")`. If
    log_scan_decision() appended a row between our read and our rewrite,
    that row was silently lost. This caused row counts to bounce
    (3.5k -> 7k -> 3k). Now we use safe_io.safe_rewrite_csv which
    re-reads inside the lock, so concurrent appenders wait their turn
    and never get clobbered.
    """
    if not os.path.exists(STRATEGY_LOG):
        return []

    updated_log: list = []

    def _mutator(rows: list[dict]) -> list[dict]:
        """Runs INSIDE safe_rewrite_csv's lock with a fresh read. Mutates
        rows in place and returns them. Cannot do other I/O on the file."""
        for row in rows:
            if row.get("result"):             # already resolved
                continue
            if row.get("decision") == DECISION_FIRED:  # handled by outcome_tracker
                continue
            market = row.get("market")
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
                    alert_ts = row.get("timestamp", "")
                    try:
                        alert_dt = pd.Timestamp(alert_ts, tz="UTC")
                        recent = market_data[market_data.index >= alert_dt]
                        if recent.empty:
                            recent = market_data.iloc[-5:]
                    except Exception:
                        recent = market_data.iloc[-5:]
                    period_high = float(recent["High"].max())
                    period_low  = float(recent["Low"].min())
                elif isinstance(market_data, (int, float)):
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
                    row["result_checked_at"] = datetime.now(timezone.utc).isoformat()
                    updated_log.append(dict(row))
                elif hit_stop:
                    row["result"]           = "WOULD_LOSE"
                    row["result_checked_at"] = datetime.now(timezone.utc).isoformat()
                    updated_log.append(dict(row))
            except Exception:
                continue

        # Always return rows — safe_rewrite_csv writes whatever we return.
        # If we didn't update anything, this is a no-op rewrite of the
        # existing data (slightly wasteful but still correct).
        return rows

    # Only call the rewrite if there's something worth doing. Reading first
    # under a fresh lock-less peek is fine because if we DO mutate, the
    # safe_rewrite_csv call re-reads inside its own lock.
    try:
        # Quick peek to decide whether to bother taking the lock
        with open(STRATEGY_LOG, "r", newline="", encoding="utf-8") as f:
            sample = list(csv.DictReader(f))
        has_pending = any(
            (not r.get("result")) and r.get("decision") != DECISION_FIRED
            for r in sample
        )
        if not has_pending:
            return []
    except Exception:
        # If the peek fails for any reason, fall through and try the rewrite
        pass

    safe_io.safe_rewrite_csv(STRATEGY_LOG, COLS, _mutator)
    return updated_log


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


# ── Batch 2A: auto-migrate the CSV at import time ──
try:
    _ensure_csv()
except Exception:
    # If migration fails for any reason, don't crash the bot at import time —
    # the next call to log_scan_decision will retry via _ensure_csv() anyway.
    pass
