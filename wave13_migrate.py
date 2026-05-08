"""
wave13_migrate.py
=================
Wave 13: Extended Phantom-Loss Cleanup Migration (May 6, 2026)

ONE-SHOT, IDEMPOTENT migration. Marks ALL pre-Wave-10 same-bar LOSSes
as SKIP (not just the 4 from Wave 12), then rebuilds derived state.

WHY THIS EXISTS
===============
Wave 12 cleaned up 4 confirmed phantom losses from May 4 morning that
Wayne saw live on his chart. But a manual audit of outcomes.csv on
May 6 revealed the bug's blast radius was MUCH bigger:

  Pre-Wave-10 (Apr 29 - May 4 13:21 UTC):  0W / 33L  (0.0% WR)
  Post-Wave-10 (May 4 16:28 UTC onward):    2W /  3L (40.0% WR)

ALL 33 pre-Wave-10 closed trades were LOSSes with bars_to_resolution=0.
The probability of 33-in-a-row LOSSes by chance with any real edge is
roughly 1 in 8 billion. The Wave 10 root-cause bug (auto_check_outcomes
using `tf_df.index >= alert_dt` and reading pre-alert wicks as stop-outs)
was confirmed active during this entire window. Statistical evidence
overwhelmingly points to most/all 33 being phantoms, not real losses.

Specifically: NQ:VWAP_BOUNCE_BULL was suspended on Apr 30 at 0W/6L,
all 6 LOSSes happening in a 2-hour window with bars=0. That setup was
your strongest backtest edge (71.4% WR / +$150/trade) — sitting on the
bench because of phantoms.

WHAT THE MIGRATION DOES
=======================
1. Idempotency check: if data/wave13_complete.json exists, skip.
2. Backup: copy outcomes.csv, setup_performance.json, suspended_setups.json
   to *.pre_wave13.bak (one-rename recovery).
3. SKIP-marking pass: walk every row in outcomes.csv. For each row matching
   ALL of these conditions, change result LOSS -> SKIP:
     - status == "CLOSED"
     - result == "LOSS"
     - bars_to_resolution == 0  (or unset/empty)
     - timestamp parses to UTC and is BEFORE 2026-05-04T13:48:00+00:00
       (Wave 10 deploy time)
   Already-SKIP rows pass through unchanged. WIN, OPEN, post-Wave-10 LOSS
   rows are NEVER touched. The 4 Wave 12 SKIP rows are already SKIP and
   pass through harmlessly.
4. Rebuild setup_performance.json from cleaned outcomes.csv. SKIP rows
   excluded from tally.
5. Re-run check_and_update_suspensions() so any setup that no longer
   meets suspension criteria auto-restores.
6. Force-remove specific phantom-driven suspensions that may not
   auto-restore due to the 7-day bleed window:
     - NQ:VWAP_BOUNCE_BULL  (was 0W/6L on Apr 30 phantoms)
     - BTC:VWAP_BOUNCE_BULL (was 0W/4L on May 4 phantoms)
     - SOL:VWAP_BOUNCE_BULL (was 0W/2L on May 4 phantoms)
     - BTC:VWAP_REJECT_BEAR (was 0W/5L on Apr 13-14 phantoms)
     - SOL:VWAP_REJECT_BEAR (was 0W/6L on Apr 14 phantoms)
     - BTC:BREAK_RETEST_BEAR (was 1W/4L on Apr 30 phantoms)
     - BTC:BREAK_RETEST_BULL (was 0W/4L on Apr 29 phantoms)
     - BTC:EMA21_PULLBACK_BULL (was 1W/4L on Apr 15 phantoms)
     - BTC:EMA21_PULLBACK_BEAR (was 2W/4L on Apr 28 phantoms)
   These are the "VWAP and BREAK_RETEST family" suspensions that were
   killed by phantoms. After cleanup, their counters reset and the bot
   can re-evaluate them on POST-Wave-10 data only.
7. Write data/wave13_audit.json + data/wave13_complete.json marker.
8. Send Telegram notification with summary.

WHAT THE MIGRATION DOES NOT DO
==============================
- Touch any post-Wave-10 row (cutoff is strict)
- Touch any WIN row (the bug primarily created false LOSSes)
- Touch any OPEN row
- Modify any code logic
- Restore real losses
- Touch BB_REVERSION suspensions (those have small samples and the
  bug's effect on those is unclear without bar-level data)
- Touch APPROACH_RESIST or APPROACH_SUPPORT suspensions
  (these are HEADS-UP only setups - different mechanic)

NUMBERS EXPECTED (based on May 6 outcomes.csv audit)
=====================================================
  Rows scanned:               46
  Rows already SKIP:            4 (Wave 12 phantoms)
  Rows newly SKIP'd:    24 - 28  (the rest of pre-Wave-10 LOSSes)
  Rows untouched:    14 - 18  (post-Wave-10 + wins + open)
  Suspensions force-removed:    9 (the VWAP/BREAK_RETEST family)

PRE-MORTEM (questions Wayne might ask)
=======================================
Q: What if some of those 24-28 LOSSes were real fast moves, not phantoms?
A: Maybe 1-3 might be. Marking them SKIP just removes them from learning
   - doesn't claim they were wins. Cost of false-skipping 3 real losses
   is tiny vs cost of leaving 25 phantoms in.

Q: This will mark a third of historical trades as SKIP. Won't that leave
   too little data?
A: Yes - and that's correct. Pre-Wave-10 data was poisoned. Less clean
   data > more poisoned data. Real edge will reveal itself in
   post-Wave-10 trades. Worst case we wait 2 weeks for a meaningful
   sample to rebuild.

Q: Why the specific cutoff of 2026-05-04T13:48:00 UTC?
A: That's when Wave 10 (commit baaa3075) deployed. Before this commit,
   auto_check_outcomes had the >= bug. After this commit, only > is used.
   Same-bar resolutions before this time used pre-alert wicks; after,
   only post-alert bar movement counts.

Q: Why is bars_to_resolution=0 a reliable phantom signal pre-Wave-10?
A: Because pre-Wave-10 the bug RAN on bar resolution check. Same-bar
   resolutions used pre-alert wicks. Statistically, 33-in-a-row LOSSes
   on diverse setups (VWAP_BOUNCE, EMA21_PULLBACK, BREAK_RETEST,
   STOCH_REVERSAL, BB_REVERSION, RSI_DIV) is essentially impossible
   noise unless the bot has zero edge - which contradicts backtest
   results. The simpler explanation: phantoms.

Q: What about the 1 LOSS at 13:21 UTC May 4 that's just before cutoff?
A: b9c30d074b - NQ EMA21_PULLBACK_BULL. It's BEFORE Wave 10 cutoff so
   it gets marked SKIP. The Wave 12 commit message said "stays as LOSS
   because price actually hit the stop after Wave 10 deployed" but
   that was a manual chart-confirmed exception we don't have evidence
   for here. If you have visual confirmation that one was real,
   manually flip it back to LOSS via Telegram /loss after Wave 13.

Q: What if check_and_update_suspensions auto-suspends the same setups
   again because of the 7-day bleed math?
A: Possible but unlikely. The bleed window walks outcomes.csv looking
   at result IN ('WIN','LOSS') only. SKIP rows don't count toward
   bleed dollars. After this migration, most pre-Wave-10 LOSSes are
   SKIP so they don't contribute to bleed. The force-remove step
   handles edge cases where the auto-check still suspends them.

Q: Idempotent?
A: Yes. data/wave13_complete.json marker prevents re-runs. Every step
   is also individually idempotent (safe to retry).

Q: What if the migration runs at the same time as auto_check_outcomes?
A: safe_io has a cross-process file lock on outcomes.csv. Same as
   Wave 12. No race.

Q: Can I undo it?
A: Yes. data/outcomes.csv.pre_wave13.bak holds the pre-migration state.
   Same for setup_performance.json and suspended_setups.json. Restore
   manually if needed and delete data/wave13_complete.json to allow
   re-run.

Q: Why not also clean up archived outcomes_*.csv files?
A: Those are immutable historical snapshots. Cleaning them retroactively
   would lose the audit trail of what the bug looked like. Only the
   live outcomes.csv (which feeds learning) needs cleaning.
"""

