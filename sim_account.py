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

# Task 5: Enable eval mode by default
try:
    from position_sizer import set_eval_mode
    set_eval_mode(True)
    _log.info("Eval mode enabled by default")
except Exception:
    pass

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
    "enabled":          True,
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
    # Task 3D: Archive even zero-PnL days so we don't lose history
    if not today:
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

    Wave 30 (May 11, 2026): changed to call _handle_session_end() which
    branches on eval outcome (ACTIVE = preserve balance via daily counter
    reset only; BUSTED/PASSED = full preset reset for new eval). The
    previous behavior unconditionally wiped balance every session, which
    broke Topstep eval simulation.

    This runs on EVERY load_state() call.
    """
    current_sid = _get_session_date()
    stored_sid = state.get("session_date", state.get("today_date", ""))

    if stored_sid and stored_sid != current_sid:
        # Wave 30: shared end-of-session handler (carries balance if eval ACTIVE)
        _handle_session_end(state, stored_sid, current_sid)
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


# ── Wave 30 (May 11, 2026): Eval outcome + daily counters reset ──
#
# These three functions replace the previous behavior where every
# 4PM ET session boundary called _reset_to_fresh_preset() and wiped
# balance/peak/total_pnl/trades. Topstep evals are CUMULATIVE - the
# balance must carry day-to-day until the user busts or passes.
#
# Wave 30 changes the contract so:
#   - On session boundary with ACTIVE eval: reset daily counters only.
#     Balance, peak, starting_balance, total_pnl, trades, open trades
#     all preserved.
#   - On session boundary with BUSTED or PASSED eval: full preset reset
#     to start a fresh eval (unchanged from previous behavior).
#
def check_eval_outcome(state: dict) -> str:
    """
    Wave 30 (May 11, 2026): Detect whether the current eval has ended.

    Returns one of:
      "ACTIVE"            - eval still in progress, carry balance
      "BUSTED_MAX_DD"     - (peak - balance) >= max_drawdown
      "BUSTED_DAILY_LOSS" - today_pnl <= -daily_loss_limit
      "PASSED_TARGET"     - (balance - starting_balance) >= profit_target

    These thresholds match standard Topstep eval rules:
      50K eval: daily_loss=$1000, max_drawdown=$2000, profit_target=$3000

    Defensive: any exception returns "ACTIVE" (safest default - the worst
    case is the eval continues when it should have ended, which Wayne can
    fix manually via /reset).
    """
    try:
        balance       = float(state.get("balance", 0))
        peak          = float(state.get("peak_balance", balance))
        starting      = float(state.get("starting_balance", 50_000.0))
        today_pnl     = float(state.get("today_pnl", 0))
        max_dd        = float(state.get("max_drawdown", 2_000.0))
        daily_limit   = float(state.get("daily_loss_limit", 1_000.0))
        profit_target = float(state.get("profit_target", 3_000.0))

        # Profit target check: total profit since eval started
        if (balance - starting) >= profit_target:
            return "PASSED_TARGET"

        # Daily loss check: today_pnl negative beyond daily_loss_limit
        if today_pnl <= -daily_limit:
            return "BUSTED_DAILY_LOSS"

        # Max drawdown check: drawdown from peak
        drawdown = peak - balance
        if drawdown >= max_dd:
            return "BUSTED_MAX_DD"

        return "ACTIVE"
    except Exception as _err:
        _log.warning("check_eval_outcome failed (%s) - defaulting to ACTIVE", _err)
        return "ACTIVE"


def _reset_daily_counters(state: dict, new_session_date: str):
    """
    Wave 30 (May 11, 2026): Reset ONLY the daily counters; preserve
    everything else.

    This is the new normal-case session boundary behavior. The previous
    _reset_to_fresh_preset() wiped balance / peak / total_pnl / trades /
    open_sim_trades, which broke Topstep eval simulation by preventing
    cumulative balance accumulation.

    Resets:
      today_pnl          -> 0.0
      today_date         -> new_session_date
      session_date       -> new_session_date
      trades             -> [] (today's trades archived to sim_history)

    Preserves:
      balance            (cumulative since eval started)
      peak_balance       (highest seen this eval)
      starting_balance   (preset starting; doesn't change until new eval)
      total_pnl          (cumulative since eval started)
      open_sim_trades    (Topstep 4:10 force-flatten should empty these,
                          but if any survive, they continue tracking)
      All preset config: daily_loss_limit, max_drawdown, profit_target
      All settings: max_contracts_NQ/GC, use_mnq, account_risk_pct, etc.
    """
    state["today_pnl"]    = 0.0
    state["today_date"]   = new_session_date
    state["session_date"] = new_session_date
    state["trades"]       = []
    # NOTE: balance, peak_balance, starting_balance, total_pnl,
    # open_sim_trades, preset config, and all settings preserved.


def _handle_session_end(state: dict, stored_sid: str, current_sid: str) -> str:
    """
    Wave 30 (May 11, 2026): Shared end-of-session handler used by both
    _ensure_session_current() and on_session_close(). Returns the eval
    outcome string for callers that want to log it.

    Flow:
      1. Archive closing session (sim_history + per-day file + lifetime_stats)
      2. Check eval outcome
      3. If ACTIVE     -> _reset_daily_counters (preserve balance)
      4. If BUSTED/PASSED -> _reset_to_fresh_preset (start new eval)

    Both paths leave the state in a valid "ready for next session" form.
    Caller is expected to save_state() after.
    """
    # Step 1: archive (regardless of outcome)
    _archive_day(state)
    _archive_sim_state(state, stored_sid)
    _update_lifetime_stats(state)

    # Step 2: detect eval outcome
    outcome = check_eval_outcome(state)

    # Step 3/4: branch on outcome
    if outcome == "ACTIVE":
        _log.info("Session rolled: %s -> %s (eval ACTIVE, balance preserved)",
                  stored_sid, current_sid)
        _reset_daily_counters(state, current_sid)
    else:
        _log.info("Session rolled: %s -> %s (eval %s, full reset for new eval)",
                  stored_sid, current_sid, outcome)
        # TODO Bucket 2: archive eval outcome to data/eval_history.json
        # before the reset wipes the final state.
        _reset_to_fresh_preset(state, current_sid)

    return outcome


def on_session_close(state: dict = None) -> dict:
    """
    Called by SessionClock on FUTURES_SESSION_CLOSE event.

    Wave 30 (May 11, 2026): refactored to use the shared
    _handle_session_end() helper so the eval outcome branch (ACTIVE =
    carry balance; BUSTED/PASSED = full reset) is applied consistently
    with _ensure_session_current(). Previously this always called
    _reset_to_fresh_preset() and wiped balance.

    Returns the new state.
    """
    if state is None:
        state = _load_state_raw()
    sid = state.get("session_date", state.get("today_date", ""))
    if not sid:
        sid = _get_session_date()

    new_sid = _get_session_date()
    # Wave 30: shared end-of-session handler (carries balance if eval ACTIVE)
    outcome = _handle_session_end(state, sid, new_sid)
    save_state(state)
    _log.info("Session close complete: %s -> %s (outcome=%s)", sid, new_sid, outcome)
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
    """
    Update lifetime_stats.json with the finishing session's results.
    Task 3A: Always writes the file, even on zero-PnL / zero-trade sessions.

    May 1: Tracks lifetime_balance (running total since combine started)
    independently of the daily-reset session balance. Wayne wants to see
    the combine progress in alerts/status, not just today's session P&L.
    """
    stats = _load_lifetime_stats()

    today_trades = state.get("trades", [])
    session_trades = []
    for t in today_trades:
        closed_at = t.get("closed_at", "")
        if t.get("status") == "CLOSED" and closed_at:
            session_trades.append(t)

    session_pnl = state.get("today_pnl", 0.0)
    wins = sum(1 for t in session_trades if t.get("result") == "WIN")
    losses = sum(1 for t in session_trades if t.get("result") == "LOSS")

    stats["total_sessions"]    = stats.get("total_sessions", 0) + 1
    stats["total_trades"]      = stats.get("total_trades", 0) + len(session_trades)
    stats["total_wins"]        = stats.get("total_wins", 0) + wins
    stats["total_losses"]      = stats.get("total_losses", 0) + losses
    stats["total_pnl_dollars"] = round(stats.get("total_pnl_dollars", 0) + session_pnl, 2)

    # May 1: lifetime balance tracking. The combine starting balance is set
    # the first time we update lifetime stats; thereafter it stays fixed and
    # the lifetime_balance rolls (start + cumulative pnl).
    if "lifetime_starting_balance" not in stats:
        stats["lifetime_starting_balance"] = float(state.get("starting_balance", 50_000.0))
        stats["combine_started_at"] = datetime.now(timezone.utc).isoformat()
    stats["lifetime_balance"] = round(
        stats["lifetime_starting_balance"] + stats["total_pnl_dollars"], 2
    )
    # Track lifetime peak so we can show how far from peak the combine is.
    stats["lifetime_peak_balance"] = max(
        stats.get("lifetime_peak_balance", stats["lifetime_starting_balance"]),
        stats["lifetime_balance"],
    )

    if session_pnl > stats.get("best_session_pnl", float("-inf")):
        stats["best_session_pnl"] = round(session_pnl, 2)
    if session_pnl < stats.get("worst_session_pnl", float("inf")):
        stats["worst_session_pnl"] = round(session_pnl, 2)

    # Per-setup stats
    # Wave 8 (May 3) BUGFIX: previously this used t.get("tier") which made
    # keys like "NQ:MEDIUM" instead of the actual setup name. The result was
    # a useless best_setup_overall field. Now prefers the real setup name,
    # falling back to tier for any legacy trade dicts written before Wave 8.
    per_setup = stats.get("per_setup_stats", {})
    for t in session_trades:
        setup = (
            t.get("setup_type")
            or t.get("setup")
            or t.get("tier", "UNKNOWN")
        )
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
    _log.info(
        "Lifetime stats saved: sessions=%d, trades=%d, pnl=$%s, balance=$%s",
        stats["total_sessions"], stats["total_trades"],
        stats["total_pnl_dollars"], stats.get("lifetime_balance", 0),
    )


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
        # May 1: lifetime balance tracking (combine-level, not session-level).
        "lifetime_starting_balance": 50_000.0,
        "lifetime_balance":          50_000.0,
        "lifetime_peak_balance":     50_000.0,
        "combine_started_at":        "",
    }


def get_lifetime_balance() -> float:
    """
    Return the running combine balance (start + cumulative P&L across all sessions).
    Used by format_sim_block and bot.py for inline display in alerts.

    Backward-compat (May 2 audit fix): older lifetime_stats.json files written
    before the lifetime_balance fields existed only have total_pnl_dollars.
    Reconstruct balance as starting_balance + total_pnl_dollars + today_pnl in
    that case so the display works immediately, not just after the next 4 PM
    close. The proper fields populate next time _update_lifetime_stats runs.
    """
    stats = _load_lifetime_stats()
    # Get live session pnl ONCE (avoids two load_state calls)
    try:
        live_pnl = float(load_state().get("today_pnl", 0.0))
    except Exception:
        live_pnl = 0.0

    # Path A: new-format file with lifetime_balance set
    if stats.get("lifetime_balance"):
        return round(float(stats["lifetime_balance"]) + live_pnl, 2)

    # Path B: legacy file — reconstruct from total_pnl_dollars
    starting = float(stats.get("lifetime_starting_balance", 50_000.0))
    cum_pnl  = float(stats.get("total_pnl_dollars", 0.0))
    return round(starting + cum_pnl + live_pnl, 2)


def get_lifetime_pnl() -> float:
    """Cumulative P&L since combine started (includes today's open session)."""
    stats = _load_lifetime_stats()
    cum = float(stats.get("total_pnl_dollars", 0.0))
    try:
        cum += float(load_state().get("today_pnl", 0.0))
    except Exception:
        pass
    return round(cum, 2)


def _save_lifetime_stats(stats: dict):
    try:
        with open(LIFETIME_STATS_FILE, "w") as f:
            json.dump(stats, f, indent=2)
    except Exception:
        pass


def get_lifetime_stats() -> dict:
    return _load_lifetime_stats()


def eval_progression_text() -> str:
    """
    Wave 33 (May 11, 2026): Single-screen Topstep eval progression view.

    Returns a Telegram-formatted Markdown block answering the four
    key questions of any prop firm eval journey:
      1. Where am I now (balance, peak, % of starting)
      2. How far to PASS (target, remaining, progress bar)
      3. How safe from BUST (daily + DD cushions)
      4. Am I on track (pace, days to pass at pace, trade quality)

    Reads from lifetime_stats.json (combine cumulative) + sim_account.json
    (today's session for daily cushion). No state mutation.
    """
    state = load_state()
    if not state.get("enabled"):
        return (
            "\U0001f4b0 *Sim Mode is OFF*\n"
            "Use the Settings menu to turn it on.\n"
            "Sim tracks your eval account alongside real alerts."
        )

    risk  = check_risk_limits(state)
    stats = _load_lifetime_stats()

    # Section 1: Balance
    life_bal  = get_lifetime_balance()
    life_pnl  = get_lifetime_pnl()
    starting  = float(stats.get("lifetime_starting_balance", 50_000.0))
    peak      = float(stats.get("lifetime_peak_balance", max(starting, life_bal)))
    pct       = ((life_bal - starting) / starting * 100.0) if starting > 0 else 0.0

    # Section 2: Path to PASS
    target    = float(state.get("profit_target", 3_000.0))
    to_pass   = max(0.0, target - life_pnl)
    pct_done  = max(0.0, min(100.0, (life_pnl / target * 100.0) if target > 0 else 0.0))
    bars      = int(pct_done / 10)
    bar       = "\u2588" * bars + "\u2591" * (10 - bars)

    # Section 3: Bust guardrails
    daily_left = float(risk.get("daily_left", 0))
    cushion    = float(risk.get("dd_left", 0))
    daily_lim  = float(state.get("daily_loss_limit", 1_000.0))
    max_dd     = float(state.get("max_drawdown", 2_000.0))

    # Section 4: Pace
    started_iso = stats.get("combine_started_at", "")
    days_in     = 1
    if started_iso:
        try:
            started = datetime.fromisoformat(started_iso)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            days_in = max(1, int(elapsed / 86400))
        except Exception:
            pass

    daily_avg = life_pnl / days_in if days_in > 0 else 0.0

    # Trade quality
    wins   = int(stats.get("total_wins", 0))
    losses = int(stats.get("total_losses", 0))
    total  = wins + losses
    wr     = round(wins / max(1, total) * 100.0, 1)

    best_sess  = float(stats.get("best_session_pnl", 0))
    worst_sess = float(stats.get("worst_session_pnl", 0))

    # Profit factor from per_setup totals
    per_setup       = stats.get("per_setup_stats", {})
    total_winning   = sum(s.get("pnl", 0) for s in per_setup.values() if s.get("pnl", 0) > 0)
    total_losing    = abs(sum(s.get("pnl", 0) for s in per_setup.values() if s.get("pnl", 0) < 0))
    if total_losing > 0:
        pf = total_winning / total_losing
    elif total_winning > 0:
        pf = 99.99  # no losses, cap display
    else:
        pf = 0.0

    # Status indicator
    if life_pnl >= target:
        status_emoji = "\U0001f3c6"
        status_text  = "PASSED! Withdraw and start funded account."
    elif cushion < 500:
        status_emoji = "\U0001f6a8"
        status_text  = "DANGER ZONE - cushion below $500"
    elif life_pnl < -1500:
        status_emoji = "\u26a0\ufe0f"
        status_text  = "Deep drawdown - be selective today"
    elif daily_avg > 200:
        status_emoji = "\U0001f680"
        status_text  = "Excellent pace - keep it up"
    elif daily_avg > 50:
        status_emoji = "\u2705"
        status_text  = "On track for pass"
    elif daily_avg > 0:
        status_emoji = "\U0001f4c8"
        status_text  = "Building positive momentum"
    else:
        status_emoji = "\u23f3"
        status_text  = "Below water - focus on quality setups only"

    # Pace projection (only if positive avg)
    if daily_avg > 0 and to_pass > 0:
        days_to_pass = int(to_pass / daily_avg)
        if days_to_pass > 365:
            pace_line = "Below pace - small avg makes target distant"
        else:
            pace_line = f"At this pace: ~`{days_to_pass}` days to PASS"
    elif to_pass <= 0:
        pace_line = "Target already reached!"
    else:
        pace_line = "Need positive avg to reach PASS - focus on cleaner setups"

    # Formatted P&L strings
    pnl_str   = f"+${life_pnl:,.0f}"  if life_pnl   >= 0 else f"-${abs(life_pnl):,.0f}"
    best_str  = f"+${best_sess:,.0f}" if best_sess  >= 0 else f"-${abs(best_sess):,.0f}"
    worst_str = f"+${worst_sess:,.0f}" if worst_sess >= 0 else f"-${abs(worst_sess):,.0f}"
    avg_str   = f"+${daily_avg:,.0f}" if daily_avg  >= 0 else f"-${abs(daily_avg):,.0f}"

    day_word = "day" if days_in == 1 else "days"

    return (
        f"\U0001f3af *TOPSTEP $50K EVAL*\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\n"
        f"*Balance:* `${life_bal:,.2f}` ({pct:+.2f}%)\n"
        f"*Peak:*    `${peak:,.2f}`  \u00b7  *Total P&L:* `{pnl_str}`\n"
        f"\n"
        f"*\U0001f3af Path to PASS* (+${target:,.0f} from start)\n"
        f"  {bar} `{pct_done:.0f}%`\n"
        f"  `${to_pass:,.0f}` left to target\n"
        f"\n"
        f"*\U0001f6e1 Bust Guardrails*\n"
        f"  Daily: `${daily_left:,.0f}` cushion (limit -${daily_lim:,.0f})\n"
        f"  DD:    `${cushion:,.0f}` cushion (max -${max_dd:,.0f})\n"
        f"\n"
        f"*\U0001f4ca Pace*\n"
        f"  `{days_in}` {day_word} in  \u00b7  Avg `{avg_str}/day`\n"
        f"  {pace_line}\n"
        f"\n"
        f"*\U0001f3b2 Trade Quality*\n"
        f"  `{wins}W/{losses}L` ({wr}% WR) over `{total}` trades\n"
        f"  Best day: `{best_str}`  \u00b7  Worst: `{worst_str}`\n"
        f"  Profit factor: `{pf:.2f}`\n"
        f"\n"
        f"{status_emoji} {status_text}"
    )


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
            # Wave 18 (May 9, 2026): wrap key in backticks. Setup names
            # like NQ:BREAK_RETEST_BULL have underscores which Telegram
            # Markdown was interpreting as italic markers. Unmatched
            # underscores broke the parse and the message was silently
            # rejected, so the Lifetime button appeared to do nothing.
            lines.append(f"  `{key}`: {w}W/{l}L | {p_str}")

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
                   contracts: int, tier: str,
                   setup_type: str = "UNKNOWN") -> dict:
    """
    Wave 8 (May 3): added setup_type parameter (default UNKNOWN for backward
    compat). When the bot opens a sim trade from an alert it now passes the
    real setup name so per_setup_stats can group correctly.
    """
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
        "setup_type": setup_type,  # Wave 8: real setup name, not tier
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
def reconcile_with_outcomes() -> int:
    """
    Wave 36 (May 11, 2026): Topstep sim reconciliation against outcomes.csv.

    The bot has two close-detection paths for NQ/GC trades:
      A. outcome_tracker.py uses high/low/close (catches wicks)
      B. sim_account.auto_check_sim_trades uses 15m close (misses wicks)

    When path A closes a trade but path B misses it, sim balance stays
    stale. This function reads outcomes.csv (source of truth), finds any
    open_sim_trades that should be closed, and closes them with the
    correct exit_price + result.

    Mirrors crypto_sim.reconcile_with_outcomes (Wave 5). Same semantics:
      - Safe to call repeatedly (no-op when in sync)
      - close_sim_trade is idempotent on alert_id
      - Returns count of reconciled trades
    """
    state = load_state()
    if not state.get("enabled") or not state.get("open_sim_trades"):
        return 0

    outcomes_path = os.path.join(_BASE_DIR, "data", "outcomes.csv")
    if not os.path.exists(outcomes_path):
        return 0

    # Read outcomes.csv into {alert_id: (status, result, exit_price)}
    # Use last occurrence per alert_id (most recent state).
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
                _log.warning("Wave 36 reconcile: outcomes.csv missing required columns")
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
                if result not in ("WIN", "LOSS"):
                    continue
                try:
                    exit_price = float(ep_raw) if ep_raw else 0.0
                except Exception:
                    exit_price = 0.0
                if exit_price > 0:
                    closed_lookup[aid] = (status, result, exit_price)
    except Exception as e:
        _log.warning("Wave 36 reconcile: failed to read outcomes.csv: %s", e)
        return 0

    # Walk open_sim_trades and close any that are CLOSED in outcomes.csv
    reconciled = 0
    for t in list(state["open_sim_trades"]):
        aid = t.get("alert_id")
        if aid in closed_lookup:
            _, result, exit_price = closed_lookup[aid]
            r = close_sim_trade(aid, exit_price, result)
            if r is not None:
                reconciled += 1
                _log.info(
                    "Wave 36 reconcile: closed stale sim trade %s "
                    "(%s %s, result=%s, exit=%.4f, pnl=%.2f)",
                    aid, t.get("market"), t.get("direction"),
                    result, exit_price, r.get("pnl", 0),
                )
    return reconciled


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
                     setup_name: str = "UNKNOWN",
                     context: Optional[dict] = None) -> str:
    """
    Returns the SIM MODE block for Telegram alerts.
    Shows intelligent sizing with full reasoning.
    Also opens a sim trade automatically.

    Optional `context` dict matches the crypto_sim format and includes:
      trend_score, rsi, adx, htf_bias, regime, session, news_flag, chart_read.
    When provided, a "Context" line is appended so NQ/GC alerts mirror the
    BTC/SOL crypto sim format.
    """
    state = load_state()
    if not state.get("enabled"):
        return ""

    # Dual-track sim: BTC/SOL go through crypto_sim.format_crypto_sim_block instead.
    # This Topstep eval sim only handles NQ and GC futures.
    if market in ("BTC", "SOL"):
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

    # Wave 8: pass setup_name so per_setup_stats keys correctly
    open_sim_trade(alert_id, market, direction, entry, stop, target, contracts, tier,
                   setup_type=setup_name)

    used_pct = risk["daily_used_pct"]
    bar_n    = int(min(10, used_pct / 10))
    bar      = "\U0001f7e5" * bar_n + "\u2b1c" * (10 - bar_n)
    plus_dp2 = '+' if risk['daily_pnl'] >= 0 else ''

    # May 1: lifetime balance line (combine-cumulative). Wayne wants to see
    # whether the combine is up or down overall, not just today's session.
    _life_bal = get_lifetime_balance()
    _life_pnl = get_lifetime_pnl()
    _life_pnl_sign = '+' if _life_pnl >= 0 else ''
    _life_starting = float(_load_lifetime_stats().get("lifetime_starting_balance", 50_000.0))
    _life_pct = ((_life_bal - _life_starting) / _life_starting * 100.0) if _life_starting > 0 else 0.0

    # Wave 34 (May 11, 2026): Compact SIM block. Was 8-10 lines with
    # redundant data (session==combine post-Wave 30, today P&L doubled by
    # daily cushion, 0% progress bar). Now 4 lines + optional warning.
    # Same info, less noise.
    try:
        rr_display = total_reward / max(1, total_risk)
    except Exception:
        rr_display = 0.0
    today_pnl_str = f"{plus_dp2}${risk['daily_pnl']:,.0f}"
    block = (
        f"\n\U0001f4b0 *SIM \u2014 {contracts} {label}* | Risk `${total_risk:,.0f}` \u2192 Reward `${total_reward:,.0f}` (R:R `{rr_display:.1f}`)\n"
        f"  Balance: `${risk['balance']:,.2f}` ({today_pnl_str} today \u00b7 combine {_life_pnl_sign}${_life_pnl:,.0f} \u00b7 {_life_pct:+.1f}%)\n"
        f"  Cushion: Daily `${risk['daily_left']:,.0f}` \u00b7 DD `${risk['dd_left']:,.0f}`\n"
        f"  _{reasoning}_"
    )
    # Only show the daily-limit warning when actually approaching (>=60%).
    # Wave 34: dropped the always-shown progress bar that was 0% on quiet days.
    if used_pct >= 60:
        block += f"\n  \u26a0\ufe0f `{used_pct:.0f}%` of daily limit used \u2014 be selective"

    # Wave 34: context line dropped. The main alert header already shows
    # Trend/ADX/RSI on every alert (see format_alert in bot.py), so the
    # context here was duplicating data. `context` arg kept for backward
    # compat with callers but no longer rendered.
    _ = context  # noqa: kept for backward compat

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

    # May 1: lifetime stats — combine-level, persist across daily resets.
    # May 2 audit: backward-compat for legacy stats files missing lifetime_*
    # fields. Use total_pnl_dollars to reconstruct what we can.
    life_stats = _load_lifetime_stats()
    life_bal  = get_lifetime_balance()
    life_pnl  = get_lifetime_pnl()
    # Starting balance: prefer lifetime field, fall back to current sim start.
    life_start = float(
        life_stats.get("lifetime_starting_balance")
        or state.get("starting_balance", 50_000.0)
    )
    # Peak: prefer recorded lifetime peak; legacy files don't have one, so use
    # max(starting, current bal) as a safe lower bound. Real peak fills in next
    # session close. Note: we deliberately don't use "best_session_pnl" because
    # that's per-session, not cumulative.
    life_peak = float(life_stats.get("lifetime_peak_balance") or max(life_start, life_bal))
    life_pct   = ((life_bal - life_start) / life_start * 100.0) if life_start > 0 else 0.0
    life_sessions = int(life_stats.get("total_sessions", 0))
    life_trades   = int(life_stats.get("total_trades", 0))
    life_wins     = int(life_stats.get("total_wins", 0))
    life_losses   = int(life_stats.get("total_losses", 0))
    life_wr       = round(life_wins / max(1, life_wins + life_losses) * 100, 1)
    life_pnl_str  = f"+${life_pnl:,.2f}" if life_pnl >= 0 else f"-${abs(life_pnl):,.2f}"
    life_drawdown = max(0.0, life_peak - life_bal)
    # Profit target progress: how close we are to combine pass.
    target_amt = float(state.get("profit_target", 3_000.0))
    target_pct = round(min(100.0, max(0.0, (life_pnl / max(1, target_amt)) * 100.0)), 1)

    return (
        f"\U0001f4b0 *Sim Account \u2014 {preset} Eval*\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f3c6 *COMBINE LIFETIME*\n"
        f"  Balance:   `${life_bal:,.2f}` ({life_pct:+.1f}%)\n"
        f"  Total P&L: `{life_pnl_str}`\n"
        f"  Target:    `{target_pct:.1f}%` of `${target_amt:,.0f}` profit goal\n"
        f"  Peak:      `${life_peak:,.2f}` (drawdown: `${life_drawdown:,.2f}`)\n"
        f"  Sessions:  `{life_sessions}` | Trades: `{life_trades}` ({life_wins}W/{life_losses}L \u2014 {life_wr}% WR)\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f4c5 *TODAY'S SESSION*\n"
        # Wave 38 (May 11, 2026): Post-Wave 30 balance carries day-to-day.
        # Old label "(resets at 4PM ET close)" was a lie. The cumulative
        # framing matches the truth: balance only resets when eval ends
        # via bust or pass.
        f"  Balance:    `${risk['balance']:,.2f}` (cumulative this eval)\n"
        f"  Today P&L:  `${plus_dp}{risk['daily_pnl']:,.2f}`\n"
        f"  Daily left: `${risk['daily_left']:,.2f}` ({100-risk['daily_used_pct']:.0f}% remaining)\n"
        f"  Cushion:    `${risk['dd_left']:,.2f}` ({100-risk['dd_used_pct']:.0f}% remaining)\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"*Contract:* `{label}` | *Open trades:* `{open_t}`\n"
        f"*Closed today:* `{len(closed)}` ({wins}W/{losses}L \u2014 {wr}% WR)\n"
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
