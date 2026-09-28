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


import logging

_log = logging.getLogger(__name__)


def validate_partial(actions: List[Any]) -> Dict[str, Any]:
    """Validate a plan action-by-action, keeping what is sound.

    C1 (2026-09-28). ``validate()`` is all-or-nothing: it raises on the
    FIRST malformed action, and ``parse_plan`` treats that as "this
    candidate is not a plan" and moves on. So one bad action out of fifteen
    discarded the other fourteen -- reproduced without a model in
    scratch/chk_c1_blast_radius.py: 8 evict + 3 route-to-provider + 1
    route-to-skill + 1 split + 1 malformed consolidate produced
    ``PlanValidationError: No non-empty action array in Cerve reply
    (consolidate action #12 needs >=2 entry indices.)`` and the run was
    aborted with nothing freed.

    The blast radius is worse than the malformed action deserves, because
    the *whole plan* is dropped, not just the bad action, and the caller
    then falls back to an all-keep no-op that is indistinguishable from a
    real run (C2).

    What this returns instead:

      * ``ok``     -- the plan is fully valid; use ``clean`` wholesale.
      * ``clean``  -- the individually valid actions, in order.
      * ``dropped``-- descriptions of the rejected ones, for the log.

    Refusing to invent: an action is never repaired, defaulted, or guessed
    at here. It is either wholly valid or wholly dropped. In particular
    the missing-``target`` rule stays absolute -- a cross-store write must
    never be inferred -- so those actions are dropped, not defaulted.
    """
    if not isinstance(actions, list):
        return {
            "ok": False,
            "clean": [],
            "dropped": ["plan is not a JSON array of actions"],
        }

    clean: List[Dict[str, Any]] = []
    dropped: List[str] = []
    seen_touched: set = set()

    for n, a in enumerate(actions):
        try:
            clean.extend(validate([a], _seen=seen_touched))
        except PlanValidationError as exc:
            dropped.append(f"action #{n}: {exc}")

    return {
        "ok": not dropped,
        "clean": clean,
        "dropped": dropped,
    }


def _array_spans(text: str) -> List[str]:
    """Yield every balanced JSON array substring, in order of appearance.

    Non-overlapping scan: after finding a balanced ``[...]`` block, scanning
    resumes just past it. Arrays nested inside a yielded block are not re-yield
    (the outer block is what json.loads cares about); nested arrays within the
    plan's own objects are handled by the outer balanced scan, which takes the
    whole top-level array.

    DANGER 2026-09-27 (twice). This scanner has to survive the ECHOED PROMPT.
    ``hermes chat`` echoes the whole query before the answer, and that echo
    contains near-miss brackets (a ledger summary with "[3h]", an inventory
    array the model truncated with "…"). Two distinct failures shipped from
    here:

    1. An unbalanced ``[`` used to ``break`` the scan, discarding everything
       after it — including the real plan 31KB later — so every run silently
       degraded to the all-keep fallback while reporting success.
    2. Resyncing with ``i += 1`` fixed that but produced a SPURIOUS span that
       started mid-structure and ran 38KB to the reply's end, swallowing the
       real plan whole.

    A bracket-balanced span is not necessarily a JSON array — that is exactly
    what a mis-nested one looks like. So a span is only yielded once it
    actually parses as JSON, and a non-parsing span is resynced past instead of
    consumed. Deciding this inside the scanner (not in the caller) is what
    stops one bad bracket from hiding a good plan.
    """
    spans: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "[":
            i += 1
            continue
        end = _balanced_end(text, i)
        if end is None:
            # Unbalanced all the way to EOF. A LATER "[" can still balance on
            # its own, so resync one char in rather than abandoning the rest of
            # the reply — the earlier `break` here is what made defect 14 hide
            # the real plan behind a stray bracket in the echoed prompt.
            i += 1
            continue
        candidate = text[i : end + 1]
        if _is_json_array(candidate):
            spans.append(candidate)
            i = end + 1
        else:
            # Bracket-balanced but not valid JSON: a mis-nested span. Resync
            # one char in so the real array inside it is still reachable.
            i += 1
    return spans


def _balanced_end(text: str, start: int) -> Optional[int]:
    """Index of the ``]`` closing the ``[`` at *start*, or None if unbalanced."""
    depth = 0
    in_string = False
    escape = False
    j = start
    n = len(text)
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
                return j
        j += 1
    return None


