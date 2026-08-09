"""Triage state: cooldown bookkeeping and the awaiting-approval flag.

``state.json`` holds:
* ``last_triage_at`` — epoch seconds of the last completed triage run (used
  by the auto-trigger hooks to avoid re-triaging on every write),
* ``awaiting_approval`` — run id of a manual-mode plan waiting for review.

Written atomically (temp + os.replace).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

from .config import Config


def _load(cfg: Config) -> Dict[str, Any]:
    path = cfg.state_path
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save(cfg: Config, state: Dict[str, Any]) -> None:
    path = cfg.state_path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def mark_triage(cfg: Config, run_id: str) -> None:
    state = _load(cfg)
    state["last_triage_at"] = int(time.time())
    state["last_run_id"] = run_id
    state.pop("awaiting_approval", None)
    _save(cfg, state)


def mark_awaiting_approval(cfg: Config, run_id: str) -> None:
    state = _load(cfg)
    state["awaiting_approval"] = run_id
    _save(cfg, state)


def awaiting_approval(cfg: Config) -> Optional[str]:
    return _load(cfg).get("awaiting_approval")


def clear_awaiting(cfg: Config) -> None:
    state = _load(cfg)
    state.pop("awaiting_approval", None)
    _save(cfg, state)


def last_triage_at(cfg: Config) -> Optional[int]:
    value = _load(cfg).get("last_triage_at")
    return int(value) if value else None


def is_over_threshold(cfg: Config) -> bool:
    """True when any memory target usage fraction >= configured threshold."""
    from . import inventory

    for target in inventory.inventory_memory(cfg):
        if target["fraction"] >= cfg.threshold_percent:
            return True
    return False


def cooldown_active(cfg: Config, now: Optional[float] = None) -> bool:
    """True when the last triage ran within the cooldown window."""
    last = last_triage_at(cfg)
    if last is None:
        return False
    now = time.time() if now is None else now
    return (now - last) < cfg.cooldown_minutes * 60


def notified_runs(cfg: Config) -> list:
    """Run ids whose report has already been surfaced in a session."""
    return list(_load(cfg).get("notified_runs", []) or [])


def mark_notified(cfg: Config, run_id: str) -> None:
    """Record that a run's report was injected into a conversation."""
    state = _load(cfg)
    runs = list(state.get("notified_runs", []) or [])
    if run_id not in runs:
        runs.append(run_id)
    state["notified_runs"] = runs
    _save(cfg, state)


def record_execution(cfg: Config, run_id: str, summary: Any) -> None:
    """Persist the outcome of an applied plan (for post-execution notice)."""
    st = _load(cfg)
    st["last_execution"] = {
        "run_id": run_id,
        "summary": summary,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _save(cfg, st)


def last_execution(cfg: Config) -> Optional[Dict[str, Any]]:
    """The most recently applied plan's execution summary, if any."""
    return _load(cfg).get("last_execution")