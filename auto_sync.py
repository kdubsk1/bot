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
                try:
                    if child.stat().st_size > 5 * 1024 * 1024:
                        log.warning(f"auto_sync: skipping large file {child} (>5MB)")
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


def _do_sync_sync(label: str) -> dict:
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
