from __future__ import annotations

import httpx
import structlog

from ossdigest.config import SimpleSource as GitlabConfig
from ossdigest.models import Candidate

logger = structlog.get_logger().bind(component="source.gitlab")

USER_AGENT = "ossdigest-bot/0.1 (+https://github.com/gliedabrennung/ossdigest)"
GITLAB_API = "https://gitlab.com/api/v4/projects"


class GitlabSource:
    name = "gitlab"

    def __init__(self, config: GitlabConfig, token: str = "") -> None:
        self._config = config
        self._token = token

    async def fetch(self) -> list[Candidate]:
        if not self._config.enabled:
            return []

        headers = {"User-Agent": USER_AGENT}
        if self._token:
            headers["PRIVATE-TOKEN"] = self._token

        params = {
            "order_by": "star_count",
            "sort": "desc",
            "visibility": "public",
            "per_page": min(self._config.max_items, 100),
        }

        async with httpx.AsyncClient(headers=headers, timeout=30) as client:
            try:
                resp = await client.get(GITLAB_API, params=params)
                resp.raise_for_status()
            except httpx.HTTPError:
                logger.exception("gitlab_fetch_failed")
                return []
            items = resp.json()

        out: list[Candidate] = []
        for item in items[: self._config.max_items]:
            try:
                out.append(self._to_candidate(item))
            except Exception:
                logger.exception("gitlab_parse_failed", item=item.get("path_with_namespace"))
        return out

    def _to_candidate(self, data: dict) -> Candidate:
        license_info = data.get("license") or {}
        namespace = data.get("namespace") or {}
        return Candidate(
            host="gitlab",
            full_name=data["path_with_namespace"],
            remote_id=str(data["id"]),
            url=data["web_url"],
            description=data.get("description"),
            homepage=data.get("web_url"),
            language=None,
            languages=None,
            license_spdx=license_info.get("key"),
            stars=data.get("star_count", 0),
            forks=data.get("forks_count", 0),
            open_issues=data.get("open_issues_count", 0),
            topics=data.get("topics") or data.get("tag_list") or [],
            created_at=data["created_at"],
            pushed_at=data.get("last_activity_at", data["created_at"]),
            is_archived=bool(data.get("archived", False)),
            is_fork=bool(data.get("forked_from_project")),
            is_template=False,
            owner_type="Organization" if namespace.get("kind") == "group" else "User",
            source=self.name,
            source_meta={},
        )
