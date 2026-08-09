"""Tests for memtriage.config: defaults, load/save, validation."""

import json

import pytest

from memtriage.config import Config


def test_defaults():
    cfg = Config()
    assert cfg.threshold_percent == 0.75
    assert cfg.mode == "manual"
    assert cfg.quarantine_days == 7
    assert cfg.cooldown_minutes == 60
    assert cfg.cerveau_profile == "cerveau"


def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        Config(mode="nuclear")


def test_invalid_threshold_raises():
    with pytest.raises(ValueError):
        Config(threshold_percent=1.5)
    with pytest.raises(ValueError):
        Config(threshold_percent=0.0)


def test_save_then_load_roundtrip(tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path, mode="auto", threshold_percent=0.8)
    cfg.save()
    # Config.load() resolves the data dir from MEMTRIAGE_HOME — point it at tmp.
    monkeypatch.setenv("MEMTRIAGE_HOME", str(tmp_path))
    loaded = Config.load()
    assert loaded.mode == "auto"
    assert loaded.threshold_percent == 0.8
    assert loaded.data_dir == tmp_path


def test_load_missing_file_returns_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMTRIAGE_HOME", str(tmp_path))
    loaded = Config.load()
    assert loaded.threshold_percent == 0.75
    assert loaded.mode == "manual"


def test_load_corrupt_config_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMTRIAGE_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        Config.load()


def test_from_dict_ignores_unknown_keys(tmp_path):
    cfg = Config.from_dict({"threshold_percent": 0.5, "bogus_key": 1})
    assert cfg.threshold_percent == 0.5


def test_to_dict_contains_data_dir(tmp_path):
    cfg = Config(data_dir=tmp_path)
    d = cfg.to_dict()
    assert d["data_dir"] == str(tmp_path)
    assert d["mode"] == "manual"


def test_data_dir_str_is_coerced_to_path(tmp_path):
    cfg = Config(data_dir=str(tmp_path))
    assert isinstance(cfg.data_dir, type(tmp_path))
    assert cfg.quarantine_dir == tmp_path / "quarantine"


def test_data_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMTRIAGE_HOME", str(tmp_path / "alt"))
    from memtriage.config import _default_data_dir

    assert _default_data_dir() == tmp_path / "alt"