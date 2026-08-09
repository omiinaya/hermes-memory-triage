"""Register() smoke test: the plugin surface wires up without a session."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import plugin  # noqa: E402  (repo-root package, like the Hermes loader)
from memtriage import state  # noqa: E402


class MockCtx:
    def __init__(self):
        self.hooks = []
        self.commands = []
        self.tools = []
        self.injected = []

    def register_hook(self, hook_name, callback):
        self.hooks.append(hook_name)

    def register_command(self, name, **kwargs):
        self.commands.append(name)

    def register_tool(self, name, **kwargs):
        self.tools.append(name)

    def inject_message(self, content, role="user"):
        self.injected.append((role, content))
        return True


def test_register_registers_all_surfaces():
    ctx = MockCtx()
    plugin.register(ctx)
    assert "post_tool_call" in ctx.hooks
    assert "on_session_start" in ctx.hooks
    assert "memtriage" in ctx.commands
    assert "mem_triage" in ctx.tools


def test_slash_handler_defaults_to_status():
    ctx = MockCtx()
    plugin.register(ctx)
    out = plugin._handle_slash("")
    assert "Threshold" in out
    assert "Mode" in out


def test_slash_handler_unknown_subcommand():
    out = plugin._handle_slash("frobnicate")
    assert "Unknown subcommand" in out
    assert "status" in out


def test_tool_handler_dispatch():
    out = plugin._handle_tool({"action": "status"})
    assert "Threshold" in out
    out2 = plugin._handle_tool({"action": "purge"})
    assert "Purged" in out2


def test_post_tool_call_ignores_non_memory_tools():
    # Must be a cheap no-op for other tools (no exceptions, no state writes).
    plugin._on_post_tool_call(tool_name="shell_exec", status="ok", args={})


def test_post_tool_call_readonly_memory_call_is_noop():
    plugin._on_post_tool_call(tool_name="memory", status="ok", args={"action": ""})


def test_auto_triage_skips_when_plan_awaits_approval(tmp_path, monkeypatch):
    """A queued manual-mode plan must never be stomped by auto-triage."""
    from memtriage import state as mt_state
    from memtriage import store as memory_store
    from memtriage.config import Config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("MEMTRIAGE_HOME", str(tmp_path / "data"))
    memory_store.write_entries("memory", ["x" * 2000])  # over 75%
    cfg = Config(data_dir=tmp_path / "data")
    mt_state.mark_awaiting_approval(cfg, "run-1")

    called = {"n": 0}
    original = plugin.run_triage

    def boom(*args, **kwargs):
        called["n"] += 1
        raise AssertionError("auto-triage must not run while a plan awaits approval")

    plugin.run_triage = boom
    try:
        plugin._maybe_run_triage("test")
    finally:
        plugin.run_triage = original
    assert called["n"] == 0


def test_notify_result_injects_report_into_session(tmp_path, monkeypatch):
    """A fresh triage result must surface its report in the conversation."""
    from memtriage.config import Config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("MEMTRIAGE_HOME", str(tmp_path / "data"))
    cfg = Config(data_dir=tmp_path / "data")
    report = cfg.reports_dir / "report-run-9.md"
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    report.write_text("# Triage report run-9\n- [route-to-skill] deploy\n", encoding="utf-8")

    ctx = MockCtx()
    plugin._ctx = ctx
    try:
        plugin._notify_result(
            {
                "run_id": "run-9",
                "triggered": True,
                "mode": "manual",
                "plan": [{"action": "route-to-skill"}],
                "report_path": str(report),
                "execution": None,
            }
        )
    finally:
        plugin._ctx = None
    assert len(ctx.injected) == 1
    role, content = ctx.injected[0]
    assert role == "user"
    assert "run-9" in content
    assert "route-to-skill" in content  # report body visible
    assert "Nothing applied yet" in content
    assert "run-9" in state.notified_runs(cfg)


def test_session_start_surfaces_awaiting_plan_once(tmp_path, monkeypatch):
    """A queued plan is injected into the session exactly once."""
    from memtriage.config import Config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("MEMTRIAGE_HOME", str(tmp_path / "data"))
    cfg = Config(data_dir=tmp_path / "data")
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    (cfg.reports_dir / "report-run-7.md").write_text(
        "# Triage report run-7\n- [keep] env fact\n", encoding="utf-8"
    )
    state.mark_awaiting_approval(cfg, "run-7")

    ctx = MockCtx()
    plugin._ctx = ctx
    try:
        plugin._on_session_start(session_id="s1")
        plugin._on_session_start(session_id="s2")
    finally:
        plugin._ctx = None
    assert len(ctx.injected) == 1  # surfaced once, not on every session start
    assert "run-7" in ctx.injected[0][1]
    assert "keep" in ctx.injected[0][1]


def test_notify_execution_injects_what_it_did(tmp_path, monkeypatch):
    """After a plan is applied, the summary of what it did is injected."""
    from memtriage.config import Config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("MEMTRIAGE_HOME", str(tmp_path / "data"))
    cfg = Config(data_dir=tmp_path / "data")
    state.record_execution(
        cfg, "run-11",
        {
            "applied": ["routed to skill 'deploy' (path)", "evicted-to-quarantine 33 chars"],
            "pending": ["cron for 'cleanup': pending (hermes cron add ...)"],
            "errors": [],
        },
    )
    ctx = MockCtx()
    plugin._ctx = ctx
    try:
        plugin._notify_execution(cfg)
    finally:
        plugin._ctx = None
    assert len(ctx.injected) == 1
    content = ctx.injected[0][1]
    assert "run-11" in content
    assert "2 applied action(s)" in content
    assert "routed to skill 'deploy'" in content
    assert "pending" in content
    assert "memory" in content  # post-execution usage shown


def test_render_execution_flat():
    block = plugin._render_execution(
        {"applied": ["a"], "pending": ["p"], "errors": []}
    )
    assert "1 applied action(s)" in block
    assert "+ a" in block
    assert "pending" in block