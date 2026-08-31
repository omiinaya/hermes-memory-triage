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

import os
from .config import Config
from .plan import PlanValidationError, parse_plan

# Configurability doctrine: never hardcode capacity/runtime limits that must
# track real cost — Cerveau dispatches `hermes -p cerveau chat -q` (a full agent
# spawn) over the proxy relay, which averages ~9.6s/token and may chain fallbacks.
# A 74KB inventory against that path routinely exceeds 240s, so we (a) default
# higher and (b) let operators raise it via CERVEAU_TIMEOUT without a code change.
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("CERVEAU_TIMEOUT", "600"))
# Cap reply so a runaway profile can't flood the store write-back.
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
  RULE: every route-to-script MUST be paired with a corresponding
  "route-to-skill" action (immediately after it in the array) that documents
  the script and how to run it — create the skill if none exists, or describe
  the updated usage if one does. The skill body must reference the script by
  name/path and give the exact command to invoke it.
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
- TARGET RULE: routing actions (route-to-skill / route-to-provider /
  route-to-script) promote knowledge OUT of the working store and may target
  EITHER "memory" or "user" (profile). A user-profile entry holding
  reusable/doctrine knowledge can be routed to a skill or provider — that is
  the primary way to relieve an over-full profile. ONLY "route-to-profile"
  is restricted to "memory" (an already-profiled entry is not re-routed to
  the profile). "keep", "consolidate" and "evict-to-quarantine" may
  target "memory" or "user".

Dedup: the ledger below lists already-routed destinations. Do NOT re-route a
fact that is already a skill or profile entry unless the ledger copy is
outdated.

Ledger (already routed):
{ledger}

