"""
migrate_sessions.py - ONE-TIME migration script
=================================================
Run this ONCE before restarting the bot:
    python migrate_sessions.py

What it does:
  1. Reads outcomes.csv
  2. Computes session_id for every row from its timestamp
  3. Writes updated outcomes.csv with session_id column
  4. Archives each old session to data/archive/outcomes_YYYY-MM-DD.csv
  5. Archives contaminated sim_account.json
  6. Prints summary
"""

import csv
import json
import os
import shutil
from datetime import datetime, timezone, timedelta

# We need session_clock to be importable
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from session_clock import get_session_date, session_date_from_timestamp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTCOMES_CSV = os.path.join(BASE_DIR, "outcomes.csv")
SIM_FILE = os.path.join(BASE_DIR, "data", "sim_account.json")
ARCHIVE_DIR = os.path.join(BASE_DIR, "data", "archive")

CSV_COLS = [
    "alert_id","timestamp","market","tf","setup","direction",
    "entry","stop","target","rr","method",
    "trend_score","conviction","tier","leverage","suggested_hold",
    "rsi","atr","adx","htf_bias","hour","vol_ratio","news_flag",
    "status","result","bars_to_resolution","exit_price","last_rescore_conviction",
    "partial_exit_done","session_id"
]


def main():
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    # ── Step 1: Archive contaminated sim_account.json ─────────────
    if os.path.exists(SIM_FILE):
        archive_sim = os.path.join(ARCHIVE_DIR, "sim_contaminated_pre_migration.json")
        shutil.copy2(SIM_FILE, archive_sim)
        print(f"  Archived contaminated sim -> {archive_sim}")

    # ── Step 2: Read and enrich outcomes.csv ──────────────────────
    if not os.path.exists(OUTCOMES_CSV):
        print("No outcomes.csv found. Nothing to migrate.")
        return

    with open(OUTCOMES_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    print(f"  Read {len(rows)} rows from outcomes.csv")

    # Add session_id to every row
    for r in rows:
        ts = r.get("timestamp", "")
        if ts:
            r["session_id"] = session_date_from_timestamp(ts)
        else:
            r["session_id"] = ""

    # ── Step 3: Write updated outcomes.csv ────────────────────────
    with open(OUTCOMES_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_COLS})

    print(f"  Updated outcomes.csv with session_id column")

    # ── Step 4: Archive old sessions ─────────────────────────────
    current_session = get_session_date()
    by_session: dict[str, list] = {}
    for r in rows:
        sid = r.get("session_id", "")
        if sid:
            by_session.setdefault(sid, []).append(r)

    archived_files = []
    for sid, session_rows in sorted(by_session.items()):
        if sid == current_session:
            continue
        archive_path = os.path.join(ARCHIVE_DIR, f"outcomes_{sid}.csv")
        with open(archive_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLS)
            w.writeheader()
            for r in session_rows:
                w.writerow({k: r.get(k, "") for k in CSV_COLS})
        archived_files.append(archive_path)

    # ── Step 5: Trim live file ───────────────────────────────────
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    keep = []
    for r in rows:
        if r.get("status") == "OPEN":
            keep.append(r)
        elif r.get("session_id") == current_session:
            keep.append(r)
        elif r.get("session_id", "") >= cutoff:
            keep.append(r)

    with open(OUTCOMES_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        w.writeheader()
        for r in keep:
            w.writerow({k: r.get(k, "") for k in CSV_COLS})

    # ── Summary ──────────────────────────────────────────────────
    print(f"\n  Migrated {len(rows)} rows across {len(by_session)} sessions.")
    print(f"  Current session: {current_session} ({len(by_session.get(current_session, []))} rows)")
    print(f"  Live file trimmed to {len(keep)} rows")
    if archived_files:
        print(f"  Archive files created:")
        for f in archived_files:
            print(f"    {f}")
    else:
        print(f"  No archive files needed (all rows are current session)")
    print(f"\n  Migration complete! Safe to restart the bot.")


if __name__ == "__main__":
    print("NQ CALLS — Session Migration")
    print("=" * 40)
    main()
