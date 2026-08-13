"""Cerveau decision-profile knowledge: seeding + learning.

Ships the canonical "brain" (taxonomy, staleness rubric, safety rules, output
contract, doctrine) as templates under ``profile_seeds/cerveau/`` in the repo,
and provisions them into the decision profile's own memory files:

* ``seed_profile(cfg)`` — non-destructive: writes SOUL.md / MEMORY.md /
  USER.md ONLY when missing (or when the taxonomy has never been seeded),
  so a profile's learned entries are never clobbered by re-running setup.
* ``record_decision(cfg, summary)`` — appends a bounded learning entry to the
  decision profile's MEMORY.md (after the "Learning" marker). Every triage
  outcome becomes environment-specific experience Cerveau reviews before the
  next decision — this is what makes his judgment improve over time.

All paths resolve through HERMES_HOME (or ~/.hermes) and the configured
``cerveau_profile`` name, so the same code works on Windows/macOS/Linux.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from .config import Config

# Marker line in the seeded MEMORY.md; learning entries live below it.
LEARNING_MARKER = "Learning — the entries below this marker"
# Cap on learning entries kept (oldest dropped first) to bound memory growth.
MAX_LEARNING_ENTRIES = 40

# Shipped seed templates live at <repo>/profile_seeds/cerveau/.
_TEMPLATE_FILES = {
    "soul": "soul.template.md",
    "memory": "memory.template.md",
    "user": "user.template.md",
}


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def decision_profile_dir(cfg: Config) -> Path:
    """The decision profile's home directory (does not need to exist)."""
    return _hermes_home() / "profiles" / cfg.cerveau_profile


def templates_dir() -> Path:
    """Repo templates dir, resolved relative to this package (works when the
    plugin is a git clone under ~/.hermes/plugins/ or the repo checkout)."""
    return Path(__file__).resolve().parent.parent / "profile_seeds" / "cerveau"


def _read_template(name: str) -> str:
    path = templates_dir() / _TEMPLATE_FILES[name]
    return path.read_text(encoding="utf-8")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _has_seed_marker(text: str) -> bool:
    """True if the file already contains the seeded taxonomy marker."""
    return "Routing taxonomy" in text or "Output contract" in text


def seed_profile(cfg: Config) -> dict:
    """Non-destructively provision the decision profile's brain files.

    Returns a dict of {filename: "written"|"exists"|"skipped"}.
    """
    prof_dir = decision_profile_dir(cfg)
    memory_dir = prof_dir / "memories"
    result: dict = {}

    soul = prof_dir / "SOUL.md"
    memory = memory_dir / "MEMORY.md"
    user = memory_dir / "USER.md"

    # SOUL.md: append the mission block when the file exists but lacks it;
    # write the template when the profile has no SOUL.md at all.
    if soul.exists():
        text = soul.read_text(encoding="utf-8", errors="replace")
        if "Cerveau, the memory triage decision-maker" in text:
            result["SOUL.md"] = "exists"
        else:
            _atomic_write(soul, text.rstrip() + "\n\n" + _read_template("soul"))
            result["SOUL.md"] = "appended"
    else:
        _atomic_write(soul, _read_template("soul"))
        result["SOUL.md"] = "written"

    # MEMORY.md: seed only if no taxonomy was ever seeded (never clobber
    # learning entries on re-run).
    if memory.exists():
        text = memory.read_text(encoding="utf-8", errors="replace")
        if _has_seed_marker(text):
            result["MEMORY.md"] = "exists"
        else:
            seeded = _read_template("memory") + "\n" + text.rstrip() + "\n"
            _atomic_write(memory, seeded)
            result["MEMORY.md"] = "seeded-before-learning"
    else:
        _atomic_write(memory, _read_template("memory"))
        result["MEMORY.md"] = "written"

    # USER.md: seed only if missing (doctrine, no learning entries here).
    if user.exists():
        result["USER.md"] = "exists"
    else:
        _atomic_write(user, _read_template("user"))
        result["USER.md"] = "written"

    return result


def record_decision(cfg: Config, summary: str) -> Optional[Path]:
    """Append one bounded learning entry to the decision profile's MEMORY.md.

    The entry is timestamped and placed below the LEARNING_MARKER (which the
    seed template guarantees exists). Oldest entries are dropped to keep the
    file bounded. Returns the file path on success, None when skipped (no
    profile dir / no marker / summary empty).
    """
    summary = (summary or "").strip()
    if not summary:
        return None
    memory = decision_profile_dir(cfg) / "memories" / "MEMORY.md"
    if not memory.exists():
        return None
    text = memory.read_text(encoding="utf-8", errors="replace")
    if LEARNING_MARKER not in text:
        return None

    ts = time.strftime("%Y-%m-%d %H:%M")
    entry = f"- [{ts}] {summary}"

    # Keep everything before the marker (the seeded taxonomy + doctrine),
    # then rebuild the learning section from existing entries + the new one.
    head = text.split(LEARNING_MARKER, 1)[0].rstrip()
    entries = [
        line for line in text.splitlines()
        if line.startswith("- [")
    ]
    entries.append(entry)
    entries = entries[-MAX_LEARNING_ENTRIES:]

    rebuilt = head + "\n" + marker_header + "\n" + "\n".join(entries) + "\n"
    _atomic_write(memory, rebuilt)
    return memory


# Marker header kept in one place so record_decision and the template agree.
marker_header = LEARNING_MARKER + " are YOUR recorded triage decisions. Review them before deciding: they show what was routed, evicted, and why, in THIS environment. Past decisions that worked are patterns to repeat; repeated eviction mistakes are patterns to avoid. Your own memory is the reason your judgment improves over time."
