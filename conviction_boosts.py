"""
conviction_boosts.py - Wave 7 Iron Robot Conviction Adjustment Layer
====================================================================

Data-backed conviction boost system based on the May 3 backtest of 55
closed trades. Five layers, all additive, all reversible:

  Layer 1: Per-setup conviction boost
           VWAP_BOUNCE_BULL: +10  (proven 71% WR, +$159/trade)
           APPROACH_RESIST:  -10  (proven 31% WR avg)
           VWAP_REJECT_BEAR: -15  (0W/12L combined - dead)
           EMA21_PULLBACK_BULL: -5

  Layer 2: Per-market direction multiplier
           BTC bears: -5  (24% WR overall, mostly LONGs win)
           SOL bears: -10 (14% WR - brutal)
           NQ/GC: full conviction (working)

  Layer 3: Self-tuning bucket recalibration
           Bot tracks its own bucket WR. When MID bucket has 20+ trades
           with <30% WR, auto-raises MIN_CONVICTION floor.

  Layer 4: VWAP_BOUNCE_BULL priority lane
           Proven winner bypasses family cooldown (NOT direct dup-guard).

  Layer 5: Sunday 8 PM auto-tune
           Auto-runs backtest weekly, posts report, adjusts boosts.

CONFIG FILE: data/conviction_boosts.json
  Edit any value to override. Bot reloads on next scan. Delete file
  to revert to baked-in defaults.

PRE-MORTEM (the hard questions, answered):

  Q: What if the +10 boost makes VWAP_BOUNCE_BULL fire in chop?
  A: ADX gate, news floor, dup-guards all still apply. Boost only
     affects the conviction *score* - the gates are independent.

  Q: What if -15 on VWAP_REJECT_BEAR drops conviction below 65 floor
     and it never fires again?
  A: That's the goal. 0W/12L should never fire. If it ever genuinely
     improves, layer 3 detects that and dials back the penalty.

  Q: What if config file gets corrupted?
  A: Loader has try/except -> falls back to baked-in defaults. Logged
     as warning. Bot keeps running.

  Q: What if Layer 3 self-tuning gets confused with sparse data?
  A: Safety floor: only acts when bucket has 20+ trades. Below that,
     no adjustment. Falls back gracefully.

  Q: Performance impact?
  A: Negligible. Config loaded once per scan cycle (already cached for
     1 minute). All math is dict lookups + integer addition.
"""
from __future__ import annotations

import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional

_log = logging.getLogger("nqcalls.conviction_boosts")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_FILE = os.path.join(_BASE_DIR, "data", "conviction_boosts.json")

# Baked-in defaults from May 3 backtest of 55 closed trades.
# These match data/conviction_boosts.json so a deleted config file behaves
# identically to a fresh defaults file.
_DEFAULTS = {
    "layer_1_setup_boosts": {
        "enabled": True,
        "boosts": {
            "VWAP_BOUNCE_BULL":   10,
            "APPROACH_RESIST":   -10,
            "VWAP_REJECT_BEAR":  -15,
            "EMA21_PULLBACK_BULL": -5,
        },
    },
    "layer_2_market_multipliers": {
        "enabled": True,
        "multipliers": {
            "BTC": {"BEAR": -5,  "BULL": 0},
            "SOL": {"BEAR": -10, "BULL": 0},
            "NQ":  {"BEAR": 0,   "BULL": 0},
            "GC":  {"BEAR": 0,   "BULL": 0},
        },
    },
    "layer_3_bucket_recalibration": {
        "enabled": True,
        "min_trades_to_act": 20,
        "wr_threshold_to_raise_floor": 30.0,
        "max_floor_adjustment": 10,
        "current_floor_adjustment": 0,
        "last_recalibrated_at": None,
    },
    "layer_4_priority_lane": {
        "enabled": True,
        "priority_setups": ["VWAP_BOUNCE_BULL"],
        "bypass_family_cooldown": True,
        "bypass_zone_cooldown": False,
    },
    "layer_5_auto_tune": {
        "enabled": True,
        "schedule_day": "Sunday",
        "schedule_hour_et": 20,
        "rolling_window_days": 28,
        "max_adjustment_per_cycle": 5,
        "min_trades_to_adjust": 10,
        "last_run_at": None,
        "last_run_summary": None,
    },
    # Wave 9 (May 4): Edge Decay Defense
    # The Wave 7 hardcoded boosts were calibrated on data that turned stale
    # within 2 weeks (NQ:VWAP_BOUNCE_BULL went from 71% WR -> 0% WR in 14 days).
    # This layer auto-zeros a positive boost if the underlying setup has gone
    # cold in the recent 14-day window. Protects against fitting noise from
    # an old market regime.
    "layer_6_edge_decay": {
        "enabled": True,
        "window_days": 14,           # rolling window to evaluate decay
        "min_trades_for_decay": 3,   # need at least 3 closed trades to act
        "wr_threshold_to_zero": 25.0, # if WR < 25%, zero out positive boost
        "penalty_threshold_wr": 30.0, # if WR < 30%, apply -5 penalty even if no boost
        "max_decay_penalty": -10,    # cap penalty so we don't overshoot
        "last_decay_check_at": None,
        "last_decay_actions": [],    # list of {setup, action, boost_before, boost_after}
    },
    # Wave 9 (May 4): Daily soft auto-tune
    # Sunday 8 PM is too slow for weekly market regime changes. This runs
    # every day at 6 AM ET (before NY open) with smaller adjustment caps
    # so the bot adapts faster but can't thrash. Compounds with weekly tune.
    "layer_7_daily_soft_tune": {
        "enabled": True,
        "schedule_hour_et": 6,        # 6 AM ET daily
        "window_days": 7,             # last 7 days for daily tuning
        "min_trades_to_adjust": 5,    # smaller threshold than weekly
        "max_adjustment_per_cycle": 3, # smaller caps than weekly
        "min_dollar_threshold": 30.0, # only act if |$/trade| > $30
        "last_run_at": None,
        "last_run_summary": None,
    },
}

