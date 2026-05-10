"""
markets/market_BTC.py - Bitcoin (BTC/USD)
==========================================
BTC specific logic, settings, and scanning rules.

What makes BTC unique:
- Trades 24/7 with no closing bell — never stops
- Asia session is very active for BTC — Tokyo and Singapore move it
- More volatile than NQ and Gold — needs wider stops
- Volume spikes are extremely important signals
- Liquidity sweeps happen fast — entries need to be quick
- EMA setups work but need strong volume confirmation
- Leverage suggestions included for crypto traders
- 15m works well — BTC respects technical levels
- Sunday/weekend moves often set the week's direction
- Funding rates and exchange data can affect direction
"""

# ─── Market Identity ───────────────────────────────────────────────
NAME        = "BTC"
FULL_NAME   = "Bitcoin (BTC/USD)"
DATA_SOURCE = "crypto"
CRYPTO_SYMBOL = "BTC/USDT"
EMOJI       = "₿"

# ─── Scanning Rules ────────────────────────────────────────────────
SCAN_24_7        = True    # BTC never sleeps
ENTRY_TIMEFRAMES = ["15m", "1h"]
HTF_CONFIRM      = "1h"
HTF_SWING        = "4h"
TIMEFRAMES       = ["15m", "1h", "4h", "1d"]

# ─── Quality Filters ───────────────────────────────────────────────
MIN_ADX          = 16      # Lowered — BTC trending at 17+ overnight is real
MIN_ADX_PRIME    = 20      # US/London sessions require stronger trend
MIN_RR           = 1.5     # Wave 22 (May 9, 2026): lowered 2.0->1.5 for crypto scalp mode
NEWS_MIN_RR      = 2.8     # Lowered from 3.5 — 28 missed winners at RR 2.5 during news
MIN_CONVICTION   = 60      # Wave 22 (May 9, 2026): lowered 62->60 for crypto scalp mode
COOLDOWN_MIN     = 45      # 45 min — BTC moves faster than Gold

# ─── Per-setup ADX minimums ────────────────────────────────────────
ADX_MIN_BY_SETUP = {
    "LIQ_SWEEP_BULL":   14,
    "LIQ_SWEEP_BEAR":   14,
    "EMA50_RECLAIM":    20,
    "EMA50_BREAKDOWN":  20,
    "VWAP_BOUNCE_BULL": 10,   # Lowered from 14 — 100% WR when fired, 31 missed wins
    "VWAP_REJECT_BEAR": 28,   # RAISED from 16 — 0% WR over 5 trades, needs strong trend to fire
    "APPROACH_SUPPORT": 10,
    "APPROACH_RESIST":  10,
    "RSI_DIV_BULL":     14,
    "RSI_DIV_BEAR":     14,
    "EMA21_PULLBACK_BULL": 18,   # BTC trends hard — pullbacks are reliable
    "EMA21_PULLBACK_BEAR": 18,
    "BREAK_RETEST_BULL":   14,   # BTC respects broken levels
    "BREAK_RETEST_BEAR":   14,
}

# ─── Stop Loss Rules ───────────────────────────────────────────────
# BTC is volatile — stops need room or you get wicked out
STOP_ATR_MULT_SWEEP   = 0.4   # Slightly wider for BTC volatility
STOP_ATR_MULT_EMA     = 0.6
STOP_ATR_MULT_VWAP    = 0.3

# ─── Leverage Settings ─────────────────────────────────────────────
# Suggested leverage by conviction tier
# Conservative — designed to keep risk at account_risk_pct
LEVERAGE_BY_TIER = {
    "HIGH":   15,    # 15x max on high conviction
    "MEDIUM": 8,     # 8x on medium conviction
    "LOW":    3,     # 3x on low conviction
}

# ─── Best Setups for BTC ───────────────────────────────────────────
SETUP_PRIORITY = [
    "LIQ_SWEEP_BULL",      # #1 — BTC sweeps are very clean
    "LIQ_SWEEP_BEAR",      # #1 — same
    "VWAP_BOUNCE_BULL",    # #2 — 100% WR, VWAP is key for BTC
    "EMA21_PULLBACK_BULL", # #3 — trend continuation
    "EMA21_PULLBACK_BEAR", # #3 — trend continuation
    "BREAK_RETEST_BULL",   # #4 — institutional pattern
    "BREAK_RETEST_BEAR",   # #4 — institutional pattern
    "EMA50_RECLAIM",       # #5 — works in trending BTC
    "EMA50_BREAKDOWN",     # #5 — same
    "APPROACH_SUPPORT",    # anticipatory
    "APPROACH_RESIST",     # anticipatory
    "VWAP_REJECT_BEAR",    # DEMOTED: 0% WR over 5 trades
]

