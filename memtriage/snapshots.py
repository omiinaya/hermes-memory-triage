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
    protect: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Copy the target's store file aside BEFORE it is overwritten.

    Returns a record (path, bytes, existed) or None when there is nothing
    worth keeping — the file does not exist yet, so a write creates rather
    than destroys. Never raises: a failed backup must not block the write it
    was meant to protect, but the caller MUST see that it failed, so the
    record carries ``ok=False`` and the reason.

    ``protect`` names a snapshot that the retention prune must not delete.
    restore() passes the snapshot it is about to read, so taking its undo
    copy cannot prune away the very file the restore depends on.
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
    _prune(data_dir, target, keep, protect=protect)
    return rec


def _prune(
    data_dir: Path, target: str, keep: int,
    protect: Optional[Path] = None,
) -> int:
    """Drop the oldest snapshots for a target, newest kept.

    ``protect`` names a snapshot that must survive even if it is the oldest.
    restore() is reading that exact file moments later; pruning it first is
    how the recovery path destroys the only good copy.
    """
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
    protected = None
    if protect is not None:
        try:
            protected = Path(protect).resolve()
        except OSError:
            protected = None
    removed = 0
    for stale in snaps[keep:]:
        if protected is not None:
            try:
                if stale.resolve() == protected:
                    continue
            except OSError:
                pass
        try:
            stale.unlink()
            removed += 1
        except OSError:
            pass
    return removed


SKILLS_SNAPSHOT_SUBDIR = "skills"


def list_skill_snapshots(data_dir: Path) -> List[Dict[str, Any]]:
    """Skill snapshots, newest first.

    These live in a SUBDIRECTORY, so the top-level glob in
    :func:`list_snapshots` has never seen them. That is why 14 of them sat on
    disk with no command able to restore any: skill writes are the most
    frequent mutation this plugin makes, and the only one with no undo.

    The snapshot name is ``<skill-name>__<run-id>__SKILL.md`` and it does NOT
    record the skill's category, so the live file is located by NAME at
    restore time (see :func:`skill_path_by_name`).
    """
    out: List[Dict[str, Any]] = []
    try:
        d = snapshots_dir(data_dir) / SKILLS_SNAPSHOT_SUBDIR
        files = sorted(
            d.glob("*__*"),
            key=lambda p: (p.name.split("__", 2)[1] if "__" in p.name else "",
                           p.stat().st_mtime),
            reverse=True,
        )
    except OSError:
        return out
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        # `<skill>__<run_id>[__<NNN>]__<filename>`. The sequence number was
        # added 2026-09-27: one run can append many entries to the SAME skill,
        # and a shared snapshot name meant each write clobbered the previous
        # snapshot, so an undo replayed the oldest copy and left the rest.
        parts = p.name.split("__")
        skill = parts[0]
        run_id = parts[1] if len(parts) > 1 else ""
        seq = parts[2] if len(parts) > 2 else ""
        out.append({
            "target": SKILLS_SNAPSHOT_SUBDIR,
            "skill": skill,
            "run_id": run_id,
            "seq": seq,
            "path": str(p),
            "name": p.name,
            "bytes": st.st_size,
            "mtime_iso": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)
            ),
        })
    return out


def skill_path_by_name(skills_root: Path, name: str) -> Optional[Path]:
    """Locate a live SKILL.md by directory name, in any category.

    A snapshot records the skill NAME but not its category, so a restore has
    to search. Category is deliberately not part of the key: the whole point
    of the collision fix is that a name can live anywhere in the tree.
    """
    if not name or Path(name).name != name or name in (".", ".."):
        return None
    try:
        for p in Path(skills_root).rglob("SKILL.md"):
            if p.parent.name == name:
                return p
    except OSError:
        return None
    return None


