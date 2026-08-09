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