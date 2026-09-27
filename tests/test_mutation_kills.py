"""Tests that KILL specific mutants found by mutation testing.

Mutation testing on 2026-09-27 found 22 guards in this executor that could be
deleted with the whole suite still green — false confidence, since most of
them are the guards that stand between a plan and lost data. Each test here
exists to fail loudly when its guard is removed.

The pattern: assert the *store* and the *error list* after a deliberately
hostile plan, not just that something was applied.
"""

import pytest

from memtriage import store as st
from memtriage.executor import Executor

IDENT = "Omar Minaya, SULLEN."


def _cfg(tmp_path):
    class C:
        pass
    c = C()
    c.data_dir = tmp_path / "data"
    c.data_dir.mkdir(parents=True, exist_ok=True)
    c.skills_root = tmp_path / "hermes" / "skills"
    c.skills_root.mkdir(parents=True, exist_ok=True)
    c.scripts_root = tmp_path / "scripts"
    c.scripts_root.mkdir(parents=True, exist_ok=True)
    c.retain_snapshots = 10
    c.data_dir.mkdir(parents=True, exist_ok=True)
    return c


def _run(cfg, actions, run_id="r1"):
    ex = Executor(cfg)
    return ex, ex.execute_plan(actions, run_id=run_id, provenance="test")


# --- mutant: executor-bool-index-ok ----------------------------------------
# `isinstance(index, bool)` guard removed: True is an int in Python, so
# `removals[target].add(True)` silently removes index 1.


def test_a_bool_index_must_not_be_treated_as_index_one(tmp_path):
    """True == 1 in Python. Accepting it deletes the WRONG entry."""
    st.write_entries(st.TARGET_MEMORY, ["ZERO ENTRY", "ONE ENTRY", "TWO ENTRY"])
    cfg = _cfg(tmp_path)
    ex, res = _run(cfg, [{
        "action": "evict-to-quarantine", "target": "memory",
        "index": True, "text": "irrelevant",
    }])
    entries = st.read_entries(st.TARGET_MEMORY)
    assert entries == ["ZERO ENTRY", "ONE ENTRY", "TWO ENTRY"], (
        f"a bool index removed a real entry: {entries}"
    )
    assert any("bool" in e or "int" in e for e in res["errors"]), res["errors"]


# --- mutant: executor-outofrange-quiet -------------------------------------
# Out-of-range index dropped without a report looks identical to "refused",
# so a plan that hallucinates an index is silently a no-op.


def test_an_out_of_range_index_is_reported_not_ignored(tmp_path):
    st.write_entries(st.TARGET_MEMORY, ["A", "B"])
    cfg = _cfg(tmp_path)
    ex, res = _run(cfg, [{
        "action": "evict-to-quarantine", "target": "memory",
        "index": 99, "text": "irrelevant",
    }])
    assert st.read_entries(st.TARGET_MEMORY) == ["A", "B"]
    assert any("out of range" in e for e in res["errors"]), (
        f"a hallucinated index must be reported, got {res['errors']}"
    )


# --- mutant: executor-provider-fail-removes --------------------------------
# `if ok:` -> removed: the source is deleted even though the gateway write
# FAILED. The text then exists nowhere (not in the store, not at the
# provider, and the plan file is not a user-facing record).


def test_a_failed_provider_write_keeps_the_source_entry(tmp_path):
    st.write_entries(st.TARGET_MEMORY, ["CRITICAL FACT", "other"])
    cfg = _cfg(tmp_path)
    ex = Executor(cfg)
    ex._do_provider = lambda a, original=None: False  # gateway refused
    res = ex.execute_plan([{
        "action": "route-to-provider", "target": "memory",
        "index": 0, "text": "CRITICAL FACT",
    }], run_id="r1", provenance="test")
    assert "CRITICAL FACT" in st.read_entries(st.TARGET_MEMORY), (
        "the entry was routed to a gateway that refused, then deleted"
    )
    assert res["pending"], res


# --- mutant: executor-dop-profile-atlimit ---------------------------------


def test_route_to_profile_refuses_when_the_user_store_is_full(tmp_path, monkeypatch):
    st.write_entries(st.TARGET_USER, ["x" * 5000])
    cfg = _cfg(tmp_path)
    ex, res = _run(cfg, [{
        "action": "route-to-profile", "text": "MORE CONTENT",
    }])
    entries = st.read_entries(st.TARGET_USER)
    assert entries == ["x" * 5000], (
        "routing into a full user store made it worse"
    )
    assert any("profile" in e.lower() for e in res["errors"]), res["errors"]


# --- mutant: guard-split-fidelity-off --------------------------------------


def test_a_split_cannot_replace_an_entry_with_unrelated_text(tmp_path):
    st.write_entries(st.TARGET_USER, [IDENT, "y" * 200])
    cfg = _cfg(tmp_path)
    ex, res = _run(cfg, [{
        "action": "split", "target": "user", "index": 0,
        "keep": "COMPLETELY UNRELATED TEXT ABOUT WEATHER",
        "routes": [],
    }])
    entries = st.read_entries(st.TARGET_USER)
    assert IDENT in entries, "the identity core was replaced by unrelated text"
    assert not any("UNRELATED" in e for e in entries), entries


# --- mutant: guard-unknown-target-off --------------------------------------


