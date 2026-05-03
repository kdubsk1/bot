"""
crypto_sim.py — Persistent $1,000 crypto build-up sim track for BTC/SOL.

Runs alongside the Topstep eval sim (sim_account.py). Opens a position on every
BTC/SOL alert, sizes to risk 1.5% of current balance, applies 10x leverage,
holds up to 7 days, exits on target/stop/bias-flip/max-hold.

State persists across sessions (no daily reset). Every trade record carries the
bot's reasoning context so later analysis can ask "what setups grow the account."
"""
from __future__ import annotations

import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from safe_io import atomic_write_json

_log = logging.getLogger("nqcalls.cryptosim")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE_DIR, "data")
os.makedirs(_DATA_DIR, exist_ok=True)
CRYPTO_SIM_FILE = os.path.join(_DATA_DIR, "crypto_sim.json")

DEFAULT_STATE = {
    "enabled":           True,
    "balance":           1000.0,
    "starting_balance":  1000.0,
    "peak_balance":      1000.0,
    "profit_target":     1500.0,   # May 2: $1.5k = 50% gain target on the $1k starting bal
    "leverage":          10,
    "account_risk_pct":  1.5,
    "max_hold_days":     7,
    "open_trades":       [],
    "closed_trades":     [],
    "total_pnl":         0.0,
    "total_trades":      0,
    "wins":              0,
    "losses":            0,
    "created_at":        "",
}


# ── State load/save ───────────────────────────────────────────────
def load_crypto_state() -> dict:
    if os.path.exists(CRYPTO_SIM_FILE):
        try:
            with open(CRYPTO_SIM_FILE, "r") as f:
                data = json.load(f)
            for k, v in DEFAULT_STATE.items():
                data.setdefault(k, v if not isinstance(v, list) else [])
            return data
        except Exception as e:
            _log.warning("crypto_sim state load failed (%s) — starting fresh", e)
    state = dict(DEFAULT_STATE)
    state["open_trades"] = []
    state["closed_trades"] = []
    state["created_at"] = datetime.now(timezone.utc).isoformat()
    save_crypto_state(state)
    return state


def save_crypto_state(state: dict) -> None:
    try:
        atomic_write_json(CRYPTO_SIM_FILE, state)
    except Exception as e:
        _log.warning("crypto_sim state save failed: %s", e)


def set_enabled(enabled: bool) -> None:
    state = load_crypto_state()
    state["enabled"] = bool(enabled)
    save_crypto_state(state)


def reset_crypto_account() -> None:
    fresh = dict(DEFAULT_STATE)
    fresh["open_trades"] = []
    fresh["closed_trades"] = []
    fresh["created_at"] = datetime.now(timezone.utc).isoformat()
    save_crypto_state(fresh)


# ── Open trade ─────────────────────────────────────────────────────
def open_crypto_trade(alert_id: str, market: str, direction: str,
                      entry: float, stop: float, target: float,
                      conviction: int, tier: str,
                      context: dict) -> dict:
    state = load_crypto_state()
    if not state.get("enabled"):
        return {}

    if any(t.get("alert_id") == alert_id for t in state["open_trades"]):
        return {}

    entry = float(entry)
    stop  = float(stop)
    target = float(target)

    if entry <= 0 or abs(entry - stop) <= 0:
        return {}

    risk_pct          = float(state["account_risk_pct"]) / 100.0
    risk_dollars      = float(state["balance"]) * risk_pct
    stop_pct          = abs(entry - stop) / entry
    if stop_pct <= 0:
        return {}
    position_size_usd = risk_dollars / stop_pct
    notional_usd      = position_size_usd * float(state["leverage"])

    now = datetime.now(timezone.utc)
    trade = {
        "alert_id":          alert_id,
        "market":            market,
        "direction":         direction,
        "entry":             entry,
        "stop":              stop,
        "target":            target,
        "leverage":          state["leverage"],
        "position_size_usd": round(position_size_usd, 2),
        "notional_usd":      round(notional_usd, 2),
        "risk_dollars":      round(risk_dollars, 2),
        "opened_at":         now.isoformat(),
        "max_hold_until":    (now + timedelta(days=int(state["max_hold_days"]))).isoformat(),
        "tier":              tier,
        "conviction":        int(conviction),
        "context":           dict(context or {}),
        "status":            "OPEN",
    }
    state["open_trades"].append(trade)
    save_crypto_state(state)
    return trade


