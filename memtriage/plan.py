"""Triage plan: the structured output from Cerveau plus validation/rendering.

A plan is a JSON list of actions. Each action is one of the Cerveau-routed
decisions. The executor and the human-readable report both consume the plan;

Actions
-------
keep                    preserve the entry unchanged
consolidate             merge several entries into one tighter form
route-to-skill         write the knowledge as a new SKILL.md
route-to-profile       promote a fact into the user profile (USER.md)
route-to-provider      persist a rich fact as a scene block via the gateway
route-to-script        write a runnable script (+ optional cron registration)
evict-to-quarantine     move a stale entry to quarantine (reversible)
delete                  hard-delete an entry (only after quarantine grace)
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

VALID_ACTIONS = (
    "keep",
    "consolidate",
    "route-to-skill",
    "route-to-profile",
    "route-to-provider",
    "route-to-script",
    "evict-to-quarantine",
)

# Consolidation is the one action that must reference two or more entries by
# index; all other actions reference at most one.
CONSOLIDATION_KINDS = ("consolidate",)


class PlanValidationError(ValueError):
    """Raised when a plan received from Cerveau violates the contract."""


def _first_json_array(text: str) -> str:
    """Extract the first balanced JSON array from ``text``.

    Scans for the first ``[``, then walks brackets (strings-aware enough for
    JSON: honors escaped quotes) to find its matching ``]``, returning the
    exact substring. Raises PlanValidationError when unbalanced/missing.
    """
    start = text.find("[")
    if start == -1:
        raise PlanValidationError("No JSON action array found in Cerve reply.")
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise PlanValidationError("Unbalanced JSON array in Cerve reply.")


def parse_plan(raw: str) -> List[Dict[str, Any]]:
    """Extract and parse a JSON action list from Cerve's reply text.

    Tolerant extraction: strips markdown fences, pulls the first balanced JSON
    array, and validates the result against the action contract.
    """
    text = raw.strip()
    if "```" in text:
        import re

        fences = re.findall(r"```(?:json)?\s+(.*?)```", text, re.DOTALL)
        text = fences[0] if fences else text
    candidate = _first_json_array(text)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise PlanValidationError(f"Malformed JSON in Cerve reply: {exc}") from exc
    if not isinstance(parsed, list):
        raise PlanValidationError("Cerve reply must contain a JSON array.")
    return validate(parsed)


def validate(actions: List[Any]) -> List[Dict[str, Any]]:
    """Validate a list of action dicts; raises on contract violations."""
    if not isinstance(actions, list):
        raise PlanValidationError("Plan must be a JSON array of actions.")
    out: List[Dict[str, Any]] = []
    for n, a in enumerate(actions):
        if not isinstance(a, dict):
            raise PlanValidationError(f"Action #{n} is not an object.")
        kind = a.get("action")
        if kind not in VALID_ACTIONS:
            raise PlanValidationError(
                f"Action #{n} has invalid action {kind!r}."
            )
        if kind == "consolidate":
            entries = a.get("entries", [])
            if not isinstance(entries, list) or len(entries) < 2:
                raise PlanValidationError(
                    f"consolidate action #{n} needs ≥2 entry indices."
                )
            if not a.get("text"):
                raise PlanValidationError(f"consolidate action #{n} needs 'text'.")
        out.append(dict(a))
    return out


def render_report(
    plan: List[Dict[str, Any]],
    *,
    usage_before: Dict[str, Any],
    run_id: str,
) -> str:
    """Render a human-readable report (English-only) for review/audit."""
    lines: List[str] = []
    lines.append(f"# Memory triage report — {run_id}")
    for t in usage_before.get("memory", []):
        lines.append(
            f"- {t['target']}: {t['current']:,}/{t['limit']:,} chars "
            f"({t['fraction']*100:.0f}%)"
        )
    lines.append("")
    if not plan:
        lines.append("No actions required.")
        return "\n".join(lines)
    lines.append(f"{len(plan)} action(s):")
    for a in plan:
        kind = a["action"]
        target = a.get("target")
        reason = (a.get("reason") or "").strip()
        text = (a.get("text") or a.get("summary") or "").strip()
        head = f"- [{kind}]"
        if target:
            head = f"- [{kind} -> {target}]"
        detail = text if len(text) <= 110 else text[:107] + "..."
        lines.append(f"{head} {detail}")
        if reason:
            lines.append(f"    reason: {reason}")
        if kind == "route-to-skill" and a.get("skill_name"):
            lines.append(f"    skill: {a['skill_name']}")
        if kind == "route-to-script" and a.get("script_name"):
            lines.append(f"    script: {a['script_name']}")
    return "\n".join(lines)


def save_report(cfg, run_id: str, report: str) -> str:
    """Persist a report file; returns its path."""
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.reports_dir / f"report-{run_id}.md"
    path.write_text(report, encoding="utf-8")
    return str(path)


def save_plan(cfg, run_id: str, plan: List[Dict[str, Any]]) -> str:
    """Persist the plan JSON; returns its path (for review/apply loops)."""
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.reports_dir / f"plan-{run_id}.json"
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def load_plan(cfg, run_id: str) -> List[Dict[str, Any]]:
    """Load a saved plan JSON for review/apply."""
    path = cfg.reports_dir / f"plan-{run_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"No saved plan for run {run_id}")
    return validate(json.loads(path.read_text(encoding="utf-8")))