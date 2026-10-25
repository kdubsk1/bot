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
_neg_fetch: Dict[str, float] = {}   # Wave 95: {market|tf: last-insufficient ts} for fetch backoff


def _cache_key(market: str, timeframe: str) -> str:
    return f"{market}|{timeframe}"


def _get_cached(market: str, timeframe: str) -> Optional[pd.DataFrame]:
    # Wave 61 (_WAVE61_TTL): per-timeframe TTLs. Daily bars do not change
    # every 60 seconds -- the old flat 60s TTL forced a full provider
    # round-trip on every scan for every timeframe, which burned the
    # TwelveData daily credit limit in hours and got yfinance rate-limited.
    # _WAVE269_FRESH_ENTRY (3 Oct 2026): the line here used to say the bot trades CLOSED bars, so these windows
    # are safe. It does not: the frames carry the FORMING bar and detect_setups takes a call's entry from its
    # close, so a 4h entry could be 60 minutes old (1h: 15). The windows stay - they protect the providers -
    # and w269_fresh_tail brings a 1h / 4h frame's last bar up to date from the 15m frame before it is scanned.
    ttl_by_tf = {"15m": 180, "1h": 900, "4h": 3600, "1d": 14400}
    key = _cache_key(market, timeframe)
    entry = _cache.get(key)
    if entry is None:
        return None
    ttl = ttl_by_tf.get(timeframe, CACHE_TTL)
    if time.time() - entry["ts"] > ttl:
        return None
    if _w275_reopen_expired(market, timeframe, entry):   # _WAVE275_REOPEN_FRESH: saved before the reopen
        return None
    logger.debug("Cache hit for %s %s", market, timeframe)
    return entry["df"].copy()


_WAVE66_DAILY_DISK_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "data", "daily_cache")  # _WAVE67_OS_HOTFIX


def _daily_disk_path(market: str) -> str:
    return _os.path.join(_WAVE66_DAILY_DISK_DIR, f"{market.upper()}_1d.csv")   # _WAVE276_DAILY_FRAME: was os. (F24)


def _save_daily_disk(market: str, df: pd.DataFrame) -> None:
    # Wave 66 (_WAVE66_DAILY_DISK): persist a healthy daily frame so it
    # survives restarts and bridges yfinance throttle windows. Best-effort.
    try:
        _os.makedirs(_WAVE66_DAILY_DISK_DIR, exist_ok=True)
        df.to_csv(_daily_disk_path(market))
    except Exception as exc:
        _w276_daily_warn(market, "save", exc)   # _WAVE276_DAILY_FRAME: WARNING once per boot, not DEBUG


def _load_daily_disk(market: str) -> Optional[pd.DataFrame]:
    try:
        p = _daily_disk_path(market)
        if not _os.path.exists(p):
            return None
        df = pd.read_csv(p, index_col=0, parse_dates=True)
        if df is not None and len(df) >= MIN_BARS:
            return _normalise_df(df)
    except Exception as exc:
        _w276_daily_warn(market, "load", exc)   # _WAVE276_DAILY_FRAME: WARNING once per boot, not DEBUG
    return None


def _set_cache(market: str, timeframe: str, df: pd.DataFrame) -> None:
    key = _cache_key(market, timeframe)
    _cache[key] = {"df": df.copy(), "ts": time.time()}
    # Wave 66: also persist healthy daily frames to disk.
    # _WAVE276_DAILY_FRAME: only a frame Yahoo just delivered - never a TopstepX boot frame or a re-served one.
    if timeframe == "1d" and df is not None and len(df) >= MIN_BARS and _w276_daily.get(market, {}).get("fresh"):
        _save_daily_disk(market, df)


def _get_stale_cache(market: str, timeframe: str) -> Optional[pd.DataFrame]:
    """Return cached data even if expired -- used as fallback on errors."""
    key = _cache_key(market, timeframe)
    entry = _cache.get(key)
    if entry is not None:
        logger.warning("Returning stale cache for %s %s", market, timeframe)
        return entry["df"].copy()
    # Wave 66 (_WAVE66_DAILY_DISK): in-memory cache is wiped on restart;
    # for daily bars fall back to the last good frame saved on disk so
    # 1d data survives restarts + yfinance throttle windows.
    if timeframe == "1d":
        disk = _load_daily_disk(market)
        if disk is not None:
            logger.warning("Returning DISK daily cache for %s (%d bars)", market, len(disk))
            return disk
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
    Returns None if credentials are missing or auth fails -- caller falls back.

    Wave 61: a failed auth now sets a 10-minute cooldown. Before this, a
    dead API key meant the bot re-attempted auth on EVERY fetch (8+ times
    a minute), spamming the API and the logs while every futures scan
    waited on a doomed network call.
    """
    global _TOPSTEPX_TOKEN, _TOPSTEPX_TOKEN_EXPIRY
    now = datetime.now(timezone.utc)

    if _TOPSTEPX_TOKEN and _TOPSTEPX_TOKEN_EXPIRY:
        if now < _TOPSTEPX_TOKEN_EXPIRY - pd.Timedelta(minutes=5):
            return _TOPSTEPX_TOKEN

    _fail_until = getattr(_get_topstepx_token, "_fail_until", None)
    if _fail_until is not None and now < _fail_until:
        return None  # cooling down after a failed auth -- fall back quietly

    def _cooldown():
        _get_topstepx_token._fail_until = now + pd.Timedelta(minutes=10)

    username = _os.environ.get("TOPSTEPX_USERNAME", "").strip()
    api_key = _os.environ.get("TOPSTEPX_API_KEY", "").strip()
    if not username or not api_key:
        logger.warning("TopstepX creds not set (TOPSTEPX_USERNAME / TOPSTEPX_API_KEY) -- skipping TopstepX fetch")
        _cooldown()
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
        _cooldown()
        return None

    if resp.status_code != 200:
        logger.error("TopstepX auth HTTP %d: %s", resp.status_code, resp.text[:200])
        _cooldown()
        return None

    try:
        data = resp.json()
    except Exception as exc:
        logger.error("TopstepX auth JSON parse: %s", exc)
        _cooldown()
        return None

    if not data.get("success") or not data.get("token"):
        logger.error("TopstepX auth failed: %s -- if this persists, regenerate the "
                     "API key in the ProjectX/TopstepX dashboard and update "
                     "TOPSTEPX_API_KEY on Railway (Variables tab). "
                     "Auth attempts paused for 10 minutes.",
                     data.get("errorMessage", "unknown"))
        _cooldown()
        return None

    _get_topstepx_token._fail_until = None
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

    Apr 30 LATE LATE fix: TopstepX was returning 0 contracts with the default
    search ({"searchText": market, "live": True}). When that happens we now
    try multiple search-body variations until something returns a non-empty
    list, then run the existing 3-pass match against those results.
    """
    now = datetime.now(timezone.utc)
    cached = _TOPSTEPX_CONTRACT_CACHE.get(market)
    if cached and cached[1] > now and not force_refresh:
        return cached[0]

    sym_filter = _TOPSTEPX_SYMBOL_FILTER.get(market)
    if not sym_filter:
        logger.warning("TopstepX: no symbolId filter for market %s", market)
        return None

    bare = sym_filter.replace("F.US.", "")  # "ENQ" for NQ, "GC" for GC

    # Apr 30 LATE LATE: try several search variations. Stop at the first one
    # that returns a non-empty contract list. Each variation is logged so the
    # diagnostic startup banner can show which combo actually worked.
    SEARCH_VARIATIONS = [
        {"searchText": market,     "live": True},   # original behavior
        {"searchText": market,     "live": False},  # maybe live filter was the problem
        {"searchText": bare,       "live": True},   # "ENQ" / "GC" instead of "NQ"
        {"searchText": bare,       "live": False},
        {"searchText": sym_filter, "live": True},   # full "F.US.ENQ" prefix
        {"searchText": sym_filter, "live": False},
    ]

    contracts: list = []
    used_body: dict = {}

    for body in SEARCH_VARIATIONS:
        try:
            resp = _topstepx_post(
                "/api/Contract/search",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type":  "application/json",
                    "accept":        "text/plain",
                },
                body=body,
                timeout=10,
            )
        except Exception as exc:
            logger.warning("TopstepX contract search network error for %s body=%s: %s", market, body, exc)
            continue

        if resp.status_code != 200:
            logger.warning("TopstepX contract search HTTP %d for %s body=%s: %s",
                           resp.status_code, market, body, resp.text[:120])
            continue

        try:
            data = resp.json()
        except Exception:
            continue

        if not data.get("success"):
            logger.warning("TopstepX contract search failed for %s body=%s: %s",
                           market, body, data.get("errorMessage"))
            continue

        cs = data.get("contracts", [])
        if cs:
            contracts = cs
            used_body = body
            logger.info("TopstepX %s: search OK with body=%s, got %d contracts",
                        market, body, len(cs))
            break  # use the first successful variation
        else:
            logger.info("TopstepX %s: body=%s returned 0 contracts, trying next", market, body)

    if not contracts:
        logger.error("TopstepX %s: ALL %d search variations returned empty. "
                     "API may have changed shape or your account may lack data "
                     "permissions. Falling back to TwelveData/yfinance.",
                     market, len(SEARCH_VARIATIONS))
        return None

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
        for c in contracts:
            sid = str(c.get("symbolId", ""))
            if c.get("activeContract") and bare in sid:
                match = c
                logger.info("TopstepX %s: matched via contains(%s) — symbolId=%s", market, bare, sid)
                break

    # Apr 30 LATE LATE: Pass 4 — if we got contracts but NONE have activeContract=true
    # (e.g. live: false fallback returned only inactive ones), pick the first
    # contract whose symbolId contains our bare symbol and is not explicitly
    # marked inactive. Logged loudly so we know we're using a possibly stale match.
    if not match:
        for c in contracts:
            sid = str(c.get("symbolId", ""))
            if bare in sid:
                match = c
                logger.warning("TopstepX %s: NO activeContract found, using best-effort symbolId=%s (used_body=%s) — verify with Wayne",
                               market, sid, used_body)
                break

    if not match:
        # Diagnostic: dump what we actually got back so Wayne can see why
        sample_ids = [str(c.get("symbolId", "")) for c in contracts[:10]]
        logger.warning("TopstepX: no matching %s contract (tried symbolId=%s, %d results, used_body=%s, sample IDs: %s)",
                       market, sym_filter, len(contracts), used_body, sample_ids)
        return None

    contract_id = match["id"]
    _TOPSTEPX_CONTRACT_CACHE[market] = (contract_id, now + pd.Timedelta(hours=4))
    logger.info("TopstepX %s active contract: %s (%s)", market, contract_id, match.get("description", ""))
    return contract_id