# In-memory cache to avoid disk reads on every scan
_CACHE: Optional[dict] = None
_CACHE_LOADED_AT: float = 0.0
_CACHE_TTL_SEC = 60.0  # reload from disk at most once per minute


def _load_config() -> dict:
    """Load config from disk with TTL cache. Returns merged config (defaults + overrides)."""
    global _CACHE, _CACHE_LOADED_AT
    import time
    now = time.time()
    if _CACHE is not None and (now - _CACHE_LOADED_AT) < _CACHE_TTL_SEC:
        return _CACHE

    config = json.loads(json.dumps(_DEFAULTS))  # deep copy of defaults
    if os.path.exists(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                disk = json.load(f)
            # Merge: disk values override defaults, but missing keys keep defaults.
            for layer_name, layer_cfg in disk.items():
                if layer_name.startswith("_"):
                    continue
                if layer_name in config and isinstance(layer_cfg, dict):
                    config[layer_name].update(layer_cfg)
                else:
                    config[layer_name] = layer_cfg
        except Exception as e:
            _log.warning(f"conviction_boosts config load failed ({e}); using baked defaults")

    _CACHE = config
    _CACHE_LOADED_AT = now
    return config


def _save_config(config: dict) -> bool:
    """Atomically write config to disk. Used by Layer 3 + Layer 5 auto-adjust."""
    try:
        from safe_io import atomic_write_json
        atomic_write_json(_CONFIG_FILE, config)
        # Invalidate cache so next read picks up changes
        global _CACHE
        _CACHE = None
        return True
    except Exception as e:
        _log.warning(f"conviction_boosts save failed: {e}")
        return False


def reset_cache() -> None:
    """Force reload from disk on next access. Useful after manual edits."""
    global _CACHE
    _CACHE = None


# ============================================================
# LAYER 1 + 2: Apply conviction adjustment to a single signal
# ============================================================
def adjust_conviction(base_conviction: int, market: str, setup_type: str,
                      direction: str) -> tuple[int, dict]:
    """
    Apply Layer 1 (setup boost) + Layer 2 (market multiplier) to a base
    conviction score. Returns (adjusted_score, breakdown_dict).

    Args:
        base_conviction: The conviction from outcome_tracker.conviction_score()
        market: NQ, GC, BTC, or SOL
        setup_type: VWAP_BOUNCE_BULL, APPROACH_RESIST, etc.
        direction: LONG, SHORT, WATCH_LONG, WATCH_SHORT

    Returns:
        (final_conviction, breakdown) where breakdown is a dict with keys:
            base, setup_boost, market_mult, final, applied_layers
        Final is clamped to [0, 100].

    The breakdown dict is included in alert metadata so users can see
    exactly why a setup was boosted/penalized.
    """
    cfg = _load_config()
    breakdown = {
        "base": int(base_conviction),
        "setup_boost": 0,
        "market_mult": 0,
        "final": int(base_conviction),
        "applied_layers": [],
    }

    # Layer 1: setup-specific boost
    l1 = cfg.get("layer_1_setup_boosts", {})
    if l1.get("enabled"):
        boosts = l1.get("boosts", {})
        if setup_type in boosts:
            b = int(boosts[setup_type])
            breakdown["setup_boost"] = b
            breakdown["applied_layers"].append(f"L1:{setup_type}{'+' if b >= 0 else ''}{b}")

    # Layer 2: per-market direction multiplier
    l2 = cfg.get("layer_2_market_multipliers", {})
    if l2.get("enabled"):
        mults = l2.get("multipliers", {})
        market_cfg = mults.get(market, {})
        # Determine direction class
        is_bear = ("BEAR" in setup_type) or ("SHORT" in direction)
        is_bull = ("BULL" in setup_type) or ("LONG" in direction)
        if is_bear and "BEAR" in market_cfg:
            m = int(market_cfg["BEAR"])
            if m != 0:
                breakdown["market_mult"] = m
                breakdown["applied_layers"].append(f"L2:{market}-BEAR{'+' if m >= 0 else ''}{m}")
        elif is_bull and "BULL" in market_cfg:
            m = int(market_cfg["BULL"])
            if m != 0:
                breakdown["market_mult"] = m
                breakdown["applied_layers"].append(f"L2:{market}-BULL{'+' if m >= 0 else ''}{m}")

    # Compute final, clamped
    final = breakdown["base"] + breakdown["setup_boost"] + breakdown["market_mult"]
    final = max(0, min(100, final))
    breakdown["final"] = final
    return final, breakdown


# ============================================================
# LAYER 3: Self-tuning bucket recalibration
# ============================================================
def get_min_conviction_adjustment() -> int:
    """
    Return the current Layer 3 floor adjustment. Added to cfg.MIN_CONVICTION
    when checking if a setup fires. Updated by recalibrate_bucket_floors().
    """
    cfg = _load_config()
    l3 = cfg.get("layer_3_bucket_recalibration", {})
    if not l3.get("enabled"):
        return 0
    return int(l3.get("current_floor_adjustment", 0))


def recalibrate_bucket_floors(force: bool = False) -> dict:
    """
    Walk closed trades from outcomes.csv, compute per-bucket WR, and adjust
    the floor if MID/UPPER-MID bucket has 20+ trades with <30% WR.

    Called weekly by Layer 5 auto-tune. Can also be called via the new
    /tune Telegram command.

    Returns: {"action": str, "floor_before": int, "floor_after": int, ...}
    """
    cfg = _load_config()
    l3 = cfg.get("layer_3_bucket_recalibration", {})
    if not l3.get("enabled") and not force:
        return {"action": "skipped (layer disabled)", "changed": False}

    min_trades = int(l3.get("min_trades_to_act", 20))
    wr_thresh = float(l3.get("wr_threshold_to_raise_floor", 30.0))
    max_adj = int(l3.get("max_floor_adjustment", 10))
    current_adj = int(l3.get("current_floor_adjustment", 0))

    # Load outcomes
    outcomes_path = os.path.join(_BASE_DIR, "outcomes.csv")
    if not os.path.exists(outcomes_path):
        return {"action": "skipped (no outcomes.csv)", "changed": False}

    import csv
    buckets = {"HIGH (80+)": {"w": 0, "l": 0}, "UPPER-MID (70-79)": {"w": 0, "l": 0},
               "MID (65-69)": {"w": 0, "l": 0}, "LOW (50-64)": {"w": 0, "l": 0}}
    try:
        with open(outcomes_path, "r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("status") != "CLOSED":
                    continue
                result = r.get("result")
                if result not in ("WIN", "LOSS"):
                    continue
                try:
                    c = int(float(r.get("conviction", 0)))
                except Exception:
                    continue
                if c >= 80:
                    bucket = "HIGH (80+)"
                elif c >= 70:
                    bucket = "UPPER-MID (70-79)"
                elif c >= 65:
                    bucket = "MID (65-69)"
                elif c >= 50:
                    bucket = "LOW (50-64)"
                else:
                    continue
                if result == "WIN":
                    buckets[bucket]["w"] += 1
                else:
                    buckets[bucket]["l"] += 1
    except Exception as e:
        _log.warning(f"recalibrate_bucket_floors: {e}")
        return {"action": f"failed: {e}", "changed": False}

    # Decision logic: if MID has 20+ and <30% WR, raise floor by 5.
    # If UPPER-MID is also losing, raise by 10. Cap at max_adj.
    new_adj = current_adj
    reason_parts = []

    mid = buckets["MID (65-69)"]
    mid_total = mid["w"] + mid["l"]
    mid_wr = (mid["w"] / mid_total * 100) if mid_total > 0 else 100

    upper = buckets["UPPER-MID (70-79)"]
    upper_total = upper["w"] + upper["l"]
    upper_wr = (upper["w"] / upper_total * 100) if upper_total > 0 else 100

    if mid_total >= min_trades and mid_wr < wr_thresh:
        new_adj = max(new_adj, 5)
        reason_parts.append(f"MID({mid_total}) WR {mid_wr:.0f}% < {wr_thresh}")

    if upper_total >= min_trades and upper_wr < wr_thresh:
        new_adj = max(new_adj, 10)
        reason_parts.append(f"UPPER-MID({upper_total}) WR {upper_wr:.0f}% < {wr_thresh}")

    new_adj = min(new_adj, max_adj)

    # If floor adjustment decreased (buckets recovered), gradually relax it.
    # Only relax by 1 per cycle to avoid flapping.
    if new_adj < current_adj:
        new_adj = current_adj - 1

    result = {
        "action": "no_change",
        "changed": False,
        "floor_before": current_adj,
        "floor_after": new_adj,
        "buckets": {k: {"wins": v["w"], "losses": v["l"],
                        "total": v["w"] + v["l"],
                        "wr": round((v["w"] / max(1, v["w"] + v["l"]) * 100), 1)}
                    for k, v in buckets.items()},
        "reason": " AND ".join(reason_parts) if reason_parts else "buckets healthy",
    }

    if new_adj != current_adj:
        cfg["layer_3_bucket_recalibration"]["current_floor_adjustment"] = new_adj
        cfg["layer_3_bucket_recalibration"]["last_recalibrated_at"] = \
            datetime.now(timezone.utc).isoformat()
        if _save_config(cfg):
            result["action"] = "raised" if new_adj > current_adj else "relaxed"
            result["changed"] = True
            _log.info(f"recalibrate_bucket_floors: {current_adj} -> {new_adj} "
                      f"(reason: {result['reason']})")
    return result


# ============================================================
# LAYER 4: Priority lane bypass
# ============================================================
def is_priority_setup(setup_type: str) -> bool:
    """
    Returns True if this setup is on the priority lane. Used by bot.py
    to decide whether to bypass family/zone cooldowns.
    """
    cfg = _load_config()
    l4 = cfg.get("layer_4_priority_lane", {})
    if not l4.get("enabled"):
        return False
    return setup_type in l4.get("priority_setups", [])


def can_bypass_family_cooldown(setup_type: str) -> bool:
    cfg = _load_config()
    l4 = cfg.get("layer_4_priority_lane", {})
    if not l4.get("enabled"):
        return False
    if setup_type not in l4.get("priority_setups", []):
        return False
    return bool(l4.get("bypass_family_cooldown", False))


def can_bypass_zone_cooldown(setup_type: str) -> bool:
    cfg = _load_config()
    l4 = cfg.get("layer_4_priority_lane", {})
    if not l4.get("enabled"):
        return False
    if setup_type not in l4.get("priority_setups", []):
        return False
    return bool(l4.get("bypass_zone_cooldown", False))


# ============================================================
# LAYER 5: Auto-tune (Sunday 8 PM)
# ============================================================
def should_run_auto_tune_now() -> bool:
    """
    Returns True if today is the configured auto-tune day at the configured
    hour AND we haven't run yet today. Called by the bot's scan loop.
    """
    cfg = _load_config()
    l5 = cfg.get("layer_5_auto_tune", {})
    if not l5.get("enabled"):
        return False

    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        now = datetime.now(et)
    except Exception:
        # Fallback: UTC - 4. Slightly off during EDT/EST transitions but fine.
        from datetime import timedelta
        now = datetime.now(timezone.utc) - timedelta(hours=4)

    target_day = l5.get("schedule_day", "Sunday")
    target_hour = int(l5.get("schedule_hour_et", 20))
    if now.strftime("%A") != target_day:
        return False
    if now.hour != target_hour:
        return False

    # Don't run if we already ran in the last 23 hours
    last_run = l5.get("last_run_at")
    if last_run:
        try:
            from datetime import timedelta
            last = datetime.fromisoformat(last_run)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - last < timedelta(hours=23):
                return False
        except Exception:
            pass

    return True


def run_auto_tune() -> dict:
    """
    Sunday 8 PM auto-tune cycle.

    1. Compute per-setup edge from last 28 days of closed trades
    2. For each setup with >= min_trades_to_adjust trades:
         - If $/trade is positive AND not already boosted: nudge boost +5
         - If $/trade is negative AND boost is positive: nudge boost -5
         - Cap at max_adjustment_per_cycle (5) per cycle
    3. Run Layer 3 bucket recalibration too
    4. Save config, return summary for Telegram report

    All adjustments are SMALL (5 points max per week) to avoid overfitting
    to noise. Multiple cycles compound over months.
    """
    cfg = _load_config()
    l5 = cfg.get("layer_5_auto_tune", {})
    if not l5.get("enabled"):
        return {"action": "skipped (disabled)", "changes": []}

    window_days = int(l5.get("rolling_window_days", 28))
    min_trades = int(l5.get("min_trades_to_adjust", 10))
    max_adj = int(l5.get("max_adjustment_per_cycle", 5))

    # Compute per-setup stats from outcomes.csv (last N days)
    outcomes_path = os.path.join(_BASE_DIR, "outcomes.csv")
    if not os.path.exists(outcomes_path):
        return {"action": "skipped (no outcomes.csv)", "changes": []}

    import csv
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    setup_stats = {}
    try:
        with open(outcomes_path, "r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("status") != "CLOSED":
                    continue
                result = r.get("result")
                if result not in ("WIN", "LOSS"):
                    continue
                try:
                    ts = datetime.fromisoformat(r.get("timestamp", ""))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        continue
                except Exception:
                    continue
                setup = r.get("setup", "?")
                try:
                    rr = float(r.get("rr", 0))
                except Exception:
                    rr = 0.0
                if setup not in setup_stats:
                    setup_stats[setup] = {"w": 0, "l": 0, "dollar": 0.0}
                if result == "WIN":
                    setup_stats[setup]["w"] += 1
                    setup_stats[setup]["dollar"] += rr * 100.0
                else:
                    setup_stats[setup]["l"] += 1
                    setup_stats[setup]["dollar"] -= 100.0
    except Exception as e:
        return {"action": f"failed: {e}", "changes": []}

    # Apply nudges
    l1 = cfg.get("layer_1_setup_boosts", {})
    boosts = l1.get("boosts", {}).copy()
    changes = []

    for setup, stats in setup_stats.items():
        total = stats["w"] + stats["l"]
        if total < min_trades:
            continue
        avg_dollar = stats["dollar"] / total
        wr = stats["w"] / total * 100
        current_boost = boosts.get(setup, 0)
        new_boost = current_boost

        # Positive EV setup with no boost yet -> bump up
        if avg_dollar > 50 and current_boost < 10:
            new_boost = min(current_boost + max_adj, 15)
        # Negative EV setup with positive boost -> bump down
        elif avg_dollar < -50 and current_boost > -15:
            new_boost = max(current_boost - max_adj, -20)

        if new_boost != current_boost:
            boosts[setup] = new_boost
            changes.append({
                "setup": setup,
                "wr": round(wr, 1),
                "avg_dollar": round(avg_dollar, 0),
                "trades": total,
                "boost_before": current_boost,
                "boost_after": new_boost,
            })

    # Save updated boosts
    if changes:
        cfg["layer_1_setup_boosts"]["boosts"] = boosts

    # Also run Layer 3 recalibration
    l3_result = recalibrate_bucket_floors(force=False)

    # Update last_run timestamp
    cfg["layer_5_auto_tune"]["last_run_at"] = datetime.now(timezone.utc).isoformat()
    cfg["layer_5_auto_tune"]["last_run_summary"] = {
        "cycle_changes": len(changes),
        "l3_changed": l3_result.get("changed", False),
        "n_setups_analyzed": len(setup_stats),
    }
    _save_config(cfg)

    return {
        "action": "completed",
        "changes": changes,
        "n_setups_analyzed": len(setup_stats),
        "window_days": window_days,
        "l3_recalibration": l3_result,
    }


# ============================================================
# LAYER 6 (Wave 9): Edge Decay Defense
# ============================================================
def _read_setup_outcomes_in_window(window_days: int) -> dict:
    """
    Walk outcomes.csv and return per-setup stats for closed trades within
    the last `window_days` days. Returns dict[setup_type] = {w, l, dollar}.

    Wave 9 (May 4): shared helper used by Layer 6 (edge decay) and
    Layer 7 (daily soft tune) so both read the same source of truth.

    Note: groups by setup_type ALONE (not market:setup), matching how
    Layer 1 boosts work. A boost on VWAP_BOUNCE_BULL applies across all
    markets, so its decay should reflect cross-market WR.
    """
    outcomes_path = os.path.join(_BASE_DIR, "outcomes.csv")
    if not os.path.exists(outcomes_path):
        return {}
    import csv
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    stats = {}
    try:
        with open(outcomes_path, "r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("status") != "CLOSED":
                    continue
                result = r.get("result")
                if result not in ("WIN", "LOSS"):
                    continue
                try:
                    ts = datetime.fromisoformat(r.get("timestamp", ""))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        continue
                except Exception:
                    continue
                setup = r.get("setup", "?")
                try:
                    rr = float(r.get("rr", 0))
                except Exception:
                    rr = 0.0
                if setup not in stats:
                    stats[setup] = {"w": 0, "l": 0, "dollar": 0.0}
                if result == "WIN":
                    stats[setup]["w"] += 1
                    stats[setup]["dollar"] += rr * 100.0
                else:
                    stats[setup]["l"] += 1
                    stats[setup]["dollar"] -= 100.0
    except Exception as e:
        _log.warning(f"_read_setup_outcomes_in_window: {e}")
    return stats


def check_edge_decay(force: bool = False) -> dict:
    """
    Wave 9 (May 4): Edge Decay Defense.

    Walks recent (last 14d) closed trades. For each setup that currently
    has a positive Layer 1 boost, check if it's gone cold:
      - If WR < 25% with 3+ trades -> zero out the boost
      - If WR < 30% (and no positive boost yet) -> apply -5 penalty

    The Wave 7 boosts were hard-coded from May 3 backtest data. Within
    2 weeks, NQ:VWAP_BOUNCE_BULL went from 71% WR to 0%. Without this
    layer, the bot would keep boosting a dead setup forever.

    Returns: {action, changed, decay_actions: [...]}
    """
    cfg = _load_config()
    l6 = cfg.get("layer_6_edge_decay", {})
    if not l6.get("enabled") and not force:
        return {"action": "skipped (layer disabled)", "changed": False, "decay_actions": []}

    window_days = int(l6.get("window_days", 14))
    min_trades = int(l6.get("min_trades_for_decay", 3))
    zero_thresh = float(l6.get("wr_threshold_to_zero", 25.0))
    pen_thresh = float(l6.get("penalty_threshold_wr", 30.0))
    max_penalty = int(l6.get("max_decay_penalty", -10))

    setup_stats = _read_setup_outcomes_in_window(window_days)
    l1 = cfg.get("layer_1_setup_boosts", {})
    boosts = l1.get("boosts", {}).copy()
    decay_actions = []

    for setup, stats in setup_stats.items():
        total = stats["w"] + stats["l"]
        if total < min_trades:
            continue
        wr = stats["w"] / total * 100
        current_boost = boosts.get(setup, 0)

        # Case 1: Setup has a positive boost but WR < zero_threshold
        # -> zero it out
        if current_boost > 0 and wr < zero_thresh:
            decay_actions.append({
                "setup": setup,
                "action": "zeroed",
                "reason": f"{stats['w']}W/{stats['l']}L = {wr:.1f}% WR < {zero_thresh}",
                "boost_before": current_boost,
                "boost_after": 0,
                "window_days": window_days,
            })
            boosts[setup] = 0

        # Case 2: Setup has zero/positive boost but WR < penalty threshold
        # AND wasn't already zeroed -> apply -5 penalty
        elif current_boost >= 0 and wr < pen_thresh and current_boost == 0:
            new_boost = max(max_penalty, current_boost - 5)
            decay_actions.append({
                "setup": setup,
                "action": "penalized",
                "reason": f"{stats['w']}W/{stats['l']}L = {wr:.1f}% WR < {pen_thresh}",
                "boost_before": current_boost,
                "boost_after": new_boost,
                "window_days": window_days,
            })
            boosts[setup] = new_boost

        # Case 3: Setup has a negative boost but WR has recovered
        # -> gradually relax the penalty (1 point per cycle)
        elif current_boost < 0 and wr >= pen_thresh:
            new_boost = min(0, current_boost + 1)
            if new_boost != current_boost:
                decay_actions.append({
                    "setup": setup,
                    "action": "relaxed",
                    "reason": f"{stats['w']}W/{stats['l']}L = {wr:.1f}% WR >= {pen_thresh}",
                    "boost_before": current_boost,
                    "boost_after": new_boost,
                    "window_days": window_days,
                })
                boosts[setup] = new_boost

    # Save updated config if anything changed
    changed = len(decay_actions) > 0
    if changed:
        cfg["layer_1_setup_boosts"]["boosts"] = boosts
        cfg["layer_6_edge_decay"]["last_decay_check_at"] = datetime.now(timezone.utc).isoformat()
        cfg["layer_6_edge_decay"]["last_decay_actions"] = decay_actions[-20:]  # keep last 20
        _save_config(cfg)
        _log.info(f"check_edge_decay: {len(decay_actions)} actions applied")
        for action in decay_actions:
            _log.info(f"  {action['action']} {action['setup']}: "
                      f"{action['boost_before']} -> {action['boost_after']} "
                      f"({action['reason']})")

    return {
        "action": "applied" if changed else "no_change",
        "changed": changed,
        "decay_actions": decay_actions,
        "window_days": window_days,
        "n_setups_analyzed": len(setup_stats),
    }


# ============================================================
# LAYER 7 (Wave 9): Daily soft auto-tune
# ============================================================
def should_run_daily_soft_tune_now() -> bool:
    """
    Wave 9 (May 4): Returns True if we're at the configured daily soft-tune
    hour AND haven't run yet today. Distinct from Layer 5 (Sunday 8 PM weekly).
    """
    cfg = _load_config()
    l7 = cfg.get("layer_7_daily_soft_tune", {})
    if not l7.get("enabled"):
        return False

    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        now = datetime.now(et)
    except Exception:
        from datetime import timedelta
        now = datetime.now(timezone.utc) - timedelta(hours=4)

    target_hour = int(l7.get("schedule_hour_et", 6))
    if now.hour != target_hour:
        return False

    # Don't run if we already ran in the last 23 hours
    last_run = l7.get("last_run_at")
    if last_run:
        try:
            from datetime import timedelta
            last = datetime.fromisoformat(last_run)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - last < timedelta(hours=23):
                return False
        except Exception:
            pass

    return True


def run_daily_soft_tune() -> dict:
    """
    Wave 9 (May 4): Daily soft auto-tune cycle.

    Compared to Layer 5 (Sunday weekly):
      - Window: 7 days (vs 28)
      - Min trades: 5 (vs 10)
      - Max adjustment: +/-3 per cycle (vs +/-5)
      - Min dollar threshold: $30 (vs $50)

    Step 1: Run edge-decay check (Layer 6) FIRST. Zero out boosts on
            stale-edge setups before computing new tunings.
    Step 2: For each setup with 5+ trades in last 7 days:
              - Positive EV (>$30/trade) and not max-boosted -> +3
              - Negative EV (<-$30/trade) and not max-penalized -> -3
    Step 3: Save config with new boosts + last_run timestamp.

    Compounds with Layer 5 (Sunday) for fast adaptation.
    """
    cfg = _load_config()
    l7 = cfg.get("layer_7_daily_soft_tune", {})
    if not l7.get("enabled"):
        return {"action": "skipped (disabled)", "changes": [], "decay": {}}

    # Step 1: Edge decay check first
    decay_result = check_edge_decay()

    # Reload config after edge-decay (may have updated boosts)
    cfg = _load_config()
    window_days = int(l7.get("window_days", 7))
    min_trades = int(l7.get("min_trades_to_adjust", 5))
    max_adj = int(l7.get("max_adjustment_per_cycle", 3))
    min_dollar = float(l7.get("min_dollar_threshold", 30.0))

    setup_stats = _read_setup_outcomes_in_window(window_days)
    l1 = cfg.get("layer_1_setup_boosts", {})
    boosts = l1.get("boosts", {}).copy()
    changes = []

    for setup, stats in setup_stats.items():
        total = stats["w"] + stats["l"]
        if total < min_trades:
            continue
        avg_dollar = stats["dollar"] / total
        wr = stats["w"] / total * 100
        current_boost = boosts.get(setup, 0)
        new_boost = current_boost

        if avg_dollar > min_dollar and current_boost < 15:
            new_boost = min(current_boost + max_adj, 15)
        elif avg_dollar < -min_dollar and current_boost > -20:
            new_boost = max(current_boost - max_adj, -20)

        if new_boost != current_boost:
            boosts[setup] = new_boost
            changes.append({
                "setup": setup,
                "wr": round(wr, 1),
                "avg_dollar": round(avg_dollar, 0),
                "trades": total,
                "boost_before": current_boost,
                "boost_after": new_boost,
            })

    # Save
    if changes:
        cfg["layer_1_setup_boosts"]["boosts"] = boosts

    cfg["layer_7_daily_soft_tune"]["last_run_at"] = datetime.now(timezone.utc).isoformat()
    cfg["layer_7_daily_soft_tune"]["last_run_summary"] = {
        "cycle_changes": len(changes),
        "decay_actions": len(decay_result.get("decay_actions", [])),
        "n_setups_analyzed": len(setup_stats),
    }
    _save_config(cfg)

    return {
        "action": "completed",
        "changes": changes,
        "decay": decay_result,
        "n_setups_analyzed": len(setup_stats),
        "window_days": window_days,
    }


# ============================================================
# Status / introspection (used by /edge and /tune Telegram cmds)
# ============================================================
def get_status_text() -> str:
    """Return a Telegram-formatted status of all 5 layers."""
    cfg = _load_config()
    lines = ["\U0001f9e0 *Wave 7 Iron Robot Status*", "\u2501" * 16]

    # Layer 1
    l1 = cfg.get("layer_1_setup_boosts", {})
    en = "\u2705" if l1.get("enabled") else "\u26d4"
    lines.append(f"{en} *L1: Setup Boosts*")
    boosts = l1.get("boosts", {})
    if boosts:
        for s, b in sorted(boosts.items(), key=lambda x: -x[1]):
            sign = "+" if b >= 0 else ""
            icon = "\U0001f7e2" if b > 0 else ("\U0001f534" if b < 0 else "\U000026aa")
            lines.append(f"  {icon} `{s}` {sign}{b}")
    else:
        lines.append("  (no setups boosted)")

    # Layer 2
    l2 = cfg.get("layer_2_market_multipliers", {})
    en = "\u2705" if l2.get("enabled") else "\u26d4"
    lines.append(f"{en} *L2: Market Multipliers*")
    mults = l2.get("multipliers", {})
    for mkt, dirs in mults.items():
        bear = dirs.get("BEAR", 0)
        bull = dirs.get("BULL", 0)
        if bear == 0 and bull == 0:
            continue
        parts = []
        if bear != 0:
            parts.append(f"BEAR{'+' if bear >= 0 else ''}{bear}")
        if bull != 0:
            parts.append(f"BULL{'+' if bull >= 0 else ''}{bull}")
        lines.append(f"  `{mkt}`: {', '.join(parts)}")

    # Layer 3
    l3 = cfg.get("layer_3_bucket_recalibration", {})
    en = "\u2705" if l3.get("enabled") else "\u26d4"
    adj = l3.get("current_floor_adjustment", 0)
    lines.append(f"{en} *L3: Bucket Floor Adj* `+{adj}`")

    # Layer 4
    l4 = cfg.get("layer_4_priority_lane", {})
    en = "\u2705" if l4.get("enabled") else "\u26d4"
    setups = l4.get("priority_setups", [])
    lines.append(f"{en} *L4: Priority Lane* `{', '.join(setups) if setups else '(none)'}`")

    # Layer 5
    l5 = cfg.get("layer_5_auto_tune", {})
    en = "\u2705" if l5.get("enabled") else "\u26d4"
    last = l5.get("last_run_at", "never")
    if last and last != "never":
        try:
            t = datetime.fromisoformat(last)
            last = t.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            pass
    lines.append(f"{en} *L5: Weekly Auto-Tune* (last: `{last}`)")

    # Layer 6 (Wave 9)
    l6 = cfg.get("layer_6_edge_decay", {})
    en = "\u2705" if l6.get("enabled") else "\u26d4"
    last_decay = l6.get("last_decay_check_at", "never")
    if last_decay and last_decay != "never":
        try:
            t = datetime.fromisoformat(last_decay)
            last_decay = t.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            pass
    last_actions = l6.get("last_decay_actions", [])
    lines.append(f"{en} *L6: Edge Decay Defense* (last check: `{last_decay}`)")
    if last_actions:
        lines.append(f"  Last {min(3, len(last_actions))} actions:")
        for a in last_actions[-3:]:
            lines.append(f"  \u2022 `{a['setup']}` {a['action']} "
                         f"({a['boost_before']}\u2192{a['boost_after']}) — {a['reason']}")

    # Layer 7 (Wave 9)
    l7 = cfg.get("layer_7_daily_soft_tune", {})
    en = "\u2705" if l7.get("enabled") else "\u26d4"
    last_soft = l7.get("last_run_at", "never")
    if last_soft and last_soft != "never":
        try:
            t = datetime.fromisoformat(last_soft)
            last_soft = t.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            pass
    lines.append(f"{en} *L7: Daily Soft Tune* (last: `{last_soft}`)")

    lines.append("\u2501" * 16)
    lines.append("_Edit data/conviction_boosts.json to override._")
    return "\n".join(lines)
