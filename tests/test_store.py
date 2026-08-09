"""Tests for memtriage.store: faithful memory-format read/write."""

import os

import pytest

from memtriage import store

TARGET_MEMORY = store.TARGET_MEMORY
TARGET_USER = store.TARGET_USER


def test_parse_serialize_roundtrip():
    body = "first entry\n§\nsecond entry\n§\nthird"
    entries = store.parse_entries(body)
    assert entries == ["first entry", "second entry", "third"]
    assert store.serialize_entries(entries) == body


def test_parse_skips_blank():
    body = "a\n§\n\n§\nb"
    assert store.parse_entries(body) == ["a", "b"]


def test_char_count_matches_builtin():
    entries = ["aaaa", "bb"]
    # len("\n§\n".join(entries)) == len("aaaa\n§\nbb")
    assert store.char_count(entries) == len("aaaa\n§\nbb")


def test_char_limits():
    assert store.char_limit(TARGET_MEMORY) == 2200
    assert store.char_limit(TARGET_USER) == 1375


def test_missing_file_reads_empty(tmp_path):
    os.environ["HERMES_HOME"] = str(tmp_path)
    assert store.read_entries(TARGET_MEMORY) == []


def test_write_then_read(tmp_path):
    os.environ["HERMES_HOME"] = str(tmp_path)
    store.write_entries(TARGET_MEMORY, ["one", "two"])
    assert store.read_entries(TARGET_MEMORY) == ["one", "two"]
    u = store.usage(TARGET_MEMORY)
    assert u["limit"] == 2200
    assert u["current"] == store.char_count(["one", "two"])


def test_append_budget_guard(tmp_path):
    os.environ["HERMES_HOME"] = str(tmp_path)
    big = "x" * 3000
    res = store.append_entry(TARGET_MEMORY, big)
    assert res["success"] is False
    assert "exceed the limit" in res["error"]


def test_append_dedup(tmp_path):
    os.environ["HERMES_HOME"] = str(tmp_path)
    store.append_entry(TARGET_MEMORY, "hello")
    res = store.append_entry(TARGET_MEMORY, "hello")
    assert res["success"] is False
    assert "already exists" in res["error"]


def test_replace_and_remove(tmp_path):
    os.environ["HERMES_HOME"] = str(tmp_path)
    store.write_entries(TARGET_MEMORY, ["alpha beta", "gamma"])
    assert store.replace_entry(TARGET_MEMORY, "alpha", "ALPHA") is True
    assert store.read_entries(TARGET_MEMORY)[0] == "ALPHA"
    assert store.remove_entry(TARGET_MEMORY, "ALPHA") is True
    assert store.read_entries(TARGET_MEMORY) == ["gamma"]
    assert store.remove_entry(TARGET_MEMORY, "nonexistent") is False