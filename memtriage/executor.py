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
import shutil
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import ledger, locking, quarantine, snapshots, store as memory_store
from .config import Config, _default_data_dir

# Marker file recording which run ids have already landed. A plan's indices
# are positional, so a replay resolves them against a shifted store and would
# delete unrelated entries — refuse the replay instead.
APPLIED_RUNS_FILENAME = "applied_runs.json"
MAX_APPLIED_RUNS = 200

# Identity/doctrine markers that make a "user" entry un-removable. Mirrors
# the intent of cerveau.PROTECTED_MARKERS but is the executor's own single
# source of truth so the fallback and the executor cannot drift apart.
# Deliberately SPECIFIC names/claims, not topic words: a bare "identity" marker
# made every entry mentioning the identity guard itself un-removable, which
# blocks exactly the doctrine routing that should be moved to a skill.
IDENTITY_MARKERS = (
    "sullen", "minaya", "never evict", "voice boundary", "vault",
    "cyber-name", "guarded", "ciel", "real name",
)

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


def _already_applied(cfg: Config, run_id: str) -> Optional[str]:
    """Return the ISO timestamp when ``run_id`` was already applied, else None.

    Reads the applied-runs marker file, which is the ONLY reliable way to
    detect a replay: plan indices are positional, so once a run has landed,
    the same indices refer to different (or no) entries.
    """
    if not run_id:
        return None
    path = cfg.data_dir / APPLIED_RUNS_FILENAME
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw.get(run_id)


def _mark_applied(cfg: Config, run_id: str) -> None:
    """Record that ``run_id`` landed, so a retry is refused, not replayed."""
    if not run_id:
        return
    path = cfg.data_dir / APPLIED_RUNS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
    except (json.JSONDecodeError, OSError):
        raw = {}
    raw[run_id] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # Bound growth: keep the newest 200 run ids.
    if len(raw) > MAX_APPLIED_RUNS:
        keep = sorted(raw.items())[-MAX_APPLIED_RUNS:]
        raw = dict(keep)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _yaml_scalar(value: str) -> str:
    """Escape a value for a position that ALREADY has surrounding quotes.

    The skill frontmatter template writes ``provenance: "{value}"``, so this
    escapes the interior only. Do NOT add another layer of quotes here.
    """
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", " ")
        .replace("\r", " ")
    )


def _yaml_quote(value: str) -> str:
    """Quote a scalar for YAML frontmatter, quotes included.

    A body-derived description containing ``": "``, a leading ``#``, or a
    quote produced invalid frontmatter, which silently made the new skill
    invisible to ``inventory_skills`` — routed knowledge that could never be
    loaded again. Always double-quote and escape.
    """
    return f'"{_yaml_scalar(value)}"'


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique tmp name: a fixed "<name>.tmp" races with a concurrent executor
    # and can publish a half-written file.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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


