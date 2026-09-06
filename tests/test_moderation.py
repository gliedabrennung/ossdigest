from __future__ import annotations

import pytest

from ossdigest.db import init_db
from ossdigest.moderation import (
    ModerationPreview,
    approve,
    block_owner_and_reject,
    build_callback_data,
    build_preview_text,
    defer,
    parse_callback_data,
    reject,
    replace_draft,
)


def test_build_and_parse_callback_data_roundtrip():
    data = build_callback_data("ap", 42)
    assert data == "mod:ap:42"
    assert len(data.encode()) <= 64
    assert parse_callback_data(data) == ("ap", 42)


def test_parse_callback_data_rejects_garbage():
    assert parse_callback_data("garbage") is None
    assert parse_callback_data("mod:xx:1") is None
    assert parse_callback_data("mod:ap:notanumber") is None


def test_build_callback_data_too_long_raises():
    with pytest.raises(ValueError):
        build_callback_data("ap", int("9" * 100))


def _make_post(conn, repo_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO posts (repo_id, body_html, status, created_at) VALUES (?, 'x', 'pending', 'now')",
        (repo_id,),
    )
    return cur.lastrowid


_repo_counter = 0


def _make_repo(conn) -> int:
    global _repo_counter
    _repo_counter += 1
    remote_id = str(_repo_counter)
    cur = conn.execute(
        """
        INSERT INTO repos (host, remote_id, full_name, norm_name, url, first_seen_at, last_checked_at)
        VALUES ('github', ?, 'o/r', 'r', 'https://github.com/o/r', 'now', 'now')
        """,
        (remote_id,),
    )
    return cur.lastrowid


def test_approve_transitions_pending_to_approved(tmp_path):
    conn = init_db(tmp_path / "t.db")
    repo_id = _make_repo(conn)
    post_id = _make_post(conn, repo_id)

    assert approve(conn, post_id) is True
    row = conn.execute("SELECT status, priority FROM posts WHERE id = ?", (post_id,)).fetchone()
    assert row["status"] == "approved"
    assert row["priority"] == 10


def test_double_approve_second_call_detects_race(tmp_path):
    conn = init_db(tmp_path / "t.db")
    repo_id = _make_repo(conn)
    post_id = _make_post(conn, repo_id)

    assert approve(conn, post_id) is True
    assert approve(conn, post_id) is False


def test_reject_and_defer(tmp_path):
    conn = init_db(tmp_path / "t.db")
    repo_id = _make_repo(conn)
    p1 = _make_post(conn, repo_id)
    assert reject(conn, p1) is True
    assert conn.execute("SELECT status FROM posts WHERE id=?", (p1,)).fetchone()["status"] == "rejected"

    repo_id2 = _make_repo(conn)
    p2 = _make_post(conn, repo_id2)
    assert defer(conn, p2) is True
    row = conn.execute("SELECT status, priority FROM posts WHERE id=?", (p2,)).fetchone()
    assert row["status"] == "deferred"
    assert row["priority"] == -5


def test_block_owner_and_reject_inserts_blocklist_entry(tmp_path):
    conn = init_db(tmp_path / "t.db")
    repo_id = _make_repo(conn)
    post_id = _make_post(conn, repo_id)

    assert block_owner_and_reject(conn, post_id, "baduser", "spam") is True
    status = conn.execute("SELECT status FROM posts WHERE id=?", (post_id,)).fetchone()["status"]
    assert status == "rejected"
    blocked = conn.execute("SELECT value, reason FROM blocklist WHERE kind='owner'").fetchone()
    assert blocked["value"] == "baduser"
    assert blocked["reason"] == "spam"


def test_replace_draft_updates_body_and_keeps_pending(tmp_path):
    conn = init_db(tmp_path / "t.db")
    repo_id = _make_repo(conn)
    post_id = _make_post(conn, repo_id)

    ok = replace_draft(
        conn, post_id, new_body_html="new text", writer_model="m", writer_prompt_ver="v2", added_cost_usd=0.01
    )
    assert ok is True
    row = conn.execute("SELECT body_html, status, cost_usd FROM posts WHERE id=?", (post_id,)).fetchone()
    assert row["body_html"] == "new text"
    assert row["status"] == "pending"
    assert row["cost_usd"] == pytest.approx(0.01)


def test_build_preview_text_formats_service_block():
    preview = ModerationPreview(
        post_id=1, body_html="<b>Post</b>", final_score=7.8, usefulness=8, novelty=7, maturity=8,
        category="cli", source="hn", source_meta={"hn_points": 214}, stars=1240, stars_delta_7d=380,
        red_flags=[], is_duplicate=False, queue_size=6,
    )
    text = build_preview_text(preview)
    assert "<b>Post</b>" in text
    assert "Оценка: 7.8 (польза 8 / новизна 7 / зрелость 8)" in text
    assert "Категория: cli" in text
    assert "Источник: hn (214 очков)" in text
    assert "Звёзд: 1240 (+380 за неделю)" in text
    assert "Флаги: —" in text
    assert "В очереди: 6 постов" in text
