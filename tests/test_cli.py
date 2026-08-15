import subprocess
import tomllib

from codex_auth_switch.cli import main


def test_reset_does_not_load_account_config_or_run_external_commands(
    monkeypatch, tmp_path, capsys
) -> None:
    codex_config = tmp_path / "config.toml"
    codex_config.write_text('model_provider = "tokenfactory"\n', encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("reset must not invoke external commands")

    monkeypatch.setattr("subprocess.run", forbidden)
    result = main(
        [
            "--reset",
            "--config",
            str(tmp_path / "missing.json"),
            "--codex-config",
            str(codex_config),
        ]
    )

    assert result == 0
    assert "model_provider" not in tomllib.loads(codex_config.read_text(encoding="utf-8"))
    assert "default ChatGPT OAuth flow" in capsys.readouterr().out


def test_single_repairs_provider_before_codex_login(monkeypatch, tmp_path) -> None:
    account_config = tmp_path / "accounts.json"
    account_config.write_text(
        '{"model":"gpt-5.6-sol","sources":[{"source_id":"a",'
        '"label":"Account A","onepassword_ref":"op://vault/a/token"}]}',
        encoding="utf-8",
    )
    codex_config = tmp_path / "config.toml"
    codex_config.write_text('model_provider = "tokenfactory"\n', encoding="utf-8")

    def fake_run(args, **kwargs):
        if args[:3] == ["op", "read", "--no-newline"]:
            return subprocess.CompletedProcess(args, 0, "secret", "")
        assert args == ["codex", "login", "--with-access-token"]
        assert tomllib.loads(codex_config.read_text(encoding="utf-8"))["model_provider"] == (
            "openai"
        )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_auth_switch.cli.subprocess.run", fake_run)

    result = main(
        [
            "--single",
            "a",
            "--config",
            str(account_config),
            "--codex-config",
            str(codex_config),
        ]
    )

    assert result == 0


def test_tokenfactory_mode_writes_complete_provider(tmp_path) -> None:
    codex_config = tmp_path / "config.toml"

    result = main(
        [
            "--tokenfactory",
            "--skip-tokenfactory-health-check",
            "--codex-config",
            str(codex_config),
        ]
    )

    assert result == 0
    payload = tomllib.loads(codex_config.read_text(encoding="utf-8"))
    assert payload["model_provider"] == "tokenfactory"
    assert payload["model_providers"]["tokenfactory"]["wire_api"] == "responses"


def test_tokenfactory_mode_updates_existing_url_after_health_check(
    monkeypatch, tmp_path, capsys
) -> None:
    codex_config = tmp_path / "config.toml"
    codex_config.write_text(
        'model_provider = "openai"\n\n'
        "[model_providers.tokenfactory]\n"
        'base_url = "http://127.0.0.1:8080/v1"\n',
        encoding="utf-8",
    )
    checked = []
    monkeypatch.setattr(
        "codex_auth_switch.cli._check_tokenfactory_health",
        lambda url, *, timeout: checked.append((url, timeout)),
    )

    result = main(
        [
            "--tokenfactory",
            "--tokenfactory-url",
            "https://gateway.example.test:8443/v1",
            "--tokenfactory-health-timeout",
            "2.5",
            "--codex-config",
            str(codex_config),
        ]
    )

    assert result == 0
    assert checked == [("https://gateway.example.test:8443/v1", 2.5)]
    payload = tomllib.loads(codex_config.read_text(encoding="utf-8"))
    assert payload["model_provider"] == "tokenfactory"
    assert payload["model_providers"]["tokenfactory"]["base_url"] == (
        "https://gateway.example.test:8443/v1"
    )
    assert "TokenFactory API:" in capsys.readouterr().out


def test_failed_health_check_does_not_change_codex_config(monkeypatch, tmp_path, capsys) -> None:
    codex_config = tmp_path / "config.toml"
    original = 'model_provider = "openai"\n'
    codex_config.write_text(original, encoding="utf-8")

    def fail(*args, **kwargs):
        raise RuntimeError("gateway unavailable")

    monkeypatch.setattr("codex_auth_switch.cli._check_tokenfactory_health", fail)
    result = main(
        [
            "--tokenfactory",
            "--tokenfactory-url",
            "https://gateway.example.test:8443/v1",
            "--codex-config",
            str(codex_config),
        ]
    )

    assert result == 1
    assert codex_config.read_text(encoding="utf-8") == original
    assert "gateway unavailable" in capsys.readouterr().err
