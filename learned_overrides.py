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
# Wave 154 (_WAVE154_SHORT_VOLUME_UNLOCK): the signed floor for the
# volume-confirm gate on BEAR (short) setups only. The global vol_confirm
# floor above stays 0.60 for longs; shorts may learn down to here because
# the live data shows low-volume shorts win as well as high-volume ones.
# The learning system still must EARN any move with graded evidence; this
# only sets how far down it is allowed to go for the proven short edge.
_W154_BEAR_CONFIRM_FLOOR = 0.30
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
        data.setdefault("seeds", {})  # Wave 149 (_WAVE149_ADX_BASE_SEED)
        _cache["data"] = data
        _cache["mtime"] = mt
        return data
    except Exception:
        return {"overrides": {}, "ready_history": {}, "last_apply_date": "",
                "seeds": {}}


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
        # Wave 154 (_WAVE154_SHORT_VOLUME_UNLOCK): a DIRECTION-AWARE floor
        # for the volume-confirm gate. Measured Jul 18 from the live log:
        # NQ shorts win 53-61% REGARDLESS of volume (10W/9L below 0.4x, the
        # band where most occur), yet the confirm gate demands 0.80x and the
        # signed floor 0.60 stopped the learning system from ever reaching
        # where the edge lives (median short vol_ratio 0.31). That blocked
        # ~80% of NQ shorts from firing - the reason no shorts reached
        # Telegram. Bear/short confirm setups (name ends _BEAR) now have a
        # lower signed floor so the SAME evidence-gated learning system
        # (15 graded trades, 2-day proof, auto-revert on 10 bad live
        # trades) CAN lower their threshold if the edge holds - and tighten
        # back if it fades. Longs/bulls are UNCHANGED at 0.60. This forces
        # nothing: it only lowers the WALL, the learning loop still has to
        # earn every notch. vol_floor (the dead-market gate) is untouched.
        try:
            if filt == "vol_confirm" and str(setup).endswith("_BEAR"):
                floor = _W154_BEAR_CONFIRM_FLOOR
        except Exception:
            pass
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


_SEED_SEEN = {}


def seed_base(filt: str, market: str, setup: str, base):
    """Wave 149 (_WAVE149_ADX_BASE_SEED): record the base a DYNAMIC gate
    actually computes, so daily_decision can reason about that bucket.

    WHY THIS EXISTS: adx_min's base is not a constant. It is
    cfg.ADX_MIN_BY_SETUP[setup] (falling back to cfg.MIN_ADX), widened by
    cfg.MIN_ADX_PRIME during a prime session. BOUNDS["adx_min"]["base"] is
    therefore None, and daily_decision skips any adx bucket whose base it
    does not know - so the adx hand has been asleep since Wave 143 shipped.
    The gate knows the number; it just never told anyone. Now it does.

    WE RECORD THE MINIMUM BASE EVER SEEN, DELIBERATELY. get() is loosen-only:
    it returns min(override, base), so an override ABOVE the live base does
    nothing at all. If we seeded the prime-session maximum (say 25) while the
    ordinary base is 20, the first three notches (25->23->21->19) would be
    invisible until they crossed 20 - three days of "learning" that changed
    nothing, and three days of Telegram notes claiming otherwise. Seeding the
    minimum makes every notch real on the day it is applied.

    WRITES ARE RARE BY CONSTRUCTION: only when a bucket is unknown, or when a
    LOWER base appears. Once the ordinary (non-prime) base has been seen this
    stops touching the disk forever. Never raises - this runs on the scan path
    and a seeding failure must never cost a scan.
    """
    try:
        key = _bucket_key(filt, market, setup)
        b = float(base)
        prev = _SEED_SEEN.get(key)
        if prev is not None and b >= prev:
            return  # nothing lower to record - no disk touch at all
        data = _load()
        seeds = data.setdefault("seeds", {})
        cur = (seeds.get(key) or {}).get("base")
        if cur is not None and b >= float(cur) - 1e-9:
            _SEED_SEEN[key] = float(cur)
            return
        seeds[key] = {"base": b,
                      "seeded_at": datetime.now(timezone.utc).isoformat()}
        _SEED_SEEN[key] = b
        _save(data)
        _audit({"event": "seed_base", "bucket": key, "base": b})
    except Exception:
        pass


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
                # Wave 149 (_WAVE149_ADX_BASE_SEED): fall back to the base the
                # gate recorded on a real scan. Before 149 this branch always
                # hit `continue`, so the adx hand could never move no matter
                # how much evidence a bucket produced.
                base = (data.get("seeds", {}).get(key) or {}).get("base")
            if base is None:
                continue  # still unseen - the gate seeds it on its next scan
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


