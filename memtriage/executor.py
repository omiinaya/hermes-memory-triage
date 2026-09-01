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

Index discipline: routing, evict and consolidate actions all reference the
ORIGINAL inventory indices. The executor snapshots each target's entries once,
resolves every removal against that snapshot, then rebuilds each target a single
time at the end. An earlier removal can therefore never shift a later index, so
multi-evict / multi-route runs are correct. Routing actions copy knowledge to
its destination AND drop the source, so the working store actually shrinks —
that is what makes the impact measurement meaningful.
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


_PROVIDER_KEY_ENV = ("TDAI_LLM_API_KEY", "MEMORY_TENCENTDB_LLM_API_KEY")
_PROVIDER_SERVICE_ID_ENV = ("TDAI_GATEWAY_SERVICE_ID", "MEMORY_TENCENTDB_GATEWAY_SERVICE_ID")


def _provider_api_key(cfg) -> str:
    """Provider gateway key: in-memory override first, then env, else empty.

    NEVER committed, never logged. The env family is TDAI_* /
    MEMORY_TENCENTDB_* (source: /root/.memory-tencentdb/tdai-gateway.yaml,
    e.g. TDAI_LLM_API_KEY). Empty means "no key available" and the caller
    fails open to a pending notice.
    """
    override = getattr(cfg, "provider_api_key", "") or ""
    if override:
        return override
    for name in _PROVIDER_KEY_ENV:
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return ""


def _dispatch_to_provider(cfg, text: str, scene_path: str = "memtriage/triage.md") -> str:
    """Best-effort knowledge write to the provider gateway. Returns a notice.

    Correct wiring (re-verified live 2026-08-13 against the running gateway
    at 127.0.0.1:8420, v0.1.0):
      - Route:  /v2/conversation/add  (the CREATE path: it ingests the text
        as an L0 message and the gateway pipeline extracts L1 atomic notes
        from it, returning 200). /v2/atomic/update and /v2/scenario/write
        are UPDATE-ONLY — they 404 when the note/file does not exist, so a
        new offload can never land there.
      - Auth:   Authorization: Bearer {api_key} + x-tdai-service-id (both
        required; the gateway 401s without either). The key comes from env
        (TDAI_LLM_API_KEY / MEMORY_TENCENTDB_LLM_API_KEY) — never committed.
      - Body:   {session_id, messages:[{role, content}]}; tenancy defaults
        to the default bucket when omitted.
    """
    api_key = _provider_api_key(cfg)
    if not api_key:
        return "pending (no provider api key in env: TDAI_LLM_API_KEY / MEMORY_TENCENTDB_LLM_API_KEY)"
    service_id = ""
    for name in _PROVIDER_SERVICE_ID_ENV:
        val = os.environ.get(name, "").strip()
        if val:
            service_id = val
            break
    if not service_id:
        service_id = getattr(cfg, "provider_service_id", "") or "hermes-memtriage"
    url = cfg.provider_base_url.rstrip("/") + "/v2/conversation/add"
    payload = {
        "session_id": scene_path,
        "messages": [{"role": "user", "content": text}],
    }
    try:
        import urllib.request

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "x-tdai-service-id": service_id,
            },
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


def _usage_snapshot() -> Dict[str, Dict[str, Any]]:
    """Per-target usage snapshot keyed by target name."""
    snap: Dict[str, Dict[str, Any]] = {}
    for t in memory_store.TARGET_MEMORY, memory_store.TARGET_USER:
        u = memory_store.usage(t)
        snap[u["target"]] = {
            "current": u["current"],
            "limit": u["limit"],
            "fraction": u["fraction"],
        }
    return snap


