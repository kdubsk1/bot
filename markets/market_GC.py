"""
markets/market_GC.py - Gold Futures
=====================================
Gold specific logic, settings, and scanning rules.

What makes Gold unique:
- Slower moving than NQ and crypto — needs wider stops
- 1h and 4h are the best timeframes — 15m is noisy on Gold
- Very sensitive to CPI, Fed, inflation data, geopolitical events
- Asian session matters — London open often sets the daily direction
- Tends to trend strongly once it breaks a level
- Safe haven asset — rallies on fear, falls on dollar strength
- Less fake-outs than NQ but moves are slower to develop
- Liquidity sweeps around round numbers (2000, 2100, 2300 etc)
"""

# ─── Market Identity ───────────────────────────────────────────────
NAME        = "GC"
FULL_NAME   = "Gold Futures"
DATA_SOURCE = "yfinance"
YF_TICKER   = "GC=F"
EMOJI       = "🥇"

# ─── Scanning Rules ────────────────────────────────────────────────
SCAN_24_7        = True    # London open is very important for Gold
ENTRY_TIMEFRAMES = ["1h", "4h"]   # Gold works better on higher TFs
HTF_CONFIRM      = "4h"    # Use 4h to confirm 1h setups
HTF_SWING        = "1d"    # Use daily to confirm 4h setups
TIMEFRAMES       = ["1h", "4h", "1d"]

# ─── Quality Filters ───────────────────────────────────────────────
MIN_ADX          = 14      # Lowered — Gold at 15+ ADX overnight is a real trend
MIN_ADX_PRIME    = 18      # London/NY sessions require stronger trend
MIN_RR           = 1.5     # Wave 25 (May 11, 2026): lowered 2.5->1.5 based on backtest_pro showing 30+ winners killed at RR 1.5-1.79
NEWS_MIN_RR      = 3.5     # Lowered from 4.5 — was blocking too many winners
MIN_CONVICTION   = 62      # Slightly lower threshold — Gold is cleaner
COOLDOWN_MIN     = 90      # 90 min between alerts — Gold is slower

# ─── Per-setup ADX minimums ────────────────────────────────────────
# KEY INSIGHT from strategy_log: GC VWAP_BOUNCE_BULL hits at ADX 12-14.
# Lowered specifically for that setup — other trend setups keep higher bar.
ADX_MIN_BY_SETUP = {
    "LIQ_SWEEP_BULL":   12,   # Gold sweeps happen in low-ADX overnight
    "LIQ_SWEEP_BEAR":   12,
    "EMA50_RECLAIM":    18,   # trend setups need real trend
    "EMA50_BREAKDOWN":  18,
    "VWAP_BOUNCE_BULL": 8,    # ★ LOWERED AGAIN: 39 missed wins at ADX 12-13, 0 missed losses
    "VWAP_REJECT_BEAR": 16,   # keep slightly higher for shorts
    "APPROACH_SUPPORT":  8,   # anticipatory — any strength ok
    "APPROACH_RESIST":   8,
    "RSI_DIV_BULL":     12,   # Gold divergence works well at low ADX
    "RSI_DIV_BEAR":     12,
    "EMA21_PULLBACK_BULL": 16,   # Gold trends cleanly — pullbacks work well
    "EMA21_PULLBACK_BEAR": 16,
    "BREAK_RETEST_BULL":   14,   # Gold respects broken levels strongly
    "BREAK_RETEST_BEAR":   14,
}

# ─── Stop Loss Rules ───────────────────────────────────────────────
# Gold needs wider stops — it wicks more before moving
STOP_ATR_MULT_SWEEP   = 0.5   # Wider stops for Gold sweeps
STOP_ATR_MULT_EMA     = 0.7   # EMA setups need room to breathe
STOP_ATR_MULT_VWAP    = 0.4   # VWAP bounce stops

