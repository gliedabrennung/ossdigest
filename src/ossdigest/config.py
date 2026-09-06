from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Secrets(BaseSettings):

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str = ""
    telegram_channel_id: str = ""
    telegram_admin_ids: str = ""
    github_token: str = ""
    gitlab_token: str = ""
    groq_api_key: str = ""
    database_path: str = "./data/bot.db"
    database_url: str = ""
    log_level: str = "INFO"
    sentry_dsn: str = ""

    openbao_addr: str = ""
    openbao_token: str = ""

    azure_key_vault_url: str = ""

    @property
    def admin_ids(self) -> list[int]:
        return [int(x) for x in self.telegram_admin_ids.split(",") if x.strip()]


class GithubSearchSource(BaseModel):
    enabled: bool = True
    weight: float = 1.0
    queries: list[str] = Field(default_factory=list)
    max_items: int = 120


class SimpleSource(BaseModel):
    enabled: bool = False
    weight: float = 1.0
    max_items: int = 50
    min_points: int | None = None
    boards: list[str] = Field(default_factory=list)


class SourcesConfig(BaseModel):
    github_search: GithubSearchSource = Field(default_factory=GithubSearchSource)
    github_trending_html: SimpleSource = Field(default_factory=SimpleSource)
    hacker_news: SimpleSource = Field(default_factory=SimpleSource)
    lobsters: SimpleSource = Field(default_factory=SimpleSource)
    gitlab: SimpleSource = Field(default_factory=SimpleSource)


class HeuristicsConfig(BaseModel):
    min_stars: int = 120
    min_desc_len: int = 20
    max_stale_days: int = 60
    require_license: bool = True
    max_stars_per_day: int = 3000
    readme_min_chars: int = 400
    readme_max_chars: int = 200_000
    blocked_languages: list[str] = Field(default_factory=list)
    blocklist_patterns_file: str = "config/blocklist.txt"


class ReadmeConfig(BaseModel):
    max_chars: int = 4000
    min_chars: int = 400
    strip_sections: list[str] = Field(default_factory=list)


class GroqConfig(BaseModel):
    base_url: str = "https://api.groq.com/openai/v1"
    daily_budget_usd: float = 1.50
    timeout_seconds: float = 90
    max_retries: int = 3


class JudgeConfig(BaseModel):
    model: str
    fallback_model: str | None = None
    prompt_version: str = "judge.v1"
    prompt_file: str = "prompts/judge.v1.txt"
    temperature: float = 0.1
    max_tokens: int = 700
    reasoning_effort: str = "low"
    publish_threshold: float = 7.0
    hold_threshold: float = 5.5
    max_candidates_per_run: int = 60
    growth_bonus_threshold: int = 300
    hn_points_bonus_threshold: int = 150


class WriterConfig(BaseModel):
    model: str
    prompt_version: str = "writer.v1"
    prompt_file: str = "prompts/writer.v1.txt"
    template_file: str = "templates/post.html.j2"
    temperature: float = 0.5
    max_tokens: int = 900
    reasoning_effort: str = "low"
    language: str = "ru"
    target_chars: tuple[int, int] = (400, 700)
    hard_max_chars: int = 900


class ModerationConfig(BaseModel):
    enabled: bool = True
    timeout_hours: int = 72
    timeout_action: Literal["auto_reject", "auto_approve"] = "auto_reject"


class PublisherConfig(BaseModel):
    timezone: str = "Asia/Almaty"
    slots: list[str] = Field(default_factory=lambda: ["10:00", "14:30", "19:00"])
    jitter_minutes: int = 12
    max_per_day: int = 3
    min_gap_minutes: int = 90
    skip_if_queue_empty: bool = True
    disable_web_page_preview: bool = False
    diversity_check: bool = True


class DedupConfig(BaseModel):
    repost_cooldown_days: int = 365


class AlertsConfig(BaseModel):
    queue_low_threshold: int = 3
    notify_admin_on_error: bool = True


class AppConfig(BaseModel):
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    heuristics: HeuristicsConfig = Field(default_factory=HeuristicsConfig)
    readme: ReadmeConfig = Field(default_factory=ReadmeConfig)
    groq: GroqConfig = Field(default_factory=GroqConfig)
    judge: JudgeConfig
    writer: WriterConfig
    moderation: ModerationConfig = Field(default_factory=ModerationConfig)
    publisher: PublisherConfig = Field(default_factory=PublisherConfig)
    dedup: DedupConfig = Field(default_factory=DedupConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)


def require_model_configured(model: str, field_name: str) -> None:
    if model.startswith("<"):
        raise ValueError(
            f"{field_name} не настроен: сверьте модели через "
            "GET /api/v1/models и укажите реальное имя в config.yaml"
        )


def load_config(path: str | Path = "config/config.yaml") -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return AppConfig.model_validate(raw)


def load_secrets() -> Secrets:
    secrets = Secrets()
    if secrets.openbao_addr and secrets.openbao_token:
        from ossdigest.secrets_backend import fetch_secrets_from_openbao

        data = fetch_secrets_from_openbao(secrets.openbao_addr, secrets.openbao_token)
        overrides = {k: v for k, v in data.items() if k in Secrets.model_fields and v}
        secrets = secrets.model_copy(update=overrides)
    elif secrets.azure_key_vault_url:
        from ossdigest.secrets_backend import fetch_secrets_from_keyvault

        data = fetch_secrets_from_keyvault(secrets.azure_key_vault_url)
        overrides = {k: v for k, v in data.items() if k in Secrets.model_fields and v}
        secrets = secrets.model_copy(update=overrides)
    return secrets