def restore_skill(
    data_dir: Path,
    name: str,
    skills_root: Path,
    keep: int = DEFAULT_RETAIN_SNAPSHOTS,
) -> Dict[str, Any]:
    """Restore a skill file from a pre-write snapshot.

    ``skills_root`` is REQUIRED and passed in, not resolved from the
    environment inside this function. An earlier draft called a
    ``store.skills_root()`` helper, which does not exist, and the shape I
    was reaching for instead -- resolve the tree from ambient env -- is
    exactly the leak vector that once put a test fixture into the live
    USER.md. The caller owns the path so a sandbox cannot reach the real
    tree by accident.

    Snapshots the CURRENT file first, so restoring a wrong snapshot is
    itself reversible -- the same contract :func:`restore` gives for the
    stores.
    """
    import shutil as _shutil

    root = snapshots_dir(data_dir) / SKILLS_SNAPSHOT_SUBDIR
    if not name or Path(name).name != name or name in (".", ".."):
        return {"restored": False, "reason": f"invalid snapshot name {name!r}"}
    src = root / name
    try:
        if root.resolve() not in src.resolve().parents:
            return {"restored": False,
                    "reason": f"snapshot {name!r} is outside {root}"}
    except OSError as exc:
        return {"restored": False, "reason": str(exc)}
    if not src.exists():
        return {"restored": False, "reason": f"no such snapshot {name!r}"}
    skill = name.split("__", 1)[0]
    dest = skill_path_by_name(skills_root, skill)
    if dest is None:
        return {
            "restored": False,
            "reason": (
                f"no live skill named {skill!r} under {skills_root}. The "
                f"snapshot is intact at {src}; copy it back manually if the "
                f"skill was deleted rather than modified."
            ),
        }
    undo: Optional[Path] = None
    try:
        # Snapshot the current state first, so this restore is undoable.
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            run_id = name.split("__")[1] if name.count("__") > 1 else "manual"
            undo = root / f"{skill}__undo-{run_id}__{dest.name}"
            tmp = undo.with_name(undo.name + f".tmp{os.getpid()}")
            _shutil.copy2(dest, tmp)
            os.replace(tmp, undo)
        tmp2 = dest.with_name(dest.name + f".tmp{os.getpid()}")
        _shutil.copy2(src, tmp2)
        os.replace(tmp2, dest)
    except OSError as exc:
        return {"restored": False, "reason": str(exc)}
    out: Dict[str, Any] = {
        "restored": True, "from": str(src), "to": str(dest),
        "bytes": dest.stat().st_size,
    }
    if undo:
        out["undo"] = str(undo)
    return out


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
    src_path = Path(src)
    # The undo snapshot can push the retention cap over, and _prune would
    # then delete the OLDEST snapshot -- which, when the user is restoring
    # exactly that oldest one, is the file about to be read. Verified failure:
    # copy2 raised FileNotFoundError and the only good copy was gone, i.e. the
    # recovery path destroyed the thing it was recovering FROM. `protect`
    # exempts the source from the prune for the duration of this restore.
    try:
        undo = take(data_dir, target, "before-restore", keep=keep,
                    protect=src_path)
    except OSError as exc:
        undo = {"ok": False, "reason": str(exc)}
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with memory_store.file_lock(dest):
            tmp = dest.with_name(dest.name + f".restore{os.getpid()}")
            # copy the BYTES, then re-apply the source's mtime: a failed
            # copy2 can otherwise leave a partial temp file behind.
            shutil.copyfile(src_path, tmp)
            try:
                shutil.copystat(src_path, tmp)
            except OSError:
                pass
            os.replace(tmp, dest)
    except OSError as exc:
        return {
            "restored": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "undo": undo,
        }
    # No prune after the copy. The restore already made the source
    # unnecessary, but deleting it removes the only evidence of what was
    # restored and leaves the retention count permanently one over the cap
    # until the next write. The cap is enforced on the next take().
    return {
        "restored": True,
        "target": target,
        "from": str(src),
        "to": str(dest),
        "undo_snapshot": (undo or {}).get("path"),
    }