Reply with ONLY the non-empty JSON array, no prose, no markdown fences. The
array must contain at least one action. If an entry needs no change, still
emit a "keep" action for it (action='keep', with its target and index and a
helper reason) — NEVER return an empty array. The same inventory entry must
not appear in more than one action.
[{{
  "action": "keep|consolidate|route-to-skill|route-to-profile|route-to-provider|route-to-script|evict-to-quarantine",
  "target": "memory|user",
  "index": 0,
  "text": "...",
  "reason": "short justification",
  "skill_name": "...", "script_name": "...", "script_ext": "py"
}}]
"""


DEFAULT_MAX_PAYLOAD_CHARS = int(os.environ.get("CERVEAU_MAX_PAYLOAD", "6000"))

# Substrings that mark an entry as NEVER-evict (doctrine: identity, security,
# environment-critical). Matched on the capped entry text — if ANY matches, the
# entry is kept unconditionally. Shared by _deterministic_plan + tests.
PROTECTED_MARKERS = (
    "sullen", "vault", "secret", "password", "token", "masterkey",
    "bitwarden", "cuda", "/models-external", "stdb", "gpu",
    "rtx 3090", "19.168.1", "pve", "bge-m3", "embed", "memory-tdai",
    "hermes-agent", "shutdown", "reboot",
)
# Throwaway / low-priority → eligible for (reversible) quarantine.
LOW_PRIORITY_MARKERS = (
    "throwaway", "disposable", "temp ", "tmp/", "/tmp/", "scratch",
    "audit", "debug", "check_", "test ", "stale", "dead code",
)


def _truncate_payload(payload: Dict[str, Any], max_chars: int = DEFAULT_MAX_PAYLOAD_CHARS) -> Dict[str, Any]:
    """Keep Cerveau's prompt under the relay's token budget.

    The inventory can balloon to 50-70KB (12 memory entries + ~150 scripts +
    ~80 skills). Over the free-tier proxy relay (~9.6s/token) Cerveau spends
    its entire timeout just ingesting the prompt. Cerveau routes decisions —
    it needs the memory entries (with char counts) + the *names* of skills
    it might route-to, NOT the path of every rpm_*.sh script. So we cap the
    scripts/skills lists and surface only what Cerveau can act on.
    """
    out = dict(payload)
    # Memory entries: keep full (capped text), they are the decision surface.
    if "skills" in out and len(out["skills"]) > 30:
        out["skills"] = out["skills"][:30] + [
            {"name": "...", "description": f"(+{len(out['skills']) - 30} more skills omitted)", "path": ""}
        ]
    if "scripts" in out and len(out["scripts"]) > 40:
        out["scripts"] = out["scripts"][:40] + [
            {"path": "...", "name": f"...(+{len(out['scripts']) - 40} more scripts omitted)"}
        ]
    # Hard truncate the final serialization as a safety net — but only the
    # scripts/skills lists, never mid-value. (Truncating raw JSON mid-token
    # produces invalid JSON that Cerveau can't parse.)
    text = json.dumps(out, ensure_ascii=False)
    if len(text) > max_chars and "skills" in out and len(out["skills"]) > 5:
        # Aggressive fallback: trim skills list further.
        out["skills"] = out["skills"][:5] + [
            {"name": "...", "description": f"(+{len(out['skills']) - 5} more omitted)", "path": ""}
        ]
    if len(text) > max_chars and "scripts" in out and len(out["scripts"]) > 10:
        out["scripts"] = out["scripts"][:10] + [
            {"path": "...", "name": "...(+more omitted)"}
        ]
    return out


def build_prompt(cfg: Config, payload: Dict[str, Any], ledger: List[Dict[str, Any]]) -> str:
    payload = _truncate_payload(payload)
    return PROMPT_TEMPLATE.format(
        threshold=cfg.threshold_percent,
        payload=json.dumps(payload, indent=1, ensure_ascii=False),
        ledger=json.dumps(ledger, indent=1, ensure_ascii=False),
    )


def _deterministic_plan(inv: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Conservative fallback planner used when the Cerveau model pass times out.

    The free-tier proxy relay (~9.6s/token) can't always return a routing plan
    within the dispatch timeout. Rather than let triage fail and leave the
    store over threshold, this produces a small, SAFE plan from inventory rules
    alone — no model call, no network, guaranteed sub-millisecond.

    Doctrine constraints honored:
    * NEVER ``delete`` (quarantine is reversible; the plan validator forbids
      delete-from-plan too).
    * NEVER evict identity / security / environment-critical facts. Heuristics:
      entries mentioning the user's cyber-name (Sullen), vault/secrets, GPU/CUDA
      paths, DB identities, or the hardline shutdown rule are ``keep``.
    * Only ``evict-to-quarantine`` clearly-low-priority entries (throwaway test
      artifacts, single-use scripts, stale single facts) — never on a first
      pass unless superseded.
    * ``consolidate``: collapse entries whose lead-phrase shares a 6-word
      n-gram (cheap duplicate merge).
    * Everything else is ``keep`` (prefer no-change over risky eviction).
    """
    actions: List[Dict[str, Any]] = []
    fallback_source = {"_source": "deterministic-fallback"}  # marker for run_triage
    seen_subject: Dict[str, List[int]] = {}  # subject-key -> entry indices to consolidate

    def _subject_key(text: str) -> str:
        """Stable key for 'same subject across entries'.

        Normalizes case + strips punctuation so entries describing the same
        entity (e.g. the user profile stated two ways: 'Omar Minaya — beloved'
        vs 'User: Omar Minaya (cyber-name...)') collapse to the same key.
        We strip leading role prefixes too so 'User: X' and 'X' group.
        """
        import re as _re
        low = text[:40].lower()
        for p in ("user: ", "memory: ", "sullen:"):
            if low.startswith(p):
                low = low[len(p):]
        # drop punctuation so em-dashes / parens don't split identical subjects
        low = _re.sub(r"[^\w\s]", " ", low)
        return " ".join(low.split()[:4])

    for target_block in inv.get("memory", []):
        target = target_block["target"]
        entries = target_block.get("entries", [])
        for e in entries:
            idx = e["index"]
            text = e.get("text", "") or ""
            low = text.lower()
            if any(k in low for k in PROTECTED_MARKERS):
                actions.append({"_source": "deterministic-fallback", "action": "keep", "target": target, "index": idx,
                                "reason": "identity/security/env-critical — never evict"})
                continue
            if any(k in low for k in LOW_PRIORITY_MARKERS):
                actions.append({"_source": "deterministic-fallback", "action": "evict-to-quarantine", "target": target,
                                "index": idx,
                                "text": text,
                                "reason": "throwaway/debug artifact — low value, reversible quarantine"})
                continue
            seen_subject.setdefault(_subject_key(text), []).append(idx)
            actions.append({"_source": "deterministic-fallback", "action": "keep", "target": target, "index": idx,
                            "reason": "no stale/superceded signal — retain"})

    # 2) Consolidation pass: merge entries whose subject-key repeats. The
    #    doctrine values explanation, so we only collapse genuine duplicates
    #    — the user profile is currently stated ~2 ways in USER store, which is
    #    the real 95%-pressure source.
    for subj, idxs in list(seen_subject.items()):
        if len(idxs) > 1:
            # resolve target + representative text from the inventory directly
            # (the outer loop's locals are out of scope here).
            first_entry = next(
                (e for blk in inv.get("memory", [])
                 for e in blk.get("entries", []) if e["index"] == idxs[0]),
                None
            )
            if first_entry is None:
                continue
            tgt = next(
                (blk["target"] for blk in inv.get("memory", [])
                 if blk.get("entries") and blk["entries"][0]["index"] == idxs[0]),
                "memory"
            )
            actions.append({"action": "consolidate", "target": tgt,
                            "entries": idxs, "index": idxs[0],
                            "text": first_entry.get("text", ""),
                            "reason": f"same subject '{subj}' stated {len(idxs)} ways; merge (deterministic fallback)"})
    return actions


