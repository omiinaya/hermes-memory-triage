"""Memory store access: read/write the built-in Hermes memory files.

Faithfully replicates the format and locking discipline of the built-in
``memory`` tool (tools/memory_tool.py) so writes from this plugin are
invisible to the tool and vice versa:

* Files: ``$HERMES_HOME/memories/MEMORY.md`` (agent notes) and
  ``USER.md`` (user profile).
* Entries are separated by a line containing only ``§`` (the literal
  delimiter is ``\\n§\\n``); each entry is stripped.
* Usage is ``len("\\n§\\n".join(entries))`` — the same metric the built-in
  tool reports as ``current/limit``.
* Writes hold an exclusive lock on ``<file>.lock`` (fcntl on POSIX,
  msvcrt on Windows, no-op where neither exists) and replace the file
  atomically via ``os.replace``.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

try:  # pragma: no cover - platform probe
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover - platform probe
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

ENTRY_DELIMITER = "\n§\n"

TARGET_MEMORY = "memory"
TARGET_USER = "user"

DEFAULT_CHAR_LIMITS = {TARGET_MEMORY: 2200, TARGET_USER: 1375}

MEMORY_FILENAME = "MEMORY.md"
USER_FILENAME = "USER.md"


def memories_dir() -> Path:
    """Resolve the Hermes memories directory (HERMES_HOME aware)."""
    hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return Path(hermes_home) / "memories"


def path_for(target: str) -> Path:
    if target == TARGET_USER:
        return memories_dir() / USER_FILENAME
    return memories_dir() / MEMORY_FILENAME


def parse_entries(raw: str) -> List[str]:
    """Split a memory file body into stripped, non-empty entries."""
    return [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]


def serialize_entries(entries: List[str]) -> str:
    """Join entries back into the canonical file body (no trailing marker)."""
    stripped = [e.strip() for e in entries if e.strip()]
    return ENTRY_DELIMITER.join(stripped)


def char_count(entries: List[str]) -> int:
    if not entries:
        return 0
    return len(ENTRY_DELIMITER.join(entries))


def char_limit(target: str) -> int:
    return DEFAULT_CHAR_LIMITS.get(target, DEFAULT_CHAR_LIMITS[TARGET_MEMORY])


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Exclusive advisory lock on ``<path>.lock`` (mirrors the built-in tool)."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None and msvcrt is None:
        yield
        return
    fd = open(lock_path, "a+", encoding="utf-8")
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
        yield
    finally:
        if fcntl:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
        elif msvcrt:
            try:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, IOError):
                pass
        fd.close()


def read_entries(target: str) -> List[str]:
    """Read entries for a target; empty file or missing file -> []."""
    path = path_for(target)
    if not path.exists():
        return []
    try:
        return parse_entries(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return []


def write_entries(target: str, entries: List[str]) -> None:
    """Replace the target file's entries, under lock, atomically."""
    path = path_for(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = serialize_entries(entries)
    with file_lock(path):
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)


def usage(target: str) -> Dict[str, Any]:
    """Compute the usage report for a target, mirroring the built-in tool."""
    entries = read_entries(target)
    current = char_count(entries)
    limit = char_limit(target)
    fraction = (current / limit) if limit else 0.0
    return {
        "target": target,
        "path": str(path_for(target)),
        "entries": entries,
        "current": current,
        "limit": limit,
        "fraction": round(fraction, 4),
    }


def replace_entry(target: str, old_text: str, new_content: str) -> bool:
    """Replace the first entry containing ``old_text`` with ``new_content``.

    Mirrors the built-in tool's substring-match semantics: the *shortest*
    entry containing ``old_text`` is replaced.  Returns False when no entry
    matches (no-op), True on success.
    """
    entries = read_entries(target)
    matches = [e for e in entries if old_text in e]
    if not matches:
        return False
    victim = min(matches, key=len)
    idx = entries.index(victim)
    new_entries = list(entries)
    new_entries[idx] = new_content.strip()
    write_entries(target, new_entries)
    return True


def remove_entry(target: str, old_text: str) -> bool:
    """Remove the shortest entry containing ``old_text``. False when absent."""
    entries = read_entries(target)
    matches = [e for e in entries if old_text in e]
    if not matches:
        return False
    victim = min(matches, key=len)
    new_entries = [e for e in entries if e is not victim]
    write_entries(target, new_entries)
    return True


def append_entry(target: str, content: str) -> Dict[str, Any]:
    """Append an entry; refuse (like the built-in tool) when over budget."""
    content = content.strip()
    if not content:
        return {"success": False, "error": "Content cannot be empty."}
    entries = read_entries(target)
    if content in entries:
        return {
            "success": False,
            "error": "Entry already exists (no duplicate added).",
        }
    new_total = char_count(entries + [content])
    if new_total > char_limit(target):
        current = char_count(entries)
        return {
            "success": False,
            "error": (
                f"Memory at {current:,}/{char_limit(target):,} chars. "
                f"Adding this entry ({len(content)} chars) would exceed the limit."
            ),
            "usage": f"{current:,}/{char_limit(target):,}",
        }
    write_entries(target, entries + [content])
    return {"success": True}
