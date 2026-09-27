"""Regression tests for the 2026-09-27 safety audit.

Every test here reproduces a defect found by audit and CONFIRMED by executing
the plugin's own code. They all run against a temp HERMES_HOME / MEMTRIAGE_HOME
(see ``_cfg``) so they can never touch the real memory store.

The theme of every one of these: the plugin must REFUSE rather than guess. An
over-eager eviction, a stale index, or an ambiguous model reply that ends in
silent data loss is strictly worse than a triage run that does nothing.
"""

import json

import pytest

from memtriage import cerveau, store as memory_store
from memtriage.config import Config
from memtriage.executor import Executor
from memtriage.plan import PlanValidationError, validate


def _cfg(tmp_path, monkeypatch):
    """Isolated config: temp HERMES_HOME + temp plugin data dir."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("MEMTRIAGE_HOME", str(tmp_path / "data"))
    return Config(
        data_dir=tmp_path / "data",
        scripts_dir=str(tmp_path / "scripts"),
        provider_base_url="http://127.0.0.1:1",  # closed port -> pending
    )


# -- 1. an unreadable store must not be treated as an empty store ----------

def test_unreadable_store_aborts_instead_of_wiping_it(tmp_path, monkeypatch):
    """A store that exists but cannot be READ must never be overwritten.

    Regression: read_entries() swallowed OSError/UnicodeDecodeError and
    returned [], execute_plan rebuilt from that phantom empty snapshot, and
    wrote back a store containing only the plan's appends. A single transient
    EACCES/EBUSY/concurrent-replace could therefore replace the entire user
    profile. The realistic trigger is a read that raises, not a parse issue.
    """
    cfg = _cfg(tmp_path, monkeypatch)
    memory_store.write_entries("user", ["Omar Minaya — cyber-name SULLEN"])
    path = memory_store.path_for("user")

    real_read = memory_store.path_for("user").read_text

    def exploding_read(self, *a, **kw):
        if self.name == "USER.md":
            raise PermissionError(13, "Permission denied")
        return real_read(self, *a, **kw)

    monkeypatch.setattr(
        type(memory_store.path_for("user")), "read_text", exploding_read,
    )

    ex = Executor(cfg)
    summary = ex.execute_plan(
        [{"action": "route-to-provider", "target": "user", "index": 0,
          "text": "anything"}],
        "test-run", "provenance:p",
    )
    assert any("ABORTED" in e for e in summary["errors"]), summary["errors"]


def test_read_entries_strict_distinguishes_empty_from_unreadable(tmp_path, monkeypatch):
    """Absent and genuinely-empty both yield []; a raising read must raise."""
    cfg = _cfg(tmp_path, monkeypatch)
    # Genuinely absent -> [] is correct and must not raise.
    assert memory_store.read_entries_strict("user") == []
    # Genuinely empty -> [] is correct.
    memory_store.path_for("user").parent.mkdir(parents=True, exist_ok=True)
    memory_store.path_for("user").write_text("", encoding="utf-8")
    assert memory_store.read_entries_strict("user") == []
    # Present but unreadable -> must raise, not fake an empty store.
    memory_store.path_for("user").write_text("real content", encoding="utf-8")

    def boom(self, *a, **kw):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(
        type(memory_store.path_for("user")), "read_text", boom,
    )
    with pytest.raises(memory_store.StoreUnreadable):
        memory_store.read_entries_strict("user")


# -- 2. the fallback must not merge across targets -------------------------

def test_fallback_does_not_merge_entries_across_targets():
    """memory#0 and user#1 sharing a subject key must not produce one merge.

    Regression: seen_subject was keyed on the subject alone and declared
    outside the per-target loop, so the emitted consolidate carried
    target="memory" with entries=[0, 1] where 1 was a USER index. The executor
    then removed memory#1 — an uninvolved entry — with no quarantine record.
    """
    inv = {"memory": [
        {"target": "memory", "entries": [
            {"index": 0, "text": "relay pool on tor is per-app egress", "chars": 34},
            {"index": 1, "text": "PVE host services belong on the host", "chars": 35},
        ]},
        {"target": "user", "entries": [
            {"index": 0, "text": "pve host is the pve host", "chars": 23},
            {"index": 1, "text": "relay pool on tor is per-app egress", "chars": 34},
        ]},
    ]}
    actions = cerveau._deterministic_plan(inv)
    for a in actions:
        if a.get("action") == "consolidate":
            # A consolidate may only reference indices of its OWN target.
            target = a["target"]
            block = next(
                b for b in inv["memory"] if b["target"] == target
            )
            valid = {e["index"] for e in block["entries"]}
            assert all(i in valid for i in a["entries"]), (
                f"consolidate on {target} references foreign indices {a['entries']}"
            )


def test_fallback_refuses_to_merge_truncated_inventory_text():
    """The inventory caps memory entry text at 160 chars — never merge on that.

    Regression: the merge body was the inventory's truncated copy, so
    consolidating a 1,280-char entry replaced it with its first 160 chars and
    the other 1,120 characters were discarded with no quarantine record.
    """
    long_text = "relay pool on tor is per-app egress " + ("x" * 1200)
    capped = long_text[:160]
    inv = {"memory": [{
        "target": "memory",
        "entries": [
            {"index": 0, "text": capped, "chars": len(long_text)},
            {"index": 1, "text": capped + " variant", "chars": len(long_text) + 8},
        ],
    }]}
    actions = cerveau._deterministic_plan(inv)
    assert not any(a.get("action") == "consolidate" for a in actions), actions
    # It degrades to keep, with the reason stated.
    assert any("truncated" in a.get("reason", "") for a in actions)


# -- 3. marker matching must be on word boundaries -------------------------

def test_low_priority_markers_do_not_match_latest():
    """'test ' must not match 'latest'.

    Regression: LOW_PRIORITY_MARKERS contained the substring "test ", which
    matched "latest" inside ordinary doctrine. In auto mode with force=True
    those entries were evicted to quarantine unapproved.
    """
    for text in (
        "PVE host 3090: latest nvidia driver pinned",
        "oem-ui is the OFFICIAL house design system; latest tokens",
        "protest the config layout",
    ):
        assert not cerveau._matches(text, cerveau.LOW_PRIORITY_MARKERS), text
    # Genuine low-priority signals must still match.
    for text in ("this is a throwaway note", "dead code in the relay", "tmp file"):
        assert cerveau._matches(text, cerveau.LOW_PRIORITY_MARKERS), text


# -- 4. stale / retried plans must not delete unrelated entries ------------

def test_stale_plan_does_not_delete_a_shifted_entry(tmp_path, monkeypatch):
    """Re-applying a plan after the store shifted must not delete the wrong entry.

    Removals are keyed by the text the plan expected at that index. If the
    index now holds something else, the removal is refused.
    """
    cfg = _cfg(tmp_path, monkeypatch)
    memory_store.write_entries("memory", ["original entry zero", "entry one"])
    ex = Executor(cfg)
    # The plan states the text it expects at index 0.
    plan = [{"action": "evict-to-quarantine", "target": "memory", "index": 0,
             "text": "original entry zero", "reason": "stale"}]
    ex.execute_plan(plan, "run-1", "provenance:p")
    assert memory_store.read_entries("memory") == ["entry one"]

    # A concurrent write inserts a NEW entry at index 0.
    memory_store.write_entries("memory", ["brand new entry", "entry one"])

    # A DIFFERENT run acting on the same (now shifted) index must refuse:
    # the plan expected "original entry zero" and something else is there.
    shifted = [{"action": "evict-to-quarantine", "target": "memory", "index": 0,
                "text": "original entry zero", "reason": "stale"}]
    summary = ex.execute_plan(shifted, "run-2", "provenance:p")
    assert any("stale" in e for e in summary["errors"]), summary["errors"]
    assert "brand new entry" in memory_store.read_entries("memory")


def test_replaying_the_same_run_id_is_refused(tmp_path, monkeypatch):
    """Positional indices make a replay unsafe — refuse it outright."""
    cfg = _cfg(tmp_path, monkeypatch)
    memory_store.write_entries("memory", ["entry zero", "entry one"])
    ex = Executor(cfg)
    plan = [{"action": "evict-to-quarantine", "target": "memory", "index": 0,
             "text": "entry zero", "reason": "stale"}]
    first = ex.execute_plan(plan, "run-1", "provenance:p")
    assert memory_store.read_entries("memory") == ["entry one"]
    # Same run id, same plan -> must NOT be replayed.
    memory_store.write_entries("memory", ["fresh entry", "entry one"])
    second = ex.execute_plan(plan, "run-1", "provenance:p")
    assert any("already applied" in e for e in second["errors"]), second["errors"]
    assert "fresh entry" in memory_store.read_entries("memory")


# -- 5. consolidation safety ------------------------------------------------

def test_consolidate_rejects_repeated_index():
    """entries=[0, 0] is not a merge — validate() must reject it."""
    with pytest.raises(PlanValidationError):
        validate([{"action": "consolidate", "target": "memory",
                   "entries": [0, 0], "text": "merged"}])


def test_consolidate_rejects_double_mutation_of_one_entry():
    """One mutating action per entry."""
    with pytest.raises(PlanValidationError):
        validate([
            {"action": "route-to-skill", "target": "memory", "index": 0,
             "skill_name": "x", "text": "body"},
            {"action": "evict-to-quarantine", "target": "memory", "index": 0,
             "reason": "stale"},
        ])


def test_consolidate_refuses_truncated_merge_body(tmp_path, monkeypatch):
    """The executor is the second line of defence against truncated merges."""
    cfg = _cfg(tmp_path, monkeypatch)
    full = "relay pool doctrine " + ("y" * 500)
    memory_store.write_entries("memory", [full, full + " variant"])
    ex = Executor(cfg)
    summary = ex.execute_plan(
        [{"action": "consolidate", "target": "memory", "entries": [0, 1],
          "text": full[:160],
          "_source_entries": [
              {"index": 0, "text": full[:160], "chars": len(full)},
              {"index": 1, "text": (full + " variant")[:160],
               "chars": len(full) + 8},
          ]}],
        "test-run", "provenance:p",
    )
    assert any("truncated" in e for e in summary["errors"]), summary["errors"]
    # Both full entries survive.
    assert memory_store.read_entries("memory") == [full, full + " variant"]


# -- 6. routing must persist the REAL source text, not a capped copy -------

def test_route_to_skill_persists_full_source_text(tmp_path, monkeypatch):
    """A routing action must not persist the inventory's capped paraphrase."""
    cfg = _cfg(tmp_path, monkeypatch)
    full = "relay pool doctrine: " + ("z" * 400)
    memory_store.write_entries("memory", [full, "unrelated entry"])
    monkeypatch.setattr(
        "memtriage.executor._dispatch_to_provider",
        lambda cfg, text, scene_path=None: "200 ok",
    )
    ex = Executor(cfg)
    # The action's text is a truncated paraphrase (as the inventory would give),
    # but names index 0 whose real entry is the full text.
    ex.execute_plan(
        [{"action": "route-to-provider", "target": "memory", "index": 0,
          "text": full[:160]}],
        "test-run", "provenance:p",
    )
    # Source is removed (routing is a move) — so the destination must hold the
    # FULL text, not the 160-char copy.
    assert memory_store.read_entries("memory") == ["unrelated entry"]
    ledger_entries = json.loads(
        (cfg.data_dir / "ledger.json").read_text(encoding="utf-8")
    )
    provider_entry = next(
        e for e in ledger_entries if e["kind"] == "provider"
    )
    assert len(provider_entry["summary"]) > 160 or full[:200] in provider_entry["summary"]


# -- 7. E2BIG must degrade, not crash --------------------------------------

def test_oversized_prompt_falls_back_instead_of_crashing(tmp_path, monkeypatch):
    """An argv-size overflow must reach the deterministic fallback.

    Regression: dispatch() caught only FileNotFoundError and TimeoutExpired, so
    OSError(E2BIG) escaped and aborted the whole run. The prompt is a single
    argv element and Linux caps it at MAX_ARG_STRLEN.
    """
    cfg = _cfg(tmp_path, monkeypatch)
    inv = {"memory": [{
        "target": "memory",
        "entries": [{"index": 0, "text": "some fact", "chars": 9}],
    }]}

    def boom(*a, **kw):
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr(cerveau.subprocess, "run", boom)
    actions = cerveau.dispatch(
        cfg, "x" * 200000, inventory=inv,
    )
    assert isinstance(actions, list)
    assert any(a.get("_source") == "deterministic-fallback" for a in actions)
