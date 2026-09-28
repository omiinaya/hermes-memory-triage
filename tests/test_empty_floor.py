"""The empty-memory floor was atomic, and that is why the store never shrank.

Reproduced in scratch/undo_repro.py before this change: routing every entry
freed 0 chars, and the SAME plan plus one `keep` freed everything. Cerveau
routes 9-or-10 of 10 nearly every run, so the plan that should have relie�ved
the store usually hit the one shape the guard rejected wholesale.

The rule is unchanged -- memory is never emptied. Only its granularity moved:
keep the smallest entry, honour the rest.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from memtriage import config as config_mod  # noqa: E402
from memtriage import executor as executor_mod  # noqa: E402
from memtriage import store as store_mod  # noqa: E402

RUN = "20260927-TEST-FLOOR"
ORIGINAL_SKILL = "# House rules\n\nCurated doctrine.\n"


def _entry(i: int) -> str:
    return (
        f"durable knowledge about topic {i}. Entry number {i}: durable "
        f"knowledge about topic {i}. unique clause {i}"
    )


@pytest.fixture
def env(monkeypatch, tmp_path):
    import os

    hermes = pathlib.Path(os.environ["HERMES_HOME"])
    data = pathlib.Path(os.environ["MEMTRIAGE_HOME"])
    skill = hermes / "skills" / "creative" / "house-rules" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(ORIGINAL_SKILL, encoding="utf-8")
    c = config_mod.Config.load()
    c.data_dir = data
    return {"cfg": c, "skill": skill}


def _plan(n: int = 10):
    return [
        {
            "action": "route-to-skill",
            "target": "memory",
            "index": i,
            "skill_name": "house-rules",
            "text": _entry(i) + f" m{i}",
        }
        for i in range(n)
    ]


def test_routing_everything_now_frees_everything_but_one(env):
    """The exact case that used to free nothing."""
    store_mod.write_entries("memory", [_entry(i) + f" m{i}" for i in range(10)])
    before = store_mod.char_count(store_mod.read_entries("memory"))
    ex = executor_mod.Executor(env["cfg"])
    summary = ex.execute_plan(_plan(10), RUN, "test")

    after = store_mod.read_entries("memory")
    assert len(after) == 1, f"expected 1 surviving entry, got {len(after)}"
    assert store_mod.char_count(after) < before / 5, (
        f"store barely moved: {before} -> {store_mod.char_count(after)}"
    )
    # The guard still announces itself, and says what it did.
    assert any("would be emptied entirely" in e for e in summary["errors"])
    assert any("smallest entry #0" in e for e in summary["errors"])


def test_the_survivor_is_the_smallest_entry(env):
    """Not just any entry -- the one that frees the most pressure."""
    entries = [_entry(i) + " m" + ("x" * i * 40) for i in range(6)]
    store_mod.write_entries("memory", entries)
    ex = executor_mod.Executor(env["cfg"])
    summary = ex.execute_plan(
        [
            {"action": "route-to-skill", "target": "memory", "index": i,
             "skill_name": "house-rules", "text": entries[i]}
            for i in range(6)
        ],
        RUN, "test",
    )
    after = store_mod.read_entries("memory")
    assert len(after) == 1
    assert after[0] == entries[0], "the SMALLEST entry must be the one kept"
    assert any("smallest entry #0" in e for e in summary["errors"])


def test_the_kept_entrys_route_write_is_undone(env):
    """The write that the kept entry paid for must be rolled back.

    The other nine stay -- the guard honoured them -- so this is the
    partial-rollback case, not the all-or-nothing one.
    """
    entries = [_entry(i) + f" m{i}" for i in range(10)]
    store_mod.write_entries("memory", entries)
    ex = executor_mod.Executor(env["cfg"])
    ex.execute_plan(_plan(10), RUN, "test")

    # Only ONE write is undone. Honest limitation, not a passing detail:
    # because rewinds run newest-first and entry 0's snapshot IS the
    # pristine file, restoring it necessarily rewinds every later append to
    # that same file too. When several appends target one skill, undoing the
    # first is indistinguishable from undoing them all. The entries are
    # still removed from the store and their text is in the ledger, so
    # nothing is lost -- but the skill file is a conservative rollback, not
    # a surgical one. `memtriage restore-file skill` replays any of the
    # per-write snapshots if that granularity is ever needed.
    text = env["skill"].read_text(encoding="utf-8")
    assert "unique clause 0" not in text, (
        "entry 0 was kept in the store, so its skill write must be undone"
    )


def test_a_plan_that_keeps_something_is_untouched(env):
    """Selective undo proven from the other side: no floor, no rewind."""
    entries = [_entry(i) + f" m{i}" for i in range(10)]
    store_mod.write_entries("memory", entries)
    # Keep index 9, route the other nine. A `keep` on an index the plan
    # also routes is rejected by the one-action-per-(target,index) guard,
    # so the keeper is the one entry the plan leaves alone.
    plan = _plan(9) + [{"action": "keep", "target": "memory", "index": 9}]
    ex = executor_mod.Executor(env["cfg"])
    ex.execute_plan(plan, RUN, "test")
    text = env["skill"].read_text(encoding="utf-8")
    for i in range(9):
        assert f"unique clause {i}" in text, (
            f"entry {i} WAS removed, so its write must stand"
        )


def test_the_store_is_never_actually_empty(env):
    store_mod.write_entries("memory", [_entry(i) + f" m{i}" for i in range(10)])
    ex = executor_mod.Executor(env["cfg"])
    ex.execute_plan(_plan(10), RUN, "test")
    assert len(store_mod.read_entries("memory")) >= 1


def test_an_already_empty_store_does_not_crash(env):
    """No entries means no keeper; `min()` on an empty mapping raises.

    Caught by the five existing snapshot tests, which run against an empty
    store.
    """
    store_mod.write_entries("memory", [])
    ex = executor_mod.Executor(env["cfg"])
    summary = ex.execute_plan(
        [{"action": "route-to-skill", "target": "memory", "index": 0,
          "skill_name": "house-rules", "text": "x"}],
        RUN, "test",
    )
    assert store_mod.read_entries("memory") == []
    assert not any("ValueError" in e for e in summary["errors"])



def test_selective_undo_across_TWO_skills(env):
    """The keeper's route goes to a DIFFERENT skill, so the rewind is visible.

    `test_the_kept_entrys_route_write_is_undone` cannot prove selectivity:
    every append targets one file, and entry 0's snapshot IS the pristine
    file, so undoing "just entry 0" rewinds every append to that file
    anyway. Splitting the writes across two skills removes that confound --
    here, undoing all writes would empty BOTH skills, and the mutation
    "only_index=None" is caught.
    """
    import os

    entries = [_entry(i) + f" m{i}" for i in range(10)]
    store_mod.write_entries("memory", entries)
    skills_root = pathlib.Path(os.environ["HERMES_HOME"]) / "skills"
    a = skills_root / "creative" / "house-rules" / "SKILL.md"
    b = skills_root / "creative" / "other-rules" / "SKILL.md"
    b.parent.mkdir(parents=True, exist_ok=True)
    b.write_text(ORIGINAL_SKILL, encoding="utf-8")

    plan = [
        {
            "action": "route-to-skill",
            "target": "memory",
            "index": i,
            # entry 0 (the keeper) goes to house-rules; the other nine to
            # other-rules. So undoing "all" would wipe both files.
            "skill_name": "house-rules" if i == 0 else "other-rules",
            "text": entries[i],
        }
        for i in range(10)
    ]
    ex = executor_mod.Executor(env["cfg"])
    ex.execute_plan(plan, RUN, "test")

    # house-rules: entry 0 is KEPT in the store, so its write is undone.
    assert a.read_text(encoding="utf-8") == ORIGINAL_SKILL
    # other-rules: entries 1-9 WERE removed, so their writes must all stand.
    other = b.read_text(encoding="utf-8")
    for i in range(1, 10):
        assert f"unique clause {i}" in other, (
            f"entry {i} was removed from the store, so its write to "
            f"other-rules must survive"
        )
    assert "unique clause 0" not in other


def test_the_never_write_empty_safety_net_holds(env):
    """`if not final:` is unreachable while a keeper exists -- so prove the
    OUTCOME, not the branch: after any plan, the store holds at least one
    entry. A mutant that drops the net only differs if `keeper` were removed
    from the kept set, and this asserts that can never be observed.
    """
    import os

    skills_root = pathlib.Path(os.environ["HERMES_HOME"]) / "skills"
    entries = [_entry(i) + f" m{i}" for i in range(10)]
    store_mod.write_entries("memory", entries)
    ex = executor_mod.Executor(env["cfg"])
    ex.execute_plan(
        [
            {"action": "route-to-skill", "target": "memory", "index": i,
             "skill_name": f"rule-{i}", "text": entries[i]}
            for i in range(10)
        ],
        RUN, "test",
    )
    after = store_mod.read_entries("memory")
    assert len(after) >= 1, "the memory store was written empty"
    # And the file on disk is non-empty, not just the in-memory list.
    path = store_mod.path_for("memory")
    assert path.stat().st_size > 0
