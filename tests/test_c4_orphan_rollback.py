"""C4: an I/O failure on the store write orphaned every route write.

Reproduced before the fix in scratch/writefail_orphan.py: 9 skill writes
committed, the store left unchanged, 0 route writes rolled back, and the
run stamped into applied_runs.json anyway -- so the replay guard
permanently refused the legitimate retry, and ledger.json asserted the
knowledge had been routed when it had not.

Two defects, both here:
  1. a failed store write must roll back the route writes it orphaned;
  2. a run whose store write failed must NOT be stamped as applied.
"""

import json
import pathlib

import pytest

from memtriage import store as memory_store
from memtriage.config import Config
from memtriage.executor import Executor


def _cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("MEMTRIAGE_HOME", str(tmp_path / "data"))
    return Config(
        data_dir=tmp_path / "data",
        scripts_dir=str(tmp_path / "scripts"),
        provider_base_url="http://127.0.0.1:1",
    )


def _seed(n=10):
    """n routable memory entries, plus a keeper so the floor stays quiet.

    The keeper matters: without it the empty-memory floor fires FIRST and
    undoes the route writes itself, which would make this test pass for the
    wrong reason.
    """
    return [f"knowledge entry number {i} worth routing to a skill" for i in range(n)]


def _route_plan(n=10):
    return [
        {
            "action": "route-to-skill", "target": "memory", "index": i,
            "skill_name": f"c4-skill-{i}", "reason": "test",
        }
        for i in range(n)
    ]


def _break_the_write(monkeypatch):
    """Make write_entries fail the way a full disk or a read-only fs would."""
    def boom(target, entries):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(memory_store, "write_entries", boom)


def test_a_failed_store_write_rolls_back_its_route_writes(tmp_path, monkeypatch):
    """C4a. The source entries stayed, so the routed copies are orphans.

    Rolling them back is the same contract every other guard follows: a
    source that stays means the write that replaced it goes back. Leaving
    them is the one failure worse than a refused run, because the store and
    the skills now disagree and nothing reconciles them.
    """
    cfg = _cfg(tmp_path, monkeypatch)
    entries = _seed(10)
    memory_store.write_entries("memory", entries)

    _break_the_write(monkeypatch)
    summary = Executor(cfg).execute_plan(_route_plan(10), "run-c4", "test")

    skills_root = pathlib.Path(cfg.skills_root)
    # rglob, not glob: routed skills land under a category subdir
    # (e.g. <skills_root>/tools/<name>/SKILL.md), so a top-level glob
    # finds nothing and reports a false pass.
    landed = (
        [p for p in skills_root.rglob("SKILL.md") if "c4-skill-" in str(p)]
        if skills_root.exists() else []
    )
    assert not landed, (
        f"the route writes were left orphaned after the store write failed: "
        f"{[p.name for p in landed]}"
    )
    # The DIRECTORY matters as much as the file. A routed SKILL.md lives in
    # <category>/<name>/, so unlinking the file but leaving the dir still
    # makes inventory_skills report a skill with no body -- the orphan's
    # footprint surviving the cleanup that was supposed to remove it.
    skill_dirs = (
        sorted(
            p for p in skills_root.rglob("*")
            if p.is_dir() and "c4-skill-" in p.name
        )
        if skills_root.exists() else []
    )
    assert not skill_dirs, (
        f"the empty skill directories survived the rollback: "
        f"{[p.name for p in skill_dirs]}"
    )
    assert any("were now orphans" in e for e in summary["errors"]), (
        f"the rollback was not reported: {summary['errors']}"
    )
    # The store itself is untouched, which is the whole premise.
    assert memory_store.read_entries("memory") == entries


def test_a_failed_store_write_is_not_stamped_as_applied(tmp_path, monkeypatch):
    """C4b. No index moved, so the replay stamp protects nothing.

    The stamp exists to stop a plan being re-applied to a store whose
    indices have shifted. A write that never landed shifted nothing, so
    stamping it permanently refuses the retry that would fix the failure.
    """
    cfg = _cfg(tmp_path, monkeypatch)
    memory_store.write_entries("memory", _seed(10))

    _break_the_write(monkeypatch)
    Executor(cfg).execute_plan(_route_plan(10), "run-c4-stamp", "test")

    applied_path = pathlib.Path(cfg.data_dir) / "applied_runs.json"
    if applied_path.exists():
        applied_runs = json.loads(applied_path.read_text())
    else:
        # No file at all is the CORRECT outcome for a run whose only write
        # failed: nothing was applied, so there is nothing to record.
        applied_runs = {}
    assert "run-c4-stamp" not in applied_runs, (
        "a run whose store write failed was stamped as applied, which "
        "permanently refuses the legitimate retry"
    )