# ── Close trade ────────────────────────────────────────────────────
def close_crypto_trade(alert_id: str, exit_price: float,
                       result: str, exit_reason: str) -> Optional[dict]:
    state = load_crypto_state()
    match = None
    for t in state["open_trades"]:
        if t.get("alert_id") == alert_id:
            match = t
            break
    if match is None:
        return None

    entry = float(match["entry"])
    direction = match["direction"]
    if direction == "LONG":
        pct_move = (float(exit_price) - entry) / entry
    else:
        pct_move = (entry - float(exit_price)) / entry
    pnl_dollars = float(match["position_size_usd"]) * pct_move * float(match["leverage"])

    opened_at = datetime.fromisoformat(match["opened_at"])
    now = datetime.now(timezone.utc)
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    held_hours = (now - opened_at).total_seconds() / 3600.0

    match["status"]      = "CLOSED"
    match["exit_price"]  = float(exit_price)
    match["exit_reason"] = exit_reason
    match["result"]      = result
    match["pnl_dollars"] = round(pnl_dollars, 2)
    match["closed_at"]   = now.isoformat()
    match["held_hours"]  = round(held_hours, 2)

    state["balance"]      = round(float(state["balance"]) + pnl_dollars, 2)
    state["peak_balance"] = max(float(state.get("peak_balance", state["balance"])), state["balance"])
    state["total_pnl"]    = round(float(state.get("total_pnl", 0.0)) + pnl_dollars, 2)
    state["total_trades"] = int(state.get("total_trades", 0)) + 1
    if result == "WIN":
        state["wins"] = int(state.get("wins", 0)) + 1
    else:
        state["losses"] = int(state.get("losses", 0)) + 1

    state["closed_trades"].append(match)
    state["open_trades"] = [t for t in state["open_trades"] if t.get("alert_id") != alert_id]
    save_crypto_state(state)
    return match


# ── Auto check open trades ─────────────────────────────────────────
def auto_check_crypto_trades(live_prices: dict, live_frames: dict) -> list:
    state = load_crypto_state()
    closed = []
    if not state["open_trades"]:
        return closed

    now = datetime.now(timezone.utc)

    for t in list(state["open_trades"]):
        market = t.get("market")
        alert_id = t.get("alert_id")
        direction = t.get("direction")
        price = live_prices.get(market)
        try:
            entry = float(t["entry"])
            stop  = float(t["stop"])
            target = float(t["target"])
        except Exception:
            continue

        # 1) Max-hold cutoff
        try:
            max_hold = datetime.fromisoformat(t.get("max_hold_until", ""))
            if max_hold.tzinfo is None:
                max_hold = max_hold.replace(tzinfo=timezone.utc)
        except Exception:
            max_hold = None
        if max_hold is not None and now > max_hold and price is not None:
            if direction == "LONG":
                pct_move = (float(price) - entry) / entry
            else:
                pct_move = (entry - float(price)) / entry
            pnl_est = float(t["position_size_usd"]) * pct_move * float(t["leverage"])
            r = close_crypto_trade(alert_id, float(price),
                                   "WIN" if pnl_est > 0 else "LOSS",
                                   "max_hold_exceeded")
            if r:
                closed.append(r)
            continue

        # 2) Bias-flip
        try:
            from outcome_tracker import trend_score
            frames = live_frames.get(market, {})
            if frames:
                current_trend, _ = trend_score(frames, market)
                ctx = t.get("context", {}) or {}
                orig_trend = int(ctx.get("trend_score", 0))
                if direction == "SHORT" and orig_trend <= -3 and current_trend >= 3 and price is not None:
                    if direction == "LONG":
                        pct_move = (float(price) - entry) / entry
                    else:
                        pct_move = (entry - float(price)) / entry
                    pnl_est = float(t["position_size_usd"]) * pct_move * float(t["leverage"])
                    r = close_crypto_trade(alert_id, float(price),
                                           "WIN" if pnl_est > 0 else "LOSS",
                                           "bias_flip")
                    if r:
                        closed.append(r)
                    continue
                if direction == "LONG" and orig_trend >= 3 and current_trend <= -3 and price is not None:
                    if direction == "LONG":
                        pct_move = (float(price) - entry) / entry
                    else:
                        pct_move = (entry - float(price)) / entry
                    pnl_est = float(t["position_size_usd"]) * pct_move * float(t["leverage"])
                    r = close_crypto_trade(alert_id, float(price),
                                           "WIN" if pnl_est > 0 else "LOSS",
                                           "bias_flip")
                    if r:
                        closed.append(r)
                    continue
        except Exception as e:
            _log.warning("crypto bias-flip check failed for %s: %s", market, e)

        # 3) Target / stop hit
        if price is None:
            continue
        price = float(price)
        hit_target = hit_stop = False
        if direction == "LONG":
            if price >= target:
                hit_target = True
            if price <= stop:
                hit_stop = True
        else:  # SHORT
            if price <= target:
                hit_target = True
            if price >= stop:
                hit_stop = True

        if hit_stop:
            r = close_crypto_trade(alert_id, stop, "LOSS", "stop_hit")
            if r:
                closed.append(r)
        elif hit_target:
            r = close_crypto_trade(alert_id, target, "WIN", "target_hit")
            if r:
                closed.append(r)

    return closed


