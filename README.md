# Open Source Digest Bot

Telegram-бот, который находит новые и набирающие популярность open source
инструменты, отбирает из них полезные (эвристики + LLM-судья на Groq),
пишет короткий пост на русском и публикует в канал по расписанию с ручной
модерацией.

## Архитектура

Два независимых процесса, связанных только через БД:

- **collector** (`ossdigest-collector`) — разовый запуск (cron, раз в сутки):
  собирает кандидатов из источников (GitHub Search, Hacker News, Lobsters,
  GitLab) → эвристики → очистка README → судья (Groq) → автор (Groq) →
  черновик в БД → отправка на модерацию в ЛС.
- **publisher** (`ossdigest-publisher`) — постоянно работающий процесс:
  обрабатывает кнопки модерации, публикует по расписанию из очереди
  `approved` (слоты/лимит в сутки — `publisher` в `config/config.yaml`).

БД — SQLite для локальной разработки или Postgres в проде (см. ниже), схема
одна и та же (`db/migrations/` — SQLite, `db/migrations_postgres/` — Postgres).

## Секреты

Три варианта, выбираются автоматически по тому, что задано в окружении
(порядок приоритета: OpenBao → Azure Key Vault → голые переменные из `.env`):

### 1. `.env` — локальная разработка

```bash
cp .env.example .env   # заполнить TELEGRAM_BOT_TOKEN, GITHUB_TOKEN, GROQ_API_KEY и т.д.
```

### 2. OpenBao — self-hosted (docker-compose)

Секреты (токены Telegram/GitHub/Groq) хранятся не в `.env`, а в
[OpenBao](https://openbao.org/) (Vault-совместимое open-source хранилище) —
контейнер поднимается тем же `docker-compose.yml`. Приложение получает
только read-only токен с доступом к одному пути `secret/data/ossdigest`.

```bash
docker compose up -d openbao
```

Разово создать `secrets/seed.json` (в `.gitignore`, не коммитится):

```json
{
  "telegram_bot_token": "...",
  "telegram_channel_id": "-100...",
  "telegram_admin_ids": "123456789",
  "github_token": "ghp_...",
  "groq_api_key": "gsk_..."
}
```

```bash
scripts/openbao_bootstrap.sh secrets/seed.json
```

Скрипт: инициализирует OpenBao (1 unseal key — single-node деплой),
включает KV v2, создаёт read-only политику и токен приложения, пишет
секреты. Результат:

- `secrets/openbao-keys.json` (chmod 600, **не теряй** — это unseal key +
  root token, без него секреты не расшифровать после перезапуска контейнера);
- `.env.openbao` (chmod 600) — токен приложения, подключается в
  `docker-compose.yml` как `env_file` у `collector`/`publisher`.

После бутстрапа удали `secrets/seed.json` — секреты уже в OpenBao.

Если OpenBao перезапущен и стал `sealed` (например, после `docker compose
down` без `-v` том сохраняется, но новый процесс стартует sealed) — просто
перезапусти `scripts/openbao_bootstrap.sh secrets/seed.json` ещё раз (он
не переинициализирует, только unseal + проверит секреты) либо вручную:
`curl -s --request POST http://127.0.0.1:8200/v1/sys/unseal --data "{\"key\":\"$(python3 -c "import json;print(json.load(open('secrets/openbao-keys.json'))['keys'][0])")\"}"`.

### 3. Azure Key Vault — облачный деплой

Для продакшена в Azure Container Apps: задать `AZURE_KEY_VAULT_URL`
(например `https://<vault>.vault.azure.net/`) и `AZURE_CLIENT_ID`
(managed identity с ролью *Key Vault Secrets User*). Секреты читаются той
же аутентификацией `DefaultAzureCredential` — без токенов в переменных
окружения. В Key Vault ожидаются секреты `telegram-bot-token`,
`telegram-channel-id`, `telegram-admin-ids`, `github-token`, `groq-api-key`
и опционально `database-url` (если он задан — приложение подключается к
Postgres вместо локального SQLite).

## Быстрый старт (без Docker)

```bash
uv sync --extra dev
cp .env.example .env   # заполнить токены (или использовать OpenBao/Key Vault, см. выше)
```

**Перед первым запуском** проверь `config/config.yaml`: `judge.model` и
`writer.model` заданы (`openai/gpt-oss-120b` на Groq — модель с надёжной
поддержкой `structured_outputs`/`json_schema strict`; `fallback_model:
openai/gpt-oss-20b` подхватывается при ошибке основной). Groq — бесплатный
dev-tier с лимитами запросов/токенов в минуту и в сутки на модель (см.
заголовки `x-ratelimit-*` в ответе) — при исчерпании коллектор
останавливается с алертом админам, ретраить не пытается.

```bash
uv run pytest                    # 126 тестов, все моки, без реальных API
uv run ossdigest-collector       # один прогон коллектора
uv run ossdigest-publisher       # постоянный процесс модерации/публикации
```

## Docker

```bash
docker compose up -d openbao publisher  # секреты + постоянный процесс
docker compose run --rm collector       # разовый прогон (повесить на host cron)
```

Коллектор стоит запускать за 3-4 часа до первого слота публикации, чтобы
модерация успевала (см. `publisher.slots` в `config/config.yaml`).

## Бэкапы

```bash
./scripts/backup.sh   # sqlite3 .backup, хранит 14 последних копий в data/backups/
```

Повесить на ежедневный host cron. Не нужно при Postgres — используй
стандартный бэкап управляемого сервиса (например Azure Backup для Flexible
Server).

## Золотой набор (приёмка)

1. Разметить `config/golden_set.csv` (не входит в репозиторий, размечается
   вручную владельцем канала): 30 репозиториев, которые он бы опубликовал,
   30 — точно нет.

   ```csv
   full_name,expected_publish
   owner/repo,1
   owner/other,0
   ```

2. Прогнать:

   ```bash
   uv run python -m scripts.eval_golden_set
   ```

   Требует реальных `GITHUB_TOKEN`/`GROQ_API_KEY` (бесплатно, но с лимитами
   запросов) — не мокается намеренно. Печатает precision/recall/ложные
   одобрения (цель: precision ≥ 0.80, recall ≥ 0.60, 0 ложных одобрений
   awesome-списков/учебных материалов).

## Структура репозитория

```
src/ossdigest/
  collector.py           процесс COLLECTOR
  publisher.py           процесс PUBLISHER (aiogram + планировщик)
  pipeline.py            связка эвристики → README → судья → автор → очередь
  sources/               github_search, hn, lobsters, gitlab (Source.fetch())
  heuristics.py          эвристический отбор до/после README
  readme_prep.py         очистка и обрезка README
  judge.py, writer.py    LLM-судья и генератор поста
  moderation.py          callback_data, атомарные переходы статусов
  dedup.py               механизмы дедупликации
  star_snapshots.py      снапшоты и дельта звёзд
  secrets_backend.py     чтение секретов из OpenBao / Azure Key Vault
  db/migrations/          пронумерованные .sql для SQLite
  db/migrations_postgres/ те же миграции для Postgres
config/config.yaml        все пороги и параметры, без хардкода
prompts/judge.v1.txt, writer.v1.txt   системные промпты, версионируются отдельно
templates/post.html.j2    шаблон финального сообщения
tests/                     respx/фикстуры, без обращений к реальным API
scripts/                    eval_golden_set.py, backup.sh, healthcheck.py, openbao_bootstrap.sh
ops/openbao/config.hcl      конфиг OpenBao (file storage, tls_disable)
```