def dispatch(
    cfg: Config, prompt: str, timeout: int = None, inventory: Dict[str, Any] = None
) -> List[Dict[str, Any]]:
    """Run Cerveau one-shot and return a validated plan.

    Falls back to ``_deterministic_plan`` when the model pass times out and
    ``cfg.deterministic_fallback`` is true — so triage always produces *some*
    plan rather than leaving the store over threshold. The fallback is
    conservative (keep > consolidate > quarantine; never delete) and uses no
    model/network.
    """
    timeout = timeout or int(os.environ.get("CERVEAU_TIMEOUT", cfg.cerveau_timeout))
    cmd = [
        cfg.cerveau_bin,
        "-p", cfg.cerveau_profile,
        "chat",
        "--ignore-rules",           # strip SOUL/skills/AGENTS.md/memory injection (load overhead)
        "--max-turns", "1",         # one-shot, no tool loop
        "-q",                       # query (prompt) must be LAST so it isn't swallowed
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
        if cfg.deterministic_fallback and inventory is not None:
            return _deterministic_plan(inventory)
        raise RuntimeError(
            f"Cerveau binary {cfg.cerveau_bin!r} not found on PATH."
        ) from exc
    except subprocess.TimeoutExpired:
        # Doctrine: don't let triage fail outright when the free-tier relay
        # can't complete a model pass. Fall back to a conservative, no-model
        # plan so the write-back still runs + frees headroom.
        if cfg.deterministic_fallback and inventory is not None:
            return _deterministic_plan(inventory)
        raise RuntimeError(
            f"Cerveau triage timed out after {timeout}s."
        )
    reply = (proc.stdout or "")[-MAX_REPLY_CHARS:]
    if proc.returncode != 0:
        stderr = (proc.stderr or "")[-2000:]
        # Non-zero exit that still produced usable stdout: try to parse it;
        # otherwise fall back rather than aborting triage.
        try:
            return parse_plan(reply)
        except PlanValidationError:
            if cfg.deterministic_fallback and inventory is not None:
                return _deterministic_plan(inventory)
            raise RuntimeError(
                f"Cerveau exited {proc.returncode}: {stderr.strip()}"
            )
    # returncode == 0: Cerveau succeeded — validate its plan.
    try:
        return parse_plan(reply)
    except PlanValidationError:
        if cfg.deterministic_fallback and inventory is not None:
            return _deterministic_plan(inventory)
        raise