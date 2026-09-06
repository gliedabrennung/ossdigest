from __future__ import annotations

from datetime import date, timedelta

from ossdigest.db import init_db
from ossdigest.normalizer import upsert_repo
from ossdigest.star_snapshots import get_delta_7d, record_snapshot


def _repo_id(conn, candidate_factory):
    repo_id, _ = upsert_repo(conn, candidate_factory())
    return repo_id


def test_delta_none_without_both_snapshots(tmp_path, candidate_factory):
    conn = init_db(tmp_path / "t.db")
    repo_id = _repo_id(conn, candidate_factory)
    assert get_delta_7d(conn, repo_id) is None

    record_snapshot(conn, repo_id, 100, snap_date=date.today())
    assert get_delta_7d(conn, repo_id) is None


def test_delta_computed_when_both_snapshots_present(tmp_path, candidate_factory):
    conn = init_db(tmp_path / "t.db")
    repo_id = _repo_id(conn, candidate_factory)
    today = date.today()
    record_snapshot(conn, repo_id, 100, snap_date=today - timedelta(days=7))
    record_snapshot(conn, repo_id, 380, snap_date=today)
    assert get_delta_7d(conn, repo_id, today=today) == 280


def test_record_snapshot_upserts_same_day(tmp_path, candidate_factory):
    conn = init_db(tmp_path / "t.db")
    repo_id = _repo_id(conn, candidate_factory)
    record_snapshot(conn, repo_id, 100)
    record_snapshot(conn, repo_id, 150)
    row = conn.execute(
        "SELECT stars FROM star_snapshots WHERE repo_id = ? AND snap_date = ?",
        (repo_id, date.today().isoformat()),
    ).fetchone()
    assert row["stars"] == 150
