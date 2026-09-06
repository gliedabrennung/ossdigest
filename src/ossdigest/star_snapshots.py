from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import structlog

from ossdigest.github_client import GithubClient

logger = structlog.get_logger().bind(component="star_snapshots")


def record_snapshot(conn: sqlite3.Connection, repo_id: int, stars: int, snap_date: date | None = None) -> None:
    snap_date = snap_date or date.today()
    conn.execute(
        """
        INSERT INTO star_snapshots (repo_id, snap_date, stars) VALUES (?, ?, ?)
        ON CONFLICT (repo_id, snap_date) DO UPDATE SET stars = excluded.stars
        """,
        (repo_id, snap_date.isoformat(), stars),
    )


def get_delta_7d(conn: sqlite3.Connection, repo_id: int, *, today: date | None = None) -> int | None:
    today = today or date.today()
    week_ago = today - timedelta(days=7)

    today_row = conn.execute(
        "SELECT stars FROM star_snapshots WHERE repo_id = ? AND snap_date = ?",
        (repo_id, today.isoformat()),
    ).fetchone()
    week_row = conn.execute(
        "SELECT stars FROM star_snapshots WHERE repo_id = ? AND snap_date <= ? ORDER BY snap_date DESC LIMIT 1",
        (repo_id, week_ago.isoformat()),
    ).fetchone()

    if today_row is None or week_row is None:
        return None
    return today_row["stars"] - week_row["stars"]


async def refresh_all_tracked_star_snapshots(
    conn: sqlite3.Connection, github_client: GithubClient, *, batch_size: int = 100
) -> int:
    today = date.today().isoformat()
    rows = conn.execute(
        """
        SELECT id, node_id FROM repos
        WHERE tracked = 1 AND node_id IS NOT NULL
        AND id NOT IN (SELECT repo_id FROM star_snapshots WHERE snap_date = ?)
        """,
        (today,),
    ).fetchall()

    node_to_repo = {r["node_id"]: r["id"] for r in rows}
    node_ids = list(node_to_repo.keys())

    updated = 0
    for i in range(0, len(node_ids), batch_size):
        batch = node_ids[i : i + batch_size]
        try:
            stars_by_node = await github_client.graphql_stars_batch(batch)
        except Exception:
            logger.exception("star_snapshot_batch_failed", batch_start=i)
            continue
        for node_id, stars in stars_by_node.items():
            repo_id = node_to_repo.get(node_id)
            if repo_id is not None:
                record_snapshot(conn, repo_id, stars)
                updated += 1

    return updated
