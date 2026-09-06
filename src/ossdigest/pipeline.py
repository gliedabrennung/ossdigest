from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import structlog
from aiogram import Bot

from ossdigest.alerts import notify_admins
from ossdigest.config import HeuristicsConfig, JudgeConfig, ReadmeConfig, WriterConfig
from ossdigest.dedup import (
    find_norm_name_duplicates,
    has_active_post,
    is_already_posted,
    is_owner_blocked,
)
from ossdigest.github_client import GithubClient
from ossdigest.heuristics import check_heuristics_pre_readme, check_heuristics_readme
from ossdigest.judge import JudgeAssessment, JudgeOutcome, judge_candidate, route_judgement
from ossdigest.models import Candidate
from ossdigest.moderation import ModerationPreview, build_moderation_keyboard, build_preview_text, queue_size
from ossdigest.groq_client import GroqClient, GroqDailyLimitExceeded
from ossdigest.readme_prep import clean_readme, readme_hash
from ossdigest.star_snapshots import get_delta_7d, record_snapshot
from ossdigest.writer import write_post

logger = structlog.get_logger().bind(component="pipeline")


class DailyBudgetExceeded(Exception):
    pass


@dataclass
class CandidateOutcome:
    repo_full_name: str
    heuristics_passed: bool
    reject_reason: str | None = None
    judged: bool = False
    judge_reused: bool = False
    verdict: str | None = None
    draft_created: bool = False
    cost_usd: float = 0.0
    skipped_active_post: bool = False


