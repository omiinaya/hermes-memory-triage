"""Tests for C3, C5, and the untested snapshot containment guard.

C3  scripts_root ignored HERMES_HOME, so the test fixture's isolation was
    only half real and a route-to-script planted a file in the LIVE tree.
C5  store.write_entries used a fixed temp name, and nothing refused to
    empty a store that had content.
S1  restore_skill's root-containment guard is a live security control with
    ZERO test coverage; scratch/chk_restore_escape.py proved that removing
    it lets a symlinked snapshot write outside the snapshots root.
"""

import os
import pathlib

import pytest

from memtriage import snapshots
from memtriage import store as memory_store
from memtriage.config import Config, DEFAULT_SCRIPTS_DIR
from memtriage.executor import Executor


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


# -- C3: scripts_root must follow HERMES_HOME ------------------------------


def test_default_scripts_root_follows_hermes_home(tmp_path, monkeypatch):
    """C3. The default must resolve inside the isolated home, not ~/.hermes.

    This is the defect, verbatim: `scripts_root` read `scripts_dir` and
    nothing else, so under the full test fixture it resolved to the LIVE
    /root/.hermes/scripts (951 entries at the time of the audit).
    """
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    cfg = Config()  # bare config => scripts_dir is the DEFAULT

    assert cfg.scripts_root == home / "scripts"
    assert str(cfg.scripts_root).startswith(str(home)), (
        f"scripts_root escaped HERMES_HOME: {cfg.scripts_root}"
    )


def test_explicit_scripts_dir_still_wins(tmp_path, monkeypatch):
    """A deployment that genuinely puts scripts elsewhere must keep working."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    elsewhere = tmp_path / "opt" / "bin"
    cfg = Config(scripts_dir=str(elsewhere))
    assert cfg.scripts_root == elsewhere


def test_a_routed_script_lands_in_the_isolated_home(tmp_path, monkeypatch):
    """End to end: a route-to-script must not touch the real scripts tree."""
    cfg = _cfg(tmp_path, monkeypatch)
    # Deliberately a DEFAULT scripts_dir, not the tmp one the fixture sets,
    # so this exercises the leak itself.
    cfg = Config(
        data_dir=tmp_path / "data",
        provider_base_url="http://127.0.0.1:1",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    # body comes from the SOURCE ENTRY, not the action's text field
    # (_source_text), so the store entry must itself be the script.
    # TWO entries, deliberately: routing away the only entry in a store
    # trips the empty-memory floor, which then UNDOES the route write --
    # correct behaviour, but it would make this test pass for the wrong
    # reason. The second entry is the keeper.
    memory_store.write_entries("memory", [
        "print('hello from a routed script')",
        "a second entry so the store is not emptied by routing one away",
    ])

    Executor(cfg).execute_plan(
        [{
            "action": "route-to-script", "target": "memory", "index": 0,
            "script_name": "chk-c3-isolation", "script_ext": "py",
            "reason": "test",
        }],
        "run-c3", "test",
    )

    landed = list(cfg.scripts_root.glob("*chk-c3-isolation*"))
    assert landed, f"expected the script in {cfg.scripts_root}"
    assert str(cfg.scripts_root).startswith(str(tmp_path)), (
        "a test wrote a script outside the isolated home"
    )
    # And nothing was planted in the live tree.
    live = pathlib.Path(os.path.expanduser("~/.hermes/scripts"))
    if live.exists():
        assert not list(live.glob("*chk-c3-isolation*")), (
            "a test planted a file in the LIVE scripts tree"
        )


def test_scripts_root_matches_skills_root_shape(tmp_path, monkeypatch):
    """Both roots must honour HERMES_HOME -- that is the whole asymmetry."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    cfg = Config()
    assert cfg.scripts_root.parent == cfg.skills_root.parent


# -- C5: unique temp name, and never empty a store that had content --------


def test_store_temp_name_is_unique_to_the_writer(tmp_path, monkeypatch):
    """C5. A fixed `.tmp` name is shared across processes.

    `file_lock` is re-entrant per thread, so a second PROCESS writing the
    same store shares the temp path: one writer's os.replace renames the
    file the other is still writing, silently discarding the loser's
    content. The name must be private per process AND thread.

    Observed by watching the filesystem DURING the write, not by calling
    unique_tmp directly: calling the helper proves nothing about whether
    write_entries actually uses it, which is exactly the gap that let the
    fixed name survive.
    """
    cfg = _cfg(tmp_path, monkeypatch)
    seen = []
    real_replace = os.replace

    def spy(src, dst, *a, **kw):
        seen.append((str(src), str(dst)))
        return real_replace(src, dst, *a, **kw)

    memory_store.write_entries("memory", ["before"])
    monkeypatch.setattr(memory_store.os, "replace", spy)
    memory_store.write_entries("memory", ["after"])

    assert seen, "the write did not go through os.replace"
    src = seen[-1][0]
    assert not src.endswith(".md.tmp"), (
        f"write_entries still used the shared fixed temp name: {src}"
    )
    assert f".{os.getpid()}." in src, (
        f"the temp name is not private to this process: {src}"
    )


