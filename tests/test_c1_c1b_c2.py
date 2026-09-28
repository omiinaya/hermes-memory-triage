"""Tests for C1, C1b and C2 -- the three defects that made a triage run
silently do nothing while reporting success.

C1  one malformed action out of fifteen discarded the other fourteen
C1b a fenced schema echo was parsed INSTEAD of the real plan
C2  a deterministic fallback was indistinguishable from a real run
"""

import json

import pytest

from memtriage import executor as executor_mod
from memtriage import plan as plan_mod
from memtriage import state as state_mod
from memtriage.config import Config
from memtriage.plan import PlanValidationError, parse_plan, validate


def _cfg(tmp_path, monkeypatch):
    """Isolated config: temp HERMES_HOME + temp plugin data dir.

    Copied verbatim from test_safety_regressions so this file cannot
    accidentally inherit a fixture shape that reaches the live store.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("MEMTRIAGE_HOME", str(tmp_path / "data"))
    return Config(
        data_dir=tmp_path / "data",
        scripts_dir=str(tmp_path / "scripts"),
        provider_base_url="http://127.0.0.1:1",  # closed port -> pending
    )


def _good_actions(n=13):
    """A realistic plan: keeps plus routings, all individually valid."""
    out = []
    for i in range(n):
        out.append({
            "action": "evict-to-quarantine",
            "target": "memory",
            "index": i,
            "reason": f"stale entry {i}",
        })
    return out


# -- C1: one bad action must not destroy the rest of the plan --------------


def test_one_malformed_action_does_not_discard_the_whole_plan():
    """C1. The blast radius of a bad action is the BAD ACTION, not the plan.

    Reproduced without a model: 13 valid actions + 1 `consolidate` carrying a
    single index produced "No non-empty action array in Cerve reply
    (consolidate action #12 needs >=2 entry indices.)" and the run was
    aborted with nothing freed.
    """
    actions = _good_actions(13)
    actions.append({
        "action": "consolidate", "target": "memory", "index": 13,
        "entries": [13],  # a merge needs >=2 -- this is the malformed one
        "text": "merged text",
    })
    raw = json.dumps(actions)

    parsed = parse_plan(raw)

    assert len(parsed) == 13, (
        f"expected the 13 valid actions to survive, got {len(parsed)}: {parsed}"
    )
    assert all(a["action"] == "evict-to-quarantine" for a in parsed)


def test_a_plan_that_is_entirely_invalid_still_raises():
    """Salvaging must not turn "no usable plan" into "an empty plan".

    If every action is malformed there is nothing to salvage, and the caller
    (cerveau.dispatch) must still see the refusal so it can use the
    deterministic fallback rather than proceed with nothing.
    """
    raw = json.dumps([
        {"action": "consolidate", "target": "memory", "entries": [0],
         "text": "x"},
        {"action": "nonsense", "target": "memory", "index": 1},
        {"action": "evict-to-quarantine", "target": "nowhere", "index": 2},
    ])
    with pytest.raises(PlanValidationError):
        parse_plan(raw)


def test_salvaged_plan_records_what_it_dropped():
    """A salvaged plan must SAY it was salvaged.

    The dropped action is the model's unfulfilled intent for that entry. An
    operator reading only the executed plan would assume every proposed
    action ran, so the reasons ride along on the actions themselves.
    """
    actions = _good_actions(3)
    actions.append({
        "action": "consolidate", "target": "memory", "entries": [9],
        "text": "merged", "index": 9,
    })
    parsed = parse_plan(json.dumps(actions))

    assert len(parsed) == 3
    dropped = parsed[0].get("_dropped_actions")
    assert dropped, "the surviving actions must carry the drop reason"
    assert any("consolidate" in d for d in dropped), dropped


def test_salvage_preserves_the_one_action_per_entry_rule():
    """Splitting validation per-action must not weaken a cross-action rule.

    If each action were validated in isolation, a plan that routed entry #0
    AND consolidated it would pass -- the exact double-apply the original
    single-pass validate() existed to stop.
    """
    raw = json.dumps([
        {"action": "evict-to-quarantine", "target": "memory", "index": 0,
         "reason": "stale"},
        {"action": "consolidate", "target": "memory", "entries": [0, 1],
         "text": "merged", "index": 0},
    ])
    parsed = parse_plan(raw)
    # The FIRST action is valid; the second conflicts with it and is dropped.
    assert len(parsed) == 1, parsed
    assert parsed[0]["action"] == "evict-to-quarantine"


def test_a_missing_target_is_never_defaulted_during_salvage():
    """Salvage must not invent a target.

    Guessing "memory" for a profile fact is what replaced a live 338-char
    identity entry with a 65-char stub. An action with no target is dropped,
    not defaulted -- in a salvaged plan just as in a strict one.
    """
    raw = json.dumps([
        {"action": "route-to-provider", "index": 0, "text": "a fact"},
    ])
    with pytest.raises(PlanValidationError):
        parse_plan(raw)


# -- C1b: the fence shortcut returned the wrong content --------------------


def test_a_fenced_schema_echo_does_not_replace_the_real_plan():
    """C1b. The old code parsed ONLY the first fenced block.

    `fences[0]` bypassed _array_spans, so a reply that echoed the prompt's
    schema inside a fence and then stated the real plan bare parsed to the
    REMINDER. The plan below is longer, so it must win.
    """
    reminder = [{"action": "keep", "target": "memory", "index": 0,
                 "reason": "example"}]
    real = _good_actions(5)
    raw = (
        "Here is the schema you asked for:\n"
        "```json\n" + json.dumps(reminder) + "\n```\n"
        "And here is my actual plan:\n"
        + json.dumps(real)
    )

    parsed = parse_plan(raw)

    assert len(parsed) == 5, parsed
    assert all(a["action"] == "evict-to-quarantine" for a in parsed), (
        "the fenced schema reminder was parsed instead of the real plan"
    )


def test_a_fenced_real_plan_is_still_parsed():
    """Removing the fence shortcut must not break a legitimately fenced plan."""
    raw = "```json\n" + json.dumps(_good_actions(4)) + "\n```"
    parsed = parse_plan(raw)
    assert len(parsed) == 4
    assert all(a["action"] == "evict-to-quarantine" for a in parsed)


def test_ambiguous_equal_length_replies_still_refuse():
    """C1b must not have weakened the ambiguity refusal.

    Two different plans of equal length are still genuinely ambiguous, and
    guessing between them is what the refusal was added to prevent.
    """
    a = [{"action": "evict-to-quarantine", "target": "memory", "index": 0,
          "reason": "plan A"}]
    b = [{"action": "evict-to-quarantine", "target": "memory", "index": 1,
          "reason": "plan B"}]
    with pytest.raises(PlanValidationError):
        parse_plan("Plan A:\n" + json.dumps(a) + "\nPlan B:\n" + json.dumps(b))


# -- C2: a fallback must be visible ----------------------------------------


def test_validate_still_raises_whole_plan_in_strict_mode():
    """validate() keeps its all-or-nothing contract for direct callers.

    load_plan() uses it on a plan this plugin wrote, where a single bad
    action should still be a hard error rather than a silent salvage.
    """
    with pytest.raises(PlanValidationError):
        validate([
            {"action": "evict-to-quarantine", "target": "memory", "index": 0,
             "reason": "stale"},
            {"action": "consolidate", "target": "memory", "entries": [1],
             "text": "x"},
        ])


def test_report_carries_the_fallback_warning():
    """A report that reads like a model decision when none happened is a lie."""
    note = (
        "WARNING: Cerveau was not consulted for this run. The plan below "
        "is the deterministic fallback (2 of 2 action(s) are no-op keeps)."
    )
    plan = [
        {"action": "keep", "target": "memory", "index": 0, "reason": "r"},
        {"action": "keep", "target": "memory", "index": 1, "reason": "r"},
    ]
    report = plan_mod.render_report(
        plan, usage_before={"memory": []}, run_id="t", fallback_note=note,
    )
    assert "Cerveau was not consulted" in report
    assert report.index("Cerveau was not consulted") < report.index("2 action")


def test_no_fallback_warning_when_cerveau_was_consulted():
    """The warning must not fire on a real run, or it stops meaning anything."""
    plan = [{"action": "keep", "target": "memory", "index": 0, "reason": "r"}]
    report = plan_mod.render_report(
        plan, usage_before={"memory": []}, run_id="t",
    )
    assert "Cerveau was not consulted" not in report


def test_fallback_is_persisted_in_state(tmp_path, monkeypatch):
    """State is what an operator greps days later, not a returned dict."""
    cfg = _cfg(tmp_path, monkeypatch)
    assert state_mod.fallback_runs(cfg) == []

    state_mod.note_fallback(cfg, "run-1")
    state_mod.note_fallback(cfg, "run-2")

    runs = state_mod.fallback_runs(cfg)
    assert [r["run_id"] for r in runs] == ["run-1", "run-2"]
    assert runs[0]["at"], "each entry needs a timestamp"


def test_fallback_history_is_bounded(tmp_path, monkeypatch):
    """An audit trail of 'the model was skipped' must not grow forever."""
    cfg = _cfg(tmp_path, monkeypatch)
    for i in range(state_mod.FALLBACK_HISTORY + 10):
        state_mod.note_fallback(cfg, f"run-{i}")
    runs = state_mod.fallback_runs(cfg)
    assert len(runs) == state_mod.FALLBACK_HISTORY
    # the oldest are dropped, the newest kept
    assert runs[-1]["run_id"] == f"run-{state_mod.FALLBACK_HISTORY + 9}"


def test_status_surfaces_a_fallback_streak(tmp_path, monkeypatch):
    """`mem_triage status` is the surface; the fallback must appear there."""
    from memtriage import commands

    cfg = _cfg(tmp_path, monkeypatch)
    assert "Cerveau was not consulted" not in commands.cmd_status(cfg)

    state_mod.note_fallback(cfg, "run-x")
    out = commands.cmd_status(cfg)
    assert "Cerveau was not consulted" in out
    assert "run-x" in out


def test_report_shows_dropped_actions_in_a_salvaged_plan():
    """The report is the audit artifact; the salvage must be visible in it."""
    plan = [
        {"action": "evict-to-quarantine", "target": "memory", "index": 0,
         "reason": "stale",
         "_dropped_actions": ["action #3: consolidate needs >=2 indices"]},
    ]
    report = plan_mod.render_report(
        plan, usage_before={"memory": []}, run_id="t",
    )
    assert "dropped" in report.lower()
    assert "consolidate" in report
