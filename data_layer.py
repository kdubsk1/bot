"""
data_layer.py  --  Unified data module for NQ CALLS trading bot.
Replaces fetch_yfinance, fetch_crypto, and get_frames from bot.py.
"""

import time
import logging
import requests as _requests
from datetime import datetime, timezone
from typing import Dict, Optional

import pandas as pd
import numpy as np

logger = logging.getLogger("nqcalls.data")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATABENTO_API_KEY: Optional[str] = None   # Set to enable Databento for NQ/GC
TWELVE_DATA_API_KEY = "fef556d1f9244c0f9db09cb9828c26a2"

# ---------------------------------------------------------------------------
# TopstepX (ProjectX Gateway API) — PRIMARY source for NQ/GC futures
# ---------------------------------------------------------------------------
# Wayne subscribed to TopstepX API Access ($14.50/mo with promo).
# Topstep support officially approved data-only use from Railway (2026-04-21).
# This integration is READ-ONLY — NEVER calls order/position/trade endpoints.
import os as _os

TOPSTEPX_API_BASE = "https://api.topstepx.com"

# Endpoint allowlist — ANY TopstepX POST must use one of these paths.
# Using _topstepx_post() ensures we can never accidentally hit a trade endpoint.
_TOPSTEPX_ALLOWED_ENDPOINTS = {
    "/api/Auth/loginKey",
    "/api/Contract/search",
    "/api/History/retrieveBars",
}

# Timeframe mapping: bot tf string -> (API unit, unitNumber)
_TOPSTEPX_TF_MAP = {
    "15m": (2, 15),
    "1h":  (3, 1),
    "4h":  (3, 4),
    "1d":  (4, 1),
}

# Bars to request per timeframe — mirrors _TD_BAR_COUNT
_TOPSTEPX_BAR_COUNT = {"15m": 500, "1h": 500, "4h": 500, "1d": 730}

# Symbol filter: match on symbolId (stable across contract rollovers)
_TOPSTEPX_SYMBOL_FILTER = {
    "NQ": "F.US.ENQ",   # E-mini NASDAQ-100 (NOT MNQ, NQG natgas, NQM crude)
    "GC": "F.US.GC",    # E-mini Gold      (NOT MGC micro)
}

# In-memory caches (reset on process restart — that's fine)
_TOPSTEPX_TOKEN = None
_TOPSTEPX_TOKEN_EXPIRY = None          # datetime in UTC
_TOPSTEPX_CONTRACT_CACHE = {}          # {"NQ": (contract_id, expiry_dt), ...}

CACHE_TTL = 60  # seconds

# yfinance interval -> (yf_interval, yf_period)
_YF_INTERVAL_MAP = {
    "15m": ("15m", "10d"),
    "1h":  ("60m", "60d"),
    "4h":  ("60m", "60d"),   # fetched as 60m then resampled
    "1d":  ("1d",  "2y"),
}

# Crypto exchange priority and symbol mapping
_CRYPTO_EXCHANGES = ["coinbase", "kraken", "bybit"]

_CRYPTO_SYMBOL_MAP = {
    "coinbase": {"BTC/USDT": "BTC/USD", "SOL/USDT": "SOL/USD"},
    "kraken":   {"BTC/USDT": "BTC/USD", "SOL/USDT": "SOL/USD"},
    "bybit":    {"BTC/USDT": "BTC/USDT", "SOL/USDT": "SOL/USDT"},
}

# ccxt timeframe mapping
_CCXT_TIMEFRAME_MAP = {
    "15m": "15m",
    "1h":  "1h",
    "4h":  "4h",
    "1d":  "1d",
}

# Markets that use crypto vs futures data
_CRYPTO_MARKETS = {"BTC", "SOL"}
_FUTURES_MARKETS = {"NQ", "GC"}

# yfinance ticker mapping
_YF_TICKER_MAP = {
    "NQ": "NQ=F",
    "GC": "GC=F",
}

MIN_BARS = 20

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
_cache: Dict[str, dict] = {}


def _cache_key(market: str, timeframe: str) -> str:
    return f"{market}|{timeframe}"


def _get_cached(market: str, timeframe: str) -> Optional[pd.DataFrame]:
    key = _cache_key(market, timeframe)
    entry = _cache.get(key)
    if entry is None:
        return None
    if time.time() - entry["ts"] > CACHE_TTL:
        return None
    logger.debug("Cache hit for %s %s", market, timeframe)
    return entry["df"].copy()


def _set_cache(market: str, timeframe: str, df: pd.DataFrame) -> None:
    key = _cache_key(market, timeframe)
    _cache[key] = {"df": df.copy(), "ts": time.time()}


def _get_stale_cache(market: str, timeframe: str) -> Optional[pd.DataFrame]:
    """Return cached data even if expired -- used as fallback on errors."""
    key = _cache_key(market, timeframe)
    entry = _cache.get(key)
    if entry is not None:
        logger.warning("Returning stale cache for %s %s", market, timeframe)
        return entry["df"].copy()
    return None


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------
_STANDARD_COLS = ["Open", "High", "Low", "Close", "Volume"]


