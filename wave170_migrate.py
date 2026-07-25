"""
wave170_migrate.py

Wave 170: EXPECTANCY TRUTH + THE GREAT UN-BENCH (Jul 25, 2026)

ONE-SHOT, IDEMPOTENT migration. Two jobs:

  1. Write real_avg_rr_win + real_expectancy into data/setup_performance.json,
     computed from the REAL trade ledgers (outcomes.csv + data/archive/*).
  2. Rebuild data/suspended_setups.json so a setup is benched ONLY when real
     money says it loses.

WHY (measured on 359 real closed trades this session)
=====================================================
The bench gate benches on WIN RATE < 35 percent. Win rate alone is meaningless
without R:R, and the live data proves it:

    BTC:BREAK_RETEST_BEAR   22.2% WR but 3.82R avg win -> +0.070R  PROFITABLE
    SOL:VWAP_BOUNCE_BULL    15.4% WR at  1.82R avg win -> -0.566R  genuinely bad

Same "low win rate", opposite economics. A win-rate gate cannot tell them apart,
so it benched money-makers. Measured consequences on the live bench list:

    BTC:EMA21_PULLBACK_BEAR  7W/4L  63.6%  2.70R  ->  +1.355R  BENCHED
    GC:BB_REVERSION_BEAR     4W/5L  44.4%  3.36R  ->  +0.936R  BENCHED
    NQ:BB_REVERSION_BULL     6W/7L  46.2%  2.49R  ->  +0.612R  BENCHED

And the whole eval book (NQ+GC) is 60W/107L = 35.9% at 2.53R average win, which
is an expectancy of +0.269R per trade. Breakeven at 2.53R is 28.3% WR. The bot
has a real edge; it simply was not allowed to fire.

Worst of all, EVERY NQ short setup (8 of 8) was benched, all of them on ZERO or
ONE real trade - pure paper evidence. The bot could not short its own eval
instrument at all.

THE NEW RULE (real money only)
==============================
Bench a setup only when ALL of:
    - it has at least _W170_MIN_REAL_TRADES (8) REAL closed trades, and
    - its REAL expectancy is below _W170_MIN_EXPECTANCY (-0.10 R/trade)
The real-dollar bleed gate ($500 / 7d) is untouched and remains the backstop.

Expectancy = winrate * avg_RR_on_wins - (1 - winrate)   [in R units]

A setup with too little REAL evidence is NOT benched. Unproven is not guilty.

WHAT THIS MIGRATION DOES
========================
1. Idempotency check: if data/wave170_complete.json exists, skip.
2. Backup setup_performance.json and suspended_setups.json to *.pre_wave170.bak
3. Recompute per setup from the ledgers (dedup by alert_id):
     real_wins, real_losses, real_total, real_win_rate,
     real_avg_rr_win, real_expectancy
   (real_* counters are refreshed here too, so this is self-sufficient even if
   Wave 169 had not run.)
4. Rebuild suspended_setups.json: keep an entry ONLY if it fails the new rule.
   Every freed setup is recorded in the audit with its evidence.
5. Write data/wave170_audit.json + data/wave170_complete.json.

WHAT THIS MIGRATION DOES NOT DO
===============================
- Does NOT touch the blended wins/losses/total/win_rate fields.
- Does NOT touch outcomes.csv or any archive (ledgers are read-only).
- Does NOT change conviction scoring, sizing, RR floors, or the entry gate.
  A freed setup still has to earn its conviction score to fire.
- Does NOT touch the parole/probation records (Wave 144/157) or the dollar
  bleed gate.

RECOVERY
========
Rename data/setup_performance.json.pre_wave170.bak and
data/suspended_setups.json.pre_wave170.bak back over their originals and delete
data/wave170_complete.json.
"""

import os
import json
import csv
import glob
import shutil
import logging
from datetime import datetime, timezone

_log = logging.getLogger(__name__)

_BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DATA_DIR       = os.path.join(_BASE_DIR, "data")
OUTCOMES_CSV   = os.path.join(_BASE_DIR, "outcomes.csv")
ARCHIVE_GLOB   = os.path.join(DATA_DIR, "archive", "outcomes_*.csv")
PERF_FILE      = os.path.join(DATA_DIR, "setup_performance.json")
SUSPENDED_FILE = os.path.join(DATA_DIR, "suspended_setups.json")
MARKER_FILE    = os.path.join(DATA_DIR, "wave170_complete.json")
AUDIT_FILE     = os.path.join(DATA_DIR, "wave170_audit.json")

