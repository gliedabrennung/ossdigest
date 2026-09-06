from __future__ import annotations

import asyncio
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

from ossdigest.config import AppConfig, Secrets, load_config, load_secrets
from ossdigest.db import init_db
from ossdigest.github_client import GithubClient
from ossdigest.judge import JudgeAssessment, load_prompt
from ossdigest.logging_setup import configure_logging, get_logger
from ossdigest.moderation import (
    approve,
    block_owner_and_reject,
    build_moderation_keyboard,
    defer,
    parse_callback_data,
    reject,
    replace_draft,
    result_label,
)
from ossdigest.groq_client import GroqClient
from ossdigest.readme_prep import clean_readme
from ossdigest.state import set_state
from ossdigest.writer import write_post

logger = get_logger("publisher")

router = Router()


class EditState(StatesGroup):
    waiting_for_text = State()


@dataclass
class PublisherServices:
    conn: sqlite3.Connection
    bot: Bot
    config: AppConfig
    secrets: Secrets
    github_client: GithubClient
    groq_client: GroqClient
    writer_system_prompt: str


def _post_and_repo(conn: sqlite3.Connection, post_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT posts.*, repos.full_name, repos.host, repos.owner_type
        FROM posts JOIN repos ON repos.id = posts.repo_id
        WHERE posts.id = ?
        """,
        (post_id,),
    ).fetchone()


async def _answer_race(callback: CallbackQuery) -> None:
    await callback.answer("Уже обработано", show_alert=True)


async def _mark_message_result(callback: CallbackQuery, action: str) -> None:
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"{result_label(action)} (пост #{callback.data.split(':')[2]})")
    except TelegramAPIError:
        logger.exception("mark_message_result_failed")


@router.callback_query(F.data.startswith("mod:"))
async def on_moderation_callback(callback: CallbackQuery, state: FSMContext, services: PublisherServices) -> None:
    parsed = parse_callback_data(callback.data or "")
    if parsed is None:
        await callback.answer()
        return
    action, post_id = parsed
    conn = services.conn

    if action == "ap":
        ok = approve(conn, post_id)
        if not ok:
            await _answer_race(callback)
            return
        await callback.answer("Одобрено")
        await _mark_message_result(callback, action)

    elif action == "df":
        ok = defer(conn, post_id)
        if not ok:
            await _answer_race(callback)
            return
        await callback.answer("Отложено")
        await _mark_message_result(callback, action)

    elif action == "rj":
        ok = reject(conn, post_id)
        if not ok:
            await _answer_race(callback)
            return
        await callback.answer("Отклонено")
        await _mark_message_result(callback, action)

    elif action == "bo":
        row = _post_and_repo(conn, post_id)
        if row is None:
            await callback.answer("Пост не найден", show_alert=True)
            return
        owner = row["full_name"].split("/", 1)[0]
        ok = block_owner_and_reject(conn, post_id, owner, "заблокирован модератором")
        if not ok:
            await _answer_race(callback)
            return
        await callback.answer("Автор заблокирован")
        await _mark_message_result(callback, action)

    elif action == "ed":
        row = conn.execute("SELECT status FROM posts WHERE id = ?", (post_id,)).fetchone()
        if row is None or row["status"] != "pending":
            await _answer_race(callback)
            return
        await state.set_state(EditState.waiting_for_text)
        await state.update_data(post_id=post_id)
        await callback.answer()
        await callback.message.answer(
            f"Пришлите новый текст поста (HTML, пост #{post_id}). Он полностью заменит текущий."
        )

    elif action == "rg":
        await callback.answer("Перегенерирую…")
        await _regenerate(callback, services, post_id)

    else:
        await callback.answer()


async def _regenerate(callback: CallbackQuery, services: PublisherServices, post_id: int) -> None:
    conn = services.conn
    row = conn.execute(
        """
        SELECT posts.*, repos.full_name, repos.host, repos.description, repos.language,
               repos.license_spdx, repos.stars, repos.forks, repos.open_issues,
               repos.created_at, repos.pushed_at, repos.topics, repos.url,
               judgements.usefulness, judgements.novelty, judgements.maturity,
               judgements.category, judgements.red_flags
        FROM posts
        JOIN repos ON repos.id = posts.repo_id
        LEFT JOIN judgements ON judgements.id = posts.judgement_id
        WHERE posts.id = ? AND posts.status = 'pending'
        """,
        (post_id,),
    ).fetchone()
    if row is None:
        await callback.message.answer("Пост не найден или уже обработан.")
        return

    import json as _json

    from ossdigest.models import Candidate

    candidate = Candidate(
        host=row["host"], full_name=row["full_name"], remote_id="0",
        url=row["url"], description=row["description"], language=row["language"],
        license_spdx=row["license_spdx"], stars=row["stars"], forks=row["forks"],
        open_issues=row["open_issues"], topics=_json.loads(row["topics"] or "[]"),
        created_at=row["created_at"], pushed_at=row["pushed_at"],
        source="regenerate", source_meta={},
    )
    assessment = JudgeAssessment(
        is_usable_tool=True,
        category=row["category"] or "other",
        usefulness=row["usefulness"] or 5,
        novelty=row["novelty"] or 5,
        maturity=row["maturity"] or 5,
        target_audience="—",
        red_flags=_json.loads(row["red_flags"] or "[]"),
        one_liner_ru="—",
        verdict="publish",
        reasoning="regenerate",
    )

    owner, name = row["full_name"].split("/", 1)
    readme_raw = await services.github_client.get_readme_raw(owner, name)
    cleaned = clean_readme(readme_raw or "", services.config.readme)

    outcome = await write_post(
        services.groq_client, services.config.writer, candidate,
        readme_clean=cleaned, stars_delta_7d=None,
        judge_assessment=assessment, system_prompt=services.writer_system_prompt,
    )
    if outcome.post_html is None:
        await callback.message.answer("Не удалось перегенерировать пост (автор вернул недостаточно данных).")
        return

    replace_draft(
        conn, post_id, new_body_html=outcome.post_html,
        writer_model=outcome.model_used, writer_prompt_ver=outcome.prompt_version,
        added_cost_usd=outcome.cost_usd,
    )
    await callback.message.answer(
        outcome.post_html, parse_mode="HTML", reply_markup=build_moderation_keyboard(post_id)
    )


@router.message(StateFilter(EditState.waiting_for_text))
async def on_edit_text(message: Message, state: FSMContext, services: PublisherServices) -> None:
    data = await state.get_data()
    post_id = data.get("post_id")
    await state.clear()
    if post_id is None or message.text is None:
        return

    from ossdigest.html_sanitize import validate_and_sanitize_html

    sanitized = validate_and_sanitize_html(message.text, auto_close=True)
    ok = replace_draft(
        services.conn, post_id, new_body_html=sanitized.html,
        writer_model="manual_edit", writer_prompt_ver="manual", added_cost_usd=0.0,
    )
    if not ok:
        await message.answer("Пост уже не в статусе ожидания — правки не применены.")
        return
    await message.answer(
        f"Текст поста #{post_id} обновлён:\n\n{sanitized.html}",
        parse_mode="HTML",
        reply_markup=build_moderation_keyboard(post_id),
    )


@router.message(Command("queue"))
async def on_queue_command(message: Message, services: PublisherServices) -> None:
    from ossdigest.moderation import queue_size

    n = queue_size(services.conn)
    await message.answer(f"В очереди на публикацию: {n} постов")


async def apply_moderation_timeouts(
    conn: sqlite3.Connection, timeout_hours: int, timeout_action: str, publish_threshold: float
) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=timeout_hours)).isoformat()
    rows = conn.execute(
        """
        SELECT posts.id AS id, judgements.final_score AS final_score
        FROM posts LEFT JOIN judgements ON judgements.id = posts.judgement_id
        WHERE posts.status = 'pending' AND posts.created_at < ?
        """,
        (cutoff,),
    ).fetchall()

    count = 0
    for row in rows:
        score = row["final_score"]
        if timeout_action == "auto_approve" and score is not None and score >= publish_threshold:
            ok = approve(conn, row["id"])
        else:
            ok = reject(conn, row["id"])
        if ok:
            count += 1
    return count


def _select_next_post(conn: sqlite3.Connection, *, diversity_check: bool, recent_language: str | None, recent_category: str | None) -> sqlite3.Row | None:
    rows = conn.execute(
        """
        SELECT posts.*, repos.language, judgements.category
        FROM posts
        JOIN repos ON repos.id = posts.repo_id
        LEFT JOIN judgements ON judgements.id = posts.judgement_id
        WHERE posts.status = 'approved'
        ORDER BY posts.priority DESC, posts.created_at ASC
        """
    ).fetchall()
    if not rows:
        return None
    if not diversity_check or len(rows) == 1:
        return rows[0]

    for row in rows:
        same_language = recent_language is not None and row["language"] == recent_language
        same_category = recent_category is not None and row["category"] == recent_category
        if not same_language and not same_category:
            return row
    return rows[0]


async def publish_next(services: PublisherServices) -> bool:
    conn = services.conn
    cfg = services.config.publisher

    last = conn.execute(
        "SELECT repos.language AS language, judgements.category AS category "
        "FROM posts JOIN repos ON repos.id = posts.repo_id "
        "LEFT JOIN judgements ON judgements.id = posts.judgement_id "
        "WHERE posts.status = 'published' ORDER BY posts.published_at DESC LIMIT 1"
    ).fetchone()
    recent_language = last["language"] if last else None
    recent_category = last["category"] if last else None

    row = _select_next_post(
        conn, diversity_check=cfg.diversity_check,
        recent_language=recent_language, recent_category=recent_category,
    )
    if row is None:
        logger.info("publish_skipped_empty_queue")
        return False

    post_id = row["id"]
    cur = conn.execute(
        "UPDATE posts SET status = 'publishing' WHERE id = ? AND status = 'approved'", (post_id,)
    )
    if cur.rowcount == 0:
        logger.warning("publish_race_lost", post_id=post_id)
        return False

    channel_id = services.secrets.telegram_channel_id
    try:
        msg = await services.bot.send_message(
            channel_id, row["body_html"], parse_mode="HTML",
            disable_web_page_preview=cfg.disable_web_page_preview,
        )
    except TelegramRetryAfter as exc:
        logger.warning("publish_rate_limited", retry_after=exc.retry_after)
        await asyncio.sleep(exc.retry_after)
        conn.execute("UPDATE posts SET status = 'approved' WHERE id = ?", (post_id,))
        return False
    except TelegramAPIError as exc:
        error_count = row["error_count"] + 1
        conn.execute(
            "UPDATE posts SET status = 'failed', error_count = ?, last_error = ? WHERE id = ?",
            (error_count, str(exc), post_id),
        )
        conn.execute(
            "INSERT INTO publish_log (post_id, ts, action, detail) VALUES (?, ?, 'publish_failed', ?)",
            (post_id, datetime.now(timezone.utc).isoformat(), str(exc)),
        )
        logger.error("publish_failed", post_id=post_id, error=str(exc), error_count=error_count)
        if error_count >= 3:
            from ossdigest.alerts import notify_admins

            await notify_admins(
                services.bot, services.secrets.admin_ids,
                f"🛑 Пост #{post_id} не публикуется 3 раза подряд: {exc}",
            )
        return False

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE posts SET status = 'published', published_at = ?, tg_message_id = ? WHERE id = ?",
        (now, msg.message_id, post_id),
    )
    conn.execute(
        "INSERT INTO publish_log (post_id, ts, action, detail) VALUES (?, ?, 'published', ?)",
        (post_id, now, f"tg_message_id={msg.message_id}"),
    )
    logger.info("post_published", post_id=post_id, tg_message_id=msg.message_id)
    return True


def _next_slot_delay_seconds(slots: list[str], timezone_name: str, jitter_minutes: int, now: datetime | None = None) -> float:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(timezone_name)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)

    candidates = []
    for slot in slots:
        hour, minute = map(int, slot.split(":"))
        candidate_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate_dt <= now:
            candidate_dt += timedelta(days=1)
        candidates.append(candidate_dt)

    next_dt = min(candidates)
    jitter = random.uniform(-jitter_minutes, jitter_minutes) if jitter_minutes else 0
    next_dt += timedelta(minutes=jitter)
    return max((next_dt - now).total_seconds(), 1.0)


async def publisher_scheduler_loop(services: PublisherServices) -> None:
    cfg = services.config.publisher
    while True:
        published_today = _count_published_today(services.conn)
        if published_today >= cfg.max_per_day:
            await asyncio.sleep(_seconds_until_midnight(cfg.timezone))
            continue

        delay = _next_slot_delay_seconds(cfg.slots, cfg.timezone, cfg.jitter_minutes)
        await asyncio.sleep(delay)

        last_published_at = _last_published_at(services.conn)
        if last_published_at is not None:
            gap_minutes = (datetime.now(timezone.utc) - last_published_at).total_seconds() / 60
            if gap_minutes < cfg.min_gap_minutes:
                await asyncio.sleep((cfg.min_gap_minutes - gap_minutes) * 60)

        from ossdigest.moderation import queue_size

        if cfg.skip_if_queue_empty and queue_size(services.conn) == 0:
            logger.info("publish_skipped_empty_queue")
            continue

        await publish_next(services)


def _count_published_today(conn: sqlite3.Connection) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM posts WHERE status = 'published' AND substr(published_at, 1, 10) = ?",
        (today,),
    ).fetchone()
    return row["n"]


def _last_published_at(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute(
        "SELECT published_at FROM posts WHERE status = 'published' ORDER BY published_at DESC LIMIT 1"
    ).fetchone()
    if not row or not row["published_at"]:
        return None
    dt = datetime.fromisoformat(row["published_at"])
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _seconds_until_midnight(timezone_name: str) -> float:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(timezone_name)
    now = datetime.now(tz)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - now).total_seconds()


async def heartbeat_loop(conn: sqlite3.Connection, interval_seconds: float = 60.0) -> None:
    while True:
        set_state(conn, "publisher_heartbeat", datetime.now(timezone.utc).isoformat())
        await asyncio.sleep(interval_seconds)


async def check_channel_permissions(bot: Bot, channel_id: str) -> bool:
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(channel_id, me.id)
    except TelegramAPIError:
        logger.exception("channel_permission_check_failed", channel_id=channel_id)
        return False
    can_post = getattr(member, "can_post_messages", None)
    is_admin_status = member.status in ("administrator", "creator")
    if not is_admin_status or can_post is False:
        logger.critical("bot_missing_channel_permissions", channel_id=channel_id, status=member.status)
        return False
    return True


async def moderation_timeout_loop(
    conn: sqlite3.Connection, moderation_config, publish_threshold: float, interval_seconds: float = 3600.0
) -> None:
    while True:
        try:
            n = await apply_moderation_timeouts(
                conn, moderation_config.timeout_hours, moderation_config.timeout_action, publish_threshold
            )
            if n:
                logger.info("moderation_timeouts_applied", count=n)
        except Exception:
            logger.exception("moderation_timeout_loop_failed")
        await asyncio.sleep(interval_seconds)


async def run() -> None:
    secrets = load_secrets()
    config = load_config()
    configure_logging(secrets.log_level)

    conn = init_db(secrets.database_path, database_url=secrets.database_url)
    bot = Bot(token=secrets.telegram_bot_token)
    github_client = GithubClient(secrets.github_token)
    groq_client = GroqClient(
        secrets.groq_api_key,
        base_url=config.groq.base_url,
        timeout=config.groq.timeout_seconds,
        max_retries=config.groq.max_retries,
    )
    writer_system_prompt = load_prompt(config.writer.prompt_file)

    services = PublisherServices(
        conn=conn, bot=bot, config=config, secrets=secrets,
        github_client=github_client, groq_client=groq_client,
        writer_system_prompt=writer_system_prompt,
    )

    ok = await check_channel_permissions(bot, secrets.telegram_channel_id)
    if not ok:
        from ossdigest.alerts import notify_admins

        await notify_admins(
            bot, secrets.admin_ids,
            "🛑 У бота нет прав администратора с публикацией сообщений в канале. Публикация невозможна.",
        )

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    dp["services"] = services

    tasks = [
        asyncio.create_task(dp.start_polling(bot)),
        asyncio.create_task(publisher_scheduler_loop(services)),
        asyncio.create_task(heartbeat_loop(conn)),
        asyncio.create_task(moderation_timeout_loop(conn, config.moderation, config.judge.publish_threshold)),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        await bot.session.close()
        await github_client.aclose()
        await groq_client.aclose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
