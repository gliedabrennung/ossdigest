from __future__ import annotations

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

logger = structlog.get_logger().bind(component="alerts")


async def notify_admins(bot: Bot, admin_ids: list[int], text: str) -> None:
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except TelegramAPIError:
            logger.exception("admin_notify_failed", admin_id=admin_id)


def format_run_summary(stats: dict) -> str:
    by_source = stats.get("by_source", {})
    source_line = ", ".join(f"{k} {v}" for k, v in by_source.items()) or "—"

    top_reasons = stats.get("top_reject_reasons", [])
    reasons_line = ", ".join(f"{reason} {count}" for reason, count in top_reasons) or "—"

    return (
        f"<b>Прогон коллектора {stats.get('started_at', '')}</b>\n"
        f"Собрано кандидатов: {stats.get('candidates_total', 0)} ({source_line})\n"
        f"Новых (не видели раньше): {stats.get('candidates_new', 0)}\n"
        f"Прошли эвристики: {stats.get('heuristics_passed', 0)}\n"
        f"  Топ причин отказа: {reasons_line}\n"
        f"Оценено судьёй: {stats.get('judged', 0)} ({stats.get('judged_reused', 0)} переиспользовано по хэшу README)\n"
        f"  publish {stats.get('verdict_publish', 0)} · hold {stats.get('verdict_hold', 0)} · "
        f"reject {stats.get('verdict_reject', 0)}\n"
        f"Черновиков создано: {stats.get('drafts_created', 0)}\n"
        f"Стоимость: ${stats.get('cost_usd', 0):.3f}\n"
        f"Длительность: {stats.get('duration_s', 0):.0f} с\n"
        f"Очередь после прогона: {stats.get('queue_size', 0)}"
    )
