"""Tests for memtriage.ledger: routing provenance + dedup."""

import time

from memtriage import ledger
from memtriage.config import Config


def _cfg(tmp_path):
    return Config(data_dir=tmp_path)


def test_record_and_load(tmp_path):
    cfg = _cfg(tmp_path)
    ledger.record(cfg, kind="skill", destination="deploy", summary="x",
                  run_id="r1", provenance="sess:abc")
    loaded = ledger.load(cfg)
    assert len(loaded) == 1
    assert loaded[0]["kind"] == "skill"
    assert loaded[0]["destination"] == "deploy"
    assert loaded[0]["provenance"] == "sess:abc"
    assert loaded[0]["routed_at"]


def test_already_routed(tmp_path):
    cfg = _cfg(tmp_path)
    assert ledger.already_routed(cfg, "deploy") is False
    ledger.record(cfg, kind="skill", destination="deploy", summary="s",
                  run_id="r1", provenance="p")
    assert ledger.already_routed(cfg, "deploy") is True


def test_same_destination_replaces(tmp_path):
    cfg = _cfg(tmp_path)
    ledger.record(cfg, kind="skill", destination="deploy", summary="v1",
                  run_id="r1", provenance="p")
    ledger.record(cfg, kind="skill", destination="deploy", summary="v2",
                  run_id="r2", provenance="p")
    loaded = ledger.load(cfg)
    assert len(loaded) == 1
    assert loaded[0]["summary"] == "v2"


def test_summary_empty_then_populated(tmp_path):
    cfg = _cfg(tmp_path)
    assert ledger.summary(cfg) == "No routes recorded yet."
    ledger.record(cfg, kind="user", destination="USER.md", summary="hi",
                  run_id="r1", provenance="p")
    assert "user->USER.md" in ledger.summary(cfg)