"""
markets/market_SOL.py - Solana (SOL/USD)
==========================================
SOL specific logic, settings, and scanning rules.

What makes SOL unique:
- Most volatile of the four markets — biggest % moves
- 15m is the primary timeframe — moves happen fast
- Smaller position sizes recommended due to volatility
- Volume spikes are the #1 signal on SOL
- Liquidity sweeps are very aggressive — wider stops needed
- Highly correlated to BTC — check BTC bias first
- Can move 5-15% in a single session — size accordingly
- Weekend candles can be very large
- Less institutional than BTC — more retail driven
- Setups need stronger confirmation than other markets
"""

# ─── Market Identity ───────────────────────────────────────────────
NAME          = "SOL"
FULL_NAME     = "Solana (SOL/USD)"
DATA_SOURCE   = "crypto"
CRYPTO_SYMBOL = "SOL/USDT"
EMOJI         = "◎"

# ─── Scanning Rules ────────────────────────────────────────────────
SCAN_24_7        = True
ENTRY_TIMEFRAMES = ["15m", "1h"]   # 15m primary — SOL moves fast
HTF_CONFIRM      = "1h"
HTF_SWING        = "4h"
TIMEFRAMES       = ["15m", "1h", "4h", "1d"]

# ─── Quality Filters ───────────────────────────────────────────────
# SOL requires STRONGER confirmation than other markets due to volatility
MIN_ADX          = 22      # Needs clear trend — SOL chops violently
MIN_RR           = 2.0     # Wave 22 (May 9, 2026): lowered 2.5->2.0 for crypto scalp mode
NEWS_MIN_RR      = 4.0     # Lowered from 5.0 — was blocking some real setups, still high for safety
MIN_CONVICTION   = 70      # Wave 22 (May 9, 2026): lowered 72->70 for crypto scalp mode (modest reduction; SOL has weak data)
COOLDOWN_MIN     = 60      # 1 hour cooldown

# ─── Per-setup ADX minimums ────────────────────────────────────────
# SOL needs stronger ADX than other markets due to volatility
ADX_MIN_BY_SETUP = {
    "LIQ_SWEEP_BULL":   16,
    "LIQ_SWEEP_BEAR":   16,
    "EMA50_RECLAIM":    22,
    "EMA50_BREAKDOWN":  22,
    "VWAP_BOUNCE_BULL": 12,   # Lowered from 16 — 19 missed wins at avg ADX 17.8
    "VWAP_REJECT_BEAR": 30,   # RAISED from 20 — 0% WR over 4 trades, almost never works on SOL
    "APPROACH_SUPPORT": 12,
    "APPROACH_RESIST":  12,
    "RSI_DIV_BULL":     16,   # SOL needs stronger signal for divergence
    "RSI_DIV_BEAR":     16,
    "EMA21_PULLBACK_BULL": 22,   # SOL needs strong trend confirmation
    "EMA21_PULLBACK_BEAR": 22,
    "BREAK_RETEST_BULL":   18,   # SOL break-retests need momentum
    "BREAK_RETEST_BEAR":   18,
}

# ─── Stop Loss Rules ───────────────────────────────────────────────
# SOL needs the WIDEST stops — it wicks aggressively
STOP_ATR_MULT_SWEEP   = 0.6   # Wide stops for SOL sweeps
STOP_ATR_MULT_EMA     = 0.8   # EMA setups need lots of room
STOP_ATR_MULT_VWAP    = 0.5   # VWAP bounces

# ─── Leverage Settings ─────────────────────────────────────────────
# Lower leverage than BTC due to higher volatility
LEVERAGE_BY_TIER = {
    "HIGH":   10,    # 10x max on high conviction
    "MEDIUM": 5,     # 5x on medium conviction
    "LOW":    2,     # 2x on low conviction
}

# ─── Best Setups for SOL ───────────────────────────────────────────
SETUP_PRIORITY = [
    "LIQ_SWEEP_BULL",      # #1 — sweeps are violent on SOL but reliable
    "LIQ_SWEEP_BEAR",      # #1 — same
    "VWAP_BOUNCE_BULL",    # #2 — VWAP important for SOL intraday
    "EMA21_PULLBACK_BULL", # #3 — trend continuation
    "EMA21_PULLBACK_BEAR", # #3 — trend continuation
    "BREAK_RETEST_BULL",   # #4 — institutional pattern
    "BREAK_RETEST_BEAR",   # #4 — institutional pattern
    "APPROACH_SUPPORT",    # anticipatory setups
    "APPROACH_RESIST",     # anticipatory
    "EMA50_RECLAIM",       # less reliable on SOL than BTC
    "EMA50_BREAKDOWN",     # same
    "VWAP_REJECT_BEAR",    # DEMOTED: 0% WR over 4 trades
]

