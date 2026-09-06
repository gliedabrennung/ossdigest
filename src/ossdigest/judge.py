from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

import structlog
from pydantic import BaseModel, ValidationError

from ossdigest.config import JudgeConfig, require_model_configured
from ossdigest.models import Candidate
from ossdigest.groq_client import (
    GroqClient,
    GroqResult,
    parse_structured_json,
)

logger = structlog.get_logger().bind(component="judge")

Category = Literal[
    "library", "cli", "desktop_app", "web_app", "framework",
    "infrastructure", "database", "dataset", "model",
    "learning_material", "link_list", "other",
]
Verdict = Literal["publish", "hold", "reject"]


class JudgeAssessment(BaseModel):
    is_usable_tool: bool
    category: Category
    usefulness: int
    novelty: int
    maturity: int
    target_audience: str
    red_flags: list[str]
    one_liner_ru: str
    verdict: Verdict
    reasoning: str


JUDGE_JSON_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "repo_assessment",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "is_usable_tool": {
                    "type": "boolean",
                    "description": (
                        "true, если это работающий инструмент, библиотека или "
                        "приложение, которое разработчик может установить и "
                        "использовать. false для списков ссылок, учебных "
                        "материалов, конфигов, демо и заготовок."
                    ),
                },
                "category": {"type": "string", "enum": list(get_args(Category))},
                "usefulness": {
                    "type": "integer",
                    "description": "1-10. Насколько это решает реальную задачу разработчика.",
                },
                "novelty": {
                    "type": "integer",
                    "description": (
                        "1-10. Насколько отличается от уже существующих решений. "
                        "Тонкая обёртка над популярным API = 2-3."
                    ),
                },
                "maturity": {
                    "type": "integer",
                    "description": (
                        "1-10. Признаки готовности: документация, тесты, релизы, "
                        "отсутствие 'WIP'."
                    ),
                },
                "target_audience": {
                    "type": "string",
                    "description": "Кому полезно, одной фразой на русском.",
                },
                "red_flags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Проблемы: заброшен, только демо, хайп без содержания, "
                        "вендор-лок, накрученные звёзды."
                    ),
                },
                "one_liner_ru": {
                    "type": "string",
                    "description": "Что это, одним предложением на русском, без маркетинговых прилагательных.",
                },
                "verdict": {"type": "string", "enum": ["publish", "hold", "reject"]},
                "reasoning": {
                    "type": "string",
                    "description": "2-3 предложения обоснования на русском.",
                },
            },
            "required": [
                "is_usable_tool", "category", "usefulness", "novelty",
                "maturity", "target_audience", "red_flags",
                "one_liner_ru", "verdict", "reasoning",
            ],
            "additionalProperties": False,
        },
    },
}


