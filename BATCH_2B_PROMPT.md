# BATCH 2B — Auto-Calibrating Conviction + Bot-Driven Backtesting

**This batch requires Batch 2A to be deployed first.** The expanded strategy_log + indicator snapshots feed this batch.

Goal: Make conviction score self-adjust based on actual performance. Setup at 100% WR → conviction approaches 100. Setup at 50% WR → conviction drops proportionally. Also add a backtesting harness that uses saved data.

**Do NOT add new setups. Do NOT change filter thresholds. Do NOT touch the sim P&L flow yet.**

Read the ENTIRE codebase first. Especially: outcome_tracker.py conviction_score and _performance_bonus, strategy_log.py, bot.py scan_market, data_layer.py.

═══════════════════════════════════════
TASK 1 — AUTO-CALIBRATING CONVICTION BONUS
═══════════════════════════════════════

**CURRENT:** `_performance_bonus()` in outcome_tracker.py uses fixed buckets (+12 / +6 / 0 / -6 / -12) based on win rate tiers. It caps bonus at ±12 points which means even a 100% WR setup can only boost conviction by 12. Wayne wants: 100% WR setup → conviction approaches 100.

**NEW DESIGN — REPLACE `_performance_bonus()` with `_performance_adjustment()`:**

The new function returns an adjustment that SCALES with win rate and sample size. Key principles:
- Small samples get small adjustments (Bayesian prior prevents noise)
- High WR with large sample → big positive adjustment (up to +30)
- Low WR with large sample → big negative adjustment (down to -40, because losses cost more than wins gain)
- Adjustment range is wider than current ±12

```python
def _performance_adjustment(market: str, setup_type: str) -> tuple[int, str]:
    """
    Returns (adjustment_points, reason_string) for conviction score.
    
    Uses Bayesian prior of 50% WR with 10 pseudo-trades to prevent
    small-sample overreaction. As actual sample grows, the prior
    influence diminishes.
    
    Returns tuple so we can explain the adjustment in logs.
    """
    perf = _load_performance()
    key = f"{market}:{setup_type}"
    data = perf.get(key, {})
    wins = data.get("wins", 0)
    losses = data.get("losses", 0)
    total = wins + losses
    
    if total == 0:
        return 0, "no history yet"
    
    # Bayesian adjusted WR: 50% prior with 10 pseudo-trades
    # With 0 trades: 50%. With 10 real trades at 80% WR: (8+5)/(10+10) = 65%
    # With 40 real trades at 80% WR: (32+5)/(40+10) = 74%
    PRIOR_WR = 0.50
    PRIOR_STRENGTH = 10
    adjusted_wr = (wins + PRIOR_WR * PRIOR_STRENGTH) / (total + PRIOR_STRENGTH)
    
    # Sample confidence: 0.0 (no data) -> 1.0 (40+ trades)
    confidence = min(1.0, total / 40.0)
    
    # Distance from neutral (50%)
    distance = adjusted_wr - 0.50
    
    # Scale adjustment by confidence AND distance
    # Max upside: +30 at 100% WR with 40+ trades
    # Max downside: -40 at 0% WR with 40+ trades
    if distance >= 0:
        # Positive: wr * 2 * 30 * confidence, capped at 30
        adjustment = int(round(distance * 2 * 30 * confidence))
    else:
        # Negative: more severe penalty (losses hurt more than wins help)
        adjustment = int(round(distance * 2 * 40 * confidence))
    
    # Hard cap
    adjustment = max(-40, min(30, adjustment))
    
    reason = (
        f"{wins}W/{losses}L over {total} trades = {round(adjusted_wr*100,1)}% adjusted WR "
        f"(confidence {round(confidence*100)}%)"
    )
    return adjustment, reason
```

**Then update `conviction_score()` in outcome_tracker.py to use the new function:**

Replace the existing `_performance_bonus()` call block with:
```python
# LEARNING ADJUSTMENT — Bayesian confidence-weighted
market = setup.get("market", "")
if market and setup_type:
    adj, reason = _performance_adjustment(market, setup_type)
    s += adj
    bd["learning_adjustment"] = adj
    bd["learning_reason"] = reason
```

