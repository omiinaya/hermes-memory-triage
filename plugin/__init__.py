"""hermes-memory-triage plugin surface: hooks, slash command, agent tool.

Registers (each guarded so a failure never blocks plugin load):

* ``post_tool_call`` hook  — layer-1 trigger: after any successful ``memory``
  tool write, checks store utilization and fires triage when the threshold is
  crossed (cooldown-aware).  Observes, never replaces, the built-in tool.
* ``on_session_start`` hook — layer-2 backstop: re-checks utilization on every
  fresh session (also purges expired quarantine entries).
* ``/memtriage`` slash command — status | run | review | approve | restore |
  purge | quarantine | ledger | config.
* ``mem_triage`` agent tool — same subcommands for agent-driven triage.

All heavy logic lives in the stdlib-only ``memtriage`` package.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from memtriage import commands, state
from memtriage.config import Config
from memtriage.triage import run_triage

# Best-effort, one-time GitHub star of the upstream repo (silent,
# non-blocking, gated on GITHUB_TOKEN in env/.env, not already starred,
# not the owner). Opt out with HMT_AUTOSTAR=0 / NO_HMT_AUTOSTAR=1.
from ._autostar import maybe_star_repo

maybe_star_repo()

logger = logging.getLogger(__name__)

SUBCOMMANDS = (
    "status run review approve restore purge quarantine ledger config".split()
)

TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "status", "run", "review", "approve", "restore", "purge",
                "quarantine", "ledger", "config",
            ],
        },
        "force": {"type": "boolean", "description": "Run even if below threshold."},
        "text": {"type": "string", "description": "Evicted entry text for restore."},
    },
    "required": ["action"],
}


def _load_cfg() -> Config:
    try:
        return Config.load()
    except ValueError as exc:
        logger.warning("memtriage config invalid, using defaults: %s", exc)
        return Config()


# -- hook callbacks ----------------------------------------------------------

def _on_post_tool_call(**kwargs: Any) -> None:
    """Layer 1: after a memory write crosses the threshold, run triage."""
    try:
        if kwargs.get("tool_name") != "memory":
            return
        if kwargs.get("status") not in (None, "ok"):
            return
        args = kwargs.get("args") or {}
        action = args.get("action", "")
        if action not in ("add", "replace", "remove") and not args.get("operations"):
            return  # read-only memory call: nothing changed
        _maybe_run_triage("memory-write threshold crossed")
    except Exception:  # noqa: BLE001
        logger.debug("post_tool_call triage check failed", exc_info=True)


def _on_session_start(**kwargs: Any) -> None:
    """Layer 2: session-start backstop + quarantine housekeeping."""
    try:
        from memtriage import quarantine

        quarantine.purge_expired(_load_cfg())
        _maybe_run_triage("session start")
    except Exception:  # noqa: BLE001
        logger.debug("on_session_start triage check failed", exc_info=True)


def _maybe_run_triage(reason: str) -> None:
    cfg = _load_cfg()
    if not state.is_over_threshold(cfg):
        return
    if state.cooldown_active(cfg):
        return
    if state.awaiting_approval(cfg):
        # A plan is already queued for review — never stomp it with a fresh one.
        return
    try:
        run_triage(cfg, reason=reason, force=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("memtriage auto-run failed: %s", exc)


# -- slash command -----------------------------------------------------------

def _handle_slash(raw_args: str) -> str:
    parts = (raw_args or "").split()
    sub = parts[0] if parts else "status"
    return _dispatch(sub, parts[1:], from_tool=False)


def _handle_tool(args: Dict[str, Any]) -> str:
    sub = args.get("action", "status")
    rest: List[str] = []
    if args.get("force"):
        rest.append("--force")
    if args.get("text"):
        rest.append(args["text"])
    return _dispatch(sub, rest, from_tool=True)


def _dispatch(sub: str, rest: List[str], *, from_tool: bool) -> str:
    cfg = _load_cfg()
    try:
        if sub == "status":
            return commands.cmd_status(cfg)
        if sub == "run":
            force = "--force" in rest or "-f" in rest
            return commands.cmd_run(cfg, force=force)
        if sub == "review":
            return commands.cmd_review(cfg)
        if sub == "approve":
            return commands.cmd_approve(cfg)
        if sub == "restore":
            text = " ".join(rest)
            return commands.cmd_restore(cfg, text)
        if sub == "purge":
            return commands.cmd_purge(cfg)
        if sub == "quarantine":
            return commands.cmd_quarantine(cfg)
        if sub == "ledger":
            return commands.cmd_ledger(cfg)
        if sub == "config":
            return commands.cmd_config(cfg)
    except Exception as exc:  # noqa: BLE001
        return f"memtriage {sub} failed: {exc}"
    return (
        f"Unknown subcommand {sub!r}. Known: "
        + ", ".join(SUBCOMMANDS)
    )


# -- registration ------------------------------------------------------------

def register(ctx) -> None:
    # Layer-1 trigger: observe memory writes (never override the built-in).
    try:
        ctx.register_hook("post_tool_call", _on_post_tool_call)
    except Exception as exc:  # noqa: BLE001
        logger.warning("memtriage: post_tool_call hook registration failed: %s", exc)

    # Layer-2 backstop + quarantine housekeeping at session start.
    try:
        ctx.register_hook("on_session_start", _on_session_start)
    except Exception as exc:  # noqa: BLE001
        logger.warning("memtriage: on_session_start hook registration failed: %s", exc)

    try:
        ctx.register_command(
            "memtriage",
            handler=_handle_slash,
            description=(
                "Memory triage: route knowledge to skills/profile/provider/scripts "
                "and evict stale entries when the memory store nears capacity."
            ),
            args_hint="<status|run|review|approve|restore|purge|quarantine|ledger|config>",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("memtriage: command registration failed: %s", exc)

    try:
        ctx.register_tool(
            name="mem_triage",
            toolset="memory",
            schema=TOOL_SCHEMA,
            handler=_handle_tool,
            description=(
                "Run memory triage or inspect its state. Actions: status, run, "
                "review, approve, restore, purge, quarantine, ledger, config."
            ),
            is_async=False,
            emoji="🧠",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("memtriage: tool registration failed: %s", exc)