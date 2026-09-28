"""Triage state: cooldown bookkeeping and the awaiting-approval flag.

``state.json`` holds:
* ``last_triage_at`` — epoch seconds of the last completed triage run (used
  by the auto-trigger hooks to avoid re-triaging on every write),
* ``awaiting_approval`` — run id of a manual-mode plan waiting for review.

Written atomically (temp + os.replace) under a lock: concurrent writers
previously shared one temp name, so of 60 concurrent ``mark_notified`` calls
only 18 survived and 31 raised FileNotFoundError. See :mod:`atomicio`.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

from . import atomicio
from .config import Config


def _load(cfg: Config) -> Dict[str, Any]:
    return atomicio._read(cfg.state_path)


def _save(cfg: Config, state: Dict[str, Any]) -> None:
    atomicio.write_json_atomic(cfg.state_path, state)


def _update(cfg: Config, mutate) -> None:
    """Read-modify-write state.json under a lock."""
    atomicio.read_modify_write(cfg.state_path, mutate)


def mark_triage(cfg: Config, run_id: str) -> None:
    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        state["last_triage_at"] = int(time.time())
        state["last_run_id"] = run_id
        state.pop("awaiting_approval", None)
        return state
    _update(cfg, mutate)


def mark_awaiting_approval(cfg: Config, run_id: str) -> None:
    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        # Never clobber a DIFFERENT pending plan: an auto-run firing while a
        # plan awaits review used to overwrite it, orphaning plan-<id>.json
        # on disk with nothing pointing at it (unapprovable, unreported).
        pending = state.get("awaiting_approval")
        if pending and pending != run_id:
            return None
        state["awaiting_approval"] = run_id
        # When it was raised, so it can EXPIRE. Without this a single
        # unapproved plan wedged auto mode permanently: the hook skips
        # whenever `awaiting_approval` is set, `clear_awaiting()` had zero
        # call sites in the codebase, and nothing else ever removed the key.
        # The store stayed over budget with the plugin reporting nothing.
        state["awaiting_approval_at"] = time.time()
        return state
    _update(cfg, mutate)


def awaiting_approval_expired(
    cfg: Config, max_age_minutes: Optional[int] = None, now: Optional[float] = None
) -> bool:
    """True when a pending plan has sat unreviewed past the staleness window.

    The window defaults to the cooldown, so an unattended run retries at
    roughly the cadence it would have used anyway. Expiry does NOT delete the
    plan or discard it silently: :func:`clear_awaiting` is called by the
    caller, which also reports it, so the wedge is visible rather than
    merely gone.

    A pending plan with NO timestamp is treated as expired: it predates this
    bookkeeping (or was written by hand), and a plan nobody can date is
    exactly the case where silently keeping the brake on forever is wrong.
    """
    if awaiting_approval(cfg) is None:
        return False
    if max_age_minutes is None:
        max_age_minutes = cfg.cooldown_minutes
    if max_age_minutes <= 0:
        return False
    st = _load(cfg)
    set_at = st.get("awaiting_approval_at")
    if set_at is None:
        return True
    now = time.time() if now is None else now
    return (now - float(set_at)) >= max_age_minutes * 60


def awaiting_approval(cfg: Config) -> Optional[str]:
    return _load(cfg).get("awaiting_approval")


def clear_awaiting(cfg: Config) -> None:
    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        state.pop("awaiting_approval", None)
        return state
    _update(cfg, mutate)


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
    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        runs = list(state.get("notified_runs", []) or [])
        if run_id not in runs:
            runs.append(run_id)
        state["notified_runs"] = runs
        return state
    _update(cfg, mutate)


def record_execution(cfg: Config, run_id: str, summary: Any) -> None:
    """Persist the outcome of an applied plan (for post-execution notice)."""
    def mutate(state: Dict[str, Any]) -> Dict[str, Any]:
        state["last_execution"] = {
            "run_id": run_id,
            "summary": summary,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        return state
    _update(cfg, mutate)


def last_execution(cfg: Config) -> Optional[Dict[str, Any]]:
    """The most recently applied plan's execution summary, if any."""
    return _load(cfg).get("last_execution")