def _w279_fetch_topstepx_raw(market: str, timeframe: str) -> pd.DataFrame:   # _WAVE279_TOPSTEPX_HEALTH: was _fetch_topstepx (now its wrapper)
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
        "live":              w279_live_flag(),   # _WAVE279_TOPSTEPX_HEALTH: False once this account refused True (re-tried at each 6 PM ET open)
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

    def _do_post():   # _WAVE279_TOPSTEPX_HEALTH: the same request, timed and noted (_w279_rec_post)
        return _w279_rec_post(body, lambda: _topstepx_post("/api/History/retrieveBars", headers=headers, body=body,
                                                           timeout=15))

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
        # May 1 fix: When TopstepX subscription doesn't include live data, retrieveBars returns
        # success=false with errorMessage=None. Retry once with live=False (delayed/historical).
        # That's the SAME data the chart UI shows, just with 15-min delay (irrelevant for 1h+
        # technical analysis). For 15m bars near the current bar this introduces a tiny lag,
        # but the bot is making decisions on closed bars so it doesn't matter.
        if body.get("live") is True:
            logger.info("TopstepX %s %s: live=True (funded env) not available — using live=False (Combine sim feed, real-time L1)", market, timeframe)
            body["live"] = False
            try:
                resp_dl = _do_post()
                if resp_dl.status_code == 200:
                    data = resp_dl.json()
                    if data.get("success"):
                        logger.info("TopstepX %s %s: live=False worked ✅ (Combine sim feed - real-time L1)", market, timeframe)
                    else:
                        logger.warning("TopstepX %s %s: live=False also failed: %s",
                                       market, timeframe, data.get("errorMessage", "unknown"))
                        return pd.DataFrame(columns=_STANDARD_COLS)
                else:
                    logger.warning("TopstepX %s %s: live=False retry HTTP %d",
                                   market, timeframe, resp_dl.status_code)
                    return pd.DataFrame(columns=_STANDARD_COLS)
            except Exception as exc:
                logger.warning("TopstepX %s %s: live=False retry exception: %s", market, timeframe, exc)
                return pd.DataFrame(columns=_STANDARD_COLS)
        else:
            logger.warning("TopstepX error for %s %s: %s", market, timeframe, data.get("errorMessage", "unknown"))
            return pd.DataFrame(columns=_STANDARD_COLS)

    # Wave 115 (_WAVE115_FEED_MODE): by here the fetch succeeded; body["live"]
    # says whether the account served real-time (True) or the delayed retry
    # (False). Record + announce changes. Guarded: only when the key exists.
    if "live" in body:
        _note_tsx_feed_mode(body.get("live") is True)
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

# Wave 115 (_WAVE115_FEED_MODE): account-wide TopstepX feed mode, made VISIBLE.
# The API tries live=True on every fetch; the account entitlement decides what
# we actually get. mode is "LIVE" or "SIM-REALTIME" (None until the first successful
# fetch). On every CHANGE we log at ERROR with the Wave-62 "DATA ALERT" prefix
# (the established transport that reaches Telegram), so Wayne is told the
# moment the feed upgrades to real-time (or ever degrades back).
_tsx_feed: Dict[str, Optional[str]] = {"mode": None}


def _note_tsx_feed_mode(was_live: bool) -> None:
    try:
        # Wave 133 (_WAVE133_FEED_RELABEL): measured Jul 13 (Wave 132 age
        # telemetry): newest-bar age ~1.0-1.3 min -> the Combine sim feed IS
        # real-time L1 (as Topstep documents). "DELAYED" was an unmeasured
        # assumption from the live flag, which selects funded-vs-sim env.
        new_mode = "LIVE" if was_live else "SIM-REALTIME"
        old_mode = _tsx_feed.get("mode")
        if new_mode == old_mode:
            return
        _tsx_feed["mode"] = new_mode
        if old_mode is None:
            logger.info("TopstepX feed mode: %s%s", new_mode,
                        "" if was_live else " (Combine sim feed - real-time L1, measured age ~1min)")
        else:
            logger.error("DATA ALERT: TopstepX feed mode changed %s -> %s%s",
                         old_mode, new_mode,
                         " - funded-environment feed active!" if was_live else
                         " - on the Combine sim feed (real-time L1)")
    except Exception:
        pass
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
    """Fetch OHLCV data via Twelve Data REST API for NQ or GC.

    Wave 61: circuit breaker. When TwelveData reports the daily credit
    limit is exhausted, every further call that day is wasted latency in
    the scan loop. We now pause TwelveData for 60 minutes when that
    happens and fall straight through to the next source.
    """
    _blocked_until = getattr(_fetch_twelvedata, "_blocked_until", 0)
    if time.time() < _blocked_until:
        return pd.DataFrame(columns=_STANDARD_COLS)

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
                "symbol": symbol,
                "interval": interval,
                "outputsize": outputsize,
                "apikey": TWELVE_DATA_API_KEY,
                "format": "JSON",
            },
            timeout=15,
        )
        data = resp.json()

        if data.get("status") == "error" or "values" not in data:
            msg = str(data.get("message", "unknown error"))
            logger.warning("TwelveData error for %s %s: %s", market, timeframe, msg)
            low = msg.lower()
            if "credit" in low or "limit" in low:
                _fetch_twelvedata._blocked_until = time.time() + 3600
                logger.warning("TwelveData circuit OPEN for 60 min (daily credit/limit exhausted)")
            return pd.DataFrame(columns=_STANDARD_COLS)

        values = data["values"]
        if not values:
            logger.warning("TwelveData returned empty values for %s %s", market, timeframe)
            return pd.DataFrame(columns=_STANDARD_COLS)

        df = pd.DataFrame(values)
        df = df.rename(columns={
            "datetime": "timestamp",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        })
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp")
        df = _normalise_df(df)

        if len(df) >= MIN_BARS:
            # Success -- reset fallback counter
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
_EXCHANGE_CACHE = {}  # name -> ccxt exchange instance (built + markets loaded once)


def _create_exchange(name: str):
    """Return a cached ccxt exchange instance, created only once per process.

    Wave 58 (leak fix): a new exchange object was previously built and
    load_markets() called on EVERY crypto fetch (BTC/SOL x every timeframe
    x every scan cycle). Each object opened its own HTTP session that was
    never closed, leaking memory steadily until Railway OOM-killed the bot.
    We now build each exchange once and reuse it for the life of the
    process; the caller's load_markets() becomes a cheap no-op on the
    cached copy.
    """
    import ccxt
    cached = _EXCHANGE_CACHE.get(name)
    if cached is not None:
        return cached
    exchange_class = getattr(ccxt, name, None)
    if exchange_class is None:
        raise ValueError(f"Unknown ccxt exchange: {name}")
    exch = exchange_class({"enableRateLimit": True})
    _EXCHANGE_CACHE[name] = exch
    return exch


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

    # Apr 30 LATE LATE fix: 4h crypto often fails because
    #  - coinbase doesn't support 4h candles (skipped above)
    #  - kraken returns very few bars on 4h
    #  - bybit is geo-blocked from US (CloudFront 403)
    # When that happens, fetch 1h instead and resample to 4h. This is mathematically
    # equivalent (4h = 4 × 1h aggregated) and gives us full historical depth.
    if timeframe == "4h":
        logger.info("All crypto exchanges failed for %s 4h — falling back to 1h → 4h resample",
                    market)
        try:
            # Direct call (bypass cache) to avoid feedback loop
            df_1h = _fetch_crypto(market, "1h")
            if df_1h is not None and len(df_1h) >= 80:  # 80 1h bars = 20 4h bars
                df_4h_resampled = _resample_to_4h(df_1h)
                if len(df_4h_resampled) >= MIN_BARS:
                    logger.info("Resampled 1h→4h for %s: %d bars (saved by fallback)",
                                market, len(df_4h_resampled))
                    return df_4h_resampled
                else:
                    logger.warning("1h→4h resample for %s only produced %d bars (need %d)",
                                   market, len(df_4h_resampled), MIN_BARS)
            else:
                logger.warning("1h fetch for %s 4h-fallback returned only %d bars (need 80)",
                               market, 0 if df_1h is None else len(df_1h))
        except Exception as exc:
            logger.warning("4h←1h resample fallback failed for %s: %s", market, exc)

    logger.error("All crypto exchanges failed for %s %s. Last error: %s", market, timeframe, last_error)
    return pd.DataFrame(columns=_STANDARD_COLS)


