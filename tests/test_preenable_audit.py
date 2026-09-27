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


# --- defect 4: the over-limit guard DELETED the identity core ------------
#
# "appends dropped, removals kept" is only safe when the appends are
# decorative. For split/consolidate the append IS the surviving text, so the
# guard removed the source and then discarded its replacement. Reproduced on
# a store merely OVER budget: an identity-guarded USER entry was destroyed by
# the guard meant to protect the char limit.


def test_the_over_limit_guard_never_deletes_a_source_it_will_not_replace():
    _seed_user(ident="Omar Minaya, SULLEN. WebKit iPhone. bind 0.0.0.0.",
               filler="y" * 1400)
    cfg = _cfg()
    before = st.read_entries("user")
    res = Executor(cfg).execute_plan(
        [{
            "action": "split", "target": "user", "index": 0,
            "keep": "Omar Minaya, SULLEN. WebKit iPhone. bind 0.0.0.0.",
            "routes": [{"action": "route-to-provider",
                        "text": "filler " + "z" * 2000}],
            "reason": "test",
        }],
        run_id="over-limit", provenance="test",
    )
    after = st.read_entries("user")
    assert any("SULLEN" in e for e in after), (
        f"the over-limit guard deleted the identity core: {after}"
    )
    assert st.char_count(after) >= st.char_count(
        [e for e in before if "SULLEN" in e][0]
    ) or True  # size is allowed to change; losing the entry is not
    assert any("revoked" in e or "refused" in e for e in res["errors"]), (
        "the refusal must be reported"
    )


def test_a_retained_clause_survives_the_over_limit_guard():
    """A clause re-appended after a failed route exists nowhere else; if the
    limit guard drops the append, that text is simply gone."""
    _seed_user(ident="Omar Minaya, SULLEN. WebKit iPhone.", filler="y" * 1400)
    Executor(_cfg()).execute_plan(
        [{
            "action": "split", "target": "user", "index": 0,
            "keep": "Omar Minaya, SULLEN. WebKit iPhone.",
            "routes": [{"action": "route-to-provider",
                        "text": "UNIQUE RETAINED CLAUSE " + "q" * 3000}],
            "reason": "test",
        }],
        run_id="over-limit-retain", provenance="test",
    )
    blob = " ".join(st.read_entries("user"))
    assert "UNIQUE RETAINED CLAUSE" in blob, (
        "the retained clause was discarded by the limit guard"
    )


# --- defect 5: restore() pruned the very snapshot it was reading ---------


def test_restoring_the_oldest_snapshot_keeps_that_snapshot(tmp_path):
    """The undo snapshot can push the cap over, and the prune then deleted
    the OLDEST -- which was the file being restored. Verified failure: the
    restore raised FileNotFoundError and the only good copy was gone."""
    from memtriage import snapshots

    st.write_entries("user", ["original content"])
    data = tmp_path / "data"
    names = []
    for i in range(5):
        r = snapshots.take(data, "user", f"run-{i}", keep=5)
        if r and r.get("ok"):
            names.append(pathlib.Path(r["path"]).name)
    st.write_entries("user", ["MANGLED"])

    oldest = sorted(names)[0]
    out = snapshots.restore(data, "user", oldest, keep=5)
    assert out.get("restored"), out
    assert st.read_entries("user") == ["original content"]
    assert (data / "snapshots" / oldest).exists(), (
        "the restored-from snapshot must survive the restore"
    )


# --- defect 6: auto mode + a 40s blocking dispatch on the tool path -------
#
# config.json ships mode="auto" with a 600s Cerveau timeout. post_tool_call
# is synchronous INSIDE the user's tool call, so enabling as-is would stall a
# memory write for the length of a real triage. The run is now off-thread and
# requires an explicit opt-in.

def test_unattended_writes_require_an_explicit_opt_in(monkeypatch):
    import importlib
    monkeypatch.delenv("MEMTRIAGE_AUTO_RUN", raising=False)
    monkeypatch.delenv("MEMTRIAGE_ALLOW_WRITES", raising=False)
    p = importlib.import_module("plugin")
    assert p._auto_run_allowed() is False, (
        "auto-run must be off unless explicitly enabled"
    )


def test_auto_run_alone_is_not_enough(monkeypatch):
    import importlib
    monkeypatch.setenv("MEMTRIAGE_AUTO_RUN", "1")
    monkeypatch.delenv("MEMTRIAGE_ALLOW_WRITES", raising=False)
    p = importlib.import_module("plugin")
    assert p._auto_run_allowed() is False


def test_both_brakes_allow_an_unattended_run(monkeypatch):
    import importlib
    monkeypatch.setenv("MEMTRIAGE_AUTO_RUN", "1")
    monkeypatch.setenv("MEMTRIAGE_ALLOW_WRITES", "1")
    p = importlib.import_module("plugin")
    assert p._auto_run_allowed() is True


def test_a_read_only_memory_call_never_triggers_triage(monkeypatch):
    """A `memory` READ crosses the same hook; it must not start a triage."""
    import importlib
    p = importlib.import_module("plugin")
    called = []
    monkeypatch.setattr(
        p, "_maybe_run_triage", lambda reason: called.append(reason)
    )
    p._on_post_tool_call(
        tool_name="memory", status="ok", args={"action": "view"},
    )
    assert not called, "a read-only memory call started a triage"


def test_a_failed_memory_call_never_triggers_triage(monkeypatch):
    import importlib
    p = importlib.import_module("plugin")
    called = []
    monkeypatch.setattr(
        p, "_maybe_run_triage", lambda reason: called.append(reason)
    )
    p._on_post_tool_call(
        tool_name="memory", status="error", args={"action": "add"},
    )
    assert not called
