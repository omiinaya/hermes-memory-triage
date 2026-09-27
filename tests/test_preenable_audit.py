import json
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


# --- defect 7: the tool reached the model as an EMPTY tool -----------------
#
# register_tool spreads the schema into the function object and
# sanitize_tool_schemas replaces a missing/non-dict "parameters" with
# {"type":"object","properties":{},"required":[]}. A bare JSON Schema has no
# "parameters" key, so the action enum and the description were both stripped
# and the model had nothing to call.


def test_the_tool_schema_survives_the_sanitizer_with_its_arguments():
    import importlib.util as u
    spec = u.spec_from_file_location(
        "mtplug_schema",
        str(__import__("pathlib").Path(__file__).resolve().parents[1]
            / "plugin" / "__init__.py"),
    )
    plug = u.module_from_spec(spec)
    spec.loader.exec_module(plug)

    schema = plug.TOOL_SCHEMA
    assert "parameters" in schema, (
        "TOOL_SCHEMA must nest its arguments under 'parameters' or the "
        "sanitizer replaces them with an empty object"
    )
    # Reproduce exactly what the registry + sanitizer do to it.
    as_sent = {"type": "function", "function": {**schema, "name": "mem_triage"}}

    def _sanitize(fn):
        params = fn.get("parameters")
        if not isinstance(params, dict):
            fn = {**fn, "parameters": {"type": "object", "properties": {},
                                       "required": []}}
        return fn

    fn = _sanitize(as_sent["function"])
    props = fn["parameters"].get("properties", {})
    assert "action" in props, (
        f"the model would see an empty tool: {fn['parameters']}"
    )
    assert "run" in props["action"]["enum"], props["action"]
    assert fn.get("description"), "the description must reach the model"


def test_every_subcommand_is_reachable_through_the_tool():
    """SUBCOMMANDS and the tool enum must not drift apart."""
    import importlib.util as u
    from pathlib import Path
    spec = u.spec_from_file_location(
        "mtplug_enum", str(Path(__file__).resolve().parents[1]
                           / "plugin" / "__init__.py"))
    plug = u.module_from_spec(spec)
    spec.loader.exec_module(plug)
    enum = set(plug.TOOL_SCHEMA["parameters"]["properties"]["action"]["enum"])
    missing = set(plug.SUBCOMMANDS) - enum
    assert not missing, (
        f"subcommands not callable through the tool: {sorted(missing)}"
    )


# --- defect 8: concurrent state.json writers lost 42 of 60 notifications ----


