from __future__ import annotations

import httpx
import structlog

from ossdigest.config import SimpleSource as LobstersConfig
from ossdigest.github_client import GithubClient
from ossdigest.models import Candidate
from ossdigest.sources.github_common import extract_github_repo, github_repo_to_candidate

logger = structlog.get_logger().bind(component="source.lobsters")

USER_AGENT = "ossdigest-bot/0.1 (+https://github.com/gliedabrennung/ossdigest)"


class LobstersSource:
    name = "lobsters"

    def __init__(self, config: LobstersConfig, github_client: GithubClient) -> None:
        self._config = config
        self._github = github_client

    async def fetch(self) -> list[Candidate]:
        if not self._config.enabled:
            return []

        boards = self._config.boards or ["programming"]
        out: list[Candidate] = []
        seen: set[tuple[str, str]] = set()

        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30) as client:
            for board in boards:
                try:
                    resp = await client.get(f"https://lobste.rs/t/{board}.json")
                    resp.raise_for_status()
                except httpx.HTTPError:
                    logger.exception("lobsters_fetch_failed", board=board)
                    continue

                for item in resp.json()[: self._config.max_items]:
                    url = item.get("url")
                    if not url:
                        continue
                    repo_ref = extract_github_repo(url)
                    if not repo_ref or repo_ref in seen:
                        continue
                    seen.add(repo_ref)

                    owner, repo = repo_ref
                    try:
                        data = await self._github.get_repo(owner, repo)
                    except Exception:
                        logger.exception("lobsters_repo_enrich_failed", owner=owner, repo=repo)
                        continue
                    if not data:
                        continue

                    out.append(
                        github_repo_to_candidate(
                            data,
                            source=self.name,
                            source_meta={
                                "lobsters_score": item.get("score"),
                                "lobsters_url": item.get("comments_url"),
                                "board": board,
                            },
                        )
                    )

        return out
