from __future__ import annotations

import re
from typing import Any

from ossdigest.models import Candidate

_GITHUB_REPO_RE = re.compile(
    r"^https?://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/"
    r"([A-Za-z0-9_.-]+?)(?:\.git)?(?:[/?#].*)?$"
)

_GITHUB_NON_REPO_PREFIXES = {
    "topics", "orgs", "marketplace", "sponsors", "collections", "trending",
    "settings", "notifications", "issues", "pulls", "explore", "search",
    "features", "about", "pricing", "apps",
}


def extract_github_repo(url: str) -> tuple[str, str] | None:
    m = _GITHUB_REPO_RE.match(url.strip())
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if owner.lower() in _GITHUB_NON_REPO_PREFIXES:
        return None
    return owner, repo


def github_repo_to_candidate(
    data: dict[str, Any], *, source: str, source_meta: dict[str, Any] | None = None
) -> Candidate:
    license_info = data.get("license") or {}
    owner = data.get("owner") or {}
    parent = data.get("parent") or {}
    return Candidate(
        host="github",
        full_name=data["full_name"],
        remote_id=str(data["id"]),
        node_id=data.get("node_id"),
        url=data["html_url"],
        description=data.get("description"),
        homepage=data.get("homepage") or None,
        language=data.get("language"),
        languages=None,
        license_spdx=license_info.get("spdx_id") if license_info.get("spdx_id") != "NOASSERTION" else None,
        stars=data.get("stargazers_count", 0),
        forks=data.get("forks_count", data.get("forks", 0)),
        open_issues=data.get("open_issues_count", 0),
        topics=data.get("topics") or [],
        created_at=data["created_at"],
        pushed_at=data["pushed_at"],
        is_archived=bool(data.get("archived", False)),
        is_fork=bool(data.get("fork", False)),
        is_template=bool(data.get("is_template", False)),
        parent_stars=parent.get("stargazers_count"),
        owner_type="Organization" if owner.get("type") == "Organization" else "User",
        source=source,
        source_meta=source_meta or {},
    )
