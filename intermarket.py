"""
intermarket.py - NQ CALLS intermarket "tape" sensor (Wave 108, hardened by Wave 113)
_WAVE113_INTERMARKET_FETCH_V2

Reads a small set of FREE correlated instruments (via yfinance, which the bot
already depends on) and distills them into one honest per-market read:

    get_tape("NQ") -> {"tape": "BULLISH" | "BEARISH" | "NEUTRAL" | "UNKNOWN",
                       "score": -1.0..+1.0, "detail": {...}, "age_s": float}

Confirmers per market (kept deliberately small and cheap):
    NQ  <- ES=F (S&P futures), QQQ (Nasdaq ETF), SMH (semis), ^VIX (inverted)
    GC  <- DX=F (dollar, inverted), ^TNX (10y yield, inverted), ^VIX
    BTC <- QQQ (risk appetite), ^VIX (inverted)
    SOL <- BTC-USD (crypto leader), QQQ

Wave 113 hardening (production found ~99% UNKNOWN reads on Railway):
  * Yahoo rate-limits datacenter IPs hard. v1 used raw yf.download in a 7-ticker
    burst -> almost everything 429'd. v2 copies data_layer's battle-tested
    pattern: yf.Ticker(t).history(...) + a pacing sleep BEFORE every network
    call + longer backoff after a failure.
  * LAST-GOOD memory cache: a failed refresh serves the previous good series
    for up to 90 minutes (tape is regime context - stale-but-recent beats
    UNKNOWN). age_s reports the oldest data used.
  * NON-BLOCKING: log_snapshot() now does all fetching in a daemon thread
    (single-flight lock), so the pacing sleeps can never stall the bot's
    async scan loop.

Original Wave 108 rules still hold: SHADOW ONLY (never blocks or fires
trades), trivial cost, everything fail-safe. ASCII-only (Railway cp1252).
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

_TTL_S = 600            # a good series stays fresh this long (10 min)
_FAIL_BACKOFF_S = 1500  # after a failed fetch, leave that ticker alone 25 min
_LAST_GOOD_MAX_S = 5400 # serve stale last-good data for up to 90 min
_PACE_S = 2.5           # sleep before EVERY network call (datacenter-IP kindness)
_SNAPSHOT_MIN_GAP_S = 540

# Per-market confirmers. invert=True means "up is risk-OFF" for that market
# (e.g. VIX rising is bearish for NQ; dollar rising is bearish for gold).
CONFIRMERS = {
    "NQ":  [("ES=F", False), ("QQQ", False), ("SMH", False), ("^VIX", True)],
    "GC":  [("DX=F", True), ("^TNX", True), ("^VIX", False)],  # gold: fear is a BID (safe haven), dollar/yields inverse
    "BTC": [("QQQ", False), ("^VIX", True)],
    "SOL": [("BTC-USD", False), ("QQQ", False)],
}

_BULL_AT = 0.35
_BEAR_AT = -0.35

# ticker -> {"ts": last_attempt_epoch, "ok": bool,
#            "good_ts": last_success_epoch, "good_vals": list or None}
_cache = {}
_cache_lock = threading.Lock()
_last_snapshot_at = 0.0
_snap_lock = threading.Lock()


def _fetch_closes(ticker):
    """One paced yfinance fetch (~2 days of 15m closes). list[float] or None.
    Uses Ticker().history like data_layer does - it behaves far better than
    yf.download under Yahoo's rate limiting of cloud IPs."""
    try:
        import yfinance as yf
        time.sleep(_PACE_S)  # pace BEFORE the call; runs in a worker thread
        tk = yf.Ticker(ticker)
        df = tk.history(interval="15m", period="2d", auto_adjust=True)
        if df is None or len(df) == 0:
            return None
        closes = df["Close"].dropna()
        try:
            closes = closes.squeeze()
        except Exception:
            pass
        vals = [float(v) for v in list(closes)[-80:]]
        return vals if len(vals) >= 30 else None
    except Exception as exc:
        log.debug("intermarket fetch failed for %s: %s", ticker, exc)
        return None


def _get_closes_cached(ticker):
    """Return (vals, age_s) using: fresh cache -> refetch -> last-good (<=90m).
    (None, None) only when nothing usable exists."""
    now = time.time()
    with _cache_lock:
        ent = dict(_cache.get(ticker) or {})
    age_since_try = now - ent.get("ts", 0)
    wait = _TTL_S if ent.get("ok") else _FAIL_BACKOFF_S
    if ent and age_since_try < wait:
        # within cooldown: serve good data (fresh or last-good) if young enough
        if ent.get("good_vals") is not None and (now - ent.get("good_ts", 0)) <= _LAST_GOOD_MAX_S:
            return ent["good_vals"], now - ent["good_ts"]
        return None, None
    vals = _fetch_closes(ticker)
    with _cache_lock:
        prev = dict(_cache.get(ticker) or {})
        if vals is not None:
            _cache[ticker] = {"ts": now, "ok": True, "good_ts": now, "good_vals": vals}
        else:
            _cache[ticker] = {"ts": now, "ok": False,
                              "good_ts": prev.get("good_ts", 0),
                              "good_vals": prev.get("good_vals")}
    if vals is not None:
        return vals, 0.0
    with _cache_lock:
        ent = dict(_cache.get(ticker) or {})
    if ent.get("good_vals") is not None and (now - ent.get("good_ts", 0)) <= _LAST_GOOD_MAX_S:
        return ent["good_vals"], now - ent["good_ts"]
    return None, None


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
    result = {"tape": "UNKNOWN", "score": 0.0, "detail": {}, "age_s": 0.0}
    try:
        confs = CONFIRMERS.get(str(market).upper())
        if not confs:
            return result
        scores = []
        oldest = 0.0
        for ticker, invert in confs:
            closes, age = _get_closes_cached(ticker)
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
    Returns immediately; the async scan loop is never blocked by fetch pacing.
    Never raises."""
    global _last_snapshot_at
    try:
        now = time.time()
        if (now - _last_snapshot_at) < _SNAPSHOT_MIN_GAP_S:
            return False
        if not _snap_lock.acquire(blocking=False):
            return False  # a snapshot is already in flight
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