def test_an_unknown_target_is_refused_before_any_side_effect(tmp_path):
    st.write_entries(st.TARGET_MEMORY, ["KEEPER ENTRY"])
    cfg = _cfg(tmp_path)
    ex, res = _run(cfg, [{
        "action": "evict-to-quarantine", "target": "nonsense",
        "index": 0, "text": "x",
    }])
    assert st.read_entries(st.TARGET_MEMORY) == ["KEEPER ENTRY"]
    assert any("nonsense" in e for e in res["errors"]), res["errors"]


# --- mutant: guard-memory-empty-off ----------------------------------------
# The "never empty memory" rule.


def test_memory_is_never_emptied_completely(tmp_path):
    st.write_entries(st.TARGET_MEMORY, [IDENT, "filler " * 40])
    cfg = _cfg(tmp_path)
    ex, res = _run(cfg, [
        {"action": "evict-to-quarantine", "target": "memory", "index": 0,
         "text": "x"},
        {"action": "evict-to-quarantine", "target": "memory", "index": 1,
         "text": "x"},
    ])
    assert st.read_entries(st.TARGET_MEMORY), (
        f"memory was emptied entirely: {res['errors']}"
    )


# --- mutant: executor-yamlquote-off ---------------------------------------


def test_a_routed_skill_has_parsable_frontmatter(tmp_path):
    """An unquoted description containing ': ' makes the skill invisible."""
    st.write_entries(st.TARGET_MEMORY, ["FACT: with a colon in it"])
    cfg = _cfg(tmp_path)
    ex, res = _run(cfg, [{
        "action": "route-to-skill", "target": "memory", "index": 0,
        "skill_name": "yaml-quote-probe", "text": "FACT: with a colon",
    }])
    skill = cfg.skills_root / "tools" / "yaml-quote-probe" / "SKILL.md"
    assert skill.is_file(), res["errors"]
    import yaml
    head = skill.read_text(encoding="utf-8").split("---")[1]
    data = yaml.safe_load(head)
    assert data["name"] == "yaml-quote-probe"
    assert ":" in data["description"], data


# --- mutant: executor-skill-overwrite-ok ----------------------------------
# I verified this one is ALREADY guarded; the test pins that so it stays.


def test_routing_never_clobbers_a_hand_written_skill(tmp_path):
    skill = (
        _cfg(tmp_path).skills_root / "tools" / "precious" / "SKILL.md"
    )
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("---\nname: precious\n---\n\nORIGINAL", encoding="utf-8")
    st.write_entries(st.TARGET_MEMORY, ["ORIGINAL"])
    cfg = _cfg(tmp_path)
    ex, res = _run(cfg, [{
        "action": "route-to-skill", "target": "memory", "index": 0,
        "skill_name": "precious", "text": "ORIGINAL",
    }])
    assert skill.read_text(encoding="utf-8").endswith("ORIGINAL"), (
        "a hand-written skill was overwritten by triage"
    )


# --- mutant: store-strict-no-raise ----------------------------------------
# read_entries_strict returning [] instead of raising turns a transiently
# unreadable store into a valid-looking EMPTY one, and the write then wipes
# the file (the bug that motivated StoreUnreadable in the first place).


def test_an_unreadable_non_empty_store_raises_rather_than_reading_empty(tmp_path):
    """A latin-1 byte makes the file undecodable as utf-8.

    read_entries used to swallow that and return [], which looks identical
    to a genuinely empty store -- and the write that follows then replaces
    the file with nothing, destroying it. That is the empty-store wipe this
    exception exists to prevent, so the test must produce a file that is
    REALLY undecodable, not merely unusual.
    """
    p = st.path_for(st.TARGET_MEMORY)
    p.write_bytes(
        (IDENT + "\n\u00a7\nsomething real\n").encode("utf-8")
        + b"\xff\xfe not utf-8 \x80"
    )
    with pytest.raises(st.StoreUnreadable):
        st.read_entries_strict(st.TARGET_MEMORY)
    # And the file must be untouched by the failed read.
    assert p.stat().st_size > len(IDENT), "the file was modified"


# --- mutant: store-write-nonatomic ----------------------------------------
# A non-atomic write means a crash mid-write tears the store. What is
# observable from a test: a failed write must leave the original intact.


def test_a_failed_write_leaves_the_original_file_intact(tmp_path, monkeypatch):
    st.write_entries(st.TARGET_MEMORY, ["ORIGINAL CONTENT"])
    p = st.path_for(st.TARGET_MEMORY)
    before = p.read_text(encoding="utf-8")

    real_replace = st.os.replace

    def boom(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(st.os, "replace", boom)
    with pytest.raises(OSError):
        st.write_entries(st.TARGET_MEMORY, ["TOTALLY NEW CONTENT"])
    monkeypatch.setattr(st.os, "replace", real_replace)
    assert p.read_text(encoding="utf-8") == before, (
        "a failed write tore the store"
    )


# --- mutant: triage-threshold-inverted -----------------------------------
# is_over_threshold inverted means triage never fires on a full store, which
# looks exactly like "the plugin is doing nothing".


def test_a_full_store_is_detected_as_over_threshold(tmp_path):
    from memtriage import state
    from memtriage.config import Config
    cfg = Config()
    st.write_entries(st.TARGET_MEMORY, ["x" * (st.char_limit("memory") + 50)])
    try:
        assert state.is_over_threshold(cfg), (
            "a store over its limit was not detected"
        )
    finally:
        st.write_entries(st.TARGET_MEMORY, ["seed"])
