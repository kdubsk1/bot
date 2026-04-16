"""
sim_account.py - NQ CALLS Paper Trading / Eval Simulator
==========================================================
Tracks a simulated trading account alongside real bot alerts.

Now uses PositionSizer (position_sizer.py) for intelligent contract sizing:
  - Quarter-Kelly based on historical edge
  - Survival constraint: max 12% of cushion per trade
  - Conviction, regime, correlation multipliers
  - Full sizing reasoning shown in every alert
"""

from __future__ import annotations
import os, json, logging
from datetime import datetime, timezone, timedelta
from typing import Optional

_log = logging.getLogger("nqcalls.sim")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SIM_FILE  = os.path.join(_BASE_DIR, "data", "sim_account.json")
LIFETIME_STATS_FILE = os.path.join(_BASE_DIR, "data", "lifetime_stats.json")
os.makedirs(os.path.join(_BASE_DIR, "data"), exist_ok=True)
os.makedirs(os.path.join(_BASE_DIR, "data", "archive"), exist_ok=True)

# ── Contract specs ────────────────────────────────────────────────
NQ_POINT_VALUE  = 20.0
MNQ_POINT_VALUE = 2.0
GC_POINT_VALUE  = 10.0

# ── Eval account presets ──────────────────────────────────────────
EVAL_PRESETS = {
    "50k":    {"balance": 50_000,  "daily_loss_limit": 1_000, "max_drawdown": 2_000,  "profit_target": 3_000},
    "100k":   {"balance": 100_000, "daily_loss_limit": 2_000, "max_drawdown": 4_500,  "profit_target": 6_000},
    "150k":   {"balance": 150_000, "daily_loss_limit": 3_000, "max_drawdown": 6_000,  "profit_target": 9_000},
    "custom": {"balance": 50_000,  "daily_loss_limit": 1_000, "max_drawdown": 2_000,  "profit_target": 3_000},
}

SIM_HISTORY_FILE = os.path.join(_BASE_DIR, "data", "sim_history.json")

DEFAULT_STATE = {
    "enabled":          False,
    "mode":             "eval",
    "preset":           "50k",
    "balance":          50_000.0,
    "peak_balance":     50_000.0,
    "starting_balance": 50_000.0,
    "daily_loss_limit": 1_000.0,
    "max_drawdown":     2_000.0,
    "profit_target":    3_000.0,
    "max_contracts_NQ": 10,
    "max_contracts_GC": 5,
    "use_mnq":          True,
    "account_risk_pct": 1.5,
    "today_pnl":        0.0,
    "today_date":       "",
    "session_date":     "",
    "total_pnl":        0.0,
    "trades":           [],
    "open_sim_trades":  [],
}

# ── History ───────────────────────────────────────────────────────
def _load_history() -> list:
    if os.path.exists(SIM_HISTORY_FILE):
        try:
            with open(SIM_HISTORY_FILE) as f:
                return json.load(f)
        except:
            pass
    return []

