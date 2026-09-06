from __future__ import annotations

from ossdigest.db import init_db
from ossdigest.normalizer import ingest, norm_name, upsert_repo


def test_norm_name_strips_separators_and_suffixes():
    assert norm_name("owner/My-Cool_Tool") == "mycooltool"
    assert norm_name("owner/react.js") == "reactjs"
    assert norm_name("owner/my-tool-py") == "mytool"
    assert norm_name("owner/my-tool-cli") == "mytool"


def test_upsert_repo_inserts_new_then_updates(tmp_path, candidate_factory):
    conn = init_db(tmp_path / "t.db")
    c = candidate_factory(stars=100)
    repo_id, is_new = upsert_repo(conn, c)
    assert is_new is True

    c2 = candidate_factory(stars=200)
    repo_id2, is_new2 = upsert_repo(conn, c2)
    assert is_new2 is False
    assert repo_id2 == repo_id

    row = conn.execute("SELECT stars FROM repos WHERE id = ?", (repo_id,)).fetchone()
    assert row["stars"] == 200


def test_ingest_dedupes_same_repo_across_sources(tmp_path, candidate_factory):
    conn = init_db(tmp_path / "t.db")
    c1 = candidate_factory(source="github_search")
    c2 = candidate_factory(source="hn")
    stats = ingest(conn, [c1, c2])

    assert stats["total"] == 2
    assert stats["new_repos"] == 1

    repos = conn.execute("SELECT COUNT(*) AS n FROM repos").fetchone()["n"]
    candidates = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert repos == 1
    assert candidates == 2


def test_ingest_handles_renamed_repo_by_remote_id(tmp_path, candidate_factory):
    conn = init_db(tmp_path / "t.db")
    c1 = candidate_factory(remote_id="42", full_name="old/name")
    ingest(conn, [c1])

    c2 = candidate_factory(remote_id="42", full_name="new/name")
    ingest(conn, [c2])

    repos = conn.execute("SELECT full_name FROM repos WHERE remote_id = '42'").fetchall()
    assert len(repos) == 1
    assert repos[0]["full_name"] == "new/name"
