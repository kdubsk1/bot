"""
wave169_migrate.py

Wave 169: HONEST REAL-COUNTER BACKFILL (Jul 24, 2026)

ONE-SHOT, IDEMPOTENT migration. Backfills real_wins / real_losses /
real_total / real_win_rate in data/setup_performance.json from the REAL
trade ledgers (outcomes.csv + data/archive/outcomes_*.csv).

WHY
===
Wave 152 (_WAVE152_HONEST_COUNTER) started TAGGING every counter increment
with its source, writing real_* and paper_* alongside the legacy blended
wins/losses/total. But the historical real trades that closed BEFORE Wave
152 were never backfilled, so real_* is near-empty today.

Measured on the live data file this session:
    - 80 setups, blended "trades" total = 3472
    - real fired trades in those same counters = 8
    -> the scorecard the suspension engine reads is ~99.8 percent PAPER.

The suspension engine (outcome_tracker.check_and_update_suspensions, the
reads at ~L486-489) and the Bayesian conviction penalty (_performance_bonus,
reads at ~L761-763) both consume the BLENDED counters. That is how ~45
setups came to be benched - including a batch of ~32 stamped
2026-07-15T22:13:02 - on losses that never cost a real dollar.

This migration does NOT change that behavior. It only makes the honest
real_* numbers EXIST and be correct, so a later wave can repoint those
gates to read real-only evidence with a real sample behind it.

WHAT THIS MIGRATION DOES
========================
1. Idempotency check: if data/wave169_complete.json exists, skip.
2. Backup: copy setup_performance.json to *.pre_wave169.bak
   (one-rename recovery).
3. Read every real close from outcomes.csv + data/archive/outcomes_*.csv.
   Counted rows: status == CLOSED and result in (WIN, LOSS).
   Deduplicated by alert_id (a trade present in both the live ledger and
   an archive counts exactly once).
4. For each EXISTING market:setup entry in setup_performance.json, write
   real_wins / real_losses / real_total / real_win_rate as an absolute
   recompute from the ledgers.
5. Write data/wave169_audit.json (full before/after per setup, plus a
   read-only PREVIEW of which suspended setups have no real losing
   evidence) and data/wave169_complete.json marker.

WHAT THIS MIGRATION DOES NOT DO
===============================
- Does NOT modify the blended wins / losses / total / win_rate fields.
  Those keep feeding every gate exactly as they do today.
- Does NOT modify suspended_setups.json. Nothing is un-benched here.
- Does NOT modify outcomes.csv or any archive file. Ledgers are read-only.
- Does NOT create new setup_performance entries. A setup that appears in
  the ledgers but has no perf entry is reported in the audit and skipped.
- Does NOT change conviction, sizing, RR floors, or what is allowed to fire.

Net effect on live trading behavior today: ZERO. This wave only adds
fields that no gate reads yet. The repoint is a separate, later wave.

RECOVERY
========
Rename data/setup_performance.json.pre_wave169.bak back over
data/setup_performance.json and delete data/wave169_complete.json.

Q: Idempotent?
A: Yes. data/wave169_complete.json prevents re-runs. The computation is
   also an absolute recompute from the ledgers, so even a forced re-run
   produces identical values (no double counting).

Q: Does it fight the live Wave 152 counters?
A: No. It sets the historical truth; record_trade_result keeps incrementing
   real_* forward from there.
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
MARKER_FILE    = os.path.join(DATA_DIR, "wave169_complete.json")
AUDIT_FILE     = os.path.join(DATA_DIR, "wave169_audit.json")

# Live suspension-gate constants (outcome_tracker.py L129-133). Used ONLY to
# generate a read-only preview in the audit. No suspension is changed here.
_SUSPEND_MIN_TRADES = 5
_SUSPEND_WR_BELOW   = 35.0


def is_already_complete() -> bool:
    """Idempotency check. True if migration already ran."""
    return os.path.exists(MARKER_FILE)


def _backup_files() -> list:
    """Create *.pre_wave169.bak copies."""
    created = []
    for target in (PERF_FILE,):
        if not os.path.exists(target):
            continue
        bak = target + ".pre_wave169.bak"
        if os.path.exists(bak):
            created.append(bak + " (existing, kept)")
            continue
        try:
            shutil.copy2(target, bak)
            created.append(bak)
        except Exception as e:
            _log.warning("Wave 169 backup failed for %s: %s", target, e)
    return created


def _ledger_files() -> list:
    """Live ledger first, then archives (sorted for deterministic order)."""
    files = []
    if os.path.exists(OUTCOMES_CSV):
        files.append(OUTCOMES_CSV)
    files.extend(sorted(glob.glob(ARCHIVE_GLOB)))
    return files


def collect_real_closes(audit: dict) -> dict:
    """
    Read every real close from the ledgers.
    Returns {alert_id: (market, setup, result)}. Dedup by alert_id.
    """
    by_alert = {}
    files_read = 0
    rows_scanned = 0

    for path in _ledger_files():
        try:
            with open(path, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for raw in reader:
                    rows_scanned += 1
                    row = {}
                    for k, v in raw.items():
                        if k is None:
                            continue
                        row[k.strip().lower()] = (v or "").strip()
                    aid    = row.get("alert_id", "")
                    market = row.get("market", "")
                    setup  = row.get("setup", "")
                    status = row.get("status", "").upper()
                    result = row.get("result", "").upper()
                    if not aid or not market or not setup:
                        continue
                    if status != "CLOSED":
                        continue
                    if result not in ("WIN", "LOSS"):
                        continue
                    by_alert[aid] = (market, setup, result)
            files_read += 1
        except Exception as e:
            audit.setdefault("errors", []).append(
                "read_failed %s: %s" % (os.path.basename(path), e)
            )
            _log.warning("Wave 169 could not read %s: %s", path, e)

    audit["ledger_files_read"] = files_read
    audit["ledger_rows_scanned"] = rows_scanned
    audit["real_closes_deduped"] = len(by_alert)
    return by_alert


def aggregate_real(by_alert: dict) -> dict:
    """market:setup -> real_wins / real_losses / real_total / real_win_rate."""
    real = {}
    for _aid, (market, setup, result) in by_alert.items():
        key = "%s:%s" % (market, setup)
        d = real.setdefault(key, {"real_wins": 0, "real_losses": 0})
        if result == "WIN":
            d["real_wins"] += 1
        else:
            d["real_losses"] += 1
    for _key, d in real.items():
        total = d["real_wins"] + d["real_losses"]
        d["real_total"] = total
        d["real_win_rate"] = round(d["real_wins"] / total * 100, 1) if total else 0.0
    return real


def _preview_unbench(real: dict, audit: dict):
    """
    READ-ONLY preview for the NEXT wave: which currently-suspended setups have
    no real losing evidence. Changes nothing.
    """
    suspended = {}
    if os.path.exists(SUSPENDED_FILE):
        try:
            with open(SUSPENDED_FILE, "r", encoding="utf-8") as f:
                suspended = json.load(f) or {}
        except Exception as e:
            audit.setdefault("errors", []).append("suspended_read_failed: %s" % e)
            return

    keep, free = [], []
    for key in suspended:
        r = real.get(key, {})
        rt = int(r.get("real_total", 0))
        rwr = float(r.get("real_win_rate", 0.0))
        if rt >= _SUSPEND_MIN_TRADES and rwr < _SUSPEND_WR_BELOW:
            keep.append({"key": key, "real_total": rt, "real_win_rate": rwr})
        else:
            free.append({"key": key, "real_total": rt, "real_win_rate": rwr})

    audit["preview_suspended_now"] = len(suspended)
    audit["preview_would_keep_suspended"] = keep
    audit["preview_no_real_bad_evidence"] = free
    audit["preview_note"] = (
        "PREVIEW ONLY - no suspension was changed by Wave 169. The real-dollar "
        "bleed gate ($500/7d) is not evaluated here and remains live."
    )


def backfill_perf(real: dict, audit: dict) -> int:
    """Write real_* into EXISTING setup_performance entries. Blended untouched."""
    import safe_io

    # Nothing real to write -> do not touch the file at all. This guards the
    # case where the ledgers are missing/unreadable: we would rather change
    # nothing than rewrite the learning file for no reason.
    if not real:
        audit["setups_backfilled"] = 0
        audit["per_setup_changes"] = {}
        audit["ledger_only_setups_skipped"] = []
        audit["skipped_write_reason"] = "no real closes found in ledgers"
        _log.warning("Wave 169: no real closes found - setup_performance.json left untouched")
        return 0

    perf = {}
    if os.path.exists(PERF_FILE):
        try:
            with open(PERF_FILE, "r", encoding="utf-8") as f:
                perf = json.load(f) or {}
        except Exception as e:
            audit.setdefault("errors", []).append("perf_read_failed: %s" % e)
            raise

    changes = {}
    ledger_only = []
    written = 0

    for key, vals in sorted(real.items()):
        entry = perf.get(key)
        if not isinstance(entry, dict):
            ledger_only.append(key)
            continue
        before = {
            "real_wins":   entry.get("real_wins"),
            "real_losses": entry.get("real_losses"),
            "real_total":  entry.get("real_total"),
        }
        entry["real_wins"]     = vals["real_wins"]
        entry["real_losses"]   = vals["real_losses"]
        entry["real_total"]    = vals["real_total"]
        entry["real_win_rate"] = vals["real_win_rate"]
        after = {
            "real_wins":     vals["real_wins"],
            "real_losses":   vals["real_losses"],
            "real_total":    vals["real_total"],
            "real_win_rate": vals["real_win_rate"],
            "blended_total": entry.get("total"),
            "blended_win_rate": entry.get("win_rate"),
        }
        changes[key] = {"before": before, "after": after}
        written += 1

    os.makedirs(DATA_DIR, exist_ok=True)
    safe_io.atomic_write_json(PERF_FILE, perf)

    audit["setups_backfilled"] = written
    audit["per_setup_changes"] = changes
    audit["ledger_only_setups_skipped"] = ledger_only
    return written


def run_migration() -> dict:
    audit = {
        "wave": 169,
        "purpose": "backfill real_wins/real_losses/real_total from real ledgers",
        "timestamp_started": datetime.now(timezone.utc).isoformat(),
        "errors": [],
    }

    audit["backups"] = _backup_files()

    by_alert = collect_real_closes(audit)
    real = aggregate_real(by_alert)
    audit["setups_with_real_trades"] = len(real)

    _preview_unbench(real, audit)
    backfill_perf(real, audit)

    audit["timestamp_completed"] = datetime.now(timezone.utc).isoformat()
    return audit


def _write_audit(audit: dict):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(AUDIT_FILE, "w", encoding="utf-8") as f:
            json.dump(audit, f, indent=2)
    except Exception as e:
        _log.warning("Wave 169 audit write failed: %s", e)


def _write_marker(audit: dict):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        marker = {
            "wave": 169,
            "completed_at":        audit.get("timestamp_completed"),
            "setups_backfilled":   audit.get("setups_backfilled", 0),
            "real_closes_deduped": audit.get("real_closes_deduped", 0),
            "ledger_files_read":   audit.get("ledger_files_read", 0),
            "errors":              len(audit.get("errors", [])),
        }
        with open(MARKER_FILE, "w", encoding="utf-8") as f:
            json.dump(marker, f, indent=2)
    except Exception as e:
        _log.warning("Wave 169 marker write failed: %s", e)


def maybe_run() -> dict:
    """
    Top-level entry point. Called from bot.py _post_init AFTER wave13_migrate.
    Returns a dict with status info. Never raises.
    """
    if is_already_complete():
        return {"ran": False, "ok": True, "reason": "already_complete"}

    try:
        _log.info("Wave 169 migration starting (real-counter backfill)...")
        audit = run_migration()
        _write_audit(audit)
        _write_marker(audit)

        n_setups  = audit.get("setups_backfilled", 0)
        n_closes  = audit.get("real_closes_deduped", 0)
        n_files   = audit.get("ledger_files_read", 0)
        n_free    = len(audit.get("preview_no_real_bad_evidence", []))
        n_errors  = len(audit.get("errors", []))

        summary = (
            "backfilled=%d real_closes=%d ledgers=%d "
            "preview_no_real_bad_evidence=%d errors=%d"
            % (n_setups, n_closes, n_files, n_free, n_errors)
        )
        _log.info("Wave 169 migration complete: %s", summary)
        return {"ran": True, "ok": n_errors == 0, "summary": summary, "audit": AUDIT_FILE}

    except Exception as e:
        _log.error("Wave 169 migration FAILED: %s", e, exc_info=True)
        return {"ran": True, "ok": False, "summary": "failed: %s" % e}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    print(json.dumps(maybe_run(), indent=2))
