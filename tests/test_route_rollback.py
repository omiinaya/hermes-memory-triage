"""A guard that revokes a removal must revoke the route write it paid for.

The empty-memory floor on 2026-09-27: a plan routing all 10 memory entries
left the store 100% intact (correct) while 19 skill writes stayed committed
(wrong). The text then lived in a skill, in the store, and in no record of
why -- and the next run routed it again. The repro that found this is
scratch/undo_repro.py; these tests are the same thing, permanently.

Reproducing it three ways in a row was the point:
  1. no undo at all              -> writes survived
  2. undo, but shared snap names -> "undid 10", nine still present
  3. undo, unique names, but
     forward order               -> same, oldest copy replayed last
Only reverse order (last-in-first-out) with a unique name per write is
correct, and only a test that reaches the FILE catches the difference.
"""
import pathlib
import shutil

import pytest

from memtriage import executor as executor_mod

RUN = "20260927-TEST-UNDO"
ORIGINAL_SKILL = "# House rules\n\nCurated doctrine.\n"


def _entry(i):
    return (
        f"durable knowledge about topic {i}. Entry number {i}: durable "
        f"knowledge about topic {i}. Entry number {i}: durable knowledge "
        f"about topic {i}. Entry number {i}: durable knowledge about "
        f"topic {i}."
    )


@pytest.fixture
def undo_env(monkeypatch, tmp_path):
    """A skills tree + store, isolated from the live install."""
    hermes = tmp_path / "hermes"
    data = tmp_path / "mt"
    skills = hermes / "skills"
    scripts = tmp_path / "scripts"
    for d in (skills, scripts, data):
        d.mkdir(parents=True, exist_ok=True)
    skill = skills / "creative" / "house-rules" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(ORIGINAL_SKILL, encoding="utf-8")
    # Write through the REAL store API. The entry delimiter is "\n\u00a7\n",
    # NOT a blank line: seeding with "\n\n" produced ONE entry, which made
    # the empty-memory floor fire on every plan and hid the cross-target
    # behaviour this test is for.
    from memtriage import store as store_mod

    store_mod.write_entries(
        "memory", [_entry(i) for i in range(10)]
    )
    store_mod.write_entries("user", [])
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setenv("MEMTRIAGE_HOME", str(data))
    return {"skill": skill, "data": data, "skills": skills, "scripts": scripts}


def _cfg(undo_env):
    from memtriage import config as config_mod

    cfg = config_mod.Config.load()
    cfg.data_dir = undo_env["data"]
    cfg.scripts_dir = str(undo_env["scripts"])
    return cfg


def _route_all(undo_env, count=10):
    """A plan that routes EVERY memory entry, so the floor must fire."""
    return [
        {
            "action": "route-to-skill",
            "target": "memory",
            "index": i,
            "skill_name": "house-rules",
            "text": _entry(i),
        }
        for i in range(count)
    ]


def test_a_revoked_removal_rolls_back_the_skill_write(undo_env):
    """The headline defect. Store unchanged AND the skill byte-identical."""
    ex = executor_mod.Executor(_cfg(undo_env))
    summary = ex.execute_plan(_route_all(undo_env), RUN, "test")

    assert any("emptied entirely" in e for e in summary["errors"]), (
        "the floor should have fired -- if it did not, this test is not "
        "exercising the guard it claims to"
    )
    assert undo_env["skill"].read_text(encoding="utf-8") == ORIGINAL_SKILL, (
        "the skill still carries routed text whose source entries were kept"
    )


def test_the_rollback_is_reported(undo_env):
    """A silent rollback is a hidden side effect; it must be visible."""
    ex = executor_mod.Executor(_cfg(undo_env))
    summary = ex.execute_plan(_route_all(undo_env), RUN, "test")
    assert any("undid" in e for e in summary["errors"])
    assert summary["route_writes_undone"]


def test_each_write_gets_its_own_snapshot(undo_env):
    """Ten appends to one skill need ten snapshots, not one shared name.

    A shared name means each write overwrites the previous snapshot, so an
    undo can only ever restore the oldest copy. The text is made distinct per
    entry because `_extend_skill` is idempotent: a body already present is
    not appended twice, so ten identical entries would produce one write.
    """
    from memtriage import snapshots as snapshots_mod

    plan = []
    for i in range(10):
        body = _entry(i) + f" unique clause {i}"
        plan.append({
            "action": "route-to-skill",
            "target": "memory",
            "index": i,
            "skill_name": "house-rules",
            "text": body,
        })
        # keep the store from tripping a staleness check on identical text
    ex = executor_mod.Executor(_cfg(undo_env))
    ex.execute_plan(plan, RUN, "test")
    snaps = snapshots_mod.list_skill_snapshots(undo_env["data"])
    assert len(snaps) == 10, [s["name"] for s in snaps]
    assert len({s["name"] for s in snaps}) == 10
    assert sorted(s["seq"] for s in snaps) == [f"{i:03d}" for i in range(1, 11)]