**Important**: do NOT delete `_performance_bonus()`. Keep it for backward compatibility (the /learning command still uses it). But mark it deprecated in a comment and have it internally delegate to the new function.

═══════════════════════════════════════
TASK 2 — RUNTIME CONVICTION VISIBILITY
═══════════════════════════════════════

The sim/telegram never shows the RAW score breakdown. Wayne wants to see WHY conviction landed where it did. Update the format_alert() function in bot.py to include an expandable "Why this score" section when a setup fires.

In format_alert, after the Chart Read line, add:

```python
# ── Score breakdown (Task 2) ──
if setup.get("score_breakdown"):
    bd = setup["score_breakdown"]
    score_lines = []
    for factor, points in sorted(bd.items(), key=lambda x: -abs(x[1]) if isinstance(x[1], (int, float)) else 0):
        if factor == "learning_reason":
            continue  # printed separately
        if isinstance(points, (int, float)) and points != 0:
            sign = "+" if points > 0 else ""
            score_lines.append(f"  {factor}: {sign}{points}")
    if score_lines:
        msg += f"━━━━━━━━━━━━━━━━━━\n🧮 *Score Breakdown:*\n"
        msg += "\n".join(score_lines) + "\n"
        if bd.get("learning_reason"):
            msg += f"  _learning: {bd['learning_reason']}_\n"
```

In scan_market, when calling `format_alert`, pass the `bd` (score breakdown) dict by attaching it to the `stp` dict:
```python
stp["score_breakdown"] = bd  # where bd comes from conviction_score()
```

═══════════════════════════════════════
TASK 3 — AUTO-REMOVAL SYSTEM FOR DEAD SETUPS
═══════════════════════════════════════

Wayne's rule: auto-review at 3-5 trades, auto-REMOVE at 10-15 trades if still 0% WR.

HOWEVER — Opus 4.7 pushback: "10-15 trades is still noise zone. 40+ is when you can remove with confidence."

Compromise: **Three-tier response system based on sample size:**

- **5 trades, 0% WR**: Flag for review (log warning, Telegram alert)
- **10 trades, 0% WR**: Suspend (existing suspension system handles this)
- **25 trades, still below 20% WR**: Permanent removal (add to dead_setups.json, excluded from detect_setups())

Add new file: `data/dead_setups.json`. Create helper functions in outcome_tracker.py:

```python
DEAD_SETUPS_FILE = os.path.join(_BASE_DIR, "data", "dead_setups.json")
_DEAD_MIN_TRADES = 25
_DEAD_WR_BELOW   = 20.0

def get_dead_setups() -> dict:
    """Read dead_setups.json — permanently removed setups."""
    if os.path.exists(DEAD_SETUPS_FILE):
        try:
            with open(DEAD_SETUPS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_dead_setups(data: dict):
    with open(DEAD_SETUPS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def is_setup_dead(market: str, setup: str) -> bool:
    return f"{market}:{setup}" in get_dead_setups()

def check_and_mark_dead_setups() -> list[str]:
    """
    Called at session close. Marks setups as permanently dead if:
      - total trades >= 25 AND win rate < 20%
    Returns list of newly marked strings for logging.
    """
    perf = _load_performance()
    dead = get_dead_setups()
    changes = []
    for key, data in perf.items():
        if key in dead:
            continue
        wins = data.get("wins", 0)
        losses = data.get("losses", 0)
        total = wins + losses
        if total >= _DEAD_MIN_TRADES:
            wr = wins / total * 100 if total > 0 else 0
            if wr < _DEAD_WR_BELOW:
                dead[key] = {
                    "reason": f"{wins}W/{losses}L ({round(wr,1)}% over {total} trades)",
                    "killed_at": datetime.now(timezone.utc).isoformat(),
                    "total_at_death": total,
                    "wr_at_death": round(wr, 1),
                }
                changes.append(f"DEAD {key} ({wins}W/{losses}L, {round(wr,1)}% WR)")
    _save_dead_setups(dead)
    return changes
```

