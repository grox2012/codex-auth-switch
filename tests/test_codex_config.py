import tomllib

import pytest

from codex_auth_switch.codex_config import (
    reset_codex_config,
    resolve_tokenfactory_base_url,
    set_codex_model_provider,
)


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
        "# Managed by codex-auth-switch for TokenFactory.\n"
        f'model_catalog_json = "{tmp_path / "models_cache.json"}"\n\n'
        "[model_providers.tokenfactory]\n"
        'base_url = "http://127.0.0.1:8080/v1"\n\n'
        "[model_providers.tokenfactory.auth]\n"
        'command = "token-command"\n\n'
        "[notice]\n"
        "hide_rate_limit_model_nudge = true\n",
        encoding="utf-8",
    )

    result = reset_codex_config(path)

    assert result.changed is True
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    assert payload["model"] == "gpt-5.6-sol"
    assert "model_provider" not in payload
    assert "model_catalog_json" not in payload
    assert "model_providers" not in payload
    assert payload["notice"]["hide_rate_limit_model_nudge"] is True
    assert result.backup_path is not None and result.backup_path.exists()


def test_reset_is_idempotent(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")

    result = reset_codex_config(path)

    assert result.changed is False
    assert result.backup_path is None


def test_explicit_tokenfactory_url_replaces_existing_base_url(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'model_provider = "tokenfactory"\n\n'
        "[model_providers.tokenfactory]\n"
        'name = "Custom display name"\n'
        'base_url = "http://127.0.0.1:8080/v1"\n'
        'wire_api = "responses"\n'
        "stream_max_retries = 2\n",
        encoding="utf-8",
    )

    set_codex_model_provider(
        path,
        "tokenfactory",
        tokenfactory_url="https://gateway.example.test:8443/v1/",
    )

    provider = tomllib.loads(path.read_text(encoding="utf-8"))["model_providers"]["tokenfactory"]
    assert provider == {
        "name": "Custom display name",
        "base_url": "https://gateway.example.test:8443/v1",
        "wire_api": "responses",
        "stream_max_retries": 2,
    }


def test_existing_tokenfactory_url_is_preserved_without_override(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[model_providers.tokenfactory]\nbase_url = "https://gateway.example.test:8443/v1"\n',
        encoding="utf-8",
    )

    set_codex_model_provider(path, "tokenfactory")

    assert (
        tomllib.loads(path.read_text(encoding="utf-8"))["model_providers"]["tokenfactory"][
            "base_url"
        ]
        == "https://gateway.example.test:8443/v1"
    )


def test_tokenfactory_switch_does_not_pin_model_catalog(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
    (tmp_path / "models_cache.json").write_text(
        '{"client_version":"0.147.0","models":[{"slug":"gpt-5.6-sol"}]}',
        encoding="utf-8",
    )

    set_codex_model_provider(path, "tokenfactory")

    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    assert "model_catalog_json" not in payload
    assert payload["model_provider"] == "tokenfactory"


def test_tokenfactory_switch_preserves_user_model_catalog(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('model_catalog_json = "/custom/catalog.json"\n', encoding="utf-8")
    (tmp_path / "models_cache.json").write_text(
        '{"models":[{"slug":"gpt-5.6-sol"}]}',
        encoding="utf-8",
    )

    set_codex_model_provider(path, "tokenfactory")
    set_codex_model_provider(path, "openai")

    assert tomllib.loads(path.read_text(encoding="utf-8"))["model_catalog_json"] == (
        "/custom/catalog.json"
    )


def test_switching_to_openai_removes_only_managed_model_catalog(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'model = "gpt-5.6-sol"\n'
        "# Managed by codex-auth-switch for TokenFactory.\n"
        'model_catalog_json = "/pinned/models_cache.json"\n',
        encoding="utf-8",
    )

    set_codex_model_provider(path, "openai")

    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    assert "model_catalog_json" not in payload
    assert payload["model_provider"] == "openai"


def test_tokenfactory_switch_removes_managed_model_catalog(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'model = "gpt-5.6-sol"\n'
        "# Managed by codex-auth-switch for TokenFactory.\n"
        'model_catalog_json = "/pinned/models_cache.json"\n',
        encoding="utf-8",
    )

    set_codex_model_provider(path, "tokenfactory")

    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    assert "model_catalog_json" not in payload
    assert payload["model_provider"] == "tokenfactory"


def test_windows_crlf_config_remains_valid_when_url_is_replaced(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(
        b'model_provider = "openai"\r\n\r\n'
        b"[model_providers.tokenfactory]\r\n"
        b'base_url = "http://127.0.0.1:8080/v1"\r\n'
    )

    set_codex_model_provider(
        path,
        "tokenfactory",
        tokenfactory_url="https://gateway.example.test:8443/v1",
    )

    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    assert payload["model_provider"] == "tokenfactory"
    assert payload["model_providers"]["tokenfactory"]["wire_api"] == "responses"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080",
        "ftp://gateway.example.test/v1",
        "https://user:secret@gateway.example.test/v1",
        "https://gateway.example.test/v1?token=secret",
    ],
)
def test_invalid_tokenfactory_url_is_rejected(tmp_path, url) -> None:
    with pytest.raises(ValueError, match="must be an http"):
        resolve_tokenfactory_base_url(tmp_path / "config.toml", url)
