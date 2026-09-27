"""Wave 2 regressions: split, blocked reporting, locking, retention, learning.

Each test here was written against a defect confirmed by executing the
plugin's own code in an isolated temp HERMES_HOME. None of them touch the
live store.
"""

from __future__ import annotations

import json
import os
import re
import string
import threading
import time
from pathlib import Path

import pytest

from memtriage import ledger as ledger_mod
from memtriage import learning as learning_mod
from memtriage import locking, retention
from memtriage import store as memory_store
from memtriage.config import Config
from memtriage.executor import Executor
from memtriage.plan import PlanValidationError, validate


def test_suite_never_touches_the_live_store():
    """The autouse fixture must actually redirect the store.

    A test that leaks HERMES_HOME writes to the user's real MEMORY.md /
    USER.md. That happened during this work: an early version of
    test_wave2.py created a real skill under ~/.hermes/skills and rewrote
    the live user store. This asserts the redirection is in force, so a
    future conftest regression fails loudly instead of silently mutating
    the user's memory.
    """
    from pathlib import Path as _P

    live = _P(os.path.expanduser("~/.hermes")) / "memories"
    resolved = memory_store.memories_dir().resolve()
    assert resolved != live.resolve(), (
        f"store is still resolving to the LIVE install: {resolved}"
    )
    assert "pytest" in str(resolved) or str(resolved).startswith("/tmp"), (
        f"store resolved somewhere unexpected: {resolved}"
    )


# -- prompt template integrity ------------------------------------------

def test_prompt_template_has_no_unescaped_braces():
    """A new example with a raw `{` breaks the prompt at RUNTIME only.

    The template is rendered with str.format, so a literal JSON brace in the
    taxonomy becomes a KeyError the first time a real triage dispatches —
    which is exactly when nobody is running the unit tests. Guard it.

    Detection is done by the formatter itself, not a regex: `{{` is a legal
    escape and a naive scanner flags it, which is how the first version of
    this test produced a false positive on a correct template.
    """
    from memtriage.cerveau import PROMPT_TEMPLATE

    # A legal template renders with a dummy payload and these kwargs.
    PROMPT_TEMPLATE.format(threshold=0.75, payload="{}", ledger="[]")
    # An unescaped brace names a missing field, so the real render fails.
    from memtriage.cerveau import build_prompt

    out = build_prompt(Config(), {"memory": []}, [])
    assert "split" in out


# -- split action ---------------------------------------------------------

IDENTITY_BLOB = (
    "Omar Minaya, cyber-name SULLEN. Ciel is my partner, never 'Papa'. "
    "VIEWING: I view oem sites on my iPHONE in Safari, so WebKit is the "
    "engine that matters - verify layout in WebKit at an iPhone viewport. "
    "CONVENTIONS: commit EARLY+OFTEN and push EVERY commit, authored "
    "omiinaya, never root. LAN-BIND: always bind to 0.0.0.0, never localhost."
)


def _user_cfg(tmp_path: Path) -> Config:
    cfg = Config(mode="auto", data_dir=tmp_path / "data")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    memory_store.write_entries(memory_store.TARGET_USER, [IDENTITY_BLOB])
    return cfg


def _oversized_user_cfg(tmp_path: Path) -> Config:
    """A user store genuinely AT its wall, so over_budget is meaningful."""
    cfg = Config(mode="auto", data_dir=tmp_path / "data")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    # char_limit reads the built-in tool's config, which under the isolated
    # HERMES_HOME is the module default. Pad past it, not to it.
    limit = memory_store.char_limit(memory_store.TARGET_USER)
    blob = IDENTITY_BLOB + (" " + "filler doctrine clause. " * 400)
    memory_store.write_entries(
        memory_store.TARGET_USER, [blob[: int(limit * 1.05)]]
    )
    u = memory_store.usage(memory_store.TARGET_USER)
    assert u["current"] > u["limit"], u
    return cfg