# The new bench rule. Kept in sync with outcome_tracker._W170_* constants.
_W170_MIN_REAL_TRADES = 8
_W170_MIN_EXPECTANCY  = -0.10
# Conservative fallback when a setup has wins but no usable RR data.
_W170_DEFAULT_RR      = 2.0


def is_already_complete() -> bool:
    """Idempotency check. True if migration already ran."""
    return os.path.exists(MARKER_FILE)


def _backup_files() -> list:
    created = []
    for target in (PERF_FILE, SUSPENDED_FILE):
        if not os.path.exists(target):
            continue
        bak = target + ".pre_wave170.bak"
        if os.path.exists(bak):
            created.append(os.path.basename(bak) + " (existing, kept)")
            continue
        try:
            shutil.copy2(target, bak)
            created.append(os.path.basename(bak))
        except Exception as e:
            _log.warning("Wave 170 backup failed for %s: %s", target, e)
    return created


def _ledger_files() -> list:
    files = []
    if os.path.exists(OUTCOMES_CSV):
        files.append(OUTCOMES_CSV)
    files.extend(sorted(glob.glob(ARCHIVE_GLOB)))
    return files


def collect_real(audit: dict) -> dict:
    """
    Read every real close. Returns {alert_id: (market, setup, result, rr)}.
    Dedup by alert_id so a trade in both the live ledger and an archive counts once.
    """
    by_alert = {}
    files_read = 0
    for path in _ledger_files():
        try:
            with open(path, "r", newline="", encoding="utf-8", errors="replace") as f:
                for raw in csv.DictReader(f):
                    row = {}
                    for k, v in raw.items():
                        if k is None:
                            continue
                        row[k.strip().lower()] = (v or "").strip()
                    aid    = row.get("alert_id", "")
                    market = row.get("market", "")
                    setup  = row.get("setup", "")
                    if not aid or not market or not setup:
                        continue
                    if row.get("status", "").upper() != "CLOSED":
                        continue
                    result = row.get("result", "").upper()
                    if result not in ("WIN", "LOSS"):
                        continue
                    try:
                        rr = float(row.get("rr", "") or 0.0)
                    except Exception:
                        rr = 0.0
                    by_alert[aid] = (market, setup, result, rr)
            files_read += 1
        except Exception as e:
            audit.setdefault("errors", []).append(
                "read_failed %s: %s" % (os.path.basename(path), e))
            _log.warning("Wave 170 could not read %s: %s", path, e)
    audit["ledger_files_read"] = files_read
    audit["real_closes_deduped"] = len(by_alert)
    return by_alert


def aggregate(by_alert: dict) -> dict:
    """market:setup -> real counters + avg RR on wins + expectancy (R/trade)."""
    agg = {}
    for _aid, (market, setup, result, rr) in by_alert.items():
        key = "%s:%s" % (market, setup)
        d = agg.setdefault(key, {"real_wins": 0, "real_losses": 0, "_rr_sum": 0.0})
        if result == "WIN":
            d["real_wins"] += 1
            if rr > 0:
                d["_rr_sum"] += rr
        else:
            d["real_losses"] += 1
    for _key, d in agg.items():
        wins   = d["real_wins"]
        total  = wins + d["real_losses"]
        d["real_total"] = total
        wr = (wins / total) if total else 0.0
        d["real_win_rate"] = round(wr * 100, 1)
        avg_rr = (d["_rr_sum"] / wins) if wins and d["_rr_sum"] > 0 else 0.0
        d["real_avg_rr_win"] = round(avg_rr, 3)
        eff_rr = avg_rr if avg_rr > 0 else _W170_DEFAULT_RR
        # No wins at all -> every trade lost 1R.
        d["real_expectancy"] = round(wr * eff_rr - (1.0 - wr), 4) if total else 0.0
        del d["_rr_sum"]
    return agg


def should_bench(stats: dict) -> bool:
    """The new rule: real evidence only, expectancy based."""
    if not stats:
        return False
    if int(stats.get("real_total", 0)) < _W170_MIN_REAL_TRADES:
        return False
    return float(stats.get("real_expectancy", 0.0)) < _W170_MIN_EXPECTANCY


