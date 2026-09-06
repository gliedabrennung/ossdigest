from __future__ import annotations

import json

import httpx
import pytest
import respx

from ossdigest.config import JudgeConfig
from ossdigest.judge import (
    JudgeAssessment,
    build_user_prompt,
    compute_final_score,
    judge_candidate,
    route_judgement,
)
from ossdigest.groq_client import (
    GroqCallFailed,
    GroqClient,
    GroqDailyLimitExceeded,
    parse_structured_json,
    strip_json_fences,
)

GOOD_ASSESSMENT = {
    "is_usable_tool": True,
    "category": "cli",
    "usefulness": 8,
    "novelty": 7,
    "maturity": 8,
    "target_audience": "Бэкенд-разработчики",
    "red_flags": [],
    "one_liner_ru": "Инструмент командной строки для X.",
    "verdict": "publish",
    "reasoning": "Решает реальную задачу, есть внятное отличие.",
}


def groq_response(content: str, *, prompt_tokens=500, completion_tokens=100):
    return httpx.Response(
        200,
        json={
            "id": "gen-1",
            "model": "test/model",
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
    )


@pytest.fixture
def judge_config():
    return JudgeConfig(
        model="test/cheap-model",
        fallback_model="test/fallback-model",
        prompt_version="judge.v1",
        temperature=0.1,
        max_tokens=700,
        publish_threshold=7.0,
        hold_threshold=5.5,
        growth_bonus_threshold=300,
        hn_points_bonus_threshold=150,
    )


def test_strip_json_fences():
    assert strip_json_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_json_fences('{"a": 1}') == '{"a": 1}'


def test_parse_structured_json_from_fenced():
    data = parse_structured_json('```json\n{"a": 1}\n```')
    assert data == {"a": 1}


@pytest.mark.parametrize(
    "verdict,score,expected_route,expected_attention",
    [
        ("reject", 9.0, "reject", False),
        ("publish", 8.0, "publish", False),
        ("publish", 6.0, "publish", True),
        ("hold", 9.0, "publish", True),
        ("hold", 2.0, "publish", True),
        ("publish", 3.0, "reject", False),
    ],
)
def test_route_judgement(verdict, score, expected_route, expected_attention):
    route, needs_attention = route_judgement(
        verdict, score, publish_threshold=7.0, hold_threshold=5.5
    )
    assert route == expected_route
    assert needs_attention == expected_attention


def test_compute_final_score_base():
    a = JudgeAssessment(**GOOD_ASSESSMENT)
    score = compute_final_score(
        a, stars_delta_7d=None, hn_points=None, source="github_search",
        growth_bonus_threshold=300, hn_points_bonus_threshold=150,
    )
    assert score == pytest.approx(0.45 * 8 + 0.35 * 7 + 0.20 * 8)


def test_compute_final_score_modifiers():
    a = JudgeAssessment(**{**GOOD_ASSESSMENT, "is_usable_tool": False, "red_flags": ["a", "b", "c", "d", "e"]})
    score = compute_final_score(
        a, stars_delta_7d=500, hn_points=200, source="hn",
        growth_bonus_threshold=300, hn_points_bonus_threshold=150,
    )
    base = 0.45 * 8 + 0.35 * 7 + 0.20 * 8
    expected = base - 2.0 - 2.0 + 0.5 + 0.5
    assert score == pytest.approx(expected)


def test_build_user_prompt_includes_key_fields(candidate_factory):
    c = candidate_factory(source="hn", source_meta={"hn_points": 214})
    prompt = build_user_prompt(c, readme_clean="Some cleaned readme.", stars_delta_7d=42)
    assert c.full_name in prompt
    assert "42" in prompt
    assert "Some cleaned readme." in prompt
    assert "hn_points=214" in prompt


@pytest.mark.asyncio
async def test_judge_candidate_happy_path(candidate_factory, judge_config):
    with respx.mock(base_url="https://api.groq.com/openai/v1") as mock:
        mock.post("/chat/completions").mock(
            return_value=groq_response(json.dumps(GOOD_ASSESSMENT))
        )
        async with GroqClient(api_key="test") as client:
            outcome = await judge_candidate(
                client, judge_config, candidate_factory(),
                readme_clean="readme", stars_delta_7d=None, system_prompt="system",
            )
    assert outcome.parse_failed is False
    assert outcome.route == "publish"
    assert outcome.needs_attention is False
    assert outcome.cost_usd == 0.0


@pytest.mark.asyncio
async def test_judge_candidate_handles_fenced_json(candidate_factory, judge_config):
    fenced = f"```json\n{json.dumps(GOOD_ASSESSMENT)}\n```"
    with respx.mock(base_url="https://api.groq.com/openai/v1") as mock:
        mock.post("/chat/completions").mock(return_value=groq_response(fenced))
        async with GroqClient(api_key="test") as client:
            outcome = await judge_candidate(
                client, judge_config, candidate_factory(),
                readme_clean="readme", stars_delta_7d=None, system_prompt="system",
            )
    assert outcome.parse_failed is False
    assert outcome.assessment.category == "cli"


@pytest.mark.asyncio
async def test_judge_candidate_retries_on_invalid_then_succeeds(candidate_factory, judge_config):
    bad = json.dumps({**GOOD_ASSESSMENT, "category": "not_a_real_category"})
    good = json.dumps(GOOD_ASSESSMENT)
    with respx.mock(base_url="https://api.groq.com/openai/v1") as mock:
        route = mock.post("/chat/completions")
        route.side_effect = [groq_response(bad), groq_response(good)]
        async with GroqClient(api_key="test") as client:
            outcome = await judge_candidate(
                client, judge_config, candidate_factory(),
                readme_clean="readme", stars_delta_7d=None, system_prompt="system",
            )
    assert outcome.parse_failed is False
    assert outcome.route == "publish"
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_judge_candidate_falls_back_to_hold_after_two_bad_responses(candidate_factory, judge_config):
    with respx.mock(base_url="https://api.groq.com/openai/v1") as mock:
        route = mock.post("/chat/completions")
        route.side_effect = [groq_response("not json at all"), groq_response("still not json")]
        async with GroqClient(api_key="test") as client:
            outcome = await judge_candidate(
                client, judge_config, candidate_factory(),
                readme_clean="readme", stars_delta_7d=None, system_prompt="system",
            )
    assert outcome.parse_failed is True
    assert outcome.assessment is None
    assert outcome.route == "publish"
    assert outcome.needs_attention is True


@pytest.mark.asyncio
async def test_groq_falls_back_to_fallback_model_when_primary_fails(candidate_factory, judge_config):
    with respx.mock(base_url="https://api.groq.com/openai/v1") as mock:
        mock.post("/chat/completions").mock(
            side_effect=[
                httpx.Response(401, text="invalid model"),
                groq_response(json.dumps(GOOD_ASSESSMENT)),
            ]
        )
        async with GroqClient(api_key="test", max_retries=0) as client:
            outcome = await judge_candidate(
                client, judge_config, candidate_factory(),
                readme_clean="readme", stars_delta_7d=None, system_prompt="system",
            )
    assert outcome.parse_failed is False
    assert outcome.route == "publish"


@pytest.mark.asyncio
async def test_groq_call_failed_raised_when_no_fallback_configured(candidate_factory, judge_config):
    judge_config.fallback_model = None
    with respx.mock(base_url="https://api.groq.com/openai/v1") as mock:
        mock.post("/chat/completions").mock(return_value=httpx.Response(401, text="invalid api key"))
        async with GroqClient(api_key="test", max_retries=0) as client:
            with pytest.raises(GroqCallFailed):
                await judge_candidate(
                    client, judge_config, candidate_factory(),
                    readme_clean="readme", stars_delta_7d=None, system_prompt="system",
                )


@pytest.mark.asyncio
async def test_groq_daily_limit_exceeded_raised_immediately_no_retry(candidate_factory, judge_config):
    judge_config.fallback_model = None
    with respx.mock(base_url="https://api.groq.com/openai/v1") as mock:
        route = mock.post("/chat/completions")
        route.mock(
            return_value=httpx.Response(
                429, text="rate limited", headers={"x-ratelimit-remaining-requests": "0"}
            )
        )
        async with GroqClient(api_key="test") as client:
            with pytest.raises(GroqDailyLimitExceeded):
                await judge_candidate(
                    client, judge_config, candidate_factory(),
                    readme_clean="readme", stars_delta_7d=None, system_prompt="system",
                )
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_groq_retries_on_429_then_succeeds(candidate_factory, judge_config):
    with respx.mock(base_url="https://api.groq.com/openai/v1") as mock:
        route = mock.post("/chat/completions")
        route.side_effect = [
            httpx.Response(429, text="rate limited"),
            groq_response(json.dumps(GOOD_ASSESSMENT)),
        ]
        async with GroqClient(api_key="test") as client:
            outcome = await judge_candidate(
                client, judge_config, candidate_factory(),
                readme_clean="readme", stars_delta_7d=None, system_prompt="system",
            )
    assert outcome.parse_failed is False
    assert route.call_count == 2
