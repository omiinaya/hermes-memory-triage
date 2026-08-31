"""Inventory: gather everything Cerveau needs to make routing decisions.

Reads the same surfaces the plugin can write to:
* the built-in memory store (MEMORY.md / USER.md entries),
* the skills tree (names + descriptions from SKILL.md frontmatter),
* the scripts directory listing.

All best-effort — a missing directory simply yields an empty inventory rather
than raising, so the triage always produces something usable for Cerveau.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

from . import store as memory_store
from .config import Config

FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL
)
KEY_RE = re.compile(r"^([A-Za-z0-9_.-]+):\s*(.*)$")


def _page_frontmatter(path: Path) -> Dict[str, str]:
    """Return a {key: value} dict from a YAML-ish frontmatter block.

    Only reads top-level scalar lines (no nested structure) — all we need are
    ``name`` and ``description``.
    """
    out: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    m = FRONTMATTER_RE.match(text)
    if not m:
        return out
    for line in m.group(1).splitlines():
        km = KEY_RE.match(line)
        if km:
            out[km.group(1)] = km.group(2).strip().strip("\"'")
    return out


def inventory_skills(cfg: Config) -> List[Dict[str, Any]]:
    """List every SKILL.md under the skills root (recursively)."""
    result: List[Dict[str, Any]] = []
    root = cfg.skills_root
    if not root.exists():
        return result
    for sk in sorted(root.rglob("SKILL.md")):
        meta = _page_frontmatter(sk)
        result.append(
            {
                "name": meta.get("name", sk.parent.name),
                "description": meta.get("description", ""),
                "path": str(sk),
            }
        )
    return result


def inventory_scripts(cfg: Config) -> List[Dict[str, Any]]:
    """List scripts under the configured scripts directory (recursive)."""
    result: List[Dict[str, Any]] = []
    root = cfg.scripts_root
    if not root.exists():
        return result
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.name.startswith("."):
            result.append({"path": str(p), "name": p.name})
    return result


def _cap_entry_text(text: str, max_chars: int = 160) -> str:
    """Truncate a single memory entry for the Cerveau payload.

    Cerveau routes decisions, it does not need byte-faithful copies — a
    160-char lead plus the character count is enough for ``keep vs evict``.
    This keeps the relay prompt well under token budget; without it a 95%
    user store (3k chars across ~10 entries + scripts/skills list) produces
    a ~74KB JSON blob that the proxy relay (avg 9.6s/token) can't return
    within the dispatch timeout.
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0] + " …[truncated]"


def inventory_memory(cfg: Config) -> List[Dict[str, Any]]:
    """Returns per-target usage plus a compact entry breakdown."""
    targets: List[Dict[str, Any]] = []
    for target in (memory_store.TARGET_MEMORY, memory_store.TARGET_USER):
        u = memory_store.usage(target)
        # The user profile is the decision surface for "can we relieve the
        # over-full profile" — truncating a 2,041-char doctrine blob to 160
        # chars hides the sub-facts Cerveau could route away (and hides that
        # many are static/rottable). Give Cerveau the FULL user entry text;
        # the memory target keeps the compact cap (it's already well under
        # threshold and payload size matters for the slow relay).
        cap = None if target == memory_store.TARGET_USER else 160
        entries = [
            {
                "index": i,
                "text": e if cap is None else _cap_entry_text(e, cap),
                "chars": len(e),
            }
            for i, e in enumerate(u["entries"])
        ]
        targets.append(
            {
                "target": target,
                "current": u["current"],
                "limit": u["limit"],
                "fraction": u["fraction"],
                "entries": entries,
            }
        )
    return targets


def collect(cfg: Config) -> Dict[str, Any]:
    """Build the complete inventory payload for Cerveau."""
    return {
        "memory": inventory_memory(cfg),
        "skills": inventory_skills(cfg),
        "scripts": inventory_scripts(cfg),
    }