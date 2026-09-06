from __future__ import annotations

import httpx
import structlog

logger = structlog.get_logger().bind(component="secrets_backend")

DEFAULT_PATH = "secret/data/ossdigest"

KEY_VAULT_SECRET_NAMES = {
    "telegram_bot_token": "telegram-bot-token",
    "telegram_channel_id": "telegram-channel-id",
    "telegram_admin_ids": "telegram-admin-ids",
    "github_token": "github-token",
    "groq_api_key": "groq-api-key",
}

OPTIONAL_KEY_VAULT_SECRET_NAMES = {
    "database_url": "database-url",
}

KEY_VAULT_API_VERSION = "7.4"


def fetch_secrets_from_openbao(addr: str, token: str, *, path: str = DEFAULT_PATH, timeout: float = 10.0) -> dict[str, str]:
    url = f"{addr.rstrip('/')}/v1/{path}"
    resp = httpx.get(url, headers={"X-Vault-Token": token}, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data", {}).get("data")
    if data is None:
        raise ValueError(f"OpenBao: секрет по пути {path} пуст или не найден")
    return data


def fetch_secrets_from_keyvault(vault_url: str, *, timeout: float = 10.0) -> dict[str, str]:
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()
    token = credential.get_token("https://vault.azure.net/.default").token
    headers = {"Authorization": f"Bearer {token}"}

    data: dict[str, str] = {}
    for field_name, secret_name in KEY_VAULT_SECRET_NAMES.items():
        url = f"{vault_url.rstrip('/')}/secrets/{secret_name}?api-version={KEY_VAULT_API_VERSION}"
        resp = httpx.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data[field_name] = resp.json()["value"]

    for field_name, secret_name in OPTIONAL_KEY_VAULT_SECRET_NAMES.items():
        url = f"{vault_url.rstrip('/')}/secrets/{secret_name}?api-version={KEY_VAULT_API_VERSION}"
        resp = httpx.get(url, headers=headers, timeout=timeout)
        if resp.status_code == 404:
            continue
        resp.raise_for_status()
        data[field_name] = resp.json()["value"]
    return data
