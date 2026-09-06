from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone


def is_already_posted(conn: sqlite3.Connection, repo_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM posts WHERE repo_id = ? AND status = 'published' LIMIT 1",
        (repo_id,),
    ).fetchone()
    return row is not None


def is_owner_blocked(conn: sqlite3.Connection, full_name: str) -> bool:
    owner = full_name.split("/", 1)[0].lower()
    row = conn.execute(
        "SELECT 1 FROM blocklist WHERE kind = 'owner' AND lower(value) = ? LIMIT 1",
        (owner,),
    ).fetchone()
    return row is not None


def find_norm_name_duplicates(conn: sqlite3.Connection, repo_id: int, norm_name: str) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM repos WHERE norm_name = ? AND id != ?",
        (norm_name, repo_id),
    ).fetchall()
    return [r["id"] for r in rows]


_ACTIVE_POST_STATUSES = ("draft", "pending", "approved", "publishing", "published", "deferred")


def has_active_post(conn: sqlite3.Connection, repo_id: int) -> bool:
    placeholders = ",".join("?" for _ in _ACTIVE_POST_STATUSES)
    row = conn.execute(
        f"SELECT 1 FROM posts WHERE repo_id = ? AND status IN ({placeholders}) LIMIT 1",
        (repo_id, *_ACTIVE_POST_STATUSES),
    ).fetchone()
    return row is not None


def is_within_repost_cooldown(conn: sqlite3.Connection, repo_id: int, cooldown_days: int) -> bool:
    row = conn.execute(
        "SELECT published_at FROM posts WHERE repo_id = ? AND published_at IS NOT NULL "
        "ORDER BY published_at DESC LIMIT 1",
        (repo_id,),
    ).fetchone()
    if not row:
        return False
    published_at = datetime.fromisoformat(row["published_at"])
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - published_at < timedelta(days=cooldown_days)
