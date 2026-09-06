from __future__ import annotations

import time

import httpx
import structlog

from ossdigest.config import SimpleSource as HnConfig
from ossdigest.github_client import GithubClient
from ossdigest.models import Candidate
from ossdigest.sources.github_common import extract_github_repo, github_repo_to_candidate

logger = structlog.get_logger().bind(component="source.hn")

HN_API = "https://hn.algolia.com/api/v1/search_by_date"
USER_AGENT = "ossdigest-bot/0.1 (+https://github.com/gliedabrennung/ossdigest)"


class HackerNewsSource:
    name = "hn"

    def __init__(self, config: HnConfig, github_client: GithubClient) -> None:
        self._config = config
        self._github = github_client

    async def fetch(self) -> list[Candidate]:
        if not self._config.enabled:
            return []

        ts_24h_ago = int(time.time()) - 24 * 3600
        params = {
            "tags": "story",
            "numericFilters": f"points>{self._config.min_points},created_at_i>{ts_24h_ago}",
            "hitsPerPage": 200,
        }

        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30) as client:
            try:
                resp = await client.get(HN_API, params=params)
                resp.raise_for_status()
            except httpx.HTTPError:
                logger.exception("hn_fetch_failed")
                return []
            hits = resp.json().get("hits", [])

        out: list[Candidate] = []
        seen: set[tuple[str, str]] = set()
        for hit in hits[: self._config.max_items]:
            url = hit.get("url")
            if not url:
                continue
            repo_ref = extract_github_repo(url)
            if not repo_ref:
                continue
            if repo_ref in seen:
                continue
            seen.add(repo_ref)

            owner, repo = repo_ref
            try:
                data = await self._github.get_repo(owner, repo)
            except Exception:
                logger.exception("hn_repo_enrich_failed", owner=owner, repo=repo)
                continue
            if not data:
                continue

            candidate = github_repo_to_candidate(
                data,
                source=self.name,
                source_meta={
                    "hn_points": hit.get("points"),
                    "hn_url": f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                },
            )
            out.append(candidate)

        return out
