"""
data_layer.py  --  Unified data module for NQ CALLS trading bot.
Replaces fetch_yfinance, fetch_crypto, and get_frames from bot.py.
"""

import time
import logging
from datetime import datetime, timezone
from typing import Dict, Optional

import pandas as pd
import numpy as np

logger = logging.getLogger("nqcalls.data")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATABENTO_API_KEY: Optional[str] = None   # Set to enable Databento for NQ/GC

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
# yfinance fetcher
# ---------------------------------------------------------------------------
def _fetch_yfinance(market: str, timeframe: str) -> pd.DataFrame:
    """Fetch OHLCV data via yfinance for NQ or GC."""
    import yfinance as yf

    ticker = _YF_TICKER_MAP.get(market)
    if ticker is None:
        raise ValueError(f"No yfinance ticker for market {market}")

    yf_interval, yf_period = _YF_INTERVAL_MAP[timeframe]

    logger.info("yfinance fetch: %s  interval=%s  period=%s", ticker, yf_interval, yf_period)

    try:
        tk = yf.Ticker(ticker)
        df = tk.history(interval=yf_interval, period=yf_period, auto_adjust=True)
    except Exception as exc:
        logger.error("yfinance error for %s %s: %s", market, timeframe, exc)
        return pd.DataFrame(columns=_STANDARD_COLS)

    if df is None or df.empty:
        logger.warning("yfinance returned empty data for %s %s", market, timeframe)
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
    """Dispatch to the correct data source based on market."""
    market_upper = market.upper()

    if market_upper in _CRYPTO_MARKETS:
        return _fetch_crypto(market_upper, timeframe)

    if market_upper in _FUTURES_MARKETS:
        if DATABENTO_API_KEY is not None:
            return _fetch_databento(market_upper, timeframe)
        else:
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
            logger.info("  %s %s: %d bars", market_upper, tf, len(df))
        except Exception as exc:
            logger.error("  %s %s: failed (%s)", market_upper, tf, exc)
            frames[tf] = pd.DataFrame(columns=_STANDARD_COLS)

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