def write_perf(agg: dict, audit: dict) -> int:
    import safe_io
    if not agg:
        audit["setups_updated"] = 0
        audit["skipped_write_reason"] = "no real closes found in ledgers"
        _log.warning("Wave 170: no real closes - setup_performance.json left untouched")
        return 0
    perf = {}
    if os.path.exists(PERF_FILE):
        with open(PERF_FILE, "r", encoding="utf-8") as f:
            perf = json.load(f) or {}
    written = 0
    ledger_only = []
    for key, vals in sorted(agg.items()):
        entry = perf.get(key)
        if not isinstance(entry, dict):
            ledger_only.append(key)
            continue
        entry["real_wins"]       = vals["real_wins"]
        entry["real_losses"]     = vals["real_losses"]
        entry["real_total"]      = vals["real_total"]
        entry["real_win_rate"]   = vals["real_win_rate"]
        entry["real_avg_rr_win"] = vals["real_avg_rr_win"]
        entry["real_expectancy"] = vals["real_expectancy"]
        written += 1
    os.makedirs(DATA_DIR, exist_ok=True)
    safe_io.atomic_write_json(PERF_FILE, perf)
    audit["setups_updated"] = written
    audit["ledger_only_setups_skipped"] = ledger_only
    return written


def rebuild_suspensions(agg: dict, audit: dict) -> dict:
    import safe_io
    suspended = {}
    if os.path.exists(SUSPENDED_FILE):
        try:
            with open(SUSPENDED_FILE, "r", encoding="utf-8") as f:
                suspended = json.load(f) or {}
        except Exception as e:
            audit.setdefault("errors", []).append("suspended_read_failed: %s" % e)
            return {}

    kept, freed = {}, []
    for key, meta in suspended.items():
        stats = agg.get(key, {})
        ev = {
            "real_total":      int(stats.get("real_total", 0)),
            "real_win_rate":   float(stats.get("real_win_rate", 0.0)),
            "real_avg_rr_win": float(stats.get("real_avg_rr_win", 0.0)),
            "real_expectancy": float(stats.get("real_expectancy", 0.0)),
            "old_reason":      (meta or {}).get("reason", ""),
        }
        if should_bench(stats):
            new_meta = dict(meta or {})
            new_meta["reason"] = (
                "REAL evidence: %dW/%dL over %d real trades, avg %.2fR, "
                "expectancy %+.3fR/trade"
                % (int(stats.get("real_wins", 0)), int(stats.get("real_losses", 0)),
                   ev["real_total"], ev["real_avg_rr_win"], ev["real_expectancy"])
            )
            new_meta["wave170_evidence"] = ev
            kept[key] = new_meta
        else:
            freed.append({"key": key, **ev})

    # Also bench anything NOT currently suspended that now fails the real rule.
    newly = []
    for key, stats in agg.items():
        if key in kept or key in suspended:
            continue
        if should_bench(stats):
            kept[key] = {
                "reason": (
                    "REAL evidence: %dW/%dL over %d real trades, avg %.2fR, "
                    "expectancy %+.3fR/trade"
                    % (stats["real_wins"], stats["real_losses"], stats["real_total"],
                       stats["real_avg_rr_win"], stats["real_expectancy"])
                ),
                "suspended_at": datetime.now(timezone.utc).isoformat(),
                "wave170_evidence": {
                    "real_total": stats["real_total"],
                    "real_win_rate": stats["real_win_rate"],
                    "real_avg_rr_win": stats["real_avg_rr_win"],
                    "real_expectancy": stats["real_expectancy"],
                },
            }
            newly.append({"key": key, "real_total": stats["real_total"],
                          "real_expectancy": stats["real_expectancy"]})

    os.makedirs(DATA_DIR, exist_ok=True)
    safe_io.atomic_write_json(SUSPENDED_FILE, kept)

    audit["suspended_before"] = len(suspended)
    audit["suspended_after"]  = len(kept)
    audit["freed"]            = sorted(freed, key=lambda r: -r["real_expectancy"])
    audit["freed_count"]      = len(freed)
    audit["kept_benched"]     = sorted(kept.keys())
    audit["newly_benched"]    = newly
    return kept


