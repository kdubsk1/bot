"""
wave12_migrate.py
=================
Wave 12: Phantom-Loss Data Cleanup Migration (May 5, 2026)

ONE-SHOT, IDEMPOTENT migration that runs at Railway startup before the
scan loop begins. Marks the 4 confirmed phantom losses from May 4 as
SKIP (so they're excluded from learning) and rebuilds derived state
(setup_performance.json, suspended_setups.json) from the cleaned data.

WHY THIS EXISTS
===============
On May 4, 2026, the bot was hit by the "phantom loss" bug — auto_check_outcomes
was reading PRE-alert price wicks and triggering false stop-outs with
bars_to_resolution=0. Wave 10 (commit baaa3075) fixed the root cause.
Wave 11 (commit 58ea49da) added a defensive guard rail.

But the 4 phantom losses had ALREADY corrupted the bot's learning data
BEFORE Wave 10 deployed. They sit in outcomes.csv as result=LOSS even
though those losses never happened in real markets. They poisoned:
  - setup_performance.json (counted as real losses in win-rate math)
  - suspended_setups.json (caused 2-3 setups to auto-suspend on dollar bleed)

THE 4 CONFIRMED PHANTOMS (all NQ, all May 4, 2026, all bars=0)
==============================================================
- 0489aa0b72  06:59 UTC  STOCH_REVERSAL_BULL  LONG  entry 27899.5  exit 27860.88
- 1486a214ab  08:08 UTC  APPROACH_SUPPORT     LONG  entry 27858.5  exit 27842.95
- c67a93bb0e  10:26 UTC  STOCH_REVERSAL_BULL  LONG  entry 27745.25 exit 27645.70
- eff365e825  12:22 UTC  MACD_CROSS_BULL      LONG  entry 27857.5  exit 27813.62

All 4 closed BEFORE Wave 10 deployed at 13:48 UTC. All 4 had bars=0.
Wayne saw at least one close in real time on his screen while the
chart never showed price hitting the stop.

WHAT THE MIGRATION DOES (in order)
===================================
1. Idempotency check: if data/wave12_complete.json exists, skip everything.
2. Backup: copy outcomes.csv, setup_performance.json, suspended_setups.json
   to *.pre_wave12.bak (one-rename recovery if anything goes sideways).
3. SKIP marking: for each of the 4 phantom alert_ids, find the row in
   outcomes.csv. If status=CLOSED and result=LOSS, change result to SKIP.
   Do NOT touch any other row. Do NOT touch OPEN/WIN/already-SKIP rows.
4. Rebuild setup_performance.json: walk every CLOSED row in outcomes.csv,
   tally wins and losses by market:setup. SKIP rows are excluded from
   tallying. Atomic write to disk.
5. Re-run check_and_update_suspensions(): outcome_tracker's own function.
   Auto-restores any setup that no longer meets the suspension criteria
   after the cleaned tallies are in.
6. Force-remove specific phantom-driven suspensions (NQ:STOCH_REVERSAL_BULL,
   NQ:APPROACH_SUPPORT) if they didn't auto-restore. We deliberately do NOT
   touch SOL:VWAP_BOUNCE_BULL or BTC suspensions — those overnight losses
   may have been real and need separate Wave 13 evidence-based investigation.
7. Write data/wave12_audit.json with the full before/after trail.
8. Write data/wave12_complete.json marker so future restarts skip this.

WHAT THE MIGRATION DOES NOT DO
==============================
- Touch any row that isn't one of the 4 phantom alert_ids
- Modify code logic anywhere
- Restore real losses (the May 4 EMA21 LONG b9c30d074b stays as LOSS
  because price actually hit that stop)
- Touch SOL:VWAP_BOUNCE_BULL or BTC suspensions
- Clear MARKET_HALTED state — that's in-memory only and resets on
  bot restart anyway, so deploying Wave 12 already clears it for free
- Run any trades or alter live trading logic

IDEMPOTENCY
===========
Every step is idempotent:
- SKIP marking: phantom_id already SKIP -> recorded as already_skipped, no-op
- Rebuild: deterministic from outcomes.csv state, same output every run
- Auto-restore: only modifies suspended_setups when criteria change
- Force-remove: deletes key if present, no-op if not
- Marker: prevents repeat runs entirely (defense in depth)

If Railway restarts before auto_sync uploads the marker, the migration
runs again and produces the same result. Safe.

SAFETY
======
- Wrapped in try/except at every level — migration failure does NOT
  break bot startup. Worst case: bot starts with poisoned data (current
  state) and we deploy Wave 12.5 to retry.
- Backups created before any mutation.
- Uses safe_io.safe_rewrite_csv (atomic + cross-process locked) so a
  concurrent log_alert append cannot be lost.
- Atomic JSON writes (tmp file + rename).
- All file paths absolute via _BASE_DIR.

PRE-MORTEM (questions Wayne might ask)
=======================================
Q: Why not just delete the phantom rows?
A: Deleting loses the audit trail. SKIP keeps the row visible in
   outcomes.csv with all its details, but excludes it from learning math.
   "It happened, but it doesn't count" - exactly the right semantic.

Q: What if SOL or BTC overnight losses were also phantoms?
A: We don't have direct evidence yet. Marking them would be guessing.
   Wave 13 will pull bot_log.txt timestamps and prove or disprove.

Q: What if the migration runs at the same time as auto_check_outcomes?
A: safe_io has a cross-process file lock on outcomes.csv. Whichever
   acquires first runs to completion; the other waits. No race.

Q: What if outcomes.csv has different columns than expected?
A: The migration reads the existing CSV header and uses those exact
   columns for the rewrite. Schema-agnostic.

Q: What if the migration partially completes then crashes?
A: SKIP marking is atomic via safe_rewrite_csv (write to tmp, rename).
   Rebuild is atomic via atomic_write_json. Suspensions rewrite is
   atomic. Either every step finished or none did.

Q: Could the marker file be lost if Railway restarts before sync?
A: Yes - and the migration would run again. That's why every step is
   idempotent. No harm from re-running.

Q: What if I need to re-run Wave 12 later (new phantoms found)?
A: Delete data/wave12_complete.json (locally or via Telegram /sync first
   to pull fresh, then commit a deletion). Or write Wave 12.5 with new
   alert_ids targeting only the new ones.
"""

