from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Source:
    source_id: str
    label: str
    onepassword_ref: str
    priority: int = 100
    enabled: bool = True


@dataclass(frozen=True)
class Settings:
    model: str
    sources: list[Source]


def user_config_path() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return root / "CodexAuthSwitch" / "config.json"
    if sys_platform() == "darwin":
        return Path.home() / "Library" / "Application Support" / "CodexAuthSwitch" / "config.json"
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "codex-auth-switch" / "config.json"


def legacy_config_path() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return root / "TokenFactory" / "config.json"
    if sys_platform() == "darwin":
        return Path.home() / "Library" / "Application Support" / "TokenFactory" / "config.json"
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "tokenfactory" / "config.json"


def resolve_config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    if configured := os.environ.get("CODEX_AUTH_SWITCH_CONFIG"):
        return Path(configured).expanduser()
    current = user_config_path()
    if current.exists():
        return current
    if configured := os.environ.get("TOKENFACTORY_CONFIG"):
        return Path(configured).expanduser()
    legacy = legacy_config_path()
    return legacy if legacy.exists() else current


def load_settings(path: Path) -> Settings:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}; run `codex-auth-switch init`") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a JSON object")

    model = raw.get("model")
    if not isinstance(model, str) or not model:
        models = raw.get("models")
        if isinstance(models, list) and models and isinstance(models[0], dict):
            model = models[0].get("id")
    if not isinstance(model, str) or not model:
        model = "gpt-5.6-sol"

    source_values = raw.get("sources")
    if not isinstance(source_values, list):
        raise ConfigError("config field 'sources' must be an array")
    sources: list[Source] = []
    seen: set[str] = set()
    for index, value in enumerate(source_values):
        if not isinstance(value, dict):
            raise ConfigError(f"sources[{index}] must be an object")
        if value.get("provider", "codex") != "codex":
            continue
        source_id = value.get("source_id")
        reference = value.get("onepassword_ref")
        if not isinstance(source_id, str) or not source_id:
            raise ConfigError(f"sources[{index}].source_id must be a non-empty string")
        if source_id in seen:
            raise ConfigError(f"duplicate source_id: {source_id}")
        if not isinstance(reference, str) or not reference.startswith("op://"):
            raise ConfigError(f"sources[{index}].onepassword_ref must be an op:// reference")
        seen.add(source_id)
        sources.append(
            Source(
                source_id=source_id,
                label=str(value.get("label") or source_id),
                onepassword_ref=reference,
                priority=int(value.get("priority", 100)),
                enabled=bool(value.get("enabled", True)),
            )
        )
    sources = sorted((source for source in sources if source.enabled), key=_source_sort_key)
    return Settings(model=model, sources=sources)


def _source_sort_key(source: Source) -> tuple[int, str]:
    return (-source.priority, source.source_id)


def sys_platform() -> str:
    import sys

    return sys.platform
