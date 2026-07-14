"""
intermarket.py - NQ CALLS intermarket "tape" sensor
(Wave 108 origin, Wave 113 fetch-hardening, Wave 123 multi-source rewrite)
_WAVE123_INTERMARKET_MULTISOURCE

Reads a small set of correlated instruments and distills them into one honest
per-market read:

    get_tape("NQ") -> {"tape": "BULLISH"|"BEARISH"|"NEUTRAL"|"UNKNOWN",
                       "score": -1.0..+1.0, "detail": {...}, "sources": {...},
                       "age_s": float}

Why Wave 123: production ran ~99% UNKNOWN. Root cause: the sensor pulled every
confirmer from yfinance, and Yahoo rate-limits Railway's datacenter IP so hard
that ~every call returned empty. Fix: fetch each confirmer from TwelveData
first (the same paid-key REST source data_layer already uses for NQ/GC, which
works fine from Railway), and only fall back to yfinance if TwelveData misses.
Also: the confirmer set now uses TwelveData-friendly liquid ETFs/indices that
proxy the old futures cleanly (SPY for S&P futures, UUP for the dollar, TLT
inverse for 10y yields), so every confirmer is reliably fetchable for free.

Confirmers per market (invert=True means "up is risk-OFF" for this market).
Wave 134 (_WAVE134_CONFIRMER_EXPANSION): VIX (dead on TwelveData free) replaced
by VIXY (ETF proxy, fetchable), and the read set widened - every ticker chosen
to be TwelveData-carried so the whole tape runs on the proven "td" source:
    NQ  <- SPY (S&P), QQQ (Nasdaq), SMH (semis), IWM (small-caps breadth),
           HYG (credit risk appetite), NVDA (AI bellwether), VIXY (inverted)
    GC  <- UUP (dollar, inverted), TLT (bonds; up = yields down = gold bid),
           SLV (silver confirms metal moves), GDX (miners lead/confirm gold),
           VIXY (fear is a safe-haven BID for gold)
    BTC <- QQQ (risk appetite), ETH (crypto breadth), VIXY (inverted)
    SOL <- BTC (crypto leader), ETH (closest peer), QQQ (risk appetite)

Longevity / robustness (unchanged Wave 108 guarantees, kept intact):
  * SHADOW ONLY - never blocks, never fires, never affects a trade.
  * Every network call is paced and wrapped; get_tape never raises.
  * log_snapshot() fetches in a daemon thread (single-flight lock) so the
    async scan loop can never be stalled by fetch pacing.
  * LAST-GOOD memory cache serves recent-but-stale data if a refresh fails.
  * Refresh cadence is deliberately slow (~30 min) - this is regime context,
    not entry timing - which also keeps us well inside TwelveData's free
    daily request budget and stops any IP-throttling from over-fetching.
  * ASCII-only (Railway console is cp1252).

Each confirmer read is tagged with its source (td / yf / cache) and written to
the tape, so we can see at a glance which source is actually feeding the bot.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("intermarket")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TAPE_LOG_FILE = os.path.join(_BASE_DIR, "data", "intermarket_tape.jsonl")

_TTL_S = 1800            # a good series stays fresh this long (30 min)
_FAIL_BACKOFF_S = 1500   # after a failed fetch, leave that ticker alone 25 min
_LAST_GOOD_MAX_S = 7200  # serve stale last-good data for up to 2 hours
_PACE_S = 8.0            # sleep before EVERY network call. Wave 134: 13 unique
                         # tickers per snapshot; TwelveData free allows 8 req/min,
                         # so >=7.5s spacing keeps every snapshot inside the limit
                         # (snapshot runs in a daemon thread - scans never wait).
_SNAPSHOT_MIN_GAP_S = 1800  # at most one snapshot per 30 min

# Per-market confirmers. invert=True means "up is risk-OFF" for that market.
CONFIRMERS = {
    "NQ":  [("SPY", False), ("QQQ", False), ("SMH", False), ("IWM", False),
            ("HYG", False), ("NVDA", False), ("VIXY", True)],
    "GC":  [("UUP", True), ("TLT", False), ("SLV", False), ("GDX", False),
            ("VIXY", False)],
    "BTC": [("QQQ", False), ("ETH", False), ("VIXY", True)],
    "SOL": [("BTC", False), ("ETH", False), ("QQQ", False)],
}

# canonical confirmer -> per-source symbol. "td" = TwelveData symbol (primary),
# "yf" = yfinance symbol (fallback). None means that source cannot serve it.
_SYMBOL_MAP = {
    "SPY": {"td": "SPY",     "yf": "SPY"},
    "QQQ": {"td": "QQQ",     "yf": "QQQ"},
    "SMH": {"td": "SMH",     "yf": "SMH"},
    "UUP": {"td": "UUP",     "yf": "UUP"},
    "TLT": {"td": "TLT",     "yf": "TLT"},
    "VIX": {"td": "VIX",     "yf": "^VIX"},
    "BTC": {"td": "BTC/USD", "yf": "BTC-USD"},
    # Wave 134 additions - all TwelveData-carried (free tier):
    "VIXY": {"td": "VIXY",    "yf": "VIXY"},
    "IWM":  {"td": "IWM",     "yf": "IWM"},
    "HYG":  {"td": "HYG",     "yf": "HYG"},
    "NVDA": {"td": "NVDA",    "yf": "NVDA"},
    "SLV":  {"td": "SLV",     "yf": "SLV"},
    "GDX":  {"td": "GDX",     "yf": "GDX"},
    "ETH":  {"td": "ETH/USD", "yf": "ETH-USD"},
}

_BULL_AT = 0.35
_BEAR_AT = -0.35

_TD_URL = "https://api.twelvedata.com/time_series"
_TD_INTERVAL = "15min"
_TD_OUTPUTSIZE = 80

# ticker -> {"ts", "ok", "good_ts", "good_vals", "src"}
_cache = {}
_cache_lock = threading.Lock()
_last_snapshot_at = 0.0
_snap_lock = threading.Lock()


def _twelvedata_key():
    """Reuse the TwelveData key data_layer already loads (single source)."""
    try:
        import data_layer
        return getattr(data_layer, "TWELVE_DATA_API_KEY", None)
    except Exception:
        return None


def _fetch_closes_td(td_symbol):
    """One paced TwelveData fetch. Returns list[float] (oldest-first) or None."""
    key = _twelvedata_key()
    if not key or not td_symbol:
        return None
    try:
        import requests
        time.sleep(_PACE_S)
        params = {"symbol": td_symbol, "interval": _TD_INTERVAL,
                  "outputsize": _TD_OUTPUTSIZE, "apikey": key, "format": "JSON"}
        resp = requests.get(_TD_URL, params=params, timeout=12)
        if resp.status_code != 200:
            return None
        data = resp.json()
        rows = data.get("values")
        if not rows or not isinstance(rows, list):
            return None
        vals = []
        for row in reversed(rows):  # TwelveData returns newest-first
            c = row.get("close")
            if c is None:
                continue
            try:
                vals.append(float(c))
            except (TypeError, ValueError):
                continue
        return vals if len(vals) >= 30 else None
    except Exception as exc:
        log.debug("intermarket TD fetch failed for %s: %s", td_symbol, exc)
        return None


def _fetch_closes_yf(yf_symbol):
    """Fallback: one paced yfinance fetch. Returns list[float] or None."""
    if not yf_symbol:
        return None
    try:
        import yfinance as yf
        time.sleep(_PACE_S)
        tk = yf.Ticker(yf_symbol)
        df = tk.history(interval="15m", period="2d", auto_adjust=True)
        if df is None or len(df) == 0:
            return None
        closes = df["Close"].dropna()
        try:
            closes = closes.squeeze()
        except Exception:
            pass
        vals = [float(v) for v in list(closes)[-_TD_OUTPUTSIZE:]]
        return vals if len(vals) >= 30 else None
    except Exception as exc:
        log.debug("intermarket YF fetch failed for %s: %s", yf_symbol, exc)
        return None


def _fetch_closes(canonical):
    """Fetch one confirmer's closes via TwelveData, then yfinance.
    Returns (list[float] or None, source_tag)."""
    m = _SYMBOL_MAP.get(canonical, {"td": None, "yf": canonical})
    vals = _fetch_closes_td(m.get("td"))
    if vals:
        return vals, "td"
    vals = _fetch_closes_yf(m.get("yf"))
    if vals:
        return vals, "yf"
    return None, None


def _get_closes_cached(canonical):
    """Return (vals, age_s, src) using fresh cache -> refetch -> last-good.
    (None, None, None) only when nothing usable exists."""
    now = time.time()
    with _cache_lock:
        ent = dict(_cache.get(canonical) or {})
    age_since_try = now - ent.get("ts", 0)
    wait = _TTL_S if ent.get("ok") else _FAIL_BACKOFF_S
    if ent and age_since_try < wait:
        if ent.get("good_vals") is not None and (now - ent.get("good_ts", 0)) <= _LAST_GOOD_MAX_S:
            return ent["good_vals"], now - ent["good_ts"], "cache"
        return None, None, None
    vals, src = _fetch_closes(canonical)
    with _cache_lock:
        prev = dict(_cache.get(canonical) or {})
        if vals is not None:
            _cache[canonical] = {"ts": now, "ok": True, "good_ts": now,
                                 "good_vals": vals, "src": src}
        else:
            _cache[canonical] = {"ts": now, "ok": False,
                                 "good_ts": prev.get("good_ts", 0),
                                 "good_vals": prev.get("good_vals"),
                                 "src": prev.get("src")}
    if vals is not None:
        return vals, 0.0, src
    with _cache_lock:
        ent = dict(_cache.get(canonical) or {})
    if ent.get("good_vals") is not None and (now - ent.get("good_ts", 0)) <= _LAST_GOOD_MAX_S:
        return ent["good_vals"], now - ent["good_ts"], "cache"
    return None, None, None


def _ema(vals, span):
    k = 2.0 / (span + 1.0)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1.0 - k)
    return e


def _score_ticker(closes):
    """Score one instrument's tape in [-1, +1]: EMA20 position + 2h momentum."""
    try:
        last = closes[-1]
        pos = 1.0 if last > _ema(closes, 20) else -1.0
        base = closes[-9]
        mom_pct = (last - base) / base * 100.0 if base else 0.0
        mom = 1.0 if mom_pct > 0.15 else (-1.0 if mom_pct < -0.15 else 0.0)
        return (pos + mom) / 2.0
    except Exception:
        return None


