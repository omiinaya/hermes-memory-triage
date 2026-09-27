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

import re

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
    "split",
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
        # Ambiguity is a refusal, not a coin flip. A reply can contain more
        # than one well-formed action array: the real plan plus a recap, or
        # the prompt's own schema example echoed back. The old code returned
        # valid[-1], so a trailing all-keep recap SILENTLY replaced a real
        # routing plan — triage reported success and freed nothing. When two
        # candidates are not obviously the same plan, refuse and let the
        # deterministic fallback run rather than guess.
        if len(valid) == 1:
            return valid[0]
        # Prefer a strictly longer plan: the real plan always covers at least
        # as many entries as a recap of it.
        by_len = sorted(valid, key=len, reverse=True)
        if len(by_len[0]) > len(by_len[1]):
            return by_len[0]
        # Genuinely ambiguous (equal length, different content) — do not pick.
        raise PlanValidationError(
            f"Ambiguous Cerve reply: {len(valid)} distinct action arrays of "
            f"equal length; refusing to guess. The model emitted a plan and "
            f"then restated it differently."
        )
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
    seen_touched: set = set()
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
            # Distinct ints, not just length: entries=[0, 0] satisfied the old
            # len>=2 check and then replaced a single real entry with the
            # "merged" text while claiming a two-entry merge.
            if any(isinstance(i, bool) or not isinstance(i, int) for i in entries):
                raise PlanValidationError(
                    f"consolidate action #{n} entries must all be ints, "
                    f"got {entries!r}."
                )
            if len(set(entries)) < 2:
                raise PlanValidationError(
                    f"consolidate action #{n} needs ≥2 DISTINCT entry indices, "
                    f"got {entries!r} — a repeated index is not a merge."
                )
            if not a.get("text"):
                raise PlanValidationError(f"consolidate action #{n} needs 'text'.")
        if kind == "split":
            if not isinstance(a.get("routes"), list) or not a.get("routes"):
                raise PlanValidationError(
                    f"split action #{n} needs at least one route in 'routes'."
                )
            if not a.get("keep"):
                raise PlanValidationError(f"split action #{n} needs 'keep'.")

        # One mutating action per (target, index): a plan that both routed
        # entry #0 to a skill and consolidated it applied twice, leaving the
        # merged text appended beside the original it was meant to replace.
        if kind not in ("keep",):
            idx = a.get("index")
            if kind == "consolidate":
                touched = {(a.get("target", "memory"), i) for i in (a.get("entries") or [])}
            elif isinstance(idx, int) and not isinstance(idx, bool):
                touched = {(a.get("target", "memory"), idx)}
            else:
                touched = set()
            for key in touched:
                if key in seen_touched:
                    raise PlanValidationError(
                        f"Action #{n} ({kind}) touches {key[0]}#{key[1]}, "
                        f"which an earlier action already mutates. One action "
                        f"per entry."
                    )
                seen_touched.add(key)
        _reject_placeholder_content(a, n, kind)
        out.append(dict(a))
    return out


# Cerveau is shown a worked EXAMPLE containing angle-bracket placeholders
# ("<the deploy/repo clauses>"). It sometimes copies them back verbatim as
# real routed content, and the executor then WRITES that literal to a SKILL.md,
# to a script, or into the provider store. 2026-09-27: a live run created
# /root/.hermes/skills/tools/team-doctrine/SKILL.md whose entire body was
# "<the deploy/repo clauses>". Shape validation cannot catch this — the
# placeholder is a perfectly well-formed string.
_PLACEHOLDER_RE = re.compile(r"^<[^<>\n]{1,120}>$")


def _looks_like_placeholder(text: str) -> bool:
    """True for a whole-string angle-bracket placeholder like ``<the ...>``.

    Deliberately narrow: a real entry that merely CONTAINS a bracketed phrase
    ("set DIM=<name> in the config") is legitimate content and must pass.
    """
    s = (text or "").strip()
    if not s:
        return True
    return bool(_PLACEHOLDER_RE.match(s))


def _reject_placeholder_content(a: Dict[str, Any], n: int, kind: str) -> None:
    """Refuse a plan whose routed text is a prompt placeholder, not real content.

    Refusing the whole plan is deliberate: a plan that shipped one placeholder
    is a plan the model did not actually read the store to write, and applying
    the rest of it would write half-understood content.
    """
    fields = ["text", "summary", "keep"]
    for key in ("routes",):
        for r in (a.get(key) or []):
            if isinstance(r, dict):
                for f in fields:
                    if f in r and _looks_like_placeholder(str(r.get(f) or "")):
                        raise PlanValidationError(
                            f"Action #{n} route carries a PROMPT PLACEHOLDER in "
                            f"{f!r} ({str(r.get(f))[:60]!r}) rather than real content — "
                            f"Cerveau echoed the example from the prompt. Refusing "
                            f"the plan instead of writing placeholder text to disk."
                        )
    for f in fields:
        if f in a and _looks_like_placeholder(str(a.get(f) or "")):
            raise PlanValidationError(
                f"Action #{n} ({kind}) carries a PROMPT PLACEHOLDER in {f!r} "
                f"({str(a.get(f))[:60]!r}) rather than real content — Cerveau "
                f"echoed the example from the prompt. Refusing the plan."
            )


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