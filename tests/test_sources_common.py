from __future__ import annotations

from datetime import datetime, timezone

from ossdigest.sources.github_common import extract_github_repo, github_repo_to_candidate
from ossdigest.sources.github_search import render_query


def test_extract_github_repo_variants():
    assert extract_github_repo("https://github.com/owner/repo") == ("owner", "repo")
    assert extract_github_repo("https://github.com/owner/repo/") == ("owner", "repo")
    assert extract_github_repo("https://github.com/owner/repo.git") == ("owner", "repo")
    assert extract_github_repo("https://github.com/owner/repo/tree/main/src") == ("owner", "repo")
    assert extract_github_repo("https://github.com/owner/repo?tab=readme-ov-file") == ("owner", "repo")
    assert extract_github_repo("https://github.com/topics/cli") is None
    assert extract_github_repo("https://github.com/orgs/foo/discussions") is None
    assert extract_github_repo("https://gitlab.com/owner/repo") is None
    assert extract_github_repo("https://example.com/owner/repo") is None


def test_render_query_substitutes_relative_dates():
    now = datetime(2026, 9, 5, tzinfo=timezone.utc)
    q = render_query("stars:>150 pushed:>{d-14} created:>{d-180}", now=now)
    assert "pushed:>2026-08-22" in q
    assert "created:>2026-03-09" in q


def test_github_repo_to_candidate_maps_fields():
    data = {
        "id": 123,
        "full_name": "owner/repo",
        "html_url": "https://github.com/owner/repo",
        "description": "A tool",
        "homepage": "",
        "language": "Go",
        "license": {"spdx_id": "MIT"},
        "stargazers_count": 42,
        "forks_count": 3,
        "open_issues_count": 1,
        "topics": ["cli"],
        "created_at": "2026-01-01T00:00:00Z",
        "pushed_at": "2026-02-01T00:00:00Z",
        "archived": False,
        "fork": False,
        "is_template": False,
        "owner": {"type": "Organization"},
    }
    c = github_repo_to_candidate(data, source="github_search")
    assert c.full_name == "owner/repo"
    assert c.remote_id == "123"
    assert c.license_spdx == "MIT"
    assert c.owner_type == "Organization"
    assert c.source == "github_search"


def test_github_repo_to_candidate_handles_noassertion_license():
    data = {
        "id": 1, "full_name": "a/b", "html_url": "https://github.com/a/b",
        "description": "d", "homepage": None, "language": "Go",
        "license": {"spdx_id": "NOASSERTION"}, "stargazers_count": 1,
        "forks_count": 0, "open_issues_count": 0, "topics": [],
        "created_at": "2026-01-01T00:00:00Z", "pushed_at": "2026-01-01T00:00:00Z",
        "archived": False, "fork": False, "is_template": False,
        "owner": {"type": "User"},
    }
    c = github_repo_to_candidate(data, source="github_search")
    assert c.license_spdx is None


def test_github_repo_to_candidate_captures_parent_stars_for_fork():
    data = {
        "id": 1, "full_name": "a/b", "html_url": "https://github.com/a/b",
        "description": "d", "homepage": None, "language": "Go",
        "license": {"spdx_id": "MIT"}, "stargazers_count": 500,
        "forks_count": 0, "open_issues_count": 0, "topics": [],
        "created_at": "2026-01-01T00:00:00Z", "pushed_at": "2026-01-01T00:00:00Z",
        "archived": False, "fork": True, "is_template": False,
        "owner": {"type": "User"},
        "parent": {"stargazers_count": 50},
    }
    c = github_repo_to_candidate(data, source="hn")
    assert c.is_fork is True
    assert c.parent_stars == 50
