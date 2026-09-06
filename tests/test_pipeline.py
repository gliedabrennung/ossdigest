from __future__ import annotations

import json

import httpx
import pytest
import respx

from ossdigest.config import HeuristicsConfig, JudgeConfig, ReadmeConfig, WriterConfig
from ossdigest.db import init_db
from ossdigest.github_client import GithubClient
from ossdigest.groq_client import GroqClient
from ossdigest.heuristics import compile_blocklist_patterns
from ossdigest.normalizer import ingest
from ossdigest.pipeline import DailyBudgetExceeded, PipelineContext, get_spent_today, process_candidate

GOOD_ASSESSMENT = {
    "is_usable_tool": True,
    "category": "cli",
    "usefulness": 8,
    "novelty": 8,
    "maturity": 8,
    "target_audience": "Бэкенд-разработчики",
    "red_flags": [],
    "one_liner_ru": "Инструмент для X.",
    "verdict": "publish",
    "reasoning": "ok",
}

GOOD_POST = {
    "title": "CoolTool",
    "body": "<b>CoolTool</b> решает задачу X без лишних зависимостей для разработчиков.",
    "bullets": ["Быстрый старт"],
    "tags": ["cli"],
    "insufficient_data": False,
}

README = "# CoolTool\n\nCoolTool is a real tool that solves a real problem for developers.\n" + ("x" * 400)


def groq_response(content: str):
    return httpx.Response(
        200,
        json={
            "id": "gen-1", "model": "test/model",
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 400, "completion_tokens": 100},
        },
    )


def _heuristics_config():
    return HeuristicsConfig(
        min_stars=120, min_desc_len=20, max_stale_days=60, require_license=True,
        max_stars_per_day=3000, readme_min_chars=100, readme_max_chars=200_000,
        blocked_languages=["Markdown"],
    )


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))


@pytest.fixture
def ctx_factory(tmp_path):
    def make(conn, bot=None, moderation_enabled=True, daily_budget_usd=10.0):
        return PipelineContext(
            conn=conn,
            github_client=GithubClient(token=""),
            groq_client=None,
            bot=bot,
            heuristics_config=_heuristics_config(),
            readme_config=ReadmeConfig(max_chars=4000, min_chars=100, strip_sections=[]),
            judge_config=JudgeConfig(
                model="test/judge", prompt_version="judge.v1", temperature=0.1, max_tokens=700,
                publish_threshold=7.0, hold_threshold=5.5, max_candidates_per_run=60,
            ),
            writer_config=WriterConfig(
                model="test/writer", prompt_version="writer.v1", temperature=0.5, max_tokens=900,
                target_chars=(400, 700), hard_max_chars=900,
            ),
            blocklist_patterns=compile_blocklist_patterns("config/blocklist.txt"),
            judge_system_prompt="system-judge",
            writer_system_prompt="system-writer",
            admin_ids=[111],
            moderation_enabled=moderation_enabled,
            daily_budget_usd=daily_budget_usd,
        )

    return make


async def _setup_repo_and_candidate_rows(conn, candidate):
    stats = ingest(conn, [candidate])
    repo = conn.execute(
        "SELECT id FROM repos WHERE host=? AND remote_id=?", (candidate.host, candidate.remote_id)
    ).fetchone()
    cand_rows = conn.execute(
        "SELECT id FROM candidates WHERE repo_id=?", (repo["id"],)
    ).fetchall()
    return repo["id"], [r["id"] for r in cand_rows]


@pytest.mark.asyncio
async def test_process_candidate_happy_path_creates_post_and_sends_moderation(tmp_path, candidate_factory, ctx_factory):
    conn = init_db(tmp_path / "t.db")
    candidate = candidate_factory(description="A real tool that solves a real developer problem end to end.")
    repo_id, cand_rows = await _setup_repo_and_candidate_rows(conn, candidate)

    bot = FakeBot()
    ctx = ctx_factory(conn, bot=bot)

    with respx.mock(base_url="https://api.github.com") as gh_mock, \
         respx.mock(base_url="https://api.groq.com/openai/v1") as groq_mock:
        gh_mock.get(f"/repos/{candidate.full_name}/readme").mock(
            return_value=httpx.Response(200, text=README)
        )
        groq_mock.post("/chat/completions").mock(
            side_effect=[groq_response(json.dumps(GOOD_ASSESSMENT)), groq_response(json.dumps(GOOD_POST))]
        )
        async with GroqClient(api_key="test") as client:
            ctx.groq_client = client
            outcome = await process_candidate(ctx, candidate, repo_id, cand_rows)

    assert outcome.heuristics_passed is True
    assert outcome.judged is True
    assert outcome.verdict == "publish"
    assert outcome.draft_created is True

    post = conn.execute("SELECT * FROM posts WHERE repo_id=?", (repo_id,)).fetchone()
    assert post is not None
    assert post["status"] == "pending"
    assert "CoolTool" in post["body_html"]

    assert len(bot.sent) == 1
    assert bot.sent[0][0] == 111


