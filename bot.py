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
import yfinance as yf
import ccxt
from data_layer import get_frames as dl_get_frames, get_current_price
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
    CONSECUTIVE_LOSSES[market] = CONSECUTIVE_LOSSES.get(market, 0) + 1
    if market == "BTC":
        CORRELATION_LOCKOUT["SOL"] = datetime.now(timezone.utc) + timedelta(minutes=30)
    elif market == "SOL":
        CORRELATION_LOCKOUT["BTC"] = datetime.now(timezone.utc) + timedelta(minutes=30)

def _record_win(market: str):
    CONSECUTIVE_LOSSES[market] = 0

def _is_halted(market: str) -> bool:
    return MARKET_HALTED.get(market, False)

def _is_correlation_locked(market: str) -> bool:
    expiry = CORRELATION_LOCKOUT.get(market)
    if expiry and datetime.now(timezone.utc) < expiry:
        return True
    return False

# ── yfinance cache ────────────────────────────────────────────────
_YF_CACHE: dict = {}
_YF_MIN_AGE = 60

def fetch_yfinance(symbol, tf):
    cache_key = (symbol, tf)
    now = _time.time()
    cached = _YF_CACHE.get(cache_key)
    if cached and now - cached[0] < _YF_MIN_AGE:
        return cached[1]
    try:
        imap = {"3m":"5m","15m":"15m","1h":"60m","4h":"60m","1d":"1d"}
        pmap = {"3m":"5d","15m":"10d","1h":"60d","4h":"60d","1d":"2y"}
        df = yf.download(symbol, interval=imap[tf], period=pmap[tf], progress=False, auto_adjust=False)
        if df is None or df.empty:
            return cached[1] if cached else None
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.title)
        for c in ["Open","High","Low","Close","Volume"]:
            if c not in df.columns: return cached[1] if cached else None
        df = df[["Open","High","Low","Close","Volume"]].dropna()
        if tf == "4h":
            df = df.resample("4h").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
        result = df if len(df) >= 20 else None
        if result is not None:
            _YF_CACHE[cache_key] = (now, result)
        return result
    except Exception as e:
        log.warning(f"yf {symbol} {tf}: {e}")
        return cached[1] if cached else None

_EX = None
def _exchanges():
    global _EX
    if _EX is None:
        _EX = []
        for n in ("coinbase","kraken","kucoin","bybit","okx"):
            try: _EX.append(getattr(ccxt, n)({"enableRateLimit": True}))
            except: pass
    return _EX

