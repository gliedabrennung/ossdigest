from __future__ import annotations

import httpx
import pytest
import respx
from azure.identity import DefaultAzureCredential
from azure.core.credentials import AccessToken

from ossdigest.config import load_secrets
from ossdigest.secrets_backend import fetch_secrets_from_keyvault, fetch_secrets_from_openbao


def test_fetch_secrets_from_openbao_happy_path():
    with respx.mock(base_url="http://openbao:8200") as mock:
        mock.get("/v1/secret/data/ossdigest").mock(
            return_value=httpx.Response(
                200,
                json={"data": {"data": {"telegram_bot_token": "abc", "github_token": "def"}, "metadata": {}}},
            )
        )
        data = fetch_secrets_from_openbao("http://openbao:8200", "test-token")

    assert data == {"telegram_bot_token": "abc", "github_token": "def"}


def test_fetch_secrets_from_openbao_sends_token_header():
    with respx.mock(base_url="http://openbao:8200") as mock:
        route = mock.get("/v1/secret/data/ossdigest").mock(
            return_value=httpx.Response(200, json={"data": {"data": {"a": "b"}}})
        )
        fetch_secrets_from_openbao("http://openbao:8200", "my-token")

    assert route.calls.last.request.headers["X-Vault-Token"] == "my-token"


def test_fetch_secrets_from_openbao_raises_on_missing_data():
    with respx.mock(base_url="http://openbao:8200") as mock:
        mock.get("/v1/secret/data/ossdigest").mock(return_value=httpx.Response(200, json={"data": {}}))
        with pytest.raises(ValueError):
            fetch_secrets_from_openbao("http://openbao:8200", "token")


def test_fetch_secrets_from_openbao_raises_on_http_error():
    with respx.mock(base_url="http://openbao:8200") as mock:
        mock.get("/v1/secret/data/ossdigest").mock(return_value=httpx.Response(403, json={"errors": ["permission denied"]}))
        with pytest.raises(httpx.HTTPStatusError):
            fetch_secrets_from_openbao("http://openbao:8200", "bad-token")


def test_load_secrets_merges_openbao_values(monkeypatch):
    monkeypatch.setenv("OPENBAO_ADDR", "http://openbao:8200")
    monkeypatch.setenv("OPENBAO_TOKEN", "app-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-env-should-be-overridden")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    with respx.mock(base_url="http://openbao:8200") as mock:
        mock.get("/v1/secret/data/ossdigest").mock(
            return_value=httpx.Response(
                200,
                json={"data": {"data": {
                    "telegram_bot_token": "from-openbao",
                    "github_token": "gh-from-openbao",
                }}},
            )
        )
        secrets = load_secrets()

    assert secrets.telegram_bot_token == "from-openbao"
    assert secrets.github_token == "gh-from-openbao"
    assert secrets.log_level == "DEBUG"


def test_load_secrets_without_openbao_env_uses_plain_env(monkeypatch):
    monkeypatch.delenv("OPENBAO_ADDR", raising=False)
    monkeypatch.delenv("OPENBAO_TOKEN", raising=False)
    monkeypatch.delenv("AZURE_KEY_VAULT_URL", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "plain-env-token")

    secrets = load_secrets()
    assert secrets.telegram_bot_token == "plain-env-token"


def _mock_get_token(monkeypatch):
    monkeypatch.setattr(
        DefaultAzureCredential, "get_token",
        lambda self, *scopes, **kwargs: AccessToken("fake-token", 9999999999),
    )


def test_fetch_secrets_from_keyvault_happy_path(monkeypatch):
    _mock_get_token(monkeypatch)
    with respx.mock(base_url="https://myvault.vault.azure.net") as mock:
        mock.get("/secrets/telegram-bot-token", params={"api-version": "7.4"}).mock(
            return_value=httpx.Response(200, json={"value": "tg-token"})
        )
        mock.get("/secrets/telegram-channel-id", params={"api-version": "7.4"}).mock(
            return_value=httpx.Response(200, json={"value": "-100123"})
        )
        mock.get("/secrets/telegram-admin-ids", params={"api-version": "7.4"}).mock(
            return_value=httpx.Response(200, json={"value": "111"})
        )
        mock.get("/secrets/github-token", params={"api-version": "7.4"}).mock(
            return_value=httpx.Response(200, json={"value": "ghp_x"})
        )
        mock.get("/secrets/groq-api-key", params={"api-version": "7.4"}).mock(
            return_value=httpx.Response(200, json={"value": "gsk_x"})
        )
        mock.get("/secrets/database-url", params={"api-version": "7.4"}).mock(
            return_value=httpx.Response(404, json={"error": "not found"})
        )
        data = fetch_secrets_from_keyvault("https://myvault.vault.azure.net")

    assert data == {
        "telegram_bot_token": "tg-token",
        "telegram_channel_id": "-100123",
        "telegram_admin_ids": "111",
        "github_token": "ghp_x",
        "groq_api_key": "gsk_x",
    }


def test_fetch_secrets_from_keyvault_includes_optional_database_url_when_present(monkeypatch):
    _mock_get_token(monkeypatch)
    with respx.mock(base_url="https://myvault.vault.azure.net", assert_all_called=False) as mock:
        mock.get("/secrets/database-url", params={"api-version": "7.4"}).mock(
            return_value=httpx.Response(200, json={"value": "postgresql://u:p@host/db"})
        )
        mock.get(url__regex=r".*").mock(return_value=httpx.Response(200, json={"value": "x"}))
        data = fetch_secrets_from_keyvault("https://myvault.vault.azure.net")

    assert data["database_url"] == "postgresql://u:p@host/db"


def test_fetch_secrets_from_keyvault_sends_bearer_token(monkeypatch):
    _mock_get_token(monkeypatch)
    with respx.mock(base_url="https://myvault.vault.azure.net", assert_all_called=False) as mock:
        route = mock.get("/secrets/telegram-bot-token", params={"api-version": "7.4"}).mock(
            return_value=httpx.Response(200, json={"value": "tg-token"})
        )
        mock.get(url__regex=r".*").mock(return_value=httpx.Response(200, json={"value": "x"}))
        fetch_secrets_from_keyvault("https://myvault.vault.azure.net")

    assert route.calls.last.request.headers["Authorization"] == "Bearer fake-token"


def test_fetch_secrets_from_keyvault_raises_on_http_error(monkeypatch):
    _mock_get_token(monkeypatch)
    with respx.mock(base_url="https://myvault.vault.azure.net") as mock:
        mock.get(url__regex=r".*").mock(return_value=httpx.Response(403, json={"error": "forbidden"}))
        with pytest.raises(httpx.HTTPStatusError):
            fetch_secrets_from_keyvault("https://myvault.vault.azure.net")


def test_load_secrets_merges_keyvault_values(monkeypatch):
    monkeypatch.delenv("OPENBAO_ADDR", raising=False)
    monkeypatch.delenv("OPENBAO_TOKEN", raising=False)
    monkeypatch.setenv("AZURE_KEY_VAULT_URL", "https://myvault.vault.azure.net")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-env-should-be-overridden")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    _mock_get_token(monkeypatch)

    with respx.mock(base_url="https://myvault.vault.azure.net") as mock:
        mock.get(url__regex=r".*telegram-bot-token.*").mock(return_value=httpx.Response(200, json={"value": "from-keyvault"}))
        mock.get(url__regex=r".*").mock(return_value=httpx.Response(200, json={"value": "x"}))
        secrets = load_secrets()

    assert secrets.telegram_bot_token == "from-keyvault"
    assert secrets.log_level == "DEBUG"