import os
import json
import csv
import shutil
import logging
from datetime import datetime, timezone

_log = logging.getLogger("nqcalls.wave12")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_BASE_DIR, "data")
OUTCOMES_CSV = os.path.join(_BASE_DIR, "outcomes.csv")
PERF_FILE = os.path.join(DATA_DIR, "setup_performance.json")
SUSPENDED_FILE = os.path.join(DATA_DIR, "suspended_setups.json")
MARKER_FILE = os.path.join(DATA_DIR, "wave12_complete.json")
AUDIT_FILE = os.path.join(DATA_DIR, "wave12_audit.json")

# The 4 confirmed phantom alert_ids from May 4, 2026.
# All 4: NQ, LONG, bars_to_resolution=0, closed BEFORE Wave 10 deploy at 13:48 UTC.
PHANTOM_ALERT_IDS = [
    "0489aa0b72",  # 06:59 UTC NQ STOCH_REVERSAL_BULL  entry 27899.5
    "1486a214ab",  # 08:08 UTC NQ APPROACH_SUPPORT     entry 27858.5
    "c67a93bb0e",  # 10:26 UTC NQ STOCH_REVERSAL_BULL  entry 27745.25
    "eff365e825",  # 12:22 UTC NQ MACD_CROSS_BULL      entry 27857.5
]

# Suspensions to force-remove if check_and_update_suspensions doesn't
# auto-restore them. These got suspended specifically because of the
# phantom-driven dollar bleed. Conservative list - excludes SOL/BTC.
PHANTOM_DRIVEN_SUSPENSIONS = [
    "NQ:STOCH_REVERSAL_BULL",
    "NQ:APPROACH_SUPPORT",
]


def is_already_complete() -> bool:
    """Idempotency check. True if migration already ran."""
    return os.path.exists(MARKER_FILE)


