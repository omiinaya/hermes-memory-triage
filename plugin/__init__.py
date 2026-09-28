"""hermes-memory-triage plugin surface: hooks, slash command, agent tool.

Registers (each guarded so a failure never blocks plugin load):

* ``post_tool_call`` hook  — layer-1 trigger: after any successful ``memory``
  tool write, checks store utilization and fires triage when the threshold is
  crossed (cooldown-aware).  Observes, never replaces, the built-in tool.
* ``on_session_start`` hook — layer-2 backstop: re-checks utilization on every
  fresh session (also purges expired quarantine entries).
* **In-session notification** — whenever triage produces a plan (or one is
  already queued for review), the plugin injects the report into the active
  conversation so it is actually seen, not just written to disk.
* ``/memtriage`` slash command — status | run | review | approve | restore |
  purge | quarantine | ledger | config.
* ``mem_triage`` agent tool — same subcommands for agent-driven triage.

All heavy logic lives in the stdlib-only ``memtriage`` package.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List

# Hermes loads plugins with submodule_search_locations=<plugin>/ — but the
# `memtriage` package lives one level UP as a sibling of plugin/. Put the
# plugin's parent dir on sys.path so `from memtriage import ...` resolves.
_parent = str(Path(__file__).resolve().parent.parent)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from memtriage import commands, state
from memtriage.config import Config
from memtriage.triage import run_triage

logger = logging.getLogger(__name__)

# Captured at register() time so hook callbacks can surface reports in the
# active conversation via inject_message. None in headless runs (no session).
_ctx: Any = None

SUBCOMMANDS = (
    "status run review approve restore restore-file snapshots purge "
    "quarantine decisions ledger config setup".split()
)

TOOL_DESCRIPTION = (
    "Run memory triage or inspect its state. Actions: status, run, review, "
    "approve, restore, restore-file, snapshots, purge, quarantine, decisions, "
    "ledger, config, setup."
)

# The argument schema. Hermes' registry does ``{**schema, "name": ...}`` and
# then ``sanitize_tool_schemas`` REPLACES any ``function.parameters`` that is
# not a dict with ``{"type":"object","properties":{},"required":[]}``. A bare
# JSON Schema (properties at the top level, no "parameters" key) therefore
# reaches the model as an EMPTY tool with no description -- the action enum
# invisible and the tool effectively uncallable. Verified 2026-09-27.
# It must be nested under "parameters".
_ARGUMENTS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "description": "What to do.",
            "enum": [
                "status", "run", "review", "approve", "restore",
                "restore-file", "snapshots", "purge",
                "quarantine", "decisions", "ledger", "config", "setup",
            ],
        },
        "force": {"type": "boolean", "description": "Run even if below threshold."},
        "text": {"type": "string", "description": "Evicted entry text for restore."},
    },
    "required": ["action"],
}

# The shape register_tool wants: a description plus the arguments nested under
# "parameters". The registry spreads this into the function object, so
# "description" arrives at the model and "parameters" survives sanitization.
TOOL_SCHEMA = {
    "description": TOOL_DESCRIPTION,
    "parameters": _ARGUMENTS,
}


def _load_cfg() -> Config:
    try:
        return Config.load()
    except ValueError as exc:
        logger.warning("memtriage config invalid, using defaults: %s", exc)
        return Config()


def _auto_run_allowed() -> bool:
    """Whether an unattended triage may run without a human in the loop.

    Two independent brakes, both defaulting to the cautious answer:

    * ``MEMTRIAGE_AUTO_RUN`` -- an explicit opt-in. Unset or "0" means the
      plugin never writes to the stores on its own; the user runs
      ``memtriage run`` when they want it. This is the default because
      ``mode: auto`` in config.json was set long before the guards in this
      executor existed, so "auto" does not imply the current safety rules
      were ever considered.
    * ``MEMTRIAGE_ALLOW_WRITES`` -- an extra brake on top of that for the
      case where auto-run is enabled but writes are not wanted yet (plan and
      report only).

    Either can be set to "1"/"true" to permit the behaviour.
    """
    def _truthy(name: str) -> bool:
        return os.environ.get(name, "").strip().lower() in (
            "1", "true", "yes", "on",
        )

    if not _truthy("MEMTRIAGE_AUTO_RUN"):
        # INFO, not debug. Verified 2026-09-27: a drop-in carrying these vars
        # was added to hermes-gateway.service and `systemctl show -p
        # Environment` reported them present -- because it echoes the UNIT
        # FILE. /proc/<pid>/environ had neither, so auto-triage silently did
        # nothing for hours while every "is it enabled" check said yes. At
        # debug level a wrong brake is indistinguishable from "nothing was
        # over threshold", which is exactly how that went unnoticed.
        logger.info(
            "memtriage: over threshold but MEMTRIAGE_AUTO_RUN is not set in "
            "the RUNNING process; not running an unattended triage. Run "
            "`mem_triage action=run` manually. (If you believe the var IS "
            "set, check /proc/<pid>/environ -- `systemctl show -p Environment` "
            "reads the unit file, not the process.)"
        )
        return False
    if not _truthy("MEMTRIAGE_ALLOW_WRITES"):
        logger.info(
            "memtriage: MEMTRIAGE_AUTO_RUN is set but MEMTRIAGE_ALLOW_WRITES "
            "is not in the RUNNING process; not writing to the stores "
            "unattended."
        )
        return False
    return True


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
    """Layer 2: session-start backstop + quarantine housekeeping.

    Also surfaces any plan that is already queued for review but has not yet
    been shown in a session (e.g. created headlessly).
    """
    try:
        from memtriage import quarantine

        cfg = _load_cfg()
        quarantine.purge_expired(cfg)
        awaiting = state.awaiting_approval(cfg)
        if awaiting and awaiting not in state.notified_runs(cfg):
            _notify_awaiting(cfg, awaiting)
        _maybe_run_triage("session start")
    except Exception:  # noqa: BLE001
        logger.debug("on_session_start triage check failed", exc_info=True)


def _maybe_run_triage(reason: str) -> None:
    """Run an over-threshold triage, OFF the caller's thread.

    post_tool_call fires synchronously inside the user's own tool call, and a
    triage shells out to Cerveau with a 600s timeout. Measured live: a real
    triage prompt (45KB) takes ~40s, so running this inline stalled the
    session that triggered it for the better part of a minute -- and in auto
    mode that stall happens on a memory write, which is exactly when the user
    is waiting on a result.

    The hook returns immediately; the work happens on a daemon thread and the
    result is still surfaced via _notify_result. Threshold, cooldown and
    awaiting-approval are all checked BEFORE the thread starts, so this does
    not change how often a triage runs -- only how long the caller waits.
    """
    cfg = _load_cfg()
    if not state.is_over_threshold(cfg):
        return
    if state.cooldown_active(cfg):
        return
    if state.awaiting_approval(cfg):
        # A plan is already queued for review — never stomp it with a fresh one.
        #
        # But it must not block FOREVER. Nothing removed this key:
        # `clear_awaiting()` had zero call sites, so a single unapproved plan
        # wedged auto mode permanently and the store sat over budget while
        # the plugin reported nothing. Expire on the cooldown window and say so.
        if state.awaiting_approval_expired(cfg):
            stale = state.awaiting_approval(cfg)
            state.clear_awaiting(cfg)
            logger.info(
                "memtriage: plan %s sat unreviewed past the %d-minute window; "
                "expiring it so auto-triage can run again (its plan file is "
                "kept in reports/ and can still be reviewed by hand)",
                stale, cfg.cooldown_minutes,
            )
        else:
            return
    if not _auto_run_allowed():
        return

    def _work() -> None:
        try:
            result = run_triage(cfg, reason=reason, force=True)
            _notify_result(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("memtriage auto-run failed: %s", exc)

    threading.Thread(
        target=_work, name="memtriage-auto", daemon=True
    ).start()


# -- in-session notification -------------------------------------------------

def _inject(text: str) -> bool:
    """Best-effort injection of a message into the active conversation.

    Returns whether the message was actually DELIVERED. Callers must only
    mark a run notified on True: a headless run (cron, CLI, no gateway
    channel) previously marked itself delivered while nothing was ever shown,
    so every later session start saw the id in ``notified_runs`` and
    suppressed the report permanently. Verified 2026-09-27.
    """
    if _ctx is None:
        logger.debug("memtriage: no session context; cannot inject message")
        return False
    try:
        _ctx.inject_message(text, role="user")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("memtriage: inject_message failed: %s", exc)
        return False


def _report_body(report_path: str) -> str:
    path = Path(report_path)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _notify_result(result: Dict[str, Any]) -> None:
    """Surface a fresh triage result (its report) in the active session."""
    if not result or not result.get("triggered"):
        return
    run_id = result.get("run_id", "")
    mode = result.get("mode", "manual")
    plan = result.get("plan") or []
    report = _report_body(result.get("report_path", ""))
    execution = result.get("execution")
    head = (
        f"[memtriage] {mode} triage {run_id} finished — {len(plan)} action(s).\n"
    )
    if execution:
        head += _render_execution(execution)
    else:
        head += (
            "Nothing applied yet — review and approve, or edit the plan "
            "before it runs."
        )
    body = f"\n\n{report}" if report else ""
    # Only mark delivered if it WAS delivered. A headless run (cron / CLI /
    # no channel) must stay eligible for the next session start.
    if _inject(head + body) and run_id:
        state.mark_notified(_load_cfg(), run_id)


def _render_execution(execution: Dict[str, Any]) -> str:
    """Render a compact 'what it did, and why' block from an executor summary."""
    applied = execution.get("applied", [])
    pending = execution.get("pending", [])
    errors = execution.get("errors", [])
    blocked = execution.get("blocked", [])
    decisions = execution.get("decisions", [])
    lines = [_plural("applied", len(applied))]
    lines += [f"  + {a}" for a in applied]
    if pending:
        lines.append(_plural("pending", len(pending), "best-effort: gateway/cron unreachable"))
        lines += [f"  ~ {p}" for p in pending]
    # Blocked is the one that matters most: a refusal left the store exactly
    # as over budget as it was, and it used to be invisible here.
    if blocked:
        lines.append(
            f"{len(blocked)} refused by a safety guard — nothing was removed, "
            f"the store is still as full as before:"
        )
        lines += [f"  x {b}" for b in blocked]
    if errors:
        lines.append(f"errors ({len(errors)}):")
        lines += [f"  ! {e}" for e in errors]
    # The "why": each decision's stated reason, capped so the injection stays
    # readable in a chat window.
    reasons = [
        (d.get("action") or {}).get("reason")
        for d in decisions
        if (d.get("action") or {}).get("reason")
    ]
    if reasons:
        shown = reasons[:12]
        lines.append(f"why ({len(reasons)} stated reason(s)):")
        for r in shown:
            lines.append(f"  - {r}")
        if len(reasons) > len(shown):
            lines.append(f"  ... +{len(reasons) - len(shown)} more")
    log = execution.get("decision_log")
    if log:
        lines.append(f"full log: {log} (mem_triage action=decisions)")
    return "\n".join(lines)


def _plural(noun: str, n: int, tail: str = "") -> str:
    suffix = f" — {tail}" if tail else ""
    return f"{n} {noun} action(s){suffix}."


def _notify_execution(cfg: Config) -> None:
    """After a plan is applied (manual approve), inject what it did."""
    from memtriage import plan as plan_mod

    rec = state.last_execution(cfg)
    if not rec:
        return
    run_id = rec.get("run_id", "?")
    execution = rec.get("summary") or {}
    lines = [f"[memtriage] Plan {run_id} executed — impact:"]
    lines += plan_mod.render_impact(execution) or ["  (no usage snapshots)"]
    lines.append(_render_execution(execution))
    # Post-execution store usage, so the user sees how much headroom was won.
    from memtriage import inventory as _inv

    for t in _inv.inventory_memory(cfg):
        frac = t["fraction"] * 100
        lines.append(f"  {t['target']}: {t['current']:,}/{t['limit']:,} chars ({frac:.0f}%)")
    _inject("\n".join(lines))


def _notify_awaiting(cfg: Config, run_id: str) -> None:
    """Surface an already-queued plan that was never shown in a session."""
    report = _report_body(str(cfg.reports_dir / f"report-{run_id}.md"))
    head = (
        f"[memtriage] A triage plan is awaiting your review (run {run_id}).\n"
        "Nothing applied yet — run /memtriage review (or approve) to act on it."
    )
    body = f"\n\n{report}" if report else ""
    if _inject(head + body):
        state.mark_notified(cfg, run_id)


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
            # `mode: auto` in config.json makes triage.execute_plan WRITE on the
            # spot. A tool call is unattended just as much as the post_tool_call
            # hook is, so both go through the same two brakes. Without this a
            # single `mem_triage {"action":"run"}` from the model wrote a
            # SKILL.md to disk with no human in the loop (2026-09-27).
            if cfg.mode == "auto" and not _auto_run_allowed():
                cfg.mode = "manual"
                logger.info(
                    "memtriage: auto mode downgraded to manual for this run "
                    "(MEMTRIAGE_AUTO_RUN / MEMTRIAGE_ALLOW_WRITES not both set); "
                    "a plan was produced but nothing was written."
                )
            return commands.cmd_run(cfg, force=force)
        if sub == "review":
            return commands.cmd_review(cfg)
        if sub == "approve":
            out = commands.cmd_approve(cfg)
            # After the plan is applied, inject what it did in-session.
            _notify_execution(_load_cfg())
            return out
        if sub == "restore":
            text = " ".join(rest)
            return commands.cmd_restore(cfg, text)
        if sub == "purge":
            return commands.cmd_purge(cfg)
        if sub == "quarantine":
            return commands.cmd_quarantine(cfg)
        if sub == "decisions":
            return commands.cmd_decisions(cfg, rest[0] if rest else "")
        if sub == "ledger":
            return commands.cmd_ledger(cfg)
        if sub == "config":
            return commands.cmd_config(cfg)
        if sub == "snapshots":
            target = rest[0] if rest else ""
            return commands.cmd_snapshots(cfg, target=target)
        if sub == "restore-file":
            if len(rest) < 2:
                return "Usage: memtriage restore-file <memory|user> <snapshot-name>"
            return commands.cmd_restore_file(cfg, target=rest[0], name=rest[1])
        if sub == "setup":
            verify_only = "--verify" in rest
            return commands.cmd_setup(cfg, verify_only=verify_only)
    except Exception as exc:  # noqa: BLE001
        return f"memtriage {sub} failed: {exc}"
    return (
        f"Unknown subcommand {sub!r}. Known: "
        + ", ".join(SUBCOMMANDS)
    )


# -- registration ------------------------------------------------------------

def register(ctx) -> None:
    global _ctx
    _ctx = ctx  # capture the live context so hooks can inject reports in-session

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