def test_a_successful_run_is_still_stamped(tmp_path, monkeypatch):
    """The stamp must still work -- the fix must not disable it."""
    cfg = _cfg(tmp_path, monkeypatch)
    memory_store.write_entries("memory", _seed(10))

    Executor(cfg).execute_plan(_route_plan(10), "run-c4-ok", "test")

    applied_runs = json.loads(
        (pathlib.Path(cfg.data_dir) / "applied_runs.json").read_text()
    )
    assert "run-c4-ok" in applied_runs, (
        "a successful run must still be stamped, or a retry could double-apply"
    )


def test_a_script_route_is_rolled_back_when_its_store_write_fails(
    tmp_path, monkeypatch
):
    """The same defect existed on the SCRIPT route and was independently fixed.

    `_do_script` recorded its undo token AFTER `_write_atomic`, so the
    snapshot held the body it had just written and the rollback restored
    the orphan. The skill route's own test cannot catch this -- it only
    covers `_do_skill` -- so the script path needs its own case.

    Also pins the derived-`created` behaviour: a brand-new script has no
    prior state, so the rollback DELETES it rather than "restoring" an
    empty file over it.
    """
    cfg = _cfg(tmp_path, monkeypatch)
    memory_store.write_entries("memory", _seed(6))
    _break_the_write(monkeypatch)

    Executor(cfg).execute_plan(
        [
            {"action": "route-to-script", "target": "memory", "index": i,
             "script_name": f"c4-script-{i}", "script_ext": "py",
             "reason": "test"}
            for i in range(3)
        ],
        "run-c4-script", "test",
    )

    scripts_root = pathlib.Path(cfg.scripts_root)
    left = (
        sorted(scripts_root.glob("c4-script-*.py"))
        if scripts_root.exists() else []
    )
    assert not left, (
        f"the script route writes were left orphaned after the store write "
        f"failed: {[p.name for p in left]}"
    )
    assert memory_store.read_entries("memory"), "the memory store was emptied"


def test_a_failed_write_on_one_target_does_not_orphan_the_other(
    tmp_path, monkeypatch
):
    """A partial failure must not leave the surviving target's writes alone.

    `user` fails to write; `memory` succeeds. memory's removals DID take
    effect, so its route writes must STAY. Rolling those back would destroy
    knowledge that was in fact routed.

    Store sizes are chosen so the two OTHER guards stay quiet: memory keeps
    a keeper so the empty-memory floor does not fire, and the user store is
    well above the 10% floor even after its removals. Otherwise the test
    would pass on the wrong guard's work.
    """
    cfg = _cfg(tmp_path, monkeypatch)
    memory_store.write_entries(
        "memory",
        [f"memory knowledge entry {i} worth routing to a skill" for i in range(3)]
        + ["KEEPER: a memory entry that must not be routed away"],
    )
    # Route only 3 of 8 profile entries, so the store stays well above the
    # 10% floor. The floor is doing its job; this test is about the I/O
    # path, and a guard that fires first would mask it.
    user_limit = memory_store.char_limit("user")
    memory_store.write_entries(
        "user",
        [f"profile entry {i}, " + "p" * (user_limit // 8) for i in range(8)],
    )

    real = memory_store.write_entries

    def only_user_fails(target, entries):
        if target == "user":
            raise OSError(28, "No space left on device")
        return real(target, entries)

    monkeypatch.setattr(memory_store, "write_entries", only_user_fails)
    summary = Executor(cfg).execute_plan(
        [
            {"action": "route-to-skill", "target": "memory", "index": i,
             "skill_name": f"c4-partial-mem-{i}", "reason": "test"}
            for i in range(3)
        ]
        + [
            {"action": "route-to-skill", "target": "user", "index": i,
             "skill_name": f"c4-partial-user-{i}", "reason": "test"}
            for i in range(3)
        ],
        "run-c4-partial", "test",
    )

    root = pathlib.Path(cfg.skills_root)
    # rglob: routed skills live under <skills_root>/tools/<name>/SKILL.md,
    # so a top-level glob finds nothing and reports a false pass.
    def _named(tag):
        if not root.exists():
            return []
        return [p for p in root.rglob("SKILL.md") if tag in str(p)]

    mem_kept = _named("c4-partial-mem-")
    user_left = _named("c4-partial-user-")

    assert mem_kept, (
        "memory's route writes were rolled back even though memory's store "
        f"write succeeded -- that destroys knowledge that was really routed. "
        f"errors: {summary['errors']}"
    )
    assert not user_left, (
        "user's route writes were left orphaned even though its store write "
        f"failed: {[p.name for p in user_left]}"
    )
    assert memory_store.read_entries("user"), "the user store was emptied"
