"""D1: execute_plan must exclude a concurrent writer to BOTH stores.

The 2026-09-28 audit: execute_plan locked only USER.md.lock while reading and
rewriting MEMORY.md too. `_lock_path` is per-file, so MEMORY.md.lock was a
different inode and the built-in memory tool's append was never excluded. An
append landing in the read-to-write window was destroyed with no error.

These tests assert the LOCK SCOPE directly rather than racing a real process:
a concurrent holder of MEMORY.md.lock must make execute_plan report that it
proceeded unlocked. That is the observable fact, and it is what a regression
would remove.
"""
import pathlib
import sys

import pytest

from memtriage import executor as ex_mod
from memtriage import locking as locking_mod
from memtriage import store as store_mod


def _entry(i: int) -> str:
    return (
        f"durable knowledge about subsystem {i} and how it interacts with the "
        f"deployment pipeline, staging, and the test gates that guard it " * 2
    )


@pytest.fixture
def env(monkeypatch, tmp_path):
    # The autouse `isolated_env` fixture in conftest.py ALREADY creates and
    # exports HERMES_HOME / MEMTRIAGE_HOME. Re-mkdir'ing without exist_ok
    # raises FileExistsError, and re-pointing them here fights the fixture.
    # This fixture only seeds the two stores with distinguishable entries.
    data = tmp_path / "mt"
    monkeypatch.setenv("MEMTRIAGE_HOME", str(data))
    data.mkdir(parents=True, exist_ok=True)
    store_mod.write_entries("memory", [_entry(i) for i in range(4)])
    store_mod.write_entries("user", [_entry(10 + i) for i in range(4)])
    return data


def _run(run_id="20260928-TEST-LOCKSCOPE"):
    cfg = ex_mod.Config.load()
    ex = ex_mod.Executor(cfg)
    return ex.execute_plan(
        [{"action": "evict-to-quarantine", "target": "memory",
          "index": 0, "text": "", "reason": "stale"}],
        run_id=run_id, provenance="test",
    )


def test_the_memory_store_lock_is_taken_not_just_the_profile(env):
    """The regression: MEMORY.md.lock was never acquired.

    Asserted by holding MEMORY.md.lock in another process and requiring
    execute_plan to SAY it is proceeding unlocked. Under the old code the
    profile lock was uncontended, the run looked clean, and this test fails.
    """
    mem_path = store_mod.path_for(store_mod.TARGET_MEMORY)
    usr_path = store_mod.path_for(store_mod.TARGET_USER)

    # Simulate a concurrent writer holding ONLY the memory-store lock, which
    # is exactly what the built-in memory tool does for a memory append.
    import fcntl

    handle = open(locking_mod._lock_path(mem_path), "a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        res = _run("20260928-TEST-LOCKSCOPE-A")
        joined = " ".join(res["errors"])
        assert "could not acquire the store lock" in joined, (
            "execute_plan proceeded while MEMORY.md.lock was held by another "
            "writer and said nothing. This is the 2026-09-28 data-loss bug."
        )
        assert "MEMORY.md" in joined, (
            f"the error must name the store it failed to lock; got: {joined}"
        )
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def test_a_contended_profile_lock_is_also_reported(env):
    """Regression guard for the original behaviour: USER.md.lock too."""
    usr_path = store_mod.path_for(store_mod.TARGET_USER)
    import fcntl

    handle = open(locking_mod._lock_path(usr_path), "a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        res = _run("20260928-TEST-LOCKSCOPE-B")
        joined = " ".join(res["errors"])
        assert "could not acquire the store lock" in joined
        assert "USER.md" in joined
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def test_multi_store_lock_holds_both_and_releases_them(env):
    """Held set is exactly the two store paths, and both are released."""
    paths = [store_mod.path_for(t) for t in
             (store_mod.TARGET_MEMORY, store_mod.TARGET_USER)]
    with locking_mod.multi_store_lock(paths) as held:
        assert sorted(held) == sorted(paths)
        # re-entrant acquire in the same thread must not deadlock
        with locking_mod.multi_store_lock(paths) as held2:
            assert sorted(held2) == sorted(paths)
    # after exit, a fresh acquire in ANOTHER process must succeed
    import fcntl
    h = open(locking_mod._lock_path(paths[0]), "a+")
    try:
        fcntl.flock(h.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fcntl.flock(h.fileno(), fcntl.LOCK_UN)
        h.close()


def test_multi_store_lock_takes_a_deterministic_order():
    """Order must not depend on the caller's argument order."""
    import fcntl
    a = pathlib.Path("/tmp/memtriage_ordertest/MEMORY.md")
    b = pathlib.Path("/tmp/memtriage_ordertest/USER.md")
    a.parent.mkdir(parents=True, exist_ok=True)
    for order in ([a, b], [b, a]):
        with locking_mod.multi_store_lock(order) as held:
            assert [p.name for p in held] == ["MEMORY.md", "USER.md"]
