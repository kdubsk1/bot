"""
outcome_tracker.py - NQ CALLS 2026
====================================
The brain. bot.py is the plumbing.

What this file does:
- Tracks every alert as WIN / LOSS / OPEN in outcomes.csv
- Scores each setup with a conviction score (0-100)
- Learns from past trades — setups that win more get higher scores
- Re-scores open trades live — tells you to hold, exit, or let run
- ADX filter — blocks alerts when market is choppy
- Structure bias — reads real swing highs/lows not just EMAs
- Leverage suggestions for BTC/SOL based on account risk
- /stats command shows full performance breakdown
"""

from __future__ import annotations
import csv, os, json, uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
import safe_io  # data-loss fix: atomic writes + cross-process locks
try:
    from zoneinfo import ZoneInfo
    ET_ZONE = ZoneInfo("America/New_York")
except ImportError:
    ET_ZONE = None  # Python < 3.9 fallback

def _now_et():
    """DST-aware Eastern Time."""
    if ET_ZONE:
        return datetime.now(ET_ZONE)
    return datetime.now(timezone.utc) - timedelta(hours=4)  # fallback

import numpy as np
import pandas as pd

# ------------------------------------------------------------------ #
# Config
# ------------------------------------------------------------------ #
# Use absolute paths so files always land in the Trading bot folder
_BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
OUTCOMES_CSV  = os.path.join(_BASE_DIR, "outcomes.csv")
LEARNING_FILE = os.path.join(_BASE_DIR, "data", "setup_performance.json")

# Ensure data directory exists
os.makedirs(os.path.join(_BASE_DIR, "data"), exist_ok=True)
_ACCOUNT_RISK_PCT = 1.5

TIER_LOW, TIER_MED, TIER_HIGH = "LOW", "MEDIUM", "HIGH"
LEV_BY_TIER   = {TIER_LOW: 5, TIER_MED: 10, TIER_HIGH: 18}
HOLD_BY_TIER  = {
    TIER_LOW:  "scalp (minutes-hours)",
    TIER_MED:  "intraday (hours)",
    TIER_HIGH: "swing (1-3 days)"
}

ADX_MIN_FOR_RECLAIM   = 20.0
MIN_DIST_ATR_FOR_SWING = 0.5

CSV_COLS = [
    "alert_id","timestamp","market","tf","setup","direction",
    "entry","stop","target","rr","method",
    "trend_score","conviction","tier","leverage","suggested_hold",
    "rsi","atr","adx","htf_bias","hour","vol_ratio","news_flag",
    "status","result","bars_to_resolution","exit_price","last_rescore_conviction",
    "partial_exit_done","session_id"
]

# ------------------------------------------------------------------ #
# Account risk
# ------------------------------------------------------------------ #
def set_account_risk_pct(pct: float) -> None:
    global _ACCOUNT_RISK_PCT
    _ACCOUNT_RISK_PCT = max(0.1, min(10.0, float(pct)))

def get_account_risk_pct() -> float:
    return _ACCOUNT_RISK_PCT

# ------------------------------------------------------------------ #
# SETUP SUSPENSION SYSTEM
# Auto-suspends setups with negative expected value.
# Restores them when recent performance improves.
# ------------------------------------------------------------------ #
SUSPENDED_SETUPS_FILE = os.path.join(_BASE_DIR, "data", "suspended_setups.json")

# Thresholds (Apr 30 tightened — bad setups were staying active too long.
# 0W/5L setups bled $300 overnight before suspension fired. Lower bar:
# 4 trades minimum to judge (was 5), 40% WR floor (was 35%).
# Restore threshold raised to 50% to require real recovery, not lucky bounce.)
_SUSPEND_MIN_TRADES = 4       # need at least this many trades to judge
_SUSPEND_WR_BELOW   = 40.0    # suspend if win_rate < this
_RESTORE_WR_ABOVE   = 50.0    # restore if recent win_rate climbs above this


def get_suspended_setups() -> dict:
    """
    Read suspended_setups.json.
    Returns dict like:
      {"BTC:VWAP_REJECT_BEAR": {"reason": "0W/5L (0% WR)", "suspended_at": "...", ...}}
    """
    if os.path.exists(SUSPENDED_SETUPS_FILE):
        try:
            with open(SUSPENDED_SETUPS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_suspended_setups(data: dict):
    safe_io.atomic_write_json(SUSPENDED_SETUPS_FILE, data)


def is_setup_suspended(market: str, setup: str) -> bool:
    """Returns True if this market:setup combo is currently suspended."""
    key = f"{market}:{setup}"
    suspended = get_suspended_setups()
    return key in suspended


def check_and_update_suspensions() -> list[str]:
    """
    Core suspension engine.  Reads setup_performance.json.
    - If total >= 5 AND win_rate < 35%: suspend it.
    - If already suspended AND win_rate climbs back above 45%: restore it.
    Returns list of change strings for logging/Telegram.
    Saves updated suspended_setups.json.
    """
    perf = _load_performance()
    suspended = get_suspended_setups()
    changes: list[str] = []

    for key, data in perf.items():
        wins   = data.get("wins", 0)
        losses = data.get("losses", 0)
        total  = data.get("total", wins + losses)
        wr     = round(wins / max(1, total) * 100, 1)

        if key in suspended:
            # Already suspended — check for restoration
            if total >= _SUSPEND_MIN_TRADES and wr >= _RESTORE_WR_ABOVE:
                del suspended[key]
                changes.append(f"RESTORED {key} ({wr}% WR, {total} trades)")
        else:
            # Not suspended — check if it should be
            if total >= _SUSPEND_MIN_TRADES and wr < _SUSPEND_WR_BELOW:
                suspended[key] = {
                    "reason":       f"{wins}W/{losses}L ({wr}% WR)",
                    "suspended_at": datetime.now(timezone.utc).isoformat(),
                    "total_at_suspension": total,
                    "wr_at_suspension":    wr,
                }
                changes.append(f"SUSPENDED {key} ({wins}W/{losses}L, {wr}% WR)")

    _save_suspended_setups(suspended)
    return changes


def get_suspension_report() -> str:
    """Telegram-formatted string showing suspended/restored setups."""
    suspended = get_suspended_setups()
    if not suspended:
        return "✅ *No setups currently suspended.* All setups active."

    lines = [
        "⛔ *Suspended Setups*",
        "━━━━━━━━━━━━━━━━━━",
        "_These setups are blocked from firing due to negative EV._",
        "",
    ]
    for key, info in sorted(suspended.items()):
        reason = info.get("reason", "?")
        since  = info.get("suspended_at", "")[:10]
        lines.append(f"  🔴 `{key}` — {reason} (since {since})")

    lines.append("")
    lines.append("_Setups auto-restore when WR climbs above 45%._")
    return "\n".join(lines)


# ------------------------------------------------------------------ #
# LEARNING SYSTEM
# Tracks win rate per setup type per market.
# Boosts conviction for setups that historically work.
# Gets smarter every single trade automatically.
# ------------------------------------------------------------------ #
def _load_performance() -> dict:
    """Load historical setup performance from file."""
    if os.path.exists(LEARNING_FILE):
        try:
            with open(LEARNING_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}

def _save_performance(data: dict):
    safe_io.atomic_write_json(LEARNING_FILE, data)

def _performance_bonus(market: str, setup_type: str) -> int:
    """
    Bayesian-adjusted conviction bonus/penalty.
    Uses (wins + 10) / (trades + 22) to avoid small-sample overreaction.
    """
    perf = _load_performance()
    key  = f"{market}:{setup_type}"
    data = perf.get(key, {})
    wins   = data.get("wins", 0)
    losses = data.get("losses", 0)
    total  = wins + losses
    if total < 3:
        return 0
    adjusted_rate = (wins + 10) / (total + 22)
    if adjusted_rate > 0.65:  return 12
    if adjusted_rate > 0.58:  return 6
    if adjusted_rate < 0.38:  return -12
    if adjusted_rate < 0.45:  return -6
    return 0

def record_trade_result(market: str, setup_type: str, result: str):
    """
    Call this whenever a trade closes WIN or LOSS.
    Updates the learning file automatically.
    """
    perf = _load_performance()
    key  = f"{market}:{setup_type}"
    if key not in perf:
        perf[key] = {"wins": 0, "losses": 0, "total": 0}
    perf[key]["total"] += 1
    if result == "WIN":
        perf[key]["wins"] += 1
    elif result == "LOSS":
        perf[key]["losses"] += 1
    perf[key]["win_rate"] = round(perf[key]["wins"] / max(1, perf[key]["total"]) * 100, 1)
    perf[key]["last_updated"] = datetime.now().isoformat()
    _save_performance(perf)

def get_learning_summary() -> str:
    """Returns a readable summary of what the bot has learned."""
    perf = _load_performance()
    if not perf:
        return "No trade history yet. Bot learns as trades close."
    lines = ["🧠 *What the bot has learned:*\n"]
    for key, data in sorted(perf.items(), key=lambda x: x[1].get("total", 0), reverse=True):
        total = data.get("total", 0)
        if total < 3:
            continue
        wr  = data.get("win_rate", 0)
        w   = data.get("wins", 0)
        l   = data.get("losses", 0)
        adj = _performance_bonus(*key.split(":"))
        bon = f"+{adj}" if adj > 0 else str(adj)
        bar = "🟢" if wr >= 60 else "🔴" if wr < 45 else "🟡"
        lines.append(f"{bar} *{key}*: {w}W/{l}L ({wr}% WR) → Conv adj: {bon}")
    return "\n".join(lines) if len(lines) > 1 else "Not enough closed trades yet to learn from."

# ------------------------------------------------------------------ #
# Indicators
# ------------------------------------------------------------------ #
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d  = s.diff()
    up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100/(1+rs)).fillna(50)