# ─── BTC Specific Conviction Bonuses ───────────────────────────────
def extra_conviction_factors(df_entry, df_htf, setup, trend, adx_val, rsi_val) -> dict:
    from datetime import datetime, timezone, timedelta
    bonuses = {}

    direction = setup.get("direction", "")

    # BTC bonus: volume surge is very meaningful
    try:
        vol_last = float(df_entry["Volume"].iloc[-1])
        vol_avg  = float(df_entry["Volume"].rolling(20).mean().iloc[-1])
        vol_ratio = vol_last / max(1e-9, vol_avg)
        if vol_ratio >= 2.0:
            bonuses["volume_surge_big"]  = 12
        elif vol_ratio >= 1.5:
            bonuses["volume_surge_med"]  = 7
    except:
        pass

    # BTC bonus: Asia session is prime time for BTC
    now_utc = datetime.now(timezone.utc)
    hour_utc = now_utc.hour
    # Asia session roughly 0-8 UTC
    if 0 <= hour_utc <= 8:
        bonuses["asia_session_btc"] = 6
    # US session overlap
    elif 13 <= hour_utc <= 21:
        bonuses["us_session_btc"] = 5

    # BTC: trend alignment bonus
    if "LONG" in direction and trend >= 4:
        bonuses["bull_trend"] = 8
    elif "SHORT" in direction and trend <= -4:
        bonuses["bear_trend"] = 8

    # BTC penalty: RSI extremes — crypto can stay extended but adds risk
    if "LONG" in direction and rsi_val > 75:
        bonuses["overbought"] = -12
    elif "SHORT" in direction and rsi_val < 25:
        bonuses["oversold"] = -12

    # BTC bonus: strong ADX on BTC = powerful move incoming
    if adx_val >= 30:
        bonuses["strong_adx"] = 10

    # BTC penalty: VWAP_REJECT_BEAR has 0% WR — penalize heavily
    setup_type = setup.get("type", "")
    if setup_type == "VWAP_REJECT_BEAR":
        bonuses["vwap_reject_penalty"] = -20

    return bonuses


# ─── BTC Session Context ───────────────────────────────────────────
def get_session_context() -> dict:
    from datetime import datetime, timezone
    now_utc  = datetime.now(timezone.utc)
    hour_utc = now_utc.hour

    if 0 <= hour_utc < 8:
        return {"session": "Asia Session",   "emoji": "🌏",
                "note": "Prime BTC hours — Tokyo and Singapore active"}
    elif 8 <= hour_utc < 13:
        return {"session": "London Session", "emoji": "🇬🇧",
                "note": "European traders active — good BTC volume"}
    elif 13 <= hour_utc < 21:
        return {"session": "US Session",     "emoji": "🇺🇸",
                "note": "Highest volume — biggest BTC moves happen here"}
    else:
        return {"session": "Late US/Early Asia", "emoji": "🌙",
                "note": "Lower volume — valid setups still fire"}


# ─── BTC Alert Footer ──────────────────────────────────────────────
def alert_footer(setup, session_ctx) -> str:
    lines = []
    session = session_ctx.get("session", "")
    note    = session_ctx.get("note", "")
    emoji   = session_ctx.get("emoji", "")

    if session:
        lines.append(f"{emoji} Session: {session} — {note}")

    setup_type = setup.get("type", "")
    if "LIQ_SWEEP" in setup_type:
        lines.append("💡 BTC tip: Sweeps happen fast — be ready at the level before it gets there.")
    elif "VWAP" in setup_type:
        lines.append("💡 BTC tip: VWAP bounces work well during US session with volume confirmation.")
    elif "EMA50" in setup_type:
        lines.append("💡 BTC tip: EMA50 reclaims need strong volume — low volume reclaims often fail.")

    return "\n".join(lines)
