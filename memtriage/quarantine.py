"""Quarantine: reversible eviction.

Eviction is never destructive. When an entry is judged stale, its full text is
appended (with enough metadata to restore it to its original target and
position) to a quarantine file under the plugin data dir. Only an explicit
purge — after quarantine_days — deletes it for good.

The quarantine is a JSONL file, one record per evicted entry.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

QUARANTINE_FILENAME = "quarantine.jsonl"


def _file(cfg) -> Path:
    return cfg.quarantine_dir / QUARANTINE_FILENAME


def evict(cfg, *, target: str, text: str, reason: str, run_id: str) -> Dict[str, Any]:
    """Move an entry into quarantine. Returns the record written."""
    cfg.quarantine_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "target": target,
        "text": text,
        "reason": reason,
        "run_id": run_id,
        "evicted_at": int(time.time()),
        "evicted_at_iso": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        ),
    }
    with open(_file(cfg), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def all_evicted(cfg) -> list[Dict[str, Any]]:
    path = _file(cfg)
    if not path.exists():
        return []
    out: list[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def restore(cfg, text: str) -> bool:
    """Restore a previously-evicted entry by exact text match.

    Only restores entries that are still under their quarantine grace window
    (evicted within ``quarantine_days``). Returns True when restored.
    """
    cfg_meta = getattr(cfg, "quarantine_days", 7)
    window = float(cfg_meta) * 86400.0
    record, idx = None, None
    for i, rec in enumerate(all_evicted(cfg)):
        if rec.get("text") == text:
            record, idx = rec, i
            break
    if record is None or idx is None:
        return False
    evicted_at = float(record.get("evicted_at", 0))
    if time.time() - evicted_at > window:
        return False
    from . import store as memory_store

    result = memory_store.append_entry(record.get("target", "memory"), text)
    if not result.get("success", False):
        return False
    _remove_the_line(cfg, [idx])
    return True


def purge_expired(cfg) -> int:
    """Purge evicted entries whose grace window has passed. Returns count."""
    entries = all_evicted(cfg)
    window = float(getattr(cfg, "quarantine_days", 7)) * 86400.0
    now = time.time()
    keep, removed = [], 0
    for rec in entries:
        if now - float(rec.get("evicted_at", 0)) > window:
            removed += 1
        else:
            keep.append(rec)
    _rewrite_file_objs(cfg, keep)
    return removed


def _remove_the_line(cfg, indices: list[int]) -> None:
    path = _file(cfg)
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    new_lines = [l for i, l in enumerate(lines) if i not in set(indices)]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
    os.replace(tmp, path)


def _rewrite_file_objs(cfg, records: list[Dict[str, Any]]) -> None:
    path = _file(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    body = "\n".join(
        json.dumps(r, ensure_ascii=False) for r in records
    )
    tmp.write_text(body + ("\n" if body else ""), encoding="utf-8")
    os.replace(tmp, path)