# ─── SOL Specific Conviction Bonuses ───────────────────────────────
def extra_conviction_factors(df_entry, df_htf, setup, trend, adx_val, rsi_val) -> dict:
    from datetime import datetime, timezone
    bonuses = {}

    direction = setup.get("direction", "")

    # SOL #1 signal: volume spike is critical
    try:
        vol_last  = float(df_entry["Volume"].iloc[-1])
        vol_avg   = float(df_entry["Volume"].rolling(20).mean().iloc[-1])
        vol_ratio = vol_last / max(1e-9, vol_avg)
        if vol_ratio >= 2.5:
            bonuses["massive_volume"] = 15    # huge signal on SOL
        elif vol_ratio >= 1.8:
            bonuses["strong_volume"]  = 10
        elif vol_ratio >= 1.3:
            bonuses["decent_volume"]  = 5
        elif vol_ratio < 0.8:
            bonuses["low_volume_penalty"] = -12  # avoid low volume SOL setups
    except:
        pass

    # SOL: BTC correlation — if BTC trend is strong, SOL follows
    # (trend here is SOL's own trend, but we weight it more)
    if "LONG" in direction and trend >= 5:
        bonuses["strong_bull_trend"] = 12
    elif "SHORT" in direction and trend <= -5:
        bonuses["strong_bear_trend"] = 12

    # SOL penalty: counter trend is extremely risky
    if "LONG" in direction and trend <= -3:
        bonuses["counter_trend"] = -20
    elif "SHORT" in direction and trend >= 3:
        bonuses["counter_trend"] = -20

    # SOL: RSI extremes are very common — penalize chasing
    if "LONG" in direction and rsi_val > 78:
        bonuses["extended_long"] = -15
    elif "SHORT" in direction and rsi_val < 22:
        bonuses["extended_short"] = -15

    # SOL: strong ADX = one of the cleaner signals
    if adx_val >= 28:
        bonuses["strong_adx"] = 10

    # SOL penalty: VWAP_REJECT_BEAR has 0% WR over 4 trades
    setup_type = setup.get("type", "")
    if setup_type == "VWAP_REJECT_BEAR":
        bonuses["vwap_reject_penalty"] = -25

    # SOL: Asia session is active for SOL
    now_utc  = datetime.now(timezone.utc)
    hour_utc = now_utc.hour
    if 0 <= hour_utc <= 8:
        bonuses["asia_active"] = 5

    return bonuses


# ─── SOL Session Context ───────────────────────────────────────────
def get_session_context() -> dict:
    from datetime import datetime, timezone
    now_utc  = datetime.now(timezone.utc)
    hour_utc = now_utc.hour

    if 0 <= hour_utc < 8:
        return {"session": "Asia Session",   "emoji": "🌏",
                "note": "SOL active in Asia — watch for big volume spikes"}
    elif 8 <= hour_utc < 13:
        return {"session": "London Session", "emoji": "🇬🇧",
                "note": "Building up to US open — decent SOL volume"}
    elif 13 <= hour_utc < 21:
        return {"session": "US Session",     "emoji": "🇺🇸",
                "note": "Peak SOL hours — biggest moves and most volume"}
    else:
        return {"session": "Late Night",     "emoji": "🌙",
                "note": "Lower volume — SOL can still make big moves"}


# ─── SOL Alert Footer ──────────────────────────────────────────────
def alert_footer(setup, session_ctx) -> str:
    lines = []
    session = session_ctx.get("session", "")
    note    = session_ctx.get("note", "")
    emoji   = session_ctx.get("emoji", "")

    if session:
        lines.append(f"{emoji} Session: {session} — {note}")

    # SOL always gets a size warning
    lines.append("⚠️ SOL tip: High volatility — use smaller size than usual.")

    setup_type = setup.get("type", "")
    if "LIQ_SWEEP" in setup_type:
        lines.append("💡 SOL tip: Sweeps are aggressive — wait for candle close, don't anticipate entry.")
    elif "VWAP" in setup_type:
        lines.append("💡 SOL tip: VWAP bounces need volume confirmation on SOL or they fail.")
    elif "EMA50" in setup_type:
        lines.append("💡 SOL tip: EMA50 setups need very strong volume on SOL — check the candle.")

    return "\n".join(lines)