def test_undo_is_last_in_first_out(undo_env):
    """Restoring in forward order replays the OLDEST snapshot last.

    This is the bug that survived the first fix: the rollback reported
    "undid 10 route writes" while nine of them were still in the file.
    """
    ex = executor_mod.Executor(_cfg(undo_env))
    ex.execute_plan(_route_all(undo_env), RUN, "test")
    assert undo_env["skill"].read_text(encoding="utf-8") == ORIGINAL_SKILL


def test_the_user_floor_also_rolls_back_its_routes(undo_env):
    """Same contract on the `user` target, via the 10% floor.

    Evicting every user entry drops the store to 0%, so the floor revokes.
    A memory route made in the same run must NOT be rolled back by that --
    and a user route made in the same run MUST be. One test, both halves.
    """
    from memtriage import executor as ex_mod
    from memtriage import store as store_mod

    store_mod.write_entries("user", [_entry(i) + f" u{i}" for i in range(10)])
    store_mod.write_entries("memory", [_entry(i) + f" m{i}" for i in range(10)])

    ex = ex_mod.Executor(_cfg(undo_env))
    plan = [
        # paid for by memory#0 -- the memory store keeps 9 entries, so its
        # own floor stays quiet and this write must survive.
        {
            "action": "route-to-skill",
            "target": "memory",
            "index": 0,
            "skill_name": "house-rules",
            "text": _entry(0) + " m0",
        },
        # a user route, which the user floor will revoke
        {
            "action": "route-to-skill",
            "target": "user",
            "index": 0,
            "skill_name": "house-rules",
            "text": _entry(0) + " u0",
        },
    ] + [
        {"action": "evict-to-quarantine", "target": "user", "index": i}
        for i in range(1, 10)
    ]
    summary = ex.execute_plan(plan, RUN, "test")

    assert any("floor" in e for e in summary["errors"]), (
        "the user 10% floor should have fired -- if it did not, this test "
        "is not exercising the guard it claims to"
    )
    # The user store still holds its entries (the floor revoked the evictions).
    user_now = store_mod.read_entries("user")
    assert len(user_now) == 10, len(user_now)

    # THE POINT OF THE TEST: the user route was rolled back, the memory one
    # was not. Both are appends to the SAME file, so only a per-target
    # rollback can produce this outcome.
    skill = undo_env["skill"].read_text(encoding="utf-8")
    assert " m0" in skill, "the memory route was wrongly rolled back"
    assert " u0" not in skill, "the user route survived its own floor"
    assert "undid 1 route write(s) for 'user'" in " ".join(summary["errors"])


def test_rollback_leaves_another_targets_routes_alone(undo_env):
    """A `user` refusal must not undo a write that `memory` paid for.

    Two stores, one skill. Routing memory#0 and evicting user#0: the memory
    store still has nine entries so its floor stays quiet, and the user floor
    is what revokes. If the rollback ignored the target it would undo the
    memory write too, and the knowledge would exist in neither place.
    """
    from memtriage import executor as ex_mod

    from memtriage import store as store_mod

    store_mod.write_entries("user", [
        "I am Omar Minaya, my cyber-name is SULLEN. "
        "Ciel is my partner, never 'Papa'."
    ])
    # The floor dedups identical entries, so ten copies of the same text
    # collapse to one when nine are removed. Give each entry a distinct
    # clause so the store really still holds nine after the removal.
    from memtriage import store as store_mod

    store_mod.write_entries(
        "memory", [_entry(i) + f" unique clause {i}" for i in range(10)]
    )
    ex = ex_mod.Executor(_cfg(undo_env))
    plan = [
        {
            "action": "route-to-skill",
            "target": "memory",
            "index": 0,
            "skill_name": "house-rules",
            "text": _entry(0) + " unique clause 0",
        },
        # Identity-marked: the identity guard revokes this removal, which
        # puts the user store back under its 10% floor.
        {
            "action": "evict-to-quarantine",
            "target": "user",
            "index": 0,
        },
    ]
    ex.execute_plan(plan, RUN, "test")
    assert "unique clause 0" in undo_env["skill"].read_text(encoding="utf-8"), (
        "the memory route was wrongly rolled back by a `user` refusal"
    )





