"""
markets/market_NQ.py - NQ Futures (Nasdaq 100)
================================================
NQ specific logic, settings, and scanning rules.

What makes NQ unique:
- Scans 24/7 — good setups happen in Asia and London too
- Most sensitive to news (CPI, Fed, NFP, earnings)
- 15m and 1h are the best entry timeframes
- Tighter stops than crypto — NQ respects levels well
- Higher ADX requirement — NQ trends strongly when it trends
- Pre-market levels (overnight highs/lows) matter a lot
- Liquidity sweeps are extremely reliable on NQ
- EMA50 reclaims work well in trending conditions
"""

# ─── Market Identity ───────────────────────────────────────────────
NAME        = "NQ"
FULL_NAME   = "NQ Futures (Nasdaq 100)"
DATA_SOURCE = "yfinance"
YF_TICKER   = "NQ=F"
EMOJI       = "📊"

# ─── Scanning Rules ────────────────────────────────────────────────
SCAN_24_7        = True    # Scan all hours including Asia/London
ENTRY_TIMEFRAMES = ["15m", "1h"]
HTF_CONFIRM      = "1h"    # Use 1h to confirm 15m setups
HTF_SWING        = "4h"    # Use 4h to confirm 1h setups
TIMEFRAMES       = ["15m", "1h", "4h", "1d"]

# ─── Quality Filters ───────────────────────────────────────────────
MIN_ADX          = 22      # NQ needs real trend — higher than crypto
MIN_RR           = 2.5     # Minimum risk/reward
NEWS_MIN_RR      = 3.0     # Lowered from 4.0 — 20 missed winners at RR 2.5 during news
MIN_CONVICTION   = 65      # MEDIUM+ only
COOLDOWN_MIN     = 60      # 1 hour between alerts

# ─── Per-setup ADX minimums ────────────────────────────────────────
# Overrides MIN_ADX on a per-setup basis — data-driven from strategy_log
ADX_MIN_BY_SETUP = {
    "LIQ_SWEEP_BULL":   14,   # sweeps fire in low-ADX consolidation
    "LIQ_SWEEP_BEAR":   14,
    "EMA50_RECLAIM":    22,   # trend setups need real trend
    "EMA50_BREAKDOWN":  22,
    "VWAP_BOUNCE_BULL": 12,   # Lowered from 16 — VWAP bounces 100% WR when fired, 20 missed wins
    "VWAP_REJECT_BEAR": 20,
    "APPROACH_SUPPORT": 10,   # anticipatory — any strength ok
    "APPROACH_RESIST":  10,
    "RSI_DIV_BULL":     14,   # divergence works in moderate trend
    "RSI_DIV_BEAR":     14,
    "EMA21_PULLBACK_BULL": 20,   # needs real trend for pullback entry
    "EMA21_PULLBACK_BEAR": 20,
    "BREAK_RETEST_BULL":   16,   # break-retest works in moderate momentum
    "BREAK_RETEST_BEAR":   16,
}

# ─── Stop Loss Rules ───────────────────────────────────────────────
# NQ respects levels — stops can be tighter
STOP_ATR_MULT_SWEEP   = 0.3   # Tight stop for liquidity sweeps
STOP_ATR_MULT_EMA     = 0.5   # Slightly wider for EMA setups
STOP_ATR_MULT_VWAP    = 0.2   # Tight for VWAP bounces

# ─── Best Setups for NQ ────────────────────────────────────────────
# Ranked by historical reliability on NQ
SETUP_PRIORITY = [
    "LIQ_SWEEP_BULL",      # #1 — most reliable on NQ
    "LIQ_SWEEP_BEAR",      # #1 — most reliable on NQ
    "EMA50_RECLAIM",       # #2 — strong in trending markets
    "EMA50_BREAKDOWN",     # #2 — strong in trending markets
    "VWAP_BOUNCE_BULL",    # #3 — good for intraday, 100% WR
    "VWAP_REJECT_BEAR",    # #3 — good for intraday
    "EMA21_PULLBACK_BULL", # #4 — trend continuation
    "EMA21_PULLBACK_BEAR", # #4 — trend continuation
    "BREAK_RETEST_BULL",   # #5 — institutional pattern
    "BREAK_RETEST_BEAR",   # #5 — institutional pattern
    "APPROACH_SUPPORT",    # anticipatory
    "APPROACH_RESIST",     # anticipatory
]