**Integrate with scan_market in bot.py** — before the suspension check, add a dead-setup check:

```python
# Task 3: Check if setup is permanently dead
if ot.is_setup_dead(market, stp["type"]):
    log.info(f"[{market}] [{entry_tf}] {stp['type']} is DEAD — skipping entirely, not even logging")
    continue
```

Dead setups don't even shadow-log — they're gone. Suspension can shadow-log because it's reversible; death is permanent.

**Call `check_and_mark_dead_setups()`** from `_on_session_close` in bot.py, right after `check_and_update_suspensions()`. Include any newly dead setups in the suspension change Telegram message.

**Add early warning at 5 trades**: In check_and_update_suspensions, add logic that sends a Telegram warning when a setup hits exactly 5 trades with 0-20% WR: "⚠️ [MARKET:SETUP] hit 5 trades at X% WR — watching closely. Auto-suspend at 10 trades if no improvement."

═══════════════════════════════════════
TASK 4 — BOT-DRIVEN BACKTESTING ENGINE
═══════════════════════════════════════

Wayne wants: "our bot should do its own backtesting with the setups we have saved and saw already."

Build `backtest_engine.py` in the root folder. This is a new module that:

1. Reads archived historical bar data (use data_layer.get_frames to pull fresh history for the market)
2. Walks through bars sequentially, calling detect_setups + conviction_score as if live
3. Simulates entries at signal time, uses HIGH/LOW to check if target or stop hit first
4. Records every backtest result to `data/backtest_results.json`