class Executor:
    """Applies a plan; collects results into a report-friendly summary."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.applied: List[str] = []
        self.pending: List[str] = []
        self.errors: List[str] = []
        self._run_id = ""
        self._provenance = ""

    def _note(self, msg: str) -> None:
        self.applied.append(msg)

    def _note_pending(self, msg: str) -> None:
        self.pending.append(msg)

    def _ledger(self, kind: str, destination: str, summary: str) -> None:
        ledger.record(
            self.cfg, kind=kind, destination=destination,
            summary=summary, run_id=self._run_id, provenance=self._provenance,
        )

    def execute_plan(
        self, plan: List[Dict[str, Any]], run_id: str, provenance: str = ""
    ) -> Dict[str, Any]:
        """Apply the plan and its routing/evict/consolidate removals."""
        self._run_id = run_id
        self._provenance = provenance
        before = _usage_snapshot()

        original: Dict[str, List[str]] = {
            t: memory_store.read_entries(t)
            for t in (memory_store.TARGET_MEMORY, memory_store.TARGET_USER)
        }
        removals: Dict[str, set] = {t: set() for t in original}    # idx -> drop
        appends: Dict[str, List[str]] = {t: [] for t in original}  # new entries

        for n, action in enumerate(plan):
            try:
                self._apply(action, original, removals, appends)
            except Exception as exc:  # noqa: BLE001
                self.errors.append(
                    f"action #{n} ({action.get('action')}): {exc}"
                )

        # Rebuild each target once from the surviving originals + appends.
        # SAFETY FLOOR: the USER profile is identity-critical — a plan must
        # never drop it below a meaningful floor in one shot. Observed
        # catastrophe: auto-mode routed the ENTIRE 2,041-char identity/doctrine
        # entry to the provider, taking the user store from 76% to 8% in a
        # single action. The memory target is rotateable scratch and may be
        # emptied (post==0) but even then the refusal keeps entries safe.
        # For user: refuse any removal that would drop it below 10% of limit.
        # For memory: refuse only a complete empty (post == 0).
        USER_MIN_FRACTION = 0.10
        # IDENTITY GUARD: a "user" entry carrying identity/doctrine markers can
        # NEVER be routed away or evicted, even if siblings keep the store above
        # the floor. The model legitimately tries to demote the giant
        # identity/doctrine blob to the provider; that is wrong — the identity
        # belongs in the working profile. (mirror cerveau.PROTECTED_MARKERS)
        for target in original:
            limit = memory_store.char_limit(target)
            removals[target] = {
                i for i in removals[target]
                if 0 <= i < len(original[target])  # drop out-of-range
            }
            if target == memory_store.TARGET_USER:
                for i in list(removals[target]):
                    text = original[target][i]
                    low = text.lower()
                    if any(k in low for k in (
                        "sullen", "minaya", "never evict", "voice boundary",
                        "vault", "cyber-name",
                    )):
                        self.errors.append(
                            f"action would remove identity entry #{i} "
                            f"(identity/doctrine markers) — refused; kept"
                        )
                        removals[target].discard(i)
            kept = [
                e for i, e in enumerate(original[target])
                if i not in removals[target]
            ]
            final = kept + appends[target]
            post_chars = memory_store.char_count(final)
            post_fraction = (post_chars / limit) if limit else 1.0
            floor = (
                USER_MIN_FRACTION
                if target == memory_store.TARGET_USER
                else 0.0  # memory: only refuse a complete empty
            )
            # Refuse when the plan actually removes something AND the post
            # state falls below the target's floor.
            if removals[target] and limit and post_fraction < floor:
                self.errors.append(
                    f"target '{target}' would drop to "
                    f"{post_chars}/{limit} chars ({post_fraction*100:.0f}%) "
                    f"< {floor*100:.0f}% floor — refused; source entries kept"
                )
                removals[target] = set()  # keep everything in place
                kept = list(original[target])
                final = kept + appends[target]
            if final != original[target]:
                memory_store.write_entries(target, final)
                # Only count in-range removal indices when reporting freed
                # chars — a hallucinated/out-of-range index (e.g. a stale
                # ledger reference) must not crash the whole rebuild.
                freed = sum(
                    len(original[target][i])
                    for i in removals[target]
                    if 0 <= i < len(original[target])
                )
                self.applied.append(f"freed {freed} chars [{target}]")

        after = _usage_snapshot()
        return {
            "applied": self.applied,
            "pending": self.pending,
            "errors": self.errors,
            "before": before,
            "after": after,
        }

    # -- per-action dispatch ---------------------------------------------

    def _apply(self, a: Dict[str, Any], original, removals, appends) -> None:
        kind = a["action"]
        target = a.get("target", "memory")
        if kind == "keep":
            return
        if kind == "consolidate":
            self._do_consolidate(a, removals, appends)
            return
        if kind == "route-to-skill":
            self._do_skill(a)
            self._remove_source(removals, a)
            return
        if kind == "route-to-profile":
            self._do_profile(a, appends)
            self._remove_source(removals, a)
            return
        if kind == "route-to-provider":
            ok = self._do_provider(a)
            if ok:
                self._remove_source(removals, a)
            else:
                self._note_pending("route-to-provider: kept source entry (gateway write failed)")
            return
        if kind == "route-to-script":
            self._do_script(a)
            self._remove_source(removals, a)
            return
        if kind == "evict-to-quarantine":
            self._do_evict(a, original, removals)
            return
        raise ValueError(f"unhandled action {kind!r}")

    @staticmethod
    def _remove_source(removals, a) -> None:
        idx = a.get("index")
        if idx is None:
            return
        target = a.get("target", "memory")
        removals.setdefault(target, set()).add(int(idx))

    # -- action implementations ------------------------------------------

    def _do_consolidate(self, a, removals, appends) -> None:
        target = a.get("target", "memory")
        idxs = [int(i) for i in (a.get("entries") or [])]
        merged = (a.get("text") or "").strip()
        if len(idxs) < 2 or not merged:
            raise ValueError("consolidate requires entries[] (>=2) and text")
        removals[target].update(i for i in idxs)
        appends[target].append(merged)
        self._note(f"consolidated {len(idxs)} entries into {len(merged)} chars [{target}]")
        self._ledger("consolidate", f"{target}#consolidated", merged[:60])

    def _do_skill(self, a) -> None:
        name = a.get("skill_name") or a.get("name") or "routed-skill"
        body = (a.get("text") or a.get("body") or "").strip()
        if not body:
            raise ValueError("route-to-skill requires text")
        category = a.get("category") or "tools"
        target = self.cfg.skills_root / _safe(category) / _safe(name) / "SKILL.md"
        description = body.splitlines()[0][:80]
        content = SKILL_FRONTMATTER.format(
            name=_safe(name), description=description, provenance=self._provenance
        ) + body + "\n"
        _write_atomic(target, content)
        self._note(f"routed to skill '{name}' ({target})")
        self._ledger("skill", str(target), body[:200])

    def _do_profile(self, a, appends) -> None:
        text = (a.get("text") or "").strip()
        if not text:
            raise ValueError("route-to-profile requires text")
        appends["user"].append(text)
        self._note(f"routed to profile ({len(text)} chars)")
        self._ledger("user", "USER.md", text[:200])

    def _do_provider(self, a) -> bool:
        """Best-effort provider write. Returns True only if the gateway write
        succeeded. On failure the caller must KEEP the source entry in the
        working store (no silent data loss) — it is only recoverable from the
        plan file otherwise."""
        text = (a.get("text") or "").strip()
        if not text:
            raise ValueError("route-to-provider requires text")
        notice = _dispatch_to_provider(self.cfg, text)
        if notice.startswith("pending"):
            self._note_pending(f"route-to-provider: {notice}")
            return False
        self._note(f"routed to provider (gateway {notice})")
        self._ledger("provider", "provider/scene", text[:200])
        return True

    def _do_script(self, a) -> None:
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
        self._ledger("script", str(script_path), body[:200])

    def _do_evict(self, a, original, removals) -> None:
        target = a.get("target", "memory")
        index = a.get("index")
        if index is None:
            raise ValueError("evict-to-quarantine requires an index")
        idx = int(index)
        entries = original[target]
        if idx < 0 or idx >= len(entries):
            raise IndexError(f"source index {idx} out of range for target {target!r}")
        victim_text = entries[idx]
        quarantine.evict(
            self.cfg, target=target, text=victim_text,
            reason=a.get("reason", ""), run_id=self._run_id,
        )
        removals[target].add(idx)
        self._note(f"quarantined {len(victim_text)} chars [{target}]")