# ─── NQ Specific Conviction Bonuses ────────────────────────────────
# Extra points added to conviction score for NQ-specific conditions
def extra_conviction_factors(df_entry, df_htf, setup, trend, adx_val, rsi_val) -> dict:
    """
    Returns a dict of {reason: points} for NQ-specific conviction adjustments.
    Called by outcome_tracker on top of base conviction score.
    """
    bonuses = {}

    # NQ bonus: strong ADX (trending hard) = more reliable setups
    if adx_val >= 30:
        bonuses["strong_trend_adx"] = 8
    elif adx_val >= 25:
        bonuses["decent_trend_adx"] = 4

    # NQ bonus: trend score alignment is very important for NQ
    direction = setup.get("direction", "")
    if "LONG" in direction and trend >= 5:
        bonuses["strong_bull_trend"] = 10
    elif "SHORT" in direction and trend <= -5:
        bonuses["strong_bear_trend"] = 10

    # NQ penalty: trading against strong trend = dangerous
    if "LONG" in direction and trend <= -4:
        bonuses["counter_trend_penalty"] = -15
    elif "SHORT" in direction and trend >= 4:
        bonuses["counter_trend_penalty"] = -15

    # NQ bonus: RSI in ideal zone for entries
    if "LONG" in direction and 42 <= rsi_val <= 58:
        bonuses["ideal_rsi_long"] = 6
    elif "SHORT" in direction and 42 <= rsi_val <= 58:
        bonuses["ideal_rsi_short"] = 6

    return bonuses


# ─── NQ Session Context ────────────────────────────────────────────
def get_session_context() -> dict:
    """
    Returns what session NQ is currently in.
    All sessions are valid — just context for the alert message.
    """
    from datetime import datetime, timezone, timedelta
    now_et = datetime.now(timezone.utc) - timedelta(hours=4)
    hour   = now_et.hour

    if 9 <= hour < 16:
        return {"session": "US Regular",    "emoji": "🇺🇸", "note": "Main session — full volume"}
    elif 4 <= hour < 9:
        return {"session": "Pre-Market",    "emoji": "🌄", "note": "Lower volume — watch for fakeouts"}
    elif 16 <= hour < 20:
        return {"session": "After-Hours",   "emoji": "🌆", "note": "Lower volume — wider spreads"}
    elif 20 <= hour or hour < 4:
        return {"session": "Asia/Overnight","emoji": "🌙", "note": "Thin liquidity — valid setups still fire"}
    return {"session": "Unknown", "emoji": "⏰", "note": ""}


# ─── NQ Alert Footer ───────────────────────────────────────────────
def alert_footer(setup, session_ctx) -> str:
    """Extra context added to NQ alerts."""
    lines = []
    session = session_ctx.get("session", "")
    note    = session_ctx.get("note", "")
    emoji   = session_ctx.get("emoji", "")

    if session and note:
        lines.append(f"{emoji} Session: {session} — {note}")

    # NQ specific tips based on setup type
    setup_type = setup.get("type", "")
    if "LIQ_SWEEP" in setup_type:
        lines.append("💡 NQ tip: Wait for full candle close before entering sweep setups.")
    elif "EMA50" in setup_type:
        lines.append("💡 NQ tip: EMA50 reclaims work best when ADX > 22 and trend is aligned.")
    elif "VWAP" in setup_type:
        lines.append("💡 NQ tip: VWAP setups most reliable during US regular session.")

    return "\n".join(lines)
