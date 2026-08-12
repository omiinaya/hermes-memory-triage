"""Tests for memtriage.executor: applying plans to real surfaces."""

import os

from memtriage import ledger, quarantine, store as memory_store
from memtriage.config import Config
from memtriage.executor import Executor


def _cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    return Config(
        data_dir=tmp_path / "data",
        scripts_dir=str(tmp_path / "scripts"),
        provider_base_url="http://127.0.0.1:1",  # closed port -> pending
    )


def _exec(cfg):
    ex = Executor(cfg)
    ex._run_id = "test-run"
    return ex


def test_keep_is_noop(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    ex = _exec(cfg)
    summary = ex.execute_plan(
        [{"action": "keep", "target": "memory", "index": 0}],
        "test-run", "provenance:p",
    )
    assert summary["applied"] == []
    assert summary["errors"] == []


def test_consolidate_merges_entries(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    memory_store.write_entries("memory", ["old fact one", "old fact two"])
    ex = _exec(cfg)
    summary = ex.execute_plan(
        [{"action": "consolidate", "target": "memory", "entries": [0, 1],
          "text": "merged fact"}],
        "test-run", "provenance:p",
    )
    assert summary["errors"] == []
    assert memory_store.read_entries("memory") == ["merged fact"]
    assert ledger.already_routed(cfg, "memory#consolidated")


def test_route_to_skill_writes_skill(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    ex = _exec(cfg)
    summary = ex.execute_plan(
        [{"action": "route-to-skill", "skill_name": "deploy flow",
          "text": "1. build\n2. ship", "category": "ops"}],
        "test-run", "provenance:sess:abc",
    )
    assert summary["errors"] == []
    skill_path = cfg.skills_root / "ops" / "deploy-flow" / "SKILL.md"
    assert skill_path.exists()
    content = skill_path.read_text(encoding="utf-8")
    assert "name: deploy-flow" in content
    assert 'provenance: "provenance:sess:abc"' in content
    assert "1. build" in content
    assert ledger.already_routed(cfg, str(skill_path))


def test_route_to_profile_appends_user(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    ex = _exec(cfg)
    summary = ex.execute_plan(
        [{"action": "route-to-profile", "text": "User prefers espresso."}],
        "test-run", "provenance:p",
    )
    assert summary["errors"] == []
    assert "User prefers espresso." in memory_store.read_entries("user")


def test_route_to_script_writes_and_makes_executable(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    ex = _exec(cfg)
    summary = ex.execute_plan(
        [{"action": "route-to-script", "script_name": "weekly_cleanup",
          "script_ext": "sh", "text": "#!/bin/sh\necho cleanup"}],
        "test-run", "provenance:p",
    )
    assert summary["errors"] == []
    script = cfg.scripts_root / "weekly_cleanup.sh"
    assert script.exists()
    if os.name != "nt":
        assert os.access(script, os.X_OK)


def test_route_to_provider_unreachable_becomes_pending(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    memory_store.write_entries("memory", ["rich scene knowledge"])
    ex = _exec(cfg)
    summary = ex.execute_plan(
        [{"action": "route-to-provider", "text": "rich scene knowledge"}],
        "test-run", "provenance:p",
    )
    assert summary["errors"] == []
    assert any("route-to-provider" in p for p in summary["pending"])
    # Data-safety: a failed provider write must NOT silently drop the source
    # entry from the working store (it is only recoverable via the plan file).
    assert memory_store.read_entries("memory") == ["rich scene knowledge"]


def test_evict_quarantines_and_removes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    memory_store.write_entries("memory", ["stale entry", "good entry"])
    ex = _exec(cfg)
    summary = ex.execute_plan(
        [{"action": "evict-to-quarantine", "target": "memory", "index": 0,
          "reason": "superseded"}],
        "test-run", "provenance:p",
    )
    assert summary["errors"] == []
    assert memory_store.read_entries("memory") == ["good entry"]
    assert [r["text"] for r in quarantine.all_evicted(cfg)] == ["stale entry"]


def test_bad_index_reports_error_not_crash(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    memory_store.write_entries("memory", ["only"])
    ex = _exec(cfg)
    summary = ex.execute_plan(
        [{"action": "evict-to-quarantine", "target": "memory", "index": 9}],
        "test-run", "provenance:p",
    )
    assert len(summary["errors"]) == 1
    assert "out of range" in summary["errors"][0]