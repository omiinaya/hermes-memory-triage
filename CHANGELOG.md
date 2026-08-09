# Changelog

All notable changes to this project are documented here. Format based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Backup-initial implementation of the hermes-memory-triage plugin:
  - Two-layer auto-trigger via plugin hooks (post_tool_call + on_session_start),
    decoupled from the context compressor.
  - Cerveau decision profile dispatch (one-shot `hermes -p cerveau chat -q`).
  - Routing taxonomy: keep, consolidate, route-to-skill, route-to-profile,
    route-to-provider, route-to-script, evict-to-quarantine.
  - Reversible quarantine with a grace window; explicit purge only.
  - Routing ledger with provenance; dedup against already-routed artifacts.
  - manual (report + approve) and auto (apply silently) modes.
  - `/memtriage` slash command and `mem_triage` agent tool.
  - Prompt: tests suite + vendor-free stdlib-only core.