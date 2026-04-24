"""
auto_sync.py - NQ CALLS 2026
=============================
Periodically commits and pushes data/ + outcomes.csv to GitHub so that
Railway runtime data survives restarts.

THE PROBLEM
-----------
Railway's filesystem is ephemeral. Every restart (redeploy, crash, Railway
platform event, memory pressure) wipes /app/data/ and /app/outcomes.csv.
Before this module existed, the bot would lose all runtime state on every
restart: cooldowns, scan decisions, outcome results, self-learning data,
suspended setups — all reset to git-clone baseline.

HOW IT WORKS
------------
  - On first sync, configure git identity and set authenticated remote URL.
  - Every 6 hours, run:
        git add -A -- data/ outcomes.csv
        git commit -m "Auto-sync (periodic): YYYY-MM-DD HH:MM UTC [N files]"
        git pull --rebase -X ours origin main   (handle upstream changes)
        git push origin main
  - /sync Telegram command triggers an immediate manual sync.
  - On sync failure, a loud Telegram warning is sent so the user notices.

SECURITY
--------
  - GITHUB_TOKEN is read once from env var and never logged.
  - Any subprocess stdout/stderr is passed through _redact() before logging.
  - Uses fine-grained PAT with Contents: Read/Write scoped to kdubsk1/bot only.

NO-OP BEHAVIOR
--------------
  - If GITHUB_TOKEN is missing, periodic sync logs a warning and exits its
    loop cleanly. Bot continues running; only sync is disabled.
  - If git is not installed (shouldn't happen on Railway's buildpack), same.
  - If there are no changes to sync, commit/push is skipped (no empty commits).
"""
import asyncio
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple, Callable

log = logging.getLogger("auto_sync")

# ── Configuration ────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
SYNC_INTERVAL_SECONDS = 6 * 60 * 60   # 6 hours
INITIAL_DELAY_SECONDS = 5 * 60         # wait 5 min after startup before first sync
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
REPO_OWNER = "kdubsk1"
REPO_NAME = "bot"
BRANCH = "main"
GIT_USER_EMAIL = "bot@nqcalls.local"
GIT_USER_NAME = "NQ CALLS Bot"

# Paths (relative to BASE_DIR) that we sync. Everything else is left alone.
SYNC_PATHS = ["data/", "outcomes.csv"]

# ── Runtime state (for /status and startup banner) ───────────────
_configured: bool = False
_last_sync_time: Optional[datetime] = None
_last_sync_status: str = "never"
_last_sync_commit: str = ""
_last_sync_error: str = ""
_last_sync_files: int = 0


# ── Internal helpers ─────────────────────────────────────────────
def _redact(text: str) -> str:
    """Remove the token from any text so it never appears in logs."""
    if not text:
        return ""
    if GITHUB_TOKEN and GITHUB_TOKEN in text:
        text = text.replace(GITHUB_TOKEN, "***TOKEN_REDACTED***")
    # Also redact anything that looks like a PAT
    import re
    text = re.sub(r"github_pat_[A-Za-z0-9_]{20,}", "***PAT_REDACTED***", text)
    return text