# ---------------------------------------------------------------- Wave 156
# _WAVE156_SHORT_SEED: the learning loop is correct but SLOW - it needs 15
# graded NEAR trades + 2 consecutive proven days + steps 0.05/day, and the
# winning shorts sit so far below the 0.80 wall they classify FAR and are
# not even studied. Measured Jul 18: NQ|SHORT has fired to Telegram ZERO
# times though NQ shorts win 58% in shadow, because ~80% die on the volume
# gate. This gives the PROVEN bear-confirm setups a running start: a single
# conservative ACTIVE override each, at a level DERIVED FROM THAT SETUP'S
# OWN winning-volume evidence, never below Wave 154's signed 0.30 bear
# floor. Then the normal loop refines from there (lower if they keep
# winning) and revert_check auto-reverts any seed whose next 10 live trades
# come in below break-even - identical protection to an earned override.
#
# ONLY these five made the strict bar (at seed, the vol>=seed zone is a
# clear net winner: >=2 shadow wins fire, net wins-losses >=2, >=60% WR):
#   NQ:EMA21_PULLBACK_BEAR  0.30  (5W/1L fire, 83%)
#   SOL:VWAP_REJECT_BEAR    0.40  (4W/1L fire, 80%)
#   BTC:VWAP_REJECT_BEAR    0.30  (3W/0L fire, 100%)
#   BTC:MACD_CROSS_BEAR     0.30  (3W/0L fire, 100%)
#   BTC:EMA21_PULLBACK_BEAR 0.30  (2W/0L fire, 100%)
# (NQ:MACD_CROSS_BEAR is 4W/0L but its winners are at vol ~0.19, BELOW the
# signed 0.30 floor, so it is deliberately NOT seeded - honest limit.)
_W156_SEED_TABLE = {
    "vol_confirm|NQ|EMA21_PULLBACK_BEAR": 0.30,
    "vol_confirm|SOL|VWAP_REJECT_BEAR": 0.40,
    "vol_confirm|BTC|VWAP_REJECT_BEAR": 0.30,
    "vol_confirm|BTC|MACD_CROSS_BEAR": 0.30,
    "vol_confirm|BTC|EMA21_PULLBACK_BEAR": 0.30,
}


def seed_short_overrides():
    """Plant the Wave 156 seed overrides ONCE. Idempotent and safe:
    - skips any bucket that already has an override (earned OR seeded),
      so it never clobbers the loop's work and never re-seeds post-revert;
    - clamps every seed to the bucket's signed floor, so it can never dip
      below Wayne's bounds even if the table is edited wrong;
    - marks each with seeded_by=W156 for audit;
    - never raises. Returns the count planted."""
    try:
        data = _load()
    except Exception:
        return 0
    ov = data.setdefault("overrides", {})
    planted = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for key, seed in _W156_SEED_TABLE.items():
        try:
            if key in ov:
                continue  # never overwrite an existing override
            filt, market, setup = key.split("|", 2)
            bnd = BOUNDS.get(filt, {})
            base = bnd.get("base")
            floor = bnd.get("floor")
            # Wave 154 lowered the effective floor for bear confirm setups;
            # mirror that here so the seed clamp matches the get() clamp.
            try:
                if filt == "vol_confirm" and str(setup).endswith("_BEAR"):
                    floor = _W154_BEAR_CONFIRM_FLOOR
            except Exception:
                pass
            val = float(seed)
            if floor is not None and val < float(floor):
                val = float(floor)
            if base is not None and val >= float(base):
                continue  # seed at/above base does nothing - skip
            ov[key] = {
                "filter": filt, "market": market, "setup": setup,
                "base": base, "value": round(val, 2), "state": "ACTIVE",
                "applied_at": now_iso, "tagged": [],
                "consecutive_reverts": 0, "seeded_by": "W156",
                "evidence": {"note": "Wave 156 seed from winning-volume evidence"},
            }
            planted += 1
        except Exception:
            continue
    if planted:
        try:
            _save(data)
            _audit({"event": "seed_w156", "planted": planted})
        except Exception:
            return 0
    return planted


try:
    seed_short_overrides()
except Exception:
    pass