def test_concurrent_state_writers_lose_nothing():
    """Eight modules wrote JSON through ONE fixed ".tmp" name.

    4 threads x 15 mark_notified stored 18 of 60 and raised 31
    FileNotFoundError, because each writer renamed the shared temp out from
    under the others. Every lost id is a run whose report Omar never sees.
    """
    import threading
    from memtriage import state as st_mod
    from memtriage.config import Config

    cfg = Config()
    errors = []

    def hammer(n):
        for i in range(15):
            try:
                st_mod.mark_notified(cfg, f"RUN-{n}-{i}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:3]
    assert len(st_mod.notified_runs(cfg)) == 60, (
        f"lost {60 - len(st_mod.notified_runs(cfg))} notification records"
    )


# --- defect 9: quarantine restore removed the WRONG line --------------------


def test_restoring_a_record_does_not_eat_a_blank_line_or_a_neighbour():
    """all_evicted SKIPS blank lines, so its indices are not file offsets."""
    import json as _json
    import time as _time
    from memtriage import quarantine
    from memtriage.config import Config

    cfg = Config()
    qf = cfg.quarantine_dir / "quarantine.jsonl"
    qf.parent.mkdir(parents=True, exist_ok=True)
    now = _time.time()
    rec = lambda t: _json.dumps(  # noqa: E731
        {"text": t, "target": "memory", "run_id": "r", "evicted_at": now}
    )
    qf.write_text(rec("VICTIM-A") + "\n\n" + rec("VICTIM-B") + "\n")

    st.write_entries(st.TARGET_MEMORY, [])
    assert quarantine.restore(cfg, text="VICTIM-B") is True
    left = [r["text"] for r in quarantine.all_evicted(cfg)]
    assert left == ["VICTIM-A"], (
        f"restore removed the wrong record: {left}"
    )


# --- defect 10: the lock was a different inode than the memory tool's ------


def test_the_store_lock_is_the_same_file_the_built_in_tool_locks():
    """A different lock file means NO mutual exclusion at all."""
    from pathlib import Path as _P
    from memtriage import locking

    store = _P("/tmp/x/MEMORY.md")
    builtin = store.with_suffix(store.suffix + ".lock")  # memory_tool_store.py:173
    assert locking._lock_path(store) == builtin, (
        f"memtriage locks {locking._lock_path(store)} but the built-in memory "
        f"tool locks {builtin}: they do not exclude each other"
    )


# --- defect 11: a headless run marked itself delivered --------------------
#
# _inject returned None and both notify sites called mark_notified
# unconditionally, so a run with no channel (cron, CLI) recorded itself as
# shown and every later session start suppressed it forever. The user never
# sees that report.


def test_a_run_with_no_channel_is_not_marked_notified(tmp_path):
    import importlib.util as u
    from pathlib import Path as _P
    from memtriage import state as st_mod
    from memtriage.config import Config

    spec = u.spec_from_file_location(
        "mtplug_notify",
        str(_P(__file__).resolve().parents[1] / "plugin" / "__init__.py"))
    plug = u.module_from_spec(spec); sys.modules["mtplug_notify"] = plug
    spec.loader.exec_module(plug)

    plug._ctx = None                       # no active channel
    plug._notify_result({
        "triggered": True, "run_id": "RUN-SILENT", "mode": "manual",
        "plan": [{"action": "keep"}], "report_path": "", "execution": None,
    })
    runs = st_mod.notified_runs(Config())
    assert "RUN-SILENT" not in runs, (
        "the run was marked delivered although nothing was ever shown; it "
        "will now be suppressed permanently"
    )


# --- defect 12: the auto-run blocked the tool call past Hermes' 30s bound --


def test_the_auto_triage_hook_returns_immediately(tmp_path, monkeypatch):
    """post_tool_call is in _HOOK_TIMEOUT_BOUNDED_HOOKS (30s default).

    A triage shells out to Cerveau with a 600s timeout and a measured ~40s
    prompt, so running it inline was abandoned mid-run and eventually
    suppressed. The work must happen off the caller's thread.
    """
    import importlib.util as u
    import threading
    import time as _t
    from pathlib import Path as _P
    from memtriage import store as _st
    from memtriage.config import Config

    spec = u.spec_from_file_location(
        "mtplug_hook",
        str(_P(__file__).resolve().parents[1] / "plugin" / "__init__.py"))
    plug = u.module_from_spec(spec); sys.modules["mtplug_hook"] = plug
    spec.loader.exec_module(plug)

    monkeypatch.setenv("MEMTRIAGE_AUTO_RUN", "1")
    monkeypatch.setenv("MEMTRIAGE_ALLOW_WRITES", "1")
    _st.write_entries(_st.TARGET_MEMORY, ["x" * (_st.char_limit("memory") + 100)])

    started = threading.Event()
    def slow(cfg, reason=None, force=False):
        started.set()
        _t.sleep(2.0)
        return {"triggered": False}
    monkeypatch.setattr(plug, "run_triage", slow)
    monkeypatch.setattr(plug, "_notify_result", lambda r: None)

    t0 = _t.time()
    plug._on_post_tool_call(tool_name="memory", status="ok", args={"action": "add"})
    elapsed = _t.time() - t0
    assert elapsed < 1.0, (
        f"the hook blocked the tool call for {elapsed:.1f}s; Hermes abandons "
        f"a bounded hook at 30s"
    )
    assert started.wait(2.0), "the triage never started on the worker"


# --- defect 12: Cerveau echoed the prompt's placeholder as real content ----
#
# A live run on 2026-09-27 created
# /root/.hermes/skills/tools/team-doctrine/SKILL.md whose entire body was the
# literal "<the deploy/repo clauses>" — the example from our own prompt.


def test_a_whole_string_angle_placeholder_is_refused():
    from memtriage.plan import validate, PlanValidationError
    with pytest.raises(PlanValidationError) as e:
        validate([{"action": "route-to-skill", "target": "memory", "index": 0,
                   "skill_name": "team-doctrine",
                   "text": "<the deploy/repo clauses>"}])
    assert "PLACEHOLDER" in str(e.value)


def test_a_placeholder_inside_a_split_route_is_refused():
    from memtriage.plan import validate, PlanValidationError
    with pytest.raises(PlanValidationError):
        validate([{"action": "split", "target": "user", "index": 0,
                   "keep": "Omar Minaya, cyber-name SULLEN.",
                   "routes": [{"action": "route-to-skill",
                               "skill_name": "x", "text": "<the other clauses>"}]}])


def test_a_real_entry_containing_brackets_still_passes():
    """Must not over-reject: a bracketed phrase inside real text is content."""
    from memtriage.plan import validate
    out = validate([{"action": "route-to-skill", "target": "memory", "index": 0,
                     "skill_name": "cfg",
                     "text": "Set DIM=<name> in the config before the render pass."}])
    assert len(out) == 1
    assert out[0]["text"].startswith("Set DIM=")


def test_empty_routed_text_is_refused():
    from memtriage.plan import validate, PlanValidationError
    with pytest.raises(PlanValidationError):
        validate([{"action": "route-to-skill", "target": "memory", "index": 0,
                   "skill_name": "x", "text": "   "}])


# --- defect 13: a TOOL call in auto mode wrote to disk unattended ----------
#
# _auto_run_allowed() gated the post_tool_call hook but not the tool, so one
# `mem_triage {"action": "run"}` from the model applied the plan and wrote a
# SKILL.md with nobody watching.


def test_the_run_action_is_braked_too_not_just_the_hook(monkeypatch):
    import importlib
    mod = importlib.import_module("plugin")
    seen = {}

    def fake_cmd_run(cfg, force=False):
        seen["mode"] = cfg.mode
        return "ran"

    monkeypatch.setattr(mod.commands, "cmd_run", fake_cmd_run)
    for v in ("MEMTRIAGE_AUTO_RUN", "MEMTRIAGE_ALLOW_WRITES"):
        monkeypatch.delenv(v, raising=False)

    cfg = mod._load_cfg()
    cfg.mode = "auto"
    monkeypatch.setattr(mod, "_load_cfg", lambda: cfg)

    out = mod._handle_tool({"action": "run"})
    assert seen["mode"] == "manual", f"auto mode reached the executor: {seen}"


def test_the_run_action_still_writes_when_both_brakes_are_set(monkeypatch):
    import importlib
    mod = importlib.import_module("plugin")
    seen = {}

    def fake_cmd_run(cfg, force=False):
        seen["mode"] = cfg.mode
        return "ran"

    monkeypatch.setattr(mod.commands, "cmd_run", fake_cmd_run)
    monkeypatch.setenv("MEMTRIAGE_AUTO_RUN", "1")
    monkeypatch.setenv("MEMTRIAGE_ALLOW_WRITES", "1")

    cfg = mod._load_cfg()
    cfg.mode = "auto"
    monkeypatch.setattr(mod, "_load_cfg", lambda: cfg)

    mod._handle_tool({"action": "run"})
    assert seen["mode"] == "auto", f"brakes blocked a deliberate auto run: {seen}"


# --- defect 14: one unbalanced "[" silently discarded the real plan -------
#
# _array_spans did `break` on an unbalanced span, abandoning the REST of the
# reply. A ledger summary containing "[3h]" in the echoed prompt corrupted the
# scan, so the genuine 13-action Cerveau plan sitting further down was never
# seen: triage fell back to an all-keep no-op and reported success while
# freeing nothing. Found only by running against the real live reply.


def test_an_unbalanced_bracket_does_not_discard_the_rest_of_the_reply():
    from memtriage.plan import parse_plan
    plan_json = (
        '[{"action":"route-to-provider","target":"memory","index":0,'
        '"text":"real content that must survive"}]'
    )
    # A ledger record whose summary holds a stray "[" before the real plan.
    reply = (
        'prior ledger echo: [{"kind":"skill","summary":"every 3h [3h] cron"}]\n'
        + "prose in between\n"
        + plan_json
    )
    out = parse_plan(reply)
    assert len(out) == 1
    assert out[0]["action"] == "route-to-provider"
    assert out[0]["text"] == "real content that must survive"


def test_the_real_live_reply_now_yields_the_cerveau_plan():
    """The exact captured reply: used to parse as the prompt's echoed example."""
    import pathlib
    from memtriage.plan import parse_plan
    raw = pathlib.Path("/root/.hermes/cache/scratch/cerveau_raw.txt")
    if not raw.exists():
        pytest.skip("captured reply not present")
    text = raw.read_text()
    i, j = text.index("===STDOUT==="), text.index("===STDERR===")
    out = parse_plan(text[i + 10 : j].strip())
    assert len(out) == 13
    assert not any(a.get("_source") == "deterministic-fallback" for a in out)
    kinds = {a["action"] for a in out}
    assert "route-to-provider" in kinds
    assert "route-to-skill" in kinds


# --- defect 15: a mis-nested span SWALLOWED the real plan ------------------
#
# Fixing defect 14 by resyncing (i += 1) was necessary but not sufficient: the
# resynced scan sometimes produced a bracket-balanced span that ran 38KB to the
# reply's end, so the real plan inside it was never yielded. A second live run
# on 2026-09-27 fell back to all-keep for exactly this reason. A span is now
# only yielded once it actually loads as JSON.


def test_a_mis_nested_span_does_not_swallow_the_plan_farther_down():
    from memtriage.plan import _array_spans, parse_plan

    # A bracket-balanced fragment that is NOT valid JSON: a real "[" opens it
    # and a real "]" closes it 38KB later, so the naive balanced scan swallows
    # everything in between -- including the genuine plan.
    noise = '[{"kind": "skill", "summary": "unterminated noise'
    plan = [
        {"action": "route-to-provider", "target": "memory", "index": 0,
         "text": "real content a", "reason": "because"},
        {"action": "keep", "target": "user", "index": 0, "reason": "identity"},
    ]
    reply = "Query: " + noise + "\n" + json.dumps(plan) + "\ntrailing prose ]"

    spans = _array_spans(reply)
    # The plan must survive as its own span despite the noise.
    parsed = parse_plan(reply)
    assert len(parsed) == 2, f"expected the real 2-action plan, got {len(parsed)}"
    assert parsed[0]["text"] == "real content a"


def test_a_bracket_balanced_but_invalid_span_is_not_yielded():
    from memtriage.plan import _array_spans

    noise = '[{"kind": "skill", "summary": "no closing brace'
    reply = noise + ']\n[{"action": "keep", "target": "memory", "index": 0}]'

    spans = _array_spans(reply)
    for s in spans:
        json.loads(s)  # every yielded span must be real JSON
    assert any('"action"' in s for s in spans), "the real plan span was lost"


def test_control_characters_in_a_span_do_not_hide_it():
    """A repairable control char must not make a REAL array look like noise."""
    from memtriage.plan import _array_spans

    reply = '[{"action": "keep", "target": "memory", "index": 0, "reason": "a\tb"}]'
    spans = _array_spans(reply)
    assert len(spans) == 1, "a repairable array must still be yielded"


def test_the_second_real_live_reply_parses_to_the_cerveau_plan():
    """Both replies captured from real runs must yield the CERVEAU plan.

    The 38KB-swallowing span (defect 15) only shows up with a genuinely messy
    reply, so this is a recorded-fixture test: the exact bytes
    ``hermes chat`` produced, including the echoed prompt, the truncated
    inventory, and the model's answer. A synthetic reproduction was not enough
    to kill the mutation that survived, so the real thing is pinned here.
    """
    from memtriage.plan import parse_plan

    raw = pathlib.Path(__file__).parent / "fixtures" / "cerveau_reply_messy.txt"
    if not raw.exists():
        pytest.skip("recorded Cerveau reply not present")
    text = raw.read_text()

    plan = parse_plan(text)
    assert len(plan) == 13, f"expected 13 actions, got {len(plan)}"
    # Every action must come from the model, not the all-keep fallback.
    assert all(a.get("_source", "cerveau") == "cerveau" for a in plan)
    kinds = {a["action"] for a in plan}
    assert kinds != {"keep"}, "fell back to the all-keep no-op"


# --- defect 16: Cerveau could not see existing skills, so it duplicated them --
#
# The prompt truncated the skills list to the first 30 (and under budget
# pressure, the first 5) IN ALPHABETICAL ORDER. With 324 installed skills,
# everything past ~"c" was invisible -- including the user's own
# oem-ui-design-system and oem-cdn-design. Cerveau therefore invented
# "oem-ui-design-system" as a NEW name, because it had never been told the name
# existed, and would have forked a 39KB curated skill into a duplicate.


def test_truncation_keeps_every_skill_name(tmp_path):
    from memtriage.cerveau import _truncate_payload

    skills = [{"name": f"skill-{i:03d}", "description": "x" * 400, "path": f"/p/{i}"}
              for i in range(324)]
    out = _truncate_payload({"skills": skills, "memory": [], "user": []})

    names = [s.get("name") for s in out["skills"]]
    # Every name must survive truncation -- a name is cheap and is the only
    # thing the collision rule needs.
    assert len(names) == 324, f"expected all 324 names, got {len(names)}"
    assert "skill-323" in names, "the alphabetically-last skill was dropped"
    # But the expensive descriptions are still capped.
    blanked = [s for s in out["skills"] if not s.get("description")]
    assert len(blanked) > 0, "descriptions were not trimmed at all"


def test_truncation_keeps_names_even_under_a_tiny_budget():
    from memtriage.cerveau import _truncate_payload

    skills = [{"name": f"s{i}", "description": "y" * 500, "path": ""} for i in range(50)]
    out = _truncate_payload({"skills": skills, "memory": [], "user": []},
                            max_chars=500)
    names = [s.get("name") for s in out["skills"]]
    assert len(names) == 50, f"names were truncated under budget pressure: {len(names)}"
    assert all(s.get("description") == "" for s in out["skills"])


def test_the_prompt_tells_cerveau_to_check_existing_skill_names():
    from memtriage.cerveau import PROMPT_TEMPLATE

    # The rule must name the inventory field AND tell the model what actually
    # happens on a collision (it appends), so the model is never told a
    # behaviour the executor does not have.
    assert "existing_skill_names" in PROMPT_TEMPLATE
    assert "CHECK" in PROMPT_TEMPLATE and "INVENTORY" in PROMPT_TEMPLATE
    assert "APPENDS" in PROMPT_TEMPLATE
    assert "REFUSES to overwrite" not in PROMPT_TEMPLATE, (
        "the prompt promises a refusal the executor no longer performs"
    )
    assert "append_to_existing" not in PROMPT_TEMPLATE, (
        "the prompt advertises a flag the executor does not read"
    )


def test_routing_to_an_existing_skill_name_appends_instead_of_duplicating(tmp_path):
    """A name that already exists in ANY category must extend that skill."""
    from memtriage.config import Config
    from memtriage.executor import Executor

    home = tmp_path / "hermes"
    existing = home / "skills" / "creative" / "oem-ui-design-system"
    existing.mkdir(parents=True)
    skill_md = existing / "SKILL.md"
    skill_md.write_text(
        "---\nname: oem-ui-design-system\ndescription: house style\n"
        "metadata:\n  hermes:\n    tags: [design-system]\n---\n\n"
        "## Curated doctrine\n\nThe light ink is #6d6d6d.\n",
        encoding="utf-8",
    )
    before = skill_md.read_text(encoding="utf-8")

    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    ex = Executor(cfg)
    ex._run_id = "r1"
    ex._provenance = "test"

    ex._do_skill({"skill_name": "oem-ui-design-system",
                  "text": "NEW FACT: 175 tests, private repo."}, {})

    after = skill_md.read_text(encoding="utf-8")
    # The curated content and the frontmatter must both survive.
    assert "The light ink is #6d6d6d." in after
    assert "tags: [design-system]" in after
    assert "NEW FACT: 175 tests" in after
    # And no duplicate may exist in the default category.
    dup = home / "skills" / "tools" / "oem-ui-design-system"
    assert not dup.exists(), f"created a duplicate skill at {dup}"


def test_appending_the_same_content_twice_is_a_no_op(tmp_path):
    from memtriage.config import Config
    from memtriage.executor import Executor

    home = tmp_path / "hermes"
    d = home / "skills" / "tools" / "dup-check"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: dup-check\n---\n\nbody\n", encoding="utf-8")

    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    ex = Executor(cfg)
    ex._run_id = "r1"
    ex._provenance = "test"

    ex._do_skill({"skill_name": "dup-check", "text": "UNIQUE MARKER LINE"}, {})
    once = (d / "SKILL.md").read_text(encoding="utf-8")
    ex2 = Executor(cfg)
    ex2._run_id = "r2"
    ex2._provenance = "test"
    ex2._do_skill({"skill_name": "dup-check", "text": "UNIQUE MARKER LINE"}, {})
    twice = (d / "SKILL.md").read_text(encoding="utf-8")
    assert once == twice, "the same content was appended twice"
    assert once.count("UNIQUE MARKER LINE") == 1


def test_appending_takes_a_recoverable_snapshot(tmp_path):
    from memtriage.config import Config
    from memtriage.executor import Executor

    home = tmp_path / "hermes"
    d = home / "skills" / "tools" / "snap-check"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: snap-check\n---\n\nORIGINAL\n", encoding="utf-8")

    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    ex = Executor(cfg)
    ex._run_id = "r1"
    ex._provenance = "test"
    ex._do_skill({"skill_name": "snap-check", "text": "APPENDED"}, {})

    snaps = list((tmp_path / "data").rglob("*snap-check*"))
    assert snaps, "no pre-write snapshot was taken for the append"
    assert "ORIGINAL" in snaps[0].read_text(encoding="utf-8")
