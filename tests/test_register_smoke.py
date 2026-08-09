"""Register() smoke test: the plugin surface wires up without a session."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import plugin  # noqa: E402  (repo-root package, like the Hermes loader)


class MockCtx:
    def __init__(self):
        self.hooks = []
        self.commands = []
        self.tools = []

    def register_hook(self, hook_name, callback):
        self.hooks.append(hook_name)

    def register_command(self, name, **kwargs):
        self.commands.append(name)

    def register_tool(self, name, **kwargs):
        self.tools.append(name)


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