def _run_git(args: list, timeout: int = 60) -> Tuple[int, str, str]:
    """Run a git command in BASE_DIR. Returns (returncode, stdout, stderr). Redacts token."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return (result.returncode, _redact(result.stdout), _redact(result.stderr))
    except subprocess.TimeoutExpired:
        return (124, "", f"git {' '.join(args)} timed out after {timeout}s")
    except FileNotFoundError:
        return (127, "", "git command not found (not in PATH)")
    except Exception as e:
        return (1, "", f"git error: {_redact(str(e))}")


def _configure_git_once() -> bool:
    """First-run configuration: identity + authenticated remote URL. Idempotent."""
    global _configured
    if _configured:
        return True

    if not GITHUB_TOKEN:
        log.warning("auto_sync: GITHUB_TOKEN env var not set — sync will be DISABLED")
        return False

    # Check git is available
    rc, _, err = _run_git(["--version"], timeout=10)
    if rc != 0:
        log.error(f"auto_sync: git not available: {err}")
        return False

    # Identity (per-repo, doesn't affect anything outside this repo)
    _run_git(["config", "user.email", GIT_USER_EMAIL])
    _run_git(["config", "user.name", GIT_USER_NAME])

    # Authenticated remote URL. 'x-access-token' is the recommended username
    # for fine-grained PATs over HTTPS per GitHub docs.
    remote_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{REPO_OWNER}/{REPO_NAME}.git"
    rc, _, err = _run_git(["remote", "set-url", "origin", remote_url])
    if rc != 0:
        log.error(f"auto_sync: failed to set remote URL: {err}")
        return False

    # Verify by fetching remote ref list (should succeed with valid token)
    rc, _, err = _run_git(["ls-remote", "--heads", "origin"], timeout=30)
    if rc != 0:
        log.error(f"auto_sync: remote auth test failed: {err[:200]}")
        return False

    _configured = True
    log.info("auto_sync: git configured (identity + authenticated remote verified)")
    return True


def _do_sync_sync(label: str) -> dict:
    """
    Synchronous sync logic. Called from a thread via asyncio.to_thread.
    Returns {ok, message, commit_sha, files_changed}.
    """
    if not _configure_git_once():
        return {"ok": False, "message": "git not configured (token missing or invalid)",
                "commit_sha": "", "files_changed": 0}

    # Check for changes in our watched paths
    status_args = ["status", "--porcelain", "--"] + SYNC_PATHS
    rc, porcelain, err = _run_git(status_args)
    if rc != 0:
        return {"ok": False, "message": f"git status failed: {err[:200]}",
                "commit_sha": "", "files_changed": 0}

    changed_lines = [l for l in porcelain.splitlines() if l.strip()]
    files_changed = len(changed_lines)

    if files_changed == 0:
        return {"ok": True, "message": "no changes to sync",
                "commit_sha": "", "files_changed": 0}

    # Stage
    add_args = ["add", "-A", "--"] + SYNC_PATHS
    rc, _, err = _run_git(add_args)
    if rc != 0:
        return {"ok": False, "message": f"git add failed: {err[:200]}",
                "commit_sha": "", "files_changed": files_changed}

    # Commit
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    commit_msg = f"Auto-sync ({label}): {ts} [{files_changed} files]"
    rc, _, err = _run_git(["commit", "-m", commit_msg])
    if rc != 0:
        # If .gitignore filtered everything out, there's nothing to commit
        if "nothing to commit" in err.lower() or "nothing to commit" in _redact(err).lower():
            return {"ok": True, "message": "nothing to commit after staging",
                    "commit_sha": "", "files_changed": 0}
        return {"ok": False, "message": f"git commit failed: {err[:200]}",
                "commit_sha": "", "files_changed": files_changed}

    # Capture the commit SHA we just made
    rc, sha_out, _ = _run_git(["rev-parse", "HEAD"])
    commit_sha = sha_out.strip()[:7] if rc == 0 else ""

    # Pull with rebase in case remote moved ahead (user pushed from desktop
    # while bot was running). Use 'ours' merge strategy for any conflicts —
    # Railway runtime data is newer than anything pushed from desktop.
    rc, _, err = _run_git(["pull", "--rebase", "-X", "ours", "origin", BRANCH],
                          timeout=120)
    if rc != 0:
        log.warning(f"auto_sync: pull --rebase failed: {err[:200]} — aborting rebase and trying plain push")
        _run_git(["rebase", "--abort"])  # best-effort; OK if no rebase in progress

    # Push
    rc, _, err = _run_git(["push", "origin", BRANCH], timeout=120)
    if rc != 0:
        # Common causes: rejected for non-fast-forward, auth issue, rate limit
        return {"ok": False, "message": f"git push failed: {err[:200]}",
                "commit_sha": commit_sha, "files_changed": files_changed}

    return {
        "ok": True,
        "message": f"pushed {files_changed} files",
        "commit_sha": commit_sha,
        "files_changed": files_changed,
    }


async def _do_sync(label: str = "periodic") -> dict:
    """Async wrapper. Runs sync in a thread pool so it doesn't block the event loop."""
    global _last_sync_time, _last_sync_status, _last_sync_commit, _last_sync_error, _last_sync_files

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _do_sync_sync, label)
    except Exception as e:
        result = {"ok": False, "message": f"exception: {_redact(str(e))}",
                  "commit_sha": "", "files_changed": 0}

    _last_sync_time = datetime.now(timezone.utc)
    _last_sync_status = "ok" if result["ok"] else "failed"
    _last_sync_commit = result.get("commit_sha", "")
    _last_sync_error = "" if result["ok"] else result.get("message", "")
    _last_sync_files = result.get("files_changed", 0)

    return result


