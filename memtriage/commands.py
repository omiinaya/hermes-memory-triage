"""Human-facing command layer shared by the slash command and agent tool.

Every handler returns English-only text (safe to show in a terminal / report).
The plugin surface (plugin/__init__.py) just maps sub-command names to these.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from . import inventory as inventory_mod
from . import ledger as ledger_mod
from . import plan as plan_mod
from . import quarantine as quarantine_mod
from . import snapshots as snapshots_mod
from . import state as state_mod
from . import store as memory_store_mod
from .config import Config
from .triage import apply_plan, run_triage


def cmd_setup(cfg: Config, verify_only: bool = False) -> str:
    """Provision (or verify) the Cerveau decision profile.

    ``setup.py`` was written but never wired into the command surface, so the
    profile could not be created, seeded, or verified from inside the plugin —
    the two tests that exercised it had been failing since it landed.
    """
    from . import setup as setup_mod

    if verify_only:
        report = setup_mod.verify(cfg)
        return f"Cerveau profile '{cfg.cerveau_profile}' — {report}"
    steps = setup_mod.provision(cfg)
    lines = [f"Cerveau setup for '{cfg.cerveau_profile}':"]
    for step, outcome in steps.items():
        lines.append(f"  - {step}: {outcome}")
    return "\n".join(lines)


def cmd_status(cfg: Config) -> str:
    usage = inventory_mod.inventory_memory(cfg)
    lines = [
        f"Threshold: {cfg.threshold_percent*100:.0f}%   Mode: {cfg.mode}",
        f"Quarantine window: {cfg.quarantine_days}d   Cooldown: {cfg.cooldown_minutes}m",
    ]
    for t in usage:
        frac = t["fraction"] * 100
        flag = " (OVER)" if t["fraction"] >= cfg.threshold_percent else ""
        lines.append(f"{t['target']}: {t['current']:,}/{t['limit']:,} chars ({frac:.0f}%){flag}")
    awaiting = state_mod.awaiting_approval(cfg)
    if awaiting:
        lines.append(f"Awaiting approval: run {awaiting} (see reports/)")
    else:
        lines.append("No plan awaiting approval.")
    last = state_mod.last_triage_at(cfg)
    if last:
        import time

        lines.append(f"Last triage: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last))} UTC-epoch {last}")
    return "\n".join(lines)


def cmd_run(cfg: Config, force: bool = False) -> str:
    try:
        result = run_triage(cfg, reason="manual", force=force)
    except Exception as exc:  # noqa: BLE001
        return f"Triage failed: {exc}"
    if not result.get("triggered"):
        return result["message"]
    out = [f"Triage run {result['run_id']} ({result['mode']} mode):",
           f"  Report: {result['report_path']}",
           f"  Actions: {len(result['plan'])}"]
    exec_summary = result.get("execution")
    if exec_summary:
        out += [f"  Impact: {impact}" for impact in plan_mod.render_impact(exec_summary)]
        for a in exec_summary.get("applied", []):
            out.append(f"  applied: {a}")
        for p in exec_summary.get("pending", []):
            out.append(f"  pending: {p}")
        for e in exec_summary.get("errors", []):
            out.append(f"  error: {e}")
    else:
        out.append(result.get("message", ""))
    return "\n".join(out)


def cmd_review(cfg: Config) -> str:
    run_id = state_mod.awaiting_approval(cfg)
    if not run_id:
        return "Nothing awaiting approval."
    try:
        plan = plan_mod.load_plan(cfg, run_id)
    except FileNotFoundError:
        return f"Run {run_id} has no saved plan (already applied?)."
    report = plan_mod.render_report(
        plan, usage_before={"memory": inventory_mod.inventory_memory(cfg)}, run_id=run_id
    )
    return f"Run {run_id} — review:\n\n{report}\n\nRun 'memtriage approve' to apply, or edit plans/{run_id}.json first."


def cmd_approve(cfg: Config) -> str:
    run_id = state_mod.awaiting_approval(cfg)
    if not run_id:
        return "Nothing to approve."
    try:
        plan = plan_mod.load_plan(cfg, run_id)
    except FileNotFoundError as exc:
        return f"Approve failed: {exc}"
    summary = apply_plan(cfg, plan, run_id, provenance=f"session:manual approve {run_id}")
    lines = [f"Applied plan {run_id} — impact:"]
    lines += plan_mod.render_impact(summary) or ["  (no usage snapshots)"]
    for a in summary.get("applied", []):
        lines.append(f"  applied: {a}")
    for p in summary.get("pending", []):
        lines.append(f"  pending: {p}")
    for e in summary.get("errors", []):
        lines.append(f"  error: {e}")
    return "\n".join(lines)


def cmd_snapshots(cfg: Config, target: str = "") -> str:
    """List the pre-write snapshots available to restore from."""
    snaps = snapshots_mod.list_snapshots(
        cfg.data_dir, target or None
    )
    if not snaps:
        return (
            "No snapshots yet. One is taken automatically before every "
            "write that changes a store, so this will populate after the "
            "first mutating triage."
        )
    lines = [f"{len(snaps)} snapshot(s) in {snapshots_mod.snapshots_dir(cfg.data_dir)}:"]
    for s in snaps[:30]:
        lines.append(
            f"- {s['name']}  {s['bytes']} bytes  {s['mtime_iso']}"
        )
    if len(snaps) > 30:
        lines.append(f"  ... and {len(snaps) - 30} more")
    lines.append(
        "Restore with: memtriage restore-file <target> <snapshot-name>"
    )
    return "\n".join(lines)


def cmd_restore_file(cfg: Config, target: str, name: str) -> str:
    """Restore a pre-write snapshot over the live store file."""
    if not target or not name:
        return "Usage: memtriage restore-file <memory|user> <snapshot-name>"
    if target not in ("memory", "user"):
        return f"Unknown target {target!r} (expected 'memory' or 'user')."
    out = snapshots_mod.restore(
        cfg.data_dir, target, name, keep=cfg.retain_snapshots
    )
    if not out.get("restored"):
        return f"Restore failed: {out.get('reason')}"
    after = memory_store_mod.usage(target)
    return (
        f"Restored {target} from {Path(out['from']).name}.\n"
        f"Now {after['current']:,}/{after['limit']:,} chars "
        f"({len(after['entries'])} entries).\n"
        f"The state it replaced was itself snapshotted — undo with: "
        f"memtriage restore-file {target} "
        f"{Path(out['undo_snapshot'] or '').name}"
    )


def cmd_restore(cfg: Config, text: str) -> str:
    if not text:
        return "Usage: memtriage restore <exact evicted text>"
    if quarantine_mod.restore(cfg, text):
        return "Restored entry from quarantine."
    return "Not restored (no exact match, or outside grace window)."


def cmd_purge(cfg: Config) -> str:
    n = quarantine_mod.purge_expired(cfg)
    return f"Purged {n} expired quarantine entr{('y' if n == 1 else 'ies')}."


def cmd_ledger(cfg: Config) -> str:
    led = ledger_mod.load(cfg)
    if not led:
        return "Ledger empty."
    return "\n".join(
        f"- {r.get('kind')} -> {r.get('destination')}  [{r.get('routed_at', '')}]"
        for r in led
    )


def cmd_quarantine(cfg: Config) -> str:
    records = quarantine_mod.all_evicted(cfg)
    if not records:
        return "Quarantine empty."
    lines = [f"{len(records)} evicted entr(s) in quarantine:"]
    for r in records:
        lines.append(f"- [{r.get('target')}] {r.get('evicted_at_iso')} :: {r.get('text', '')[:80]}")
    return "\n".join(lines)


def cmd_config(cfg: Config) -> str:
    return "\n".join(f"{k}: {v}" for k, v in cfg.to_dict().items())