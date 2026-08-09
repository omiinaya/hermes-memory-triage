"""Tests for memtriage.plan: extraction, validation, rendering."""

import pytest

from memtriage import plan
from memtriage.plan import PlanValidationError

VALID_ACTION = {"action": "keep", "target": "memory", "index": 0, "reason": "fine"}


def test_parse_plan_from_fenced_json():
    raw = '```json\n[{"action": "keep", "target": "memory"}]\n```'
    assert plan.parse_plan(raw) == [{"action": "keep", "target": "memory"}]


def test_parse_plan_from_prose_with_trailing_text():
    raw = (
        'Here is my plan:\n[{"action": "route-to-skill", "skill_name": "x", '
        '"text": "do the thing", "reason": "procedure"}]\nHope that helps!'
    )
    out = plan.parse_plan(raw)
    assert out[0]["action"] == "route-to-skill"
    assert out[0]["skill_name"] == "x"


def test_parse_plan_skips_reasoning_prose_before_json():
    # The model reasons in prose (with stray brackets) BEFORE the real plan.
    raw = (
        "Let me think about this. The deploy procedure [see index 1] is clearly "
        "procedural. Also the old-name unit (index [2]) is stale — superseded. "
        'Final plan:\n[{"action": "route-to-skill", "skill_name": "deploy", '
        '"text": "deploy the relay", "reason": "procedure"}, '
        '{"action": "evict-to-quarantine", "target": "memory", "index": 2, '
        '"reason": "superseded"}]'
    )
    out = plan.parse_plan(raw)
    assert len(out) == 2
    assert out[0]["action"] == "route-to-skill"
    assert out[1]["action"] == "evict-to-quarantine"


def test_parse_plan_rejects_missing_array():
    with pytest.raises(PlanValidationError):
        plan.parse_plan("no json here")


def test_parse_plan_rejects_unbalanced():
    with pytest.raises(PlanValidationError):
        plan.parse_plan('[{"action": "keep"}')


def test_validate_rejects_unknown_action():
    with pytest.raises(PlanValidationError):
        plan.validate([{"action": "explode"}])


def test_validate_rejects_non_list():
    with pytest.raises(PlanValidationError):
        plan.validate({"action": "keep"})


def test_validate_consolidate_requires_entries_and_text():
    with pytest.raises(PlanValidationError):
        plan.validate([{"action": "consolidate", "text": "merged"}])
    with pytest.raises(PlanValidationError):
        plan.validate([{"action": "consolidate", "entries": [0, 1]}])


def test_validate_accepts_valid_plan():
    actions = plan.validate([VALID_ACTION])
    assert len(actions) == 1


def test_parse_plan_repairs_raw_newlines_in_strings():
    # A REAL (unescaped) newline inside a JSON string — LLMs emit these.
    raw = '[{"action": "route-to-script", "script_name": "cleanup", "text": "echo one\necho two"}]'
    out = plan.parse_plan(raw)
    assert out[0]["script_name"] == "cleanup"
    assert out[0]["text"] == "echo one\necho two"


def test_parse_plan_skips_empty_arrays():
    # Reasoning mentions [] but the real plan comes after.
    raw = (
        "No changes? Returning []. Actually wait — one item is stale. Final: "
        '[{"action": "evict-to-quarantine", "target": "memory", "index": 2, "reason": "superseded"}]'
    )
    out = plan.parse_plan(raw)
    assert len(out) == 1
    assert out[0]["action"] == "evict-to-quarantine"


def test_parse_plan_takes_last_valid_array():
    raw = (
        'Example: [{"action": "keep", "target": "memory", "index": 0, "reason": "example"}]\n'
        'Final committed plan: [{"action": "route-to-skill", "skill_name": "final", "text": "t", "reason": "real"}]'
    )
    out = plan.parse_plan(raw)
    assert out[0]["skill_name"] == "final"


def test_validate_rejects_routing_from_user_target():
    with pytest.raises(PlanValidationError):
        plan.validate(
            [{"action": "route-to-profile", "target": "user", "index": 0, "text": "x"}]
        )
    # But keep/evict may target user.
    assert plan.validate(
        [{"action": "keep", "target": "user", "index": 0, "reason": "ok"}]
    )


def test_render_report_lists_actions():
    actions = [
        {"action": "evict-to-quarantine", "target": "memory", "index": 2,
         "reason": "superseded by newer entry"},
        {"action": "route-to-skill", "skill_name": "deploy-flow",
         "text": "How to deploy the relay."},
    ]
    report = plan.render_report(
        actions,
        usage_before={"memory": [{"target": "memory", "current": 2000,
                                  "limit": 2200, "fraction": 0.91}]},
        run_id="run-1",
    )
    assert "run-1" in report
    assert "[evict-to-quarantine -> memory]" in report
    assert "deploy-flow" in report


def test_plan_save_load_roundtrip(tmp_path):
    from memtriage.config import Config

    cfg = Config(data_dir=tmp_path)
    actions = [{"action": "keep", "target": "memory", "index": 0}]
    path = plan.save_plan(cfg, "run-9", actions)
    assert plan.load_plan(cfg, "run-9") == actions
    assert path.endswith("plan-run-9.json")