def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([(h-l),(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    up = h.diff(); dn = -l.diff()
    plus_dm  = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr   = pd.concat([(h-l),(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    atr_ = tr.ewm(alpha=1/n, adjust=False).mean().replace(0, np.nan)
    pdi  = 100*pd.Series(plus_dm, index=df.index).ewm(alpha=1/n,adjust=False).mean()/atr_
    mdi  = 100*pd.Series(minus_dm,index=df.index).ewm(alpha=1/n,adjust=False).mean()/atr_
    dx   = 100*(pdi-mdi).abs()/(pdi+mdi).replace(0, np.nan)
    return dx.ewm(alpha=1/n, adjust=False).mean().fillna(0)

def vwap(df: pd.DataFrame) -> pd.Series:
    tp = (df["High"]+df["Low"]+df["Close"])/3
    v  = df["Volume"].replace(0, np.nan).ffill().fillna(1)
    try:
        grp = df.index.normalize()
        pv  = (tp*v).groupby(grp).cumsum()
        vv  = v.groupby(grp).cumsum()
    except:
        pv = (tp*v).cumsum()
        vv = v.cumsum()
    return pv/vv

# ────────────────────────────────────────────────────────
# Additional indicators (Batch 2A — used for logging context,
# NOT yet used in setup detection or scoring)
# ────────────────────────────────────────────────────────

def bollinger_bands(s: pd.Series, n: int = 20, std_dev: float = 2.0):
    """
    Returns (upper, middle, lower) Bollinger Bands as pd.Series.
    Uses simple moving average for middle band.
    Safe on short series — pandas rolling returns NaN for early rows.
    """
    middle = s.rolling(n).mean()
    std    = s.rolling(n).std()
    upper  = middle + (std * std_dev)
    lower  = middle - (std * std_dev)
    return upper, middle, lower


def stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3):
    """
    Returns (%K, %D) Stochastic oscillator as pd.Series.
    Uses df["High"], df["Low"], df["Close"] — case-sensitive column names.
    Division-by-zero in range gives NaN, filled with 50 (neutral).
    """
    low_n  = df["Low"].rolling(k_period).min()
    high_n = df["High"].rolling(k_period).max()
    rng    = (high_n - low_n).replace(0, np.nan)
    k = 100 * (df["Close"] - low_n) / rng
    k = k.fillna(50)
    d = k.rolling(d_period).mean().fillna(50)
    return k, d


def macd(s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """
    Returns (macd_line, signal_line, histogram) as pd.Series.
    Uses pandas EWM — matches the ema/rsi/adx style already in this file.
    """
    ema_fast = s.ewm(span=fast, adjust=False).mean()
    ema_slow = s.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist

# ------------------------------------------------------------------ #
# Swing structure
# ------------------------------------------------------------------ #
def swing_points(df: pd.DataFrame, lookback: int = 5):
    h, l   = df["High"].values, df["Low"].values
    highs, lows = [], []
    for i in range(lookback, len(df)-lookback):
        if h[i] == max(h[i-lookback:i+lookback+1]): highs.append(i)
        if l[i] == min(l[i-lookback:i+lookback+1]): lows.append(i)
    return highs, lows

def structure_bias(df: pd.DataFrame) -> str:
    if df is None or len(df) < 20:
        return "MIXED"
    hi, lo = swing_points(df, 5)
    if len(hi) < 2 or len(lo) < 2:
        return "MIXED"
    h1, h2 = df["High"].iloc[hi[-2]], df["High"].iloc[hi[-1]]
    l1, l2 = df["Low"].iloc[lo[-2]],  df["Low"].iloc[lo[-1]]
    if h2 > h1 and l2 > l1: return "HH_HL"
    if h2 < h1 and l2 < l1: return "LH_LL"
    return "MIXED"

def nearest_swing_level(df: pd.DataFrame, direction: str, price: float,
                        min_dist_atr: float = MIN_DIST_ATR_FOR_SWING) -> Optional[float]:
    a = atr(df).iloc[-1]
    if not np.isfinite(a) or a <= 0: return None
    hi, lo     = swing_points(df, 5)
    candidates = []
    if direction == "LONG":
        for i in hi:
            lvl = df["High"].iloc[i]
            if lvl - price > min_dist_atr*a: candidates.append(lvl)
        return min(candidates) if candidates else None
    else:
        for i in lo:
            lvl = df["Low"].iloc[i]
            if price - lvl > min_dist_atr*a: candidates.append(lvl)
        return max(candidates) if candidates else None

# ------------------------------------------------------------------ #
# Trend Brain (-10 to +10)
# ------------------------------------------------------------------ #
def _ema_stack_score(df: pd.DataFrame) -> int:
    if len(df) < 210: return 0
    c = df["Close"]
    e21, e50, e200 = ema(c,21).iloc[-1], ema(c,50).iloc[-1], ema(c,200).iloc[-1]
    p = c.iloc[-1]
    if p>e21>e50>e200: return 2
    if p>e50>e200:     return 1
    if p<e21<e50<e200: return -2
    if p<e50<e200:     return -1
    return 0

def _struct_score(df: pd.DataFrame) -> int:
    b = structure_bias(df)
    return {"HH_HL": 2, "LH_LL": -2, "MIXED": 0}[b]

def trend_score(tf_frames: dict, market: str) -> tuple[int, dict]:
    """
    Scores trend from -10 (strong bear) to +10 (strong bull).
    Reads across all available timeframes.
    """
    score = 0
    bd    = {}
    weights = {"1d": 3, "4h": 2, "1h": 1}
    for tf, w in weights.items():
        df = tf_frames.get(tf)
        if df is None or len(df) < 20:
            continue
        es = _ema_stack_score(df)
        ss = _struct_score(df)
        tf_score = (es + ss) * w
        score += tf_score
        bd[tf] = {"ema": es, "struct": ss, "weighted": tf_score}
    score = max(-10, min(10, score))
    return score, bd

# ------------------------------------------------------------------ #
# Setup Detection
# ------------------------------------------------------------------ #
def detect_setups(df_entry: pd.DataFrame, df_htf: pd.DataFrame,
                  htf_bias: str) -> list[dict]:
    """
    Detects setups on the entry timeframe confirmed by HTF bias.
    Returns list of setup dicts.
    """
    setups = []
    if df_entry is None or len(df_entry) < 50:
        return setups

    # Regime gating
    try:
        from regime_classifier import classify_regime
        regime_info = classify_regime(df_entry)
        current_regime = regime_info.get("regime", "RANGING")
    except Exception:
        current_regime = "RANGING"

    try:
        atr_v     = float(atr(df_entry).iloc[-1])
        rsi_v     = float(rsi(df_entry["Close"]).iloc[-1])
        adx_v     = float(adx(df_entry).iloc[-1])
        vwap_v    = float(vwap(df_entry).iloc[-1])

        last      = df_entry.iloc[-1]
        prev      = df_entry.iloc[-2]
        close     = float(last["Close"])
        prev_close= float(prev["Close"])

        recent    = df_entry.iloc[-30:]
        swing_low = float(recent["Low"].min())
        swing_high= float(recent["High"].max())

        # EMAs
        e20  = float(ema(df_entry["Close"], 20).iloc[-1])
        e50  = float(ema(df_entry["Close"], 50).iloc[-1])
        e200 = float(ema(df_entry["Close"], 200).iloc[-1])
        pe50 = float(ema(df_entry["Close"], 50).iloc[-2])

        bull_htf = htf_bias in ("HH_HL", "BULL")
        bear_htf = htf_bias in ("LH_LL", "BEAR")

        # ── 1. BULLISH LIQUIDITY SWEEP ──
        if (float(prev["Low"]) < swing_low and
                float(prev["Close"]) > swing_low and
                close > prev_close):
            stop = float(prev["Low"]) - atr_v * 0.3
            setups.append({
                "type":      "LIQ_SWEEP_BULL",
                "direction": "LONG",
                "entry":     close,
                "raw_stop":  stop,
                "level":     swing_low,
                "detail":    f"Swept below swing low {round(swing_low,4)}, closed back above. Stop hunt done.",
            })

        # ── 2. BEARISH LIQUIDITY SWEEP ──
        if (float(prev["High"]) > swing_high and
                float(prev["Close"]) < swing_high and
                close < prev_close):
            stop = float(prev["High"]) + atr_v * 0.3
            setups.append({
                "type":      "LIQ_SWEEP_BEAR",
                "direction": "SHORT",
                "entry":     close,
                "raw_stop":  stop,
                "level":     swing_high,
                "detail":    f"Swept above swing high {round(swing_high,4)}, closed back below. Bull trap done.",
            })

        # ── 3. BULLISH EMA50 RECLAIM ──
        if (prev_close < pe50 and close > e50 and
                e20 > e50 > e200 and 45 < rsi_v < 72 and
                adx_v >= ADX_MIN_FOR_RECLAIM):
            stop = e50 - atr_v * 0.5
            setups.append({
                "type":      "EMA50_RECLAIM",
                "direction": "LONG",
                "entry":     close,
                "raw_stop":  stop,
                "level":     e50,
                "detail":    f"Reclaimed EMA50 with bullish stack. RSI {round(rsi_v,1)}, ADX {round(adx_v,1)}.",
            })

        # ── 4. BEARISH EMA50 BREAKDOWN ──
        if (prev_close > pe50 and close < e50 and
                e20 < e50 < e200 and 28 < rsi_v < 55 and
                adx_v >= ADX_MIN_FOR_RECLAIM):
            stop = e50 + atr_v * 0.5
            setups.append({
                "type":      "EMA50_BREAKDOWN",
                "direction": "SHORT",
                "entry":     close,
                "raw_stop":  stop,
                "level":     e50,
                "detail":    f"Broke below EMA50 with bearish stack. RSI {round(rsi_v,1)}, ADX {round(adx_v,1)}.",
            })

        # ── 5. BULLISH VWAP BOUNCE ──
        vwap_bounce = False
        # Original: prev candle low dipped below VWAP, closed back above
        if float(prev["Low"]) < vwap_v and close > vwap_v and rsi_v < 62:
            vwap_bounce = True
        # NQ alternative: close within 0.1 ATR above VWAP after pullback from swing high
        if not vwap_bounce and abs(close - vwap_v) < 0.1 * atr_v and close > vwap_v:
            swing_hi_20 = float(df_entry.iloc[-20:]["High"].max())
            pullback = swing_hi_20 - close
            if pullback >= 0.5 * atr_v:
                vwap_bounce = True
        if vwap_bounce:
            stop = min(float(prev["Low"]), vwap_v) - atr_v * 0.2
            setups.append({
                "type":      "VWAP_BOUNCE_BULL",
                "direction": "LONG",
                "entry":     close,
                "raw_stop":  stop,
                "level":     vwap_v,
                "detail":    f"VWAP bounce at {round(vwap_v,4)}. Institutional support level.",
            })

        # ── 6. BEARISH VWAP REJECTION ──
        # Only fire if trend is not strongly bullish
        if (float(prev["High"]) > vwap_v and
                close < vwap_v and rsi_v > 38):
            stop = float(prev["High"]) + atr_v * 0.2
            setups.append({
                "type":      "VWAP_REJECT_BEAR",
                "direction": "SHORT",
                "entry":     close,
                "raw_stop":  stop,
                "level":     vwap_v,
                "detail":    f"Pushed above VWAP {round(vwap_v,4)}, rejected. Institutions selling.",
            })

        # ── 7. APPROACHING SUPPORT (Anticipatory) ──
        proximity = atr_v * 0.75
        if (close > swing_low and
                close - swing_low < proximity and rsi_v < 45):
            stop = swing_low - atr_v * 0.5
            setups.append({
                "type":      "APPROACH_SUPPORT",
                "direction": "WATCH_LONG",
                "entry":     swing_low,
                "raw_stop":  stop,
                "level":     swing_low,
                "detail":    f"Within {round(close-swing_low,2)} pts of support {round(swing_low,4)}. Get ready.",
            })

        # ── 8. APPROACHING RESISTANCE (Anticipatory) ──
        if (close < swing_high and
                swing_high - close < proximity and rsi_v > 55):
            stop = swing_high + atr_v * 0.5
            setups.append({
                "type":      "APPROACH_RESIST",
                "direction": "WATCH_SHORT",
                "entry":     swing_high,
                "raw_stop":  stop,
                "level":     swing_high,
                "detail":    f"Within {round(swing_high-close,2)} pts of resistance {round(swing_high,4)}. Get ready.",
            })

        # ── 9. BULLISH EMA21 PULLBACK ──
        # Price pulls back to EMA21 in a bullish trend and bounces
        # Bread-and-butter trend continuation — tight stop, clean entry
        e21 = float(ema(df_entry["Close"], 21).iloc[-1])
        if (e20 > e50 > e200 and                    # bullish EMA stack
                close > e21 and                       # closed above EMA21
                float(prev["Low"]) <= e21 * 1.002 and # prev bar touched or dipped to EMA21
                rsi_v > 40 and rsi_v < 65 and         # not overbought
                adx_v >= 18):                         # needs some trend
            stop = min(float(prev["Low"]), e21) - atr_v * 0.3
            setups.append({
                "type":      "EMA21_PULLBACK_BULL",
                "direction": "LONG",
                "entry":     close,
                "raw_stop":  stop,
                "level":     e21,
                "detail":    f"Pulled back to EMA21 ({round(e21,4)}) in bullish stack and bounced. "
                             f"RSI {round(rsi_v,1)}, ADX {round(adx_v,1)}. Trend continuation.",
            })

        # ── 10. BEARISH EMA21 PULLBACK ──
        if (e20 < e50 < e200 and                     # bearish EMA stack
                close < e21 and                       # closed below EMA21
                float(prev["High"]) >= e21 * 0.998 and # prev bar rallied to EMA21
                rsi_v > 35 and rsi_v < 60 and
                adx_v >= 18):
            stop = max(float(prev["High"]), e21) + atr_v * 0.3
            setups.append({
                "type":      "EMA21_PULLBACK_BEAR",
                "direction": "SHORT",
                "entry":     close,
                "raw_stop":  stop,
                "level":     e21,
                "detail":    f"Rallied to EMA21 ({round(e21,4)}) in bearish stack and rejected. "
                             f"RSI {round(rsi_v,1)}, ADX {round(adx_v,1)}. Trend continuation.",
            })

        # ── 11. BREAK-RETEST BULL ──
        # Price broke above swing high earlier, pulled back to retest it as support
        # Classic institutional pattern — broken resistance becomes support
        if len(df_entry) >= 40:
            try:
                # Look for a prior swing high that was broken in last 15 bars
                lookback_range = df_entry.iloc[-40:-10]
                recent_range = df_entry.iloc[-10:]
                old_high = float(lookback_range["High"].max())

                # Check: did we break above old_high recently and now retesting?
                broke_above = any(float(recent_range["Close"].iloc[i]) > old_high
                                  for i in range(len(recent_range)))
                near_level = abs(close - old_high) < atr_v * 0.5
                holding_above = close > old_high * 0.998

                if (broke_above and near_level and holding_above and
                        float(prev["Low"]) <= old_high * 1.003 and  # dipped to retest
                        rsi_v > 40 and rsi_v < 68):
                    stop = old_high - atr_v * 0.4
                    setups.append({
                        "type":      "BREAK_RETEST_BULL",
                        "direction": "LONG",
                        "entry":     close,
                        "raw_stop":  stop,
                        "level":     old_high,
                        "detail":    f"Broke above {round(old_high,4)}, pulled back to retest as support. "
                                     f"Holding. Classic break-retest entry.",
                    })
            except Exception:
                pass

        # ── 12. BREAK-RETEST BEAR ──
        # Price broke below swing low, rallied back to retest it as resistance
        if len(df_entry) >= 40:
            try:
                lookback_range = df_entry.iloc[-40:-10]
                recent_range = df_entry.iloc[-10:]
                old_low = float(lookback_range["Low"].min())

                broke_below = any(float(recent_range["Close"].iloc[i]) < old_low
                                  for i in range(len(recent_range)))
                near_level = abs(close - old_low) < atr_v * 0.5
                holding_below = close < old_low * 1.002

                if (broke_below and near_level and holding_below and
                        float(prev["High"]) >= old_low * 0.997 and
                        rsi_v > 32 and rsi_v < 60):
                    stop = old_low + atr_v * 0.4
                    setups.append({
                        "type":      "BREAK_RETEST_BEAR",
                        "direction": "SHORT",
                        "entry":     close,
                        "raw_stop":  stop,
                        "level":     old_low,
                        "detail":    f"Broke below {round(old_low,4)}, rallied back to retest as resistance. "
                                     f"Rejected. Classic break-retest entry.",
                    })
            except Exception:
                pass

        # ── VOLATILITY CONTRACTION BREAKOUT ──
        if len(df_entry) >= 100:
            try:
                atr7 = atr(df_entry, 7)
                atr7_vals = atr7.iloc[-100:].values
                atr7_current = float(atr7.iloc[-1])
                atr7_pctile = float((atr7_vals < atr7_current).sum() / len(atr7_vals) * 100)

                if atr7_pctile <= 25:
                    range7_high = float(df_entry.iloc[-7:]["High"].max())
                    range7_low  = float(df_entry.iloc[-7:]["Low"].min())
                    range7 = range7_high - range7_low
                    # Check if this is narrowest range in 40 bars
                    is_narrowest = True
                    for j in range(7, min(40, len(df_entry))):
                        slice_hi = float(df_entry.iloc[-j-7:-j]["High"].max()) if j+7 <= len(df_entry) else range7_high
                        slice_lo = float(df_entry.iloc[-j-7:-j]["Low"].min()) if j+7 <= len(df_entry) else range7_low
                        if (slice_hi - slice_lo) < range7:
                            is_narrowest = False
                            break

                    vol_ok = vol_ratio >= 1.5 if 'vol_ratio' in dir() else True

                    if is_narrowest and vol_ok:
                        if close > range7_high:
                            stop_vcb = range7_low - atr_v * 0.2
                            setups.append({
                                "type": "VOLATILITY_CONTRACTION_BREAKOUT",
                                "direction": "LONG",
                                "entry": close,
                                "raw_stop": stop_vcb,
                                "level": range7_high,
                                "detail": f"Volatility squeeze breakout above {round(range7_high,4)}. "
                                          f"7-bar range narrowest in 40 bars. Volume surge confirmed.",
                            })
                        elif close < range7_low:
                            stop_vcb = range7_high + atr_v * 0.2
                            setups.append({
                                "type": "VOLATILITY_CONTRACTION_BREAKOUT",
                                "direction": "SHORT",
                                "entry": close,
                                "raw_stop": stop_vcb,
                                "level": range7_low,
                                "detail": f"Volatility squeeze breakdown below {round(range7_low,4)}. "
                                          f"7-bar range narrowest in 40 bars. Volume surge confirmed.",
                            })
            except Exception:
                pass

        # ── FAILED BREAKDOWN BULL ──
        if len(df_entry) >= 25:
            try:
                low_20 = float(df_entry.iloc[-20:]["Low"].min())
                # Check if price closed below 20-bar low in last 3 bars
                broke_below = False
                breakdown_vol = 0
                for bi in range(2, 5):
                    if bi < len(df_entry) and float(df_entry.iloc[-bi]["Close"]) < low_20:
                        broke_below = True
                        breakdown_vol = float(df_entry.iloc[-bi]["Volume"])
                        break
                if broke_below and close > low_20 and breakdown_vol > 0:
                    cur_vol = float(last["Volume"])
                    if cur_vol >= breakdown_vol * 1.2:
                        stop_fb = low_20 - atr_v * 0.3
                        setups.append({
                            "type": "FAILED_BREAKDOWN_BULL",
                            "direction": "LONG",
                            "entry": close,
                            "raw_stop": stop_fb,
                            "level": low_20,
                            "detail": f"Failed breakdown below {round(low_20,4)}. Price reclaimed with "
                                      f"{round(cur_vol/breakdown_vol,1)}x the breakdown volume. Bear trap.",
                        })
            except Exception:
                pass

        # ── FAILED BREAKOUT BEAR ──
        if len(df_entry) >= 25:
            try:
                high_20 = float(df_entry.iloc[-20:]["High"].max())
                broke_above = False
                breakout_vol = 0
                for bi in range(2, 5):
                    if bi < len(df_entry) and float(df_entry.iloc[-bi]["Close"]) > high_20:
                        broke_above = True
                        breakout_vol = float(df_entry.iloc[-bi]["Volume"])
                        break
                if broke_above and close < high_20 and breakout_vol > 0:
                    cur_vol = float(last["Volume"])
                    if cur_vol >= breakout_vol * 1.2:
                        stop_fb = high_20 + atr_v * 0.3
                        setups.append({
                            "type": "FAILED_BREAKOUT_BEAR",
                            "direction": "SHORT",
                            "entry": close,
                            "raw_stop": stop_fb,
                            "level": high_20,
                            "detail": f"Failed breakout above {round(high_20,4)}. Price rejected with "
                                      f"{round(cur_vol/breakout_vol,1)}x the breakout volume. Bull trap.",
                        })
            except Exception:
                pass

        # ── 13. BULLISH RSI DIVERGENCE ──
        # Price lower low but RSI higher low = hidden buying pressure / selling exhaustion
        if len(df_entry) >= 25:
            try:
                window     = df_entry.iloc[-22:]
                rsi_full   = rsi(df_entry["Close"])
                rsi_window = rsi_full.iloc[-22:]

                # Find the lowest low in the first half of the window (earlier reference)
                first_half = window.iloc[:12]
                first_half_rsi = rsi_window.iloc[:12]
                first_low_pos  = first_half["Low"].idxmin()
                first_low_price = float(first_half["Low"][first_low_pos])
                first_low_rsi   = float(first_half_rsi[first_low_pos])

                # Recent low (last 5 bars)
                recent_low_price = float(window["Low"].iloc[-5:].min())
                recent_low_rsi   = float(rsi_window.iloc[-5:].min())

                # Bullish divergence: price lower low, RSI higher low
                if (recent_low_price < first_low_price * 0.9995 and
                        recent_low_rsi   > first_low_rsi   + 4 and
                        rsi_v < 52 and
                        close > recent_low_price * 1.001):
                    stop = recent_low_price - atr_v * 0.4
                    setups.append({
                        "type":      "RSI_DIV_BULL",
                        "direction": "LONG",
                        "entry":     close,
                        "raw_stop":  stop,
                        "level":     first_low_price,
                        "detail":    f"Bullish RSI divergence: price lower low ({round(recent_low_price,2)}) "
                                     f"but RSI higher low ({round(recent_low_rsi,1)} vs {round(first_low_rsi,1)}). "
                                     f"Sellers exhausted. Reversal likely.",
                    })
            except Exception:
                pass

        # ── 10. BEARISH RSI DIVERGENCE ──
        # Price higher high but RSI lower high = hidden selling pressure / buying exhaustion
        if len(df_entry) >= 25:
            try:
                window     = df_entry.iloc[-22:]
                rsi_full   = rsi(df_entry["Close"])
                rsi_window = rsi_full.iloc[-22:]

                first_half     = window.iloc[:12]
                first_half_rsi = rsi_window.iloc[:12]
                first_high_pos   = first_half["High"].idxmax()
                first_high_price = float(first_half["High"][first_high_pos])
                first_high_rsi   = float(first_half_rsi[first_high_pos])

                recent_high_price = float(window["High"].iloc[-5:].max())
                recent_high_rsi   = float(rsi_window.iloc[-5:].max())

                # Bearish divergence: price higher high, RSI lower high
                if (recent_high_price > first_high_price * 1.0005 and
                        recent_high_rsi   < first_high_rsi   - 4 and
                        rsi_v > 48 and
                        close < recent_high_price * 0.999):
                    stop = recent_high_price + atr_v * 0.4
                    setups.append({
                        "type":      "RSI_DIV_BEAR",
                        "direction": "SHORT",
                        "entry":     close,
                        "raw_stop":  stop,
                        "level":     first_high_price,
                        "detail":    f"Bearish RSI divergence: price higher high ({round(recent_high_price,2)}) "
                                     f"but RSI lower high ({round(recent_high_rsi,1)} vs {round(first_high_rsi,1)}). "
                                     f"Buyers exhausted. Reversal likely.",
                    })
            except Exception:
                pass

        # ── VWAP_RECLAIM (all markets, BULL only) ──
        # Price was below VWAP for 3+ bars, then reclaims above with volume
        try:
            if len(df_entry) >= 10:
                vwap_series = vwap(df_entry)
                below_count = 0
                for bi in range(4, 1, -1):
                    if float(df_entry.iloc[-bi]["Close"]) < float(vwap_series.iloc[-bi]):
                        below_count += 1
                if below_count >= 3 and close > vwap_v:
                    vol_check = vol_ratio if 'vol_ratio' in dir() else 0
                    if vol_check >= 1.2 and 45 <= rsi_v <= 65:
                        # Check 1h trend is not strongly bearish
                        if htf_bias != "LH_LL":
                            stop_vr = vwap_v - atr_v * 0.3
                            setups.append({
                                "type":      "VWAP_RECLAIM",
                                "direction": "LONG",
                                "entry":     close,
                                "raw_stop":  stop_vr,
                                "level":     vwap_v,
                                "detail":    f"Reclaimed VWAP ({round(vwap_v,4)}) after {below_count} bars below. "
                                             f"Volume {round(vol_check,1)}x avg. Institutional buying.",
                            })
        except Exception:
            pass

        # ── HTF_LEVEL_BOUNCE (all markets) ──
        # Price bounces off a key level (1h swing or prior day H/L) with engulfing/pin bar
        try:
            if df_htf is not None and len(df_htf) >= 20 and len(df_entry) >= 5:
                hi_idx, lo_idx = swing_points(df_htf, 5) if len(df_htf) >= 15 else ([], [])
                key_levels = []
                # 1h swing highs/lows
                for i in hi_idx[-3:]:
                    key_levels.append(("resist", float(df_htf["High"].iloc[i])))
                for i in lo_idx[-3:]:
                    key_levels.append(("support", float(df_htf["Low"].iloc[i])))

                # Check for bullish engulfing or pin bar near key level
                for level_type, level in key_levels:
                    dist = abs(close - level)
                    if dist < 0.25 * atr_v:
                        # Check for bullish engulfing at support
                        body_cur = close - float(last["Open"])
                        body_prev = prev_close - float(prev["Open"])
                        is_bull_engulf = (body_cur > 0 and body_prev < 0 and
                                         abs(body_cur) > abs(body_prev) * 1.2)
                        # Check for pin bar (long lower wick)
                        lower_wick = min(float(last["Open"]), close) - float(last["Low"])
                        upper_wick = float(last["High"]) - max(float(last["Open"]), close)
                        body_size = abs(body_cur)
                        is_pin_bull = (lower_wick > body_size * 2 and upper_wick < body_size)
                        is_pin_bear = (upper_wick > body_size * 2 and lower_wick < body_size)

                        vol_ok = vol_ratio >= 1.1 if 'vol_ratio' in dir() else False
                        if level_type == "support" and (is_bull_engulf or is_pin_bull) and vol_ok:
                            stop_htf = level - atr_v * 0.4
                            setups.append({
                                "type":      "HTF_LEVEL_BOUNCE",
                                "direction": "LONG",
                                "entry":     close,
                                "raw_stop":  stop_htf,
                                "level":     level,
                                "detail":    f"Bouncing off 1H key support {round(level,4)}. "
                                             f"{'Bullish engulfing' if is_bull_engulf else 'Pin bar'} confirmed.",
                            })
                        elif level_type == "resist" and is_pin_bear and vol_ok:
                            stop_htf = level + atr_v * 0.4
                            setups.append({
                                "type":      "HTF_LEVEL_BOUNCE",
                                "direction": "SHORT",
                                "entry":     close,
                                "raw_stop":  stop_htf,
                                "level":     level,
                                "detail":    f"Rejecting off 1H key resistance {round(level,4)}. "
                                             f"Bearish pin bar confirmed.",
                            })
        except Exception:
            pass

    except Exception as e:
        import logging
        logging.getLogger("nqcalls").warning(f"detect_setups error: {e}")

    # HTF filter — don't fire confirmed longs in bear structure or vice versa
    filtered = []
    for s in setups:
        d = s["direction"]
        if d == "LONG"  and bear_htf: continue
        if d == "SHORT" and bull_htf: continue
        # Extra: block bearish setups in strongly bullish HTF
        if s["type"] in ("VWAP_REJECT_BEAR", "EMA50_BREAKDOWN", "EMA21_PULLBACK_BEAR", "BREAK_RETEST_BEAR") and htf_bias == "HH_HL": continue
        # Extra: block bullish setups in strongly bearish HTF
        if s["type"] in ("VWAP_BOUNCE_BULL", "EMA50_RECLAIM", "EMA21_PULLBACK_BULL", "BREAK_RETEST_BULL") and htf_bias == "LH_LL": continue
        filtered.append(s)

    # Regime filter — skip setups that conflict with current regime
    regime_filtered = []
    import logging as _logging
    _rlog = _logging.getLogger("nqcalls")
    BEAR_SETUPS = {"VWAP_REJECT_BEAR", "APPROACH_RESIST", "LIQ_SWEEP_BEAR", "RSI_DIV_BEAR",
                   "EMA50_BREAKDOWN", "EMA21_PULLBACK_BEAR", "BREAK_RETEST_BEAR", "FAILED_BREAKOUT_BEAR"}
    BULL_SETUPS = {"VWAP_BOUNCE_BULL", "APPROACH_SUPPORT", "LIQ_SWEEP_BULL", "RSI_DIV_BULL",
                   "EMA50_RECLAIM", "EMA21_PULLBACK_BULL", "BREAK_RETEST_BULL", "FAILED_BREAKDOWN_BULL"}
    for s in filtered:
        st = s["type"]
        if st in BEAR_SETUPS and current_regime == "TRENDING_BULL":
            _rlog.info(f"Regime skip: {st} blocked in TRENDING_BULL")
            continue
        if st in BULL_SETUPS and current_regime == "TRENDING_BEAR":
            _rlog.info(f"Regime skip: {st} blocked in TRENDING_BEAR")
            continue
        regime_filtered.append(s)

    return regime_filtered

# ------------------------------------------------------------------ #
# Conviction Scoring (0-100)
# ------------------------------------------------------------------ #
# ── Task 7: Setup-aware volume direction ─────────────────────────
VOLUME_DIRECTION = {
    "VWAP_BOUNCE_BULL": "confirm",
    "VWAP_REJECT_BEAR": "confirm",
    "VWAP_RECLAIM":     "confirm",
    "BREAK_RETEST_BULL": "invert",   # high vol on retest = no rejection = bearish
    "BREAK_RETEST_BEAR": "invert",
    "EMA21_PULLBACK_BULL": "confirm",
    "EMA21_PULLBACK_BEAR": "confirm",
    "EMA50_RECLAIM":    "confirm",
    "EMA50_BREAKDOWN":  "confirm",
    "APPROACH_SUPPORT":  "neutral",
    "APPROACH_RESIST":   "neutral",
    "LIQ_SWEEP_BULL":   "confirm",
    "LIQ_SWEEP_BEAR":   "confirm",
    "OPENING_RANGE_BREAKOUT": "confirm",
    "HTF_LEVEL_BOUNCE": "confirm",
}


def conviction_score(setup: dict, trend: int, df_entry: pd.DataFrame,
                     df_htf: Optional[pd.DataFrame], news_flag: bool,
                     adx_val: float, rsi_val: float,
                     vol_ratio: float, clean_path_atr: float) -> tuple[int, str, dict]:
    """
    Scores a setup from 0-100.
    HIGH = 80+, MEDIUM = 65-79, LOW = 50-64, REJECT = below 50
    Includes: learning bonus, time-of-day, setup-aware volume.
    """
    # Audit Finding #2 / BACKLOG #3 (2026-04-28): base 30 → 15.
    # Old base meant setups with no real edge still cleared TIER_LOW (50)
    # with one or two soft bonuses. Lower floor forces score to be earned.
    s  = 15  # base
    bd = {}
    setup_type = setup.get("type", "")

    # Trend alignment (+20 max)
    direction = setup.get("direction", "")
    if "LONG" in direction:
        tq = max(0, min(20, trend * 2))
    elif "SHORT" in direction:
        tq = max(0, min(20, -trend * 2))
    else:
        tq = 5
    s += tq; bd["trend"] = tq

    # HTF structure bonus (+10)
    if df_htf is not None:
        bias = structure_bias(df_htf)
        if ("LONG" in direction and bias == "HH_HL") or \
           ("SHORT" in direction and bias == "LH_LL"):
            s += 10; bd["htf_struct"] = 10
        else:
            bd["htf_struct"] = 0

    # Task 7: Setup-aware volume scoring (+15 max / -10 penalty)
    vol_dir = VOLUME_DIRECTION.get(setup_type, "confirm")
    if vol_dir == "confirm":
        if vol_ratio >= 2.0:   vq = 15
        elif vol_ratio >= 1.5: vq = 10
        elif vol_ratio >= 1.2: vq = 5
        elif vol_ratio < 0.5:  vq = -10
        elif vol_ratio < 0.8:  vq = -5
        else:                  vq = 0
    elif vol_dir == "invert":
        # For break-retest: high volume on retest bar = bad (means selling/buying INTO the retest)
        if vol_ratio >= 2.0:   vq = -10
        elif vol_ratio >= 1.5: vq = -5
        elif vol_ratio < 0.8:  vq = 10  # low vol retest = healthy
        elif vol_ratio < 0.5:  vq = 15
        else:                  vq = 0
    else:  # neutral
        vq = 0
    s += vq; bd["volume"] = vq

    # RSI quality (+10)
    if "LONG" in direction:
        rq = 10 if 40 < rsi_val < 60 else (5 if rsi_val < 70 else 0)
    elif "SHORT" in direction:
        rq = 10 if 40 < rsi_val < 60 else (5 if rsi_val > 30 else 0)
    else:
        rq = 5
    s += rq; bd["rsi"] = rq

    # Clean path to target (+15)
    cp = 15 if clean_path_atr >= 2.0 else (7 if clean_path_atr >= 1.2 else 0)
    s += cp; bd["clean_path"] = cp

    # News penalty (-20)
    if news_flag:
        s -= 20; bd["news_penalty"] = -20

    # ADX regime — penalize reclaims in choppy market (-15)
    if setup_type in ("EMA50_RECLAIM", "EMA50_BREAKDOWN") and adx_val < ADX_MIN_FOR_RECLAIM:
        s -= 15; bd["adx_weak"] = -15

    # LEARNING BONUS — historical win rate adjustment
    market = setup.get("market", "")
    if market and setup_type:
        learn_bonus = _performance_bonus(market, setup_type)
        s += learn_bonus
        bd["learning_bonus"] = learn_bonus

    # Task 7: TIME_OF_DAY factor
    try:
        now_et = _now_et()
        hm = now_et.hour * 60 + now_et.minute
        market_check = setup.get("market", "")
        is_futures = market_check in ("NQ", "GC")
        if 570 <= hm <= 630:       # 9:30-10:30 AM ET
            s += 5; bd["time_of_day"] = 5
        elif 630 < hm <= 720:      # 10:30 AM-12:00 PM ET
            bd["time_of_day"] = 0
        elif 720 < hm <= 840:      # 12:00-2:00 PM ET (lunch chop)
            s -= 8; bd["time_of_day"] = -8
        elif 840 < hm <= 930:      # 2:00-3:30 PM ET
            s += 5; bd["time_of_day"] = 5
        elif 930 < hm <= 960:      # 3:30-4:00 PM ET (close chop)
            s -= 15; bd["time_of_day"] = -15
        elif is_futures and (hm < 570 or hm > 960):  # Outside RTH for futures
            s -= 10; bd["time_of_day"] = -10
        else:
            bd["time_of_day"] = 0
    except Exception:
        bd["time_of_day"] = 0

    # Task 7: REMOVED regime scoring as a score factor
    # Regime is now only used as a GATE in detect_setups(), not a scoring bonus/penalty
    # This eliminates the duplicate signal with trend_score

    s = max(0, min(100, int(s)))

    if   s >= 80: tier = TIER_HIGH
    elif s >= 65: tier = TIER_MED
    elif s >= 50: tier = TIER_LOW
    else:         tier = "REJECT"

    return s, tier, bd

# ------------------------------------------------------------------ #
# Structure-aware target (DYNAMIC RR — Apr 30 update)
# ------------------------------------------------------------------ #
def structure_target(df: pd.DataFrame, direction: str,
                     entry: float, stop: float, atr_val: float,
                     min_rr: float = 1.5, market: str = "",
                     trend_score_val: int = 0) -> tuple[float, float, str]:
    """
    Finds the BEST real swing level for this trade.
    Apr 30 redesign per Wayne's request: don't hard-restrict to a fixed RR.
    Bot picks the smartest target in the 1.5R – 5.0R band, preferring
    targets in the 2R–3R sweet spot which historically have the best WR.

    Selection logic:
      1. Walk swings near→far. Reject anything > 5.0R (too far to reach).
      2. Prefer the FIRST swing that gives 2.0R–3.0R (sweet spot, 41.9% WR).
      3. If no 2–3R swing exists, take the next-best in the 1.5–5R band.
      4. NQ super-trend exception: if |trend_score| ≥ 7, allow as low as 1.2R.

    Returns (target, rr, method) or (0, 0, '<reason>') if nothing found.
    Reason codes: 'no_target' (no swings), 'rr_too_high' (all >5R),
    'rr_too_low' (all <min_rr).
    """
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0, 0.0, "no_target"

    # NQ strong trend override — lower RR minimum when trend is very strong
    if market == "NQ" and abs(trend_score_val) >= 7:
        min_rr = min(min_rr, 1.2)

    # Apr 30: dynamic upper bound. Old MAX_RR=4.0 was too tight; sweet spot is 2-3R
    # but we should accept up to 5R when no closer level exists.
    MAX_RR = 5.0
    SWEET_LO, SWEET_HI = 2.0, 3.0

    a = atr(df).iloc[-1]
    hi, lo = swing_points(df, 5)
    candidates = []

    if direction in ("LONG", "WATCH_LONG"):
        for i in hi:
            lvl = float(df["High"].iloc[i])
            if lvl - entry > 0.3 * a:  # must be meaningfully above entry
                rr = (lvl - entry) / risk
                candidates.append((lvl, rr))
        candidates.sort(key=lambda x: x[0])  # nearest first
    else:
        for i in lo:
            lvl = float(df["Low"].iloc[i])
            if entry - lvl > 0.3 * a:  # must be meaningfully below entry
                rr = (entry - lvl) / risk
                candidates.append((lvl, rr))
        candidates.sort(key=lambda x: -x[0])  # nearest first (highest low first)

    if not candidates:
        return 0.0, 0.0, "no_target"

    # Filter to viable RR band
    viable = [(lvl, rr) for lvl, rr in candidates if min_rr <= rr <= MAX_RR]
    if not viable:
        # Why did we fail? Be specific so strategy_log can analyze it.
        if all(rr > MAX_RR for _, rr in candidates):
            return 0.0, 0.0, "rr_too_high"
        return 0.0, 0.0, "rr_too_low"

    # Prefer the first target in the 2-3R sweet spot (sorted near→far)
    sweet = [(lvl, rr) for lvl, rr in viable if SWEET_LO <= rr <= SWEET_HI]
    if sweet:
        lvl, rr = sweet[0]
        return float(lvl), float(rr), "swing_level_sweet"

    # No sweet-spot swing — take the nearest viable target
    lvl, rr = viable[0]
    return float(lvl), float(rr), "swing_level"

# ------------------------------------------------------------------ #
# Leverage (BTC/SOL only)
# ------------------------------------------------------------------ #
def suggest_leverage(tier: str, entry: float, stop: float,
                     account_risk_pct: Optional[float] = None) -> tuple[int, float]:
    cap      = LEV_BY_TIER.get(tier, 5)
    risk_pct = abs(entry-stop)/entry*100 if entry else 0
    arp      = account_risk_pct if account_risk_pct is not None else _ACCOUNT_RISK_PCT
    if risk_pct <= 0:
        return cap, 0.0
    raw = arp / risk_pct
    lev = max(1, min(cap, int(round(raw))))
    return lev, round(lev * risk_pct, 2)

# ------------------------------------------------------------------ #
# News windows
# ------------------------------------------------------------------ #
def in_news_window(now_utc: Optional[datetime] = None) -> bool:
    if now_utc:
        et = now_utc.astimezone(ET_ZONE) if ET_ZONE else now_utc - timedelta(hours=4)
    else:
        et = _now_et()
    hm  = et.hour*60 + et.minute
    windows = [(8*60+25, 8*60+45), (9*60+25, 9*60+45),
               (13*60+55, 14*60+15), (15*60+55, 16*60+10)]
    return any(a <= hm <= b for a, b in windows)

# ------------------------------------------------------------------ #
# Auto outcome checker
# Runs after every scan — checks if open trades hit target or stop
# ------------------------------------------------------------------ #
def _log_trade_outcome(trade_row: dict, result: str, exit_price: float):
    """
    Write an outcome row to strategy_log.csv so we have a complete
    detection -> fire -> outcome chain in one file.
    Deferred import to avoid module circularity.
    """
    try:
        import strategy_log as sl
    except Exception:
        return

    try:
        entry = float(trade_row.get("entry", 0))
        exit_p = float(exit_price)
        pts = exit_p - entry if "LONG" in trade_row.get("direction", "") else entry - exit_p
        pts_str = f"+{round(pts,2)}" if pts >= 0 else f"{round(pts,2)}"

        ts_open = trade_row.get("timestamp", "")
        held_hours = ""
        try:
            open_dt = datetime.fromisoformat(ts_open)
            if open_dt.tzinfo is None:
                open_dt = open_dt.replace(tzinfo=timezone.utc)
            held_s = (datetime.now(timezone.utc) - open_dt).total_seconds()
            held_hours = round(held_s / 3600, 1)
        except Exception:
            pass

        decision_const = sl.DECISION_CLOSED_WIN if result == "WIN" else sl.DECISION_CLOSED_LOSS
        reason = (f"Trade closed {result} at {round(exit_p,4)}, "
                  f"{pts_str} pts from entry {round(entry,4)}"
                  f"{', held ' + str(held_hours) + 'h' if held_hours else ''}.")

        sl.log_scan_decision(
            trade_row.get("market", "?"),
            trade_row.get("tf", "?"),
            trade_row.get("setup", "?"),
            trade_row.get("direction", "?"),
            float(exit_p),
            float(entry),
            float(trade_row.get("stop", 0) or 0),
            float(trade_row.get("target", 0) or 0),
            float(trade_row.get("rr", 0) or 0),
            int(float(trade_row.get("conviction", 0) or 0)),
            trade_row.get("tier", "?"),
            int(float(trade_row.get("trend_score", 0) or 0)),
            float(trade_row.get("adx", 0) or 0),
            float(trade_row.get("rsi", 0) or 0),
            float(trade_row.get("vol_ratio", 0) or 0),
            trade_row.get("htf_bias", "?"),
            bool(int(trade_row.get("news_flag", 0) or 0)),
            decision_const,
            "",
            detection_reason=reason,
            result=result,
        )

        # Apr 30 fix: also update the original FIRED row's result column so the
        # 9k+ scan decisions become queryable by win rate. Previously these rows
        # stayed result="" forever, making per-setup WR analysis impossible from
        # strategy_log alone.
        try:
            sl.update_fired_row_result(
                market=trade_row.get("market", "?"),
                setup_type=trade_row.get("setup", "?"),
                direction=trade_row.get("direction", "?"),
                entry=float(entry),
                result=result,
            )
        except Exception as _ufr_e:
            import logging
            logging.getLogger("nqcalls").debug(f"update_fired_row_result: {_ufr_e}")
    except Exception as e:
        import logging
        logging.getLogger("nqcalls").debug(f"_log_trade_outcome error: {e}")


def auto_check_outcomes(live_frames: dict):
    """
    Checks every open trade against candle HIGH/LOW range since alert timestamp.
    Uses H/L not just close — catches intra-bar wicks that close misses.
    This is critical for learning data accuracy.
    """
    import logging
    _log = logging.getLogger("nqcalls")
    open_trades = load_open_trades()
    if not open_trades:
        return []

    closed_now = []
    for row in open_trades:
        market = row.get("market")
        frames = live_frames.get(market, {})
        if not isinstance(frames, dict) or not frames:
            _log.warning(f"auto_check_outcomes: no frames for {market} {row.get('alert_id', '?')}")
            continue
        try:
            entry     = float(row.get("entry",  0))
            stop      = float(row.get("stop",   0))
            target    = float(row.get("target", 0))
            direction = row.get("direction", "LONG")
            alert_id  = row.get("alert_id")
            setup_type= row.get("setup", "")
            ts_str    = row.get("timestamp", "")
            if target == 0 or stop == 0:
                continue

            # Audit Finding #4 / BACKLOG #6 (2026-04-28): multi-TF outcome.
            # Old code only checked 15m frame, which misses intrabar wicks
            # on 1m/5m entries and undermeasures time-to-win for 1h/4h setups.
            # Take the high/low across every available frame since alert.
            try:
                alert_dt = pd.Timestamp(ts_str, tz="UTC")
            except Exception:
                alert_dt = None

            period_high = float("-inf")
            period_low  = float("inf")
            frames_used = []
            for tf_name, tf_df in frames.items():
                if tf_df is None or getattr(tf_df, "empty", True):
                    continue
                try:
                    if alert_dt is not None:
                        recent_tf = tf_df[tf_df.index >= alert_dt]
                        if recent_tf.empty:
                            recent_tf = tf_df.iloc[-5:]
                    else:
                        recent_tf = tf_df.iloc[-5:]
                    period_high = max(period_high, float(recent_tf["High"].max()))
                    period_low  = min(period_low,  float(recent_tf["Low"].min()))
                    frames_used.append(tf_name)
                except Exception as _frame_err:
                    _log.debug(f"auto_check_outcomes frame {tf_name} error: {_frame_err}")
                    continue
            if not frames_used:
                _log.warning(f"auto_check_outcomes: no usable frames for {alert_id} {market}")
                continue

            hit_target = hit_stop = False
            if direction == "LONG":
                if period_high >= target: hit_target = True
                if period_low  <= stop:   hit_stop   = True
            else:
                if period_low  <= target: hit_target = True
                if period_high >= stop:   hit_stop   = True

            # If both hit same candle — stop wins (conservative)
            if hit_target and hit_stop:
                hit_target = False

            if hit_target:
                update_result(alert_id, "WIN", 0, target)
                record_trade_result(market, setup_type, "WIN")
                closed_now.append({"alert_id": alert_id, "result": "WIN",
                                   "market": market, "price": target})
                _log_trade_outcome(row, "WIN", target)
            elif hit_stop:
                update_result(alert_id, "LOSS", 0, stop)
                record_trade_result(market, setup_type, "LOSS")
                closed_now.append({"alert_id": alert_id, "result": "LOSS",
                                   "market": market, "price": stop})
                _log_trade_outcome(row, "LOSS", stop)
        except Exception as e:
            _log.warning(f"auto_check_outcomes {row.get('alert_id')}: {e}")
            continue

    return closed_now

# ------------------------------------------------------------------ #
# Mid-trade re-scoring
# ------------------------------------------------------------------ #
def rescore_open_trade(row: dict, live_frames: dict, news_flag: bool) -> dict:
    try:
        market    = row["market"]
        direction = row["direction"]
        df_entry  = live_frames.get(row.get("tf","15m"))
        if df_entry is None or (hasattr(df_entry,'empty') and df_entry.empty):
            df_entry = live_frames.get("15m")
        # Use market's actual HTF_CONFIRM, not hardcoded 1h
        try:
            from markets import get_market_config
            htf_key = get_market_config(market).HTF_CONFIRM
        except Exception:
            htf_key = "1h"
        df_htf = live_frames.get(htf_key) or live_frames.get("1h")
        if df_entry is None:
            return {"action":"HOLD","new_conviction":None,"delta":0,"note":"no data"}

        tscore, _ = trend_score(live_frames, market)
        adx_v     = float(adx(df_entry).iloc[-1])
        rsi_v     = float(rsi(df_entry["Close"]).iloc[-1])
        vol_mean  = df_entry["Volume"].rolling(20).mean().iloc[-1]
        vol_ratio = float(df_entry["Volume"].iloc[-1] / max(1e-9, vol_mean))

        pseudo_setup = {
            "type":      row.get("setup", "LIQ_SWEEP_BULL"),
            "direction": direction,
            "market":    market,
        }
        new_conv, new_tier, _ = conviction_score(
            pseudo_setup, tscore, df_entry, df_htf,
            news_flag, adx_v, rsi_v, vol_ratio, 1.5
        )
        old_conv  = int(float(row.get("last_rescore_conviction") or row.get("conviction") or 0))
        delta     = new_conv - old_conv

        aligned = (direction=="LONG" and tscore>0) or (direction=="SHORT" and tscore<0)
        if not aligned and abs(tscore) >= 3:
            return {"action":"EXIT_SUGGEST","new_conviction":new_conv,"delta":delta,
                    "note":f"Trend flipped against you (score {tscore}). Consider exiting."}
        if delta <= -20:
            return {"action":"WARN","new_conviction":new_conv,"delta":delta,
                    "note":f"Conviction dropped {abs(delta)} pts → {new_conv}. Tighten stop."}
        if delta >= 10:
            return {"action":"LET_RUN","new_conviction":new_conv,"delta":delta,
                    "note":f"Conviction strengthened +{delta} → {new_conv}. Let it run."}
        return {"action":"HOLD","new_conviction":new_conv,"delta":delta,"note":""}

    except Exception as e:
        return {"action":"HOLD","new_conviction":None,"delta":0,"note":f"rescore err: {e}"}

# ------------------------------------------------------------------ #
# Auto Strategy Review (every 10 closed trades)
# ------------------------------------------------------------------ #
_AUTO_REVIEW_FILE = os.path.join(_BASE_DIR, "data", "last_review_count.json")

def check_auto_review() -> Optional[str]:
    """
    Returns a strategy review message every 10 closed trades, or None.
    Tracks last reviewed count so it only fires once per threshold.
    """
    try:
        rows = []
        if os.path.exists(OUTCOMES_CSV):
            with open(OUTCOMES_CSV, newline="") as f:
                rows = list(csv.DictReader(f))
        closed = [r for r in rows if r.get("status") == "CLOSED" and r.get("result") in ("WIN","LOSS")]
        total_closed = len(closed)

        # Load last reviewed count
        last_count = 0
        if os.path.exists(_AUTO_REVIEW_FILE):
            with open(_AUTO_REVIEW_FILE) as f:
                last_count = json.load(f).get("count", 0)

        # Fire every 10 trades
        if total_closed < 10 or total_closed // 10 == last_count // 10:
            return None

        # Save new count
        with open(_AUTO_REVIEW_FILE, "w") as f:
            json.dump({"count": total_closed}, f)

        # Build the review
        perf = _load_performance()
        lines = [
            "🔬 *Auto Strategy Review*  ({} trades)".format(total_closed),
            "━━━━━━━━━━━━━━━━━━",
        ]

        # Analyze each setup
        flagged = []
        strong  = []
        for key, data in sorted(perf.items()):
            wins   = data.get("wins", 0)
            losses = data.get("losses", 0)
            total  = wins + losses
            if total < 3:
                continue
            wr = round(wins / total * 100, 1)
            market, setup = key.split(":", 1) if ":" in key else ("?", key)
            icon = "✅" if wr >= 55 else "⚠️" if wr >= 40 else "❌"
            lines.append(f"{icon} {market} {setup}: {wr}% WR ({wins}W/{losses}L)")
            if wr < 40:
                flagged.append((market, setup, wr, total))
            elif wr >= 60 and total >= 5:
                strong.append((market, setup, wr, total))

        if not perf:
            lines.append("Not enough data yet.")
            return "\n".join(lines)

        # Suggestions
        lines.append("━━━━━━━━━━━━━━━━━━")
        if flagged:
            lines.append("⚠️ *Underperforming (below 40% WR):*")
            for market, setup, wr, total in flagged:
                lines.append(f"  - {market} {setup}: {wr}% over {total} trades")
                lines.append(f"    Consider raising min conviction or ADX for this setup")
        if strong:
            lines.append("🔥 *Top performers (60%+ WR):*")
            for market, setup, wr, total in strong:
                lines.append(f"  - {market} {setup}: {wr}% over {total} trades")

        if not flagged and not strong:
            lines.append("All setups performing within normal range.")

        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("🧠 Review triggered automatically every 10 trades.")
        return "\n".join(lines)
    except Exception as e:
        return None

# ------------------------------------------------------------------ #
# CSV logger
# ------------------------------------------------------------------ #
def _ensure_csv():
    if not os.path.exists(OUTCOMES_CSV):
        with open(OUTCOMES_CSV, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_COLS).writeheader()

def log_alert(row: dict) -> str:
    _ensure_csv()
    row = dict(row)
    row.setdefault("alert_id",   uuid.uuid4().hex[:10])
    row.setdefault("timestamp",  datetime.now(timezone.utc).isoformat())
    row.setdefault("status",     "OPEN")
    row.setdefault("result",     "")
    row.setdefault("bars_to_resolution", "")
    row.setdefault("exit_price", "")
    row.setdefault("last_rescore_conviction", row.get("conviction",""))
    # Session ID — always compute fresh, never rely on caller passing it in
    try:
        from session_clock import get_session_date
        row["session_id"] = get_session_date()
    except Exception:
        row["session_id"] = datetime.now().strftime("%Y-%m-%d")
    clean = {k: row.get(k,"") for k in CSV_COLS}
    # Locked atomic append — prevents _write_all from clobbering this row
    safe_io.safe_append_csv(OUTCOMES_CSV, CSV_COLS, clean)
    return clean["alert_id"]

def _read_all() -> list[dict]:
    _ensure_csv()
    # Use safe_read_csv so we don't catch a partial state mid-rewrite
    rows = safe_io.safe_read_csv(OUTCOMES_CSV)
    # Backward compatibility: fill missing session_id from timestamp
    try:
        from session_clock import session_date_from_timestamp
        for r in rows:
            if not r.get("session_id"):
                ts = r.get("timestamp", "")
                if ts:
                    r["session_id"] = session_date_from_timestamp(ts)
                else:
                    r["session_id"] = ""
    except Exception:
        pass
    return rows

def _write_all(rows: list[dict]):
    """Atomic full rewrite. Used by update_result/update_rescore/etc.
    Note: callers that read-then-write should use _safe_mutate_csv instead
    so the read happens INSIDE the lock and concurrent appenders aren't
    clobbered. _write_all is left here for backwards compatibility but
    its read-then-write callers (update_result etc.) have been switched
    to the safer pattern."""
    safe_io.safe_rewrite_csv(OUTCOMES_CSV, CSV_COLS, lambda _: list(rows))

def _safe_mutate_csv(mutator):
    """Locked read-modify-rewrite of outcomes.csv. The mutator gets the
    fresh row list (read inside the lock) and returns the new list.
    This is the ONLY safe way to do conditional updates without losing
    rows that were appended between read and write."""
    return safe_io.safe_rewrite_csv(OUTCOMES_CSV, CSV_COLS, mutator)

def update_result(alert_id: str, result: str, bars: int, exit_price: float):
    def _mut(rows):
        for r in rows:
            if r.get("alert_id") == alert_id:
                r["status"]             = "CLOSED"
                r["result"]             = result
                r["bars_to_resolution"] = bars
                r["exit_price"]         = exit_price
        return rows
    _safe_mutate_csv(_mut)


def auto_expire_stale_trades(max_hours: int = 24) -> list[tuple]:
    """
    Task 2: Auto-close OPEN trades older than max_hours.
    Sets status=CLOSED, result=SKIP, exit_price=entry (zero P&L).
    Keeps exact same CSV schema — no new columns added.
    Returns list of (alert_id, market, setup, hours_old) tuples for logging.

    DATA-LOSS FIX: now uses _safe_mutate_csv so we don't lose log_alert()
    appends that happen during the function call.
    """
    import logging as _logging
    _log = _logging.getLogger("nqcalls")
    now_utc = datetime.now(timezone.utc)
    cutoff_seconds = max_hours * 3600
    expired: list[tuple] = []

    def _mut(rows):
        for r in rows:
            if r.get("status") != "OPEN":
                continue
            ts_str = r.get("timestamp", "")
            if not ts_str:
                continue
            alert_id = r.get("alert_id", "?")
            market   = r.get("market", "?")
            setup    = r.get("setup", "?")
            entry    = r.get("entry", "")
            try:
                alert_dt = datetime.fromisoformat(ts_str)
                if alert_dt.tzinfo is None:
                    alert_dt = alert_dt.replace(tzinfo=timezone.utc)
                age_seconds = (now_utc - alert_dt).total_seconds()
            except Exception as _ts_err:
                # Audit Finding #5 (2026-04-28): silent skip on bad timestamps
                # left 11 trades OPEN 10+ days. Loud-fail and force-close
                # so the row exits the OPEN set instead of haunting the CSV.
                _log.warning(
                    f"Stale-trade expiry: bad timestamp '{ts_str}' on "
                    f"{alert_id} {market} {setup} ({_ts_err}) — force-closing."
                )
                r["status"]             = "CLOSED"
                r["result"]             = "SKIP"
                r["exit_price"]         = entry
                r["bars_to_resolution"] = ""
                expired.append((alert_id, market, setup, -1.0))
                continue
            if age_seconds < cutoff_seconds:
                continue

            hours    = round(age_seconds / 3600, 1)

            r["status"]             = "CLOSED"
            r["result"]             = "SKIP"
            r["exit_price"]         = entry
            r["bars_to_resolution"] = ""
            expired.append((alert_id, market, setup, hours))
            _log.info(f"Auto-expired stale OPEN trade: {alert_id} {market} {setup} (opened {hours}h ago)")
        return rows

    _safe_mutate_csv(_mut)
    return expired

def update_rescore(alert_id: str, new_conviction: int):
    def _mut(rows):
        for r in rows:
            if r.get("alert_id") == alert_id:
                r["last_rescore_conviction"] = new_conviction
        return rows
    _safe_mutate_csv(_mut)

def update_partial_exit(alert_id: str):
    def _mut(rows):
        for r in rows:
            if r.get("alert_id") == alert_id:
                r["partial_exit_done"] = "True"
        return rows
    _safe_mutate_csv(_mut)

def load_open_trades() -> list[dict]:
    return [r for r in _read_all() if r.get("status") == "OPEN"]


# ------------------------------------------------------------------ #
# Session-based trade queries and archiving
# ------------------------------------------------------------------ #
def get_session_trades(session_id: str = None) -> list[dict]:
    """Return only trades from the specified session (defaults to current session)."""
    if session_id is None:
        try:
            from session_clock import get_session_date
            session_id = get_session_date()
        except Exception:
            session_id = datetime.now().strftime("%Y-%m-%d")
    rows = _read_all()
    return [r for r in rows if r.get("session_id") == session_id]


def archive_session(session_id: str) -> str:
    """
    Archive trades from the specified session:
      - Copy matching rows to data/archive/outcomes_YYYY-MM-DD.csv
      - Keep only open trades and last 7 days of closed trades in live file
    Returns the archive file path.
    """
    archive_dir = os.path.join(_BASE_DIR, "data", "archive")
    os.makedirs(archive_dir, exist_ok=True)
    archive_path = os.path.join(archive_dir, f"outcomes_{session_id}.csv")

    rows = _read_all()
    session_rows = [r for r in rows if r.get("session_id") == session_id]

    # Write archive file
    if session_rows:
        with open(archive_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLS)
            w.writeheader()
            for r in session_rows:
                w.writerow({k: r.get(k, "") for k in CSV_COLS})

    # Rebuild live file: keep open trades + last 7 days of closed trades
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    keep = []
    for r in rows:
        if r.get("status") == "OPEN":
            keep.append(r)
        elif r.get("session_id", "") >= cutoff:
            keep.append(r)
        # Older closed trades are dropped from live file (already archived)

    _write_all(keep)
    return archive_path


def archive_old_sessions() -> list[str]:
    """
    Called once at startup.  For each unique session_id in outcomes.csv
    that is NOT the current session, copy those rows to
    data/archive/outcomes_YYYY-MM-DD.csv.  Then trim the live file to
    only: open trades + current session + last 7 days.
    Returns list of archive files created.
    """
    try:
        from session_clock import get_session_date
        current = get_session_date()
    except Exception:
        current = datetime.now().strftime("%Y-%m-%d")

    archive_dir = os.path.join(_BASE_DIR, "data", "archive")
    os.makedirs(archive_dir, exist_ok=True)

    rows = _read_all()
    if not rows:
        return []

    # Group by session_id
    by_session: dict[str, list] = {}
    for r in rows:
        sid = r.get("session_id", "")
        if sid:
            by_session.setdefault(sid, []).append(r)

    created = []
    for sid, session_rows in by_session.items():
        if sid == current:
            continue  # don't archive today's session
        archive_path = os.path.join(archive_dir, f"outcomes_{sid}.csv")
        if os.path.exists(archive_path):
            continue  # already archived
        with open(archive_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLS)
            w.writeheader()
            for r in session_rows:
                w.writerow({k: r.get(k, "") for k in CSV_COLS})
        created.append(archive_path)

    # Trim live file: open + current session + last 7 days
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    keep = []
    for r in rows:
        if r.get("status") == "OPEN":
            keep.append(r)
        elif r.get("session_id") == current:
            keep.append(r)
        elif r.get("session_id", "") >= cutoff:
            keep.append(r)

    _write_all(keep)
    return created


def build_session_summary(session_id: str = None) -> dict:
    """
    Build a summary dict for a session:
      total_trades, wins, losses, win_rate, total_pnl_r,
      setups_fired, markets_traded, open_count, best_setup, worst_setup
    """
    trades = get_session_trades(session_id)
    closed = [r for r in trades if r.get("status") == "CLOSED" and r.get("result") in ("WIN", "LOSS")]
    open_trades = [r for r in trades if r.get("status") == "OPEN"]

    wins = sum(1 for r in closed if r["result"] == "WIN")
    losses = sum(1 for r in closed if r["result"] == "LOSS")
    total = wins + losses
    win_rate = round(wins / max(1, total) * 100, 1)

    # Compute total P&L in R-multiples
    total_pnl_r = 0.0
    setup_pnl: dict[str, float] = {}
    for r in closed:
        try:
            rr_val = float(r.get("rr", 0))
        except (ValueError, TypeError):
            rr_val = 0.0
        r_result = rr_val if r["result"] == "WIN" else -1.0
        total_pnl_r += r_result
        setup_key = f"{r.get('market')}:{r.get('setup')}"
        setup_pnl[setup_key] = setup_pnl.get(setup_key, 0.0) + r_result

    best_setup = max(setup_pnl, key=setup_pnl.get) if setup_pnl else "N/A"
    worst_setup = min(setup_pnl, key=setup_pnl.get) if setup_pnl else "N/A"
    markets_traded = list(set(r.get("market", "?") for r in trades))
    setups_fired = list(set(r.get("setup", "?") for r in trades))

    return {
        "session_id": session_id,
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl_r": round(total_pnl_r, 2),
        "best_setup": best_setup,
        "worst_setup": worst_setup,
        "markets_traded": markets_traded,
        "setups_fired": setups_fired,
        "open_count": len(open_trades),
    }


def load_archived_session(session_id: str) -> list[dict]:
    """Load trades from an archived session file."""
    archive_path = os.path.join(_BASE_DIR, "data", "archive", f"outcomes_{session_id}.csv")
    if not os.path.exists(archive_path):
        return []
    try:
        with open(archive_path, newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def list_archived_sessions() -> list[str]:
    """Return list of available archived session dates."""
    archive_dir = os.path.join(_BASE_DIR, "data", "archive")
    if not os.path.exists(archive_dir):
        return []
    dates = []
    for fname in sorted(os.listdir(archive_dir)):
        if fname.startswith("outcomes_") and fname.endswith(".csv"):
            date_str = fname.replace("outcomes_", "").replace(".csv", "")
            dates.append(date_str)
    return dates

# ------------------------------------------------------------------ #
# /stats
# ------------------------------------------------------------------ #
# ------------------------------------------------------------------ #
# Daily Report
# ------------------------------------------------------------------ #
DAILY_REPORT_FILE = os.path.join(_BASE_DIR, "data", "daily_report.txt")

def build_daily_report() -> tuple[str, str]:
    """
    Builds a full daily report of everything that happened today.
    Returns (full_text_for_file, short_summary_for_telegram).
    Called automatically at 8pm EST every day.
    """
    today     = datetime.now().strftime("%Y-%m-%d")
    today_dt  = datetime.now().strftime("%A, %B %d, %Y")
    rows      = _read_all()
    perf      = _load_performance()

    # Filter to today's alerts
    today_rows = []
    for r in rows:
        ts = r.get("timestamp", "")
        if today in ts:
            today_rows.append(r)

    # Separate by status
    today_closed = [r for r in today_rows if r.get("status") == "CLOSED"]
    today_open   = [r for r in today_rows if r.get("status") == "OPEN"]
    today_wins   = [r for r in today_closed if r.get("result") == "WIN"]
    today_losses = [r for r in today_closed if r.get("result") == "LOSS"]
    today_skips  = [r for r in today_closed if r.get("result") == "SKIP"]

    total_closed = len(today_closed)
    wins   = len(today_wins)
    losses = len(today_losses)
    wr     = round((wins / max(1, wins + losses)) * 100, 1)

    # All-time stats
    all_closed = [r for r in rows if r.get("status") == "CLOSED"]
    all_wins   = sum(1 for r in all_closed if r.get("result") == "WIN")
    all_losses = sum(1 for r in all_closed if r.get("result") == "LOSS")
    all_wr     = round((all_wins / max(1, all_wins + all_losses)) * 100, 1)

    # Best and worst setups today
    setup_results = {}
    for r in today_closed:
        key = f"{r.get('market')}:{r.get('setup')}"
        setup_results.setdefault(key, {"wins": 0, "losses": 0})
        if r.get("result") == "WIN":  setup_results[key]["wins"]   += 1
        if r.get("result") == "LOSS": setup_results[key]["losses"] += 1

    # Learning adjustments that happened today
    learning_updates = []
    for key, data in perf.items():
        last_updated = data.get("last_updated", "")
        if today in last_updated:
            wr_val = data.get("win_rate", 0)
            bonus  = _performance_bonus(*key.split(":"))
            learning_updates.append((key, wr_val, bonus, data.get("total", 0)))

    # ── Build full text file ──────────────────────────────────────
    lines = [
        f"NQ CALLS DAILY REPORT",
        f"Date: {today_dt}",
        f"Generated: {datetime.now().strftime('%H:%M EST')}",
        f"{'='*50}",
        f"",
        f"TODAY'S PERFORMANCE",
        f"{'-'*30}",
        f"Alerts fired today:  {len(today_rows)}",
        f"Closed today:        {total_closed}",
        f"Wins:                {wins}",
        f"Losses:              {losses}",
        f"Skipped:             {len(today_skips)}",
        f"Still open:          {len(today_open)}",
        f"Today win rate:      {wr}%",
        f"",
    ]

    if today_rows:
        lines.append("TODAY'S ALERTS:")
        lines.append("-" * 30)
        for r in today_rows:
            result  = r.get("result") or r.get("status", "OPEN")
            result_icon = {"WIN": "✅", "LOSS": "❌", "SKIP": "⏭", "OPEN": "🔄"}.get(result, "❓")
            ts = r.get("timestamp", "")[:16].replace("T", " ")
            lines.append(
                f"  {result_icon} {r.get('market')} | {r.get('setup')} | {r.get('direction')} | "
                f"Conv:{r.get('conviction')} | RR:{r.get('rr')} | Tier:{r.get('tier')} | "
                f"Entry:{r.get('entry')} Stop:{r.get('stop')} Target:{r.get('target')} | "
                f"Exit:{r.get('exit_price') or 'open'} | {ts}"
            )
        lines.append("")

    if setup_results:
        lines.append("SETUP BREAKDOWN TODAY:")
        lines.append("-" * 30)
        for key, res in sorted(setup_results.items()):
            w = res["wins"]; l = res["losses"]
            swr = round(w / max(1, w+l) * 100)
            lines.append(f"  {key}: {w}W / {l}L ({swr}% WR)")
        lines.append("")

    lines += [
        "ALL-TIME STATS",
        "-" * 30,
        f"Total closed: {len(all_closed)}",
        f"All-time W/L: {all_wins}W / {all_losses}L ({all_wr}% WR)",
        "",
    ]

    if learning_updates:
        lines.append("LEARNING UPDATES TODAY:")
        lines.append("-" * 30)
        for key, wr_val, bonus, total in sorted(learning_updates):
            bon_str = f"+{bonus}" if bonus > 0 else str(bonus)
            lines.append(f"  {key}: {wr_val}% WR over {total} trades → Conv adj: {bon_str}")
        lines.append("")

    lines += [
        "ALL-TIME LEARNING FILE:",
        "-" * 30,
    ]
    for key, data in sorted(perf.items(), key=lambda x: x[1].get("total", 0), reverse=True):
        w   = data.get("wins", 0)
        l   = data.get("losses", 0)
        wr_val = data.get("win_rate", 0)
        bonus  = _performance_bonus(*key.split(":"))
        bon_str = f"+{bonus}" if bonus > 0 else str(bonus)
        lines.append(f"  {key}: {w}W/{l}L ({wr_val}% WR) → Conv adj: {bon_str}")

    lines += [
        "",
        "STILL OPEN AT END OF DAY:",
        "-" * 30,
    ]
    still_open = load_open_trades()
    if still_open:
        for r in still_open:
            ts = r.get("timestamp", "")[:16].replace("T", " ")
            lines.append(
                f"  🔄 {r.get('market')} | {r.get('setup')} | "
                f"Entry:{r.get('entry')} Target:{r.get('target')} | "
                f"ID:{r.get('alert_id')} | {ts}"
            )
    else:
        lines.append("  No open trades.")

    lines += [
        "",
        "=" * 50,
        "END OF REPORT",
        "Paste this into Claude to review and update bot settings.",
    ]

    full_text = "\n".join(lines)

    # Save to file
    try:
        report_path = os.path.join(_BASE_DIR, "data", f"daily_report_{today}.txt")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(full_text)
        # Also save as latest
        with open(DAILY_REPORT_FILE, "w", encoding="utf-8") as f:
            f.write(full_text)
    except Exception as e:
        pass

    # ── Short Telegram summary ────────────────────────────────────
    icon = "🟢" if wr >= 60 else "🔴" if wr < 45 else "🟡"
    short = (
        f"📋 *NQ CALLS — Daily Report*\n"
        f"📅 {today_dt}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"*Today:* {len(today_rows)} alerts | "
        f"{wins}W / {losses}L | {icon} {wr}% WR\n"
        f"*All-time:* {all_wins}W / {all_losses}L | {all_wr}% WR\n"
    )
    if today_wins:
        short += f"\n✅ *Wins today:*\n"
        for r in today_wins:
            short += f"  {r.get('market')} {r.get('setup')} [{r.get('tf')}]\n"
    if today_losses:
        short += f"\n❌ *Losses today:*\n"
        for r in today_losses:
            short += f"  {r.get('market')} {r.get('setup')} [{r.get('tf')}]\n"
    if len(today_open) > 0:
        short += f"\n🔄 *Still open:* {len(today_open)} trade(s)\n"
    if learning_updates:
        short += f"\n🧠 *Learning updated:* {len(learning_updates)} setup(s) adjusted\n"
    short += (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"_Full report saved. Paste to Claude to review._"
    )

    return full_text, short


def print_stats(session_only: bool = True) -> str:
    """
    Print stats. Defaults to current session only.
    Set session_only=False for all-time stats.
    """
    rows = _read_all()
    if not rows:
        return "No alerts logged yet."

    # Get current session ID for filtering
    try:
        from session_clock import get_session_date
        sid = get_session_date()
    except Exception:
        sid = datetime.now().strftime("%Y-%m-%d")

    if session_only:
        display_rows = [r for r in rows if r.get("session_id") == sid]
        header = f"📊 *NQ CALLS Stats — Session {sid}*"
    else:
        display_rows = rows
        header = "📊 *NQ CALLS Stats — All Time*"

    if not display_rows:
        return f"{header}\nNo trades this session yet."

    closed = [r for r in display_rows if r.get("status") == "CLOSED"]
    open_  = [r for r in display_rows if r.get("status") == "OPEN"]
    wins   = sum(1 for r in closed if r.get("result") == "WIN")
    losses = sum(1 for r in closed if r.get("result") == "LOSS")
    be     = len(closed) - wins - losses
    wr     = (wins / max(1, wins+losses)) * 100

    def tier_wr(t):
        sub = [r for r in closed if r.get("tier") == t]
        w   = sum(1 for r in sub if r.get("result") == "WIN")
        l   = sum(1 for r in sub if r.get("result") == "LOSS")
        return f"{t}: {w}W/{l}L ({(w/max(1,w+l))*100:.0f}%)" if sub else f"{t}: —"

    by_mkt = {}
    for r in closed:
        m = r.get("market","?")
        by_mkt.setdefault(m, [0,0])
        if r.get("result") == "WIN":  by_mkt[m][0] += 1
        if r.get("result") == "LOSS": by_mkt[m][1] += 1
    mkt_lines = [f"  {m}: {w}W/{l}L ({(w/max(1,w+l))*100:.0f}%)"
                 for m,(w,l) in by_mkt.items()]

    return (
        f"{header}\n"
        f"Total: {len(display_rows)} | Open: {len(open_)} | Closed: {len(closed)}\n"
        f"Overall: {wins}W / {losses}L / {be}BE — WR {wr:.1f}%\n\n"
        f"By tier:\n  {tier_wr(TIER_HIGH)}\n  {tier_wr(TIER_MED)}\n  {tier_wr(TIER_LOW)}\n\n"
        f"By market:\n" + ("\n".join(mkt_lines) if mkt_lines else "  —") +
        f"\n\n{get_learning_summary()}"
    )