def test_two_writers_never_share_a_temp_path(tmp_path, monkeypatch):
    """The property itself: two calls in sequence must not collide."""
    from memtriage.atomicio import unique_tmp

    p = tmp_path / "MEMORY.md"
    names = {str(unique_tmp(p)) for _ in range(3)}
    assert len(names) == 1  # same process+thread: stable, and that is fine
    assert str(unique_tmp(p)) != str(p) + ".tmp"


def test_write_entries_leaves_no_temp_behind(tmp_path, monkeypatch):
    """A failed replace must not leave litter, and must not touch the store."""
    cfg = _cfg(tmp_path, monkeypatch)
    memory_store.write_entries("memory", ["original"])
    memory_store.write_entries("memory", ["replacement"])

    assert memory_store.read_entries("memory") == ["replacement"]
    leftovers = [
        p for p in pathlib.Path(memory_store.path_for("memory")).parent.iterdir()
        if ".tmp" in p.name
    ]
    assert not leftovers, f"temp files left behind: {leftovers}"


def test_executor_refuses_to_empty_a_store_that_had_content(
    tmp_path, monkeypatch
):
    """C5. Zero entries from a non-empty store is never legitimate.

    The memory floor already keeps one entry, but it is keyed to 'memory'
    and to a plan shape. This is the unconditional last line before the
    write, and it must leave the store intact.

    The 10% floor is lowered here deliberately: with the floor active
    the run is refused EARLIER for a different (also correct) reason, so
    the test would pass without ever reaching the guard. Lowering it is
    what makes this a test of the backstop rather than of the floor.
    """
    from memtriage import executor as executor_mod

    cfg = _cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(executor_mod, "USER_MIN_FRACTION", 0.0)
    entries = [f"entry number {i} with some text" for i in range(6)]
    memory_store.write_entries("user", entries)

    summary = Executor(cfg).execute_plan(
        [
            {"action": "evict-to-quarantine", "target": "user", "index": i,
             "reason": "stale"}
            for i in range(6)
        ],
        "run-c5", "test",
    )

    assert memory_store.read_entries("user"), (
        "the store was emptied; entries now: "
        f"{memory_store.read_entries('user')}"
    )
    assert any("refusing to empty" in e for e in summary["errors"]), (
        f"the empty-store backstop did not fire: {summary['errors']}"
    )


def test_an_already_empty_store_is_not_the_same_case(tmp_path, monkeypatch):
    """Writing/keeping an empty store is legitimate and must not be refused."""
    cfg = _cfg(tmp_path, monkeypatch)
    memory_store.write_entries("user", [])

    summary = Executor(cfg).execute_plan(
        [{"action": "keep", "target": "user", "index": 0, "reason": "x"}],
        "run-c5b", "test",
    )

    assert not any("refusing to empty" in e for e in summary["errors"]), (
        summary["errors"]
    )


# -- S1: restore_skill's containment guard --------------------------------


def test_restore_skill_refuses_a_symlinked_snapshot_outside_the_root(
    tmp_path, monkeypatch
):
    """S1. The containment check is a live, currently-untested control.

    `restore_skill` resolves the snapshot path and refuses anything outside
    the snapshots root. The obvious `../` escape is already blocked by the
    name check, which is why a basename probe "proves nothing" -- the real
    threat is a SYMLINK inside the snapshots root pointing at live content.
    With the guard, the symlink is refused; with it removed (verified by
    mutation), the outside content is written into a live skill.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    data = tmp_path / "data"
    snap_dir = data / "snapshots" / "skills"
    snap_dir.mkdir(parents=True, exist_ok=True)

    outside = tmp_path / "outside.md"
    outside.write_text("SECRET CONTENT FROM OUTSIDE\n", encoding="utf-8")
    link = snap_dir / "evil-skill__run-1__001__SKILL.md"
    link.symlink_to(outside)

    skills = tmp_path / "hermes" / "skills" / "evil-skill"
    skills.mkdir(parents=True, exist_ok=True)
    live = skills / "SKILL.md"
    live.write_text("HONEST ORIGINAL\n", encoding="utf-8")

    out = snapshots.restore_skill(
        data, link.name, tmp_path / "hermes" / "skills"
    )
    assert not out.get("restored"), f"the symlink escape was NOT refused: {out}"
    assert live.read_text(encoding="utf-8") == "HONEST ORIGINAL\n", (
        "content from outside the snapshots root was written into a live skill"
    )