# ── Format alert block ─────────────────────────────────────────────
def format_crypto_sim_block(market: str, tier: str,
                            entry: float, stop: float, target: float,
                            alert_id: str, conviction: int,
                            context: dict) -> str:
    state = load_crypto_state()
    if not state.get("enabled"):
        return ""
    if market not in ("BTC", "SOL"):
        return ""

    entry = float(entry)
    stop  = float(stop)
    target = float(target)

    if entry <= 0 or abs(entry - stop) <= 0:
        return ""

    direction = "LONG" if target > entry else "SHORT"

    risk_pct          = float(state["account_risk_pct"]) / 100.0
    risk_dollars      = float(state["balance"]) * risk_pct
    stop_pct          = abs(entry - stop) / entry
    if stop_pct <= 0:
        return ""
    position_size_usd = risk_dollars / stop_pct
    notional_usd      = position_size_usd * float(state["leverage"])

    target_pct = abs(target - entry) / entry
    reward_est = position_size_usd * target_pct * float(state["leverage"])
    rr = abs(target - entry) / abs(entry - stop) if abs(entry - stop) > 0 else 0.0

    open_crypto_trade(alert_id, market, direction,
                      entry, stop, target,
                      int(conviction), tier, context or {})

    ctx = context or {}
    trend = ctx.get("trend_score", 0)
    rsi = ctx.get("rsi", 0)
    adx = ctx.get("adx", 0)
    regime = ctx.get("regime", "UNKNOWN")

    lev = int(state["leverage"])
    bal = float(state["balance"])
    risk_pct_disp = float(state["account_risk_pct"])
    max_hold_days = int(state["max_hold_days"])

    # May 2 Wave 5: surface lifetime stats inline so the alert tells the
    # whole story without needing /cryptostatus. Wayne flagged that the
    # alert balance "doesn't update" feeling — it does, but the lifetime
    # context wasn't visible per-alert.
    starting     = float(state.get("starting_balance", 1000.0))
    pct_lifetime = ((bal - starting) / starting * 100.0) if starting > 0 else 0.0
    total_pnl    = float(state.get("total_pnl", 0.0))
    total_pnl_s  = f"+${total_pnl:,.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):,.2f}"
    wins_l       = int(state.get("wins", 0))
    losses_l     = int(state.get("losses", 0))
    n_l          = wins_l + losses_l
    wr_l         = (wins_l / n_l * 100.0) if n_l > 0 else 0.0
    peak_bal     = float(state.get("peak_balance", bal))
    drawdown     = max(0.0, peak_bal - bal)
    profit_tgt   = float(state.get("profit_target", 1500.0))
    tgt_progress = max(0.0, min(100.0, ((bal - starting) / max(1.0, profit_tgt - starting)) * 100.0))
    bal_icon     = "🟢" if total_pnl >= 0 else "🔴"

    lines = [
        "🪙 *CRYPTO SIM — Position Opened*",
        f"  📦 Size: `${position_size_usd:,.2f}` → `${notional_usd:,.2f}` notional ({lev}x lev)",
        f"  🎯 Risk: `${risk_dollars:,.2f}` ({risk_pct_disp:.1f}%) | Max hold: {max_hold_days}d",
        f"  💰 Reward: `${reward_est:,.2f}` ({rr:.2f}R if target hits)",
        "  ━━━━━━━━━━━━",
        f"  {bal_icon} *Lifetime:* `${bal:,.2f}` ({pct_lifetime:+.1f}% all-time)",
        f"  *Total P&L:* `{total_pnl_s}`  |  *WR:* `{wr_l:.1f}%` ({wins_l}W/{losses_l}L)",
        f"  *Peak:* `${peak_bal:,.2f}`  |  *Drawdown:* `${drawdown:,.2f}`",
        f"  *Target:* `{tgt_progress:.1f}%` of `${profit_tgt:,.0f}` goal",
        "  ━━━━━━━━━━━━",
        f"  📊 Context: Trend `{trend}` | RSI {rsi} | ADX {adx} | {regime}",
    ]
    return "\n".join(lines)