# ---------------------------------------------------------------------------
# Unified fetch dispatcher
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# _WAVE276_DAILY_FRAME: where the daily frame really came from, and the last good one kept (review F24)
# ---------------------------------------------------------------------------
_W276_DAILY_TTL_S = 14400                         # _get_cached's 1d re-use time (Wave 61): a good day asks every ~4 h
_W276_DAILY_BACKOFF_S = (1800, 3600, 7200, 14400)  # after the 1st / 2nd / 3rd / later refusal in a row
_W276_DAILY_MAX_ASKS = 6                          # never more Yahoo asks than this in any 24 hours (a good day: 6)
_W276_DAILY_NAMES = {"yahoo": "Yahoo", "disk": "the disk copy", "topstepx": "TopstepX front contract",
                     "twelve_data": "TwelveData"}
_w276_daily = {}                                  # market -> {"src", "fresh", "refusals", "asks", "pending", "why"}
_w276_daily_warned = set()                        # (market, "save" / "load"): warned once this boot


def _w276_daily_warn(market, what, exc):
    key = (str(market).upper(), what)
    if key in _w276_daily_warned:
        logger.debug("daily disk %s failed for %s: %s", what, market, exc)
        return
    _w276_daily_warned.add(key)
    logger.warning("daily disk %s FAILED for %s: %s (said once per boot; the daily frame cannot survive a restart "
                   "while this fails)", what, market, exc)


def _w276_daily_desc(df):
    try:
        return "%d bars, newest daily bar %s" % (len(df), df.index[-1].strftime("%Y-%m-%d"))
    except Exception:
        return "%s bars" % (len(df) if df is not None else 0)


def _w276_daily_yahoo(market, st):
    """One Yahoo request for the continuous daily frame: (frame, None), or (None, why). Never raises. Asks only if
    fewer than _W276_DAILY_MAX_ASKS asks were made in the last 24 hours."""
    try:
        now = time.time()
        st["asks"] = [t for t in st.get("asks", []) if now - t < 86400]
        if len(st["asks"]) >= _W276_DAILY_MAX_ASKS:
            return None, "not asked: %d asks in the last 24 h" % len(st["asks"])
        ticker = _YF_TICKER_MAP.get(market)
        if ticker is None:
            return None, "no Yahoo ticker"
        yf_interval, yf_period = _YF_INTERVAL_MAP["1d"]
        import yfinance as yf
        logger.info("yfinance fetch: %s  interval=%s  period=%s", ticker, yf_interval, yf_period)
        time.sleep(3)                                  # as _fetch_yfinance: give Yahoo room
        st["asks"].append(time.time())
        _YF_LAST_FETCH["%s|%s" % (ticker, yf_interval)] = time.time()
        df = yf.Ticker(ticker).history(interval=yf_interval, period=yf_period, auto_adjust=True)
        if df is None or len(df) == 0:
            return None, "Yahoo returned no rows"
        df = _normalise_df(df)
        if len(df) < MIN_BARS:
            return None, "Yahoo returned only %d rows" % len(df)
        return df, None
    except Exception as exc:
        return None, "Yahoo refused: %s" % str(exc)[:120]