# ─── Best Setups for Gold ──────────────────────────────────────────
SETUP_PRIORITY = [
    "LIQ_SWEEP_BULL",      # #1 — London open sweeps are gold
    "LIQ_SWEEP_BEAR",      # #1 — same
    "EMA50_RECLAIM",       # #2 — Gold trends after reclaims
    "EMA50_BREAKDOWN",     # #2 — same
    "VWAP_BOUNCE_BULL",    # #3 — UPGRADED: 39 missed wins, nearly 100% would-win
    "EMA21_PULLBACK_BULL", # #4 — trend continuation
    "EMA21_PULLBACK_BEAR", # #4 — trend continuation
    "BREAK_RETEST_BULL",   # #5 — Gold respects broken levels
    "BREAK_RETEST_BEAR",   # #5 — same
    "APPROACH_SUPPORT",    # anticipatory around key levels
    "APPROACH_RESIST",     # anticipatory
    "VWAP_REJECT_BEAR",    # less reliable on Gold
]

# ─── Gold Specific Conviction Bonuses ──────────────────────────────
def extra_conviction_factors(df_entry, df_htf, setup, trend, adx_val, rsi_val) -> dict:
    """
    Returns NQ-specific conviction adjustments for Gold.
    """
    from datetime import datetime, timezone, timedelta
    bonuses = {}

    direction = setup.get("direction", "")

    # Gold bonus: daily trend alignment is very important
    if "LONG" in direction and trend >= 4:
        bonuses["daily_bull_aligned"] = 12
    elif "SHORT" in direction and trend <= -4:
        bonuses["daily_bear_aligned"] = 12

    # Gold penalty: counter-trend is very risky on Gold
    if "LONG" in direction and trend <= -3:
        bonuses["counter_trend"] = -18
    elif "SHORT" in direction and trend >= 3:
        bonuses["counter_trend"] = -18

    # Gold: use stricter ADX during prime sessions
    now_et = datetime.now(timezone.utc) - timedelta(hours=4)
    hour   = now_et.hour
    if 2 <= hour <= 8:
        bonuses["london_session"] = 8
    elif 8 <= hour <= 11:
        bonuses["us_open_overlap"] = 6

    # Gold bonus: strong ADX means it's trending cleanly
    if adx_val >= 28:
        bonuses["strong_adx"] = 8
    elif adx_val >= 20:
        bonuses["decent_adx"] = 4

    # Gold: avoid RSI extremes more than NQ
    if "LONG" in direction and rsi_val > 72:
        bonuses["overbought_penalty"] = -10
    elif "SHORT" in direction and rsi_val < 28:
        bonuses["oversold_penalty"] = -10

    return bonuses


# ─── Gold Session Context ──────────────────────────────────────────
def get_session_context() -> dict:
    from datetime import datetime, timezone, timedelta
    now_et = datetime.now(timezone.utc) - timedelta(hours=4)
    hour   = now_et.hour

    if 2 <= hour < 8:
        return {"session": "London Session", "emoji": "🇬🇧",
                "note": "Peak Gold hours — highest volume and clearest moves"}
    elif 8 <= hour < 12:
        return {"session": "London/NY Overlap", "emoji": "🌍",
                "note": "Most volatile period for Gold — respect levels"}
    elif 12 <= hour < 16:
        return {"session": "US Afternoon", "emoji": "🇺🇸",
                "note": "Slower Gold action — watch for fades"}
    else:
        return {"session": "Asia Session", "emoji": "🌏",
                "note": "Lower volume — valid setups still fire"}


# ─── Gold Alert Footer ─────────────────────────────────────────────
def alert_footer(setup, session_ctx) -> str:
    lines = []
    session = session_ctx.get("session", "")
    note    = session_ctx.get("note", "")
    emoji   = session_ctx.get("emoji", "")

    if session:
        lines.append(f"{emoji} Session: {session} — {note}")

    setup_type = setup.get("type", "")
    if "LIQ_SWEEP" in setup_type:
        lines.append("💡 Gold tip: London sweeps are the cleanest — trust them.")
    elif "EMA50" in setup_type:
        lines.append("💡 Gold tip: Once Gold reclaims EMA50 it tends to trend — let it run.")
    elif "APPROACH" in setup_type:
        lines.append("💡 Gold tip: Round numbers (2000, 2100 etc) are key liquidity zones.")

    return "\n".join(lines)