def get_spent_today(conn: sqlite3.Connection) -> float:
    today = datetime.now(timezone.utc).date().isoformat()
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM judgements WHERE substr(created_at, 1, 10) = ?",
        (today,),
    ).fetchone()
    judged = row["s"]
    row2 = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM posts WHERE substr(created_at, 1, 10) = ?",
        (today,),
    ).fetchone()
    return judged + row2["s"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _update_candidate_rows(
    conn: sqlite3.Connection,
    candidate_row_ids: list[int],
    *,
    heuristics_ok: bool,
    reject_reason: str | None = None,
    readme_hash_val: str | None = None,
    readme_chars: int | None = None,
    stars_delta_7d: int | None = None,
) -> None:
    if not candidate_row_ids:
        return
    placeholders = ",".join("?" for _ in candidate_row_ids)
    conn.execute(
        f"""
        UPDATE candidates SET
            heuristics_ok = ?, reject_reason = ?, readme_hash = COALESCE(?, readme_hash),
            readme_chars = COALESCE(?, readme_chars), stars_delta_7d = COALESCE(?, stars_delta_7d)
        WHERE id IN ({placeholders})
        """,
        (int(heuristics_ok), reject_reason, readme_hash_val, readme_chars, stars_delta_7d, *candidate_row_ids),
    )


def _find_reused_judgement(
    conn: sqlite3.Connection, repo_id: int, readme_hash_val: str, prompt_version: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM judgements
        WHERE repo_id = ? AND readme_hash = ? AND prompt_version = ?
          AND category IS NOT NULL AND usefulness IS NOT NULL
          AND novelty IS NOT NULL AND maturity IS NOT NULL
        ORDER BY created_at DESC LIMIT 1
        """,
        (repo_id, readme_hash_val, prompt_version),
    ).fetchone()


def _persist_judgement(conn: sqlite3.Connection, candidate_id: int, repo_id: int, outcome: JudgeOutcome, readme_hash_val: str) -> int:
    a = outcome.assessment
    cur = conn.execute(
        """
        INSERT INTO judgements (
            candidate_id, repo_id, readme_hash, model, prompt_version, raw_response,
            is_usable_tool, category, usefulness, novelty, maturity, verdict,
            final_score, red_flags, tokens_in, tokens_out, cost_usd, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            candidate_id, repo_id, readme_hash_val, outcome.model_used, outcome.prompt_version,
            outcome.raw_response,
            int(a.is_usable_tool) if a else None,
            a.category if a else None,
            a.usefulness if a else None,
            a.novelty if a else None,
            a.maturity if a else None,
            a.verdict if a else "hold",
            outcome.final_score,
            json.dumps(a.red_flags) if a else "[]",
            outcome.tokens_in, outcome.tokens_out, outcome.cost_usd, _now(),
        ),
    )
    return cur.fetchone()["id"]


@dataclass
class PipelineContext:
    conn: sqlite3.Connection
    github_client: GithubClient
    groq_client: GroqClient
    bot: Bot | None
    heuristics_config: HeuristicsConfig
    readme_config: ReadmeConfig
    judge_config: JudgeConfig
    writer_config: WriterConfig
    blocklist_patterns: list[re.Pattern[str]]
    judge_system_prompt: str
    writer_system_prompt: str
    admin_ids: list[int]
    moderation_enabled: bool
    daily_budget_usd: float


async def process_candidate(
    ctx: PipelineContext, candidate: Candidate, repo_id: int, candidate_row_ids: list[int]
) -> CandidateOutcome:
    outcome = CandidateOutcome(repo_full_name=candidate.full_name, heuristics_passed=False)

    already_posted = is_already_posted(ctx.conn, repo_id)
    owner_blocked = is_owner_blocked(ctx.conn, candidate.full_name)
    record_snapshot(ctx.conn, repo_id, candidate.stars)

    pre_result = check_heuristics_pre_readme(
        candidate,
        config=ctx.heuristics_config,
        blocklist_patterns=ctx.blocklist_patterns,
        already_posted=already_posted,
        owner_blocked=owner_blocked,
    )
    if not pre_result.passed:
        _update_candidate_rows(ctx.conn, candidate_row_ids, heuristics_ok=False, reject_reason=pre_result.reason)
        outcome.reject_reason = pre_result.reason
        return outcome

    if has_active_post(ctx.conn, repo_id):
        outcome.skipped_active_post = True
        return outcome

    readme_raw: str | None = None
    if candidate.host == "github":
        owner, name = candidate.full_name.split("/", 1)
        try:
            readme_raw = await ctx.github_client.get_readme_raw(owner, name)
        except Exception:
            logger.exception("readme_fetch_failed", repo=candidate.full_name)

    result = check_heuristics_readme(candidate, config=ctx.heuristics_config, readme_raw=readme_raw)
    _update_candidate_rows(ctx.conn, candidate_row_ids, heuristics_ok=result.passed, reject_reason=result.reason)

    if not result.passed:
        outcome.reject_reason = result.reason
        return outcome

    outcome.heuristics_passed = True

    assert readme_raw is not None
    cleaned = clean_readme(readme_raw, ctx.readme_config)
    h = readme_hash(cleaned)
    stars_delta_7d = get_delta_7d(ctx.conn, repo_id)
    _update_candidate_rows(
        ctx.conn, candidate_row_ids, heuristics_ok=True,
        readme_hash_val=h, readme_chars=len(cleaned), stars_delta_7d=stars_delta_7d,
    )

    reused_row = _find_reused_judgement(ctx.conn, repo_id, h, ctx.judge_config.prompt_version)
    if reused_row is not None:
        outcome.judged = True
        outcome.judge_reused = True
        assessment = JudgeAssessment(
            is_usable_tool=bool(reused_row["is_usable_tool"]),
            category=reused_row["category"],
            usefulness=reused_row["usefulness"],
            novelty=reused_row["novelty"],
            maturity=reused_row["maturity"],
            target_audience="",
            red_flags=json.loads(reused_row["red_flags"] or "[]"),
            one_liner_ru="",
            verdict=reused_row["verdict"],
            reasoning="",
        )
        final_score = reused_row["final_score"]
        route, needs_attention_judge = route_judgement(
            assessment.verdict, final_score,
            publish_threshold=ctx.judge_config.publish_threshold,
            hold_threshold=ctx.judge_config.hold_threshold,
        )
        judgement_id = reused_row["id"]
        outcome.verdict = assessment.verdict
    else:
        if get_spent_today(ctx.conn) >= ctx.daily_budget_usd:
            await _alert_budget_exceeded(ctx, "judge")
            raise DailyBudgetExceeded("daily_budget_usd exceeded before judge call")

        try:
            judge_outcome = await judge_candidate(
                ctx.groq_client, ctx.judge_config, candidate,
                readme_clean=cleaned, stars_delta_7d=stars_delta_7d,
                system_prompt=ctx.judge_system_prompt,
            )
        except GroqDailyLimitExceeded:
            await _alert_daily_free_limit(ctx, "judge")
            raise DailyBudgetExceeded("Groq: лимит запросов модели исчерпан")

        judgement_id = None
        candidate_row_id = candidate_row_ids[0] if candidate_row_ids else None
        if candidate_row_id is not None:
            judgement_id = _persist_judgement(ctx.conn, candidate_row_id, repo_id, judge_outcome, h)

        outcome.judged = True
        outcome.cost_usd += judge_outcome.cost_usd
        assessment = judge_outcome.assessment
        final_score = judge_outcome.final_score
        route = judge_outcome.route
        needs_attention_judge = judge_outcome.needs_attention
        outcome.verdict = assessment.verdict if assessment else "hold"

    if route == "reject":
        outcome.reject_reason = f"judge_{outcome.verdict}"
        _update_candidate_rows(ctx.conn, candidate_row_ids, heuristics_ok=True, reject_reason=outcome.reject_reason)
        return outcome

    if assessment is None:
        assessment = JudgeAssessment(
            is_usable_tool=True, category="other", usefulness=5, novelty=5, maturity=5,
            target_audience="—", red_flags=[], one_liner_ru="—", verdict="hold",
            reasoning="Ответ судьи не прошёл валидацию после ретрая.",
        )

    if get_spent_today(ctx.conn) >= ctx.daily_budget_usd:
        await _alert_budget_exceeded(ctx, "writer")
        raise DailyBudgetExceeded("daily_budget_usd exceeded before writer call")

    try:
        writer_outcome = await write_post(
            ctx.groq_client, ctx.writer_config, candidate,
            readme_clean=cleaned, stars_delta_7d=stars_delta_7d,
            judge_assessment=assessment, system_prompt=ctx.writer_system_prompt,
        )
    except GroqDailyLimitExceeded:
        await _alert_daily_free_limit(ctx, "writer")
        raise DailyBudgetExceeded("Groq: лимит запросов модели исчерпан")

    outcome.cost_usd += writer_outcome.cost_usd

    if writer_outcome.insufficient_data:
        outcome.reject_reason = "writer_insufficient_data"
        _update_candidate_rows(ctx.conn, candidate_row_ids, heuristics_ok=True, reject_reason=outcome.reject_reason)
        return outcome

    if writer_outcome.post_html is None:
        logger.error("writer_failed_no_post", repo=candidate.full_name)
        outcome.reject_reason = "writer_parse_failed"
        _update_candidate_rows(ctx.conn, candidate_row_ids, heuristics_ok=True, reject_reason=outcome.reject_reason)
        if ctx.bot:
            await notify_admins(
                ctx.bot, ctx.admin_ids,
                f"⚠️ Автор не смог сгенерировать пост для {candidate.full_name} после ретрая.",
            )
        return outcome

    repo_norm_name = ctx.conn.execute(
        "SELECT norm_name FROM repos WHERE id = ?", (repo_id,)
    ).fetchone()["norm_name"]
    is_duplicate = len(find_norm_name_duplicates(ctx.conn, repo_id, repo_norm_name)) > 0
    needs_attention = needs_attention_judge or writer_outcome.needs_attention or is_duplicate
    status = "pending" if ctx.moderation_enabled else "approved"

    cur = ctx.conn.execute(
        """
        INSERT INTO posts (
            repo_id, judgement_id, body_html, status, priority, needs_attention,
            writer_model, writer_prompt_ver, cost_usd, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            repo_id, judgement_id, writer_outcome.post_html, status, round(final_score, 2),
            int(needs_attention), writer_outcome.model_used, writer_outcome.prompt_version,
            outcome.cost_usd, _now(),
        ),
    )
    post_id = cur.fetchone()["id"]
    outcome.draft_created = True

    if ctx.moderation_enabled and ctx.bot:
        preview = ModerationPreview(
            post_id=post_id,
            body_html=writer_outcome.post_html,
            final_score=final_score,
            usefulness=assessment.usefulness,
            novelty=assessment.novelty,
            maturity=assessment.maturity,
            category=assessment.category,
            source=candidate.source,
            source_meta=candidate.source_meta or {},
            stars=candidate.stars,
            stars_delta_7d=stars_delta_7d,
            red_flags=assessment.red_flags,
            is_duplicate=is_duplicate,
            queue_size=queue_size(ctx.conn) + 1,
        )
        text = build_preview_text(preview)
        keyboard = build_moderation_keyboard(post_id)
        for admin_id in ctx.admin_ids:
            try:
                await ctx.bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=keyboard)
            except Exception:
                logger.exception("moderation_send_failed", admin_id=admin_id, post_id=post_id)

    return outcome


async def _alert_budget_exceeded(ctx: PipelineContext, stage: str) -> None:
    spent = get_spent_today(ctx.conn)
    logger.critical("daily_budget_exceeded", stage=stage, spent_usd=spent, budget_usd=ctx.daily_budget_usd)
    if ctx.bot:
        await notify_admins(
            ctx.bot, ctx.admin_ids,
            f"🛑 Превышен дневной бюджет LLM ($ {spent:.2f} / ${ctx.daily_budget_usd:.2f}) "
            f"на этапе {stage}. Коллектор остановлен.",
        )


async def _alert_daily_free_limit(ctx: PipelineContext, stage: str) -> None:
    logger.critical("groq_rate_limit_exceeded", stage=stage)
    if ctx.bot:
        await notify_admins(
            ctx.bot, ctx.admin_ids,
            f"🛑 Исчерпан лимит запросов Groq для модели на этапе {stage}. Коллектор остановлен. "
            f"Лимит сбрасывается в течение суток или минут (см. заголовки x-ratelimit-reset-requests "
            f"в логе); при частых остановках смените модель в config.yaml.",
        )
