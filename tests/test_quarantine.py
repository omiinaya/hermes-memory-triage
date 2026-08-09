"""Tests for memtriage.quarantine: reversible eviction."""

from memtriage import quarantine
from memtriage.config import Config


def _cfg(tmp_path, monkeypatch, days=7):
    # Isolate the memory store so restore() never touches the real one.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    return Config(data_dir=tmp_path / "data", quarantine_days=days)


def test_evict_and_list(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    rec = quarantine.evict(cfg, target="memory", text="stale fact",
                           reason="superseded", run_id="r1")
    assert rec["text"] == "stale fact"
    listed = quarantine.all_evicted(cfg)
    assert len(listed) == 1
    assert listed[0]["evicted_at_iso"]


def test_restore_within_window(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, days=7)
    quarantine.evict(cfg, target="memory", text="recover me",
                     reason="trial", run_id="r1")
    assert quarantine.restore(cfg, "recover me") is True
    assert quarantine.all_evicted(cfg) == []


def test_restore_after_window_false(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, days=7)
    quarantine.evict(cfg, target="memory", text="expired fact",
                     reason="old", run_id="r1")
    # Force the eviction timestamp far into the past.
    recs = quarantine.all_evicted(cfg)
    recs[0]["evicted_at"] = 0
    quarantine._rewrite_file_objs(cfg, recs)
    assert quarantine.restore(cfg, "expired fact") is False


def test_purge_expired(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, days=7)
    quarantine.evict(cfg, target="memory", text="keep me", reason="a", run_id="r")
    quarantine.evict(cfg, target="memory", text="drop me", reason="a", run_id="r")
    recs = quarantine.all_evicted(cfg)
    # Mark only "drop me" as expired by rewriting with evicted_at=0 for it.
    for r in recs:
        if r["text"] == "drop me":
            r["evicted_at"] = 0
    quarantine._rewrite_file_objs(cfg, recs)
    removed = quarantine.purge_expired(cfg)
    assert removed == 1
    remaining = quarantine.all_evicted(cfg)
    assert [r["text"] for r in remaining] == ["keep me"]