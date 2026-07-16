"""
filter_ledger.py - NQ CALLS 2026
=================================
Wave 141 (_WAVE141_FILTER_LEDGER): the learning loop's EYES.

Wave 140 made rejections gradeable: every deduped rejection at the
volume / trend-guard / suspended / ADX gates becomes a paper trade that
resolves WOULD_WIN or WOULD_LOSE under the exact same rules as real
trades. This module aggregates those verdicts into an honest ledger
per FILTER: is this gate saving money, or costing wins?

READ-ONLY BY DESIGN. This wave sees and reports; it changes nothing.
The auto-adjust hands (Wave 142) will only act on verdicts this ledger
marks READY, after Wayne signs off on the bounds - his law: any change
to what fires needs data proof + explicit sign-off.

Verdict math mirrors the house standard (Wave 63): a bucket is READY
to consider loosening only when it has MIN_SAMPLE graded outcomes AND
its shadow win rate clears the break-even rate for its average R:R by
a 25% margin. Anything below that is NOT_READY - keep collecting.

Outputs:
  - build_ledger()        -> dict (all buckets + verdicts + coverage)
  - build_ledger_report() -> monospace text for Telegram (/ledger)
  - data/filter_ledger.json   (latest ledger snapshot, atomic write)
  - data/learning_loop.jsonl  (append-only audit: verdict CHANGES only,
                               so the file records the story, not spam)
"""

from __future__ import annotations
import os
import re
import json
from datetime import datetime, timezone

import strategy_log as sl
import safe_io

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE_DIR, "data")
LEDGER_JSON = os.path.join(_DATA_DIR, "filter_ledger.json")
LOOP_JSONL = os.path.join(_DATA_DIR, "learning_loop.jsonl")

os.makedirs(_DATA_DIR, exist_ok=True)

MIN_SAMPLE = 15        # graded outcomes required before any verdict
MARGIN = 1.25          # WR must beat break-even by 25% (Wave 63 standard)
WR_CAP = 0.95          # never require more than 95% (avoid impossible bars)


# ---------------------------------------------------------------- classify
def classify_reason(reason: str):
    """Map a raw reject_reason string to (filter_class, near_miss_band).

    The band answers "how close was this to passing?" - only near misses
    are loosen candidates; a 0.05x volume print failing a 0.8x gate is
    not evidence the gate is wrong.
    """
    r = reason or ""
    m = re.search(r"vol_ratio >= 0\.8x \(got ([0-9.]+)x\)", r)
    if m:
        try:
            v = float(m.group(1))
        except ValueError:
            return "VOL_CONFIRM_0.8", "ALL"
        if v >= 0.6:
            return "VOL_CONFIRM_0.8", "NEAR_0.6-0.8"
        if v >= 0.4:
            return "VOL_CONFIRM_0.8", "MID_0.4-0.6"
        return "VOL_CONFIRM_0.8", "FAR_<0.4"
    m = re.search(r"dead market \(vol_ratio=([0-9.]+)x", r)
    if m:
        try:
            v = float(m.group(1))
        except ValueError:
            return "VOL_FLOOR_0.3", "ALL"
        return ("VOL_FLOOR_0.3", "NEAR_0.2-0.3") if v >= 0.2 else ("VOL_FLOOR_0.3", "FAR_<0.2")
    if r.startswith("Wave 74 trend-alignment guard"):
        return "TREND_GUARD", "ALL"
    if r.startswith("Suspended due to"):
        return "SUSPENDED", "ALL"
    m = re.search(r"ADX ([0-9.]+) below .*? minimum ([0-9.]+)", r)
    if m:
        try:
            gap = float(m.group(2)) - float(m.group(1))
        except ValueError:
            return "ADX_MIN", "ALL"
        if gap < 3:
            return "ADX_MIN", "NEAR_<3"
        if gap < 8:
            return "ADX_MIN", "MID_3-8"
        return "ADX_MIN", "FAR_>8"
    m = re.search(r"R:R ([0-9.]+) below minimum ([0-9.]+)", r)
    if m:
        try:
            gap = float(m.group(2)) - float(m.group(1))
        except ValueError:
            return "RR_MIN", "ALL"
        return ("RR_MIN", "NEAR_<0.3") if gap < 0.3 else ("RR_MIN", "FAR_>=0.3")
    if "Conviction" in r and "short of" in r:
        return "CONV_MIN", "NEAR"
    if "news" in r.lower():
        return "NEWS", "ALL"
    return "OTHER", "ALL"