# ── Public API ───────────────────────────────────────────────────
async def manual_sync() -> str:
    """
    Triggered by /sync Telegram command. Returns a user-friendly message.
    """
    log.info("auto_sync: manual sync requested")
    r = await _do_sync(label="manual")
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    if r["ok"]:
        if r["files_changed"] == 0:
            return f"🟢 *Manual sync @ {ts}*\n  No changes to sync (everything up to date)"
        return (
            f"🔄 *Manual sync @ {ts}*\n"
            f"  Files: `{r['files_changed']}` changed\n"
            f"  Commit: `{r['commit_sha'] or 'n/a'}`\n"
            f"  {r['message']}"
        )
    return (
        f"⚠️ *Manual sync FAILED @ {ts}*\n"
        f"  Error: {r['message']}\n"
        f"  Data may be lost on next Railway restart. Check logs."
    )


async def periodic_sync_loop(telegram_send: Optional[Callable] = None):
    """
    Background task: sync every SYNC_INTERVAL_SECONDS.
    First sync happens after INITIAL_DELAY_SECONDS so the bot can stabilize first.

    Args:
        telegram_send: optional async callable(text) to notify Telegram on events.
                       If None, events are only logged.
    """
    log.info(
        f"auto_sync: periodic_sync_loop started "
        f"(interval: {SYNC_INTERVAL_SECONDS/3600:.1f}h, "
        f"initial delay: {INITIAL_DELAY_SECONDS}s)"
    )

    if not GITHUB_TOKEN:
        log.warning("auto_sync: GITHUB_TOKEN not set — periodic sync DISABLED")
        if telegram_send:
            try:
                await telegram_send(
                    "⚠️ *Auto-Sync DISABLED*\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "GITHUB_TOKEN env var not set on Railway.\n"
                    "Runtime data will NOT persist across restarts.\n"
                    "Add the token and redeploy."
                )
            except Exception:
                pass
        return

    # Initial delay to let bot finish starting up before first sync
    await asyncio.sleep(INITIAL_DELAY_SECONDS)

    while True:
        try:
            r = await _do_sync(label="periodic")
            ts = datetime.now(timezone.utc).strftime("%H:%M UTC")

            if r["ok"] and r["files_changed"] > 0:
                log.info(
                    f"auto_sync: periodic sync OK — {r['files_changed']} files, "
                    f"commit {r['commit_sha']}"
                )
                if telegram_send:
                    try:
                        await telegram_send(
                            f"🔄 *Auto-sync @ {ts}*\n"
                            f"  Files: `{r['files_changed']}` changed\n"
                            f"  Commit: `{r['commit_sha']}`"
                        )
                    except Exception as e:
                        log.warning(f"auto_sync: telegram send failed: {e}")
            elif r["ok"]:
                log.info("auto_sync: periodic sync — no changes to commit")
            else:
                log.error(f"auto_sync: periodic sync FAILED: {r['message']}")
                if telegram_send:
                    try:
                        await telegram_send(
                            f"⚠️ *Auto-sync FAILED @ {ts}*\n"
                            f"  Error: {r['message'][:200]}\n"
                            f"  Data may be lost on next restart.\n"
                            f"  Try /sync manually."
                        )
                    except Exception:
                        pass
        except Exception as e:
            log.error(f"auto_sync: loop iteration exception: {_redact(str(e))}")

        await asyncio.sleep(SYNC_INTERVAL_SECONDS)


def status() -> str:
    """Return a human-readable auto-sync status string for startup banner / /status."""
    if not GITHUB_TOKEN:
        return "⚠️ Auto-sync DISABLED (GITHUB_TOKEN not set)"
    if _last_sync_time is None:
        return "Auto-sync: waiting for first cycle"
    age_s = (datetime.now(timezone.utc) - _last_sync_time).total_seconds()
    if age_s < 60:
        age_str = f"{int(age_s)}s ago"
    elif age_s < 3600:
        age_str = f"{int(age_s/60)}m ago"
    else:
        age_str = f"{age_s/3600:.1f}h ago"
    icon = "✅" if _last_sync_status == "ok" else "❌"
    msg = f"{icon} Auto-sync {_last_sync_status} ({age_str}, {_last_sync_files} files)"
    if _last_sync_commit:
        msg += f" | commit {_last_sync_commit}"
    if _last_sync_error:
        msg += f" | err: {_last_sync_error[:80]}"
    return msg