def load_prompt(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def build_user_prompt(
    candidate: Candidate,
    *,
    readme_clean: str,
    stars_delta_7d: int | None,
) -> str:
    source_meta_short = ", ".join(f"{k}={v}" for k, v in (candidate.source_meta or {}).items())
    delta_str = str(stars_delta_7d) if stars_delta_7d is not None else "н/д"
    return f"""Репозиторий: {candidate.full_name}
Ссылка: {candidate.url}
Описание: {candidate.description or "—"}
Язык: {candidate.language or "—"}
Лицензия: {candidate.license_spdx or "—"}
Звёзд: {candidate.stars} (прирост за 7 дней: {delta_str})
Форков: {candidate.forks}
Открытых issues: {candidate.open_issues}
Создан: {candidate.created_at.date()}
Последний коммит: {candidate.pushed_at.date()}
Топики: {", ".join(candidate.topics) or "—"}
Источник: {candidate.source} {source_meta_short}

README (очищенный, обрезанный):
---
{readme_clean}
---"""


@dataclass
class JudgeOutcome:
    assessment: JudgeAssessment | None
    final_score: float
    route: Literal["publish", "reject"]
    needs_attention: bool
    model_used: str
    prompt_version: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    raw_response: str
    parse_failed: bool = False
    reject_reason: str | None = None


def compute_final_score(
    assessment: JudgeAssessment,
    *,
    stars_delta_7d: int | None,
    hn_points: int | None,
    source: str,
    growth_bonus_threshold: int,
    hn_points_bonus_threshold: int,
) -> float:
    score = 0.45 * assessment.usefulness + 0.35 * assessment.novelty + 0.20 * assessment.maturity
    if not assessment.is_usable_tool:
        score -= 2.0
    score -= min(len(assessment.red_flags) * 0.5, 2.0)
    if stars_delta_7d is not None and stars_delta_7d > growth_bonus_threshold:
        score += 0.5
    if source == "hn" and hn_points is not None and hn_points > hn_points_bonus_threshold:
        score += 0.5
    return score


def route_judgement(
    verdict: Verdict, final_score: float, *, publish_threshold: float, hold_threshold: float
) -> tuple[Literal["publish", "reject"], bool]:
    if verdict == "reject":
        return "reject", False
    if verdict == "publish" and final_score >= publish_threshold:
        return "publish", False
    if verdict == "hold" or (hold_threshold <= final_score < publish_threshold):
        return "publish", True
    return "reject", False


async def judge_candidate(
    client: GroqClient,
    config: JudgeConfig,
    candidate: Candidate,
    *,
    readme_clean: str,
    stars_delta_7d: int | None,
    system_prompt: str,
) -> JudgeOutcome:
    require_model_configured(config.model, "judge.model")

    hn_points = (candidate.source_meta or {}).get("hn_points")
    user_prompt = build_user_prompt(candidate, readme_clean=readme_clean, stars_delta_7d=stars_delta_7d)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    result = await client.chat_completion(
        messages=messages,
        model=config.model,
        fallback_model=config.fallback_model,
        response_format=JUDGE_JSON_SCHEMA,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        reasoning_effort=config.reasoning_effort,
    )

    assessment = _try_parse(result.content)
    if assessment is None:
        strict_messages = messages + [
            {
                "role": "user",
                "content": (
                    "Твой предыдущий ответ не прошёл валидацию по JSON-схеме. "
                    "Верни СТРОГО валидный JSON без пояснений, без markdown-фенсов, "
                    "точно соответствующий схеме repo_assessment."
                ),
            }
        ]
        try:
            retry_result = await client.chat_completion(
                messages=strict_messages,
                model=config.model,
                fallback_model=config.fallback_model,
                response_format=JUDGE_JSON_SCHEMA,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                reasoning_effort=config.reasoning_effort,
            )
        except Exception:
            logger.exception("judge_retry_call_failed", repo=candidate.full_name)
            retry_result = None

        if retry_result is not None:
            assessment = _try_parse(retry_result.content)
            result = _merge_usage(result, retry_result)

    if assessment is None:
        logger.error("judge_parse_failed_final", repo=candidate.full_name, raw=result.content[:300])
        return JudgeOutcome(
            assessment=None,
            final_score=0.0,
            route="publish",
            needs_attention=True,
            model_used=result.model_used,
            prompt_version=config.prompt_version,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=result.cost_usd,
            raw_response=result.content,
            parse_failed=True,
        )

    final_score = compute_final_score(
        assessment,
        stars_delta_7d=stars_delta_7d,
        hn_points=hn_points,
        source=candidate.source,
        growth_bonus_threshold=config.growth_bonus_threshold,
        hn_points_bonus_threshold=config.hn_points_bonus_threshold,
    )
    route, needs_attention = route_judgement(
        assessment.verdict,
        final_score,
        publish_threshold=config.publish_threshold,
        hold_threshold=config.hold_threshold,
    )

    return JudgeOutcome(
        assessment=assessment,
        final_score=final_score,
        route=route,
        needs_attention=needs_attention,
        model_used=result.model_used,
        prompt_version=config.prompt_version,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost_usd=result.cost_usd,
        raw_response=result.content,
        reject_reason=None if route == "publish" else f"judge_verdict_{assessment.verdict}",
    )


def _try_parse(content: str) -> JudgeAssessment | None:
    try:
        data = parse_structured_json(content)
        return JudgeAssessment.model_validate(data)
    except (ValueError, ValidationError) as exc:
        logger.warning("judge_parse_attempt_failed", error=str(exc))
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
