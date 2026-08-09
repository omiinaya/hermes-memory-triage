# Cerveau — the decision profile

Cerveau is a dedicated Hermes profile whose sole purpose is making memory
routing decisions for the hermes-memory-triage plugin. It receives the full
inventory as a JSON payload and replies with a validated JSON action plan.

## Create

```bash
hermes profile create cerveau --description "Memory triage decision-maker: routes knowledge to its proper home and flags stale entries for the hermes-memory-triage plugin."
```

## Wire the model (fresh profiles have NO providers — mandatory)

```bash
hermes -p cerveau config set model.default deepseek-v4-flash-free
hermes -p cerveau config set model.provider custom:relay
hermes -p cerveau config set model.base_url http://127.0.0.1:4002/v1
hermes -p cerveau config migrate
```

`config migrate` rewrites `custom_providers` and DROPS `api_key` — re-apply the
relay client key to the provider entry afterwards (copy it programmatically
from the working profile, never retype it):

```python
# from the default profile's config
src = next(c for c in global_cfg["custom_providers"] if c["name"] == "relay")
entry = {k: src[k] for k in ("name", "base_url", "api_key")}
```

## Seed identity + knowledge

- `SOUL.md`: append the mission line — "Cerveau: memory triage decision-maker.
  Sole purpose: routing decisions. Reply with structured JSON plans only."
- `memories/MEMORY.md`: seed the taxonomy, destination definitions, staleness
  signals, safety rules, and the output JSON schema (mirror
  `memtriage/cerveau.py` PROMPT_TEMPLATE).
- `memories/USER.md`: the team doctrine (English-only, cross-platform,
  provider-authoritative, reversible eviction).

## Verify

```bash
hermes profile show cerveau
hermes -p cerveau chat -q "What are the three non-negotiable safety rules for
memory triage eviction?"   # must answer from seeded memory, not defaults
```

## Notes

- The plugin invokes Cerveau one-shot: `<cerveau_bin> -p cerveau chat -q <payload>`
  with a 240s timeout and a 60k reply cap.
- Cerveau learns: his own memories record decisions + outcomes, so routing
  improves over time. The plugin's ledger is the durable source of truth.
