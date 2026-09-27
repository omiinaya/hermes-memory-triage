"""Tests for the Cerveau deterministic fallback planner.

Covers:
- _deterministic_plan produces a plan that passes plan.validate
- protected markers are NEVER evicted (doctrine: identity/security/env-critical)
- the fallback is selected by run_triage.dispatch when the model fails

ISOLATION: every test here builds its own Config against a temp data dir and
an injected inventory. The previous version called ``Config.load()`` and
``run_triage(..., dispatch=True)`` against the real ``~/.memtriage``: it read
the live store, wrote a live report, and spent up to the 600s Cerveau
timeout on a real model call. That is a live-mutating test in a unit suite,
and it is what made this file the slowest and least deterministic test in
the repo.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memtriage import cerveau as cv
from memtriage import inventory as inv
from memtriage import plan as plan_mod
from memtriage import triage as tr
from memtriage.config import Config

PLANNER_MARKERS = (
    # Mirrors the protected/identity surface the planner must respect.
    "pve", "vault", "secret", "cuda", "gpu", "hermes-agent",
)
LOW_PRIORITY_TEXT = (
    "scratch note: debug this later, not important, temporary, throwaway"
)


def _cfg(tmp_path: Path) -> Config:
    cfg = Config(mode="manual", data_dir=tmp_path / "data")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _inventory(entries):
    return {
        "memory": [{
            "target": "memory",
            "entries": [
                {"index": i, "text": t, "chars": len(t)}
                for i, t in enumerate(entries)
            ],
        }],
    }


def test_deterministic_plan_is_valid():
    cfg = Config()
    payload = _inventory([
        "PVE host doctrine: never reboot the host mid-job.",
        "same subject stated two ways: the PVE host doctrine entry.",
        LOW_PRIORITY_TEXT,
    ])
    plan = cv._deterministic_plan(payload)
    # must pass the SAME validator the model path uses
    assert plan_mod.validate(plan) == plan, "fallback plan must be schema-valid"


def test_every_entry_is_covered_exactly_once():
    payload = _inventory([
        "PVE host doctrine: never reboot mid-job.",
        "PVE host doctrine restated: never reboot mid-job.",
        LOW_PRIORITY_TEXT,
        "a third unrelated durable note about thunder storage.",
    ])
    plan = cv._deterministic_plan(payload)
    seen = []
    for a in plan:
        if a["action"] == "consolidate":
            seen.extend(a["entries"])
        else:
            seen.append(a["index"])
    assert sorted(seen) == [0, 1, 2, 3], f"orphans or duplicates: {plan}"


def test_no_protected_evicted():
    payload = _inventory([
        "PVE host doctrine: never reboot the host mid-job.",
        "vault rules: the real name is vault-only, never in chat.",
        LOW_PRIORITY_TEXT,
    ])
    plan = cv._deterministic_plan(payload)
    quarantined = {a["index"] for a in plan if a["action"] == "evict-to-quarantine"}
    protected = {0, 1}
    assert not (quarantined & protected), (
        f"protected entries were quarantined: {quarantined & protected}"
    )
    # The throwaway note IS eligible.
    assert 2 in quarantined


def test_latest_does_not_match_the_test_marker():
    """Regression: 'test ' substring-matched 'latest', so ordinary doctrine
    became an unapproved eviction candidate in auto mode."""
    payload = _inventory([
        "PVE host 3090: latest nvidia driver pinned to 275 W.",
    ])
    plan = cv._deterministic_plan(payload)
    assert not [a for a in plan if a["action"] == "evict-to-quarantine"], plan


def test_fallback_never_merges_across_targets():
    """Regression: seen_subject was keyed on subject alone, so a memory
    entry and a user entry sharing a subject produced target=memory with
    entries spanning both — the executor then removed an uninvolved entry."""
    shared = "thunder storage doctrine: all big data on mrx-thunder."
    payload = {
        "memory": [
            {"target": "memory", "entries": [
                {"index": 0, "text": shared, "chars": len(shared)},
            ]},
            {"target": "user", "entries": [
                {"index": 0, "text": shared, "chars": len(shared)},
            ]},
        ],
    }
    plan = cv._deterministic_plan(payload)
    for a in plan:
        if a["action"] == "consolidate":
            assert a["target"] in ("memory", "user")
            # A single entry per target here: nothing to merge.
            assert len(set(a["entries"])) >= 2


def test_fallback_refuses_to_merge_a_truncated_entry():
    """Regression: merging used the inventory's 160-char cap as the body,
    discarding the remainder of every source entry, unquarantined."""
    full = "PVE host doctrine: never reboot mid-job. " * 20
    payload = {
        "memory": [{
            "target": "memory",
            "entries": [
                {"index": 0, "text": full[:160], "chars": len(full)},
                {"index": 1, "text": full[:160], "chars": len(full)},
            ],
        }],
    }
    plan = cv._deterministic_plan(payload)
    assert not [a for a in plan if a["action"] == "consolidate"], (
        "a truncated inventory copy must never become a merge body"
    )


def test_run_triage_uses_fallback_when_cerveau_fails(tmp_path, monkeypatch):
    """dispatch() itself must degrade, not run_triage.

    Patching dispatch would bypass the very handler under test, so this
    raises from subprocess.run and lets the real dispatch decide.
    """
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(
        tr.inventory_mod, "collect",
        lambda c: _inventory([LOW_PRIORITY_TEXT]),
    )
    monkeypatch.setattr(
        cv.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="hermes", timeout=1)
        ),
    )
    result = tr.run_triage(cfg, "test: forced fallback", force=True)
    assert result["dispatcher"] == "deterministic-fallback", result.get("dispatcher")
    assert any(a.get("_source") == "deterministic-fallback" for a in result["plan"])
    # And it wrote nothing to a live store: manual mode only persists a plan.
    assert result["execution"] is None


def test_dispatch_survives_an_argv_overflow(tmp_path, monkeypatch):
    """Regression: E2BIG escaped every handler and crashed the run.

    The prompt is one argv element; Linux caps one at MAX_ARG_STRLEN. An
    over-cap inventory raised a bare OSError that skipped the fallback, so a
    triage died instead of degrading.
    """
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(
        cv.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(OSError(7, "Argument list too long")),
    )
    out = cv.dispatch(cfg, "a very large prompt", inventory=_inventory(["x"]))
    assert out and all(a.get("_source") == "deterministic-fallback" for a in out)


def test_dispatch_decodes_non_utf8_output(tmp_path, monkeypatch):
    """Regression: a child whose bytes are not valid UTF-8 must not abort.

    dispatch passes encoding="utf-8", errors="replace" to subprocess.run, so
    the parser always receives str. This test pins THAT contract: if the
    encoding/errors kwargs are ever dropped, a single non-UTF-8 byte in a
    memory entry raises UnicodeDecodeError straight out of the run and the
    deterministic fallback is skipped.
    """
    cfg = _cfg(tmp_path)
    seen = {}

    def fake_run(*a, **k):
        seen.update(k)
        # Emulate what subprocess.run returns GIVEN those kwargs.
        out = b'[{"action": "keep", "target": "memory", "index": 0, "text": "caf\xe9"}]'
        return subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=out.decode(k.get("encoding") or "utf-8",
                              k.get("errors") or "strict"),
            stderr="",
        )

    monkeypatch.setattr(cv.subprocess, "run", fake_run)
    out = cv.dispatch(cfg, "prompt", inventory=_inventory(["x"]))
    assert seen.get("encoding") == "utf-8"
    assert seen.get("errors") == "replace"
    assert out and isinstance(out[0].get("text", ""), str)
    assert "caf" in out[0]["text"]  # replacement char, not a crash


if __name__ == "__main__":
    print("use pytest: python -m pytest tests/test_cerveau_fallback.py -q")