@pytest.mark.asyncio
async def test_process_candidate_rejected_by_heuristics_no_llm_calls(tmp_path, candidate_factory, ctx_factory):
    conn = init_db(tmp_path / "t.db")
    candidate = candidate_factory(stars=10)
    repo_id, cand_rows = await _setup_repo_and_candidate_rows(conn, candidate)

    ctx = ctx_factory(conn)
    with respx.mock(base_url="https://api.groq.com/openai/v1", assert_all_called=False) as groq_mock:
        route = groq_mock.post("/chat/completions")
        async with GroqClient(api_key="test") as client:
            ctx.groq_client = client
            outcome = await process_candidate(ctx, candidate, repo_id, cand_rows)

    assert outcome.heuristics_passed is False
    assert outcome.reject_reason == "too_few_stars"
    assert route.call_count == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM posts").fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_process_candidate_reused_judgement_skips_judge_call(tmp_path, candidate_factory, ctx_factory):
    conn = init_db(tmp_path / "t.db")
    candidate = candidate_factory(description="A real tool that solves a real developer problem end to end.")
    repo_id, cand_rows = await _setup_repo_and_candidate_rows(conn, candidate)
    ctx = ctx_factory(conn)

    with respx.mock(base_url="https://api.github.com") as gh_mock, \
         respx.mock(base_url="https://api.groq.com/openai/v1") as groq_mock:
        gh_mock.get(f"/repos/{candidate.full_name}/readme").mock(return_value=httpx.Response(200, text=README))
        judge_route = groq_mock.post("/chat/completions")
        judge_route.side_effect = [groq_response(json.dumps(GOOD_ASSESSMENT)), groq_response(json.dumps(GOOD_POST))]
        async with GroqClient(api_key="test") as client:
            ctx.groq_client = client
            await process_candidate(ctx, candidate, repo_id, cand_rows)

    with respx.mock(base_url="https://api.github.com", assert_all_called=False) as gh_mock2, \
         respx.mock(base_url="https://api.groq.com/openai/v1", assert_all_called=False) as groq_mock2:
        gh_mock2.get(f"/repos/{candidate.full_name}/readme").mock(return_value=httpx.Response(200, text=README))
        route2 = groq_mock2.post("/chat/completions")
        async with GroqClient(api_key="test") as client2:
            ctx.groq_client = client2
            outcome2 = await process_candidate(ctx, candidate, repo_id, cand_rows)

    assert outcome2.skipped_active_post is True
    assert route2.call_count == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM posts").fetchone()["n"] == 1


@pytest.mark.asyncio
async def test_process_candidate_stops_on_budget_exceeded(tmp_path, candidate_factory, ctx_factory):
    conn = init_db(tmp_path / "t.db")
    candidate = candidate_factory(description="A real tool that solves a real developer problem end to end.")
    repo_id, cand_rows = await _setup_repo_and_candidate_rows(conn, candidate)
    ctx = ctx_factory(conn, daily_budget_usd=0.0)

    with respx.mock(base_url="https://api.github.com") as gh_mock, \
         respx.mock(base_url="https://api.groq.com/openai/v1", assert_all_called=False) as groq_mock:
        gh_mock.get(f"/repos/{candidate.full_name}/readme").mock(return_value=httpx.Response(200, text=README))
        route = groq_mock.post("/chat/completions")
        async with GroqClient(api_key="test") as client:
            ctx.groq_client = client
            with pytest.raises(DailyBudgetExceeded):
                await process_candidate(ctx, candidate, repo_id, cand_rows)

    assert route.call_count == 0


def test_get_spent_today_sums_judgements_and_posts(tmp_path):
    conn = init_db(tmp_path / "t.db")
    assert get_spent_today(conn) == 0.0