```python
"""
backtest_engine.py — Historical simulation harness for NQ CALLS
================================================================
Walks through historical bars, runs setup detection and scoring
against each bar, simulates trade outcomes.

Primary use: validate setup performance before relying on live data.
Secondary use: re-test setups after calibration changes.
"""

from __future__ import annotations
import os, json
from datetime import datetime, timezone, timedelta
from typing import Optional
import pandas as pd
import numpy as np

import outcome_tracker as ot
from markets import get_market_config
from data_layer import get_frames

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKTEST_RESULTS_FILE = os.path.join(_BASE_DIR, "data", "backtest_results.json")


def run_backtest(market: str, days_back: int = 30, setup_filter: Optional[str] = None) -> dict:
    """
    Run a backtest for a single market.
    
    Args:
        market: "NQ", "GC", "BTC", or "SOL"
        days_back: how many days of history to test
        setup_filter: if set, only test this specific setup type
        
    Returns: dict with detailed results per setup
    """
    cfg = get_market_config(market)
    entry_tf = cfg.ENTRY_TIMEFRAMES[0]
    htf_key = cfg.HTF_CONFIRM
    
    frames = get_frames(market)
    df_entry_full = frames.get(entry_tf)
    df_htf_full   = frames.get(htf_key)
    
    if df_entry_full is None or len(df_entry_full) < 100:
        return {"error": "insufficient data"}
    
    # Walk forward: at each bar, use only bars BEFORE it for detection
    # Minimum 50 bars of history needed before first detection
    start_idx = 50
    results = {
        "market": market,
        "timeframe": entry_tf,
        "days_tested": days_back,
        "bars_tested": len(df_entry_full) - start_idx,
        "setups_detected": 0,
        "setups_fired": 0,
        "setups_won": 0,
        "setups_lost": 0,
        "per_setup": {},
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    
    # Walk bar by bar
    for i in range(start_idx, len(df_entry_full) - 20):
        # Slice historical data up to bar i (inclusive)
        df_hist = df_entry_full.iloc[:i+1]
        
        # Align HTF frame to same time
        current_ts = df_entry_full.index[i]
        df_htf_hist = df_htf_full[df_htf_full.index <= current_ts] if df_htf_full is not None else None
        
        if df_htf_hist is None or len(df_htf_hist) < 20:
            continue
        
        htf_bias = ot.structure_bias(df_htf_hist)
        
        # Detect setups at this historical moment
        try:
            setups = ot.detect_setups(df_hist, df_htf_hist, htf_bias)
        except Exception:
            continue
        
        if not setups:
            continue
        
        for stp in setups:
            if setup_filter and stp["type"] != setup_filter:
                continue
            stp["market"] = market
            results["setups_detected"] += 1
            setup_key = f"{market}:{stp['type']}"
            if setup_key not in results["per_setup"]:
                results["per_setup"][setup_key] = {"detected": 0, "fired": 0, "wins": 0, "losses": 0, "open": 0}
            results["per_setup"][setup_key]["detected"] += 1
            
            # Compute conviction at that moment
            try:
                adx_v = float(ot.adx(df_hist).iloc[-1])
                rsi_v = float(ot.rsi(df_hist["Close"]).iloc[-1])
                atr_v = float(ot.atr(df_hist).iloc[-1])
                vol_mean = float(df_hist["Volume"].rolling(20).mean().iloc[-1]) if len(df_hist) >= 20 else 0
                vol_last = float(df_hist["Volume"].iloc[-1])
                vol_ratio = vol_last / max(1e-9, vol_mean)
                
                tgt, rr, method = ot.structure_target(df_hist, stp["direction"], stp["entry"],
                                                      stp["raw_stop"], atr_v, market=market)
                if method == "no_target" or tgt == 0:
                    continue
                
                trend = 0  # simplified — full trend_score requires multi-TF frames
                conv, tier, _ = ot.conviction_score(
                    stp, trend, df_hist, df_htf_hist, False, adx_v, rsi_v, vol_ratio,
                    abs(tgt - stp["entry"]) / max(1e-9, atr_v)
                )
                
                if conv < cfg.MIN_CONVICTION:
                    continue
                
                results["setups_fired"] += 1
                results["per_setup"][setup_key]["fired"] += 1
                
                # Walk forward up to 20 bars to see if target or stop hit
                future = df_entry_full.iloc[i+1:i+21]
                hit_target = hit_stop = False
                for _, bar in future.iterrows():
                    bar_hi, bar_lo = float(bar["High"]), float(bar["Low"])
                    if stp["direction"] == "LONG":
                        if bar_lo <= stp["raw_stop"]:
                            hit_stop = True; break
                        if bar_hi >= tgt:
                            hit_target = True; break
                    else:
                        if bar_hi >= stp["raw_stop"]:
                            hit_stop = True; break
                        if bar_lo <= tgt:
                            hit_target = True; break
                
                if hit_target:
                    results["setups_won"] += 1
                    results["per_setup"][setup_key]["wins"] += 1
                elif hit_stop:
                    results["setups_lost"] += 1
                    results["per_setup"][setup_key]["losses"] += 1
                else:
                    results["per_setup"][setup_key]["open"] += 1
            except Exception:
                continue
    
    # Compute win rates per setup
    for key, d in results["per_setup"].items():
        total_closed = d["wins"] + d["losses"]
        d["win_rate"] = round(d["wins"] / max(1, total_closed) * 100, 1)
    
    results["finished_at"] = datetime.now(timezone.utc).isoformat()
    return results


def save_backtest_results(results: dict):
    """Append results to backtest_results.json."""
    all_results = []
    if os.path.exists(BACKTEST_RESULTS_FILE):
        try:
            with open(BACKTEST_RESULTS_FILE) as f:
                all_results = json.load(f)
        except Exception:
            pass
    all_results.append(results)
    with open(BACKTEST_RESULTS_FILE, "w") as f:
        json.dump(all_results, f, indent=2)


def run_full_backtest() -> str:
    """Run backtest on all 4 markets. Returns Telegram-formatted summary."""
    lines = ["🔬 *Backtest Results*", "━━━━━━━━━━━━━━━━━━"]
    for m in ["NQ", "GC", "BTC", "SOL"]:
        try:
            r = run_backtest(m)
            save_backtest_results(r)
            if "error" in r:
                lines.append(f"❌ {m}: {r['error']}")
                continue
            lines.append(f"*{m}*: {r['bars_tested']} bars | {r['setups_detected']} detected | {r['setups_fired']} fired")
            lines.append(f"  {r['setups_won']}W / {r['setups_lost']}L")
            for key, d in sorted(r["per_setup"].items(), key=lambda x: -x[1]["win_rate"])[:5]:
                _, setup = key.split(":", 1)
                lines.append(f"  • {setup}: {d['wins']}W/{d['losses']}L ({d['win_rate']}%)")
            lines.append("")
        except Exception as e:
            lines.append(f"❌ {m}: {e}")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("_Results saved to data/backtest_results.json_")
    return "\n".join(lines)
```

