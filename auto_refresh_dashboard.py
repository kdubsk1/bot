"""
auto_refresh_dashboard.py
=========================
Local loop that re-runs generate_dashboard.py every 5 minutes.

WHY: dashboard.html is static. To see fresh PnL data, the script needs to
regenerate it. This loop does that for you so you can leave it running and
the dashboard always shows the latest sim/trade data.

USAGE:
  Option A — quick start: double-click AUTO_REFRESH_DASHBOARD.bat
  Option B — terminal: python auto_refresh_dashboard.py
  Option C — Windows Task Scheduler: schedule this script to run at startup

The browser tab showing dashboard.html will auto-reload itself every 60s
(meta refresh tag), so as long as this script is running, the dashboard
stays current.

Press Ctrl+C to stop.
"""
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REFRESH_INTERVAL_SECONDS = 300  # 5 minutes
SCRIPT_DIR = Path(__file__).parent
GENERATOR = SCRIPT_DIR / "generate_dashboard.py"


def generate():
    """Run generate_dashboard.py and capture output."""
    try:
        result = subprocess.run(
            [sys.executable, str(GENERATOR)],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(SCRIPT_DIR),
        )
        if result.returncode == 0:
            return True, result.stdout
        return False, result.stderr or result.stdout
    except subprocess.TimeoutExpired:
        return False, "Timed out after 60s"
    except Exception as e:
        return False, str(e)


def main():
    print("=" * 60)
    print("  NQ CALLS Dashboard Auto-Refresh Loop")
    print("=" * 60)
    print(f"  Refresh interval: {REFRESH_INTERVAL_SECONDS}s ({REFRESH_INTERVAL_SECONDS // 60}min)")
    print(f"  Generator script: {GENERATOR}")
    print(f"  Press Ctrl+C to stop")
    print("=" * 60)
    print()

    if not GENERATOR.exists():
        print(f"ERROR: {GENERATOR} not found.")
        sys.exit(1)

    iteration = 0
    while True:
        iteration += 1
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] Refresh #{iteration}...", end=" ", flush=True)

        ok, msg = generate()
        if ok:
            # Pull just the last line of useful output
            lines = [l for l in msg.split("\n") if l.strip()]
            last = lines[-1] if lines else "OK"
            print("✓")
        else:
            print(f"✗  {msg[:100]}")

        # Sleep with periodic countdown so user can see it's alive
        for remaining in range(REFRESH_INTERVAL_SECONDS, 0, -30):
            time.sleep(min(30, remaining))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nStopped by user. Dashboard will not refresh until you start me again.")