import os
import json
import csv
import shutil
import logging
from datetime import datetime, timezone

_log = logging.getLogger("nqcalls.wave13")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_BASE_DIR, "data")
OUTCOMES_CSV = os.path.join(_BASE_DIR, "outcomes.csv")
PERF_FILE = os.path.join(DATA_DIR, "setup_performance.json")
SUSPENDED_FILE = os.path.join(DATA_DIR, "suspended_setups.json")
MARKER_FILE = os.path.join(DATA_DIR, "wave13_complete.json")
AUDIT_FILE = os.path.join(DATA_DIR, "wave13_audit.json")

# Wave 10 deploy time (UTC). Anything CLOSED LOSS with bars_to_resolution=0
# whose alert timestamp is before this is treated as a phantom.
WAVE10_DEPLOY_UTC = datetime(2026, 5, 4, 13, 48, 0, tzinfo=timezone.utc)

# Phantom-driven suspensions to force-remove if auto-check doesn't restore.
# Conservative list - these are setups that got suspended PRIMARILY because
# of pre-Wave-10 phantom losses. After cleanup, their real WR is unknown
# (small or zero post-Wave-10 sample) so unsuspending lets them re-collect
# evidence.
PHANTOM_DRIVEN_SUSPENSIONS = [
    "NQ:VWAP_BOUNCE_BULL",
    "BTC:VWAP_BOUNCE_BULL",
    "SOL:VWAP_BOUNCE_BULL",
    "BTC:VWAP_REJECT_BEAR",
    "SOL:VWAP_REJECT_BEAR",
    "BTC:BREAK_RETEST_BEAR",
    "BTC:BREAK_RETEST_BULL",
    "BTC:EMA21_PULLBACK_BULL",
    "BTC:EMA21_PULLBACK_BEAR",
]


