from __future__ import annotations

import json

import httpx
import pytest
import respx

from ossdigest.config import WriterConfig
from ossdigest.judge import JudgeAssessment
from ossdigest.groq_client import GroqClient
from ossdigest.writer import render_post, write_post

GOOD_ASSESSMENT = JudgeAssessment(
    is_usable_tool=True,
    category="cli",
    usefulness=8,
    novelty=7,
    maturity=8,
    target_audience="Бэкенд-разработчики",
    red_flags=[],
    one_liner_ru="Инструмент командной строки для X.",
    verdict="publish",
    reasoning="ok",
)

GOOD_POST = {
    "title": "CoolTool",
    "body": "<b>CoolTool</b> решает задачу X для разработчиков без лишних зависимостей.",
    "bullets": ["Быстрый старт", "Минимум зависимостей"],
    "tags": ["cli", "devtools"],
    "insufficient_data": False,
}


def groq_response(content: str):
    return httpx.Response(
        200,
        json={
            "id": "gen-1",
            "model": "test/writer-model",
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 400, "completion_tokens": 150},
        },
    )


@pytest.fixture
def writer_config():
    return WriterConfig(
        model="test/writer-model",
        prompt_version="writer.v1",
        temperature=0.5,
        max_tokens=900,
        target_chars=(400, 700),
        hard_max_chars=900,
    )


def test_render_post_escapes_interpolated_fields():
    html_out = render_post(
        title="Cool & Tool <script>",
        body_html="<b>Body</b>",
        bullets=["A & B"],
        language="Go",
        license_spdx="MIT",
        stars=42,
        url="https://github.com/o/r",
        full_name="o/r",
    )
    assert "Cool &amp; Tool &lt;script&gt;" in html_out
    assert "A &amp; B" in html_out
    assert "<b>Body</b>" in html_out
    assert "o/r" in html_out


@pytest.mark.asyncio
async def test_write_post_happy_path(candidate_factory, writer_config):
    with respx.mock(base_url="https://api.groq.com/openai/v1") as mock:
        mock.post("/chat/completions").mock(return_value=groq_response(json.dumps(GOOD_POST)))
        async with GroqClient(api_key="test") as client:
            outcome = await write_post(
                client, writer_config, candidate_factory(),
                readme_clean="readme", stars_delta_7d=None,
                judge_assessment=GOOD_ASSESSMENT, system_prompt="system",
            )
    assert outcome.parse_failed is False
    assert outcome.insufficient_data is False
    assert outcome.post_html is not None
    assert "CoolTool" in outcome.post_html
    assert "<b>CoolTool</b>" in outcome.post_html


@pytest.mark.asyncio
async def test_write_post_insufficient_data(candidate_factory, writer_config):
    payload = {**GOOD_POST, "insufficient_data": True}
    with respx.mock(base_url="https://api.groq.com/openai/v1") as mock:
        mock.post("/chat/completions").mock(return_value=groq_response(json.dumps(payload)))
        async with GroqClient(api_key="test") as client:
            outcome = await write_post(
                client, writer_config, candidate_factory(),
                readme_clean="readme", stars_delta_7d=None,
                judge_assessment=GOOD_ASSESSMENT, system_prompt="system",
            )
    assert outcome.insufficient_data is True
    assert outcome.post_html is None


@pytest.mark.asyncio
async def test_write_post_autofixes_unclosed_tag(candidate_factory, writer_config):
    payload = {**GOOD_POST, "body": "<b>Unclosed bold tag describing the tool in detail"}
    with respx.mock(base_url="https://api.groq.com/openai/v1") as mock:
        mock.post("/chat/completions").mock(return_value=groq_response(json.dumps(payload)))
        async with GroqClient(api_key="test") as client:
            outcome = await write_post(
                client, writer_config, candidate_factory(),
                readme_clean="readme", stars_delta_7d=None,
                judge_assessment=GOOD_ASSESSMENT, system_prompt="system",
            )
    assert outcome.post_html is not None
    assert outcome.needs_attention is True
    assert "</b>" in outcome.post_html


@pytest.mark.asyncio
async def test_write_post_truncates_overlong_body(candidate_factory, writer_config):
    long_body = "Очень длинный текст поста. " * 60
    payload = {**GOOD_POST, "body": long_body}
    with respx.mock(base_url="https://api.groq.com/openai/v1") as mock:
        mock.post("/chat/completions").mock(return_value=groq_response(json.dumps(payload)))
        async with GroqClient(api_key="test") as client:
            outcome = await write_post(
                client, writer_config, candidate_factory(),
                readme_clean="readme", stars_delta_7d=None,
                judge_assessment=GOOD_ASSESSMENT, system_prompt="system",
            )
    assert outcome.needs_attention is True
    assert len(outcome.post_html) < len(long_body) + 200


@pytest.mark.asyncio
async def test_write_post_retries_on_bad_json_then_succeeds(candidate_factory, writer_config):
    with respx.mock(base_url="https://api.groq.com/openai/v1") as mock:
        route = mock.post("/chat/completions")
        route.side_effect = [groq_response("not json"), groq_response(json.dumps(GOOD_POST))]
        async with GroqClient(api_key="test") as client:
            outcome = await write_post(
                client, writer_config, candidate_factory(),
                readme_clean="readme", stars_delta_7d=None,
                judge_assessment=GOOD_ASSESSMENT, system_prompt="system",
            )
    assert outcome.parse_failed is False
    assert route.call_count == 2
