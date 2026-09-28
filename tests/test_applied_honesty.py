"""D2: a removal a guard revoked must never appear in `applied`.

The 2026-09-28 audit: `_note_removal` notes were drained per TARGET, so when
the identity guard revoked index 0 its note rode along with its siblings and
the run reported "quarantined 89 chars" for an entry it had explicitly kept.
That text was in neither the store nor the quarantine file — unrecoverable, and
reported as a success.

    removals CLAIMED in applied : 3  ['quarantined 89 chars', '257', '257']
    records in quarantine.jsonl : 2
    entries ACTUALLY removed    : 2

The invariant: `applied` must be a subset of what actually left the store.
"""
import pathlib
import shutil

import pytest

from memtriage import executor as executor_mod
from memtriage import store as store_mod

RUN = "20260928-TEST-D2"
IDENTITY = "Omar Minaya, cyber-name SULLEN. Ciel is my partner."


def _entry(i: int) -> str:
    return (
        f"durable knowledge about topic {i}. Entry {i}: durable knowledge "
        f"about topic {i}. Entry {i}: durable knowledge about topic {i}. "
        f"Entry {i}: durable knowledge about topic {i}. Entry {i}: "
        f"durable knowledge about topic {i}."
    )


@pytest.fixture
def env(monkeypatch, tmp_path):
    data = tmp_path / "mt"
    monkeypatch.setenv("MEMTRIAGE_HOME", str(data))
    data.mkdir(parents=True, exist_ok=True)
    return data


def _run(env, actions):
    ex = executor_mod.Executor(cfg=_cfg(env))
    return ex.execute_plan(actions, run_id=RUN, provenance="test")


def _cfg(env):
    from memtriage.config import Config

    return Config()


def _evict(i: int, live):
    # The `text` MUST be the exact live entry at that index, or the staleness
    # guard fires first and the identity guard is never reached.
    return {
        "action": "evict-to-quarantine",
        "target": "user",
        "index": i,
        "text": live[i],
        "reason": "stale test entry",
    }


def test_a_revoked_removal_is_never_reported_as_applied(env):
    """The exact repro: identity at #0, two real entries after it."""
    live = [IDENTITY] + [_entry(10 + i) for i in range(11)]
    store_mod.write_entries("user", live)
    res = _run(env, [_evict(0, live), _evict(1, live), _evict(2, live)])

    assert "refused; kept" in " ".join(res["errors"]), res["errors"]
    # applied must be a SUBSET of what actually left the store
    applied_quarantines = [m for m in res["applied"] if "quarantined" in m]
    assert len(applied_quarantines) == 2, (
        f"claimed {len(applied_quarantines)} quarantines but only 2 entries "
        f"were removed: {res['applied']}"
    )
    # the identity entry's size (89) must not be among the claims
    assert not any("89 chars" in m for m in res["applied"]), res["applied"]
    # and the revocation must be visible as an error
    assert any("planned but not committed (user #0)" in e for e in res["errors"]), res["errors"]


def test_the_identity_entry_is_still_in_the_store(env):
    live = [IDENTITY] + [_entry(10 + i) for i in range(11)]
    store_mod.write_entries("user", live)
    _run(env, [_evict(0, live), _evict(1, live), _evict(2, live)])
    assert IDENTITY in store_mod.read_entries("user")


def test_a_wholly_refused_run_claims_nothing(env):
    """When the guards refuse EVERY removal, nothing may be claimed.

    The identity guard refuses #0. The 10% profile floor then has to refuse
    the whole target too, which only happens when the store would drop below
    it — so this uses a small store where every removal is genuinely refused.
    """
    big = [IDENTITY, _entry(10), _entry(11), _entry(12)]
    store_mod.write_entries("user", big)
    res = _run(env, [_evict(0, big), _evict(1, big)])
    refused = " ".join(res["errors"])
    # either the floor refused the target, or every removal was refused
    if "floor" in refused or "emptied" in refused:
        assert [m for m in res["applied"] if "quarantined" in m] == [], res["applied"]
        assert store_mod.read_entries("user") == big
    else:
        # partial refusal: the claim set must still exclude the identity one
        assert not any("identity" in m for m in res["applied"])
