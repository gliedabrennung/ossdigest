from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any

import jinja2
import structlog
from pydantic import BaseModel, ValidationError

from ossdigest.config import WriterConfig, require_model_configured
from ossdigest.html_sanitize import truncate_html_safely, validate_and_sanitize_html
from ossdigest.judge import JudgeAssessment, build_user_prompt
from ossdigest.models import Candidate
from ossdigest.groq_client import GroqClient, GroqResult, parse_structured_json

logger = structlog.get_logger().bind(component="writer")

TELEGRAM_MESSAGE_LIMIT = 4096
TARGET_POST_LENGTH = 1000


class WriterOutput(BaseModel):
    title: str
    body: str
    bullets: list[str]
    tags: list[str]
    insufficient_data: bool


WRITER_JSON_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "post_content",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "bullets": {"type": "array", "items": {"type": "string"}},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2-4 хештега без #, латиницей, snake_case",
                },
                "insufficient_data": {"type": "boolean"},
            },
            "required": ["title", "body", "bullets", "tags", "insufficient_data"],
            "additionalProperties": False,
        },
    },
}


def build_writer_user_prompt(
    candidate: Candidate,
    *,
    readme_clean: str,
    stars_delta_7d: int | None,
    one_liner_ru: str,
    target_audience: str,
) -> str:
    base = build_user_prompt(candidate, readme_clean=readme_clean, stars_delta_7d=stars_delta_7d)
    return (
        f"{base}\n\n"
        f"Оценка судьи — что это: {one_liner_ru}\n"
        f"Оценка судьи — кому полезно: {target_audience}"
    )


@dataclass
class WriterOutcome:
    post_html: str | None
    title: str | None
    tags: list[str]
    needs_attention: bool
    model_used: str
    prompt_version: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    insufficient_data: bool = False
    parse_failed: bool = False


_TEMPLATE_ENV = jinja2.Environment(
    loader=jinja2.FileSystemLoader("."),
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_post(
    *,
    title: str,
    body_html: str,
    bullets: list[str],
    language: str | None,
    license_spdx: str | None,
    stars: int,
    url: str,
    full_name: str,
    template_file: str = "templates/post.html.j2",
) -> str:
    template = _TEMPLATE_ENV.get_template(template_file)
    rendered = template.render(
        title=html.escape(title),
        body=body_html,
        bullets=[html.escape(b) for b in bullets],
        language=html.escape(language) if language else None,
        license=html.escape(license_spdx) if license_spdx else None,
        stars=stars,
        url=str(url),
        full_name=html.escape(full_name),
    )
    lines = [line for line in rendered.split("\n")]
    return "\n".join(lines).strip() + "\n"


async def write_post(
    client: GroqClient,
    config: WriterConfig,
    candidate: Candidate,
    *,
    readme_clean: str,
    stars_delta_7d: int | None,
    judge_assessment: JudgeAssessment,
    system_prompt: str,
) -> WriterOutcome:
    require_model_configured(config.model, "writer.model")

    user_prompt = build_writer_user_prompt(
        candidate,
        readme_clean=readme_clean,
        stars_delta_7d=stars_delta_7d,
        one_liner_ru=judge_assessment.one_liner_ru,
        target_audience=judge_assessment.target_audience,
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    result = await client.chat_completion(
        messages=messages,
        model=config.model,
        response_format=WRITER_JSON_SCHEMA,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        reasoning_effort=config.reasoning_effort,
    )

    output = _try_parse(result.content)
    if output is None:
        retry_messages = messages + [
            {
                "role": "user",
                "content": (
                    "Твой предыдущий ответ не прошёл валидацию по JSON-схеме. "
                    "Верни СТРОГО валидный JSON без пояснений и без markdown-фенсов, "
                    "точно соответствующий схеме post_content."
                ),
            }
        ]
        try:
            retry_result = await client.chat_completion(
                messages=retry_messages,
                model=config.model,
                response_format=WRITER_JSON_SCHEMA,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                reasoning_effort=config.reasoning_effort,
            )
        except Exception:
            logger.exception("writer_retry_call_failed", repo=candidate.full_name)
            retry_result = None

        if retry_result is not None:
            output = _try_parse(retry_result.content)
            result = _merge_usage(result, retry_result)

    if output is None:
        logger.error("writer_parse_failed_final", repo=candidate.full_name, raw=result.content[:300])
        return WriterOutcome(
            post_html=None, title=None, tags=[], needs_attention=True,
            model_used=result.model_used, prompt_version=config.prompt_version,
            tokens_in=result.tokens_in, tokens_out=result.tokens_out, cost_usd=result.cost_usd,
            parse_failed=True,
        )

    if output.insufficient_data:
        return WriterOutcome(
            post_html=None, title=output.title, tags=output.tags, needs_attention=False,
            model_used=result.model_used, prompt_version=config.prompt_version,
            tokens_in=result.tokens_in, tokens_out=result.tokens_out, cost_usd=result.cost_usd,
            insufficient_data=True,
        )

    body_sanitized = validate_and_sanitize_html(output.body, auto_close=False)
    needs_attention = False
    body_html = body_sanitized.html
    if body_sanitized.errors:
        logger.warning("writer_html_invalid_autofixed", repo=candidate.full_name, errors=body_sanitized.errors)
        body_html = validate_and_sanitize_html(output.body, auto_close=True).html
        needs_attention = True

    if len(body_html) > config.hard_max_chars:
        body_html = truncate_html_safely(output.body, config.hard_max_chars)
        needs_attention = True

    post_html = render_post(
        title=output.title,
        body_html=body_html,
        bullets=output.bullets,
        language=candidate.language,
        license_spdx=candidate.license_spdx,
        stars=candidate.stars,
        url=str(candidate.url),
        full_name=candidate.full_name,
        template_file=config.template_file,
    )

    if len(post_html) > TARGET_POST_LENGTH:
        needs_attention = True
    if len(post_html) > TELEGRAM_MESSAGE_LIMIT:
        overflow = len(post_html) - TELEGRAM_MESSAGE_LIMIT + 20
        body_html = truncate_html_safely(output.body, max(200, config.hard_max_chars - overflow))
        post_html = render_post(
            title=output.title, body_html=body_html, bullets=output.bullets,
            language=candidate.language, license_spdx=candidate.license_spdx,
            stars=candidate.stars, url=str(candidate.url), full_name=candidate.full_name,
            template_file=config.template_file,
        )
        needs_attention = True

    return WriterOutcome(
        post_html=post_html,
        title=output.title,
        tags=output.tags,
        needs_attention=needs_attention,
        model_used=result.model_used,
        prompt_version=config.prompt_version,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost_usd=result.cost_usd,
    )


def _try_parse(content: str) -> WriterOutput | None:
    try:
        data = parse_structured_json(content)
        return WriterOutput.model_validate(data)
    except (ValueError, ValidationError) as exc:
        logger.warning("writer_parse_attempt_failed", error=str(exc))
        return None


def _merge_usage(first: GroqResult, second: GroqResult) -> GroqResult:
    return GroqResult(
        content=second.content,
        model_used=second.model_used,
        tokens_in=first.tokens_in + second.tokens_in,
        tokens_out=first.tokens_out + second.tokens_out,
        cost_usd=first.cost_usd + second.cost_usd,
        raw_response=second.raw_response,
    )
