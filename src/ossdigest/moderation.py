from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

ACTION_APPROVE = "ap"
ACTION_DEFER = "df"
ACTION_REJECT = "rj"
ACTION_BLOCK_OWNER = "bo"
ACTION_EDIT = "ed"
ACTION_REGENERATE = "rg"

_BUTTON_LABELS = {
    ACTION_APPROVE: "✅ Опубликовать",
    ACTION_DEFER: "🕓 В конец очереди",
    ACTION_REJECT: "❌ Отклонить",
    ACTION_BLOCK_OWNER: "🚫 Автор в блок",
    ACTION_EDIT: "✏️ Правки",
    ACTION_REGENERATE: "🔁 Перегенерировать",
}

_RESULT_LABELS = {
    ACTION_APPROVE: "✅ Одобрено",
    ACTION_DEFER: "🕓 Отложено в конец очереди",
    ACTION_REJECT: "❌ Отклонено",
    ACTION_BLOCK_OWNER: "🚫 Отклонено, автор в блок-листе",
    ACTION_EDIT: "✏️ Ожидание правок",
    ACTION_REGENERATE: "🔁 Перегенерировано",
}


def build_callback_data(action: str, post_id: int) -> str:
    data = f"mod:{action}:{post_id}"
    if len(data.encode("utf-8")) > 64:
        raise ValueError(f"callback_data превышает 64 байта: {data!r}")
    return data


def parse_callback_data(data: str) -> tuple[str, int] | None:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "mod":
        return None
    action, raw_id = parts[1], parts[2]
    if action not in _BUTTON_LABELS or not raw_id.isdigit():
        return None
    return action, int(raw_id)


def build_moderation_keyboard(post_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=_BUTTON_LABELS[ACTION_APPROVE], callback_data=build_callback_data(ACTION_APPROVE, post_id))],
        [
            InlineKeyboardButton(text=_BUTTON_LABELS[ACTION_DEFER], callback_data=build_callback_data(ACTION_DEFER, post_id)),
            InlineKeyboardButton(text=_BUTTON_LABELS[ACTION_REJECT], callback_data=build_callback_data(ACTION_REJECT, post_id)),
        ],
        [InlineKeyboardButton(text=_BUTTON_LABELS[ACTION_BLOCK_OWNER], callback_data=build_callback_data(ACTION_BLOCK_OWNER, post_id))],
        [
            InlineKeyboardButton(text=_BUTTON_LABELS[ACTION_EDIT], callback_data=build_callback_data(ACTION_EDIT, post_id)),
            InlineKeyboardButton(text=_BUTTON_LABELS[ACTION_REGENERATE], callback_data=build_callback_data(ACTION_REGENERATE, post_id)),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def result_label(action: str) -> str:
    return _RESULT_LABELS.get(action, action)


@dataclass
class ModerationPreview:
    post_id: int
    body_html: str
    final_score: float | None
    usefulness: int | None
    novelty: int | None
    maturity: int | None
    category: str | None
    source: str
    source_meta: dict
    stars: int
    stars_delta_7d: int | None
    red_flags: list[str]
    is_duplicate: bool
    queue_size: int


def build_preview_text(preview: ModerationPreview) -> str:
    score_line = "—"
    if preview.final_score is not None:
        score_line = (
            f"{preview.final_score:.1f} (польза {preview.usefulness} / "
            f"новизна {preview.novelty} / зрелость {preview.maturity})"
        )

    source_extra = ""
    if preview.source == "hn" and "hn_points" in preview.source_meta:
        source_extra = f" ({preview.source_meta['hn_points']} очков)"
    elif preview.source == "lobsters" and "lobsters_score" in preview.source_meta:
        source_extra = f" ({preview.source_meta['lobsters_score']} очков)"

    delta_str = f" (+{preview.stars_delta_7d} за неделю)" if preview.stars_delta_7d else ""
    flags = ", ".join(preview.red_flags) if preview.red_flags else "—"
    if preview.is_duplicate:
        flags = f"{flags}; возможный дубликат по имени" if flags != "—" else "возможный дубликат по имени"

    service_block = (
        "────────────\n"
        f"Оценка: {score_line}\n"
        f"Категория: {preview.category or '—'}\n"
        f"Источник: {preview.source}{source_extra}\n"
        f"Звёзд: {preview.stars}{delta_str}\n"
        f"Флаги: {flags}\n"
        f"В очереди: {preview.queue_size} постов"
    )
    return f"{preview.body_html}\n\n{service_block}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _transition(
    conn: sqlite3.Connection,
    post_id: int,
    *,
    new_status: str,
    action: str,
    priority_delta: float = 0.0,
    from_status: str = "pending",
) -> bool:
    cur = conn.execute(
        f"""
        UPDATE posts SET
            status = ?,
            moderated_at = ?,
            moderator_action = ?,
            priority = priority + ?
        WHERE id = ? AND status = ?
        """,
        (new_status, _now(), action, priority_delta, post_id, from_status),
    )
    return cur.rowcount > 0


def approve(conn: sqlite3.Connection, post_id: int) -> bool:
    return _transition(conn, post_id, new_status="approved", action="approve", priority_delta=10)


def defer(conn: sqlite3.Connection, post_id: int) -> bool:
    return _transition(conn, post_id, new_status="deferred", action="defer", priority_delta=-5)


def reject(conn: sqlite3.Connection, post_id: int) -> bool:
    return _transition(conn, post_id, new_status="rejected", action="reject")


def block_owner_and_reject(conn: sqlite3.Connection, post_id: int, owner: str, reason: str) -> bool:
    ok = _transition(conn, post_id, new_status="rejected", action="block_owner")
    if ok:
        conn.execute(
            "INSERT INTO blocklist (kind, value, reason, created_at) VALUES ('owner', ?, ?, ?)",
            (owner, reason, _now()),
        )
    return ok


def defer_to_end_of_queue_after_approval(conn: sqlite3.Connection, post_id: int) -> bool:
    return _transition(
        conn, post_id, new_status="approved", action="requeue", from_status="deferred"
    )


def apply_edit(conn: sqlite3.Connection, post_id: int, new_body_html: str) -> bool:
    cur = conn.execute(
        "UPDATE posts SET body_html = ?, moderated_at = ?, moderator_action = 'edit' "
        "WHERE id = ? AND status = 'pending'",
        (new_body_html, _now(), post_id),
    )
    return cur.rowcount > 0


def replace_draft(
    conn: sqlite3.Connection,
    post_id: int,
    *,
    new_body_html: str,
    writer_model: str,
    writer_prompt_ver: str,
    added_cost_usd: float,
) -> bool:
    cur = conn.execute(
        """
        UPDATE posts SET
            body_html = ?, writer_model = ?, writer_prompt_ver = ?,
            cost_usd = COALESCE(cost_usd, 0) + ?
        WHERE id = ? AND status = 'pending'
        """,
        (new_body_html, writer_model, writer_prompt_ver, added_cost_usd, post_id),
    )
    return cur.rowcount > 0


def queue_size(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM posts WHERE status = 'approved'").fetchone()
    return row["n"]
