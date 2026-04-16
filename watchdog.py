"""
watchdog.py - NQ CALLS Auto-Restart Guardian
=============================================
Run THIS instead of bot.py directly.
  python watchdog.py

What it does:
  - Starts bot.py as a subprocess
  - If the bot crashes, waits 15 seconds and restarts it
  - Sends a Telegram message on every crash + restart
  - Keeps a crash log at data/crash_log.txt
  - Prevents restart loops (if bot crashes 5x in 10 min, stops and alerts you)
  - Prevents sleep on Windows while running

Usage:
  Double-click START_WATCHDOG.bat  (created by this script on first run)
  Or: python watchdog.py
"""

import subprocess
import sys
import os
import time
import json
import requests
from datetime import datetime, timezone
from collections import deque
from pathlib import Path

BASE_DIR = Path(__file__).parent
CRASH_LOG = BASE_DIR / "data" / "crash_log.txt"
CONFIG_FILE = BASE_DIR / "config.py"

# ── Read Telegram config ──────────────────────────────────────────
def _get_telegram_config():
    """Pull token and chat_id from env vars first, then fall back to config.py."""
    import os as _os
    token = _os.environ.get("TELEGRAM_TOKEN")
    chat_id_raw = _os.environ.get("CHAT_ID")
    if token and chat_id_raw:
        try:
            return token, int(chat_id_raw)
        except ValueError:
            return token, chat_id_raw

    # Fallback: read from config.py
    try:
        with open(CONFIG_FILE) as f:
            for line in f:
                if "TELEGRAM_TOKEN" in line and "=" in line:
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                if "CHAT_ID" in line and "=" in line:
                    raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                    try:
                        chat_id_raw = int(raw)
                    except ValueError:
                        chat_id_raw = raw
    except Exception:
        pass
    return token, chat_id_raw

TELEGRAM_TOKEN, CHAT_ID = _get_telegram_config()

def _tg(msg: str):
    """Send a Telegram message. Never raises — watchdog must keep running."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception:
        pass

def _log(msg: str):
    """Write to crash log and print to console."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        CRASH_LOG.parent.mkdir(exist_ok=True)
        with open(CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ── Windows sleep prevention ──────────────────────────────────────
def _prevent_sleep():
    """Tell Windows not to sleep while watchdog is running."""
    try:
        import ctypes
        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000003)
    except Exception:
        pass

def _allow_sleep():
    """Re-enable Windows sleep."""
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    except Exception:
        pass

# ── Create the .bat launcher ──────────────────────────────────────
def _create_bat():
    bat_path = BASE_DIR / "START_WATCHDOG.bat"
    if not bat_path.exists():
        content = f'@echo off\ntitle NQ CALLS Watchdog\ncd /d "{BASE_DIR}"\npython watchdog.py\npause\n'
        bat_path.write_text(content)
        print(f"Created {bat_path}")

# ── Main watchdog loop ────────────────────────────────────────────
def run():
    _create_bat()
    _prevent_sleep()

    bot_script = str(BASE_DIR / "bot.py")
    python_exe = sys.executable

    # Crash rate limiter: if 5 crashes within 600 seconds, stop
    MAX_CRASHES = 5
    CRASH_WINDOW = 600  # seconds
    crash_times: deque = deque()

    restart_count = 0
    _log("Watchdog started — launching bot.py")
    _tg("🐕 *NQ CALLS Watchdog started*\nBot launching now. I'll restart it automatically if anything crashes.")

    while True:
        start_time = time.time()

        try:
            proc = subprocess.Popen(
                [python_exe, bot_script],
                cwd=str(BASE_DIR),
            )
            _log(f"Bot started (PID {proc.pid})")
            exit_code = proc.wait()  # blocks until bot exits
        except Exception as e:
            _log(f"Failed to start bot: {e}")
            exit_code = -1

        uptime = time.time() - start_time
        now = time.time()
        crash_times.append(now)

        # Remove crashes outside the window
        while crash_times and (now - crash_times[0]) > CRASH_WINDOW:
            crash_times.popleft()

        restart_count += 1
        uptime_str = f"{int(uptime//60)}m {int(uptime%60)}s"

        _log(f"Bot exited (code {exit_code}) after {uptime_str}. Crash #{restart_count}.")

        # Check crash rate
        if len(crash_times) >= MAX_CRASHES:
            msg = (
                f"🚨 *NQ CALLS — Watchdog STOPPED*\n"
                f"Bot crashed {MAX_CRASHES} times in {CRASH_WINDOW//60} minutes.\n"
                f"Last exit code: `{exit_code}`\n"
                f"Watchdog is pausing — manual restart needed.\n"
                f"Check `data/crash_log.txt` for details."
            )
            _log("Too many crashes — watchdog stopping. Manual intervention required.")
            _tg(msg)
            _allow_sleep()
            break

        # Normal single crash — notify and restart
        if exit_code != 0:
            msg = (
                f"⚠️ *NQ CALLS Bot crashed* (exit code `{exit_code}`)\n"
                f"Uptime was {uptime_str}\n"
                f"Restarting in 15 seconds... (attempt #{restart_count})"
            )
        else:
            msg = (
                f"ℹ️ *NQ CALLS Bot stopped cleanly*\n"
                f"Uptime was {uptime_str}\n"
                f"Restarting in 15 seconds..."
            )

        _log(msg.replace("*", "").replace("`", ""))
        _tg(msg)
        time.sleep(15)
        _log("Restarting bot now...")


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        _log("Watchdog stopped by user (Ctrl+C)")
        _tg("🛑 *NQ CALLS Watchdog stopped* — manual shutdown.")
        _allow_sleep()
