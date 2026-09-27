"""Pre-write snapshots: the store file must be recoverable after a bad write.

THE INCIDENT THIS EXISTS FOR. On 2026-09-27 a test fixture leaked into the
live USER.md, padding it with 110 copies of "filler doctrine clause". The
write itself was correct and atomic — it was the CONTENT that was wrong, so
quarantine held nothing and the only recovery was hand-editing the file.
A pre-write snapshot turns that into one command.

These tests pin the properties that make it trustworthy:
  * a snapshot is taken before EVERY content-changing write, and only then;
  * restore is byte-exact and itself snapshotted (so restore is undoable);
  * a failed snapshot is reported, never swallowed;
  * retention caps snapshots without touching the live store.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from memtriage import snapshots
from memtriage import store as memory_store
from memtriage.config import Config
from memtriage.executor import Executor

VALID = "memory", "user"


# -- the core contract ---------------------------------------------------


def test_a_write_that_changes_the_store_leaves_a_restorable_snapshot(tmp_path):
    cfg = Config(mode="auto", data_dir=tmp_path / "data")
    original = ["the real profile, exactly as the user wrote it"]
    memory_store.write_entries(memory_store.TARGET_USER, original)

    result = Executor(cfg).execute_plan([{
        "action": "route-to-profile", "target": "memory", "index": 0,
        "text": "a routed fact",
    }], "run-snap-1", "test")

    snaps = result.get("snapshots") or []
    assert snaps, "a content-changing write must leave a snapshot"
    s = snaps[0]
    assert s["target"] == "user"
    assert s["existed"] is True
    # The snapshot holds the PRE-write content, byte for byte.
    snap_path = Path(s["path"])
    assert snap_path.read_text(encoding="utf-8").strip() == original[0]
    # And the live store has genuinely moved on.
    assert "a routed fact" in memory_store.read_entries(memory_store.TARGET_USER)


def test_restore_puts_the_exact_bytes_back(tmp_path):
    cfg = Config(mode="auto", data_dir=tmp_path / "data")
    memory_store.write_entries(memory_store.TARGET_USER, ["original text"])
    before = memory_store.path_for(memory_store.TARGET_USER).read_bytes()

    result = Executor(cfg).execute_plan([{
        "action": "route-to-profile", "target": "memory", "index": 0,
        "text": "x",
    }], "run-snap-2", "test")
    assert "x" in memory_store.read_entries(memory_store.TARGET_USER), (
        "precondition: the write landed"
    )
    assert memory_store.path_for(memory_store.TARGET_USER).read_bytes() != before

    out = snapshots.restore(
        cfg.data_dir, "user", Path(result["snapshots"][0]["path"]).name
    )
    assert out["restored"] is True, out
    assert memory_store.path_for(memory_store.TARGET_USER).read_bytes() == before


def test_restore_is_itself_undoable(tmp_path):
    """Restoring replaces live content, so the replaced state is kept too."""
    cfg = Config(mode="auto", data_dir=tmp_path / "data")
    memory_store.write_entries(memory_store.TARGET_USER, ["v1"])
    r1 = Executor(cfg).execute_plan([{
        "action": "route-to-profile", "target": "memory", "index": 0, "text": "x",
    }], "run-snap-3", "test")
    snap = Path(r1["snapshots"][0]["path"]).name
    memory_store.write_entries(memory_store.TARGET_USER, ["v2"])

    out = snapshots.restore(cfg.data_dir, "user", snap)
    assert out["restored"] is True
    assert out.get("undo_snapshot"), "the restore must name its own undo point"
    assert memory_store.read_entries(memory_store.TARGET_USER) == ["v1"]


def test_a_no_op_write_leaves_no_snapshot(tmp_path):
    """A refused action writes nothing, so it must not litter snapshots."""
    cfg = Config(mode="auto", data_dir=tmp_path / "data")
    memory_store.write_entries(memory_store.TARGET_USER, ["keep me"])
    result = Executor(cfg).execute_plan([{
        "action": "keep", "target": "user", "index": 0,
    }], "run-snap-4", "test")
    assert result["applied"] == []
    assert result.get("snapshots") == []


def test_the_incident_scenario_is_now_one_command(tmp_path):
    """A correct atomic write of WRONG content is still recoverable.

    This is the shape of the 2026-09-27 damage: the write mechanism worked
    perfectly, the content was wrong. Quarantine is empty because nothing was
    "evicted" in the plan's own terms. The snapshot is the only way back.
    """
    cfg = Config(mode="auto", data_dir=tmp_path / "data")
    real = "the user's actual profile, hand written over months"
    memory_store.write_entries(memory_store.TARGET_USER, [real])

    # A bug elsewhere in the pipeline substitutes garbage for the content.
    Executor(cfg).execute_plan([{
        "action": "route-to-profile", "target": "memory", "index": 0,
        "text": "filler doctrine clause. ",
    }], "run-snap-5", "test")
    damaged = memory_store.read_entries(memory_store.TARGET_USER)
    assert damaged != [real], "precondition: the store is now wrong"

    snaps = snapshots.list_snapshots(cfg.data_dir, "user")
    assert snaps, "the bad write left a snapshot to recover from"
    out = snapshots.restore(cfg.data_dir, "user", snaps[0]["name"])
    assert out["restored"] is True
    assert memory_store.read_entries(memory_store.TARGET_USER) == [real]


# -- failure handling ----------------------------------------------------


def test_a_failed_snapshot_is_reported_not_swallowed(tmp_path, monkeypatch):
    """If we cannot snapshot, the run must say the write is unrecoverable."""
    cfg = Config(mode="auto", data_dir=tmp_path / "data")
    memory_store.write_entries(memory_store.TARGET_USER, ["x"])
    monkeypatch.setattr(
        snapshots, "take",
        lambda *a, **k: {"ok": False, "existed": True, "reason": "disk full"},
    )
    result = Executor(cfg).execute_plan([{
        "action": "route-to-profile", "target": "memory", "index": 0, "text": "y",
    }], "run-snap-6", "test")
    assert any("snapshot FAILED" in e for e in result["errors"]), result["errors"]


def test_a_missing_store_file_is_not_an_error(tmp_path):
    """A brand-new install has no file; that is normal, not a failure."""
    cfg = Config(mode="auto", data_dir=tmp_path / "data")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    assert not memory_store.path_for(memory_store.TARGET_USER).exists()
    out = snapshots.take(cfg.data_dir, "user", "run-new")
    assert out["existed"] is False
    # Not a failure: nothing was destroyed, so nothing needed preserving.
    # The run must NOT treat this as a snapshot failure (see the disk-full
    # test, which is a real one and must be reported).
    assert out.get("reason", "").startswith("no file yet"), out


def test_restoring_a_nonexistent_snapshot_fails_cleanly(tmp_path):
    cfg = Config(mode="auto", data_dir=tmp_path / "data")
    out = snapshots.restore(cfg.data_dir, "user", "no-such-snapshot")
    assert out["restored"] is False
    assert out.get("reason")


def test_path_traversal_in_a_snapshot_name_is_refused(tmp_path):
    """A snapshot name comes from a CLI argument — it must not escape."""
    cfg = Config(mode="auto", data_dir=tmp_path / "data")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    out = snapshots.restore(cfg.data_dir, "user", "../../../etc/passwd")
    assert out["restored"] is False
    assert "reason" in out


# -- retention -----------------------------------------------------------


def test_snapshots_are_capped_but_the_latest_survives(tmp_path):
    # A snapshot only exists when there was a file to preserve, so this must
    # start from a real store — take() on a fresh install is correctly a
    # no-op (nothing was destroyed).
    memory_store.write_entries(memory_store.TARGET_USER, ["seed"])
    for i in range(12):
        snapshots.take(tmp_path, "user", f"run-{i:02d}", keep=10)
    names = [s["name"] for s in snapshots.list_snapshots(tmp_path, "user")]
    assert len(names) <= 10, names
    assert any("run-11" in n for n in names), (
        f"the newest must never be the one pruned: {names}"
    )


def test_snapshots_survive_garbage_in_their_directory(tmp_path):
    """One unreadable neighbour must not hide every good snapshot."""
    memory_store.write_entries(memory_store.TARGET_USER, ["seed"])
    snapshots.take(tmp_path, "user", "run-good")
    (snapshots.snapshots_dir(tmp_path) / "not-a-snapshot.txt").write_text("junk")
    names = [s["name"] for s in snapshots.list_snapshots(tmp_path, "user")]
    assert any("run-good" in n for n in names)


# -- surface -------------------------------------------------------------


def test_snapshots_and_restore_file_are_in_the_command_surface():
    import plugin  # noqa: F401

    assert "snapshots" in plugin.SUBCOMMANDS
    assert "restore-file" in plugin.SUBCOMMANDS
    enum = plugin.TOOL_SCHEMA["properties"]["action"]["enum"]
    assert "snapshots" in enum
    assert "restore-file" in enum


def test_listing_with_no_snapshots_explains_itself(tmp_path):
    from memtriage.commands import cmd_snapshots

    cfg = Config(mode="auto", data_dir=tmp_path / "data")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    out = cmd_snapshots(cfg)
    assert "No snapshots yet" in out
    assert "before every write" in out


def test_restore_file_rejects_an_unknown_target(tmp_path):
    from memtriage.commands import cmd_restore_file

    cfg = Config(mode="auto", data_dir=tmp_path / "data")
    out = cmd_restore_file(cfg, target="everything", name="whatever")
    assert "Unknown target" in out
