"""
safe_io.py - NQ CALLS 2026
============================
Single source of truth for safe file I/O. Used by strategy_log.py and
outcome_tracker.py to prevent the data-loss bugs that caused row-counts
to bounce around (3.5k -> 7k -> 3k).

WHAT THIS FIXES
---------------
1. **Truncate-write race**: Old code did `open(path, "w")` to rewrite a CSV.
   If two writers ran concurrently (scan loop + missed-setup checker), the
   second one's truncate would clobber the first one's appends. Result:
   silently lost rows.

2. **Crash-mid-write corruption**: Old code wrote rows one at a time into
   the live file. If the bot crashed (OOM, Railway restart, anything)
   mid-write, the file was left half-written. Then auto_sync committed
   that corrupt half-file as canonical state.

3. **Read-modify-rewrite stale snapshot**: Old code read all rows, mutated
   some, then `open("w")` and rewrote. Any row appended between the read
   and the rewrite was wiped. This is the 7k -> 3k bug.

THE FIX
-------
- `atomic_write(path, bytes)`: write to {path}.tmp, then os.replace().
  os.replace() is atomic on POSIX and Windows. Either the new file is
  fully there or the old one is — never half-state.

- `file_lock(path)`: exclusive lock via fcntl (Linux/Railway) or msvcrt
  (Windows/dev). Cross-process safe. Auto-releases on context exit even
  on exceptions.

- `safe_append_csv`: append a single row under the lock. No other writer
  can be doing a rewrite at the same time.

- `safe_rewrite_csv(path, fieldnames, mutator_fn)`: under the lock,
  RE-READ the file, pass rows to mutator_fn, then atomically write
  the result. This guarantees rewrites use the freshest data — no stale
  snapshot from before the function call.

WHY THIS DESIGN
---------------
- Single .py module; minimal surface area; easy to review.
- Pure stdlib, no new deps. Works identical on Railway (Linux) and
  Wayne's Windows dev box.
- Backwards-compatible: existing callers keep working; we just swap
  their write paths to use these helpers.
- The lock is keyed on a sidecar `.lock` file, NOT the data file itself.
  Locking the data file directly would race with the rename in
  atomic_write.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from typing import Any, Callable, Iterable, List, Optional

# Platform-specific lock backend
if sys.platform == "win32":
    import msvcrt
    _IS_WINDOWS = True
else:
    import fcntl
    _IS_WINDOWS = False


def _lock_path(path: str) -> str:
    """The sidecar lockfile for `path`. We lock this, not the data file
    itself, so atomic rename of the data file doesn't race the lock."""
    return path + ".lock"


@contextmanager
def file_lock(path: str, timeout_s: float = 30.0):
    """
    Acquire an exclusive lock keyed on `path`. Cross-process safe.

    Locks a sidecar `.lock` file (not `path` itself), so rename-based
    atomic writes on `path` don't conflict with the lock.

    On Linux uses fcntl.flock (blocking with poll timeout via deadline).
    On Windows uses msvcrt.locking (also blocking with retry).

    Auto-releases on context exit, including exceptions.
    """
    lock_file = _lock_path(path)
    # Ensure parent dir exists
    parent = os.path.dirname(lock_file)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # Open with O_RDWR | O_CREAT — we don't care about file contents,
    # we just need a stable inode/handle to lock.
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if _IS_WINDOWS:
            # msvcrt.locking is byte-level; lock 1 byte at offset 0.
            # LK_LOCK retries every second up to 10 times.
            import time
            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"safe_io: timed out waiting for lock on {path}"
                        )
                    time.sleep(0.05)
        else:
            # fcntl.flock blocks indefinitely; use LOCK_EX | LOCK_NB with
            # retry loop for timeout support.
            import time
            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"safe_io: timed out waiting for lock on {path}"
                        )
                    time.sleep(0.05)

        yield  # caller does the work
    finally:
        try:
            if _IS_WINDOWS:
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


