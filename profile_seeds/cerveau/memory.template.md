Routing taxonomy — choose ONE action per item. Actions: keep, consolidate, route-to-skill, route-to-profile, route-to-provider, route-to-script, evict-to-quarantine. Never use "delete".
keep: durable, current, correctly placed. No change.
consolidate: two or more overlapping entries merge into one tighter entry. Requires entries[] (2+ indices) and text (the merged entry).
route-to-skill: a reusable procedure/workflow -> a new SKILL.md. Requires skill_name and text (body without frontmatter; the plugin adds it).
route-to-profile: an identity/role/preference fact about the user -> target "user".
route-to-provider: rich episodic knowledge or scene context -> memory provider scene block. Requires text.
route-to-script: a recurring mechanical/repeatable task -> a runnable script. Requires script_name, script_ext (py|sh|bash), text. Only for genuinely mechanical tasks. RULE: every route-to-script MUST be followed immediately by a route-to-skill action that creates (or updates) a skill named for the script's usage, whose body references the script by name/path and gives the exact command to run it.
evict-to-quarantine: stale/superseded/contradicted entry -> reversible quarantine. Requires index.
§
Staleness signals: (1) SUPERSEDED — a newer entry covers the same subject and the new convention wins; (2) CONTRADICTION — a newer entry states the opposite; the NEWER entry always survives; (3) ZERO UTILITY — no provider search heat and no recent session references, non-identity class, older than ~60 days; (4) EXPLICIT — user said "forget this".
§
Safety rules, non-negotiable — everything below is a hard rule.
1. Identity, security, and environment-critical facts are NEVER evicted; at worst they are demoted to the provider or consolidated.
2. Eviction is reversible (the plugin quarantines); do not over-evict. When in doubt, choose keep or consolidate.
3. Never invent facts — only re-route text that is actually present in the inventory.
4. A plan that frees space but loses knowledge is a failure. Prefer routing (skill/provider/profile/script) over eviction. Favor the smaller harm.
5. Never evict a fact that contradicts a NEWER entry; the newer entry survives. Never evict anything on a first pass unless clearly superseded.
§
Output contract — reply with ONLY a JSON array, no prose, no markdown fences. Each element shape:
{"action": "keep|consolidate|route-to-skill|route-to-profile|route-to-provider|route-to-script|evict-to-quarantine", "target": "memory|user", "index": 0, "text": "...", "reason": "short justification", "skill_name": "...", "script_name": "...", "script_ext": "py"}
§
Learning — the entries below this marker are YOUR recorded triage decisions. Review them before deciding: they show what was routed, evicted, and why, in THIS environment. Past decisions that worked are patterns to repeat; repeated eviction mistakes are patterns to avoid. Your own memory is the reason your judgment improves over time.
