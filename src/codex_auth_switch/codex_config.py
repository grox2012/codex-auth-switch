from __future__ import annotations

import os
import re
import shutil
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CODEX_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
TOKENFACTORY_PROVIDER_ID = "tokenfactory"
TOKENFACTORY_PROVIDER_DEFAULTS = {
    "name": "TokenFactory local gateway",
    "base_url": "http://127.0.0.1:8080/v1",
    "wire_api": "responses",
}

_TOP_LEVEL_MODEL_PROVIDER = re.compile(r"^\s*model_provider\s*=")
_TABLE_HEADER = re.compile(r"^\s*\[\s*([^\]]+)\s*\]\s*(?:#.*)?$")
_SIMPLE_KEY = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")


@dataclass(frozen=True)
class ResetResult:
    changed: bool
    config_path: Path
    backup_path: Path | None = None


def set_codex_model_provider(
    config_path: Path,
    provider: str,
    *,
    tokenfactory_url: str = TOKENFACTORY_PROVIDER_DEFAULTS["base_url"],
) -> None:
    if not provider or any(character.isspace() for character in provider):
        raise ValueError("model provider must be a non-empty identifier")

    path = config_path.expanduser()
    original, mode = _read_config(path)
    updated = original
    if provider == TOKENFACTORY_PROVIDER_ID:
        updated = _ensure_tokenfactory_provider(updated, tokenfactory_url)
    updated = _replace_top_level_model_provider(updated, provider)
    payload = tomllib.loads(updated)
    if provider == TOKENFACTORY_PROVIDER_ID:
        definitions = payload.get("model_providers")
        if not isinstance(definitions, dict) or not isinstance(
            definitions.get(TOKENFACTORY_PROVIDER_ID), dict
        ):
            raise ValueError("tokenfactory model provider definition is missing")
    _atomic_write(path, updated, mode)


def reset_codex_config(config_path: Path) -> ResetResult:
    """Remove only settings managed by this tool; never invoke an external command."""

    path = config_path.expanduser()
    if not path.exists():
        return ResetResult(changed=False, config_path=path)

    original, mode = _read_config(path)
    updated = _remove_top_level_model_provider(original)
    updated = _remove_tokenfactory_provider_tables(updated)
    updated = _normalize_blank_lines(updated)
    tomllib.loads(updated)
    if updated == original:
        return ResetResult(changed=False, config_path=path)

    backup_path = path.with_name(f"{path.name}.codex-auth-switch.bak")
    shutil.copyfile(path, backup_path)
    try:
        os.chmod(backup_path, mode)
    except OSError:
        pass
    _atomic_write(path, updated, mode)
    return ResetResult(changed=True, config_path=path, backup_path=backup_path)


def _read_config(path: Path) -> tuple[str, int]:
    try:
        return path.read_text(encoding="utf-8"), path.stat().st_mode & 0o777
    except FileNotFoundError:
        return "", 0o600


def _replace_top_level_model_provider(original: str, provider: str) -> str:
    replacement = f'model_provider = "{provider}"\n'
    lines = original.splitlines(keepends=True)
    updated_lines: list[str] = []
    replaced = False
    in_top_level = True
    for line in lines:
        if line.lstrip().startswith("["):
            if not replaced:
                updated_lines.append(replacement)
                replaced = True
            in_top_level = False
        if in_top_level and _TOP_LEVEL_MODEL_PROVIDER.match(line):
            if not replaced:
                updated_lines.append(replacement)
                replaced = True
            continue
        updated_lines.append(line)
    if not replaced:
        if updated_lines and not updated_lines[-1].endswith(("\n", "\r")):
            updated_lines[-1] += "\n"
        updated_lines.append(replacement)
    return "".join(updated_lines)


def _remove_top_level_model_provider(original: str) -> str:
    lines = original.splitlines(keepends=True)
    updated_lines: list[str] = []
    in_top_level = True
    for line in lines:
        if line.lstrip().startswith("["):
            in_top_level = False
        if in_top_level and _TOP_LEVEL_MODEL_PROVIDER.match(line):
            continue
        updated_lines.append(line)
    return "".join(updated_lines)


def _ensure_tokenfactory_provider(original: str, base_url: str) -> str:
    lines = original.splitlines(keepends=True)
    table_index = None
    table_end = len(lines)
    for index, line in enumerate(lines):
        match = _TABLE_HEADER.match(line.rstrip("\r\n"))
        if not match:
            continue
        table_name = match.group(1).strip()
        if table_index is None and table_name == "model_providers.tokenfactory":
            table_index = index
            continue
        if table_index is not None:
            table_end = index
            break

    defaults = {**TOKENFACTORY_PROVIDER_DEFAULTS, "base_url": base_url}
    if table_index is None:
        updated = original
        if updated and not updated.endswith(("\n", "\r")):
            updated += "\n"
        if updated and not updated.endswith("\n\n"):
            updated += "\n"
        updated += (
            "# Managed by codex-auth-switch.\n"
            "[model_providers.tokenfactory]\n"
            f'name = "{defaults["name"]}"\n'
            f'base_url = "{defaults["base_url"]}"\n'
            f'wire_api = "{defaults["wire_api"]}"\n'
        )
        return updated

    existing_keys: set[str] = set()
    for line in lines[table_index + 1 : table_end]:
        match = _SIMPLE_KEY.match(line)
        if match:
            existing_keys.add(match.group(1))
    missing = [
        f'{key} = "{value}"\n'
        for key, value in defaults.items()
        if key not in existing_keys
    ]
    if missing:
        lines[table_end:table_end] = missing
    return "".join(lines)


def _remove_tokenfactory_provider_tables(original: str) -> str:
    lines = original.splitlines(keepends=True)
    updated: list[str] = []
    removing = False
    for line in lines:
        match = _TABLE_HEADER.match(line.rstrip("\r\n"))
        if match:
            table_name = match.group(1).strip()
            removing = table_name == "model_providers.tokenfactory" or table_name.startswith(
                "model_providers.tokenfactory."
            )
        if removing:
            continue
        if line.strip() == "# Managed by codex-auth-switch.":
            continue
        updated.append(line)
    return "".join(updated)


def _normalize_blank_lines(value: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", value).lstrip("\n")


def _atomic_write(path: Path, value: str, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
