"""Tests for Cerveau profile provisioning + learning (memtriage.learning / setup).

Covers the install-gap fix: seeding the decision profile's brain
non-destructively, bounded learning entries, and the setup/verify command
surface. Paths are isolated via MEMTRIAGE_HOME + HERMES_HOME so nothing touches
a real profile.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from memtriage.config import Config
from memtriage import learning

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch) -> Config:
    """Config pointed at a temp HERMES_HOME + MEMTRIAGE_HOME (isolated)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("MEMTRIAGE_HOME", str(tmp_path / "memtriage"))
    return Config(cerveau_profile="cerveau")


def test_seed_profile_first_run_writes_all_files(cfg: Config):
    result = learning.seed_profile(cfg)
    prof = learning.decision_profile_dir(cfg)
    soul = prof / "SOUL.md"
    memory = prof / "memories" / "MEMORY.md"
    user = prof / "memories" / "USER.md"
    assert soul.exists() and memory.exists() and user.exists()
    assert result["SOUL.md"] == "written"
    assert result["MEMORY.md"] == "written"
    assert result["USER.md"] == "written"
    # Brain content present.
    assert "Cerveau, the memory triage decision-maker" in soul.read_text()
    assert "Routing taxonomy" in memory.read_text()
    assert "Routing must be reversible" in user.read_text()


def test_seed_profile_is_idempotent(cfg: Config):
    first = learning.seed_profile(cfg)
    second = learning.seed_profile(cfg)
    assert first["MEMORY.md"] == "written"
    assert second["MEMORY.md"] == "exists"  # not re-written / not clobbered
    assert second["USER.md"] == "exists"


def test_record_decision_appends_and_is_bounded(cfg: Config):
    learning.seed_profile(cfg)
    memory = learning.decision_profile_dir(cfg) / "memories" / "MEMORY.md"
    for i in range(learning.MAX_LEARNING_ENTRIES + 10):
        learning.record_decision(cfg, f"decision {i}")
    text = memory.read_text(encoding="utf-8")
    assert "Routing taxonomy" in text  # seed head preserved
    assert learning.LEARNING_MARKER in text
    entries = [ln for ln in text.splitlines() if ln.startswith("- [")]
    assert len(entries) == learning.MAX_LEARNING_ENTRIES  # bounded
    newest = learning.MAX_LEARNING_ENTRIES + 9
    assert any(f"decision {newest}" in e for e in entries)
    assert not any("decision 0" in e for e in entries)  # oldest dropped


def test_record_decision_skips_without_profile(cfg: Config):
    assert learning.record_decision(cfg, "x") is None


def test_seed_appends_to_existing_soul_without_mission(cfg: Config):
    prof = learning.decision_profile_dir(cfg)
    memories = prof / "memories"
    memories.mkdir(parents=True, exist_ok=True)
    (prof / "SOUL.md").write_text("You are a generic agent with no mission.\n")
    result = learning.seed_profile(cfg)
    assert result["SOUL.md"] == "appended"
    assert "Cerveau, the memory triage decision-maker" in (prof / "SOUL.md").read_text()


def test_templates_are_shipped():
    tdir = learning.templates_dir()
    assert tdir.is_dir()
    for f in ("soul.template.md", "memory.template.md", "user.template.md"):
        assert (tdir / f).is_file()


def test_setup_command_verify_only_reports(cfg: Config):
    from memtriage.commands import cmd_setup

    out = cmd_setup(cfg, verify_only=True)
    assert isinstance(out, str) and out
    assert "Cerveau profile" in out


def test_setup_registered_in_surface():
    # The plugin surface advertises 'setup' as a subcommand + tool action.
    import plugin  # noqa: F401  (repo root is on sys.path above)

    assert "setup" in plugin.SUBCOMMANDS
    assert "setup" in plugin.TOOL_SCHEMA["properties"]["action"]["enum"]