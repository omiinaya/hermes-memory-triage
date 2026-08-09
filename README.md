# hermes-memory-triage

A Hermes plugin that keeps the built-in memory store healthy. When the memory
store approaches a configured utilization threshold, it analyzes what is
stored, routes each piece of knowledge to its proper home (skills, user
profile, memory provider, scripts) and quarantines stale entries — so space is
freed without losing knowledge.

It is deliberately **decoupled from the context compressor**: it watches the
*memory store*, not the conversation window. Context compression stays
untouched.

## How it triggers (two layers, no cron)

1. **`post_tool_call` hook** — after every successful `memory` tool write the
   plugin checks store utilization (`MEMORY.md` / `USER.md` char budgets:
   2200 / 1375). The instant a write pushes usage to/through the configured
   threshold, triage fires. It observes the built-in tool; it never replaces
   it.
2. **`on_session_start` hook** — backstop for sessions that boot with the
   store already over threshold (also purges expired quarantine entries).

Both hooks are cooldown-aware (`cooldown_minutes`, default 60) so triage does
not re-run on every write after a pass.

## What triage does

Pipeline: inventory → Cerveau (decision profile) → plan → apply.

**Cerveau** is a dedicated Hermes profile (`hermes -p cerveau`) whose only job
is routing decisions. It receives the full inventory (memory entries, skills,
scripts) + the routing ledger and returns a JSON plan. See
`docs/cerveau-profile.md` for setup.

**Action taxonomy** (what Cerveau chooses between):

| Action | Meaning |
|---|---|
| `keep` | entry is durable, current, correctly placed |
| `consolidate` | merge 2+ overlapping entries into one tighter entry |
| `route-to-skill` | reusable procedure → new SKILL.md (provenance-stamped) |
| `route-to-profile` | identity/preference fact → USER.md |
| `route-to-provider` | rich scene knowledge → memory provider gateway |
| `route-to-script` | recurring mechanical task → runnable script (+ optional cron) |
| `evict-to-quarantine` | stale/superseded entry → reversible quarantine |

**Staleness signals** (Cerveau's rubric): superseded by a newer fact,
contradiction (the newer entry always survives), zero provider heat + no
recent session references, or an explicit "forget this". Identity/security/
environment facts are never evicted — at worst demoted to the provider.

**Safety model**:
- `mode: manual` (default) — triage produces a report; you review and approve
  (`/memtriage review`, `/memtriage approve`), or edit the saved plan JSON
  first.
- `mode: auto` — plans apply immediately, silently.
- Eviction is always reversible: entries go to a quarantine file, restorable
  within `quarantine_days` (default 7), and only an explicit
  `/memtriage purge` hard-deletes expired entries.
- The routing ledger records every artifact with provenance, so Cerveau never
  re-routes what already exists elsewhere.

## Configuration

Config lives at `~/.memtriage/config.json` (override with `MEMTRIAGE_HOME`).
Created on first use with defaults:

```json
{
  "threshold_percent": 0.75,
  "mode": "manual",
  "quarantine_days": 7,
  "cooldown_minutes": 60,
  "cerveau_profile": "cerveau",
  "cerveau_bin": "hermes",
  "scripts_dir": "~/.hermes/scripts",
  "provider_base_url": "http://127.0.0.1:8420",
  "data_dir": "/home/<user>/.memtriage"
}
```

## Usage

| Command | Purpose |
|---|---|
| `/memtriage status` | threshold, mode, per-target usage, awaiting approval |
| `/memtriage run [--force]` | run a triage pass now |
| `/memtriage review` | show the plan awaiting approval |
| `/memtriage approve` | apply the awaiting plan |
| `/memtriage restore <text>` | restore an evicted entry from quarantine |
| `/memtriage purge` | hard-delete quarantine entries past the grace window |
| `/memtriage quarantine` | list evicted entries |
| `/memtriage ledger` | show the routing provenance ledger |
| `/memtriage config` | show effective configuration |

The `mem_triage` agent tool exposes the same actions (action: status | run |
review | approve | restore | purge | quarantine | ledger | config) so agents
can drive triage too.

## Installation

```bash
hermes plugins install <owner>/hermes-memory-triage --enable
hermes plugins update hermes-memory-triage   # after iterating
```

Takes effect on a NEW session. Data directory (`~/.memtriage/`) is created on
first run; the plugin is fully functional in manual mode before the Cerveau
profile exists (triage will report the dispatch failure until it is wired).

## Repository layout

```
plugin/plugin.yaml      manifest (name: hermes-memory-triage)
plugin/__init__.py      hooks + slash command + agent tool registration
memtriage/              stdlib-only core (no third-party deps)
  config.py             config + data-dir resolution
  store.py              memory file format + locking (mirrors the built-in)
  inventory.py          memory/skills/scripts inventory
  ledger.py             routing provenance + dedup
  quarantine.py         reversible eviction store
  plan.py               action contract, validation, reports
  cerveau.py            decision-profile dispatch (one-shot chat -q)
  executor.py           plan application to real destinations
  triage.py             orchestrator (trigger -> plan -> apply)
  commands.py           human-readable subcommand layer
tests/                  pytest suite
```

## Development

```bash
# Run the suite (uses the Hermes venv python on this host)
/usr/local/lib/hermes-agent/venv/bin/python -m pytest tests/ -q
```

Core is stdlib-only and cross-platform (Windows/macOS/Linux): paths via
`pathlib`, locks via `fcntl`/`msvcrt` with no-op fallback, atomic writes via
`os.replace`.

## Verification scope

- Suite green on self-hosted Linux / Python 3.13 (55 tests, ~0.2s).
- The register() smoke test uses a mock ctx (no session required).
- Cerveau dispatch is exercised via unit tests against `parse_plan`/`build_prompt`;
  live end-to-end runs require a wired `cerveau` profile (see docs).
- Windows/macOS paths are exercised by construction (pathlib, no shell), not by
  CI on this host yet.