# ── Reconcile with outcomes.csv (Wave 5 stale-trade fix) ────────────
def reconcile_with_outcomes() -> int:
    """
    Wave 5 fix for the stale crypto open_trades issue.

    The bot has two systems that close trades independently:
      - outcome_tracker.py writes results to outcomes.csv (the source of truth)
      - crypto_sim.py tracks paper sim positions in crypto_sim.json

    When outcome_tracker closes a trade via auto-resolve (stop hit / target hit
    on a future bar), it doesn't always reach back into crypto_sim to close
    the matching open_trade. Result: open_trades grows stale even though the
    balance and total_pnl are correct (the close path runs SOME of the time).

    This function walks any open_trade and asks outcomes.csv: "is this alert
    actually closed? if so, close it here too with the correct exit/result."

    Returns the number of trades reconciled. Safe to call repeatedly — no-ops
    when everything is already in sync.
    """
    state = load_crypto_state()
    if not state.get("open_trades"):
        return 0

    outcomes_path = os.path.join(_BASE_DIR, "outcomes.csv")
    if not os.path.exists(outcomes_path):
        return 0

    # Read outcomes.csv into a dict of {alert_id: (status, result, exit_price)}
    # Only the LAST occurrence of each alert_id matters (most recent state).
    closed_lookup = {}
    try:
        with open(outcomes_path, "r", encoding="utf-8") as f:
            header_line = f.readline()
            cols = [c.strip() for c in header_line.strip().split(",")]
            try:
                idx_id     = cols.index("alert_id")
                idx_status = cols.index("status")
                idx_result = cols.index("result")
                idx_exit   = cols.index("exit_price")
            except ValueError:
                _log.warning("reconcile_with_outcomes: missing required columns in outcomes.csv")
                return 0
            for line in f:
                row = line.strip().split(",")
                if len(row) <= max(idx_id, idx_status, idx_result, idx_exit):
                    continue
                aid    = row[idx_id]
                status = row[idx_status]
                result = row[idx_result]
                ep_raw = row[idx_exit]
                if status != "CLOSED":
                    continue
                try:
                    exit_price = float(ep_raw) if ep_raw else 0.0
                except Exception:
                    exit_price = 0.0
                if exit_price > 0:
                    closed_lookup[aid] = (status, result, exit_price)
    except Exception as e:
        _log.warning("reconcile_with_outcomes: failed to read outcomes.csv: %s", e)
        return 0

    # Walk open_trades and close any that are CLOSED in outcomes.csv
    reconciled = 0
    for t in list(state["open_trades"]):
        aid = t.get("alert_id")
        if aid in closed_lookup:
            _, result, exit_price = closed_lookup[aid]
            r = close_crypto_trade(aid, exit_price, result, "outcomes_reconcile")
            if r is not None:
                reconciled += 1
                _log.info("reconcile_with_outcomes: closed stale crypto trade %s "
                          "(%s %s, result=%s, exit=%.4f)",
                          aid, t.get("market"), t.get("direction"),
                          result, exit_price)
    return reconciled


