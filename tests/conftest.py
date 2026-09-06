from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from ossdigest.models import Candidate


def make_candidate(**overrides: Any) -> Candidate:
    now = datetime.now(timezone.utc)
    defaults: dict[str, Any] = dict(
        host="github",
        full_name="someone/cool-tool",
        remote_id="123",
        url="https://github.com/someone/cool-tool",
        description="A cool tool that solves a real developer problem end to end.",
        homepage=None,
        language="Go",
        languages=None,
        license_spdx="MIT",
        stars=500,
        forks=10,
        open_issues=5,
        topics=["cli", "developer-tools"],
        created_at=now - timedelta(days=200),
        pushed_at=now - timedelta(days=3),
        is_archived=False,
        is_fork=False,
        is_template=False,
        parent_stars=None,
        owner_type="User",
        source="github_search",
        source_meta={},
    )
    defaults.update(overrides)
    return Candidate(**defaults)


@pytest.fixture
def candidate_factory():
    return make_candidate