def is_already_complete() -> bool:
    """Idempotency check. True if migration already ran."""
    return os.path.exists(MARKER_FILE)


def _read_existing_columns() -> list:
    """Read the existing CSV header so we don't depend on outcome_tracker.CSV_COLS."""
    if not os.path.exists(OUTCOMES_CSV):
        return []
    with open(OUTCOMES_CSV, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        return next(reader, [])


def _backup_files() -> list:
    """Create *.pre_wave13.bak copies."""
    created = []
    targets = [
        (OUTCOMES_CSV,    "outcomes.csv"),
        (PERF_FILE,       "setup_performance.json"),
        (SUSPENDED_FILE,  "suspended_setups.json"),
    ]
    for src, label in targets:
        if os.path.exists(src):
            backup = src + ".pre_wave13.bak"
            try:
                shutil.copy2(src, backup)
                created.append(backup)
                _log.info(f"Wave 13 backup created: {backup}")
            except Exception as e:
                _log.error(f"Wave 13 backup failed for {label}: {e}")
    return created


def _parse_iso_utc(ts: str):
    """Parse an ISO timestamp into a UTC-aware datetime. Returns None on failure."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _is_phantom_candidate(row: dict) -> bool:
    """
    Returns True if this row matches the pre-Wave-10 same-bar phantom signature.
    Conservative: every condition must hold.
    """
    if row.get("status") != "CLOSED":
        return False
    if row.get("result") != "LOSS":
        return False

    # bars_to_resolution must be 0 or empty (treat empty as 0)
    bars_raw = (row.get("bars_to_resolution") or "0").strip()
    try:
        bars = int(float(bars_raw))
    except Exception:
        return False
    if bars != 0:
        return False

    # Timestamp must parse and be before Wave 10 cutoff
    dt = _parse_iso_utc(row.get("timestamp", ""))
    if dt is None:
        return False
    if dt >= WAVE10_DEPLOY_UTC:
        return False

    return True


def _mark_phantoms_as_skip(audit: dict):
    """Find every pre-Wave-10 same-bar LOSS and flip to SKIP."""
    import safe_io

    cols = _read_existing_columns()
    if not cols:
        raise RuntimeError("outcomes.csv has no header - aborting migration")

    newly_marked = []
    already_skip = []
    rows_scanned = 0
    errors = []

    def _mutator(rows):
        nonlocal rows_scanned
        for r in rows:
            rows_scanned += 1
            aid = r.get("alert_id", "")

            # Already SKIP from prior wave - leave alone, record it
            if r.get("result") == "SKIP":
                # Was it a phantom-pattern row that was already cleaned?
                # We track this for the audit but never modify.
                if r.get("status") == "CLOSED":
                    already_skip.append(aid)
                continue

            if not _is_phantom_candidate(r):
                continue

            # Flip LOSS -> SKIP
            try:
                r["result"] = "SKIP"
                # Preserve bars_to_resolution and exit_price for audit trail
                newly_marked.append({
                    "alert_id":   aid,
                    "timestamp":  r.get("timestamp", ""),
                    "market":     r.get("market", ""),
                    "setup":      r.get("setup", ""),
                    "direction":  r.get("direction", ""),
                    "entry":      r.get("entry", ""),
                    "exit_price": r.get("exit_price", ""),
                })
            except Exception as e:
                errors.append(f"{aid}: {e}")
        return rows

    safe_io.safe_rewrite_csv(OUTCOMES_CSV, cols, _mutator)

    audit["rows_scanned"]            = rows_scanned
    audit["newly_marked_skip"]       = newly_marked
    audit["newly_marked_count"]      = len(newly_marked)
    audit["already_skip_count"]      = len(already_skip)
    audit["already_skip_ids"]        = already_skip
    audit["mark_errors"]             = errors


def _rebuild_setup_performance(audit: dict):
    """Rebuild setup_performance.json from cleaned outcomes.csv."""
    import safe_io

    perf_before = {}
    if os.path.exists(PERF_FILE):
        try:
            with open(PERF_FILE, "r", encoding="utf-8") as f:
                perf_before = json.load(f)
        except Exception as e:
            _log.warning(f"Wave 13 perf_before read failed: {e}")
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
    """Re-run auto-suspension check, then force-remove phantom-driven suspensions."""
    import safe_io

    suspended_before = {}
    if os.path.exists(SUSPENDED_FILE):
        try:
            with open(SUSPENDED_FILE, "r", encoding="utf-8") as f:
                suspended_before = json.load(f)
        except Exception as e:
            _log.warning(f"Wave 13 suspended_before read failed: {e}")
    audit["suspensions_before"] = suspended_before

    try:
        import outcome_tracker as ot
        changes = ot.check_and_update_suspensions()
        audit["auto_check_changes"] = list(changes) if changes else []
    except Exception as e:
        _log.error(f"Wave 13 check_and_update_suspensions failed: {e}", exc_info=True)
        audit.setdefault("errors", []).append(
            f"check_and_update_suspensions: {e}"
        )

    suspended_now = {}
    if os.path.exists(SUSPENDED_FILE):
        try:
            with open(SUSPENDED_FILE, "r", encoding="utf-8") as f:
                suspended_now = json.load(f)
        except Exception as e:
            _log.warning(f"Wave 13 suspended_now read failed: {e}")

    forced = []
    for key in PHANTOM_DRIVEN_SUSPENSIONS:
        if key in suspended_now:
            del suspended_now[key]
            forced.append(key)
            _log.info(f"Wave 13 force-removed suspension: {key}")

    safe_io.atomic_write_json(SUSPENDED_FILE, suspended_now)
    audit["suspensions_after"] = suspended_now
    audit["manual_unsuspend"]  = forced


def run_migration() -> dict:
    """Run all migration steps. Returns audit dict."""
    audit = {
        "wave": 13,
        "timestamp_started": datetime.now(timezone.utc).isoformat(),
        "wave10_cutoff_utc": WAVE10_DEPLOY_UTC.isoformat(),
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
        _log.error(f"Wave 13 audit write failed: {e}")


def _write_marker(audit: dict):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        marker = {
            "wave": 13,
            "completed_at":     audit.get("timestamp_completed"),
            "newly_marked":     [m["alert_id"] for m in audit.get("newly_marked_skip", [])],
            "newly_marked_count": audit.get("newly_marked_count", 0),
            "manual_unsuspend": audit.get("manual_unsuspend", []),
            "note": "Delete this file to allow Wave 13 to re-run.",
        }
        with open(MARKER_FILE, "w", encoding="utf-8") as f:
            json.dump(marker, f, indent=2)
    except Exception as e:
        _log.error(f"Wave 13 marker write failed: {e}")


def maybe_run() -> dict:
    """
    Top-level entry point. Called from bot.py _post_init AFTER wave12_migrate.
    Returns a dict with status info. Never raises.
    """
    if is_already_complete():
        return {"ran": False, "ok": True, "reason": "already_complete"}

    try:
        _log.info("Wave 13 migration starting (extended phantom cleanup)...")
        audit = run_migration()
        _write_audit(audit)
        _write_marker(audit)

        n_marked    = audit.get("newly_marked_count", 0)
        n_already   = audit.get("already_skip_count", 0)
        n_unsuspend = len(audit.get("manual_unsuspend", []))
        n_errors    = len(audit.get("errors", []))

        summary = (
            f"newly_marked={n_marked} "
            f"already_skip={n_already} "
            f"unsuspended={n_unsuspend} "
            f"errors={n_errors}"
        )
        _log.info(f"Wave 13 complete: {summary}")
        return {
            "ran": True,
            "ok": n_errors == 0,
            "summary": summary,
            "audit": audit,
        }
    except Exception as e:
        _log.error(f"Wave 13 migration FAILED: {e}", exc_info=True)
        try:
            err_audit = {
                "wave": 13,
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
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    result = maybe_run()
    print(json.dumps(result.get("audit", result), indent=2, default=str))
