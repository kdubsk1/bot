"""
backtest.py - NQ CALLS Historical Backtester
==============================================
Downloads historical OHLCV data, runs it through the SAME detect_setups
and conviction_score logic the live bot uses, and outputs a full
performance report.

Usage:
    python backtest.py --market NQ --days 90
    python backtest.py --market BTC --days 30
    python backtest.py --all --days 60
    python backtest.py --market GC --days 90 --save-csv

What it does:
    1. Downloads historical candles via yfinance / ccxt
    2. Walks through each candle bar-by-bar (simulating live scans)
    3. Runs detect_setups + conviction_score on each bar
    4. Checks if price hit target or stop in future bars
    5. Outputs win rate by setup type, market, hour, conviction tier

This uses the REAL bot logic — not a simplified version.
Results here = what the bot would have fired in real time.
"""

import argparse
import sys
import os
import json
import csv
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import pandas as pd
import numpy as np
import yfinance as yf

# Add project root to path so imports work
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BASE_DIR)

import outcome_tracker as ot
from markets import get_market_config, get_all_markets

# ── Data download ─────────────────────────────────────────────────

YF_MAP = {"NQ": "NQ=F", "GC": "GC=F", "BTC": "BTC-USD", "SOL": "SOL-USD"}

def download_data(market: str, days: int) -> dict:
    """
    Downloads OHLCV data for all timeframes the bot uses.
    Returns {tf: DataFrame}.
    """
    symbol = YF_MAP.get(market)
    if not symbol:
        print(f"  Unknown market: {market}")
        return {}

    cfg = get_market_config(market)
    frames = {}

    # yfinance interval/period mapping
    # For backtesting we need enough history
    tf_config = {
        "15m": {"interval": "15m", "period": f"{min(days, 60)}d"},
        "1h":  {"interval": "60m", "period": f"{min(days, 730)}d"},
        "4h":  {"interval": "60m", "period": f"{min(days, 730)}d"},  # resample from 1h
        "1d":  {"interval": "1d",  "period": f"{min(days, 730*3)}d"},
    }

    for tf in cfg.TIMEFRAMES:
        tc = tf_config.get(tf)
        if not tc:
            continue
        try:
            print(f"  Downloading {market} {tf}...")
            df = yf.download(symbol, interval=tc["interval"], period=tc["period"],
                             progress=False, auto_adjust=False)
            if df is None or df.empty:
                print(f"    No data for {tf}")
                continue

            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.rename(columns=str.title)
            for c in ["Open", "High", "Low", "Close", "Volume"]:
                if c not in df.columns:
                    print(f"    Missing column {c} for {tf}")
                    continue
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()

            # Resample to 4h if needed
            if tf == "4h":
                df = df.resample("4h").agg({
                    "Open": "first", "High": "max", "Low": "min",
                    "Close": "last", "Volume": "sum"
                }).dropna()

            if len(df) >= 20:
                frames[tf] = df
                print(f"    Got {len(df)} candles")
            else:
                print(f"    Only {len(df)} candles (need 20+)")
        except Exception as e:
            print(f"    Error downloading {tf}: {e}")

    return frames


# ── Backtesting engine ────────────────────────────────────────────

def check_trade_outcome(df: pd.DataFrame, bar_idx: int, direction: str,
                        entry: float, stop: float, target: float,
                        max_bars: int = 50) -> dict:
    """
    Walk forward from bar_idx and check if target or stop was hit.
    Uses High/Low (not just Close) to match the live bot behavior.
    Returns {result, bars, exit_price}.
    """
    for i in range(bar_idx + 1, min(bar_idx + 1 + max_bars, len(df))):
        bar = df.iloc[i]
        high = float(bar["High"])
        low = float(bar["Low"])

        hit_target = hit_stop = False

        if direction == "LONG":
            if high >= target:
                hit_target = True
            if low <= stop:
                hit_stop = True
        else:  # SHORT
            if low <= target:
                hit_target = True
            if high >= stop:
                hit_stop = True

        # If both hit same bar, stop wins (conservative — matches live bot)
        if hit_target and hit_stop:
            hit_target = False

        if hit_target:
            return {"result": "WIN", "bars": i - bar_idx, "exit_price": target}
        if hit_stop:
            return {"result": "LOSS", "bars": i - bar_idx, "exit_price": stop}

    return {"result": "EXPIRED", "bars": max_bars, "exit_price": float(df.iloc[-1]["Close"])}


