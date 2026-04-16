"""
regime_classifier.py – Classifies the current market regime from 15-minute OHLCV data.

Regimes: TRENDING_BULL, TRENDING_BEAR, RANGING, VOLATILE_EXPANSION
"""

import numpy as np
import pandas as pd
from outcome_tracker import adx, atr, ema


# ---------------------------------------------------------------------------
# Setup-to-regime preference map
# ---------------------------------------------------------------------------
SETUP_REGIME_PREFERENCES = {
    # Mean-reversion setups – prefer ranging, but don't strictly avoid trends
    "LIQ_SWEEP_BULL":                   {"prefer": "RANGING", "avoid": []},
    "LIQ_SWEEP_BEAR":                   {"prefer": "RANGING", "avoid": []},
    "VWAP_BOUNCE_BULL":                 {"prefer": "RANGING", "avoid": []},
    "VWAP_REJECT_BEAR":                 {"prefer": "RANGING", "avoid": []},
    "APPROACH_SUPPORT":                 {"prefer": "RANGING", "avoid": []},
    "APPROACH_RESIST":                  {"prefer": "RANGING", "avoid": []},
    "RSI_DIV_BULL":                     {"prefer": "RANGING", "avoid": []},
    "RSI_DIV_BEAR":                     {"prefer": "RANGING", "avoid": []},
    "FAILED_BREAKDOWN_BULL":            {"prefer": "RANGING", "avoid": []},
    "FAILED_BREAKOUT_BEAR":             {"prefer": "RANGING", "avoid": []},
    "VOLATILITY_CONTRACTION_BREAKOUT":  {"prefer": "RANGING", "avoid": []},

    # Continuation long setups
    "EMA21_PULLBACK_BULL":  {"prefer": "TRENDING_BULL", "avoid": ["TRENDING_BEAR"]},
    "EMA50_RECLAIM":        {"prefer": "TRENDING_BULL", "avoid": ["TRENDING_BEAR"]},
    "BREAK_RETEST_BULL":    {"prefer": "TRENDING_BULL", "avoid": ["TRENDING_BEAR"]},

    # Continuation short setups
    "EMA21_PULLBACK_BEAR":  {"prefer": "TRENDING_BEAR", "avoid": ["TRENDING_BULL"]},
    "EMA50_BREAKDOWN":      {"prefer": "TRENDING_BEAR", "avoid": ["TRENDING_BULL"]},
    "BREAK_RETEST_BEAR":    {"prefer": "TRENDING_BEAR", "avoid": ["TRENDING_BULL"]},
}


# ---------------------------------------------------------------------------
# Core classifier
# ---------------------------------------------------------------------------
def classify_regime(df: pd.DataFrame, market: str = "") -> dict:
    """Classify the current market regime from a 15-minute OHLCV dataframe.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain columns: high, low, close (case-insensitive OK).
    market : str, optional
        Informational label (e.g. "NQ", "GC").

    Returns
    -------
    dict with keys:
        regime            – TRENDING_BULL | TRENDING_BEAR | RANGING | VOLATILE_EXPANSION
        confidence        – 0-100
        adx               – current ADX(14) value
        atr_percentile    – ATR(14) percentile over last 100 bars
        ema50_slope_pct   – EMA50 slope as pct change per bar (last 5 bars)
    """
    # --- Indicators ---
    adx_series = adx(df, 14)
    atr_series = atr(df, 14)
    ema50_series = ema(df["close"], 50)

    current_adx = float(adx_series.iloc[-1])

    # ATR percentile over last 100 bars
    lookback = min(100, len(atr_series))
    atr_window = atr_series.iloc[-lookback:]
    current_atr = float(atr_series.iloc[-1])
    atr_pct = float((atr_window < current_atr).sum() / lookback * 100)

    # EMA50 slope as pct change per bar over last 5 bars
    ema50_now = float(ema50_series.iloc[-1])
    ema50_5ago = float(ema50_series.iloc[-6])  # 5-bar span
    ema50_slope_pct = ((ema50_now / ema50_5ago) - 1) * 100 / 5  # per-bar pct

    current_close = float(df["close"].iloc[-1])

    # --- Classification ---
    if atr_pct > 85:
        regime = "VOLATILE_EXPANSION"
        # Confidence scales with how extreme the ATR percentile is
        confidence = min(100, int(50 + (atr_pct - 85) * 3.33))
    elif current_adx > 25 and ema50_slope_pct > 0.15 and current_close > ema50_now:
        regime = "TRENDING_BULL"
        confidence = min(100, int(current_adx * 2))
    elif current_adx > 25 and ema50_slope_pct < -0.15 and current_close < ema50_now:
        regime = "TRENDING_BEAR"
        confidence = min(100, int(current_adx * 2))
    else:
        regime = "RANGING"
        # Lower ADX → more confident it's ranging
        confidence = min(100, int((50 - current_adx) * 2.5)) if current_adx < 50 else 0
        confidence = max(0, confidence)

    return {
        "regime": regime,
        "confidence": confidence,
        "adx": round(current_adx, 2),
        "atr_percentile": round(atr_pct, 1),
        "ema50_slope_pct": round(ema50_slope_pct, 4),
    }
