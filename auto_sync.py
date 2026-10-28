"""
auto_sync.py - NQ CALLS 2026 — GitHub API VERSION
==================================================
Periodically commits and pushes data/ + outcomes.csv to GitHub via the
GitHub REST API (no git CLI required) so Railway runtime data survives restarts.

WHY API INSTEAD OF GIT CLI?
---------------------------
Railway's Python buildpack does NOT include git. subprocess.run(["git", ...])
returns FileNotFoundError. So we use api.github.com directly with our PAT.

THE PROBLEM WE SOLVE
--------------------
Railway's filesystem is ephemeral. Every restart wipes /app/data/ and
/app/outcomes.csv. Without persistence, the bot loses all runtime state on
restart: cooldowns, scan decisions, outcome results, self-learning state.

HOW IT WORKS
------------
  - Every 6 hours (after a 5-min initial delay), walk SYNC_PATHS, compute
    SHA-1 hash for each file, compare against current GitHub SHAs from a
    single tree fetch, build a list of changed files.
  - If anything changed: create blobs for each via /git/blobs, build a new
    tree via /git/trees, create a commit via /git/commits, update the
    main ref via /git/refs/heads/main.
  - All in a single atomic commit.

  - /sync Telegram command triggers an immediate manual sync.
  - On sync failure, a loud Telegram warning fires.

SECURITY
--------
  - GITHUB_TOKEN read once from env var, never logged.
  - Errors from API calls are sanitized through _redact() before logging.
  - Uses fine-grained PAT with Contents: Read/Write scoped to kdubsk1/bot only.

NO-OP BEHAVIOR
--------------
  - If GITHUB_TOKEN missing, periodic loop exits cleanly. Bot keeps running.
  - If a file is unreadable, it's skipped (logged warning).
  - If no changes, no API calls beyond the tree-fetch. No empty commits.
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple, Callable, List, Dict

log = logging.getLogger("auto_sync")

# Wave 101 (_WAVE101_LOUD_SYNC_SKIP): files skipped for exceeding the sync
# size cap are recorded here so periodic_sync_loop can fire a LOUD Telegram
# alert. Previously an oversized file was skipped with only a log.warning
# (silent), so a file could stop syncing and lose its data on the next
# Railway restart with no notice -- this is what cost ~12 days of
# strategy_log data. Now Wayne is told the moment any file stops syncing.
_SKIPPED_LARGE = []

# ── Configuration ────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
SYNC_INTERVAL_SECONDS = 6 * 60 * 60   # 6 hours
INITIAL_DELAY_SECONDS = 5 * 60         # 5 min after startup before first sync
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
REPO_OWNER = "kdubsk1"
REPO_NAME = "bot"
BRANCH = "main"
COMMITTER_NAME = "NQ CALLS Bot"
COMMITTER_EMAIL = "bot@nqcalls.local"

# Paths (relative to BASE_DIR) we sync. Files OR directories. Directories
# are walked recursively. Anything not under these paths is left alone.
# Apr 30 LATE: also push docs/dashboard.html so GitHub Pages stays live.
# Regenerated at the top of _do_sync_sync (see _regenerate_dashboard).
SYNC_PATHS = ["data", "outcomes.csv", "docs/dashboard.html"]

API_BASE = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}"

# ── Runtime state ─────────────────────────────────────────────────
_last_sync_time: Optional[datetime] = None
_last_sync_status: str = "never"
_last_sync_commit: str = ""
_last_sync_error: str = ""
_last_sync_files: int = 0


# ── Internal helpers ─────────────────────────────────────────────
def _redact(text: str) -> str:
    """Strip the token from any text so it never appears in logs."""
    if not text:
        return ""
    if GITHUB_TOKEN and GITHUB_TOKEN in text:
        text = text.replace(GITHUB_TOKEN, "***TOKEN_REDACTED***")
    text = re.sub(r"github_pat_[A-Za-z0-9_]{20,}", "***PAT_REDACTED***", text)
    return text


def _api_request(method: str, path: str, body: Optional[dict] = None,
                 timeout: int = 30) -> Tuple[int, dict]:
    """
    Call api.github.com. Returns (status_code, parsed_json_or_error_dict).
    Uses urllib (no requests dep). Raises nothing — wraps errors in dict.
    """
    if not GITHUB_TOKEN:
        return (0, {"error": "GITHUB_TOKEN not set"})

    url = path if path.startswith("http") else f"{API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "nqcalls-bot/1.0",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return (resp.status, json.loads(raw) if raw else {})
            except json.JSONDecodeError:
                return (resp.status, {"_raw": raw})
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
            try:
                return (e.code, json.loads(err_body))
            except json.JSONDecodeError:
                return (e.code, {"error": err_body})
        except Exception:
            return (e.code, {"error": str(e)})
    except urllib.error.URLError as e:
        return (0, {"error": f"URL error: {_redact(str(e))}"})
    except Exception as e:
        return (0, {"error": f"request exception: {_redact(str(e))}"})


def _git_blob_sha(content_bytes: bytes) -> str:
    """
    Compute git blob SHA-1: sha1('blob ' + len + '\\0' + content).
    This matches what GitHub stores so we can compare without re-uploading.
    """
    header = f"blob {len(content_bytes)}\0".encode("utf-8")
    return hashlib.sha1(header + content_bytes).hexdigest()


# ---- _WAVE241_SYNC_SKIP -------------------------------------------
# Wave 241 (Wayne, Q6, 15 Sep 2026): never sync these. data/archive still syncs.
W241_SKIP_SUFFIXES = (".bak", ".lock")
W241_SKIP_NAMES = ("sim_account.json",)
W241_SKIP_DIR_PREFIXES = ("_backup_pre_pull_",)
# _WAVE247_LOG_ROTATION: a 2.36 MB one-off import sitting in data/, not bot data. Re-hashed on every sync.
W247_SKIP_FILE_PREFIXES = ("trench-radar-",)


def _w241_skip(path: Path) -> bool:
    """True for a file the sync must leave alone: *.bak, *.lock, sim_account.json,
    or anything under a _backup_pre_pull_* folder. Never raises."""
    try:
        name = path.name
        if name.endswith(W241_SKIP_SUFFIXES) or name in W241_SKIP_NAMES:
            return True
        if name.startswith(W247_SKIP_FILE_PREFIXES):  # _WAVE247_LOG_ROTATION
            return True
        return any(part.startswith(W241_SKIP_DIR_PREFIXES) for part in path.parts[:-1])
    except Exception:
        return False
# ---- end _WAVE241_SYNC_SKIP ---------------------------------------


def _walk_sync_paths() -> List[Path]:
    """
    Walk SYNC_PATHS and return a list of all regular file Paths to sync.
    Skips dot-files, __pycache__, and anything > 5MB (GitHub blob limit is
    100MB but our state files should be tiny — anything bigger is a bug).
    """
    files: List[Path] = []
    for raw in SYNC_PATHS:
        p = BASE_DIR / raw
        if not p.exists():
            continue
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            for child in p.rglob("*"):
                if not child.is_file():
                    continue
                if any(part.startswith(".") for part in child.relative_to(BASE_DIR).parts):
                    continue
                if "__pycache__" in child.parts:
                    continue
                if _w241_skip(child.relative_to(BASE_DIR)):  # _WAVE241_SYNC_SKIP
                    continue
                try:
                    if child.stat().st_size > 25 * 1024 * 1024:  # Wave 59: raised from 5MB so strategy_log keeps backing up to GitHub
                        log.error(f"auto_sync: SKIPPING large file {child} (>25MB) - NOT backed up!")
                        try:
                            _mb = child.stat().st_size / (1024.0 * 1024.0)
                            _rec = "%s (%.1f MB)" % (child.name, _mb)
                            if _rec not in _SKIPPED_LARGE:
                                _SKIPPED_LARGE.append(_rec)
                        except Exception:
                            pass
                        continue
                except OSError:
                    continue
                files.append(child)
    return files


def _regenerate_dashboard():
    """
    Apr 30 LATE: regenerate dashboard.html (and docs/dashboard.html for
    GitHub Pages) before each sync. This is what makes the live URL stay
    fresh — every 6 hours when auto_sync runs, the dashboard gets
    regenerated from the latest data files and pushed to GitHub.

    Runs in a try/except: dashboard regen is a NICE-TO-HAVE, not critical.
    If it fails (e.g., import error), we log and keep going — the data
    sync itself still works.
    """
    try:
        import generate_dashboard
        generate_dashboard.main()
        log.info("auto_sync: dashboard regenerated for live URL")
    except Exception as e:
        log.warning(f"auto_sync: dashboard regen failed (non-fatal): {_redact(str(e))}")


def _w265_do_sync_unlocked(label: str) -> dict:  # _WAVE265_LEDGER_AND_RESTORE: _do_sync_sync (below) takes the lock
    """
    Synchronous sync logic. Called from a thread via run_in_executor.
    Returns {ok, message, commit_sha, files_changed}.

    Algorithm:
      0. (NEW Apr 30 LATE) Regenerate dashboard.html so docs/dashboard.html
         contains the freshest data when we sync.
      1. GET /git/refs/heads/main → base commit SHA
      2. GET /git/commits/{sha} → base tree SHA
      3. Walk SYNC_PATHS, compute local blob SHA for each file
      4. GET /git/trees/{base_tree}?recursive=1 → remote tree
      5. Diff: for each local file, if SHA differs from remote, upload blob via
         POST /git/blobs (or use inline base64 in tree call)
      6. POST /git/trees with the changed tree entries (base_tree=base_tree)
      7. POST /git/commits with parent=base_commit, tree=new_tree
      8. PATCH /git/refs/heads/main with new commit SHA
    """
    if not GITHUB_TOKEN:
        return {"ok": False, "message": "GITHUB_TOKEN not set",
                "commit_sha": "", "files_changed": 0}

    # Step 0: refresh the dashboard before we walk paths so the latest
    # version of docs/dashboard.html is included in this sync.
    _regenerate_dashboard()

    # Step 1: get current ref (base commit SHA)
    s, r = _api_request("GET", f"/git/refs/heads/{BRANCH}")
    if s != 200 or "object" not in r:
        return {"ok": False, "message": f"failed to get branch ref: {s} {str(r)[:200]}",
                "commit_sha": "", "files_changed": 0}
    base_commit_sha = r["object"]["sha"]

    # Step 2: get base commit (its tree SHA)
    s, r = _api_request("GET", f"/git/commits/{base_commit_sha}")
    if s != 200 or "tree" not in r:
        return {"ok": False, "message": f"failed to get base commit: {s} {str(r)[:200]}",
                "commit_sha": "", "files_changed": 0}
    base_tree_sha = r["tree"]["sha"]

    # Step 3: walk local files, compute their blob SHAs
    local_files = _walk_sync_paths()
    local_map: Dict[str, Tuple[Path, str, bytes]] = {}  # path-in-repo → (Path, blob_sha, content)
    for fp in local_files:
        try:
            content = fp.read_bytes()
        except Exception as e:
            log.warning(f"auto_sync: cannot read {fp}: {_redact(str(e))}")
            continue
        repo_path = str(fp.relative_to(BASE_DIR)).replace("\\", "/")
        local_map[repo_path] = (fp, _git_blob_sha(content), content)
    _w284_trim_for_commit(local_map)   # _WAVE284_TAIL_PUSH: the tail in this commit = rows newer than its scan log

    # Step 4: get the recursive tree from GitHub
    s, r = _api_request("GET", f"/git/trees/{base_tree_sha}?recursive=1")
    if s != 200:
        return {"ok": False, "message": f"failed to get tree: {s} {str(r)[:200]}",
                "commit_sha": "", "files_changed": 0}
    remote_tree = {entry["path"]: entry for entry in r.get("tree", []) if entry.get("type") == "blob"}

    # Step 5: diff. For each local file:
    #   - new (not on remote) OR
    #   - changed (sha differs)
    # we need to upload as a blob and include in the new tree.
    changed: List[dict] = []  # list of {path, mode, type, sha} entries for tree creation
    for repo_path, (fp, local_sha, content) in local_map.items():
        remote_entry = remote_tree.get(repo_path)
        if remote_entry and remote_entry.get("sha") == local_sha:
            continue  # unchanged
        # Upload blob
        b64 = base64.b64encode(content).decode("ascii")
        s2, r2 = _api_request("POST", "/git/blobs",
                              {"content": b64, "encoding": "base64"})
        if s2 != 201 or "sha" not in r2:
            return {"ok": False, "message": f"failed to create blob for {repo_path}: {s2} {str(r2)[:200]}",
                    "commit_sha": "", "files_changed": 0}
        changed.append({
            "path": repo_path,
            "mode": "100644",
            "type": "blob",
            "sha": r2["sha"],
        })

    if not changed:
        return {"ok": True, "message": "no changes to sync",
                "commit_sha": "", "files_changed": 0}

    files_changed = len(changed)

    # Step 6: create new tree (base_tree means "start from existing tree, apply these changes")
    s, r = _api_request("POST", "/git/trees",
                        {"base_tree": base_tree_sha, "tree": changed})
    if s != 201 or "sha" not in r:
        return {"ok": False, "message": f"failed to create tree: {s} {str(r)[:200]}",
                "commit_sha": "", "files_changed": files_changed}
    new_tree_sha = r["sha"]

    # Step 7: create commit
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    commit_msg = f"Auto-sync ({label}): {ts} [{files_changed} files]"
    s, r = _api_request("POST", "/git/commits", {
        "message": commit_msg,
        "tree": new_tree_sha,
        "parents": [base_commit_sha],
        "author": {"name": COMMITTER_NAME, "email": COMMITTER_EMAIL,
                   "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
    })
    if s != 201 or "sha" not in r:
        return {"ok": False, "message": f"failed to create commit: {s} {str(r)[:200]}",
                "commit_sha": "", "files_changed": files_changed}
    new_commit_sha = r["sha"]

    # Step 8: update ref (PATCH /git/refs/heads/main)
    s, r = _api_request("PATCH", f"/git/refs/heads/{BRANCH}",
                        {"sha": new_commit_sha, "force": False})
    if s != 200:
        # If force=False fails, the remote moved (someone else pushed). Try force update — still
        # safe because we built our tree on top of base_tree which IS the remote.
        # Actually safer: just retry the entire sync next cycle. Don't force-update.
        return {"ok": False,
                "message": f"failed to update ref (remote moved? retry next cycle): {s} {str(r)[:200]}",
                "commit_sha": new_commit_sha, "files_changed": files_changed}

    return {
        "ok": True,
        "message": f"committed {files_changed} files via API",
        "commit_sha": new_commit_sha[:7],
        "files_changed": files_changed,
    }


async def _do_sync(label: str = "periodic") -> dict:
    """Async wrapper. Runs API calls in a thread pool to keep event loop free."""
    global _last_sync_time, _last_sync_status, _last_sync_commit
    global _last_sync_error, _last_sync_files

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
    """Triggered by /sync Telegram command."""
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
    """Background task: sync every SYNC_INTERVAL_SECONDS, after INITIAL_DELAY_SECONDS."""
    log.info(
        f"auto_sync: periodic_sync_loop started "
        f"(interval: {SYNC_INTERVAL_SECONDS/3600:.1f}h, "
        f"initial delay: {INITIAL_DELAY_SECONDS}s) [API mode]"
    )

    if not GITHUB_TOKEN:
        log.warning("auto_sync: GITHUB_TOKEN not set — periodic sync DISABLED")
        if telegram_send:
            try:
                await telegram_send(
                    "⚠️ *Auto-Sync DISABLED*\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "GITHUB_TOKEN env var not set.\n"
                    "Runtime data will NOT persist across restarts."
                )
            except Exception:
                pass
        return

    await asyncio.sleep(INITIAL_DELAY_SECONDS)

    while True:
        try:
            r = await _do_sync(label="periodic")
            # Wave 101 (_WAVE101_LOUD_SYNC_SKIP): if any file was too large to
            # sync, alert LOUDLY so a silent data-loss can never happen again.
            if _SKIPPED_LARGE:
                if telegram_send:
                    try:
                        await telegram_send(
                            "WARNING: auto-sync SKIPPED oversized files (NOT backed up):\n"
                            + "\n".join("  - " + s for s in sorted(set(_SKIPPED_LARGE)))
                            + "\nThese exceed the 25MB sync cap and will lose recent "
                            "data on the next restart. They need rotation."
                        )
                    except Exception:
                        pass
                _SKIPPED_LARGE.clear()
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


# ---- _WAVE243_HEARTBEAT -------------------------------------------
# Wave 243: the 6-hourly sync is too coarse to tell whether the scanner is alive right now, so the
# heartbeat file gets its own small push: ONE file, ONE commit, through the contents API - no tree
# walk, no dashboard rebuild. Unchanged content is not pushed. Data-only commits on main, exactly
# like the 6-hourly ones (Railway records them as SKIPPED). Change W243_PUSH_SECONDS to trade commit
# noise against how stale the outside view may be.
W243_HEARTBEAT_PATH = "data/heartbeat.json"
W243_PUSH_SECONDS = 900
_w243_last_push_sha = [""]


def _w243_push_heartbeat_sync() -> dict:
    """Push data/heartbeat.json on its own. Returns {ok, message, changed}. Never raises."""
    try:
        if not GITHUB_TOKEN:
            return {"ok": False, "message": "GITHUB_TOKEN not set", "changed": False}
        fp = BASE_DIR / W243_HEARTBEAT_PATH
        if not fp.exists():
            return {"ok": False, "message": "no heartbeat file yet", "changed": False}
        content = fp.read_bytes()
        local_sha = _git_blob_sha(content)
        if local_sha == _w243_last_push_sha[0]:
            return {"ok": True, "message": "unchanged", "changed": False}
        s, r = _api_request("GET", "/contents/%s?ref=%s" % (W243_HEARTBEAT_PATH, BRANCH))
        remote_sha = r.get("sha") if (s == 200 and isinstance(r, dict)) else None
        if remote_sha == local_sha:
            _w243_last_push_sha[0] = local_sha
            return {"ok": True, "message": "unchanged on GitHub", "changed": False}
        body = {
            "message": "Heartbeat: %s" % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "content": base64.b64encode(content).decode("ascii"),
            "branch": BRANCH,
            "committer": {"name": COMMITTER_NAME, "email": COMMITTER_EMAIL},
        }
        if remote_sha:
            body["sha"] = remote_sha
        s, r = _api_request("PUT", "/contents/%s" % W243_HEARTBEAT_PATH, body)
        if s in (200, 201):
            _w243_last_push_sha[0] = local_sha
            return {"ok": True, "message": "pushed", "changed": True}
        # 409 = the ref moved (the 6-hourly sync committed first). Next cycle picks it up.
        return {"ok": False, "message": "heartbeat push HTTP %s" % s, "changed": False}
    except Exception as e:
        return {"ok": False, "message": "exception: %s" % _redact(str(e)), "changed": False}


async def heartbeat_push_loop():
    """Wave 243: push the heartbeat file every W243_PUSH_SECONDS. Log only - never Telegram,
    never an exception out of the loop."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            await asyncio.sleep(W243_PUSH_SECONDS)
            res = await loop.run_in_executor(None, _w265_locked, _w243_push_heartbeat_sync)  # _WAVE265_LEDGER_AND_RESTORE
            if res.get("changed"):
                log.info("auto_sync: heartbeat pushed")
            elif not res.get("ok"):
                log.warning("auto_sync: heartbeat push: %s", res.get("message"))
            # _WAVE265_LEDGER_AND_RESTORE: the ledger goes with the heartbeat, every 15 minutes, when it changed
            res = await loop.run_in_executor(None, _w265_locked, _w265_push_ledger_sync)
            if res.get("changed"):
                log.info("auto_sync: ledger pushed (W265)")
            elif not res.get("ok"):
                log.warning("auto_sync: W265 ledger push: %s", res.get("message"))
            # _WAVE284_TAIL_PUSH: the scan-log tail, on the same 15-minute clock, under the same lock, when it changed
            res = await loop.run_in_executor(None, _w265_locked, _w284_push_tail_sync)
            if res.get("changed"):
                log.info("auto_sync: scan tail pushed (%s rows)", res.get("rows"))
            elif not res.get("ok"):
                log.warning("auto_sync: scan tail push: %s", res.get("message"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("auto_sync: heartbeat loop iteration: %s", _redact(str(e)))
# ---- end _WAVE243_HEARTBEAT ---------------------------------------


# ---- _WAVE265_LEDGER_AND_RESTORE ------------------------------------------
# Wave 265 (24 Sep 2026): (A) outcomes.csv reaches GitHub every 15 minutes, with the heartbeat; (B) at boot, every
# sync-path file the image has that differs from GitHub HEAD is brought up to HEAD before startup uses it (bot.py's
# main() reads data/cooldowns.json first and re-reads it after - _WAVE268_COOLDOWN_FIX) - so a
# restart that is not a deploy can never push the image's old files over newer ones; (C) one lock, so the 15-minute
# pushes, the 6-hourly sync, /sync and the restore never race on the branch ref. Nothing here deletes anything.
import threading as _w265_threading
import time as _w265_time

W265_LEDGER_PATH = "outcomes.csv"
W265_RESTORE_BUDGET_SEC = 240          # the boot waits at most this long for downloads; the rest is logged, not written
W265_RESTORE_MAX_BYTES = 200 * 1024 * 1024
W265_TMP_SUFFIX = ".w265tmp"
_W265_LOCK = _w265_threading.Lock()
_w265_last_ledger_sha = [""]
_W265_ARCHIVE_RE = re.compile(r"^data/archive/outcomes_[^/]+\.csv$")


def _w265_locked(fn, *args):
    """Run one GitHub write at a time (the 15-minute pushes, the 6-hourly sync, /sync, the boot restore)."""
    with _W265_LOCK:
        return fn(*args)


def _do_sync_sync(label: str) -> dict:
    """Wave 265: the tree sync, under the same lock as the 15-minute pushes. Same result dict as before."""
    with _W265_LOCK:
        _W284_CUT[0] = None
        res = _w265_do_sync_unlocked(label)
        _w284_trim_on_disk(res)   # _WAVE284_TAIL_PUSH: only after the commit landed
        return res


def _w265_push_ledger_sync() -> dict:
    """Push outcomes.csv on its own, like the heartbeat: contents API, one file, one commit, only when it changed.
    Returns {ok, message, changed}. Never raises."""
    try:
        if not GITHUB_TOKEN:
            return {"ok": False, "message": "GITHUB_TOKEN not set", "changed": False}
        fp = BASE_DIR / W265_LEDGER_PATH
        if not fp.exists():
            return {"ok": False, "message": "no ledger file yet", "changed": False}
        content = fp.read_bytes()
        local_sha = _git_blob_sha(content)
        if local_sha == _w265_last_ledger_sha[0]:
            return {"ok": True, "message": "unchanged", "changed": False}
        s, r = _api_request("GET", "/contents/%s?ref=%s" % (W265_LEDGER_PATH, BRANCH))
        if s not in (200, 404):
            return {"ok": False, "message": "ledger check HTTP %s" % s, "changed": False}
        remote_sha = r.get("sha") if (s == 200 and isinstance(r, dict)) else None
        if remote_sha == local_sha:
            _w265_last_ledger_sha[0] = local_sha
            return {"ok": True, "message": "unchanged on GitHub", "changed": False}
        body = {
            "message": "Ledger: %s" % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "content": base64.b64encode(content).decode("ascii"),
            "branch": BRANCH,
            "committer": {"name": COMMITTER_NAME, "email": COMMITTER_EMAIL},
        }
        if remote_sha:
            body["sha"] = remote_sha
        s, r = _api_request("PUT", "/contents/%s" % W265_LEDGER_PATH, body, timeout=60)
        if s in (200, 201):
            _w265_last_ledger_sha[0] = local_sha
            return {"ok": True, "message": "pushed", "changed": True}
        # 409 = the ref moved between the check and the write. Next cycle picks it up.
        return {"ok": False, "message": "ledger push HTTP %s" % s, "changed": False}
    except Exception as e:
        return {"ok": False, "message": "exception: %s" % _redact(str(e)), "changed": False}


# ---- _WAVE284_TAIL_PUSH ----------------------------------------------------------------------------------------
# TAIL-PUSH (27 Oct 2026, Phase 1: stop losing data): the scan log reaches GitHub only with the 6-hourly sync, so a
# crash lost up to 6 hours of scan rows. strategy_log.py also appends every scan row to data/scan_log_tail.csv; it
# is pushed here every 15 minutes (with the ledger), trimmed at every full push (in that same commit, then on disk),
# and merged back into the restored scan log at boot. Never raises.
W284_LOG_PATH = "data/strategy_log.csv"
W284_TAIL_PATH = "data/scan_log_tail.csv"
_W284_PART_RE = re.compile(r"^data/archive/strategy_log_[^/]+\.csv$")
_W284_KEY = ("timestamp", "market", "tf", "setup_type", "direction", "decision")
_w284_last_tail_sha = [""]
_W284_CUT = [None]          # the newest scan row in the full push being made (its timestamp)


def _w284_ts(s):
    """A scan row timestamp as an aware datetime, or None."""
    try:
        d = datetime.fromisoformat((s or "").strip().replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _w284_rows(content):
    """(header, rows) of CSV bytes."""
    import csv as _c
    import io as _i
    rd = _c.DictReader(_i.StringIO(content.decode("utf-8", "replace"), newline=""))
    rows = list(rd)
    return list(rd.fieldnames or []), rows


def _w284_bytes(header, rows):
    import csv as _c
    import io as _i
    buf = _i.StringIO()
    w = _c.DictWriter(buf, fieldnames=header, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in header})
    return buf.getvalue().encode("utf-8")


def _w284_newest(content):
    """The timestamp of the last row of a scan-log file (rows are in time order), or None when it has none."""
    try:
        _h, rows = _w284_rows(content)
        return _w284_ts(rows[-1].get("timestamp")) if rows else None
    except Exception:
        return None


def _w284_keep(rows, cut):
    """The tail rows newer than `cut` (a row without a readable timestamp is kept - never lost). Strictly newer: the
    row at the cut is in this push, and a restart may not have the archive part it sits in."""
    out = []
    for r in rows:
        t = _w284_ts(r.get("timestamp"))
        if t is None or t > cut:
            out.append(r)
    return out


def _w284_trim_for_commit(local_map):
    """Inside the full sync: the tail that goes into this commit holds only the rows newer than the newest scan row
    in this commit (the live log, or the newest archive part when the live log has no rows). Sets _W284_CUT."""
    _W284_CUT[0] = None
    try:
        lg = local_map.get(W284_LOG_PATH)
        if lg is None:
            return            # the scan log is not in this push (too big / unreadable): never trim
        cut = _w284_newest(lg[2])
        if cut is None:
            parts = sorted(p for p in local_map if _W284_PART_RE.match(p))
            cut = _w284_newest(local_map[parts[-1]][2]) if parts else None
        if cut is None:
            return
        _W284_CUT[0] = cut
        tl = local_map.get(W284_TAIL_PATH)
        if tl is not None:
            header, rows = _w284_rows(tl[2])
            kept = _w284_keep(rows, cut)
            if len(kept) != len(rows):
                content = _w284_bytes(header, kept)
                local_map[W284_TAIL_PATH] = (tl[0], _git_blob_sha(content), content)
    except Exception as e:
        _W284_CUT[0] = None
        log.warning("TAIL-PUSH: trim for the full push skipped: %s" % _redact(str(e)))


def _w284_trim_on_disk(res):
    """After the full sync: when it landed (ok), drop from the tail on disk the rows that push carried. Fresh rows,
    under safe_io's lock: a row appended during the sync is newer and stays."""
    cut, _W284_CUT[0] = _W284_CUT[0], None
    try:
        fp = BASE_DIR / W284_TAIL_PATH
        if not (res or {}).get("ok") or cut is None or not fp.exists():
            return
        import safe_io
        header = _w284_rows(fp.read_bytes())[0]
        seen = {}

        def _mut(rows):
            seen["before"] = len(rows)
            kept = _w284_keep(rows, cut)
            seen["after"] = len(kept)
            return kept
        if header:
            safe_io.safe_rewrite_csv(str(fp), header, _mut)
            log.info("TAIL-PUSH: tail trimmed after the full push - kept %s of %s row(s)"
                     % (seen.get("after"), seen.get("before")))
    except Exception as e:
        log.warning("TAIL-PUSH: tail trim after the full push skipped: %s" % _redact(str(e)))


def _w284_push_tail_sync() -> dict:
    """Push data/scan_log_tail.csv on its own, like the ledger: contents API, one file, one commit, only when it
    changed. The caller holds _W265_LOCK. Returns {ok, message, changed, rows}. Never raises."""
    try:
        if not GITHUB_TOKEN:
            return {"ok": False, "message": "GITHUB_TOKEN not set", "changed": False}
        fp = BASE_DIR / W284_TAIL_PATH
        if not fp.exists():
            return {"ok": True, "message": "no tail yet", "changed": False}
        content = fp.read_bytes()
        local_sha = _git_blob_sha(content)
        if local_sha == _w284_last_tail_sha[0]:
            return {"ok": True, "message": "unchanged", "changed": False}
        s, r = _api_request("GET", "/contents/%s?ref=%s" % (W284_TAIL_PATH, BRANCH))
        if s not in (200, 404):
            return {"ok": False, "message": "tail check HTTP %s" % s, "changed": False}
        remote_sha = r.get("sha") if (s == 200 and isinstance(r, dict)) else None
        if remote_sha == local_sha:
            _w284_last_tail_sha[0] = local_sha
            return {"ok": True, "message": "unchanged on GitHub", "changed": False}
        body = {
            "message": "Scan tail: %s" % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "content": base64.b64encode(content).decode("ascii"),
            "branch": BRANCH,
            "committer": {"name": COMMITTER_NAME, "email": COMMITTER_EMAIL},
        }
        if remote_sha:
            body["sha"] = remote_sha
        s, r = _api_request("PUT", "/contents/%s" % W284_TAIL_PATH, body, timeout=60)
        if s in (200, 201):
            _w284_last_tail_sha[0] = local_sha
            return {"ok": True, "message": "pushed", "changed": True, "rows": max(0, content.count(b"\n") - 1)}
        # 409 = the ref moved between the check and the write. Next cycle picks it up.
        return {"ok": False, "message": "tail push HTTP %s" % s, "changed": False}
    except Exception as e:
        return {"ok": False, "message": "exception: %s" % _redact(str(e)), "changed": False}


def _w284_merge_tail_at_boot(remote):
    """At boot, after the restore (the caller holds _W265_LOCK): append to the restored scan log the tail rows not
    older than its newest row whose key is not in it. Only when both files on disk equal GitHub HEAD (`remote`: the
    restore's path -> tree entry). Returns the one-line summary. Never raises."""
    try:
        if W284_TAIL_PATH not in remote:
            return "boot merge: no tail on GitHub yet - nothing to merge"
        if W284_LOG_PATH not in remote:
            return "boot merge not run - no scan log on GitHub"
        for p in (W284_LOG_PATH, W284_TAIL_PATH):
            f = BASE_DIR / p
            if not f.exists() or _git_blob_sha(f.read_bytes()) != remote[p].get("sha"):
                return "boot merge not run - %s on disk is not GitHub HEAD's (the restore left or failed it)" % p
        import safe_io
        lp = BASE_DIR / W284_LOG_PATH
        with safe_io.file_lock(str(lp)):
            raw = lp.read_bytes()
            header, rows = _w284_rows(raw)
            theader, trows = _w284_rows((BASE_DIR / W284_TAIL_PATH).read_bytes())
            if not trows:
                return "boot merge: the tail is empty - nothing to merge"
            if theader != header:
                return "boot merge not run - the tail's header differs from the scan log's"
            ref = _w284_ts(rows[-1].get("timestamp")) if rows else None
            if ref is None:   # the live log has no rows (a rotation): the newest archive part's newest row
                parts = sorted((BASE_DIR / "data" / "archive").glob("strategy_log_*.csv"))
                ref = _w284_newest(parts[-1].read_bytes()) if parts else None
            known = set(tuple((r.get(k) or "").strip() for k in _W284_KEY) for r in rows)
            seen, add = set(), []
            already = older = dup = 0
            for r in trows:
                k = tuple((r.get(c) or "").strip() for c in _W284_KEY)
                if k in seen:
                    dup += 1
                    continue
                seen.add(k)
                if k in known:
                    already += 1
                    continue
                t = _w284_ts(r.get("timestamp"))
                if ref is not None and (t is None or t < ref):
                    older += 1
                    continue
                add.append(r)
            if add:
                body = _w284_bytes(header, add).split(b"\n", 1)[1]   # the rows, without a second header
                with open(lp, "ab") as fh:
                    if raw and not raw.endswith(b"\n"):
                        fh.write(b"\r\n")
                    fh.write(body)
                    fh.flush()
                    os.fsync(fh.fileno())
        what = ("boot merge - tail %d row(s): %d added to the scan log, %d already in it, %d older than its "
                "newest row (skipped), %d duplicated in the tail (skipped)" % (len(trows), len(add), already, older,
                                                                          dup))
        if not add and older + already == len(trows) - dup:
            what += " - the tail is not newer than the restored log, nothing merged"
        return what
    except Exception as e:
        return "boot merge failed, nothing written: %s" % _redact(str(e))
# ---- end _WAVE284_TAIL_PUSH ------------------------------------------------------------------------------------


def _w265_in_sync_paths(path: str) -> bool:
    """The same files the sync walks: data/**, outcomes.csv, docs/dashboard.html - minus dot-paths, __pycache__
    and the Wave 241/247 skip list."""
    parts = path.split("/")
    if not (path in ("outcomes.csv", "docs/dashboard.html") or (parts[0] == "data" and len(parts) > 1)):
        return False
    if any(p.startswith(".") for p in parts) or "__pycache__" in parts:
        return False
    return not _w241_skip(Path(path))


def _w265_rank(path: str) -> int:
    """The ledger first, then its archives, then everything else - what matters most is fetched inside the budget."""
    if path in (W284_LOG_PATH, W284_TAIL_PATH):   # _WAVE284_TAIL_PUSH: the scan log and its tail before the bulk
        return 1
    return 0 if path == W265_LEDGER_PATH else 1 if _W265_ARCHIVE_RE.match(path) else 2


def _w265_write(dest: Path, data: bytes) -> bool:
    """Atomic write: a temp file next to it, fsync, os.replace. On any error the old file is untouched and the temp
    file is removed. Returns True when written."""
    tmp = dest.with_name(dest.name + W265_TMP_SUFFIX)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
        return True
    except Exception as e:
        log.error("W265: could not write %s: %s" % (dest.name, _redact(str(e))))
        return False
    finally:
        try:
            if tmp.exists():
                os.remove(tmp)
        except Exception:
            pass


def _w265_restore_log(out: dict) -> dict:
    msg = ("W265: restore at boot - %d file(s) differ from GitHub HEAD; %d replaced, %d created, %d failed, "
           "%d left (time/size budget), %.2f MB" % (out["differ"], out["replaced"], out["created"], out["failed"],
                                                 out["left"], out["bytes"] / (1024.0 * 1024.0)))
    if out.get("why"):
        msg += " - " + out["why"]
    (log.info if out.get("ok") else log.warning)(msg)
    log.info("TAIL-PUSH: %s" % (out.get("tail") or "boot merge not run - the restore stopped before it (%s)"
                                % (out.get("why") or "see the W265 line")))   # _WAVE284_TAIL_PUSH: one line a boot
    return out


def w265_restore_at_boot(budget_sec=None, max_bytes=None) -> dict:
    """Wave 265 Part B. Call FIRST in startup (_post_init). main() has read data/cooldowns.json before it; bot.py
    re-reads it when this replaced or created a file (_WAVE268_COOLDOWN_FIX). Returns a summary dict
    {ok, differ, replaced, created, failed, left, bytes, why}. Never raises, never deletes."""
    t0 = _w265_time.monotonic()
    budget = W265_RESTORE_BUDGET_SEC if budget_sec is None else budget_sec
    cap = W265_RESTORE_MAX_BYTES if max_bytes is None else max_bytes
    out = {"ok": False, "differ": 0, "replaced": 0, "created": 0, "failed": 0, "left": 0, "bytes": 0, "why": ""}
    try:
        if not GITHUB_TOKEN:
            out["why"] = "GITHUB_TOKEN not set - booting on the image's files"
            return _w265_restore_log(out)
        with _W265_LOCK:
            s, r = _api_request("GET", "/git/refs/heads/%s" % BRANCH)
            if s != 200 or "object" not in r:
                out["why"] = "could not read the branch (HTTP %s) - booting on the image's files" % s
                return _w265_restore_log(out)
            s, r = _api_request("GET", "/git/commits/%s" % r["object"]["sha"])
            if s != 200 or "tree" not in r:
                out["why"] = "could not read the head commit (HTTP %s) - booting on the image's files" % s
                return _w265_restore_log(out)
            s, r = _api_request("GET", "/git/trees/%s?recursive=1" % r["tree"]["sha"], timeout=60)
            if s != 200 or r.get("truncated"):
                out["why"] = ("GitHub's file list came back truncated" if s == 200 else
                              "could not read the file list (HTTP %s)" % s) + " - booting on the image's files"
                return _w265_restore_log(out)
            remote = {}
            for e in r.get("tree", []):
                path = e.get("path") or ""
                if e.get("type") == "blob" and e.get("sha") and _w265_in_sync_paths(path):
                    remote[path] = e
            local = {}
            for fp in _walk_sync_paths():
                try:
                    local[str(fp.relative_to(BASE_DIR)).replace("\\", "/")] = _git_blob_sha(fp.read_bytes())
                except Exception:
                    continue
            todo = []
            for path, e in remote.items():
                have = local.get(path)
                if have is None:
                    # only a missing ledger archive is created; a file that exists but was not hashed (too big,
                    # unreadable) is never overwritten
                    # _WAVE284_TAIL_PUSH: ... and the scan-log tail (the image of the deploy that ships it has none)
                    if (_W265_ARCHIVE_RE.match(path) or path == W284_TAIL_PATH) and not (BASE_DIR / path).exists():
                        todo.append((path, e, "created"))
                elif have != e["sha"]:
                    todo.append((path, e, "replaced"))
            todo.sort(key=lambda x: (_w265_rank(x[0]), x[0]))
            out["differ"] = len(todo)
            for path, e, kind in todo:
                size = int(e.get("size") or 0)
                if _w265_time.monotonic() - t0 > budget or out["bytes"] + size > cap:
                    out["left"] += 1
                    continue
                s2, b = _api_request("GET", "/git/blobs/%s" % e["sha"], timeout=60)
                data = None
                try:
                    if s2 == 200 and isinstance(b, dict) and b.get("encoding") == "base64":
                        data = base64.b64decode(b.get("content") or "")
                except Exception:
                    data = None
                if data is None or _git_blob_sha(data) != e["sha"]:
                    out["failed"] += 1
                    log.error("W265: restore of %s failed (HTTP %s, %s) - kept the image's copy"
                              % (path, s2, "sha mismatch" if data is not None else "no content"))
                    continue
                if _w265_write(BASE_DIR / path, data):
                    out[kind] += 1
                    out["bytes"] += len(data)
                else:
                    out["failed"] += 1
            if out["left"]:
                out["why"] = "the time/size budget ran out - %d file(s) keep the image's copy" % out["left"]
            out["tail"] = _w284_merge_tail_at_boot(remote)   # _WAVE284_TAIL_PUSH: under the same lock
        out["ok"] = out["failed"] == 0 and out["left"] == 0
    except Exception as ex:
        out["why"] = "exception: %s" % _redact(str(ex))
    return _w265_restore_log(out)
# ---- end _WAVE265_LEDGER_AND_RESTORE --------------------------------------


def status() -> str:
    """Human-readable auto-sync status string for startup banner / /status."""
    if not GITHUB_TOKEN:
        return "⚠️ Auto-sync DISABLED (GITHUB_TOKEN not set)"
    if _last_sync_time is None:
        return "Auto-sync: waiting for first cycle [API mode]"
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