def run_backtest(market: str, frames: dict, min_conviction: int = 50) -> list:
    """
    Walks through the entry timeframe bar-by-bar, running detect_setups
    and conviction_score at each bar. Returns list of trade dicts.
    """
    cfg = get_market_config(market)
    trades = []

    # We need enough data for indicators (200 EMA needs 210+ bars)
    primary_tf = cfg.ENTRY_TIMEFRAMES[0]
    df_primary = frames.get(primary_tf)
    if df_primary is None or len(df_primary) < 220:
        print(f"  Not enough {primary_tf} data for {market} (have {len(df_primary) if df_primary is not None else 0}, need 220+)")
        return trades

    # Build HTF frames for each entry timeframe
    htf_map = {
        cfg.ENTRY_TIMEFRAMES[0]: cfg.HTF_CONFIRM,
    }
    if len(cfg.ENTRY_TIMEFRAMES) > 1:
        htf_map[cfg.ENTRY_TIMEFRAMES[1]] = cfg.HTF_SWING

    cooldowns = {}  # (market, setup_type) -> last_bar_index

    for entry_tf in cfg.ENTRY_TIMEFRAMES:
        df_entry = frames.get(entry_tf)
        htf_key = htf_map.get(entry_tf, cfg.HTF_CONFIRM)
        df_htf = frames.get(htf_key)

        if df_entry is None or len(df_entry) < 220:
            print(f"  Skipping {entry_tf} — not enough data")
            continue
        if df_htf is None or len(df_htf) < 20:
            print(f"  Skipping {entry_tf} — no HTF data")
            continue

        print(f"  Backtesting {market} [{entry_tf}] — {len(df_entry)} bars...")

        # Walk through bars starting at 210 (need EMA200 warmup)
        start_bar = 210
        for bar_idx in range(start_bar, len(df_entry) - 1):
            # Slice up to current bar (simulate "live" — bot only sees past data)
            df_slice = df_entry.iloc[:bar_idx + 1].copy()
            # HTF slice — find bars before current timestamp
            current_ts = df_entry.index[bar_idx]
            df_htf_slice = df_htf[df_htf.index <= current_ts]
            if len(df_htf_slice) < 20:
                continue

            # Get HTF bias
            htf_bias = ot.structure_bias(df_htf_slice)

            # Build trend frames (use sliced data)
            trend_frames = {}
            for tf_name, tf_df in frames.items():
                if tf_df is not None:
                    tf_slice = tf_df[tf_df.index <= current_ts]
                    if len(tf_slice) >= 20:
                        trend_frames[tf_name] = tf_slice

            # Get trend score
            trend, _ = ot.trend_score(trend_frames, market)

            # Detect setups
            try:
                setups = ot.detect_setups(df_slice, df_htf_slice, htf_bias)
            except Exception:
                continue

            if not setups:
                continue

            # Calculate indicators
            try:
                adx_v = float(ot.adx(df_slice).iloc[-1])
                rsi_v = float(ot.rsi(df_slice["Close"]).iloc[-1])
                atr_v = float(ot.atr(df_slice).iloc[-1])
                vol_mean = float(df_slice["Volume"].rolling(20).mean().iloc[-1])
                vol_last = float(df_slice["Volume"].iloc[-1])
                vol_ratio = vol_last / max(1e-9, vol_mean) if vol_mean > 0 else 0
            except Exception:
                continue

            # Volume sanity check (same as live bot)
            if vol_mean < 1.0 or vol_ratio < 0.1:
                continue

            for stp in setups:
                stp["market"] = market

                # Per-setup ADX check (same as live bot)
                adx_min_by_setup = getattr(cfg, "ADX_MIN_BY_SETUP", {})
                required_adx = adx_min_by_setup.get(stp["type"], cfg.MIN_ADX)
                if adx_v < required_adx:
                    continue

                # Cooldown check
                cd_key = (market, stp["type"])
                last_fire = cooldowns.get(cd_key, -999)
                cd_bars = max(1, int(cfg.COOLDOWN_MIN / (15 if entry_tf == "15m" else 60)))
                if bar_idx - last_fire < cd_bars:
                    continue

                # Get structural target
                tgt, rr, method = ot.structure_target(
                    df_slice, stp["direction"], stp["entry"], stp["raw_stop"], atr_v
                )

                if method == "no_target" or tgt == 0:
                    continue

                # Conviction score
                clean_path = abs(tgt - stp["entry"]) / max(1e-9, atr_v)
                conv, tier, breakdown = ot.conviction_score(
                    stp, trend, df_slice, df_htf_slice,
                    False,  # no news in backtest
                    adx_v, rsi_v, vol_ratio, clean_path
                )

                # Apply market-specific conviction bonuses
                extra = cfg.extra_conviction_factors(
                    df_slice, df_htf_slice, stp, trend, adx_v, rsi_v
                )
                conv = max(0, min(100, conv + sum(extra.values())))
                if conv >= 80:
                    tier = "HIGH"
                elif conv >= 65:
                    tier = "MEDIUM"
                elif conv >= 50:
                    tier = "LOW"
                else:
                    tier = "REJECT"

                # Dynamic R:R minimum (same as live bot)
                if conv >= 80:
                    tier_min_rr = 1.5
                elif conv >= 65:
                    tier_min_rr = 2.0
                else:
                    tier_min_rr = 2.5
                min_rr = max(tier_min_rr, cfg.MIN_RR)
                if rr < min_rr:
                    continue

                if tier == "REJECT" or conv < min_conviction:
                    continue

                # Check outcome by walking forward
                outcome = check_trade_outcome(
                    df_entry, bar_idx, stp["direction"],
                    stp["entry"], stp["raw_stop"], tgt
                )

                # Get the hour (in UTC)
                try:
                    hour = current_ts.hour if hasattr(current_ts, "hour") else 0
                except Exception:
                    hour = 0

                # Get day of week
                try:
                    dow = current_ts.dayofweek if hasattr(current_ts, "dayofweek") else 0
                except Exception:
                    dow = 0

                trade = {
                    "market": market,
                    "tf": entry_tf,
                    "setup_type": stp["type"],
                    "direction": stp["direction"],
                    "entry": round(stp["entry"], 4),
                    "stop": round(stp["raw_stop"], 4),
                    "target": round(tgt, 4),
                    "rr": round(rr, 2),
                    "method": method,
                    "conviction": conv,
                    "tier": tier,
                    "trend": trend,
                    "adx": round(adx_v, 1),
                    "rsi": round(rsi_v, 1),
                    "vol_ratio": round(vol_ratio, 2),
                    "htf_bias": htf_bias,
                    "hour_utc": hour,
                    "day_of_week": dow,
                    "result": outcome["result"],
                    "bars_to_resolution": outcome["bars"],
                    "exit_price": outcome["exit_price"],
                    "timestamp": str(current_ts),
                }
                trades.append(trade)

                # Set cooldown
                cooldowns[cd_key] = bar_idx

    return trades


