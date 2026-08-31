"""Plugin configuration: load/save JSON config, resolve the data directory.

The data directory holds config.json, ledger.json, quarantine/, reports/ and
state.json.  Default location is ``~/.memtriage`` (override with the
``MEMTRIAGE_HOME`` environment variable).  All paths are built with pathlib
and resolved against the user's home so the plugin works on Windows, macOS
and Linux unchanged.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_FILENAME = "config.json"
LEDGER_FILENAME = "ledger.json"
STATE_FILENAME = "state.json"
QUARANTINE_DIRNAME = "quarantine"
REPORTS_DIRNAME = "reports"
SKILLS_DIRNAME = "skills"

DEFAULT_THRESHOLD_PERCENT = 0.75
DEFAULT_QUARANTINE_DAYS = 7
DEFAULT_COOLDOWN_MINUTES = 60
DEFAULT_CERVEAU_PROFILE = "cerveau"
DEFAULT_CERVEAU_TIMEOUT = 600
DEFAULT_DETERMINISTIC_FALLBACK = True
DEFAULT_SCRIPTS_DIR = "~/.hermes/scripts"
DEFAULT_PROVIDER_BASE_URL = "http://127.0.0.1:8420"

VALID_MODES = ("manual", "auto")


@dataclass
class Config:
    """Plugin configuration with sane defaults for a fresh install."""

    threshold_percent: float = DEFAULT_THRESHOLD_PERCENT
    mode: str = "manual"
    quarantine_days: int = DEFAULT_QUARANTINE_DAYS
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES
    cerveau_profile: str = DEFAULT_CERVEAU_PROFILE
    cerveau_bin: str = "hermes"
    cerveau_timeout: int = DEFAULT_CERVEAU_TIMEOUT
    deterministic_fallback: bool = DEFAULT_DETERMINISTIC_FALLBACK
    scripts_dir: str = DEFAULT_SCRIPTS_DIR
    provider_base_url: str = DEFAULT_PROVIDER_BASE_URL
    data_dir: Path = field(default_factory=lambda: _default_data_dir())

    def __post_init__(self) -> None:
        # data_dir may arrive as a str (manual/CLI construction); normalize it.
        self.data_dir = Path(os.path.expanduser(str(self.data_dir)))
        if self.mode not in VALID_MODES:
            raise ValueError(
                f"mode must be one of {VALID_MODES!r}, got {self.mode!r}"
            )
        if not 0.0 < self.threshold_percent < 1.0:
            raise ValueError(
                "threshold_percent must be in (0.0, 1.0), "
                f"got {self.threshold_percent!r}"
            )

    # -- paths -----------------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.data_dir / CONFIG_FILENAME

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / LEDGER_FILENAME

    @property
    def state_path(self) -> Path:
        return self.data_dir / STATE_FILENAME

    @property
    def quarantine_dir(self) -> Path:
        return self.data_dir / QUARANTINE_DIRNAME

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / REPORTS_DIRNAME

    @property
    def skills_root(self) -> Path:
        """Resolved skills directory (where SKILL.md trees are written)."""
        p = Path(os.path.expanduser(self.skills_root_raw))
        return p

    @property
    def scripts_root(self) -> Path:
        """Resolved scripts directory (where routed scripts are written)."""
        return Path(os.path.expanduser(self.scripts_dir))

    @property
    def skills_root_raw(self) -> str:
        """Skills root as configured; default follows HERMES_HOME else ~/.hermes."""
        hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        return str(Path(hermes_home) / SKILLS_DIRNAME)

    # -- serialization ---------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "threshold_percent": self.threshold_percent,
            "mode": self.mode,
            "quarantine_days": self.quarantine_days,
            "cooldown_minutes": self.cooldown_minutes,
            "cerveau_profile": self.cerveau_profile,
            "cerveau_bin": self.cerveau_bin,
            "cerveau_timeout": self.cerveau_timeout,
            "deterministic_fallback": self.deterministic_fallback,
            "scripts_dir": self.scripts_dir,
            "provider_base_url": self.provider_base_url,
            "data_dir": str(self.data_dir),
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Config":
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs: Dict[str, Any] = {}
        for key, value in raw.items():
            if key in allowed:
                kwargs[key] = value
        data_dir = kwargs.pop("data_dir", None)
        cfg = cls(**kwargs)
        if data_dir:
            cfg.data_dir = Path(os.path.expanduser(str(data_dir)))
        return cfg

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self.config_path, self.to_dict())

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if cfg.config_path.exists():
            try:
                raw = json.loads(cfg.config_path.read_text(encoding="utf-8"))
                return cls.from_dict(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                # Corrupt or invalid config: fail open to defaults, never crash.
                raise ValueError(
                    f"Invalid config at {cfg.config_path}: {exc}"
                ) from exc
        return cfg


def _default_data_dir() -> Path:
    override = os.environ.get("MEMTRIAGE_HOME")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~/.memtriage"))


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically via a temp file + os.replace (cross-platform)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def ensure_data_dirs(cfg: Config) -> None:
    """Create the plugin's data directory layout if missing."""
    for d in (cfg.data_dir, cfg.quarantine_dir, cfg.reports_dir):
        d.mkdir(parents=True, exist_ok=True)
