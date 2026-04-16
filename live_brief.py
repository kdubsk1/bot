"""
live_brief.py - NQ CALLS 2026
===============================
Generates a professional live brief Telegram message for each market.

Usage:
    from live_brief import generate_live_brief
    msg = generate_live_brief("NQ", {"15m": df_15m, "1h": df_1h, "4h": df_4h, "1d": df_1d})
"""

from __future__ import annotations
import numpy as np
import pandas as pd
from markets import get_market_config
from outcome_tracker import ema, rsi, atr, vwap, swing_points
from regime_classifier import classify_regime


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #
_SEP = "━━━━━━━━━━━━━━━━━━"


def _safe(text: str) -> str:
    """Make text Telegram MarkdownV2-safe: replace underscores with spaces."""
    return str(text).replace("_", " ")


def _fmt_price(price: float, market: str) -> str:
    """Format price with appropriate decimals per market."""
    if market in ("BTC",):
        return f"{price:,.0f}"
    if market in ("SOL",):
        return f"{price:,.2f}"
    if market in ("GC",):
        return f"{price:,.1f}"
    return f"{price:,.0f}"


def _ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure DataFrame has both capitalized and lowercase column names."""
    col_map = {"open": "Open", "high": "High", "low": "Low",
               "close": "Close", "volume": "Volume"}
    for lower, upper in col_map.items():
        if lower in df.columns and upper not in df.columns:
            df[upper] = df[lower]
        elif upper in df.columns and lower not in df.columns:
            df[lower] = df[upper]
    return df


# ------------------------------------------------------------------ #
# Swing level helpers
# ------------------------------------------------------------------ #
def _get_resistances(frames: dict, price: float, count: int = 2) -> list[float]:
    """Get nearest swing high levels above current price from 15m and 4h frames."""
    levels = []
    for tf_key in ("15m", "4h"):
        df = frames.get(tf_key)
        if df is None or len(df) < 20:
            continue
        df = _ensure_columns(df)
        highs_idx, _ = swing_points(df, 5)
        for i in highs_idx:
            lvl = float(df["High"].iloc[i])
            if lvl > price:
                levels.append(lvl)
    # Deduplicate close levels (within 0.1%)
    levels.sort()
    filtered = []
    for lvl in levels:
        if not filtered or abs(lvl - filtered[-1]) / max(filtered[-1], 1) > 0.001:
            filtered.append(lvl)
    return filtered[:count]


def _get_supports(frames: dict, price: float, count: int = 2) -> list[float]:
    """Get nearest swing low levels below current price from 15m and 4h frames."""
    levels = []
    for tf_key in ("15m", "4h"):
        df = frames.get(tf_key)
        if df is None or len(df) < 20:
            continue
        df = _ensure_columns(df)
        _, lows_idx = swing_points(df, 5)
        for i in lows_idx:
            lvl = float(df["Low"].iloc[i])
            if lvl < price:
                levels.append(lvl)
    # Deduplicate close levels, sort descending (nearest first)
    levels.sort(reverse=True)
    filtered = []
    for lvl in levels:
        if not filtered or abs(lvl - filtered[-1]) / max(filtered[-1], 1) > 0.001:
            filtered.append(lvl)
    return filtered[:count]


# ------------------------------------------------------------------ #
# Narrative generator
# ------------------------------------------------------------------ #
def _build_narrative(market: str, regime: str, bias: str, price: float,
                     rsi_val: float, resistances: list, supports: list,
                     atr_val: float, fmt: callable) -> str:
    """Build a plain-English narrative paragraph based on bias and regime."""

    p = fmt(price)

    if regime == "TRENDING_BULL":
        support_ref = fmt(supports[0]) if supports else "N/A"
        target_ref = fmt(resistances[0]) if resistances else "N/A"
        rsi_note = "room to run" if rsi_val < 65 else "getting extended"
        return (
            f"{market} is in a strong uptrend at {p}. "
            f"Trend intact above {support_ref}. "
            f"Next target: {target_ref} (prior swing high). "
            f"RSI at {rsi_val:.0f}, {rsi_note}."
        )

    elif regime == "TRENDING_BEAR":
        resist_ref = fmt(resistances[0]) if resistances else "N/A"
        target_ref = fmt(supports[0]) if supports else "N/A"
        return (
            f"{market} is in a downtrend at {p}. "
            f"Resistance at {resist_ref} (prior swing low, now ceiling). "
            f"Next downside target: {target_ref}. "
            f"Bounces into {resist_ref} are sell opportunities."
        )

    elif regime == "VOLATILE_EXPANSION":
        return (
            f"{market} is in a volatile expansion phase. "
            f"ATR spiked. Wide stops required. "
            f"Trade reduced size or wait for regime to settle."
        )

    else:  # RANGING
        upper = fmt(resistances[0]) if resistances else "N/A"
        lower = fmt(supports[0]) if supports else "N/A"
        return (
            f"{market} is consolidating between {lower} and {upper}. "
            f"No clear directional edge \u2014 wait for a break of either "
            f"level with volume."
        )


# ------------------------------------------------------------------ #
# Main function
# ------------------------------------------------------------------ #
def generate_live_brief(market: str, frames: dict) -> str:
    """Generate a professional live brief Telegram message for a market.

    Parameters
    ----------
    market : str
        Market symbol, e.g. "NQ", "GC", "BTC", "SOL".
    frames : dict
        DataFrames keyed by timeframe: {"15m": df, "1h": df, "4h": df, "1d": df}.

    Returns
    -------
    str
        Formatted Telegram message (Markdown-safe).
    """
    cfg = get_market_config(market)
    emoji = cfg.EMOJI
    full_name = _safe(cfg.FULL_NAME)

    fmt = lambda p: _fmt_price(p, market)

    # Ensure columns on all frames
    for tf in frames:
        frames[tf] = _ensure_columns(frames[tf])

    # ── Current price and daily change ──────────────────────────────
    df_1d = frames.get("1d")
    df_15m = frames.get("15m")

    current_price = float(df_15m["Close"].iloc[-1]) if df_15m is not None else 0.0

    daily_change_pct = 0.0
    if df_1d is not None and len(df_1d) >= 2:
        prev_close = float(df_1d["Close"].iloc[-2])
        if prev_close != 0:
            daily_change_pct = ((current_price - prev_close) / prev_close) * 100

    change_arrow = "\U0001f7e2" if daily_change_pct >= 0 else "\U0001f534"
    change_sign = "+" if daily_change_pct >= 0 else ""

    # ── Regime ──────────────────────────────────────────────────────
    regime_info = classify_regime(df_15m, market) if df_15m is not None else {
        "regime": "RANGING", "confidence": 0, "adx": 0
    }
    regime = regime_info["regime"]
    regime_label = _safe(regime)
    regime_conf = regime_info.get("confidence", 0)

    # ── Bias ────────────────────────────────────────────────────────
    bias = "NEUTRAL"
    if df_15m is not None and len(df_15m) >= 200:
        close = float(df_15m["Close"].iloc[-1])
        ema50_val = float(ema(df_15m["Close"], 50).iloc[-1])
        ema200_val = float(ema(df_15m["Close"], 200).iloc[-1])
        if close > ema50_val > ema200_val:
            bias = "BULLISH"
        elif close < ema50_val < ema200_val:
            bias = "BEARISH"

    bias_emoji = {"BULLISH": "\U0001f7e2", "BEARISH": "\U0001f534", "NEUTRAL": "\u26aa"}[bias]

    # ── RSI ─────────────────────────────────────────────────────────
    rsi_val = 50.0
    if df_15m is not None:
        rsi_val = float(rsi(df_15m["Close"], 14).iloc[-1])
    if rsi_val > 70:
        rsi_label = "Overbought"
    elif rsi_val < 30:
        rsi_label = "Oversold"
    else:
        rsi_label = "Neutral"

    # ── VWAP ────────────────────────────────────────────────────────
    vwap_status = "N/A"
    if df_15m is not None:
        try:
            vwap_val = float(vwap(df_15m).iloc[-1])
            vwap_status = "Above VWAP" if current_price > vwap_val else "Below VWAP"
        except Exception:
            vwap_status = "N/A"

    # ── Support / Resistance ────────────────────────────────────────
    resistances = _get_resistances(frames, current_price, 2)
    supports = _get_supports(frames, current_price, 2)

    resist_lines = []
    for i, lvl in enumerate(resistances, 1):
        resist_lines.append(f"  R{i}: {fmt(lvl)}")
    if not resist_lines:
        resist_lines.append("  No clear levels")

    support_lines = []
    for i, lvl in enumerate(supports, 1):
        support_lines.append(f"  S{i}: {fmt(lvl)}")
    if not support_lines:
        support_lines.append("  No clear levels")

    # ── Prior day high / low ────────────────────────────────────────
    prior_high = "N/A"
    prior_low = "N/A"
    if df_1d is not None and len(df_1d) >= 2:
        prior_high = fmt(float(df_1d["High"].iloc[-2]))
        prior_low = fmt(float(df_1d["Low"].iloc[-2]))

    # ── ATR (daily) ─────────────────────────────────────────────────
    atr_daily = 0.0
    if df_1d is not None and len(df_1d) >= 14:
        atr_daily = float(atr(df_1d, 14).iloc[-1])

    # ── Narrative ───────────────────────────────────────────────────
    narrative = _build_narrative(
        market, regime, bias, current_price,
        rsi_val, resistances, supports, atr_daily, fmt
    )

    # ── Assemble message ────────────────────────────────────────────
    lines = [
        f"{emoji} {full_name} Live Brief",
        _SEP,
        f"Price: {fmt(current_price)}  {change_arrow} {change_sign}{daily_change_pct:.2f}%",
        "",
        f"Regime: {regime_label} ({regime_conf}% confidence)",
        f"Bias: {bias_emoji} {bias}",
        f"RSI(14): {rsi_val:.1f} ({rsi_label})",
        f"VWAP: {vwap_status}",
        _SEP,
        "Resistance:",
        *resist_lines,
        "Support:",
        *support_lines,
        _SEP,
        f"Prior Day High: {prior_high}",
        f"Prior Day Low:  {prior_low}",
        f"ATR(14) Daily:  {fmt(atr_daily)} pts expected move",
        _SEP,
        narrative,
    ]

    return "\n".join(lines)
