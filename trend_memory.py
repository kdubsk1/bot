"""
trend_memory.py - Wave 68: the self-grading trend brain.

Wayne's vision: the bot always has an opinion on each market's direction, it
REMEMBERS that opinion, and later checks whether price actually moved that way,
so it learns from its own predictions (like it already learns from trades).

This module is standalone and side-effect-free on import, so it cannot break
anything that already runs. It is wired in with tiny hooks:
  - cmd_trend / periodic loop call log_trend_read(market, score, price)
  - the scan loop calls grade_due_reads(get_frames) once per cycle
  - market_trend_text appends accuracy_summary(market) to its output

HARD RULE (Wayne): no learnable data is ever deleted. trend_reads.jsonl is
append-only; grading marks rows graded in place via a rewrite that PRESERVES
every row (only flips graded/result fields).
"""
import os as _os
import json as _json
import time as _time
from datetime import datetime, timezone

_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "data")
READS_PATH = _os.path.join(_DIR, "trend_reads.jsonl")
ACC_PATH = _os.path.join(_DIR, "trend_accuracy.json")

# How long after a read we judge it, and how big a move "counts".
HORIZON_MIN = 60          # grade a read 60 minutes after it was made
MIN_MOVE_PCT = 0.05       # +/-0.05% move = a real directional outcome (else FLAT)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _bias_from_score(score):
    # trend_score is -10..+10; mirror the bot's own thresholds.
    try:
        s = int(score)
    except Exception:
        return "NEUTRAL"
    if s >= 3:
        return "LONG"
    if s <= -3:
        return "SHORT"
    return "NEUTRAL"


def log_trend_read(market, score, price):
    """Append one trend read. Never raises into the caller."""
    try:
        if price is None or float(price) <= 0:
            return
        _os.makedirs(_DIR, exist_ok=True)
        row = {
            "ts": _now_iso(),
            "market": str(market).upper(),
            "score": int(score) if score is not None else 0,
            "bias": _bias_from_score(score),
            "price": float(price),
            "graded": False,
            "result": None,
        }
        with open(READS_PATH, "a", encoding="utf-8") as f:
            f.write(_json.dumps(row) + "\n")
    except Exception:
        pass


def _read_all():
    rows = []
    try:
        if not _os.path.exists(READS_PATH):
            return rows
        with open(READS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(_json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return rows


def _age_min(iso_ts):
    try:
        t = datetime.fromisoformat(iso_ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 60.0
    except Exception:
        return 0.0


def _current_price(market, get_frames):
    """Best-effort current price from the 15m (or any) frame."""
    try:
        frames = get_frames(market)
        for tf in ("15m", "1h", "4h", "1d"):
            df = frames.get(tf) if frames else None
            if df is not None and len(df) > 0:
                return float(df["Close"].iloc[-1])
    except Exception:
        pass
    return None


def grade_due_reads_from_frames(frames_by_market):
    """Convenience: grade using an already-fetched {market: {tf: df}} dict
    (avoids re-fetching in the scan loop)."""
    def _getter(m):
        return frames_by_market.get(m, {}) if frames_by_market else {}
    return grade_due_reads(_getter)


def grade_due_reads(get_frames):
    """
    Grade every ungraded read older than HORIZON_MIN. Rewrites the file
    PRESERVING ALL ROWS (only fills graded/result). Updates trend_accuracy.json.
    Returns the number of reads graded this call.
    """
    rows = _read_all()
    if not rows:
        return 0
    price_cache = {}
    graded_now = 0
    changed = False
    for r in rows:
        if r.get("graded"):
            continue
        if _age_min(r.get("ts", _now_iso())) < HORIZON_MIN:
            continue
        mkt = r.get("market")
        if mkt not in price_cache:
            price_cache[mkt] = _current_price(mkt, get_frames)
        now_px = price_cache.get(mkt)
        entry_px = r.get("price")
        if now_px is None or not entry_px:
            continue  # can't grade yet; leave for next pass (no data lost)
        move_pct = (now_px - entry_px) / entry_px * 100.0
        bias = r.get("bias", "NEUTRAL")
        if abs(move_pct) < MIN_MOVE_PCT:
            result = "FLAT"
        elif (bias == "LONG" and move_pct > 0) or (bias == "SHORT" and move_pct < 0):
            result = "CORRECT"
        elif bias == "NEUTRAL":
            result = "FLAT" if abs(move_pct) < (MIN_MOVE_PCT * 4) else "MOVED"
        else:
            result = "WRONG"
        r["graded"] = True
        r["result"] = result
        r["move_pct"] = round(move_pct, 3)
        graded_now += 1
        changed = True

    if changed:
        # Rewrite atomically, preserving every row.
        try:
            tmp = READS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(_json.dumps(r) + "\n")
            _os.replace(tmp, READS_PATH)
        except Exception:
            return graded_now
        _rebuild_accuracy(rows)
    return graded_now


def _rebuild_accuracy(rows):
    """Aggregate graded reads into per-market accuracy (directional only)."""
    try:
        agg = {}
        for r in rows:
            if not r.get("graded"):
                continue
            res = r.get("result")
            if res not in ("CORRECT", "WRONG"):
                continue  # FLAT/MOVED don't count for/against directional accuracy
            mkt = r.get("market", "?")
            d = agg.setdefault(mkt, {"correct": 0, "wrong": 0})
            if res == "CORRECT":
                d["correct"] += 1
            else:
                d["wrong"] += 1
        for mkt, d in agg.items():
            n = d["correct"] + d["wrong"]
            d["total"] = n
            d["accuracy_pct"] = round(d["correct"] / n * 100, 1) if n else 0.0
        agg["_updated"] = _now_iso()
        _os.makedirs(_DIR, exist_ok=True)
        tmp = ACC_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(agg, f, indent=2)
        _os.replace(tmp, ACC_PATH)
    except Exception:
        pass


def accuracy_summary(market):
    """One-line accuracy string for display, or '' if not enough data."""
    try:
        if not _os.path.exists(ACC_PATH):
            return ""
        with open(ACC_PATH, "r", encoding="utf-8") as f:
            agg = _json.load(f)
        d = agg.get(str(market).upper())
        if not d or d.get("total", 0) < 10:
            return ""
        return "Trend-read accuracy: %.0f%% over %d graded reads." % (
            d.get("accuracy_pct", 0), d.get("total", 0))
    except Exception:
        return ""
