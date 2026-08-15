from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from codex_auth_switch import __version__
from codex_auth_switch.codex_config import (
    DEFAULT_CODEX_CONFIG_PATH,
    reset_codex_config,
    resolve_tokenfactory_base_url,
    set_codex_model_provider,
)
from codex_auth_switch.config import (
    ConfigError,
    Settings,
    Source,
    load_settings,
    resolve_config_path,
    user_config_path,
)

PROBE_PROMPT = "Return exactly: CODEX_AUTH_SWITCH_OK"


@dataclass(frozen=True)
class ProbeResult:
    source: Source
    available: bool
    message: str
    elapsed_ms: int


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "init":
        return _init(argv[1:])
    if argv and argv[0] == "doctor":
        return _doctor(argv[1:])
    if argv and argv[0] == "status":
        argv = argv[1:]

    parser = argparse.ArgumentParser(
        description="Safely switch local Codex accounts and repair provider configuration.",
        epilog=(
            "Recovery: codex-auth-switch --reset\n"
            "Single account: codex-auth-switch --single SOURCE_ID\n"
            "TokenFactory: codex-auth-switch --tokenfactory"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", default=None, help="Account config JSON path.")
    parser.add_argument("--codex-config", default=str(DEFAULT_CODEX_CONFIG_PATH))
    parser.add_argument("--model", default=None)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--tokenfactory-url",
        default=os.environ.get("TOKENFACTORY_BASE_URL"),
        help=(
            "TokenFactory API base ending in /v1. Defaults to TOKENFACTORY_BASE_URL, "
            "then the existing provider URL, then http://127.0.0.1:8080/v1."
        ),
    )
    parser.add_argument(
        "--tokenfactory-health-timeout",
        type=float,
        default=5.0,
        help="Seconds to wait for the TokenFactory health check (default: 5).",
    )
    parser.add_argument(
        "--skip-tokenfactory-health-check",
        action="store_true",
        help="Write the provider config without checking TokenFactory /healthz.",
    )
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--single", nargs="?", const="recommended", metavar="SOURCE_ID")
    operation.add_argument("--switch", nargs="?", const="recommended", metavar="SOURCE_ID")
    operation.add_argument("--tokenfactory", action="store_true")
    operation.add_argument("--reset", action="store_true")
    args = parser.parse_args(argv)
    codex_config_path = Path(args.codex_config).expanduser()

    if args.reset:
        return _reset(codex_config_path)
    if args.tokenfactory:
        return _set_provider(
            codex_config_path,
            "tokenfactory",
            args.tokenfactory_url,
            check_health=not args.skip_tokenfactory_health_check,
            health_timeout=args.tokenfactory_health_timeout,
        )

    if args.single:
        try:
            set_codex_model_provider(codex_config_path, "openai")
        except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
            print(f"switch failed: repairing model_provider failed: {exc}", file=sys.stderr)
            return 1

    config_path = resolve_config_path(args.config)
    try:
        settings = load_settings(config_path)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    model = args.model or settings.model

    if args.single or args.switch:
        requested = args.single or args.switch
        return _switch_account(
            settings,
            requested,
            model=model,
            timeout=args.timeout,
            provider="openai" if args.single else None,
        )

    results = [_probe(source, model=model, timeout=args.timeout) for source in settings.sources]
    _print_status(results, config_path=config_path, model=model, as_json=args.json)
    return 0 if any(result.available for result in results) else 1


def _reset(config_path: Path) -> int:
    try:
        result = reset_codex_config(config_path)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        print(f"reset failed: {exc}", file=sys.stderr)
        return 1
    if result.changed:
        print("Codex configuration reset to built-in provider defaults.")
        print(f"Config: {result.config_path}")
        print(f"Backup: {result.backup_path}")
    else:
        print("Codex configuration already uses built-in provider defaults.")
        print(f"Config: {result.config_path}")
    print("Run `codex` to continue with the default ChatGPT OAuth flow.")
    return 0


def _set_provider(
    config_path: Path,
    provider: str,
    tokenfactory_url: str | None,
    *,
    check_health: bool,
    health_timeout: float,
) -> int:
    try:
        effective_url = resolve_tokenfactory_base_url(config_path, tokenfactory_url)
        if check_health:
            _check_tokenfactory_health(effective_url, timeout=health_timeout)
        set_codex_model_provider(
            config_path,
            provider,
            tokenfactory_url=effective_url,
        )
    except (OSError, tomllib.TOMLDecodeError, ValueError, RuntimeError) as exc:
        print(f"provider switch failed: {exc}", file=sys.stderr)
        return 1
    print(f"Default provider: {provider}")
    print(f"TokenFactory API: {effective_url}")
    return 0


def _check_tokenfactory_health(base_url: str, *, timeout: float) -> None:
    if timeout <= 0:
        raise ValueError("TokenFactory health timeout must be greater than zero")
    parsed = urlsplit(base_url)
    api_path = parsed.path.rstrip("/")
    health_path = f"{api_path[:-3]}/healthz" if api_path.endswith("/v1") else "/healthz"
    health_url = urlunsplit((parsed.scheme, parsed.netloc, health_path, "", ""))
    request = urllib.request.Request(
        health_url,
        headers={"Accept": "application/json", "User-Agent": "codex-auth-switch"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"TokenFactory health check returned HTTP {response.status}: {health_url}"
                )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(
            f"TokenFactory is unreachable at {health_url}; Codex config was not changed: {exc}"
        ) from exc


def _switch_account(
    settings: Settings,
    requested: str,
    *,
    model: str,
    timeout: float,
    provider: str | None,
) -> int:
    source = _select_source(settings, requested, model=model, timeout=timeout)
    if source is None:
        return 1
    try:
        token = _read_secret(source.onepassword_ref)
        result = subprocess.run(
            ["codex", "login", "--with-access-token"],
            input=token,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=dict(os.environ),
        )
    except RuntimeError as exc:
        print(f"switch failed: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("switch failed: codex CLI was not found", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print("switch failed: codex login timed out", file=sys.stderr)
        return 1
    if result.returncode != 0:
        message = _safe_message(result.stderr or result.stdout, token)
        print(f"switch failed: codex login failed: {message}", file=sys.stderr)
        if provider:
            print(
                "Codex config remains on the built-in openai provider so the CLI stays usable.",
                file=sys.stderr,
            )
        return 1
    print(f"Switched local Codex account: {source.source_id} ({source.label})")
    if provider:
        print(f"Default provider: {provider}")
    return 0


def _select_source(
    settings: Settings,
    requested: str,
    *,
    model: str,
    timeout: float,
) -> Source | None:
    if requested != "recommended":
        source = next((item for item in settings.sources if item.source_id == requested), None)
        if source is None:
            print(f"switch failed: unknown source_id {requested!r}", file=sys.stderr)
        return source
    for source in settings.sources:
        result = _probe(source, model=model, timeout=timeout)
        if result.available:
            return source
    print("switch failed: no available recommended Codex account", file=sys.stderr)
    return None


def _probe(source: Source, *, model: str, timeout: float) -> ProbeResult:
    started = time.monotonic()
    try:
        token = _read_secret(source.onepassword_ref)
    except RuntimeError as exc:
        return ProbeResult(source, False, str(exc), _elapsed_ms(started))
    codex_home = _codex_home(source.source_id)
    codex_home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["CODEX_ACCESS_TOKEN"] = token
    env["CODEX_HOME"] = str(codex_home)
    try:
        result = subprocess.run(
            [
                "codex",
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--ephemeral",
                "--model",
                model,
                PROBE_PROMPT,
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        return ProbeResult(source, False, "codex CLI was not found", _elapsed_ms(started))
    except subprocess.TimeoutExpired:
        return ProbeResult(source, False, "codex probe timed out", _elapsed_ms(started))
    if result.returncode != 0:
        return ProbeResult(
            source,
            False,
            _safe_message(result.stderr or result.stdout, token),
            _elapsed_ms(started),
        )
    return ProbeResult(source, True, "available", _elapsed_ms(started))


def _read_secret(reference: str) -> str:
    try:
        result = subprocess.run(
            ["op", "read", "--no-newline", reference],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
            env=dict(os.environ),
        )
    except FileNotFoundError as exc:
        raise RuntimeError("op CLI was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("op read timed out") from exc
    if result.returncode != 0:
        raise RuntimeError(f"op read failed: {_safe_message(result.stderr or result.stdout)}")
    if not result.stdout.strip():
        raise RuntimeError("op read returned an empty token")
    return result.stdout


def _print_status(
    results: list[ProbeResult],
    *,
    config_path: Path,
    model: str,
    as_json: bool,
) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "config": str(config_path),
                    "model": model,
                    "results": [
                        {
                            "source_id": result.source.source_id,
                            "label": result.source.label,
                            "available": result.available,
                            "message": result.message,
                            "elapsed_ms": result.elapsed_ms,
                        }
                        for result in results
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    print(f"Config: {config_path}")
    print(f"Model:  {model}")
    for result in results:
        status = "available" if result.available else "unavailable"
        print(
            f"{result.source.source_id:<24} {status:<11} {result.elapsed_ms:>6}ms  {result.message}"
        )


def _init(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="codex-auth-switch init")
    parser.add_argument("--config", default=None)
    parser.add_argument("--source-id", default="codex-primary")
    parser.add_argument("--label", default="Codex Primary")
    parser.add_argument(
        "--onepassword-ref",
        default="op://tokenfactory/codex-primary/access-token",
    )
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    path = Path(args.config).expanduser() if args.config else user_config_path()
    if path.exists() and not args.force:
        print(f"init refused: config already exists: {path}", file=sys.stderr)
        return 2
    payload = {
        "model": args.model,
        "sources": [
            {
                "source_id": args.source_id,
                "label": args.label,
                "onepassword_ref": args.onepassword_ref,
                "priority": 100,
            }
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Created config: {path}")
    print("Next: codex-auth-switch doctor")
    return 0


def _doctor(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="codex-auth-switch doctor")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    path = resolve_config_path(args.config)
    checks: list[tuple[str, bool, str]] = []
    try:
        settings = load_settings(path)
    except ConfigError as exc:
        checks.append(("config", False, str(exc)))
    else:
        checks.append(("config", True, f"{len(settings.sources)} enabled Codex account(s)"))
    checks.append(("op", shutil.which("op") is not None, shutil.which("op") or "not found"))
    checks.append(
        ("codex", shutil.which("codex") is not None, shutil.which("codex") or "not found")
    )
    for name, ok, message in checks:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {message}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def _codex_home(source_id: str) -> Path:
    safe_id = "".join(
        character if character.isalnum() or character in "-_" else "_" for character in source_id
    )
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
        return root / "CodexAuthSwitch" / "codex-home" / safe_id
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "codex-auth-switch" / "codex-home" / safe_id


def _safe_message(value: str, secret: str | None = None) -> str:
    message = value.strip() or "unknown error"
    if secret:
        message = message.replace(secret, "[REDACTED]")
    return message.replace("\r", " ").replace("\n", " ")[:500]


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


if __name__ == "__main__":
    raise SystemExit(main())