def _save_history(history: list):
    with open(SIM_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

def _archive_day(state: dict):
    today = state.get("today_date", "")
    if not today or state.get("today_pnl", 0) == 0:
        return
    history = _load_history()
    if any(h.get("date") == today for h in history):
        return
    today_trades = [t for t in state.get("trades", []) if today in t.get("closed_at", "")]
    wins   = sum(1 for t in today_trades if t.get("result") == "WIN")
    losses = sum(1 for t in today_trades if t.get("result") == "LOSS")
    history.append({
        "date":    today,
        "pnl":     round(state.get("today_pnl", 0), 2),
        "trades":  len(today_trades),
        "wins":    wins,
        "losses":  losses,
        "balance": round(state.get("balance", 0), 2),
        "preset":  state.get("preset", "50k"),
    })
    _save_history(history)

def get_period_summary(days: int = 7) -> dict:
    history  = _load_history()
    cutoff   = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    recent   = [h for h in history if h.get("date", "") >= cutoff]
    pnl      = sum(h.get("pnl", 0) for h in recent)
    wins     = sum(h.get("wins", 0) for h in recent)
    losses   = sum(h.get("losses", 0) for h in recent)
    return {
        "days": days,
        "trading_days": len(recent),
        "pnl":    round(pnl, 2),
        "wins":   wins,
        "losses": losses,
        "wr":     round(wins / max(1, wins + losses) * 100, 1),
        "days_data": recent[-7:],
    }

# ── Load / Save ───────────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(SIM_FILE):
        try:
            with open(SIM_FILE, "r") as f:
                data = json.load(f)
            for k, v in DEFAULT_STATE.items():
                data.setdefault(k, v)
        except Exception:
            data = dict(DEFAULT_STATE)
    else:
        data = dict(DEFAULT_STATE)

    # Safety: force MNQ for sub-$150k accounts
    if not data.get("use_mnq", False):
        _log.warning("Forcing use_mnq=True — full NQ inappropriate for sub-$150k accounts")
        data["use_mnq"] = True
        save_state(data)

    # Check if session rolled over (handles bot restart after boundary)
    data = _ensure_session_current(data)
    return data

def save_state(state: dict):
    with open(SIM_FILE, "w") as f:
        json.dump(state, f, indent=2)

def get_state() -> dict:
    return load_state()

# ── Session reset ────────────────────────────────────────────────
def _get_session_date() -> str:
    """Get current session date from session_clock."""
    try:
        from session_clock import get_session_date
        return get_session_date()
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")

def _ensure_session_current(state: dict) -> dict:
    """
    Compare state['session_date'] to get_session_date().
    If they differ, a new session started (or bot was restarted after
    a session boundary was missed).
    Action: archive old state, then reset to fresh preset balance.
    This runs on EVERY load_state() call — the only reset mechanism.
    """
    current_sid = _get_session_date()
    stored_sid = state.get("session_date", state.get("today_date", ""))

    if stored_sid and stored_sid != current_sid:
        _log.info("Session rolled: %s -> %s — archiving and resetting", stored_sid, current_sid)
        _archive_day(state)
        _archive_sim_state(state, stored_sid)
        _update_lifetime_stats(state)
        # Reset to fresh preset — this prevents the $72k accumulation bug
        _reset_to_fresh_preset(state, current_sid)
        save_state(state)
    elif not stored_sid:
        state["today_date"]   = current_sid
        state["session_date"] = current_sid

    return state


def _reset_to_fresh_preset(state: dict, new_session_date: str):
    """Reset state to fresh preset values while keeping settings."""
    preset_key = state.get("preset", "50k")
    preset = EVAL_PRESETS.get(preset_key, EVAL_PRESETS["50k"])
    state["balance"]          = float(preset["balance"])
    state["peak_balance"]     = float(preset["balance"])
    state["starting_balance"] = float(preset["balance"])
    state["daily_loss_limit"] = float(preset["daily_loss_limit"])
    state["max_drawdown"]     = float(preset["max_drawdown"])
    state["profit_target"]    = float(preset["profit_target"])
    state["today_pnl"]        = 0.0
    state["total_pnl"]        = 0.0
    state["today_date"]       = new_session_date
    state["session_date"]     = new_session_date
    state["trades"]           = []
    state["open_sim_trades"]  = []


def on_session_close(state: dict = None) -> dict:
    """
    Called by SessionClock on FUTURES_SESSION_CLOSE event.
    Archives and resets the sim for the closing session.
    Returns the new state.
    """
    if state is None:
        state = _load_state_raw()
    sid = state.get("session_date", state.get("today_date", ""))
    if not sid:
        sid = _get_session_date()

    _archive_day(state)
    _archive_sim_state(state, sid)
    _update_lifetime_stats(state)

    # Reset to fresh preset for next session
    new_sid = _get_session_date()
    _reset_to_fresh_preset(state, new_sid)
    save_state(state)
    _log.info("Session close reset complete: %s -> %s", sid, new_sid)
    return state


def _reset_daily_if_needed(state: dict) -> dict:
    """Legacy compatibility shim — session logic is now in load_state() via _ensure_session_current."""
    return state

def _load_state_raw() -> dict:
    """Load state without triggering ensure_session_current (avoids recursion)."""
    if os.path.exists(SIM_FILE):
        try:
            with open(SIM_FILE, "r") as f:
                data = json.load(f)
            for k, v in DEFAULT_STATE.items():
                data.setdefault(k, v)
            return data
        except Exception:
            pass
    return dict(DEFAULT_STATE)


def _archive_sim_state(state: dict, session_id: str):
    """Archive sim_account.json to data/archive/sim_YYYY-MM-DD.json."""
    if not session_id:
        return
    archive_path = os.path.join(_BASE_DIR, "data", "archive", f"sim_{session_id}.json")
    try:
        with open(archive_path, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def _update_lifetime_stats(state: dict):
    """Update lifetime_stats.json with the finishing session's results."""
    stats = _load_lifetime_stats()

    today_trades = state.get("trades", [])
    # Filter to trades closed this session
    sid = state.get("session_date", state.get("today_date", ""))
    session_trades = []
    for t in today_trades:
        closed_at = t.get("closed_at", "")
        if t.get("status") == "CLOSED" and closed_at:
            session_trades.append(t)

    if not session_trades and state.get("today_pnl", 0) == 0:
        return  # Nothing to record

    session_pnl = state.get("today_pnl", 0.0)
    wins = sum(1 for t in session_trades if t.get("result") == "WIN")
    losses = sum(1 for t in session_trades if t.get("result") == "LOSS")

    stats["total_sessions"]    = stats.get("total_sessions", 0) + 1
    stats["total_trades"]      = stats.get("total_trades", 0) + len(session_trades)
    stats["total_wins"]        = stats.get("total_wins", 0) + wins
    stats["total_losses"]      = stats.get("total_losses", 0) + losses
    stats["total_pnl_dollars"] = round(stats.get("total_pnl_dollars", 0) + session_pnl, 2)

    if session_pnl > stats.get("best_session_pnl", float("-inf")):
        stats["best_session_pnl"] = round(session_pnl, 2)
    if session_pnl < stats.get("worst_session_pnl", float("inf")):
        stats["worst_session_pnl"] = round(session_pnl, 2)

    # Per-setup stats
    per_setup = stats.get("per_setup_stats", {})
    for t in session_trades:
        setup = t.get("tier", "UNKNOWN")
        market = t.get("market", "?")
        key = f"{market}:{setup}"
        if key not in per_setup:
            per_setup[key] = {"wins": 0, "losses": 0, "pnl": 0.0}
        if t.get("result") == "WIN":
            per_setup[key]["wins"] += 1
        elif t.get("result") == "LOSS":
            per_setup[key]["losses"] += 1
        per_setup[key]["pnl"] = round(per_setup[key]["pnl"] + t.get("pnl", 0), 2)
    stats["per_setup_stats"] = per_setup

    # Best setup overall
    if per_setup:
        best_key = max(per_setup, key=lambda k: per_setup[k].get("pnl", 0))
        stats["best_setup_overall"] = best_key

    stats["last_updated"] = datetime.now(timezone.utc).isoformat()
    _save_lifetime_stats(stats)


def _load_lifetime_stats() -> dict:
    if os.path.exists(LIFETIME_STATS_FILE):
        try:
            with open(LIFETIME_STATS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "total_sessions": 0,
        "total_trades": 0,
        "total_wins": 0,
        "total_losses": 0,
        "total_pnl_dollars": 0.0,
        "best_session_pnl": 0.0,
        "worst_session_pnl": 0.0,
        "best_setup_overall": "N/A",
        "per_setup_stats": {},
    }


def _save_lifetime_stats(stats: dict):
    try:
        with open(LIFETIME_STATS_FILE, "w") as f:
            json.dump(stats, f, indent=2)
    except Exception:
        pass


def get_lifetime_stats() -> dict:
    return _load_lifetime_stats()


def lifetime_stats_text() -> str:
    """Returns formatted lifetime stats for /lifetime command."""
    stats = _load_lifetime_stats()
    total_t = stats.get("total_trades", 0)
    total_w = stats.get("total_wins", 0)
    total_l = stats.get("total_losses", 0)
    wr = round(total_w / max(1, total_w + total_l) * 100, 1)
    pnl = stats.get("total_pnl_dollars", 0)
    pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
    best = stats.get("best_session_pnl", 0)
    worst = stats.get("worst_session_pnl", 0)
    best_str = f"+${best:,.2f}" if best >= 0 else f"-${abs(best):,.2f}"
    worst_str = f"+${worst:,.2f}" if worst >= 0 else f"-${abs(worst):,.2f}"

    lines = [
        "📊 *Lifetime Stats*",
        "━━━━━━━━━━━━━━━━━━",
        f"*Sessions:* `{stats.get('total_sessions', 0)}`",
        f"*Total Trades:* `{total_t}`",
        f"*W/L:* `{total_w}W / {total_l}L` ({wr}% WR)",
        f"*Total P&L:* `{pnl_str}`",
        "━━━━━━━━━━━━━━━━━━",
        f"*Best Session:* `{best_str}`",
        f"*Worst Session:* `{worst_str}`",
        f"*Best Setup:* `{stats.get('best_setup_overall', 'N/A')}`",
    ]

    per_setup = stats.get("per_setup_stats", {})
    if per_setup:
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("*Per-Setup Breakdown:*")
        for key, data in sorted(per_setup.items(), key=lambda x: x[1].get("pnl", 0), reverse=True)[:10]:
            w = data.get("wins", 0)
            l = data.get("losses", 0)
            p = data.get("pnl", 0)
            p_str = f"+${p:,.2f}" if p >= 0 else f"-${abs(p):,.2f}"
            lines.append(f"  {key}: {w}W/{l}L | {p_str}")

    return "\n".join(lines)

# ── Risk limits ───────────────────────────────────────────────────
def check_risk_limits(state: Optional[dict] = None) -> dict:
    if state is None:
        state = load_state()
    # Session check already happens in load_state(), but be safe
    # for callers that pass a state dict directly.

    daily_pnl   = state["today_pnl"]
    daily_limit = state["daily_loss_limit"]
    daily_used  = abs(min(0, daily_pnl))
    daily_left  = daily_limit - daily_used

    drawdown    = state["peak_balance"] - state["balance"]
    max_dd      = state["max_drawdown"]
    dd_left     = max_dd - drawdown

    total_profit = state["balance"] - state["starting_balance"]
    profit_target = state["profit_target"]
    target_left  = max(0, profit_target - total_profit)

    daily_ok   = daily_left > daily_limit * 0.3
    dd_ok      = dd_left > max_dd * 0.3
    target_hit = total_profit >= profit_target

    use_mnq = state.get("use_mnq", True)

    return {
        "daily_pnl":      round(daily_pnl, 2),
        "daily_left":     round(daily_left, 2),
        "daily_used_pct": round(daily_used / max(1, daily_limit) * 100, 1),
        "drawdown":       round(drawdown, 2),
        "dd_left":        round(dd_left, 2),
        "dd_used_pct":    round(drawdown / max(1, max_dd) * 100, 1),
        "total_profit":   round(total_profit, 2),
        "target_left":    round(target_left, 2),
        "balance":        round(state["balance"], 2),
        "daily_ok":       daily_ok,
        "dd_ok":          dd_ok,
        "target_hit":     target_hit,
        "can_trade":      daily_ok and dd_ok and not target_hit,
        "warning":        not daily_ok or not dd_ok,
        "distance_to_max_drawdown": round(dd_left, 2),
        "session_date":   state.get("session_date", ""),
        "session_pnl":    round(daily_pnl, 2),
        "contracts_label": "MNQ" if use_mnq else "NQ",
    }

# ── Contract sizing (PositionSizer + fallback) ────────────────────
def suggest_contracts(market: str, tier: str, entry: float, stop: float,
                      state: Optional[dict] = None,
                      conviction: int = 70,
                      regime: str = "UNKNOWN",
                      setup_name: str = "UNKNOWN",
                      open_trades: list = None) -> dict:
    """
    Dynamic contract sizing using PositionSizer waterfall:
      1. Survival (max % of cushion)
      2. Kelly criterion (quarter-Kelly until 100 trades)
      3. Conviction multiplier
      4. Regime multiplier
      5. Correlation / exposure multiplier

    Falls back to tier-based sizing if sizer fails.
    """
    if state is None:
        state = load_state()
    state = _reset_daily_if_needed(state)

    use_mnq     = state.get("use_mnq", True)
    balance     = state["balance"]
    daily_limit = state["daily_loss_limit"]

    if market not in ("NQ", "GC"):
        return {"market": market, "type": "crypto", "label": "leverage"}

    label = "MNQ" if use_mnq else ("NQ" if market == "NQ" else "GC")
    pv    = MNQ_POINT_VALUE if (market == "NQ" and use_mnq) else (
            NQ_POINT_VALUE  if market == "NQ" else GC_POINT_VALUE)
    max_c = state.get("max_contracts_NQ", 10) if market == "NQ" \
            else state.get("max_contracts_GC", 5)

    stop_pts = abs(entry - stop)
    if stop_pts <= 0:
        return {"contracts": 1, "label": label, "risk_per_contract": 0,
                "max_contracts": max_c, "use_mnq": use_mnq, "reasoning": "zero_stop"}

    risk_per_contract = stop_pts * pv

    # ── Try PositionSizer ─────────────────────────────────────────
    try:
        from position_sizer import PositionSizer, get_edge_tracker, correlated_open_risk

        data_dir     = os.path.join(_BASE_DIR, "data")
        edge_tracker = get_edge_tracker(data_dir)
        estimate     = edge_tracker.get_best_estimate(setup_name, regime)
        risk         = check_risk_limits(state)
        dd_floor     = balance - risk["dd_left"]
        daily_used   = risk["daily_used_pct"] / 100 * daily_limit

        corr_risk = 0.0
        if open_trades:
            corr_risk = correlated_open_risk(market, open_trades)
        open_pos = len(open_trades) if open_trades else 0

        sizer  = PositionSizer()
        result = sizer.calculate(
            market=market, use_mnq=use_mnq,
            entry=entry, stop=stop,
            conviction=conviction, regime=regime,
            edge_estimate=estimate,
            balance=balance, dd_floor=dd_floor,
            daily_used=daily_used, daily_limit=daily_limit,
            open_positions=open_pos, correlated_risk=corr_risk,
        )

        if result.get("rejected"):
            return {
                "contracts": 0, "label": label,
                "risk_per_contract": round(risk_per_contract, 2),
                "max_contracts": max_c, "use_mnq": use_mnq,
                "rejected": True,
                "reject_reason": result.get("reject_reason", "unknown"),
                "reasoning": result.get("reasoning", ""),
                "sizer_result": result,
            }

        return {
            "contracts":         result["contracts"],
            "label":             label,
            "risk_per_contract": round(risk_per_contract, 2),
            "total_risk":        result["dollar_risk"],
            "cushion_pct":       result["cushion_pct"],
            "max_contracts":     max_c,
            "use_mnq":           use_mnq,
            "reasoning":         result["reasoning"],
            "sizer_result":      result,
        }
    except Exception:
        pass

    # ── Fallback: simple tier sizing ──────────────────────────────
    risk_pct    = state["account_risk_pct"] / 100
    dollar_risk = balance * risk_pct
    by_risk     = int(dollar_risk / max(1, risk_per_contract))
    tier_mult   = {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.3}.get(tier, 0.5)
    suggested   = max(1, int(by_risk * tier_mult))
    daily_left  = daily_limit + state["today_pnl"]
    if daily_left > 0:
        suggested = min(suggested, int(daily_left / max(1, risk_per_contract)))
    suggested = max(1, min(suggested, max_c))

    return {
        "contracts":         suggested,
        "label":             label,
        "risk_per_contract": round(risk_per_contract, 2),
        "max_contracts":     max_c,
        "use_mnq":           use_mnq,
        "reasoning":         f"fallback_tier_{tier}",
    }

# ── Open sim trade ────────────────────────────────────────────────
def open_sim_trade(alert_id: str, market: str, direction: str,
                   entry: float, stop: float, target: float,
                   contracts: int, tier: str) -> dict:
    state = load_state()
    state = _reset_daily_if_needed(state)
    trade = {
        "alert_id":  alert_id,
        "market":    market,
        "direction": direction,
        "entry":     entry,
        "stop":      stop,
        "target":    target,
        "contracts": contracts,
        "tier":      tier,
        "opened_at": datetime.now().isoformat(),
        "status":    "OPEN",
        "pnl":       0.0,
    }
    state["open_sim_trades"].append(trade)
    save_state(state)
    return trade

# ── Close sim trade ───────────────────────────────────────────────
def close_sim_trade(alert_id: str, exit_price: float, result: str) -> Optional[dict]:
    state = load_state()
    state = _reset_daily_if_needed(state)
    match = None
    for t in state["open_sim_trades"]:
        if t["alert_id"] == alert_id:
            match = t
            break
    if not match:
        return None

    market    = match["market"]
    contracts = match["contracts"]
    direction = match["direction"]
    entry     = match["entry"]
    use_mnq   = state.get("use_mnq", True)

    if market == "NQ":
        pv = MNQ_POINT_VALUE if use_mnq else NQ_POINT_VALUE
    elif market == "GC":
        pv = GC_POINT_VALUE
    else:
        pv = 1.0

    points = (exit_price - entry) if direction == "LONG" else (entry - exit_price)
    pnl    = round(points * pv * contracts, 2)

    match["status"]     = "CLOSED"
    match["exit_price"] = exit_price
    match["result"]     = result
    match["pnl"]        = pnl
    match["closed_at"]  = datetime.now().isoformat()

    state["balance"]      = round(state["balance"] + pnl, 2)
    state["today_pnl"]    = round(state["today_pnl"] + pnl, 2)
    state["total_pnl"]    = round(state["total_pnl"] + pnl, 2)
    state["peak_balance"] = max(state["peak_balance"], state["balance"])
    state["trades"].append(match)
    state["open_sim_trades"] = [t for t in state["open_sim_trades"] if t["alert_id"] != alert_id]
    save_state(state)
    return match

# ── Auto check sim trades ─────────────────────────────────────────
def auto_check_sim_trades(live_prices: dict) -> list:
    state  = load_state()
    closed = []
    for t in list(state["open_sim_trades"]):
        market = t["market"]
        price  = live_prices.get(market)
        if price is None:
            continue
        direction  = t["direction"]
        hit_target = hit_stop = False
        if direction == "LONG":
            if price >= t["target"]: hit_target = True
            if price <= t["stop"]:   hit_stop   = True
        else:
            if price <= t["target"]: hit_target = True
            if price >= t["stop"]:   hit_stop   = True
        if hit_target:
            r = close_sim_trade(t["alert_id"], t["target"], "WIN")
            if r: closed.append(r)
        elif hit_stop:
            r = close_sim_trade(t["alert_id"], t["stop"], "LOSS")
            if r: closed.append(r)
    return closed

# ── Format sim block for alerts ───────────────────────────────────
def format_sim_block(market: str, tier: str, entry: float, stop: float,
                     target: float, alert_id: str,
                     conviction: int = 70, regime: str = "UNKNOWN",
                     setup_name: str = "UNKNOWN") -> str:
    """
    Returns the SIM MODE block for Telegram alerts.
    Shows intelligent sizing with full reasoning.
    Also opens a sim trade automatically.
    """
    state = load_state()
    if not state.get("enabled"):
        return ""

    state = _reset_daily_if_needed(state)
    risk  = check_risk_limits(state)

    if not risk["can_trade"]:
        if risk["target_hit"]:
            return "\n\U0001f4b0 *SIM:* \U0001f3af Profit target reached! No more trades today."
        warning = ""
        if not risk["daily_ok"]:
            warning = f"\u26a0\ufe0f Daily limit almost hit! ${risk['daily_left']:,.0f} remaining."
        if not risk["dd_ok"]:
            warning += f"\n\u26a0\ufe0f Drawdown limit close! ${risk['dd_left']:,.0f} remaining."
        return f"\n\U0001f4b0 *SIM:* {warning}\nNo sim trade opened."

    direction = "LONG" if target > entry else "SHORT"

    # Build open trades list for correlation check
    open_trades_raw  = state.get("open_sim_trades", [])
    use_mnq          = state.get("use_mnq", True)
    open_trades_info = []
    for t in open_trades_raw:
        m   = t["market"]
        spv = MNQ_POINT_VALUE if (m == "NQ" and use_mnq) else \
              NQ_POINT_VALUE  if m == "NQ" else \
              GC_POINT_VALUE  if m == "GC" else 1.0
        dr  = abs(float(t.get("entry", 0)) - float(t.get("stop", 0))) * spv * t.get("contracts", 1)
        open_trades_info.append({"market": m, "dollar_risk": dr})

    c_info = suggest_contracts(
        market, tier, entry, stop, state,
        conviction=conviction, regime=regime,
        setup_name=setup_name, open_trades=open_trades_info,
    )

    if c_info.get("type") == "crypto":
        risk_amt   = round(state["balance"] * state["account_risk_pct"] / 100, 2)
        reward_amt = round(risk_amt * abs(target - entry) / max(0.01, abs(entry - stop)), 2)
        open_sim_trade(alert_id, market, direction, entry, stop, target, 1, tier)
        plus_dp = '+' if risk['daily_pnl'] >= 0 else ''
        return (
            f"\n\U0001f4b0 *SIM MODE*\n"
            f"  Risk: `${risk_amt}` | Reward: `${reward_amt}`\n"
            f"  Today P&L: `${plus_dp}{risk['daily_pnl']:,.2f}`\n"
            f"  Daily left: `${risk['daily_left']:,.0f}`"
        )

    contracts  = c_info.get("contracts", 1)
    label      = c_info["label"]
    risk_per_c = c_info.get("risk_per_contract", 0)
    total_risk = c_info.get("total_risk", round(risk_per_c * max(contracts, 1), 2))
    cushion_pct= c_info.get("cushion_pct", 0)
    reasoning  = c_info.get("reasoning", "")
    rejected   = c_info.get("rejected", False)

    spv          = MNQ_POINT_VALUE if (market == "NQ" and use_mnq) else \
                   NQ_POINT_VALUE  if market == "NQ" else GC_POINT_VALUE
    total_reward = round(abs(target - entry) * spv * max(contracts, 1), 2)

    if rejected or contracts == 0:
        reason = c_info.get("reject_reason", "sized to zero")
        return (
            f"\n\U0001f4b0 *SIM \u2014 No Position*\n"
            f"  \u26d4 Sizer rejected: `{reason}`\n"
            f"  Balance: `${risk['balance']:,.2f}` | Cushion: `${risk['dd_left']:,.0f}`"
        )

    open_sim_trade(alert_id, market, direction, entry, stop, target, contracts, tier)

    used_pct = risk["daily_used_pct"]
    bar_n    = int(min(10, used_pct / 10))
    bar      = "\U0001f7e5" * bar_n + "\u2b1c" * (10 - bar_n)
    plus_dp2 = '+' if risk['daily_pnl'] >= 0 else ''

    block = (
        f"\n\U0001f4b0 *SIM MODE \u2014 {label}*\n"
        f"  \U0001f4e6 Size: `{contracts}` {label}  |  Risk: `${total_risk:,.0f}` ({cushion_pct:.1f}% of cushion)\n"
        f"  \U0001f4b8 Reward est: `${total_reward:,.0f}`\n"
        f"  _{reasoning}_\n"
        f"  \U0001f4c5 Today P&L: `${plus_dp2}{risk['daily_pnl']:,.2f}`\n"
        f"  \U0001f6e1 Daily left: `${risk['daily_left']:,.0f}` | Cushion: `${risk['dd_left']:,.0f}`\n"
        f"  {bar} {used_pct:.0f}% daily limit used"
    )
    if used_pct >= 60:
        block += "\n  \u26a0\ufe0f Getting close to daily limit \u2014 be selective"
    return block

# ── Settings helpers ──────────────────────────────────────────────
def set_preset(preset_key: str):
    state  = load_state()
    preset = EVAL_PRESETS.get(preset_key, EVAL_PRESETS["50k"])
    state.update({
        "preset":           preset_key,
        "balance":          preset["balance"],
        "starting_balance": preset["balance"],
        "peak_balance":     preset["balance"],
        "daily_loss_limit": preset["daily_loss_limit"],
        "max_drawdown":     preset["max_drawdown"],
        "profit_target":    preset["profit_target"],
        "today_pnl":        0.0,
        "total_pnl":        0.0,
        "trades":           [],
        "open_sim_trades":  [],
    })
    save_state(state)

def toggle_sim(enabled: bool):
    state = load_state()
    state["enabled"] = enabled
    save_state(state)

def toggle_mnq(use_mnq: bool):
    state = load_state()
    state["use_mnq"] = use_mnq
    save_state(state)

def set_max_contracts(market: str, n: int):
    state = load_state()
    if market == "NQ":   state["max_contracts_NQ"] = n
    elif market == "GC": state["max_contracts_GC"] = n
    save_state(state)

def reset_sim(preset_key: str = None):
    state = load_state()
    set_preset(preset_key or state.get("preset", "50k"))

# ── Status / reporting ────────────────────────────────────────────
def sim_status_text() -> str:
    state = load_state()
    if not state.get("enabled"):
        return (
            "\U0001f4b0 *Sim Mode is OFF*\n"
            "Use the Settings menu to turn it on.\n"
            "Sim tracks your eval account alongside real alerts."
        )
    state  = _reset_daily_if_needed(state)
    risk   = check_risk_limits(state)
    preset = state.get("preset", "50k").upper()
    label  = "MNQ" if state.get("use_mnq", True) else "NQ"
    open_t = len(state.get("open_sim_trades", []))
    closed = [t for t in state.get("trades", []) if t.get("status") == "CLOSED"]
    wins   = sum(1 for t in closed if t.get("result") == "WIN")
    losses = sum(1 for t in closed if t.get("result") == "LOSS")
    wr     = round(wins / max(1, wins + losses) * 100, 1)
    status_icon = "\U0001f7e2" if risk["can_trade"] else "\U0001f534"
    plus_tp  = '+' if risk['total_profit'] >= 0 else ''
    plus_dp  = '+' if risk['daily_pnl'] >= 0 else ''
    status_txt = 'Ready' if risk['can_trade'] else '\u26a0\ufe0f NEAR LIMITS \u2014 be careful'
    return (
        f"\U0001f4b0 *Sim Account \u2014 {preset} Eval*\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"*Balance:* `${risk['balance']:,.2f}`\n"
        f"*Total P&L:* `${plus_tp}{risk['total_profit']:,.2f}`\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"*Today P&L:* `${plus_dp}{risk['daily_pnl']:,.2f}`\n"
        f"*Daily left:* `${risk['daily_left']:,.2f}` ({100-risk['daily_used_pct']:.0f}% remaining)\n"
        f"*Cushion left:* `${risk['dd_left']:,.2f}` ({100-risk['dd_used_pct']:.0f}% remaining)\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"*Contract:* `{label}` | *Open trades:* `{open_t}`\n"
        f"*Closed:* `{len(closed)}` ({wins}W/{losses}L \u2014 {wr}% WR)\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"{status_icon} *Status:* {status_txt}"
    )

def sim_period_text(days: int = 7) -> str:
    s     = get_period_summary(days)
    label = "Weekly" if days <= 7 else "Monthly"
    icon  = "\U0001f7e2" if s["pnl"] >= 0 else "\U0001f534"
    pstr  = f"+${s['pnl']:,.2f}" if s["pnl"] >= 0 else f"-${abs(s['pnl']):,.2f}"
    lines = [
        f"\U0001f4c5 *SIM {label} Summary ({days} days)*",
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
        f"{icon} *Total P&L:* `{pstr}`",
        f"*Trading days:* `{s['trading_days']}`",
        f"*Trades:* `{s['wins']}W / {s['losses']}L` ({s['wr']}% WR)",
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
    ]
    if s["days_data"]:
        lines.append("*Day by day:*")
        for d in s["days_data"]:
            di = "\U0001f7e2" if d.get("pnl", 0) >= 0 else "\U0001f534"
            dp = f"+${d['pnl']:,.0f}" if d.get("pnl", 0) >= 0 else f"-${abs(d['pnl']):,.0f}"
            lines.append(f"  {di} {d.get('date','')[-5:]} \u2014 {dp} ({d.get('wins',0)}W/{d.get('losses',0)}L)")
    else:
        lines.append("No history yet \u2014 data builds up over time.")
    return "\n".join(lines)

def sim_daily_section() -> str:
    state = load_state()
    if not state.get("enabled"):
        return ""
    state  = _reset_daily_if_needed(state)
    risk   = check_risk_limits(state)
    today  = datetime.now().strftime("%Y-%m-%d")
    today_trades = [t for t in state.get("trades", []) if today in t.get("closed_at", "")]
    wins   = sum(1 for t in today_trades if t.get("result") == "WIN")
    losses = sum(1 for t in today_trades if t.get("result") == "LOSS")
    pnl    = sum(t.get("pnl", 0) for t in today_trades)
    lines  = [
        "",
        "SIM ACCOUNT \u2014 TODAY",
        "-" * 30,
        f"Balance:        ${risk['balance']:,.2f}",
        f"Today P&L:      ${pnl:+,.2f}",
        f"Today trades:   {len(today_trades)} ({wins}W / {losses}L)",
        f"Daily limit used: {risk['daily_used_pct']}%",
        f"Drawdown used:    {risk['dd_used_pct']}%",
        f"Total P&L:      ${risk['total_profit']:+,.2f}",
    ]
    if today_trades:
        lines.append("")
        lines.append("Today's sim trades:")
        for t in today_trades:
            icon = "\u2705" if t.get("result") == "WIN" else "\u274c"
            lines.append(
                f"  {icon} {t.get('market')} | {t.get('contracts')}x"
                f" | Entry:{t.get('entry')} Exit:{t.get('exit_price')}"
                f" | P&L: ${t.get('pnl',0):+,.2f}"
            )
    return "\n".join(lines)

# ── Edge tracking integration ──────────────────────────────────────
def record_trade_for_sizing(setup_name: str, regime: str, won: bool, r_multiple: float):
    """Call this after every closed trade to update the EdgeTracker."""
    try:
        from position_sizer import get_edge_tracker
        data_dir = os.path.join(_BASE_DIR, "data")
        tracker  = get_edge_tracker(data_dir)
        tracker.record(setup_name, regime, won, r_multiple)
    except Exception:
        pass

def get_edge_summary() -> str:
    """Returns edge estimate summary for /learned command."""
    try:
        from position_sizer import get_edge_tracker
        data_dir = os.path.join(_BASE_DIR, "data")
        tracker  = get_edge_tracker(data_dir)
        return tracker.summary()
    except Exception as e:
        return f"Edge tracker unavailable: {e}"
