"""Bounded growth for the plugin's own state.

Three structures grew without limit on the live install:

* ``reports/`` — one markdown file per run. 520 files / 840KB observed.
* ``state.json``'s ``notified_runs`` — every run id ever notified, with each
  ``mark_notified`` rewriting the whole ~16KB file. 475 entries observed.
* ``ledger.json`` — one row per routed item. 73 rows observed.

None of these had any pruning function anywhere in the codebase. Because the
Cerveau prompt is a single argv element, unbounded growth does not degrade
gracefully: it eventually crosses ``MAX_ARG_STRLEN`` (131072) and the
dispatch dies with ``OSError[E2BIG]``.

The oldest N records are kept. Reports are pruned by mtime because run ids
sort lexicographically, and mtime is what actually reflects recency.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config


def prune_reports(cfg: Config, keep: Optional[int] = None) -> int:
    """Keep the ``keep`` newest report files; delete the rest. Returns count."""
    keep = cfg.retain_reports if keep is None else keep
    if keep < 0:
        return 0
    d: Path = cfg.reports_dir
    if not d.is_dir():
        return 0
    try:
        files = [p for p in d.iterdir() if p.is_file() and p.suffix in (".md", ".json")]
    except OSError:
        return 0
    if len(files) <= keep:
        return 0
    try:
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    removed = 0
    for p in files[keep:]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            # A report we cannot delete is a nuisance, not a failure: the
            # triage run that produced it already succeeded.
            continue
    return removed


def prune_notified_runs(cfg: Config, keep: Optional[int] = None) -> int:
    """Trim ``state.json``'s ``notified_runs`` to the newest ``keep`` ids."""
    keep = cfg.retain_notified_runs if keep is None else keep
    if keep < 0:
        return 0
    path = cfg.state_path
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return 0
    runs = data.get("notified_runs")
    if not isinstance(runs, list) or len(runs) <= keep:
        return 0
    trimmed = list(runs)[-keep:]
    data["notified_runs"] = trimmed
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, path)
    except OSError:
        return 0
    return len(runs) - len(trimmed)


def prune_ledger(cfg: Config, keep: Optional[int] = None) -> int:
    """Trim the ledger to the newest ``keep`` rows."""
    from . import ledger as ledger_mod  # local import: avoids a cycle

    keep = cfg.retain_ledger_rows if keep is None else keep
    if keep < 0:
        return 0
    rows = ledger_mod.load(cfg)
    if len(rows) <= keep:
        return 0
    try:
        ledger_mod._write(cfg, rows[-keep:])
    except OSError:
        return 0
    return len(rows) - keep


def enforce_all(cfg: Config) -> Dict[str, int]:
    """Run every pruner. Returns per-kind counts for the run summary."""
    return {
        "reports": prune_reports(cfg),
        "notified_runs": prune_notified_runs(cfg),
        "ledger": prune_ledger(cfg),
    }
