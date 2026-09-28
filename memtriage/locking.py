"""A cross-process lock around the memory stores.

The plugin's read-modify-write is not exclusive. Two writers exist:

1. This plugin: read the whole store, apply a plan, write the whole file.
2. The built-in ``memory`` tool, which is a live writer on the same files
   and is called from the same conversation that triggers a triage.

Between the read and the ``os.replace`` write, a ``memory`` append lands and
is then silently discarded, because the plugin's snapshot predates it. The
staleness check added in this wave only catches a *shifted index*; it cannot
catch an append after the last entry, which leaves every index intact.

``fcntl.flock`` is advisory and unavailable on Windows, so this degrades to
an ``O_EXCL`` lockfile mutex there. Both are best-effort: the point is to
close the common case, not to promise a transaction.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:  # POSIX
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    _HAVE_FCNTL = False

# A lockfile older than this was left by a crashed process and is safe to
# break. Only used by the non-fcntl path.
STALE_LOCK_SECONDS = 60
# Two writers colliding is normal here (the built-in memory tool and a
# concurrent triage), so a plain store write waits rather than proceeding
# unlocked. The plan path uses a shorter budget and reports the timeout.
LOCK_WAIT_SECONDS = 30.0

# REENTRANCY IS REQUIRED, NOT OPTIONAL. execute_plan holds the store lock
# across its whole read-modify-write, and write_entries() takes the same lock
# again for the write. flock is per open-file-description, so a second
# acquire on a NEW descriptor in the SAME process blocks forever — the
# process deadlocks against itself. This tracks what the current thread
# already holds so the nested acquire is a no-op.
#
# The held-state is thread-local, not process-local, so two threads in one
# process still contend with each other exactly as two processes would.
_held: "threading.local" = threading.local()


def _held_map() -> dict:
    m = getattr(_held, "locks", None)
    if m is None:
        m = {}
        _held.locks = m
    return m


def _is_held(key: str) -> bool:
    return _held_map().get(key, 0) > 0


def _lock_path(store_path: Path) -> Path:
    """The lock file this module shares with the built-in memory tool.

    It MUST be the same inode the built-in tool locks, or the two provide no
    mutual exclusion at all and a triage can read-modify-write a store the
    memory tool is writing. ``tools/memory_tool_store.py:173`` computes
    ``path.with_suffix(path.suffix + ".lock")`` -- i.e. ``MEMORY.md.lock``.

    The previous ``.memtriage.lock`` suffix was a different file, so the
    comment claiming these exclude each other was false: a concurrent
    ``memory`` tool write and a triage both proceeded.
    """
    return store_path.with_suffix(store_path.suffix + ".lock")


@contextmanager
def store_lock(
    store_path: Path, *, timeout: float = 10.0, poll: float = 0.05
) -> Iterator[bool]:
    """Hold an exclusive lock on ``store_path``'s lock file.

    Yields True if the lock was acquired, False on timeout. A timeout is
    not an error: the caller decides whether proceeding unlocked is worse
    than skipping the run. Re-entrant within a thread: a nested acquire
    yields True without blocking.
    """
    lock_file = _lock_path(store_path)
    key = str(lock_file)
    if _is_held(key):
        # Already ours via an outer acquire. Do not touch flock: a second
        # lock on a new descriptor would block against our own process.
        _held_map()[key] += 1
        try:
            yield True
        finally:
            _held_map()[key] -= 1
        return

    try:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_file, "a+")
    except OSError:
        # Cannot even create the lock file (read-only fs, permissions).
        # Proceed unlocked rather than refuse to run.
        yield False
        return

    acquired = False
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                if _HAVE_FCNTL:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    _exclusive_create(lock_file)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(poll)
        if acquired:
            _held_map()[key] = _held_map().get(key, 0) + 1
        yield acquired
    finally:
        if acquired:
            _held_map()[key] -= 1
            try:
                if _HAVE_FCNTL:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[union-attr]
                else:
                    lock_file.unlink()
            except OSError:
                pass
        handle.close()


def _exclusive_create(lock_file: Path) -> None:
    """O_EXCL create is the only atomic test-and-set in the stdlib."""
    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # Break a lock left behind by a crashed process, but only once it is
        # old enough that no live holder can still own it.
        try:
            age = time.time() - lock_file.stat().st_mtime
        except OSError:
            raise
        if age < STALE_LOCK_SECONDS:
            raise
        try:
            lock_file.unlink()
        except OSError:
            raise
        fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    finally:
        os.close(fd)


@contextmanager
def multi_store_lock(
    store_paths, *, timeout: float = 10.0, poll: float = 0.05
) -> "Iterator[list]":
    """Hold the lock for EVERY store this plan will rewrite.

    WHY THIS EXISTS. ``execute_plan`` used to take a lock on ONE store (the
    profile) and then read AND rewrite BOTH. ``_lock_path`` derives the lock
    file from the store path, so ``MEMORY.md.lock`` and ``USER.md.lock`` are
    different inodes. The built-in ``memory`` tool locks per-file, so an
    append to ``MEMORY.md`` took ``MEMORY.md.lock`` -- which nothing held --
    and the executor's later ``os.replace`` published a body built from a
    snapshot taken before that append. The append was destroyed with no error
    raised anywhere. Reproduced 2026-09-28; the comment above that lock
    claimed the hole was closed.

    ORDER MATTERS and is fixed (sorted by path) so two writers cannot
    deadlock by grabbing the pair in opposite orders.

    Yields the list of paths whose lock was actually acquired, so the caller
    can report which store it failed to exclude. An empty list means NOTHING
    is held; callers must treat that as "proceeding unlocked", not success.
    """
    paths = sorted({Path(p) for p in store_paths}, key=lambda p: str(p))
    held: "list[Path]" = []
    contexts = []
    try:
        for p in paths:
            ctx = store_lock(p, timeout=timeout, poll=poll)
            got = ctx.__enter__()
            contexts.append(ctx)
            if got:
                held.append(p)
        yield held
    finally:
        for ctx in reversed(contexts):
            ctx.__exit__(None, None, None)
