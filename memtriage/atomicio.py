"""Atomic JSON writes that are safe under concurrency.

Eight modules wrote JSON with the same pattern:

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(...)
    os.replace(tmp, path)

That is atomic against a CRASH but not against CONCURRENCY: every writer
uses the identical temp name, so two writers race on it. Measured on
2026-09-27 with 4 threads doing 15 ``mark_notified`` each: 60 attempts
stored 18 ids and 31 of them raised ``FileNotFoundError`` on the
``os.replace`` -- because the other thread had already renamed the file out
from under them. Every ``notified_runs`` entry lost that way is a run whose
report Omar is never shown.

The temp name must be unique per writer (pid + thread), and the
read-modify-write span must be held under a lock, or the loser of the race
still writes back a state it read before the winner's change.
"""

import json
import os
import threading
from pathlib import Path
from typing import Any

# Per-process re-entrancy so a nested write of the same path cannot deadlock
# on its own lock (the executor takes a store lock, then writes state).
_local = threading.local()


def _held() -> set:
    s = getattr(_local, "held", None)
    if s is None:
        s = set()
        _local.held = s
    return s


def unique_tmp(path: Path) -> Path:
    """A temp path unique to this process AND thread."""
    return path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )


def write_json_atomic(path: Path, payload: Any, *, indent: Any = 2) -> None:
    """Serialise ``payload`` to ``path`` atomically, race-free.

    Never leaves a partial file: the temp name is private to this writer, so
    a concurrent writer cannot truncate or rename it away mid-write.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = unique_tmp(path)
    try:
        tmp.write_text(json.dumps(payload, indent=indent), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        # A failed replace can leave the temp behind; it is ours alone, so
        # removing it cannot destroy another writer's work.
        try:
            tmp.unlink()
        except OSError:
            pass


def read_modify_write(path: Path, mutate):
    """Apply ``mutate(state)`` to the JSON at ``path`` under a lock.

    ``mutate`` receives the current state and returns the state to persist
    (or None to skip writing). The whole load-modify-save is serialised, so
    concurrent callers cannot clobber one another's fields.
    """
    from . import locking

    path = Path(path)
    held = _held()
    key = str(path)
    reentrant = key in held
    if reentrant:
        # Already inside a locked update for this path: take the lock
        # ourselves in a re-entrant way rather than deadlocking.
        held.add(key)
        try:
            state = _read(path)
            new = mutate(state)
            if new is not None:
                write_json_atomic(path, new)
            return
        finally:
            pass

    with locking.store_lock(path):
        held.add(key)
        try:
            state = _read(path)
            new = mutate(state)
            if new is not None:
                write_json_atomic(path, new)
        finally:
            held.discard(key)


def _read(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
