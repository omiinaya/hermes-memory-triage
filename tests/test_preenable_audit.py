"""Regression tests for the 2026-09-27 pre-enablement audit.

Each test here corresponds to a defect found by dry-running the REAL Cerveau
plan against a copy of the live store, immediately before enabling the plugin.
"""
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from memtriage import store as st  # noqa: E402
from memtriage.config import Config  # noqa: E402
from memtriage.executor import Executor, _production_data_dir  # noqa: E402


def _cfg():
    return Config.load()


# --- defect 1: a guard refusal must never make the store BIGGER -----------
#
# consolidate/split work by removing source entries and appending a
# replacement. When a later guard revoked the removal, the replacement stayed
# in `appends` and was written anyway, so a REFUSED action duplicated live
# content. Observed: user went 2 entries/223 chars -> 3 entries/296 chars.


def _seed_user(ident="Omar Minaya, SULLEN.", filler="x" * 200):
    """Seed USER.md in the CURRENT (fixture-isolated) hermes home.

    Read the path from store.path_for rather than os.environ: conftest
    monkeypatches HERMES_HOME per-test, and building the path by hand is how
    the first draft of this file ended up seeding nothing.
    """
    path = st.path_for("user")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ident + "\n§\n" + filler, encoding="utf-8")
    return ident


def test_a_refused_consolidate_does_not_grow_the_store():
    _seed_user()
    cfg = _cfg()
    before = st.read_entries("user")
    before_chars = st.char_count(before)

    res = Executor(cfg).execute_plan(
        [{
            "action": "consolidate", "target": "user", "entries": [0, 1],
            "text": "merged identity plus filler content that is long enough "
                    "to matter here",
        }],
        run_id="refuse-consolidate", provenance="test",
    )

    after = st.read_entries("user")
    assert len(after) <= len(before), "a refused consolidate duplicated an entry"
    assert st.char_count(after) <= before_chars, "a refusal grew the store"
    assert any("withdrew" in e for e in res["errors"]), (
        "the withdrawal must be reported, not silent"
    )


def test_a_refused_consolidate_leaves_the_file_untouched():
    """Not just entry count: the exact bytes must be identical."""
    _seed_user()
    path = st.path_for("user")
    before = path.read_bytes()
    Executor(_cfg()).execute_plan(
        [{
            "action": "consolidate", "target": "user", "entries": [0, 1],
            "text": "merged identity plus filler content long enough to matter",
        }],
        run_id="refuse-bytes", provenance="test",
    )
    assert path.read_bytes() == before


def test_an_unrelated_append_survives_a_revoked_removal():
    """A route-to-profile append replaces nothing, so it must be KEPT even
    when a sibling consolidate is revoked -- it is the only copy of its text."""
    _seed_user()
    cfg = _cfg()
    res = Executor(cfg).execute_plan([
        {
            "action": "consolidate", "target": "user", "entries": [0, 1],
            "text": "merged identity plus filler content long enough to matter",
        },
        {
            "action": "route-to-profile",
            "text": "UNIQUE CLAUSE THAT EXISTS NOWHERE ELSE",
        },
    ], run_id="mixed-append", provenance="test")

    blob = " ".join(st.read_entries("user"))
    assert "UNIQUE CLAUSE THAT EXISTS NOWHERE ELSE" in blob, (
        "a non-substitute append was wrongly withdrawn as a duplicate"
    )


# --- defect 2: a provider route that did not confirm is still routed away --
#
# _do_provider reports failure by RETURNING False; it does not raise. The
# split loop ignored the return value, so a clause whose gateway write never
# happened was removed from the store and written nowhere: silent loss.


