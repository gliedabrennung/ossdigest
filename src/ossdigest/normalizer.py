from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

from ossdigest.models import Candidate

_NORM_SUFFIXES = ("-js", "-py", "-rs", "-go", "-cli", "-lib")
_NORM_STRIP_RE = re.compile(r"[-_.]")


def norm_name(full_name: str) -> str:
    repo_name = full_name.split("/")[-1].lower()
    for suffix in _NORM_SUFFIXES:
        if repo_name.endswith(suffix):
            repo_name = repo_name[: -len(suffix)]
            break
    return _NORM_STRIP_RE.sub("", repo_name)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_repo(conn: sqlite3.Connection, candidate: Candidate) -> tuple[int, bool]:
    now = _now()
    existing = conn.execute(
        "SELECT id FROM repos WHERE host = ? AND remote_id = ?",
        (candidate.host, candidate.remote_id),
    ).fetchone()

    if existing:
        conn.execute(
            """
            UPDATE repos SET
                full_name = ?, norm_name = ?, url = ?, description = ?,
                language = ?, license_spdx = ?, topics = ?, stars = ?,
                forks = ?, open_issues = ?, pushed_at = ?, is_archived = ?,
                is_fork = ?, owner_type = ?, last_checked_at = ?, node_id = COALESCE(?, node_id)
            WHERE id = ?
            """,
            (
                candidate.full_name,
                norm_name(candidate.full_name),
                str(candidate.url),
                candidate.description,
                candidate.language,
                candidate.license_spdx,
                json.dumps(candidate.topics),
                candidate.stars,
                candidate.forks,
                candidate.open_issues,
                candidate.pushed_at.isoformat(),
                int(candidate.is_archived),
                int(candidate.is_fork),
                candidate.owner_type,
                now,
                candidate.node_id,
                existing["id"],
            ),
        )
        return existing["id"], False

    cur = conn.execute(
        """
        INSERT INTO repos (
            host, remote_id, node_id, full_name, norm_name, url, description,
            language, license_spdx, topics, stars, forks, open_issues,
            created_at, pushed_at, is_archived, is_fork, owner_type,
            first_seen_at, last_checked_at, tracked
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        RETURNING id
        """,
        (
            candidate.host,
            candidate.remote_id,
            candidate.node_id,
            candidate.full_name,
            norm_name(candidate.full_name),
            str(candidate.url),
            candidate.description,
            candidate.language,
            candidate.license_spdx,
            json.dumps(candidate.topics),
            candidate.stars,
            candidate.forks,
            candidate.open_issues,
            candidate.created_at.isoformat(),
            candidate.pushed_at.isoformat(),
            int(candidate.is_archived),
            int(candidate.is_fork),
            candidate.owner_type,
            now,
            now,
        ),
    )
    return cur.fetchone()["id"], True


def insert_candidate(conn: sqlite3.Connection, repo_id: int, candidate: Candidate) -> int:
    cur = conn.execute(
        """
        INSERT INTO candidates (repo_id, source, source_meta, discovered_at)
        VALUES (?, ?, ?, ?)
        RETURNING id
        """,
        (repo_id, candidate.source, json.dumps(candidate.source_meta), _now()),
    )
    return cur.fetchone()["id"]


def ingest(conn: sqlite3.Connection, candidates: list[Candidate]) -> dict[str, int]:
    stats = {"total": 0, "new_repos": 0}
    conn.execute("BEGIN")
    try:
        for candidate in candidates:
            repo_id, is_new = upsert_repo(conn, candidate)
            insert_candidate(conn, repo_id, candidate)
            stats["total"] += 1
            if is_new:
                stats["new_repos"] += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return stats