# ------------------------------------------------------------------ ledger
def build_ledger() -> dict:
    """Aggregate every graded shadow outcome into per-filter buckets."""
    try:
        rows = sl._load_all_strategy_rows()
    except Exception:
        rows = []
    buckets = {}
    graded = 0
    for row in rows:
        res = row.get("result", "")
        if res not in ("WOULD_WIN", "WOULD_LOSE"):
            continue
        if row.get("decision") == sl.DECISION_FIRED:
            continue
        fc, band = classify_reason(row.get("reject_reason", ""))
        market = row.get("market", "?") or "?"
        key = "%s|%s|%s" % (fc, band, market)
        b = buckets.setdefault(key, {
            "filter": fc, "band": band, "market": market,
            "wins": 0, "losses": 0, "rr_sum": 0.0, "rr_n": 0, "setups": {},
        })
        if res == "WOULD_WIN":
            b["wins"] += 1
        else:
            b["losses"] += 1
        graded += 1
        try:
            rr = float(row.get("rr") or 0)
            if rr > 0:
                b["rr_sum"] += rr
                b["rr_n"] += 1
        except (TypeError, ValueError):
            pass
        st = row.get("setup_type", "?") or "?"
        b["setups"][st] = b["setups"].get(st, 0) + 1

    entries = []
    for key, b in buckets.items():
        n = b["wins"] + b["losses"]
        wr = (b["wins"] / n) if n else 0.0
        avg_rr = (b["rr_sum"] / b["rr_n"]) if b["rr_n"] else 0.0
        if avg_rr > 0:
            breakeven = 1.0 / (1.0 + avg_rr)
            required = min(WR_CAP, breakeven * MARGIN)
            expectancy = wr * avg_rr - (1.0 - wr)
        else:
            breakeven = 1.0
            required = 1.0
            expectancy = -(1.0 - wr)
        if n < MIN_SAMPLE:
            verdict = "NOT_READY_SAMPLE"
        elif wr >= required:
            verdict = "READY_LOOSEN_CANDIDATE"
        elif n >= MIN_SAMPLE and wr < breakeven * 0.75:
            verdict = "GATE_EARNING_ITS_KEEP"
        else:
            verdict = "INCONCLUSIVE"
        top_setups = sorted(b["setups"].items(), key=lambda x: x[1], reverse=True)[:3]
        entries.append({
            "key": key, "filter": b["filter"], "band": b["band"],
            "market": b["market"], "n": n, "wins": b["wins"],
            "losses": b["losses"], "shadow_wr": round(wr * 100.0, 1),
            "avg_rr": round(avg_rr, 2), "breakeven_wr": round(breakeven * 100.0, 1),
            "required_wr": round(required * 100.0, 1),
            "expectancy_r": round(expectancy, 3), "verdict": verdict,
            "top_setups": top_setups,
        })
    entries.sort(key=lambda e: (e["verdict"] != "READY_LOOSEN_CANDIDATE", -e["n"]))
    ledger = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "graded_total": graded,
        "min_sample": MIN_SAMPLE,
        "entries": entries,
    }
    _persist(ledger)
    return ledger