def _normalise_df(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure DataFrame has standard OHLCV columns and a UTC DatetimeIndex."""
    if df is None or df.empty:
        return pd.DataFrame(columns=_STANDARD_COLS)

    # Handle MultiIndex columns produced by newer yfinance versions
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # Rename common variations
    col_map = {}
    for col in df.columns:
        cl = str(col).lower().strip()
        if cl == "open":
            col_map[col] = "Open"
        elif cl == "high":
            col_map[col] = "High"
        elif cl == "low":
            col_map[col] = "Low"
        elif cl == "close":
            col_map[col] = "Close"
        elif cl == "volume":
            col_map[col] = "Volume"
    if col_map:
        df = df.rename(columns=col_map)

    # Keep only standard columns
    for c in _STANDARD_COLS:
        if c not in df.columns:
            df[c] = np.nan
    df = df[_STANDARD_COLS].copy()

    # Ensure numeric
    for c in _STANDARD_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # Ensure UTC DatetimeIndex
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    df = df.sort_index()
    df = df.dropna(subset=["Close"])
    return df


def _resample_to_4h(df_60m: pd.DataFrame) -> pd.DataFrame:
    """Resample 60-minute bars to 4-hour bars."""
    if df_60m.empty:
        return df_60m
    resampled = df_60m.resample("4h").agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }).dropna(subset=["Close"])
    return resampled


# ---------------------------------------------------------------------------
# TopstepX fetcher (PRIMARY source for NQ / GC — real CME futures data)
# ---------------------------------------------------------------------------

def _topstepx_post(path: str, headers: dict, body: dict, timeout: int = 15):
    """
    Safe POST wrapper — enforces the TopstepX endpoint allowlist.
    Raises RuntimeError if `path` is not whitelisted. This prevents
    accidental calls to trade/order/position endpoints that would
    violate Wayne's Topstep TOS.
    """
    if path not in _TOPSTEPX_ALLOWED_ENDPOINTS:
        raise RuntimeError(
            f"TopstepX endpoint '{path}' not in allowlist — BUG, refusing to send. "
            f"Allowed: {sorted(_TOPSTEPX_ALLOWED_ENDPOINTS)}"
        )
    return _requests.post(
        TOPSTEPX_API_BASE + path,
        headers=headers,
        json=body,
        timeout=timeout,
    )


def _get_topstepx_token() -> Optional[str]:
    """
    Return a valid JWT session token, refreshing if needed.
    Tokens last 24h; we refresh when less than 5min remaining.
    Returns None if credentials are missing or auth fails — caller falls back.
    """
    global _TOPSTEPX_TOKEN, _TOPSTEPX_TOKEN_EXPIRY
    now = datetime.now(timezone.utc)

    if _TOPSTEPX_TOKEN and _TOPSTEPX_TOKEN_EXPIRY:
        if now < _TOPSTEPX_TOKEN_EXPIRY - pd.Timedelta(minutes=5):
            return _TOPSTEPX_TOKEN

    username = _os.environ.get("TOPSTEPX_USERNAME", "").strip()
    api_key  = _os.environ.get("TOPSTEPX_API_KEY", "").strip()
    if not username or not api_key:
        logger.warning("TopstepX creds not set (TOPSTEPX_USERNAME / TOPSTEPX_API_KEY) — skipping TopstepX fetch")
        return None

    try:
        resp = _topstepx_post(
            "/api/Auth/loginKey",
            headers={"Content-Type": "application/json", "accept": "text/plain"},
            body={"userName": username, "apiKey": api_key},
            timeout=10,
        )
    except Exception as exc:
        logger.error("TopstepX auth network error: %s", exc)
        return None

    if resp.status_code != 200:
        logger.error("TopstepX auth HTTP %d: %s", resp.status_code, resp.text[:200])
        return None

    try:
        data = resp.json()
    except Exception as exc:
        logger.error("TopstepX auth JSON parse: %s", exc)
        return None

    if not data.get("success") or not data.get("token"):
        logger.error("TopstepX auth failed: %s", data.get("errorMessage", "unknown"))
        return None

    _TOPSTEPX_TOKEN = data["token"]
    _TOPSTEPX_TOKEN_EXPIRY = now + pd.Timedelta(hours=24)
    logger.info("TopstepX authenticated successfully (token expires %s)", _TOPSTEPX_TOKEN_EXPIRY.isoformat())
    return _TOPSTEPX_TOKEN


def _invalidate_topstepx_token():
    """Force a re-auth on next call — used after a 401 response."""
    global _TOPSTEPX_TOKEN, _TOPSTEPX_TOKEN_EXPIRY
    _TOPSTEPX_TOKEN = None
    _TOPSTEPX_TOKEN_EXPIRY = None


def _get_topstepx_contract_id(market: str, token: str, force_refresh: bool = False) -> Optional[str]:
    """
    Return the active contractId (e.g. "CON.F.US.ENQ.M26") for NQ or GC.
    Caches 4h to minimize API calls. Filters by symbolId — stable across rollovers.
    """
    now = datetime.now(timezone.utc)
    cached = _TOPSTEPX_CONTRACT_CACHE.get(market)
    if cached and cached[1] > now and not force_refresh:
        return cached[0]

    sym_filter = _TOPSTEPX_SYMBOL_FILTER.get(market)
    if not sym_filter:
        logger.warning("TopstepX: no symbolId filter for market %s", market)
        return None

    try:
        resp = _topstepx_post(
            "/api/Contract/search",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
                "accept":        "text/plain",
            },
            body={"searchText": market, "live": True},
            timeout=10,
        )
    except Exception as exc:
        logger.warning("TopstepX contract search network error for %s: %s", market, exc)
        return None

    if resp.status_code != 200:
        logger.warning("TopstepX contract search HTTP %d for %s: %s", resp.status_code, market, resp.text[:200])
        return None

    try:
        data = resp.json()
    except Exception:
        return None

    if not data.get("success"):
        logger.warning("TopstepX contract search failed for %s: %s", market, data.get("errorMessage"))
        return None

    contracts = data.get("contracts", [])
    match = None

    # Apr 30 fix: loosened filter. The exact symbolId match was failing because
    # TopstepX's symbolId format may have shifted (e.g. F.US.ENQ.M26 vs F.US.ENQ).
    # Strategy: try exact match first, then partial (contains), then ANY active
    # contract whose symbolId starts with our prefix. Log what we actually got
    # so we can diagnose if all three fail.

    # Pass 1: exact match (original behaviour)
    for c in contracts:
        if c.get("activeContract") and c.get("symbolId") == sym_filter:
            match = c
            break

    # Pass 2: prefix match (handles symbolId like "F.US.ENQ.M26")
    if not match:
        for c in contracts:
            if c.get("activeContract") and str(c.get("symbolId", "")).startswith(sym_filter):
                match = c
                logger.info("TopstepX %s: matched via prefix — symbolId=%s", market, c.get("symbolId"))
                break

    # Pass 3: contains match (last resort — e.g. symbolId="CME:ENQ.M26" or similar)
    if not match:
        # market_clean: strip the F.US. prefix to get the bare symbol ("ENQ", "GC")
        bare = sym_filter.replace("F.US.", "")
        for c in contracts:
            sid = str(c.get("symbolId", ""))
            if c.get("activeContract") and bare in sid:
                match = c
                logger.info("TopstepX %s: matched via contains(%s) — symbolId=%s", market, bare, sid)
                break

    if not match:
        # Diagnostic: dump what we actually got back so Wayne can see why
        sample_ids = [str(c.get("symbolId", "")) for c in contracts[:10]]
        logger.warning("TopstepX: no active %s contract (tried symbolId=%s, %d results, sample IDs: %s)",
                       market, sym_filter, len(contracts), sample_ids)
        return None

    contract_id = match["id"]
    _TOPSTEPX_CONTRACT_CACHE[market] = (contract_id, now + pd.Timedelta(hours=4))
    logger.info("TopstepX %s active contract: %s (%s)", market, contract_id, match.get("description", ""))
    return contract_id


def _fetch_topstepx(market: str, timeframe: str) -> pd.DataFrame:
    """
    Primary fetch for NQ/GC — real CME futures data from TopstepX.
    Returns empty DataFrame on any failure (caller falls back to TwelveData → yfinance).

    Signature matches the existing _fetch_twelvedata / _fetch_yfinance convention
    (market, timeframe). Bar count is looked up from _TOPSTEPX_BAR_COUNT.
    """
    if market not in _TOPSTEPX_SYMBOL_FILTER:
        return pd.DataFrame(columns=_STANDARD_COLS)
    if timeframe not in _TOPSTEPX_TF_MAP:
        logger.warning("TopstepX: no tf mapping for %s", timeframe)
        return pd.DataFrame(columns=_STANDARD_COLS)

    token = _get_topstepx_token()
    if not token:
        return pd.DataFrame(columns=_STANDARD_COLS)

    contract_id = _get_topstepx_contract_id(market, token)
    if not contract_id:
        return pd.DataFrame(columns=_STANDARD_COLS)

    unit, unit_number = _TOPSTEPX_TF_MAP[timeframe]
    bars = _TOPSTEPX_BAR_COUNT.get(timeframe, 500)

    # Lookback: 2x the expected span to make sure we get enough bars
    # (accounts for weekends, market closures, partial bars)
    tf_minutes = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}[timeframe]
    lookback_minutes = tf_minutes * bars * 2
    now_utc = datetime.now(timezone.utc)
    start_time = (now_utc - pd.Timedelta(minutes=lookback_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_time   = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    body = {
        "contractId":        contract_id,
        "live":              True,
        "startTime":         start_time,
        "endTime":           end_time,
        "unit":              unit,
        "unitNumber":        unit_number,
        "limit":             bars,
        "includePartialBar": True,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "accept":        "text/plain",
    }

    def _do_post():
        return _topstepx_post("/api/History/retrieveBars", headers=headers, body=body, timeout=15)

    # Attempt with one retry on 401 (token expired) and one retry on 5xx (transient)
    try:
        resp = _do_post()
    except Exception as exc:
        logger.warning("TopstepX retrieveBars network error for %s %s: %s", market, timeframe, exc)
        return pd.DataFrame(columns=_STANDARD_COLS)

    if resp.status_code == 401:
        logger.info("TopstepX token expired — refreshing and retrying %s %s", market, timeframe)
        _invalidate_topstepx_token()
        new_token = _get_topstepx_token()
        if not new_token:
            return pd.DataFrame(columns=_STANDARD_COLS)
        headers["Authorization"] = f"Bearer {new_token}"
        try:
            resp = _do_post()
        except Exception as exc:
            logger.warning("TopstepX retry after 401 failed: %s", exc)
            return pd.DataFrame(columns=_STANDARD_COLS)

    if resp.status_code == 429:
        logger.warning("TopstepX RATE LIMITED for %s %s — backing off, using fallback", market, timeframe)
        return pd.DataFrame(columns=_STANDARD_COLS)

    if resp.status_code >= 500:
        # Single retry on server error with small sleep
        logger.warning("TopstepX HTTP %d for %s %s — retrying once", resp.status_code, market, timeframe)
        time.sleep(1.0)
        try:
            resp = _do_post()
        except Exception as exc:
            logger.warning("TopstepX 5xx retry failed: %s", exc)
            return pd.DataFrame(columns=_STANDARD_COLS)

    if resp.status_code != 200:
        logger.warning("TopstepX HTTP %d for %s %s: %s", resp.status_code, market, timeframe, resp.text[:200])
        return pd.DataFrame(columns=_STANDARD_COLS)

    try:
        data = resp.json()
    except Exception as exc:
        logger.warning("TopstepX JSON parse error for %s %s: %s", market, timeframe, exc)
        return pd.DataFrame(columns=_STANDARD_COLS)

    if not data.get("success"):
        logger.warning("TopstepX error for %s %s: %s", market, timeframe, data.get("errorMessage", "unknown"))
        return pd.DataFrame(columns=_STANDARD_COLS)

    bars_list = data.get("bars", [])
    if not bars_list:
        # Empty result — could mean contract expired / rolled over. Invalidate cache and retry once.
        logger.warning("TopstepX returned 0 bars for %s %s — invalidating contract cache and retrying", market, timeframe)
        _TOPSTEPX_CONTRACT_CACHE.pop(market, None)
        new_contract = _get_topstepx_contract_id(market, token, force_refresh=True)
        if new_contract and new_contract != contract_id:
            body["contractId"] = new_contract
            try:
                resp2 = _do_post()
                if resp2.status_code == 200:
                    data2 = resp2.json()
                    if data2.get("success"):
                        bars_list = data2.get("bars", [])
            except Exception:
                pass
        if not bars_list:
            return pd.DataFrame(columns=_STANDARD_COLS)

    # Normalize into bot's standard OHLCV shape
    df = pd.DataFrame(bars_list)
    df = df.rename(columns={"t": "timestamp", "o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp")
    df = _normalise_df(df)  # reuse the existing normaliser for safety

    # Mark source tracking
    fb_key = f"{market}|{timeframe}"
    _last_source[fb_key] = "topstepx"

    logger.info("TopstepX %s %s: fetched %d bars ✅", market, timeframe, len(df))
    return df


def probe_topstepx() -> dict:
    """
    Startup self-test for TopstepX connectivity.
    Returns dict describing what worked:
      {
        "auth":         bool,
        "nq_contract":  "CON.F.US.ENQ.M26" | None,
        "gc_contract":  "CON.F.US.GC.M26"  | None,
        "nq_bars_15m":  int,
        "gc_bars_15m":  int,
      }
    bot.py calls this during _post_init() and includes the result in the
    startup Telegram message so Wayne can confirm the feed is live.
    """
    result = {
        "auth":        False,
        "nq_contract": None,
        "gc_contract": None,
        "nq_bars_15m": 0,
        "gc_bars_15m": 0,
    }

    token = _get_topstepx_token()
    if not token:
        return result
    result["auth"] = True

    try:
        result["nq_contract"] = _get_topstepx_contract_id("NQ", token)
    except Exception as exc:
        logger.warning("probe_topstepx: NQ contract lookup error: %s", exc)

    try:
        result["gc_contract"] = _get_topstepx_contract_id("GC", token)
    except Exception as exc:
        logger.warning("probe_topstepx: GC contract lookup error: %s", exc)

    if result["nq_contract"]:
        try:
            df = _fetch_topstepx("NQ", "15m")
            result["nq_bars_15m"] = 0 if df is None else len(df)
        except Exception as exc:
            logger.warning("probe_topstepx: NQ 15m fetch error: %s", exc)

    if result["gc_contract"]:
        try:
            df = _fetch_topstepx("GC", "15m")
            result["gc_bars_15m"] = 0 if df is None else len(df)
        except Exception as exc:
            logger.warning("probe_topstepx: GC 15m fetch error: %s", exc)

    return result


# ---------------------------------------------------------------------------
# Twelve Data fetcher (FALLBACK 1 for NQ / GC)
# ---------------------------------------------------------------------------
# NQ symbol candidates — probe at startup to find what works
# TwelveData free tier: futures may not be supported. We try indexes/ETFs as
# proxies since NQ futures track NDX/QQQ closely.
_TD_NQ_CANDIDATES = [
    "NQ=F",     # yfinance-style continuous contract
    "NQ1!",     # TradingView-style continuous
    "NQM26",    # June 2026 contract
    "NDX",      # Nasdaq 100 index (very close proxy)
    "QQQ",      # Invesco QQQ ETF (close proxy)
    "NQ/USD",   # original (kept as fallback)
]

# GC symbol candidates — same pattern as NQ
_TD_GC_CANDIDATES = [
    "GC=F",     # yfinance-style
    "GC1!",     # TradingView continuous
    "GCM26",    # June 2026 contract
    "XAU/USD",  # spot gold forex (tracks futures closely)
    "GLD",      # SPDR Gold ETF (close proxy)
]

_TD_SYMBOL_MAP = {"NQ": "NQ/USD", "GC": "XAU/USD"}  # updated by probes at startup
_TD_TF_MAP     = {"15m": "15min", "1h": "1h", "4h": "4h", "1d": "1day"}
_TD_BAR_COUNT  = {"15m": 500, "1h": 500, "4h": 500, "1d": 730}

# Fallback counter: track consecutive failures per market|tf
_td_fallback_count: Dict[str, int] = {}
# Track which data source was last used per market|tf
_last_source: Dict[str, str] = {}


def probe_nq_symbol():
    """Try NQ symbol candidates on Twelve Data, set the one that works."""
    for candidate in _TD_NQ_CANDIDATES:
        try:
            resp = _requests.get(
                "https://api.twelvedata.com/time_series",
                params={
                    "symbol":     candidate,
                    "interval":   "1h",
                    "outputsize": 5,
                    "apikey":     TWELVE_DATA_API_KEY,
                    "format":     "JSON",
                },
                timeout=10,
            )
            data = resp.json()
            if data.get("status") != "error" and "values" in data and len(data["values"]) >= 3:
                _TD_SYMBOL_MAP["NQ"] = candidate
                logger.info("TwelveData NQ symbol probe: '%s' works (%d bars)", candidate, len(data["values"]))
                return candidate
            else:
                msg = data.get("message", "no values")
                logger.info("TwelveData NQ probe: '%s' failed (%s)", candidate, msg)
        except Exception as exc:
            logger.info("TwelveData NQ probe: '%s' exception: %s", candidate, exc)
    logger.warning("TwelveData NQ symbol probe: ALL candidates failed, keeping default '%s'", _TD_SYMBOL_MAP["NQ"])
    return _TD_SYMBOL_MAP["NQ"]


def probe_gc_symbol():
    """Try GC symbol candidates on Twelve Data, set the one that works."""
    for candidate in _TD_GC_CANDIDATES:
        try:
            resp = _requests.get(
                "https://api.twelvedata.com/time_series",
                params={
                    "symbol":     candidate,
                    "interval":   "1h",
                    "outputsize": 5,
                    "apikey":     TWELVE_DATA_API_KEY,
                    "format":     "JSON",
                },
                timeout=10,
            )
            data = resp.json()
            if data.get("status") != "error" and "values" in data and len(data["values"]) >= 3:
                _TD_SYMBOL_MAP["GC"] = candidate
                logger.info("TwelveData GC symbol probe: '%s' works (%d bars)", candidate, len(data["values"]))
                return candidate
            else:
                msg = data.get("message", "no values")
                logger.info("TwelveData GC probe: '%s' failed (%s)", candidate, msg)
        except Exception as exc:
            logger.info("TwelveData GC probe: '%s' exception: %s", candidate, exc)
    logger.warning("TwelveData GC symbol probe: ALL candidates failed, keeping default '%s'", _TD_SYMBOL_MAP["GC"])
    return _TD_SYMBOL_MAP["GC"]


def _fetch_twelvedata(market: str, timeframe: str) -> pd.DataFrame:
    """Fetch OHLCV data via Twelve Data REST API for NQ or GC."""
    symbol = _TD_SYMBOL_MAP.get(market)
    if symbol is None:
        logger.warning("No Twelve Data symbol for market %s", market)
        return pd.DataFrame(columns=_STANDARD_COLS)

    interval = _TD_TF_MAP.get(timeframe)
    if interval is None:
        logger.warning("No Twelve Data interval for timeframe %s", timeframe)
        return pd.DataFrame(columns=_STANDARD_COLS)

    outputsize = _TD_BAR_COUNT.get(timeframe, 500)

    logger.info("TwelveData fetch: %s  interval=%s  bars=%d", symbol, interval, outputsize)

    try:
        resp = _requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol":     symbol,
                "interval":   interval,
                "outputsize": outputsize,
                "apikey":     TWELVE_DATA_API_KEY,
                "format":     "JSON",
            },
            timeout=15,
        )
        data = resp.json()

        if data.get("status") == "error" or "values" not in data:
            msg = data.get("message", "unknown error")
            logger.warning("TwelveData error for %s %s: %s", market, timeframe, msg)
            return pd.DataFrame(columns=_STANDARD_COLS)

        values = data["values"]
        if not values:
            logger.warning("TwelveData returned empty values for %s %s", market, timeframe)
            return pd.DataFrame(columns=_STANDARD_COLS)

        df = pd.DataFrame(values)
        df = df.rename(columns={
            "datetime": "timestamp",
            "open":     "Open",
            "high":     "High",
            "low":      "Low",
            "close":    "Close",
            "volume":   "Volume",
        })
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp")
        df = _normalise_df(df)

        if len(df) >= MIN_BARS:
            # Success — reset fallback counter
            fb_key = f"{market}|{timeframe}"
            _td_fallback_count[fb_key] = 0
            _last_source[fb_key] = "twelve_data"

        logger.info("TwelveData got %d bars for %s %s", len(df), market, timeframe)
        return df

    except Exception as exc:
        logger.warning("TwelveData exception for %s %s: %s", market, timeframe, exc)
        return pd.DataFrame(columns=_STANDARD_COLS)


# ---------------------------------------------------------------------------
# yfinance fetcher (backup for NQ / GC)
# ---------------------------------------------------------------------------
# Per-ticker last-fetch timestamp to throttle yfinance
_YF_LAST_FETCH: Dict[str, float] = {}
_YF_MIN_INTERVAL_S = 90.0  # Don't hit yfinance for same ticker/tf more than once per 90 seconds


def _fetch_yfinance(market: str, timeframe: str) -> pd.DataFrame:
    """Fetch OHLCV data via yfinance for NQ or GC with aggressive throttling."""
    import yfinance as yf

    ticker = _YF_TICKER_MAP.get(market)
    if ticker is None:
        raise ValueError(f"No yfinance ticker for market {market}")

    yf_interval, yf_period = _YF_INTERVAL_MAP[timeframe]
    throttle_key = f"{ticker}|{yf_interval}"

    # Check per-ticker cooldown — if we fetched this ticker+interval in last
    # _YF_MIN_INTERVAL_S seconds, return stale cache instead of hitting yfinance
    now_ts = time.time()
    last_ts = _YF_LAST_FETCH.get(throttle_key, 0)
    if now_ts - last_ts < _YF_MIN_INTERVAL_S:
        stale = _get_stale_cache(market, timeframe)
        if stale is not None:
            logger.info("yfinance throttled for %s %s (cooldown %.0fs) — using stale cache",
                        ticker, yf_interval, _YF_MIN_INTERVAL_S - (now_ts - last_ts))
            return stale
        # No stale cache available — proceed with fetch but log it
        logger.info("yfinance throttled for %s %s but no stale cache — fetching anyway",
                    ticker, yf_interval)

    logger.info("yfinance fetch: %s  interval=%s  period=%s", ticker, yf_interval, yf_period)

    try:
        time.sleep(3)  # Increased from 1s — give Yahoo more breathing room
        tk = yf.Ticker(ticker)
        df = tk.history(interval=yf_interval, period=yf_period, auto_adjust=True)
        _YF_LAST_FETCH[throttle_key] = time.time()
    except Exception as exc:
        err_str = str(exc).lower()
        if "too many requests" in err_str or "rate" in err_str:
            logger.error("yfinance RATE LIMITED for %s %s: %s", market, timeframe, exc)
            # On rate limit, return stale cache if available
            stale = _get_stale_cache(market, timeframe)
            if stale is not None:
                logger.warning("Returning stale cache for %s %s due to rate limit", market, timeframe)
                return stale
        else:
            logger.error("yfinance error for %s %s: %s", market, timeframe, exc)
        return pd.DataFrame(columns=_STANDARD_COLS)

    if df is None or df.empty:
        logger.warning("yfinance returned empty data for %s %s", market, timeframe)
        # Try stale cache one more time
        stale = _get_stale_cache(market, timeframe)
        if stale is not None:
            return stale
        return pd.DataFrame(columns=_STANDARD_COLS)

    df = _normalise_df(df)

    # Resample to 4h if needed
    if timeframe == "4h":
        df = _resample_to_4h(df)

    return df


# ---------------------------------------------------------------------------
# Databento fetcher (stub -- activated when DATABENTO_API_KEY is set)
# ---------------------------------------------------------------------------
def _fetch_databento(market: str, timeframe: str) -> pd.DataFrame:
    """Fetch OHLCV data via Databento for NQ or GC."""
    try:
        import databento as db
    except ImportError:
        logger.error("databento package not installed; falling back to yfinance")
        return _fetch_yfinance(market, timeframe)

    if DATABENTO_API_KEY is None:
        return _fetch_yfinance(market, timeframe)

    # Databento dataset / symbol mapping
    dataset = "GLBX.MDP3"
    symbol_map = {"NQ": "NQ.FUT", "GC": "GC.FUT"}
    symbol = symbol_map.get(market)
    if symbol is None:
        logger.error("No Databento symbol for market %s", market)
        return _fetch_yfinance(market, timeframe)

    # Map timeframe to Databento schema / bar size
    schema_map = {
        "15m": "ohlcv-15m",
        "1h":  "ohlcv-1h",
        "4h":  "ohlcv-4h",
        "1d":  "ohlcv-1d",
    }
    schema = schema_map.get(timeframe)
    if schema is None:
        logger.error("No Databento schema for timeframe %s", timeframe)
        return _fetch_yfinance(market, timeframe)

    logger.info("Databento fetch: %s  dataset=%s  schema=%s", symbol, dataset, schema)

    try:
        client = db.Historical(key=DATABENTO_API_KEY)
        now = datetime.now(timezone.utc)
        # Determine lookback based on timeframe
        lookback_days = {"15m": 10, "1h": 60, "4h": 60, "1d": 730}
        start = now - pd.Timedelta(days=lookback_days.get(timeframe, 60))

        data = client.timeseries.get_range(
            dataset=dataset,
            symbols=[symbol],
            schema=schema,
            start=start.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            end=now.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        )
        df = data.to_df()
    except Exception as exc:
        logger.error("Databento error for %s %s: %s -- falling back to yfinance", market, timeframe, exc)
        return _fetch_yfinance(market, timeframe)

    if df is None or df.empty:
        logger.warning("Databento returned empty data for %s %s -- falling back to yfinance", market, timeframe)
        return _fetch_yfinance(market, timeframe)

    # Databento columns are lowercase
    rename = {"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}
    df = df.rename(columns=rename)
    df = _normalise_df(df)
    return df


# ---------------------------------------------------------------------------
# ccxt crypto fetcher
# ---------------------------------------------------------------------------
def _create_exchange(name: str):
    """Create a ccxt exchange instance by name."""
    import ccxt
    exchange_class = getattr(ccxt, name, None)
    if exchange_class is None:
        raise ValueError(f"Unknown ccxt exchange: {name}")
    return exchange_class({"enableRateLimit": True})


def _fetch_crypto(market: str, timeframe: str) -> pd.DataFrame:
    """Fetch OHLCV data via ccxt with exchange fallback chain."""
    import ccxt

    # Build the base symbol from market name
    base_symbol = f"{market}/USDT"
    ccxt_tf = _CCXT_TIMEFRAME_MAP.get(timeframe)
    if ccxt_tf is None:
        logger.error("No ccxt timeframe mapping for %s", timeframe)
        return pd.DataFrame(columns=_STANDARD_COLS)

    # Determine how many bars to request
    bar_counts = {"15m": 700, "1h": 500, "4h": 500, "1d": 730}
    limit = bar_counts.get(timeframe, 500)

    # Coinbase does NOT support 4h candles — skip it entirely for 4h
    # to avoid the 2-3 second timeout + warning on every scan cycle.
    # Coinbase supported intervals: 1m, 5m, 15m, 1h, 6h, 1d.
    if timeframe == "4h":
        exchanges_to_try = [e for e in _CRYPTO_EXCHANGES if e != "coinbase"]
    else:
        exchanges_to_try = _CRYPTO_EXCHANGES

    last_error = None

    for exch_name in exchanges_to_try:
        # Map symbol for this exchange
        symbol = _CRYPTO_SYMBOL_MAP.get(exch_name, {}).get(base_symbol, base_symbol)
        logger.info("ccxt fetch: %s on %s  tf=%s  limit=%d", symbol, exch_name, ccxt_tf, limit)

        try:
            exchange = _create_exchange(exch_name)
            exchange.load_markets()

            if symbol not in exchange.markets:
                logger.warning("Symbol %s not found on %s, trying next exchange", symbol, exch_name)
                continue

            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=ccxt_tf, limit=limit)

            if not ohlcv or len(ohlcv) == 0:
                logger.warning("Empty OHLCV from %s for %s %s", exch_name, symbol, ccxt_tf)
                continue

            df = pd.DataFrame(ohlcv, columns=["timestamp", "Open", "High", "Low", "Close", "Volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df.set_index("timestamp", inplace=True)
            df = _normalise_df(df)

            if len(df) >= MIN_BARS:
                logger.info("Got %d bars from %s for %s %s", len(df), exch_name, market, timeframe)
                return df
            else:
                logger.warning("Only %d bars from %s for %s %s (need %d)", len(df), exch_name, market, timeframe, MIN_BARS)

        except Exception as exc:
            last_error = exc
            logger.warning("ccxt error on %s for %s %s: %s", exch_name, market, timeframe, exc)
            continue

    logger.error("All crypto exchanges failed for %s %s. Last error: %s", market, timeframe, last_error)
    return pd.DataFrame(columns=_STANDARD_COLS)


# ---------------------------------------------------------------------------
# Unified fetch dispatcher
# ---------------------------------------------------------------------------
def _fetch_raw(market: str, timeframe: str) -> pd.DataFrame:
    """
    Dispatch to data source by market. Source priority:
      NQ/GC:   TopstepX (primary) -> TwelveData -> yfinance
      BTC/SOL: ccxt (unchanged)
    """
    market_upper = market.upper()

    if market_upper in _CRYPTO_MARKETS:
        return _fetch_crypto(market_upper, timeframe)

    if market_upper in _FUTURES_MARKETS:
        # PRIMARY: TopstepX (real CME futures data)
        df_tsx = _fetch_topstepx(market_upper, timeframe)
        if df_tsx is not None and len(df_tsx) >= MIN_BARS:
            logger.info("Using TopstepX for %s %s (%d bars)", market_upper, timeframe, len(df_tsx))
            return df_tsx
        logger.info("TopstepX insufficient/unavailable for %s %s — trying TwelveData", market_upper, timeframe)

        # FALLBACK 1: TwelveData (unchanged logic)
        df_td = _fetch_twelvedata(market_upper, timeframe)
        if df_td is not None and len(df_td) >= MIN_BARS:
            logger.info("Using TwelveData for %s %s (%d bars)", market_upper, timeframe, len(df_td))
            return df_td

        # FALLBACK 2: yfinance (last resort)
        fb_key = f"{market_upper}|{timeframe}"
        _td_fallback_count[fb_key] = _td_fallback_count.get(fb_key, 0) + 1
        n = _td_fallback_count[fb_key]
        _last_source[fb_key] = "yfinance"
        logger.warning(
            "Fallback chain to yfinance for %s %s (attempt %d).",
            market_upper, timeframe, n
        )
        if n >= 3:
            logger.error(
                "DATA ALERT: %s %s using yfinance fallback %dx in a row — check TopstepX auth & symbols",
                market_upper, timeframe, n
            )
        return _fetch_yfinance(market_upper, timeframe)

    logger.error("Unknown market: %s", market)
    return pd.DataFrame(columns=_STANDARD_COLS)


def _fetch_with_cache(market: str, timeframe: str) -> pd.DataFrame:
    """Fetch data with 60-second cache and stale-cache fallback on error."""
    market_upper = market.upper()

    # Check cache first
    cached = _get_cached(market_upper, timeframe)
    if cached is not None:
        return cached

    # Fetch fresh data
    try:
        df = _fetch_raw(market_upper, timeframe)
    except Exception as exc:
        logger.error("Unexpected error fetching %s %s: %s", market_upper, timeframe, exc)
        df = pd.DataFrame(columns=_STANDARD_COLS)

    # Validate
    if df is not None and len(df) >= MIN_BARS:
        _set_cache(market_upper, timeframe, df)
        return df.copy()

    # Not enough data -- try stale cache
    logger.warning("Insufficient data for %s %s (%d bars, need %d)",
                    market_upper, timeframe, 0 if df is None else len(df), MIN_BARS)
    stale = _get_stale_cache(market_upper, timeframe)
    if stale is not None:
        return stale

    # Return whatever we got (possibly empty)
    if df is not None:
        return df
    return pd.DataFrame(columns=_STANDARD_COLS)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
_ALL_TIMEFRAMES = ["15m", "1h", "4h", "1d"]


def get_frames(market: str) -> Dict[str, pd.DataFrame]:
    """
    Return a dict keyed by "15m", "1h", "4h", "1d" with OHLCV DataFrames.

    Each DataFrame has columns: Open, High, Low, Close, Volume
    and a UTC DatetimeIndex.

    Parameters
    ----------
    market : str
        One of "NQ", "GC", "BTC", "SOL"

    Returns
    -------
    dict[str, pd.DataFrame]
    """
    market_upper = market.upper()
    logger.info("get_frames(%s) called", market_upper)
    frames: Dict[str, pd.DataFrame] = {}

    for tf in _ALL_TIMEFRAMES:
        try:
            df = _fetch_with_cache(market_upper, tf)
            frames[tf] = df
        except Exception as exc:
            logger.error("  %s %s: failed (%s)", market_upper, tf, exc)
            frames[tf] = pd.DataFrame(columns=_STANDARD_COLS)

    # Task 9: Summary log line per market
    bar_counts = "/".join(f"{tf}:{len(frames.get(tf, []))}bars" for tf in _ALL_TIMEFRAMES)
    sources = set(_last_source.get(f"{market_upper}|{tf}", "cache") for tf in _ALL_TIMEFRAMES)
    source_str = "+".join(sorted(sources))
    logger.info("Data check %s: %s | source=%s", market_upper, bar_counts, source_str)

    return frames


def get_current_price(market: str) -> float:
    """
    Return the latest close price for the given market.

    Uses 15m data for the freshest price.

    Parameters
    ----------
    market : str
        One of "NQ", "GC", "BTC", "SOL"

    Returns
    -------
    float
        Latest close price, or NaN if unavailable.
    """
    market_upper = market.upper()
    logger.debug("get_current_price(%s)", market_upper)

    try:
        df = _fetch_with_cache(market_upper, "15m")
        if df is not None and not df.empty:
            price = float(df["Close"].iloc[-1])
            logger.info("Current price %s: %.4f", market_upper, price)
            return price
    except Exception as exc:
        logger.error("Error getting current price for %s: %s", market_upper, exc)

    # Fallback: try 1h
    try:
        df = _fetch_with_cache(market_upper, "1h")
        if df is not None and not df.empty:
            price = float(df["Close"].iloc[-1])
            logger.warning("Current price %s (from 1h fallback): %.4f", market_upper, price)
            return price
    except Exception as exc:
        logger.error("Fallback price fetch failed for %s: %s", market_upper, exc)

    return float("nan")


# ---------------------------------------------------------------------------
# Optional: quick self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    )

    for mkt in ["NQ", "GC", "BTC", "SOL"]:
        print(f"\n{'='*60}")
        print(f"  {mkt}")
        print(f"{'='*60}")
        price = get_current_price(mkt)
        print(f"  Current price: {price}")
        frames = get_frames(mkt)
        for tf, df in frames.items():
            print(f"  {tf}: {len(df)} bars", end="")
            if not df.empty:
                print(f"  | {df.index[0]} -> {df.index[-1]}  last close={df['Close'].iloc[-1]:.4f}")
            else:
                print("  (empty)")