def atomic_write(path: str, data: bytes) -> None:
    """
    Atomically write `data` to `path`. Crash-safe.

    Writes to a temp file in the SAME directory (so os.replace stays on
    the same filesystem and is truly atomic), fsyncs the file contents,
    then renames over the target. If anything fails before the rename,
    the original file is untouched. After the rename, readers see either
    the old file or the new one — never a half-written file.

    Caller is responsible for serializing concurrent writers via file_lock.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("atomic_write expects bytes; encode strings first")

    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)

    # NamedTemporaryFile in same dir → same filesystem → atomic rename
    fd, tmp_path = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".",
        suffix=".tmp",
        dir=parent,
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())  # durable to disk before rename
            except OSError:
                # fsync isn't supported on every filesystem; not fatal
                pass
        os.replace(tmp_path, path)  # atomic on POSIX and Windows
        tmp_path = None  # rename succeeded; don't try to clean up
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    """Convenience: encode text and atomically write."""
    atomic_write(path, text.encode(encoding))


def atomic_write_json(path: str, obj: Any, indent: Optional[int] = 2) -> None:
    """Atomically write JSON. Replaces the unsafe `open(path, "w")` pattern."""
    text = json.dumps(obj, indent=indent, default=str)
    atomic_write_text(path, text)


def safe_append_csv(path: str, fieldnames: List[str], row: dict) -> None:
    """
    Atomically append a single row to a CSV file under an exclusive lock.

    If the file doesn't exist, creates it with a header row first.
    If the file exists but is empty (or has wrong header), this still
    appends — caller is responsible for header consistency via
    a separate `_ensure_csv()` call before first use.

    The lock prevents the read-modify-rewrite race in `safe_rewrite_csv`
    from clobbering this append.

    NOTE: append-mode (`"a"`) does NOT use atomic-replace because that
    would require copying the entire existing file. Instead we rely
    purely on the lock to serialize against rewriters. CSV append is
    line-buffered and naturally atomic at the OS level for short rows
    on local filesystems.
    """
    with file_lock(path):
        need_header = not os.path.exists(path) or os.path.getsize(path) == 0
        # Use io.StringIO + os write for the row to avoid pulling in
        # newline translations. Write in binary mode.
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fieldnames)
        if need_header:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fieldnames})
        line_bytes = buf.getvalue().encode("utf-8")

        # Append in binary so newline handling matches the rewrite path.
        with open(path, "ab") as f:
            f.write(line_bytes)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass


def safe_rewrite_csv(
    path: str,
    fieldnames: List[str],
    mutator: Callable[[List[dict]], List[dict]],
) -> int:
    """
    Atomically rewrite a CSV under an exclusive lock.

    Steps inside the lock:
      1. Read all rows from disk RIGHT NOW (fresh snapshot, not the one
         the caller had earlier).
      2. Pass the fresh rows to `mutator(rows) -> new_rows`.
      3. Build the full CSV text in memory.
      4. Atomically replace the file (write tmp, fsync, rename).

    This pattern guarantees:
      - No appends between read and write get lost — they're inside
        the same lock, so concurrent appenders wait until we're done.
      - If we crash between the in-memory build and the rename, the
        original file is untouched.
      - If we crash between `open(tmp)` and `replace`, the tmp file
        is the only orphan; the data file is untouched.

    Returns the number of rows written.

    The caller's `mutator` MUST NOT do any other I/O on `path` — it should
    just operate on the rows in memory.
    """
    with file_lock(path):
        # Step 1: fresh read inside the lock
        rows: List[dict] = []
        if os.path.exists(path):
            with open(path, "r", newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

        # Step 2: caller mutates
        new_rows = list(mutator(rows))

        # Step 3: build full CSV text
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fieldnames)
        w.writeheader()
        for r in new_rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})

        # Step 4: atomic replace
        atomic_write_text(path, buf.getvalue())

        return len(new_rows)


def safe_read_csv(path: str) -> List[dict]:
    """
    Read a CSV under a shared lock — guarantees we don't read mid-rewrite.

    On a tiny scale (single bot process), the lock is mostly belt-and-suspenders;
    but on Railway with auto_sync + scan loop running concurrently, this matters.
    """
    if not os.path.exists(path):
        return []
    with file_lock(path):
        with open(path, "r", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))


def is_locked(path: str) -> bool:
    """
    Best-effort check: is some other process currently holding the lock?
    Used by auto_sync to skip syncing mid-write rather than catch a
    partially-rewritten file.

    Returns True if locked, False if free or unknown. Always closes the FD.
    """
    lock_file = _lock_path(path)
    if not os.path.exists(lock_file):
        return False
    try:
        fd = os.open(lock_file, os.O_RDWR)
    except OSError:
        return False
    try:
        if _IS_WINDOWS:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                # got it → not locked
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                return False
            except OSError:
                return True
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                return False
            except BlockingIOError:
                return True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
