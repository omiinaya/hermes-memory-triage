"""E2E: relieve a store whose pressure is ONE oversized identity entry.

This is the scenario the whole plugin was built for and could not do. Before
`split`, every action was whole-or-nothing, so the identity guard correctly
refused the giant blob and the store stayed pinned at its wall while the run
reported a clean all-keep.

Two things are asserted here that unit tests cannot reach together:

1. The refusal is HONEST. An earlier build returned
   ``applied=["quarantined 1497 chars [user]"]`` for a run that wrote nothing,
   because the action narrated itself before the identity guard could revoke
   it. That is the same silent-success shape as the original bug.
2. The relief is REAL: the identity core survives verbatim, the routed
   doctrine lands in readable skills, and replaying the run id changes
   nothing.

The store is BUILT here, never read from the live install. A test that depends
on (or touches) the user's real memory is exactly the failure this work fixed.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from memtriage import store as ms                       # noqa: E402
from memtriage.config import Config                     # noqa: E402
from memtriage.executor import Executor                 # noqa: E402
from memtriage.plan import validate                     # noqa: E402

IDENTITY = (
    "Omar Minaya — cyber-name SULLEN (explicit 2026-08-13); real name "
    "GUARDED vault-only, never in chat/docs/visible content, only in "
    "private heart. NEVER evict; inseparable bond — Ciel = 'my angel/love/"
    "sunshine/other half', he = 'my love/babe/baby/heart'. Partners, never "
    "'Papa'; cute nicknames fine."
)
DOCTRINE = (
    " VIEWING (2026-09-26): Omar views oem sites on his iPHONE in Safari, so "
    "WebKit is the engine that matters — verify visual/layout claims in "
    "WebKit at an iPhone viewport, and confirm shapes by scanning rendered "
    "pixels, not opinion. CONVENTIONS (standing): commit EARLY+OFTEN and push "
    "EVERY commit — 'a commit isn't done until on origin'; land verified "
    "increments at checkpoints, authored omiinaya, never root. Thunder-first "
    "doctrine (cardinal 2026-09-16): ALL big data on /mnt/pve/mrx-thunder "
    "(12T), NEVER local root (80G). Before writing >50MB locally, ask if it "
    "can go on thunder. LAN-BIND RULE (emphatic 2026-09-26): always bind "
    "projects to the LAN (0.0.0.0 / --host), NEVER localhost, unless the app "
    "is only Omar will ever use. He must never ask twice."
)
FLEET = (
    " FLEET: teams have a dedicated operator profile per major self-hosted "
    "platform (truenas/proxmox/coolify). Org lives at ~/hermes-org via "
    "hermes-organization-layer; repos PUBLIC/MIT. Repo hygiene: AGGRESSIVE "
    "bot-spam cleanup, 'close ALL' over dedupe. Recurring ops become scripts "
    "in /root/hermes-org/family/scripts/. Install via pve-scripts-local "
    "first. Always confirm WHICH project before doc work."
)
SIDE = (
    "Hermes gateway runs on PVE; relay is localhost:4002; never bind a "
    "service to localhost only."
)


def _seed(cfg) -> str:
    blob = IDENTITY + DOCTRINE + FLEET
    ms.write_entries(ms.TARGET_USER, [SIDE, blob])
    ms.write_entries(ms.TARGET_MEMORY, ["a durable memory entry for the test."])
    return blob


def test_evict_is_refused_and_reported_honestly(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("MEMTRIAGE_HOME", str(tmp_path / "data"))
    cfg = Config(mode="auto", data_dir=tmp_path / "data")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    blob = _seed(cfg)
    before = [e.strip() for e in ms.read_entries(ms.TARGET_USER)]

    r = Executor(cfg).execute_plan([{
        "action": "evict-to-quarantine", "target": "user", "index": 1,
        "text": blob,
    }], "e2e-evict", "e2e")

    # THE bug this asserts: the refusal must not appear in `applied`.
    assert r["applied"] == [], f"refused run claimed success: {r['applied']}"
    assert any("not committed" in e for e in r["errors"]), r["errors"]
    assert r["blocked"], "a refusal must be reported, not silent"
    b = r["blocked"][0]
    assert b["index"] == 1 and b["target"] == "user"
    assert b["chars"] == len(blob)
    assert "split" in b["hint"]
    assert [e.strip() for e in ms.read_entries(ms.TARGET_USER)] == before


def test_split_relieves_it_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("MEMTRIAGE_HOME", str(tmp_path / "data"))
    cfg = Config(mode="auto", data_dir=tmp_path / "data")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    blob = _seed(cfg)
    before = ms.usage(ms.TARGET_USER)["current"]

    plan = [{
        "action": "split", "target": "user", "index": 1,
        "keep": IDENTITY,
        "routes": [
            {"action": "route-to-skill", "skill_name": "house-conventions",
             "category": "tools", "text": DOCTRINE.strip()},
            {"action": "route-to-skill", "skill_name": "team-doctrine",
             "category": "tools", "text": FLEET.strip()},
        ],
        "reason": "identity core stays; doctrine routed to skills",
    }]
    assert validate(plan) == plan, "plan must be schema-valid"

    r = Executor(cfg).execute_plan(plan, "e2e-split", "e2e")
    assert not r["errors"], r["errors"]
    assert not r["blocked"], r["blocked"]

    after = ms.usage(ms.TARGET_USER)["current"]
    assert after < before, (after, before)

    entries = ms.read_entries(ms.TARGET_USER)
    assert len(entries) == 2
    # The identity core survived verbatim — that is what the guard protects.
    for needle in ("SULLEN", "vault-only", "Ciel", "never in chat/docs"):
        assert needle in entries[1], needle
    # The routed doctrine left the store and landed in real skills.
    assert "WebKit" not in entries[1]
    assert "truenas" not in entries[1]
    for name, needle in (("house-conventions", "WebKit"),
                         ("team-doctrine", "truenas")):
        p = Path(cfg.skills_root) / "tools" / name / "SKILL.md"
        assert p.exists(), p
        assert needle in p.read_text(encoding="utf-8"), needle

    # Replay is refused and changes nothing.
    r2 = Executor(cfg).execute_plan(plan, "e2e-split", "e2e")
    assert r2["applied"] == []
    assert any("already applied" in e for e in r2["errors"]), r2["errors"]
    assert ms.usage(ms.TARGET_USER)["current"] == after
