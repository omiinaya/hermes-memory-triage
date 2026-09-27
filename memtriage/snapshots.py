"""Pre-write snapshots of the memory stores.

WHY THIS EXISTS. Quarantine backs up ENTRIES the plugin removes. It does not
back up the FILE before a write. Those are different failure modes:

* Quarantine protects against losing an entry the plugin meant to drop.
* A snapshot protects against the plugin writing WRONG content — a bad plan,
  a mangled rebuild, a bug that pads or truncates.

A live incident on 2026-09-27 was exactly the second kind: a correct atomic
write of incorrect content (110 repetitions of a test fixture string). The
atomic write worked perfectly; it faithfully persisted garbage. Recovery meant
hand-editing the file to strip the injected text. A snapshot would have made
it a single restore.

WHY A BYTE COPY AND NOT A TEXT SNAPSHOT. The store's integrity is the exact
bytes on disk. ``write_entries`` replaces the whole file via ``os.replace``, so
the only thing that can restore it faithfully is the previous bytes. This
copies the file, it does not re-serialize parsed entries.

DESTINATION. ``<data_dir>/snapshots/`` — inside the plugin's own data dir, NOT
next to the store. A backup that lands in the same directory as the thing it
protects is one ``rm -rf`` away from dying with it.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SNAPSHOTS_DIRNAME = "snapshots"
# A snapshot per write is correct but wasteful: the same run may touch a
# target more than once, and the pre-run state is what matters. Capping the
# set keeps a long-lived install from accumulating copies of a file that only
# changes when the plugin runs.
DEFAULT_RETAIN_SNAPSHOTS = 30


def snapshots_dir(data_dir: Path) -> Path:
    return Path(data_dir) / SNAPSHOTS_DIRNAME


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _stamp(run_id: str) -> str:
    """UTC timestamp + a sanitised run id.

    The run id is part of the NAME, not the directory: two snapshots of the
    same target in the same second must not collide, and a run that touches
    both targets must be identifiable in `snapshots` output. Sub-second
    precision keeps rapid successive writes distinct even when the caller
    reuses a run id.
    """
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    micro = f"{time.time() % 1:.3f}".lstrip("0.").replace(".", "")
    safe = _SAFE.sub("-", (run_id or "manual")).strip("-") or "manual"
    return f"{ts}{micro}-{safe}"


def snapshot_path(
    data_dir: Path, target: str, run_id: str, filename: str
) -> Path:
    # "__" is the field separator: a run id may contain hyphens, so
    # splitting on "-" made the target ambiguous to parse back out.
    return (
        snapshots_dir(data_dir)
        / f"{target}__{_stamp(run_id)}__{filename}"
    )


def take(
    data_dir: Path,
    target: str,
    run_id: str,
    keep: int = DEFAULT_RETAIN_SNAPSHOTS,
) -> Optional[Dict[str, Any]]:
    """Copy the target's store file aside BEFORE it is overwritten.

    Returns a record (path, bytes, existed) or None when there is nothing
    worth keeping — the file does not exist yet, so a write creates rather
    than destroys. Never raises: a failed backup must not block the write it
    was meant to protect, but the caller MUST see that it failed, so the
    record carries ``ok=False`` and the reason.
    """
    from . import store as memory_store

    try:
        snapshots_dir(data_dir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"target": target, "ok": False, "existed": True,
                "reason": f"could not create snapshot dir: {exc}"}
    src = memory_store.path_for(target)
    dest = snapshot_path(data_dir, target, run_id, src.name)
    rec: Dict[str, Any] = {
        "target": target,
        "source": str(src),
        "path": str(dest),
        "ok": False,
    }
    try:
        if not src.exists():
            rec.update(existed=False, reason="no file yet; nothing to preserve")
            return rec
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Copy to a unique temp name then rename: a half-written snapshot is
        # worse than none, because a restore would silently truncate.
        tmp = dest.with_name(dest.name + f".tmp{os.getpid()}")
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
        rec.update(
            ok=True,
            existed=True,
            bytes=src.stat().st_size,
        )
    except OSError as exc:
        rec.update(reason=f"{type(exc).__name__}: {exc}")
        return rec
    _prune(data_dir, target, keep)
    return rec


def _prune(data_dir: Path, target: str, keep: int) -> int:
    """Drop the oldest snapshots for a target, newest kept."""
    if keep <= 0:
        return 0
    try:
        # Sort by the timestamp embedded in the NAME, not mtime. mtime has
        # coarse granularity on some filesystems, so a batch of snapshots
        # taken in the same second ties and the "newest kept" guarantee
        # becomes arbitrary — which can drop the very snapshot a restore
        # would need. The name sorts lexicographically in real time order.
        snaps = sorted(
            snapshots_dir(data_dir).glob(f"{target}__*"),
            key=lambda p: (p.name.split("__", 2)[1], p.stat().st_mtime),
            reverse=True,
        )
    except (OSError, IndexError):
        return 0
    removed = 0
    for stale in snaps[keep:]:
        try:
            stale.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def list_snapshots(data_dir: Path, target: Optional[str] = None) -> List[Dict[str, Any]]:
    """All retained snapshots, newest first."""
    out: List[Dict[str, Any]] = []
    try:
        # Same name-based ordering as _prune, so the listing agrees with
        # what the cap actually kept.
        files = sorted(
            snapshots_dir(data_dir).glob("*__*"),
            key=lambda p: (p.name.split("__", 2)[1], p.stat().st_mtime),
            reverse=True,
        )
    except (OSError, IndexError):
        return out
    for p in files:
        name = p.name
        target_name = name.split("__", 1)[0]
        if target and target_name != target:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({
            "target": target_name,
            "path": str(p),
            "name": name,
            "bytes": st.st_size,
            "mtime_iso": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)
            ),
        })
    return out


def restore(
    data_dir: Path,
    target: str,
    name: str,
    keep: int = DEFAULT_RETAIN_SNAPSHOTS,
) -> Dict[str, Any]:
    """Restore a snapshot over the live store file.

    Takes a snapshot of the CURRENT file first, so a restore is itself
    reversible — you can undo a restore that went wrong.
    """
    from . import store as memory_store

    dest = memory_store.path_for(target)
    root = snapshots_dir(data_dir)
    # CONTAINMENT. `name` arrives from a CLI argument, so a value like
    # "../../../etc/passwd" would otherwise read or clobber outside the
    # snapshot dir. Resolve first, then refuse anything that is not a
    # descendant of the root.
    if not name or Path(name).name != name or name in (".", ".."):
        return {"restored": False,
                "reason": f"invalid snapshot name {name!r}"}
    try:
        root_r = root.resolve()
    except OSError as exc:
        return {"restored": False, "reason": str(exc)}
    src = root / name
    try:
        if root_r not in src.resolve().parents:
            return {"restored": False,
                    "reason": f"snapshot {name!r} is outside {root_r}"}
    except OSError as exc:
        return {"restored": False, "reason": str(exc)}
    if not src.is_file():
        # Tolerate a bare filename match, since users paste what they read.
        matches = [
            p for p in root.glob("*__*")
            if p.name == name or p.name.endswith(name)
        ]
        if len(matches) != 1:
            return {
                "restored": False,
                "reason": (
                    f"no snapshot named {name!r}"
                    + ("" if len(matches) != 1 else f" ({len(matches)} ambiguous matches)")
                ),
            }
        src = matches[0]
    try:
        undo = take(data_dir, target, "before-restore", keep=keep)
    except OSError as exc:
        undo = {"ok": False, "reason": str(exc)}
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with memory_store.file_lock(dest):
            tmp = dest.with_name(dest.name + f".restore{os.getpid()}")
            shutil.copy2(src, tmp)
            os.replace(tmp, dest)
    except OSError as exc:
        return {
            "restored": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "undo": undo,
        }
    return {
        "restored": True,
        "target": target,
        "from": str(src),
        "to": str(dest),
        "undo_snapshot": (undo or {}).get("path"),
    }