def get_tape(market):
    """Aggregate confirmer scores into one tape read for a market."""
    result = {"tape": "UNKNOWN", "score": 0.0, "detail": {},
              "sources": {}, "age_s": 0.0}
    try:
        confs = CONFIRMERS.get(str(market).upper())
        if not confs:
            return result
        scores = []
        oldest = 0.0
        for ticker, invert in confs:
            closes, age, src = _get_closes_cached(ticker)
            result["sources"][ticker] = src
            if not closes:
                result["detail"][ticker] = None
                continue
            s = _score_ticker(closes)
            if s is None:
                result["detail"][ticker] = None
                continue
            if invert:
                s = -s
            result["detail"][ticker] = round(s, 2)
            scores.append(s)
            if age and age > oldest:
                oldest = age
        if not scores:
            return result
        mean = sum(scores) / len(scores)
        result["score"] = round(mean, 3)
        result["age_s"] = round(oldest, 0)
        result["tape"] = ("BULLISH" if mean >= _BULL_AT
                          else "BEARISH" if mean <= _BEAR_AT
                          else "NEUTRAL")
        return result
    except Exception as exc:
        log.debug("intermarket get_tape failed for %s: %s", market, exc)
        return result


def _snapshot_worker(markets):
    try:
        snap = {"ts": datetime.now(timezone.utc).isoformat(),
                "tape": {m: get_tape(m) for m in markets}}
        os.makedirs(os.path.dirname(TAPE_LOG_FILE), exist_ok=True)
        with open(TAPE_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(snap, separators=(",", ":")) + "\n")
    except Exception as exc:
        log.debug("intermarket snapshot failed: %s", exc)
    finally:
        try:
            _snap_lock.release()
        except Exception:
            pass


def log_snapshot(markets=None):
    """Kick off one tape snapshot in a DAEMON THREAD (single-flight, throttled).
    Returns immediately; the async scan loop is never blocked. Never raises."""
    global _last_snapshot_at
    try:
        now = time.time()
        if (now - _last_snapshot_at) < _SNAPSHOT_MIN_GAP_S:
            return False
        if not _snap_lock.acquire(blocking=False):
            return False
        _last_snapshot_at = now
        markets = markets or list(CONFIRMERS.keys())
        t = threading.Thread(target=_snapshot_worker, args=(markets,),
                             name="intermarket-snap", daemon=True)
        t.start()
        return True
    except Exception as exc:
        log.debug("intermarket snapshot spawn failed: %s", exc)
        try:
            _snap_lock.release()
        except Exception:
            pass
        return False
