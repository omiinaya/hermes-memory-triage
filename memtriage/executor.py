"""Executor: apply a validated triage plan to the real destinations.

The plugin applies its own plans (no internal tool coupling). Every write
targets a surface this plugin is licensed to own:
* memory store files (exact \\n§\\n format, lock-disciplined, atomic),
* the skills tree (SKILL.md files),
* the user profile (USER.md),
* the scripts directory (runnable scripts, best-effort cron),
* the memory provider gateway (best-effort scene block persist).

Provider and cron delivery are best-effort: if the gateway is unreachable or a
registration fails, the action is recorded as "pending" (visible in the report)
rather than silently dropped.

Every routed artifact is recorded in the ledger with provenance so Cerveau
never re-routes it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from . import ledger, quarantine, store as memory_store
from .config import Config

SKILL_FRONTMATTER = """---
name: {name}
description: {description}
version: 1.0.0
metadata:
  provenance: "{provenance}"
---
"""


def _safe(value: str) -> str:
    """Collapse a value into a safe directory/filename token."""
    out = []
    for ch in value.lower().strip():
        out.append(ch if ch.isalnum() or ch in ("-", "_") else "-")
    cleaned = "".join(out).strip("-_")
    return cleaned or "untitled"


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _make_executable(path: Path) -> None:
    try:
        os.chmod(path, os.stat(path).st_mode | 0o111)
    except OSError:
        pass  # Windows / restricted FS: ignore


def _dispatch_to_provider(cfg, text: str) -> str:
    """Best-effort scene block write to the provider gateway. Returns notice."""
    url = cfg.provider_base_url.rstrip("/") + "/v3/atomic/update"
    payload = {"op": "upsert", "kind": "scene", "content": text}
    try:
        import urllib.request

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return str(resp.status)
    except Exception as exc:  # noqa: BLE001
        return f"pending (gateway unreachable: {exc})"


def _register_cron(script_abs: str, schedule: str) -> str:
    """Best-effort cron registration via ``hermes cron add`` CLI."""
    try:
        proc = subprocess.run(
            ["hermes", "cron", "add", "--script", script_abs, "--schedule", schedule],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0:
            return "ok"
        return f"pending (hermes cron add: {proc.stderr.strip()[:200]})"
    except Exception as exc:  # noqa: BLE001
        return f"pending ({exc})"


class Executor:
    """Applies a plan; collects results into a report-friendly summary."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.applied: List[str] = []
        self.pending: List[str] = []
        self.errors: List[str] = []
        self._run_id = ""

    def _note(self, msg: str) -> None:
        self.applied.append(msg)

    def _note_pending(self, msg: str) -> None:
        self.pending.append(msg)

    def execute_plan(
        self, plan: List[Dict[str, Any]], run_id: str, provenance: str
    ) -> Dict[str, Any]:
        self._run_id = run_id
        for n, action in enumerate(plan):
            kind = action["action"]
            try:
                self._apply(kind, action, provenance)
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"action #{n} ({kind}): {exc}")
        return {"applied": self.applied, "pending": self.pending, "errors": self.errors}

    # -- per-action dispatch ---------------------------------------------

    def _apply(self, kind: str, a: Dict[str, Any], provenance: str) -> None:
        target = a.get("target", "memory")
        if kind == "keep":
            return
        if kind == "consolidate":
            self._do_consolidate(target, a, provenance)
            return
        if kind == "route-to-skill":
            self._do_skill(a, provenance)
            return
        if kind == "route-to-profile":
            self._do_profile(a, provenance)
            return
        if kind == "route-to-provider":
            self._do_provider(a, provenance)
            return
        if kind == "route-to-script":
            self._do_script(a, provenance)
            return
        if kind == "evict-to-quarantine":
            self._do_evict(target, a)
            return
        raise ValueError(f"unhandled action {kind!r}")

    # -- action implementations ------------------------------------------

    def _do_consolidate(self, target: str, a: Dict[str, Any], provenance: str) -> None:
        idxs = [int(i) for i in (a.get("entries") or [])]
        merged = (a.get("text") or "").strip()
        if not idxs or not merged:
            raise ValueError("consolidate requires entries[] and text")
        for i in sorted(idxs, reverse=True):
            _drop_index(target, i)
        _append_guarded(target, merged)
        self._note(f"consolidated {len(idxs)} entries into {len(merged)} chars [{target}]")
        ledger.record(
            self.cfg, kind="consolidate", destination=f"{target}#consolidated",
            summary=merged[:60], run_id=self._run_id, provenance=provenance,
        )

    def _do_skill(self, a: Dict[str, Any], provenance: str) -> None:
        name = a.get("skill_name") or a.get("name") or "routed-skill"
        body = (a.get("text") or a.get("body") or "").strip()
        if not body:
            raise ValueError("route-to-skill requires text")
        category = a.get("category") or "tools"
        root = self.cfg.skills_root
        target = root / _safe(category) / _safe(name) / "SKILL.md"
        description = body.splitlines()[0][:80]
        content = SKILL_FRONTMATTER.format(
            name=_safe(name), description=description, provenance=provenance
        ) + body + "\n"
        _write_atomic(target, content)
        self._note(f"routed to skill '{name}' ({target})")
        ledger.record(
            self.cfg, kind="skill", destination=str(target),
            summary=body[:200], run_id=self._run_id, provenance=provenance,
        )

    def _do_profile(self, a: Dict[str, Any], provenance: str) -> None:
        text = (a.get("text") or "").strip()
        if not text:
            raise ValueError("route-to-profile requires text")
        _append_guarded("user", text)
        self._note(f"routed to profile ({len(text)} chars)")
        ledger.record(
            self.cfg, kind="user", destination="USER.md",
            summary=text[:200], run_id=self._run_id, provenance=provenance,
        )

    def _do_provider(self, a: Dict[str, Any], provenance: str) -> None:
        text = (a.get("text") or "").strip()
        if not text:
            raise ValueError("route-to-provider requires text")
        notice = _dispatch_to_provider(self.cfg, text)
        if notice.startswith("pending"):
            self._note_pending(f"route-to-provider: {notice}")
        else:
            self._note(f"routed to provider (gateway {notice})")
        ledger.record(
            self.cfg, kind="provider", destination="provider/scene",
            summary=text[:200], run_id=self._run_id, provenance=provenance,
        )

    def _do_script(self, a: Dict[str, Any], provenance: str) -> None:
        script_name = a.get("script_name") or "routed-script"
        ext = (a.get("script_ext") or "py").lstrip(".").lower()
        body = (a.get("text") or "").strip()
        if not body:
            raise ValueError("route-to-script requires text")
        if ext not in ("py", "sh", "bash"):
            raise ValueError(f"unsupported script_ext {ext!r}")
        script_path = self.cfg.scripts_root / f"{_safe(script_name)}.{ext}"
        _write_atomic(script_path, body)
        if ext in ("sh", "bash"):
            _make_executable(script_path)
        self._note(f"routed to script '{script_name}' ({script_path})")
        if a.get("cron_schedule"):
            result = _register_cron(str(script_path), a["cron_schedule"])
            if result == "ok":
                self._note(f"registered cron for '{script_name}'")
            else:
                self._note_pending(f"cron for '{script_name}': {result}")
        ledger.record(
            self.cfg, kind="script", destination=str(script_path),
            summary=body[:200], run_id=self._run_id, provenance=provenance,
        )

    def _do_evict(self, target: str, a: Dict[str, Any]) -> None:
        index = a.get("index")
        if index is None:
            raise ValueError("evict-to-quarantine requires an index")
        victim = _drop_index(target, int(index))
        quarantine.evict(
            self.cfg, target=target, text=victim,
            reason=a.get("reason", ""), run_id=self._run_id,
        )
        self._note(f"evicted-to-quarantine {len(victim)} chars [{target}]")


# -- store helpers -----------------------------------------------------------


def _drop_index(target: str, idx: int) -> str:
    """Remove the entry at ``idx`` and return its text."""
    entries = memory_store.read_entries(target)
    if idx < 0 or idx >= len(entries):
        raise IndexError(f"index {idx} out of range for target {target!r}")
    victim = entries[idx]
    kept = [e for i, e in enumerate(entries) if i != idx]
    memory_store.write_entries(target, kept)
    return victim


def _append_guarded(target: str, text: str) -> None:
    result = memory_store.append_entry(target, text)
    if not result.get("success", False):
        raise ValueError(result.get("error", "append failed"))