def test_a_split_route_the_provider_refused_keeps_its_clause():
    ident = _seed_user()
    res = Executor(_cfg()).execute_plan(
        [{
            "action": "split", "target": "user", "index": 0, "keep": ident,
            "routes": [{
                "action": "route-to-provider",
                "text": "CLAUSE THAT EXISTS NOWHERE ELSE",
            }],
            "reason": "test",
        }],
        run_id="split-provider-refused", provenance="test",
    )
    blob = " ".join(st.read_entries("user"))
    assert "CLAUSE THAT EXISTS NOWHERE ELSE" in blob, (
        "the clause was routed away but the provider never wrote it"
    )
    assert any("stays in the store" in e for e in res["errors"])


def test_a_split_whose_every_route_fails_still_keeps_all_clauses():
    ident = _seed_user()
    res = Executor(_cfg()).execute_plan(
        [{
            "action": "split", "target": "user", "index": 0, "keep": ident,
            "routes": [
                {"action": "route-to-provider", "text": "CLAUSE ALPHA"},
                {"action": "route-to-provider", "text": "CLAUSE BETA"},
            ],
            "reason": "test",
        }],
        run_id="split-all-fail", provenance="test",
    )
    blob = " ".join(st.read_entries("user"))
    assert "CLAUSE ALPHA" in blob and "CLAUSE BETA" in blob
    assert len(res["errors"]) == 2


# --- defect 3: a sandboxed run wrote to the PRODUCTION gateway -----------
#
# The dry run was fully isolated (data_dir, HERMES_HOME, copied store) yet
# pushed three real entries into production memory, because the provider is
# an external side effect that no data_dir redirect covers.


def test_a_sandboxed_run_refuses_to_write_to_the_production_gateway():
    from memtriage.executor import _dispatch_to_provider

    prod = _production_data_dir()
    cfg = _cfg()
    assert str(cfg.data_dir) != prod, "test precondition: run must be sandboxed"

    notice = _dispatch_to_provider(cfg, "sandbox write attempt")
    assert notice.startswith("pending"), (
        f"a sandboxed run reached the production gateway: {notice!r}"
    )
    assert "production gateway" in notice


def test_the_production_data_root_ignores_memtriage_home(monkeypatch):
    """_production_data_dir must resolve the REAL install even while
    MEMTRIAGE_HOME points somewhere else."""
    monkeypatch.setenv("MEMTRIAGE_HOME", "/tmp/somewhere-else")
    assert _production_data_dir() == os.path.expanduser("~/.memtriage")


def test_the_production_guard_is_not_a_silent_no_op(monkeypatch):
    """The override must actually let a write THROUGH to the network layer,
    otherwise the guard is untestable dead code.

    Points provider_base_url at a LOCAL sink. An earlier draft of this test
    left it on the real gateway, so every suite run pushed a real entry into
    production memory -- the very failure this audit exists to stop. A test
    must never have a production side effect.
    """
    import http.server
    import json as _json
    import threading
    import urllib.request

    from memtriage import executor as ex

    got = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            got.append(_json.loads(self.rfile.read(n) or b"{}"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("MEMTRIAGE_ALLOW_PROVIDER", "1")
        monkeypatch.setattr(ex, "_provider_api_key", lambda cfg: "k")
        cfg = _cfg()
        monkeypatch.setattr(
            cfg, "provider_base_url",
            f"http://127.0.0.1:{srv.server_address[1]}",
            raising=False,
        )
        out = ex._dispatch_to_provider(cfg, "override write")
    finally:
        srv.shutdown()

    assert out == "200", f"override did not reach the network layer: {out!r}"
    assert got and got[0]["messages"][0]["content"] == "override write"


def test_the_guard_blocks_the_real_gateway_unless_overridden():
    """The mirror case, with no override: the call must NOT reach the wire."""
    from memtriage import executor as ex

    cfg = _cfg()
    assert ex._dispatch_to_provider(cfg, "must not land").startswith("pending")


# --- defect 4: a config pinning data_dir outside the root ----------------


def test_a_config_pinning_a_foreign_data_dir_is_refused():
    from memtriage.config import Config as C

    raw = {"data_dir": "/root/.memtriage", "mode": "auto"}
    with pytest.raises(ValueError, match="outside the active data root"):
        C.from_dict(raw)
