"""Cerveau decision-profile provisioning (the "memtriage setup" path).

Closes the install gap: a fresh plugin install has no ``cerveau`` profile, so
dispatch fails at "profile not found". ``provision`` automates what the docs
used to say to do by hand:

    hermes profile create cerveau …
    hermes -p cerveau config set model.default <model>
    hermes -p cerveau config set model.provider custom:<slug>
    hermes -p cerveau config set model.base_url <relay>
    hermes -p cerveau config migrate
    (re-apply the custom-provider api_key, copied programmatically)
    seed SOUL/MEMORY/USER from the shipped templates
    verify (profile show + a smoke question the seeded memory must answer)

Everything environment-specific (model, provider slug, base URL, api key) is
DISCOVERED at runtime from the currently-active profile's working config — a
fresh install does not need to know them in advance. Secrets are never echoed.

This module is intentionally thin and best-effort: each step returns a notice
string. It never hardcodes a path or a secret.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import learning
from .config import Config

_DESCRIPTION = (
    "Memory triage decision-maker: routes knowledge to its proper home and "
    "flags stale entries for the hermes-memory-triage plugin."
)


def _run(cmd) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


def _hermes() -> str:
    return os.environ.get("HERMES_BIN", "hermes")


def profile_exists(cfg: Config) -> bool:
    return learning.decision_profile_dir(cfg).is_dir()


def _read_active_provider() -> dict:
    """Best-effort: pull the working profile's model + custom provider entry.

    Reads ``hermes config get model --json`` for the active model/provider and
    ``hermes config get custom_providers --json`` for the matching provider
    (name/base_url/api_key). Returns {} if undiscoverable.
    """
    out: dict = {}
    try:
        p1 = _run([_hermes(), "config", "get", "model", "--json"])
        model = json.loads(p1.stdout or "{}")
        out["model.default"] = (
            model.get("default") or model.get("model") or ""
        )
        out["model.provider"] = model.get("provider") or ""
        out["base_url"] = model.get("base_url") or ""
    except Exception:  # noqa: BLE001
        pass

    providers: list = []
    try:
        p2 = _run([_hermes(), "config", "get", "custom_providers", "--json"])
        providers = json.loads(p2.stdout or "[]") or []
    except Exception:  # noqa: BLE001
        pass

    if providers:
        # Prefer the provider that matches the active custom:<slug> ref.
        slug = (out.get("model.provider") or "").split(":", 1)[-1]
        chosen = next((p for p in providers if p.get("name") == slug), providers[0])
        out["api_key"] = chosen.get("api_key", "")
        if not out["base_url"]:
            out["base_url"] = chosen.get("base_url", "")
        out["provider_slug"] = chosen.get("name", "")
    return out


def _set_profile(cfg: Config, key: str, value: str) -> bool:
    return _run([_hermes(), "-p", cfg.cerveau_profile, "config", "set", key, value]).returncode == 0


def provision(cfg: Config) -> dict:
    """Create + wire + seed + verify the decision profile. Returns a report
    dict of step -> outcome strings (best-effort, never raises)."""
    steps: dict = {}
    prov = _read_active_provider()

    # 1) Create the profile if missing.
    if profile_exists(cfg):
        steps["profile"] = f"exists ({cfg.cerveau_profile})"
    else:
        rc = _run(
            [_hermes(), "profile", "create", cfg.cerveau_profile,
             "--description", _DESCRIPTION]
        ).returncode
        steps["profile"] = (
            f"created ({cfg.cerveau_profile})" if rc == 0
            else f"create failed rc={rc}"
        )

    # 2) Wire the model + provider (fresh profiles have NO providers).
    default = prov.get("model.default", "")
    if default:
        _set_profile(cfg, "model.default", default)
    provider_ref = prov.get("model.provider", "")
    if provider_ref:
        _set_profile(cfg, "model.provider", provider_ref)
    base_url = prov.get("base_url", "")
    if base_url:
        _set_profile(cfg, "model.base_url", base_url)
    steps["model"] = (
        f"{default or '?'} via {provider_ref or '?'}"
        + (f" @ {base_url}" if base_url else "")
    )

    # 3) config migrate (normalizes; notoriously drops api_key — see
    #    provider-authoring pitfalls) then re-apply the provider entry by
    #    appending a custom_providers block carrying the key.
    _run([_hermes(), "-p", cfg.cerveau_profile, "config", "migrate"])
    if prov.get("api_key"):
        ok = _apply_provider_entry(cfg, prov)
        steps["provider"] = "api_key applied" if ok else "provider write failed"
    else:
        steps["provider"] = "no active provider discovered; set model manually"

    # 4) Seed the brain non-destructively.
    seeded = learning.seed_profile(cfg)
    steps["seed"] = "; ".join(f"{k}={v}" for k, v in seeded.items())

    # 5) Verify wiring + smoke check.
    steps["verify"] = verify(cfg)
    return steps


def _apply_provider_entry(cfg: Config, prov: dict) -> bool:
    """Append a custom_providers block to the profile's config.yaml with the
    discovered name/base_url/api_key (never echoed). Best-effort."""
    try:
        cfg_path = learning.decision_profile_dir(cfg) / "config.yaml"
        if not cfg_path.exists():
            return False
        text = cfg_path.read_text(encoding="utf-8", errors="replace")
        entry = {
            "name": prov.get("provider_slug", ""),
            "base_url": prov.get("base_url", ""),
            "api_key": prov.get("api_key", ""),
        }
        if not entry["name"] and prov.get("model.provider", "").startswith("custom:"):
            entry["name"] = prov["model.provider"].split(":", 1)[-1]
        if not entry["name"]:
            return False
        block = "\ncustom_providers:\n"
        block += f"  - name: {entry['name']}\n"
        block += f"    base_url: \"{entry['base_url']}\"\n"
        block += f"    api_key: \"{entry['api_key']}\"\n"
        if "custom_providers:" not in text:
            cfg_path.write_text(text.rstrip() + block, encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return False


def verify(cfg: Config) -> str:
    """Confirm the profile resolves a model/provider and the seeded memory
    actually reaches the model. Returns a short English report."""
    lines: list = []
    try:
        p = _run([_hermes(), "profile", "show", cfg.cerveau_profile])
        lines.append("profile show rc=" + str(p.returncode))
    except Exception:  # noqa: BLE001
        lines.append("profile show failed")

    # Smoke: a question only the seeded memory can answer.
    q = (
        "In one sentence: what are the two non-negotiable safety rules for "
        "memory triage eviction?"
    )
    try:
        smoke = _run(
            [_hermes(), "-p", cfg.cerveau_profile, "chat", "-q", q]
        )
        reply = (smoke.stdout or "")[-2000:]
        ok = any(
            kw in reply.lower()
            for kw in ("quarantin", "never evict", "reversib", "never invent")
        )
        lines.append(
            "smoke=" + ("PASS (seeded memory reached the model)" if ok
                        else "UNVERIFIED (no seeded keyword in reply)")
        )
    except Exception as exc:  # noqa: BLE001
        lines.append(f"smoke failed: {exc}")
    return " | ".join(lines)