"""Skill snapshots were written but unreachable -- 14 of them, on disk.

`route-to-skill` is this plugin's most frequent mutation (22 routes, 14
appends in the audit window) and, until 2026-09-27, the only one with no
undo: `_do_skill` copies a pre-write snapshot into
`snapshots/skills/`, a SUBDIRECTORY that `list_snapshots` (top-level glob)
and `restore` (`store.path_for`, memory stores only) both missed. A wrong
line in a skill is worse than a wrong line in a store, because it is
followed every matching session -- so this closes the gap rather than
documenting it.

These tests also pin the CONTRACT, so a future refactor cannot quietly
re-break reachability:

  * the lister must see the subdirectory;
  * a restore must be byte-exact;
  * a restore must itself be undoable (a restore is a write);
  * a traversal attempt must be refused;
  * a missing live skill must refuse WITHOUT deleting the snapshot.
"""
import os
import shutil
from pathlib import Path

import pytest

from memtriage import commands as commands_mod
from memtriage import snapshots as snapshots_mod


ORIGINAL = "# Curated body\n\nHand-written doctrine that must survive.\n"
APPENDED = (
    "\n\n## Routed by memtriage (x)\n\n"
    "<!-- memtriage:route:20260927-220322-0642cfd6 -->\nA WRONG CLAIM.\n"
)
SNAP_NAME = "oem-ui-design-system__20260927-220322-0642cfd6__SKILL.md"


@pytest.fixture
def skill_tree(isolated_env, tmp_path):
    """A live skill that has been appended to, plus its pre-write snapshot.

    Laid out exactly as the executor leaves it: a snapshot named
    `<skill>__<run_id>__SKILL.md` in a SUBDIRECTORY, and a live file whose
    category is NOT recorded in the snapshot name.
    """
    # The autouse fixture points HERMES_HOME at <tmp>/hermes, and
    # Config.skills_root appends "skills" to it -- so the tree must be nested
    # there, not at <tmp>/skills. (My first draft got this wrong and the
    # restore correctly REFUSED rather than reaching the real tree, which is
    # the containment behaving as designed.)
    skills = tmp_path / "hermes" / "skills"
    data = tmp_path / "mt"
    snap_dir = data / "snapshots" / "skills"
    snap_dir.mkdir(parents=True)

    live = skills / "creative" / "oem-ui-design-system" / "SKILL.md"
    live.parent.mkdir(parents=True)
    live.write_text(ORIGINAL)
    (snap_dir / SNAP_NAME).write_text(ORIGINAL)

    live.write_text(ORIGINAL + APPENDED)
    return {"live": live, "data": data, "skills": skills}


def _cfg(skill_tree):
    from memtriage import config as config_mod

    cfg = config_mod.Config.load()
    cfg.data_dir = Path(skill_tree["data"])
    # skills_root is a read-only property over skills_root_raw; the fixture
    # already redirected HERMES_HOME, so the resolved path is the sandbox.
    return cfg


def test_skill_snapshots_are_listed(skill_tree):
    """The regression itself: these were invisible to every command."""
    snaps = snapshots_mod.list_skill_snapshots(skill_tree["data"])
    assert [s["name"] for s in snaps] == [SNAP_NAME]
    assert snaps[0]["skill"] == "oem-ui-design-system"
    assert snaps[0]["bytes"] == len(ORIGINAL)


def test_snapshots_command_shows_them(skill_tree):
    out = commands_mod.cmd_snapshots(_cfg(skill_tree))
    assert SNAP_NAME in out
    assert "restore-file skill" in out


def test_snapshots_command_dispatches_on_target_skill(skill_tree):
    """`snapshots skill` must reach the subdir, not fall through.

    A mutation that renamed this branch to a bogus target survived the first
    mutation run TWICE, because the fixture had no store snapshots -- so the
    fall-through default produced byte-identical output. The store snapshot
    below is what makes the dispatch observable: if the branch is renamed,
    the default listing mixes both kinds in and the assertion trips.
    """
    (skill_tree["data"] / "snapshots" / "memory__RUN1__MEMORY.md").write_text("m")
    cfg = _cfg(skill_tree)
    out = commands_mod.cmd_snapshots(cfg, "skill")
    assert SNAP_NAME in out
    assert "skill snapshot(s)" in out
    # A store snapshot EXISTS, so if dispatch fell through it would appear.
    assert "store snapshot(s)" not in out
    assert "memory__RUN1__MEMORY.md" not in out


def test_snapshots_command_reports_both_kinds_by_default(skill_tree):
    """Default listing shows BOTH, so skills are never invisible again."""
    (skill_tree["data"] / "snapshots" / "memory__RUN1__MEMORY.md").write_text("m")
    out = commands_mod.cmd_snapshots(_cfg(skill_tree))
    assert "store snapshot(s)" in out
    assert "skill snapshot(s)" in out


def test_restore_is_byte_exact(skill_tree):
    cfg = _cfg(skill_tree)
    out = commands_mod.cmd_restore_file(cfg, "skill", SNAP_NAME)
    assert "Restored skill" in out
    assert skill_tree["live"].read_text() == ORIGINAL
    assert "A WRONG CLAIM" not in skill_tree["live"].read_text()


def test_a_restore_is_itself_undoable(skill_tree):
    """A restore is a write, so it owes the same recoverability."""
    cfg = _cfg(skill_tree)
    commands_mod.cmd_restore_file(cfg, "skill", SNAP_NAME)
    assert skill_tree["live"].read_text() == ORIGINAL
    undo = f"oem-ui-design-system__undo-20260927-220322-0642cfd6__SKILL.md"
    out = commands_mod.cmd_restore_file(cfg, "skill", undo)
    assert "Restored skill" in out
    assert skill_tree["live"].read_text() == ORIGINAL + APPENDED


def test_restore_refuses_a_traversal(skill_tree):
    cfg = _cfg(skill_tree)
    for evil in ("../../../etc/passwd", "..", "a/b/SKILL.md", "."):
        out = commands_mod.cmd_restore_file(cfg, "skill", evil)
        assert "Restore failed" in out, evil
    assert skill_tree["live"].read_text() == ORIGINAL + APPENDED


def test_missing_skill_refuses_and_keeps_the_snapshot(skill_tree):
    """Deleting a skill must not be a way to lose its last good copy."""
    cfg = _cfg(skill_tree)
    shutil.rmtree(skill_tree["live"].parent.parent)
    out = commands_mod.cmd_restore_file(cfg, "skill", SNAP_NAME)
    assert "Restore failed" in out
    assert "no live skill" in out
    assert (skill_tree["data"] / "snapshots" / "skills" / SNAP_NAME).exists()


def test_restore_does_not_leave_temp_files(skill_tree):
    cfg = _cfg(skill_tree)
    commands_mod.cmd_restore_file(cfg, "skill", SNAP_NAME)
    leftovers = list((skill_tree["data"] / "snapshots" / "skills").glob("*.tmp*"))
    assert leftovers == []
    assert skill_tree["live"].read_text() == ORIGINAL