def _is_json_array(candidate: str) -> bool:
    """True when *candidate* is a JSON array. Control chars repaired first.

    Uses the lenient object_hook on purpose: a control character inside a
    string (the ledger is full of them) is repairable, so such a span is a
    REAL array and must be yielded for the caller to parse. What we are
    rejecting here is a span that is not an array at all — the mis-nested
    fragment — and that fails to load under any settings.
    """
    if not candidate.lstrip()[:1] == "[":
        return False
    try:
        loaded = json.loads(_auto_escape_controls_in_strings(candidate))
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(loaded, list)


def parse_plan(raw: str) -> List[Dict[str, Any]]:
    """Extract and parse a JSON action list from Cerve's reply text.

    Robust extraction: strips markdown fences, then tries every balanced JSON
    array in the reply (the model may include reasoning prose before the plan)
    and returns the first array that parses AND validates as a plan of actions.
    Stray control characters inside strings are repaired first.
    """
    text = raw.strip()
    # C1b (2026-09-28). This used to short-circuit to the FIRST fenced block
    # and parse ONLY that, which bypassed _array_spans and therefore bypassed
    # the ambiguity refusal below. A reply that echoes the schema inside a
    # fence and then states the real plan bare parsed to the *reminder* --
    # the prompt's own example -- instead of the plan. Both defects point the
    # same way: collecting candidates and letting the ambiguity logic choose
    # is strictly safer than trusting position.
    #
    # So fences are no longer special-cased at all. They are just text; the
    # scanner finds every balanced JSON array anywhere in the reply, fenced
    # or not, and the candidate-selection logic below decides. A fenced
    # prompt echo is then simply one more candidate -- and it loses to a
    # longer real plan, or loses to the ambiguity refusal, both of which are
    # correct outcomes.
    valid: List[List[Dict[str, Any]]] = []
    partial: List[Dict[str, Any]] = []
    last_err: Optional[Exception] = None
    for candidate in _array_spans(text):
        try:
            parsed = json.loads(_auto_escape_controls_in_strings(candidate))
            if isinstance(parsed, list) and parsed:
                result = validate_partial(parsed)
                if result["clean"]:
                    # Remember any salvage so a candidate that is MOSTLY bad
                    # can still be reported rather than silently vanishing.
                    if not result["ok"]:
                        partial.append(
                            {
                                "actions": result["clean"],
                                "dropped": result["dropped"],
                            }
                        )
                    valid.append(result["clean"])
        except json.JSONDecodeError as exc:
            last_err = exc
    def _finish(chosen: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return a salvaged plan, annotating any action that was dropped.

        A salvaged plan must be honest about being salvaged. The dropped
        action is not merely absent: the model's intent for that entry was
        never carried out, and an operator reading only the executed plan
        would assume every proposed action ran. So each surviving action
        gets the reason it lost its neighbours, and the log records it.
        """
        for entry in partial:
            if entry["actions"] is chosen or entry["actions"] == chosen:
                for a in chosen:
                    a["_dropped_actions"] = list(entry["dropped"])
                _log.warning(
                    "Cerveau plan partially invalid: kept %d action(s), "
                    "dropped %d: %s",
                    len(chosen),
                    len(entry["dropped"]),
                    "; ".join(entry["dropped"]),
                )
                break
        return chosen

    if valid:
        # Ambiguity is a refusal, not a coin flip. A reply can contain more
        # than one well-formed action array: the real plan plus a recap, or
        # the prompt's own schema example echoed back. The old code returned
        # valid[-1], so a trailing all-keep recap SILENTLY replaced a real
        # routing plan — triage reported success and freed nothing. When two
        # candidates are not obviously the same plan, refuse and let the
        # deterministic fallback run rather than guess.
        if len(valid) == 1:
            return _finish(valid[0])
        # Prefer a strictly longer plan: the real plan always covers at least
        # as many entries as a recap of it.
        by_len = sorted(valid, key=len, reverse=True)
        if len(by_len[0]) > len(by_len[1]):
            return _finish(by_len[0])
        # Equal length, so length cannot break the tie. Deduplicate first: a
        # reply often restates the SAME plan verbatim, and those are not a
        # conflict.
        seen, distinct = set(), []
        for cand in by_len:
            key = json.dumps(cand, sort_keys=True)
            if key not in seen:
                seen.add(key)
                distinct.append(cand)
        if len(distinct) == 1:
            return _finish(distinct[0])
        # Genuinely ambiguous (equal length, different content) — do not pick.
        raise PlanValidationError(
            f"Ambiguous Cerveau reply: {len(distinct)} distinct action arrays of "
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


def validate(
    actions: List[Any], _seen: Optional[set] = None
) -> List[Dict[str, Any]]:
    """Validate a list of action dicts; raises on contract violations.

    ``_seen`` carries the (target, index) keys already claimed by earlier
    actions. ``validate_partial`` passes one set across every action so the
    "one mutating action per entry" rule is still enforced over the WHOLE
    plan rather than restarting per action. It is private: the one-mutating-
    action rule must not be satisfiable by calling this per action.
    """
    if not isinstance(actions, list):
        raise PlanValidationError("Plan must be a JSON array of actions.")
    out: List[Dict[str, Any]] = []
    seen_touched: set = set() if _seen is None else _seen
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
        # An EXPLICIT target, on every action that points at an entry. A
        # missing one used to be silently defaulted to "memory", so a model
        # that routed a PROFILE fact without saying so wrote it into
        # MEMORY.md while the removal bookkeeping still removed the profile
        # entry it belonged to -- a live auto run replaced the 338-char
        # identity core with a 65-char stub. Guessing the store is never safe.
        if kind in ("keep", "evict-to-quarantine", "consolidate") or "index" in a:
            tgt = a.get("target")
            if tgt not in ("memory", "user"):
                raise PlanValidationError(
                    f"Action #{n} ({kind}) must state an explicit 'target' of "
                    f"'memory' or 'user'; got {tgt!r}."
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

# Bare-word placeholders. The angle-bracket form above is the common case,
# but the prompt's example also names skills "example-skill-name" / "your-skill
# -name", and those pass ^<...>$ happily. Matched only against whole
# skill_name VALUES, so a real skill that merely contains the word "example"
# ("example-workflow") is unaffected: the anchors require the whole value.
_PLACEHOLDER_NAME_RE = re.compile(
    r"^(?:your[-_ ]?|example|sample|dummy|placeholder|todo|tbd|xxx+|foo|bar)"
    r"[-_ ]?(?:skill[-_ ]?)?(?:name)?$",
    re.IGNORECASE,
)


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
    # A placeholder skill_name is the same failure one field over: the model
    # copied the example's name instead of choosing one. Length-preference
    # alone does not catch it — a 1-action echo loses to a 14-action plan, but
    # a model that emits ONLY the echo would otherwise have it applied.
    for holder in (a, *(r for r in (a.get("routes") or []) if isinstance(r, dict))):
        nm = str(holder.get("skill_name") or "").strip()
        if nm and (_looks_like_placeholder(nm) or _PLACEHOLDER_NAME_RE.match(nm)):
            raise PlanValidationError(
                f"Action #{n} ({kind}) routes to skill_name {nm!r}, which is a "
                f"PROMPT PLACEHOLDER, not a real skill name. Refusing the plan."
            )


def render_report(
    plan: List[Dict[str, Any]],
    *,
    usage_before: Dict[str, Any],
    run_id: str,
    fallback_note: str = "",
) -> str:
    """Render a human-readable report (English-only) for review/audit.

    ``fallback_note`` is prepended as a WARNING when Cerveau was not
    consulted (C2). It is a parameter rather than something derived here so
    the caller that actually knows whether the model was consulted is the
    one that says so -- the report must never imply a model decision that
    did not happen.
    """
    lines: List[str] = []
    lines.append(f"# Memory triage report — {run_id}")
    if fallback_note:
        lines.append("")
        lines.append(f"**{fallback_note}**")
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
    # A salvaged plan must SAY SO in the report an operator reads, not just
    # in the log. The dropped action is the model's unfulfilled intent for
    # that entry, and an operator who reads only this file would otherwise
    # assume every proposed action ran. Report it once, above the list.
    salvaged = [d for a in plan for d in (a.get("_dropped_actions") or [])]
    if salvaged:
        uniq = sorted(set(salvaged))
        lines.append("")
        lines.append(
            f"WARNING: {len(uniq)} proposed action(s) were malformed and "
            f"dropped; the {len(plan)} below are the rest. The entries they "
            f"referenced were NOT acted on:"
        )
        for d in uniq:
            lines.append(f"  - dropped {d}")
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