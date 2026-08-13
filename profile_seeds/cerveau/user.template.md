Team doctrine — apply these principles to every routing decision.
Routing must be reversible; eviction always goes to quarantine, never to instant destruction.
The memory provider (managed DB) is the authoritative long-term store; local file stores are for the working budget only.
All output (UI, messages, metadata, docs) is English only.
The target environment is cross-platform (Windows/macOS/Linux); never assume POSIX-only paths or shell.
When a destination (provider gateway or cron) is unreachable, the plan must surface it as a pending action, not silently drop it.
Prefer routing durable knowledge to its proper home over keeping it bloated in the working memory store.
