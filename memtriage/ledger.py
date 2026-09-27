"""Routing ledger + provenance.

Every artifact Cerveau routes to a durable destination is recorded here with
the source triage run and a timestamp.  Future triage runs receive the ledger
so the decision model never re-routes what already exists elsewhere, and so a
user (or another agent) can audit where any fact went.

The ledger is a simple JSON list in the plugin data dir, written atomically.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config


def _load(cfg: Config) -> List[Dict[str, Any]]:
    path = cfg.ledger_path
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write(cfg: Config, ledger: List[Dict[str, Any]]) -> None:
    path = cfg.ledger_path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def record(
    cfg: Config,
    *,
    kind: str,
    destination: str,
    summary: str,
    run_id: str,
    provenance: str,
) -> None:
    """Record one routed artifact.

    A re-run of the SAME (kind, destination) with the SAME run_id is an
    idempotent overwrite. A DIFFERENT run_id appends a new record instead of
    erasing the previous one — keying only on destination meant every later
    consolidation overwrote the earlier run's audit record, and
    ``already_routed`` then permanently reported the content as handled.
    """
    ledger = _load(cfg)
    entry = {
        "kind": kind,  # skill | user | provider | script | consolidate
        "destination": destination,
        "summary": summary,
        "run_id": run_id,
        "provenance": provenance,
        "routed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    ledger = [
        r for r in ledger
        if not (r.get("destination") == destination and r.get("run_id") == run_id)
    ]
    ledger.append(entry)
    _write(cfg, ledger)


def already_routed(cfg: Config, destination: str) -> bool:
    """True when a ledger entry already exists for a destination."""
    return any(r.get("destination") == destination for r in load(cfg))


def load(cfg: Config) -> List[Dict[str, Any]]:
    return _load(cfg)


def summary(cfg: Config) -> str:
    """Human-readable summary line of what has been routed so far."""
    ledger = _load(cfg)
    if not ledger:
        return "No routes recorded yet."
    return "; ".join(
        f"{r.get('kind', '?')}->{r.get('destination', '?')}" for r in ledger
    )