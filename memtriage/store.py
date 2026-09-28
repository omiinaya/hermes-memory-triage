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

# atomicio imports nothing from this package, so this cannot cycle.
from .atomicio import unique_tmp

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
    """Return the char limit for a target, honoring the live Hermes config.

    The built-in memory tool reads memory_char_limit / user_char_limit from
    config.yaml (raised to 4000/3000 in this deployment). Mirror that source
    so triage reports the same (honest) capacity the tool actually enforces,
    instead of a stale hardcoded default. Falls back to defaults if the
    config file is unreadable.
    """
    override = None
    try:
        import yaml  # type: ignore
        cfg_path = Path(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")) / "config.yaml"
        if cfg_path.exists():
            with open(cfg_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            m = (data.get("memory") or {})
            override = {
                TARGET_MEMORY: int(m.get("memory_char_limit") or DEFAULT_CHAR_LIMITS[TARGET_MEMORY]),
                TARGET_USER: int(m.get("user_char_limit") or DEFAULT_CHAR_LIMITS[TARGET_USER]),
            }
    except Exception:  # noqa: S110 — fall back to defaults on any read error
        pass
    limits = override or DEFAULT_CHAR_LIMITS
    return limits.get(target, limits[TARGET_MEMORY])


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Exclusive advisory lock on the store, blocking.

    Shares ONE lock file with :mod:`memtriage.locking` (the read-modify-write
    span) so a write from a built-in-tool-mirroring function actually
    excludes an executing plan, and vice versa. Previously this used a
    separate ``.lock`` name and only spanned the write, so the two paths
    could interleave freely.
    """
    from . import locking as _locking

    ctx = _locking.store_lock(path, timeout=_locking.LOCK_WAIT_SECONDS)
    ctx.__enter__()
    try:
        yield
    finally:
        ctx.__exit__(None, None, None)


class StoreUnreadable(RuntimeError):
    """The store file exists but could not be read.

    CRITICAL DISTINCTION: an absent or genuinely empty file yields ``[]``.
    A file that exists and is non-empty but cannot be read (EACCES, EBUSY,
    a concurrent ``os.replace``, truncation, undecodable bytes) must NOT be
    reported as empty — doing so made ``execute_plan`` rebuild the store from
    an empty snapshot and overwrite the user's real profile with a single
    entry. Callers that rebuild a store from a snapshot must use
    :func:`read_entries_strict` so this case aborts instead of destroying data.
    """


def read_entries(target: str) -> List[str]:
    """Read entries for a target; missing or genuinely empty file -> [].

    Swallows read errors and returns ``[]`` — the historical behaviour, kept
    for read-only callers (``usage``, ``replace_entry``, ``append_entry``)
    where a permissive result is harmless. Never use this to build a
    rewrite snapshot; use :func:`read_entries_strict`.
    """
    path = path_for(target)
    if not path.exists():
        return []
    try:
        return parse_entries(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return []


def read_entries_strict(target: str) -> List[str]:
    """Like :func:`read_entries` but raises instead of faking an empty store.

    Returns ``[]`` ONLY when the file is absent or truly empty. An existing,
    non-empty file that fails to read raises :class:`StoreUnreadable` so the
    caller aborts the whole operation rather than writing back a store built
    from a phantom empty snapshot.
    """
    path = path_for(target)
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise StoreUnreadable(
            f"{path} exists but could not be read ({type(exc).__name__}: {exc}); "
            f"refusing to treat it as an empty store"
        ) from exc
    entries = parse_entries(raw)
    if not entries and raw.strip():
        # Non-empty bytes that parse to zero entries means the delimiter
        # format changed underneath us (or the file is corrupt). That is not
        # the same as an empty store and must not be silently accepted.
        raise StoreUnreadable(
            f"{path} has {len(raw)} bytes but parses to 0 entries "
            f"(unrecognised format?); refusing to treat it as an empty store"
        )
    return entries


def write_entries(target: str, entries: List[str]) -> None:
    """Replace the target file's entries, under lock, atomically.

    C5 (2026-09-28). The temp file was a FIXED name (``<path>.tmp``).
    ``file_lock`` is re-entrant per thread, so a second PROCESS writing the
    same store would share that temp path -- one writer's ``os.replace``
    renames the file the other is still writing, and the loser's content is
    silently discarded. ``atomicio.unique_tmp`` already solved exactly this
    and its docstring calls the bug measured-and-fixed; this call site was
    simply never converted. Temp names are now private per process AND
    thread.

    Note there is deliberately NO guard here against an empty list:
    writing an empty store is a legitimate primitive operation (tests seed
    one, and a store can genuinely be empty). The hazard is the EXECUTOR
    arriving at zero entries, which is a different question and is refused
    there, in ``Executor._execute_locked``.
    """
    path = path_for(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = serialize_entries(entries)
    with file_lock(path):
        tmp = unique_tmp(path)
        try:
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, path)
        finally:
            # A failed replace can leave the temp behind; the name is
            # private to this process+thread, so removing it cannot destroy
            # another writer's work.
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass


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
