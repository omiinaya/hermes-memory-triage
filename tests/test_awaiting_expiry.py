"""One unapproved plan used to wedge auto mode forever.

The hook skipped whenever `awaiting_approval` was set. Nothing in the
codebase ever unset it -- `clear_awaiting()` had zero call sites -- so one
manual-mode plan, or one auto run that asked for review, left the store over
budget with the plugin reporting nothing at all, indefinitely.

A pending plan still BLOCKS, and that is correct: acting on a second plan
while a first is unreviewed means two sets of writes racing one store. What
was missing is that the block could never lift on its own.
"""
import os
import pathlib
import time

import pytest

from memtriage import config as config_mod
from memtriage import state as state_mod


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    # The autouse `isolated_env` fixture has already created and exported
    # HERMES_HOME/MEMTRIAGE_HOME under tmp_path. Re-mkdir'ing them here
    # raised FileExistsError, so read the roots the fixture established
    # rather than assuming a clean tmp_path.
    hermes = pathlib.Path(os.environ["HERMES_HOME"])
    data = pathlib.Path(os.environ["MEMTRIAGE_HOME"])
    c = config_mod.Config.load()
    c.data_dir = data
    return c


def test_a_fresh_plan_still_blocks(cfg):
    """The brake must hold. Expiry is not a licence to act on a new plan."""
    state_mod.mark_awaiting_approval(cfg, "RUN-A")
    assert state_mod.awaiting_approval(cfg) == "RUN-A"
    assert not state_mod.awaiting_approval_expired(cfg, now=time.time())


def test_a_stale_plan_expires(cfg):
    state_mod.mark_awaiting_approval(cfg, "RUN-A")
    later = time.time() + (cfg.cooldown_minutes * 60) + 1
    assert state_mod.awaiting_approval_expired(cfg, now=later)


def test_expiry_is_reported_not_silent(cfg):
    """clear_awaiting drops the key; the caller must have said something."""
    state_mod.mark_awaiting_approval(cfg, "RUN-A")
    state_mod.clear_awaiting(cfg)
    assert state_mod.awaiting_approval(cfg) is None


def test_an_undated_pending_plan_expires(cfg):
    """A plan that predates the timestamp cannot be aged, so it must not
    hold the brake on forever. Silently keeping it was the original bug."""
    import json

    state_mod.mark_awaiting_approval(cfg, "RUN-OLD")
    path = cfg.state_path
    st = json.loads(path.read_text())
    st.pop("awaiting_approval_at", None)
    path.write_text(json.dumps(st))
    assert state_mod.awaiting_approval_expired(cfg, now=time.time())


def test_nothing_pending_never_expires(cfg):
    assert not state_mod.awaiting_approval_expired(cfg, now=time.time())


def test_a_second_plan_does_not_clobber_a_pending_one(cfg):
    """Pre-existing rule, kept: the first unreviewed plan must survive."""
    state_mod.mark_awaiting_approval(cfg, "RUN-A")
    state_mod.mark_awaiting_approval(cfg, "RUN-B")
    assert state_mod.awaiting_approval(cfg) == "RUN-A"
