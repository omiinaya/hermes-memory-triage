"""Tests for memtriage.executor: applying plans to real surfaces."""

import os

from memtriage import ledger, quarantine, store as memory_store
from memtriage.config import Config
from memtriage.executor import Executor


def _cfg(tmp_path, monkeypatch):
    # CRITICAL isolation: point the memory store at temp dirs so no test
    # reads/writes the REAL ~/.hermes/memories/USER.md / MEMORY.md.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("MEMTRIAGE_HOME", str(tmp_path / "data"))
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


def test_out_of_range_routing_index_does_not_crash_rebuild(tmp_path, monkeypatch):
    """A routing action with a hallucinated/out-of-range source index must not
    crash the whole plan rebuild (regression: index 2 on a 2-entry store was
    crashing the 'freed chars' sum after _remove_source added it unguarded)."""
    cfg = _cfg(tmp_path, monkeypatch)
    memory_store.write_entries("user", ["entry zero", "entry one"])
    ex = _exec(cfg)
    # route-to-skill targeting user, but index 2 is out of range (store has 2)
    summary = ex.execute_plan(
        [{"action": "route-to-skill", "target": "user", "index": 2,
          "skill_name": "phantom", "text": "phantom body"}],
        "test-run", "provenance:p",
    )
    # Must not raise; the out-of-range removal is simply skipped.
    assert summary["errors"] == []
    # Both real entries survive (index 2 removes nothing).
    assert memory_store.read_entries("user") == ["entry zero", "entry one"]
    # The skill WAS written (routing side-effect), but source kept.
    assert (cfg.skills_root / "tools" / "phantom" / "SKILL.md").exists()


def test_safety_floor_prevents_emptying_target(tmp_path, monkeypatch):
    """A plan that would empty a target must be refused: the source entries
    stay in the working store (regression: auto-mode routed the ENTIRE
    2,041-char user profile to the provider, emptying USER.md).

    Uses a consolidate (no provider/network involved) that would merge the
    store down to empty-free content — deterministic and portable."""
    cfg = _cfg(tmp_path, monkeypatch)
    memory_store.write_entries("user", ["user identity core"])
    ex = _exec(cfg)
    summary = ex.execute_plan(
        # route-to-script removes the source index 0, which would drop the
        # user store below the 10% identity floor — must be refused.
        [{"action": "route-to-script", "target": "user", "index": 0,
          "script_name": "ph", "script_ext": "sh", "text": "#!/bin/sh\n"}],
        "test-run", "provenance:p",
    )
    # The removal was refused (would drop below the user floor) — entry stays.
    assert memory_store.read_entries("user") == ["user identity core"]
    assert any("floor" in e for e in summary["errors"])


def test_identity_entry_never_routed_away_even_above_floor(tmp_path, monkeypatch):
    """The identity/doctrine entry must survive a route-away even when sibling
    entries keep the user store ABOVE the 10% floor. Regression: auto-mode
    routed the giant 2,041-char identity blob to the provider while the emoji +
    disk-gate + H3 entries kept the store at 53% — above the floor — so the
    floor did NOT protect it and the identity left the working profile."""
    cfg = _cfg(tmp_path, monkeypatch)
    identity = ("Omar Minaya — cyber-name SULLEN (explicit 2026-08-13); "
                "real name GUARDED vault-only. NEVER evict. Voice boundary "
                "(2026-08-12): never adopt skills that change how Ciel talks.")
    siblings = [
        "Emoji preference (2026-08-29): prefer hearts, never the sun.",
        "Disk-space gate (2026-08-30): check filesystem room before install.",
    ]
    memory_store.write_entries("user", [identity] + siblings)
    # Simulate a SUCCESSFUL gateway write (the failure path keeps the entry
    # anyway; the guard must hold even when the write would have succeeded).
    monkeypatch.setattr(
        "memtriage.executor._dispatch_to_provider",
        lambda cfg, text, scene_path=None: "200 ok",
    )
    ex = _exec(cfg)
    summary = ex.execute_plan(
        # Model proposes routing the WHOLE identity entry (idx 0) to provider,
        # while siblings keep the store above the floor.
        [{"action": "route-to-provider", "target": "user", "index": 0,
          "text": identity}],
        "test-run", "provenance:p",
    )
    entries = memory_store.read_entries("user")
    assert identity in entries, "identity entry was routed away!"
    assert len(entries) == 3, "siblings must remain too"
    assert any("identity" in e for e in summary["errors"])