def test_split_relieves_an_oversized_identity_entry(tmp_path):
    """The whole point: an identity blob that blocks every other action.

    Before `split`, the identity guard refused the entry and the store stayed
    pinned at its wall. With split, the core stays and the doctrine clauses
    are routed out, so the target actually shrinks.

    The store is padded to its real limit so the 10% user safety floor is
    satisfied by what REMAINS — otherwise the floor correctly refuses the
    plan and the test would be asserting the wrong thing.
    """
    cfg = Config(mode="auto", data_dir=tmp_path / "data")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    limit = memory_store.char_limit(memory_store.TARGET_USER)
    # The identity blob is the LAST entry and is oversized. The earlier
    # entries hold the 10% user safety floor AND leave headroom for the
    # replacement append, so the store sits just UNDER its limit here —
    # the over-budget case is covered separately by
    # test_identity_guard_refusal_is_reported_as_blocked_not_silent.
    # Size the filler from the limit, never from a magic repeat count:
    # char_limit differs between the live install and an isolated HERMES_HOME.
    room = limit - len(IDENTITY_BLOB) - 120  # 120 for the replacement append
    assert room > 0, f"identity blob alone exceeds the {limit}-char limit"
    filler = ("durable doctrine entry. " * (room // 24 + 1))[:room // 2]
    memory_store.write_entries(
        memory_store.TARGET_USER, [filler, filler, IDENTITY_BLOB]
    )
    u = memory_store.usage(memory_store.TARGET_USER)
    assert u["current"] <= u["limit"], (u["current"], u["limit"])
    before = memory_store.char_count(memory_store.read_entries(
        memory_store.TARGET_USER))
    ex = Executor(cfg)
    plan = [{
        "action": "split", "target": "user", "index": 2,
        "keep": "Omar Minaya, cyber-name SULLEN. Ciel is my partner, "
                "never 'Papa'.",
        "routes": [{
            "action": "route-to-skill", "skill_name": "house-conventions",
            "text": "VIEWING: I view oem sites on my iPHONE in Safari, so "
                    "WebKit is the engine that matters - verify layout in "
                    "WebKit at an iPhone viewport.",
        }],
        "reason": "identity core stays; doctrine clause belongs in a skill",
    }]
    result = ex.execute_plan(plan, "run-split-1", "test")

    assert not result["errors"], result["errors"]
    after_entries = memory_store.read_entries(memory_store.TARGET_USER)
    assert len(after_entries) == 3
    assert "SULLEN" in after_entries[2]
    # The routed clause must NOT still be in the store, and must be in the skill.
    assert "WebKit" not in after_entries[2]
    skill = Path(cfg.skills_root) / "tools" / "house-conventions" / "SKILL.md"
    assert skill.exists(), skill
    assert "WebKit" in skill.read_text(encoding="utf-8")
    # And the target genuinely shrank.
    after = memory_store.char_count(after_entries)
    assert after < before, f"{after} !< {before}"


def test_split_refuses_a_keep_that_is_not_in_the_source(tmp_path):
    """A split must not be able to replace a real entry with invented text."""
    cfg = _user_cfg(tmp_path)
    ex = Executor(cfg)
    result = ex.execute_plan([{
        "action": "split", "target": "user", "index": 0,
        "keep": "Something completely unrelated to the source entry.",
        "routes": [],
    }], "run-split-bad", "test")
    assert result["errors"]
    assert memory_store.read_entries(memory_store.TARGET_USER) == [IDENTITY_BLOB]


def test_split_route_failure_keeps_the_clause_in_the_store(tmp_path):
    """A failed route must not cost us the text it was carrying.

    The clause is explicitly re-appended to the replacement, because the
    source entry IS being removed — anything not successfully routed away
    would otherwise be silently lost.
    """
    cfg = Config(mode="auto", data_dir=tmp_path / "data")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    # Filler holds the 10% user safety floor, so the split's own removal is
    # what the floor evaluates. Without it the floor correctly refuses and
    # keeps the source — which is a different test entirely.
    limit = memory_store.char_limit(memory_store.TARGET_USER)
    filler = "durable doctrine entry. " * (limit // 24)
    memory_store.write_entries(
        memory_store.TARGET_USER, [filler[: limit // 2], IDENTITY_BLOB]
    )
    clause = ("VIEWING: I view oem sites on my iPHONE in Safari, so WebKit "
              "is the engine that matters.")
    ex = Executor(cfg)
    result = ex.execute_plan([{
        "action": "split", "target": "user", "index": 1,
        "keep": "Omar Minaya, cyber-name SULLEN.",
        "routes": [{
            # Not a routing action -> refused.
            "action": "consolidate", "entries": [0, 1], "text": clause,
        }],
    }], "run-split-routefail", "test")
    assert any("split route" in e for e in result["errors"])
    after = memory_store.read_entries(memory_store.TARGET_USER)
    # Two entries: the filler, plus the source replaced by keep + the
    # retained clause.
    assert len(after) == 2, after
    assert "SULLEN" in after[1]
    assert "WebKit" in after[1], "the unrouted clause must survive"
    # And it must NOT have been written anywhere as a skill.
    assert not (Path(cfg.skills_root) / "tools" / "routed-skill").exists()

def test_split_validation_requires_keep_and_routes():
    with pytest.raises(PlanValidationError):
        validate([{"action": "split", "target": "user", "index": 0,
                   "routes": [{"action": "route-to-skill"}]}])
    with pytest.raises(PlanValidationError):
        validate([{"action": "split", "target": "user", "index": 0,
                   "keep": "x", "routes": []}])
    with pytest.raises(PlanValidationError):
        validate([{"action": "split", "target": "user", "index": 0,
                   "keep": "x"}])


def test_split_is_a_valid_action():
    validate([{"action": "split", "target": "user", "index": 0,
               "keep": "x", "routes": [{"action": "route-to-skill",
                                        "skill_name": "s", "text": "t"}]}])


# -- blocked reporting ----------------------------------------------------

def test_identity_guard_refusal_is_reported_as_blocked_not_silent(tmp_path):
    """The reason this went unnoticed for two days: refusals were invisible."""
    cfg = _oversized_user_cfg(tmp_path)
    oversized = memory_store.read_entries(memory_store.TARGET_USER)[0]
    ex = Executor(cfg)
    result = ex.execute_plan([{
        "action": "evict-to-quarantine", "target": "user", "index": 0,
        "text": oversized,
    }], "run-blocked", "test")
    assert result["blocked"], "a guard refusal must surface, not vanish"
    b = result["blocked"][0]
    assert b["target"] == "user"
    assert b["index"] == 0
    assert b["chars"] == len(oversized)
    assert b["over_budget"] is True
    assert "split" in b["hint"]
    # The store is still intact.
    assert memory_store.read_entries(memory_store.TARGET_USER) == [oversized]


# -- locking --------------------------------------------------------------

def test_store_lock_is_exclusive_across_threads(tmp_path):
    target = tmp_path / "MEMORY.md"
    target.write_text("a\n", encoding="utf-8")
    order: list = []

    def hold(seconds: float, tag: str):
        with locking.store_lock(target, timeout=5.0) as ok:
            order.append((tag, ok))
            time.sleep(seconds)

    t1 = threading.Thread(target=hold, args=(0.4, "first"))
    t1.start()
    time.sleep(0.1)
    t2 = threading.Thread(target=hold, args=(0.0, "second"))
    t2.start()
    t1.join()
    t2.join()

    assert [t for t, _ in order] == ["first", "second"]
    assert all(ok for _, ok in order)


def test_store_lock_reports_timeout_instead_of_hanging(tmp_path):
    target = tmp_path / "MEMORY.md"
    target.write_text("a\n", encoding="utf-8")
    result: list = []

    def hold():
        with locking.store_lock(target, timeout=5.0):
            time.sleep(0.5)

    t = threading.Thread(target=hold)
    t.start()
    time.sleep(0.1)
    with locking.store_lock(target, timeout=0.2) as ok:
        result.append(ok)
    t.join()
    assert result == [False]


def test_executor_reports_when_it_could_not_lock(tmp_path, monkeypatch):
    cfg = Config(mode="auto", data_dir=tmp_path / "data")
    memory_store.write_entries(memory_store.TARGET_MEMORY, ["keep me"])
    memory_store.write_entries(memory_store.TARGET_USER, ["keep me too"])
    monkeypatch.setattr(
        locking, "store_lock",
        lambda *a, **k: _NullLock(),
    )
    out = Executor(cfg).execute_plan(
        [{"action": "keep", "target": "memory", "index": 0}],
        "run-nolock", "test",
    )
    assert out["lock_acquired"] is False
    assert any("lock" in e for e in out["errors"])


class _NullLock:
    def __enter__(self):
        return False

    def __exit__(self, *a):
        return False


# -- retention ------------------------------------------------------------

def test_reports_are_pruned_to_the_cap(tmp_path):
    cfg = Config(data_dir=tmp_path / "data", retain_reports=5)
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    for i in range(12):
        p = cfg.reports_dir / f"report-2026092{i}-x.md"
        p.write_text(f"run {i}", encoding="utf-8")
        os.utime(p, (1000 + i, 1000 + i))
    removed = retention.prune_reports(cfg)
    assert removed == 7
    left = sorted(p.name for p in cfg.reports_dir.iterdir())
    assert len(left) == 5
    # The NEWEST survive.
    assert "report-202609211-x.md" in left


def test_notified_runs_are_capped(tmp_path):
    cfg = Config(data_dir=tmp_path / "data", retain_notified_runs=10)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.state_path.write_text(
        json.dumps({"notified_runs": [f"r{i}" for i in range(50)]}),
        encoding="utf-8",
    )
    removed = retention.prune_notified_runs(cfg)
    assert removed == 40
    data = json.loads(cfg.state_path.read_text(encoding="utf-8"))
    assert len(data["notified_runs"]) == 10
    assert data["notified_runs"][-1] == "r49"


def test_ledger_is_capped(tmp_path):
    cfg = Config(data_dir=tmp_path / "data", retain_ledger_rows=5)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    for i in range(12):
        ledger_mod.record(cfg, kind="skill", destination=f"d{i}",
                          summary=f"s{i}", run_id=f"r{i}", provenance="p")
    removed = retention.prune_ledger(cfg)
    assert removed == 7
    rows = ledger_mod.load(cfg)
    assert len(rows) == 5
    assert rows[-1]["destination"] == "d11"


def test_enforce_all_is_safe_on_an_empty_install(tmp_path):
    cfg = Config(data_dir=tmp_path / "fresh")
    assert retention.enforce_all(cfg) == {
        "reports": 0, "notified_runs": 0, "ledger": 0,
    }


def test_retention_survives_a_corrupt_state_file(tmp_path):
    cfg = Config(data_dir=tmp_path / "data", retain_notified_runs=5)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.state_path.write_text("{not json", encoding="utf-8")
    assert retention.prune_notified_runs(cfg) == 0


# -- the learning loop that was dead -------------------------------------

def test_run_triage_records_a_learning_entry(tmp_path, monkeypatch):
    """record_decision was never called from anywhere: 0 entries in 518 runs."""
    from memtriage import triage as triage_mod

    cfg = Config(mode="auto", data_dir=tmp_path / "data")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    memory_store.write_entries(memory_store.TARGET_MEMORY, ["x" * 100])
    memory_store.write_entries(memory_store.TARGET_USER, ["y" * 100])
    # A profile dir with the seeded marker, so record_decision will write.
    prof = learning_mod.decision_profile_dir(cfg)
    (prof / "memories").mkdir(parents=True, exist_ok=True)
    (prof / "memories" / "MEMORY.md").write_text(
        "head\n\n" + learning_mod.LEARNING_MARKER + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        triage_mod.cerveau_mod, "dispatch",
        lambda *a, **k: [{"action": "keep", "target": "memory", "index": 0}],
    )
    out = triage_mod.run_triage(cfg, "test", force=True)
    assert out["execution"] is not None
    text = (prof / "memories" / "MEMORY.md").read_text(encoding="utf-8")
    # An all-keep run is the exact case that used to record nothing useful.
    assert "plan: keep×1" in text


def test_learning_entry_names_a_blocked_refusal(tmp_path, monkeypatch):
    from memtriage import triage as triage_mod

    cfg = Config(mode="auto", data_dir=tmp_path / "data")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    memory_store.write_entries(
        memory_store.TARGET_USER, [IDENTITY_BLOB])
    prof = learning_mod.decision_profile_dir(cfg)
    (prof / "memories").mkdir(parents=True, exist_ok=True)
    (prof / "memories" / "MEMORY.md").write_text(
        "head\n\n" + learning_mod.LEARNING_MARKER + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        triage_mod.cerveau_mod, "dispatch",
        lambda *a, **k: [{
            "action": "evict-to-quarantine", "target": "user", "index": 0,
            "text": IDENTITY_BLOB,
        }],
    )
    triage_mod.run_triage(cfg, "test", force=True)
    text = (prof / "memories" / "MEMORY.md").read_text(encoding="utf-8")
    assert "REFUSED by identity guard" in text
    assert "manual split" in text
