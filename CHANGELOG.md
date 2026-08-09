# Changelog

All notable changes to this project are documented here. Format based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed
- Routing actions (route-to-skill/profile/provider/script) now actually free
  the working store: after copying knowledge to its destination, the source
  entry is dropped from memory. Previously they only copied (ledger + write)
  and the store never shrank — so triage had no measurable impact.
- Multi-removal runs (several evicts/consolidations/routes in one plan) no
  longer error with "source index out of range": all removals are resolved
  against the original inventory indices and each target is rebuilt once, so
  an earlier drop can no longer shift a later index.

### Added
- Execution summaries now report impact in percentage points: before/after
  usage snapshots are recorded per run and rendered as e.g.
  `memory: 98% -> 84% (+14pp freed)` in the `/memtriage` output and the
  in-session post-execution notice.
- Initial implementation of the hermes-memory-triage plugin:
  - Two-layer auto-trigger via plugin hooks (post_tool_call + on_session_start),
    decoupled from the context compressor.
  - Cerveau decision profile dispatch (one-shot `hermes -p cerveau chat -q`).
  - Routing taxonomy: keep, consolidate, route-to-skill, route-to-profile,
    route-to-provider, route-to-script, evict-to-quarantine. Every
    route-to-script is paired with a companion route-to-skill by rule.
  - Reversible quarantine with a grace window; explicit purge only.
  - Routing ledger with provenance; dedup against already-routed artifacts.
  - manual (report + approve) and auto (apply silently) modes.
  - `/memtriage` slash command and `mem_triage` agent tool.
  - Robust Cerveau plan extraction: fences, prose-before-JSON, stray control
    characters, empty arrays, last-array-wins, strict contract validation.
  - In-session notification: triage reports are injected into the active
    conversation (once per run) instead of only written to disk.
  - Tests: 64-test suite, stdlib-only core, cross-platform paths/locks.