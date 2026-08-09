"""Cerveau dispatch: hand the inventory to the decision profile.

Cerveau is a dedicated Hermes profile (``hermes -p cerveau``) whose only job
is routing decisions. The plugin invokes it one-shot via:

    <cerveau_bin> -p <profile> chat -q <prompt>

with the full inventory + taxonomy as a JSON payload, and expects a JSON array
of validated actions back in the reply. A timeout and a reply-size cap keep a
misbehaving profile from hanging or flooding the triage.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, List

from .config import Config
from .plan import PlanValidationError, parse_plan

DEFAULT_TIMEOUT_SECONDS = 240
MAX_REPLY_CHARS = 60_000

# The taxonomy Cerveau is seeded with (mirrors memtriage/plan.py actions).
PROMPT_TEMPLATE = """You are Cerveau, the memory triage decision-maker.

A Hermes memory store has reached {threshold:.0%} of its budget. Your job is to
produce a routing plan: a JSON array of actions. Route every piece of knowledge
to its proper home and flag stale entries. Act now; do not ask questions.

Input inventory (JSON):
{payload}

Routing taxonomy — choose ONE action per item:

- "keep": the entry is durable, current, and correctly placed. No change.
- "consolidate": two or more entries overlap; merge them into one tighter
  entry. Requires "entries": [indices...] (>=2) and "text": the merged text.
- "route-to-skill": a reusable procedure or workflow that will be needed
  again -> becomes a SKILL.md. Requires "skill_name" and "text" (the SKILL.md
  body, without frontmatter — the plugin adds it).
- "route-to-profile": an identity/role/preference fact about the user ->
  target "user".
- "route-to-provider": rich episodic knowledge or scene context that should
  live in the memory provider (scene block). Requires "text".
- "route-to-script": a recurring automatable task -> a runnable script.
  Requires "script_name", "script_ext" (py|sh|bash), and "text" (script body).
  Only use when the task is genuinely mechanical and repeatable.
- "evict-to-quarantine": stale/superseded/contradicted entry. The plugin
  quarantines it reversibly — you are not deleting. Never evict an entry that
  is identity-critical or security/environment-critical; demote those to
  "route-to-provider" instead. Never evict a fact that contradicts a NEWER
  entry; evict the older one. Never evict anything on a first pass unless it
  is clearly superseded.
- "delete": never use. The plugin never hard-deletes on a triage pass.

Staleness signals (from the user's doctrine):
1. Superseded — a newer entry covers the same subject (new convention wins).
2. Contradiction — a newer entry states the opposite; the newer one survives.
3. Zero utility — no provider search heat and no recent session references,
   non-identity class, older than ~60 days.
4. Explicit "forget this" instructions from the user.

Safety rules (non-negotiable):
- Identity, security, and environment-critical facts are NEVER evicted; at
  worst they are demoted to the provider or consolidated.
- Evictions are reversible (quarantine); do not over-evict. When in doubt,
  choose "keep" or "consolidate".
- Never invent facts; only re-route text that is present in the inventory.
- A routing plan that frees space but loses knowledge is a failure. Prefer
  routing (skill/provider/profile/script) over eviction.

Dedup: the ledger below lists already-routed destinations. Do NOT re-route a
fact that is already a skill or profile entry unless the ledger copy is
outdated.

Ledger (already routed):
{ledger}

Reply with ONLY the JSON array, no prose, no markdown fences:
[{{
  "action": "keep|consolidate|route-to-skill|route-to-profile|route-to-provider|route-to-script|evict-to-quarantine",
  "target": "memory|user",
  "index": 0,
  "text": "...",
  "reason": "short justification",
  "skill_name": "...", "script_name": "...", "script_ext": "py"
}}]
"""


def build_prompt(cfg: Config, payload: Dict[str, Any], ledger: List[Dict[str, Any]]) -> str:
    return PROMPT_TEMPLATE.format(
        threshold=cfg.threshold_percent,
        payload=json.dumps(payload, indent=1, ensure_ascii=False),
        ledger=json.dumps(ledger, indent=1, ensure_ascii=False),
    )


def dispatch(cfg: Config, prompt: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> List[Dict[str, Any]]:
    """Run Cerveau one-shot and return a validated plan."""
    cmd = [
        cfg.cerveau_bin,
        "-p",
        cfg.cerveau_profile,
        "chat",
        "-q",
        prompt,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Cerveau binary {cfg.cerveau_bin!r} not found on PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Cerveau triage timed out after {timeout}s."
        ) from exc
    reply = (proc.stdout or "")[-MAX_REPLY_CHARS:]
    if proc.returncode != 0:
        stderr = (proc.stderr or "")[-2000:]
        raise RuntimeError(
            f"Cerveau exited {proc.returncode}: {stderr.strip()}"
        )
    try:
        return parse_plan(reply)
    except PlanValidationError as exc:
        raise RuntimeError(f"Cerveau returned an invalid plan: {exc}") from exc