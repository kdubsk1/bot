"""
learned_overrides.py - NQ CALLS 2026
=====================================
Wave 143 (_WAVE143_LEARNED_OVERRIDES): the learning loop's HANDS.

Wayne signed off the bounds on Jul 15, 2026 (recorded in UPGRADE_IDEAS.md).
This module is the ONLY place the bot may adjust its own gate thresholds,
and it can only do so inside those signed bounds:

  WHITELIST (per market+setup bucket, nothing else EVER):
    vol_confirm  base 0.80x  step 0.05  floor 0.60
    vol_floor    base 0.30x  step 0.05  floor 0.20
    adx_min      (dynamic per-setup base)  step 2  floor 12
    conv_min     base 48     step 2     floor 44
  PROOF: per-setup shadow evidence n>=15 with WR >= 1.25x break-even,
         sustained on 2 consecutive daily checks (no one-day flukes).
  PACE:  max ONE apply per day bot-wide; a bucket that moved waits 14
         days before moving again.
  REVERT: every live trade that fires ONLY because of an override is
         tagged; once 10 tagged trades close, if their WR is below their
         break-even the override self-reverts. Revert cooldown 1 DAY
         (Wayne's amendment); 3 consecutive reverts on the same bucket
         -> 1 WEEK. /revert gives Wayne instant manual override.
  NOTES: one Telegram note per apply and per revert - NOTHING else.
         Silent daily checks. The audit file gets everything.

The gates read values through get(); if this module is missing or broken
the gates fall back to their hardcoded bases - the learning system can
never break a scan.
"""

from __future__ import annotations
import os
import json
from datetime import datetime, timezone, timedelta

import safe_io

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE_DIR, "data")
STORE = os.path.join(_DATA_DIR, "learned_overrides.json")
AUDIT = os.path.join(_DATA_DIR, "learning_loop.jsonl")
os.makedirs(_DATA_DIR, exist_ok=True)

# ---- Wayne's signed bounds (Jul 15, 2026). Do not edit without sign-off. ----
BOUNDS = {
    "vol_confirm": {"base": 0.80, "step": 0.05, "floor": 0.60, "round": 2},
    "vol_floor":   {"base": 0.30, "step": 0.05, "floor": 0.20, "round": 2},
    "adx_min":     {"base": None, "step": 2.0,  "floor": 12.0, "round": 1},
    "conv_min":    {"base": 48,   "step": 2,    "floor": 44,   "round": 0},
}
MIN_SAMPLE = 15
MARGIN = 1.25
CONSECUTIVE_READY_DAYS = 2
FILTER_REMOVE_COOLDOWN_D = 14
REVERT_TRADES = 10
REVERT_COOLDOWN_D = 1          # Wayne's amendment (was 30)
REVERT_STREAK_FOR_WEEK = 3     # 3 consecutive reverts -> 1 week
REVERT_WEEK_D = 7

# ledger filter_class -> our whitelist filter (only NEAR bands are evidence).
# Names are Wave 142's THRESHOLD-AGNOSTIC classes: a bucket must keep its
# identity across a move, or the hands would erase the very history that
# justified the move. NEAR = the gap to whatever the gate actually required,
# so this map stays correct at every future threshold.
_ACTIONABLE = {
    ("VOL_CONFIRM", "NEAR"): "vol_confirm",
    ("VOL_FLOOR", "NEAR"): "vol_floor",
    ("ADX_MIN", "NEAR"): "adx_min",
    ("CONV_MIN", "NEAR"): "conv_min",
}

_cache = {"data": None, "mtime": None}


# ------------------------------------------------------------------- store
def _load() -> dict:
    try:
        mt = os.path.getmtime(STORE) if os.path.exists(STORE) else None
        if _cache["data"] is not None and _cache["mtime"] == mt:
            return _cache["data"]
        if os.path.exists(STORE):
            with open(STORE, "r", encoding="utf-8") as fh:
                data = json.load(fh) or {}
        else:
            data = {}
        data.setdefault("overrides", {})
        data.setdefault("ready_history", {})
        data.setdefault("last_apply_date", "")
        _cache["data"] = data
        _cache["mtime"] = mt
        return data
    except Exception:
        return {"overrides": {}, "ready_history": {}, "last_apply_date": ""}


