from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SendMessage

from ossdigest.config import AppConfig, JudgeConfig, ModerationConfig, PublisherConfig, Secrets, WriterConfig
from ossdigest.db import init_db
from ossdigest.publisher import (
    PublisherServices,
    _count_published_today,
    _last_published_at,
    _next_slot_delay_seconds,
    _select_next_post,
    apply_moderation_timeouts,
    publish_next,
)


def _app_config(**overrides):
    base = dict(
        judge=JudgeConfig(model="m", prompt_version="judge.v1", publish_threshold=7.0, hold_threshold=5.5),
        writer=WriterConfig(model="m", prompt_version="writer.v1"),
        publisher=PublisherConfig(
            timezone="UTC", slots=["10:00"], jitter_minutes=0, max_per_day=3,
            min_gap_minutes=0, skip_if_queue_empty=True, disable_web_page_preview=False,
            diversity_check=True,
        ),
        moderation=ModerationConfig(enabled=True, timeout_hours=72, timeout_action="auto_reject"),
    )
    base.update(overrides)
    return AppConfig(**base)


class FakeMessage:
    def __init__(self, message_id: int):
        self.message_id = message_id


class FakeBot:
    def __init__(self, responses=None, fail_times: int = 0):
        self.sent = []
        self._responses = responses or []
        self._fail_times = fail_times
        self._calls = 0

    async def send_message(self, chat_id, text, parse_mode=None, disable_web_page_preview=None, reply_markup=None):
        self._calls += 1
        self.sent.append((chat_id, text))
        if self._calls <= self._fail_times:
            raise TelegramAPIError(SendMessage(chat_id=str(chat_id), text=text), "boom")
        if self._responses:
            return self._responses.pop(0)
        return FakeMessage(message_id=1000 + self._calls)


def _make_repo(conn, i: int, language="Go") -> int:
    cur = conn.execute(
        """
        INSERT INTO repos (host, remote_id, full_name, norm_name, url, language, first_seen_at, last_checked_at)
        VALUES ('github', ?, ?, ?, 'https://github.com/o/r', ?, 'now', 'now')
        """,
        (str(i), f"o/r{i}", f"r{i}", language),
    )
    return cur.lastrowid


def _make_candidate(conn, repo_id: int) -> int:
    return conn.execute(
        "INSERT INTO candidates (repo_id, source, discovered_at) VALUES (?, 'github_search', 'now')",
        (repo_id,),
    ).lastrowid


def _make_post(conn, repo_id: int, *, status="approved", priority=0.0, created_at=None) -> int:
    created_at = created_at or datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO posts (repo_id, body_html, status, priority, created_at) VALUES (?, 'body', ?, ?, ?)",
        (repo_id, status, priority, created_at),
    )
    return cur.lastrowid


def _services(conn, config, bot):
    return PublisherServices(
        conn=conn, bot=bot, config=config,
        secrets=Secrets(telegram_channel_id="-100123", telegram_admin_ids="111"),
        github_client=None, groq_client=None, writer_system_prompt="",
    )


def test_select_next_post_picks_highest_priority(tmp_path, candidate_factory):
    conn = init_db(tmp_path / "t.db")
    r1 = _make_repo(conn, 1)
    r2 = _make_repo(conn, 2)
    _make_post(conn, r1, priority=1.0)
    p2 = _make_post(conn, r2, priority=5.0)

    row = _select_next_post(conn, diversity_check=False, recent_language=None, recent_category=None)
    assert row["id"] == p2


def test_select_next_post_diversity_skips_same_language_and_category(tmp_path):
    conn = init_db(tmp_path / "t.db")
    r1 = _make_repo(conn, 1, language="Go")
    r2 = _make_repo(conn, 2, language="Rust")
    _make_post(conn, r1, priority=10.0)
    p2 = _make_post(conn, r2, priority=5.0)

    row = _select_next_post(conn, diversity_check=True, recent_language="Go", recent_category=None)
    assert row["id"] == p2


def test_select_next_post_falls_back_when_all_same(tmp_path):
    conn = init_db(tmp_path / "t.db")
    r1 = _make_repo(conn, 1, language="Go")
    _make_post(conn, r1, priority=10.0)

    row = _select_next_post(conn, diversity_check=True, recent_language="Go", recent_category=None)
    assert row is not None


def test_select_next_post_empty_queue_returns_none(tmp_path):
    conn = init_db(tmp_path / "t.db")
    assert _select_next_post(conn, diversity_check=True, recent_language=None, recent_category=None) is None


def test_next_slot_delay_computes_seconds_until_next_slot():
    now = datetime(2026, 9, 5, 8, 0, tzinfo=timezone.utc)
    delay = _next_slot_delay_seconds(["10:00", "14:30"], "UTC", 0, now=now)
    assert delay == pytest.approx(2 * 3600, abs=1)


