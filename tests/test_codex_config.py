import tomllib

from codex_auth_switch.codex_config import reset_codex_config, set_codex_model_provider


def test_tokenfactory_switch_defines_provider_before_selecting_it(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")

    set_codex_model_provider(path, "tokenfactory")

    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    assert payload["model_provider"] == "tokenfactory"
    assert payload["model_providers"]["tokenfactory"] == {
        "name": "TokenFactory local gateway",
        "base_url": "http://127.0.0.1:8080/v1",
        "wire_api": "responses",
    }


def test_reset_removes_managed_provider_and_preserves_unrelated_settings(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'model = "gpt-5.6-sol"\n'
        'model_provider = "tokenfactory"\n\n'
        '[model_providers.tokenfactory]\n'
        'base_url = "http://127.0.0.1:8080/v1"\n\n'
        '[model_providers.tokenfactory.auth]\n'
        'command = "token-command"\n\n'
        '[notice]\n'
        'hide_rate_limit_model_nudge = true\n',
        encoding="utf-8",
    )

    result = reset_codex_config(path)

    assert result.changed is True
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    assert payload["model"] == "gpt-5.6-sol"
    assert "model_provider" not in payload
    assert "model_providers" not in payload
    assert payload["notice"]["hide_rate_limit_model_nudge"] is True
    assert result.backup_path is not None and result.backup_path.exists()


def test_reset_is_idempotent(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")

    result = reset_codex_config(path)

    assert result.changed is False
    assert result.backup_path is None
