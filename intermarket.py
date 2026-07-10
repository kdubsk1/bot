"""
intermarket.py - NQ CALLS intermarket "tape" sensor (Wave 108)

Reads a small set of FREE correlated instruments (via yfinance, which the bot
already depends on) and distills them into one honest per-market read:

    get_tape("NQ") -> {"tape": "BULLISH" | "BEARISH" | "NEUTRAL" | "UNKNOWN",
                       "score": -1.0..+1.0, "detail": {...}, "age_s": float}

Confirmers per market (kept deliberately small and cheap):
    NQ  <- ES=F (S&P futures), QQQ (Nasdaq ETF), SMH (semis), ^VIX (inverted)
    GC  <- DX=F (dollar, inverted), ^TNX (10y yield, inverted), ^VIX
    BTC <- QQQ (risk appetite), ^VIX (inverted)
    SOL <- BTC-USD (crypto leader), QQQ

Design rules (Wave 108 charter):
  * SHADOW ONLY: this module never blocks or fires trades. It is logged next to
    every scan cycle so we can PROVE with our own data whether tape confirmation
    would have saved the losing shorts, before any gating wave trusts it.
  * COST: one yfinance fetch per unique ticker per _TTL_S (10 min), shared
    across all markets via a module cache. Roughly 7 tickers -> ~40 tiny
    requests/hour. No new dependencies, no paid data.
  * FAIL-SAFE: every path is wrapped; on any failure a ticker is skipped and,
    if nothing is left, the tape is "UNKNOWN". A yfinance outage can never
    break scanning.

ASCII-only (Railway cp1252).
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

# How long a fetched series stays fresh. 10 minutes keeps cost trivial while
# staying current enough for tape context (this is regime, not entry timing).
_TTL_S = 600
# Never write snapshots more often than this (keeps the jsonl tiny).
_SNAPSHOT_MIN_GAP_S = 540

# Per-market confirmers. invert=True means "up is risk-OFF" for that market
# (e.g. VIX rising is bearish for NQ; dollar rising is bearish for gold).
CONFIRMERS = {
    "NQ":  [("ES=F", False), ("QQQ", False), ("SMH", False), ("^VIX", True)],
    "GC":  [("DX=F", True), ("^TNX", True), ("^VIX", False)],  # gold: fear is a BID (safe haven), dollar/yields inverse
    "BTC": [("QQQ", False), ("^VIX", True)],
    "SOL": [("BTC-USD", False), ("QQQ", False)],
}

_BULL_AT = 0.35   # mean confirmer score >= this -> BULLISH tape
_BEAR_AT = -0.35  # mean confirmer score <= this -> BEARISH tape

_cache = {}            # ticker -> (fetched_at_epoch, closes_list or None)
_cache_lock = threading.Lock()
_last_snapshot_at = 0.0


def _fetch_closes(ticker):
    """Fetch ~2 days of 15m closes for a ticker. Returns list[float] or None."""
    try:
        import yfinance as yf
        df = yf.download(ticker, period="2d", interval="15m",
                         progress=False, auto_adjust=True)
        if df is None or len(df) == 0:
            return None
        closes = df["Close"].dropna()
        # yfinance can return a DataFrame column for single tickers on some
        # versions; squeeze to a 1-D series either way.
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
    now = time.time()
    with _cache_lock:
        hit = _cache.get(ticker)
        if hit and (now - hit[0]) < _TTL_S:
            return hit[1]
    vals = _fetch_closes(ticker)
    with _cache_lock:
        # Cache failures too (as None) so a down ticker is not re-hit
        # every cycle; it retries after the TTL expires.
        _cache[ticker] = (now, vals)
    return vals


def _ema(vals, span):
    k = 2.0 / (span + 1.0)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1.0 - k)
    return e


def _score_ticker(closes):
    """
    Score one instrument's tape in [-1, +1] from two simple, robust reads:
      * position: last price above (+1) / below (-1) its 20-period EMA
      * momentum: % change over the last 8 bars (2h of 15m bars):
        > +0.15% -> +1, < -0.15% -> -1, else 0
    Score = average of the two. Crude on purpose -- this is tape CONTEXT,
    and simple signals survive regime changes better than clever ones.
    """
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
        for ticker, invert in confs:
            closes = _get_closes_cached(ticker)
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
        if not scores:
            return result
        mean = sum(scores) / len(scores)
        result["score"] = round(mean, 3)
        result["tape"] = ("BULLISH" if mean >= _BULL_AT
                          else "BEARISH" if mean <= _BEAR_AT
                          else "NEUTRAL")
        return result
    except Exception as exc:
        log.debug("intermarket get_tape failed for %s: %s", market, exc)
        return result


def log_snapshot(markets=None):
    """
    Append one JSON line with the tape read for each market. Throttled to at
    most one line per _SNAPSHOT_MIN_GAP_S. Never raises. Later analysis joins
    these by timestamp against outcomes.csv / strategy_log.csv to answer:
    "would a tape gate have saved the losing shorts?"
    """
    global _last_snapshot_at
    try:
        now = time.time()
        if (now - _last_snapshot_at) < _SNAPSHOT_MIN_GAP_S:
            return False
        _last_snapshot_at = now
        markets = markets or list(CONFIRMERS.keys())
        snap = {"ts": datetime.now(timezone.utc).isoformat(),
                "tape": {m: get_tape(m) for m in markets}}
        os.makedirs(os.path.dirname(TAPE_LOG_FILE), exist_ok=True)
        with open(TAPE_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(snap, separators=(",", ":")) + "\n")
        return True
    except Exception as exc:
        log.debug("intermarket snapshot failed: %s", exc)
        return False