def test_next_slot_delay_wraps_to_next_day_when_all_slots_passed():
    now = datetime(2026, 9, 5, 20, 0, tzinfo=timezone.utc)
    delay = _next_slot_delay_seconds(["10:00", "14:30"], "UTC", 0, now=now)
    expected = timedelta(hours=14).total_seconds()
    assert delay == pytest.approx(expected, abs=1)


@pytest.mark.asyncio
async def test_apply_moderation_timeouts_auto_reject(tmp_path):
    conn = init_db(tmp_path / "t.db")
    repo_id = _make_repo(conn, 1)
    old = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
    post_id = conn.execute(
        "INSERT INTO posts (repo_id, body_html, status, created_at) VALUES (?, 'x', 'pending', ?)",
        (repo_id, old),
    ).lastrowid

    n = await apply_moderation_timeouts(conn, 72, "auto_reject", 7.0)
    assert n == 1
    assert conn.execute("SELECT status FROM posts WHERE id=?", (post_id,)).fetchone()["status"] == "rejected"


@pytest.mark.asyncio
async def test_apply_moderation_timeouts_auto_approve_requires_score(tmp_path):
    conn = init_db(tmp_path / "t.db")
    repo_id = _make_repo(conn, 1)
    old = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()

    candidate_id = _make_candidate(conn, repo_id)
    judgement_id = conn.execute(
        "INSERT INTO judgements (candidate_id, repo_id, model, prompt_version, raw_response, final_score, created_at) "
        "VALUES (?, ?, 'm', 'v', '{}', 3.0, 'now')",
        (candidate_id, repo_id),
    ).lastrowid
    low_score_post = conn.execute(
        "INSERT INTO posts (repo_id, judgement_id, body_html, status, created_at) VALUES (?, ?, 'x', 'pending', ?)",
        (repo_id, judgement_id, old),
    ).lastrowid

    n = await apply_moderation_timeouts(conn, 72, "auto_approve", 7.0)
    assert n == 1
    assert conn.execute("SELECT status FROM posts WHERE id=?", (low_score_post,)).fetchone()["status"] == "rejected"


@pytest.mark.asyncio
async def test_apply_moderation_timeouts_auto_approve_high_score(tmp_path):
    conn = init_db(tmp_path / "t.db")
    repo_id = _make_repo(conn, 1)
    old = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()

    candidate_id = _make_candidate(conn, repo_id)
    judgement_id = conn.execute(
        "INSERT INTO judgements (candidate_id, repo_id, model, prompt_version, raw_response, final_score, created_at) "
        "VALUES (?, ?, 'm', 'v', '{}', 8.5, 'now')",
        (candidate_id, repo_id),
    ).lastrowid
    post_id = conn.execute(
        "INSERT INTO posts (repo_id, judgement_id, body_html, status, created_at) VALUES (?, ?, 'x', 'pending', ?)",
        (repo_id, judgement_id, old),
    ).lastrowid

    n = await apply_moderation_timeouts(conn, 72, "auto_approve", 7.0)
    assert n == 1
    assert conn.execute("SELECT status FROM posts WHERE id=?", (post_id,)).fetchone()["status"] == "approved"


@pytest.mark.asyncio
async def test_publish_next_happy_path(tmp_path):
    conn = init_db(tmp_path / "t.db")
    repo_id = _make_repo(conn, 1)
    post_id = _make_post(conn, repo_id)
    bot = FakeBot()
    services = _services(conn, _app_config(), bot)

    ok = await publish_next(services)
    assert ok is True
    row = conn.execute("SELECT status, tg_message_id, published_at FROM posts WHERE id=?", (post_id,)).fetchone()
    assert row["status"] == "published"
    assert row["tg_message_id"] is not None
    assert row["published_at"] is not None

    log = conn.execute("SELECT action FROM publish_log WHERE post_id=?", (post_id,)).fetchone()
    assert log["action"] == "published"


@pytest.mark.asyncio
async def test_publish_next_empty_queue(tmp_path):
    conn = init_db(tmp_path / "t.db")
    bot = FakeBot()
    services = _services(conn, _app_config(), bot)
    assert await publish_next(services) is False


@pytest.mark.asyncio
async def test_publish_next_marks_failed_and_increments_error_count(tmp_path):
    conn = init_db(tmp_path / "t.db")
    repo_id = _make_repo(conn, 1)
    post_id = _make_post(conn, repo_id)
    bot = FakeBot(fail_times=1)
    services = _services(conn, _app_config(), bot)

    ok = await publish_next(services)
    assert ok is False
    row = conn.execute("SELECT status, error_count FROM posts WHERE id=?", (post_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["error_count"] == 1


def test_count_published_today_and_last_published_at(tmp_path):
    conn = init_db(tmp_path / "t.db")
    repo_id = _make_repo(conn, 1)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO posts (repo_id, body_html, status, created_at, published_at) VALUES (?, 'x', 'published', 'now', ?)",
        (repo_id, now),
    )
    assert _count_published_today(conn, "UTC") == 1
    assert _last_published_at(conn) is not None
