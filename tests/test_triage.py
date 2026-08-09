"""Tests for memtriage.triage + state: orchestration, threshold, cooldown."""

from memtriage import state, store as memory_store
from memtriage.config import Config
from memtriage.triage import run_triage


def _cfg(tmp_path, monkeypatch, mode="manual"):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("MEMTRIAGE_HOME", str(tmp_path / "data"))
    return Config(data_dir=tmp_path / "data", mode=mode)


def test_below_threshold_no_trigger(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    memory_store.write_entries("memory", ["tiny"])
    result = run_triage(cfg, reason="manual", dispatch=False)
    assert result["triggered"] is False
    assert "below threshold" in result["message"]


def test_forced_triage_manual_sets_awaiting(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, mode="manual")
    memory_store.write_entries("memory", ["x" * 2000])  # ~91% of 2200
    result = run_triage(cfg, reason="manual", force=True, dispatch=False)
    assert result["triggered"] is True
    assert result["mode"] == "manual"
    assert result["execution"] is None
    assert state.awaiting_approval(cfg) == result["run_id"]
    assert result["report_path"].endswith(".md")


def test_forced_triage_auto_executes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, mode="auto")
    memory_store.write_entries("memory", ["x" * 2000])
    result = run_triage(cfg, reason="manual", force=True, dispatch=False)
    assert result["triggered"] is True
    assert result["execution"] == {"applied": [], "pending": [], "errors": []}
    assert state.awaiting_approval(cfg) is None
    assert state.last_triage_at(cfg) is not None
    assert state.last_execution(cfg) is not None  # post-execution summary recorded


def test_over_threshold_detection(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    memory_store.write_entries("memory", ["x" * 1700])  # > 75% of 2200
    assert state.is_over_threshold(cfg) is True
    memory_store.write_entries("memory", ["tiny"])
    assert state.is_over_threshold(cfg) is False


def test_cooldown_blocks_after_triage(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    state.mark_triage(cfg, "r1")
    assert state.cooldown_active(cfg) is True
    state._save(cfg, {"last_triage_at": 0})  # expired
    assert state.cooldown_active(cfg) is False