def _production_data_dir() -> str:
    """The real, non-sandboxed data root, ignoring MEMTRIAGE_HOME.

    ``config._default_data_dir`` honours MEMTRIAGE_HOME, so it tracks the
    sandbox and cannot answer "is this run isolated?". This resolves the
    production location: MEMTRIAGE_HOME unset, or the path a real
    ~/.memtriage install would use.
    """
    from . import config as _cfgmod

    saved = os.environ.pop("MEMTRIAGE_HOME", None)
    try:
        prod = _cfgmod._default_data_dir()
        real_config = Path(os.path.expanduser("~/.memtriage")) / "config.json"
        if real_config.is_file():
            try:
                raw = json.loads(real_config.read_text(encoding="utf-8"))
                pinned = raw.get("data_dir")
                if pinned:
                    prod = Path(os.path.expanduser(str(pinned)))
            except (OSError, ValueError, UnicodeDecodeError):
                pass
        return str(prod)
    finally:
        if saved is not None:
            os.environ["MEMTRIAGE_HOME"] = saved


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
    # A sandboxed run (isolated data_dir / HERMES_HOME) must never write to the
    # REAL production gateway. The provider is an external side effect that no
    # data_dir redirect covers, so isolation has to be enforced explicitly --
    # a dry run against a copied store previously pushed three real entries
    # into production memory.
    # The check is against the PRODUCTION data root (~/.memtriage, possibly
    # pinned by a real config.json), not against whatever _default_data_dir()
    # resolves to -- that helper is itself MEMTRIAGE_HOME-aware, so it moves
    # with the sandbox and would never detect isolation.
    isolated = os.environ.get("MEMTRIAGE_ALLOW_PROVIDER", "").strip()
    data_dir = str(getattr(cfg, "data_dir", ""))
    prod_dir = _production_data_dir()
    home = os.environ.get("HERMES_HOME", "")
    if not isolated and (
        data_dir != prod_dir
        or (home and home != os.path.expanduser("~/.hermes"))
    ):
        return (
            "pending (refusing to write to the production gateway from a "
            "non-default data root; set MEMTRIAGE_ALLOW_PROVIDER=1 to "
            "override)"
        )
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
        # Quarantine writes are buffered until a removal has survived every
        # guard, so a refused eviction leaves no phantom record.
        self._pending_quarantine: List[Tuple[str, str]] = []
        # Removal notes per target, committed only when that target's store
        # is actually rewritten. Keyed by target because the rebuild loop
        # handles each target independently.
        self._provisional: Dict[str, List[str]] = {}
        # Pre-write snapshot paths taken this run, surfaced in the summary so
        # a user can undo the whole run from the report.
        self._snapshot_records: List[Dict[str, Any]] = []

    def _note(self, msg: str) -> None:
        self.applied.append(msg)

    def _note_removal(self, target: str, msg: str) -> None:
        """Narrate a REMOVAL, which a later guard can still revoke.

        A routing action has already written its skill/provider/script by the
        time it is narrated, so those claims are safe in `applied`. A removal
        is not: the staleness check, the identity guard and the safety floor
        all run AFTER the action, so an eviction that is later refused must
        not appear in `applied` — doing so reported "quarantined 2990 chars"
        for a run that wrote nothing.
        """
        self._provisional.setdefault(target, []).append(msg)

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
        # A reused Executor must not concatenate two runs' results.
        self.applied = []
        self.pending = []
        self.errors = []
        self._provisional = {}
        self._snapshot_records = []
        # Which source entries each pending append REPLACES, per target.
        # Keyed by provenance, not text: a consolidation paraphrases its
        # sources, so only the executor knows which append is a substitute.
        # None = the append replaces nothing and must always be kept.
        self._append_sources: Dict[str, List[Optional[frozenset]]] = {}
        # Refusals the user must act on. Distinct from errors: an identity
        # guard refusal on an over-budget target is correct behaviour that
        # nonetheless leaves the store unrelieved, and it must be visible.
        self.blocked: List[Dict[str, Any]] = []
        # Removals authorised by a `split`. A split is the identity guard's
        # escape hatch: it keeps a verbatim clause of the entry in the store
        # and routes the rest away, so the guard must not then refuse the
        # removal and leave the store exactly as over budget as it was. The
        # safety floor below still applies to a split.
        self._split_removals: set = set()
        before = _usage_snapshot()

        # EXCLUSIVITY: the built-in `memory` tool is a live writer on these
        # same files, and the plugin's own hook fires from it. Without a lock,
        # a `memory` append landing between our snapshot and our os.replace
        # is silently discarded — and the index-staleness check cannot see
        # it, because an append at the end leaves every index intact.
        # Whichever writer gets here first wins; the other waits briefly.
        ctx = locking.store_lock(memory_store.path_for(memory_store.TARGET_USER))
        acquired = ctx.__enter__()
        if not acquired:
            self.errors.append(
                "could not acquire the memory-store lock within the timeout; "
                "another writer is active. Proceeding — verify the result."
            )
        try:
            return self._execute_locked(
                plan, run_id, provenance, before, acquired
            )
        finally:
            ctx.__exit__(None, None, None)

    def _execute_locked(
        self,
        actions: List[Dict[str, Any]],
        run_id: str,
        provenance: str,
        before: Dict[str, Any],
        lock_acquired: bool,
    ) -> Dict[str, Any]:
        # CRITICAL: read the snapshot with the STRICT reader. A permissive
        # read that returns [] for an unreadable-but-present store used to
        # rebuild that store from an empty snapshot, overwriting the user's
        # real profile with a single entry. Abort the whole plan instead.
        try:
            original: Dict[str, List[str]] = {
                t: memory_store.read_entries_strict(t)
                for t in (memory_store.TARGET_MEMORY, memory_store.TARGET_USER)
            }
        except memory_store.StoreUnreadable as exc:
            # Nothing is written. The plan is not partially applied because
            # it is not applied at all.
            return {
                "applied": [],
                "pending": [],
                "errors": [
                    f"ABORTED — store snapshot unreadable, nothing written: {exc}"
                ],
                "blocked": [],
                "before": before,
                "after": before,
                "lock_acquired": lock_acquired,
            }
        removals: Dict[str, set] = {t: set() for t in original}    # idx -> drop
        # (target, idx) -> expected source text, taken from the plan when the
        # plan states it. At rebuild time a removal is honoured only if the
        # entry at that index is still the one the plan named.
        expected: Dict[Tuple[str, int], str] = {}
        appends: Dict[str, List[str]] = {t: [] for t in original}  # new entries

        # IDEMPOTENCY: refuse to apply the same run twice. Indices are
        # positional, so re-applying a plan after it already landed would
        # resolve them against a store that has since shifted and remove
        # unrelated entries. This is the guard that actually makes retries
        # safe — comparing the plan's own text against the current store
        # cannot distinguish "shifted" from "legitimately rewritten".
        already = _already_applied(self.cfg, run_id)
        if already:
            return {
                "applied": [],
                "pending": [],
                "errors": [
                    f"ABORTED — run {run_id!r} was already applied at "
                    f"{already}; positional indices are not safe to replay. "
                    f"Nothing written."
                ],
                "blocked": [],
                "before": before,
                "after": before,
                "lock_acquired": lock_acquired,
            }

        for n, action in enumerate(actions):
            try:
                self._apply(action, original, removals, appends, expected)
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
            # Honour a removal only if the entry at that index is still the
            # one the plan named. A stale plan (retried after a partial
            # apply, or shifted by a concurrent memory-tool write) would
            # otherwise delete an unrelated entry.
            for i in list(removals[target]):
                want = expected.get((target, i))
                if want is None:
                    continue
                if not (0 <= i < len(original[target])):
                    removals[target].discard(i)
                    self.errors.append(
                        f"removal {target}#{i} out of range — dropped from plan"
                    )
                elif original[target][i] != want:
                    # The plan named the text it expected at this index and
                    # something else is there now (a concurrent memory-tool
                    # write, or an earlier run of this same plan). Refuse
                    # rather than delete an unrelated entry.
                    removals[target].discard(i)
                    self.errors.append(
                        f"removal {target}#{i} is stale — that index now holds a "
                        f"different entry; refusing to delete it"
                    )
            if target == memory_store.TARGET_USER:
                for i in list(removals[target]):
                    # Bounds-check: the staleness pass above may have left an
                    # index that is no longer addressable, and this loop used
                    # to index blindly and crash the whole rebuild.
                    if not (0 <= i < len(original[target])):
                        removals[target].discard(i)
                        self.errors.append(
                            f"removal {target}#{i} out of range — dropped from plan"
                        )
                        continue
                    text = original[target][i]
                    low = text.lower()
                    if (target, i) in self._split_removals:
                        # Already satisfied by a split: a verbatim clause of
                        # this entry is being kept in its place.
                        continue
                    if any(k in low for k in IDENTITY_MARKERS):
                        # A guard refusal on an OVER-BUDGET target is the
                        # condition the whole plugin exists to relieve. The
                        # old code refused silently, so the run looked like a
                        # clean all-keep while the store stayed pinned at its
                        # wall for days. Record it as a named blocked state so
                        # it surfaces in the report and to the user.
                        self.blocked.append({
                            "target": target,
                            "index": i,
                            "chars": len(text),
                            "entries": len(original[target]),
                            "over_budget": (
                                memory_store.char_count(original[target])
                                >= limit
                            ),
                            "hint": (
                                "manual split required: this entry mixes "
                                "identity with routable doctrine; use a "
                                "'split' action (keep=<core>, routes=[...])"
                            ),
                        })
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
            # state falls below the target's floor. The comparison is
            # strictly-less: landing exactly ON the floor is allowed, and a
            # float comparison that rejected 10.0% vs 10% would refuse the
            # very split that relieves a store pinned at its wall.
            if removals[target] and limit and post_fraction < floor - 1e-9:
                self.errors.append(
                    f"target '{target}' would drop to "
                    f"{post_chars}/{limit} chars ({post_fraction*100:.0f}%) "
                    f"< {floor*100:.0f}% floor — refused; source entries kept"
                )
                removals[target] = set()  # keep everything in place
                # A split/consolidate replacement only makes sense as the
                # SUBSTITUTE for entries we just decided not to remove. With
                # the source restored, appending the replacement would leave
                # the store holding both copies — a refusal that made the
                # store BIGGER. Withdraw any replacement that was derived
                # from a now-revoked removal.
                dropped = self._withdraw_replacement(
                    target, appends[target], original[target]
                )
                if dropped:
                    self.errors.append(
                        f"withdrew {dropped} replacement entr"
                        f"{'y' if dropped == 1 else 'ies'} for '{target}': "
                        f"their source entries were kept, so the merged text "
                        f"would have duplicated live content"
                    )
                kept = list(original[target])
                final = kept + appends[target]
            # The "never empty memory" rule was dead: floor==0.0 made
            # `post_fraction < floor` unsatisfiable. Special-case it.
            if target == memory_store.TARGET_MEMORY and removals[target] and not final:
                self.errors.append(
                    "target 'memory' would be emptied entirely — refused; "
                    "source entries kept"
                )
                removals[target] = set()
                dropped = self._withdraw_replacement(
                    target, appends[target], original[target]
                )
                if dropped:
                    self.errors.append(
                        f"withdrew {dropped} replacement entr"
                        f"{'y' if dropped == 1 else 'ies'} for 'memory': their "
                        f"source entries were kept, so the merged text would "
                        f"have duplicated live content"
                    )
                final = list(original[target])
            # Do not push a target PAST its own limit — once over, every
            # built-in `append` is refused for the rest of the session.
            if limit and memory_store.char_count(final) > limit and appends[target]:
                self.errors.append(
                    f"target '{target}' would exceed its {limit:,}-char limit "
                    f"({memory_store.char_count(final):,} with appends) — "
                    f"appends dropped, removals kept"
                )
                # Dropping a split/consolidate append is NEVER a neutral trim.
                # That append is the surviving content: dropping it deletes the
                # source and then discards the text meant to replace it. Two
                # distinct cases, both losses:
                #
                #  (a) a pure replacement (its source survives the drop) --
                #      restore the source instead of deleting it;
                #  (b) a replacement carrying a RETAINED clause that exists
                #      nowhere else -- that text is simply gone if dropped.
                #
                # Observed (a) destroying an identity-guarded USER entry on a
                # store that was merely over budget. A limit guard must never
                # trade a source for a replacement it then throws away, so
                # when any removal depends on an append, revoke the removal
                # and keep the store as it is.
                if self._appends_back_removals(target, removals[target]):
                    self.errors.append(
                        f"target '{target}' is over its {limit:,}-char limit; "
                        f"dropping the replacement text would have deleted "
                        f"the source without preserving it, so the removals "
                        f"are revoked and the store is left as-is"
                    )
                    removals[target] = set()
                    # The appends are KEPT here, not dropped. With the
                    # removals revoked the store is already over its limit,
                    # but these appends may carry a clause that exists
                    # nowhere else (a split clause whose route failed), and
                    # dropping them would lose it outright. The store being
                    # over budget is the status quo we were handed; silently
                    # deleting unique content to tidy it is not a trade the
                    # plugin gets to make silently.
                    final = list(original[target]) + appends[target]
                else:
                    appends[target] = []
                    final = [
                        e for i, e in enumerate(original[target])
                        if i not in removals[target]
                    ]
            if final != original[target]:
                # PRE-WRITE SNAPSHOT. Quarantine covers entries this plan
                # removed; it does NOT cover the file this write replaces. The
                # guards above have all passed, so this is the last moment
                # before the live store is overwritten — and the content is
                # final. A live incident on 2026-09-27 was a correct atomic
                # write of WRONG content, which quarantine could not help
                # with; this makes that a one-command restore.
                snap = snapshots.take(
                    self.cfg.data_dir, target, self._run_id,
                    keep=self.cfg.retain_snapshots,
                )
                if snap and snap.get("ok"):
                    self._snapshot_records.append(snap)
                elif snap and snap.get("existed"):
                    # Not fatal: the write is still atomic and reversible by
                    # quarantine. But it must be visible, because it means a
                    # botched write would be unrecoverable.
                    self.errors.append(
                        f"pre-write snapshot FAILED for {target} "
                        f"({snap.get('reason')}); this write is not "
                        f"byte-recoverable if it lands wrong"
                    )
                try:
                    memory_store.write_entries(target, final)
                except OSError as exc:
                    # One target failing must not abort the other or lose the
                    # record of what already committed.
                    self.errors.append(
                        f"writing target '{target}' failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue
                # Quarantine is flushed only for removals that SURVIVED every
                # guard, so a refused eviction never leaves a phantom record
                # that `restore` would later duplicate.
                for i in sorted(removals[target]):
                    if 0 <= i < len(original[target]):
                        self._pending_quarantine.append(
                            (target, original[target][i])
                        )
                # A split REPLACES the source with a shorter clause, so the
                # sum of removed lengths is not the saving — it ignores the
                # append. Report the real net change in stored characters.
                freed = memory_store.char_count(original[target]) - memory_store.char_count(final)
                if freed:
                    self.applied.append(
                        f"freed {freed} chars [{target}] "
                        f"({memory_store.char_count(original[target])}"
                        f" -> {memory_store.char_count(final)})"
                    )
                # This target's removal notes are now true: the store was
                # rewritten. Notes for OTHER targets stay provisional until
                # those targets are handled, so a guard that revokes one
                # target's removal never launders another target's success.
                self.applied.extend(self._provisional.pop(target, []))

        # Removal notes still provisional belong to targets whose store was
        # never rewritten (a guard revoked the removal, or final ==
        # original). Report them as not-committed, never as a success.
        for _t, msgs in self._provisional.items():
            for m in msgs:
                self.errors.append(f"planned but not committed ({_t}): {m}")
        self._provisional = {}

        # Quarantine is flushed ONLY for removals that survived every guard and
        # were actually written, so `restore` can never re-append an entry that
        # is still live.
        for q_target, q_text in self._pending_quarantine:
            try:
                quarantine.evict(
                    self.cfg, target=q_target, text=q_text,
                    reason=f"removed by run {self._run_id}", run_id=self._run_id,
                )
            except OSError as exc:
                self.errors.append(
                    f"quarantine write failed for {q_target}: {exc}"
                )
        self._pending_quarantine = []

        # Record the run as landed so a retry is refused rather than replayed
        # against a store whose indices have since shifted.
        try:
            _mark_applied(self.cfg, self._run_id)
        except OSError as exc:
            self.errors.append(f"could not record run id for replay safety: {exc}")

        after = _usage_snapshot()
        return {
            "applied": self.applied,
            "pending": self.pending,
            "errors": self.errors,
            "blocked": self.blocked,
            "before": before,
            "after": after,
            "lock_acquired": lock_acquired,
            "snapshots": list(self._snapshot_records),
        }

    # -- per-action dispatch ---------------------------------------------

    def _apply(
        self, a: Dict[str, Any], original, removals, appends, expected
    ) -> None:
        kind = a["action"]
        target = a.get("target", "memory")
        # An unknown target creates a removal key the rebuild loop never visits,
        # so the side effect commits while the source entry silently stays —
        # a permanent duplicate that the ledger then says is already handled.
        if target not in (memory_store.TARGET_MEMORY, memory_store.TARGET_USER):
            raise ValueError(
                f"unknown target {target!r} "
                f"(expected {memory_store.TARGET_MEMORY!r} or {memory_store.TARGET_USER!r})"
            )
        if kind == "keep":
            return
        if kind == "consolidate":
            self._do_consolidate(a, original, removals, appends, expected)
            return
        if kind == "route-to-skill":
            self._do_skill(a, original)
            self._remove_source(removals, a, original, expected)
            return
        if kind == "route-to-profile":
            self._do_profile(a, appends)
            self._remove_source(removals, a, original, expected)
            return
        if kind == "route-to-provider":
            ok = self._do_provider(a, original)
            if ok:
                self._remove_source(removals, a, original, expected)
            else:
                self._note_pending("route-to-provider: kept source entry (gateway write failed)")
            return
        if kind == "route-to-script":
            self._do_script(a, original)
            self._remove_source(removals, a, original, expected)
            return
        if kind == "evict-to-quarantine":
            self._do_evict(a, original, removals, expected)
            return
        if kind == "split":
            self._do_split(a, original, removals, appends, expected)
            return
        raise ValueError(f"unhandled action {kind!r}")

    @staticmethod
    def _remove_source(removals, a, original, expected) -> None:
        """Queue a removal, keyed by the text the plan expected to find there.

        Recording the expected text is what makes a stale or retried plan
        safe: at rebuild time the index is only honoured if it still holds
        that exact entry, so re-applying a plan can never delete an unrelated
        entry that shifted into the slot.
        """
        idx = a.get("index")
        if idx is None:
            return
        # Reject bools/floats: ``True`` would silently become index 1.
        if isinstance(idx, bool) or not isinstance(idx, int):
            raise ValueError(f"index must be an int, got {idx!r}")
        target = a.get("target", "memory")
        removals.setdefault(target, set()).add(idx)
        # Prefer the text the PLAN states it expected at this index — that is
        # the independent record the staleness check compares against. But a
        # ROUTING action's ``text`` is the payload destined for the new
        # location (a paraphrase, possibly a truncated inventory copy), not an
        # assertion about the source. Only a destructive action
        # (evict-to-quarantine) makes such an assertion, so only that one
        # gets the staleness check; every other action falls back to the live
        # value, which keeps the removal an exact no-op against a real entry.
        destructive = a.get("action") == "evict-to-quarantine"
        stated = a.get("text") if destructive else None
        entries = original.get(target) or []
        if not isinstance(stated, str) or not stated:
            # Out of range is not a crash: report it and do not queue a
            # removal that the rebuild loop cannot resolve.
            if not (0 <= idx < len(entries)):
                raise IndexError(
                    f"source index {idx} out of range for target {target!r} "
                    f"({len(entries)} entries)"
                )
            stated = entries[idx]
        if stated is not None:
            expected[(target, idx)] = stated

    # -- action implementations ------------------------------------------

    @staticmethod
    def _source_text(a: Dict[str, Any], original: Dict[str, List[str]]) -> str:
        """The REAL entry text for a routing action.

        The inventory caps entry text for the model payload (``inventory.py``),
        so ``a['text']`` is a truncated paraphrase. Persisting that and then
        deleting the source is silent knowledge loss, so when the action names
        a readable source index we route the true full text instead.
        """
        idx = a.get("index")
        target = a.get("target", "memory")
        if isinstance(idx, int) and not isinstance(idx, bool):
            entries = original.get(target) or []
            if 0 <= idx < len(entries):
                return entries[idx]
        return a.get("text") or a.get("body") or ""

    def _do_consolidate(self, a, original, removals, appends, expected) -> None:
        target = a.get("target", "memory")
        idxs = [int(i) for i in (a.get("entries") or [])]
        merged = (a.get("text") or "").strip()
        if len(set(idxs)) < 2 or not merged:
            raise ValueError(
                "consolidate requires >=2 DISTINCT entries[] and text "
                f"(got entries={idxs})"
            )
        entries = original.get(target) or []
        # Validate every index before mutating anything, so a hallucinated
        # index cannot append merged text while removing nothing.
        for i in idxs:
            if not (0 <= i < len(entries)):
                raise ValueError(
                    f"consolidate index {i} out of range "
                    f"(target '{target}' has {len(entries)} entries); nothing merged"
                )
        # Refuse to merge on a TRUNCATED base: the action's text may be the
        # inventory's 160-char cap, and writing that back as the merged entry
        # would silently discard the rest with no quarantine record.
        capped = any(
            (e.get("chars") or 0) > len(e.get("text") or "")
            for e in (a.get("_source_entries") or [])
        )
        if capped and len(merged) < max(
            (e.get("chars") or 0) for e in a["_source_entries"]
        ):
            raise ValueError(
                "consolidate text is a truncated inventory copy; refusing to "
                "merge (would silently discard the rest of the entries)"
            )
        removals[target].update(idxs)
        for i in idxs:
            expected[(target, i)] = entries[i]
        # This append SUBSTITUTES for the entries just removed. If a later
        # guard revokes the removal, the merged text must be withdrawn too.
        self._add_append(target, appends, merged, sources=frozenset(idxs))
        self._note_removal(
            target,
            f"consolidated {len(idxs)} entries into {len(merged)} chars [{target}]",
        )
        self._ledger("consolidate", f"{target}#consolidated", merged[:60])

    def _find_existing_skill(self, name: str) -> Optional[Path]:
        """Locate an installed SKILL.md by directory name, any category.

        Category is irrelevant to the collision question: a skill named
        ``oem-ui-design-system`` living under ``creative/`` is still that skill,
        and writing ``tools/oem-ui-design-system/`` beside it forks the user's
        doctrine into an unregistered duplicate. Returns None when absent.
        """
        safe = _safe(name)
        if not safe:
            return None
        root = self.cfg.skills_root
        if not root.exists():
            return None
        # Direct child first, then any category, then any depth. A skill that
        # lives at any depth is still the same skill by name.
        direct = root / safe / "SKILL.md"
        if direct.exists():
            return direct
        for depth in ("*", "*/*"):
            for match in root.glob(f"{depth}/{safe}/SKILL.md"):
                if match.exists():
                    return match
        hits = list(root.rglob(f"{safe}/SKILL.md"))
        return hits[0] if hits else None

    def _extend_skill(self, path: Path, body: str, a: Dict[str, Any]) -> None:
        """Append routed content to an EXISTING skill instead of duplicating it.

        Frontmatter is preserved verbatim (parsing and re-emitting YAML would
        drop keys the plugin does not model, and ``metadata.hermes`` carries the
        tags that make a skill discoverable). A pre-write snapshot is taken so
        an append is as recoverable as a create. Idempotent: content already
        present is not appended twice.
        """
        existing = path.read_text(encoding="utf-8", errors="replace")
        marker = body.strip()
        if marker and marker in existing:
            self._note(f"skill '{path.parent.name}' already contains this content; nothing to append")
            return
        snap = None
        try:
            # A plain file copy, NOT snapshots.take(): that resolves its source
            # through store.path_for(target), which only knows the two memory
            # stores. A skill is an arbitrary path, so copy it directly.
            snap_dir = snapshots.snapshots_dir(self.cfg.data_dir) / "skills"
            snap_dir.mkdir(parents=True, exist_ok=True)
            dest = snap_dir / f"{path.parent.name}__{self._run_id}__{path.name}"
            tmp = dest.with_name(dest.name + f".tmp{os.getpid()}")
            shutil.copy2(path, tmp)
            os.replace(tmp, dest)
            snap = dest
        except (OSError, ValueError) as exc:  # noqa: BLE001
            # A failed snapshot does not block the write: the write is still
            # atomic, and refusing here would turn a full disk into a total
            # routing outage. Report it so the append is not silently
            # unrecoverable.
            self._note(f"pre-write snapshot FAILED for {path}: {exc}")
        addition = f"\n\n## Routed by memtriage ({self._provenance})\n\n{marker}\n"
        _write_atomic(path, existing.rstrip("\n") + addition)
        self._note(f"appended to existing skill '{path.parent.name}' ({path})")
        if snap:
            self._note(f"  pre-write snapshot: {snap}")
        self._ledger("skill-append", str(path), body[:200])

    def _do_skill(self, a, original) -> None:
        name = a.get("skill_name") or a.get("name") or "routed-skill"
        body = self._source_text(a, original).strip()
        if not body:
            raise ValueError("route-to-skill requires text")
        category = a.get("category") or "tools"
        # DANGER 2026-09-27: Cerveau cannot see most installed skill names (the
        # prompt truncated the skills list alphabetically), so it invented names
        # that collided with real curated skills. When the named skill already
        # exists ANYWHERE under skills_root — not just in the default category —
        # extend it instead of writing a duplicate. This is the enforcement
        # point; the prompt rule is only advice.
        existing = self._find_existing_skill(name)
        if existing is not None:
            return self._extend_skill(existing, body, a)
        target = self.cfg.skills_root / _safe(category) / _safe(name) / "SKILL.md"
        # Never silently clobber a hand-written skill; a collision must be an
        # explicit, reported refusal rather than an overwrite.
        if target.exists() and not ledger.already_routed(self.cfg, str(target)):
            raise ValueError(
                f"skill '{target}' already exists and was not written by "
                f"triage; refusing to overwrite — pick a different skill_name"
            )
        # YAML-quote the description: an unquoted body-derived line containing
        # ": " or starting with "#" produced invalid frontmatter, which made
        # the skill invisible to inventory_skills and unreachable forever.
        description = _yaml_quote((body.splitlines() or [""])[0][:80])
        content = SKILL_FRONTMATTER.format(
            name=_safe(name), description=description,
            provenance=_yaml_scalar(self._provenance),
        ) + body + "\n"
        _write_atomic(target, content)
        self._note(f"routed to skill '{name}' ({target})")
        self._ledger("skill", str(target), body[:200])

    def _do_profile(self, a, appends) -> None:
        text = (a.get("text") or "").strip()
        if not text:
            raise ValueError("route-to-profile requires text")
        # Routing INTO the profile can only make an over-budget store worse.
        # Demote to provider rather than deepen the problem.
        if memory_store.char_count(
            memory_store.read_entries_strict(memory_store.TARGET_USER)
        ) >= memory_store.char_limit(memory_store.TARGET_USER):
            self.errors.append(
                "route-to-profile skipped: target 'user' is already at/over its "
                "char limit; routing into it would worsen the pressure"
            )
            raise ValueError("route-to-profile refused: user store at/over limit")
        # Replaces nothing — this is new text, so it must never be withdrawn.
        self._add_append("user", appends, text, sources=None)
        self._note(f"routed to profile ({len(text)} chars)")
        self._ledger("user", "USER.md", text[:200])

    def _do_provider(self, a, original=None) -> bool:
        """Best-effort provider write. Returns True only if the gateway write
        succeeded. On failure the caller must KEEP the source entry in the
        working store (no silent data loss) — it is only recoverable from the
        plan file otherwise."""
        text = (
            self._source_text(a, original) if original is not None
            else (a.get("text") or "")
        ).strip()
        if not text:
            raise ValueError("route-to-provider requires text")
        notice = _dispatch_to_provider(self.cfg, text)
        if notice.startswith("pending"):
            self._note_pending(f"route-to-provider: {notice}")
            return False
        self._note(f"routed to provider (gateway {notice})")
        # Record the FULL routed text, not a 200-char summary: the source
        # entry is being removed, so the ledger is the only remaining record
        # of what was written. A truncated summary here is silent loss.
        self._ledger("provider", "provider/scene", text)
        return True

    def _do_script(self, a, original) -> None:
        script_name = a.get("script_name") or "routed-script"
        ext = (a.get("script_ext") or "py").lstrip(".").lower()
        body = self._source_text(a, original).strip()
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

    def _do_split(self, a, original, removals, appends, expected) -> None:
        """Split one oversized entry: keep the identity core, route the rest.

        This is the action that makes the plugin able to RELIEVE a profile
        whose single entry is 2,988 of 2,996 chars and matches every identity
        marker. Every other action is whole-or-nothing, so the identity guard
        correctly refused the entire blob — including the clauses (viewing
        rules, deployment doctrine, repo conventions) that genuinely belong in
        a skill and are already mirrored there.

        Contract:
        * ``keep``  — the clause(s) that must STAY in the working store.
        * ``routes``— a list of routing actions, each a normal action dict
          WITHOUT a source index (the source is this entry).
        * The original entry is removed and replaced by the kept clause(s).

        A route that fails leaves its clause in the store rather than losing
        it: split is deliberately partial-relief-or-nothing per route.
        """
        target = a.get("target", "user")
        index = a.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError(f"split needs an int index, got {index!r}")
        entries = original.get(target) or []
        if not (0 <= index < len(entries)):
            raise IndexError(
                f"source index {index} out of range for target {target!r} "
                f"({len(entries)} entries)"
            )
        source_text = entries[index]
        keep_text = (a.get("keep") or "").strip()
        if not keep_text:
            raise ValueError("split requires 'keep'")

        # Fidelity check: the kept clause must actually come from the source.
        # Otherwise a "split" could silently replace a real entry with
        # unrelated text, which is data loss wearing a helpful hat.
        probe = re.sub(r"\s+", " ", keep_text)[:60].strip().lower()
        if probe and probe not in re.sub(r"\s+", " ", source_text).lower():
            raise ValueError(
                "split 'keep' does not appear in the source entry; refusing to "
                "replace a real entry with unrelated text"
            )

        routed = []
        retained: List[str] = []  # clauses whose route failed — must not be lost
        for route in (a.get("routes") or []):
            if not isinstance(route, dict):
                raise ValueError(f"split routes must be objects, got {route!r}")
            r = dict(route)
            r.setdefault("index", index)
            try:
                if r.get("action") == "route-to-skill":
                    self._do_skill(r, original)
                elif r.get("action") == "route-to-provider":
                    # _do_provider REPORTS failure by returning False (it does
                    # not raise), so the return value must be checked. Treating
                    # a False as success routed the clause away from the store
                    # while it was never written anywhere -- silent loss.
                    if not self._do_provider(r, original):
                        raise ValueError(
                            "route-to-provider did not confirm the write; "
                            "its clause stays in the store"
                        )
                elif r.get("action") == "route-to-script":
                    self._do_script(r, original)
                else:
                    raise ValueError(
                        f"split route must be a routing action, got "
                        f"{r.get('action')!r}"
                    )
                routed.append(r.get("action"))
            except Exception as exc:  # noqa: BLE001
                # The clause STAYS in the store. Dropping it here would make
                # split lossy on any transient route failure — the entry is
                # being removed, so anything not routed away is gone unless it
                # is explicitly re-appended below.
                clause = (r.get("text") or r.get("body") or "").strip()
                if clause:
                    retained.append(clause)
                self.errors.append(
                    f"split route {r.get('action')!r} failed ({exc}); its "
                    f"clause stays in the store"
                )

        removals[target].add(index)
        self._split_removals.add((target, index))
        # The replacement is the kept clause plus any clause whose route
        # failed, never the model's paraphrase of either.
        replacement = keep_text
        if retained:
            replacement = keep_text + "\n" + "\n".join(retained)
        # Substitutes for the source entry ONLY when every clause routed away
        # successfully. If a route failed, `retained` carries content that
        # exists nowhere else and must survive any guard revocation.
        sources = frozenset({index}) if not retained else None
        self._add_append(target, appends, replacement, sources=sources)
        # Only claim an expectation when the plan stated the source text; a
        # self-assigned value would make the staleness check a no-op.
        stated = a.get("text")
        if isinstance(stated, str) and stated:
            expected[(target, index)] = stated
        self._note_removal(
            target,
            f"split {target}#{index} ({len(source_text)} chars) → kept "
            f"{len(keep_text)} chars, routed {len(routed)} clause(s)",
        )
        self._ledger(
            "split", f"{target}#split#{self._run_id}", keep_text[:80]
        )

    def _appends_back_removals(self, target: str, removals) -> bool:
        """True when dropping this target's appends would lose content.

        Two ways that happens: a recorded replacement whose source entry is
        being removed, or a replacement carrying a clause that was re-appended
        after its route failed (sources=None) -- that clause exists nowhere
        else, so dropping the append deletes it outright.
        """
        if not removals or not self._append_sources.get(target):
            return False
        for src in self._append_sources[target]:
            if not src:
                # sources=None: carries unique retained content.
                return True
            if any(i in removals for i in src):
                return True
        return False

    def _add_append(
        self,
        target: str,
        appends: Dict[str, List[str]],
        text: str,
        sources: Optional[frozenset] = None,
    ) -> None:
        """Append text, recording WHICH source entries it replaces.

        Provenance is tracked explicitly rather than inferred later from text
        similarity. A consolidation paraphrases its sources, so a content
        heuristic cannot tell a replacement from genuinely new text — and
        getting that wrong either duplicates live content or silently drops a
        clause. ``sources`` is None for an append that replaces nothing (a
        plain route-to-profile, or a split clause whose route failed).
        """
        appends[target].append(text)
        self._append_sources.setdefault(target, []).append(sources)

    def _withdraw_replacement(
        self, target: str, appends: List[str], original: List[str]
    ) -> int:
        """Remove appends that were substitutes for now-revoked removals.

        ``consolidate`` and ``split`` both work by removing their source
        entries and appending a replacement. If a later guard revokes the
        removal, the replacement is orphaned: the source text is still in the
        store AND the merged copy would be appended next to it. A guard that
        refused an action must never leave the store larger than before.

        Selection is by recorded provenance (:meth:`_add_append`), never by
        text matching. An append with no sources carries content that exists
        nowhere else and is always kept.
        """
        sources = self._append_sources.get(target) or []
        if not sources:
            return 0
        # Drop any append whose source entries are ALL still live: the
        # replacement is then a duplicate of content the store kept.
        keep_idx = []
        dropped = 0
        for i, src in enumerate(sources[: len(appends)]):
            if src and all(j < len(original) for j in src):
                dropped += 1
                continue
            keep_idx.append(i)
        if not dropped:
            return 0
        appends[:] = [appends[i] for i in keep_idx]
        self._append_sources[target] = [sources[i] for i in keep_idx]
        # The removal notes for this target described a change that is no
        # longer happening.
        self._provisional.pop(target, None)
        return dropped

    def _do_evict(self, a, original, removals, expected) -> None:
        target = a.get("target", "memory")
        index = a.get("index")
        if index is None:
            raise ValueError("evict-to-quarantine requires an index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError(f"index must be an int, got {index!r}")
        idx = index
        entries = original.get(target) or []
        if idx < 0 or idx >= len(entries):
            raise IndexError(f"source index {idx} out of range for target {target!r}")
        victim_text = entries[idx]
        # Do NOT write to quarantine here: the identity guard and the safety
        # floor run later and may revoke this removal. The record is buffered
        # and flushed by execute_plan only for removals that survive.
        removals[target].add(idx)
        # Only claim an expectation when the PLAN stated one. Overwriting with
        # the live value here would make the staleness check in the rebuild
        # loop compare a value against itself, and it could never fire.
        stated = a.get("text")
        if isinstance(stated, str) and stated:
            expected[(target, idx)] = stated
        self._note_removal(target, f"quarantined {len(victim_text)} chars [{target}]")