def _w276_daily_frame(market):
    """The daily frame for NQ / gold, in order: Yahoo fresh; else the Yahoo or disk frame already held; else the disk
    copy; else the TopstepX frame already held; else None (the caller tries TopstepX, then TwelveData, as before).
    Every serve is logged with where it came from, its newest daily bar and its bar count. Never raises."""
    st = _w276_daily.setdefault(market, {"src": None, "fresh": False, "refusals": 0, "asks": [], "pending": False})
    st["fresh"], st["pending"] = False, False
    try:
        df, why = _w276_daily_yahoo(market, st)
        key = _cache_key(market, "1d")
        if df is not None:
            st.update(src="yahoo", fresh=True, refusals=0, why=None)
            _last_source[key] = "yfinance"
            logger.info("Daily frame %s: Yahoo fresh - %s (saved to disk)", market, _w276_daily_desc(df))
            return df
        if not str(why).startswith("not asked"):
            st["refusals"] = st.get("refusals", 0) + 1
        st["why"] = why
        mem = _cache.get(key)
        if mem is not None and st.get("src") in ("yahoo", "disk"):
            logger.warning("Daily frame %s: %s - the same frame as before (%s, %s)", market, why,
                           _W276_DAILY_NAMES.get(st["src"], st["src"]), _w276_daily_desc(mem["df"]))
            return mem["df"].copy()
        disk = _load_daily_disk(market)
        if disk is not None:
            st["src"] = "disk"
            _last_source[key] = "disk"
            try:
                saved = datetime.fromtimestamp(_os.path.getmtime(_daily_disk_path(market)), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                saved = "UNKNOWN"
            logger.warning("Daily frame %s: %s - the disk copy (%s; file written %s)", market, why,
                           _w276_daily_desc(disk), saved)
            return disk
        if mem is not None:
            logger.warning("Daily frame %s: %s - NO disk copy - the same frame as before (%s, %s)", market, why,
                           _W276_DAILY_NAMES.get(st.get("src"), st.get("src") or "UNKNOWN source"), _w276_daily_desc(mem["df"]))
            return mem["df"].copy()
        logger.warning("Daily frame %s: %s - NO disk copy and nothing held - trying TopstepX's front contract "
                       "(a short history)", market, why)
        st["pending"] = True
        return None
    except Exception as exc:
        logger.warning("Daily frame %s: the lookup failed (%s) - trying TopstepX as before", market, exc)
        st["pending"] = True
        return None


def _w276_daily_after_set(market):
    """Called by _fetch_with_cache right after a daily frame is cached. A frame Yahoo just delivered keeps today's
    4-hour re-use. Any other frame is NOT stamped fresh: it expires after the back-off (30 min, 1 h, 2 h, then 4 h),
    or later if 6 asks were already made in the last 24 hours. Never raises."""
    try:
        st = _w276_daily.get(market)
        key = _cache_key(market, "1d")
        e = _cache.get(key)
        if not st or e is None or st.get("fresh"):
            return
        now = time.time()
        if st.get("pending"):
            st["src"] = _last_source.get(key) or "UNKNOWN"
            st["pending"] = False
            logger.warning("Daily frame %s: %s - %s", market, _W276_DAILY_NAMES.get(st["src"], st["src"]),
                           _w276_daily_desc(e["df"]))
        n = max(1, int(st.get("refusals", 0) or 1))
        wait = _W276_DAILY_BACKOFF_S[min(n, len(_W276_DAILY_BACKOFF_S)) - 1]
        asks = sorted(t for t in st.get("asks", []) if now - t < 86400)
        if len(asks) >= _W276_DAILY_MAX_ASKS:
            wait = max(wait, asks[-_W276_DAILY_MAX_ASKS] + 86400 - now + 1)
        e["ts"] = now - _W276_DAILY_TTL_S + wait
        logger.info("Daily frame %s: Yahoo asked again in %d min", market, int(round(wait / 60.0)))
    except Exception as exc:
        logger.warning("Daily frame %s: back-off not set (%s)", market, exc)


def _fetch_raw(market: str, timeframe: str) -> pd.DataFrame:
    """
    Dispatch to data source by market. Source priority (Wave 62):
      NQ/GC 15m/1h/4h: TopstepX (real CME) -> TwelveData -> yfinance
      NQ/GC 1d:        yfinance (continuous, ~2y dailies) -> TopstepX -> TwelveData
      BTC/SOL:         ccxt (unchanged)

    Wave 62 (_WAVE62_DAILY): TopstepX serves bars for the FRONT contract
    only (e.g. ENQ.M26), which carries just weeks-to-months of daily
    history -- GC's contract returned 0 daily bars and NQ's only ~63,
    starving the higher-timeframe bias and long EMAs. The continuous
    contracts on yfinance (NQ=F / GC=F) carry ~2 years of dailies, and
    with the Wave 61 four-hour cache TTL this costs only a handful of
    calls per day. Intraday stays on TopstepX where CME precision
    matters. A TopstepX success also resets the fallback-streak counter
    so DATA ALERTs stop after recovery.
    """
    market_upper = market.upper()

    if market_upper in _CRYPTO_MARKETS:
        return _fetch_crypto(market_upper, timeframe)

    if market_upper in _FUTURES_MARKETS:
        fb_key = f"{market_upper}|{timeframe}"

        # Wave 62: daily bars come from the continuous contract on yfinance
        # _WAVE276_DAILY_FRAME (F24): Yahoo fresh -> the frame already held -> the disk copy -> TopstepX, each one
        # logged by where it really came from, with its newest daily bar and bar count (_w276_daily_frame).
        if timeframe == "1d":
            df_yf = _w276_daily_frame(market_upper)
            if df_yf is not None and len(df_yf) >= MIN_BARS:
                _td_fallback_count[fb_key] = 0
                return df_yf

        # PRIMARY: TopstepX (real CME futures data)
        df_tsx = _w279_scan_fetch(market_upper, timeframe)   # _WAVE279_TOPSTEPX_HEALTH: + one retry after a timeout
        if df_tsx is not None and len(df_tsx) >= MIN_BARS:
            _td_fallback_count[fb_key] = 0  # Wave 62: healthy again -> clear streak
            logger.info("Using TopstepX for %s %s (%d bars)", market_upper, timeframe, len(df_tsx))
            return df_tsx
        # _WAVE279_TOPSTEPX_HEALTH (Q69 A - Wayne, 8 Oct 2026: no stand-in, ever): NQ / gold 15m, 1h, 4h come from
        # TopstepX ONLY. TwelveData (QQQ / XAU/USD spot) and Yahoo (delayed) are not asked: _fetch_with_cache serves
        # the kept TopstepX frame with its age, and scan_market writes no new call from one over 10 minutes old
        # (w279_block_reason). Only the daily frame goes on down the chain below, as before (Wave 276).
        if timeframe != "1d":
            logger.warning("TopstepX failed for %s %s - no stand-in: TwelveData and Yahoo are not asked for NQ / "
                           "gold intraday (data/topstepx_health.jsonl says why)", market_upper, timeframe)
            return pd.DataFrame(columns=_STANDARD_COLS)
        logger.info("TopstepX insufficient/unavailable for %s %s -- trying TwelveData", market_upper, timeframe)

        # FALLBACK 1: TwelveData
        df_td = _fetch_twelvedata(market_upper, timeframe)
        if df_td is not None and len(df_td) >= MIN_BARS:
            logger.info("Using TwelveData for %s %s (%d bars)", market_upper, timeframe, len(df_td))
            return df_td

        # FALLBACK 2: yfinance (last resort for intraday; already tried for 1d)
        if timeframe == "1d":
            return pd.DataFrame(columns=_STANDARD_COLS)

        _td_fallback_count[fb_key] = _td_fallback_count.get(fb_key, 0) + 1
        n = _td_fallback_count[fb_key]
        _last_source[fb_key] = "yfinance"
        logger.warning(
            "Fallback chain to yfinance for %s %s (attempt %d).",
            market_upper, timeframe, n
        )
        if n >= 3:
            logger.error(
                "DATA ALERT: %s %s using yfinance fallback %dx in a row -- check TopstepX auth & symbols",
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
    # _WAVE279_TOPSTEPX_HEALTH: NQ / gold intraday while CME is shut (5-6 PM ET, the weekend) - the kept TopstepX
    # frame as it is (its fetch time unchanged), TopstepX not asked, nothing backed off. None otherwise.
    _w279_k = _w279_shut_frame(market_upper, timeframe)
    if _w279_k is not None:
        return _w279_k

    # Wave 95: negative-fetch backoff. A frame that just came back
    # insufficient is NOT re-hammered every cycle (the NQ 1d Sept-contract
    # case was spamming yfinance/TwelveData rate limits ~every 30s). Serve
    # stale cache if we have it, else empty, until the cooldown elapses.
    _neg_cd = {"1d": 1800, "4h": 600, "1h": 300, "15m": 120}.get(timeframe, 300)
    _nkey = _cache_key(market_upper, timeframe)
    _nts = _neg_fetch.get(_nkey)
    if _nts is not None and (time.time() - _nts) < _neg_cd:
        _stale = _get_stale_cache(market_upper, timeframe)
        if _stale is not None:
            _w279_kept(market_upper, timeframe)   # _WAVE279_TOPSTEPX_HEALTH: "kept the TopstepX frame (age N s)"
            return _stale
        return pd.DataFrame(columns=_STANDARD_COLS)

    # Fetch fresh data
    try:
        df = _fetch_raw(market_upper, timeframe)
    except Exception as exc:
        logger.error("Unexpected error fetching %s %s: %s", market_upper, timeframe, exc)
        df = pd.DataFrame(columns=_STANDARD_COLS)

    # Validate
    if df is not None and len(df) >= MIN_BARS:
        _set_cache(market_upper, timeframe, df)
        if timeframe == "1d":
            _w276_daily_after_set(market_upper)   # _WAVE276_DAILY_FRAME: a frame served after a refusal is not stamped fresh
            _w279_daily_note(market_upper, df)   # _WAVE279_TOPSTEPX_HEALTH: source + bars to the jsonl when they change
        _neg_fetch.pop(_cache_key(market_upper, timeframe), None)  # Wave 95: success clears backoff
        return df.copy()

    # Wave 95: record the insufficient result so we back off next cycle
    _neg_fetch[_cache_key(market_upper, timeframe)] = time.time()

    # Not enough data -- try stale cache
    logger.warning("Insufficient data for %s %s (%d bars, need %d)",
                    market_upper, timeframe, 0 if df is None else len(df), MIN_BARS)
    stale = _get_stale_cache(market_upper, timeframe)
    if stale is not None:
        _w279_kept(market_upper, timeframe)   # _WAVE279_TOPSTEPX_HEALTH: "kept the TopstepX frame (age N s)"
        return stale

    # Return whatever we got (possibly empty)
    if df is not None:
        return df
    return pd.DataFrame(columns=_STANDARD_COLS)


# ---------------------------------------------------------------------------
# _WAVE269_FRESH_ENTRY: a 1h / 4h frame's last bar, brought up to date from the 15m frame
# ---------------------------------------------------------------------------
_W269_BAR_MIN = {"1h": 60, "4h": 240}


def w269_fresh_tail(df_tf, df_15m, timeframe, market=""):
    """Q44 option A (Wayne, 2 Oct 2026). A 1h / 4h frame is re-used for up to 15 / 60 minutes (_get_cached) with
    its forming bar, and detect_setups takes a call's entry from that bar's close - so a 4h call could quote a
    price the market left an hour ago, and the same stale call was written again and again (gold: 93 exact
    repeats, every one on 4h). This brings the frame's last bar up to date from the 15m frame, which is
    re-fetched every 3 minutes: High, Low, Close and Volume, plus the next bar when one began since the frame
    was fetched. No provider is asked for anything.

    Returns a NEW frame - or df_tf itself, untouched, whenever it cannot be sure: an unknown timeframe, an empty
    frame, a 15m frame that does not reach back to the bar's open, a frame fetched after the 15m frame, frames
    from two different providers, a frame more than one bar behind, or two frames that disagree about the bar.
    Never raises.

    _WAVE275_REOPEN_FRESH (review F01 / F46, 5 Oct 2026): "behind" is counted in TRADED bars - a weekend or a daily
    break is one bar, not twelve - so the new bar after a reopen is added at its own time; and every path that hands
    the frame back untouched writes one log line saying why (on 4 Oct it was silent while Friday's 4h frame was
    scanned for 50 minutes)."""
    mkt = str(market or "").upper()

    def _as_fetched(why):
        try:
            logger.info("W269 fresh tail %s %s: left as fetched - %s", mkt, timeframe, why)
        except Exception:
            pass
        return df_tf

    try:
        dur = _W269_BAR_MIN.get(timeframe)
        if dur is None:
            return _as_fetched("not a 1h / 4h frame")
        if df_tf is None or df_15m is None or len(df_tf) == 0 or len(df_15m) == 0:
            return _as_fetched("an empty frame")
        e_tf, e_15 = _cache.get(_cache_key(mkt, timeframe)), _cache.get(_cache_key(mkt, "15m"))
        if e_tf and e_15 and e_tf.get("ts", 0) >= e_15.get("ts", 0):
            return _as_fetched("fetched after the 15m frame, so it is the fresher one")
        if _last_source.get(_cache_key(mkt, timeframe)) != _last_source.get(_cache_key(mkt, "15m")):
            return _as_fetched("the two frames came from different providers")
        t_last = df_tf.index[-1]
        if df_15m.index[0] > t_last:
            return _as_fetched("the 15m frame does not reach back to this bar's open")
        tail = df_15m[df_15m.index >= t_last]
        if len(tail) == 0:
            return _as_fetched("the 15m frame ends before this bar")
        bucket = np.asarray((tail.index - t_last).total_seconds() // (dur * 60)).astype(int)
        later = sorted(set(int(b) for b in bucket) - {0})
        if len(later) > 1:
            return _as_fetched("%d traded bars behind: too old to patch" % len(later))
        cur = tail[bucket == 0]
        if len(cur) == 0:
            return _as_fetched("the 15m frame has nothing inside this bar")
        last = df_tf.iloc[-1]
        old_h, old_l, old_c = float(last["High"]), float(last["Low"]), float(last["Close"])
        lo0, hi0 = float(cur["Low"].min()), float(cur["High"].max())
        tol = 0.0005 * abs(old_c)
        if not (lo0 - tol <= old_l and old_h <= hi0 + tol and lo0 - tol <= old_c <= hi0 + tol):
            logger.info("W269 fresh tail %s %s: not used - the frames disagree about the bar (bar H/L/C "
                        "%.4f/%.4f/%.4f, 15m range %.4f-%.4f)", mkt, timeframe, old_h, old_l, old_c, lo0, hi0)
            return df_tf
        out = df_tf.copy()
        for _c in ("Open", "High", "Low", "Close", "Volume"):
            out[_c] = out[_c].astype(float)
        new_c = float(cur["Close"].iloc[-1])
        out.iloc[-1, out.columns.get_loc("High")] = max(old_h, hi0)
        out.iloc[-1, out.columns.get_loc("Low")] = min(old_l, lo0)
        out.iloc[-1, out.columns.get_loc("Close")] = new_c
        out.iloc[-1, out.columns.get_loc("Volume")] = max(float(last["Volume"]), float(cur["Volume"].sum()))
        nxt = tail[bucket == later[0]] if later else tail.iloc[:0]
        if len(nxt) > 0:                                   # a new bar began since the frame was fetched
            row = pd.DataFrame({"Open": [float(nxt["Open"].iloc[0])], "High": [float(nxt["High"].max())],
                                "Low": [float(nxt["Low"].min())], "Close": [float(nxt["Close"].iloc[-1])],
                                "Volume": [float(nxt["Volume"].sum())]},
                               index=[t_last + pd.Timedelta(minutes=dur * later[0])])
            row.index.name = out.index.name
            out = pd.concat([out, row.reindex(columns=out.columns)])
            new_c = float(row["Close"].iloc[0])
        if new_c != old_c or len(nxt) > 0:
            logger.info("W269 fresh tail %s %s: close %.4f -> %.4f%s", mkt, timeframe, old_c, new_c,
                        " (+1 new bar)" if len(nxt) > 0 else "")
        return out
    except Exception as exc:
        try:
            logger.warning("W269 fresh tail skipped for %s %s (%s)", market, timeframe, exc)
        except Exception:
            pass
        return df_tf


# ---------------------------------------------------------------------------
# _WAVE275_REOPEN_FRESH: nothing scanned after a reopen is priced from bars saved before it (Q56 / review F01)
# ---------------------------------------------------------------------------
_W275_BAR_MIN = {"15m": 15, "1h": 60, "4h": 240}
_W275_RETRY_S = {"15m": 25, "1h": 90, "4h": 90}   # re-ask after: 15m on the next scan (a reopen scans every 30 s);
                               # a 1h / 4h frame after 3 scans - Wave 269 brings it up to date from a newer 15m first
_W275_RETRY_WINDOW_S = 1800    # ...only in the first 30 minutes after a reopen, so a wrong calendar cannot ask all night
_W275_TOL = 0.0001             # a bar holding the reopen: its close may sit 0.01% outside the 15m range since the reopen
_w275_redo = {}                # cache key -> reopen (epoch s): a frame the gate refused, asked for again on the next scan
_w275_said = {}                # cache key -> reopen (epoch s) already logged as "fetched fresh"


def w275_last_reopen(now_utc=None):
    """The latest CME Globex reopen for NQ / gold at or before `now_utc` (default: now), as a UTC datetime, or None
    when it cannot be worked out (then nothing here changes anything). The moment is session_clock's
    FUTURES_SESSION_OPEN - 6:00 PM New York time, Sunday to Thursday - taken in New York time, so the summer / winter
    change is right: 22:00 UTC in summer, 23:00 UTC in winter. An evening whose next trade date is a full closure in
    rules.FUTURES_HOLIDAYS is not a reopen (the market stays shut). Never raises."""
    try:
        import datetime as _dtm
        import session_clock as _sc
        tz = _sc.ET_ZONE
        if tz is None:
            return None
        sched = [s for s in _sc._EVENT_SCHEDULE if s[0] == _sc.SessionEvent.FUTURES_SESSION_OPEN]
        if len(sched) != 1:
            return None
        _ev, hh, mm, days = sched[0]
        try:
            from rules import FUTURES_HOLIDAYS as _hol
        except Exception:
            _hol = {}
        if now_utc is None:
            now_utc = datetime.fromtimestamp(time.time(), timezone.utc)
        now_et = now_utc.astimezone(tz)
        for back in range(0, 15):
            d = now_et.date() - _dtm.timedelta(days=back)
            if days is not None and d.weekday() not in days:
                continue
            cand = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
            if cand > now_et:
                continue
            full = _hol.get((d + _dtm.timedelta(days=1)).strftime("%Y-%m-%d"))
            if full and full[0] is None:
                continue
            return cand.astimezone(timezone.utc)
    except Exception:
        return None
    return None


def _w275_reopen_expired(market, timeframe, entry):
    """_get_cached's extra rule for NQ / gold 15m, 1h, 4h. True = treat the saved frame as expired now:
    (1) it was saved before the latest reopen - fetched fresh on the first scan at or after it, at once;
    (2) in the first 30 minutes after the reopen, once it is 25 s old (15m) / 90 s old (1h, 4h): it still has no bar
        from the new session, or the reopen gate refused it. Never raises (False on any doubt: the normal re-use
        times then apply)."""
    try:
        if market not in _FUTURES_MARKETS or timeframe not in _W275_BAR_MIN:
            return False
        reo = w275_last_reopen()
        if reo is None:
            return False
        r, now, key = reo.timestamp(), time.time(), _cache_key(market, timeframe)
        ts = float(entry.get("ts", 0))
        if ts < r:
            if _w275_said.get(key) != r:
                _w275_said[key] = r
                logger.info("REOPEN-FRESH %s %s: saved %s UTC, before the %s UTC reopen - fetched fresh now",
                            market, timeframe, datetime.fromtimestamp(ts, timezone.utc).strftime("%a %H:%M:%S"),
                            reo.strftime("%a %H:%M"))
            return True
        if now - r > _W275_RETRY_WINDOW_S or now - ts < _W275_RETRY_S[timeframe]:
            return False
        if _w275_redo.get(key) == r:
            _w275_redo.pop(key, None)
            return True
        df = entry.get("df")
        if df is not None and len(df) > 0:
            if df.index[-1] + pd.Timedelta(minutes=_W275_BAR_MIN[timeframe]) <= pd.Timestamp(reo):
                return True
        return False
    except Exception:
        return False


def w275_reopen_gate(market, timeframe, df_e, df_15m):
    """Called by scan_market for each entry timeframe, after the Wave 269 patch, before any setup is priced.
    None = scan it. Otherwise the reason it must not be priced this scan; a reason starting "market:" means nothing
    in this market can be priced this scan (its 15m frame has no bar from the new session yet). BTC / SOL: always
    None. Never raises: on its own failure it logs a warning and says None (the scan goes on as before)."""
    try:
        mkt = str(market or "").upper()
        if mkt not in _FUTURES_MARKETS:
            return None
        reo = w275_last_reopen()
        if reo is None:
            return None
        R = pd.Timestamp(reo)
        rs = reo.strftime("%a %H:%M UTC")
        if df_15m is None or len(df_15m) == 0 or df_15m.index[-1] < R:
            _w275_redo[_cache_key(mkt, "15m")] = reo.timestamp()
            newest = "none" if (df_15m is None or len(df_15m) == 0) else df_15m.index[-1].strftime("%a %H:%M UTC")
            return ("market: the provider has no bar from the new session yet (newest 15m bar %s, reopen %s) - "
                    "not priced from old bars" % (newest, rs))
        dur = _W275_BAR_MIN.get(timeframe)
        if dur is None or timeframe == "15m" or df_e is None or len(df_e) == 0:
            return None
        key, k15 = _cache_key(mkt, timeframe), _cache_key(mkt, "15m")
        t_last = df_e.index[-1]
        end = t_last + pd.Timedelta(minutes=dur)
        if end <= R:
            _w275_redo[key] = _w275_redo[k15] = reo.timestamp()     # a newer 15m frame lets Wave 269 catch it up
            return "its newest bar (%s) is from before the %s reopen" % (t_last.strftime("%a %H:%M UTC"), rs)
        if t_last < R:                                     # the bar holds the reopen: its close must be a new price
            post = df_15m[(df_15m.index >= R) & (df_15m.index < end)]
            c = float(df_e["Close"].iloc[-1])
            if len(post) == 0:
                _w275_redo[key] = _w275_redo[k15] = reo.timestamp()
                return "its bar holding the %s reopen has no 15m bar since the reopen to check against" % rs
            lo, hi = float(post["Low"].min()), float(post["High"].max())
            tol = _W275_TOL * abs(c)
            if not (lo - tol <= c <= hi + tol):
                _w275_redo[key] = _w275_redo[k15] = reo.timestamp()
                return ("its close %.4f has not traded since the %s reopen (15m range since then %.4f-%.4f)"
                        % (c, rs, lo, hi))
        _w275_redo.pop(key, None)                          # priced fine now: no need to ask for it again
        return None
    except Exception as exc:
        try:
            logger.warning("REOPEN-FRESH gate failed for %s %s (%s) - scanned as before", market, timeframe, exc)
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# _WAVE279_TOPSTEPX_HEALTH (Q69 A - Wayne, 8 Oct 2026: "if Top Step fails, we should see why it failed and fix it")
# ---------------------------------------------------------------------------
# NQ / gold 15m, 1h and 4h come from TopstepX ONLY - never QQQ / XAU/USD spot (TwelveData) or Yahoo's delayed bars,
# which is what _fetch_raw fell through to before (2 Oct 21:49 UTC: 500 QQQ bars served as NQ's 15m chart). When
# TopstepX fails, the last good TopstepX frame stays in the cache with its age: up to W279_KEEP_MAX_S it is used and
# the log says "kept the TopstepX frame (age N s)"; older, scan_market writes no new call (real or LAB) in that market
# until TopstepX answers (w279_block_reason). Every failed TopstepX request is one line in data/topstepx_health.jsonl.
# The daily frame (1d) keeps Wave 276's chain; its source and bar count are written to the same file when they change.
import json as _w279_json
import re as _w279_re
import threading as _w279_threading

W279_KEEP_MAX_S = 600            # a kept TopstepX frame is used up to this age (s); older: no new call in that market
W279_TIMEOUT_RETRY_S = 2.0       # a scan fetch that timed out is asked once more after this many seconds
W279_DEAD_S = 60.0               # a timeout AND its retry: that market's other frames are not asked for this long
W279_HEALTH_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "data", "topstepx_health.jsonl")
_W279_INTRADAY = ("15m", "1h", "4h")
_W279_LOCK = _w279_threading.RLock()
_W279_TL = _w279_threading.local()   # this thread's retrieveBars requests (FRESH-PRICE fetches on its own threads)
_w279_state = {}                 # "NQ|15m" -> {"failing": bool, "since": first failure in a row (epoch s), "why": str}
_w279_env = {"refused_at": None}  # when this account last refused live=True (None: live=True is asked)
_w279_fetches = []               # [(epoch s, ok)] - every TopstepX fetch of the last hour (market-shut skips not counted)
_w279_last = {"failure": None}   # the newest failure line written
_w279_dead = {}                  # market -> time.monotonic() when a timeout and its retry both timed out
_w279_said = {}                  # ("shut", market) / ("block", market) -> already said
_w279_daily_seen = {}            # market -> (source, bars) of the daily frame last written
_W279_SHUT_NAMES = {"break": "the 5-6 PM ET daily break", "weekend": "the weekend close"}


def w279_market_shut(now_utc=None):
    """"break" (5:00-6:00 PM New York time, Monday to Thursday) or "weekend" (Friday 5:00 PM to Sunday 6:00 PM New York
    time) while CME Globex is shut for NQ and gold, else None. New York time, so summer and winter are both right. No
    holiday calendar: on a holiday TopstepX is asked as on any day. Never raises (None on any doubt)."""
    try:
        import session_clock as _sc
        tz = _sc.ET_ZONE
        if tz is None:
            return None
        if now_utc is None:
            now_utc = datetime.fromtimestamp(time.time(), timezone.utc)
        et = now_utc.astimezone(tz)
        wd, hm = et.weekday(), et.hour * 60 + et.minute
        if wd == 5 or (wd == 4 and hm >= 1020) or (wd == 6 and hm < 1080):
            return "weekend"
        if wd in (0, 1, 2, 3) and 1020 <= hm < 1080:
            return "break"
        return None
    except Exception:
        return None


def w279_live_flag():
    """The live flag for the next retrieveBars: False once this account refused live=True, until the first fetch after
    the next 6 PM ET reopen (w275_last_reopen), which asks live=True once again. Never raises (True on any doubt)."""
    try:
        ra = _w279_env.get("refused_at")
        if ra is None:
            return True
        reo = w275_last_reopen()
        if reo is not None and float(ra) < reo.timestamp():
            return True
        return False
    except Exception:
        return True


def _w279_redact(s, n=200):
    """A short, one-line snippet for the jsonl: anything that looks like a token, a key or a password is replaced
    first, then it is cut to n characters. Never raises."""
    try:
        t = str("" if s is None else s)
        t = _w279_re.sub(r"eyJ[A-Za-z0-9_\-]{4,}(?:\.[A-Za-z0-9_\-]*){0,2}", "[redacted]", t)
        t = _w279_re.sub(r"(?i)(bearer\s+)\S+", r"\1[redacted]", t)
        t = _w279_re.sub(r"(?i)(\"?(?:token|apikey|api_key|key|password|username)\"?\s*[:=]\s*)\"?[^\",}\s]+\"?",
                         r"\1[redacted]", t)
        for k in ("TOPSTEPX_API_KEY", "TOPSTEPX_USERNAME"):
            v = _os.environ.get(k, "").strip()
            if len(v) >= 4:
                t = t.replace(v, "[redacted]")
        return " ".join(t.split())[:n]
    except Exception:
        return ""


def _w279_rec_post(body, post):
    """One retrieveBars request, exactly as before (an exception is raised again, the response returned), timed and
    noted for this thread: the live flag it asked with, the status, success, the number of bars, or the exception."""
    rec = {"live": body.get("live"), "ms": None, "status": None, "exc": None, "msg": "", "success": None, "bars": None}
    reqs = getattr(_W279_TL, "reqs", None)
    if reqs is not None:
        reqs.append(rec)
    t0 = time.monotonic()
    try:
        resp = post()
    except Exception as exc:
        rec.update(ms=int(round((time.monotonic() - t0) * 1000)), exc=type(exc).__name__, msg=_w279_redact(exc))
        raise
    rec["ms"] = int(round((time.monotonic() - t0) * 1000))
    try:
        rec["status"] = int(resp.status_code)
        if rec["status"] == 200:
            try:
                d = resp.json()
                rec["success"] = bool(d.get("success"))
                rec["bars"] = len(d.get("bars") or [])
                if not rec["success"]:
                    rec["msg"] = _w279_redact("errorCode=%s errorMessage=%s" % (d.get("errorCode"), d.get("errorMessage")))
            except Exception as exc:
                rec["exc"] = type(exc).__name__
                rec["msg"] = _w279_redact(getattr(resp, "text", ""))
        else:
            rec["msg"] = _w279_redact(getattr(resp, "text", ""))
    except Exception:
        pass
    return resp


def _w279_req_kind(r):
    """Why one request failed, or None when it brought bars."""
    if r.get("exc"):
        return "exception"
    if r.get("status") is None:
        return "no_answer"
    if r.get("status") != 200:
        return "http_%s" % r.get("status")
    if r.get("success") is False:
        return "live_true_refused" if r.get("live") is True else "refused"
    if not r.get("bars"):
        return "empty"
    return None


def _w279_is_timeout(r):
    return bool(r) and ("timeout" in str(r.get("exc") or "").lower() or "timed out" in str(r.get("msg") or "").lower())


def _w279_note(rec):
    """Append one line to data/topstepx_health.jsonl. Never raises."""
    try:
        line = _w279_json.dumps(rec, default=str)
        with _W279_LOCK:
            _os.makedirs(_os.path.dirname(W279_HEALTH_FILE), exist_ok=True)
            with open(W279_HEALTH_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            if rec.get("kind") != "daily_frame":
                _w279_last["failure"] = dict(rec)
    except Exception as exc:
        try:
            logger.warning("TOPSTEPX-HEALTH: jsonl line not written (%s)", exc)
        except Exception:
            pass


def _w279_line(m, tf, kind, r, ok, shut):
    r = r or {}
    return {"utc": datetime.fromtimestamp(time.time(), timezone.utc).isoformat(), "market": m, "timeframe": tf,
            "kind": kind, "live": r.get("live"), "exception": r.get("exc"), "http_status": r.get("status"),
            "ms": r.get("ms"), "body": _w279_redact(r.get("msg") or ""), "market_shut": shut, "fetch_ok": bool(ok)}


def _w279_after_fetch(m, tf, ok, reqs, raised, shut):
    """The bookkeeping after one TopstepX fetch: the account's environment, the hour's counts, one jsonl line per
    failed request (or one for a fetch that failed before a request was sent), and the frame's failing state."""
    now = time.time()
    key = _cache_key(m, tf)
    refused = [r for r in reqs if _w279_req_kind(r) == "live_true_refused"]
    with _W279_LOCK:
        if refused:
            _w279_env["refused_at"] = now
            logger.info("TopstepX: live=True refused (%s %s) - this account is on the Combine sim feed; live=False is "
                        "asked straight away from now on (one request a fetch), and live=True once again at the next "
                        "6 PM ET open", m, tf)
        elif ok and reqs and reqs[-1].get("live") is True and _w279_env.get("refused_at") is not None:
            _w279_env["refused_at"] = None
            logger.warning("TopstepX: live=True answered (%s %s) - the funded-environment feed; asked from now on", m, tf)
        elif (not ok) and reqs and _w279_req_kind(reqs[-1]) == "refused":
            _w279_env["refused_at"] = None     # live=False refused too: the next fetch asks live=True first
        _w279_fetches.append((now, bool(ok)))
        while _w279_fetches and _w279_fetches[0][0] < now - 3600:
            _w279_fetches.pop(0)
        _w279_said.pop(("shut", m), None)
    failed = [r for r in reqs if _w279_req_kind(r)]
    for r in failed:
        _w279_note(_w279_line(m, tf, _w279_req_kind(r), r, ok, shut))
    why = ""
    if not ok and not failed:
        if raised is not None:
            why = "raised"
            _w279_note(_w279_line(m, tf, why, {"exc": type(raised).__name__, "msg": _w279_redact(raised)}, ok, shut))
        elif not reqs:
            why = "no_token" if _TOPSTEPX_TOKEN is None else "no_contract"
            _w279_note(_w279_line(m, tf, why, {"msg": "no retrieveBars request was sent (%s) - see the TopstepX auth / "
                                               "contract line in the log" % why.replace("_", " ")}, ok, shut))
        else:
            why = "too_few_bars"
            _w279_note(_w279_line(m, tf, why, reqs[-1], ok, shut))
    elif failed:
        why = _w279_req_kind(failed[-1])
    _W279_TL.timed_out = (not ok) and bool(reqs) and _w279_is_timeout(reqs[-1])
    with _W279_LOCK:
        st = _w279_state.setdefault(key, {"failing": False, "since": None, "why": None})
        if ok:
            was = st["failing"]
            st.update(failing=False, since=None, why=None)
            _w279_dead.pop(m, None)
            if was:
                logger.warning("TopstepX %s %s: answers again", m, tf)
            for t2 in _W279_INTRADAY:   # TopstepX answers: the market's other failing frames are asked on the next scan
                if t2 != tf and (_w279_state.get(_cache_key(m, t2)) or {}).get("failing"):
                    _neg_fetch.pop(_cache_key(m, t2), None)
        else:
            if not st["failing"]:
                st["since"] = now
            st.update(failing=True, why=why or "failed")


def _w279_say_shut(m, shut):
    with _W279_LOCK:
        if _w279_said.get(("shut", m)):
            return
        _w279_said[("shut", m)] = True
    logger.info("TopstepX %s: market shut (%s) - no new bars asked until it reopens at 6 PM ET; the kept TopstepX "
                "frames are used as they are", m, _W279_SHUT_NAMES.get(shut, shut))


def _fetch_topstepx(market: str, timeframe: str) -> pd.DataFrame:
    """_WAVE279_TOPSTEPX_HEALTH: the TopstepX fetch (_w279_fetch_topstepx_raw - its own code, but for the live flag
    and the request notes), with: no intraday bars asked while CME is shut when a kept frame exists; every failed
    request written to data/topstepx_health.jsonl; the fetch counted for the heartbeat. Returns what the raw fetch
    returns (an empty frame on any failure); an exception it raises is raised again after it is written down."""
    m = str(market).upper()
    _W279_TL.timed_out = False
    if m not in _TOPSTEPX_SYMBOL_FILTER or timeframe not in _TOPSTEPX_TF_MAP:
        return _w279_fetch_topstepx_raw(market, timeframe)
    shut = w279_market_shut()
    if shut and timeframe in _W279_INTRADAY and _cache_key(m, timeframe) in _cache:
        _w279_say_shut(m, shut)
        return pd.DataFrame(columns=_STANDARD_COLS)
    _W279_TL.reqs = []
    df, raised = None, None
    try:
        df = _w279_fetch_topstepx_raw(market, timeframe)
    except Exception as exc:
        raised = exc
    reqs = list(getattr(_W279_TL, "reqs", None) or [])
    _W279_TL.reqs = None
    ok = df is not None and len(df) >= MIN_BARS
    try:
        _w279_after_fetch(m, timeframe, ok, reqs, raised, shut)
    except Exception as exc:
        logger.warning("TOPSTEPX-HEALTH: bookkeeping failed for %s %s (%s)", m, timeframe, exc)
    if raised is not None:
        raise raised
    return df


def _w279_scan_fetch(market, timeframe):
    """_fetch_raw's TopstepX fetch. NQ / gold 15m, 1h, 4h: a fetch that TIMED OUT is asked once more after
    W279_TIMEOUT_RETRY_S; when the retry times out too, that market's other frames are not asked for W279_DEAD_S, so a
    dead TopstepX holds the scan up once a pass, not once a frame. The daily frame: _fetch_topstepx, as before."""
    if timeframe not in _W279_INTRADAY:
        return _fetch_topstepx(market, timeframe)
    d = _w279_dead.get(market)
    if d is not None and time.monotonic() - d < W279_DEAD_S:
        with _W279_LOCK:
            st = _w279_state.setdefault(_cache_key(market, timeframe), {"failing": False, "since": None, "why": None})
            if not st["failing"]:
                st["since"] = time.time()
            st.update(failing=True, why="not asked: timed out twice")
        logger.warning("TopstepX %s %s: not asked - TopstepX timed out twice for %s %.0f s ago (the scan is not held "
                       "up again for %.0f s)", market, timeframe, market, time.monotonic() - d, W279_DEAD_S)
        return pd.DataFrame(columns=_STANDARD_COLS)
    df = _fetch_topstepx(market, timeframe)
    if (df is not None and len(df) >= MIN_BARS) or not getattr(_W279_TL, "timed_out", False):
        return df
    logger.warning("TopstepX %s %s: timed out - asked once more in %.0f s", market, timeframe, W279_TIMEOUT_RETRY_S)
    time.sleep(W279_TIMEOUT_RETRY_S)
    df = _fetch_topstepx(market, timeframe)
    if (df is None or len(df) < MIN_BARS) and getattr(_W279_TL, "timed_out", False):
        _w279_dead[market] = time.monotonic()
        logger.warning("TopstepX %s %s: timed out again - %s's other frames are not asked for %.0f s",
                       market, timeframe, market, W279_DEAD_S)
    return df


def _w279_shut_frame(market, timeframe):
    """_fetch_with_cache, NQ / gold 15m, 1h, 4h, the saved frame past its re-use time: while CME is shut the kept
    TopstepX frame is served as it is (its fetch time unchanged, so its age stays true) and TopstepX is not asked.
    None otherwise (and when no frame is kept - a boot while shut asks once). Never raises."""
    try:
        if market not in _FUTURES_MARKETS or timeframe not in _W279_INTRADAY:
            return None
        e = _cache.get(_cache_key(market, timeframe))
        if e is None:
            return None
        shut = w279_market_shut()
        if not shut:
            return None
        _w279_say_shut(market, shut)
        return e["df"].copy()
    except Exception:
        return None


def _w279_kept(market, timeframe):
    """_fetch_with_cache is serving the kept frame after a failed fetch: say its age, and whether it may still be used
    for a new call. Never raises."""
    try:
        if market not in _FUTURES_MARKETS or timeframe not in _W279_INTRADAY:
            return
        e = _cache.get(_cache_key(market, timeframe))
        if e is None:
            return
        age = max(0.0, time.time() - float(e.get("ts", 0)))
        if age <= W279_KEEP_MAX_S:
            logger.warning("TopstepX %s %s: kept the TopstepX frame (age %d s)", market, timeframe, int(age))
        else:
            logger.warning("TopstepX %s %s: kept the TopstepX frame (age %d s) - over %d s: no new %s call until "
                           "TopstepX answers; open calls are graded on it", market, timeframe, int(age),
                           W279_KEEP_MAX_S, market)
    except Exception:
        pass


def w279_block_reason(market):
    """None = this market may write new calls. Otherwise why not: one of its 15m / 1h / 4h TopstepX fetches is failing
    and the kept frame is over W279_KEEP_MAX_S old (or there is none). One DATA ALERT line when a market blocks, one
    when it is free again. BTC / SOL: always None. Never raises (None on its own failure)."""
    try:
        m = str(market or "").upper()
        if m not in _FUTURES_MARKETS:
            return None
        now = time.time()
        worst = None
        with _W279_LOCK:
            for tf in _W279_INTRADAY:
                st = _w279_state.get(_cache_key(m, tf))
                if not st or not st.get("failing"):
                    continue
                e = _cache.get(_cache_key(m, tf))
                age = None if e is None else max(0.0, now - float(e.get("ts", 0)))
                if age is None or age > W279_KEEP_MAX_S:
                    worst = (tf, age, dict(st))
                    break
        if worst is None:
            with _W279_LOCK:
                was = _w279_said.pop(("block", m), None)
            if was:
                logger.error("DATA ALERT: TopstepX answers again for %s - new %s calls may be written again", m, m)
            return None
        tf, age, st = worst
        since = datetime.fromtimestamp(float(st.get("since") or now), timezone.utc).strftime("%H:%M:%S")
        why = ("TopstepX has not answered for %s %s since %s UTC (%s); %s" % (
            m, tf, since, st.get("why") or "failed",
            "no TopstepX frame is kept" if age is None else
            "the kept frame is %d s old - over the %d s limit" % (int(age), W279_KEEP_MAX_S)))
        with _W279_LOCK:
            first = not _w279_said.get(("block", m))
            _w279_said[("block", m)] = True
        if first:
            logger.error("DATA ALERT: %s - no new %s call (real or LAB) until TopstepX answers", why, m)
        return why
    except Exception:
        return None


def w279_health_summary():
    """The heartbeat's "topstepx" field: TopstepX fetches that brought bars / failed in the last hour (since the last
    boot at most), the newest failure line, and the markets blocked now. Never raises."""
    try:
        now = time.time()
        with _W279_LOCK:
            recent = [ok for t, ok in _w279_fetches if now - t <= 3600]
            last = _w279_last.get("failure")
            blocked = sorted(m for (k, m), v in _w279_said.items() if k == "block" and v)
        return {"ok_last_hour": sum(1 for x in recent if x), "failed_last_hour": sum(1 for x in recent if not x),
                "last_failure": dict(last) if last else None, "blocked": blocked}
    except Exception:
        return {"ok_last_hour": None, "failed_last_hour": None, "last_failure": None, "blocked": None}


def _w279_daily_note(market, df):
    """The daily frame's source and bar count, one jsonl line when either changes (log only). Never raises."""
    try:
        src = _last_source.get(_cache_key(market, "1d")) or "UNKNOWN"
        bars = 0 if df is None else int(len(df))
        prev = _w279_daily_seen.get(market)
        if prev == (src, bars):
            return
        _w279_daily_seen[market] = (src, bars)
        try:
            newest = df.index[-1].strftime("%Y-%m-%d")
        except Exception:
            newest = None
        _w279_note({"utc": datetime.fromtimestamp(time.time(), timezone.utc).isoformat(), "market": market,
                    "timeframe": "1d", "kind": "daily_frame", "source": src, "bars": bars, "newest": newest,
                    "before": list(prev) if prev else None})
    except Exception:
        pass
# ---- end _WAVE279_TOPSTEPX_HEALTH ------------------------------------------


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
    _feed115 = (" | feed=" + (_tsx_feed.get("mode") or "n/a")) if market_upper in _FUTURES_MARKETS else ""  # Wave 115
    # Wave 132 (_WAVE132_FEED_FRESHNESS): measure, don't assume. Log the age
    # of the newest 15m bar (from its OPEN time) so the logs PROVE whether
    # the feed is real-time or delayed. Interpretation: if partial bars are
    # included, a real-time feed shows age 0-15m; completed-bars-only shows
    # 15-30m; a 15-min-DELAYED feed shifts each range +15m (30-45m). Wave
    # 115's LIVE/DELAYED label only reflects which API flag worked - Topstep
    # includes real-time Level 1 data in the Combine, so the sim feed may
    # already be real-time; this field settles it with data.
    _fresh132 = ""
    try:
        _df15 = frames.get("15m")
        if _df15 is not None and len(_df15) > 0:
            _newest = _df15.index[-1]
            _age_min = (pd.Timestamp.now(tz="UTC") - _newest).total_seconds() / 60.0
            _fresh132 = " | newest15m=%s age=%.1fm" % (_newest.strftime("%H:%M"), _age_min)
    except Exception:
        _fresh132 = ""
    logger.info("Data check %s: %s | source=%s%s%s", market_upper, bar_counts, source_str, _feed115, _fresh132)

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
# _WAVE271_FRESH_PRICE (Q64 A / Q65 A - Wayne, 6 Oct 2026: "no data should be over a minute old")
# ---------------------------------------------------------------------------
# A saved 15m frame is re-used for up to 180 s (_get_cached), and get_current_price is its last close. So the
# 3:55 PM flatten and a call written in a 30-second scan window could use a price up to 3 minutes old - on 6 Oct
# the flatten closed two NQ longs 13 points above stops the market had traded through in the 27 seconds after the
# last fetch, and Wave 270 looked at the same saved bars. These fetch the 15m frame fresh at those two moments.
W271_FETCH_TIMEOUT_S = 6.0       # the longest a fresh fetch is waited for (all markets at once); then the saved frame
W271_ENTRY_MAX_AGE_S = 45.0      # a scan re-fetches a saved 15m frame older than this before it looks for setups
W271_AGE_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "data", "price_age.jsonl")
_W271_FRESH_TS: Dict[str, float] = {}     # market -> the cache time of the frame w271_fetch_fresh_15m saved
_W271_INFLIGHT: Dict[str, tuple] = {}     # market -> (thread, started) of a fetch still running past its timeout


def w271_saved_age(market):
    """Seconds since the saved 15m frame of this market was fetched, or None when there is none. Never raises."""
    try:
        e = _cache.get(_cache_key(str(market).upper(), "15m"))
        if not e:
            return None
        return max(0.0, time.time() - float(e.get("ts", 0)))
    except Exception:
        return None


def w271_fetch_fresh_15m(markets, why, timeout_s=None):
    """Fetch the 15m frame of each market fresh, NOT from the saved frame -> {market: (ok, detail)}.
    One request per market - NQ / gold: TopstepX only (_fetch_topstepx: no fall-through to another series); BTC /
    SOL: _fetch_raw (ccxt, as an expired cache does) - all markets at once on daemon
    threads, waited for at most timeout_s seconds in total (W271_FETCH_TIMEOUT_S). A frame of MIN_BARS bars or more
    is saved exactly as _fetch_with_cache saves one, so get_current_price and get_frames then return it. Anything
    else leaves the saved frame alone and logs one line. A fetch still running from an earlier timeout is not
    started again. Never raises."""
    out = {}
    try:
        import threading as _w271_threading
        tmo = W271_FETCH_TIMEOUT_S if timeout_s is None else float(timeout_s)
        jobs = {}
        for m in dict.fromkeys(str(x).upper() for x in (markets or ()) if x):
            prev = _W271_INFLIGHT.get(m)
            if prev is not None and prev[0].is_alive():
                out[m] = (False, "the fetch started %.0f s ago is still running" % (time.monotonic() - prev[1]))
                continue
            _W271_INFLIGHT.pop(m, None)
            box = {}

            def _job(m=m, box=box):
                try:
                    # NQ / gold: TopstepX ONLY - the series the saved frame came from. _fetch_raw would fall
                    # through to TwelveData (gold = XAU/USD spot) or Yahoo (delayed) and call that "fresh".
                    box["df"] = (_fetch_topstepx(m, "15m") if m in _FUTURES_MARKETS else _fetch_raw(m, "15m"))
                except Exception as exc:
                    box["err"] = "%s: %s" % (type(exc).__name__, exc)
            th = _w271_threading.Thread(target=_job, name="w271-fresh-" + m, daemon=True)
            jobs[m] = (th, box, time.monotonic())
            th.start()
        end = time.monotonic() + tmo
        for m, (th, box, st) in jobs.items():
            th.join(max(0.0, end - time.monotonic()))
            if th.is_alive():
                _W271_INFLIGHT[m] = (th, st)
                out[m] = (False, "no answer within %.0f s" % tmo)
                continue
            if "err" in box:
                out[m] = (False, box["err"])
                continue
            df = box.get("df")
            if df is None or len(df) < MIN_BARS:
                out[m] = (False, "the provider returned %d bars (need %d)" % (0 if df is None else len(df), MIN_BARS))
                continue
            _set_cache(m, "15m", df)
            _neg_fetch.pop(_cache_key(m, "15m"), None)
            _W271_FRESH_TS[m] = float(_cache[_cache_key(m, "15m")]["ts"])
            out[m] = (True, "%d bars, last close %.4f, %.1f s" % (len(df), float(df["Close"].iloc[-1]),
                                                                  time.monotonic() - st))
            logger.info("FRESH-PRICE: %s 15m fetched fresh for the %s (%s)", m, why, out[m][1])
        for m, (ok, detail) in out.items():
            if not ok:
                age = w271_saved_age(m)
                logger.warning("FRESH-PRICE: %s used the saved frame (age %s s) for %s: %s", why,
                               "?" if age is None else "%.0f" % age, m, detail)
    except Exception as exc:
        try:
            logger.warning("FRESH-PRICE: %s used the saved frames: the fresh fetch failed (%s)", why, exc)
        except Exception:
            pass
    return out


def w271_fresh_frames(market, frames, max_age_s=None):
    """A scan's frames with the 15m frame fetched fresh first when the saved one is older than max_age_s
    (W271_ENTRY_MAX_AGE_S), so a call written by this scan is priced from bars under a minute old. Returns a NEW
    dict when it fetched - the caller's dict is never changed - and frames itself otherwise. Never raises."""
    try:
        if not isinstance(frames, dict) or "15m" not in frames:
            return frames
        m = str(market).upper()
        lim = W271_ENTRY_MAX_AGE_S if max_age_s is None else float(max_age_s)
        age = w271_saved_age(m)
        if age is not None and age <= lim:
            return frames
        if m in _FUTURES_MARKETS and age is not None and w279_market_shut():   # _WAVE279_TOPSTEPX_HEALTH: CME shut
            return frames
        res = w271_fetch_fresh_15m([m], "entry").get(m)
        if not res or not res[0]:
            return frames
        out = dict(frames)
        out["15m"] = _cache[_cache_key(m, "15m")]["df"].copy()
        return out
    except Exception:
        return frames


def w271_note_price_age(alert_id, what, market, **extra):
    """One line in data/price_age.jsonl: how old the price a call was written from (what = entry) or closed at
    (what = flatten) was. age_sec = seconds since the 15m frame it came from was fetched; source = fresh when this
    item fetched that frame, saved when it is the frame the scan or the cache already had. The 15m frame's last
    close is written too, so a 1h / 4h entry that is not that close can be told apart. Never raises."""
    try:
        import json as _w271_json
        m = str(market or "").upper()
        e = _cache.get(_cache_key(m, "15m"))
        age = w271_saved_age(m)
        src = "saved"
        last = None
        if e:
            if _W271_FRESH_TS.get(m) == float(e.get("ts", -1)):
                src = "fresh"
            try:
                last = float(e["df"]["Close"].iloc[-1])
            except Exception:
                last = None
        else:
            src = "none"
        rec = {"alert_id": alert_id, "what": what, "market": m,
               "age_sec": None if age is None else round(age, 1), "source": src,
               "utc": datetime.now(timezone.utc).isoformat(), "last_15m_close": last}
        for k, v in extra.items():
            if k not in rec:
                rec[k] = v
        _os.makedirs(_os.path.dirname(W271_AGE_FILE), exist_ok=True)
        with open(W271_AGE_FILE, "a", encoding="utf-8") as fh:
            fh.write(_w271_json.dumps(rec, default=str) + "\n")
    except Exception as exc:
        try:
            logger.debug("FRESH-PRICE: price_age line not written (%s)", exc)
        except Exception:
            pass
# ---- end _WAVE271_FRESH_PRICE ---------------------------------------------


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
