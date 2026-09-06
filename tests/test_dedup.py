from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ossdigest.db import init_db
from ossdigest.dedup import (
    find_norm_name_duplicates,
    has_active_post,
    is_already_posted,
    is_owner_blocked,
    is_within_repost_cooldown,
)
from ossdigest.normalizer import ingest, upsert_repo


def test_is_already_posted(tmp_path, candidate_factory):
    conn = init_db(tmp_path / "t.db")
    repo_id, _ = upsert_repo(conn, candidate_factory())
    assert is_already_posted(conn, repo_id) is False

    conn.execute(
        "INSERT INTO posts (repo_id, body_html, status, created_at) VALUES (?, 'x', 'published', 'now')",
        (repo_id,),
    )
    assert is_already_posted(conn, repo_id) is True


def test_is_owner_blocked(tmp_path, candidate_factory):
    conn = init_db(tmp_path / "t.db")
    assert is_owner_blocked(conn, "baduser/repo") is False
    conn.execute(
        "INSERT INTO blocklist (kind, value, reason, created_at) VALUES ('owner', 'baduser', 'spam', 'now')"
    )
    assert is_owner_blocked(conn, "BadUser/repo") is True


def test_find_norm_name_duplicates(tmp_path, candidate_factory):
    conn = init_db(tmp_path / "t.db")
    c1 = candidate_factory(remote_id="1", full_name="alice/cooltool")
    c2 = candidate_factory(remote_id="2", full_name="bob/cool-tool")
    ingest(conn, [c1, c2])

    repo1 = conn.execute("SELECT id, norm_name FROM repos WHERE remote_id='1'").fetchone()
    dupes = find_norm_name_duplicates(conn, repo1["id"], repo1["norm_name"])
    assert len(dupes) == 1


def test_has_active_post(tmp_path, candidate_factory):
    conn = init_db(tmp_path / "t.db")
    repo_id, _ = upsert_repo(conn, candidate_factory())
    assert has_active_post(conn, repo_id) is False

    conn.execute(
        "INSERT INTO posts (repo_id, body_html, status, created_at) VALUES (?, 'x', 'pending', 'now')",
        (repo_id,),
    )
    assert has_active_post(conn, repo_id) is True


def test_has_active_post_ignores_rejected(tmp_path, candidate_factory):
    conn = init_db(tmp_path / "t.db")
    repo_id, _ = upsert_repo(conn, candidate_factory())
    conn.execute(
        "INSERT INTO posts (repo_id, body_html, status, created_at) VALUES (?, 'x', 'rejected', 'now')",
        (repo_id,),
    )
    assert has_active_post(conn, repo_id) is False


def test_is_within_repost_cooldown(tmp_path, candidate_factory):
    conn = init_db(tmp_path / "t.db")
    repo_id, _ = upsert_repo(conn, candidate_factory())
    assert is_within_repost_cooldown(conn, repo_id, 365) is False

    recent = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    conn.execute(
        "INSERT INTO posts (repo_id, body_html, status, created_at, published_at) "
        "VALUES (?, 'x', 'published', 'now', ?)",
        (repo_id, recent),
    )
    assert is_within_repost_cooldown(conn, repo_id, 365) is True
    assert is_within_repost_cooldown(conn, repo_id, 5) is False
