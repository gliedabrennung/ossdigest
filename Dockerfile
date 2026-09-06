FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY prompts ./prompts
COPY templates ./templates
COPY config ./config
COPY scripts ./scripts

RUN uv pip install --system --no-cache .

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/data/backups \
    && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1

CMD ["ossdigest-publisher"]
