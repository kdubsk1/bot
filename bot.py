"""
bot.py - NQ CALLS 2026
========================
Engine. outcome_tracker.py = brain. strategy_log.py = memory.
All critical bugs from Opus review applied:
  - DST-aware timezone (zoneinfo)
  - Per-market cooldowns, cooldown set AFTER fire only
  - Persistent cooldowns across restarts
  - Centralized get_frames (one fetch per scan cycle)
  - Volume sanity check
  - APPROACH setup deduplication (active_setups.json)
  - Per-setup ADX_MIN_BY_SETUP
  - httpx log silenced
  - tg_send retry (3x)
  - ENTER NOW alert headers (not CONFIRMED SETUP)
  - Contract size capped: 5 MNQ or 1 NQ max
  - Zone lockouts, family cooldowns, session halts
  - Regime-aware entry gates
  - Topstep drawdown awareness
"""
import asyncio, logging, os, traceback, json
import time as _time
import random as _rnd  # Pre-Batch 2026-04-20: for sampled REJECTED logging
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    ET_ZONE = ZoneInfo("America/New_York")
except ImportError:
    ET_ZONE = None

def _now_et():
    if ET_ZONE: return datetime.now(ET_ZONE)
    return datetime.now(timezone.utc) - timedelta(hours=4)

import pandas as pd
import numpy as np
from data_layer import get_frames as dl_get_frames, get_current_price, probe_nq_symbol, probe_gc_symbol, probe_topstepx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

import outcome_tracker as ot
import safe_io  # Wave 48: atomic_write_json helpers
# Wave 48 (May 12, 2026): state files now use atomic writes - see safe_io.atomic_write_json call sites.
from markets import get_market_config, get_all_markets
import sim_account as sim
import crypto_sim
import strategy_log as sl
import dashboard as dash
import strategy_review as sr
from config import TELEGRAM_TOKEN, CHAT_ID
from session_clock import SessionClock, SessionEvent, get_session_date
import auto_sync  # Persistence: commits data/ + outcomes.csv to GitHub every 6h so Railway runtime data survives restarts
import trend_memory  # Wave 68: self-grading trend-read memory  _WAVE68_TREND_MEM
import conviction_boosts as cb  # Wave 7: Iron Robot conviction adjustment layer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, "bot_log.txt"), encoding="utf-8"),
        logging.StreamHandler()
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)
log = logging.getLogger("nqcalls")


# Wave 50 (May 13, 2026): in-memory tracker for last Telegram alert per
# open trade. Used by watch_open_trades to send 2-hour heartbeats when
# rescore returns HOLD (no significant change to alert on). Trader sees
# at least one status update per open trade every 2 hours.
#
# Keyed by alert_id. Pruned per scan to drop entries for closed trades.
# Resets on bot restart (acceptable: worst case is one extra heartbeat
# burst after restart, which is actually useful behavior).
_LAST_HEARTBEAT = {}
_HEARTBEAT_INTERVAL_SEC = 2 * 60 * 60  # 2 hours

SETTINGS = {
    "scanner_on": False, "scan_interval_min": 5, "cooldown_min": 60,
    "min_rr": 1.5,
    "min_conviction": 65, "account_risk_pct": 1.5,
    "morning_brief": True, "asia_brief": True, "rescore_on": True,
    "markets": {"NQ": True, "GC": True, "BTC": True, "SOL": True}
}
ot.set_account_risk_pct(SETTINGS["account_risk_pct"])

# ── Scanner state persistence (Task 1) ───────────────────────────
SCANNER_STATE_FILE = os.path.join(BASE_DIR, "data", "scanner_state.json")

# Wave 26 (May 11, 2026): bot brain log - exhaustive internal record
# for Claude analysis. Phantom-loss detections, mid-trade re-scores,
# partial-exit suggestions go here INSTEAD of Telegram. Telegram
# stays curated (entries, real exits, briefs, watchdog, commands).
BOT_BRAIN_FILE = os.path.join(BASE_DIR, "data", "bot_brain.jsonl")

def _save_scanner_state():
    """
    Persist scanner_on so it survives Railway restarts.
    Wave 22 (May 9, 2026): also append JSONL event for audit trail.
    """
    try:
        os.makedirs(os.path.dirname(SCANNER_STATE_FILE), exist_ok=True)
        data = {
            "scanner_on":   bool(SETTINGS["scanner_on"]),
            "last_changed": datetime.now(timezone.utc).isoformat(),
        }
        # Wave 48 (May 12, 2026): atomic write.
        safe_io.atomic_write_json(SCANNER_STATE_FILE, data)
        # Wave 22: persistent JSONL audit log of every scanner toggle
        try:
            evt_path = os.path.join(BASE_DIR, "data", "scanner_events.jsonl")
            evt = {
                "timestamp":   data["last_changed"],
                "scanner_on":  data["scanner_on"],
                "action":      "TURNED_ON" if data["scanner_on"] else "TURNED_OFF",
            }
            with open(evt_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(evt) + "\n")
        except Exception:
            pass  # never break state save on logging failure
    except Exception as e:
        log.warning(f"_save_scanner_state: {e}")

def bot_brain_log(event_type: str, data: dict):
    """
    Wave 26 (May 11, 2026): Persistent exhaustive log of the bot's
    internal thoughts.

    Used for events that are signal-worthy for Claude analysis but
    NOT signal-worthy for Telegram. Examples:
      - phantom_loss   : guard blocked a phantom-priced close
      - partial_exit   : 1R hit, would suggest partial off
      - rescore        : mid-trade conviction shift

    Each entry is a single JSON line in data/bot_brain.jsonl.
    Append-only, never trimmed. Cheap (~500 bytes/event, ~25KB/day max).

    Wrapped in try/except - logging failure must never crash the bot.
    """
    try:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type":      event_type,
        }
        if isinstance(data, dict):
            entry.update(data)
        else:
            entry["raw"] = str(data)
        os.makedirs(os.path.dirname(BOT_BRAIN_FILE), exist_ok=True)
        with open(BOT_BRAIN_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as _bb_err:
        try:
            log.warning(f"bot_brain_log[{event_type}] failed: {_bb_err}")
        except Exception:
            pass  # logger itself unavailable - silently drop


def _load_scanner_state() -> dict:
    """Returns {'scanner_on': bool, 'last_changed': iso_str, 'hours_ago': float}."""
    # Wave 57 (May 13, 2026): stale-OFF defaults to ON for autonomous mode.
    # Background: Railway redeploys were leaving the bot off because the state
    # file had a stale OFF from days ago. Cockroach autonomy requires bot to
    # come back on after restart unless very recently toggled off.
    try:
        if os.path.exists(SCANNER_STATE_FILE):
            with open(SCANNER_STATE_FILE) as f:
                data = json.load(f)
            last = data.get("last_changed", "")
            scanner_on = bool(data.get("scanner_on", False))
            hours_ago = 0.0
            if last:
                try:
                    last_dt = datetime.fromisoformat(last)
                    hours_ago = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600.0
                except Exception:
                    pass
            # Wave 57 stale-OFF override: if OFF and last change >24h ago,
            # default to ON. Recent OFF (within 24h) respected as manual.
            if (not scanner_on) and hours_ago > 24.0:
                log.info(
                    f"Wave 57: scanner_state stale ({hours_ago:.1f}h old), "
                    f"defaulting to ON for autonomous operation"
                )
                scanner_on = True
            return {
                "scanner_on":   scanner_on,
                "last_changed": last,
                "hours_ago":    round(hours_ago, 1),
            }
        else:
            # Wave 57: no state file = fresh boot. Default ON.
            log.info("Wave 57: no scanner_state.json, defaulting to ON")
            return {"scanner_on": True, "last_changed": "", "hours_ago": 0.0}
    except Exception as e:
        log.warning(f"_load_scanner_state: {e}")
    # Wave 57: on error, also default ON (safer than leaving off forever)
    return {"scanner_on": True, "last_changed": "", "hours_ago": 0.0}

ALL_MARKETS  = get_all_markets()
YF_MAP       = {"NQ": "NQ=F", "GC": "GC=F"}
CRYPTO_MAP   = {"BTC": "BTC/USDT", "SOL": "SOL/USDT"}
MARKET_NAMES = {"NQ":"NQ Futures (Nasdaq 100)","GC":"Gold Futures","BTC":"Bitcoin","SOL":"Solana"}
CYCLE_CONV   = [50,60,65,70,80]
CYCLE_RR     = [1.5,2.0,2.5,3.0]
CYCLE_INT    = [1,3,5,10,15]
CYCLE_CD     = [15,30,60,120]
CYCLE_RISK   = [0.5,1.0,1.5,2.0,3.0]

def _cycle(cur, opts):
    try:    i = opts.index(cur)
    except: i = -1
    return opts[(i+1) % len(opts)]

# ── Persistent cooldowns ──────────────────────────────────────────
COOLDOWNS: dict = {}
COOLDOWN_FILE = os.path.join(BASE_DIR, "data", "cooldowns.json")

def _load_cooldowns():
    """
    Load persistent cooldowns from disk. Wave 8 (May 3): auto-prune any
    cooldowns older than 24 hours so the file doesn't accumulate stale
    Apr-13/14/15 entries forever. Cooldowns themselves are scoped to
    minutes (typically 60-120) so anything 24h+ old is definitely expired.
    """
    global COOLDOWNS
    try:
        if os.path.exists(COOLDOWN_FILE):
            with open(COOLDOWN_FILE) as f:
                raw = json.load(f)
            now = datetime.now(timezone.utc)
            prune_before = now - timedelta(hours=24)
            COOLDOWNS = {}
            n_pruned = 0
            for k, v in raw.items():
                try:
                    ts = datetime.fromisoformat(v)
                    if ts < prune_before:
                        n_pruned += 1
                        continue
                    COOLDOWNS[tuple(k.split("|", 1))] = ts
                except Exception:
                    continue
            log.info(f"Loaded {len(COOLDOWNS)} cooldowns from disk "
                     f"(pruned {n_pruned} older than 24h)")
            if n_pruned > 0:
                _save_cooldowns()  # Persist the pruned set
    except Exception as e:
        log.warning(f"_load_cooldowns: {e}")
        COOLDOWNS = {}

def _save_cooldowns():
    try:
        os.makedirs(os.path.dirname(COOLDOWN_FILE), exist_ok=True)
        # Wave 48 (May 12, 2026): atomic write.
        safe_io.atomic_write_json(
            COOLDOWN_FILE,
            {f"{k[0]}|{k[1]}": v.isoformat() for k, v in COOLDOWNS.items()},
        )
    except Exception as e:
        log.warning(f"_save_cooldowns: {e}")

def _cooldown_ok(market: str, setup_type: str) -> bool:
    key  = (market, setup_type)
    last = COOLDOWNS.get(key)
    if last is None:
        return True
    try:
        cfg   = get_market_config(market)
        cd_min = getattr(cfg, "COOLDOWN_MIN", SETTINGS["cooldown_min"])
    except Exception:
        cd_min = SETTINGS["cooldown_min"]
    return datetime.now(timezone.utc) - last >= timedelta(minutes=cd_min)

def _mark_cooldown(market: str, setup_type: str):
    COOLDOWNS[(market, setup_type)] = datetime.now(timezone.utc)
    _save_cooldowns()

# Apr 30 dup-guard: blocks same market+direction+setup within 10 minutes.
# Born of the $300 BTC SHORT BREAK_RETEST_BEAR loss (2026-04-30 02:42 + 02:44 ET):
# two near-identical alerts fired 2 minutes apart, both lost. The shadow-only
# cooldown system never blocked them. This is a HARD guard — even if the
# cooldown is shadow-logged, this prevents the actual fire.
#
# May 1 broadening (after the BTC SHORT $210 loss today):
# the original guard was keyed on (market, setup_type, direction). When BTC
# fired BREAK_RETEST_BEAR + APPROACH_RESIST 2 minutes apart, both shorts hit
# stop. The setup_type was different so the dup-guard didn't fire. We now
# layer a SECOND, broader guard: (market, direction) for 30 min, independent
# of setup_type. Same direction, same market, within 30 min = blocked, period.
_RECENT_FIRES: dict = {}              # (market, setup_type, direction) -> datetime UTC (10 min)
_RECENT_DIRECTION_FIRES: dict = {}    # (market, direction) -> datetime UTC (30 min)
_RECENT_FIRE_WINDOW_MIN = 10
_RECENT_DIRECTION_WINDOW_MIN = 30

def _recent_fire_blocked(market: str, setup_type: str, direction: str) -> bool:
    """True if this exact alert (market+setup+direction) fired in last 10 min."""
    key = (market, setup_type, direction)
    last = _RECENT_FIRES.get(key)
    if last is None:
        return False
    return datetime.now(timezone.utc) - last < timedelta(minutes=_RECENT_FIRE_WINDOW_MIN)

def _recent_direction_blocked(market: str, direction: str) -> bool:
    """True if same market+direction (any setup) fired in last 30 min. May 1 broadening."""
    key = (market, direction)
    last = _RECENT_DIRECTION_FIRES.get(key)
    if last is None:
        return False
    return datetime.now(timezone.utc) - last < timedelta(minutes=_RECENT_DIRECTION_WINDOW_MIN)

def _mark_recent_fire(market: str, setup_type: str, direction: str):
    _RECENT_FIRES[(market, setup_type, direction)] = datetime.now(timezone.utc)
    _RECENT_DIRECTION_FIRES[(market, direction)] = datetime.now(timezone.utc)

# ── Active setup deduplication ────────────────────────────────────
ACTIVE_FILE = os.path.join(BASE_DIR, "data", "active_setups.json")

def _load_active():
    try:
        if os.path.exists(ACTIVE_FILE):
            with open(ACTIVE_FILE) as f: return json.load(f)
    except: pass
    return {}

def _save_active(d):
    try:
        # Wave 48 (May 12, 2026): atomic write.
        safe_io.atomic_write_json(ACTIVE_FILE, d)
    except Exception as e: log.warning(f"_save_active: {e}")

def _prune_stale_state_files():
    """
    Wave 8 (May 3): walk family_cooldowns.json and active_setups.json and
    drop entries that are clearly stale. Helps keep state files clean and
    /diag output accurate.

    Rules:
      - family_cooldowns: drop any whose `expiry` is in the past
      - active_setups:    drop any whose `fired_at` is more than 12h ago
                          (matches the existing 8h ignore-window in
                          _is_approach_active, with 4h slop)
    """
    now = datetime.now(timezone.utc)

    # family_cooldowns
    try:
        if os.path.exists(FAMILY_CD_FILE):
            with open(FAMILY_CD_FILE) as f:
                cds = json.load(f)
            keep = {}
            n_dropped = 0
            for key, entry in cds.items():
                try:
                    expiry = datetime.fromisoformat(entry.get("expiry", ""))
                    if expiry > now:
                        keep[key] = entry
                    else:
                        n_dropped += 1
                except Exception:
                    n_dropped += 1
            if n_dropped > 0:
                # Wave 48 (May 12, 2026): atomic write.
                safe_io.atomic_write_json(FAMILY_CD_FILE, keep)
                log.info(f"Wave 8 prune: dropped {n_dropped} expired family cooldowns")
    except Exception as e:
        log.warning(f"_prune_stale_state_files family_cd: {e}")

    # active_setups
    try:
        if os.path.exists(ACTIVE_FILE):
            with open(ACTIVE_FILE) as f:
                d = json.load(f)
            cutoff = now - timedelta(hours=12)
            keep = {}
            n_dropped = 0
            for key, entry in d.items():
                try:
                    fired_at = datetime.fromisoformat(entry.get("fired_at", ""))
                    if fired_at > cutoff:
                        keep[key] = entry
                    else:
                        n_dropped += 1
                except Exception:
                    n_dropped += 1
            if n_dropped > 0:
                # Wave 48 (May 12, 2026): atomic write.
                safe_io.atomic_write_json(ACTIVE_FILE, keep)
                log.info(f"Wave 8 prune: dropped {n_dropped} stale active_setups")
    except Exception as e:
        log.warning(f"_prune_stale_state_files active: {e}")


def _is_approach_active(market, setup_type, entry, tolerance_pct=0.15):
    d   = _load_active()
    key = f"{market}:{setup_type}"
    prev = d.get(key)
    if not prev: return False
    try:
        fired_ts = datetime.fromisoformat(prev["fired_at"])
        if datetime.now(timezone.utc) - fired_ts > timedelta(hours=8):
            return False
        prev_entry = float(prev["entry"])
        if prev_entry == 0: return False
        drift = abs(entry - prev_entry) / prev_entry * 100
        return drift < tolerance_pct
    except: return False

def _mark_approach_active(market, setup_type, entry):
    d = _load_active()
    d[f"{market}:{setup_type}"] = {"entry": float(entry), "fired_at": datetime.now(timezone.utc).isoformat()}
    _save_active(d)

# ── Zone lockout ──────────────────────────────────────────────────
ZONE_LOCKOUT_FILE = os.path.join(BASE_DIR, "data", "zone_lockouts.json")

def _load_zone_lockouts() -> list:
    try:
        if os.path.exists(ZONE_LOCKOUT_FILE):
            with open(ZONE_LOCKOUT_FILE) as f:
                zones = json.load(f)
            now = datetime.now(timezone.utc)
            active = [z for z in zones if datetime.fromisoformat(z["expiry"]) > now]
            if len(active) != len(zones):
                _save_zone_lockouts(active)
            return active
    except Exception as e:
        log.warning(f"_load_zone_lockouts: {e}")
    return []

def _save_zone_lockouts(zones: list):
    try:
        # Wave 48 (May 12, 2026): atomic write.
        safe_io.atomic_write_json(ZONE_LOCKOUT_FILE, zones)
    except Exception as e:
        log.warning(f"_save_zone_lockouts: {e}")

def _add_zone_lockout(market: str, direction: str, entry: float, atr_v: float):
    zones = _load_zone_lockouts()
    half_atr = atr_v * 0.5
    zones.append({
        "market": market,
        "direction": direction,
        "zone_low": round(entry - half_atr, 4),
        "zone_high": round(entry + half_atr, 4),
        "expiry": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
    })
    _save_zone_lockouts(zones)

def _zone_locked(market: str, direction: str, entry_price: float) -> bool:
    zones = _load_zone_lockouts()
    for z in zones:
        if z["market"] == market and z["direction"] == direction:
            if z["zone_low"] <= entry_price <= z["zone_high"]:
                return True
    return False

# ── Setup family cooldowns ────────────────────────────────────────
# Wave 161 (_WAVE161_DIRECTION_GATE): the symmetric, self-correcting side
# gate. Built on the Wave-158 direction finding, upgraded with the full
# archive measurement (Jul 19): direction edge FLIPS with the regime -
# NQ:LONG +0.84R over full history yet -0.41R in the last 40 graded;
# BTC:LONG just recovered to +0.32R; SOL:SHORT just broke to -0.67R. So
# the gate is an EXPECTANCY gate on a rolling recent window, per market
# per side - never a hardcoded anti-long rule. Mechanics: for each
# market:direction, keep the last _W161_WINDOW graded shadows from the
# full maintained history (the same _load_all_strategy_rows the parole
# system trusts); if the window holds >= _W161_MIN_N and its expectancy
# is at/below _W161_GATE_EXP, setups on that side are rejected with a
# clear reason instead of fired. Gated rejects keep logging + grading
# (Wave 140), so the window keeps moving and the gate lifts itself the
# moment the side recovers. Thin/unknown sides are NEVER gated. Cached
# 6h; never raises - any failure disables the gate (fires as before).
_W161_WINDOW = 40
_W161_MIN_N = 30
_W161_GATE_EXP = -0.15
_W161_TTL_SEC = 6 * 3600
_W161_SIDE_LIVE = {}
_w161_state = {"built_at": 0.0}


def _w161_refresh_side_exp(force=False):
    """Rebuild the per market:direction rolling-window expectancy from the
    full strategy-log history if the cache is older than the TTL. Never
    raises - on any failure the map is left as-is (or empty = gate off)."""
    import time as _t
    try:
        now = _t.time()
        if not force and (now - _w161_state.get("built_at", 0.0)) < _W161_TTL_SEC:
            return
        try:
            import strategy_log as _sl
            rows = _sl._load_all_strategy_rows()
        except Exception:
            _w161_state["built_at"] = now  # do not hammer on failure
            return
        seq = {}
        for r in rows:
            try:
                res = (r.get("result") or "").strip()
                if res not in ("WOULD_WIN", "WOULD_LOSE"):
                    continue
                if "LAB|" in (r.get("setup_type") or r.get("setup") or ""):
                    continue
                mk = (r.get("market") or "").strip()
                d = (r.get("direction") or "").strip().replace("WATCH_", "")
                if not mk or d not in ("LONG", "SHORT"):
                    continue
                try:
                    rr = float(r.get("rr") or 0)
                except (TypeError, ValueError):
                    rr = 0.0
                seq.setdefault(mk + ":" + d, []).append(
                    (1 if res == "WOULD_WIN" else 0, rr))
            except Exception:
                continue
        fresh = {}
        for key, arr in seq.items():
            win = arr[-_W161_WINDOW:]
            n = len(win)
            if n < _W161_MIN_N:
                continue
            rrs = [x[1] for x in win if x[1] > 0]
            avg = (sum(rrs) / len(rrs)) if rrs else 0.0
            if avg <= 0:
                continue
            wr = sum(x[0] for x in win) / float(n)
            fresh[key] = round(wr * avg - (1.0 - wr), 3)
        _W161_SIDE_LIVE.clear()
        _W161_SIDE_LIVE.update(fresh)
        _w161_state["built_at"] = now
    except Exception:
        return


def _w161_side_exp(market, direction):
    """Rolling-window expectancy for a market side, or None when the data
    is thin/unknown (None = the gate stays open). Never raises."""
    try:
        _w161_refresh_side_exp()
        key = "%s:%s" % (str(market), str(direction or "").replace("WATCH_", ""))
        return _W161_SIDE_LIVE.get(key)
    except Exception:
        return None


# -- Setup family cooldowns (comment above dict retained below) --
SETUP_FAMILIES = {
    "mean_rev_long":  {"VWAP_BOUNCE_BULL", "APPROACH_SUPPORT", "LIQ_SWEEP_BULL", "RSI_DIV_BULL",
                       "FAILED_BREAKDOWN_BULL"},
    "mean_rev_short": {"VWAP_REJECT_BEAR", "APPROACH_RESIST", "LIQ_SWEEP_BEAR", "RSI_DIV_BEAR",
                       "FAILED_BREAKOUT_BEAR"},
    "cont_long":      {"EMA21_PULLBACK_BULL", "EMA50_RECLAIM", "BREAK_RETEST_BULL",
                       "VOLATILITY_CONTRACTION_BREAKOUT"},
    "cont_short":     {"EMA21_PULLBACK_BEAR", "EMA50_BREAKDOWN", "BREAK_RETEST_BEAR"},
}
FAMILY_CD_FILE = os.path.join(BASE_DIR, "data", "family_cooldowns.json")

def _get_family(setup_type: str) -> str:
    for fam, members in SETUP_FAMILIES.items():
        if setup_type in members:
            return fam
    return ""

def _family_cooldown_ok(market: str, setup_type: str) -> bool:
    # Wave 7 Layer 4: priority setups (e.g. VWAP_BOUNCE_BULL) bypass family cooldown.
    # The 10-min same-setup dup-guard still applies, just not the cross-setup family lock.
    try:
        if cb.can_bypass_family_cooldown(setup_type):
            return True
    except Exception:
        pass
    fam = _get_family(setup_type)
    if not fam:
        return True
    try:
        if os.path.exists(FAMILY_CD_FILE):
            with open(FAMILY_CD_FILE) as f:
                cds = json.load(f)
            key = f"{market}:{fam}"
            entry = cds.get(key)
            if entry:
                expiry = datetime.fromisoformat(entry["expiry"])
                if datetime.now(timezone.utc) < expiry:
                    return False
    except Exception:
        pass
    return True

def _set_family_cooldown(market: str, setup_type: str, result: str):
    fam = _get_family(setup_type)
    if not fam:
        return
    cd_min = 90 if result == "LOSS" else 30
    try:
        cds = {}
        if os.path.exists(FAMILY_CD_FILE):
            with open(FAMILY_CD_FILE) as f:
                cds = json.load(f)
        cds[f"{market}:{fam}"] = {
            "expiry": (datetime.now(timezone.utc) + timedelta(minutes=cd_min)).isoformat(),
            "reason": result,
        }
        # Wave 48 (May 12, 2026): atomic write.
        safe_io.atomic_write_json(FAMILY_CD_FILE, cds)
    except Exception:
        pass

# ── Consecutive loss session halt ─────────────────────────────────
CONSECUTIVE_LOSSES: dict = {}
MARKET_HALTED: dict = {}
CORRELATION_LOCKOUT: dict = {}

# ── Topstep eval daily gates (Task 8) ────────────────────────────
# Pre-Batch 2026-04-20: This gate no longer blocks trades. It is retained
# to support shadow logging (DECISION_SHADOW_HALTED) and the daily recap.
DAILY_LOSS_GATE = False       # True after 2 consecutive session losses (counter only — no blocking)
DAILY_PROFIT_LOCKED = False   # True after +$150 session P&L
DAILY_TRADE_COUNT = 0         # Incremented on each fired alert
MAX_DAILY_TRADES = 3
PROFIT_LOCK_THRESHOLD = 150.0

def _on_session_close(event, now_et):
    """
    FUTURES_SESSION_CLOSE (4 PM ET) handler.
    Clears halts for NQ/GC, resets sim, updates suspensions, queues Telegram summary.
    """
    for m in ("NQ", "GC"):
        MARKET_HALTED.pop(m, None)
        CONSECUTIVE_LOSSES.pop(m, None)

    # Build session summary BEFORE resetting sim
    global _SESSION_CLOSE_SUMMARY
    try:
        sid = get_session_date()  # still returns the closing session at this point
        summary = ot.build_session_summary(sid)
        st = sim.load_state()
        risk = sim.check_risk_limits(st)
        _SESSION_CLOSE_SUMMARY = {
            "sid": sid,
            "summary": summary,
            "sim_pnl": risk["daily_pnl"],
            "sim_balance": risk["balance"],
        }
    except Exception as e:
        log.error(f"Session close summary build: {e}")
        _SESSION_CLOSE_SUMMARY = None

    # Update setup suspensions
    global _SUSPENSION_CHANGES
    try:
        changes = ot.check_and_update_suspensions()
        _SUSPENSION_CHANGES = changes
        if changes:
            log.info(f"Suspension changes at session close: {changes}")
    except Exception as e:
        log.error(f"Suspension check at session close: {e}")
        _SUSPENSION_CHANGES = []

    # Reset sim
    try:
        sim.on_session_close()
        log.info("Sim session reset at futures close")
    except Exception as e:
        log.error(f"Sim session close reset: {e}")

    log.info("Futures session close: halts cleared for NQ/GC")

_SESSION_CLOSE_SUMMARY = None
_SUSPENSION_CHANGES = []


def _on_crypto_day(event, now_et):
    """CRYPTO_DAY_BOUNDARY (4 PM ET) handler. Clears halts for BTC/SOL."""
    for m in ("BTC", "SOL"):
        MARKET_HALTED.pop(m, None)
        CONSECUTIVE_LOSSES.pop(m, None)
    log.info("Crypto day boundary: halts cleared for BTC/SOL")

def _record_loss(market: str):
    global DAILY_LOSS_GATE
    CONSECUTIVE_LOSSES[market] = CONSECUTIVE_LOSSES.get(market, 0) + 1
    if market == "BTC":
        CORRELATION_LOCKOUT["SOL"] = datetime.now(timezone.utc) + timedelta(minutes=30)
    elif market == "SOL":
        CORRELATION_LOCKOUT["BTC"] = datetime.now(timezone.utc) + timedelta(minutes=30)
    # Task 8: Check for 2 consecutive session losses (across all markets)
    total_consec = sum(CONSECUTIVE_LOSSES.get(m, 0) for m in ("NQ", "GC", "BTC", "SOL"))
    if total_consec >= 2 and not DAILY_LOSS_GATE:
        DAILY_LOSS_GATE = True
        log.warning("DAILY_LOSS_GATE activated: 2 consecutive losses this session")

def _record_win(market: str):
    CONSECUTIVE_LOSSES[market] = 0

def _is_halted(market: str) -> bool:
    # Pre-Batch 2026-04-20: always False. Halts are disabled.
    # MARKET_HALTED dict is still populated by _record_loss for shadow logging
    # and the daily recap; this function returns False so no gate blocks.
    return False

def _is_correlation_locked(market: str) -> bool:
    expiry = CORRELATION_LOCKOUT.get(market)
    if expiry and datetime.now(timezone.utc) < expiry:
        return True
    return False

def get_frames(market):
    return dl_get_frames(market)

# ── Telegram ──────────────────────────────────────────────────────
async def tg_send(app, text):
    # Wave 90 (M1 fix): Telegram hard limit is 4096 chars. Oversized messages
    # used to fail with "message is too long" (not a parse error), retry the
    # same oversized text 3x, and get silently dropped. Now: split at line
    # boundaries (hard-slice any single monster line) and send the parts.
    if text and len(text) > 4000:
        try:
            parts, cur = [], ""
            for ln in str(text).split("\n"):
                while len(ln) > 4000:
                    if cur:
                        parts.append(cur); cur = ""
                    parts.append(ln[:4000]); ln = ln[4000:]
                if len(cur) + len(ln) + 1 > 4000:
                    parts.append(cur); cur = ln
                else:
                    cur = cur + "\n" + ln if cur else ln
            if cur:
                parts.append(cur)
            for p in parts:
                await tg_send(app, p)
            return
        except Exception as e:
            log.warning(f"W90 long-message split failed, sending as-is: {e}")
    for attempt in range(3):
        try:
            mode = "Markdown" if attempt < 2 else None
            await app.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=mode)
            log.info("Sent.")
            return
        except Exception as e:
            if "parse" in str(e).lower() or "entities" in str(e).lower():
                log.warning(f"tg_send Markdown error, retrying as plain text: {e}")
                try:
                    clean = text.replace("*","").replace("`","").replace("_","")
                    await app.bot.send_message(chat_id=CHAT_ID, text=clean, parse_mode=None)
                    log.info("Sent (plain text fallback).")
                    return
                except Exception as e2:
                    log.error(f"tg_send plain text fallback failed: {e2}")
                    return
            wait = 2 ** attempt
            log.warning(f"tg_send attempt {attempt+1} failed: {e} — retry in {wait}s")
            await asyncio.sleep(wait)
    log.error("tg_send: all 3 attempts failed, dropping message")

def _md(text):
    if not isinstance(text, str):
        text = str(text)
    text = text.replace("_", " ")
    for ch in ("*", "`", "["):
        text = text.replace(ch, "")
    return text

# ---- Wave 91 (Jul 5, 2026): M2+M3+M4 - every reply_text made entity-safe ----
# The Jul 5 message audit found ~114 reply_text sites (menu commands, buttons)
# sending parse_mode="Markdown" with NO fallback: one Markdown-breaking char in
# dynamic content and the button silently fails. Instead of editing 114 sites,
# wrap the ONE choke point: telegram.Message.reply_text itself. On a parse /
# entity / too-long error the reply is resent as sanitized plain text (M2),
# clamped under the 4096 limit (M3), with "[" stripped too (M4). Any other
# error re-raises unchanged - old behavior preserved. Alerts already use
# tg_send (Wave 90 splitter); this covers the interactive surface.
try:
    from telegram import Message as _W91Message
    _w91_orig_reply_text = _W91Message.reply_text

    async def _w91_safe_reply_text(self, text, *args, **kwargs):
        try:
            return await _w91_orig_reply_text(self, text, *args, **kwargs)
        except Exception as e:
            _msg = str(e).lower()
            if ("parse" in _msg) or ("entities" in _msg) or ("too long" in _msg):
                try:
                    clean = str(text)
                    for ch in ("*", "`", "["):
                        clean = clean.replace(ch, "")
                    clean = clean.replace("_", " ")
                    if len(clean) > 4000:
                        clean = clean[:3990] + "\n[...]"
                    kwargs.pop("parse_mode", None)
                    log.warning(f"W91 safe-reply fallback engaged: {e}")
                    return await _w91_orig_reply_text(self, clean, *args,
                                                      parse_mode=None, **kwargs)
                except Exception as e2:
                    log.error(f"W91 safe-reply fallback failed: {e2}")
                    raise
            raise

    _W91Message.reply_text = _w91_safe_reply_text
    log.info("W91: reply_text choke-point guard installed")
except Exception as _w91e:
    log.warning(f"W91 reply guard not installed (non-fatal): {_w91e}")

# ── Alert formatter ───────────────────────────────────────────────
def format_alert(market, tf, setup, conv, tier, trend, target, rr, method,
                 adx_v, rsi_v, lev=None, risk_at_stop=None, hold=None,
                 extra_footer="", alert_id="",
                 regime="UNKNOWN", htf_bias="UNKNOWN"):
    cfg       = get_market_config(market)
    is_watch  = "WATCH" in setup.get("direction","")
    direction = setup["direction"]
    is_long   = "LONG" in direction
    arrow     = "LONG" if is_long else "SHORT"
    dir_icon  = "📈" if is_long else "📉"

    # ── Header: ENTER NOW for confirmed, HEADS UP for watch ───────
    if is_watch:
        header = "👀 *HEADS UP — Setup Forming*"
        arrow  = "WATCH " + arrow
    else:
        enter_emoji = "🟢" if is_long else "🔴"
        header = f"{enter_emoji} *ENTER NOW — {market} {'LONG' if is_long else 'SHORT'}*"

    nw  = "\n⚠️ *HIGH IMPACT NEWS — Extra caution!*" if ot.in_news_window() else ""
    te  = {"HIGH":"🔥","MEDIUM":"✅","LOW":"⚡"}.get(tier,"")
    safe_method = _md(method)

    # Wave 8 (May 3): show Wave 7 Iron Robot adjustment when active so Wayne
    # can SEE that the boost is firing. Only displayed when applied_layers is
    # non-empty — silent for setups that don't get a W7 adjustment.
    w7_line = ""
    try:
        w7 = setup.get("_w7_breakdown") or {}
        applied = w7.get("applied_layers") or []
        if applied:
            base = int(w7.get("base", conv))
            delta = int(w7.get("setup_boost", 0)) + int(w7.get("market_mult", 0))
            arrow_w7 = "🟢" if delta > 0 else ("🔴" if delta < 0 else "⚪")
            sign = "+" if delta >= 0 else ""
            w7_line = f"{arrow_w7} *W7:* {base} → {conv}/100 ({sign}{delta})  `{', '.join(applied)}`\n"
    except Exception:
        pass

    # Wave 79: The Read - one-glance quality read (grade + confluence + hour edge)
    read_line = ""
    try:
        rd = setup.get("_read") or {}
        if rd:
            _rcs = int(rd.get("cscore", 0)); _rce = float(rd.get("cexp", 0.0))
            _grade = "A+" if _rcs >= 85 else "A" if _rcs >= 70 else "B" if _rcs >= 50 else "C"
            _rht = ""
            if "tlabel" in rd:
                _rht = f"  |  *{rd.get('tlabel')} hour* ({float(rd.get('texp', 0.0)):+.2f}R)"
            # Wave 158 (_WAVE158_DIRECTION_READ): show the current side edge
            _rhd = ""
            if "dlabel" in rd:
                _rhd = f"  |  *side {rd.get('dlabel')}* ({float(rd.get('dexp', 0.0)):+.2f}R)"
            read_line = f"\U0001f9e0 *Read:* {_grade}  |  Confluence {_rcs}/100 ({_rce:+.2f}R){_rht}{_rhd}\n"
    except Exception:
        pass

    msg = (
        f"{header}{nw}\n"
        f"{cfg.EMOJI} {dir_icon} {arrow}  |  *{_md(cfg.FULL_NAME)}*  |  [{tf}]\n"
        f"{te} Tier: *{tier}*  |  Conviction: *{conv}/100*\n"
        f"{w7_line}"
        f"{read_line}"
        f"🔭 Trend: `{trend:+d}`  |  ADX: `{round(adx_v,1)}`  |  RSI: `{round(rsi_v,1)}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📍 *Entry:*  `{round(setup['entry'],4)}`\n"
        f"🛑 *Stop:*   `{round(setup['raw_stop'],4)}`  ← place immediately\n"
        f"🎯 *Target:* `{round(target,4)}` ({safe_method}, {round(rr,2)}R)\n"
    )

    # Wave 5: leverage and hold moved BELOW size so the trading triangle
    # (entry / stop / target / size) is visually grouped at top.
    # ── Contract size — capped at 5 MNQ or 1 NQ ──────────────────
    size_line = ""
    if market in ("BTC", "SOL"):
        if lev is not None:
            size_line = f"📦 *Size:* {lev}x leverage"
    else:
        sim_state = sim.load_state()
        use_mnq   = sim_state.get("use_mnq", False)
        label     = "MNQ" if use_mnq else ("NQ" if market == "NQ" else "GC")

        if sim_state.get("enabled"):
            c_info = sim.suggest_contracts(market, tier, setup["entry"], setup["raw_stop"], sim_state)
            if isinstance(c_info, dict) and c_info.get("contracts"):
                raw_contracts = c_info["contracts"]
                # Hard cap: 5 MNQ or 1 full NQ/GC
                if use_mnq:
                    contracts = min(raw_contracts, 5)
                else:
                    contracts = 1
                size_line = f"📦 *Size:* {contracts} {label}"

        if not size_line:
            # Fallback estimate — caps enforced
            if use_mnq:
                tier_sizes = {"HIGH": 5, "MEDIUM": 3, "LOW": 1}
                est = tier_sizes.get(tier, 1)
            else:
                est = 1
            size_line = f"📦 *Size:* {est} {label}"

    if size_line:
        msg += f"{size_line}\n"

    # Wave 5: leverage + hold moved here (below size, above chart read)
    if lev is not None:
        msg += f"📊 *Leverage:* `{lev}x`  (risk: {risk_at_stop}%)\n"
    if hold:
        msg += f"⏱ *Hold:* {_md(hold)}\n"

    msg += f"━━━━━━━━━━━━━━━━━━\n📋 *Chart Read:*\n{_md(setup['detail'])}\n━━━━━━━━━━━━━━━━━━\n"
    # Wave 137 (_WAVE137_WHY_LINE): confluence context - which independent
    # reads (Wave-134 expanded tape) line up with THIS trade right now, plus
    # the news-window flag. Additive-only per Wayne's law: informs, never
    # blocks. get_tape serves the warm snapshot cache (no network in the
    # alert path); fully wrapped so it can never break an alert.
    try:
        import intermarket as _im137
        _t137 = _im137.get_tape(market) or {}
        _tape137 = _t137.get("tape")
        if _tape137 and _tape137 != "UNKNOWN":
            _det137 = {k: v for k, v in (_t137.get("detail") or {}).items() if v is not None}
            if is_long:
                _al137 = [k for k, v in _det137.items() if v >= 0.5]
            else:
                _al137 = [k for k, v in _det137.items() if v <= -0.5]
            _who137 = (" - " + ", ".join(_al137[:4])) if _al137 else ""
            _news137 = "  |  \U0001F4C5 news window" if ot.in_news_window() else ""
            msg += (f"\U0001F9ED *Why:* tape {_tape137} - {len(_al137)}/{len(_det137)} "
                    f"reads with this trade{_who137}{_news137}\n"
                    + "\u2501" * 18 + "\n")
    except Exception as _we137:
        log.debug(f"[{market}] why-line skipped: {_we137}")

    if extra_footer:
        msg += f"{_md(extra_footer)}\n━━━━━━━━━━━━━━━━━━\n"
    sb = sim.format_sim_block(market, tier, setup["entry"], setup["raw_stop"], target, alert_id,
                                       conviction=conv, regime=setup.get("regime","UNKNOWN"), setup_name=setup.get("type","UNKNOWN"))
    if sb:
        msg += f"{sb}\n━━━━━━━━━━━━━━━━━━\n"
    if market in ("BTC", "SOL"):
        try:
            crypto_context = {
                "suggested_lev": lev,  # Wave 109 (_WAVE109_LEVERAGE_AUTOAPPLY): bot-chosen per-trade leverage; sim caps at /leverage
                "trend_score": trend,
                "rsi": round(rsi_v, 2),
                "adx": round(adx_v, 2),
                "htf_bias": setup.get("htf_bias", "UNKNOWN"),
                "regime": setup.get("regime", "UNKNOWN"),
                "session": get_market_config(market).get_session_context().get("session", ""),
                "news_flag": ot.in_news_window(),
                "chart_read": setup.get("detail", ""),
            }
            crypto_block = crypto_sim.format_crypto_sim_block(
                market, tier, setup["entry"], setup["raw_stop"], target,
                alert_id, conv, crypto_context,
            )
            if crypto_block:
                msg += f"{crypto_block}\n━━━━━━━━━━━━━━━━━━\n"
        except Exception as _ce:
            log.warning(f"[{market}] crypto_sim block: {_ce}")
    msg += "⚠️ Not financial advice. Manage your risk."
    return msg

# ── Pre-Batch 2026-04-20: Sampled REJECTED log helper ────────────
def _sample_reject_log(market: str, entry_tf: str, setup_type: str, reason: str, rate: float = 0.1):
    """
    Emit a Railway-visible REJECTED log line at `rate` probability (default 10%).
    Full rejection detail is still in strategy_log.csv via sl.log_scan_decision —
    this is just for log scrollback visibility without spamming.
    """
    try:
        if _rnd.random() < rate:
            r = str(reason)[:80] if reason else "?"
            log.info(f"[{market}] [{entry_tf}] REJECTED_SAMPLE: {setup_type} reason={r}")
    except Exception:
        pass


# ── Batch 2A: Detection reason + confidence factor builders ──────
def _build_detection_reason(stp: dict, snapshot: dict, adx_v: float,
                             rsi_v: float, vol_ratio: float) -> str:
    """
    Build a rich human-readable sentence explaining why this setup was detected.
    Uses the setup's 'detail' field plus indicator context.
    """
    setup_type = stp.get("type", "UNKNOWN")
    base = stp.get("detail", "")

    # Indicator context phrase
    bb_pos = ""
    bb_u = snapshot.get("bb_upper", 0)
    bb_l = snapshot.get("bb_lower", 0)
    close = snapshot.get("close_price", 0)
    if bb_u and bb_l and bb_u > bb_l and close:
        pct = (close - bb_l) / (bb_u - bb_l)
        if pct <= 0.2:
            bb_pos = "price in lower 20% of Bollinger range"
        elif pct >= 0.8:
            bb_pos = "price in upper 20% of Bollinger range"
        elif 0.4 <= pct <= 0.6:
            bb_pos = "price at Bollinger middle"

    stoch_phrase = ""
    sk = snapshot.get("stoch_k", 50)
    sd = snapshot.get("stoch_d", 50)
    if sk <= 20 and sk > sd:
        stoch_phrase = f"Stoch oversold turning up ({sk:.0f}>{sd:.0f})"
    elif sk >= 80 and sk < sd:
        stoch_phrase = f"Stoch overbought turning down ({sk:.0f}<{sd:.0f})"
    elif sk <= 20:
        stoch_phrase = f"Stoch oversold ({sk:.0f})"
    elif sk >= 80:
        stoch_phrase = f"Stoch overbought ({sk:.0f})"

    macd_phrase = ""
    ml = snapshot.get("macd_line", 0)
    ms = snapshot.get("macd_signal", 0)
    mh = snapshot.get("macd_hist", 0)
    if ml > ms and mh > 0:
        macd_phrase = "MACD bullish (line>signal, hist positive)"
    elif ml < ms and mh < 0:
        macd_phrase = "MACD bearish (line<signal, hist negative)"

    context_parts = []
    if bb_pos:       context_parts.append(bb_pos)
    if stoch_phrase: context_parts.append(stoch_phrase)
    if macd_phrase:  context_parts.append(macd_phrase)
    if vol_ratio:    context_parts.append(f"volume {vol_ratio:.1f}x avg")
    if adx_v:        context_parts.append(f"ADX {adx_v:.1f}")
    if rsi_v:        context_parts.append(f"RSI {rsi_v:.1f}")

    ctx_str = " | ".join(context_parts) if context_parts else ""
    if base and ctx_str:
        return f"{base} [Context: {ctx_str}]"
    elif base:
        return base
    elif ctx_str:
        return f"{setup_type} detected. [Context: {ctx_str}]"
    else:
        return f"{setup_type} detected."


def _build_confidence_factors(snapshot: dict, trend: int, adx_v: float,
                                rsi_v: float) -> dict:
    """
    Returns a dict of qualitative flags useful for later analysis.
    Separate from score_breakdown — this is qualitative, that's quantitative.
    """
    factors = {}
    close = snapshot.get("close_price", 0)
    bb_u = snapshot.get("bb_upper", 0)
    bb_l = snapshot.get("bb_lower", 0)
    if bb_u and bb_l and close and bb_u > bb_l:
        pct = (close - bb_l) / (bb_u - bb_l)
        if pct <= 0.2:   factors["bb_position"] = "near_lower"
        elif pct >= 0.8: factors["bb_position"] = "near_upper"
        elif pct >= 0.4 and pct <= 0.6: factors["bb_position"] = "middle"
        else: factors["bb_position"] = "intermediate"

    sk = snapshot.get("stoch_k", 50)
    sd = snapshot.get("stoch_d", 50)
    if sk <= 20:
        factors["stoch_signal"] = "oversold_cross_up" if sk > sd else "oversold"
    elif sk >= 80:
        factors["stoch_signal"] = "overbought_cross_down" if sk < sd else "overbought"
    else:
        factors["stoch_signal"] = "neutral"

    ml = snapshot.get("macd_line", 0)
    ms = snapshot.get("macd_signal", 0)
    mh = snapshot.get("macd_hist", 0)
    if ml > ms and mh > 0:
        factors["macd_signal"] = "bullish"
    elif ml < ms and mh < 0:
        factors["macd_signal"] = "bearish"
    else:
        factors["macd_signal"] = "transitioning"

    factors["trend_strength"] = "strong_bull" if trend >= 5 else "bull" if trend >= 2 else "bear" if trend <= -2 else "strong_bear" if trend <= -5 else "neutral"
    factors["adx_regime"] = "trending" if adx_v >= 25 else "weak_trend" if adx_v >= 18 else "choppy"
    factors["rsi_zone"] = "overbought" if rsi_v >= 70 else "oversold" if rsi_v <= 30 else "neutral_upper" if rsi_v >= 55 else "neutral_lower" if rsi_v <= 45 else "neutral"

    return factors


# ── Scan one market ───────────────────────────────────────────────
# Wave 76 (Jun 29, 2026): Confluence scoring engine, backfilled from history.
# Trend-aligned per-setup and per-pair expectancy (R) from the full strategy_log.
# Rates a live aligned stack by what has actually won TOGETHER. Shadow only in
# W76 (score logged next to each stack); a later wave lets it drive sizing.
#
# Wave 159 (_WAVE159_READ_REFRESH): the W76 seed below was backfilled Jun 29
# and drifts as the market changes - measured Jul 19, 7 of 17 setups had
# FLIPPED SIGN on solid samples, so the read was calling winners losers. Keep
# the read honest: recompute each setup's trend-aligned expectancy from the
# full maintained history (_load_all_strategy_rows, the same store the parole
# system trusts), cached 6h, prefer the fresh value when n>=15 else fall back
# to the seed. Display only, exactly like W76. Never raises.
_W159_TTL_SEC = 6 * 3600
_W159_MIN_SAMPLE = 15
_W159_EDGE_LIVE = {}
_w159_state = {"built_at": 0.0}


def _w159_aligned(direction, trend_raw, htf_bias):
    """True when a setup was trend-aligned - LONG in an up context, SHORT in
    a down context - matching how W76 was backfilled."""
    try:
        d = str(direction or "").replace("WATCH_", "")
        try:
            tr = float(trend_raw)
        except (TypeError, ValueError):
            tr = 0.0
        hb = str(htf_bias or "")
        if d == "LONG":
            return tr > 0 or hb == "HH_HL"
        if d == "SHORT":
            return tr < 0 or hb == "LH_LL"
        return False
    except Exception:
        return False


def _w159_refresh_setup_edges(force=False):
    """Rebuild _W159_EDGE_LIVE from the full strategy-log history if the cache
    is older than the TTL. Cheap after the first build (guarded by timestamp).
    Never raises - on any failure the live map is left as-is and the read
    falls back to the W76 seed."""
    import time as _t
    try:
        now = _t.time()
        if not force and (now - _w159_state.get("built_at", 0.0)) < _W159_TTL_SEC:
            return
        try:
            import strategy_log as _sl
            rows = _sl._load_all_strategy_rows()
        except Exception:
            _w159_state["built_at"] = now  # do not hammer on failure
            return
        agg = {}
        for r in rows:
            try:
                res = (r.get("result") or "").strip()
                if res not in ("WOULD_WIN", "WOULD_LOSE"):
                    continue
                st = (r.get("setup_type") or "").strip()
                if not st or "LAB|" in st:
                    continue
                if not _w159_aligned(r.get("direction"), r.get("trend"),
                                     r.get("htf_bias")):
                    continue
                a = agg.setdefault(st, {"w": 0, "l": 0, "rr": 0.0, "rrn": 0})
                if res == "WOULD_WIN":
                    a["w"] += 1
                else:
                    a["l"] += 1
                try:
                    rr = float(r.get("rr") or 0)
                    if rr > 0:
                        a["rr"] += rr
                        a["rrn"] += 1
                except (TypeError, ValueError):
                    pass
            except Exception:
                continue
        fresh = {}
        for st, a in agg.items():
            n = a["w"] + a["l"]
            if n < _W159_MIN_SAMPLE:
                continue
            arr = (a["rr"] / a["rrn"]) if a["rrn"] else 0.0
            if arr <= 0:
                continue
            wr = a["w"] / n
            fresh[st] = round(wr * arr - (1.0 - wr), 3)
        _W159_EDGE_LIVE.clear()
        _W159_EDGE_LIVE.update(fresh)
        _w159_state["built_at"] = now
    except Exception:
        return


def _w159_edge(setup):
    """The freshest edge for a setup: the live recompute when we have a solid
    sample, else the Wave-76 seed, else None (unknown setup)."""
    try:
        if setup in _W159_EDGE_LIVE:
            return _W159_EDGE_LIVE[setup]
    except Exception:
        pass
    return _W76_SETUP_EDGE.get(setup)


_W76_SETUP_EDGE = {
    "APPROACH_RESIST": 1.263, "BB_REVERSION_BULL": 1.036, "BREAK_RETEST_BULL": 0.828,
    "VWAP_BOUNCE_BULL": 0.795, "STOCH_REVERSAL_BEAR": 0.685, "RSI_DIV_BEAR": 0.178,
    "MACD_CROSS_BEAR": 0.126, "BB_REVERSION_BEAR": 0.064, "VWAP_REJECT_BEAR": -0.022,
    "RSI_DIV_BULL": -0.096, "BREAK_RETEST_BEAR": -0.694, "STOCH_REVERSAL_BULL": -0.712,
    "EMA21_PULLBACK_BEAR": -0.722, "EMA50_RECLAIM": -0.724, "EMA21_PULLBACK_BULL": -0.779,
    "MACD_CROSS_BULL": -0.905, "EMA50_BREAKDOWN": -0.952,
}
_W76_PAIR_EDGE = {
    "BB_REVERSION_BULL|VWAP_BOUNCE_BULL": 2.017,
    "BREAK_RETEST_BULL|VWAP_BOUNCE_BULL": 1.650,
    "EMA21_PULLBACK_BEAR|VWAP_BOUNCE_BULL": -0.899,
    "RSI_DIV_BULL|VWAP_BOUNCE_BULL": -0.917,
    "EMA21_PULLBACK_BEAR|RSI_DIV_BULL": -0.945,
    "MACD_CROSS_BULL|VWAP_BOUNCE_BULL": -1.0,
    "EMA21_PULLBACK_BEAR|VWAP_REJECT_BEAR": -1.0,
    "RSI_DIV_BULL|VWAP_REJECT_BEAR": -1.0,
}

def _confluence_score(stack_types, market=""):
    """Wave 76: rate an aligned stack by backfilled expectancy.
    Returns (score_0_100, expected_R, basis). A known pair is the most specific
    evidence and overrides; a BAD pair (conflict) dominates a good one. Else the
    strongest known setup leads, dragged toward the stack mean."""
    try:
        _w159_refresh_setup_edges()  # Wave 159: keep the read fresh (cached)
        st = [s for s in (stack_types or []) if s]
        edges = [_w159_edge(s) for s in st if _w159_edge(s) is not None]
        pairs = []
        for i in range(len(st)):
            for j in range(i + 1, len(st)):
                key = "|".join(sorted([st[i], st[j]]))
                if key in _W76_PAIR_EDGE:
                    pairs.append(_W76_PAIR_EDGE[key])
        if pairs:
            worst, best = min(pairs), max(pairs)
            exp_r = worst if worst <= -0.5 else best
            basis = "pair"
        elif edges:
            mx = max(edges); mean = sum(edges) / len(edges)
            exp_r = 0.7 * mx + 0.3 * mean
            basis = "setup"
        else:
            exp_r = 0.0; basis = "unknown"
        score = int(max(0, min(100, round((exp_r + 1.0) / 3.0 * 100))))
        return score, round(float(exp_r), 3), basis
    except Exception:
        return 0, 0.0, "error"


_W80_HOUR_EDGE = {0: -0.269, 1: 0.135, 2: 0.265, 3: 0.226, 4: 0.444, 5: 0.081,
                  6: -0.054, 7: -0.538, 8: -0.597, 11: -0.19, 12: 1.264, 13: 0.632,
                  14: 0.527, 15: 0.539, 16: 0.048, 17: 0.413, 18: 0.393, 19: -0.214,
                  20: 1.388, 21: -0.798, 22: 0.084, 23: 0.38}

# Wave 160 (_WAVE160_HOUR_REFRESH): the W80 hour map was backfilled Jun 29
# and drifts as the market changes - measured Jul 19 (n>=10): hour 3 went
# +0.23 -> -0.45, hour 15 +0.54 -> -0.02, hour 16 +0.05 -> +1.59. Same cure
# as Wave 159 gave the confluence read: recompute each UTC hour's
# trend-aligned expectancy from the full maintained history (reusing Wave
# 159's alignment filter and the same full-history loader the parole system
# trusts), cached 6h, prefer the fresh value when n>=15 else fall back to
# the W80 seed. Display only, exactly like W80. Never raises.
_W160_TTL_SEC = 6 * 3600
_W160_MIN_SAMPLE = 15
_W160_HOUR_LIVE = {}
_w160_state = {"built_at": 0.0}


def _w160_refresh_hour_edges(force=False):
    """Rebuild _W160_HOUR_LIVE from the full strategy-log history if the
    cache is older than the TTL. Cheap after the first build (timestamp
    guard). Never raises - on any failure the hour read falls back to the
    W80 seed."""
    import time as _t
    try:
        now = _t.time()
        if not force and (now - _w160_state.get("built_at", 0.0)) < _W160_TTL_SEC:
            return
        try:
            import strategy_log as _sl
            rows = _sl._load_all_strategy_rows()
        except Exception:
            _w160_state["built_at"] = now  # do not hammer on failure
            return
        agg = {}
        for r in rows:
            try:
                res = (r.get("result") or "").strip()
                if res not in ("WOULD_WIN", "WOULD_LOSE"):
                    continue
                if "LAB|" in (r.get("setup_type") or ""):
                    continue
                if not _w159_aligned(r.get("direction"), r.get("trend"),
                                     r.get("htf_bias")):
                    continue
                ts = r.get("timestamp") or ""
                if len(ts) < 13 or ts[10] != "T":
                    continue
                try:
                    hr = int(ts[11:13])
                except ValueError:
                    continue
                if not 0 <= hr <= 23:
                    continue
                a = agg.setdefault(hr, {"w": 0, "l": 0, "rr": 0.0, "rrn": 0})
                if res == "WOULD_WIN":
                    a["w"] += 1
                else:
                    a["l"] += 1
                try:
                    rr = float(r.get("rr") or 0)
                    if rr > 0:
                        a["rr"] += rr
                        a["rrn"] += 1
                except (TypeError, ValueError):
                    pass
            except Exception:
                continue
        fresh = {}
        for hr, a in agg.items():
            n = a["w"] + a["l"]
            if n < _W160_MIN_SAMPLE:
                continue
            arr = (a["rr"] / a["rrn"]) if a["rrn"] else 0.0
            if arr <= 0:
                continue
            wr = a["w"] / n
            fresh[hr] = round(wr * arr - (1.0 - wr), 3)
        _W160_HOUR_LIVE.clear()
        _W160_HOUR_LIVE.update(fresh)
        _w160_state["built_at"] = now
    except Exception:
        return


def _w160_hour_edge(hour):
    """Freshest edge for a UTC hour: the live recompute when the sample is
    solid, else the W80 seed, else None (thin/unknown -> _time_edge already
    treats None as neutral)."""
    try:
        if hour in _W160_HOUR_LIVE:
            return _W160_HOUR_LIVE[hour]
    except Exception:
        pass
    return _W80_HOUR_EDGE.get(hour)


def _time_edge(hour):
    """Wave 80 (Jun 29, 2026): time-of-day edge, backfilled (trend-aligned).
    Returns (score_0_100, expected_R, label). 12-15 + 17-18 + 20 UTC are prime;
    7-8, 19, 21 bleed. Thin/unknown hours -> neutral. Shadow only in W80."""
    try:
        _w160_refresh_hour_edges()  # Wave 160: keep the hour read fresh (cached)
        er = _w160_hour_edge(int(hour))
        if er is None:
            return 50, 0.0, "neutral"
        score = int(max(0, min(100, round((er + 1.0) / 3.0 * 100))))
        if er >= 0.5:
            label = "prime"
        elif er >= 0.1:
            label = "good"
        elif er > -0.1:
            label = "neutral"
        else:
            label = "dead"
        return score, round(float(er), 3), label
    except Exception:
        return 50, 0.0, "neutral"

# Wave 158 (_WAVE158_DIRECTION_READ): per-market long-vs-short expectancy,
# backfilled (trend-aligned) from the full strategy_log graded shadows.
# Measured Jul 18: longs bleed on every market (-0.05 to -0.56R), shorts
# win on NQ/BTC/SOL (+0.27 to +0.96R), GC short is weak (-0.11R). This is
# a REGIME read, not a rule - it rides in The Read footer so Wayne sees on
# every alert whether the side is currently paying, and a later wave can
# turn it into a symmetric self-correcting gate. GC:LONG is absent (no
# graded data - Gold longs are not being generated) so it reads neutral.
# SHADOW/DISPLAY ONLY: nothing here blocks or sizes a trade.
_W158_DIRECTION_EDGE = {
    "NQ:SHORT": 0.956, "BTC:SHORT": 0.335, "SOL:SHORT": 0.265,
    "GC:SHORT": -0.105, "SOL:LONG": -0.052, "NQ:LONG": -0.334,
    "BTC:LONG": -0.561,
}

def _direction_edge(market, direction):
    """Wave 158 (_WAVE158_DIRECTION_READ): current per-market side edge.
    Returns (score_0_100, expected_R, label). Unknown market:direction
    (e.g. no graded data yet) -> neutral, exactly like _time_edge. Never
    raises. Display only - does not gate or size anything."""
    try:
        _k = "%s:%s" % (str(market), str(direction).replace("WATCH_", ""))
        # Wave 162 (_WAVE162_SIDE_TRUTH): prefer the LIVE rolling window
        # the Wave-161 gate acts on, so the Read's side line and the gate
        # can never disagree; the Wave-158 seed is only the cold-start
        # fallback for sides the window has not covered yet.
        er = None
        try:
            er = _w161_side_exp(market, direction)
        except Exception:
            er = None
        if er is None:
            er = _W158_DIRECTION_EDGE.get(_k)
        if er is None:
            return 50, 0.0, "neutral"
        score = int(max(0, min(100, round((er + 1.0) / 3.0 * 100))))
        if er >= 0.5:
            label = "hot"
        elif er >= 0.1:
            label = "favored"
        elif er > -0.1:
            label = "neutral"
        else:
            label = "against"
        return score, round(float(er), 3), label
    except Exception:
        return 50, 0.0, "neutral"

def _log_time_context(alert_id, market, tf, hour=None):
    """Wave 80 (Jun 29, 2026): log this entry time-of-day edge (shadow).
    Time-of-day is one of the strongest edges in the data. Recorded per fired trade
    (by alert_id) so we can confirm it holds on fresh data before it drives anything."""
    import json as _json
    try:
        if hour is None:
            hour = datetime.now(timezone.utc).hour
        tscore, texp, tlabel = _time_edge(hour)
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "alert_id": alert_id,
               "market": market, "tf": tf, "hour": int(hour),
               "time_edge_score": tscore, "time_expected_R": texp, "time_label": tlabel}
        path = os.path.join(BASE_DIR, "data", "time_context.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec) + "\n")
    except Exception as _e:
        try:
            log.warning("time context log failed: " + str(_e))
        except Exception:
            pass


def _log_decision_journal(alert_id, market, tf, stp, all_setups, tgt, rr, method, conv, tier, trend, htf_bias, news_flag, snapshot, adx_v, rsi_v, vol_ratio, w7=None):
    """Wave 78 (Jun 29, 2026): the bot's decision journal - its 'thoughts'.
    For every fired trade, record the FULL reasoning in one place: the aligned
    signal stack, the confluence score + expected edge (W76), trend context, the
    R/target chosen, conviction, the adaptive layers in play, and a plain-English
    summary of why it took the trade and what it expects. Append-only, saved
    forever (data/decision_journal.jsonl). Substrate for /why, post-trade
    reflection, and the hypothesis engine. Logging only; no firing impact."""
    import json as _json
    try:
        direction = stp.get("direction")
        aligned = sorted({s.get("type", "?") for s in (all_setups or []) if s.get("direction") == direction})
        try:
            cscore, cexp, cbasis = _confluence_score(aligned, market)
        except Exception:
            cscore, cexp, cbasis = 0, 0.0, "unavailable"
        trend_word = {"HH_HL": "uptrend", "LH_LL": "downtrend", "MIXED": "mixed/no-trend"}.get(str(htf_bias), str(htf_bias))
        thought = ("Took " + str(direction) + " " + str(market) + " " + str(tf) + " on " +
                   str(stp.get("type")) + " in a " + trend_word + ". Stack: " +
                   (", ".join(aligned) if aligned else "(single)") + " | confluence " +
                   str(cscore) + "/100, expected " + ("%+.2fR" % cexp) + " (" + cbasis +
                   "). Target " + ("%.2f" % rr) + "R, conviction " + str(conv) + " (" + str(tier) + ").")
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "alert_id": alert_id, "market": market, "tf": tf,
            "setup": stp.get("type"), "direction": direction,
            "entry": round(stp.get("entry", 0), 4), "stop": round(stp.get("raw_stop", 0), 4),
            "target": round(tgt, 4), "rr": round(rr, 2), "method": method,
            "conviction": conv, "tier": tier, "trend_score": trend,
            "htf_bias": htf_bias, "trend_word": trend_word,
            "hour": datetime.now(timezone.utc).hour, "news_flag": int(news_flag),
            "stack": aligned, "stack_size": len(aligned),
            "confluence_score": cscore, "expected_R": cexp, "score_basis": cbasis,
            "adaptive": (w7 or {}),
            "thought": thought,
        }
        path = os.path.join(BASE_DIR, "data", "decision_journal.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec) + "\n")
    except Exception as _e:
        try:
            log.warning("decision journal log failed: " + str(_e))
        except Exception:
            pass


def _log_confluence_stack(alert_id, market, tf, fired_stp, all_setups, snapshot, adx_v, rsi_v, vol_ratio):
    """
    Wave 72 (Jun 26, 2026): Confluence Memory foundation.

    At each fired entry, record the FULL stack of signals that were aligned with
    the entry's direction at that scan - not just the one we fired on - plus each
    one's detection reason (the 'why'). Shadow/logging only; does NOT change
    firing. Builds the permanent record (data/confluence_log.jsonl) that later
    waves score, so the bot can learn which COMBINATIONS of setups win together
    and begin composing its own strategy. Append-only; nothing is overwritten.
    """
    import json as _json
    try:
        direction = fired_stp.get("direction")
        aligned = [s for s in (all_setups or []) if s.get("direction") == direction]
        stack_types = sorted({s.get("type", "?") for s in aligned})
        _cscore, _cexp, _cbasis = _confluence_score(stack_types, market)
        reasons = {}
        for s in aligned:
            t = s.get("type", "?")
            if t in reasons:
                continue
            try:
                reasons[t] = _build_detection_reason(s, snapshot, adx_v, rsi_v, vol_ratio)
            except Exception:
                reasons[t] = ""
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "alert_id": alert_id,
            "market": market,
            "tf": tf,
            "fired": fired_stp.get("type"),
            "direction": direction,
            "stack": stack_types,
            "stack_size": len(stack_types),
            "confluence_score": _cscore,
            "expected_R": _cexp,
            "score_basis": _cbasis,
            "reasons": reasons,
        }
        path = os.path.join(BASE_DIR, "data", "confluence_log.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec) + "\n")
    except Exception as _e:
        try:
            log.warning("confluence stack log failed: " + str(_e))
        except Exception:
            pass


_W73_LAST_STACK = {}


def _log_scan_stack(market, tf, all_setups, snapshot, adx_v, rsi_v, vol_ratio, htf_bias):
    """
    Wave 73 (Jun 29, 2026): Confluence Memory - capture the NON-FIRED stacks too.

    W72 records the stack at FIRED entries. This records the FULL set of setups
    DETECTED on a market each scan - including ones the bot never fires - so we
    can later learn which COMBINATIONS the bot is wrongly skipping (combos that
    would have won). Shadow/logging only; does not change firing. Deduped per
    market:tf - only logs when the present stack changes or 10+ minutes pass - so
    it does not spam a line every scan. Append-only to data/scan_stacks.jsonl.
    """
    import json as _json
    try:
        if not all_setups:
            return
        items = []
        seen = set()
        for s in all_setups:
            t = s.get("type", "?")
            d = s.get("direction", "?")
            if (t, d) in seen:
                continue
            seen.add((t, d))
            try:
                reason = _build_detection_reason(s, snapshot, adx_v, rsi_v, vol_ratio)
            except Exception:
                reason = ""
            items.append({"type": t, "direction": d, "reason": reason})
        if not items:
            return
        fingerprint = tuple(sorted((i["type"], i["direction"]) for i in items))
        cache_key = market + ":" + str(tf)
        now = datetime.now(timezone.utc)
        last = _W73_LAST_STACK.get(cache_key)
        if last and last[0] == fingerprint and (now - last[1]).total_seconds() < 600:
            return
        _W73_LAST_STACK[cache_key] = (fingerprint, now)
        rec = {
            "ts": now.isoformat(),
            "market": market,
            "tf": tf,
            "htf_bias": htf_bias,
            "n": len(items),
            "signals": items,
        }
        path = os.path.join(BASE_DIR, "data", "scan_stacks.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec) + "\n")
    except Exception as _e:
        try:
            log.warning("scan stack log failed: " + str(_e))
        except Exception:
            pass


def _market_position_open(market: str) -> bool:
    # Wave 129 (_WAVE129_ONE_POSITION_PER_MARKET): fire-time position guard.
    # ONE position per market, enforced at the moment of fire against ALL
    # THREE position sources (any one can drift from the others):
    #   1) outcomes rows still OPEN (the alert lifecycle),
    #   2) the futures sim open positions (NQ/GC),
    #   3) the crypto sim open positions (BTC/SOL).
    # The scan-level already_in check reads only source 1 and only ONCE per
    # scan pass - stale by fire time: a second setup in the same pass (e.g.
    # opposite direction, which the dup-guards allow) or a row-vs-sim
    # lifecycle drift could double-enter one market (observed on NQ Jul 10).
    # Never blocks on error - a guard failure must not stop the bot.
    try:
        for _r in ot.load_open_trades():
            if _r.get("market") == market:
                return True
    except Exception as _e:
        log.warning(f"W129 guard: outcomes check failed (non-fatal): {_e}")
    try:
        if market in ("NQ", "GC"):
            for _t in sim.load_state().get("open_sim_trades", []):
                if _t.get("market") == market and _t.get("status", "OPEN") == "OPEN":
                    return True
        elif market in ("BTC", "SOL"):
            for _t in crypto_sim.load_crypto_state().get("open_trades", []):
                if _t.get("market") == market:
                    return True
    except Exception as _e:
        log.warning(f"W129 guard: sim check failed (non-fatal): {_e}")
    return False


# Wave 140 (_WAVE140_LEARNABLE_REJECTS): give rejected/shadow rows the SAME
# structure target the fire path would have used, so check_missed_setups can
# grade them into real WOULD_WIN/WOULD_LOSE evidence. Without a target these
# rows could never resolve - the bot's #1 filters produced ZERO learnable
# shadow data (measured Jul 15: 1,511 vol-confirm near-misses, none graded;
# 3,709 suspended-shadow rows, none graded).
# DEDUP: the same setup instance is re-detected and re-logged many times per
# hour; grading every copy would count one market move dozens of times and
# pollute the Wave-60 learning stats. Only the FIRST rejection per
# (market, setup, direction) per hour gets levels; the rest stay level-less
# and are stamped NO_LEVELS at birth by Wave 139 (one graded instance is
# one paper trade, mirroring how one real fire would have been one position).
# DETECTED rows are deliberately NOT leveled: a detection that later FIRES
# would grade the same move twice (paper + real).
_W140_LEVELED = {}
_W140_DEDUP_S = 3600.0
_W140_LAST_MISSED = [0.0]

# Wave 150 (_WAVE150_SETUP_LAB): the lab lane. Named tenant setup classes
# get a shadow row logged at FULL cadence - BEFORE any gate (volume, trend,
# suspension, conviction) can consume or block them - so the bot builds
# graded WOULD_WIN/WOULD_LOSE evidence for setups it currently never fires.
# STRUCTURALLY UNABLE TO FIRE: the lab writes rows straight to the strategy
# log and never appends anything to `setups`, so nothing new can ever reach
# the fire loop or ot.log_alert. The grader (strategy_log._mutator, read
# from source Jul 18) grades ANY unresolved non-FIRED row carrying both
# levels, so lab rows resolve exactly like the SUSP| parole rows do.
# Types are logged as "LAB|<type>" so every downstream stat keys them apart
# from the real, gated setup. One leveled row per (market,type,direction)
# per hour via the Wave-146 lane dedup (lane="LAB|").
# HONEST LIMIT (FABEL_THE_PAPER_CONVICTION_JUL18.md): lab evidence is PAPER
# and flows into the same blended counter as everything else. It RANKS and
# COMPARES tenants; it cannot GRADUATE one to live firing until the
# honest-counter wave splits real from paper. Graduation is a separate,
# Wayne-signed wave regardless - this wave only builds the evidence.
_W150_LAB_TENANTS = {
    # market -> set of setup types. "_ALL_SHORT_" = every detected SHORT
    # on that market. The "*" row applies to every market.
    "NQ": {"_ALL_SHORT_"},
    "*": {"RSI_DIV_BULL", "RSI_DIV_BEAR"},
}


def _w150_is_tenant(market, stp):
    """True if this detected setup belongs to the lab tenant registry."""
    try:
        t = str(stp.get("type", ""))
        d = str(stp.get("direction", ""))
        if not t or t.startswith("LAB|"):
            return False
        mk = _W150_LAB_TENANTS.get(market, set())
        anyk = _W150_LAB_TENANTS.get("*", set())
        if t in mk or t in anyk:
            return True
        if "_ALL_SHORT_" in mk and d == "SHORT":
            return True
        return False
    except Exception:
        return False


def _w150_lab_lane(market, entry_tf, setups, df_e, atr_v, trend,
                   adx_v, rsi_v, vol_ratio, htf_bias, news_flag, cur_price):
    """Log one leveled LAB| shadow row per tenant per hour. Swallow-all:
    a lab failure must never touch a scan. Returns rows logged (tests)."""
    n = 0
    try:
        for stp in (setups or []):
            try:
                if not _w150_is_tenant(market, stp):
                    continue
                tgt, rr = _w140_shadow_levels(market, stp, df_e, atr_v,
                                              trend, lane="LAB|")
                if not tgt or not rr:
                    continue  # deduped this hour, or no structure target
                sl.log_scan_decision(market, entry_tf,
                    "LAB|" + str(stp.get("type", "?")),
                    str(stp.get("direction", "?")),
                    cur_price, stp["entry"], stp["raw_stop"], tgt, rr,
                    0, "LAB",
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    "SHADOW_LAB",
                    "Lab lane: tenant logged ungated at full cadence - "
                    "structurally cannot fire")
                n += 1
            except Exception:
                continue
    except Exception:
        pass
    return n


_W148_LAST = {"m": ""}


def _w148_note():
    """Wave 148 (_WAVE148_LEVELS_XRAY): the shadow-levels helper's verdict as a
    short log suffix. '' when levels were found - nothing to explain."""
    m = _W148_LAST.get("m", "")
    return (" | levels: " + m) if m else ""


def _w148_reject_reason(method, market):
    """Wave 148: the TRUTH about why no target was found, replacing a hardcoded
    sentence that was wrong for two of the three possible causes. Read from the
    source: outcome_tracker.structure_target returns tgt=0.0 with one of three
    codes - no_target / rr_too_high / rr_too_low - and the old line called all
    three 'too close for minimum R:R'. For the entire BREAK_RETEST family the
    real code is rr_too_high (levels too FAR in R terms because the stop is
    tight), so the log recorded the exact opposite of what happened, and every
    audit that read it was misled. The cap mirrors Wave 75's per-market table
    (GC 3.0 / NQ 3.5 / BTC 4.0 / SOL 4.0, default 3.5) - if that table moves,
    move this one in the same commit."""
    cap = {"GC": 3.0, "NQ": 3.5, "BTC": 4.0, "SOL": 4.0}.get(market, 3.5)
    if method == "rr_too_high":
        return ("No usable swing target \u2014 every candidate level prices ABOVE "
                "the %.1fR cap for %s (stop is tight, so real structure sits too "
                "far in R terms)" % (cap, market))
    if method == "rr_too_low":
        return ("No usable swing target \u2014 nearest structural level prices "
                "BELOW the minimum R:R (stop is wide, so reachable structure is "
                "worth too little)")
    return ("No usable swing target \u2014 no swing level in range at all "
            "(method=%s)" % (method or "unknown"))


def _w140_shadow_levels(market, stp, df_e, atr_v, trend, lane=""):
    """Return (target, rr) for a rejected setup, or (0, 0) when deduped or
    unavailable. Never raises - a levels failure must never break a scan.
    Wave 146 (_WAVE146_PAROLE_LANE): 'lane' gives a caller its own dedup
    namespace. Measured live: 1,759 suspended-shadow rows had produced only
    27 leveled paper trades (1.5%) because a benched setup's volume/trend
    rejections consume the shared hourly slot before its parole evidence
    can - the suspension gate sits AFTER those gates, so it usually found
    the slot already spent. The SUSPENDED site now runs in its own lane:
    parole cases build at full speed, still one independent instance per
    bucket per hour, zero change to any other site's cadence."""
    try:
        key = lane + "%s:%s:%s" % (market, stp.get("type"), stp.get("direction"))
        now = _time.time()
        _last = _W140_LEVELED.get(key)
        if _last is not None and now - _last < _W140_DEDUP_S:
            # Wave 148: mark the dedup explicitly. Without this the module dict
            # still holds the PREVIOUS call's code and the note would report a
            # stale reason - a log that lies about lying.
            _W148_LAST["m"] = "deduped"
            return 0, 0
        tgt, rr, method = ot.structure_target(df_e, stp["direction"], stp["entry"],
                                              stp["raw_stop"], atr_v,
                                              market=market, trend_score_val=trend)
        # Wave 148 (_WAVE148_LEVELS_XRAY): structure_target hands back one of
        # three failure codes and this helper threw it away, so nothing could
        # say WHY a benched setup never produced a target. Measured Jul 17: 8 of
        # 19 triggering benched buckets have entry AND stop but have NEVER been
        # leveled - they cannot be graded, so they can never earn parole, and
        # the fire path rejects no-target setups too. Record the code.
        _W148_LAST["m"] = "" if (tgt and method != "no_target") else (method or "no_target")
        if method == "no_target" or not tgt:
            return 0, 0
        _W140_LEVELED[key] = now
        if len(_W140_LEVELED) > 400:
            _W140_LEVELED.clear()  # tiny map; a clear just re-levels sooner
        return round(float(tgt), 4), round(float(rr), 2)
    except Exception:
        return 0, 0


def _w149_seed(filt, market, setup, base):
    """Wave 149 (_WAVE149_ADX_BASE_SEED): hand the learned-overrides store the
    base this gate actually computed. adx_min is the only whitelisted filter
    whose base is dynamic (per-setup, widened during prime session), so the
    store cannot know it without being told - which is why the adx hand has
    been asleep since Wave 143. Guarded lazy import and swallow-all: this
    path can touch the disk, and a scan must never die for a seed."""
    try:
        import learned_overrides as _lo
        _lo.seed_base(filt, market, setup, base)
    except Exception:
        pass


def _w143_get(filt, market, setup, base):
    """Wave 143 (_WAVE143_LEARNED_OVERRIDES): the gates' read path into the
    learned-overrides store - the HANDS of the learning loop, built to
    Wayne's signed bounds (Jul 15: whitelist vol_confirm/vol_floor/adx_min/
    conv_min only; hard floors 0.6x/0.2x/12/44; 2-day proof; 1 change/day;
    auto-revert on 10 bad live trades; 1-day revert cooldown, 3 strikes ->
    a week). Guarded lazy import with a full fallback: if the module is
    missing or broken every gate silently uses its base - the learning
    system can NEVER break a scan."""
    try:
        import learned_overrides as _lo
        return _lo.get(filt, market, setup, base)
    except Exception:
        return base


def _w143_mark_via(stp, filt, market, observed, base):
    """Wave 143: if this value passed ONLY because of a learned override,
    mark the setup so the trade (if it fires) is tagged for the revert
    tracker. Never raises."""
    try:
        import learned_overrides as _lo
        if _lo.is_pass_via_override(filt, market, stp["type"], observed, base):
            stp["_w143_via"] = (filt, market, stp["type"])
    except Exception:
        pass


def _w143_outcome_lookup(alert_id):
    """Wave 143: resolve a tagged alert to (result, rr) from outcomes so the
    revert tracker can judge an override on the live trades it unlocked."""
    try:
        for r in ot._read_all():
            if str(r.get("alert_id", "")) == str(alert_id) and r.get("status") == "CLOSED":
                return r.get("result", ""), float(r.get("rr") or 0)
    except Exception:
        pass
    return "", 0.0


async def scan_market(app, market, frames):
    global DAILY_TRADE_COUNT, DAILY_PROFIT_LOCKED, DAILY_LOSS_GATE
    cfg        = get_market_config(market)
    primary_tf = cfg.ENTRY_TIMEFRAMES[0]
    df_primary = frames.get(primary_tf)
    if df_primary is None or df_primary.empty:
        log.warning(f"[{market}] Missing primary frame."); return

    futures_ok = _futures_session_ok(market)
    crypto_ok  = _crypto_session_ok(market)
    already_in = any(r.get("market") == market for r in ot.load_open_trades())

    news_flag = ot.in_news_window()
    trend, _  = ot.trend_score(frames, market)
    _htf = frames.get(cfg.HTF_CONFIRM)
    if _htf is None or (hasattr(_htf,"empty") and _htf.empty):
        _htf = frames.get("1h")
    htf_bias = ot.structure_bias(_htf)
    session  = cfg.get_session_context()
    log.info(f"[{market}] Trend:{trend:+d} HTF:{htf_bias} Session:{session['session']} News:{news_flag}")

    # Dual-track sim: check crypto sim trades for BTC/SOL only
    # Closes positions on target/stop/bias-flip/7-day max-hold and posts to Telegram.
    if market in ("BTC", "SOL"):
        try:
            df15 = frames.get("15m")
            if df15 is not None and not df15.empty:
                cur_price = float(df15["Close"].iloc[-1])
                # Wave 5: reconcile against outcomes.csv first so we don't
                # double-close trades that auto-resolve already closed.
                try:
                    crypto_sim.reconcile_with_outcomes()
                except Exception as _re:
                    log.warning(f"[{market}] crypto reconcile in scan: {_re}")
                closed_crypto = crypto_sim.auto_check_crypto_trades(
                    {market: cur_price},
                    {market: frames},
                )
                for cc in closed_crypto:
                    cicon = "✅" if cc.get("result") == "WIN" else "❌"
                    cpnl = cc.get("pnl_dollars", 0)
                    cpnl_sign = f"+${cpnl:.2f}" if cpnl >= 0 else f"-${abs(cpnl):.2f}"
                    cstate = crypto_sim.load_crypto_state()
                    cmsg = (
                        f"{cicon} *CRYPTO SIM CLOSED — {cc.get('market')} {cc.get('direction')}*\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Reason: `{str(cc.get('exit_reason', '?')).upper()}`\n"
                        f"Entry: `{cc.get('entry')}` → Exit: `{cc.get('exit_price')}`\n"
                        f"P&L: `{cpnl_sign}` ({cc.get('result')})\n"
                        f"Held: `{cc.get('held_hours', 0):.1f}`h\n"
                        f"Balance: `${cstate.get('balance', 0):.2f}`"
                    )
                    await tg_send(app, cmsg)
        except Exception as _ce:
            log.warning(f"[{market}] crypto_sim auto_check: {_ce}")

    # Wave 86: resolve after-close shadows (data-only, never sends Telegram)
    try:
        _check_afterclose_shadows(market, frames)
    except Exception:
        pass
    # Wave 87: score map-vs-live targets (data-only, never sends Telegram)
    try:
        _check_target_map_shadows(market, frames)
    except Exception:
        pass
    # Wave 88: bench retrial - collect + score suspended-setup signals (silent)
    try:
        _collect_bench_shadows(market)
        _check_bench_shadows(market, frames)
    except Exception:
        pass
    # Auto-check outcomes
    # Wave 21 (May 9, 2026): capture closures so we can trigger dashboard regen
    _closed_now = list(ot.auto_check_outcomes({market: frames}))
    for c in _closed_now:
        icon   = "✅" if c["result"]=="WIN" else "❌"
        result = c["result"]
        all_rows = ot._read_all()
        orig = next((r for r in all_rows if r.get("alert_id")==c.get("alert_id")), {})
        entry_p = orig.get("entry", "?")
        exit_p  = c["price"]
        setup_n = _md(orig.get("setup", ""))
        tf_n    = orig.get("tf", "")
        dir_n   = orig.get("direction", "")
        tier_n  = orig.get("tier", "")
        try:
            pts  = float(exit_p) - float(entry_p)
            if "SHORT" in dir_n: pts = -pts
            pts_str = f"+{round(pts,2)}" if pts>=0 else str(round(pts,2))
            pct_val = round(pts/float(entry_p)*100,2)
            pct_str = f"+{pct_val}%" if pct_val>=0 else f"{pct_val}%"
        except: pts_str="?"; pct_str="?"

        today_str = _now_et().strftime("%Y-%m-%d")
        today_closed = [r for r in all_rows
                        if r.get("status")=="CLOSED" and r.get("result") in ("WIN","LOSS")
                        and today_str in r.get("timestamp","")]
        day_w = sum(1 for r in today_closed if r["result"]=="WIN")
        day_l = sum(1 for r in today_closed if r["result"]=="LOSS")

        # Wave 84 (Jul 2, 2026): the Sim P&L section is the ONE part of this alert
        # that can raise (sim math / risk-dict access / state read). Wrap it so a
        # sim error can NEVER drop the whole WIN/LOSS alert - the trade result must
        # always reach Telegram. On any failure, fall back to a short honest note.
        sim_line = ""
        try:
            sim_state = sim.load_state()
            if sim_state.get("enabled"):
                risk = sim.check_risk_limits(sim_state)
                sim_trades = sim_state.get("trades", [])
                sim_match = next((t for t in reversed(sim_trades)
                                  if t.get("alert_id")==c.get("alert_id")), None)
                if sim_match:
                    spnl = sim_match.get("pnl", 0)
                    spnl_str = f"+${spnl:,.2f}" if spnl>=0 else f"-${abs(spnl):,.2f}"
                    contr = sim_match.get("contracts", 1)
                    sim_line = (
                        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                        f"\U0001f4b0 *Sim P&L*\n"
                        f"  Trade: `{spnl_str}` ({contr} contracts)\n"
                        f"  Today: `${risk['daily_pnl']:+,.2f}`\n"
                        f"  Balance: `${risk['balance']:,.2f}`\n"
                        f"  Daily limit left: `${risk['daily_left']:,.2f}`\n"
                    )
                else:
                    # Smart line: no sim fill matched this alert - say so honestly
                    # instead of a silent generic block, but still show the account.
                    sim_line = (
                        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                        f"\U0001f4b0 *Sim Account* (this trade not tracked)\n"
                        f"  Today: `${risk['daily_pnl']:+,.2f}`\n"
                        f"  Balance: `${risk['balance']:,.2f}`\n"
                    )
        except Exception as _sim_e:
            log.warning(f"[{market}] W84: sim P&L section failed, alert still sending: {_sim_e}")
            sim_line = (
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                "\U0001f4b0 *Sim:* not tracked for this alert\n"
            )

        msg = (
            f"{icon} *Trade {result}* — {cfg.EMOJI} *{_md(cfg.FULL_NAME)}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📋 {setup_n} [{tf_n}] | {dir_n} | {tier_n}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📍 Entry: `{entry_p}`\n"
            f"🏁 Exit:  `{exit_p}`\n"
            f"📐 Move:  `{pts_str} pts` ({pct_str})\n"
            f"{sim_line}"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📅 *Today:* {day_w}W / {day_l}L\n"
            f"🧠 Bot learning updated."
        )
        await tg_send(app, msg)

        try:
            review_msg = ot.check_auto_review()
            if review_msg:
                await tg_send(app, review_msg)
        except Exception as e:
            log.warning(f"Auto review: {e}")

        if result == "LOSS":
            _record_loss(market)
            # Pre-Batch 2026-04-20: The "2 consecutive losses today — trading
            # halted for session / Rest up" Telegram message was REMOVED here.
            # The DAILY_LOSS_GATE no longer blocks trades; counter-only.
            try:
                atr_v_exit = float(ot.atr(frames.get("15m", pd.DataFrame())).iloc[-1]) if frames.get("15m") is not None else 0
                if atr_v_exit > 0:
                    _add_zone_lockout(market, dir_n, float(entry_p), atr_v_exit)
            except Exception:
                pass
            _set_family_cooldown(market, orig.get("setup",""), "LOSS")
            losses_count = CONSECUTIVE_LOSSES.get(market, 0)
            if losses_count >= 3:
                MARKET_HALTED[market] = True
                await tg_send(app,
                    f"⛔ *{_md(cfg.FULL_NAME)} HALTED*\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"3 consecutive losses on {market}.\n"
                    f"No new entries until next session.\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Take a break. Review next session."
                )
            elif losses_count >= 2:
                log.warning(f"[{market}] 2 consecutive losses — next entry needs regime confirmation")
            # Update edge tracker for LOSS
            try:
                entry_v = float(orig.get("entry", 0))
                stop_v  = float(orig.get("stop", 0))
                if entry_v and stop_v and abs(entry_v - stop_v) > 0:
                    sim.record_trade_for_sizing(orig.get("setup","UNKNOWN"), htf_bias, False, 1.0)
            except Exception:
                pass
        elif result == "WIN":
            _record_win(market)
            _set_family_cooldown(market, orig.get("setup",""), "WIN")
            # Update edge tracker with actual R-multiple
            try:
                entry_v = float(orig.get("entry", 0))
                stop_v  = float(orig.get("stop", 0))
                tgt_v   = float(exit_p)
                if entry_v and stop_v and abs(entry_v - stop_v) > 0:
                    r_mult = abs(tgt_v - entry_v) / abs(entry_v - stop_v)
                    sim.record_trade_for_sizing(orig.get("setup","UNKNOWN"), htf_bias, True, r_mult)
            except Exception:
                pass

    # Wave 21 (May 9, 2026): trigger background dashboard regen if any
    # trades closed. Updates the live dashboard within seconds of a W/L
    # instead of waiting up to 5 min for the auto-refresh loop. Fire-and-
    # forget on background thread; doesn't block the rest of the scan.
    if _closed_now:
        try:
            import generate_dashboard as _w21_gd
            asyncio.create_task(asyncio.to_thread(_w21_gd.main))
            log.info(
                f"[{market}] Wave 21: dashboard regen triggered "
                f"after {len(_closed_now)} closure(s)"
            )
        except Exception as _w21_regen_err:
            log.warning(f"Wave 21 regen trigger failed: {_w21_regen_err}")

    # ============================================================
    # Wave 11 (May 4): Phantom-Loss Alarm Handler
    # Pick up any events the phantom guard caught during this scan
    # and Telegram-alarm them. Filter to only this market's events so
    # the alarm appears in the right scan_market call. Safe even if
    # the queue is empty - just returns []. Wrapped in try/except so
    # an alarm bug can never block sim/crypto auto-checks below.
    # ============================================================
    try:
        phantom_events = ot.get_and_clear_phantom_events()
        # Re-queue any events for OTHER markets so they fire on those
        # markets' scans (preserves correct attribution in alarms).
        leftover = []
        for evt in phantom_events:
            if evt.get("market") != market:
                leftover.append(evt)
                continue
            try:
                reasons = evt.get("reasons", [])
                reasons_str = ", ".join(reasons) if reasons else "unknown"
                elapsed = evt.get("elapsed_seconds")
                elapsed_str = f"{elapsed}s" if elapsed is not None else "unknown"
                period_h = evt.get("period_high")
                period_l = evt.get("period_low")
                period_str = (f"`{period_h:.2f}` / `{period_l:.2f}`"
                              if period_h is not None and period_l is not None
                              else "`(none)`")
                cc = evt.get("current_close")
                cc_str = f"`{cc:.4f}`" if cc is not None else "`(none)`"
                frames_str = ",".join(evt.get("frames_used", [])) or "(none)"
                msg = (
                    f"🛡 *PHANTOM-LOSS GUARD ACTIVATED*\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"Bot tried to close `{evt.get('alert_id','?')}` as a "
                    f"*{evt.get('would_have_been','?')}* but the guard refused.\n\n"
                    f"🔍 *Setup:* {market} {_md(evt.get('setup','?'))} {evt.get('direction','?')}\n"
                    f"⚠️ *Why blocked:* `{reasons_str}`\n\n"
                    f"📊 *Diagnostics:*\n"
                    f"• Alert opened: `{evt.get('alert_timestamp','?')[:19]}`\n"
                    f"• Elapsed: `{elapsed_str}`\n"
                    f"• Entry / Stop / Target: `{evt.get('entry')}` / `{evt.get('stop')}` / `{evt.get('target')}`\n"
                    f"• Would have exited at: `{evt.get('would_have_exited_at')}`\n"
                    f"• Current close: {cc_str}\n"
                    f"• Period H/L (post-alert): {period_str}\n"
                    f"• Frames seen: `{frames_str}`\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"✅ Trade left OPEN. No data corruption.\n"
                    f"_Wave 10 + Wave 11 self-defense layer working as designed._"
                )
                # Wave 26 (May 11, 2026): Telegram alert silenced.
                # Phantom-loss events persisted to data/phantom_events.jsonl
                # via _record_phantom_event (Wave 10/11) AND now to
                # data/bot_brain.jsonl for Claude analysis. The formatted
                # 'msg' variable above is left unused (cosmetic; raw evt
                # dict has all the same data plus more).
                bot_brain_log("phantom_loss", evt)
                log.info(f"[{market}] Wave 26: phantom-loss event logged (no Telegram)")
            except Exception as _pe_msg:
                log.warning(f"[{market}] phantom alarm log: {_pe_msg}")
        # Push back any other-market events
        if leftover:
            for evt in leftover:
                ot._record_phantom_event(evt)
    except Exception as _pe_outer:
        log.warning(f"[{market}] phantom alarm handler outer: {_pe_outer}")

    if sim.load_state().get("enabled"):
        try:
            df15 = frames.get("15m")
            if df15 is not None and not df15.empty:
                price = float(df15["Close"].iloc[-1])
                # Wave 36 (May 11, 2026): reconcile against outcomes.csv FIRST so we
                # close any sim trade that outcome_tracker already resolved (wick
                # hits etc that auto_check_sim_trades misses on 15m close prices).
                try:
                    _w36_n = sim.reconcile_with_outcomes()
                    if _w36_n:
                        log.info(f"[{market}] Wave 36 reconcile: closed {_w36_n} stale sim trade(s)")
                except Exception as _w36_e:
                    log.warning(f"[{market}] Wave 36 sim reconcile in scan: {_w36_e}")
                for sc in sim.auto_check_sim_trades({market: price}):
                    s_icon = "\u2705" if sc.get("result")=="WIN" else "\u274c"
                    pnl    = sc.get("pnl",0)
                    contr  = sc.get("contracts",1)
                    risk   = sim.check_risk_limits()
                    pnl_sign = f"+${pnl:,.2f}" if pnl>=0 else f"-${abs(pnl):,.2f}"
                    bar_n  = int(min(10, risk["daily_used_pct"]/10))
                    bar    = "\ud83d\udfe5"*bar_n + "\u2b1c"*(10-bar_n)
                    today_sign = f"+${risk['daily_pnl']:,.2f}" if risk['daily_pnl']>=0 else f"-${abs(risk['daily_pnl']):,.2f}"
                    msg = (
                        f"{s_icon} *SIM {sc.get('result')}* \u2014 {cfg.EMOJI} *{cfg.FULL_NAME}*\n"
                        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                        f"P&L: `{pnl_sign}` | Contracts: `{contr}`\n"
                        f"Balance: `${risk['balance']:,.2f}` | Today: `{today_sign}`\n"
                        f"{bar} {risk['daily_used_pct']:.0f}% daily limit used"
                    )
                    await tg_send(app, msg)
        except Exception as e:
            log.warning(f"[{market}] sim check: {e}")

    # Dual-track: also check crypto sim trades
    try:
        df15 = frames.get("15m")
        live_price_map = {market: float(df15["Close"].iloc[-1])} if df15 is not None and not df15.empty else {}
        closed_crypto = crypto_sim.auto_check_crypto_trades(
            live_price_map,
            {market: frames},
        )
        for cc in closed_crypto:
            icon = "✅" if cc.get("result") == "WIN" else "❌"
            pnl = cc.get("pnl_dollars", 0)
            pnl_sign = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            msg = (
                f"{icon} *CRYPTO SIM CLOSED — {cc.get('market')} {cc.get('direction')}*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Reason: `{cc.get('exit_reason', '?').upper()}`\n"
                f"Entry: `{cc.get('entry')}` → Exit: `{cc.get('exit_price')}`\n"
                f"P&L: `{pnl_sign}` ({cc.get('result')})\n"
                f"Held: `{cc.get('held_hours', 0):.1f}` hours\n"
                f"Balance: `${crypto_sim.load_crypto_state()['balance']:.2f}`"
            )
            await tg_send(app, msg)
    except Exception as e:
        log.warning(f"[{market}] crypto_sim check: {e}")

    # Pre-Batch Follow-up Part A 2026-04-20: 3-loss per-market halt REMOVED.
    # Shadow-log each scan cycle that would have been blocked. Scan-level gate
    # (no setup detected yet) → single SHADOW_SCAN row with target=0 stop=0.
    # check_missed_setups() will correctly skip outcome-tracking rows with 0 target.
    _halt_per_market_would_fire = bool(MARKET_HALTED.get(market, False))
    if _halt_per_market_would_fire:
        log.info(f"[{market}] SHADOW: 3-loss per-market halt (firing anyway)")
        try:
            sl.log_scan_decision(
                market=market, tf="*", setup_type="SHADOW_SCAN",
                direction="-", price=0, entry=0, stop=0, target=0, rr=0,
                conviction=0, tier="SHADOW",
                trend=0, adx=0, rsi=0, vol_ratio=0,
                htf_bias="-", news_flag=0,
                decision=sl.DECISION_SHADOW_MARKET_HALT,
                reject_reason=f"{market} would have been halted: CONSECUTIVE_LOSSES={CONSECUTIVE_LOSSES.get(market, 0)}",
            )
        except Exception as e:
            log.warning(f"SHADOW_MARKET_HALT log failed: {e}")
    # Fall through — do NOT return. Signal continues normal flow.

    # Pre-Batch Follow-up Part A 2026-04-20: Correlation lockout REMOVED.
    _corr_would_fire = _is_correlation_locked(market)
    if _corr_would_fire:
        log.info(f"[{market}] SHADOW: correlation lockout (firing anyway)")
        try:
            sl.log_scan_decision(
                market=market, tf="*", setup_type="SHADOW_SCAN",
                direction="-", price=0, entry=0, stop=0, target=0, rr=0,
                conviction=0, tier="SHADOW",
                trend=0, adx=0, rsi=0, vol_ratio=0,
                htf_bias="-", news_flag=0,
                decision=sl.DECISION_SHADOW_CORRELATION,
                reject_reason=f"{market} would have been blocked: correlation lockout active (BTC/SOL 30-min)",
            )
        except Exception as e:
            log.warning(f"SHADOW_CORRELATION log failed: {e}")
    # Fall through.
    if already_in or not futures_ok or not crypto_ok:
        if already_in:
            log.info(f"[{market}] Already in position — skipping new entry scan")
        elif not futures_ok:
            log.info(f"[{market}] 4PM-6PM settlement window — no new entries for {market}")
        else:
            log.info(f"[{market}] Crypto 2-5 AM ET dead zone — no new entries (audit #7)")
        return

    # ── Task 8: Topstep eval daily gates ─────────────────────────
    now_et_check = _now_et()
    hm_check = now_et_check.hour * 60 + now_et_check.minute

    # Pre-Batch Follow-up Part A 2026-04-20: Topstep rule — no new NQ/GC entries
    # 3:30-4:10 PM ET (widened from 3:30-4:00). This gate STAYS (Topstep-required,
    # not a halt). Crypto unaffected.
    if market in ("NQ", "GC") and 930 <= hm_check < 970:  # 15:30 to 16:10
        log.info(f"[{market}] Topstep no-trade window 3:30-4:10 PM ET — skipping new entry")
        return

    # Pre-Batch 2026-04-20: The DAILY_LOSS_GATE is REMOVED.
    # We preserve the counter logic so we can measure the counterfactual —
    # but the gate no longer blocks signals. Instead, if the gate WOULD have
    # fired, we add SHADOW_HALTED context to every signal this scan evaluates.
    _halt_would_fire = DAILY_LOSS_GATE
    # Fall through — the signal flow continues as normal.

    # Pre-Batch Follow-up Part A 2026-04-20: Daily profit lock REMOVED.
    _profit_lock_would_fire = DAILY_PROFIT_LOCKED
    if _profit_lock_would_fire:
        log.info(f"[{market}] SHADOW: profit lock at +$150 (firing anyway)")
        try:
            sl.log_scan_decision(
                market=market, tf="*", setup_type="SHADOW_SCAN",
                direction="-", price=0, entry=0, stop=0, target=0, rr=0,
                conviction=0, tier="SHADOW",
                trend=0, adx=0, rsi=0, vol_ratio=0,
                htf_bias="-", news_flag=0,
                decision=sl.DECISION_SHADOW_PROFIT_LOCK,
                reject_reason="daily profit lock active (+$150 threshold hit earlier)",
            )
        except Exception as e:
            log.warning(f"SHADOW_PROFIT_LOCK log failed: {e}")
    # Fall through.

    # Pre-Batch Follow-up Part A 2026-04-20: Max daily trades cap REMOVED.
    _max_trades_would_fire = (DAILY_TRADE_COUNT >= MAX_DAILY_TRADES)
    if _max_trades_would_fire:
        log.info(f"[{market}] SHADOW: max {MAX_DAILY_TRADES} daily trades (firing anyway, count={DAILY_TRADE_COUNT})")
        try:
            sl.log_scan_decision(
                market=market, tf="*", setup_type="SHADOW_SCAN",
                direction="-", price=0, entry=0, stop=0, target=0, rr=0,
                conviction=0, tier="SHADOW",
                trend=0, adx=0, rsi=0, vol_ratio=0,
                htf_bias="-", news_flag=0,
                decision=sl.DECISION_SHADOW_MAX_TRADES,
                reject_reason=f"would have been blocked: trade #{DAILY_TRADE_COUNT + 1} of {MAX_DAILY_TRADES}-trade daily cap",
            )
        except Exception as e:
            log.warning(f"SHADOW_MAX_TRADES log failed: {e}")
    # Fall through.

    for entry_tf in cfg.ENTRY_TIMEFRAMES:
        htf_key = cfg.HTF_CONFIRM if entry_tf==cfg.ENTRY_TIMEFRAMES[0] else cfg.HTF_SWING
        _w145_news_15m = bool(entry_tf=="15m" and news_flag)  # Wave 145a (_WAVE145_NEWS_TF_EVIDENCE): the silent 15m news skip, now measured at the fire point
        df_e = frames.get(entry_tf)
        df_h = frames.get(htf_key)
        if df_e is None or df_h is None: continue
        if df_e.empty: continue

        setups = ot.detect_setups(df_e, df_h, htf_bias)

        # ── Task 6: OPENING_RANGE_BREAKOUT (NQ and GC only, 9:30-10:30 AM ET) ──
        if market in ("NQ", "GC") and entry_tf == "15m":
            try:
                orb_et = _now_et()
                orb_hm = orb_et.hour * 60 + orb_et.minute
                if 570 <= orb_hm <= 630:  # 9:30 AM to 10:30 AM ET
                    # Get the first 2 bars of RTH (9:30 and 9:45 = first 30 min)
                    if len(df_e) >= 10:
                        # Find bars from today's 9:30-10:00 AM range
                        orb_bars = []
                        for idx in range(len(df_e)):
                            bar_time = df_e.index[idx]
                            if hasattr(bar_time, 'tz_convert'):
                                bar_et = bar_time.tz_convert(ET_ZONE) if ET_ZONE else bar_time
                            else:
                                bar_et = bar_time
                            bh = bar_et.hour * 60 + bar_et.minute
                            if 570 <= bh < 600 and bar_et.date() == orb_et.date():
                                orb_bars.append(df_e.iloc[idx])
                        if len(orb_bars) >= 1:
                            orb_high = max(float(b["High"]) for b in orb_bars)
                            orb_low = min(float(b["Low"]) for b in orb_bars)
                            orb_close = float(df_e["Close"].iloc[-1])
                            orb_rsi = float(ot.rsi(df_e["Close"]).iloc[-1])
                            orb_vol_mean = float(df_e["Volume"].rolling(20).mean().iloc[-1]) if len(df_e) >= 20 else 0
                            orb_vol_last = float(df_e["Volume"].iloc[-1])
                            orb_vol_ratio = orb_vol_last / max(1e-9, orb_vol_mean) if orb_vol_mean > 0 else 0
                            orb_atr = float(ot.atr(df_e).iloc[-1])

                            if orb_close > orb_high and orb_vol_ratio > 1.3 and 45 <= orb_rsi <= 70:
                                stop_orb = orb_low - orb_atr * 0.2
                                setups.append({
                                    "type":      "OPENING_RANGE_BREAKOUT",
                                    "direction": "LONG",
                                    "entry":     orb_close,
                                    "raw_stop":  stop_orb,
                                    "level":     orb_high,
                                    "detail":    f"Opening range breakout above {round(orb_high,2)}. "
                                                 f"Vol {round(orb_vol_ratio,1)}x avg. First 30min range broken.",
                                })
                            elif orb_close < orb_low and orb_vol_ratio > 1.3 and 30 <= orb_rsi <= 55:
                                stop_orb = orb_high + orb_atr * 0.2
                                setups.append({
                                    "type":      "OPENING_RANGE_BREAKOUT",
                                    "direction": "SHORT",
                                    "entry":     orb_close,
                                    "raw_stop":  stop_orb,
                                    "level":     orb_low,
                                    "detail":    f"Opening range breakdown below {round(orb_low,2)}. "
                                                 f"Vol {round(orb_vol_ratio,1)}x avg. First 30min range broken.",
                                })
            except Exception as e:
                log.debug(f"ORB detection error: {e}")

        if not setups: log.info(f"[{market}] [{entry_tf}] No setups."); continue

        adx_v    = float(ot.adx(df_e).iloc[-1])
        rsi_v    = float(ot.rsi(df_e["Close"]).iloc[-1])
        atr_v    = float(ot.atr(df_e).iloc[-1])
        # Audit Finding #9 (2026-04-28): session-aware volume baseline.
        # 20-bar window on 15m = 5h; for 24/7 crypto the window gets pulled
        # down by overnight bars and inflates vol_ratio for anemic candles.
        # Use ~24h window for crypto; futures keep 20-bar (closed overnight).
        if market in ("BTC", "SOL"):
            _vol_window = {"1m": 1440, "5m": 288, "15m": 96,
                           "30m": 48, "1h": 24, "4h": 6}.get(entry_tf, 96)
        else:
            _vol_window = 20
        vol_mean = float(df_e["Volume"].rolling(_vol_window).mean().iloc[-1]) if len(df_e) >= _vol_window else None
        vol_last = float(df_e["Volume"].iloc[-1])
        vol_ratio= (vol_last / max(1e-9, vol_mean)) if (vol_mean and vol_mean > 0) else 0.0
        cur_price= float(df_e["Close"].iloc[-1])

        # ── Batch 2A: Build full indicator snapshot for logging ──
        def _safe_float(val, default=0.0):
            try:
                v = float(val)
                if not np.isfinite(v):
                    return default
                return v
            except (ValueError, TypeError):
                return default

        snapshot_context = {"close_price": cur_price}

        # Bollinger Bands
        try:
            bb_upper, bb_middle, bb_lower = ot.bollinger_bands(df_e["Close"])
            bb_u = _safe_float(bb_upper.iloc[-1])
            bb_m = _safe_float(bb_middle.iloc[-1])
            bb_l = _safe_float(bb_lower.iloc[-1])
            bb_width_pct = ((bb_u - bb_l) / bb_m * 100) if bb_m > 0 else 0.0
            snapshot_context.update({
                "bb_upper": bb_u, "bb_middle": bb_m, "bb_lower": bb_l,
                "bb_width_pct": bb_width_pct,
            })
        except Exception as e:
            log.debug(f"[{market}] BB calc: {e}")
            snapshot_context.update({"bb_upper": 0, "bb_middle": 0, "bb_lower": 0, "bb_width_pct": 0})

        # Stochastic
        try:
            stoch_k_s, stoch_d_s = ot.stochastic(df_e)
            snapshot_context["stoch_k"] = _safe_float(stoch_k_s.iloc[-1], 50)
            snapshot_context["stoch_d"] = _safe_float(stoch_d_s.iloc[-1], 50)
        except Exception as e:
            log.debug(f"[{market}] Stoch calc: {e}")
            snapshot_context.update({"stoch_k": 50, "stoch_d": 50})

        # MACD
        try:
            macd_l, macd_s_sig, macd_h = ot.macd(df_e["Close"])
            snapshot_context["macd_line"]   = _safe_float(macd_l.iloc[-1])
            snapshot_context["macd_signal"] = _safe_float(macd_s_sig.iloc[-1])
            snapshot_context["macd_hist"]   = _safe_float(macd_h.iloc[-1])
        except Exception as e:
            log.debug(f"[{market}] MACD calc: {e}")
            snapshot_context.update({"macd_line": 0, "macd_signal": 0, "macd_hist": 0})

        # EMAs + VWAP
        try:
            snapshot_context["vwap"]   = _safe_float(ot.vwap(df_e).iloc[-1])
            snapshot_context["ema20"]  = _safe_float(ot.ema(df_e["Close"], 20).iloc[-1])  if len(df_e) >= 20 else 0
            snapshot_context["ema50"]  = _safe_float(ot.ema(df_e["Close"], 50).iloc[-1])  if len(df_e) >= 50 else 0
            snapshot_context["ema200"] = _safe_float(ot.ema(df_e["Close"], 200).iloc[-1]) if len(df_e) >= 200 else 0
            snapshot_context["ema21"]  = _safe_float(ot.ema(df_e["Close"], 21).iloc[-1])  if len(df_e) >= 21 else 0
        except Exception as e:
            log.debug(f"[{market}] EMA calc: {e}")

        # Context: ATR, swings, volumes, regime, session
        snapshot_context["atr"] = atr_v
        snapshot_context["swing_high_30"] = _safe_float(df_e.iloc[-30:]["High"].max()) if len(df_e) >= 30 else 0
        snapshot_context["swing_low_30"]  = _safe_float(df_e.iloc[-30:]["Low"].min())  if len(df_e) >= 30 else 0
        snapshot_context["volume_raw"]    = vol_last
        snapshot_context["volume_20ma"]   = _safe_float(vol_mean) if vol_mean else 0
        snapshot_context["session_name"]  = session.get("session", "Unknown")

        try:
            from regime_classifier import classify_regime
            # NOTE: classify_regime uses lowercase column names. May fail silently.
            regime_info = classify_regime(df_e)
            snapshot_context["regime"] = regime_info.get("regime", "UNKNOWN")
        except Exception as e:
            log.debug(f"[{market}] Regime classify: {e}")
            snapshot_context["regime"] = "UNKNOWN"

        if vol_mean is None or not np.isfinite(vol_mean) or vol_mean < 1.0:
            # Apr 30 fix: 996 silent rejections in 7 days from this gate.
            # ccxt sometimes returns volume=0 from one exchange. Don't reject —
            # treat volume as neutral (1.0x) and let other filters decide.
            # Per-setup volume gate below still catches legit dead markets.
            log.info(f"[{market}] [{entry_tf}] Volume data degraded (vol_mean={vol_mean}) — treating as neutral 1.0x")
            vol_ratio = 1.0
            vol_mean = max(1.0, vol_mean or 1.0)
            # No `continue` — let setups continue to other filters
        # Audit Finding #6 / BACKLOG #4 (2026-04-28): per-setup volume gate.
        # April 14 alerts fired at vol_ratio 0.02-0.29 with HIGH conviction.
        # Universal 0.8 floor is too coarse — BREAK_RETEST (invert volume)
        # genuinely wants quiet retests. Per-setup gate:
        #   < 0.3      → dead market, reject everything
        #   confirm    → require >= 0.8
        #   invert     → pass through (conviction scores low vol as healthy)
        #   neutral    → pass through
        # Wave 150 (_WAVE150_SETUP_LAB): the lab lane runs BEFORE any gate
        # so a tenant's evidence cadence is never throttled by the filters
        # (the Wave-146 lesson: gates upstream of a logger consume its
        # slots). Writes shadow rows only; appends nothing to `setups`;
        # structurally cannot fire.
        try:
            _w150_lab_lane(market, entry_tf, setups, df_e, atr_v, trend,
                           adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                           cur_price)
        except Exception as _w150e:
            log.debug(f"[{market}] lab lane skipped: {_w150e}")
        _filtered_setups = []
        for stp in setups:
            _vol_dir = ot.VOLUME_DIRECTION.get(stp["type"], "confirm")
            _reject_reason = None
            # Wave 143 (_WAVE143_LEARNED_OVERRIDES): the volume gates read
            # through the learned-overrides store. Base thresholds unchanged
            # (0.3x floor / 0.8x confirm); a bucket only reads lower after the
            # ledger proved it on 2 consecutive days of graded paper evidence,
            # never below Wayne's signed floors (0.2x / 0.6x). The reason text
            # carries the LIVE threshold - Wave 142's classifier parses it at
            # any value, so the eyes stay open after every move.
            _w143_floor = _w143_get("vol_floor", market, stp["type"], 0.3)
            _w143_conf = _w143_get("vol_confirm", market, stp["type"], 0.8)
            if vol_ratio < _w143_floor:
                _reject_reason = f"dead market (vol_ratio={vol_ratio:.2f}x < {_w143_floor:.2f}x floor)"
            elif _vol_dir == "confirm" and vol_ratio < _w143_conf:
                _reject_reason = f"confirm setup needs vol_ratio >= {_w143_conf:.2f}x (got {vol_ratio:.2f}x)"
            # Wave 161 (_WAVE161_DIRECTION_GATE): symmetric side-expectancy
            # gate. Gates WHICHEVER side is bleeding in THIS market right
            # now, judged by the last 40 graded shadows; never gates on
            # thin data (<30 in the window). Self-correcting: gated
            # rejects still log + grade via Wave 140, so the window keeps
            # updating and the gate lifts when the side recovers.
            if _reject_reason is None:
                _w161_exp = _w161_side_exp(market, stp.get("direction"))
                if _w161_exp is not None and _w161_exp <= _W161_GATE_EXP:
                    _reject_reason = (f"side edge negative (last{_W161_WINDOW} "
                                      f"graded {_w161_exp:+.2f}R <= {_W161_GATE_EXP:+.2f}R)")
            if _reject_reason is None:
                if _vol_dir == "confirm":
                    _w143_mark_via(stp, "vol_confirm", market, vol_ratio, 0.8)
                _w143_mark_via(stp, "vol_floor", market, vol_ratio, 0.3)
            if _reject_reason:
                try:
                    sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                        cur_price, stp["entry"], stp["raw_stop"], *_w140_shadow_levels(market, stp, df_e, atr_v, trend), 0, "REJECT",
                        trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                        sl.DECISION_REJECTED,
                        f"{_reject_reason} — insufficient participation",
                        context=snapshot_context,
                        detection_reason=f"{stp['type']} rejected: {_reject_reason}")
                    _sample_reject_log(market, entry_tf, stp["type"], _reject_reason)
                except Exception as e:
                    log.warning(f"low-volume log failed: {e}")
                continue
            _filtered_setups.append(stp)
        if not _filtered_setups:
            log.info(f"[{market}] [{entry_tf}] All setups rejected on volume gate (vol_ratio={vol_ratio:.2f})")
            continue
        setups = _filtered_setups

        # Wave 73: Confluence Memory - record the FULL detected stack this scan (fired or not) so non-fired combos are learnable
        try:
            _log_scan_stack(market, entry_tf, setups, snapshot_context, adx_v, rsi_v, vol_ratio, htf_bias)
        except Exception:
            pass

        session_name = session.get("session","")
        is_prime_session = any(s in session_name for s in ("US Regular","London","Pre-Market","London/NY"))

        # ── Batch 2A: Log every raw detection BEFORE any filter ──
        # One DETECTED row per setup. Subsequent rows (REJECTED/ALMOST/FIRED) are
        # logged separately as the setup goes through the filter chain.
        for stp in setups:
            try:
                stp["market"] = market  # needed for scoring context later
                det_reason = _build_detection_reason(stp, snapshot_context,
                                                      adx_v, rsi_v, vol_ratio)
                conf_factors = _build_confidence_factors(snapshot_context, trend,
                                                           adx_v, rsi_v)
                sl.log_scan_decision(
                    market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"],
                    0, 0, 0, "DETECT",
                    trend, adx_v, rsi_v, vol_ratio,
                    htf_bias, news_flag,
                    sl.DECISION_DETECTED, "",
                    context=snapshot_context,
                    detection_reason=det_reason,
                    confidence_factors=conf_factors,
                )
            except Exception as e:
                log.debug(f"[{market}] DETECTED log error for {stp.get('type')}: {e}")

        for stp in setups:
            stp["market"] = market

            # Wave 74: Trend-alignment guard - counter-trend entries went 0-for-465 in backtest.
            # Skip setups whose direction opposes a CLEAR HTF trend (MIXED is allowed). Logged for audit.
            _w74_dir = str(stp.get("direction", "")).replace("WATCH_", "")
            # Wave 107 (_WAVE107_SHORT_TREND_GATE): the htf_bias part of this guard only
            # blocks shorts against a CLEAR bullish STRUCTURE (HH_HL); it misses shorts
            # fired into a bullish trend_score when structure is mixed. Live data: shorts
            # were 0/12, and every short fired into trend_score >= 2 lost. So ALSO reject a
            # SHORT when the trend_score is moderately bullish (>= 2, the code's existing
            # "trend present" threshold). Longs are deliberately NOT gated this way -- 3 of
            # 9 winning longs were counter-trend (trend_score <= -2), so gating longs would
            # kill working trades. Edit the 2 below to tune the short trend gate.
            _w74_counter = (htf_bias == "LH_LL" and _w74_dir == "LONG") or (htf_bias == "HH_HL" and _w74_dir == "SHORT") or (_w74_dir == "SHORT" and isinstance(trend, (int, float)) and trend >= 2)
            if _w74_counter:
                try:
                    sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                        cur_price, stp["entry"], stp["raw_stop"], *_w140_shadow_levels(market, stp, df_e, atr_v, trend), 0, "REJECT",
                        trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                        sl.DECISION_REJECTED,
                        "Wave 74 trend-alignment guard: counter-trend vs " + str(htf_bias) + " (0/465 historical)",
                        context=snapshot_context,
                        detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio))
                except Exception:
                    pass
                continue

            # Setup suspension check — block negative EV setups
            # Task 4: Shadow-log so we can retroactively analyze if suspensions were correct
            # Wave 89: paroled setups (bench-retrial-cleared, PAROLE_SETUPS)
            # may pass IF a non-suspended same-direction partner fired this
            # scan - Wayne's "earn the way back by combining" rule. A parole
            # pass still faces every modern gate below (trend, conviction).
            if ot.is_setup_suspended(market, stp["type"]) and not _parole_pass(market, stp, setups):
                log.info(f"[{market}] [{entry_tf}] Shadow-log {stp['type']} — suspended (would-have-fired)")
                suspended_info = ot.get_suspended_setups().get(f"{market}:{stp['type']}", {})
                reason_text = suspended_info.get("reason", "unknown")
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], *_w140_shadow_levels(market, stp, df_e, atr_v, trend, lane="SUSP|"), 0, "REJECT",
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    sl.DECISION_SHADOW_SUSPENDED,
                    f"Suspended due to {reason_text} — shadow-logged to track would-have-fired rate",
                    context=snapshot_context,
                    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio) + _w148_note())
                continue

            adx_min_by_setup = getattr(cfg, "ADX_MIN_BY_SETUP", {})
            required_adx = adx_min_by_setup.get(stp["type"], cfg.MIN_ADX)
            if is_prime_session:
                prime_adx = getattr(cfg, "MIN_ADX_PRIME", required_adx)
                required_adx = max(required_adx, prime_adx)
            # Wave 143: ADX minimum reads through the learned store (floor 12,
            # Wayne-signed). The gate passes its own computed base in, so the
            # per-setup / prime-session logic above stays the source of truth.
            _w143_adx_base = required_adx
            # Wave 149: tell the store what this gate actually requires, so the
            # daily decision can reason about adx like the other three filters.
            _w149_seed("adx_min", market, stp["type"], _w143_adx_base)
            required_adx = _w143_get("adx_min", market, stp["type"], required_adx)
            if adx_v >= required_adx:
                _w143_mark_via(stp, "adx_min", market, adx_v, _w143_adx_base)
            if adx_v < required_adx:
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], *_w140_shadow_levels(market, stp, df_e, atr_v, trend), 0, "REJECT",
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    sl.DECISION_REJECTED,
                    f"ADX {round(adx_v,1)} below {stp['type']} minimum {required_adx} — market too choppy for this setup type",
                    context=snapshot_context,
                    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio))
                _sample_reject_log(market, entry_tf, stp["type"], f"ADX {round(adx_v,1)} < {required_adx}")
                continue

            # Pre-Batch Follow-up Part A 2026-04-20: Per-setup cooldown REMOVED.
            _cd_would_fire = not _cooldown_ok(market, stp["type"])
            if _cd_would_fire:
                log.info(f"[{market}] [{entry_tf}] SHADOW: {stp['type']} cooldown (firing anyway)")
                try:
                    sl.log_scan_decision(
                        market=market, tf=entry_tf, setup_type=stp["type"], direction=stp["direction"],
                        price=cur_price, entry=stp["entry"], stop=stp["raw_stop"],
                        target=0, rr=0, conviction=0, tier="SHADOW",
                        trend=trend, adx=adx_v, rsi=rsi_v, vol_ratio=vol_ratio,
                        htf_bias=htf_bias, news_flag=news_flag,
                        decision=sl.DECISION_SHADOW_COOLDOWN,
                        reject_reason=f"per-setup cooldown active for {stp['type']}",
                        context=snapshot_context,
                        detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio),
                    )
                except Exception as e:
                    log.warning(f"SHADOW_COOLDOWN log failed: {e}")
            # Fall through — do NOT continue.

            # Pre-Batch Follow-up Part A 2026-04-20: Family cooldown REMOVED.
            _fam_would_fire = not _family_cooldown_ok(market, stp["type"])
            if _fam_would_fire:
                log.info(f"[{market}] [{entry_tf}] SHADOW: {stp['type']} family cooldown (firing anyway)")
                try:
                    sl.log_scan_decision(
                        market=market, tf=entry_tf, setup_type=stp["type"], direction=stp["direction"],
                        price=cur_price, entry=stp["entry"], stop=stp["raw_stop"],
                        target=0, rr=0, conviction=0, tier="SHADOW",
                        trend=trend, adx=adx_v, rsi=rsi_v, vol_ratio=vol_ratio,
                        htf_bias=htf_bias, news_flag=news_flag,
                        decision=sl.DECISION_SHADOW_FAMILY_CD,
                        reject_reason=f"family cooldown active ({_get_family(stp['type'])})",
                        context=snapshot_context,
                        detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio),
                    )
                except Exception as e:
                    log.warning(f"SHADOW_FAMILY_CD log failed: {e}")
            # Fall through.

            # Pre-Batch Follow-up Part A 2026-04-20: Loss zone lockout REMOVED.
            _zone_would_fire = _zone_locked(market, stp["direction"], stp["entry"])
            if _zone_would_fire:
                log.info(f"[{market}] [{entry_tf}] SHADOW: {stp['type']} zone lockout (firing anyway)")
                try:
                    sl.log_scan_decision(
                        market=market, tf=entry_tf, setup_type=stp["type"], direction=stp["direction"],
                        price=cur_price, entry=stp["entry"], stop=stp["raw_stop"],
                        target=0, rr=0, conviction=0, tier="SHADOW",
                        trend=trend, adx=adx_v, rsi=rsi_v, vol_ratio=vol_ratio,
                        htf_bias=htf_bias, news_flag=news_flag,
                        decision=sl.DECISION_SHADOW_ZONE_LOCK,
                        reject_reason=f"loss zone lockout active near entry {round(stp['entry'], 4)}",
                        context=snapshot_context,
                        detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio),
                    )
                except Exception as e:
                    log.warning(f"SHADOW_ZONE_LOCK log failed: {e}")
            # Fall through.

            if stp["type"] in ("APPROACH_SUPPORT","APPROACH_RESIST"):
                # Wave 27 (May 11, 2026): APPROACH setups are DETECTION-ONLY.
                # Wave 14 was supposed to remove them as alerts but left a leak:
                # if trend was strong enough (>= +2 for SUPPORT, <= -2 for RESIST)
                # they could still fire. Lifetime stats showed NQ:APPROACH_SUPPORT
                # firing 2 trades (1W/1L) and backtest showed 2 more losses on NQ.
                # Hard-skip ALL APPROACH alerts here. The setup type is still
                # detected by detect_setups() for use in conviction scoring of
                # OTHER setups (e.g., a LIQ_SWEEP_BULL near an APPROACH_SUPPORT
                # still gets that conviction bonus).
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    sl.DECISION_REJECTED,
                    "APPROACH setups are detection-only (Wave 27 block; never fire as alerts)",
                    context=snapshot_context,
                    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio))
                continue

            # Wave 64 (_WAVE64_CRYPTO_STOPS): crypto needs room to breathe.
            # detect_setups pads stops by only 0.2-0.5 ATR -- fine for NQ/GC,
            # but inside normal wick noise for BTC/SOL, so positions got
            # chopped out by routine volatility (the crypto sim bled from
            # stops, not direction). Enforce a minimum TOTAL stop distance
            # of 1.2 ATR for crypto, BEFORE target/RR math -- so the Wave 63
            # evidence-based RR floor then demands a target that pays for
            # the wider risk. Crypto is shadow-only until proven (Wave 60),
            # so this is validated on shadow outcomes before money moves.
            if market in ("BTC", "SOL") and atr_v > 0:
                try:
                    _min_stop_dist = 1.2 * atr_v
                    if abs(stp["entry"] - stp["raw_stop"]) < _min_stop_dist:
                        if stp["direction"] == "LONG":
                            stp["raw_stop"] = stp["entry"] - _min_stop_dist
                        else:
                            stp["raw_stop"] = stp["entry"] + _min_stop_dist
                except Exception:
                    pass
            tgt, rr, method = ot.structure_target(df_e, stp["direction"], stp["entry"], stp["raw_stop"], atr_v,
                                                   market=market, trend_score_val=trend)

            if method == "no_target" or tgt == 0:
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    sl.DECISION_REJECTED,
                    _w148_reject_reason(method, market),
                    context=snapshot_context,
                    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio))
                _sample_reject_log(market, entry_tf, stp["type"], "No swing target available")
                continue

            sim_risk = sim.check_risk_limits()
            if sim_risk.get("dd_left", 9999) <= 500:
                log.info(f"[{market}] Near max drawdown — all entries blocked")
                await tg_send(app,
                    "🚨 *Near max drawdown limit*\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "All new entries suspended.\n"
                    "Protect the account."
                )
                return

            quick_conv, quick_tier, quick_bd = ot.conviction_score(
                stp, trend, df_e, df_h, news_flag, adx_v, rsi_v, vol_ratio,
                abs(tgt-stp["entry"])/max(1e-9, atr_v)
            )
            # Wave 63: evidence-based RR floor per market:setup bucket.
            # Breakeven RR for win-rate p is (1-p)/p; we require a 25% margin
            # above breakeven, clamped to [0.8, 2.5], using the SAME shrunk
            # bucket stats as the conviction score. The old static map was
            # calibrated on inflated shadow stats (the "83% WR" setup's real
            # record was 1W/20L).
            setup_floor = ot.get_rr_floor(stp["type"], market)
            _global_min_rr = cfg.NEWS_MIN_RR if news_flag else SETTINGS["min_rr"]
            min_rr = max(setup_floor, _global_min_rr)
            if rr < min_rr:
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], tgt, rr, 0, "REJECT",
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    sl.DECISION_REJECTED,
                    f"R:R {round(rr,2)} below minimum {min_rr} (tier quick-conv {quick_conv}, target {round(tgt,4)})",
                    context=snapshot_context,
                    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio))
                _sample_reject_log(market, entry_tf, stp["type"], f"RR {round(rr,2)} < {min_rr}")
                continue

            clean_path = abs(tgt-stp["entry"])/max(1e-9, atr_v)
            conv, tier, bd_core = ot.conviction_score(stp, trend, df_e, df_h, news_flag, adx_v, rsi_v, vol_ratio, clean_path)
            extra         = cfg.extra_conviction_factors(df_e, df_h, stp, trend, adx_v, rsi_v)
            conv          = max(0, min(100, conv))  # Wave 60: heuristic extras logged only (proven non-predictive)

            # Wave 7 Iron Robot Layer 1+2: setup-specific boost + per-market direction multiplier.
            # Data-backed adjustments from May 3 backtest of 55 closed trades.
            # See conviction_boosts.py and data/conviction_boosts.json for the rules.
            try:
                conv_pre_w7 = conv
                w7_breakdown = {"base": conv, "setup_boost": 0, "market_mult": 0, "final": conv, "applied_layers": []}  # Wave 60: W7 boost disabled (noise)
                if w7_breakdown.get("applied_layers"):
                    log.info(f"[{market}] [{entry_tf}] W7 conv adj: {conv_pre_w7} -> {conv} "
                             f"({', '.join(w7_breakdown['applied_layers'])})")
            except Exception as _w7e:
                log.warning(f"[{market}] W7 adjust_conviction failed (non-fatal): {_w7e}")
                w7_breakdown = {"base": conv, "setup_boost": 0, "market_mult": 0,
                                "final": conv, "applied_layers": []}
            # Wave 8 (May 3): attach w7_breakdown to setup dict so format_alert
            # can show the breakdown to Wayne. Otherwise the alert just shows
            # final conviction with no indication that Wave 7 modified it.
            stp["_w7_breakdown"] = w7_breakdown
            # Merge core breakdown with market-specific extras for full transparency
            bd_final = dict(bd_core)
            for _k, _v in (extra or {}).items():
                bd_final[f"extra_{_k}"] = _v
            bd_final["base"] = 15  # the starting base score in conviction_score (lowered 30→15 per BACKLOG #3)
            # Wave 7: include the Iron Robot adjustments in the breakdown so
            # /edge and the alert metadata can show exactly what was applied.
            try:
                if w7_breakdown.get("setup_boost"):
                    bd_final["w7_setup_boost"] = w7_breakdown["setup_boost"]
                if w7_breakdown.get("market_mult"):
                    bd_final["w7_market_mult"] = w7_breakdown["market_mult"]
                if w7_breakdown.get("applied_layers"):
                    bd_final["w7_layers"] = ",".join(w7_breakdown["applied_layers"])
            except Exception:
                pass
            bd_final["final_score"] = conv
            # Wave 143 (_WAVE143_LEARNED_OVERRIDES): the conviction minimum is
            # learnable within Wayne's signed floor (44). CRITICAL: the gate is
            # secretly TWO gates - the tier ladder below independently assigns
            # REJECT under the same 48, so a naive hook on the fire line alone
            # would be a fake loosening (tier=="REJECT" still blocks). The
            # variable is hoisted ABOVE the ladder and both use it, so a
            # learned 46 genuinely fires as LOW tier. HIGH/MEDIUM cuts (60/53)
            # are untouched - only the floor between LOW and REJECT moves.
            _WAVE60_MIN_CONV = _w143_get("conv_min", market, stp["type"], 48)
            if   conv>=60: tier="HIGH"   # Wave 60: tiers on the evidence (win-rate) scale
            elif conv>=53: tier="MEDIUM"
            elif conv>=_WAVE60_MIN_CONV: tier="LOW"
            else:          tier="REJECT"
            if tier == "LOW":
                _w143_mark_via(stp, "conv_min", market, conv, 48)
            if tier=="REJECT" or conv < _WAVE60_MIN_CONV:
                decision = sl.DECISION_ALMOST if conv >= _WAVE60_MIN_CONV-5 else sl.DECISION_REJECTED
                _conv_reason = (
                    f"Conviction {conv} below {_WAVE60_MIN_CONV} minimum (tier={tier}); gap: {_WAVE60_MIN_CONV - conv} points"
                    if decision == sl.DECISION_REJECTED else
                    f"Conviction {conv} just short of {_WAVE60_MIN_CONV} minimum by {_WAVE60_MIN_CONV - conv} points — ALMOST"
                )
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    decision, _conv_reason,
                    context=snapshot_context,
                    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio),
                    score_breakdown=bd_final,
                    confidence_factors=_build_confidence_factors(snapshot_context, trend, adx_v, rsi_v))
                _sample_reject_log(market, entry_tf, stp["type"], _conv_reason)
                continue

            # May 1 news-window tightening: when news_flag is True, raise the
            # conviction floor by 10. The 15m timeframe is already blocked above
            # (entry_tf=="15m" and news_flag continues), so this primarily affects
            # 1h/4h setups during news. Born of the BTC SHORT loss pattern from
            # Apr 30 — the bot fired multiple shorts during news windows when
            # volatility spikes can fake out structural setups.
            if news_flag:
                _news_floor = 58  # Wave 60: 48 + 10 on the win-rate scale
                if conv < _news_floor:
                    sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                        cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
                        trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                        sl.DECISION_REJECTED,
                        f"News-window: conviction {conv} below tightened floor {_news_floor} (base {cfg.MIN_CONVICTION} + 10) — high-impact news active, requiring stronger signal",
                        context=snapshot_context,
                        detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio),
                        score_breakdown=bd_final)
                    _sample_reject_log(market, entry_tf, stp["type"], f"news floor {conv}<{_news_floor}")
                    continue

            dd_pct = sim_risk.get("daily_used_pct", 0)
            if dd_pct > 75:
                if conv < 90:
                    sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                        cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
                        trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                        sl.DECISION_REJECTED,
                        f"Daily drawdown {dd_pct:.0f}% requires conviction >=90, got {conv} — protecting account",
                        context=snapshot_context,
                        detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio),
                        score_breakdown=bd_final)
                    _sample_reject_log(market, entry_tf, stp["type"], f"DD>75% needs conv90, got {conv}")
                    continue
            elif dd_pct > 50:
                if conv < 80:
                    sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                        cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
                        trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                        sl.DECISION_REJECTED,
                        f"Daily drawdown {dd_pct:.0f}% requires conviction >=80, got {conv} — cautious mode",
                        context=snapshot_context,
                        detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio),
                        score_breakdown=bd_final)
                    _sample_reject_log(market, entry_tf, stp["type"], f"DD>50% needs conv80, got {conv}")
                    continue

            lev = risk_pct = hold = None
            if market in ("BTC","SOL"):
                lev_cap = cfg.LEVERAGE_BY_TIER.get(tier,5)
                # Wave 22: pass regime for trending-aware leverage scaling
                _w22_regime = snapshot_context.get("regime", "UNKNOWN") if isinstance(snapshot_context, dict) else "UNKNOWN"
                lev, risk_pct = ot.suggest_leverage(tier, stp["entry"], stp["raw_stop"], SETTINGS["account_risk_pct"], regime=_w22_regime)
                lev = min(lev, lev_cap)
            hold = ot.suggest_hold(market, entry_tf, tier)  # Wave 135 (_WAVE135_HOLD_TIME_TRUTH): tf+market truth, not tier

            # Apr 30 dup-guard: block same market+setup+direction firing within 10 min.
            # Prevents the BTC SHORT BREAK_RETEST_BEAR x2 in 2 minutes loss pattern.
            if _recent_fire_blocked(market, stp["type"], stp["direction"]):
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    sl.DECISION_REJECTED,
                    f"Dup-guard: {market} {stp['type']} {stp['direction']} fired within last {_RECENT_FIRE_WINDOW_MIN} min — blocking duplicate",
                    context=snapshot_context,
                    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio),
                    score_breakdown=bd_final)
                log.info(f"[{market}] [{entry_tf}] DUP-GUARD blocked {stp['type']} {stp['direction']} (within {_RECENT_FIRE_WINDOW_MIN} min)")
                continue

            # May 1 broader dup-guard: block same market+direction (any setup) within 30 min.
            # Prevents the BTC SHORT $210 loss pattern (BREAK_RETEST_BEAR + APPROACH_RESIST
            # firing 2 min apart, different setup types but same direction).
            if _recent_direction_blocked(market, stp["direction"]):
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    sl.DECISION_REJECTED,
                    f"Dup-guard (direction): {market} {stp['direction']} — another short/long fired within last {_RECENT_DIRECTION_WINDOW_MIN} min — blocking same-direction stack",
                    context=snapshot_context,
                    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio),
                    score_breakdown=bd_final)
                log.info(f"[{market}] [{entry_tf}] DUP-GUARD-DIR blocked {stp['type']} {stp['direction']} (within {_RECENT_DIRECTION_WINDOW_MIN} min)")
                continue

            # Wave 8 (May 3): include Wave 7 breakdown fields so we can later
            # measure whether the boost actually changed outcomes. Without this
            # the auto-tune (Layer 5) flies blind and we lose the signal.
            # Wave 129 (_WAVE129_ONE_POSITION_PER_MARKET): re-check at FIRE
            # time. One market = one position - the scan-start already_in is
            # stale by now if an earlier setup in this same pass just fired,
            # and it never saw the sims at all.
            if _market_position_open(market):
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    sl.DECISION_REJECTED,
                    f"One-position guard (Wave 129): {market} already has an open position - blocking second entry",
                    context=snapshot_context,
                    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio),
                    score_breakdown=bd_final)
                log.info(f"[{market}] [{entry_tf}] W129 ONE-POSITION guard blocked {stp['type']} {stp['direction']} - position already open")
                continue

            if _w145_news_15m:
                # Wave 145a (_WAVE145_NEWS_TF_EVIDENCE): this 15m setup passed
                # EVERY live gate during a news window - including the news
                # conviction floor (+10). The old hard skip at the timeframe
                # loop hid these forever, unmeasured: 513 news-window scan rows
                # in the live log and not one of them 15m. Evidence phase:
                # nothing fires; the setup is logged with full levels so the
                # grader prices the blackout in WOULD_WIN / WOULD_LOSE. The
                # skip gets removed on that proof - never on a hunch (Law #1).
                try:
                    sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                        cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
                        trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                        "SHADOW_NEWS_TF",
                        "news-window 15m blackout: passed every live gate, would have fired",
                        context=snapshot_context,
                        detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio),
                        score_breakdown=bd_final)
                    log.info(f"[{market}] [15m] W145 NEWS-TF shadow: {stp['type']} {stp['direction']} passed all gates during news")
                except Exception as _w145e:
                    log.warning(f"W145 news shadow log failed: {_w145e}")
                continue

            _w7_for_log = w7_breakdown if "w7_breakdown" in dir() else {}
            alert_id = ot.log_alert({
                "market":market, "tf":entry_tf, "setup":stp["type"], "direction":stp["direction"],
                "entry":round(stp["entry"],4), "stop":round(stp["raw_stop"],4), "target":round(tgt,4),
                "rr":round(rr,2), "method":method, "trend_score":trend, "conviction":conv, "tier":tier,
                "leverage":lev or "", "suggested_hold":hold or "", "rsi":round(rsi_v,2),
                "atr":round(atr_v,4), "adx":round(adx_v,2), "htf_bias":htf_bias,
                "hour":datetime.now(timezone.utc).hour, "vol_ratio":round(vol_ratio,2), "news_flag":int(news_flag),
                # Wave 8: W7 transparency
                "w7_setup_boost": int(_w7_for_log.get("setup_boost", 0)) if _w7_for_log else 0,
                "w7_market_mult": int(_w7_for_log.get("market_mult", 0)) if _w7_for_log else 0,
                "w7_applied_layers": ",".join(_w7_for_log.get("applied_layers", [])) if _w7_for_log else "",
            })

            # Wave 72: Confluence Memory - record the full aligned signal stack for this entry (shadow; no firing impact)
            try:
                _log_confluence_stack(alert_id, market, entry_tf, stp, setups, snapshot_context, adx_v, rsi_v, vol_ratio)
            except Exception as _cm_e:
                try:
                    log.warning("confluence stack log failed: " + str(_cm_e))
                except Exception:
                    pass

            # Wave 78: Decision journal - full reasoning for this trade
            try:
                _log_decision_journal(alert_id, market, entry_tf, stp, setups, tgt, rr, method, conv, tier, trend, htf_bias, news_flag, snapshot_context, adx_v, rsi_v, vol_ratio, _w7_for_log)
            except Exception as _dj_e:
                try:
                    log.warning("decision journal log failed: " + str(_dj_e))
                except Exception:
                    pass

            # Wave 80: Time-of-day edge (shadow) - log the hour edge for this entry
            try:
                _log_time_context(alert_id, market, entry_tf)
            except Exception as _tc_e:
                try:
                    log.warning("time context log failed: " + str(_tc_e))
                except Exception:
                    pass

            # Wave 87 (Jul 3, 2026): THE MAP (shadow) - log the level-aware
            # target next to the live one; a silent checker scores which one
            # price actually reaches. Live targeting is UNCHANGED.
            try:
                _w87_lm = _build_level_map(market, frames, _now_et())
                _w87_mt, _w87_reason = _map_target(stp["direction"], stp["entry"],
                                                   stp["raw_stop"], tgt, _w87_lm)
                _log_target_map_shadow(alert_id, market, stp["direction"],
                                       round(stp["entry"], 4), round(stp["raw_stop"], 4),
                                       round(tgt, 4), _w87_mt, _w87_reason,
                                       {k: round(v, 4) for k, v in _w87_lm.items()})
            except Exception as _w87e:
                log.warning(f"W87 map shadow (non-fatal): {_w87e}")

            # Wave 143: a trade that exists only because a learned override
            # let it through gets tagged; the revert tracker judges the
            # override on these exact trades and undoes itself if they run
            # below break-even. Real accountability for every loosening.
            try:
                if stp.get("_w143_via"):
                    import learned_overrides as _lo143
                    _f143, _m143, _s143 = stp["_w143_via"]
                    _lo143.tag_trade(_f143, _m143, _s143, str(alert_id))
                    log.info(f"[{market}] W143 tagged trade {alert_id} via {_f143} override")
            except Exception as _w143te:
                log.warning(f"W143 tag_trade failed (non-fatal): {_w143te}")

            # Task 8: Increment daily trade counter
            DAILY_TRADE_COUNT += 1

            # Task 8: Check profit lock after trade fires
            try:
                sr_check = sim.check_risk_limits()
                if sr_check.get("daily_pnl", 0) >= PROFIT_LOCK_THRESHOLD and not DAILY_PROFIT_LOCKED:
                    DAILY_PROFIT_LOCKED = True
                    pnl_val = sr_check["daily_pnl"]
                    log.info(f"DAILY_PROFIT_LOCKED at +${pnl_val:,.2f}")
            except Exception:
                pass

            sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
                trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                sl.DECISION_FIRED, "",
                context=snapshot_context,
                detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio),
                score_breakdown=bd_final,
                confidence_factors=_build_confidence_factors(snapshot_context, trend, adx_v, rsi_v))

            # Pre-Batch 2026-04-20: If the (removed) daily loss gate would have
            # blocked this signal, write a shadow row so we can measure whether
            # blocking it would have been the right call. The signal fires normally.
            if _halt_would_fire:
                try:
                    sl.log_scan_decision(
                        market, entry_tf, stp["type"], stp["direction"],
                        cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
                        trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                        sl.DECISION_SHADOW_HALTED,
                        "would_have_been_halted_by_2loss_gate (ignored): fired anyway under Pre-Batch rules",
                        context=snapshot_context,
                        detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio),
                        score_breakdown=bd_final,
                        confidence_factors=_build_confidence_factors(snapshot_context, trend, adx_v, rsi_v))
                except Exception as e:
                    log.warning(f"Shadow-log SHADOW_HALTED failed (non-fatal): {e}")

            _mark_cooldown(market, stp["type"])
            _mark_recent_fire(market, stp["type"], stp["direction"])  # Apr 30 dup-guard

            if stp["type"] in ("APPROACH_SUPPORT","APPROACH_RESIST"):
                _mark_approach_active(market, stp["type"], stp["entry"])

            # Wave 79: The Read - compute this setup quality read for the alert
            try:
                _rd_al = sorted({s.get("type") for s in setups if s.get("direction") == stp.get("direction")})
                _rcs, _rce, _rcb = _confluence_score(_rd_al, market)
                _read = {"cscore": _rcs, "cexp": _rce}
                try:
                    _rts, _rte, _rtl = _time_edge(datetime.now(timezone.utc).hour)
                    _read["tlabel"] = _rtl; _read["texp"] = _rte
                except Exception:
                    pass
                # Wave 158 (_WAVE158_DIRECTION_READ): tag the alert with the
                # current per-market side edge. Display only - never gates.
                try:
                    _rds, _rde, _rdl = _direction_edge(market, stp.get("direction"))
                    _read["dlabel"] = _rdl; _read["dexp"] = _rde
                except Exception:
                    pass
                stp["_read"] = _read
            except Exception:
                pass
            footer = cfg.alert_footer(stp, session)
            # Wave 90: parole fires say so on their face - honest UI
            if stp.get("_w89_partner"):
                _w90_badge = "\U0001f393 Probation fire \u2014 partner: " + str(stp["_w89_partner"]).replace("_", " ")
                footer = (footer + "\n" + _w90_badge) if footer else _w90_badge
            await tg_send(app, format_alert(market, entry_tf, stp, conv, tier, trend,
                                             tgt, rr, method, adx_v, rsi_v, lev, risk_pct, hold,
                                             extra_footer=footer, alert_id=alert_id))
            log.info(
                f"[{market}] [{entry_tf}] FIRED: {stp['type']} {stp['direction']} "
                f"Conv:{conv}/{tier} RR:{round(rr,2)} Entry:{round(stp['entry'],4)} "
                f"Stop:{round(stp['raw_stop'],4)} Target:{round(tgt,4)} "
                f"Trend:{trend:+d} HTF:{htf_bias} ADX:{adx_v:.1f} RSI:{rsi_v:.1f} "
                f"vol:{vol_ratio:.2f} shadow_halt:{_halt_would_fire}"
            )

    # Pre-Batch 2026-04-20: Per-market scan summary for readability (Task 3.1)
    # Uses locals().get() because some variables may not exist if we early-returned.
    try:
        _l = locals()
        _setups = _l.get('setups', [])
        _entry_tf = _l.get('entry_tf', '?')
        _trend = _l.get('trend', 0)
        _htf_bias = _l.get('htf_bias', '?')
        _adx_v = _l.get('adx_v', 0.0)
        _rsi_v = _l.get('rsi_v', 0.0)
        _vol_ratio = _l.get('vol_ratio', 0.0)
        log.info(
            f"[{market}] SCAN_SUMMARY tf={_entry_tf} trend={_trend:+d} htf={_htf_bias} "
            f"adx={float(_adx_v):.1f} rsi={float(_rsi_v):.1f} vol_ratio={float(_vol_ratio):.2f} "
            f"detected={len(_setups) if _setups else 0} halt_pending={DAILY_LOSS_GATE}"
        )
    except Exception:
        pass

# ── Market session rules ──────────────────────────────────────────
FUTURES_MARKETS = {"NQ", "GC"}
# Pre-Batch Follow-up Part A 2026-04-20: Topstep-accurate timing.
# No new NQ/GC entries 3:30-4:10 PM ET, force-flatten at 4:10 PM, reopen 6:00 PM.
# Crypto (BTC, SOL) UNAFFECTED — runs 24/7.
FUTURES_NOTRADE_START_ET = (15, 30)   # 3:30 PM — stop accepting new futures entries
FUTURES_FLAT_BY_ET       = (16, 10)   # 4:10 PM — force-close all open futures positions
FUTURES_REOPEN_ET        = (18, 0)    # 6:00 PM — futures trading reopens

# Backward-compat (some code may still reference these)
FUTURES_CLOSE_ET   = (16, 5)    # 4:05 PM (was 3:55 PM)
FUTURES_CLOSED_ET  = (16, 10)   # 4:10 PM (was 4:00 PM)

def _futures_session_ok(market: str) -> bool:
    """
    True if NQ/GC is open for new entries.
    Topstep: no new entries 3:30-4:10 PM ET, reopens 6:00 PM ET.
    Crypto (BTC, SOL) always True — 24/7.
    """
    if market not in FUTURES_MARKETS:
        return True
    now = _now_et()
    hm  = now.hour * 60 + now.minute
    notrade_start = FUTURES_NOTRADE_START_ET[0] * 60 + FUTURES_NOTRADE_START_ET[1]
    reopen        = FUTURES_REOPEN_ET[0]        * 60 + FUTURES_REOPEN_ET[1]
    return not (notrade_start <= hm < reopen)

def _crypto_session_ok(market: str) -> bool:
    """
    Audit Finding #7 / BACKLOG #3 (2026-04-28): block crypto entries
    2:00-5:00 AM ET. April 14 sample: 8 trades fired in this window,
    7 lost (87.5% loss rate). Thinnest-liquidity crossover between
    Asia close and London open.
    """
    if market not in ("BTC", "SOL"):
        return True
    hm = _now_et().hour * 60 + _now_et().minute
    return not (120 <= hm < 300)  # 2:00 to 5:00 AM ET

# ---- Wave 88 (Jul 3, 2026): BENCH RETRIAL - score suspended setups in shadow ----
BENCH_SHADOW_PATH = os.path.join(BASE_DIR, "data", "bench_shadow.jsonl")
_W88_SL_CACHE = {"mtime": None, "rows": []}

def _w88_recent_bench_rows():
    """Wave 88: read REJECTED_SUSPENDED rows (Task-4 shadow logs) from
    data/strategy_log.csv, last 5 days only (bar coverage). Cached by file
    mtime so the 26MB log is parsed at most once per change."""
    path = os.path.join(BASE_DIR, "data", "strategy_log.csv")
    try:
        mt = os.path.getmtime(path)
    except Exception:
        return []
    if _W88_SL_CACHE["mtime"] == mt:
        return _W88_SL_CACHE["rows"]
    rows = []
    try:
        import csv as _csv
        cutoff = datetime.now(timezone.utc) - timedelta(days=5)
        with open(path, encoding="utf-8", errors="replace") as f:
            for r in _csv.DictReader(f):
                if r.get("decision") != "REJECTED_SUSPENDED":
                    continue
                try:
                    ts = datetime.fromisoformat(str(r.get("timestamp", "")).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        continue
                except Exception:
                    continue
                rows.append(r)
    except Exception as e:
        log.warning(f"W88 bench-row read failed (non-fatal): {e}")
    _W88_SL_CACHE["mtime"] = mt
    _W88_SL_CACHE["rows"] = rows
    return rows

def _collect_bench_shadows(market):
    """Wave 88: turn recent benched detections into pending shadow trials.
    Uniform yardstick: target = entry +/- 2R (judges SIGNAL quality, not
    target luck). Dedupe key = timestamp|setup|tf. Data-only, never raises."""
    try:
        rows = [r for r in _w88_recent_bench_rows() if r.get("market") == market]
        if not rows:
            return
        seen = set()
        if os.path.exists(BENCH_SHADOW_PATH):
            with open(BENCH_SHADOW_PATH, encoding="utf-8") as f:
                for ln in f:
                    try:
                        seen.add(json.loads(ln).get("bench_id"))
                    except Exception:
                        continue
        out = []
        for r in rows:
            bid = f"{r.get('timestamp')}|{r.get('setup_type')}|{r.get('tf')}"
            if bid in seen:
                continue
            try:
                entry = float(r.get("entry")); stp_ = float(r.get("stop"))
                risk = abs(entry - stp_)
                if risk <= 0:
                    continue
                is_long = "LONG" in str(r.get("direction", ""))
                tgt = entry + 2 * risk if is_long else entry - 2 * risk
                out.append({"kind": "pending", "bench_id": bid, "market": market,
                            "setup": r.get("setup_type"), "tf": r.get("tf"),
                            "direction": r.get("direction"), "entry": entry,
                            "stop": stp_, "target_2r": round(tgt, 4),
                            "ts": r.get("timestamp")})
            except Exception:
                continue
        if out:
            os.makedirs(os.path.dirname(BENCH_SHADOW_PATH), exist_ok=True)
            with open(BENCH_SHADOW_PATH, "a", encoding="utf-8") as f:
                for rec in out:
                    f.write(json.dumps(rec) + "\n")
            log.info(f"W88 bench: {len(out)} new shadow trial(s) for {market}")
    except Exception as e:
        log.warning(f"W88 bench collect failed (non-fatal): {e}")

def _check_bench_shadows(market, frames):
    """Wave 88: walk 15m bars since each benched signal - did 2R or the stop
    come first? Verdicts BENCH_2R_FIRST / BENCH_STOP_FIRST / BENCH_NEITHER_36H
    with bar counts. Suspensions UNCHANGED - evidence only, for the parole
    review. Data-only, never sends Telegram."""
    try:
        if not os.path.exists(BENCH_SHADOW_PATH):
            return
        df = frames.get("15m")
        if df is None or len(df) == 0:
            return
        pend, done = [], set()
        with open(BENCH_SHADOW_PATH, encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                if r.get("kind") == "resolved":
                    done.add(r.get("bench_id"))
                elif r.get("kind") == "pending":
                    pend.append(r)
        out = []
        for r in pend:
            if r.get("market") != market or r.get("bench_id") in done:
                continue
            try:
                t0 = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00"))
                if t0.tzinfo is None:
                    t0 = t0.replace(tzinfo=timezone.utc)
                is_long = "LONG" in str(r.get("direction", ""))
                tgt = float(r["target_2r"]); stp_ = float(r["stop"])
                try:
                    sub = df[df.index > t0]
                except Exception:
                    sub = df[df.index > t0.replace(tzinfo=None)]
                verdict, bars = None, 0
                for i in range(len(sub)):
                    hi = float(sub["High"].iloc[i]); lo = float(sub["Low"].iloc[i])
                    bars = i + 1
                    if is_long:
                        if hi >= tgt: verdict = "BENCH_2R_FIRST"; break
                        if lo <= stp_: verdict = "BENCH_STOP_FIRST"; break
                    else:
                        if lo <= tgt: verdict = "BENCH_2R_FIRST"; break
                        if hi >= stp_: verdict = "BENCH_STOP_FIRST"; break
                if verdict is None:
                    age_h = (datetime.now(timezone.utc) - t0).total_seconds() / 3600.0
                    if age_h >= 36:
                        verdict = "BENCH_NEITHER_36H"
                if verdict:
                    out.append({"kind": "resolved", "bench_id": r.get("bench_id"),
                                "market": market, "setup": r.get("setup"),
                                "direction": r.get("direction"), "verdict": verdict,
                                "bars_15m": bars,
                                "ts": datetime.now(timezone.utc).isoformat()})
            except Exception:
                continue
        if out:
            with open(BENCH_SHADOW_PATH, "a", encoding="utf-8") as f:
                for r in out:
                    f.write(json.dumps(r) + "\n")
                    log.info(f"W88 bench resolved: {r['market']} {r['setup']} -> {r['verdict']} ({r['bars_15m']} bars)")
    except Exception as e:
        log.warning(f"W88 bench check failed (non-fatal): {e}")


# ---- Wave 93 (Jul 6, 2026): DATA LIFEBOAT - rotate strategy_log so it always syncs ----
# Root cause (Jul 6 audit): auto_sync silently skips any file over 25 MiB, and
# Railway wipes its filesystem to the repo copy on every deploy - so once
# data/strategy_log.csv passed 25 MiB (Jul 3) every row written between deploys
# was lost (scan decisions, benched-signal rows, the parole ledger). This
# rotates the live log into dated archive PARTS (each well under the 25 MiB
# sync cap) and starts a fresh header-only live file, so the live log never
# exceeds the cap again and auto_sync - which rglobs data/ including
# data/archive/ - preserves everything. Cheap size check each scan cycle;
# only acts when the file crosses the threshold. Never raises.
STRATEGY_LOG_ROTATE_MB = 15      # rotate once the live log passes this
STRATEGY_LOG_PART_MB = 18        # keep each archive part under the 25 MiB sync cap

# Wave 105 (_WAVE105_DATA_LOG_ROTATE): generalize Wave 93 rotation to other
# append-only JSONL logs so none can grow past auto_sync's 25 MB skip cap and
# silently stop syncing (the data loss that bit strategy_log.csv). Each listed
# file is write-only -- appended for later analysis, never read back for live
# logic (verified in-code) -- so archiving + truncating is lossless. Extend the
# list ONLY with files confirmed write-only. os.replace is atomic; a racing
# append recreates the fresh empty file, so no line is ever lost.
_ROTATE_DATA_LOGS = [
    ("watch_alerts_suppressed.jsonl", 15),
    ("intermarket_tape.jsonl", 15),  # Wave 108: tape snapshots (tiny, but insured)
]


# Wave 121 (_WAVE121_STATE_SIZE_SENTINEL): early-warning size sentinel for
# STATE files (not append logs -- those rotate). crypto_sim.json once grew
# unbounded and silently slowed the bot for days (fixed by Wave 100). This
# checks each small state file once/hour and Telegrams Wayne if any exceeds
# a generous cap, so the NEXT unbounded-growth bug is caught the day it
# starts. Pure observability: it never trims or writes anything.
_LAST_STATE_SIZE_CHECK = 0.0
# (filename in data/, cap in KB). Caps are generous -- these files are
# normally well under 50KB, so a warning means real abnormal growth.
_STATE_FILES_TO_WATCH = [
    ("crypto_sim.json", 500),
    ("sim_account.json", 500),
    ("scanner_state.json", 250),
    ("lifetime_stats.json", 500),
    ("cooldowns.json", 250),
    ("active_setups.json", 250),
    ("zone_lockouts.json", 250),
    ("family_cooldowns.json", 250),
    ("sim_history.json", 1000),
]


async def _check_state_file_sizes(app):
    """Once/hour, alert if any watched state file exceeds its cap. Never raises."""
    global _LAST_STATE_SIZE_CHECK
    try:
        import time as _t
        _now = _t.time()
        if _now - _LAST_STATE_SIZE_CHECK < 3600:
            return
        _LAST_STATE_SIZE_CHECK = _now
        _bloated = []
        for _fname, _cap_kb in _STATE_FILES_TO_WATCH:
            try:
                _fp = os.path.join(BASE_DIR, "data", _fname)
                if not os.path.exists(_fp):
                    continue
                _kb = os.path.getsize(_fp) / 1024.0
                if _kb > _cap_kb:
                    _bloated.append("%s %.0fKB (cap %dKB)" % (_fname, _kb, _cap_kb))
            except Exception:
                continue
        if _bloated:
            await tg_send(app, "DATA ALERT: state file(s) bloating: " + "; ".join(_bloated) + ". Likely unbounded growth -- tell FABEL to add an array cap (like Wave 100).")
    except Exception as _sse:
        log.debug("state-size sentinel failed: %s" % _sse)


def _rotate_data_logs():
    for _name, _mb in _ROTATE_DATA_LOGS:
        try:
            _p = os.path.join(BASE_DIR, "data", _name)
            if not os.path.exists(_p):
                continue
            if os.path.getsize(_p) <= _mb * 1024 * 1024:
                continue
            _adir = os.path.join(BASE_DIR, "data", "archive")
            os.makedirs(_adir, exist_ok=True)
            _stem, _ext = os.path.splitext(_name)
            _ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            _dest = os.path.join(_adir, "%s_%s%s" % (_stem, _ts, _ext))
            os.replace(_p, _dest)
            open(_p, "a", encoding="utf-8").close()
            log.info("Wave 105: rotated %s (%.1f MB) to archive/%s" % (_name, os.path.getsize(_dest) / (1024.0 * 1024.0), os.path.basename(_dest)))
        except Exception as _rot_err:
            log.warning("Wave 105: rotate of %s failed: %s" % (_name, _rot_err))


def _rotate_strategy_log():
    try:
        path = os.path.join(BASE_DIR, "data", "strategy_log.csv")
        if not os.path.exists(path):
            return
        size = os.path.getsize(path)
        if size <= STRATEGY_LOG_ROTATE_MB * 1024 * 1024:
            return
        archive_dir = os.path.join(BASE_DIR, "data", "archive")
        os.makedirs(archive_dir, exist_ok=True)
        limit = STRATEGY_LOG_PART_MB * 1024 * 1024
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            header = f.readline()
            if not header:
                return
            part = 1
            ppath = os.path.join(archive_dir, "strategy_log_" + stamp + "_part%02d.csv" % part)
            out = open(ppath, "w", encoding="utf-8")
            out.write(header)
            written = len(header.encode("utf-8"))
            for line in f:
                lb = len(line.encode("utf-8"))
                if written + lb > limit:
                    out.close()
                    part += 1
                    ppath = os.path.join(archive_dir, "strategy_log_" + stamp + "_part%02d.csv" % part)
                    out = open(ppath, "w", encoding="utf-8")
                    out.write(header)
                    written = len(header.encode("utf-8"))
                out.write(line)
                written += lb
            out.close()
        with open(path, "w", encoding="utf-8") as f:
            f.write(header)
        log.info("W93 rotated strategy_log.csv (%d bytes) into %d archive part(s) [%s]" % (size, part, stamp))
    except Exception as _w93e:
        log.warning("W93 strategy_log rotation failed (non-fatal): %s" % _w93e)


# ---- Wave 89 (Jul 3, 2026): PAROLE - the earned way back for benched setups ----
# Evidence: Jul 3 crypto bench back-trial (3,048 benched signals replayed vs
# Coinbase 15m history, uniform 2R yardstick, breakeven 33.4%). Parole class =
# ONLY candidates with n>=100 decided trials and positive 2R expectancy:
#   BTC:BREAK_RETEST_BULL   40.5% @454   BTC:VWAP_REJECT_BEAR  36.2% @398
#   BTC:BB_REVERSION_BULL   41.7% @228   BTC:APPROACH_RESIST   69.9% @166
#   SOL:APPROACH_RESIST     87.9% @124   BTC:STOCH_REVERSAL_BULL 40.3% @124
#   BTC:BREAK_RETEST_BEAR   35.8% @106
# Small-n candidates (RSI_DIV_BEAR 100% @17 etc.) stay benched until W88's
# fresh shadow record confirms them. Futures setups wait for W88 evidence.
PAROLE_SETUPS = {
    "BTC:BREAK_RETEST_BULL", "BTC:VWAP_REJECT_BEAR", "BTC:BB_REVERSION_BULL",
    "BTC:APPROACH_RESIST", "SOL:APPROACH_RESIST", "BTC:STOCH_REVERSAL_BULL",
    "BTC:BREAK_RETEST_BEAR",
}
PAROLE_LEDGER_PATH = os.path.join(BASE_DIR, "data", "parole_ledger.jsonl")

def _parole_event(ev):
    try:
        os.makedirs(os.path.dirname(PAROLE_LEDGER_PATH), exist_ok=True)
        ev["ts"] = datetime.now(timezone.utc).isoformat()
        with open(PAROLE_LEDGER_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev) + "\n")
    except Exception:
        pass

def _parole_pass(market, stp, setups):
    """Wave 89: a paroled (back-trial-cleared) setup may fire DESPITE its
    suspension - but only with a CONFLUENCE PARTNER: another non-suspended
    setup in the same scan, same direction, different type (Wayne's rule:
    a benched setup earns its way back by combining with another strat).
    A parole pass still runs the FULL modern gauntlet afterwards (trend
    guard, conviction floors, cooldowns). Every decision is ledgered.
    Fails safe: any error -> False -> behaves exactly like before W89."""
    try:
        key = f"{market}:{stp.get('type')}"
        if key not in PAROLE_SETUPS:
            return False
        if not ot.is_setup_suspended(market, stp.get("type")):
            return False
        want = "SHORT" if "SHORT" in str(stp.get("direction", "")) else "LONG"
        partner = None
        for o in (setups or []):
            if o is stp or o.get("type") == stp.get("type"):
                continue
            odir = "SHORT" if "SHORT" in str(o.get("direction", "")) else "LONG"
            if odir != want:
                continue
            if ot.is_setup_suspended(market, o.get("type")):
                continue
            partner = o.get("type")
            break
        if partner:
            stp["_w89_partner"] = partner  # Wave 90: probation badge on the alert
            log.info(f"[{market}] W89 PAROLE fire: {stp.get('type')} (partner: {partner})")
            _parole_event({"event": "granted", "market": market,
                           "setup": stp.get("type"), "partner": partner,
                           "direction": want})
            return True
        _parole_event({"event": "denied_no_partner", "market": market,
                       "setup": stp.get("type"), "direction": want})
        return False
    except Exception as _w89e:
        log.warning(f"W89 parole gate failed safe (non-fatal): {_w89e}")
        return False


# ---- Wave 87 (Jul 3, 2026): THE MAP (shadow) - level-aware targets ----
TARGET_MAP_SHADOW_PATH = os.path.join(BASE_DIR, "data", "target_map_shadow.jsonl")

def _build_level_map(market, frames, now_et):
    """Wave 87: structural level map at scan time - session high/low,
    prior-day high/low, prior-week high/low, and a daily-ATR reachability
    budget. Wayne rule: scalp-realistic targets (hourly moves, not multi-day
    dreams). Every value best-effort; missing frames just mean fewer levels."""
    lm = {}
    try:
        d1 = frames.get("1d")
        if d1 is not None and len(d1) >= 2:
            lm["prior_day_high"] = float(d1["High"].iloc[-2])
            lm["prior_day_low"] = float(d1["Low"].iloc[-2])
            rng = (d1["High"] - d1["Low"]).tail(14)
            if len(rng) > 0:
                lm["daily_atr"] = float(rng.mean())
            if len(d1) >= 6:
                lm["prior_week_high"] = float(d1["High"].iloc[-6:-1].max())
                lm["prior_week_low"] = float(d1["Low"].iloc[-6:-1].min())
    except Exception:
        pass
    try:
        m15 = frames.get("15m")
        if m15 is not None and len(m15) > 0:
            ses_start_et = now_et.replace(hour=18, minute=0, second=0, microsecond=0)
            if now_et.hour < 18:
                ses_start_et = ses_start_et - timedelta(days=1)
            t0 = ses_start_et.astimezone(timezone.utc)
            try:
                sub = m15[m15.index >= t0]
            except Exception:
                sub = m15[m15.index >= t0.replace(tzinfo=None)]
            if len(sub) > 0:
                lm["session_high"] = float(sub["High"].max())
                lm["session_low"] = float(sub["Low"].min())
    except Exception:
        pass
    return lm

def _map_target(direction, entry, stop, live_target, level_map):
    """Wave 87: what THE MAP would aim at. (a) First obstacle: if a structural
    level sits between entry and the live swing target (and is at least 1R
    away), take the level - price has to fight through it anyway. (b)
    Reachability clip: if the pick is farther than 1x daily ATR, clip to the
    farthest in-reach level (or the ATR budget itself). Returns (target, reason)."""
    try:
        entry = float(entry); live_target = float(live_target)
        is_long = "LONG" in str(direction)
        names = ("session_high", "session_low", "prior_day_high", "prior_day_low",
                 "prior_week_high", "prior_week_low")
        lvls = [(k, float(level_map[k])) for k in names if k in level_map]
        risk = abs(entry - float(stop)) or 1e-9
        if is_long:
            between = sorted([(k, v) for k, v in lvls
                              if entry < v < live_target and (v - entry) >= risk],
                             key=lambda x: x[1])
        else:
            between = sorted([(k, v) for k, v in lvls
                              if live_target < v < entry and (entry - v) >= risk],
                             key=lambda x: -x[1])
        tgt, reason = live_target, "no_obstacle_keep_live"
        if between:
            tgt, reason = between[0][1], "first_obstacle:" + between[0][0]
        datr = float(level_map.get("daily_atr", 0) or 0)
        if datr > 0 and abs(tgt - entry) > datr:
            budget = entry + datr if is_long else entry - datr
            if is_long:
                inreach = sorted([(k, v) for k, v in lvls
                                  if entry < v <= budget and (v - entry) >= risk],
                                 key=lambda x: -x[1])
            else:
                inreach = sorted([(k, v) for k, v in lvls
                                  if budget <= v < entry and (entry - v) >= risk],
                                 key=lambda x: x[1])
            if inreach:
                tgt, reason = inreach[0][1], "atr_clip_to:" + inreach[0][0]
            else:
                tgt, reason = budget, "atr_clip_budget"
        return round(float(tgt), 4), reason
    except Exception as e:
        return None, f"map_error:{e}"

def _log_target_map_shadow(alert_id, market, direction, entry, stop,
                           live_target, map_target, reason, level_map):
    """Wave 87: pending shadow record at alert time. Data-only, never raises."""
    try:
        rec = {"kind": "pending", "alert_id": alert_id, "market": market,
               "direction": direction, "entry": entry, "stop": stop,
               "live_target": live_target, "map_target": map_target,
               "map_reason": reason, "levels": level_map,
               "ts": datetime.now(timezone.utc).isoformat()}
        os.makedirs(os.path.dirname(TARGET_MAP_SHADOW_PATH), exist_ok=True)
        with open(TARGET_MAP_SHADOW_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        log.info(f"W87 map shadow: {market} {alert_id} live={live_target} map={map_target} ({reason})")
    except Exception as e:
        log.warning(f"W87 map shadow log failed (non-fatal): {e}")

def _check_target_map_shadows(market, frames):
    """Wave 87: walk 15m bars since the alert - first-touch bar counts for
    map target / live target / stop. Resolve on stop hit, live-target hit,
    or 36h. Verdicts: MAP_HIT / LIVE_HIT / STOP_FIRST / NEITHER_36H.
    Recorded outcomes UNCHANGED - shadow data only."""
    try:
        if not os.path.exists(TARGET_MAP_SHADOW_PATH):
            return
        df = frames.get("15m")
        if df is None or len(df) == 0:
            return
        pend, done = [], set()
        with open(TARGET_MAP_SHADOW_PATH, encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                if r.get("kind") == "resolved":
                    done.add(r.get("alert_id"))
                elif r.get("kind") == "pending":
                    pend.append(r)
        out = []
        for r in pend:
            if r.get("market") != market or r.get("alert_id") in done:
                continue
            try:
                t0 = datetime.fromisoformat(r["ts"])
                is_long = "LONG" in str(r.get("direction", ""))
                mt = r.get("map_target")
                lt = float(r["live_target"]); stp_ = float(r["stop"])
                try:
                    sub = df[df.index > t0]
                except Exception:
                    sub = df[df.index > t0.replace(tzinfo=None)]
                b_map = b_live = b_stop = None
                for i in range(len(sub)):
                    hi = float(sub["High"].iloc[i]); lo = float(sub["Low"].iloc[i]); n = i + 1
                    if is_long:
                        if mt is not None and b_map is None and hi >= float(mt): b_map = n
                        if b_live is None and hi >= lt: b_live = n
                        if b_stop is None and lo <= stp_: b_stop = n
                    else:
                        if mt is not None and b_map is None and lo <= float(mt): b_map = n
                        if b_live is None and lo <= lt: b_live = n
                        if b_stop is None and hi >= stp_: b_stop = n
                    if b_stop is not None or b_live is not None:
                        break
                age_h = (datetime.now(timezone.utc) - t0).total_seconds() / 3600.0
                if b_stop is None and b_live is None and age_h < 36:
                    continue
                if b_map is not None and (b_stop is None or b_map < b_stop):
                    verdict = "MAP_HIT"
                elif b_live is not None and (b_stop is None or b_live < b_stop):
                    verdict = "LIVE_HIT"
                elif b_stop is not None:
                    verdict = "STOP_FIRST"
                else:
                    verdict = "NEITHER_36H"
                out.append({"kind": "resolved", "alert_id": r.get("alert_id"),
                            "market": market, "verdict": verdict,
                            "bars_to_map": b_map, "bars_to_live": b_live,
                            "bars_to_stop": b_stop, "map_reason": r.get("map_reason"),
                            "ts": datetime.now(timezone.utc).isoformat()})
            except Exception:
                continue
        if out:
            with open(TARGET_MAP_SHADOW_PATH, "a", encoding="utf-8") as f:
                for r in out:
                    f.write(json.dumps(r) + "\n")
                    log.info(f"W87 map resolved: {r['market']} {r['alert_id']} -> {r['verdict']} (map:{r['bars_to_map']} live:{r['bars_to_live']} stop:{r['bars_to_stop']})")
    except Exception as e:
        log.warning(f"W87 map shadow check failed (non-fatal): {e}")


# ---- Wave 86 (Jul 2, 2026): reliable 4:10 settle + after-close shadow ----
AFTERCLOSE_SHADOW_PATH = os.path.join(BASE_DIR, "data", "afterclose_shadow.jsonl")

def _futures_pre_cutoff(trades, now_et):
    """Wave 86: open NQ/GC trades opened BEFORE today's 16:10 ET cutoff.
    Evening-session entries (after the cutoff) belong to the NEW session and
    are NOT settle candidates until the next day's 4:10."""
    try:
        cut = now_et.replace(hour=16, minute=10, second=0, microsecond=0)
    except Exception:
        return [t for t in trades if t.get("market") in ("NQ", "GC")]
    out = []
    for t in trades:
        if t.get("market") not in ("NQ", "GC"):
            continue
        try:
            ts = datetime.fromisoformat(str(t.get("timestamp", "")).replace("Z", "+00:00"))
            if ts.astimezone(cut.tzinfo) < cut:
                out.append(t)
        except Exception:
            out.append(t)  # unparseable timestamp -> settle it (safe default)
    return out

def _shadow_log_settle(row, settle_price, settle_result):
    """Wave 86: after a 4:10 settle the official P&L is OVER (Wayne: combine
    rules), but we keep watching the idea 24/7 in shadow so no data is lost.
    Appends a pending entry; _check_afterclose_shadows() resolves it later.
    Data-only: never raises, never sends Telegram."""
    try:
        rec = {
            "kind": "pending",
            "alert_id": row.get("alert_id"),
            "market": row.get("market"),
            "direction": row.get("direction"),
            "setup": row.get("setup"),
            "entry": row.get("entry"),
            "stop": row.get("stop"),
            "target": row.get("target"),
            "settle_price": settle_price,
            "settle_result": settle_result,
            "settle_ts": datetime.now(timezone.utc).isoformat(),
        }
        os.makedirs(os.path.dirname(AFTERCLOSE_SHADOW_PATH), exist_ok=True)
        with open(AFTERCLOSE_SHADOW_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        log.info(f"W86 shadow: tracking {rec['market']} {rec['alert_id']} after close")
    except Exception as e:
        log.warning(f"W86 shadow log failed (non-fatal): {e}")

def _check_afterclose_shadows(market, frames):
    """Wave 86: resolve pending after-close shadows for this market by walking
    15m bars in ORDER since the settle: which came FIRST, target or stop?
    36h horizon. Recorded outcomes are UNCHANGED - shadow data only."""
    try:
        if not os.path.exists(AFTERCLOSE_SHADOW_PATH):
            return
        df = frames.get("15m")
        if df is None or len(df) == 0:
            return
        pend, resolved_ids = [], set()
        with open(AFTERCLOSE_SHADOW_PATH, encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                if r.get("kind") == "resolved":
                    resolved_ids.add(r.get("alert_id"))
                elif r.get("kind") == "pending":
                    pend.append(r)
        out = []
        for r in pend:
            if r.get("market") != market or r.get("alert_id") in resolved_ids:
                continue
            try:
                t0 = datetime.fromisoformat(r["settle_ts"])
                tgt = float(r["target"]); stp = float(r["stop"])
                direction = str(r.get("direction", ""))
                try:
                    sub = df[df.index > t0]
                except Exception:
                    sub = df[df.index > t0.replace(tzinfo=None)]
                verdict, bars = None, 0
                for i in range(len(sub)):
                    hi = float(sub["High"].iloc[i]); lo = float(sub["Low"].iloc[i])
                    bars = i + 1
                    if "LONG" in direction:
                        if hi >= tgt: verdict = "SHADOW_TARGET_FIRST"; break
                        if lo <= stp: verdict = "SHADOW_STOP_FIRST"; break
                    else:
                        if lo <= tgt: verdict = "SHADOW_TARGET_FIRST"; break
                        if hi >= stp: verdict = "SHADOW_STOP_FIRST"; break
                if verdict is None:
                    age_h = (datetime.now(timezone.utc) - t0).total_seconds() / 3600.0
                    if age_h >= 36:
                        verdict = "SHADOW_NEITHER_36H"
                if verdict:
                    out.append({"kind": "resolved", "alert_id": r.get("alert_id"),
                                "market": market, "verdict": verdict,
                                "bars_15m_after_close": bars,
                                "settle_result": r.get("settle_result"),
                                "ts": datetime.now(timezone.utc).isoformat()})
            except Exception:
                continue
        if out:
            with open(AFTERCLOSE_SHADOW_PATH, "a", encoding="utf-8") as f:
                for r in out:
                    f.write(json.dumps(r) + "\n")
                    log.info(f"W86 shadow resolved: {r['market']} {r['alert_id']} -> {r['verdict']} ({r['bars_15m_after_close']} bars)")
    except Exception as e:
        log.warning(f"W86 shadow check failed (non-fatal): {e}")


async def force_flatten_futures(app):
    trades = ot.load_open_trades()
    # Wave 86: settle only trades opened BEFORE today's 16:10 ET cutoff -
    # evening-session entries carry to the NEXT day's 4:10. Enables safe
    # catch-up settles after restarts without touching new-session trades.
    futures_trades = _futures_pre_cutoff(trades, _now_et())
    if not futures_trades:
        return

    await tg_send(app,
        "\U0001f514 *4:10 PM Rule \u2014 Settling Futures*\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "Closing all NQ and Gold positions now.\n"
        "Topstep rule: flat by 4:10 PM ET."
    )

    for row in futures_trades:
        market = row["market"]
        cfg    = get_market_config(market)
        try:
            cur = get_current_price(market)
            if not np.isfinite(cur):
                cur = float(row["entry"])
        except Exception:
            cur = float(row["entry"])

        entry_p = row.get("entry", "?")
        # Wave 44 (May 12, 2026): pre-initialize pts so the exception path
        # doesn't leave it undefined - prevents NameError on the
        # subsequent isinstance(pts, float) check.
        pts = None
        try:
            pts  = cur - float(entry_p)
            if "SHORT" in row.get("direction", ""): pts = -pts
            pts_str = f"+{round(pts,2)}" if pts>=0 else str(round(pts,2))
        except Exception: pts_str = "?"

        result = "WIN" if (pts > 0 if isinstance(pts, float) else False) else "LOSS"
        ot.update_result(row["alert_id"], result, 0, cur)
        ot.record_trade_result(market, row.get("setup",""), result)
        _shadow_log_settle(row, cur, result)  # Wave 86: keep watching in shadow
        # Batch 2A: Log outcome to strategy_log.csv
        try:
            ot._log_trade_outcome(row, result, cur)
        except Exception:
            pass

        icon = "✅" if result=="WIN" else "❌"
        await tg_send(app,
            f"{icon} *Force Closed — 4:10 PM Rule*\n"
            f"{cfg.EMOJI} *{_md(cfg.FULL_NAME)}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Setup: `{_md(row.get('setup',''))}` [{row.get('tf','')}]\n"
            f"Entry: `{entry_p}` -> Exit: `{round(cur,4)}`\n"
            f"Move: `{pts_str} pts`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Futures closed until 6:00 PM ET reopen."
        )
        log.info(f"[{market}] Force-closed at 4PM rule — exit {cur}")

    if sim.load_state().get("enabled"):
        for row in futures_trades:
            try:
                cur = get_current_price(row["market"])
                if not np.isfinite(cur):
                    cur = float(row["entry"])
                # Wave 44 (May 12, 2026): direction-aware WIN/LOSS.
                # Old code did `cur > entry = WIN` which is wrong for SHORTS.
                # A SHORT wins when price falls (cur < entry). Without this fix,
                # every SHORT force-closed at 4:10 PM was recorded with flipped
                # WIN/LOSS, corrupting sim wins/losses, per_setup_stats, and
                # lifetime_stats.
                _w44_direction = row.get("direction", "")
                _w44_entry = float(row["entry"])
                if "SHORT" in _w44_direction:
                    r = "WIN" if cur < _w44_entry else "LOSS"
                else:
                    r = "WIN" if cur > _w44_entry else "LOSS"
                closed = sim.close_sim_trade(row["alert_id"], cur, r)
                if closed:
                    pnl = closed.get("pnl", 0)
                    risk = sim.check_risk_limits()
                    pnl_sign = f"+${pnl:,.2f}" if pnl>=0 else f"-${abs(pnl):,.2f}"
                    await tg_send(app,
                        f"💰 *SIM Force Closed — 4:10 PM Rule*\n"
                        f"P&L: `{pnl_sign}` | Balance: `${risk['balance']:,.2f}`\n"
                        f"Today: `${risk['daily_pnl']:+,.2f}`"
                    )
            except Exception as e:
                log.warning(f"sim force-close {row.get('market')}: {e}")

async def watch_open_trades(app, frames_by_market):
    if not SETTINGS["rescore_on"]: return
    trades = ot.load_open_trades()
    if not trades: return
    # Wave 50 (May 13, 2026): prune heartbeat tracker for closed trades.
    _w50_open_ids = {row.get("alert_id", "") for row in trades}
    for _k in list(_LAST_HEARTBEAT.keys()):
        if _k not in _w50_open_ids:
            _LAST_HEARTBEAT.pop(_k, None)
    for row in trades:
        m = row["market"]
        cfg = get_market_config(m)
        frames = frames_by_market.get(m, {})
        if not frames: continue

        # Partial exit check
        try:
            entry_val = float(row.get("entry", 0))
            stop_val  = float(row.get("stop", 0))
            risk_dist = abs(entry_val - stop_val)
            df_check  = frames.get(row.get("tf","15m"))  # _WAVE69_WATCH_FIX
            if df_check is None:
                df_check = frames.get("15m")
            if df_check is not None and not df_check.empty and risk_dist > 0:
                cur_p = float(df_check["Close"].iloc[-1])
                direction_r = row.get("direction","")
                is_long_r = "LONG" in direction_r
                profit_dist = (cur_p - entry_val) if is_long_r else (entry_val - cur_p)
                partial_done = row.get("partial_exit_done", "") == "True"
                if profit_dist >= risk_dist and not partial_done:
                    ot.update_partial_exit(row["alert_id"])
                    # Wave 26 (May 11, 2026): Telegram "consider taking 50% off"
                    # message silenced. The bot still tracks partial-exit triggers
                    # via ot.update_partial_exit (so re-suggestion logic works)
                    # and now logs the event to bot_brain.jsonl for Claude analysis.
                    bot_brain_log("partial_exit_suggestion", {
                        "market":     market,
                        "alert_id":   row.get("alert_id"),
                        "setup":      row.get("setup"),
                        "direction":  row.get("direction", ""),
                        "tf":         row.get("tf"),
                        "trigger":    "1R_reached",
                        "current_price": round(cur_p, 4),
                        "entry":      round(entry_val, 4),
                        "suggestion": "take_50_pct_off_move_stop_to_breakeven",
                    })
        except Exception as e:
            log.debug(f"Partial exit check: {e}")

        r = ot.rescore_open_trade(row, frames, ot.in_news_window())
        if r["new_conviction"] is not None: ot.update_rescore(row["alert_id"], r["new_conviction"])
        # Wave 50 (May 13, 2026): heartbeat-or-HOLD. If rescore says HOLD
        # but 2+ hours have passed since last alert for this trade,
        # send a status update. Otherwise true HOLD = skip.
        if r["action"]=="HOLD":
            _w50_aid = row.get("alert_id", "")
            _w50_now = datetime.now(timezone.utc)
            _w50_last = _LAST_HEARTBEAT.get(_w50_aid)
            if _w50_last is None:
                # First time seen post-restart - seed from trade timestamp
                try:
                    _w50_last = datetime.fromisoformat(row.get("timestamp", ""))
                    if _w50_last.tzinfo is None:
                        _w50_last = _w50_last.replace(tzinfo=timezone.utc)
                except Exception:
                    _w50_last = _w50_now
                _LAST_HEARTBEAT[_w50_aid] = _w50_last
            _w50_elapsed = (_w50_now - _w50_last).total_seconds()
            if _w50_elapsed < _HEARTBEAT_INTERVAL_SEC:
                continue
            # Promote HOLD to heartbeat - keep going through the alert builder
            # with synthetic action that prints status info.
            r = {**r, "action": "HEARTBEAT",
                 "note": f"Status update ({int(_w50_elapsed/3600)}h since last alert). Conviction stable."}
            _LAST_HEARTBEAT[_w50_aid] = _w50_now
        else:
            # Real alert firing - record timestamp
            _LAST_HEARTBEAT[row.get("alert_id", "")] = datetime.now(timezone.utc)

        action    = r["action"]
        delta     = r["delta"]
        new_c     = r["new_conviction"]
        note      = r["note"]
        direction = row.get("direction","")
        old_conv  = new_c - delta if new_c is not None else 0

        cur_price = None
        df_tf = frames.get(row.get("tf","15m"))  # _WAVE69_WATCH_FIX
        if df_tf is None:
            df_tf = frames.get("15m")
        if df_tf is not None and not df_tf.empty:
            cur_price = float(df_tf["Close"].iloc[-1])

        try:    entry_p = float(row.get("entry", 0))
        except: entry_p = 0
        try:    stop_p = float(row.get("stop", 0))
        except: stop_p = 0
        try:    target_p = float(row.get("target", 0))
        except: target_p = 0

        dist_lines = ""
        if cur_price and entry_p and stop_p and target_p:
            is_long = "LONG" in direction
            if is_long:
                to_target = target_p - cur_price
                to_stop   = cur_price - stop_p
                move      = cur_price - entry_p
            else:
                to_target = cur_price - target_p
                to_stop   = stop_p - cur_price
                move      = entry_p - cur_price
            move_pct = (move / entry_p * 100) if entry_p else 0
            move_sign = f"+{round(move,2)}" if move >= 0 else str(round(move,2))
            pct_sign  = f"+{round(move_pct,2)}%" if move_pct >= 0 else f"{round(move_pct,2)}%"
            total_range = abs(target_p - stop_p) if target_p != stop_p else 1
            if is_long:
                progress = (cur_price - stop_p) / total_range
            else:
                progress = (stop_p - cur_price) / total_range
            progress = max(0.0, min(1.0, progress))
            filled = int(progress * 10)
            bar = "🟩" * filled + "⬜" * (10 - filled)
            dist_lines = (
                f"📍 Now: `{round(cur_price,2)}` | Entry: `{round(entry_p,2)}`\n"
                f"📐 Move: `{move_sign} pts` ({pct_sign})\n"
                f"🎯 To target: `{round(abs(to_target),2)}` | 🛑 To stop: `{round(abs(to_stop),2)}`\n"
                f"{bar} {'🎯' if progress > 0.7 else '🛑' if progress < 0.3 else '➡️'}\n"
            )

        if action == "HEARTBEAT":
            header = f"ℹ️ *{cfg.FULL_NAME} — Status Update*"
            detail = f"{note}\n" if note else f"Conviction stable at *{new_c}/100*. Trade still open.\n"
        elif action == "LET_RUN":
            header = f"🚀 *{cfg.FULL_NAME} — Conviction Rising*"
            detail = f"Conviction up to *{new_c}/100* (+{delta}). Market trending in our favor — hold your position.\n"
        elif action == "WARN":
            header = f"⚠️ *{cfg.FULL_NAME} — Conviction Dropping*"
            detail = f"Conviction dropping (*{old_conv}* to *{new_c}*). Price showing rejection. Consider tightening your stop.\n"
        # Wave 55 (May 13, 2026): SIM rotation on EXIT_SUGGEST.
        # When the rescore says "exit this position", the SIM should
        # actively close it at current price rather than holding to
        # target/stop. outcomes.csv (parallel ledger) keeps tracking
        # the original target/stop for analysis purposes.
        if r["action"] == "EXIT_SUGGEST":
            try:
                _w55_sim_state = sim.load_state()
                if _w55_sim_state.get("enabled"):
                    _w55_aid = row.get("alert_id", "")
                    _w55_open = _w55_sim_state.get("open_sim_trades", [])
                    _w55_match = next(
                        (t for t in _w55_open if t.get("alert_id") == _w55_aid),
                        None
                    )
                    if _w55_match is not None:
                        # Get current price from primary timeframe
                        _w55_tf = row.get("tf", "15m") or "15m"
                        _w55_df = frames.get(_w55_tf)  # _WAVE69_WATCH_FIX
                        if _w55_df is None:
                            _w55_df = frames.get("15m")
                        if _w55_df is not None and not _w55_df.empty:
                            _w55_cur = float(_w55_df["Close"].iloc[-1])
                            _w55_entry = float(_w55_match.get("entry", 0) or 0)
                            # Sanity check: price within 50% of entry
                            if (_w55_cur > 0 and _w55_entry > 0
                                    and 0.5 * _w55_entry < _w55_cur < 1.5 * _w55_entry):
                                _w55_dir = _w55_match.get("direction", "")
                                if "LONG" in _w55_dir:
                                    _w55_profit = _w55_cur > _w55_entry
                                else:
                                    _w55_profit = _w55_cur < _w55_entry
                                _w55_result = "WIN" if _w55_profit else "LOSS"
                                _w55_closed = sim.close_sim_trade(
                                    _w55_aid, _w55_cur, _w55_result
                                )
                                if _w55_closed:
                                    _w55_pnl = _w55_closed.get("pnl", 0.0)
                                    log.info(
                                        f"Wave 55: SIM rotated out {_w55_aid} "
                                        f"at {_w55_cur:.2f} ({_w55_result}, "
                                        f"P&L ${_w55_pnl:+.2f})"
                                    )
            except Exception as _w55_e:
                log.warning(f"Wave 55 SIM rotation failed: {_w55_e}")

        elif action == "EXIT_SUGGEST":
            header = f"🛑 *{cfg.FULL_NAME} — Consider Exiting*"
            detail = f"{cfg.FULL_NAME} position weakening (*{new_c}/100*). Price may be reversing — consider exiting early.\n"
        else:
            header = f"ℹ️ *{cfg.FULL_NAME} — Position Update*"
            detail = f"{note}\n" if note else ""

        # Wave 26 (May 11, 2026): mid-trade rescore Telegram alert silenced.
        # The bot still re-scores conviction every scan (rescore_open_trade)
        # and stores it via ot.update_rescore (dashboard surfaces it via
        # last_rescore_conviction column). This patch only stops Telegram noise.
        # Full event detail logged to bot_brain.jsonl for Claude analysis.
        bot_brain_log("rescore", {
            "market":           row.get("market"),  # Wave 96: was bare `market` (undefined) - NameError crashed watch_open_trades every rescore
            "alert_id":         row.get("alert_id"),
            "setup":            row.get("setup"),
            "direction":        direction,
            "tf":               row.get("tf"),
            "action":           action,
            "old_conviction":   old_conv,
            "new_conviction":   new_c,
            "delta":            delta,
            "current_price":    round(cur_price, 4) if cur_price else None,
            "entry":            round(entry_p, 4) if entry_p else None,
            "stop":             round(stop_p, 4) if stop_p else None,
            "target":           round(target_p, 4) if target_p else None,
            "progress_to_target_pct": round(progress * 100, 1) if "progress" in dir() else None,
            "note":             note,
        })

# ── Smart interval engine ─────────────────────────────────────────
def get_smart_interval(active_markets, frames_by_market) -> tuple:
    now_et  = _now_et()
    hour_et = now_et.hour
    minute  = now_et.minute
    now_m   = hour_et * 60 + minute

    WINDOWS = [
        ( 8*60+25,  9*60+0,  "Pre-8:30am data"),
        ( 9*60+25, 10*60+0,  "US market open"),
        ( 9*60+45, 10*60+30, "Open range"),
        (13*60+45, 14*60+20, "Fed/FOMC window"),
        (15*60+45, 16*60+15, "Market close"),
        ( 2*60+0,   3*60+0,  "London open"),
        (17*60+45, 18*60+30, "Futures reopen"),
    ]
    for (start, end, label) in WINDOWS:
        if start <= now_m <= end:
            return (0.5, f"🔴 {label} — 30s scan")

    best_vol = best_move = 0.0; hot_mkt = ""
    for m in active_markets:
        try:
            df = frames_by_market.get(m, {}).get("15m")
            if df is None or df.empty or len(df)<20: continue
            vol_last = float(df["Volume"].iloc[-1])
            vol_avg  = float(df["Volume"].rolling(20).mean().iloc[-1])
            if vol_avg > 0:
                vr = vol_last / vol_avg
                if vr > best_vol: best_vol = vr; hot_mkt = m
            candle = abs(float(df["High"].iloc[-1]) - float(df["Low"].iloc[-1]))
            atr_v  = float(ot.atr(df).iloc[-1])
            if atr_v > 0:
                mr = candle / atr_v
                if mr > best_move: best_move = mr
        except: continue

    if best_vol >= 3.0 or best_move >= 2.5:
        return (0.5, f"🔥 {hot_mkt} EXTREME ({round(best_vol,1)}x vol) — 30s")
    if best_vol >= 2.0 or best_move >= 1.8:
        return (1.0, f"⚡ {hot_mkt} HIGH ({round(best_vol,1)}x vol) — 1min")
    if best_vol >= 1.4 or best_move >= 1.3:
        return (2.0, f"🟡 {hot_mkt} elevated ({round(best_vol,1)}x vol) — 2min")

    base = SETTINGS["scan_interval_min"]
    if   8  <= hour_et <= 16: return (base,           "🟢 US session")
    elif 2  <= hour_et <= 8:  return (base,           "🇬🇧 London session")
    elif 17 <= hour_et <= 18: return (3,              "🔔 Futures reopen window")
    else:                      return (max(base, 10), "🌙 Quiet hours")

# ── Market bias / briefs ──────────────────────────────────────────
def build_startup_state():
    log.info("Building startup market state...")
    active = [m for m in ALL_MARKETS if SETTINGS["markets"].get(m)]
    lines = [
        "🤖 *Bot Online — Current Market State*",
        f"📅 {_now_et().strftime('%A, %B %d %I:%M %p ET')}",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for m in active:
        cfg = get_market_config(m)
        try:
            frames = get_frames(m)
            trend, _ = ot.trend_score(frames, m)
            if trend >= 5:    t_label = "Strong Bull"
            elif trend >= 2:  t_label = "Bullish"
            elif trend <= -5: t_label = "Strong Bear"
            elif trend <= -2: t_label = "Bearish"
            else:             t_label = "Neutral"
            t_emoji = "🟢" if trend >= 2 else "🔴" if trend <= -2 else "⚪"

            tf_parts = []
            for tf in ["15m", "1h", "4h"]:
                df = frames.get(tf)
                if df is not None and not df.empty and len(df) >= 20:
                    bias = ot.structure_bias(df)
                    b_icon = {"HH_HL": "🟢", "LH_LL": "🔴", "MIXED": "⚪"}.get(bias, "⚪")
                    b_label = {"HH_HL": "Bullish", "LH_LL": "Bearish", "MIXED": "Mixed"}.get(bias, "?")
                    tf_parts.append(f"{tf}: {b_icon} {b_label}")
                else:
                    tf_parts.append(f"{tf}: ⚫ N/A")

            df15 = frames.get("15m")
            price_str = ""
            if df15 is not None and not df15.empty:
                price_str = f" @ `{round(float(df15['Close'].iloc[-1]), 2)}`"

            lines.append(f"{cfg.EMOJI} *{cfg.FULL_NAME}*{price_str}")
            lines.append(f"  {t_emoji} Trend: *{trend:+d}* ({t_label})")
            lines.append(f"  {' | '.join(tf_parts)}")
            lines.append("")
        except Exception as e:
            log.warning(f"startup state {m}: {e}")
            lines.append(f"{cfg.EMOJI} *{cfg.FULL_NAME}* — data unavailable")
            lines.append("")

    open_trades = ot.load_open_trades()
    if open_trades:
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append(f"📋 *Open Trades:* {len(open_trades)}")
        for t in open_trades:
            lines.append(f"  • {t.get('market')} {t.get('direction')} | {t.get('setup')} [{t.get('tf')}]")

    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("🔭 _Scanner starting — watching all markets._")
    return "\n".join(lines)

def analyze_market_bias(market):
    try:
        frames = get_frames(market); results = {}
        for tf in ["4h","1h","15m"]:
            df = frames.get(tf)
            if df is None or len(df)<50: results[tf]=None; continue
            close=float(df.iloc[-1]["Close"]); e20=float(ot.ema(df["Close"],20).iloc[-1])
            e50=float(ot.ema(df["Close"],50).iloc[-1]); e200=float(ot.ema(df["Close"],200).iloc[-1])
            rsi_v=float(ot.rsi(df["Close"]).iloc[-1]); atr_v=float(ot.atr(df).iloc[-1])
            vwap_v=float(ot.vwap(df).iloc[-1])
            results[tf]={"close":close,"e20":e20,"e50":e50,"e200":e200,"rsi":rsi_v,"atr":atr_v,
                         "vwap":vwap_v,"swing_hi":float(df.iloc[-30:]["High"].max()),
                         "swing_lo":float(df.iloc[-30:]["Low"].min()),
                         "bull":close>e20>e50>e200,"bear":close<e20<e50<e200}
        bv=bv2=0; tfl=[]
        for tf in ["4h","1h","15m"]:
            r=results.get(tf)
            if not r: tfl.append(f"  • *{tf}:* unavailable"); continue
            w=2 if tf=="4h" else 1
            if r["bull"]:   bv+=w;  stack="full bullish EMA stack"
            elif r["bear"]: bv2+=w; stack="full bearish EMA stack"
            else:           stack="mixed structure"
            rd="overbought" if r["rsi"]>70 else "oversold" if r["rsi"]<30 else "neutral" if r["rsi"]<55 else "building"
            vd="above VWAP" if r["close"]>r["vwap"] else "below VWAP"
            tfl.append(f"  • *{tf}:* {stack}, RSI {round(r['rsi'],1)} ({rd}), {vd}")
        if bv>bv2+1:   bias,em="BULLISH","📈"
        elif bv2>bv+1: bias,em="BEARISH","📉"
        else:          bias,em="NEUTRAL","➡️"
        r1h=results.get("1h") or results.get("15m") or {}
        sup=r1h.get("swing_lo",0); res=r1h.get("swing_hi",0); px=r1h.get("close",0); at=r1h.get("atr",0)
        if bias=="BULLISH": exp=f"Expecting push toward `{res}`. Pullbacks to `{round(px-at,2)}` = long entry. Bulls above `{sup}`."
        elif bias=="BEARISH": exp=f"Expecting pressure toward `{sup}`. Bounces to `{round(px+at,2)}` = short entry. Bears below `{res}`."
        else: exp=f"No clear edge. Range `{sup}`—`{res}`. Wait for level break."
        return {"bias":bias,"emoji":em,"tf_lines":tfl,"expectation":exp,"support":sup,"resistance":res,"price":px}
    except Exception as e:
        log.error(f"analyze_market_bias {market}: {e}"); return None

def get_price_change(market):
    try:
        df = fetch_yfinance({"NQ":"NQ=F","GC":"GC=F","BTC":"BTC-USD","SOL":"SOL-USD"}[market], "1d")
        if df is None or len(df)<2: return None,None
        la=float(df["Close"].iloc[-1]); pr=float(df["Close"].iloc[-2])
        return la, round(((la-pr)/pr)*100,2)
    except: return None,None

def _price_lines(markets):
    lines=[]
    for key,name in markets:
        price,chg=get_price_change(key)
        if price and chg is not None:
            a="🟢" if chg>=0 else "🔴"
            lines.append(f"  {a} *{name}:* `{round(price,2)}` ({'+' if chg>=0 else ''}{chg}%)")
        else: lines.append(f"  ⚪ *{name}:* unavailable")
    return lines

def _bias_section(markets):
    lines=[]
    for key,name in markets:
        if not SETTINGS["markets"].get(key): continue
        a=analyze_market_bias(key)
        if not a: lines.append(f"*{name}:* unavailable\n"); continue
        lines+=[f"{a['emoji']} *{name} — {a['bias']}*",*a["tf_lines"],
                f"  📍 Support: `{a['support']}` | Resistance: `{a['resistance']}`",
                f"  💬 {a['expectation']}\n"]
    return lines

def build_morning_brief():
    log.info("Building morning brief...")
    now = _now_et()
    lines=[f"🌅 *GOOD MORNING — NQ CALLS*",f"📅 {now.strftime('%A, %B %d, %Y')} | US Session",
           f"━━━━━━━━━━━━━━━━━━",f"📊 *OVERNIGHT PRICES:*"]
    lines+=_price_lines([("NQ","NQ Futures"),("GC","Gold"),("BTC","Bitcoin"),("SOL","Solana")])
    lines+=[f"━━━━━━━━━━━━━━━━━━",f"📰 *KEY TIMES TODAY (EST):*",
            f"  🔴 8:30am — Data releases",f"  🟡 9:30am — US Market Open",
            f"  🟡 2:00pm — Fed / speakers",f"  🟡 4:00pm — Market Close",
            f"━━━━━━━━━━━━━━━━━━",f"🔭 *TODAY'S BIAS (15m · 1h · 4h):*\n"]
    lines+=_bias_section([("NQ","NQ Futures"),("GC","Gold"),("BTC","Bitcoin"),("SOL","Solana")])
    lines+=[f"━━━━━━━━━━━━━━━━━━",f"💡 *REMINDERS:*",
            f"  • Wait for your setup — don't force it",f"  • Respect the bias above",
            f"  • Extra caution at 8:30am",f"  • Bot scanning 24/7 — trust the alerts",
            f"━━━━━━━━━━━━━━━━━━",f"🤖 NQ CALLS Bot is watching. Lets get it."]
    return "\n".join(lines)

def build_asia_brief():
    log.info("Building Asia brief...")
    now = _now_et()
    lines=[f"🌙 *ASIA SESSION BRIEF — NQ CALLS*",f"📅 {now.strftime('%A, %B %d, %Y')} | 6pm EST",
           f"━━━━━━━━━━━━━━━━━━",f"🌏 *Crypto + Gold most active overnight.*",
           f"━━━━━━━━━━━━━━━━━━",f"📊 *CURRENT PRICES:*"]
    lines+=_price_lines([("BTC","Bitcoin"),("SOL","Solana"),("GC","Gold"),("NQ","NQ Futures")])
    lines+=[f"━━━━━━━━━━━━━━━━━━",f"🔭 *OVERNIGHT BIAS (15m · 1h · 4h):*\n"]
    lines+=_bias_section([("BTC","Bitcoin"),("SOL","Solana"),("GC","Gold")])
    lines+=[f"━━━━━━━━━━━━━━━━━━",f"💡 *OVERNIGHT TIPS:*",
            f"  • Crypto moves fast — respect stops",f"  • Low liquidity = bigger wicks",
            f"  • Bot on overnight watch",f"━━━━━━━━━━━━━━━━━━",
            f"🤖 NQ CALLS Bot on overnight watch. Stay safe."]
    return "\n".join(lines)

# ── Session boundary safety + daily report state (Task 3B/3C) ────
_LAST_SESSION_CLOSE_FIRED = None   # session_date string that was already closed
_W94_WATCH_FAILS = [0]   # Wave 94: consecutive watch_open_trades failures (list to avoid a global decl)
_LAST_DAILY_REPORT_DATE   = None   # date string for which report was sent
# Pre-Batch Follow-up Part B 2026-04-21: weekly recap deduplication
_LAST_WEEKLY_RECAP_DATE   = None   # Monday isoformat for which weekly recap was sent
_LAST_SCAN_TIMESTAMP      = None   # Wave 19: UTC timestamp of most recent completed scan cycle

# ── Scan loop ─────────────────────────────────────────────────────
# Wave 163 (_WAVE163_UNKILLABLE_LOOP): supervisor + strong task reference.
# _W163_SCAN_TASK holds the live task so (a) the GC can never collect it and
# (b) Wave 164 can see whether it is alive and relaunch it. The supervisor
# awaits scan_loop; if it returns or raises anything except CancelledError,
# it logs and relaunches after a short backoff - forever. CancelledError
# (a real shutdown) is allowed to propagate so we do not fight a deploy.
_W163_SCAN_TASK = [None]
_W163_RESTARTS = [0]
_W163_LAST_RESTART_AT = [None]


async def _w163_scan_supervisor(app):
    """Run scan_loop forever. Any exit other than a real shutdown is treated
    as a crash and recovered from. Never raises except on CancelledError."""
    import asyncio as _asyncio
    from datetime import datetime as _dt, timezone as _tz
    while True:
        try:
            await scan_loop(app)
            # scan_loop is an infinite loop; a plain return means something
            # broke out unexpectedly - treat it as a crash and restart.
            log.error("Wave 163: scan_loop returned unexpectedly - restarting.")
        except _asyncio.CancelledError:
            log.info("Wave 163: supervisor cancelled (shutdown) - stopping.")
            raise
        except BaseException as _sup_e:
            log.error("Wave 163: scan_loop crashed (%r) - restarting." % _sup_e)
        _W163_RESTARTS[0] += 1
        _W163_LAST_RESTART_AT[0] = _dt.now(_tz.utc)
        try:
            await _asyncio.sleep(5)  # brief backoff so a hard-fail cannot hot-spin
        except _asyncio.CancelledError:
            raise


async def scan_loop(app):
    global _FLATTEN_PENDING, _SESSION_CLOSE_SUMMARY, _SUSPENSION_CHANGES, _RECAP_PENDING
    global _LAST_SESSION_CLOSE_FIRED, _LAST_DAILY_REPORT_DATE, _LAST_WEEKLY_RECAP_DATE
    global _LAST_SCAN_TIMESTAMP  # Wave 19: powers /status "last scan" line
    last_brief=last_asia=last_report=None
    last_hb=datetime.now(timezone.utc)
    scan_interval = SETTINGS["scan_interval_min"]
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            now_et = _now_et()
            _rotate_strategy_log()  # Wave 93: keep the live log under the sync cap
            _rotate_data_logs()  # Wave 105: keep other append-only logs under the cap
            await _check_state_file_sizes(app)  # Wave 121: warn if a state file bloats
            # Wave 120 (_WAVE120_DROP_ALERT): surface any dropped write immediately
            # instead of losing data silently. get_dropped_writes()/reset come from
            # Wave 118 safe_io; guarded so a partial deploy cannot crash the loop.
            try:
                if hasattr(safe_io, "get_dropped_writes"):
                    _dw = safe_io.get_dropped_writes()
                    if _dw.get("count", 0) > 0:
                        await tg_send(app, "DATA ALERT: %d write(s) were dropped (last: %s). Investigate - data may be incomplete." % (_dw["count"], _dw.get("last_path", "?")))
                        if hasattr(safe_io, "reset_dropped_writes"):
                            safe_io.reset_dropped_writes()
            except Exception as _dwe:
                log.debug("drop-write check failed: %s" % _dwe)
            # Wave 108 (_WAVE108_INTERMARKET_TAPE): shadow-log the intermarket tape
            # read each cycle (throttled inside the module to ~1 line / 9 min).
            # Pure data collection for the tape-gate analysis -- never blocks or
            # fires trades; any failure is skipped silently.
            try:
                import intermarket as _im108
                _im108.log_snapshot()
            except Exception as _im_err:
                log.debug("intermarket snapshot skipped: %s" % _im_err)

            # Wave 7 Layer 5: Sunday 8 PM ET auto-tune. Wrapped in try/except
            # so a tune failure can never take down the scan loop.
            try:
                if cb.should_run_auto_tune_now():
                    log.info("Wave 7 Layer 5: triggering Sunday auto-tune")
                    tune_result = await asyncio.to_thread(cb.run_auto_tune)
                    n_changes = len(tune_result.get("changes", []))
                    n_analyzed = tune_result.get("n_setups_analyzed", 0)
                    summary_lines = [
                        "\U0001f527 *Wave 7 Auto-Tune (Sunday 8 PM)*",
                        "\u2501" * 16,
                        f"*Analyzed:* {n_analyzed} setups (last {tune_result.get('window_days', 28)}d)",
                        f"*Changes:* {n_changes}",
                    ]
                    for ch in tune_result.get("changes", [])[:10]:
                        icon = "\U0001f7e2" if ch["boost_after"] > ch["boost_before"] else "\U0001f534"
                        summary_lines.append(
                            f"  {icon} `{ch['setup']}` {ch['wr']:.0f}% WR "
                            f"({ch['trades']}t): {ch['boost_before']:+d} \u2192 {ch['boost_after']:+d}"
                        )
                    l3r = tune_result.get("l3_recalibration", {})
                    if l3r.get("changed"):
                        summary_lines.append("")
                        summary_lines.append(
                            f"*L3 Floor:* {l3r.get('floor_before', 0)} \u2192 {l3r.get('floor_after', 0)}"
                        )
                    await tg_send(app, "\n".join(summary_lines))
            except Exception as _autotune_err:
                log.warning(f"Wave 7 auto-tune failed (non-fatal): {_autotune_err}")

            # Wave 9 (May 4) Layer 7: Daily soft auto-tune at 6 AM ET.
            # Includes Layer 6 edge-decay check (zeros boosts on stale-edge setups).
            # Wrapped in try/except so a tune failure can never take down the scan loop.
            try:
                if cb.should_run_daily_soft_tune_now():
                    log.info("Wave 9 Layer 7: triggering daily soft auto-tune (6 AM ET)")
                    soft_result = await asyncio.to_thread(cb.run_daily_soft_tune)
                    soft_changes = soft_result.get("changes", [])
                    decay_actions = soft_result.get("decay", {}).get("decay_actions", [])
                    soft_lines = [
                        "\U0001f504 *Wave 9 Daily Soft Tune (6 AM ET)*",
                        "\u2501" * 16,
                        f"*Analyzed:* {soft_result.get('n_setups_analyzed', 0)} setups "
                        f"(last {soft_result.get('window_days', 7)}d)",
                        f"*Tune changes:* {len(soft_changes)}",
                        f"*Edge-decay actions:* {len(decay_actions)}",
                    ]
                    if decay_actions:
                        soft_lines.append("")
                        soft_lines.append("\U0001f6e1\ufe0f *Edge Decay (L6):*")
                        for da in decay_actions[:8]:
                            icon = ("\U0001f7e2" if da["action"] == "relaxed" else
                                    "\U0001f534" if da["action"] in ("zeroed", "penalized") else
                                    "\u26aa")
                            soft_lines.append(
                                f"  {icon} `{da['setup']}` {da['action']}: "
                                f"{da['boost_before']:+d}\u2192{da['boost_after']:+d} — {da['reason']}"
                            )
                    if soft_changes:
                        soft_lines.append("")
                        soft_lines.append("\U0001f3af *Soft Tune Changes (L7):*")
                        for ch in soft_changes[:8]:
                            icon = "\U0001f7e2" if ch["boost_after"] > ch["boost_before"] else "\U0001f534"
                            soft_lines.append(
                                f"  {icon} `{ch['setup']}` {ch['wr']:.0f}% WR "
                                f"({ch['trades']}t): {ch['boost_before']:+d} \u2192 {ch['boost_after']:+d}"
                            )
                    if not decay_actions and not soft_changes:
                        soft_lines.append("")
                        soft_lines.append("_No changes — boosts stable._")
                    await tg_send(app, "\n".join(soft_lines))
            except Exception as _softtune_err:
                log.warning(f"Wave 9 daily soft tune failed (non-fatal): {_softtune_err}")

            # Tick the session clock — fires events synchronously
            SESSION_CLOCK.tick(now_utc)

            # Task 3B: Safety net — if SessionClock missed the 4PM close window
            # (e.g. bot was restarted during it), fire it here exactly once per day.
            if now_et.hour >= 16 and now_et.weekday() < 5:
                # Session_date is now tomorrow (>=16:00); the closing session was yesterday
                closed_session_date = now_et.date().strftime("%Y-%m-%d")
                if _LAST_SESSION_CLOSE_FIRED != closed_session_date:
                    try:
                        from session_clock import SessionEvent
                        log.info(f"Session close safety net: firing for {closed_session_date}")
                        _on_session_close(SessionEvent.FUTURES_SESSION_CLOSE, now_et)
                        _LAST_SESSION_CLOSE_FIRED = closed_session_date
                    except Exception as e:
                        log.error(f"Session close safety net error: {e}")

            # Task 3C: Daily report scheduler — 8 PM ET, once per day
            if now_et.hour >= 20:
                today_str = now_et.date().strftime("%Y-%m-%d")
                report_path = os.path.join(BASE_DIR, "data", f"daily_report_{today_str}.txt")
                if _LAST_DAILY_REPORT_DATE != today_str and not os.path.exists(report_path):
                    try:
                        _full, short = ot.build_daily_report()
                        await tg_send(app, short)

                        # Wave 143 (_WAVE143_LEARNED_OVERRIDES): the HANDS run
                        # once a day at this same checkpoint. daily_decision()
                        # applies AT MOST one bounded loosening (2 consecutive
                        # days of proof, Wayne's signed floors and pace) and
                        # returns '' on silent days - no note means no change,
                        # the no-spam guarantee lives in the module itself.
                        # revert_check() judges every active override on the
                        # live trades it unlocked and undoes itself when wrong
                        # (1-day cooldown; 3 strikes -> a week; Wayne's terms).
                        try:
                            import learned_overrides as _lo143d
                            _w143_n1 = _lo143d.daily_decision()
                            if _w143_n1:
                                await asyncio.sleep(1)
                                await tg_send(app, _w143_n1)
                            _w143_n2 = _lo143d.revert_check(_w143_outcome_lookup)
                            if _w143_n2:
                                await asyncio.sleep(1)
                                await tg_send(app, _w143_n2)
                        except Exception as _w143de:
                            log.error(f"Wave 143 hands failed (non-fatal): {_w143de}")

                        # Wave 54 (May 13, 2026): generate weekly self-review.
                        # Runs piggy-backed on the daily report trigger so it
                        # uses the same 8 PM ET schedule. File overwritten daily
                        # with current date so Wayne always sees latest version.
                        try:
                            _wr_md = ot.build_weekly_review()
                            _wr_path = os.path.join(BASE_DIR, "data",
                                f"weekly_review_{today_str}.md")
                            with open(_wr_path, "w", encoding="utf-8") as _wrf:
                                _wrf.write(_wr_md)
                            await asyncio.sleep(1)
                            await tg_send(app,
                                f"Weekly self-review saved: `weekly_review_{today_str}.md`. "
                                f"Auto-syncs to GitHub within the hour.")
                            log.info(f"Wave 54: weekly review saved to {_wr_path}")
                        except Exception as _w54_e:
                            log.error(f"Wave 54 weekly review failed: {_w54_e}", exc_info=True)

                        # Wave 35 (May 11, 2026): also send the eval progression
                        # view at the same daily checkpoint. Wayne gets two
                        # messages: (1) what happened today (above), (2) journey
                        # status (below). Inner try/except: a /eval failure does
                        # NOT break the daily report which has already been sent.
                        try:
                            await asyncio.sleep(1)  # preserve message order in Telegram
                            _eval_view = sim.eval_progression_text()
                            await tg_send(app, _eval_view)
                            log.info("Wave 35: eval progression view sent")
                        except Exception as _w35_err:
                            log.warning(f"Wave 35 eval progression send failed: {_w35_err}")

                        _LAST_DAILY_REPORT_DATE = today_str
                        log.info(f"Daily report sent for {today_str}")
                    except Exception as e:
                        log.error(f"Daily report scheduler: {e}")

            # Pre-Batch Follow-up Part B 2026-04-21: Weekly recap scheduler — Mondays at 8 AM ET
            try:
                if now_et.weekday() == 0 and now_et.hour >= 8:  # Monday, 8 AM+
                    from datetime import timedelta as _td
                    this_monday = now_et.date()
                    last_week_monday = this_monday - _td(days=7)
                    if _LAST_WEEKLY_RECAP_DATE != this_monday.isoformat():
                        try:
                            from weekly_recap import generate_weekly_recap
                            md_path, tg_text = generate_weekly_recap(last_week_monday)
                            await tg_send(app, tg_text)
                            _LAST_WEEKLY_RECAP_DATE = this_monday.isoformat()
                            log.info(f"Weekly recap sent for week of {last_week_monday}, file: {md_path}")
                        except Exception as e:
                            log.error(f"Weekly recap generation/send: {e}")
            except Exception as e:
                log.error(f"Weekly recap scheduler: {e}")

            # Pre-Batch Follow-up Part A 2026-04-20: Safety-net 4:10 PM force-flatten.
            # Primary path is SessionClock.FUTURES_PRE_FLATTEN. This covers us if that
            # event is hardcoded to the old 3:55 PM time (it currently is, per
            # session_clock.py:_EVENT_SCHEDULE). Fires exactly once in 16:10-16:20 window.
            try:
                _now_flat = _now_et()
                _hm_flat = _now_flat.hour * 60 + _now_flat.minute
                # Wave 86: the old 16:10-16:20 window could be skipped entirely by a
                # coarse scan interval (proven Jul 2: NQ + GC carried overnight because
                # no tick landed inside the window). Now ANY tick from 16:10 onward
                # settles pre-cutoff leftovers, once per day - including after a
                # restart. Evening-session trades are exempt via _futures_pre_cutoff.
                if _hm_flat >= 970:
                    open_fut = _futures_pre_cutoff(ot.load_open_trades(), _now_flat)
                    today_key = _now_flat.date().isoformat()
                    if open_fut and getattr(scan_loop, "_last_410_flatten", None) != today_key:
                        log.info(f"W86 4:10 settle: {len(open_fut)} leftover futures (catch-up capable)")
                        await force_flatten_futures(app)
                        scan_loop._last_410_flatten = today_key
            except Exception as e:
                log.error(f"4:10 PM safety-net flatten error: {e}")

            # Handle async flatten (set by _on_pre_flatten callback)
            if _FLATTEN_PENDING:
                _FLATTEN_PENDING = False
                await force_flatten_futures(app)
                log.info("4PM force-flatten complete (via SessionClock)")

            # Handle session close summary (set by _on_session_close callback)
            if _SESSION_CLOSE_SUMMARY is not None:
                s = _SESSION_CLOSE_SUMMARY
                _SESSION_CLOSE_SUMMARY = None
                try:
                    sm = s["summary"]
                    icon = "🟢" if sm["win_rate"] >= 55 else "🔴" if sm["win_rate"] < 45 else "🟡"
                    pnl_str = f"+${s['sim_pnl']:,.2f}" if s["sim_pnl"] >= 0 else f"-${abs(s['sim_pnl']):,.2f}"
                    await tg_send(app,
                        f"📅 *Session Close — {s['sid']}*\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"{icon} *{sm['wins']}W / {sm['losses']}L* ({sm['win_rate']}% WR)\n"
                        f"Sim P&L: `{pnl_str}`\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"\U0001f4bc Balance carries: `${sim.load_state()['balance']:,.2f}`\n"
                        f"Futures reopen at 6PM ET."
                    )
                except Exception as e:
                    log.error(f"Session close Telegram msg: {e}")

            # Pre-Batch 2026-04-20: Send recap Telegram message if pending
            if _RECAP_PENDING is not None:
                r = _RECAP_PENDING
                _RECAP_PENDING = None
                try:
                    await tg_send(app, r["tg_text"])
                    log.info(f"Pre-Batch: Recap Telegram summary sent. Local file: {r['path']}")
                except Exception as e:
                    log.error(f"Pre-Batch: Recap Telegram send failed: {e}")

            # Handle suspension changes (set by _on_session_close callback)
            if _SUSPENSION_CHANGES:
                changes = _SUSPENSION_CHANGES
                _SUSPENSION_CHANGES = []
                try:
                    lines = ["🔬 *Setup Suspension Update*", "━━━━━━━━━━━━━━━━━━"]
                    for c in changes:
                        icon = "⛔" if c.startswith("SUSPENDED") else "✅"
                        lines.append(f"  {icon} {c}")
                    lines.append("━━━━━━━━━━━━━━━━━━")
                    lines.append(ot.get_suspension_report())
                    await tg_send(app, "\n".join(lines))
                except Exception as e:
                    log.error(f"Suspension Telegram msg: {e}")

            if SETTINGS["morning_brief"] and now_et.hour==8 and now_et.minute>=30 and last_brief!=now_et.date():
                await tg_send(app, build_morning_brief()); last_brief=now_et.date()
            if SETTINGS["asia_brief"] and now_et.hour==18 and last_asia!=now_et.date():
                await tg_send(app, build_asia_brief()); last_asia=now_et.date()
            # Daily report moved above (Task 3C) — file-existence-guarded

            if SETTINGS["scanner_on"]:
                active=[m for m in ALL_MARKETS if SETTINGS["markets"].get(m)]

                frames_by_market = {}
                for m in active:
                    try: frames_by_market[m] = get_frames(m)
                    except Exception as e: log.error(f"get_frames {m}: {e}"); frames_by_market[m] = {}

                scan_interval, reason = get_smart_interval(active, frames_by_market)
                log.info(f"--- Scanning {active} | {reason} ---")
                _LAST_SCAN_TIMESTAMP = datetime.now(timezone.utc)  # Wave 19: track most recent scan

                try:
                    # Wave 140 (_WAVE140_LEARNABLE_REJECTS): rejected rows now
                    # carry levels, so graded paper trades stay "in flight" for
                    # hours and the pending pool is never empty - unthrottled,
                    # the checker would full-rewrite the CSV every cycle again
                    # (the exact churn Wave 139 removed). Grading reads bar
                    # HISTORY, so a 10-minute cadence loses zero accuracy.
                    if _time.time() - _W140_LAST_MISSED[0] >= 600:
                        live_15m = {m: f.get("15m") for m,f in frames_by_market.items() if f.get("15m") is not None}
                        missed = sl.check_missed_setups(live_15m)
                        _W140_LAST_MISSED[0] = _time.time()
                        if missed: log.info(f"Missed check: {len(missed)} resolved")
                except Exception as e: log.warning(f"Missed check: {e}")

                for m in active:
                    try: await scan_market(app, m, frames_by_market[m])
                    except Exception as e: log.error(f"scan {m}: {e}\n{traceback.format_exc()}")

                try:
                    await watch_open_trades(app, frames_by_market)
                    _W94_WATCH_FAILS[0] = 0  # Wave 94: success clears the streak
                except Exception as e:
                    _W94_WATCH_FAILS[0] += 1
                    log.error(f"watch: {e}\n{traceback.format_exc()}")  # Wave 94: full stack pinpoints the fault
                    if _W94_WATCH_FAILS[0] in (3, 12):  # ~15 min and ~1 hr at 5-min cadence
                        try:
                            await tg_send(app, f"\u26a0\ufe0f *Open-trade watch failing* \u2014 {_W94_WATCH_FAILS[0]} cycles in a row. Check logs.")
                        except Exception:
                            pass

                # Wave 68: grade due trend reads against what price actually did.
                try: trend_memory.grade_due_reads_from_frames(frames_by_market)
                except Exception as e: log.debug(f"trend grade: {e}")

            if (datetime.now(timezone.utc)-last_hb).total_seconds()>=3600:
                log.info(f"Heartbeat scanner={SETTINGS['scanner_on']} open={len(ot.load_open_trades())}")
                last_hb=datetime.now(timezone.utc)

        except asyncio.CancelledError:
            # Wave 163: a real shutdown (deploy/restart) - propagate so the
            # supervisor knows NOT to relaunch. Any OTHER exit is a crash the
            # supervisor will recover from.
            log.info("scan_loop: cancelled (shutdown) - exiting cleanly.")
            raise
        except Exception as e: log.error(f"scan_loop: {e}\n{traceback.format_exc()}")
        await asyncio.sleep(scan_interval*60)

# ── Menu ──────────────────────────────────────────────────────────
def main_menu():
    """
    Wave 17 (May 9, 2026): Major UI overhaul - removed user-tunable
    strategy controls, info-rich status row.

    Wave 18 (May 9, 2026): Restored Analyze button to row 6.

    Wave 18b (May 9, 2026): Menu polish based on Wayne's feedback:
      - Renamed "Live Brief" to just "Brief"
      - Removed Morning Brief / Asia Brief row entirely (briefs
        still auto-run at scheduled times; manual-trigger buttons
        weren't being used)
      - Removed MNQ toggle (force-locked to MNQ on 50k accounts -
        the toggle was cosmetic-only, no effect on bot behavior)
      - Reverted Dashboard from URL button to callback (GitHub
        Pages requires public repo - we're private. Will switch
        back to URL when alt hosting is set up)

    Layout: 9 rows / 22 buttons (was 10 / 26).

    Wave 19 (May 9, 2026): Dashboard URL button restored. Repo
    is now public + GitHub Pages enabled at /docs - URL routing
    live. Tap Dashboard -> opens browser.
    """
    s = SETTINGS
    m = s["markets"]
    ss = sim.load_state()
    sim_on = ss.get("enabled", False)
    preset = ss.get("preset", "50k").upper()
    risk = sim.check_risk_limits(ss)
    pnl = risk["daily_pnl"]
    pnl_str = f"+${pnl:,.0f}" if pnl >= 0 else f"-${abs(pnl):,.0f}"

    # Info-rich status row text
    if s["scanner_on"]:
        active_mkts = ",".join([k for k, v in m.items() if v]) or "—"
        try:
            n_open = len(ot.load_open_trades())
        except Exception:
            n_open = 0
        scan_btn = f"🟢 LIVE • {active_mkts} • {n_open} open"
    else:
        scan_btn = "🔴 OFFLINE — tap to start"

    kb = [
        # Row 1: Master scanner state (full width, info-rich)
        [InlineKeyboardButton(scan_btn, callback_data="toggle_scan")],

        # Row 2: Market toggles
        [InlineKeyboardButton(f"{'✅' if m['NQ'] else '⬜'} NQ",   callback_data="toggle_NQ"),
         InlineKeyboardButton(f"{'✅' if m['GC'] else '⬜'} Gold", callback_data="toggle_GC"),
         InlineKeyboardButton(f"{'✅' if m['BTC'] else '⬜'} BTC", callback_data="toggle_BTC"),
         InlineKeyboardButton(f"{'✅' if m['SOL'] else '⬜'} SOL", callback_data="toggle_SOL")],

        # Row 3: Manual outcome marking (when bot misses an exit)
        [InlineKeyboardButton("✅ WIN",  callback_data="trade_win"),
         InlineKeyboardButton("❌ LOSS", callback_data="trade_loss"),
         InlineKeyboardButton("⏭ SKIP", callback_data="trade_skip")],

        # Row 4: Open trades (prominent, full width)
        [InlineKeyboardButton("📋 OPEN TRADES", callback_data="open_trades")],

        # Row 5: At-a-glance info (4 cols)
        [InlineKeyboardButton("📊 Status",  callback_data="status"),
         InlineKeyboardButton("📈 Stats",   callback_data="stats"),
         InlineKeyboardButton("📅 Session", callback_data="session"),
         InlineKeyboardButton("🎯 Edge",    callback_data="learning")],

        # Row 6: Live market info & analysis (Wave 18b: "Live Brief" -> "Brief")
        [InlineKeyboardButton("📡 Brief",    callback_data="live_brief"),
         InlineKeyboardButton("📋 Report",   callback_data="report_now"),
         InlineKeyboardButton("🔬 Analyze",  callback_data="analyze")],

        # (Wave 18b: removed Morning Brief / Asia Brief row -
        #  briefs still auto-post at 8:30am ET / 6pm ET)

        # Row 7: Sim primary - status & P&L
        [InlineKeyboardButton(f"💰 SIM {'🟢' if sim_on else '🔴'}",  callback_data="toggle_sim"),
         InlineKeyboardButton(f"💵 {pnl_str} today",                     callback_data="sim_status")],

        # Row 8: Sim utilities (Wave 18b: removed MNQ toggle - cosmetic-only on 50k)
        [InlineKeyboardButton(f"🔄 Reset {preset}", callback_data="simreset_current"),
         InlineKeyboardButton("📅 Weekly",          callback_data="sim_weekly")],

        # Wave 37 (May 11, 2026): explicit reset buttons (separate from cycling preset reset)
        [InlineKeyboardButton("♻️ Reset Topstep", callback_data="reset_topstep_confirm"),
         InlineKeyboardButton("♻️ Reset Crypto",  callback_data="reset_crypto_confirm")],

        # Row 9: Archives & info footer (Wave 19: Dashboard URL restored - Pages live)
        [InlineKeyboardButton("📜 History",   callback_data="history_list"),
         InlineKeyboardButton("🏆 Lifetime",  callback_data="lifetime"),
         InlineKeyboardButton("📊 Dashboard", url="https://kdubsk1.github.io/bot/dashboard.html"),
         InlineKeyboardButton("❓ Help",          callback_data="help")],
    ]
    return InlineKeyboardMarkup(kb)

# ── Commands ──────────────────────────────────────────────────────
async def cmd_start(u,c): await u.message.reply_text("✅ *NQ CALLS Bot is live!*\nUse the menu below.",parse_mode="Markdown",reply_markup=main_menu())
async def cmd_menu(u,c):  await u.message.reply_text("NQ CALLS Control Panel:", reply_markup=main_menu())
async def cmd_stats(u,c): await u.message.reply_text(ot.print_stats(), parse_mode="Markdown")

async def cmd_open(u,c):
    trades=ot.load_open_trades()
    if not trades: await u.message.reply_text("No open trades right now."); return
    lines=["📋 *Open Trades:*\n"]
    for i,t in enumerate(trades,1):
        lines.append(f"*{i}.* {t.get('market')} | {t.get('setup')} | {t.get('direction')}\n"
                     f"   Entry:`{t.get('entry')}` Target:`{t.get('target')}`\n   ID:`{t.get('alert_id')}`\n")
    lines.append("_/win ID | /loss ID | /skip ID_")
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def _mark(u, result, args):
    trades=ot.load_open_trades()
    if not trades: await u.message.reply_text("No open trades."); return
    if args:
        match=next((t for t in trades if t["alert_id"]==args[0].strip()),None)
        if not match: await u.message.reply_text(f"ID `{args[0]}` not found.", parse_mode="Markdown"); return
    else: match=trades[-1]
    exit_p=match.get("target",0) if result=="WIN" else match.get("stop",0) if result=="LOSS" else 0
    ot.update_result(match["alert_id"],result,0,exit_p)
    if result in ("WIN","LOSS"): ot.record_trade_result(match["market"],match["setup"],result)
    # Batch 2A: Log outcome to strategy_log.csv
    if result in ("WIN","LOSS"):
        try:
            ot._log_trade_outcome(match, result, exit_p)
        except Exception:
            pass
    icons={"WIN":"✅","LOSS":"❌","SKIP":"⏭"}
    await u.message.reply_text(f"{icons.get(result,'❓')} *{result}* — {match.get('market')} | {_md(match.get('setup',''))}\nLearning updated.",parse_mode="Markdown")

async def cmd_win(u,c):  await _mark(u,"WIN",c.args)
async def cmd_loss(u,c): await _mark(u,"LOSS",c.args)
async def cmd_skip(u,c): await _mark(u,"SKIP",c.args)

async def cmd_report(u,c):
    await u.message.reply_text("⏳ Building report...")
    try:
        full,short=ot.build_daily_report()
        sec=sim.sim_daily_section()
        if sec: full+="\n"+sec
        await tg_send(c.application, short)
        await u.message.reply_text("✅ Report sent! Paste to Claude to review.",parse_mode="Markdown")
    except Exception as e: await u.message.reply_text(f"❌ {e}")

async def cmd_analyze(u,c):
    await u.message.reply_text("⏳ Analyzing strategy log...")
    try:
        report=sl.build_strategy_analysis()
        await u.message.reply_text(f"```\n{report[:4000]}\n```",parse_mode="Markdown")
        await u.message.reply_text("Full analysis saved. Paste to Claude for deeper review.",parse_mode="Markdown")
    except Exception as e: await u.message.reply_text(f"❌ {e}")

async def cmd_simstatus(u,c): await u.message.reply_text(sim.sim_status_text(),parse_mode="Markdown")

async def cmd_cryptostatus(u, c):
    """
    May 2 Wave 5: /cryptostatus shows the crypto sim build-up account separately.
    Mirrors /simstatus but reads from crypto_sim.json (BTC/SOL).

    The crypto sim has different rules:
      - $1,000 starting balance, no daily reset
      - 1.5% risk per trade, 10x leverage
      - Max hold 7 days
      - Profit target $1,500 (50% gain)
    """
    try:
        # Reconcile any stale open trades from outcomes.csv before showing.
        # If a trade closed via auto-resolve but didn't propagate to crypto_sim,
        # this catches it so the displayed open count is accurate.
        try:
            n_rec = crypto_sim.reconcile_with_outcomes()
            if n_rec:
                log.info(f"/cryptostatus: reconciled {n_rec} stale crypto trade(s)")
        except Exception as _re:
            log.warning(f"/cryptostatus reconcile: {_re}")
        await u.message.reply_text(crypto_sim.get_crypto_status_text(), parse_mode="Markdown")
    except Exception as e:
        log.error(f"/cryptostatus failed: {e}")
        await u.message.reply_text(f"Crypto status command failed: {e}")

# Wave 111 (_WAVE111_RESETCRYPTO_CMD): /resetcrypto - a discoverable slash-command
# entrance to the EXISTING reset flow (the "Reset Crypto" button + confirm dialog,
# Waves 37/52). Posts the same confirmation keyboard wired to the same
# reset_crypto_yes / reset_cancel callbacks handled by on_button, so there is
# exactly ONE reset implementation. Balance resets to the starting value and open
# trades clear; closed-trade HISTORY is preserved (Wave 52). Nothing happens
# without tapping YES in the dialog.
async def cmd_resetcrypto(u, c):
    try:
        cs_state = crypto_sim.load_crypto_state()
        cur_bal = float(cs_state.get("balance", 0))
        start_bal = float(cs_state.get("starting_balance", 1000))
        n_closed = len(cs_state.get("closed_trades", []))
        n_open = len(cs_state.get("open_trades", []))
    except Exception:
        cur_bal, start_bal, n_closed, n_open = 0, 1000, 0, 0
    confirm_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("YES - reset", callback_data="reset_crypto_yes"),
         InlineKeyboardButton("Cancel", callback_data="reset_cancel")],
    ])
    await u.message.reply_text(
        f"*Reset Crypto Sim?*\n"
        f"Balance: ${cur_bal:,.2f} -> ${start_bal:,.0f}\n"
        f"Open trades: {n_open} (cleared)  |  Closed history: {n_closed} (KEPT)\n"
        f"This cannot be undone.",
        parse_mode="Markdown",
        reply_markup=confirm_kb,
    )


async def cmd_leverage(u, c):
    """Wave 98: /leverage [N] - view or set the crypto sim leverage (BTC/SOL)."""
    try:
        _cst = crypto_sim.load_crypto_state()
        cur = int(_cst.get("leverage", 10))
        if not c.args:
            await u.message.reply_text(
                f"\u26a1 *Crypto leverage:* AUTO (max `{cur}x`)  # _WAVE119_LEVERAGE_DISPLAY\n"
                f"The bot picks leverage per trade by regime/conviction, up to your `{cur}x` ceiling.\n"
                f"Raise/lower the ceiling with `/leverage N` (1-20), e.g. `/leverage 20` to let it use more.",
                parse_mode="Markdown")
            return
        try:
            n = int(str(c.args[0]).strip().lower().rstrip("x"))
        except Exception:
            await u.message.reply_text("Usage: `/leverage N` where N is 1-20, e.g. `/leverage 5`.", parse_mode="Markdown")
            return
        if n < 1 or n > 20:
            await u.message.reply_text(f"Leverage must be 1-20 (got `{n}`).", parse_mode="Markdown")
            return
        _cst["leverage"] = n
        crypto_sim.save_crypto_state(_cst)
        await u.message.reply_text(
            f"\u2705 Crypto sim leverage ceiling set: `{cur}x` \u2192 `{n}x`\n"
            f"The bot still auto-picks per trade, now up to `{n}x`. "
            f"Applies to NEW BTC/SOL sim trades (open trades keep their entry leverage).",
            parse_mode="Markdown")
    except Exception as e:
        log.error(f"/leverage failed: {e}")
        await u.message.reply_text(f"Leverage command failed: {e}")

async def cmd_wave7(u, c):
    """
    Wave 7: /wave7 shows the Iron Robot conviction adjustment status.
    All 5 layers (setup boosts, market multipliers, bucket recalibration,
    priority lane, auto-tune) with current values.
    """
    try:
        await u.message.reply_text(cb.get_status_text(), parse_mode="Markdown")
    except Exception as e:
        log.error(f"/wave7 failed: {e}")
        await u.message.reply_text(f"Wave7 status failed: {e}")

async def cmd_tune(u, c):
    """
    Wave 7: /tune manually triggers the auto-tune cycle.
    Same as the Sunday 8 PM auto-run but on demand. Posts the diff
    of what changed (or 'no changes needed').

    Usage:
        /tune          - run full auto-tune (Layer 1 setup nudges + Layer 3)
        /tune l3       - only Layer 3 bucket recalibration
    """
    try:
        only_l3 = bool(c and c.args and c.args[0].lower() in ("l3", "layer3", "buckets"))

        await u.message.reply_text(
            "\U0001f527 Running auto-tune" + (" (L3 only)..." if only_l3 else "..."),
            parse_mode="Markdown"
        )

        if only_l3:
            result = await asyncio.to_thread(cb.recalibrate_bucket_floors, True)
        else:
            result = await asyncio.to_thread(cb.run_auto_tune)

        # Format the result for Telegram
        lines = ["\U0001f527 *Auto-Tune Result*", "\u2501" * 16]

        if only_l3:
            lines.append(f"*Action:* {result.get('action', '?')}")
            lines.append(f"*Floor:* {result.get('floor_before', 0)} \u2192 {result.get('floor_after', 0)}")
            lines.append(f"*Reason:* {result.get('reason', '?')}")
            buckets = result.get("buckets", {})
            if buckets:
                lines.append("")
                lines.append("*Buckets:*")
                for bn in ["HIGH (80+)", "UPPER-MID (70-79)", "MID (65-69)", "LOW (50-64)"]:
                    b = buckets.get(bn, {})
                    if b.get("total", 0) > 0:
                        lines.append(f"  `{bn}` {b['wins']}W/{b['losses']}L "
                                     f"({b['wr']}% WR)")
        else:
            changes = result.get("changes", [])
            n_analyzed = result.get("n_setups_analyzed", 0)
            window = result.get("window_days", 28)
            lines.append(f"*Analyzed:* {n_analyzed} setups (last {window}d)")
            lines.append(f"*Changes:* {len(changes)}")
            if changes:
                lines.append("")
                for ch in changes:
                    icon = "\U0001f7e2" if ch["boost_after"] > ch["boost_before"] else "\U0001f534"
                    lines.append(
                        f"  {icon} `{ch['setup']}` {ch['wr']:.0f}% WR "
                        f"(${ch['avg_dollar']:+.0f}/trade, {ch['trades']}t): "
                        f"{ch['boost_before']:+d} \u2192 {ch['boost_after']:+d}"
                    )
            l3 = result.get("l3_recalibration", {})
            if l3.get("changed"):
                lines.append("")
                lines.append(f"*L3 Floor:* {l3.get('floor_before', 0)} \u2192 {l3.get('floor_after', 0)}")
                lines.append(f"  ({l3.get('reason', '?')})")

        msg = "\n".join(lines)
        await u.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        log.error(f"/tune failed: {e}")
        import traceback as _tb
        log.error(_tb.format_exc())
        await u.message.reply_text(f"\u274c Auto-tune failed: {e}")

async def cmd_backtest(u, c):
    """
    May 3 Wave 6: /backtest replays closed trades through current scoring
    rules and posts a summary to Telegram.

    Usage:
        /backtest          - all data
        /backtest 14       - last 14 days only
        /backtest NQ       - one market only (NQ, GC, BTC, SOL)
        /backtest 7 NQ     - last 7 days, NQ only

    Mirrors what backtest.py does locally, but runs in-process so Wayne
    can trigger it from his phone. The full markdown report still gets
    written to data/backtest_report_<date>.md for deeper review.

    Pre-mortem:
      - Q: What if backtest takes 30+ seconds and blocks the bot loop?
        A: We run it via asyncio.to_thread so the event loop stays free.
      - Q: What if the report exceeds Telegram's 4096-char limit?
        A: We truncate to top 5 performers + top 5 suspend candidates,
           and tell Wayne to read the full markdown for everything else.
      - Q: What if outcomes.csv has zero closed trades?
        A: Send a friendly message saying so. No crash.
      - Q: What if backtest.py is missing or fails to import?
        A: Caught in try/except, error replied to user. Bot keeps running.
    """
    try:
        # Lazy import so a broken backtest.py never crashes bot startup
        import backtest as bt

        # Parse args: optional days (int) and/or market (str)
        args_dict = {"days": None, "market": None, "setup": None, "min_trades": 3}
        if c and c.args:
            for a in c.args:
                a = str(a).strip()
                # Number = days filter
                if a.isdigit():
                    args_dict["days"] = int(a)
                # Known market codes
                elif a.upper() in ("NQ", "GC", "BTC", "SOL"):
                    args_dict["market"] = a.upper()
                # Otherwise treat as setup name
                else:
                    args_dict["setup"] = a.upper()

        await u.message.reply_text(
            f"\U0001f50d Running backtest..."
            + (f" (last {args_dict['days']}d)" if args_dict["days"] else "")
            + (f" market={args_dict['market']}" if args_dict["market"] else "")
            + (f" setup={args_dict['setup']}" if args_dict["setup"] else ""),
            parse_mode="Markdown"
        )

        # Run the analysis off the event loop (it reads files + does math)
        def _run_analysis():
            rows = bt._load_outcomes()
            if not rows:
                return None, None, 0
            filtered = bt._filter_outcomes(rows, args_dict)
            if not filtered:
                return None, None, 0
            stats = bt._aggregate(filtered, args_dict["min_trades"])
            classifications = bt._classify(stats, args_dict["min_trades"])
            # Also write the markdown + JSON report to disk for deeper review
            try:
                bt._write_report(stats, classifications, args_dict)
            except Exception as _we:
                log.warning(f"/backtest write_report failed: {_we}")
            return stats, classifications, len(filtered)

        stats, classifications, n_trades = await asyncio.to_thread(_run_analysis)

        if not stats:
            await u.message.reply_text(
                "\u26a0\ufe0f No closed trades match those filters yet. "
                "Run the bot for a while or relax the filters."
            )
            return

        # Build a Telegram-friendly summary (truncated for 4096-char limit)
        lines = [
            f"\U0001f4ca *Backtest Results* ({n_trades} closed trades)",
            "\u2501" * 16,
        ]

        # By market summary
        lines.append("\U0001f3af *By Market*")
        market_rows = sorted(stats["by_market"].items(), key=lambda x: -x[1]["total_r"])
        for market, v in market_rows:
            sign = "\U0001f7e2" if v["total_r"] >= 0 else "\U0001f534"
            lines.append(
                f"  {sign} `{market}` {v['wins']}W/{v['losses']}L "
                f"({v['wr']:.1f}% WR, R={v['total_r']:+.1f})"
            )
        lines.append("\u2501" * 16)

        # Top 5 KEEP performers (highest expected $/trade)
        keep_keys = [k for k, cls in classifications.items() if cls.startswith("KEEP")]
        keep_keys.sort(key=lambda k: -stats["by_setup"][k]["expected_dollar_per_trade"])
        top_perf = keep_keys[:5]
        if top_perf:
            lines.append("\U0001f3c6 *Top Performers*")
            for k in top_perf:
                v = stats["by_setup"][k]
                lines.append(
                    f"  \u2705 `{k}` {v['wins']}W/{v['losses']}L "
                    f"({v['wr']:.1f}%, ${v['expected_dollar_per_trade']:+.0f}/trade)"
                )
            lines.append("\u2501" * 16)

        # Top 5 SUSPEND candidates (worst expected $/trade)
        susp_keys = [k for k, cls in classifications.items() if cls.startswith("SUSPEND")]
        susp_keys.sort(key=lambda k: stats["by_setup"][k]["expected_dollar_per_trade"])
        top_susp = susp_keys[:5]
        if top_susp:
            lines.append("\U0001f6d1 *Suspend Candidates*")
            for k in top_susp:
                v = stats["by_setup"][k]
                lines.append(
                    f"  \u274c `{k}` {v['wins']}W/{v['losses']}L "
                    f"({v['wr']:.1f}%, ${v['expected_dollar_per_trade']:+.0f}/trade)"
                )
            lines.append("\u2501" * 16)

        # By conviction bucket - quick view
        from collections import defaultdict as _dd
        bucket_totals = _dd(lambda: {"wins": 0, "losses": 0, "total_r": 0.0})
        for key, v in stats["by_setup_bucket"].items():
            bname = key.split(":", 2)[-1]
            bucket_totals[bname]["wins"] += v["wins"]
            bucket_totals[bname]["losses"] += v["losses"]
            bucket_totals[bname]["total_r"] += v["total_r"]
        bucket_order = ["HIGH (80+)", "UPPER-MID (70-79)", "MID (65-69)", "LOW (50-64)"]
        bucket_lines_added = False
        for bname in bucket_order:
            v = bucket_totals.get(bname)
            if not v:
                continue
            t = v["wins"] + v["losses"]
            if t == 0:
                continue
            if not bucket_lines_added:
                lines.append("\U0001f3af *By Conviction*")
                bucket_lines_added = True
            wr = v["wins"] / t * 100
            avg_r = v["total_r"] / t
            lines.append(f"  `{bname}` {v['wins']}W/{v['losses']}L "
                         f"({wr:.1f}% WR, avg R={avg_r:+.2f})")
        if bucket_lines_added:
            lines.append("\u2501" * 16)

        # Footer with totals
        n_keep = len(keep_keys)
        n_susp = len(susp_keys)
        lines.append(
            f"_Total: {n_keep} keep, {n_susp} suspend candidates._\n"
            f"_Full report: data/backtest_report_*.md_"
        )

        msg = "\n".join(lines)
        # Telegram hard limit is 4096; chunk if necessary
        if len(msg) <= 4000:
            await u.message.reply_text(msg, parse_mode="Markdown")
        else:
            chunks = []
            cur = ""
            for ln in lines:
                if len(cur) + len(ln) + 1 > 3800:
                    chunks.append(cur)
                    cur = ln
                else:
                    cur = (cur + "\n" + ln) if cur else ln
            if cur:
                chunks.append(cur)
            for chunk in chunks:
                await u.message.reply_text(chunk, parse_mode="Markdown")
    except Exception as e:
        log.error(f"/backtest failed: {e}")
        import traceback as _tb
        log.error(_tb.format_exc())
        await u.message.reply_text(f"\u274c Backtest command failed: {e}")

async def cmd_recalibrate(u, c):
    """
    Wave 9 (May 4): /recalibrate - manually trigger Layer 6 edge-decay check
    + Layer 7 daily soft tune. Same logic as the 6 AM auto-run, on demand.

    Useful when:
      - Wayne sees a suspended setup that should still be active
      - Wayne wants to force a tune after a market regime change
      - Wayne wants to verify the decay logic is working
    """
    try:
        await u.message.reply_text("\U0001f504 Running Wave 9 recalibration (edge decay + soft tune)...")
        result = await asyncio.to_thread(cb.run_daily_soft_tune)
        soft_changes = result.get("changes", [])
        decay_actions = result.get("decay", {}).get("decay_actions", [])
        lines = [
            "\U0001f527 *Wave 9 Manual Recalibration*",
            "\u2501" * 16,
            f"*Analyzed:* {result.get('n_setups_analyzed', 0)} setups "
            f"(last {result.get('window_days', 7)}d)",
            f"*Tune changes (L7):* {len(soft_changes)}",
            f"*Edge-decay actions (L6):* {len(decay_actions)}",
        ]
        if decay_actions:
            lines.append("")
            lines.append("\U0001f6e1\ufe0f *Edge Decay (L6):*")
            for da in decay_actions[:10]:
                icon = ("\U0001f7e2" if da["action"] == "relaxed" else
                        "\U0001f534" if da["action"] in ("zeroed", "penalized") else
                        "\u26aa")
                lines.append(
                    f"  {icon} `{da['setup']}` {da['action']}: "
                    f"{da['boost_before']:+d}\u2192{da['boost_after']:+d} — {da['reason']}"
                )
        if soft_changes:
            lines.append("")
            lines.append("\U0001f3af *Soft Tune (L7):*")
            for ch in soft_changes[:10]:
                icon = "\U0001f7e2" if ch["boost_after"] > ch["boost_before"] else "\U0001f534"
                lines.append(
                    f"  {icon} `{ch['setup']}` {ch['wr']:.0f}% WR "
                    f"({ch['trades']}t): {ch['boost_before']:+d} \u2192 {ch['boost_after']:+d}"
                )
        if not decay_actions and not soft_changes:
            lines.append("")
            lines.append("_No changes — boosts already aligned with current data._")
        await u.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        log.error(f"/recalibrate failed: {e}")
        import traceback as _tb
        log.error(_tb.format_exc())
        await u.message.reply_text(f"\u274c Recalibrate failed: {e}")

async def cmd_pulldata(u, c):
    """
    Wave 9 (May 4): /pulldata - report whether local Desktop data matches
    Railway runtime. The bot itself runs on Railway and writes its truth to
    GitHub via auto_sync every 6h. The local Desktop copy can drift if
    nobody runs `git pull` locally.

    This command tells Wayne the freshness gap so he can decide whether
    to manually pull the latest data files.
    """
    try:
        # Check the freshest timestamp in the on-disk outcomes.csv (Railway side)
        outcomes_path = os.path.join(BASE_DIR, "outcomes.csv")
        last_outcome = "never"
        n_rows = 0
        if os.path.exists(outcomes_path):
            import csv
            try:
                with open(outcomes_path, "r", encoding="utf-8") as f:
                    rows = list(csv.DictReader(f))
                n_rows = len(rows)
                if rows:
                    last_ts = rows[-1].get("timestamp", "")
                    last_outcome = last_ts[:19] if last_ts else "never"
            except Exception:
                pass
        sync_status = auto_sync.status() if hasattr(auto_sync, "status") else "unknown"
        lines = [
            "\U0001f4e1 *Data Freshness Check*",
            "\u2501" * 16,
            f"*Runtime outcomes.csv:* `{n_rows}` rows",
            f"*Last trade closed:* `{last_outcome}`",
            "",
            f"*Auto-sync:* {sync_status}",
            "",
            "_The bot runs on Railway. Auto-sync pushes data→GitHub every 6h._",
            "_Your local Desktop files may be stale. Run `git pull` locally_",
            "_to refresh, or just trust /backtest and /edge for live truth._",
        ]
        await u.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        log.error(f"/pulldata failed: {e}")
        await u.message.reply_text(f"\u274c Pulldata failed: {e}")

async def cmd_simreset(u,c):
    preset=c.args[0] if c.args else None
    valid=list(sim.EVAL_PRESETS.keys())
    if preset and preset not in valid: await u.message.reply_text(f"Valid: {', '.join(valid)}"); return
    sim.reset_sim(preset); st=sim.load_state()
    await u.message.reply_text(f"✅ *Sim reset — {st['preset'].upper()}*\nBalance: `${st['balance']:,.2f}`\nDaily limit: `${st['daily_loss_limit']:,.2f}`",parse_mode="Markdown")

async def cmd_simon(u,c):  sim.toggle_sim(True);  await u.message.reply_text("✅ *Sim mode ON*",parse_mode="Markdown")
async def cmd_simoff(u,c): sim.toggle_sim(False); await u.message.reply_text("⏹ *Sim mode OFF*",parse_mode="Markdown")

async def cmd_mnq(u,c):
    st=sim.load_state(); use_mnq=not st.get("use_mnq",False); sim.toggle_mnq(use_mnq)
    await u.message.reply_text(f"✅ *Contract: {'MNQ (Micro)' if use_mnq else 'NQ (Full)'}*",parse_mode="Markdown")

async def cmd_simweekly(u,c):
    await u.message.reply_text(sim.sim_period_text(7), parse_mode="Markdown")

# Wave 165 (_WAVE165_SCANNER_HEALTH): one place that turns the age of the
# last scan into an at-a-glance health badge. Green = scanning normally,
# yellow = getting slow, red = stalled (>10 min, which is also the Wave 164
# self-restart trigger, so red means "healing now or step in / grab a
# combine"). Returns (emoji, short_label, mins) so callers can format it
# however they like. Never raises.
def _w165_scanner_health():
    """Return (emoji, label, mins_since_scan_or_None) for the scanner."""
    try:
        on = SETTINGS.get("scanner_on", False)
        ts = _LAST_SCAN_TIMESTAMP
        if not on:
            return ("\U0001f534", "OFF", None)
        if ts is None:
            return ("\U0001f7e1", "starting", None)
        mins = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
        if mins < 3:
            return ("\U0001f7e2", "scanning", mins)
        if mins < 10:
            return ("\U0001f7e1", "slow", mins)
        return ("\U0001f534", "DELAYED", mins)
    except Exception:
        return ("\u2754", "unknown", None)


def _w165_scan_health_line():
    """A ready-made one-line string for /diag and /status."""
    emoji, label, mins = _w165_scanner_health()
    if mins is None:
        return "%s %s" % (emoji, label)
    if mins < 1:
        age = "last <1m ago"
    elif mins < 60:
        age = "last %dm ago" % int(mins)
    else:
        age = "last %dh %dm ago" % (int(mins / 60), int(mins % 60))
    tail = "  - check Railway" if label == "DELAYED" else ""
    return "%s %s (%s)%s" % (emoji, label, age, tail)


def _build_status_text() -> str:
    """
    Wave 19 (May 9, 2026): shared builder for /status output.
    Used by cmd_status (slash) and the status button (callback).
    Returns a Markdown-formatted Telegram message string.
    """
    on = SETTINGS.get("scanner_on", False)
    scanner_info = _load_scanner_state()
    hrs_ago = scanner_info.get("hours_ago", 0)

    active = [m for m in ALL_MARKETS if SETTINGS["markets"].get(m)]

    try:
        open_trades = ot.load_open_trades()
    except Exception:
        open_trades = []
    open_by_mkt: dict = {}
    for t in open_trades:
        m = t.get("market", "?")
        open_by_mkt[m] = open_by_mkt.get(m, 0) + 1

    regimes: dict = {}
    for m in active:
        try:
            frames = get_frames(m)
            df15 = frames.get("15m")
            if df15 is not None and not df15.empty and len(df15) >= 20:
                from regime_classifier import classify_regime
                regime = classify_regime(df15).get("regime", "UNKNOWN")
                trend, _ = ot.trend_score(frames, m)
                regimes[m] = {"regime": regime, "trend": int(trend)}
            else:
                regimes[m] = {"regime": "no data", "trend": 0}
        except Exception:
            regimes[m] = {"regime": "?", "trend": 0}

    halted = [m for m in active if _is_halted(m)]

    last_scan = _LAST_SCAN_TIMESTAMP
    if last_scan:
        delta_min = (datetime.now(timezone.utc) - last_scan).total_seconds() / 60.0
        if delta_min < 1:
            last_scan_str = "just now"
        elif delta_min < 60:
            last_scan_str = f"{int(delta_min)} min ago"
        else:
            last_scan_str = f"{int(delta_min/60)}h {int(delta_min%60)}m ago"
    else:
        last_scan_str = "never (scanner just started)" if on else "scanner off"

    news = ot.in_news_window()

    if on:
        state_line = "🟢 Running" + (f" (since {hrs_ago}h ago)" if hrs_ago > 0 else "")
    else:
        state_line = "🔴 Stopped"

    lines = [
        "🤖 *Bot Status*",
        "━" * 18,
        f"*Scanner:* {state_line}",
        f"*Last scan:* {_w165_scanner_health()[0]} {last_scan_str}",
        f"*Markets active:* {', '.join(active) if active else 'none'}",
        f"*Open trades:* `{len(open_trades)}`",
    ]
    if open_by_mkt:
        for m in ("NQ", "GC", "BTC", "SOL"):
            if m in open_by_mkt:
                lines.append(f"  {m}: {open_by_mkt[m]} open")
    if regimes:
        lines.append("━" * 18)
        lines.append("*Market regimes:*")
        for m in active:
            r = regimes.get(m, {})
            regime = r.get("regime", "?")
            trend = r.get("trend", 0)
            t_arrow = "🟢" if trend >= 2 else "🔴" if trend <= -2 else "⚪"
            lines.append(f"  {t_arrow} {m}: {regime} (trend {trend:+d})")
    lines.append("━" * 18)
    news_str = "⚠️ Active window" if news else "✅ Clear"
    lines.append(f"*News:* {news_str}")
    lines.append(f"*Conv min:* {SETTINGS['min_conviction']} | *RR min:* {SETTINGS['min_rr']}")
    if halted:
        lines.append(f"*Halted:* {', '.join(halted)}")
    return "\n".join(lines)


async def cmd_status(u, c):
    # Wave 36 (May 11, 2026): reconcile sim against outcomes.csv before
    # displaying status so /status always shows fresh PNL state, never
    # stale balance from a missed auto-close.
    try:
        _w36_n = sim.reconcile_with_outcomes()
        if _w36_n:
            log.info(f"/status: Wave 36 reconciled {_w36_n} stale sim trade(s)")
    except Exception as _w36_e:
        log.warning(f"/status Wave 36 reconcile: {_w36_e}")
    """Wave 19 (May 9, 2026): /status - bot health + market state.

    Wayne discovered /status was registered in set_my_commands but had
    no handler function. Slash command did nothing. This is the fix.
    """
    await u.message.reply_text(_build_status_text(), parse_mode="Markdown")


async def cmd_session(u,c):
    """Show current session data only."""
    sid = get_session_date()
    summary = ot.build_session_summary(sid)
    trades = ot.get_session_trades(sid)
    closed = [r for r in trades if r.get("status") == "CLOSED" and r.get("result") in ("WIN","LOSS")]
    open_ = [r for r in trades if r.get("status") == "OPEN"]
    setups_fired = list(set(r.get("setup","?") for r in trades))

    sim_state = sim.load_state()
    sim_line = ""
    if sim_state.get("enabled"):
        risk = sim.check_risk_limits(sim_state)
        sim_line = (
            f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
            f"\U0001f3e6 *Topstep Futures Sim (NQ/GC)*\n"
            f"  Balance: `${risk['balance']:,.2f}`\n"
            f"  Session P&L: `${risk['daily_pnl']:+,.2f}`\n"
            f"  Daily limit left: `${risk['daily_left']:,.2f}`\n"
        )

    icon = "🟢" if summary["win_rate"] >= 55 else "🔴" if summary["win_rate"] < 45 else "🟡"
    pnl_str = f"+{summary['total_pnl_r']}R" if summary["total_pnl_r"] >= 0 else f"{summary['total_pnl_r']}R"
    lines = [
        f"📊 *Session {sid}*",
        f"━━━━━━━━━━━━━━━━━━",
        f"*Trades:* `{summary['total_trades']}` | *Open:* `{len(open_)}`",
        f"*W/L:* `{summary['wins']}W / {summary['losses']}L` {icon} {summary['win_rate']}% WR",
        f"*P&L:* `{pnl_str}`",
        f"*Markets:* {', '.join(summary['markets_traded']) if summary['markets_traded'] else 'None'}",
    ]
    # Wave 19: per-market breakdown
    by_mkt = summary.get("by_market", {})
    if by_mkt:
        _mkt_lines = []
        for _m in ("NQ", "GC", "BTC", "SOL"):
            if _m in by_mkt:
                _d = by_mkt[_m]
                _w = _d.get("wins", 0); _l = _d.get("losses", 0); _pnl = _d.get("pnl_r", 0.0)
                _pnl_s = f"+{_pnl}R" if _pnl >= 0 else f"{_pnl}R"
                _open_n = _d.get("open", 0)
                _open_s = f" ({_open_n} open)" if _open_n else ""
                _mkt_lines.append(f"  {_m}: {_w}W/{_l}L {_pnl_s}{_open_s}")
        if _mkt_lines:
            lines.append("*Per-market today:*")
            lines.extend(_mkt_lines)
    if setups_fired:
        lines.append(f"*Setups fired:* {', '.join(setups_fired[:8])}")
    if summary.get("best_setup") != "N/A":
        lines.append(f"*Best:* `{summary['best_setup']}` | *Worst:* `{summary['worst_setup']}`")
    # Wave 97: crypto sim gets its own clearly-labeled block (BTC/SOL, leveraged)
    crypto_line = ""
    try:
        _cst = crypto_sim.load_crypto_state()
        if _cst.get("enabled"):
            _ctoday = datetime.now().strftime("%Y-%m-%d")
            _ctr = [t for t in _cst.get("closed_trades", []) if _ctoday in str(t.get("closed_at", ""))]
            _cw = sum(1 for t in _ctr if t.get("result") == "WIN")
            _cl = sum(1 for t in _ctr if t.get("result") == "LOSS")
            _cpnl = sum(float(t.get("pnl_dollars", 0) or 0) for t in _ctr)
            _copen = len(_cst.get("open_trades", []))
            crypto_line = (
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\u26a1 *Crypto Sim (BTC/SOL, {int(_cst.get('leverage', 10))}x lev)*\n"
                f"  Balance: `${float(_cst.get('balance', 0)):,.2f}`\n"
                f"  Today: `{len(_ctr)}` closed ({_cw}W/{_cl}L) | P&L: `${_cpnl:+,.2f}`\n"
                f"  Open: `{_copen}`\n"
            )
    except Exception as _w97e:
        log.debug(f"W97 crypto session block: {_w97e}")
    if sim_line:
        lines.append(sim_line)
    if crypto_line:
        lines.append(crypto_line)

    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_history(u,c):
    """Show archived session data. /history [date] or /history to list."""
    if c.args:
        date_str = c.args[0].strip()
        # Try to load from archive
        archived = ot.load_archived_session(date_str)
        if not archived:
            # Maybe it's still in the live file
            archived = ot.get_session_trades(date_str)
        if not archived:
            await u.message.reply_text(f"No data found for session {date_str}.")
            return
        closed = [r for r in archived if r.get("status") == "CLOSED" and r.get("result") in ("WIN","LOSS")]
        wins = sum(1 for r in closed if r["result"] == "WIN")
        losses = sum(1 for r in closed if r["result"] == "LOSS")
        wr = round(wins / max(1, wins + losses) * 100, 1)
        icon = "🟢" if wr >= 55 else "🔴" if wr < 45 else "🟡"
        lines = [
            f"📜 *Session {date_str}*",
            f"━━━━━━━━━━━━━━━━━━",
            f"*Trades:* `{len(archived)}`",
            f"*W/L:* `{wins}W / {losses}L` {icon} {wr}% WR",
        ]
        if closed:
            lines.append(f"━━━━━━━━━━━━━━━━━━")
            for r in closed[:15]:
                r_icon = "✅" if r["result"] == "WIN" else "❌"
                lines.append(f"  {r_icon} {r.get('market')} {r.get('setup')} [{r.get('tf')}] | {r.get('direction')}")
        await u.message.reply_text("\n".join(lines), parse_mode="Markdown")
    else:
        dates = ot.list_archived_sessions()
        if not dates:
            await u.message.reply_text("No archived sessions yet — they appear after each 4PM market close.", parse_mode="Markdown")
            return
        # Auto-load the most recent session
        date_str = dates[-1]
        archived = ot.load_archived_session(date_str)
        if not archived:
            archived = ot.get_session_trades(date_str)
        closed = [r for r in archived if r.get("status") == "CLOSED" and r.get("result") in ("WIN","LOSS")]
        wins = sum(1 for r in closed if r["result"] == "WIN")
        losses = sum(1 for r in closed if r["result"] == "LOSS")
        wr = round(wins / max(1, wins + losses) * 100, 1)
        icon = "🟢" if wr >= 55 else "🔴" if wr < 45 else "🟡"
        lines = [
            f"📜 *Most Recent Session — {date_str}*",
            f"━━━━━━━━━━━━━━━━━━",
            f"*Trades:* `{len(archived)}`",
            f"*W/L:* `{wins}W / {losses}L` {icon} {wr}% WR",
        ]
        if closed:
            lines.append(f"━━━━━━━━━━━━━━━━━━")
            for r in closed[:12]:
                r_icon = "✅" if r["result"] == "WIN" else "❌"
                lines.append(f"  {r_icon} {r.get('market')} {r.get('setup')} | {r.get('direction')}")
        lines.append(f"━━━━━━━━━━━━━━━━━━")
        # List all available dates
        lines.append("*All sessions:* " + "  ".join(f"`{d}`" for d in dates[-10:]))
        lines.append("_Use `/history YYYY-MM-DD` for a specific day_")
        await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_lifetime(u,c):
    """Show lifetime stats across all sessions."""
    await u.message.reply_text(sim.lifetime_stats_text(), parse_mode="Markdown")

async def cmd_parole(u, c):
    """
    Wave 147 (_WAVE147_PAROLE_BOARD): /parole - the revival program's live
    board. Every benched setup's case: graded paper trades toward the
    10-trade bar, record, expectancy; then who is SERVING probation and who
    GRADUATED. Pure read - computes from the same functions the engine uses,
    so the board can never disagree with the machine.
    """
    try:
        import outcome_tracker as _ot147
        susp = _ot147.get_suspended_setups()
        prob = _ot147.get_probation()
        try:
            ev = _ot147.shadow_evidence_by_setup()
        except Exception:
            ev = {}
        L = ["PAROLE BOARD - earned, never given", ""]
        rows = []
        for key in susp.keys():
            e = ev.get(key)
            n = e["n"] if e else 0
            exp = e["expectancy"] if e else None
            rows.append((key, n, exp, e))
        rows.sort(key=lambda r: (-(r[1]), -(r[2] if r[2] is not None else -9)))
        shown = 0
        none_yet = 0
        for key, n, exp, e in rows:
            if n <= 0:
                none_yet += 1
                continue
            if shown >= 15:
                continue
            shown += 1
            bar = "%d/%d" % (min(n, 10), 10)
            if n >= 10 and exp is not None and exp > 0.0:
                L.append("  %s  %s  %dW/%dL  %+.2fR  <- PAROLE EARNED, frees on next board" %
                         (key, bar, e["wins"], e["losses"], exp))
            elif exp is not None and exp > 0.0:
                L.append("  %s  %s  %dW/%dL  %+.2fR  climbing" %
                         (key, bar, e["wins"], e["losses"], exp))
            else:
                L.append("  %s  %s  %dW/%dL  %+.2fR  bench holding" %
                         (key, bar, e["wins"], e["losses"], exp if exp is not None else -1.0))
        hidden = len(rows) - shown - none_yet
        if hidden > 0:
            L.append("  ... and %d more building cases" % hidden)
        if none_yet:
            L.append("  (%d benched setups have no graded paper evidence yet)" % none_yet)
        serving = {k: v for k, v in prob.items() if v.get("state") == "SERVING"}
        grads = {k: v for k, v in prob.items() if v.get("state") == "GRADUATED"}
        if serving:
            L.append("")
            L.append("SERVING PROBATION - proving it with real trades:")
            for k, v in sorted(serving.items()):
                L.append("  %s  paper %s%% WR over %s; judged after %s live trades" %
                         (k, v.get("shadow_wr", "?"), v.get("shadow_n", "?"),
                          v.get("judge_after_trades", "?")))
        if grads:
            L.append("")
            L.append("GRADUATED - earned their way back:")
            for k, v in sorted(grads.items()):
                L.append("  %s  trial %s on %s" %
                         (k, v.get("trial_record", "?"), (v.get("graduated_at", "") or "")[:10]))
        L.append("")
        L.append("Rules: 10+ graded paper trades, expectancy-positive -> probation. "
                 "5 live trades judge it. Graduation = clean slate. Bleeding real "
                 "money benches anyone instantly. /revert overrules anything.")
        text = "\n".join(L)
    except Exception as e:
        text = "Parole board unavailable: %s" % e
    await u.message.reply_text("```\n" + text[:3900] + "\n```", parse_mode="Markdown")

async def cmd_overrides(u, c):
    """
    Wave 143 (_WAVE143_LEARNED_OVERRIDES): /overrides - what have the hands
    learned? Active overrides with their proof and live-watch progress, plus
    reverted ones cooling down. Bounds are Wayne-signed law.
    """
    try:
        import learned_overrides as _lo
        text = _lo.status_text()
    except Exception as e:
        text = f"Overrides unavailable: {e}"
    await u.message.reply_text(f"```\n{text[:4000]}\n```", parse_mode="Markdown")

async def cmd_revert(u, c):
    """
    Wave 143: /revert [market] [setup] - Wayne's instant overrule. No args
    reverts every active learned override.
    """
    try:
        import learned_overrides as _lo
        args = list(c.args or [])
        text = _lo.manual_revert(*(args[:2]))
    except Exception as e:
        text = f"Revert failed: {e}"
    await u.message.reply_text(text[:4000])

async def cmd_ledger(u, c):
    """
    Wave 141 (_WAVE141_FILTER_LEDGER): /ledger - the filter ledger.
    Read-only aggregation of the graded shadow outcomes Wave 140
    unlocked: per filter, is the gate saving money or costing wins?
    Verdicts use the Wave-63 break-even + 25%-margin standard. The
    auto-adjust hands (Wave 142) will act only on READY verdicts,
    after data proof + Wayne\'s explicit sign-off.
    """
    await u.message.reply_text("\u23F3 Building the filter ledger...")
    try:
        import filter_ledger as _fl
        report = _fl.build_ledger_report()
        await u.message.reply_text(f"```\n{report[:4000]}\n```", parse_mode="Markdown")
    except Exception as e:
        await u.message.reply_text(f"Filter ledger failed: {e}")

async def cmd_performance(u, c):
    """
    Wave 46 (May 12, 2026): Multi-dimensional performance analytics.

    Cross-tabs outcomes.csv by setup, market, tier, hour, regime,
    direction. Surfaces calibration health (HIGH vs MEDIUM tier WR).

    Optional argument: days window (default 30, capped 90).
      /performance       -> last 30 days
      /performance 7     -> last 7 days
      /performance 90    -> last 90 days
    """
    days = 30
    try:
        if c.args and len(c.args) > 0:
            days = int(c.args[0])
    except Exception:
        days = 30
    try:
        text = ot.performance_text(days)
    except Exception as e:
        text = f"*PERFORMANCE*\nError: `{e}`"
    await u.message.reply_text(text, parse_mode="Markdown")

async def cmd_trend(u, c):
    """
    Wave 49 (May 12, 2026): Market trend direction across NQ/GC/BTC/SOL.

    Optional market arg (case insensitive, aliases supported):
      /trend           -> all markets, compact summary
      /trend NQ        -> NQ detailed (also: NASDAQ)
      /trend GC        -> GC detailed (also: GOLD)
      /trend BTC       -> BTC detailed (also: BITCOIN)
      /trend SOL       -> SOL detailed (also: SOLANA)

    Shows: direction label, HTF bias (HH_HL/LH_LL/MIXED), ADX strength,
    RSI level. Single-market view also includes per-timeframe breakdown.
    """
    market = None
    try:
        if c.args and len(c.args) > 0:
            market = c.args[0]
    except Exception:
        market = None
    try:
        text = ot.market_trend_text(market)
    except Exception as e:
        text = f"*MARKET TREND*\nError: `{e}`"
    # Wave 68: log this read + append self-graded accuracy so the bot learns
    # from its own directional opinions over time.
    try:
        _tm_markets = [market] if market else ["NQ", "GC", "BTC", "SOL"]
        for _mkt in _tm_markets:
            _frames = dl_get_frames(_mkt)
            _score, _ = ot.trend_score(_frames, _mkt)
            _px = None
            for _tf in ("15m", "1h", "4h", "1d"):
                _df = _frames.get(_tf) if _frames else None
                if _df is not None and len(_df) > 0:
                    _px = float(_df["Close"].iloc[-1]); break
            trend_memory.log_trend_read(_mkt, _score, _px)
        if market:
            _acc = trend_memory.accuracy_summary(market)
            if _acc:
                text = text + "\n\n_" + _acc + "_"
    except Exception as e:
        log.debug(f"trend log: {e}")
    await u.message.reply_text(text, parse_mode="Markdown")

async def cmd_journey(u, c):
    """
    Wave 49 (May 12, 2026): Eval account history (renamed from /trend).

    Original Wave 43 logic - shows your Topstep balance journey over
    the last N sessions: per-day balance, sparkline, slope, target%
    reached, pace to PASS, best/worst day.

    Optional argument: number of sessions (default 7, capped 30).
      /journey       -> last 7
      /journey 14    -> last 14

    Note: /trend now shows MARKET trend direction. This command
    shows your ACCOUNT journey.

    Reads data/eval_history.jsonl (Wave 42) and shows the journey
    over the last 7 sessions: per-day balance, sparkline, slope,
    target progress, pace, best/worst day.

    Optional argument: number of sessions (default 7, capped 30).
      /trend       -> last 7
      /trend 14    -> last 14
    """
    days = 7
    try:
        if c.args and len(c.args) > 0:
            days = int(c.args[0])
    except Exception:
        days = 7
    await u.message.reply_text(sim.eval_trend_text(days), parse_mode="Markdown")

async def cmd_eval(u, c):
    """
    Wave 33 (May 11, 2026): Topstep eval progression view.

    Single-screen status of the entire eval journey:
    balance, path to PASS, bust guardrails, pace, trade quality.
    """
    await u.message.reply_text(sim.eval_progression_text(), parse_mode="Markdown")

async def cmd_detections(u, c):
    """
    Show the last 20 DETECTED entries from strategy_log.csv with full context.
    Optionally filter by market: /detections NQ
    """
    import csv as _csv
    log_path = os.path.join(BASE_DIR, "data", "strategy_log.csv")
    if not os.path.exists(log_path):
        await u.message.reply_text("No strategy log yet — bot hasn't scanned.")
        return

    market_filter = (c.args[0].upper() if c.args else "").strip() or None

    try:
        with open(log_path, newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
    except Exception as e:
        await u.message.reply_text(f"❌ Could not read log: {e}")
        return

    detected = [r for r in rows if r.get("decision") == "DETECTED"]
    if market_filter:
        detected = [r for r in detected if r.get("market") == market_filter]

    if not detected:
        await u.message.reply_text(
            f"No recent detections{' for ' + market_filter if market_filter else ''}."
        )
        return

    lines = [
        f"🔭 *Last 20 Detections"
        f"{' — ' + market_filter if market_filter else ''}*",
        "━━━━━━━━━━━━━━━━━━",
    ]

    # Find the last 20 DETECTED rows AND remember their position so we can
    # look forward in the rows list to find the matching outcome.
    recent_with_idx = []
    count = 0
    for i in range(len(rows) - 1, -1, -1):
        if rows[i].get("decision") == "DETECTED":
            if market_filter and rows[i].get("market") != market_filter:
                continue
            recent_with_idx.append((i, rows[i]))
            count += 1
            if count >= 20:
                break
    recent_with_idx.reverse()  # oldest first

    for idx, det_row in recent_with_idx:
        mkt = det_row.get("market", "?")
        setup = det_row.get("setup_type", "?")
        tf = det_row.get("tf", "?")
        direction = det_row.get("direction", "?")
        ts = det_row.get("timestamp", "")[:16].replace("T", " ")

        # Indicator snapshot line
        adx_v = det_row.get("adx", "?")
        rsi_v = det_row.get("rsi", "?")
        sk = det_row.get("stoch_k", "")
        mh = det_row.get("macd_hist", "")

        indicators = f"ADX {adx_v} | RSI {rsi_v}"
        if sk: indicators += f" | Stoch {sk}"
        if mh: indicators += f" | MACD hist {mh}"

        # Outcome lookup: scan forward in rows for same setup
        outcome_icon = "❓"
        outcome_note = "no follow-up"
        for j in range(idx + 1, min(idx + 15, len(rows))):
            follow = rows[j]
            if (follow.get("market") == mkt
                    and follow.get("setup_type") == setup
                    and follow.get("tf") == tf):
                dec = follow.get("decision", "")
                if dec == "FIRED":
                    outcome_icon = "🟢"
                    outcome_note = f"FIRED conv {follow.get('conviction','?')}"
                    break
                elif dec == "REJECTED":
                    outcome_icon = "❌"
                    outcome_note = (follow.get("reject_reason", "rejected") or "rejected")[:50]
                    break
                elif dec == "ALMOST":
                    outcome_icon = "🟡"
                    outcome_note = f"ALMOST conv {follow.get('conviction','?')}"
                    break
                elif dec == "REJECTED_SUSPENDED":
                    outcome_icon = "⛔"
                    outcome_note = "suspended shadow-log"
                    break

        reason = det_row.get("detection_reason", "")
        if len(reason) > 80:
            reason = reason[:77] + "..."

        lines.append(f"{outcome_icon} `{mkt} {setup}` [{tf}] {direction} | {ts}")
        lines.append(f"   {indicators}")
        if reason:
            lines.append(f"   _{_md(reason)}_")
        lines.append(f"   → {outcome_note}")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"_Total DETECTED rows: {len(detected)}_")
    lines.append(f"_Usage: /detections or /detections NQ|GC|BTC|SOL_")

    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:3900] + "\n\n_...truncated_"
    await u.message.reply_text(msg, parse_mode="Markdown")


async def cmd_rejected(u, c):
    """Task 5: Show last 10 rejected or almost-fired scan decisions."""
    import csv as _csv
    log_path = os.path.join(BASE_DIR, "data", "strategy_log.csv")
    if not os.path.exists(log_path):
        await u.message.reply_text("No strategy log yet.")
        return
    try:
        with open(log_path, newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
    except Exception as e:
        await u.message.reply_text(f"❌ Failed to read strategy log: {e}")
        return

    # Filter to REJECT* or ALMOST decisions, take the last 10
    flagged = [r for r in rows if "REJECT" in r.get("decision", "") or r.get("decision") == "ALMOST"]
    recent = flagged[-10:]

    if not recent:
        await u.message.reply_text("🔍 No recent rejections to show.")
        return

    lines = [
        "🔍 *Recent Rejections (last 10)*",
        "━━━━━━━━━━━━━━━━━━",
    ]
    for r in recent:
        decision = r.get("decision", "")
        if decision == "ALMOST":
            emoji = "🟡"
        else:
            emoji = "❌"
        mkt   = r.get("market", "?")
        setup = r.get("setup_type", "?")
        tf    = r.get("tf", "?")
        reason = r.get("reject_reason", "") or "no reason logged"
        # Trim long reasons for mobile
        if len(reason) > 60:
            reason = reason[:57] + "..."
        lines.append(f"{emoji} `{mkt} {setup} {tf}` — {_md(reason)}")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"_Showing {len(recent)} of {len(flagged)} total flagged decisions._")

    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_help(u,c):
    """
    Wave 17 (May 9, 2026): Cleaned to remove obsolete WATCH/HEADS UP
    references killed by Wave 14. Points to /commands for full list.
    """
    await u.message.reply_text(
        "🤖 *NQ CALLS — Quick Guide*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🟢 *Alerts:* only confirmed entries (LONG / SHORT)\n"
        "🔥 HIGH 80+   ✅ MEDIUM 65-79   ⚡ LOW 50-64\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📍 Entry | 🛑 Stop | 🎯 Target | 📦 Size by tier\n"
        "🔭 Trend (-10 to +10) | ADX | RSI | Volume\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🌅 8:30am Morning   🌙 6pm Asia   📋 8pm Report\n"
        "📡 /brief anytime — live market analysis\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "*Most-used:*\n"
        "`/open`  `/stats`  `/session`  `/edge`  `/diag`\n"
        "\n"
        "*Full command list:*  /commands\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ Not financial advice. Manage your risk.",
        parse_mode="Markdown")


async def cmd_side(u, c):
    """Wave 162 (_WAVE162_SIDE_TRUTH): /side - which side is paying, per
    market, straight from the Wave-161 direction gate's rolling window.
    Shows the SAME numbers the gate acts on - one source of truth."""
    try:
        try:
            _w161_refresh_side_exp()
        except Exception:
            pass
        L = ["\U0001f9ed SIDE TRUTH - last %d graded shadows per side" % _W161_WINDOW,
             "\u2501" * 27]
        for _m in ("NQ", "GC", "BTC", "SOL"):
            for _d in ("LONG", "SHORT"):
                _e = _W161_SIDE_LIVE.get("%s:%s" % (_m, _d))
                if _e is None:
                    L.append("%-3s %-5s    --     thin data (gate open)" % (_m, _d))
                elif _e <= _W161_GATE_EXP:
                    L.append("%-3s %-5s %+0.2fR  GATED" % (_m, _d, _e))
                else:
                    L.append("%-3s %-5s %+0.2fR  firing" % (_m, _d, _e))
        L.append("")
        L.append("Gate: last-%d exp <= %+0.2fR (%d+ graded) pauses that side."
                 % (_W161_WINDOW, _W161_GATE_EXP, _W161_MIN_N))
        L.append("Paused sides keep grading shadows, so they free themselves.")
        await u.message.reply_text("\n".join(L))
    except Exception as _e2:
        try:
            await u.message.reply_text("side view failed: %s" % _e2)
        except Exception:
            pass


async def cmd_commands(u, c):
    """
    Wave 17 (May 9, 2026): Comprehensive categorized command list.
    Replaces the partial command listing that was crammed into /help.
    Six categories cover all ~35 registered commands.
    """
    text = (
        "🤖 *NQ CALLS — All Commands*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📊 *TRADING*\n"
        "`/open`  — list open trades\n"
        "`/win [id]`  — mark trade as won\n"
        "`/loss [id]`  — mark trade as lost\n"
        "`/skip [id]`  — mark trade as skipped\n"
        "\n"
        "📈 *STATS & ANALYSIS*\n"
        "`/stats`  — overall stats\n"
        "`/session`  — current session\n"
        "`/history [date]`  — past sessions\n"
        "`/lifetime`  — lifetime totals\n"
        "`/edge`  — per-setup win rate\n"
        "`/setups`  — active setup catalog\n"
        "`/journal [N]`  — recent trade lessons\n"
        "`/detections [mkt]`  — recent detections\n"
        "`/rejected`  — recent rejections\n"
        "`/backtest`  — replay closed trades\n"
        "`/review [days]`  — strategy review\n"
        "`/analyze`  — strategy log patterns\n"
        # Wave 51 (May 13, 2026): added /performance, /journey, /trend
        "`/performance` — multi-dim WR analytics\n"
        "`/journey`  — eval account history sparkline\n"
        "`/trend [MKT]`  — live market trend (NQ/GC/BTC/SOL)\n"
        "\n"
        "📋 *REPORTS*\n"
        "`/report`  — full daily report\n"
        "`/recap`  — quick session recap\n"
        "`/brief`  — live market brief\n"
        "`/dashboard`  — HTML dashboard\n"
        "\n"
        "💰 *SIM ACCOUNT*\n"
        "`/simstatus`  — sim status\n"
        "`/simon`  `/simoff`  — toggle sim\n"
        "`/simreset [preset]`  — reset 50k/100k/150k\n"
        "`/mnq`  — toggle MNQ vs NQ\n"
        "`/simweekly`  — weekly breakdown\n"
        "`/cryptostatus`  — crypto sim\n"
        "\n"
        "🧠 *AUTO-TUNING*\n"
        "`/tune [l3]`  — manual auto-tune\n"
        "`/recalibrate`  — daily soft-tune\n"
        "`/wave7`  — layer status\n"
        "\n"
        # Wave 155 (_WAVE155_UI_REFRESH): surface the learning + bench
        # commands that were registered but missing from this menu.
        "🎓 *LEARNING & BENCH*\n"
        "`/suspended`  — benched setups & why\n"
        "`/parole`  — setups earning their way back\n"
        "`/ledger`  — what the gates are learning\n"
        "`/overrides`  — active learned adjustments\n"
        "`/revert [id]`  — undo a learned adjustment\n"
        "`/side`  \u2014 which side is paying + gate state\n"
        "\n"
        "⚙️ *UTILITY*\n"
        "`/diag`  — bot health check\n"
        "`/sync`  — push data to GitHub\n"
        "`/pulldata`  — data freshness\n"
        "`/menu`  — main menu\n"
        "`/start` `/help` `/commands`\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "_Type / in chat for native command dropdown._"
    )
    await u.message.reply_text(text, parse_mode="Markdown")

async def cmd_dashboard(u,c):
    """Wave 19 (May 9, 2026): regenerate live dashboard and send URL."""
    await u.message.reply_text("⏳ Refreshing dashboard...")
    try:
        import generate_dashboard
        await asyncio.to_thread(generate_dashboard.main)
        outcomes = dash.load_outcomes()
        closed = [r for r in outcomes if r.get("status") == "CLOSED" and r.get("result") in ("WIN", "LOSS")]
        wins = sum(1 for r in closed if r.get("result") == "WIN")
        losses = sum(1 for r in closed if r.get("result") == "LOSS")
        wr = round(wins / max(1, wins + losses) * 100, 1) if closed else 0
        open_n = sum(1 for r in outcomes if r.get("status") == "OPEN")
        bar = "━" * 18
        await u.message.reply_text(
            f"📊 *Dashboard Refreshed*\n{bar}\n"
            f"*All-time:* {wins}W / {losses}L ({wr}% WR)\n"
            f"*Total alerts:* {len(outcomes)}\n"
            f"*Open trades:* {open_n}\n{bar}\n"
            "🌐 [Open Dashboard](https://kdubsk1.github.io/bot/dashboard.html)\n"
            "_Auto-refreshes every 5 min._",
            parse_mode="Markdown",
            disable_web_page_preview=True)
    except Exception as e:
        await u.message.reply_text(f"❌ Dashboard error: {e}")

async def cmd_review(u,c):
    days = 7
    if c.args:
        try: days = int(c.args[0])
        except: pass
    await u.message.reply_text(f"⏳ Running strategy review ({days} days)...")
    try:
        report = sr.run_review(days)
        if "SECTION 5:" in report:
            suggestions = report.split("SECTION 5:")[1]
            if "QUICK SUMMARY" in suggestions:
                suggestions = suggestions.split("QUICK SUMMARY")[0]
            msg = f"🔬 *Strategy Review — {days} days*\n━━━━━━━━━━━━━━━━━━\n```\n{suggestions[:3500]}\n```"
        else:
            msg = f"🔬 *Strategy Review — {days} days*\n```\n{report[:3800]}\n```"
        await tg_send(c.application, msg)
        await u.message.reply_text("✅ Review complete!", parse_mode="Markdown")
    except Exception as e:
        await u.message.reply_text(f"❌ Review error: {e}")

async def cmd_brief(u,c):
    await u.message.reply_text("⏳ Scanning markets...")
    try:
        from live_brief import generate_live_brief
        active = [m for m in ALL_MARKETS if SETTINGS["markets"].get(m)]
        for m in active:
            try:
                frames = get_frames(m)
                msg = generate_live_brief(m, frames)
                await tg_send(c.application, msg)
            except Exception as e:
                log.warning(f"Brief {m}: {e}")
        await u.message.reply_text("✅ Live briefs sent!")
    except Exception as e:
        await u.message.reply_text(f"❌ {e}")

# ── Inline button handler ─────────────────────────────────────────
async def on_button(u, c):
    q=u.callback_query; await q.answer(); d=q.data

    if   d=="toggle_scan":    SETTINGS["scanner_on"]=not SETTINGS["scanner_on"]; _save_scanner_state(); sim.toggle_sim(SETTINGS["scanner_on"]); crypto_sim.set_enabled(SETTINGS["scanner_on"])
    elif d=="toggle_rescore": SETTINGS["rescore_on"]=not SETTINGS["rescore_on"]
    elif d in ("toggle_NQ","toggle_GC","toggle_BTC","toggle_SOL"):
        SETTINGS["markets"][d.split("_")[1]]=not SETTINGS["markets"][d.split("_")[1]]
    elif d=="set_conv":  SETTINGS["min_conviction"]    =_cycle(SETTINGS["min_conviction"],CYCLE_CONV)
    elif d=="set_rr":    SETTINGS["min_rr"]            =_cycle(SETTINGS["min_rr"],CYCLE_RR)
    elif d=="set_int":   SETTINGS["scan_interval_min"] =_cycle(SETTINGS["scan_interval_min"],CYCLE_INT)
    elif d=="set_cd":    SETTINGS["cooldown_min"]      =_cycle(SETTINGS["cooldown_min"],CYCLE_CD)
    elif d=="set_risk":  SETTINGS["account_risk_pct"]  =_cycle(SETTINGS["account_risk_pct"],CYCLE_RISK); ot.set_account_risk_pct(SETTINGS["account_risk_pct"])
    elif d=="toggle_sim":     sim.toggle_sim(not sim.load_state().get("enabled",False))
    elif d=="toggle_mnq":     sim.toggle_mnq(not sim.load_state().get("use_mnq",False))
    elif d=="simreset_50k":   sim.reset_sim("50k")
    elif d=="simreset_100k":  sim.reset_sim("100k")
    elif d=="simreset_150k":  sim.reset_sim("150k")
    elif d=="simreset_current":
        current = sim.load_state().get("preset","50k")
        cycle   = {"50k":"100k","100k":"150k","150k":"50k"}
        next_p  = cycle.get(current, "50k")
        sim.reset_sim(next_p); st=sim.load_state()
        await q.message.reply_text(
            f"✅ *Sim reset — {st['preset'].upper()}*\nBalance: `${st['balance']:,.2f}`\nDaily limit: `${st['daily_loss_limit']:,.2f}`",
            parse_mode="Markdown")
        return
    # Wave 37 (May 11, 2026): explicit reset buttons with 2-step confirmation.
    elif d=="reset_topstep_confirm":
        st_cur = sim.load_state()
        preset_cur = st_cur.get("preset", "50k").upper()
        bal_cur = float(st_cur.get("balance", 50_000.0))
        confirm_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, reset", callback_data="reset_topstep_yes"),
             InlineKeyboardButton("❌ Cancel",     callback_data="reset_cancel")],
        ])
        await q.message.reply_text(
            f"⚠️ *Reset Topstep {preset_cur}?*\n"
            f"Current balance: `${bal_cur:,.2f}`\n"
            f"\nThis will:\n"
            f"  • Reset balance to preset starting value\n"
            f"  • Clear all open + closed sim trades\n"
            f"  • Reset today_pnl and total_pnl to 0\n"
            f"  • PRESERVE lifetime_stats.json (combine history)\n"
            f"\nUse this when starting a fresh eval attempt.",
            parse_mode="Markdown",
            reply_markup=confirm_kb,
        )
        return
    elif d=="reset_topstep_yes":
        cur_preset = sim.load_state().get("preset", "50k")
        sim.reset_sim(cur_preset)  # reset to CURRENT preset (no cycling)
        st_new = sim.load_state()
        await q.message.reply_text(
            f"✅ *Topstep Reset Complete*\n"
            f"Preset: `{st_new['preset'].upper()}`\n"
            f"Balance: `${st_new['balance']:,.2f}`\n"
            f"Daily limit: `${st_new['daily_loss_limit']:,.2f}`\n"
            f"Max DD: `${st_new['max_drawdown']:,.2f}`\n"
            # Wave 52 (May 13, 2026): trade history now preserved across reset.
            f"\n_Lifetime stats + trade history preserved._",
            parse_mode="Markdown",
        )
        return
    elif d=="reset_crypto_confirm":
        try:
            cs_state = crypto_sim.load_crypto_state()
            cur_bal = float(cs_state.get("balance", 0))
            start_bal = float(cs_state.get("starting_balance", 1000))
            n_closed = len(cs_state.get("closed_trades", []))
            n_open = len(cs_state.get("open_trades", []))
        except Exception:
            cur_bal, start_bal, n_closed, n_open = 0, 1000, 0, 0
        confirm_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, reset", callback_data="reset_crypto_yes"),
             InlineKeyboardButton("❌ Cancel",     callback_data="reset_cancel")],
        ])
        await q.message.reply_text(
            f"⚠️ *Reset Crypto Sim?*\n"
            f"Current balance: `${cur_bal:,.2f}` of `${start_bal:,.2f}` starting\n"
            f"Closed trades: `{n_closed}`  ·  Open: `{n_open}`\n"
            f"\nThis will:\n"
            f"  • Reset balance to `$1,000`\n"
            f"  • Clear all open + closed crypto trades\n"
            f"\nUse this after Wave 31 to get clean data.",
            parse_mode="Markdown",
            reply_markup=confirm_kb,
        )
        return
    elif d=="reset_crypto_yes":
        try:
            crypto_sim.reset_crypto_account()
            new_state = crypto_sim.load_crypto_state()
            await q.message.reply_text(
                f"✅ *Crypto Sim Reset Complete*\n"
                f"Balance: `${float(new_state.get('balance', 1000)):,.2f}`\n"
                f"Leverage: `{new_state.get('leverage', 10)}x`\n"
                f"Risk per trade: `{new_state.get('account_risk_pct', 1.5)}%`\n"
                # Wave 52 (May 13, 2026): trade history preserved across reset.
                f"\n_Trade history preserved_ — fresh balance for next round.",
                parse_mode="Markdown",
            )
        except Exception as _w37_e:
            await q.message.reply_text(f"❌ Crypto reset failed: `{_w37_e}`", parse_mode="Markdown")
        return
    elif d=="reset_cancel":
        await q.message.reply_text("❌ Reset cancelled. Nothing changed.")
        return
    elif d=="sim_status":
        await q.message.reply_text(sim.sim_status_text(),parse_mode="Markdown"); return
    elif d=="sim_weekly":
        await q.message.reply_text(sim.sim_period_text(7),parse_mode="Markdown"); return
    elif d in ("trade_win","trade_loss","trade_skip"):
        result={"trade_win":"WIN","trade_loss":"LOSS","trade_skip":"SKIP"}[d]
        trades=ot.load_open_trades()
        if not trades: await q.message.reply_text("No open trades."); return
        match=trades[-1]
        exit_p=match.get("target",0) if result=="WIN" else match.get("stop",0) if result=="LOSS" else 0
        ot.update_result(match["alert_id"],result,0,exit_p)
        if result in ("WIN","LOSS"): ot.record_trade_result(match["market"],match["setup"],result)
        # Batch 2A: Log outcome to strategy_log.csv
        if result in ("WIN","LOSS"):
            try:
                ot._log_trade_outcome(match, result, exit_p)
            except Exception:
                pass
        icons={"WIN":"✅","LOSS":"❌","SKIP":"⏭"}
        await q.message.reply_text(f"{icons[result]} *{result}* — {match.get('market')} | {_md(match.get('setup',''))}\nLearning updated.",parse_mode="Markdown"); return
    elif d=="status":
        # Wave 19: delegated to shared _build_status_text helper
        await q.message.reply_text(_build_status_text(), parse_mode="Markdown"); return
    elif d=="stats":
        await q.message.reply_text(ot.print_stats(session_only=True),parse_mode="Markdown"); return
    elif d=="session":
        # Reuse cmd_session logic inline
        sid = get_session_date()
        summary = ot.build_session_summary(sid)
        trades = ot.get_session_trades(sid)
        open_ = [r for r in trades if r.get("status") == "OPEN"]
        setups_fired = list(set(r.get("setup","?") for r in trades))
        sim_state = sim.load_state()
        sim_line = ""
        if sim_state.get("enabled"):
            risk = sim.check_risk_limits(sim_state)
            sim_line = (
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\U0001f3e6 *Topstep Futures Sim (NQ/GC)*\n"
                f"  Balance: `${risk['balance']:,.2f}`\n"
                f"  Session P&L: `${risk['daily_pnl']:+,.2f}`\n"
                f"  Daily limit left: `${risk['daily_left']:,.2f}`\n"
            )
        icon = "🟢" if summary["win_rate"] >= 55 else "🔴" if summary["win_rate"] < 45 else "🟡"
        pnl_r = summary["total_pnl_r"]
        pnl_str = f"+{pnl_r}R" if pnl_r >= 0 else f"{pnl_r}R"
        msg_lines = [
            f"📊 *Session {sid}*", "━━━━━━━━━━━━━━━━━━",
            f"*Trades:* `{summary['total_trades']}` | *Open:* `{len(open_)}`",
            f"*W/L:* `{summary['wins']}W / {summary['losses']}L` {icon} {summary['win_rate']}% WR",
            f"*P&L:* `{pnl_str}`",
            f"*Markets:* {', '.join(summary['markets_traded']) if summary['markets_traded'] else 'None'}",
        ]
        # Wave 19: per-market breakdown (mirrors cmd_session)
        _by_mkt = summary.get("by_market", {})
        if _by_mkt:
            _mkt_lines = []
            for _m in ("NQ", "GC", "BTC", "SOL"):
                if _m in _by_mkt:
                    _d = _by_mkt[_m]
                    _w = _d.get("wins", 0); _l = _d.get("losses", 0); _pnl = _d.get("pnl_r", 0.0)
                    _pnl_s = f"+{_pnl}R" if _pnl >= 0 else f"{_pnl}R"
                    _open_n = _d.get("open", 0)
                    _open_s = f" ({_open_n} open)" if _open_n else ""
                    _mkt_lines.append(f"  {_m}: {_w}W/{_l}L {_pnl_s}{_open_s}")
            if _mkt_lines:
                msg_lines.append("*Per-market today:*")
                msg_lines.extend(_mkt_lines)
        if setups_fired: msg_lines.append(f"*Setups:* {', '.join(setups_fired[:8])}")
        # Wave 97: crypto block (button view mirrors /session)
        crypto_line = ""
        try:
            _cst = crypto_sim.load_crypto_state()
            if _cst.get("enabled"):
                _ctoday = datetime.now().strftime("%Y-%m-%d")
                _ctr = [t for t in _cst.get("closed_trades", []) if _ctoday in str(t.get("closed_at", ""))]
                _cw = sum(1 for t in _ctr if t.get("result") == "WIN")
                _cl = sum(1 for t in _ctr if t.get("result") == "LOSS")
                _cpnl = sum(float(t.get("pnl_dollars", 0) or 0) for t in _ctr)
                _copen = len(_cst.get("open_trades", []))
                crypto_line = (
                    f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                    f"\u26a1 *Crypto Sim (BTC/SOL, {int(_cst.get('leverage', 10))}x lev)*\n"
                    f"  Balance: `${float(_cst.get('balance', 0)):,.2f}`\n"
                    f"  Today: `{len(_ctr)}` closed ({_cw}W/{_cl}L) | P&L: `${_cpnl:+,.2f}`\n"
                    f"  Open: `{_copen}`\n"
                )
        except Exception as _w97e:
            log.debug(f"W97 crypto session block: {_w97e}")
        if sim_line: msg_lines.append(sim_line)
        if crypto_line: msg_lines.append(crypto_line)
        await q.message.reply_text("\n".join(msg_lines), parse_mode="Markdown"); return
    elif d=="history_list":
        dates = ot.list_archived_sessions()
        if not dates:
            await q.message.reply_text("No archived sessions yet.\nUse `/history YYYY-MM-DD` in chat.", parse_mode="Markdown")
        else:
            lines = ["📜 *Session Archives:*", "━━━━━━━━━━━━━━━━━━"]
            for dt in dates[-15:]:
                lines.append(f"  `{dt}`")
            lines.append("━━━━━━━━━━━━━━━━━━")
            lines.append("Use `/history YYYY-MM-DD` to view.")
            await q.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return
    elif d=="lifetime":
        await q.message.reply_text(sim.lifetime_stats_text(), parse_mode="Markdown"); return
    elif d=="open_trades":
        trades=ot.load_open_trades()
        if not trades: await q.message.reply_text("No open trades."); return
        lines=["📋 *Open Trades:*\n"]
        for i,t in enumerate(trades,1):
            lines.append(f"*{i}.* {t.get('market')} | {t.get('setup')}\nEntry:`{t.get('entry')}` Target:`{t.get('target')}`\nID:`{t.get('alert_id')}`\n")
        await q.message.reply_text("\n".join(lines),parse_mode="Markdown"); return
    elif d=="learning":
        edge_summary = sim.get_edge_summary()
        learn_summary = ot.get_learning_summary()
        await q.message.reply_text(edge_summary + "\n\n" + learn_summary, parse_mode="Markdown"); return
    elif d=="analyze":
        await q.message.reply_text("⏳ Analyzing...")
        try:
            report=sl.build_strategy_analysis()
            await q.message.reply_text(f"```\n{report[:4000]}\n```",parse_mode="Markdown")
            await q.message.reply_text("Paste to Claude to review.",parse_mode="Markdown")
        except Exception as e: await q.message.reply_text(f"❌ {e}")
        return
    elif d=="live_brief":
        await q.message.reply_text("⏳ Scanning...")
        try:
            from live_brief import generate_live_brief
            active = [m for m in ALL_MARKETS if SETTINGS["markets"].get(m)]
            for m in active:
                try:
                    frames = get_frames(m)
                    msg = generate_live_brief(m, frames)
                    await tg_send(c.application, msg)
                except Exception as e:
                    log.warning(f"Brief {m}: {e}")
            await q.message.reply_text("✅ Sent!")
        except Exception as e:
            await q.message.reply_text(f"❌ {e}")
        return
    elif d=="brief_morning":
        await q.message.reply_text("⏳ Building...")
        await tg_send(c.application,build_morning_brief()); await q.message.reply_text("✅ Sent!"); return
    elif d=="brief_asia":
        await q.message.reply_text("⏳ Building...")
        await tg_send(c.application,build_asia_brief()); await q.message.reply_text("✅ Sent!"); return
    elif d=="report_now":
        await q.message.reply_text("⏳ Building...")
        try:
            _,short=ot.build_daily_report(); await tg_send(c.application,short); await q.message.reply_text("✅ Sent!"); return
        except Exception as e: await q.message.reply_text(f"❌ {e}"); return
    elif d=="test":
        await tg_send(c.application,
            "🟢 *ENTER NOW — NQ LONG*\n"
            "📊 📈 LONG  |  *NQ Futures*  |  [15m]\n✅ Tier: *MEDIUM*  |  Conviction: *72/100*\n"
            "🔭 Trend: `+4`  |  ADX: `28.5`  |  RSI: `54.2`\n━━━━━━━━━━━━━━━━━━\n"
            "📍 *Entry:*  `19,500`\n🛑 *Stop:*   `19,430`  ← place immediately\n"
            "🎯 *Target:* `19,745` (swing level, 3.5R)\n"
            "📦 *Size:* 3 MNQ\n"
            "━━━━━━━━━━━━━━━━━━\n📋 *Chart Read:*\nTest alert — bot is working! 🎉\n━━━━━━━━━━━━━━━━━━\n"
            "⚠️ Not financial advice. Manage your risk."); return
    elif d=="rr_info":
        await q.message.reply_text(
            "⚖️ *Dynamic R:R System*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🔥 HIGH (80+) → 1.5R min | 5 MNQ\n"
            "✅ MEDIUM (65-79) → 2.0R min | 3 MNQ\n"
            "⚡ LOW (50-64) → 2.5R min | 1 MNQ\n\n"
            "Targets = real swing levels only.\n"
            "Max size: 5 MNQ or 1 full NQ contract.",
            parse_mode="Markdown"); return
    elif d=="help":
        await q.message.reply_text(
            "🤖 *Quick Guide*\n━━━━━━━━━━━━━━━━━━\n"
            "🟢 ENTER NOW — enter immediately\n"
            "👀 HEADS UP — get ready\n"
            "🔥 HIGH | ✅ MEDIUM | ⚡ LOW\n━━━━━━━━━━━━━━━━━━\n"
            "Everything is in the menu. Type /help for full guide.\n"
            "━━━━━━━━━━━━━━━━━━\n⚠️ Not financial advice.",parse_mode="Markdown"); return
    elif d=="dashboard":
        # Wave 19: regen + URL (kept as fallback for users with old menu cached)
        await q.message.reply_text("⏳ Refreshing dashboard...")
        try:
            import generate_dashboard
            await asyncio.to_thread(generate_dashboard.main)
            outcomes = dash.load_outcomes()
            closed = [r for r in outcomes if r.get("status") == "CLOSED" and r.get("result") in ("WIN", "LOSS")]
            wins = sum(1 for r in closed if r.get("result") == "WIN")
            losses = sum(1 for r in closed if r.get("result") == "LOSS")
            wr = round(wins / max(1, wins + losses) * 100, 1) if closed else 0
            bar = "━" * 18
            await q.message.reply_text(
                f"📊 *Dashboard Refreshed*\n{bar}\n"
                f"*All-time:* {wins}W / {losses}L ({wr}% WR)\n"
                f"*Total:* {len(outcomes)} alerts\n{bar}\n"
                "🌐 [Open Dashboard](https://kdubsk1.github.io/bot/dashboard.html)",
                parse_mode="Markdown",
                disable_web_page_preview=True)
        except Exception as e:
            await q.message.reply_text(f"❌ {e}")
        return
    elif d=="review":
        await q.message.reply_text("⏳ Running 7-day strategy review...")
        try:
            report = sr.run_review(7)
            if "SECTION 5:" in report:
                suggestions = report.split("SECTION 5:")[1]
                if "QUICK SUMMARY" in suggestions:
                    suggestions = suggestions.split("QUICK SUMMARY")[0]
                msg = f"🔬 *Strategy Review*\n```\n{suggestions[:3500]}\n```"
            else:
                msg = f"🔬 *Review*\n```\n{report[:3800]}\n```"
            await tg_send(c.application, msg)
        except Exception as e:
            await q.message.reply_text(f"❌ {e}")
        return

    await q.edit_message_reply_markup(reply_markup=main_menu())

# ── Session Clock Instance ────────────────────────────────────────
SESSION_CLOCK = SessionClock()

def _on_pre_flatten(event, now_et):
    """Pre-flatten handler — sets flag for async flatten on next scan tick."""
    global _FLATTEN_PENDING
    _FLATTEN_PENDING = True
    log.info("Pre-flatten event fired — will flatten futures on next scan tick")

def _on_session_close(event, now_et):
    """Session close handler - archives session, rolls sim (eval-aware carry, Wave 85), resets daily gates."""
    global _SESSION_CLOSE_SUMMARY, _SUSPENSION_CHANGES, _RECAP_PENDING
    global DAILY_LOSS_GATE, DAILY_PROFIT_LOCKED, DAILY_TRADE_COUNT
    try:
        # Wave 56 (May 13, 2026): use closing-session calendar date.
        # get_session_date(now_et) returns NEXT day when hour>=16, but the
        # session that just closed is today's date. Trades fired during the
        # day were tagged with today's date via get_session_date() (no arg).
        sid = now_et.date().strftime("%Y-%m-%d")
        summary = ot.build_session_summary(sid)
        sim_state = sim.load_state()
        sim_pnl = sim_state.get("today_pnl", 0.0)

        # Task 2: Auto-expire stale OPEN trades
        try:
            expired = ot.auto_expire_stale_trades(max_hours=24)
            if expired:
                log.info(f"Session close: auto-expired {len(expired)} stale trade(s)")
        except Exception as e:
            log.error(f"Session close auto-expire: {e}")

        # Task 4: Roll over open trades instead of archiving them
        open_trades = ot.load_open_trades()
        rolled = 0
        if open_trades:
            for t in open_trades:
                t["rolled_over"] = "True"
            rolled = len(open_trades)
            log.info(f"Session close: {rolled} open trades rolled over to new session")

        # Pre-Batch 2026-04-20: Generate daily recap markdown + Telegram summary
        # IMPORTANT: do this BEFORE sim.reset_sim() so recap captures the actual
        # session balance/PnL (after reset, sim_state would show $50k fresh).
        try:
            from session_recap import generate_recap
            from datetime import datetime as _dt
            try:
                from zoneinfo import ZoneInfo as _ZI
                _et = _dt.now(_ZI("America/New_York"))
            except Exception:
                import pytz as _pytz
                _et = _dt.now(_pytz.timezone("America/New_York"))
            recap_path, recap_tg = generate_recap(_et.date())
            log.info(f"Pre-Batch: Session recap written to {recap_path}")
            _RECAP_PENDING = {"path": str(recap_path), "tg_text": recap_tg}
        except Exception as e:
            log.error(f"Pre-Batch: Recap generation failed (non-fatal): {e}")
            _RECAP_PENDING = None

        ot.archive_session(sid)
        # Wave 85 (Jul 2, 2026): THE REAL DAILY-RESET KILLER. The hard
        # sim.reset_sim() here predated the Wave-30 carry logic and wiped the
        # whole eval account (balance AND trades) to a fresh $50k at EVERY 6PM
        # roll, bypassing check_eval_outcome entirely. W82 fixed the other
        # path; this was the second one. A real Topstep combine only ends on
        # PASSED_TARGET or BUSTED_MAX_DD - sim_account's own W82-corrected
        # session roll handles those and CARRIES the balance otherwise.
        try:
            _w85_state = sim.load_state()  # session roll runs inside load_state
            log.info("W85 session roll: balance carries at $%.2f" % float(_w85_state.get("balance", 0)))
        except Exception as _w85e:
            log.error(f"W85 session roll failed (non-fatal): {_w85e}")
        changes = ot.check_and_update_suspensions()
        _SESSION_CLOSE_SUMMARY = {"sid": sid, "summary": summary, "sim_pnl": sim_pnl, "rolled": rolled}
        _SUSPENSION_CHANGES = changes

        # Task 8: Reset ALL daily gates
        DAILY_LOSS_GATE = False
        DAILY_PROFIT_LOCKED = False
        DAILY_TRADE_COUNT = 0
        for m in ("NQ", "GC"):
            MARKET_HALTED.pop(m, None)
            CONSECUTIVE_LOSSES.pop(m, None)

        log.info(f"Session close: archived {sid}, sim rolled (carry), gates cleared, {len(changes)} suspension changes")
    except Exception as e:
        log.error(f"_on_session_close error: {e}")

def _on_crypto_day(event, now_et):
    """Crypto day boundary — reset daily crypto stats and clear halts."""
    try:
        # Wave 56 (May 13, 2026): restore MARKET_HALTED + CONSECUTIVE_LOSSES
        # clearing for BTC/SOL. The shadowed first definition at line 571
        # had this; the active definition lost it. Without this clearing,
        # crypto halts accumulate forever until a manual restart.
        for m in ("BTC", "SOL"):
            MARKET_HALTED.pop(m, None)
            CONSECUTIVE_LOSSES.pop(m, None)
        log.info("Crypto day boundary fired at 4PM ET - halts cleared for BTC/SOL")
    except Exception as e:
        log.error(f"_on_crypto_day error: {e}")

_FLATTEN_PENDING = False
_SESSION_CLOSE_SUMMARY = None
_SUSPENSION_CHANGES = []
# Pre-Batch 2026-04-20: Recap is built sync in _on_session_close, sent async by scan_loop
_RECAP_PENDING = None

SESSION_CLOCK.on(SessionEvent.FUTURES_SESSION_CLOSE, _on_session_close)
SESSION_CLOCK.on(SessionEvent.FUTURES_PRE_FLATTEN, _on_pre_flatten)
SESSION_CLOCK.on(SessionEvent.CRYPTO_DAY_BOUNDARY, _on_crypto_day)

# ── Entry ─────────────────────────────────────────────────────────
def _build_boot_banner(tsx_result, open_carried, expired_count, suspended_count,
                       mkt_status, short_sha, time_str):
    """Wave 83 (Jul 2, 2026): the ONE smart boot message. Replaces the 3-5 message
    startup spam with a single compact banner where every line EARNS its spot -
    healthy systems stay quiet, problems announce themselves. Design language:
    B3 'smart lines' (Wayne, Jul 2). Every section is try/except-wrapped so a
    bad stat can never break the boot message."""
    L = []
    L.append("\U0001f916 *NQ CALLS \u2014 ONLINE*")
    L.append("\u2501" * 18)
    # Wave 90: honest version stamp - show the real deploy build, or nothing
    try:
        _w90_sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:7]
        if _w90_sha:
            L.append("\u2699\ufe0f build `" + _w90_sha + "`")
    except Exception:
        pass
    try:
        if tsx_result.get("auth"):
            nq_b = tsx_result.get("nq_bars_15m", 0)
            gc_b = tsx_result.get("gc_bars_15m", 0)
            L.append("\U0001f4e1 TopstepX \u2705 \u00b7 NQ `" + str(nq_b) + "` \u00b7 GC `" + str(gc_b) + "` bars")
        else:
            L.append("\U0001f4e1 \u26a0\ufe0f Fallback data (TwelveData/yfinance)")
    except Exception:
        pass
    try:
        bad = {m: s for m, s in (mkt_status or {}).items() if s != "\u2705"}
        if not bad:
            L.append("\U0001f50d Hunting: NQ \u00b7 GC \u00b7 BTC \u00b7 SOL")
        else:
            L.append("\U0001f50d " + " \u00b7 ".join(m + " " + s for m, s in mkt_status.items()))
    except Exception:
        pass
    try:
        st = sim.load_state()
        if st.get("enabled"):
            bal   = float(st.get("balance", 0))
            start = float(st.get("starting_balance", 50000.0))
            tgt   = start + float(st.get("profit_target", 3000.0))
            prog  = 0.0 if tgt <= start else max(0.0, min(1.0, (bal - start) / (tgt - start)))
            n     = int(round(prog * 10))
            bar   = "\u25b0" * n + "\u25b1" * (10 - n)
            L.append("\U0001f4bc *Topstep* `$" + format(bal, ",.0f") + " / $" + format(tgt, ",.0f") + "` " + bar)
    except Exception:
        pass
    try:
        cb = float(crypto_sim.load_crypto_state().get("balance", 0))
        L.append("\U0001fa99 *Crypto* `$" + format(cb, ",.2f") + "`")
    except Exception:
        pass
    try:
        today_str = _now_et().strftime("%Y-%m-%d")
        rows = ot._read_all()
        w = sum(1 for r in rows if r.get("status") == "CLOSED" and r.get("result") == "WIN"
                and today_str in r.get("timestamp", ""))
        l = sum(1 for r in rows if r.get("status") == "CLOSED" and r.get("result") == "LOSS"
                and today_str in r.get("timestamp", ""))
        if w or l:
            L.append("\U0001f4c5 Today: `" + str(w) + "W / " + str(l) + "L`")
    except Exception:
        pass
    try:
        if not SETTINGS.get("scanner_on"):
            L.append("\u26a0\ufe0f Scanner *OFF* \u2014 tap Scanner to hunt")
        if open_carried:
            L.append("\U0001f4c8 `" + str(open_carried) + "` open trade(s) carried")
        if suspended_count:
            L.append("\u26d4 `" + str(suspended_count) + "` benched (/suspended)")
        if expired_count:
            L.append("\U0001f9f9 Expired `" + str(expired_count) + "` stale trade(s)")
    except Exception:
        pass
    L.append("`v" + str(short_sha) + " \u00b7 " + str(time_str) + " ET`")
    return "\n".join(L)


async def _post_init(app):
    log.info("Running startup...")

    # ============================================================
    # Wave 12 (May 5, 2026): Phantom-Loss Data Cleanup Migration
    # One-shot, idempotent. Marks 4 confirmed May-4 phantom losses as
    # SKIP and rebuilds setup_performance.json + suspended_setups.json
    # from cleaned data. Marker file prevents re-runs.
    #
    # Wrapped in try/except - migration failure does NOT break startup.
    # If something goes wrong, bot still starts with current (poisoned)
    # data and Wayne can investigate via data/wave12_audit.json.
    # ============================================================
    try:
        import wave12_migrate
        import wave13_migrate
        _w12_result = wave12_migrate.maybe_run()
        _w12_result = wave13_migrate.maybe_run()
        if _w12_result.get("ran"):
            ok = _w12_result.get("ok", False)
            summary = _w12_result.get("summary", "(no summary)")
            log.info(f"Wave 12 result: ok={ok} {summary}")
            try:
                icon = "\u2705" if ok else "\u26a0\ufe0f"
                msg_lines = [
                    f"{icon} *Wave 12 Migration {'Complete' if ok else 'FAILED'}*",
                    "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
                    "Phantom-loss data cleanup ran on this startup.",
                    "",
                    f"`{summary}`",
                    "",
                ]
                if ok:
                    audit = _w12_result.get("audit", {})
                    marked = audit.get("phantoms_found_and_marked", [])
                    already = audit.get("phantoms_already_skipped", [])
                    unsuspended = audit.get("manual_unsuspend", [])
                    if marked:
                        msg_lines.append("*Marked as SKIP:*")
                        for aid in marked:
                            msg_lines.append(f"  \u2022 `{aid}`")
                    if already:
                        msg_lines.append("*Already SKIP (no change):*")
                        for aid in already:
                            msg_lines.append(f"  \u2022 `{aid}`")
                    if unsuspended:
                        msg_lines.append("*Force-restored setups:*")
                        for k in unsuspended:
                            msg_lines.append(f"  \u2022 `{k}`")
                    msg_lines.append("")
                    msg_lines.append("_Audit log: data/wave12_audit.json_")
                    msg_lines.append("_Backups: outcomes.csv.pre_wave12.bak (and others)_")
                    msg_lines.append("_Use /setups and /stats to verify clean state._")
                else:
                    msg_lines.append("_Bot started normally with current data._")
                    msg_lines.append("_Check Railway logs for the error._")
                await tg_send(app, "\n".join(msg_lines))
            except Exception as _w12_tg:
                log.warning(f"Wave 12 telegram notify failed: {_w12_tg}")
        else:
            log.info("Wave 12: already complete, skipping.")
    except Exception as _w12_e:
        log.error(f"Wave 12 startup wrap failed: {_w12_e}", exc_info=True)

    # ============================================================
    # Wave 29 (May 11, 2026): Suspend confirmed losers from data.
    # Adds BTC:BREAK_RETEST_BULL (0W/13L from backtest_pro 2026-05-10)
    # and NQ:STOCH_REVERSAL_BULL (0W/2L lifetime, -$221.93) to
    # suspended_setups.json. Auto-suspend doesn't catch these because
    # (BTC) was filtered before firing so no real-fire history, and
    # (NQ) losses happened before its $200/7d threshold. Wave 29
    # hard-suspends both. Idempotent via marker file. 14-day Wave 20
    # auto-unsuspend still applies if recent data shows improvement.
    # ============================================================
    try:
        _w29_marker = os.path.join(BASE_DIR, "data", "wave29_complete.json")
        if not os.path.exists(_w29_marker):
            _w29_now_iso = datetime.now(timezone.utc).isoformat()
            _w29_targets = [
                ("BTC:BREAK_RETEST_BULL", {
                    "reason": "Wave 29 manual: backtest_pro 2026-05-10 showed 0W/13L (0% WR) - hard suspend",
                    "total_at_suspension": 13,
                    "wr_at_suspension": 0.0,
                    "bleed_at_suspension": -1300.0,
                    "wave29_manual": True,
                }),
                ("NQ:STOCH_REVERSAL_BULL", {
                    "reason": "Wave 29 manual: lifetime_stats 0W/2L (-$221.93) - hard suspend",
                    "total_at_suspension": 2,
                    "wr_at_suspension": 0.0,
                    "bleed_at_suspension": -221.93,
                    "wave29_manual": True,
                }),
            ]
            _w29_susp = ot.get_suspended_setups()
            _w29_added = []
            _w29_skipped = []
            for _k, _info in _w29_targets:
                if _k in _w29_susp:
                    _w29_skipped.append(_k)
                else:
                    _w29_susp[_k] = {**_info, "suspended_at": _w29_now_iso}
                    _w29_added.append(_k)
            ot._save_suspended_setups(_w29_susp)
            try:
                os.makedirs(os.path.dirname(_w29_marker), exist_ok=True)
                with open(_w29_marker, "w", encoding="utf-8") as _f:
                    json.dump({
                        "completed_at": _w29_now_iso,
                        "added":        _w29_added,
                        "skipped":      _w29_skipped,
                        "wave":         "Wave 29 (May 11, 2026)",
                    }, _f, indent=2)
            except Exception as _w29_marker_err:
                log.warning(f"Wave 29 marker write failed: {_w29_marker_err}")
            log.info(f"Wave 29 migration: added={_w29_added}, skipped={_w29_skipped}")
            try:
                _w29_msg = "\U0001f6ab *Wave 29 Migration*\n"
                _w29_msg += "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                _w29_msg += "Suspended confirmed losers based on data:\n"
                if _w29_added:
                    _w29_msg += "\n*Added to suspended_setups:*\n"
                    for _k in _w29_added:
                        _w29_msg += f"  \u2022 `{_k}`\n"
                if _w29_skipped:
                    _w29_msg += "\n*Already suspended (no change):*\n"
                    for _k in _w29_skipped:
                        _w29_msg += f"  \u2022 `{_k}`\n"
                _w29_msg += "\n_Wave 20 14-day auto-unsuspend still applies if data improves._\n"
                _w29_msg += "_Marker: data/wave29_complete.json_"
                await tg_send(app, _w29_msg)
            except Exception as _w29_tg:
                log.warning(f"Wave 29 telegram notify failed: {_w29_tg}")
        else:
            log.info("Wave 29: already complete, skipping.")
    except Exception as _w29_e:
        log.error(f"Wave 29 startup wrap failed: {_w29_e}", exc_info=True)

    # Task 1: Restore scanner state from disk BEFORE anything else
    scanner_info = _load_scanner_state()
    SETTINGS["scanner_on"] = scanner_info["scanner_on"]
    # Wave 103 (_WAVE103_SCANNER_ALWAYS_ON): the bot's job is to scan -- it must
    # always come up scanning after any restart. Ignore a persisted OFF on boot
    # (a manual pause only lasts the current session, never survives a reboot).
    # The one deliberate cross-restart OFF switch is the SCANNER_FORCE_OFF_ON_BOOT
    # env var checked just below, which still overrides this. We persist the
    # forced-ON so on-disk state, SETTINGS, and the watchdog all agree.
    if not SETTINGS["scanner_on"]:
        log.info("Wave 103: persisted scanner OFF ignored on boot -- autonomy default is ON.")
        SETTINGS["scanner_on"] = True
        try:
            _save_scanner_state()
        except Exception:
            pass
    hrs_ago = scanner_info["hours_ago"]
    log.info(f"Scanner state restored: {'ON' if SETTINGS['scanner_on'] else 'OFF'} "
             f"(last changed {hrs_ago} hours ago)")

    # Persistence / validation: force scanner OFF on boot if env var is set.
    # Used during validation of code changes — prevents the bot from firing
    # trades while we're watching for errors in a fresh deploy.
    if os.environ.get("SCANNER_FORCE_OFF_ON_BOOT", "").strip().lower() in ("true", "1", "yes"):
        if SETTINGS["scanner_on"]:
            SETTINGS["scanner_on"] = False
            _save_scanner_state()
            log.info("Scanner FORCE-OFF on boot (SCANNER_FORCE_OFF_ON_BOOT=true) — was ON, now OFF")
        else:
            log.info("Scanner FORCE-OFF on boot (SCANNER_FORCE_OFF_ON_BOOT=true) — already OFF")

    # May 3 Wave 8: prune stale family_cooldowns and active_setups files.
    # Same rationale as the cooldowns prune — old entries don't block anything
    # but pollute state files and clutter /diag output.
    try:
        _prune_stale_state_files()
    except Exception as _pse:
        log.warning(f"Wave 8 stale-state prune failed (non-fatal): {_pse}")

    # May 2 Wave 5: reconcile any stale crypto open_trades against outcomes.csv.
    # If outcome_tracker.py auto-resolved a stop/target hit but didn't propagate
    # the close to crypto_sim, this catches it on startup so the open_trades
    # array reflects reality.
    try:
        n_reconciled = crypto_sim.reconcile_with_outcomes()
        if n_reconciled > 0:
            log.info(f"Startup reconcile: closed {n_reconciled} stale crypto trade(s) from outcomes.csv")
    except Exception as e:
        log.warning(f"Startup crypto reconcile failed (non-fatal): {e}")

    # Wave 36 (May 11, 2026): same reconcile but for the Topstep sim (NQ/GC).
    # Crypto had this since Wave 5; Topstep was missing it - causing PNL
    # desync when outcome_tracker auto-resolved a wick-stop that
    # auto_check_sim_trades missed on 15m close prices.
    try:
        n_sim_rec = sim.reconcile_with_outcomes()
        if n_sim_rec > 0:
            log.info(f"Startup Wave 36 sim reconcile: closed {n_sim_rec} stale sim trade(s)")
    except Exception as e:
        log.warning(f"Startup Wave 36 sim reconcile failed (non-fatal): {e}")

    # TopstepX probe (primary data source for NQ/GC)
    tsx_result = {"auth": False, "nq_contract": None, "gc_contract": None, "nq_bars_15m": 0, "gc_bars_15m": 0}
    try:
        log.info("=== TOPSTEPX SELF-TEST START ===")
        tsx_result = probe_topstepx()
        log.info(f"TOPSTEPX auth={tsx_result['auth']} nq_contract={tsx_result['nq_contract']} gc_contract={tsx_result['gc_contract']} nq_bars={tsx_result['nq_bars_15m']} gc_bars={tsx_result['gc_bars_15m']}")
        log.info("=== TOPSTEPX SELF-TEST END ===")
    except Exception as e:
        log.error(f"TopstepX probe failed (non-fatal, bot will use fallbacks): {e}")

    # Probe NQ and GC symbols on Twelve Data
    try:
        nq_sym = probe_nq_symbol()
        log.info(f"TwelveData NQ symbol: {nq_sym}")
    except Exception as e:
        log.error(f"NQ symbol probe: {e}")
    try:
        gc_sym = probe_gc_symbol()
        log.info(f"TwelveData GC symbol: {gc_sym}")
    except Exception as e:
        log.error(f"GC symbol probe: {e}")

    # Data feed self-test: attempt a fresh fetch for NQ and GC and log what happened
    try:
        log.info("=== DATA FEED SELF-TEST START ===")
        for _mkt in ("NQ", "GC"):
            try:
                _frames = dl_get_frames(_mkt)
                for _tf in ("15m", "1h", "4h", "1d"):
                    _df = _frames.get(_tf)
                    _n = len(_df) if _df is not None else 0
                    log.info(f"SELF-TEST {_mkt} {_tf}: {_n} bars")
            except Exception as _e:
                log.error(f"SELF-TEST {_mkt} exception: {_e}")
        log.info("=== DATA FEED SELF-TEST END ===")
    except Exception as e:
        log.error(f"Data feed self-test: {e}")

    # Task 2: Auto-expire stale OPEN trades at startup
    expired_count = 0
    try:
        expired = ot.auto_expire_stale_trades(max_hours=24)
        expired_count = len(expired)
        if expired_count:
            log.info(f"Startup: auto-expired {expired_count} stale trade(s)")
    except Exception as e:
        log.error(f"Startup auto-expire: {e}")

    # Archive old sessions at startup
    try:
        created = ot.archive_old_sessions()
        if created:
            log.info(f"Archived {len(created)} old session(s) at startup")
    except Exception as e:
        log.error(f"Startup archive: {e}")

    # Check and update setup suspensions at startup
    suspended_count = 0
    try:
        changes = ot.check_and_update_suspensions()
        if changes:
            # Wave 83 (Jul 2, 2026): suspension chatter folded into the one boot
            # banner; full detail any time via /suspended.
            log.info("W83: %d suspension change(s) at startup - see /suspended", len(changes))
        suspended_count = len(ot.get_suspended_setups())
        log.info(f"Suspension check at startup: {len(changes)} changes, {suspended_count} currently suspended")
    except Exception as e:
        log.error(f"Startup suspension check: {e}")

    log.info("Running startup market scan...")
    try:
        state = build_startup_state()
        # Wave 83 (Jul 2, 2026): market state goes to logs, not Telegram - the
        # boot banner is the single startup message.
        log.info("W83 startup market state (log-only):\n%s", state)
    except Exception as e:
        log.error(f"Startup market state failed: {e}")

    # Task 6: Full startup verification message
    try:
        # Commit SHA (may not exist on Railway runtime)
        short_sha = "unknown"
        try:
            import subprocess as _sp
            short_sha = _sp.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=BASE_DIR, stderr=_sp.DEVNULL
            ).decode().strip() or "unknown"
        except Exception:
            short_sha = "unknown"

        # Scanner state + formatting
        scanner_line = "ON" if SETTINGS["scanner_on"] else "OFF"
        now_et_start = _now_et()
        time_str = now_et_start.strftime("%I:%M %p").lstrip("0")

        # Open trades carried
        open_carried = len(ot.load_open_trades())

        # Counts for setups
        try:
            from outcome_tracker import _load_performance
            all_setups = _load_performance()
            active_count = sum(1 for k in all_setups if k not in ot.get_suspended_setups())
        except Exception:
            active_count = 0

        # Data source status per market (quick bar-count probe, cached)
        def _market_status(mkt: str) -> str:
            try:
                frames = dl_get_frames(mkt)
                df15 = frames.get("15m")
                if df15 is not None and len(df15) >= 20:
                    return "✅"
                if df15 is not None and len(df15) > 0:
                    return "⚠️"
                return "❌"
            except Exception:
                return "❌"

        try:
            nq_s  = _market_status("NQ")
            gc_s  = _market_status("GC")
            btc_s = _market_status("BTC")
            sol_s = _market_status("SOL")
        except Exception:
            nq_s = gc_s = btc_s = sol_s = "❓"

        lines = [
            "🤖 *NQ CALLS Bot Restarted*",
            "━━━━━━━━━━━━━━━━━━",
            f"📦 Commit: `{short_sha}`",
            f"📡 Scanner: `{scanner_line}` (last changed {hrs_ago}h ago)",
            f"🕐 Time: `{time_str} ET`",
            "━━━━━━━━━━━━━━━━━━",
            "📊 *System Status*",
            f"  Open trades carried: `{open_carried}`",
            f"  Auto-expired stale: `{expired_count}`",
            f"  Active setups: `{active_count}` / Suspended: `{suspended_count}`",
            f"  Data: NQ {nq_s} | GC {gc_s} | BTC {btc_s} | SOL {sol_s}",
            "━━━━━━━━━━━━━━━━━━",
        ]

        # TopstepX API status block
        try:
            tsx_auth_icon = "✅" if tsx_result.get("auth") else "❌"
            nq_c = tsx_result.get("nq_contract") or "—"
            gc_c = tsx_result.get("gc_contract") or "—"
            nq_b = tsx_result.get("nq_bars_15m", 0)
            gc_b = tsx_result.get("gc_bars_15m", 0)
            lines.append("📡 *TopstepX API (Primary)*")
            lines.append(f"  Auth: {tsx_auth_icon}")
            lines.append(f"  NQ: `{_md(nq_c)}` ({nq_b} bars)")
            lines.append(f"  GC: `{_md(gc_c)}` ({gc_b} bars)")
            if not tsx_result.get("auth"):
                lines.append("  ⚠️ Falling back to TwelveData/yfinance")
            lines.append("━━━━━━━━━━━━━━━━━━")
        except Exception as e:
            log.error(f"TopstepX startup banner: {e}")

        # ── Batch 2A: Observability status ──
        try:
            import csv as _csv
            sl_path = os.path.join(BASE_DIR, "data", "strategy_log.csv")
            sl_rows = 0
            sl_detected = 0
            sl_fired = 0
            if os.path.exists(sl_path):
                with open(sl_path, newline="", encoding="utf-8") as f:
                    for r in _csv.DictReader(f):
                        sl_rows += 1
                        dec = r.get("decision", "")
                        if dec == "DETECTED": sl_detected += 1
                        elif dec == "FIRED":  sl_fired += 1

            lines.append("🧠 *Observability (Batch 2A)*")
            lines.append(f"  Strategy log rows: `{sl_rows:,}`")
            lines.append(f"  Detections logged: `{sl_detected:,}` | Fired: `{sl_fired:,}`")
            lines.append(f"  Indicators per scan: ADX RSI ATR VWAP EMA(20/50/200/21)")
            lines.append(f"                      BB(20,2) Stoch(14,3) MACD(12,26,9)")
            lines.append(f"  Full detection logging: ✅ Active")
            lines.append(f"  Every scan saved with score breakdown + reason")
            lines.append("━━━━━━━━━━━━━━━━━━")
        except Exception as e:
            log.error(f"Batch 2A startup section: {e}")

        # Pre-Batch 2026-04-20: Startup banner additions
        try:
            lines.append("⚙️ *Pre-Batch (2026-04-20)*")
            lines.append(f"  Halt: REMOVED (shadow-logged via SHADOW_HALTED)")
            lines.append(f"  Recap: ON (generated at 4PM ET futures close)")
            lines.append(f"  Per-scan summary: ON (grep 'SCAN_SUMMARY' in Railway logs)")
            lines.append("━━━━━━━━━━━━━━━━━━")
        except Exception as e:
            log.error(f"Pre-Batch startup banner: {e}")

        # Wave 83 (Jul 2, 2026): retire the wall-of-text restart message. Everything
        # above still computes (and is in Railway logs); Telegram gets ONE smart
        # banner where every line earns its spot.
        log.info("W83: legacy verification text suppressed (%d lines) - boot banner sent instead", len(lines))
        _w83_mkt = {"NQ": nq_s, "GC": gc_s, "BTC": btc_s, "SOL": sol_s}
        await tg_send(app, _build_boot_banner(tsx_result, open_carried, expired_count,
                                              suspended_count, _w83_mkt, short_sha, time_str))
    except Exception as e:
        log.error(f"Startup verification message: {e}")

    # Wave 17 (May 9, 2026): Register Telegram-native UI surfaces.
    # set_my_commands populates the "/" autocomplete dropdown so users
    # see all commands when they type "/" in any chat with the bot.
    # set_my_short_description shows in the bot profile / share screen.
    # This is what makes the bot feel professional / robot-like.
    try:
        from telegram import BotCommand
        await app.bot.set_my_commands([
            BotCommand("menu",       "Open main menu"),
            BotCommand("status",     "Bot status & open trades"),
            BotCommand("stats",      "Overall trading stats"),
            BotCommand("session",    "Current session breakdown"),
            BotCommand("open",       "List open trades"),
            BotCommand("edge",       "Per-setup win rate"),
            BotCommand("setups",     "Active setup catalog"),
            BotCommand("suspended",  "Suspended setups + countdown"),  # Wave 20
            BotCommand("brief",      "Live market brief"),
            BotCommand("report",     "Daily report"),
            BotCommand("recap",      "Quick session recap"),
            BotCommand("backtest",   "Replay closed trades"),
            BotCommand("simstatus",  "Sim account status"),
            BotCommand("diag",       "Bot health check"),
            BotCommand("commands",   "Full command list"),
            BotCommand("help",       "Quick guide"),
        ])
        log.info("Wave 17: Telegram /-dropdown commands registered (15)")
    except Exception as e:
        log.warning(f"Wave 17 set_my_commands failed (non-fatal): {e}")

    try:
        await app.bot.set_my_short_description(
            "Self-improving trading alert bot for NQ Futures, Gold, BTC, "
            "and SOL. Confirmed entries only. Type /menu to start."
        )
        log.info("Wave 17: Telegram short description registered")
    except Exception as e:
        log.warning(f"Wave 17 set_my_short_description failed (non-fatal): {e}")

    # Wave 18 (May 9, 2026): Dashboard auto-regen loop.
    # Every 5 minutes the bot regenerates dashboard.html and
    # docs/dashboard.html so the GitHub Pages URL stays fresh.
    # auto_sync (every 6h) commits and pushes the docs/ copy.
    # For instant push: /sync command.
    async def _wave18_dashboard_regen_loop():
        # Initial delay - let the bot finish startup first.
        await asyncio.sleep(60)
        while True:
            try:
                import generate_dashboard
                # Run in thread to avoid blocking the event loop
                # on file I/O (reads outcomes.csv, writes 25KB+ HTML).
                await asyncio.to_thread(generate_dashboard.main)
            except Exception as e:
                log.warning(f"Wave 18 dashboard regen failed: {e}")
            await asyncio.sleep(300)  # 5 minutes
    asyncio.create_task(_wave18_dashboard_regen_loop())
    log.info("Wave 18: Dashboard auto-regen loop launched (5 min cadence)")

    # Wave 163 (_WAVE163_UNKILLABLE_LOOP): never fire-and-forget the scan
    # loop. A supervisor keeps a strong task reference (so it cannot be
    # garbage-collected) and relaunches scan_loop if it EVER exits for any
    # reason other than a real shutdown (CancelledError). The scan loop
    # died silently 3x - each time a deploy cancelled the task and nothing
    # brought it back. This makes that impossible.
    _W163_SCAN_TASK[0] = asyncio.create_task(_w163_scan_supervisor(app))
    log.info("Wave 163: scan loop launched under supervisor (auto-restart).")

    # Wave 22 (May 9, 2026): scanner watchdog
    asyncio.create_task(scanner_watchdog(app))
    log.info("Wave 22: scanner watchdog launched (1h checks, 6h alert cooldown)")

    # Launch auto-sync periodic loop (commits data/ + outcomes.csv to GitHub every 6h)
    # Without this, Railway restarts wipe all runtime trade data, scan decisions,
    # suspended setups, cooldowns, etc. With it, data persists across restarts.
    async def _auto_sync_notify(text):
        try:
            await tg_send(app, text)
        except Exception as e:
            log.warning(f"auto_sync telegram notify failed: {e}")
    asyncio.create_task(auto_sync.periodic_sync_loop(telegram_send=_auto_sync_notify))
    log.info(f"Auto-sync loop launched. {auto_sync.status()}")

async def cmd_sync(u, c):
    """Manual /sync trigger — pushes data/ + outcomes.csv to GitHub immediately."""
    await u.message.reply_text("⏳ Syncing data to GitHub...")
    try:
        result = await auto_sync.manual_sync()
        await u.message.reply_text(result, parse_mode="Markdown")
    except Exception as e:
        await u.message.reply_text(f"❌ Sync error: {e}")

async def cmd_recap(u, c):
    """Apr 30: /recap — builds and sends today's daily report on demand.
    Same engine that auto-runs at session close, but you can call it any time."""
    try:
        full_text, short_summary = ot.build_daily_report()
        await u.message.reply_text(short_summary, parse_mode="Markdown")
    except Exception as e:
        log.error(f"/recap failed: {e}")
        await u.message.reply_text(f"❌ Recap failed: {e}")

async def cmd_edge(u, c):
    """
    Apr 30 LATE: /edge — show real win-rate per setup based on actual closed trades.
    Reads strategy_log.csv FIRED rows that have a WIN/LOSS result, groups by
    market+setup, sorts by sample size. This is the truth: which setups have
    real edge? Use it to decide which to keep tuning vs which to suspend.
    """
    try:
        import strategy_log as sl
        import csv as _csv
        if not os.path.exists(sl.STRATEGY_LOG):
            await u.message.reply_text("📊 No strategy log yet — keep running the bot.")
            return
        with open(sl.STRATEGY_LOG, newline="", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
        fired = [r for r in rows
                 if r.get("decision") == sl.DECISION_FIRED
                 and r.get("result") in ("WIN", "LOSS")]
        if len(fired) < 5:
            await u.message.reply_text(
                f"📊 *Edge Analysis*\n\n"
                f"Need at least 5 closed trades to show meaningful edge.\n"
                f"Currently: `{len(fired)}` closed FIRED rows in strategy_log.\n"
                f"Keep the bot running and check back later.",
                parse_mode="Markdown",
            )
            return
        # Group by market:setup
        by_setup: dict = {}
        for r in fired:
            key = f"{r.get('market','?')}:{r.get('setup_type','?')}"
            d = by_setup.setdefault(key, {"W": 0, "L": 0, "avg_conv": 0.0})
            if r["result"] == "WIN":
                d["W"] += 1
            else:
                d["L"] += 1
            try:
                d["avg_conv"] += float(r.get("conviction", 0) or 0)
            except Exception:
                pass
        # Compute WR
        for d in by_setup.values():
            tot = d["W"] + d["L"]
            d["total"] = tot
            d["wr"]    = d["W"] / max(1, tot) * 100.0
            d["avg_conv"] = d["avg_conv"] / max(1, tot)
        # Sort by sample size desc, then by WR desc
        ordered = sorted(by_setup.items(),
                         key=lambda x: (-x[1]["total"], -x[1]["wr"]))
        lines = [
            "📊 *Edge by Setup* (live data)",
            "━━━━━━━━━━━━━━━━━━",
        ]
        for key, d in ordered:
            wr = d["wr"]
            icon = "🟢" if wr >= 60 else ("🔴" if wr < 45 else "🟡")
            star = " ⭐" if d["total"] >= 10 and wr >= 60 else ""
            warn = " ⚠\ufe0f" if d["total"] >= 5 and wr < 35 else ""
            lines.append(
                f"{icon} `{key}`{star}{warn}\n"
                f"   {d['W']}W/{d['L']}L — *{wr:.0f}% WR*  (avg conv {d['avg_conv']:.0f})"
            )
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append(f"_Based on {len(fired)} closed FIRED rows._")
        lines.append("_⭐ = 10+ trades & 60%+ WR. ⚠\ufe0f = 5+ trades & sub-35% WR._")
        await u.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        log.error(f"/edge failed: {e}")
        await u.message.reply_text(f"❌ Edge command failed: {e}")

async def cmd_diag(u, c):
    """
    Apr 30 LATE LATE: /diag — instant health check.
    Probes TopstepX live + checks last data source per market+timeframe.
    Use this from your phone whenever the bot feels off.
    """
    try:
        import data_layer as dl
        lines = [
            "🩺 *Bot Health Check*",
            "━━━━━━━━━━━━━━━━━━",
        ]
        # Scanner state
        on = SETTINGS.get("scanner_on", False)
        lines.append(f"*Scanner:* {'🟢 ON' if on else '🔴 OFF'}")
        # Wave 165 (_WAVE165_SCANNER_HEALTH): the line above is the SETTING;
        # this one is whether it is actually scanning right now.
        lines.append(f"*Scanning:* {_w165_scan_health_line()}")
        # Open trades
        try:
            open_t = ot.load_open_trades()
            lines.append(f"*Open trades:* `{len(open_t)}`")
        except Exception:
            lines.append("*Open trades:* (error reading)")
        # Markets enabled
        mkts = SETTINGS.get("markets", {})
        on_list = [m for m, v in mkts.items() if v]
        lines.append(f"*Markets enabled:* {', '.join(on_list) or 'none'}")
        lines.append("")

        # TopstepX live probe
        lines.append("*TopstepX live probe:*")
        try:
            probe = dl.probe_topstepx()
            lines.append(f"  Auth: {'✅' if probe.get('auth') else '❌'}")
            lines.append(f"  NQ contract: `{probe.get('nq_contract') or '—'}` ({probe.get('nq_bars_15m', 0)} bars 15m)")
            lines.append(f"  GC contract: `{probe.get('gc_contract') or '—'}` ({probe.get('gc_bars_15m', 0)} bars 15m)")
        except Exception as e:
            lines.append(f"  ❌ probe error: {e}")
        lines.append("")

        # Last data source per market|tf
        lines.append("*Last data source per fetch:*")
        try:
            ls = getattr(dl, "_last_source", {}) or {}
            if ls:
                for key in sorted(ls.keys()):
                    lines.append(f"  `{key}` → {ls[key]}")
            else:
                lines.append("  _(no fetches yet — wait one scan cycle)_")
        except Exception:
            lines.append("  _(unable to read source map)_")
        lines.append("")

        # Auto-sync status
        try:
            lines.append(f"*Auto-sync:* {auto_sync.status()}")
        except Exception:
            pass

        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("_Run this anytime the bot feels stuck._")
        await u.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        log.error(f"/diag failed: {e}")
        await u.message.reply_text(f"❌ /diag failed: {e}")


async def cmd_setups(u, c):
    """
    Apr 30 LATE: /setups — list all active setup types and their RR floor.
    Shows which setups are suspended too. Quick reference so you know what
    the bot is hunting for at any moment.
    """
    try:
        suspended = ot.get_suspended_setups()
        floors = ot.SETUP_RR_FLOORS
        lines = [
            "🎯 *Active Setups & RR Floors*",
            "━━━━━━━━━━━━━━━━━━",
        ]
        # Show by category
        groups = {
            "🔥 Top performers":       ["VWAP_BOUNCE_BULL", "LIQ_SWEEP_BULL", "LIQ_SWEEP_BEAR"],
            "🔄 Mean reversion":       ["BB_REVERSION_BULL", "BB_REVERSION_BEAR",
                                          "STOCH_REVERSAL_BULL", "STOCH_REVERSAL_BEAR",
                                          "RSI_DIV_BULL", "RSI_DIV_BEAR"],
            "📈 Trend continuation":    ["EMA21_PULLBACK_BULL", "EMA21_PULLBACK_BEAR",
                                          "EMA50_RECLAIM", "EMA50_BREAKDOWN",
                                          "BREAK_RETEST_BULL", "BREAK_RETEST_BEAR",
                                          "MACD_CROSS_BULL", "MACD_CROSS_BEAR"],
            "🔍 Anticipatory":         ["APPROACH_SUPPORT", "APPROACH_RESIST"],
            "⚡ Volatility / breakout":  ["VOLATILITY_CONTRACTION_BREAKOUT",
                                          "FAILED_BREAKDOWN_BULL", "FAILED_BREAKOUT_BEAR",
                                          "OPENING_RANGE_BREAKOUT"],
            "🌐 HTF / VWAP":           ["HTF_LEVEL_BOUNCE", "VWAP_RECLAIM", "VWAP_REJECT_BEAR"],
        }
        for group_name, setups_in_group in groups.items():
            shown = []
            for s in setups_in_group:
                rr = floors.get(s, floors["_DEFAULT"])
                # Check suspension across markets
                suspended_markets = [k.split(":")[0] for k in suspended.keys() if k.endswith(f":{s}")]
                marker = "⛔ " if suspended_markets else ""
                shown.append(f"  {marker}`{s}` (RR {rr})")
            if shown:
                lines.append(f"\n*{group_name}*")
                lines.extend(shown)
        if suspended:
            lines.append("\n⛔ *Currently suspended (auto):*")
            for k in sorted(suspended.keys()):
                info = suspended[k]
                lines.append(f"  `{k}` — {info.get('reason','?')}")
        else:
            lines.append("\n✅ _No setups currently suspended._")
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("_RR = minimum reward:risk for that setup to fire._")
        await u.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        log.error(f"/setups failed: {e}")
        await u.message.reply_text(f"❌ Setups command failed: {e}")


async def cmd_journal(u, c):
    """
    May 2: /journal -- show the last 10 lessons learned from closed trades.
    Optional argument /journal 25 shows last 25.
    Each closed trade auto-writes one entry. Read it to remember WHY trades
    failed and which setups consistently win.
    """
    try:
        limit = 10
        if c and c.args:
            try:
                limit = max(1, min(50, int(c.args[0])))
            except Exception:
                limit = 10
        text = ot.format_journal_text(limit=limit)
        # Telegram has 4096 char limit -- chunk if needed
        if len(text) <= 4000:
            await u.message.reply_text(text, parse_mode="Markdown")
        else:
            # Split on blank lines so we don't break mid-entry
            chunks = []
            current = ""
            for line in text.split("\n"):
                if len(current) + len(line) + 1 > 3800:
                    chunks.append(current)
                    current = line
                else:
                    current = (current + "\n" + line) if current else line
            if current:
                chunks.append(current)
            for chunk in chunks:
                await u.message.reply_text(chunk, parse_mode="Markdown")
    except Exception as e:
        log.error(f"/journal failed: {e}")
        await u.message.reply_text(f"❌ Journal command failed: {e}")

# Wave 22 (May 9, 2026): scanner watchdog state
# Tracks the last time we sent a watchdog alert so we don't spam
# Telegram. Reset to None on each successful state transition.
_LAST_WATCHDOG_ALERT_AT = None
_WATCHDOG_ALERT_COOLDOWN_HOURS = 6
_WATCHDOG_OFF_THRESHOLD_HOURS = 2
# Wave 104 (_WAVE104_SCANNER_SELF_HEAL): if the scanner has been OFF at least
# this long, the watchdog stops merely alerting and AUTO-RESUMES it. Longer than
# the 2h alert window so a short intentional pause is respected, but the bot can
# never be left silently off for long -- it heals back to always-scanning.
_WATCHDOG_AUTO_RESUME_HOURS = 6
_WATCHDOG_STUCK_THRESHOLD_MIN = 30
# Wave 164 (_WAVE164_SELF_RESTART): if the scanner is ON but has not scanned
# in this many minutes, the watchdog RELAUNCHES the scan loop itself (via
# Wave 163's supervisor) instead of only alerting. Set below the 30-min
# stuck-alert so recovery happens first; the alert remains as a backstop.
_WATCHDOG_RESTART_STALE_MIN = 10
_W164_LAST_RESTART_AT = None
_W164_RESTART_COUNT = 0


async def scanner_watchdog(app):
    """
    Wave 22 (May 9, 2026): Hourly background check for stuck scanner states.

    Sends Telegram alert if:
      - scanner_on=False AND last_changed > 2 hours ago (paused too long)
      - scanner_on=True AND _LAST_SCAN_TIMESTAMP > 30 min old (stuck loop)

    Cooldown of 6h between alerts so a single stuck state doesn't spam.
    Wrapped in defensive try/except so a watchdog bug can never crash
    the bot.
    """
    global _LAST_WATCHDOG_ALERT_AT
    log.info("Wave 22: scanner watchdog started (hourly checks)")
    while True:
        try:
            # Wave 164 (_WAVE164_SELF_RESTART): 5-minute cadence so a stale
            # scanner is caught and relaunched within minutes, not an hour.
            await asyncio.sleep(5 * 60)  # 5 minutes between checks

            # Wave 157 (_WAVE157_HOURLY_PAROLE_BOARD): the parole board convenes
            # EVERY HOUR, not only at the 4PM session roll and at boot. Measured
            # gap (Jul 17): ot.check_and_update_suspensions() was called at exactly
            # three sites - session close (~569, ~5914) and startup (~6290). Two
            # costs. A setup that finished earning its parole at 10am waited until
            # 4pm to be freed. Far worse: a PROBATIONER BLEEDING REAL MONEY stayed
            # live until the next checkpoint - up to ~8 hours after the bleed gate
            # should have benched it. Real money always outranks paper, so the gate
            # that protects it must not wait on a clock.
            # SAFE BY CONSTRUCTION: the engine is evidence-driven end to end -
            # probation is earned on graded shadow proof, failed on dollar bleed,
            # graduated on live record. Nothing inside it counts calls or sessions,
            # so running it hourly is semantically identical to running it at close,
            # only sooner. It sits at the TOP of the watchdog tick, above every
            # continue, so an alert cooldown or a self-heal can never skip it.
            # Changes are rare -> silence nearly every hour (the no-clutter law).
            try:
                _w157_changes = ot.check_and_update_suspensions()
                if _w157_changes:
                    _w157_body = "\n".join("  " + str(_c) for _c in _w157_changes[:12])
                    if len(_w157_changes) > 12:
                        _w157_body += "\n  ... and %d more" % (len(_w157_changes) - 12)
                    log.info("Wave 157: hourly board applied %d change(s)" % len(_w157_changes))
                    await tg_send(app, "PAROLE BOARD (hourly):\n" + _w157_body)
            except Exception as _w157e:
                log.warning(f"Wave 157 hourly parole board failed (non-fatal): {_w157e}")

            # Wave 164 (_WAVE164_SELF_RESTART): the scanner is supposed to be ON
            # but the scan loop has gone stale (no scan in _WATCHDOG_RESTART_STALE_MIN).
            # Wave 163 restarts the loop on a normal crash; this covers the case
            # where the supervisor TASK itself is dead/done. Relaunch it here,
            # BEFORE the alert cooldown so recovery is never blocked by it, exactly
            # like the OFF->ON self-heal below. Ping once so a frequent heal is
            # visible. Fully guarded - a watchdog error can never crash the bot.
            global _W164_LAST_RESTART_AT, _W164_RESTART_COUNT
            try:
                _sc_on = _load_scanner_state().get("scanner_on", False)
                _stale_min = None
                if _sc_on and _LAST_SCAN_TIMESTAMP is not None:
                    _stale_min = (datetime.now(timezone.utc)
                                  - _LAST_SCAN_TIMESTAMP).total_seconds() / 60.0
                if _stale_min is not None and _stale_min >= _WATCHDOG_RESTART_STALE_MIN:
                    _task = None
                    try:
                        _task = _W163_SCAN_TASK[0]
                    except Exception:
                        _task = None
                    _dead = (_task is None) or _task.done()
                    # If the supervisor task is still alive we leave it be (Wave 163
                    # is handling the restart); only relaunch when it is truly gone.
                    if _dead:
                        _W163_SCAN_TASK[0] = asyncio.create_task(_w163_scan_supervisor(app))
                        _W164_LAST_RESTART_AT = datetime.now(timezone.utc)
                        _W164_RESTART_COUNT += 1
                        log.error("Wave 164: scanner stale %d min and supervisor "
                                  "gone - relaunched scan loop (heal #%d)."
                                  % (int(_stale_min), _W164_RESTART_COUNT))
                        try:
                            await tg_send(app, "\U0001f504 Scanner self-healed: it "
                                          "had stalled for %d min, so I relaunched "
                                          "the scan loop. Back to hunting."
                                          % int(_stale_min))
                        except Exception:
                            pass
            except Exception as _w164e:
                log.warning("Wave 164 self-restart check failed (non-fatal): %r"
                            % _w164e)

            # Wave 104 (_WAVE104_SCANNER_SELF_HEAL): self-heal a long unexpected OFF.
            # Runs every hour, independent of the alert cooldown, so recovery is
            # prompt. If the scanner has been OFF longer than the auto-resume
            # window, turn it back ON (the bot should always be hunting) and say so.
            _resumed = False
            try:
                _hs = _load_scanner_state()
                if not _hs.get("scanner_on", False):
                    _lc = _hs.get("last_changed", "")
                    _off_h = None
                    if _lc:
                        try:
                            _lcdt = datetime.fromisoformat(_lc)
                            if _lcdt.tzinfo is None:
                                _lcdt = _lcdt.replace(tzinfo=timezone.utc)
                            _off_h = (datetime.now(timezone.utc) - _lcdt).total_seconds() / 3600.0
                        except Exception:
                            _off_h = None
                    if _off_h is not None and _off_h >= _WATCHDOG_AUTO_RESUME_HOURS:
                        SETTINGS["scanner_on"] = True
                        _save_scanner_state()
                        log.info("Wave 104: scanner self-healed ON after %.1fh OFF" % _off_h)
                        try:
                            await tg_send(app, "Scanner self-healed: it had been OFF for %.1fh, so I turned it back ON. The bot should always be hunting -- pause it again if that was intentional." % _off_h)
                        except Exception:
                            pass
                        _resumed = True
            except Exception as _wd_heal_err:
                log.warning("scanner self-heal check failed: %s" % _wd_heal_err)
            if _resumed:
                continue

            # Cooldown: skip if we alerted recently
            now = datetime.now(timezone.utc)
            if _LAST_WATCHDOG_ALERT_AT is not None:
                hours_since = (now - _LAST_WATCHDOG_ALERT_AT).total_seconds() / 3600.0
                if hours_since < _WATCHDOG_ALERT_COOLDOWN_HOURS:
                    continue

            state = _load_scanner_state()
            scanner_on = state.get("scanner_on", False)
            last_changed_str = state.get("last_changed", "")
            alert_msg = None

            if not scanner_on:
                # Scanner OFF — check how long
                try:
                    last_changed = datetime.fromisoformat(last_changed_str) if last_changed_str else now
                    if last_changed.tzinfo is None:
                        last_changed = last_changed.replace(tzinfo=timezone.utc)
                    hours_off = (now - last_changed).total_seconds() / 3600.0
                    if hours_off >= _WATCHDOG_OFF_THRESHOLD_HOURS:
                        alert_msg = (
                            "⚠️ *SCANNER PAUSED ALERT*\n"
                            "━━━━━━━━━━━━━━━━━━\n"
                            f"Scanner has been OFF for `{hours_off:.1f}` hours.\n"
                            "No alerts will fire until you turn it back on.\n\n"
                            "Tap /menu — hit *Toggle Scan* to resume."
                        )
                except Exception as _wd_off_err:
                    log.warning(f"Wave 22 watchdog OFF-check err: {_wd_off_err}")
            else:
                # Scanner ON — check if loop is alive
                if _LAST_SCAN_TIMESTAMP is not None:
                    mins_since_scan = (now - _LAST_SCAN_TIMESTAMP).total_seconds() / 60.0
                    if mins_since_scan >= _WATCHDOG_STUCK_THRESHOLD_MIN:
                        alert_msg = (
                            "⚠️ *SCANNER STUCK ALERT*\n"
                            "━━━━━━━━━━━━━━━━━━\n"
                            f"Scanner is ON but hasn't scanned for `{int(mins_since_scan)}` min.\n"
                            "Bot may be hung or all markets halted.\n\n"
                            "Check /status and /diag. Restart Railway if needed."
                        )

            if alert_msg:
                try:
                    await tg_send(app, alert_msg)
                    _LAST_WATCHDOG_ALERT_AT = now
                    log.warning("Wave 22 watchdog: alert sent")
                except Exception as _wd_send_err:
                    log.warning(f"Wave 22 watchdog send err: {_wd_send_err}")
        except Exception as _wd_top_err:
            log.error(f"Wave 22 watchdog top-level err: {_wd_top_err}", exc_info=True)
            await asyncio.sleep(60)  # short sleep on error


async def cmd_suspended(u, c):
    """Wave 20 (May 9, 2026): /suspended - list suspended setups + countdown.

    Wayne discovered no slash command exposed the existing
    get_suspension_report() function. This adds it.
    """
    try:
        text = ot.get_suspension_report()
        await u.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        log.error(f"/suspended failed: {e}")
        await u.message.reply_text(f"❌ Suspended command failed: {e}")


async def cmd_combine(u, c):
    """
    Wave 70 (Jun 26, 2026): Real continuous Topstep combine status.

    The live per-session sim (sim_account.json) resets to the starting balance
    every day and never models the trailing drawdown, so it could not answer
    the one question that matters: would we pass or bust a real combine? This
    runs a CONTINUOUS account through the real Topstep rules - cumulative
    balance, trailing max drawdown (locks at start once up by the DD amount),
    daily loss limit, and profit target - over the daily P&L history in
    sim_history.json, starting from the real combine_started_at date.
    Read-only. Obvious early broken-sizing days (|pnl| > 2x target) are
    excluded so one corrupted day cannot fake a pass.
    """
    import json as _json
    try:
        with open(os.path.join(BASE_DIR, "data", "sim_history.json"), "r", encoding="utf-8") as f:
            rows = _json.load(f)
    except Exception as e:
        await u.message.reply_text("Combine status unavailable: " + str(e)[:120])
        return
    start, target, trail_dd, daily_loss = 50000.0, 3000.0, 2000.0, 1000.0
    try:
        with open(os.path.join(BASE_DIR, "data", "sim_account.json"), "r", encoding="utf-8") as f:
            acct = _json.load(f)
        start = float(acct.get("starting_balance", start) or start)
        target = float(acct.get("profit_target", target) or target)
        trail_dd = float(acct.get("max_drawdown", trail_dd) or trail_dd)
        daily_loss = float(acct.get("daily_loss_limit", daily_loss) or daily_loss)
    except Exception:
        pass
    combine_start = ""
    try:
        with open(os.path.join(BASE_DIR, "data", "lifetime_stats.json"), "r", encoding="utf-8") as f:
            _lt = _json.load(f)
        combine_start = str(_lt.get("combine_started_at", ""))[:10]
    except Exception:
        combine_start = ""
    daily = sorted([(str(r.get("date", "")), float(r.get("pnl", 0.0) or 0.0)) for r in rows],
                   key=lambda x: x[0])
    cap = max(5000.0, target * 2.0)
    n_excluded = sum(1 for _, p in daily if abs(p) > cap)
    daily = [(d, p) for (d, p) in daily if abs(p) <= cap]
    if combine_start:
        daily = [(d, p) for (d, p) in daily if d >= combine_start]
    bal = peak = start
    dd_line = start - trail_dd
    status = "IN PROGRESS"
    resolved_on = daily[-1][0] if daily else "n/a"
    worst_day = 0.0
    min_cushion = bal - dd_line
    for d, pnl in daily:
        if pnl < worst_day:
            worst_day = pnl
        if pnl <= -daily_loss:
            status, resolved_on = "BUSTED (daily loss limit)", d
            break
        bal += pnl
        if bal > peak:
            peak = bal
            dd_line = min(peak - trail_dd, start)
        if (bal - dd_line) < min_cushion:
            min_cushion = bal - dd_line
        if bal <= dd_line:
            status, resolved_on = "BUSTED (trailing drawdown)", d
            break
        if bal >= start + target:
            status, resolved_on = "PASSED", d
            break
    pct = (100.0 * (bal - start) / target) if target else 0.0
    days_traded = sum(1 for _, p in daily if p != 0.0)
    lines = []
    lines.append("*COMBINE STATUS* (real continuous account)")
    lines.append("Status: *" + status + "*  (as of " + str(resolved_on) + ")")
    lines.append("Balance: `$%.2f`  (`%.1f%%` to target)" % (bal, pct))
    lines.append("Peak: `$%.2f`  (`+$%.0f`)" % (peak, peak - start))
    lines.append("Target: `$%.0f`   Bust floor: `$%.2f`" % (start + target, dd_line))
    lines.append("Closest to bust: `$%.0f` cushion" % min_cushion)
    lines.append("Worst day: `$%.2f`  (limit `-$%.0f`)" % (worst_day, daily_loss))
    lines.append("Days traded: `%d`  since `%s`" % (days_traded, combine_start or "start"))
    if daily:
        recent = [x for x in daily if x[1] != 0.0][-5:]
        if recent:
            lines.append("Recent: " + ", ".join(("%s `%+.0f`" % (d[5:], p)) for d, p in recent))
    if n_excluded:
        lines.append("_(excluded " + str(n_excluded) + " broken-sizing day(s))_")
    await u.message.reply_text("\n".join(lines), parse_mode="Markdown")


def main():
    log.info("NQ CALLS Bot starting...")
    os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
    _load_cooldowns()
    app=Application.builder().token(TELEGRAM_TOKEN).post_init(_post_init).build()
    # Wave 92 (Jul 6, 2026): retired dead/duplicate commands - wave7 backtest
    # tune recalibrate pulldata (dead/superseded) + simweekly (weekly button covers
    # it). Handlers left as harmless dead code; unregistered so they leave the menu.
    for cmd,fn in [("start",cmd_start),("menu",cmd_menu),("stats",cmd_stats),
                   ("open",cmd_open),("win",cmd_win),("loss",cmd_loss),("skip",cmd_skip),
                   ("report",cmd_report),("analyze",cmd_analyze),("simstatus",cmd_simstatus),("cryptostatus",cmd_cryptostatus),("leverage",cmd_leverage),("resetcrypto",cmd_resetcrypto),
                   ("simreset",cmd_simreset),("simon",cmd_simon),("simoff",cmd_simoff),
                   ("mnq",cmd_mnq),("help",cmd_help),
                   ("dashboard",cmd_dashboard),("review",cmd_review),("brief",cmd_brief),
                   ("status",cmd_status),  # Wave 19: was missing - slash did nothing
                   ("session",cmd_session),("history",cmd_history),("lifetime",cmd_lifetime),("eval",cmd_eval),("trend",cmd_trend),("journey",cmd_journey),("performance",cmd_performance),
                   ("rejected",cmd_rejected),("detections",cmd_detections),
                   ("sync",cmd_sync),("recap",cmd_recap),
                   ("edge",cmd_edge),("setups",cmd_setups),("diag",cmd_diag),
                   ("journal",cmd_journal),
                   ("ledger",cmd_ledger),  # Wave 141: the filter ledger - the learning loop\'s eyes
                   ("overrides",cmd_overrides),  # Wave 143: the hands - learned overrides status
                   ("side",cmd_side),  # Wave 162: the direction gate's live view
                   ("revert",cmd_revert),  # Wave 143: instant manual overrule
                   ("parole",cmd_parole),  # Wave 147: the revival program's live board
                   ("suspended",cmd_suspended),  # Wave 20: visibility into auto-suspended setups
                   ("commands",cmd_commands),("combine",cmd_combine)]:
        app.add_handler(CommandHandler(cmd,fn))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("Bot ready. Open Telegram and type /start")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__":
    main()