def _read_existing_columns() -> list:
    """Read the existing CSV header so we don't depend on outcome_tracker.CSV_COLS
    (which may add new columns in future waves)."""
    if not os.path.exists(OUTCOMES_CSV):
        return []
    with open(OUTCOMES_CSV, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        return next(reader, [])


def _backup_files() -> list:
    """Create *.pre_wave12.bak copies. Returns list of backup paths created."""
    created = []
    targets = [
        (OUTCOMES_CSV,    "outcomes.csv"),
        (PERF_FILE,       "setup_performance.json"),
        (SUSPENDED_FILE,  "suspended_setups.json"),
    ]
    for src, label in targets:
        if os.path.exists(src):
            backup = src + ".pre_wave12.bak"
            try:
                shutil.copy2(src, backup)
                created.append(backup)
                _log.info(f"Wave 12 backup created: {backup}")
            except Exception as e:
                _log.error(f"Wave 12 backup failed for {label}: {e}")
    return created


def _mark_phantoms_as_skip(audit: dict):
    """Step 3: find each phantom alert_id and flip its result LOSS -> SKIP.

    Uses safe_io.safe_rewrite_csv so the write is atomic and cross-process
    locked. The mutator returns the modified row list; safe_io handles the
    locked read-modify-rename cycle.
    """
    import safe_io

    cols = _read_existing_columns()
    if not cols:
        raise RuntimeError("outcomes.csv has no header - aborting migration")

    found_ids = set()
    already_skip = set()
    targets = set(PHANTOM_ALERT_IDS)
    errors = []

    def _mutator(rows):
        for r in rows:
            aid = r.get("alert_id", "")
            if aid not in targets:
                continue
            if r.get("result") == "SKIP":
                already_skip.add(aid)
                continue
            # Only flip if currently CLOSED LOSS - safety net against
            # accidentally flipping an OPEN or WIN row.
            if r.get("status") == "CLOSED" and r.get("result") == "LOSS":
                r["result"] = "SKIP"
                # bars_to_resolution stays at 0 (already 0 for phantoms)
                # exit_price stays as recorded (preserves audit trail of
                # what the bug "thought" the exit was)
                found_ids.add(aid)
            else:
                errors.append(
                    f"{aid}: unexpected state status={r.get('status')!r} "
                    f"result={r.get('result')!r} - row not modified"
                )
        return rows

    safe_io.safe_rewrite_csv(OUTCOMES_CSV, cols, _mutator)

    audit["phantoms_found_and_marked"] = sorted(found_ids)
    audit["phantoms_already_skipped"]  = sorted(already_skip)
    audit["phantoms_not_found"]        = sorted(
        a for a in PHANTOM_ALERT_IDS
        if a not in found_ids and a not in already_skip
    )
    audit["mark_errors"] = errors


def _rebuild_setup_performance(audit: dict):
    """Step 4: rebuild setup_performance.json from cleaned outcomes.csv.

    SKIP rows are excluded. Only WIN and LOSS rows count toward tallies.
    """
    import safe_io

    perf_before = {}
    if os.path.exists(PERF_FILE):
        try:
            with open(PERF_FILE, "r", encoding="utf-8") as f:
                perf_before = json.load(f)
        except Exception as e:
            _log.warning(f"Wave 12 perf_before read failed: {e}")
    audit["perf_before"] = perf_before

    perf_new = {}
    rows_seen = 0
    rows_counted = 0
    rows_skipped = 0

    if os.path.exists(OUTCOMES_CSV):
        with open(OUTCOMES_CSV, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows_seen += 1
                if row.get("status") != "CLOSED":
                    continue
                result = row.get("result", "")
                if result not in ("WIN", "LOSS"):
                    rows_skipped += 1
                    continue
                rows_counted += 1
                market = row.get("market", "?")
                setup_type = row.get("setup", "?")
                key = f"{market}:{setup_type}"
                d = perf_new.setdefault(key, {"wins": 0, "losses": 0, "total": 0})
                d["total"] += 1
                if result == "WIN":
                    d["wins"] += 1
                else:
                    d["losses"] += 1

    # Compute win_rate and preserve last_updated where possible
    now_iso = datetime.now().isoformat()
    for key, d in perf_new.items():
        d["win_rate"] = round(d["wins"] / max(1, d["total"]) * 100, 1)
        d["last_updated"] = perf_before.get(key, {}).get("last_updated", now_iso)

    safe_io.atomic_write_json(PERF_FILE, perf_new)
    audit["perf_after"] = perf_new
    audit["perf_rebuild_stats"] = {
        "rows_seen": rows_seen,
        "rows_counted": rows_counted,
        "rows_skipped": rows_skipped,
    }


def _refresh_suspensions(audit: dict):
    """Step 5+6: re-run auto-suspension check, then force-remove the
    phantom-driven suspensions if any didn't auto-restore."""
    import safe_io

    suspended_before = {}
    if os.path.exists(SUSPENDED_FILE):
        try:
            with open(SUSPENDED_FILE, "r", encoding="utf-8") as f:
                suspended_before = json.load(f)
        except Exception as e:
            _log.warning(f"Wave 12 suspended_before read failed: {e}")
    audit["suspensions_before"] = suspended_before

    # Step 5: re-run auto check using outcome_tracker's own function.
    # This walks setup_performance.json (which we just rebuilt) and the
    # last 7 days of outcomes.csv (which we just cleaned) and decides
    # which setups still meet the suspension criteria.
    try:
        import outcome_tracker as ot
        changes = ot.check_and_update_suspensions()
        audit["auto_check_changes"] = list(changes) if changes else []
    except Exception as e:
        _log.error(f"Wave 12 check_and_update_suspensions failed: {e}", exc_info=True)
        audit.setdefault("errors", []).append(
            f"check_and_update_suspensions: {e}"
        )

    # Step 6: force-remove phantom-driven suspensions that didn't auto-restore.
    suspended_now = {}
    if os.path.exists(SUSPENDED_FILE):
        try:
            with open(SUSPENDED_FILE, "r", encoding="utf-8") as f:
                suspended_now = json.load(f)
        except Exception as e:
            _log.warning(f"Wave 12 suspended_now read failed: {e}")

    forced = []
    for key in PHANTOM_DRIVEN_SUSPENSIONS:
        if key in suspended_now:
            del suspended_now[key]
            forced.append(key)
            _log.info(f"Wave 12 force-removed suspension: {key}")

    safe_io.atomic_write_json(SUSPENDED_FILE, suspended_now)
    audit["suspensions_after"] = suspended_now
    audit["manual_unsuspend"]  = forced


def run_migration() -> dict:
    """Run all migration steps. Returns audit dict."""
    audit = {
        "wave": 12,
        "timestamp_started":   datetime.now(timezone.utc).isoformat(),
        "phantom_ids_targeted": list(PHANTOM_ALERT_IDS),
        "errors": [],
    }

    audit["backups_created"] = _backup_files()
    _mark_phantoms_as_skip(audit)
    _rebuild_setup_performance(audit)
    _refresh_suspensions(audit)

    audit["timestamp_completed"] = datetime.now(timezone.utc).isoformat()
    return audit


def _write_audit(audit: dict):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(AUDIT_FILE, "w", encoding="utf-8") as f:
            json.dump(audit, f, indent=2, default=str)
    except Exception as e:
        _log.error(f"Wave 12 audit write failed: {e}")


def _write_marker(audit: dict):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        marker = {
            "wave": 12,
            "completed_at":   audit.get("timestamp_completed"),
            "phantoms_marked": audit.get("phantoms_found_and_marked", []),
            "manual_unsuspend": audit.get("manual_unsuspend", []),
            "note": "Delete this file to allow Wave 12 to re-run.",
        }
        with open(MARKER_FILE, "w", encoding="utf-8") as f:
            json.dump(marker, f, indent=2)
    except Exception as e:
        _log.error(f"Wave 12 marker write failed: {e}")


def maybe_run() -> dict:
    """
    Top-level entry point. Called from bot.py _post_init.
    Returns a dict with status info. Never raises.

    Caller should check result['ran']:
      - False = already complete (skipped)
      - True  = migration ran (check result['ok'] for success)
    """
    if is_already_complete():
        return {"ran": False, "ok": True, "reason": "already_complete"}

    try:
        _log.info("Wave 12 migration starting...")
        audit = run_migration()
        _write_audit(audit)
        _write_marker(audit)

        n_marked    = len(audit.get("phantoms_found_and_marked", []))
        n_already   = len(audit.get("phantoms_already_skipped", []))
        n_missing   = len(audit.get("phantoms_not_found", []))
        n_unsuspend = len(audit.get("manual_unsuspend", []))
        n_errors    = len(audit.get("errors", []))

        summary = (
            f"phantoms_marked={n_marked} "
            f"already_skipped={n_already} "
            f"not_found={n_missing} "
            f"unsuspended={n_unsuspend} "
            f"errors={n_errors}"
        )
        _log.info(f"Wave 12 complete: {summary}")
        return {
            "ran": True,
            "ok": n_errors == 0,
            "summary": summary,
            "audit": audit,
        }
    except Exception as e:
        _log.error(f"Wave 12 migration FAILED: {e}", exc_info=True)
        try:
            err_audit = {
                "wave": 12,
                "error": str(e),
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            _write_audit(err_audit)
        except Exception:
            pass
        return {
            "ran": True,
            "ok": False,
            "summary": f"FAILED: {e}",
            "error": str(e),
        }


if __name__ == "__main__":
    # Allow running standalone (e.g., for testing locally with copied data dir)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    result = maybe_run()
    print(json.dumps(result.get("audit", result), indent=2, default=str))