# ── Status text ────────────────────────────────────────────────────
def get_crypto_status_text() -> str:
    state = load_crypto_state()
    bal = float(state.get("balance", 0.0))
    start = float(state.get("starting_balance", 1000.0))
    pct = ((bal - start) / start * 100.0) if start > 0 else 0.0
    total_pnl = float(state.get("total_pnl", 0.0))
    pnl_str = f"+${total_pnl:,.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):,.2f}"

    open_trades = state.get("open_trades", []) or []
    closed = state.get("closed_trades", []) or []
    wins = int(state.get("wins", 0))
    losses = int(state.get("losses", 0))
    n = wins + losses
    wr = (wins / n * 100.0) if n > 0 else 0.0

    now = datetime.now(timezone.utc)
    open_lines = []
    for t in open_trades[:5]:
        try:
            opened_at = datetime.fromisoformat(t.get("opened_at", ""))
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)
            held_h = (now - opened_at).total_seconds() / 3600.0
            held_str = f"{held_h:.1f}h"
        except Exception:
            held_str = "?"
        open_lines.append(
            f"  • {t.get('market')} {t.get('direction')} @ {t.get('entry')} (held {held_str})"
        )

    best = None
    worst = None
    for t in closed:
        p = float(t.get("pnl_dollars", 0))
        if best is None or p > best[0]:
            best = (p, t)
        if worst is None or p < worst[0]:
            worst = (p, t)

    def _trade_label(t):
        m = t.get("market", "?")
        ctx = t.get("context", {}) or {}
        setup = ctx.get("chart_read", "")
        return f"{m} {setup[:40]}" if setup else m

    # May 2 Wave 5: mirror /simstatus two-section format
    peak_bal   = float(state.get("peak_balance", bal))
    drawdown   = max(0.0, peak_bal - bal)
    profit_tgt = float(state.get("profit_target", 1500.0))
    tgt_progress = max(0.0, min(100.0, ((bal - start) / max(1.0, profit_tgt - start)) * 100.0))
    bal_icon = "🟢" if total_pnl >= 0 else "🔴"

    lines = [
        "🪙 *Crypto Sim — Build-Up Account*",
        "━━━━━━━━━━━━━━━━━━",
        "🏆 *LIFETIME STATS*",
        f"  {bal_icon} Balance:   `${bal:,.2f}` ({pct:+.1f}%)",
        f"  Total P&L:    `{pnl_str}`",
        f"  Target:       `{tgt_progress:.1f}%` of `${profit_tgt:,.0f}` goal",
        f"  Peak:         `${peak_bal:,.2f}`  (drawdown: `${drawdown:,.2f}`)",
        f"  Trades:       `{n}` ({wins}W/{losses}L — {wr:.1f}% WR)",
        "━━━━━━━━━━━━━━━━━━",
        "📂 *OPEN POSITIONS*",
        f"  Currently open: `{len(open_trades)}` trade(s)",
    ]
    if open_lines:
        lines.extend(open_lines)
    else:
        lines.append("  (none)")
    if best is not None or worst is not None:
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("🏅 *RECORD HOLDERS*")
        if best is not None:
            bp, bt = best
            b_sign = f"+${bp:,.2f}" if bp >= 0 else f"-${abs(bp):,.2f}"
            lines.append(f"  Best:  {b_sign} ({_trade_label(bt)})")
        if worst is not None:
            wp, wt = worst
            w_sign = f"+${wp:,.2f}" if wp >= 0 else f"-${abs(wp):,.2f}"
            lines.append(f"  Worst: {w_sign} ({_trade_label(wt)})")
    return "\n".join(lines)
