"""Regression tests for the 2026-09-28 17:45 incident (D3).

An unattended auto run used `consolidate` and left USER.md at 4,771 chars
against a 3,000 limit (159%), with duplicated entries, while reporting
success. Two guards failed to stop it, and neither had a test that could
fail:

  * the over-limit guard logged "appends dropped" and then kept them, so
    the store grew;
  * the truncation guard read `_source_entries`, a key nothing in
    production writes, so it never fired.

The mutation run that accompanied the fix reported the first of these as
SURVIVED, which is what these tests exist to correct.
"""

import pytest

from memtriage import store as memory_store
from memtriage.config import Config
from memtriage.executor import Executor


def _cfg(tmp_path, monkeypatch):
    """Isolated config: temp HERMES_HOME + temp plugin data dir.

    Copied verbatim from test_safety_regressions so this file cannot
    accidentally inherit a fixture shape that reaches the live store.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("MEMTRIAGE_HOME", str(tmp_path / "data"))
    return Config(
        data_dir=tmp_path / "data",
        scripts_dir=str(tmp_path / "scripts"),
        provider_base_url="http://127.0.0.1:1",  # closed port -> pending
    )


def _over_budget_user_store():
    """A user store already past its limit, like the live one at 159%."""
    return [
        "Omar Minaya, cyber-name SULLEN. Ciel is my partner, never 'Papa'. "
        + "VIEWING: iPhone Safari WebKit matters. " * 30,
        "Omar Minaya, cyber-name SULLEN. Ciel is my partner, never 'Papa'. "
        + "CONVENTIONS: commit EARLY+OFTEN and push EVERY commit. " * 30,
        "commit EARLY+OFTEN and push EVERY commit, authored omiinaya. "
        + "LAN-BIND: always bind to 0.0.0.0, never localhost. " * 30,
        "a 338-char source entry. " * 14,
        "a second 338-char source entry. " * 13,
    ]


def test_over_limit_guard_does_not_grow_an_already_over_budget_store(
    tmp_path, monkeypatch
):
    """A consolidate on a store that is ALREADY over limit must be a no-op.

    This is the exact shape of the 17:45 incident. The store starts over
    budget, so every tempting "fix" is a growth move; the guard has to
    decline the whole thing rather than trade a source for a bigger
    replacement.
    """
    cfg = _cfg(tmp_path, monkeypatch)
    base = _over_budget_user_store()
    memory_store.write_entries("user", base)
    before = memory_store.read_entries("user")
    before_chars = memory_store.char_count(before)
    assert before_chars > memory_store.char_limit("user"), (
        "fixture must actually be over budget for this to mean anything"
    )

    merged = "a 1721-char consolidated summary. " * 60
    summary = Executor(cfg).execute_plan(
        [{"action": "consolidate", "target": "user",
          "entries": [3, 4], "text": merged}],
        "test-run", "provenance:p",
    )

    after = memory_store.read_entries("user")
    assert memory_store.char_count(after) == before_chars, (
        f"store grew from {before_chars} to {memory_store.char_count(after)} "
        f"chars while the guard reported dropping the appends"
    )
    assert after == before, "the sources must survive intact"
    # And it must SAY so, not claim the consolidation happened.
    assert summary["applied"] == [], summary["applied"]
    assert any("not committed" in e for e in summary["errors"]), summary["errors"]


def test_over_limit_guard_keeps_a_unique_clause_but_drops_the_replacement(
    tmp_path, monkeypatch
):
    """Provenance decides: a replacement goes, a unique clause stays.

    A split's retained clause exists nowhere else, so dropping it loses
    text permanently. A consolidate's merge is a REPLACEMENT -- its
    sources are still in the store -- so dropping it loses nothing. An
    earlier fix of mine dropped both and broke the clause case.
    """
    cfg = _cfg(tmp_path, monkeypatch)
    long_a = "a long source entry that will push the store over budget. " * 30
    long_b = "a second long source entry pushing the store over budget. " * 30
    memory_store.write_entries("user", [long_a, long_b, long_b + " tail"])

    summary = Executor(cfg).execute_plan(
        [
            # replacement: its sources (#0, #1) are not being removed, so
            # dropping this append loses nothing.
            {"action": "consolidate", "target": "user",
             "entries": [0, 1], "text": "a merged summary. " * 80},
            # unique clause: split #2, keeping a part and routing the rest
            # to a provider that is unreachable, so the routed text comes
            # back as a retained append that exists nowhere else.
            {"action": "split", "target": "user", "index": 2,
             "keep": long_b + " tail",
             "routes": [{"action": "route-to-provider",
                         "text": "UNIQUE RETAINED CLAUSE " + "q" * 3000}],
             "reason": "test"},
        ],
        "test-run", "provenance:p",
    )

    entries = memory_store.read_entries("user")
    texts = " ".join(entries)
    assert "UNIQUE RETAINED CLAUSE" in texts, (
        "a unique retained clause must survive an over-limit guard; "
        f"entries now: {[e[:40] for e in entries]}"
    )
    assert "a merged summary." not in texts, (
        "the replacement merge must be dropped when the store is over limit"
    )
    # The sources of the dropped replacement must still be there. Compare
    # stripped: the store serializer trims trailing whitespace, so an exact
    # `in` test fails on a one-character difference and reports a phantom
    # data loss.
    stripped = {e.strip() for e in entries}
    assert long_a.strip() in stripped and long_b.strip() in stripped, (
        "dropping a replacement must not take its sources with it"
    )
    # This combination grows the store (the clause is real content and the
    # store is already over budget), so the run must not claim it freed
    # anything. See the negative-"freed" note in the D3 audit entry.
    assert not any("freed -" in a for a in summary["applied"]), (
        f"a run that GREW the store must not report it as freed: "
        f"{summary['applied']}"
    )


def test_consolidate_refuses_to_merge_from_a_truncated_inventory_view(
    tmp_path, monkeypatch
):
    """A merge shorter than a source the model only saw truncated is refused.

    The inventory caps entry text, so the model can propose a merge built
    from a summary of a truncated copy. That silently discards the rest of
    the entry, so the guard compares against the live entry and asks the
    inventory what it actually showed.
    """
    cfg = _cfg(tmp_path, monkeypatch)
    full = "relay pool doctrine " + ("y" * 500)
    memory_store.write_entries("memory", [full, full + " variant"])

    summary = Executor(cfg).execute_plan(
        [{"action": "consolidate", "target": "memory", "entries": [0, 1],
          "text": full[:160]}],
        "test-run", "provenance:p",
    )

    assert any("truncated" in e for e in summary["errors"]), summary["errors"]
    assert memory_store.read_entries("memory") == [full, full + " variant"]


def test_consolidate_allows_a_short_merge_of_short_entries(tmp_path, monkeypatch):
    """The counterpart: the guard must NOT fire on a legitimate small merge.

    Three candidate rules (size ratio against the true source, the same
    against the capped view, and word overlap) each refused this case, so
    it is pinned explicitly.
    """
    cfg = _cfg(tmp_path, monkeypatch)
    one, two = "old fact one", "old fact two"
    memory_store.write_entries("memory", [one, two])

    summary = Executor(cfg).execute_plan(
        [{"action": "consolidate", "target": "memory", "entries": [0, 1],
          "text": "merged fact"}],
        "test-run", "provenance:p",
    )

    assert summary["errors"] == [], summary["errors"]
    assert memory_store.read_entries("memory") == ["merged fact"]