def _persist(ledger: dict):
    """Atomic snapshot + append-only audit of verdict CHANGES only."""
    prev = {}
    try:
        if os.path.exists(LEDGER_JSON):
            with open(LEDGER_JSON, "r", encoding="utf-8") as fh:
                for e in (json.load(fh) or {}).get("entries", []):
                    prev[e.get("key")] = e.get("verdict")
    except Exception:
        prev = {}
    try:
        if hasattr(safe_io, "atomic_write_json"):
            safe_io.atomic_write_json(LEDGER_JSON, ledger)
        else:  # extremely defensive - safe_io has had this since early waves
            with open(LEDGER_JSON, "w", encoding="utf-8") as fh:
                json.dump(ledger, fh)
    except Exception:
        pass
    # audit only the story beats: a bucket's verdict changing
    try:
        for e in ledger.get("entries", []):
            if prev.get(e["key"]) != e["verdict"]:
                rec = {
                    "ts": ledger["built_at"], "event": "verdict",
                    "key": e["key"], "from": prev.get(e["key"]), "to": e["verdict"],
                    "n": e["n"], "shadow_wr": e["shadow_wr"],
                    "required_wr": e["required_wr"], "avg_rr": e["avg_rr"],
                }
                if hasattr(safe_io, "safe_append_jsonl"):
                    safe_io.safe_append_jsonl(LOOP_JSONL, rec)
                else:
                    with open(LOOP_JSONL, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(rec, default=str) + "\n")
    except Exception:
        pass


# ------------------------------------------------------------------ report
def build_ledger_report() -> str:
    """Monospace, mobile-honest report for /ledger."""
    led = build_ledger()
    DIV = "\u2501" * 27
    L = ["\U0001F9FE FILTER LEDGER", DIV]
    L.append("  Graded paper trades: %d" % led["graded_total"])
    L.append("  Verdict needs n>=%d + WR %sx break-even" % (led["min_sample"], MARGIN))
    entries = led["entries"]
    if not entries:
        L.append("")
        L.append("  No graded shadow outcomes yet.")
        L.append("  Wave 140 is collecting - every deduped")
        L.append("  rejection becomes a paper trade. Give")
        L.append("  it a few sessions and check back.")
        return "\n".join(L)

    ready = [e for e in entries if e["verdict"] == "READY_LOOSEN_CANDIDATE"]
    keep = [e for e in entries if e["verdict"] == "GATE_EARNING_ITS_KEEP"]
    building = [e for e in entries if e["verdict"] == "NOT_READY_SAMPLE"]
    incon = [e for e in entries if e["verdict"] == "INCONCLUSIVE"]

    if ready:
        L.append(DIV)
        L.append("\u26A1 LOOSEN CANDIDATES (proof met)")
        for e in ready[:6]:
            L.append("  %s %s %s" % (e["market"], e["filter"], e["band"]))
            L.append("    %dW/%dL = %.0f%% WR (needs %.0f%%) rr%.1f"
                     % (e["wins"], e["losses"], e["shadow_wr"], e["required_wr"], e["avg_rr"]))
    if keep:
        L.append(DIV)
        L.append("\U0001F6E1 GATES EARNING THEIR KEEP")
        for e in keep[:6]:
            L.append("  %s %s %s" % (e["market"], e["filter"], e["band"]))
            L.append("    %dW/%dL = %.0f%% WR (break-even %.0f%%)"
                     % (e["wins"], e["losses"], e["shadow_wr"], e["breakeven_wr"]))
    if incon:
        L.append(DIV)
        L.append("\u2696 INCONCLUSIVE (watching)")
        for e in incon[:4]:
            L.append("  %-3s %-16s n%d %.0f%%" % (e["market"], e["filter"], e["n"], e["shadow_wr"]))
    if building:
        L.append(DIV)
        L.append("\U0001F331 STILL COLLECTING (n<%d)" % led["min_sample"])
        for e in building[:6]:
            L.append("  %-3s %-16s n%d" % (e["market"], e["filter"], e["n"]))
    L.append(DIV)
    L.append("  Read-only: nothing auto-changes yet.")
    L.append("  Wave 142 acts only on proof + sign-off.")
    return "\n".join(L)
