"""Triage orchestrator: the single entry point for every trigger.

    run_triage(cfg, reason, force=False)

Flow:
1. (optional) check usage against threshold (skipped when ``force``),
2. build the inventory + ledger payload,
3. dispatch Cerveau (or its deterministic fallback), parse + validate the plan,
4. manual mode: persist report + mark awaiting approval,
   auto mode: execute immediately and write the report after.

Always returns a result dict with the run id, mode, usage before, plan,
report path and (in auto mode) the execution summary.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List

import logging

_log = logging.getLogger(__name__)

from . import cerveau as cerveau_mod
from . import executor as executor_mod
from . import inventory as inventory_mod
from . import ledger as ledger_mod
from . import learning as learning_mod
from . import plan as plan_mod
from . import retention as retention_mod
from . import state as state_mod
from .config import Config
from .executor import Executor


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-", time.gmtime()) + uuid.uuid4().hex[:8]


def run_triage(
    cfg: Config,
    reason: str,
    *,
    force: bool = False,
    dispatch: bool = True,
) -> Dict[str, Any]:
    """Run a full triage pass. Raises on Cerveau/plan failure.

    ``dispatch=False`` is for tests that want to inject a plan instead of
    invoking the Cerveau profile.
    """
    run_id = new_run_id()
    usage_before = inventory_mod.inventory_memory(cfg)

    if not force and not _over_threshold(cfg, usage_before):
        return {
            "run_id": run_id,
            "triggered": False,
            "reason": reason,
            "message": (
                f"Memory below threshold "
                f"({_max_fraction(usage_before)*100:.0f}% < "
                f"{cfg.threshold_percent*100:.0f}%) — no triage needed."
            ),
        }

    inv = inventory_mod.collect(cfg)
    led = ledger_mod.load(cfg)

    if dispatch:
        prompt = cerveau_mod.build_prompt(cfg, inv, led)
        actions = cerveau_mod.dispatch(cfg, prompt, inventory=inv)
    else:
        actions = []  # tests inject via execute directly
    fallback_used = any(
        a.get("_source") == "deterministic-fallback" for a in actions
    )

    # C2 (2026-09-28). A deterministic fallback is a NO-OP: every action is a
    # `keep`, so a run that never consulted Cerveau is byte-identical in
    # effect to one that consulted it and decided to change nothing. The only
    # signal was `dispatcher`, computed here and read by exactly one test --
    # grep-confirmed to appear in no report, no notification, no decision log
    # and no state. Two unattended runs (16:49, 16:50) were exactly this.
    #
    # So the fallback is now stated in all three places an operator or an
    # auditor actually reads: the report header, the decision log, and the
    # persisted state. A fallback that cannot be seen is a silent failure.
    if fallback_used and dispatch:
        _log.warning(
            "run %s: Cerveau was NOT consulted -- deterministic fallback in "
            "use; every action is a no-op keep. The model reply was missing, "
            "unparseable, or rejected.",
            run_id,
        )
        # The decision log is the record Omar asked for: a run that consulted
        # the model and a run that never did must be distinguishable there.
        # Written before the executor, because the executor's own log starts
        # with per-action lines that look identical either way.
        executor_mod.log_decision(
            run_id,
            "blocked",
            "Cerveau was NOT consulted; this run used the deterministic "
            "all-keep fallback, so no entry was routed and nothing changed. "
            "This is not a judgement that the store needed no triage.",
            None,
            cfg,
        )
        kept = sum(1 for a in actions if a.get("action") == "keep")
        actions = list(actions)
        for a in actions:
            a.setdefault("reason", "")
            a["reason"] = (
                f"[FALLBACK: Cerveau was not consulted; this is an automatic "
                f"no-op, not a judgement about this entry] {a['reason']}"
            ).strip()
        fallback_note = (
            f"WARNING: Cerveau was not consulted for this run. The plan below "
            f"is the deterministic fallback ({kept} of {len(actions)} action(s) "
            f"are no-op keeps), NOT a model decision. Nothing was routed."
        )
    else:
        fallback_note = ""

    report = plan_mod.render_report(
        actions,
        usage_before={"memory": usage_before},
        run_id=run_id,
        fallback_note=fallback_note,
    )
    report_path = plan_mod.save_report(cfg, run_id, report)

    result: Dict[str, Any] = {
        "run_id": run_id,
        "triggered": True,
        "reason": reason,
        "mode": cfg.mode,
        "usage_before": usage_before,
        "plan": actions,
        "report_path": report_path,
        "dispatcher": ("deterministic-fallback" if fallback_used else "cerveau")
        if dispatch
        else "injected",
    }

    if cfg.mode == "auto":
        provenance = f"session:auto triage {run_id} ({reason})"
        # C2: record the fallback in PERSISTED STATE, not just the returned
        # dict. `result` is read by the plugin for the notification, but the
        # notification is not what an operator greps three days later. State
        # is what `mem_triage status` and the decision log show, so a run
        # that silently did nothing is visible without re-reading code.
        if fallback_used and dispatch:
            state_mod.note_fallback(cfg, run_id)
        # Persist the plan EVEN THOUGH it is applied unattended. The manual
        # branch saves it for review; the auto branch used to save nothing, so
        # an unattended run left a report describing actions that no longer
        # existed anywhere on disk and could not be audited or replayed. The
        # applied-plan marker still refuses a replay, so this is a record,
        # not an invitation to re-run it.
        plan_mod.save_plan(cfg, run_id, actions)
        summary = Executor(cfg).execute_plan(actions, run_id, provenance)
        state_mod.record_execution(cfg, run_id, summary)
        result["execution"] = summary
        state_mod.mark_triage(cfg, run_id)
        _close_learning_loop(cfg, run_id, summary, actions)
    else:
        plan_mod.save_plan(cfg, run_id, actions)
        state_mod.mark_awaiting_approval(cfg, run_id)
        result["execution"] = None
        result["message"] = (
            f"Triage plan ready ({len(actions)} actions). Review the report and "
            f"approve or edit it before applying."
        )
    # Bounded-growth enforcement. Runs every pass regardless of mode: reports
    # accumulate on manual plans too, and notified_runs grows on every run.
    result["pruned"] = retention_mod.enforce_all(cfg)
    return result


def _close_learning_loop(
    cfg: Config,
    run_id: str,
    summary: Dict[str, Any],
    actions: List[Dict[str, Any]],
) -> None:
    """Record what this run actually did into Cerveau's decision profile.

    ``learning.record_decision`` existed and was fully implemented but was
    never called from anywhere, so the profile's MEMORY.md held 0 learning
    entries after 518 runs — the self-improvement loop was provably dead.
    A model that cannot see its own past decisions cannot improve from them.
    """
    bits: List[str] = []
    applied = summary.get("applied") or []
    if applied:
        bits.append("applied: " + "; ".join(str(a) for a in applied[:4]))
    blocked = summary.get("blocked") or []
    if blocked:
        b = blocked[0]
        bits.append(
            f"REFUSED by identity guard: {b.get('target')}#{b.get('index')} "
            f"({b.get('chars')} chars) — needs a manual split, not a retry"
        )
    errors = summary.get("errors") or []
    if errors:
        bits.append("errors: " + "; ".join(str(e) for e in errors[:3]))
    counts: Dict[str, int] = {}
    for a in actions:
        k = str(a.get("action"))
        counts[k] = counts.get(k, 0) + 1
    if counts:
        bits.append(
            "plan: " + ", ".join(f"{k}×{v}" for k, v in sorted(counts.items()))
        )
    if not bits:
        bits.append("no-op: every entry kept")
    try:
        learning_mod.record_decision(cfg, f"[{run_id}] " + " | ".join(bits))
    except (OSError, ValueError):
        # The learning log is a nice-to-have; never fail a triage over it.
        pass


def apply_plan(
    cfg: Config, plan: List[Dict[str, Any]], run_id: str, provenance: str
) -> Dict[str, Any]:
    """Apply a (possibly user-edited) plan and close the approval cycle."""
    summary = Executor(cfg).execute_plan(plan, run_id, provenance)
    state_mod.mark_triage(cfg, run_id)
    state_mod.record_execution(cfg, run_id, summary)
    return summary


def _over_threshold(cfg: Config, usage_before: List[Dict[str, Any]]) -> bool:
    return any(t["fraction"] >= cfg.threshold_percent for t in usage_before)


def _max_fraction(usage_before: List[Dict[str, Any]]) -> float:
    return max((t["fraction"] for t in usage_before), default=0.0)