# ── Report builder ────────────────────────────────────────────────

DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

def build_report(trades: list, market: str, days: int) -> str:
    """Builds a comprehensive text report from backtest trades."""
    lines = []
    lines.append("=" * 60)
    lines.append(f"  NQ CALLS BACKTEST REPORT")
    lines.append(f"  Market: {market}  |  Period: {days} days")
    lines.append(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("=" * 60)
    lines.append("")

    if not trades:
        lines.append("No trades found in this period.")
        lines.append("This could mean:")
        lines.append("  - Not enough data downloaded")
        lines.append("  - All setups were filtered out by conviction/ADX/RR")
        lines.append("  - The market was too choppy for setups to form")
        return "\n".join(lines)

    # Filter out EXPIRED for win rate calculations
    resolved = [t for t in trades if t["result"] in ("WIN", "LOSS")]
    wins = [t for t in resolved if t["result"] == "WIN"]
    losses = [t for t in resolved if t["result"] == "LOSS"]
    expired = [t for t in trades if t["result"] == "EXPIRED"]

    total = len(trades)
    total_resolved = len(resolved)
    win_count = len(wins)
    loss_count = len(losses)
    wr = round(win_count / max(1, total_resolved) * 100, 1)

    # Average bars to resolution
    avg_bars_win = round(np.mean([t["bars_to_resolution"] for t in wins]), 1) if wins else 0
    avg_bars_loss = round(np.mean([t["bars_to_resolution"] for t in losses]), 1) if losses else 0

    # Average RR
    avg_rr = round(np.mean([t["rr"] for t in trades]), 2)
    avg_rr_win = round(np.mean([t["rr"] for t in wins]), 2) if wins else 0

    # Expectancy: (WR * avg_win_RR) - ((1-WR) * 1.0)
    if total_resolved > 0:
        wr_dec = win_count / total_resolved
        expectancy = round(wr_dec * avg_rr_win - (1 - wr_dec) * 1.0, 2)
    else:
        expectancy = 0

    lines.append("OVERALL RESULTS")
    lines.append("-" * 40)
    lines.append(f"  Total setups fired:     {total}")
    lines.append(f"  Resolved (W/L):         {total_resolved}")
    lines.append(f"  Expired (no hit):       {len(expired)}")
    lines.append(f"  Wins:                   {win_count}")
    lines.append(f"  Losses:                 {loss_count}")
    lines.append(f"  Win Rate:               {wr}%")
    lines.append(f"  Avg R:R:                {avg_rr}")
    lines.append(f"  Avg R:R (wins only):    {avg_rr_win}")
    lines.append(f"  Expectancy per trade:   {expectancy}R")
    lines.append(f"  Avg bars to win:        {avg_bars_win}")
    lines.append(f"  Avg bars to loss:       {avg_bars_loss}")
    lines.append("")

    # ── By Setup Type ──
    lines.append("BY SETUP TYPE")
    lines.append("-" * 40)
    setup_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "expired": 0, "rr_sum": 0})
    for t in trades:
        key = t["setup_type"]
        if t["result"] == "WIN":
            setup_stats[key]["wins"] += 1
            setup_stats[key]["rr_sum"] += t["rr"]
        elif t["result"] == "LOSS":
            setup_stats[key]["losses"] += 1
        else:
            setup_stats[key]["expired"] += 1

    for setup, s in sorted(setup_stats.items(), key=lambda x: x[1]["wins"] + x[1]["losses"], reverse=True):
        w, l = s["wins"], s["losses"]
        total_s = w + l
        swr = round(w / max(1, total_s) * 100, 1)
        avg_win_rr = round(s["rr_sum"] / max(1, w), 2) if w > 0 else 0
        icon = "+" if swr >= 55 else "-" if swr < 45 else "~"
        lines.append(f"  [{icon}] {setup:25s}  {w}W/{l}L  ({swr}% WR)  avg win RR: {avg_win_rr}")
    lines.append("")

    # ── By Conviction Tier ──
    lines.append("BY CONVICTION TIER")
    lines.append("-" * 40)
    for tier in ["HIGH", "MEDIUM", "LOW"]:
        tier_trades = [t for t in resolved if t["tier"] == tier]
        tw = sum(1 for t in tier_trades if t["result"] == "WIN")
        tl = sum(1 for t in tier_trades if t["result"] == "LOSS")
        tt = tw + tl
        twr = round(tw / max(1, tt) * 100, 1)
        avg_conv = round(np.mean([t["conviction"] for t in tier_trades]), 0) if tier_trades else 0
        icon = {"HIGH": "H", "MEDIUM": "M", "LOW": "L"}[tier]
        lines.append(f"  [{icon}] {tier:8s}  {tw}W/{tl}L  ({twr}% WR)  avg conv: {int(avg_conv)}  total: {tt}")
    lines.append("")

    # ── By Hour of Day (UTC) ──
    lines.append("BY HOUR OF DAY (UTC)")
    lines.append("-" * 40)
    hour_stats = defaultdict(lambda: {"wins": 0, "losses": 0})
    for t in resolved:
        h = t["hour_utc"]
        if t["result"] == "WIN":
            hour_stats[h]["wins"] += 1
        else:
            hour_stats[h]["losses"] += 1

    for h in sorted(hour_stats.keys()):
        s = hour_stats[h]
        w, l = s["wins"], s["losses"]
        total_h = w + l
        hwr = round(w / max(1, total_h) * 100, 1)
        bar = "#" * int(hwr / 5)
        lines.append(f"  {h:02d}:00  {w:3d}W/{l:3d}L  ({hwr:5.1f}% WR)  {bar}")
    lines.append("")

    # ── By Day of Week ──
    lines.append("BY DAY OF WEEK")
    lines.append("-" * 40)
    dow_stats = defaultdict(lambda: {"wins": 0, "losses": 0})
    for t in resolved:
        d = t["day_of_week"]
        if t["result"] == "WIN":
            dow_stats[d]["wins"] += 1
        else:
            dow_stats[d]["losses"] += 1

    for d in sorted(dow_stats.keys()):
        s = dow_stats[d]
        w, l = s["wins"], s["losses"]
        total_d = w + l
        dwr = round(w / max(1, total_d) * 100, 1)
        lines.append(f"  {DOW_NAMES[d]:3s}  {w:3d}W/{l:3d}L  ({dwr:5.1f}% WR)")
    lines.append("")

    # ── By Direction ──
    lines.append("BY DIRECTION")
    lines.append("-" * 40)
    for direction in ["LONG", "SHORT"]:
        dir_trades = [t for t in resolved if direction in t["direction"]]
        dw = sum(1 for t in dir_trades if t["result"] == "WIN")
        dl = sum(1 for t in dir_trades if t["result"] == "LOSS")
        dt = dw + dl
        dwr = round(dw / max(1, dt) * 100, 1)
        lines.append(f"  {direction:6s}  {dw}W/{dl}L  ({dwr}% WR)")
    lines.append("")

    # ── By Trend Score Range ──
    lines.append("BY TREND STRENGTH")
    lines.append("-" * 40)
    trend_bins = [
        ("Strong bear (-10 to -5)", lambda t: t["trend"] <= -5),
        ("Weak bear (-4 to -1)",    lambda t: -4 <= t["trend"] <= -1),
        ("Neutral (0)",             lambda t: t["trend"] == 0),
        ("Weak bull (+1 to +4)",    lambda t: 1 <= t["trend"] <= 4),
        ("Strong bull (+5 to +10)", lambda t: t["trend"] >= 5),
    ]
    for label, filt in trend_bins:
        bin_trades = [t for t in resolved if filt(t)]
        bw = sum(1 for t in bin_trades if t["result"] == "WIN")
        bl = sum(1 for t in bin_trades if t["result"] == "LOSS")
        bt = bw + bl
        bwr = round(bw / max(1, bt) * 100, 1) if bt > 0 else 0
        lines.append(f"  {label:30s}  {bw:3d}W/{bl:3d}L  ({bwr:5.1f}% WR)")
    lines.append("")

    # ── By ADX Range ──
    lines.append("BY ADX RANGE")
    lines.append("-" * 40)
    adx_bins = [(10, 15), (15, 20), (20, 25), (25, 30), (30, 40), (40, 100)]
    for lo, hi in adx_bins:
        bin_trades = [t for t in resolved if lo <= t["adx"] < hi]
        bw = sum(1 for t in bin_trades if t["result"] == "WIN")
        bl = sum(1 for t in bin_trades if t["result"] == "LOSS")
        bt = bw + bl
        bwr = round(bw / max(1, bt) * 100, 1) if bt > 0 else 0
        if bt > 0:
            lines.append(f"  ADX {lo:2d}-{hi:2d}:  {bw:3d}W/{bl:3d}L  ({bwr:5.1f}% WR)")
    lines.append("")

    # ── Best & Worst Combos ──
    lines.append("TOP 5 BEST SETUP COMBOS (market:setup:tf)")
    lines.append("-" * 40)
    combo_stats = defaultdict(lambda: {"wins": 0, "losses": 0})
    for t in resolved:
        key = f"{t['market']}:{t['setup_type']}:{t['tf']}"
        if t["result"] == "WIN":
            combo_stats[key]["wins"] += 1
        else:
            combo_stats[key]["losses"] += 1

    sorted_combos = sorted(combo_stats.items(),
                           key=lambda x: x[1]["wins"] / max(1, x[1]["wins"] + x[1]["losses"]),
                           reverse=True)
    for key, s in sorted_combos[:5]:
        w, l = s["wins"], s["losses"]
        cwr = round(w / max(1, w + l) * 100, 1)
        lines.append(f"  {key:40s}  {w}W/{l}L  ({cwr}% WR)")

    lines.append("")
    lines.append("TOP 5 WORST SETUP COMBOS")
    lines.append("-" * 40)
    # Filter to combos with at least 3 trades
    worst = [(k, s) for k, s in sorted_combos if s["wins"] + s["losses"] >= 3]
    worst.sort(key=lambda x: x[1]["wins"] / max(1, x[1]["wins"] + x[1]["losses"]))
    for key, s in worst[:5]:
        w, l = s["wins"], s["losses"]
        cwr = round(w / max(1, w + l) * 100, 1)
        lines.append(f"  {key:40s}  {w}W/{l}L  ({cwr}% WR)")
    lines.append("")

    # ── Key Insights ──
    lines.append("KEY INSIGHTS")
    lines.append("-" * 40)

    if wr >= 55:
        lines.append(f"  [+] Overall win rate {wr}% is strong")
    elif wr < 45:
        lines.append(f"  [-] Overall win rate {wr}% needs improvement")

    if expectancy > 0.5:
        lines.append(f"  [+] Positive expectancy ({expectancy}R per trade)")
    elif expectancy < 0:
        lines.append(f"  [-] Negative expectancy ({expectancy}R per trade) — review filters")

    # Find best setup
    if setup_stats:
        best_setup = max(setup_stats.items(),
                         key=lambda x: x[1]["wins"] / max(1, x[1]["wins"] + x[1]["losses"]))
        bw, bl = best_setup[1]["wins"], best_setup[1]["losses"]
        bwr = round(bw / max(1, bw + bl) * 100, 1)
        if bw + bl >= 3:
            lines.append(f"  [+] Best setup: {best_setup[0]} ({bwr}% WR over {bw+bl} trades)")

    # Find worst setup
    if setup_stats:
        worst_setup_list = [(k, v) for k, v in setup_stats.items() if v["wins"] + v["losses"] >= 3]
        if worst_setup_list:
            worst_setup = min(worst_setup_list,
                              key=lambda x: x[1]["wins"] / max(1, x[1]["wins"] + x[1]["losses"]))
            ww, wl = worst_setup[1]["wins"], worst_setup[1]["losses"]
            wwr = round(ww / max(1, ww + wl) * 100, 1)
            if wwr < 45:
                lines.append(f"  [-] Worst setup: {worst_setup[0]} ({wwr}% WR) — consider raising filters")

    # Best hour
    if hour_stats:
        best_hour = max(hour_stats.items(),
                        key=lambda x: x[1]["wins"] / max(1, x[1]["wins"] + x[1]["losses"]))
        hw, hl = best_hour[1]["wins"], best_hour[1]["losses"]
        hwr = round(hw / max(1, hw + hl) * 100, 1)
        if hw + hl >= 3:
            lines.append(f"  [+] Best hour: {best_hour[0]:02d}:00 UTC ({hwr}% WR)")

    # HIGH tier performance
    high_trades = [t for t in resolved if t["tier"] == "HIGH"]
    if high_trades:
        hw = sum(1 for t in high_trades if t["result"] == "WIN")
        hl = len(high_trades) - hw
        hwr = round(hw / max(1, len(high_trades)) * 100, 1)
        if hwr >= 60:
            lines.append(f"  [+] HIGH conviction trades at {hwr}% WR — trust these")

    lines.append("")
    lines.append("=" * 60)
    lines.append("  END OF BACKTEST REPORT")
    lines.append(f"  Paste to Claude to review and update strategy files.")
    lines.append("=" * 60)

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="NQ CALLS Backtester")
    parser.add_argument("--market", type=str, help="Market to backtest (NQ, GC, BTC, SOL)")
    parser.add_argument("--all", action="store_true", help="Backtest all markets")
    parser.add_argument("--days", type=int, default=90, help="Days of history (default: 90)")
    parser.add_argument("--min-conviction", type=int, default=50, help="Min conviction to fire (default: 50)")
    parser.add_argument("--save-csv", action="store_true", help="Save all trades to data/backtest_results.csv")
    args = parser.parse_args()

    if not args.market and not args.all:
        parser.error("Specify --market or --all")

    markets = get_all_markets() if args.all else [args.market.upper()]

    all_trades = []

    for market in markets:
        if market not in YF_MAP:
            print(f"Unknown market: {market}")
            continue

        print(f"\n{'='*50}")
        print(f"BACKTESTING {market} — {args.days} days")
        print(f"{'='*50}")

        frames = download_data(market, args.days)
        if not frames:
            print(f"  No data available for {market}")
            continue

        trades = run_backtest(market, frames, args.min_conviction)
        all_trades.extend(trades)

        print(f"\n  Found {len(trades)} trades")

        # Print report for this market
        report = build_report(trades, market, args.days)
        print(f"\n{report}")

    # If --all, also print combined report
    if args.all and len(markets) > 1:
        print(f"\n{'='*60}")
        print(f"  COMBINED REPORT — ALL MARKETS")
        print(f"{'='*60}")
        report = build_report(all_trades, "ALL", args.days)
        print(report)

    # Save reports
    report_dir = os.path.join(_BASE_DIR, "data")
    os.makedirs(report_dir, exist_ok=True)

    # Save text report
    market_label = "ALL" if args.all else markets[0]
    report_file = os.path.join(report_dir, f"backtest_{market_label}_{args.days}d.txt")
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(build_report(all_trades, market_label, args.days))
    print(f"\nReport saved to: {report_file}")

    # Save CSV if requested
    if args.save_csv and all_trades:
        csv_file = os.path.join(report_dir, "backtest_results.csv")
        cols = list(all_trades[0].keys())
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for t in all_trades:
                w.writerow(t)
        print(f"CSV saved to: {csv_file}")


if __name__ == "__main__":
    main()