def fetch_crypto(symbol, tf):
    sym_map = {
        "BTC/USDT": {"coinbase":"BTC/USD","kraken":"BTC/USD","kucoin":"BTC/USDT","bybit":"BTC/USDT","okx":"BTC/USDT"},
        "SOL/USDT": {"coinbase":"SOL/USD","kraken":"SOL/USD","kucoin":"SOL/USDT","bybit":"SOL/USDT","okx":"SOL/USDT"},
    }
    tfm = {"3m":"3m","15m":"15m","1h":"1h","4h":"4h","1d":"1d"}
    for ex in _exchanges():
        sym = sym_map.get(symbol,{}).get(ex.id, symbol)
        try:
            o = ex.fetch_ohlcv(sym, tfm.get(tf,"15m"), limit=400)
            if not o or len(o)<20: continue
            df = pd.DataFrame(o, columns=["ts","Open","High","Low","Close","Volume"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            return df.set_index("ts")
        except Exception as e:
            log.debug(f"{ex.id} {sym}: {e}")
    yfs = {"BTC/USDT":"BTC-USD","SOL/USDT":"SOL-USD"}.get(symbol)
    if yfs: return fetch_yfinance(yfs, tf)
    return None

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

# ── Scan one market ───────────────────────────────────────────────
async def scan_market(app, market, frames):
    cfg        = get_market_config(market)
    primary_tf = cfg.ENTRY_TIMEFRAMES[0]
    df_primary = frames.get(primary_tf)
    if df_primary is None or df_primary.empty:
        log.warning(f"[{market}] Missing primary frame."); return

    futures_ok = _futures_session_ok(market)
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

    if _is_halted(market):
        log.info(f"[{market}] HALTED after 3 consecutive losses — skipping")
        return
    if _is_correlation_locked(market):
        log.info(f"[{market}] Correlation locked — skipping")
        return
    if already_in or not futures_ok:
        if already_in:
            log.info(f"[{market}] Already in position — skipping new entry scan")
        else:
            log.info(f"[{market}] 4PM-6PM settlement window — no new entries for {market}")
        return

    for entry_tf in cfg.ENTRY_TIMEFRAMES:
        htf_key = cfg.HTF_CONFIRM if entry_tf==cfg.ENTRY_TIMEFRAMES[0] else cfg.HTF_SWING
        if entry_tf=="15m" and news_flag: continue
        df_e = frames.get(entry_tf)
        df_h = frames.get(htf_key)
        if df_e is None or df_h is None: continue
        if df_e.empty: continue

        setups = ot.detect_setups(df_e, df_h, htf_bias)
        if not setups: log.info(f"[{market}] [{entry_tf}] No setups."); continue

        adx_v    = float(ot.adx(df_e).iloc[-1])
        rsi_v    = float(ot.rsi(df_e["Close"]).iloc[-1])
        atr_v    = float(ot.atr(df_e).iloc[-1])
        vol_mean = float(df_e["Volume"].rolling(20).mean().iloc[-1]) if len(df_e)>=20 else None
        vol_last = float(df_e["Volume"].iloc[-1])
        vol_ratio= (vol_last / max(1e-9, vol_mean)) if (vol_mean and vol_mean > 0) else 0.0
        cur_price= float(df_e["Close"].iloc[-1])

        if vol_mean is None or not np.isfinite(vol_mean) or vol_mean < 1.0:
            log.info(f"[{market}] [{entry_tf}] Volume data degraded — skip")
            for stp in setups:
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    sl.DECISION_REJECTED, f"No volume data")
            continue
        if vol_ratio < 0.1 and vol_last < 1.0:
            log.info(f"[{market}] [{entry_tf}] Zero-volume candle — skip")
            continue

        session_name = session.get("session","")
        is_prime_session = any(s in session_name for s in ("US Regular","London","Pre-Market","London/NY"))

        for stp in setups:
            stp["market"] = market

            # Setup suspension check — block negative EV setups
            if ot.is_setup_suspended(market, stp["type"]):
                log.info(f"[{market}] [{entry_tf}] Skipping {stp['type']} — suspended (negative EV)")
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    sl.DECISION_REJECTED, f"SUSPENDED — negative EV")
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
                    sl.DECISION_REJECTED, f"ADX {round(adx_v,1)} < {required_adx} for {stp['type']}")
                continue

            if not _cooldown_ok(market, stp["type"]):
                log.info(f"[{market}] [{entry_tf}] {stp['type']} on cooldown.")
                continue

            if not _family_cooldown_ok(market, stp["type"]):
                log.info(f"[{market}] [{entry_tf}] {stp['type']} family on cooldown.")
                continue

            if _zone_locked(market, stp["direction"], stp["entry"]):
                log.info(f"[{market}] [{entry_tf}] {stp['type']} in loss zone lockout.")
                continue

            if stp["type"] in ("APPROACH_SUPPORT","APPROACH_RESIST"):
                if _is_approach_active(market, stp["type"], stp["entry"]):
                    continue

            if stp["type"] == "APPROACH_RESIST" and trend > -2:
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    sl.DECISION_REJECTED, f"APPROACH_RESIST blocked: trend {trend:+d} not bearish")
                continue

            if stp["type"] == "APPROACH_SUPPORT" and trend < 2:
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    sl.DECISION_REJECTED, f"APPROACH_SUPPORT blocked: trend {trend:+d} not bullish")
                continue

            tgt, rr, method = ot.structure_target(df_e, stp["direction"], stp["entry"], stp["raw_stop"], atr_v,
                                                   market=market, trend_score_val=trend)

            if method == "no_target" or tgt == 0:
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    sl.DECISION_REJECTED, "No real swing target available")
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

            quick_conv, quick_tier, _ = ot.conviction_score(
                stp, trend, df_e, df_h, news_flag, adx_v, rsi_v, vol_ratio,
                abs(tgt-stp["entry"])/max(1e-9, atr_v)
            )
            if   quick_conv >= 80: tier_min_rr = 1.5
            elif quick_conv >= 65: tier_min_rr = 2.0
            else:                  tier_min_rr = 2.5
            min_rr = max(tier_min_rr, cfg.NEWS_MIN_RR if news_flag else SETTINGS["min_rr"])
            if rr < min_rr:
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], tgt, rr, 0, "REJECT",
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    sl.DECISION_REJECTED, f"RR {round(rr,2)} < min {min_rr}")
                continue

            clean_path = abs(tgt-stp["entry"])/max(1e-9, atr_v)
            conv, tier, _ = ot.conviction_score(stp, trend, df_e, df_h, news_flag, adx_v, rsi_v, vol_ratio, clean_path)
            extra         = cfg.extra_conviction_factors(df_e, df_h, stp, trend, adx_v, rsi_v)
            conv          = max(0, min(100, conv+sum(extra.values())))
            if   conv>=80: tier="HIGH"
            elif conv>=65: tier="MEDIUM"
            elif conv>=50: tier="LOW"
            else:          tier="REJECT"

            if tier=="REJECT" or conv < cfg.MIN_CONVICTION:
                decision = sl.DECISION_ALMOST if conv >= cfg.MIN_CONVICTION-10 else sl.DECISION_REJECTED
                sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                    cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
                    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                    decision, f"Conv {conv} < min {cfg.MIN_CONVICTION}")
                continue

            dd_pct = sim_risk.get("daily_used_pct", 0)
            if dd_pct > 75:
                if conv < 90:
                    sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                        cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
                        trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                        sl.DECISION_REJECTED, f"DD>75% needs conv 90+, got {conv}")
                    continue
            elif dd_pct > 50:
                if conv < 80:
                    sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                        cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
                        trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
                        sl.DECISION_REJECTED, f"DD>50% needs conv 80+, got {conv}")
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

            sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
                cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
                trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag, sl.DECISION_FIRED, "")

            _mark_cooldown(market, stp["type"])

            if stp["type"] in ("APPROACH_SUPPORT","APPROACH_RESIST"):
                _mark_approach_active(market, stp["type"], stp["entry"])

            footer = cfg.alert_footer(stp, session)
            await tg_send(app, format_alert(market, entry_tf, stp, conv, tier, trend,
                                             tgt, rr, method, adx_v, rsi_v, lev, risk_pct, hold,
                                             extra_footer=footer, alert_id=alert_id))
            log.info(f"[{market}] [{entry_tf}] FIRED: {stp['type']} Conv:{conv} RR:{round(rr,2)}")

