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
from typing import Any, Dict, List, Optional

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

# Routing actions promote knowledge OUT of the working store. They may target
# EITHER "memory" (working agent notes) or "user" (the user profile) — a
# profile entry holding reusable/doctrine knowledge can be routed to a skill
# or provider just like a memory entry. ONLY "route-to-profile" is restricted
# to "memory": routing a profile fact "back into the profile" is a no-op.
ROUTING_KINDS = (
    "route-to-skill",
    "route-to-provider",
    "route-to-script",
)


class PlanValidationError(ValueError):
    """Raised when a plan received from Cerveau violates the contract."""


def _array_spans(text: str) -> List[str]:
    """Yield every balanced JSON array substring, in order of appearance.

    Non-overlapping scan: after finding a balanced ``[...]`` block, scanning
    resumes just past it. Arrays nested inside a yielded block are not re-yield
    (the outer block is what json.loads cares about); nested arrays within the
    plan's own objects are handled by the outer balanced scan, which takes the
    whole top-level array.
    """
    spans: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "[":
            depth = 0
            in_string = False
            escape = False
            j = i
            while j < n:
                c = text[j]
                if in_string:
                    if escape:
                        escape = False
                    elif c == "\\":
                        escape = True
                    elif c == '"':
                        in_string = False
                    j += 1
                    continue
                if c == '"':
                    in_string = True
                elif c == "[":
                    depth += 1
                elif c == "]":
                    depth -= 1
                    if depth == 0:
                        spans.append(text[i : j + 1])
                        i = j + 1
                        break
                j += 1
            if depth != 0:
                break  # unbalanced from here; give up scanning
        else:
            i += 1
    return spans


def parse_plan(raw: str) -> List[Dict[str, Any]]:
    """Extract and parse a JSON action list from Cerve's reply text.

    Robust extraction: strips markdown fences, then tries every balanced JSON
    array in the reply (the model may include reasoning prose before the plan)
    and returns the first array that parses AND validates as a plan of actions.
    Stray control characters inside strings are repaired first.
    """
    text = raw.strip()
    if "```" in text:
        import re

        fences = re.findall(r"```(?:json)?\s+(.*?)```", text, re.DOTALL)
        text = fences[0] if fences else text
    valid: List[List[Dict[str, Any]]] = []
    last_err: Optional[Exception] = None
    for candidate in _array_spans(text):
        try:
            parsed = json.loads(_auto_escape_controls_in_strings(candidate))
            if isinstance(parsed, list) and parsed:
                validated = validate(parsed)
                valid.append(validated)  # an empty valid list is useless — skip
        except PlanValidationError as exc:
            last_err = exc
        except json.JSONDecodeError as exc:
            last_err = exc
    if valid:
        return valid[-1]  # the model commits to the plan at the END of its reply
    raise PlanValidationError(
        f"No non-empty action array in Cerve reply"
        + (f" ({last_err})" if last_err else "")
    )


def _auto_escape_controls_in_strings(s: str) -> str:
    """Repair unescaped control characters inside JSON string literals.

    LLMs sometimes emit raw newlines/tabs inside a JSON string instead of the
    escaped form (``\\n``/``\\t``/``\\r``). ``json.loads`` rejects those.
    Walk the string, and while inside a string literal, convert any literal
    control character to its escaped form. Whitespace BETWEEN tokens is left
    untouched (outside strings).
    """
    out: list[str] = []
    in_string = False
    escaped = False
    mapping = {"\n": "\\n", "\t": "\\t", "\r": "\\r"}
    for ch in s:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string and ch in mapping:
            out.append(mapping[ch])
            continue
        out.append(ch)
    return "".join(out)


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
        # TARGET RULE: routing actions promote knowledge OUT of the working
        # store. "route-to-profile" must target "memory" (an already-profiled
        # entry is not something to route back into the profile). The other
        # routing actions may target "memory" OR "user" — profile entries
        # holding reusable doctrine can be routed to a skill or provider like
        # any memory entry (this is what lets triage relieve an over-full user
        # profile).
        if kind == "route-to-profile" and a.get("target", "memory") != "memory":
            raise PlanValidationError(
                f"Action #{n} ({kind}) must target 'memory', got "
                f"{a.get('target')!r}."
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


def render_impact(execution: Dict[str, Any]) -> List[str]:
    """Per-target before→after impact in percentage points.

    Consumes the ``before``/``after`` usage snapshots the executor records,
    and renders how much the working store actually changed — the plugin's
    real impact — in percentages, not raw char counts.
    """
    before = execution.get("before") or {}
    after = execution.get("after") or {}
    out: List[str] = []
    for target in ("memory", "user"):
        a = after.get(target)
        if not a:
            continue
        after_pct = a.get("fraction", 0.0) * 100
        b = before.get(target)
        if b:
            before_pct = b.get("fraction", 0.0) * 100
            delta_pp = before_pct - after_pct
            out.append(
                f"{target}: {before_pct:.0f}% -> {after_pct:.0f}% "
                f"({delta_pp:+.0f}pp freed)"
            )
        else:
            out.append(f"{target}: {after_pct:.0f}%")
    return out


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