def _short_coverage(agg: dict, suspended: dict, audit: dict):
    """Report free short/long setups per market so a market can never be silently
    left with no way to trade one direction."""
    def is_short(name):
        s = name.split(":", 1)[1] if ":" in name else name
        return ("BEAR" in s) or ("BREAKDOWN" in s) or ("REJECT" in s) or ("RESIST" in s)
    universe = set()
    if os.path.exists(PERF_FILE):
        try:
            with open(PERF_FILE, "r", encoding="utf-8") as f:
                universe = set((json.load(f) or {}).keys())
        except Exception:
            pass
    universe |= set(agg.keys())
    cov = {}
    for key in universe:
        if ":" not in key or "|" in key:
            continue
        market = key.split(":")[0]
        c = cov.setdefault(market, {"short_free": 0, "short_total": 0,
                                    "long_free": 0, "long_total": 0})
        benched = key in suspended
        if is_short(key):
            c["short_total"] += 1
            if not benched:
                c["short_free"] += 1
        else:
            c["long_total"] += 1
            if not benched:
                c["long_free"] += 1
    audit["short_coverage_after"] = cov
    blind = [m for m, c in cov.items() if c["short_total"] > 0 and c["short_free"] == 0]
    audit["markets_with_no_shorts"] = blind
    if blind:
        _log.warning("Wave 170: markets still with NO free shorts: %s", blind)


def run_migration() -> dict:
    audit = {
        "wave": 170,
        "purpose": "expectancy-based real-only bench rule + un-bench",
        "rule": {
            "min_real_trades": _W170_MIN_REAL_TRADES,
            "min_expectancy":  _W170_MIN_EXPECTANCY,
            "default_rr":      _W170_DEFAULT_RR,
        },
        "timestamp_started": datetime.now(timezone.utc).isoformat(),
        "errors": [],
    }
    audit["backups"] = _backup_files()
    by_alert = collect_real(audit)
    agg = aggregate(by_alert)
    audit["setups_with_real_trades"] = len(agg)
    write_perf(agg, audit)
    kept = rebuild_suspensions(agg, audit)
    _short_coverage(agg, kept, audit)
    audit["timestamp_completed"] = datetime.now(timezone.utc).isoformat()
    return audit


def _write_audit(audit: dict):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(AUDIT_FILE, "w", encoding="utf-8") as f:
            json.dump(audit, f, indent=2)
    except Exception as e:
        _log.warning("Wave 170 audit write failed: %s", e)


def _write_marker(audit: dict):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        marker = {
            "wave": 170,
            "completed_at":     audit.get("timestamp_completed"),
            "freed_count":      audit.get("freed_count", 0),
            "suspended_before": audit.get("suspended_before", 0),
            "suspended_after":  audit.get("suspended_after", 0),
            "errors":           len(audit.get("errors", [])),
        }
        with open(MARKER_FILE, "w", encoding="utf-8") as f:
            json.dump(marker, f, indent=2)
    except Exception as e:
        _log.warning("Wave 170 marker write failed: %s", e)


def maybe_run() -> dict:
    """Called from bot.py _post_init AFTER wave169_migrate. Never raises."""
    if is_already_complete():
        return {"ran": False, "ok": True, "reason": "already_complete"}
    try:
        _log.info("Wave 170 migration starting (expectancy rule + un-bench)...")
        audit = run_migration()
        _write_audit(audit)
        _write_marker(audit)
        n_err = len(audit.get("errors", []))
        summary = (
            "freed=%d suspended %d->%d newly_benched=%d real_closes=%d errors=%d"
            % (audit.get("freed_count", 0), audit.get("suspended_before", 0),
               audit.get("suspended_after", 0), len(audit.get("newly_benched", [])),
               audit.get("real_closes_deduped", 0), n_err)
        )
        _log.info("Wave 170 migration complete: %s", summary)
        return {"ran": True, "ok": n_err == 0, "summary": summary, "audit": AUDIT_FILE}
    except Exception as e:
        _log.error("Wave 170 migration FAILED: %s", e, exc_info=True)
        return {"ran": True, "ok": False, "summary": "failed: %s" % e}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    print(json.dumps(maybe_run(), indent=2))
