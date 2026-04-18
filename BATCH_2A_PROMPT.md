# BATCH 2A — Save Everything + Give Real Reasons (Final)

## Mission

The bot currently logs scan decisions to `data/strategy_log.csv` but the data is sparse. We need to see **every single thing the bot is thinking**: every setup it detects, every indicator value at that moment, every scoring factor, every reason for every decision — in one unified log. This is pure observability. The bot's trading behavior MUST NOT change.

After this batch, every row in `strategy_log.csv` will tell a complete story: "On April 18 at 09:32 ET, the bot scanned NQ on the 15m timeframe. It saw a VWAP bounce setup at 19500.25 with ADX 24, RSI 54, Bollinger Bands showing price in the lower 20% of the range, Stochastic at 28 (crossing up), MACD histogram turning positive. The conviction score was 68 (MEDIUM), breakdown: trend +8, HTF +10, volume +5, RSI +10, clean_path +15, time_of_day +5, learning +6, base 30. The bot FIRED the alert because conviction cleared the 65 minimum and RR of 2.8 was above the 2.0 MEDIUM minimum."

That's the level of detail we're adding. No more mystery.

## Hard Rules

- **DO NOT** change any filter threshold (ADX mins, MIN_CONVICTION, MIN_RR, tier cutoffs)
- **DO NOT** add any new setup types to `detect_setups()`
- **DO NOT** modify any sim_account.py logic
- **DO NOT** change the format of Telegram alerts (that's Batch 2C)
- **DO NOT** touch the suspension system, zone lockout, family cooldown, or any Batch 1 feature
- **DO NOT** change scanner persistence, session rollover, or archiving
- **DO NOT** add backtesting (that's Batch 2B)
- **DO NOT** rename or reorder any existing CSV columns — strictly append new ones at the end

## Before You Start

1. Read these files fully. Do not guess at any interface:
   - `bot.py` (focus: scan_market, _post_init)
   - `outcome_tracker.py` (focus: detect_setups, conviction_score, auto_check_outcomes, CSV_COLS)
   - `strategy_log.py` (entire file)
   - `regime_classifier.py` (note: it uses lowercase `"close"`, `"high"`, `"low"` column names — bot uses `"Close"`, `"High"`, `"Low"`. The classify_regime call in detect_setups works because the frame has both? Verify by actually reading regime_classifier.py — if it's broken, we inherit that as-is, we do not fix it in this batch)
   - `markets/market_NQ.py`, `markets/market_GC.py`, `markets/market_BTC.py`, `markets/market_SOL.py` (ADX_MIN_BY_SETUP, MIN_CONVICTION, etc.)
   - `dashboard.py` (uses strategy_log.csv — must stay compatible)
   - `data_layer.py` (get_frames)
   - `session_clock.py` (get_session_date)

2. Before writing code, note these existing call sites that will need to keep working unchanged:
   - Every `sl.log_scan_decision(...)` call in `bot.py` (there are ~10 of them in scan_market)
   - `sl.check_missed_setups()` called from `scan_loop`
   - `sl.build_strategy_analysis()` called from `/analyze` command
   - `cmd_rejected()` which reads strategy_log.csv directly and filters by `decision` column
   - `dashboard.load_strategy_log()` which reads the whole file

═══════════════════════════════════════════════════════════════════
TASK 1 — EXPAND strategy_log.csv SCHEMA (backward-compatible)
═══════════════════════════════════════════════════════════════════

### 1a. Update COLS in strategy_log.py

Change the COLS list to this EXACT ordering. Do not reorder existing columns. Only append new ones at the end:

```python
COLS = [
    # ── EXISTING — KEEP THIS ORDER EXACTLY ──
    "timestamp", "market", "tf", "setup_type", "direction",
    "price", "entry", "stop", "target", "rr",
    "conviction", "tier", "trend", "adx", "rsi", "vol_ratio",
    "htf_bias", "news_flag", "decision", "reject_reason",
    "result", "result_checked_at",
    # ── NEW: scoring transparency ──
    "score_breakdown",       # JSON dict of conviction factors and their points
    "confidence_factors",    # JSON dict: BB position, Stoch signal, MACD signal, etc.
    "detection_reason",      # Human-readable sentence explaining what the bot saw
    # ── NEW: indicator snapshot at decision time ──
    "atr", "vwap", "ema20", "ema50", "ema200", "ema21",
    "bb_upper", "bb_middle", "bb_lower", "bb_width_pct",
    "stoch_k", "stoch_d", "macd_line", "macd_signal", "macd_hist",
    # ── NEW: market context ──
    "close_price", "regime", "session_name",
    "swing_high_30", "swing_low_30", "volume_raw", "volume_20ma",
]
```

### 1b. Add new decision constants in strategy_log.py

Add these below the existing DECISION_* constants:

```python
DECISION_DETECTED    = "DETECTED"     # raw detection before any filter
DECISION_CLOSED_WIN  = "CLOSED_WIN"   # trade closed as a win
DECISION_CLOSED_LOSS = "CLOSED_LOSS"  # trade closed as a loss
```

### 1c. Migrate the existing CSV file on first run

In `_ensure_csv()`, handle the case where strategy_log.csv exists with the OLD 22-column schema. We must NOT lose the existing 3,385 rows of rejection data.

Replace `_ensure_csv` with:

```python
def _ensure_csv():
    """
    Ensure strategy_log.csv exists with the current COLS schema.
    If an old-schema file exists (22 cols), migrate it in place by adding
    empty values for the new columns. Never loses data.
    """
    if not os.path.exists(STRATEGY_LOG):
        with open(STRATEGY_LOG, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=COLS).writeheader()
        return

    # File exists — check if header matches current COLS
    try:
        with open(STRATEGY_LOG, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            existing_header = next(reader, [])
    except Exception:
        existing_header = []

    if existing_header == COLS:
        return  # already migrated

    # Migration needed. Back up, then rewrite with new schema.
    backup_path = STRATEGY_LOG + ".pre_batch2a.bak"
    try:
        import shutil
        shutil.copy2(STRATEGY_LOG, backup_path)
    except Exception:
        pass

    try:
        with open(STRATEGY_LOG, newline="", encoding="utf-8") as f:
            old_rows = list(csv.DictReader(f))
    except Exception:
        old_rows = []

    with open(STRATEGY_LOG, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLS)
        writer.writeheader()
        for row in old_rows:
            # Fill missing columns with empty string
            clean = {k: row.get(k, "") for k in COLS}
            writer.writerow(clean)
```

Call `_ensure_csv()` once at module import time (at the bottom of strategy_log.py). This way the migration happens automatically on the first startup after deploy.

### 1d. Update log_scan_decision signature (backward compatible)

The current signature has 19 positional args plus `reject_reason`. Every existing caller in bot.py uses positional args. Do NOT break them.

New signature:

```python
def log_scan_decision(
    market: str, tf: str, setup_type: str, direction: str,
    price: float, entry: float, stop: float, target: float, rr: float,
    conviction: int, tier: str, trend: int,
    adx: float, rsi: float, vol_ratio: float,
    htf_bias: str, news_flag: bool,
    decision: str, reject_reason: str = "",
    # ── NEW optional keyword-only params ──
    *,
    context: Optional[dict] = None,
    detection_reason: str = "",
    score_breakdown: Optional[dict] = None,
    confidence_factors: Optional[dict] = None,
    result: str = "",
) -> str:
```

The `*` before the new params makes them keyword-only, which means existing positional calls still work. The `context` dict holds all the new indicator values (see Task 3 for exact keys).

Inside the function, build the row like this:

```python
row = {
    "timestamp":         datetime.now(timezone.utc).isoformat(),
    "market":            market,
    "tf":                tf,
    "setup_type":        setup_type,
    "direction":         direction,
    "price":             round(float(price), 4)    if price     not in ("", None) else "",
    "entry":             round(float(entry), 4)    if entry     not in ("", None) else "",
    "stop":              round(float(stop), 4)     if stop      not in ("", None) else "",
    "target":            round(float(target), 4)   if target    not in ("", None) else "",
    "rr":                round(float(rr), 2)       if rr        not in ("", None) else "",
    "conviction":        conviction,
    "tier":              tier,
    "trend":             trend,
    "adx":               round(float(adx), 1)      if adx       not in ("", None) else "",
    "rsi":               round(float(rsi), 1)      if rsi       not in ("", None) else "",
    "vol_ratio":         round(float(vol_ratio),2) if vol_ratio not in ("", None) else "",
    "htf_bias":          htf_bias,
    "news_flag":         int(bool(news_flag)),
    "decision":          decision,
    "reject_reason":     reject_reason or "",
    "result":            result or "",
    "result_checked_at": "",
    # NEW fields
    "score_breakdown":     json.dumps(score_breakdown, default=str)    if score_breakdown    else "",
    "confidence_factors":  json.dumps(confidence_factors, default=str) if confidence_factors else "",
    "detection_reason":    detection_reason or "",
}

# Pull indicator snapshot from context (all optional)
ctx = context or {}
for key in ("atr", "vwap", "ema20", "ema50", "ema200", "ema21",
            "bb_upper", "bb_middle", "bb_lower", "bb_width_pct",
            "stoch_k", "stoch_d", "macd_line", "macd_signal", "macd_hist",
            "close_price", "regime", "session_name",
            "swing_high_30", "swing_low_30", "volume_raw", "volume_20ma"):
    val = ctx.get(key, "")
    if isinstance(val, float):
        row[key] = round(val, 4)
    else:
        row[key] = val if val not in (None,) else ""

# Ensure every COLS key is present
for k in COLS:
    row.setdefault(k, "")

with open(STRATEGY_LOG, "a", newline="", encoding="utf-8") as f:
    csv.DictWriter(f, fieldnames=COLS).writerow(row)

return row["timestamp"]
```

Add `import json` at the top of strategy_log.py if not already imported. Add `from typing import Optional` too.

### 1e. Fix check_missed_setups for new column

In `check_missed_setups()`, there's a line that rewrites the entire CSV. That still works with DictWriter, but confirm it uses `fieldnames=COLS` so it gets all the new columns. It already does — good. No change needed, just verify.

═══════════════════════════════════════════════════════════════════
TASK 2 — ADD BOLLINGER BANDS, STOCHASTIC, MACD INDICATORS
═══════════════════════════════════════════════════════════════════

In `outcome_tracker.py`, find the "Indicators" section (right after `_performance_bonus`/`get_learning_summary` — it starts with the comment `# Indicators`). After the existing `vwap()` function, add these three new functions:

```python
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
```

**Critical**: Do NOT call these new functions from `detect_setups()` or `conviction_score()` in this batch. They are for logging only. The existing detection and scoring logic stays 100% untouched.

═══════════════════════════════════════════════════════════════════
TASK 3 — BUILD snapshot_context ONCE PER SCAN
═══════════════════════════════════════════════════════════════════

In `bot.py`, inside `scan_market()`, we need to build a single `snapshot_context` dict that gets attached to every log_scan_decision call in that scan pass.

Find the block that starts with:
```python
adx_v    = float(ot.adx(df_e).iloc[-1])
rsi_v    = float(ot.rsi(df_e["Close"]).iloc[-1])
atr_v    = float(ot.atr(df_e).iloc[-1])
vol_mean = float(df_e["Volume"].rolling(20).mean().iloc[-1]) if len(df_e)>=20 else None
vol_last = float(df_e["Volume"].iloc[-1])
vol_ratio= (vol_last / max(1e-9, vol_mean)) if (vol_mean and vol_mean > 0) else 0.0
cur_price= float(df_e["Close"].iloc[-1])
```

Right AFTER that block (still inside the `for entry_tf in cfg.ENTRY_TIMEFRAMES:` loop, before the `if vol_mean is None` check), insert this:

```python
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
    # NOTE: classify_regime uses lowercase column names. Pass a DataFrame with
    # lowercase column names if that's the contract, otherwise pass df_e as-is.
    # We call it defensively — if it fails we default to UNKNOWN.
    regime_info = classify_regime(df_e)
    snapshot_context["regime"] = regime_info.get("regime", "UNKNOWN")
except Exception as e:
    log.debug(f"[{market}] Regime classify: {e}")
    snapshot_context["regime"] = "UNKNOWN"
```

Place this block so `snapshot_context` is available for the rest of `scan_market` (the volume-sanity skip, the per-setup loop, everything).

**Note about regime_classifier**: Read `regime_classifier.py`. If it uses lowercase `"close"` column names but our DataFrame has `"Close"`, the existing detect_setups call to it ALREADY handles that somehow (or it always fails silently). Do not attempt to fix regime_classifier in this batch. We only want to LOG whatever it returns (or "UNKNOWN" if it fails). The goal is observability; bugs in existing code are a Batch 2C concern.

═══════════════════════════════════════════════════════════════════
TASK 4 — LOG EVERY RAW DETECTION BEFORE ANY FILTER
═══════════════════════════════════════════════════════════════════

Currently, `detect_setups()` returns a list of setup dicts. Then `scan_market` loops through them and applies filters (suspension, ADX, cooldown, zone lockout, APPROACH trend gate, RR check, conviction check, drawdown gate). Only when a filter REJECTS something does it get logged. If a setup passes all filters and fires, it gets logged as FIRED. Many setups slip through without ANY log entry.

The new rule: **every setup returned by detect_setups() gets logged as DECISION_DETECTED before any filter runs.** This is a separate row from any subsequent REJECTED/ALMOST/FIRED row.

### 4a. Add helper function to bot.py

Add this helper function near the top of bot.py (right after `_md` or `format_alert` — somewhere logical, just before `scan_market`):

```python
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
        # 0 = at lower band, 1 = at upper band
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
```

### 4b. Log every raw detection in scan_market

Right after the line `setups = ot.detect_setups(df_e, df_h, htf_bias)` in scan_market (note: this is AFTER the ORB addition block; we want ALL setups, including ORB), add the detection-logging loop.

Important — there's an ORB addition block that appends to `setups` AFTER the initial `detect_setups()` call. We want to log those too. So place the detection loop AFTER the ORB block (right before `if not setups: ...`):

```python
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
```

═══════════════════════════════════════════════════════════════════
TASK 5 — EVERY EXISTING LOG CALL GETS CONTEXT + RICHER REASONS
═══════════════════════════════════════════════════════════════════

There are multiple calls to `sl.log_scan_decision(...)` in `scan_market()`. Every single one must pass the `context=snapshot_context` keyword argument. Several also deserve richer reject reasons.

### 5a. Update each existing log_scan_decision call

Here is the full list of existing call sites in scan_market and how each must change:

**Call 1 — volume-degraded skip:**
```python
# OLD:
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    sl.DECISION_REJECTED, f"No volume data")

# NEW:
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    sl.DECISION_REJECTED,
    f"Volume data degraded or zero (vol_mean={vol_mean}) — cannot assess setup quality",
    context=snapshot_context,
    detection_reason=f"{stp['type']} detected but skipped: volume data unreliable this scan")
```

**Call 2 — suspended setup shadow log:**
```python
# OLD:
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    sl.DECISION_SHADOW_SUSPENDED, "SUSPENDED — would-have-fired")

# NEW:
suspended_info = ot.get_suspended_setups().get(f"{market}:{stp['type']}", {})
reason_text = suspended_info.get("reason", "unknown")
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    sl.DECISION_SHADOW_SUSPENDED,
    f"Suspended due to {reason_text} — shadow-logged to track would-have-fired rate",
    context=snapshot_context,
    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio))
```

**Call 3 — ADX too low:**
```python
# OLD:
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    sl.DECISION_REJECTED, f"ADX {round(adx_v,1)} < {required_adx} for {stp['type']}")

# NEW:
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    sl.DECISION_REJECTED,
    f"ADX {round(adx_v,1)} below {stp['type']} minimum {required_adx} — market too choppy for this setup type",
    context=snapshot_context,
    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio))
```

**Call 4 — APPROACH_RESIST gate:**
```python
# OLD:
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    sl.DECISION_REJECTED, f"APPROACH_RESIST blocked: trend {trend:+d} not bearish")

# NEW:
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    sl.DECISION_REJECTED,
    f"APPROACH_RESIST requires trend <= -2 for bearish approach; current trend is {trend:+d}",
    context=snapshot_context,
    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio))
```

**Call 5 — APPROACH_SUPPORT gate:**
```python
# OLD:
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    sl.DECISION_REJECTED, f"APPROACH_SUPPORT blocked: trend {trend:+d} not bullish")

# NEW:
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    sl.DECISION_REJECTED,
    f"APPROACH_SUPPORT requires trend >= +2 for bullish approach; current trend is {trend:+d}",
    context=snapshot_context,
    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio))
```

**Call 6 — no target:**
```python
# OLD:
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    sl.DECISION_REJECTED, "No real swing target available")

# NEW:
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], 0, 0, 0, "REJECT",
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    sl.DECISION_REJECTED,
    "No real swing target available — nearest structural level too close for minimum R:R",
    context=snapshot_context,
    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio))
```

**Call 7 — RR too low:**
```python
# OLD:
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], tgt, rr, 0, "REJECT",
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    sl.DECISION_REJECTED, f"RR {round(rr,2)} < min {min_rr}")

# NEW:
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], tgt, rr, 0, "REJECT",
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    sl.DECISION_REJECTED,
    f"R:R {round(rr,2)} below minimum {min_rr} (tier quick-conv {quick_conv}, target {round(tgt,4)})",
    context=snapshot_context,
    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio))
```

**Call 8 — conviction too low (REJECTED or ALMOST):**
```python
# OLD:
decision = sl.DECISION_ALMOST if conv >= cfg.MIN_CONVICTION-10 else sl.DECISION_REJECTED
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    decision, f"Conv {conv} < min {cfg.MIN_CONVICTION}")

# NEW:
decision = sl.DECISION_ALMOST if conv >= cfg.MIN_CONVICTION-10 else sl.DECISION_REJECTED
# Extract conviction breakdown from the just-computed bd (see 5b below for capture)
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    decision,
    f"Conviction {conv} below {cfg.MIN_CONVICTION} minimum (tier={tier}); gap: {cfg.MIN_CONVICTION - conv} points" if decision == sl.DECISION_REJECTED else
    f"Conviction {conv} just short of {cfg.MIN_CONVICTION} minimum by {cfg.MIN_CONVICTION - conv} points — ALMOST",
    context=snapshot_context,
    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio),
    score_breakdown=bd_final,
    confidence_factors=_build_confidence_factors(snapshot_context, trend, adx_v, rsi_v))
```

**Call 9 — DD > 75%:**
```python
# OLD:
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    sl.DECISION_REJECTED, f"DD>75% needs conv 90+, got {conv}")

# NEW:
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    sl.DECISION_REJECTED,
    f"Daily drawdown {dd_pct:.0f}% requires conviction >=90, got {conv} — protecting account",
    context=snapshot_context,
    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio),
    score_breakdown=bd_final)
```

**Call 10 — DD > 50%:**
```python
# OLD:
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    sl.DECISION_REJECTED, f"DD>50% needs conv 80+, got {conv}")

# NEW:
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    sl.DECISION_REJECTED,
    f"Daily drawdown {dd_pct:.0f}% requires conviction >=80, got {conv} — cautious mode",
    context=snapshot_context,
    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio),
    score_breakdown=bd_final)
```

**Call 11 — FIRED:**
```python
# OLD:
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag, sl.DECISION_FIRED, "")

# NEW:
sl.log_scan_decision(market, entry_tf, stp["type"], stp["direction"],
    cur_price, stp["entry"], stp["raw_stop"], tgt, rr, conv, tier,
    trend, adx_v, rsi_v, vol_ratio, htf_bias, news_flag,
    sl.DECISION_FIRED, "",
    context=snapshot_context,
    detection_reason=_build_detection_reason(stp, snapshot_context, adx_v, rsi_v, vol_ratio),
    score_breakdown=bd_final,
    confidence_factors=_build_confidence_factors(snapshot_context, trend, adx_v, rsi_v))
```

### 5b. Capture the conviction breakdown dict

The current conviction_score calls throw away the `bd` dict (3rd return value). We need to keep it for logging.

Find this block:
```python
quick_conv, quick_tier, _ = ot.conviction_score(
    stp, trend, df_e, df_h, news_flag, adx_v, rsi_v, vol_ratio,
    abs(tgt-stp["entry"])/max(1e-9, atr_v)
)
```
Change the discarded third return to a variable:
```python
quick_conv, quick_tier, quick_bd = ot.conviction_score(
    stp, trend, df_e, df_h, news_flag, adx_v, rsi_v, vol_ratio,
    abs(tgt-stp["entry"])/max(1e-9, atr_v)
)
```

And find this block below it:
```python
conv, tier, _ = ot.conviction_score(stp, trend, df_e, df_h, news_flag, adx_v, rsi_v, vol_ratio, clean_path)
extra         = cfg.extra_conviction_factors(df_e, df_h, stp, trend, adx_v, rsi_v)
conv          = max(0, min(100, conv+sum(extra.values())))
```
Change to:
```python
conv, tier, bd_core = ot.conviction_score(stp, trend, df_e, df_h, news_flag, adx_v, rsi_v, vol_ratio, clean_path)
extra         = cfg.extra_conviction_factors(df_e, df_h, stp, trend, adx_v, rsi_v)
conv          = max(0, min(100, conv+sum(extra.values())))
# Merge core breakdown with market-specific extras for full transparency
bd_final = dict(bd_core)
for k, v in (extra or {}).items():
    bd_final[f"extra_{k}"] = v
bd_final["base"] = 30  # the starting base score in conviction_score
bd_final["final_score"] = conv
```

Now `bd_final` is available for all conviction-related log calls (8, 9, 10, 11).

═══════════════════════════════════════════════════════════════════
TASK 6 — LOG CLOSED-TRADE OUTCOMES TO strategy_log.csv
═══════════════════════════════════════════════════════════════════

When a trade closes as WIN or LOSS, we want a matching row in `strategy_log.csv` so one file tells the full story: detection → fire → outcome.

### 6a. Modify outcome_tracker.auto_check_outcomes

In `auto_check_outcomes()` in outcome_tracker.py, when a trade closes, we need to emit a strategy_log entry. But outcome_tracker.py should not depend on strategy_log directly at import time (circular risk). Instead, do a deferred import inside the function.

Find the block:
```python
if hit_target:
    update_result(alert_id, "WIN", 0, target)
    record_trade_result(market, setup_type, "WIN")
    closed_now.append({"alert_id": alert_id, "result": "WIN",
                       "market": market, "price": target})
elif hit_stop:
    update_result(alert_id, "LOSS", 0, stop)
    record_trade_result(market, setup_type, "LOSS")
    closed_now.append({"alert_id": alert_id, "result": "LOSS",
                       "market": market, "price": stop})
```

Change it to:

```python
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
```

### 6b. Add _log_trade_outcome helper in outcome_tracker.py

Add this as a new module-level function, placed right before `auto_check_outcomes`:

```python
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
    except Exception as e:
        import logging
        logging.getLogger("nqcalls").debug(f"_log_trade_outcome error: {e}")
```

Also add a matching `_log_trade_outcome` call in the manual `_mark` handler and the `force_flatten_futures` path in bot.py, so human-marked WINs/LOSSes ALSO get logged. Import it at the top of bot.py. Find the `_mark` function:

```python
async def _mark(u, result, args):
    # ... existing code up through:
    ot.update_result(match["alert_id"],result,0,exit_p)
    if result in ("WIN","LOSS"): ot.record_trade_result(match["market"],match["setup"],result)
    # ADD RIGHT HERE:
    if result in ("WIN","LOSS"):
        try:
            ot._log_trade_outcome(match, result, exit_p)
        except Exception:
            pass
    # ... rest of existing code
```

Same pattern inside `on_button` where there's a parallel `_mark`-like block for trade_win/trade_loss — add the `_log_trade_outcome` call there too.

And inside `force_flatten_futures` where it does `ot.update_result(...)` and `ot.record_trade_result(...)`, add the log call there too.

═══════════════════════════════════════════════════════════════════
TASK 7 — ADD /detections COMMAND
═══════════════════════════════════════════════════════════════════

Add this command handler in bot.py, alongside the other cmd_* functions (near cmd_rejected is a good spot):

```python
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
    recent = detected[-20:]

    if not recent:
        await u.message.reply_text(
            f"No recent detections{' for ' + market_filter if market_filter else ''}."
        )
        return

    # Build outcome lookup: for each DETECTED row, did a FIRED/REJECTED/ALMOST
    # row follow it for the same market+setup+tf within the next ~10 rows?
    detected_indices = [i for i, r in enumerate(rows) if r.get("decision") == "DETECTED"]

    lines = [
        f"🔭 *Last {len(recent)} Detections"
        f"{' — ' + market_filter if market_filter else ''}*",
        "━━━━━━━━━━━━━━━━━━",
    ]

    # We iterate over the TAIL (recent), but need their position in the
    # full rows list to find the outcome.
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
        bb_wp = det_row.get("bb_width_pct", "")

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

    # Telegram messages max ~4096 chars
    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:3900] + "\n\n_...truncated_"
    await u.message.reply_text(msg, parse_mode="Markdown")
```

Register it in main() alongside the other command handlers:

```python
# In the command registration list, add:
("detections", cmd_detections),
```

═══════════════════════════════════════════════════════════════════
TASK 8 — UPDATE STARTUP VERIFICATION MESSAGE
═══════════════════════════════════════════════════════════════════

In `_post_init()` in bot.py, find the existing Task 6 startup verification message block (the one that builds the `lines` list with commit SHA, scanner state, etc.).

Add a new section after the "Data: NQ ... | SOL ..." line, before the final "⚠️ Tap the Scanner button" line:

```python
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
```

═══════════════════════════════════════════════════════════════════
TASK 9 — /help TEXT UPDATE
═══════════════════════════════════════════════════════════════════

In `cmd_help()` in bot.py, add `/detections` to the commands list. Find the existing commands line:

```python
"`/stats` `/open` `/win` `/loss` `/skip` `/report` `/brief`\n"
"`/session` `/history [date]` `/lifetime`\n"
```

Add a new line below it:
```python
"`/stats` `/open` `/win` `/loss` `/skip` `/report` `/brief`\n"
"`/session` `/history [date]` `/lifetime`\n"
"`/rejected` `/detections [market]` — see what bot is thinking\n"
```

═══════════════════════════════════════════════════════════════════
TASK 10 — VERIFICATION STEPS
═══════════════════════════════════════════════════════════════════

### 10a. Syntax check

```bash
cd "C:\Users\wayne\Desktop\Trading bot"
python -c "import ast; [ast.parse(open(f, encoding='utf-8').read()) and print(f, 'OK') for f in ['bot.py', 'outcome_tracker.py', 'sim_account.py', 'strategy_log.py']]"
```

### 10b. Import smoke test

```bash
python -c "import strategy_log as sl; print('sl imports ok'); print('Constants:', sl.DECISION_FIRED, sl.DECISION_REJECTED, sl.DECISION_ALMOST, sl.DECISION_DETECTED, sl.DECISION_CLOSED_WIN, sl.DECISION_CLOSED_LOSS, sl.DECISION_SHADOW_SUSPENDED); print('Cols count:', len(sl.COLS))"

python -c "import outcome_tracker as ot; import pandas as pd; import numpy as np; s = pd.Series(np.random.randn(100).cumsum() + 100); u, m, l = ot.bollinger_bands(s); print('BB ok:', round(float(u.iloc[-1]),2), round(float(m.iloc[-1]),2), round(float(l.iloc[-1]),2)); df = pd.DataFrame({'High': s+1, 'Low': s-1, 'Close': s}); k, d = ot.stochastic(df); print('Stoch ok:', round(float(k.iloc[-1]),1), round(float(d.iloc[-1]),1)); ml, ms, mh = ot.macd(s); print('MACD ok:', round(float(ml.iloc[-1]),3), round(float(ms.iloc[-1]),3))"

python -c "import bot; print('bot imports ok')"
```

All three commands should exit with code 0 and print OK lines.

### 10c. CSV migration self-test

```bash
python -c "
import strategy_log as sl
import os
# Force ensure_csv to run and migrate
sl._ensure_csv()
# Read header and confirm all new cols present
import csv
with open(sl.STRATEGY_LOG, encoding='utf-8') as f:
    header = next(csv.reader(f))
expected_new = ['score_breakdown', 'confidence_factors', 'detection_reason',
                'bb_upper', 'bb_middle', 'bb_lower', 'bb_width_pct',
                'stoch_k', 'stoch_d', 'macd_line', 'macd_signal', 'macd_hist',
                'close_price', 'regime', 'session_name',
                'swing_high_30', 'swing_low_30', 'volume_raw', 'volume_20ma',
                'vwap', 'ema20', 'ema50', 'ema200', 'ema21', 'atr']
for col in expected_new:
    assert col in header, f'Missing column: {col}'
print(f'Schema OK: {len(header)} columns, all new columns present')
"
```

### 10d. Commit and push

```bash
git add -A
git commit -m "Batch 2A: save-everything observability

- Expanded strategy_log.csv schema with BB/Stoch/MACD indicators, EMAs,
  VWAP, regime, session, swings, volume, and full score breakdowns
- Added bollinger_bands, stochastic, macd indicator functions to outcome_tracker
- Every setup returned by detect_setups now logs a DETECTED row BEFORE filters
- Every existing log call now carries the full indicator snapshot and a
  human-readable detection_reason
- Conviction breakdown (bd) captured and logged for every FIRED/REJECTED
  /ALMOST decision
- Trade outcomes now log CLOSED_WIN/CLOSED_LOSS rows to strategy_log.csv
  (from auto_check_outcomes, manual /win /loss, and 4PM force-flatten)
- New /detections command shows last 20 detections with outcome
- Startup message now reports observability status
- CSV migration handles existing 22-col file without data loss
- No changes to detection logic, filter thresholds, sim P&L, or alert format"
git push origin main
```

═══════════════════════════════════════════════════════════════════
FINAL REPORT (send to user via chat, not Telegram)
═══════════════════════════════════════════════════════════════════

After deploying, provide this report:

1. **Files modified** (with line counts added/removed):
   - strategy_log.py — approx +XX/-XX
   - outcome_tracker.py — approx +XX/-XX
   - bot.py — approx +XX/-XX

2. **New functions**:
   - `outcome_tracker.bollinger_bands`
   - `outcome_tracker.stochastic`
   - `outcome_tracker.macd`
   - `outcome_tracker._log_trade_outcome`
   - `bot._build_detection_reason`
   - `bot._build_confidence_factors`
   - `bot.cmd_detections`

3. **New constants**:
   - `strategy_log.DECISION_DETECTED`
   - `strategy_log.DECISION_CLOSED_WIN`
   - `strategy_log.DECISION_CLOSED_LOSS`

4. **CSV schema changes**:
   - Old: 22 columns
   - New: 45 columns (or count the actual number)
   - Migration: existing rows preserved, new columns empty
   - Backup created at `data/strategy_log.csv.pre_batch2a.bak`

5. **Expected strategy_log.csv growth rate**:
   - Current (pre-2A): ~3,385 rows over 9 days ≈ 376 rows/day
   - Post-2A: roughly 2-3x that because every detection is now logged with its own row
   - Estimated: 750-1100 rows/day = ~150-220 KB/day with the new wider schema

6. **Verification output**: Paste the results of 10a, 10b, 10c above.

7. **Anything that didn't go as planned**: Any issues, deviations, or concerns.

8. **What to do in Telegram to verify live**:
   - Tap scanner button
   - After 1-2 scans, send `/detections` — should show recent DETECTED rows
   - Send `/rejected` — should still work (backward compat)
   - Send `/analyze` — should still work (reads strategy_log.csv)
   - Check startup message — should now show "Observability (Batch 2A)" section