**Add /backtest command in bot.py:**

```python
async def cmd_backtest(u, c):
    """Run backtest on all markets. Slow — takes 30-60 seconds."""
    await u.message.reply_text("⏳ Running backtest on all 4 markets — this takes ~60s...")
    try:
        from backtest_engine import run_full_backtest
        summary = run_full_backtest()
        await tg_send(c.application, summary)
        await u.message.reply_text("✅ Backtest complete. See results above.")
    except Exception as e:
        await u.message.reply_text(f"❌ Backtest error: {e}")
```

Register in main(): `("backtest", cmd_backtest)`.

═══════════════════════════════════════
TASK 5 — BACKTEST-INFORMED INITIAL CONVICTION
═══════════════════════════════════════

When backtest results exist, `_performance_adjustment()` should consider them too — not just live trades.

Modify `_performance_adjustment()` to optionally include backtest history:

```python
def _load_backtest_priors() -> dict:
    """Load historical backtest performance as a prior for conviction adjustment."""
    bt_file = os.path.join(_BASE_DIR, "data", "backtest_results.json")
    if not os.path.exists(bt_file):
        return {}
    try:
        with open(bt_file) as f:
            all_results = json.load(f)
    except Exception:
        return {}
    
    # Aggregate across all backtest runs
    priors = {}
    for r in all_results:
        for key, d in r.get("per_setup", {}).items():
            if key not in priors:
                priors[key] = {"wins": 0, "losses": 0}
            priors[key]["wins"]   += d.get("wins", 0)
            priors[key]["losses"] += d.get("losses", 0)
    return priors
```

Then in `_performance_adjustment()`, blend live results with backtest priors:
- Live trades weighted 3x backtest trades (live is more trustworthy)
- Backtest fills in gaps where we have few live trades

═══════════════════════════════════════
TASK 6 — SCHEDULED BACKTEST
═══════════════════════════════════════

Auto-run a backtest once per week on Sunday at 9 PM ET. Add to scan_loop in bot.py, alongside the daily report scheduler.

Also run once at startup IF backtest_results.json doesn't exist or is empty (first-time setup).

═══════════════════════════════════════
SCOPE BOUNDARIES — WHAT NOT TO TOUCH
═══════════════════════════════════════

Do NOT:
- Add new setups
- Change filter thresholds
- Modify Telegram alert format beyond the score breakdown addition
- Change sim P&L flow
- Remove existing setups (auto-removal is built-in for 25+ trade samples only)

DO:
- Replace _performance_bonus with wider-range _performance_adjustment
- Add dead_setups.json system with 3-tier response
- Build backtest_engine.py
- Add /backtest command
- Auto-run backtest weekly + at first startup
- Update score breakdown visibility in alerts

═══════════════════════════════════════
VERIFY & DEPLOY
═══════════════════════════════════════

1. Syntax check:
```
python -c "import ast; [print(f) or ast.parse(open(f, encoding='utf-8').read()) for f in ['bot.py', 'outcome_tracker.py', 'backtest_engine.py']]"
```

2. Smoke test backtest:
```
python -c "from backtest_engine import run_backtest; r = run_backtest('NQ', days_back=7); print('backtest ok:', r.get('setups_detected', 'fail'))"
```

3. If clean:
```
git add -A
git commit -m "Batch 2B: auto-calibrating conviction, dead-setup removal, bot-driven backtesting"
git push origin main
```

4. Report back with:
   - Every new function + file
   - Every function modified
   - Any warnings
   - Confirmation all 6 tasks complete