# ── Market session rules ──────────────────────────────────────────
FUTURES_MARKETS = {"NQ", "GC"}
FUTURES_CLOSE_ET   = (15, 55)
FUTURES_CLOSED_ET  = (16, 0)
FUTURES_REOPEN_ET  = (18, 0)

def _futures_session_ok(market: str) -> bool:
    if market not in FUTURES_MARKETS:
        return True
    now = _now_et()
    hm  = now.hour * 60 + now.minute
    closed_start = FUTURES_CLOSED_ET[0] * 60 + FUTURES_CLOSED_ET[1]
    reopen       = FUTURES_REOPEN_ET[0]  * 60 + FUTURES_REOPEN_ET[1]
    return not (closed_start <= hm < reopen)

async def force_flatten_futures(app):
    trades = ot.load_open_trades()
    futures_trades = [t for t in trades if t.get("market") in FUTURES_MARKETS]
    if not futures_trades:
        return

    await tg_send(app,
        "🔔 *Market Close in 5 minutes*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "All NQ and Gold positions will be closed.\n"
        "Topstep rule: flat by 4PM ET."
    )

    for row in futures_trades:
        market = row["market"]
        cfg    = get_market_config(market)
        try:
            sym = YF_MAP.get(market)
            df  = fetch_yfinance(sym, "15m")
            cur = float(df["Close"].iloc[-1]) if df is not None and not df.empty else float(row["entry"])
        except:
            cur = float(row["entry"])

        entry_p = row.get("entry", "?")
        try:
            pts  = cur - float(entry_p)
            if "SHORT" in row.get("direction", ""): pts = -pts
            pts_str = f"+{round(pts,2)}" if pts>=0 else str(round(pts,2))
        except: pts_str = "?"

        result = "WIN" if (pts > 0 if isinstance(pts, float) else False) else "LOSS"
        ot.update_result(row["alert_id"], result, 0, cur)
        ot.record_trade_result(market, row.get("setup",""), result)

        icon = "✅" if result=="WIN" else "❌"
        await tg_send(app,
            f"{icon} *Force Closed — 4PM Rule*\n"
            f"{cfg.EMOJI} *{_md(cfg.FULL_NAME)}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Setup: `{_md(row.get('setup',''))}` [{row.get('tf','')}]\n"
            f"Entry: `{entry_p}` -> Exit: `{round(cur,4)}`\n"
            f"Move: `{pts_str} pts`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Futures closed. Reopens 6PM ET."
        )
        log.info(f"[{market}] Force-closed at 4PM rule — exit {cur}")

    if sim.load_state().get("enabled"):
        for row in futures_trades:
            try:
                sym = YF_MAP.get(row["market"])
                df  = fetch_yfinance(sym, "15m")
                cur = float(df["Close"].iloc[-1]) if df is not None and not df.empty else float(row["entry"])
                r = "WIN" if cur > float(row["entry"]) else "LOSS"
                closed = sim.close_sim_trade(row["alert_id"], cur, r)
                if closed:
                    pnl = closed.get("pnl", 0)
                    risk = sim.check_risk_limits()
                    pnl_sign = f"+${pnl:,.2f}" if pnl>=0 else f"-${abs(pnl):,.2f}"
                    await tg_send(app,
                        f"💰 *SIM Force Closed — 4PM Rule*\n"
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

# ── Scan loop ─────────────────────────────────────────────────────
async def scan_loop(app):
    global _FLATTEN_PENDING, _SESSION_CLOSE_SUMMARY, _SUSPENSION_CHANGES
    last_brief=last_asia=last_report=None
    last_hb=datetime.now(timezone.utc)
    scan_interval = SETTINGS["scan_interval_min"]
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            now_et = _now_et()

            # Tick the session clock — fires events synchronously
            SESSION_CLOCK.tick(now_utc)

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
            if now_et.hour==20 and last_report!=now_et.date():
                try:
                    _,short=ot.build_daily_report(); await tg_send(app,short); last_report=now_et.date()
                except Exception as e: log.error(f"Daily report: {e}")

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

    if   d=="toggle_scan":    SETTINGS["scanner_on"]=not SETTINGS["scanner_on"]
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
    """Session close handler — archives session, resets sim, sets summary flag."""
    global _SESSION_CLOSE_SUMMARY, _SUSPENSION_CHANGES
    try:
        sid = get_session_date(now_et)
        summary = ot.build_session_summary(sid)
        sim_state = sim.load_state()
        sim_pnl = sim_state.get("today_pnl", 0.0)
        ot.archive_session(sid)
        sim.reset_sim(sim_state.get("preset", "50k"))
        changes = ot.check_and_update_suspensions()
        _SESSION_CLOSE_SUMMARY = {"sid": sid, "summary": summary, "sim_pnl": sim_pnl}
        _SUSPENSION_CHANGES = changes
        log.info(f"Session close handler: archived {sid}, sim reset, {len(changes)} suspension changes")
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

SESSION_CLOCK.on(SessionEvent.FUTURES_SESSION_CLOSE, _on_session_close)
SESSION_CLOCK.on(SessionEvent.FUTURES_PRE_FLATTEN, _on_pre_flatten)
SESSION_CLOCK.on(SessionEvent.CRYPTO_DAY_BOUNDARY, _on_crypto_day)

# ── Entry ─────────────────────────────────────────────────────────
async def _post_init(app):
    log.info("Running startup...")
    # Archive old sessions at startup
    try:
        created = ot.archive_old_sessions()
        if created:
            log.info(f"Archived {len(created)} old session(s) at startup")
    except Exception as e:
        log.error(f"Startup archive: {e}")

    # Check and update setup suspensions at startup
    try:
        changes = ot.check_and_update_suspensions()
        if changes:
            lines = ["🔬 *Startup — Setup Suspension Update*", "━━━━━━━━━━━━━━━━━━"]
            for c in changes:
                icon = "⛔" if c.startswith("SUSPENDED") else "✅"
                lines.append(f"  {icon} {c}")
            await tg_send(app, "\n".join(lines))
        # Always show current suspension state at startup
        report = ot.get_suspension_report()
        await tg_send(app, report)
        log.info(f"Suspension check at startup: {len(changes)} changes")
    except Exception as e:
        log.error(f"Startup suspension check: {e}")

    log.info("Running startup market scan...")
    try:
        state = build_startup_state()
        await tg_send(app, state)
        log.info("Startup market state sent.")
    except Exception as e:
        log.error(f"Startup market state failed: {e}")
    asyncio.create_task(scan_loop(app)); log.info("Scan loop launched.")

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
                   ("session",cmd_session),("history",cmd_history),("lifetime",cmd_lifetime)]:
        app.add_handler(CommandHandler(cmd,fn))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("Bot ready. Open Telegram and type /start")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__":
    main()
