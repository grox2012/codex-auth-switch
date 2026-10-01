from __future__ import annotations

import argparse
import json
import os
import shlex
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
FILE_CREDENTIALS_OVERRIDE = 'cli_auth_credentials_store="file"'
AUTH_ENVIRONMENT_VARIABLES = (
    "CODEX_ACCESS_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_FEDERATION_RULE_ID",
    "OPENAI_IDENTITY_TOKEN_FILE",
)


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
    if argv and argv[0] == "launch":
        return _launch(argv[1:])

    parser = argparse.ArgumentParser(
        description="Safely switch local Codex accounts and repair provider configuration.",
        epilog=(
            "Recovery: codex-auth-switch --reset\n"
            "Reset shared OAuth: codex-auth-switch --single SOURCE_ID\n"
            "Launch isolated OAuth: codex-auth-switch launch SOURCE_ID\n"
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
    operation.add_argument(
        "--single",
        nargs="?",
        const="recommended",
        metavar="SOURCE_ID",
        help=(
            "Reset TokenFactory config and clear the shared login so the next plain codex "
            "run starts ChatGPT OAuth."
        ),
    )
    operation.add_argument(
        "--switch",
        nargs="?",
        const="recommended",
        metavar="SOURCE_ID",
        help="Compatibility mode: save the selected source's personal access token.",
    )
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
    config_path = resolve_config_path(args.config)
    try:
        settings = load_settings(config_path)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    model = args.model or settings.model

    if args.single:
        return _switch_to_single_account(
            settings,
            args.single,
            codex_config_path=codex_config_path,
            timeout=args.timeout,
        )
    if args.switch:
        return _switch_account(
            settings,
            args.switch,
            model=model,
            timeout=args.timeout,
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


def _switch_to_single_account(
    settings: Settings,
    requested: str,
    *,
    codex_config_path: Path,
    timeout: float,
) -> int:
    source = _configured_source(settings, requested)
    if source is None:
        return 1
    try:
        reset_result = reset_codex_config(codex_config_path)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        print(f"single-account switch failed: config reset failed: {exc}", file=sys.stderr)
        return 1

    codex_home = codex_config_path.expanduser().parent
    env = _codex_env(codex_home)
    try:
        result = subprocess.run(
            ["codex", "logout"],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        print(
            "single-account switch failed: config was reset, but codex CLI was not found "
            "to clear the saved login",
            file=sys.stderr,
        )
        return 1
    except subprocess.TimeoutExpired:
        print(
            "single-account switch failed: config was reset, but codex logout timed out",
            file=sys.stderr,
        )
        return 1
    if result.returncode != 0:
        message = _safe_message(result.stderr or result.stdout)
        print(
            f"single-account switch failed: config was reset, but codex logout failed: {message}",
            file=sys.stderr,
        )
        return 1

    print("Codex single-account mode is ready.")
    print(f"Source: {source.source_id} ({source.label})")
    print("Default provider: built-in OpenAI")
    print("Saved shared login: cleared")
    print(f"Config: {reset_result.config_path}")
    if reset_result.backup_path is not None:
        print(f"Backup: {reset_result.backup_path}")
    print(
        "Run codex, choose Sign in with ChatGPT, and authenticate the selected source "
        "account to use its OAuth reset credit."
    )
    return 0


def _configured_source(settings: Settings, requested: str) -> Source | None:
    if requested == "recommended":
        if settings.sources:
            return settings.sources[0]
        print("single-account switch failed: no enabled Codex source is configured", file=sys.stderr)
        return None
    source = next((item for item in settings.sources if item.source_id == requested), None)
    if source is None:
        print(f"single-account switch failed: unknown source_id {requested!r}", file=sys.stderr)
    return source


def _switch_account(
    settings: Settings,
    requested: str,
    *,
    model: str,
    timeout: float,
) -> int:
    """Compatibility path for explicitly saving a personal access token."""

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
        return 1
    print(f"Switched saved access-token account: {source.source_id} ({source.label})")
    print("This compatibility mode does not create a ChatGPT OAuth session.")
    return 0


def _launch(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="codex-auth-switch launch",
        description="Launch Codex with an isolated account cache and a private app server.",
    )
    parser.add_argument("--config", default=None, help="Account config JSON path.")
    parser.add_argument("source_id")
    parser.add_argument("codex_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    config_path = resolve_config_path(args.config)
    try:
        settings = load_settings(config_path)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    source = next((item for item in settings.sources if item.source_id == args.source_id), None)
    if source is None:
        print(f"launch failed: unknown source_id {args.source_id!r}", file=sys.stderr)
        return 1

    codex_home = _codex_home(source.source_id)
    codex_home.mkdir(parents=True, exist_ok=True)
    try:
        reset_codex_config(codex_home / "config.toml")
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        print(f"launch failed: isolated config reset failed: {exc}", file=sys.stderr)
        return 1
    env = _codex_env(codex_home)
    if not _ensure_chatgpt_oauth(source, env=env):
        return 1

    codex_args = list(args.codex_args)
    if codex_args[:1] == ["--"]:
        codex_args = codex_args[1:]
    command = [
        "codex",
        "--no-daemon",
        "-c",
        FILE_CREDENTIALS_OVERRIDE,
        *codex_args,
    ]
    try:
        return subprocess.run(command, check=False, env=env).returncode
    except FileNotFoundError:
        print("launch failed: codex CLI was not found", file=sys.stderr)
        return 1


def _ensure_chatgpt_oauth(source: Source, *, env: dict[str, str]) -> bool:
    auth_prefix = ["codex", "-c", FILE_CREDENTIALS_OVERRIDE]
    status_command = [*auth_prefix, "login", "status"]
    try:
        status = subprocess.run(
            status_command,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        print("launch failed: codex CLI was not found", file=sys.stderr)
        return False

    if _login_status_uses_chatgpt(status):
        return True

    if status.returncode == 0:
        print(
            f"Replacing the isolated non-OAuth login for {source.source_id} "
            "with ChatGPT OAuth."
        )
        try:
            logout = subprocess.run(
                [*auth_prefix, "logout"],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
        except FileNotFoundError:
            print("launch failed: codex CLI was not found", file=sys.stderr)
            return False
        if logout.returncode != 0:
            message = _safe_message(logout.stderr or logout.stdout)
            print(f"launch failed: could not remove the non-OAuth login: {message}", file=sys.stderr)
            return False
    else:
        print(f"ChatGPT OAuth login required for {source.source_id} ({source.label}).")

    print("Complete the browser sign-in using the matching ChatGPT account.")
    try:
        login = subprocess.run([*auth_prefix, "login"], check=False, env=env)
    except FileNotFoundError:
        print("launch failed: codex CLI was not found", file=sys.stderr)
        return False
    if login.returncode != 0:
        print(f"launch failed: ChatGPT OAuth login exited with status {login.returncode}", file=sys.stderr)
        return False

    try:
        verified = subprocess.run(
            status_command,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        print("launch failed: codex CLI was not found", file=sys.stderr)
        return False
    if not _login_status_uses_chatgpt(verified):
        message = _safe_message(verified.stderr or verified.stdout)
        print(
            "launch failed: login did not produce a ChatGPT OAuth session: " + message,
            file=sys.stderr,
        )
        return False
    return True


def _login_status_uses_chatgpt(result: subprocess.CompletedProcess[str]) -> bool:
    message = f"{result.stdout or ''}\n{result.stderr or ''}".casefold()
    return result.returncode == 0 and "logged in using chatgpt" in message


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


def _codex_env(codex_home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(codex_home)
    for name in AUTH_ENVIRONMENT_VARIABLES:
        env.pop(name, None)
    return env


def _command_for_display(parts: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def _safe_message(value: str, secret: str | None = None) -> str:
    message = value.strip() or "unknown error"
    if secret:
        message = message.replace(secret, "[REDACTED]")
    return message.replace("\r", " ").replace("\n", " ")[:500]


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


if __name__ == "__main__":
    raise SystemExit(main())
