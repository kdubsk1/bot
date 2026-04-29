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
from markets import get_market_config, get_all_markets
import sim_account as sim
import strategy_log as sl
import dashboard as dash
import strategy_review as sr
from config import TELEGRAM_TOKEN, CHAT_ID
from session_clock import SessionClock, SessionEvent, get_session_date
import auto_sync  # Persistence: commits data/ + outcomes.csv to GitHub every 6h so Railway runtime data survives restarts

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

def _save_scanner_state():
    """Persist scanner_on so it survives Railway restarts."""
    try:
        os.makedirs(os.path.dirname(SCANNER_STATE_FILE), exist_ok=True)
        data = {
            "scanner_on":   bool(SETTINGS["scanner_on"]),
            "last_changed": datetime.now(timezone.utc).isoformat(),
        }
        with open(SCANNER_STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.warning(f"_save_scanner_state: {e}")

def _load_scanner_state() -> dict:
    """Returns {'scanner_on': bool, 'last_changed': iso_str, 'hours_ago': float}."""
    try:
        if os.path.exists(SCANNER_STATE_FILE):
            with open(SCANNER_STATE_FILE) as f:
                data = json.load(f)
            last = data.get("last_changed", "")
            hours_ago = 0.0
            if last:
                try:
                    last_dt = datetime.fromisoformat(last)
                    hours_ago = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600.0
                except Exception:
                    pass
            return {
                "scanner_on":   bool(data.get("scanner_on", False)),
                "last_changed": last,
                "hours_ago":    round(hours_ago, 1),
            }
    except Exception as e:
        log.warning(f"_load_scanner_state: {e}")
    return {"scanner_on": False, "last_changed": "", "hours_ago": 0.0}

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
    global COOLDOWNS
    try:
        if os.path.exists(COOLDOWN_FILE):
            with open(COOLDOWN_FILE) as f:
                raw = json.load(f)
            COOLDOWNS = {
                tuple(k.split("|", 1)): datetime.fromisoformat(v)
                for k, v in raw.items()
            }
            log.info(f"Loaded {len(COOLDOWNS)} cooldowns from disk")
    except Exception as e:
        log.warning(f"_load_cooldowns: {e}")
        COOLDOWNS = {}

def _save_cooldowns():
    try:
        os.makedirs(os.path.dirname(COOLDOWN_FILE), exist_ok=True)
        with open(COOLDOWN_FILE, "w") as f:
            json.dump({f"{k[0]}|{k[1]}": v.isoformat() for k, v in COOLDOWNS.items()}, f)
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
        with open(ACTIVE_FILE, "w") as f: json.dump(d, f, indent=2)
    except Exception as e: log.warning(f"_save_active: {e}")

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
        with open(ZONE_LOCKOUT_FILE, "w") as f:
            json.dump(zones, f, indent=2)
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
        with open(FAMILY_CD_FILE, "w") as f:
            json.dump(cds, f, indent=2)
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

# ── Alert formatter ───────────────────────────────────────────────
def format_alert(market, tf, setup, conv, tier, trend, target, rr, method,
                 adx_v, rsi_v, lev=None, risk_at_stop=None, hold=None,
                 extra_footer="", alert_id=""):
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

    msg = (
        f"{header}{nw}\n"
        f"{cfg.EMOJI} {dir_icon} {arrow}  |  *{_md(cfg.FULL_NAME)}*  |  [{tf}]\n"
        f"{te} Tier: *{tier}*  |  Conviction: *{conv}/100*\n"
        f"🔭 Trend: `{trend:+d}`  |  ADX: `{round(adx_v,1)}`  |  RSI: `{round(rsi_v,1)}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📍 *Entry:*  `{round(setup['entry'],4)}`\n"
        f"🛑 *Stop:*   `{round(setup['raw_stop'],4)}`  ← place immediately\n"
        f"🎯 *Target:* `{round(target,4)}` ({safe_method}, {round(rr,2)}R)\n"
    )

    if lev is not None:
        msg += f"📊 *Leverage:* `{lev}x`  (risk: {risk_at_stop}%)\n"
    if hold:
        msg += f"⏱ *Hold:* {_md(hold)}\n"

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

    msg += f"━━━━━━━━━━━━━━━━━━\n📋 *Chart Read:*\n{_md(setup['detail'])}\n━━━━━━━━━━━━━━━━━━\n"
    if extra_footer:
        msg += f"{_md(extra_footer)}\n━━━━━━━━━━━━━━━━━━\n"
    sb = sim.format_sim_block(market, tier, setup["entry"], setup["raw_stop"], target, alert_id,
                                       conviction=conv, regime=setup.get("regime","UNKNOWN"), setup_name=setup.get("type","UNKNOWN"))
    if sb:
        msg += f"{sb}\n━━━━━━━━━━━━━━━━━━\n"
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

    # Auto-check outcomes
    for c in ot.auto_check_outcomes({market: frames}):
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

        sim_line = ""
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
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"💰 *Sim P&L*\n"
                    f"  Trade: `{spnl_str}` ({contr} contracts)\n"
                    f"  Today: `${risk['daily_pnl']:+,.2f}`\n"
                    f"  Balance: `${risk['balance']:,.2f}`\n"
                    f"  Daily limit left: `${risk['daily_left']:,.2f}`\n"
                )
            else:
                sim_line = (
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"💰 *Sim Account*\n"
                    f"  Today: `${risk['daily_pnl']:+,.2f}`\n"
                    f"  Balance: `${risk['balance']:,.2f}`\n"
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

    if sim.load_state().get("enabled"):
        try:
            df15 = frames.get("15m")
            if df15 is not None and not df15.empty:
                price = float(df15["Close"].iloc[-1])
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
        if entry_tf=="15m" and news_flag: continue
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
            log.info(f"[{market}] [{entry_tf}] Volume data degraded — skip")
            for stp in setups:
                _vol_reason = f"Volume data degraded or zero (vol_mean={vol_mean})"
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    sl.DECISION_REJECTED,
                    f"{_vol_reason} — cannot assess setup quality",
                    context=snapshot_context,
                    detection_reason=f"{stp['type']} detected but skipped: volume data unreliable this scan")
                _sample_reject_log(market, entry_tf, stp["type"], _vol_reason)
            continue
        # Audit Finding #6 / BACKLOG #4 (2026-04-28): per-setup volume gate.
        # April 14 alerts fired at vol_ratio 0.02-0.29 with HIGH conviction.
        # Universal 0.8 floor is too coarse — BREAK_RETEST (invert volume)
        # genuinely wants quiet retests. Per-setup gate:
        #   < 0.3      → dead market, reject everything
        #   confirm    → require >= 0.8
        #   invert     → pass through (conviction scores low vol as healthy)
        #   neutral    → pass through
        _filtered_setups = []
        for stp in setups:
            _vol_dir = ot.VOLUME_DIRECTION.get(stp["type"], "confirm")
            _reject_reason = None
            if vol_ratio < 0.3:
                _reject_reason = f"dead market (vol_ratio={vol_ratio:.2f}x < 0.3x floor)"
            elif _vol_dir == "confirm" and vol_ratio < 0.8:
                _reject_reason = f"confirm setup needs vol_ratio >= 0.8x (got {vol_ratio:.2f}x)"
            if _reject_reason:
                try:
                    sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                        cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
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

            # Setup suspension check — block negative EV setups
            # Task 4: Shadow-log so we can retroactively analyze if suspensions were correct
            if ot.is_setup_suspended(market, stp["type"]):
                log.info(f"[{market}] [{entry_tf}] Shadow-log {stp['type']} — suspended (would-have-fired)")
                suspended_info = ot.get_suspended_setups().get(f"{market}:{stp['type']}", {})
                reason_text = suspended_info.get("reason", "unknown")
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    sl.DECISION_SHADOW_SUSPENDED,
                    f"Suspended due to {reason_text} — shadow-logged to track would-have-fired rate",
                    context=snapshot_context,
                    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio))
                continue

            adx_min_by_setup = getattr(cfg, "ADX_MIN_BY_SETUP", {})
            required_adx = adx_min_by_setup.get(stp["type"], cfg.MIN_ADX)
            if is_prime_session:
                prime_adx = getattr(cfg, "MIN_ADX_PRIME", required_adx)
                required_adx = max(required_adx, prime_adx)
            if adx_v < required_adx:
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
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
                if _is_approach_active(market, stp["type"], stp["entry"]):
                    continue

            if stp["type"] == "APPROACH_RESIST" and trend > -2:
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    sl.DECISION_REJECTED,
                    f"APPROACH_RESIST requires trend <= -2 for bearish approach; current trend is {trend:+d}",
                    context=snapshot_context,
                    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio))
                _sample_reject_log(market, entry_tf, stp["type"], f"APPROACH_RESIST blocked: trend {trend:+d} not bearish")
                continue

            if stp["type"] == "APPROACH_SUPPORT" and trend < 2:
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    sl.DECISION_REJECTED,
                    f"APPROACH_SUPPORT requires trend >= +2 for bullish approach; current trend is {trend:+d}",
                    context=snapshot_context,
                    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio))
                _sample_reject_log(market, entry_tf, stp["type"], f"APPROACH_SUPPORT blocked: trend {trend:+d} not bullish")
                continue

            tgt, rr, method = ot.structure_target(df_e, stp["direction"], stp["entry"], stp["raw_stop"], atr_v,
                                                   market=market, trend_score_val=trend)

            if method == "no_target" or tgt == 0:
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    sl.DECISION_REJECTED,
                    "No real swing target available — nearest structural level too close for minimum R:R",
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
            if   quick_conv >= 80: tier_min_rr = 1.5
            elif quick_conv >= 65: tier_min_rr = 2.0
            else:                  tier_min_rr = 2.5
            _global_min_rr = cfg.NEWS_MIN_RR if news_flag else SETTINGS["min_rr"]
            # BACKLOG #7 (2026-04-28): VWAP_BOUNCE_BULL is at 83% WR (best in
            # bot) but min_rr requirements were blocking ~60% of detections.
            # Lower effective floor by 0.5 — better to take 1.5R wins than
            # miss 3R wins entirely.
            if stp["type"] == "VWAP_BOUNCE_BULL":
                tier_min_rr   = max(1.0, tier_min_rr - 0.5)
                _global_min_rr = max(1.0, _global_min_rr - 0.5)
            min_rr = max(tier_min_rr, _global_min_rr)
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
            conv          = max(0, min(100, conv+sum(extra.values())))
            # Merge core breakdown with market-specific extras for full transparency
            bd_final = dict(bd_core)
            for _k, _v in (extra or {}).items():
                bd_final[f"extra_{_k}"] = _v
            bd_final["base"] = 15  # the starting base score in conviction_score (lowered 30→15 per BACKLOG #3)
            bd_final["final_score"] = conv
            if   conv>=80: tier="HIGH"
            elif conv>=65: tier="MEDIUM"
            elif conv>=50: tier="LOW"
            else:          tier="REJECT"

            if tier=="REJECT" or conv < cfg.MIN_CONVICTION:
                decision = sl.DECISION_ALMOST if conv >= cfg.MIN_CONVICTION-10 else sl.DECISION_REJECTED
                _conv_reason = (
                    f"Conviction {conv} below {cfg.MIN_CONVICTION} minimum (tier={tier}); gap: {cfg.MIN_CONVICTION - conv} points"
                    if decision == sl.DECISION_REJECTED else
                    f"Conviction {conv} just short of {cfg.MIN_CONVICTION} minimum by {cfg.MIN_CONVICTION - conv} points — ALMOST"
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
                lev, risk_pct = ot.suggest_leverage(tier, stp["entry"], stp["raw_stop"], SETTINGS["account_risk_pct"])
                lev = min(lev, lev_cap)
            hold = ot.HOLD_BY_TIER.get(tier)

            alert_id = ot.log_alert({
                "market":market, "tf":entry_tf, "setup":stp["type"], "direction":stp["direction"],
                "entry":round(stp["entry"],4), "stop":round(stp["raw_stop"],4), "target":round(tgt,4),
                "rr":round(rr,2), "method":method, "trend_score":trend, "conviction":conv, "tier":tier,
                "leverage":lev or "", "suggested_hold":hold or "", "rsi":round(rsi_v,2),
                "atr":round(atr_v,4), "adx":round(adx_v,2), "htf_bias":htf_bias,
                "hour":datetime.now(timezone.utc).hour, "vol_ratio":round(vol_ratio,2), "news_flag":int(news_flag),
            })

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

            if stp["type"] in ("APPROACH_SUPPORT","APPROACH_RESIST"):
                _mark_approach_active(market, stp["type"], stp["entry"])

            footer = cfg.alert_footer(stp, session)
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

async def force_flatten_futures(app):
    trades = ot.load_open_trades()
    futures_trades = [t for t in trades if t.get("market") in FUTURES_MARKETS]
    if not futures_trades:
        return

    await tg_send(app,
        "🔔 *Market Close in 5 minutes*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "All NQ and Gold positions will be closed.\n"
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
        try:
            pts  = cur - float(entry_p)
            if "SHORT" in row.get("direction", ""): pts = -pts
            pts_str = f"+{round(pts,2)}" if pts>=0 else str(round(pts,2))
        except Exception: pts_str = "?"

        result = "WIN" if (pts > 0 if isinstance(pts, float) else False) else "LOSS"
        ot.update_result(row["alert_id"], result, 0, cur)
        ot.record_trade_result(market, row.get("setup",""), result)
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
                r = "WIN" if cur > float(row["entry"]) else "LOSS"
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
            df_check  = frames.get(row.get("tf","15m")) or frames.get("15m")
            if df_check is not None and not df_check.empty and risk_dist > 0:
                cur_p = float(df_check["Close"].iloc[-1])
                direction_r = row.get("direction","")
                is_long_r = "LONG" in direction_r
                profit_dist = (cur_p - entry_val) if is_long_r else (entry_val - cur_p)
                partial_done = row.get("partial_exit_done", "") == "True"
                if profit_dist >= risk_dist and not partial_done:
                    ot.update_partial_exit(row["alert_id"])
                    await tg_send(app,
                        f"📤 *Partial Exit Signal* — {cfg.EMOJI} {_md(cfg.FULL_NAME)}\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"{_md(row.get('direction',''))} hit 1R at `{round(cur_p,2)}`\n"
                        f"Consider taking 50% off.\n"
                        f"Move stop to breakeven (`{round(entry_val,2)}`).\n"
                        f"━━━━━━━━━━━━━━━━━━"
                    )
        except Exception as e:
            log.debug(f"Partial exit check: {e}")

        r = ot.rescore_open_trade(row, frames, ot.in_news_window())
        if r["new_conviction"] is not None: ot.update_rescore(row["alert_id"], r["new_conviction"])
        if r["action"]=="HOLD": continue

        action    = r["action"]
        delta     = r["delta"]
        new_c     = r["new_conviction"]
        note      = r["note"]
        direction = row.get("direction","")
        old_conv  = new_c - delta if new_c is not None else 0

        cur_price = None
        df_tf = frames.get(row.get("tf","15m")) or frames.get("15m")
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

        if action == "LET_RUN":
            header = f"🚀 *{cfg.FULL_NAME} — Conviction Rising*"
            detail = f"Conviction up to *{new_c}/100* (+{delta}). Market trending in our favor — hold your position.\n"
        elif action == "WARN":
            header = f"⚠️ *{cfg.FULL_NAME} — Conviction Dropping*"
            detail = f"Conviction dropping (*{old_conv}* to *{new_c}*). Price showing rejection. Consider tightening your stop.\n"
        elif action == "EXIT_SUGGEST":
            header = f"🛑 *{cfg.FULL_NAME} — Consider Exiting*"
            detail = f"{cfg.FULL_NAME} position weakening (*{new_c}/100*). Price may be reversing — consider exiting early.\n"
        else:
            header = f"ℹ️ *{cfg.FULL_NAME} — Position Update*"
            detail = f"{note}\n" if note else ""

        msg = (
            f"{header}\n"
            f"{cfg.EMOJI} {_md(row.get('setup',''))} [{row.get('tf')}] | {direction}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{dist_lines}"
            f"{detail}"
            f"━━━━━━━━━━━━━━━━━━"
        )
        await tg_send(app, msg)

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
_LAST_DAILY_REPORT_DATE   = None   # date string for which report was sent
# Pre-Batch Follow-up Part B 2026-04-21: weekly recap deduplication
_LAST_WEEKLY_RECAP_DATE   = None   # Monday isoformat for which weekly recap was sent

# ── Scan loop ─────────────────────────────────────────────────────
async def scan_loop(app):
    global _FLATTEN_PENDING, _SESSION_CLOSE_SUMMARY, _SUSPENSION_CHANGES, _RECAP_PENDING
    global _LAST_SESSION_CLOSE_FIRED, _LAST_DAILY_REPORT_DATE, _LAST_WEEKLY_RECAP_DATE
    last_brief=last_asia=last_report=None
    last_hb=datetime.now(timezone.utc)
    scan_interval = SETTINGS["scan_interval_min"]
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            now_et = _now_et()

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
                if 970 <= _hm_flat < 980:
                    open_fut = [t for t in ot.load_open_trades() if t.get("market") in ("NQ", "GC")]
                    today_key = _now_flat.date().isoformat()
                    if open_fut and getattr(scan_loop, "_last_410_flatten", None) != today_key:
                        log.info(f"Pre-Batch Part A: 4:10 PM safety-net flatten for {len(open_fut)} open futures")
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
                        f"Session archived. Sim reset to fresh ${sim.load_state()['balance']:,.0f}.\n"
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

                try:
                    live_15m = {m: f.get("15m") for m,f in frames_by_market.items() if f.get("15m") is not None}
                    missed = sl.check_missed_setups(live_15m)
                    if missed: log.info(f"Missed check: {len(missed)} resolved")
                except Exception as e: log.warning(f"Missed check: {e}")

                for m in active:
                    try: await scan_market(app, m, frames_by_market[m])
                    except Exception as e: log.error(f"scan {m}: {e}\n{traceback.format_exc()}")

                try: await watch_open_trades(app, frames_by_market)
                except Exception as e: log.error(f"watch: {e}")

            if (datetime.now(timezone.utc)-last_hb).total_seconds()>=3600:
                log.info(f"Heartbeat scanner={SETTINGS['scanner_on']} open={len(ot.load_open_trades())}")
                last_hb=datetime.now(timezone.utc)

        except Exception as e: log.error(f"scan_loop: {e}\n{traceback.format_exc()}")
        await asyncio.sleep(scan_interval*60)

# ── Menu ──────────────────────────────────────────────────────────
def main_menu():
    s=SETTINGS; m=s["markets"]; ss=sim.load_state()
    sim_on=ss.get("enabled",False); use_mnq=ss.get("use_mnq",False)
    preset=ss.get("preset","50k").upper(); risk=sim.check_risk_limits(ss)
    pnl=risk["daily_pnl"]; pnl_str=f"+${pnl:,.0f}" if pnl>=0 else f"-${abs(pnl):,.0f}"
    if s["scanner_on"]:
        active_mkts = " ".join([k for k,v in m.items() if v])
        scan_btn = f"🟢 ON • {active_mkts} — tap to stop"
    else:
        scan_btn = "🔴 SCANNER OFF — tap to start"
    kb=[
        [InlineKeyboardButton(scan_btn, callback_data="toggle_scan")],
        [InlineKeyboardButton(f"{'✅' if m['NQ'] else '⬜'} NQ",   callback_data="toggle_NQ"),
         InlineKeyboardButton(f"{'✅' if m['GC'] else '⬜'} Gold", callback_data="toggle_GC"),
         InlineKeyboardButton(f"{'✅' if m['BTC'] else '⬜'} BTC", callback_data="toggle_BTC"),
         InlineKeyboardButton(f"{'✅' if m['SOL'] else '⬜'} SOL", callback_data="toggle_SOL")],
        [InlineKeyboardButton("✅ WIN",         callback_data="trade_win"),
         InlineKeyboardButton("❌ LOSS",        callback_data="trade_loss"),
         InlineKeyboardButton("⏭ SKIP",        callback_data="trade_skip"),
         InlineKeyboardButton("📋 Open",        callback_data="open_trades")],
        [InlineKeyboardButton("📊 Status",      callback_data="status"),
         InlineKeyboardButton("📈 Stats",       callback_data="stats"),
         InlineKeyboardButton("🧠 Learned",     callback_data="learning"),
         InlineKeyboardButton("❓ Help",         callback_data="help")],
        [InlineKeyboardButton("🌅 Morning",     callback_data="brief_morning"),
         InlineKeyboardButton("🌙 Asia",        callback_data="brief_asia"),
         InlineKeyboardButton("📋 Report",      callback_data="report_now"),
         InlineKeyboardButton("🔬 Analyze",     callback_data="analyze"),
         InlineKeyboardButton("📡 Live",        callback_data="live_brief")],
        [InlineKeyboardButton(f"{'💰 SIM 🟢' if sim_on else '💰 SIM 🔴'}", callback_data="toggle_sim"),
         InlineKeyboardButton(f"{'🔵 MNQ' if use_mnq else '⚪ NQ'}",       callback_data="toggle_mnq"),
         InlineKeyboardButton(f"{pnl_str} today",                           callback_data="sim_status"),
         InlineKeyboardButton(f"🔄 {preset}",                               callback_data="simreset_current"),
         InlineKeyboardButton("📅 Weekly",                                   callback_data="sim_weekly")],
        [InlineKeyboardButton(f"🎯 {s['min_conviction']}",       callback_data="set_conv"),
         InlineKeyboardButton(f"🕐 {s['scan_interval_min']}m",   callback_data="set_int"),
         InlineKeyboardButton(f"⏳ CD {s['cooldown_min']}m",     callback_data="set_cd"),
         InlineKeyboardButton(f"💸 {s['account_risk_pct']}%",    callback_data="set_risk")],
        [InlineKeyboardButton("📊 Session",  callback_data="session"),
         InlineKeyboardButton("📜 History", callback_data="history_list"),
         InlineKeyboardButton("🏆 Lifetime", callback_data="lifetime")],
        [InlineKeyboardButton("🧪 Test",                                          callback_data="test"),
         InlineKeyboardButton(f"🔄 Rescore {'✅' if s['rescore_on'] else '❌'}",  callback_data="toggle_rescore"),
         InlineKeyboardButton("⚖️ RR",                                            callback_data="rr_info"),
         InlineKeyboardButton("📊 Dashboard",                                     callback_data="dashboard")],
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
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 *Sim This Session*\n"
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
    if setups_fired:
        lines.append(f"*Setups fired:* {', '.join(setups_fired[:8])}")
    if summary.get("best_setup") != "N/A":
        lines.append(f"*Best:* `{summary['best_setup']}` | *Worst:* `{summary['worst_setup']}`")
    if sim_line:
        lines.append(sim_line)

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
    await u.message.reply_text(
        "🤖 *NQ CALLS Bot — Quick Guide*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🟢 *ENTER NOW* — confirmed, enter immediately\n"
        "👀 *HEADS UP* — setup forming, get ready\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔥 HIGH (80+) — 5 MNQ / full size\n"
        "✅ MEDIUM (65-79) — 3 MNQ / normal size\n"
        "⚡ LOW (50-64) — 1 MNQ / smaller size\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📍 Entry | 🛑 Stop (place immediately) | 🎯 Target\n"
        "🔭 Trend (-10 to +10) | 📦 Size (contracts)\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🌅 8:30am Morning brief | 🌙 6pm Asia brief | 📋 8pm Daily report\n"
        "📡 Live — instant market analysis any time\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "`/stats` `/open` `/win` `/loss` `/skip` `/report` `/brief`\n"
        "`/session` `/history [date]` `/lifetime`\n"
        "`/rejected` `/detections [market]` — see what bot is thinking\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ Not financial advice. Manage your risk.",
        parse_mode="Markdown")

async def cmd_dashboard(u,c):
    await u.message.reply_text("⏳ Building dashboard...")
    try:
        html = dash.build_dashboard()
        with open(os.path.join(BASE_DIR, "data", "dashboard.html"), "w", encoding="utf-8") as f:
            f.write(html)
        outcomes = dash.load_outcomes()
        closed = [r for r in outcomes if r.get("status") == "CLOSED"]
        wins = sum(1 for r in closed if r.get("result") == "WIN")
        losses = sum(1 for r in closed if r.get("result") == "LOSS")
        wr = round(wins / max(1, wins + losses) * 100, 1)
        await u.message.reply_text(
            f"📊 *Dashboard Generated*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"*Overall:* {wins}W / {losses}L ({wr}% WR)\n"
            f"*Total alerts:* {len(outcomes)}\n"
            f"*Open:* {sum(1 for r in outcomes if r.get('status')=='OPEN')}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Dashboard saved to data/dashboard.html",
            parse_mode="Markdown")
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

    if   d=="toggle_scan":    SETTINGS["scanner_on"]=not SETTINGS["scanner_on"]; _save_scanner_state()
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
        active=[m for m in ALL_MARKETS if SETTINGS["markets"].get(m)]
        halted = [m for m in active if _is_halted(m)]
        await q.message.reply_text(
            f"*Status:* {'🟢 Running' if SETTINGS['scanner_on'] else '🔴 Stopped'}\n"
            f"*Markets:* {', '.join(active)}\n"
            f"*Open trades:* {len(ot.load_open_trades())}\n"
            f"*Conv:* {SETTINGS['min_conviction']} *R:R:* {SETTINGS['min_rr']}\n"
            f"*News:* {'⚠️ YES' if ot.in_news_window() else '✅ No'}\n"
            + (f"*Halted:* {', '.join(halted)}" if halted else ""),
            parse_mode="Markdown"); return
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
                f"━━━━━━━━━━━━━━━━━━\n"
                f"💰 *Sim This Session*\n"
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
        if setups_fired: msg_lines.append(f"*Setups:* {', '.join(setups_fired[:8])}")
        if sim_line: msg_lines.append(sim_line)
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
        await q.message.reply_text("⏳ Building dashboard...")
        try:
            html = dash.build_dashboard()
            with open(os.path.join(BASE_DIR, "data", "dashboard.html"), "w", encoding="utf-8") as f:
                f.write(html)
            outcomes = dash.load_outcomes()
            closed = [r for r in outcomes if r.get("status") == "CLOSED"]
            wins = sum(1 for r in closed if r.get("result") == "WIN")
            losses = sum(1 for r in closed if r.get("result") == "LOSS")
            wr = round(wins / max(1, wins + losses) * 100, 1)
            await q.message.reply_text(
                f"📊 *Dashboard Generated*\n━━━━━━━━━━━━━━━━━━\n"
                f"*Overall:* {wins}W / {losses}L ({wr}% WR)\n"
                f"*Total:* {len(outcomes)} alerts\n━━━━━━━━━━━━━━━━━━\n"
                f"Open data/dashboard.html in your browser.", parse_mode="Markdown")
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
    """Session close handler — archives session, resets sim, resets daily gates."""
    global _SESSION_CLOSE_SUMMARY, _SUSPENSION_CHANGES, _RECAP_PENDING
    global DAILY_LOSS_GATE, DAILY_PROFIT_LOCKED, DAILY_TRADE_COUNT
    try:
        sid = get_session_date(now_et)
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
        sim.reset_sim(sim_state.get("preset", "50k"))
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

        log.info(f"Session close: archived {sid}, sim reset, gates cleared, {len(changes)} suspension changes")
    except Exception as e:
        log.error(f"_on_session_close error: {e}")

def _on_crypto_day(event, now_et):
    """Crypto day boundary — reset daily crypto stats."""
    try:
        log.info("Crypto day boundary fired at 4PM ET")
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
async def _post_init(app):
    log.info("Running startup...")

    # Task 1: Restore scanner state from disk BEFORE anything else
    scanner_info = _load_scanner_state()
    SETTINGS["scanner_on"] = scanner_info["scanner_on"]
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
            lines = ["🔬 *Startup — Setup Suspension Update*", "━━━━━━━━━━━━━━━━━━"]
            for c in changes:
                icon = "⛔" if c.startswith("SUSPENDED") else "✅"
                lines.append(f"  {icon} {c}")
            await tg_send(app, "\n".join(lines))
        report = ot.get_suspension_report()
        await tg_send(app, report)
        suspended_count = len(ot.get_suspended_setups())
        log.info(f"Suspension check at startup: {len(changes)} changes, {suspended_count} currently suspended")
    except Exception as e:
        log.error(f"Startup suspension check: {e}")

    log.info("Running startup market scan...")
    try:
        state = build_startup_state()
        await tg_send(app, state)
        log.info("Startup market state sent.")
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

        if not SETTINGS["scanner_on"]:
            lines.append("⚠️ Tap the Scanner button to start scanning.")
        await tg_send(app, "\n".join(lines))
    except Exception as e:
        log.error(f"Startup verification message: {e}")

    asyncio.create_task(scan_loop(app)); log.info("Scan loop launched.")

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

def main():
    log.info("NQ CALLS Bot starting...")
    os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
    _load_cooldowns()
    app=Application.builder().token(TELEGRAM_TOKEN).post_init(_post_init).build()
    for cmd,fn in [("start",cmd_start),("menu",cmd_menu),("stats",cmd_stats),
                   ("open",cmd_open),("win",cmd_win),("loss",cmd_loss),("skip",cmd_skip),
                   ("report",cmd_report),("analyze",cmd_analyze),("simstatus",cmd_simstatus),
                   ("simreset",cmd_simreset),("simon",cmd_simon),("simoff",cmd_simoff),
                   ("mnq",cmd_mnq),("simweekly",cmd_simweekly),("help",cmd_help),
                   ("dashboard",cmd_dashboard),("review",cmd_review),("brief",cmd_brief),
                   ("session",cmd_session),("history",cmd_history),("lifetime",cmd_lifetime),
                   ("rejected",cmd_rejected),("detections",cmd_detections),
                   ("sync",cmd_sync)]:
        app.add_handler(CommandHandler(cmd,fn))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("Bot ready. Open Telegram and type /start")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__":
    main()
