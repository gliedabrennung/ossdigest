from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import structlog

from ossdigest.config import GithubSearchSource as GithubSearchConfig
from ossdigest.github_client import GithubClient
from ossdigest.models import Candidate
from ossdigest.sources.github_common import github_repo_to_candidate

logger = structlog.get_logger().bind(component="source.github_search")

_DATE_PLACEHOLDER = re.compile(r"\{(?:d|today)-(\d+)d?\}")


def render_query(template: str, *, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)

    def _sub(m: re.Match[str]) -> str:
        days = int(m.group(1))
        date = (now - timedelta(days=days)).date().isoformat()
        return date

    return _DATE_PLACEHOLDER.sub(_sub, template)


class GithubSearchSource:
    name = "github_search"

    def __init__(self, config: GithubSearchConfig, client: GithubClient) -> None:
        self._config = config
        self._client = client

    async def fetch(self) -> list[Candidate]:
        if not self._config.enabled:
            return []

        seen_ids: set[str] = set()
        out: list[Candidate] = []
        for template in self._config.queries:
            query = render_query(template)
            try:
                items = await self._client.search_repositories(
                    query, max_pages=max(1, self._config.max_items // 100 + 1)
                )
            except Exception:
                logger.exception("github_search_query_failed", query=query)
                continue

            for item in items[: self._config.max_items]:
                remote_id = str(item["id"])
                if remote_id in seen_ids:
                    continue
                seen_ids.add(remote_id)
                try:
                    out.append(github_repo_to_candidate(item, source=self.name))
                except Exception:
                    logger.exception("github_search_parse_failed", item_full_name=item.get("full_name"))

        return out
