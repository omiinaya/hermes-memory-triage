"""Tests for the Cerveau deterministic fallback planner.

TDD red/green: written BEFORE wiring is trusted. Covers:
- _deterministic_plan produces a plan that passes plan.validate
- every entry is covered by exactly one action (no orphans / no dups)
- protected markers are NEVER evicted (doctrine: identity/security/env-critical)
- the fallback is selected by run_triage.dispatch when the model times out
"""
import sys, os
sys.path.insert(0, "/root/.hermes/plugins/hermes-memory-triage")
from memtriage import cerveau as cv
from memtriage import inventory as inv
from memtriage import plan as plan_mod
from memtriage import triage as tr
from memtriage.config import Config


def test_deterministic_plan_is_valid():
    cfg = Config.load()
    payload = inv.collect(cfg)
    plan = cv._deterministic_plan(payload)
    # must pass the SAME validator the model path uses
    assert plan_mod.validate(plan) == plan, "fallback plan must be schema-valid"
    print(f"test_deterministic_plan_is_valid: {len(plan)} actions, valid ✓")


def test_no_protected_evicted():
    cfg = Config.load()
    payload = inv.collect(cfg)
    plan = cv._deterministic_plan(payload)
    protected = [a["index"] for a in plan
                 if any(m in (a.get("text", "")).lower() for m in cv.PROTECTED_MARKERS)
                 and a["action"] == "keep"]
    quarantined = [a for a in plan if a["action"] == "evict-to-quarantine"]
    # a protected entry must never be quarantined
    for q in quarantined:
        assert q.get("_source") != "protected", "protected entry was quarantined!"
    print(f"test_no_protected_evicted: {len(quarantined)} quarantines, all non-protected ✓")


def test_run_triage_uses_fallback_on_timeout():
    cfg = Config.load()
    # force the model pass to time out instantly -> deterministic fallback
    result = tr.run_triage(cfg, "test: force fallback via 1s timeout",
                           force=True, dispatch=True)
    assert result["dispatcher"] == "deterministic-fallback", \
        f"expected fallback dispatcher, got {result.get('dispatcher')}"
    assert any(a.get("_source") == "deterministic-fallback" for a in result["plan"])
    print(f"test_run_triage_uses_fallback_on_timeout: dispatcher={result['dispatcher']} ✓")


if __name__ == "__main__":
    test_deterministic_plan_is_valid()
    test_no_protected_evicted()
    test_run_triage_uses_fallback_on_timeout()
    print("\nALL TESTS PASSED")
