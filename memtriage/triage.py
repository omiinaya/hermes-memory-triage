"""Triage orchestrator: the single entry point for every trigger.

    run_triage(cfg, reason, force=False)

Flow:
1. (optional) check usage against threshold (skipped when ``force``),
2. build the inventory + ledger payload,
3. dispatch Cerveau, parse + validate the plan,
4. manual mode: persist report + mark awaiting approval,
   auto mode: execute immediately and write the report after.

Always returns a result dict with the run id, mode, usage before, plan,
report path and (in auto mode) the execution summary.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List

from . import cerveau as cerveau_mod
from . import inventory as inventory_mod
from . import ledger as ledger_mod
from . import plan as plan_mod
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
        actions = cerveau_mod.dispatch(cfg, prompt)
    else:
        actions = []  # tests inject via execute directly

    report = plan_mod.render_report(
        actions, usage_before={"memory": usage_before}, run_id=run_id
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
    }

    if cfg.mode == "auto":
        provenance = f"session:auto triage {run_id} ({reason})"
        summary = Executor(cfg).execute_plan(actions, run_id, provenance)
        state_mod.record_execution(cfg, run_id, summary)
        result["execution"] = summary
        state_mod.mark_triage(cfg, run_id)
    else:
        plan_mod.save_plan(cfg, run_id, actions)
        state_mod.mark_awaiting_approval(cfg, run_id)
        result["execution"] = None
        result["message"] = (
            f"Triage plan ready ({len(actions)} actions). Review the report and "
            f"approve or edit it before applying."
        )
    return result


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