def _save(data: dict):
    try:
        if hasattr(safe_io, "atomic_write_json"):
            safe_io.atomic_write_json(STORE, data)
        else:
            with open(STORE, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        _cache["data"] = data
        _cache["mtime"] = os.path.getmtime(STORE) if os.path.exists(STORE) else None
    except Exception:
        pass


def _audit(rec: dict):
    try:
        rec = dict(rec)
        rec.setdefault("ts", datetime.now(timezone.utc).isoformat())
        if hasattr(safe_io, "safe_append_jsonl"):
            safe_io.safe_append_jsonl(AUDIT, rec)
        else:
            with open(AUDIT, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


def _bucket_key(filt: str, market: str, setup: str) -> str:
    return "%s|%s|%s" % (filt, market, setup)


# -------------------------------------------------------------------- read
def get(filt: str, market: str, setup: str, base):
    """The gates' read path. Returns the ACTIVE learned value for this
    exact (filter, market, setup) bucket, else the caller's base.
    NEVER raises; never returns a value below the signed floor or above
    the base (this module can only LOOSEN, never tighten)."""
    try:
        o = _load()["overrides"].get(_bucket_key(filt, market, setup))
        if not o or o.get("state") != "ACTIVE":
            return base
        v = float(o.get("value", base))
        b = BOUNDS.get(filt, {})
        floor = b.get("floor")
        if floor is not None and v < floor:
            return floor if floor < base else base
        try:
            if v > float(base):  # loosen-only: for these gates lower = looser
                return base
        except (TypeError, ValueError):
            return base
        return v
    except Exception:
        return base


def is_pass_via_override(filt: str, market: str, setup: str, observed, base) -> bool:
    """True when `observed` passes the ACTIVE learned threshold but would
    have FAILED the base - i.e. this pass exists only because of learning.
    Used by the gates to tag the resulting trade for the revert tracker."""
    try:
        eff = get(filt, market, setup, base)
        return float(eff) <= float(observed) < float(base)
    except Exception:
        return False


def tag_trade(filt: str, market: str, setup: str, alert_id: str):
    """Record a live trade that fired only because of an override."""
    try:
        data = _load()
        o = data["overrides"].get(_bucket_key(filt, market, setup))
        if not o or o.get("state") != "ACTIVE":
            return
        tagged = o.setdefault("tagged", [])
        if alert_id and alert_id not in tagged:
            tagged.append(alert_id)
            _save(data)
            _audit({"event": "tag", "bucket": _bucket_key(filt, market, setup),
                    "alert_id": alert_id, "n_tagged": len(tagged)})
    except Exception:
        pass


# ---------------------------------------------------------------- evidence
def _per_setup_evidence():
    """Per-(filter, market, setup) shadow evidence at the SIGNED granularity.
    The /ledger aggregates per market; Wayne's bounds apply per market+setup,
    so the hands re-aggregate with setup resolution using the ledger's own
    classifier (one parser, two views)."""
    try:
        import strategy_log as sl
        import filter_ledger as fl
        rows = sl._load_all_strategy_rows()
    except Exception:
        return {}
    ev = {}
    for row in rows:
        res = row.get("result", "")
        if res not in ("WOULD_WIN", "WOULD_LOSE"):
            continue
        try:
            if row.get("decision") == sl.DECISION_FIRED:
                continue
        except Exception:
            pass
        fc, band = fl.classify_reason(row.get("reject_reason", ""))
        filt = _ACTIONABLE.get((fc, band))
        if not filt:
            continue
        key = _bucket_key(filt, row.get("market", "?") or "?",
                          row.get("setup_type", "?") or "?")
        b = ev.setdefault(key, {"w": 0, "l": 0, "rr_sum": 0.0, "rr_n": 0})
        if res == "WOULD_WIN":
            b["w"] += 1
        else:
            b["l"] += 1
        try:
            rr = float(row.get("rr") or 0)
            if rr > 0:
                b["rr_sum"] += rr
                b["rr_n"] += 1
        except (TypeError, ValueError):
            pass
    return ev


def _proven(b) -> bool:
    n = b["w"] + b["l"]
    if n < MIN_SAMPLE or not b["rr_n"]:
        return False
    wr = b["w"] / n
    avg_rr = b["rr_sum"] / b["rr_n"]
    if avg_rr <= 0:
        return False
    required = min(0.95, (1.0 / (1.0 + avg_rr)) * MARGIN)
    return wr >= required


# ---------------------------------------------------------------- decision
def daily_decision(now=None) -> str:
    """The once-a-day check. Applies AT MOST one bounded loosening across
    the whole bot, only on 2-consecutive-day proof, honoring every signed
    cooldown. Returns the Telegram note when something changed, else ''
    (silent - Wayne's no-spam requirement)."""
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    data = _load()
    if data.get("last_apply_date") == today:
        return ""
    ev = _per_setup_evidence()
    hist = data["ready_history"]
    # record today's proven buckets (one entry per day max)
    proven_today = set()
    for key, b in ev.items():
        if _proven(b):
            proven_today.add(key)
            days = hist.setdefault(key, [])
            if today not in days:
                days.append(today)
                days[:] = days[-10:]
    # prune stale history (bucket no longer proven today -> streak broken)
    for key in list(hist.keys()):
        if key not in proven_today:
            hist.pop(key, None)
    changed_note = ""
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    for key in sorted(proven_today, key=lambda k: -(ev[k]["w"] + ev[k]["l"])):
        days = hist.get(key, [])
        if today not in days or yesterday not in days:
            continue  # needs 2 CONSECUTIVE days of proof
        filt, market, setup = key.split("|", 2)
        bnd = BOUNDS[filt]
        o = data["overrides"].get(key)
        if o:
            # cooldowns: revert cooldown, and 14-day re-move cooldown
            cu = o.get("cooldown_until", "")
            if cu and cu > now.isoformat():
                continue
            if o.get("state") == "ACTIVE":
                ap = o.get("applied_at", "")
                if ap and (now - datetime.fromisoformat(ap)).days < FILTER_REMOVE_COOLDOWN_D:
                    continue
        # current effective value & base
        if filt == "adx_min":
            base = o.get("base") if o else None
            if base is None:
                continue  # adx base is injected by the gate on first sighting; skip until known
        else:
            base = bnd["base"]
        cur = float(o["value"]) if (o and o.get("state") == "ACTIVE") else float(base)
        new = round(cur - bnd["step"], bnd["round"])
        if new < bnd["floor"]:
            continue  # already at the signed floor
        b = ev[key]
        n = b["w"] + b["l"]
        wr = 100.0 * b["w"] / n
        avg_rr = b["rr_sum"] / b["rr_n"]
        req = 100.0 * min(0.95, (1.0 / (1.0 + avg_rr)) * MARGIN)
        rec = o or {"filter": filt, "market": market, "setup": setup,
                    "base": base, "consecutive_reverts": 0, "tagged": []}
        rec.update({"value": new, "state": "ACTIVE",
                    "applied_at": now.isoformat(),
                    "tagged": [],  # fresh evaluation window per notch
                    "evidence": {"n": n, "w": b["w"], "l": b["l"],
                                 "wr": round(wr, 1), "avg_rr": round(avg_rr, 2),
                                 "required_wr": round(req, 1)}})
        data["overrides"][key] = rec
        data["last_apply_date"] = today
        _save(data)
        _audit({"event": "apply", "bucket": key, "from": cur, "to": new,
                "evidence": rec["evidence"]})
        changed_note = (
            "\U0001F9E0 LEARNED: %s %s %s %.6g \u2192 %.6g\n"
            "Evidence: %dW/%dL (%.0f%% WR, needed %.0f%%) at near-miss, avg rr %.1f.\n"
            "Watching the next %d live trades this unlocks - reverts itself if they disagree."
            % (market, setup, filt, cur, new, b["w"], b["l"], wr, req, avg_rr,
               REVERT_TRADES))
        break  # ONE change per day bot-wide
    return changed_note


# ------------------------------------------------------------------ revert
def revert_check(outcome_lookup) -> str:
    """outcome_lookup: callable(alert_id) -> (result, rr) with result in
    WIN/LOSS/'' . Once REVERT_TRADES tagged trades have closed, judge them;
    below break-even -> self-revert. Returns the note or '' (silent)."""
    now = datetime.now(timezone.utc)
    data = _load()
    note = ""
    for key, o in data["overrides"].items():
        if o.get("state") != "ACTIVE":
            continue
        closed = []
        for aid in o.get("tagged", []):
            try:
                res, rr = outcome_lookup(aid)
            except Exception:
                res, rr = "", 0
            if res in ("WIN", "LOSS"):
                closed.append((res, float(rr or 0)))
        if len(closed) < REVERT_TRADES:
            continue
        closed = closed[:REVERT_TRADES]
        wins = sum(1 for r, _ in closed if r == "WIN")
        wr = wins / len(closed)
        rrs = [rr for _, rr in closed if rr > 0]
        avg_rr = (sum(rrs) / len(rrs)) if rrs else 0.0
        breakeven = (1.0 / (1.0 + avg_rr)) if avg_rr > 0 else 1.0
        if wr >= breakeven:
            # the loosening is EARNING - keep it; freeze judgment, keep watching
            o["tagged"] = []
            _save(data)
            _audit({"event": "override_validated", "bucket": key,
                    "wr": round(100 * wr, 1), "breakeven": round(100 * breakeven, 1)})
            continue
        streak = int(o.get("consecutive_reverts", 0)) + 1
        cd_days = REVERT_WEEK_D if streak >= REVERT_STREAK_FOR_WEEK else REVERT_COOLDOWN_D
        o.update({"state": "REVERTED", "reverted_at": now.isoformat(),
                  "consecutive_reverts": streak,
                  "cooldown_until": (now + timedelta(days=cd_days)).isoformat()})
        data["overrides"][key] = o
        _save(data)
        _audit({"event": "revert", "bucket": key, "wr": round(100 * wr, 1),
                "breakeven": round(100 * breakeven, 1), "streak": streak,
                "cooldown_days": cd_days})
        filt, market, setup = key.split("|", 2)
        note = ("\u21A9 REVERTED: %s %s %s back to %.6g\n"
                "The %d live trades it unlocked ran %.0f%% WR (needed %.0f%%).\n"
                "Locked for %d day%s.%s"
                % (market, setup, filt, float(o.get("base", 0) or 0),
                   len(closed), 100 * wr, 100 * breakeven, cd_days,
                   "s" if cd_days != 1 else "",
                   " Third strike - week timeout." if streak >= REVERT_STREAK_FOR_WEEK else ""))
        break  # one note max per check
    return note


def manual_revert(market: str = "", setup: str = "") -> str:
    """/revert [market] [setup] - Wayne's instant overrule. No args = all."""
    now = datetime.now(timezone.utc)
    data = _load()
    hit = []
    for key, o in data["overrides"].items():
        if o.get("state") != "ACTIVE":
            continue
        _, m, s = key.split("|", 2)
        if market and m != market:
            continue
        if setup and s != setup:
            continue
        o.update({"state": "REVERTED", "reverted_at": now.isoformat(),
                  "consecutive_reverts": int(o.get("consecutive_reverts", 0)) + 1,
                  "cooldown_until": (now + timedelta(days=REVERT_COOLDOWN_D)).isoformat()})
        hit.append(key)
        _audit({"event": "manual_revert", "bucket": key})
    if hit:
        _save(data)
        return "Reverted %d learned override(s):\n%s" % (len(hit), "\n".join("  " + k for k in hit))
    return "No active learned overrides matched."


def status_text() -> str:
    """/overrides - what has she learned?"""
    data = _load()
    DIV = "\u2501" * 27
    L = ["\U0001F9E0 LEARNED OVERRIDES", DIV]
    active = {k: o for k, o in data["overrides"].items() if o.get("state") == "ACTIVE"}
    reverted = {k: o for k, o in data["overrides"].items() if o.get("state") == "REVERTED"}
    if not active and not reverted:
        L.append("  Nothing learned yet.")
        L.append("  The hands move only on 2 days of")
        L.append("  proof (n>=%d, WR %.2fx break-even)." % (MIN_SAMPLE, MARGIN))
        return "\n".join(L)
    if active:
        L.append("ACTIVE:")
        for k, o in active.items():
            _, m, s = k.split("|", 2)
            ev = o.get("evidence", {})
            L.append("  %s %s %s: %.6g \u2192 %.6g" % (m, s, o.get("filter", "?"),
                     float(o.get("base", 0) or 0), float(o.get("value", 0))))
            L.append("    proof %sW/%sL (%s%%) | live-watch %d/%d"
                     % (ev.get("w", "?"), ev.get("l", "?"), ev.get("wr", "?"),
                        len(o.get("tagged", [])), REVERT_TRADES))
    if reverted:
        L.append(DIV)
        L.append("REVERTED (cooling down):")
        for k, o in reverted.items():
            _, m, s = k.split("|", 2)
            L.append("  %s %s %s (until %s)" % (m, s, o.get("filter", "?"),
                     (o.get("cooldown_until", "") or "")[:10]))
    L.append(DIV)
    L.append("  Bounds are Wayne-signed law. /revert")
    L.append("  [market] [setup] overrules instantly.")
    return "\n".join(L)
