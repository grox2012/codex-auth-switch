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


def test_single_resets_tokenfactory_and_clears_shared_login(monkeypatch, tmp_path, capsys) -> None:
    account_config = tmp_path / "accounts.json"
    account_config.write_text(
        '{"model":"gpt-5.6-sol","sources":[{"source_id":"a",'
        '"label":"Account A","onepassword_ref":"op://vault/a/token"}]}',
        encoding="utf-8",
    )
    codex_config = tmp_path / "config.toml"
    codex_config.write_text(
        'model = "gpt-5.6-sol"\n'
        'model_provider = "tokenfactory"\n\n'
        "# Managed by codex-auth-switch for TokenFactory.\n"
        f'model_catalog_json = "{tmp_path / "models_cache.json"}"\n\n'
        "# Managed by codex-auth-switch.\n"
        "[model_providers.tokenfactory]\n"
        'base_url = "http://127.0.0.1:8080/v1"\n'
        'wire_api = "responses"\n\n'
        "[notice]\n"
        "hide_rate_limit_model_nudge = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "inherited-access-token")
    monkeypatch.setenv("OPENAI_API_KEY", "inherited-api-key")
    monkeypatch.setenv("CODEX_API_KEY", "inherited-codex-api-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.setenv("OPENAI_FEDERATION_RULE_ID", "inherited-federation-rule")
    monkeypatch.setenv("OPENAI_IDENTITY_TOKEN_FILE", str(tmp_path / "identity-token"))

    def fake_run(args, **kwargs):
        assert args == ["codex", "logout"]
        assert kwargs["env"]["CODEX_HOME"] == str(tmp_path)
        for name in (
            "CODEX_ACCESS_TOKEN",
            "OPENAI_API_KEY",
            "CODEX_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_FEDERATION_RULE_ID",
            "OPENAI_IDENTITY_TOKEN_FILE",
        ):
            assert name not in kwargs["env"]
        payload = tomllib.loads(codex_config.read_text(encoding="utf-8"))
        assert "model_provider" not in payload
        assert "model_catalog_json" not in payload
        assert "model_providers" not in payload
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
    payload = tomllib.loads(codex_config.read_text(encoding="utf-8"))
    assert payload["model"] == "gpt-5.6-sol"
    assert payload["notice"]["hide_rate_limit_model_nudge"] is True
    assert "model_provider" not in payload
    assert "model_catalog_json" not in payload
    assert "model_providers" not in payload
    output = capsys.readouterr().out
    assert "Saved shared login: cleared" in output
    assert "Sign in with ChatGPT" in output


def test_launch_replaces_personal_access_token_with_chatgpt_oauth(
    monkeypatch, tmp_path
) -> None:
    account_config = tmp_path / "accounts.json"
    account_config.write_text(
        '{"model":"gpt-5.6-sol","sources":[{"source_id":"a",'
        '"label":"Account A","onepassword_ref":"op://vault/a/token"}]}',
        encoding="utf-8",
    )
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    (isolated_home / "config.toml").write_text(
        'model_provider = "tokenfactory"\n\n'
        "# Managed by codex-auth-switch.\n"
        "[model_providers.tokenfactory]\n"
        'base_url = "http://127.0.0.1:8080/v1"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("codex_auth_switch.cli._codex_home", lambda source_id: isolated_home)
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "inherited-access-token")
    monkeypatch.setenv("OPENAI_API_KEY", "inherited-api-key")
    monkeypatch.setenv("CODEX_API_KEY", "inherited-codex-api-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.setenv("OPENAI_FEDERATION_RULE_ID", "inherited-federation-rule")
    monkeypatch.setenv("OPENAI_IDENTITY_TOKEN_FILE", str(tmp_path / "identity-token"))

    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        assert kwargs["env"]["CODEX_HOME"] == str(isolated_home)
        for name in (
            "CODEX_ACCESS_TOKEN",
            "OPENAI_API_KEY",
            "CODEX_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_FEDERATION_RULE_ID",
            "OPENAI_IDENTITY_TOKEN_FILE",
        ):
            assert name not in kwargs["env"]
        if args[-2:] == ["login", "status"]:
            status_count = sum(call[-2:] == ["login", "status"] for call in calls)
            message = (
                "Logged in using personal access token"
                if status_count == 1
                else "Logged in using ChatGPT"
            )
            return subprocess.CompletedProcess(args, 0, message, "")
        if args[-1:] == ["logout"]:
            return subprocess.CompletedProcess(args, 0, "Logged out", "")
        if args[-1:] == ["login"]:
            assert "input" not in kwargs
            return subprocess.CompletedProcess(args, 0, "", "")
        assert args == [
            "codex",
            "--no-daemon",
            "-c",
            'cli_auth_credentials_store="file"',
            "exec",
            "hello",
        ]
        return subprocess.CompletedProcess(args, 7, "", "")

    monkeypatch.setattr("codex_auth_switch.cli.subprocess.run", fake_run)

    result = main(
        ["launch", "--config", str(account_config), "a", "--", "exec", "hello"]
    )

    assert result == 7
    auth_prefix = ["codex", "-c", 'cli_auth_credentials_store="file"']
    assert calls == [
        [*auth_prefix, "login", "status"],
        [*auth_prefix, "logout"],
        [*auth_prefix, "login"],
        [*auth_prefix, "login", "status"],
        [
            "codex",
            "--no-daemon",
            "-c",
            'cli_auth_credentials_store="file"',
            "exec",
            "hello",
        ],
    ]
    payload = tomllib.loads((isolated_home / "config.toml").read_text(encoding="utf-8"))
    assert "model_provider" not in payload
    assert "model_providers" not in payload


def test_launch_reuses_existing_chatgpt_oauth(monkeypatch, tmp_path) -> None:
    account_config = tmp_path / "accounts.json"
    account_config.write_text(
        '{"model":"gpt-5.6-sol","sources":[{"source_id":"a",'
        '"label":"Account A","onepassword_ref":"op://vault/a/token"}]}',
        encoding="utf-8",
    )
    isolated_home = tmp_path / "isolated-home"
    monkeypatch.setattr("codex_auth_switch.cli._codex_home", lambda source_id: isolated_home)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[-2:] == ["login", "status"]:
            return subprocess.CompletedProcess(args, 0, "Logged in using ChatGPT", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("codex_auth_switch.cli.subprocess.run", fake_run)

    result = main(["launch", "--config", str(account_config), "a"])

    assert result == 0
    assert calls == [
        ["codex", "-c", 'cli_auth_credentials_store="file"', "login", "status"],
        ["codex", "--no-daemon", "-c", 'cli_auth_credentials_store="file"'],
    ]


def test_launch_refuses_non_chatgpt_login(monkeypatch, tmp_path, capsys) -> None:
    account_config = tmp_path / "accounts.json"
    account_config.write_text(
        '{"model":"gpt-5.6-sol","sources":[{"source_id":"a",'
        '"label":"Account A","onepassword_ref":"op://vault/a/token"}]}',
        encoding="utf-8",
    )
    isolated_home = tmp_path / "isolated-home"
    monkeypatch.setattr("codex_auth_switch.cli._codex_home", lambda source_id: isolated_home)
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[-2:] == ["login", "status"]:
            if len(calls) == 1:
                return subprocess.CompletedProcess(args, 1, "Not logged in", "")
            return subprocess.CompletedProcess(args, 0, "Logged in using personal access token", "")
        if args[-1:] == ["login"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr("codex_auth_switch.cli.subprocess.run", fake_run)

    result = main(["launch", "--config", str(account_config), "a"])

    assert result == 1
    assert "did not produce a ChatGPT OAuth session" in capsys.readouterr().err
    assert all("--no-daemon" not in call for call in calls)


def test_login_status_requires_positive_chatgpt_confirmation() -> None:
    from codex_auth_switch.cli import _login_status_uses_chatgpt

    ambiguous = subprocess.CompletedProcess(
        ["codex", "login", "status"],
        0,
        "Login is not ChatGPT",
        "",
    )

    assert not _login_status_uses_chatgpt(ambiguous)


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
