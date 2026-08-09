"""Shared pytest fixtures for hermes-memory-triage."""

import os

import pytest


@pytest.fixture()
def monkeypatch_env(monkeypatch, tmp_path):
    """Point HERMES_HOME and MEMTRIAGE_HOME at isolated temp dirs."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("MEMTRIAGE_HOME", str(tmp_path / "data"))
    yield tmp_path