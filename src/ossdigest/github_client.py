from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import structlog

USER_AGENT = "ossdigest-bot/0.1 (+https://github.com/gliedabrennung/ossdigest)"
GITHUB_API = "https://api.github.com"

logger = structlog.get_logger().bind(component="github_client")


class GithubRateLimitError(Exception):
    pass


class GithubClient:
    def __init__(self, token: str, timeout: float = 30.0) -> None:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API, headers=headers, timeout=timeout
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "GithubClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _request(
        self, method: str, url: str, *, max_retries: int = 5, **kwargs: Any
    ) -> httpx.Response:
        attempt = 0
        while True:
            resp = await self._client.request(method, url, **kwargs)
            if resp.status_code not in (403, 429):
                return resp

            attempt += 1
            if attempt > max_retries:
                raise GithubRateLimitError(
                    f"{method} {url} превысил лимит попыток ({max_retries}): "
                    f"{resp.status_code} {resp.text[:200]}"
                )

            wait_s = self._retry_wait_seconds(resp)
            logger.warning(
                "github_rate_limited",
                status=resp.status_code,
                wait_s=wait_s,
                url=url,
                attempt=attempt,
            )
            await asyncio.sleep(wait_s)

    @staticmethod
    def _retry_wait_seconds(resp: httpx.Response) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after) + 1
            except ValueError:
                pass

        reset = resp.headers.get("X-RateLimit-Reset")
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if reset and remaining == "0":
            wait = float(reset) - time.time()
            if wait > 0:
                return wait + 1

        return 30.0

    async def search_repositories(
        self, query: str, *, sort: str = "stars", order: str = "desc", max_pages: int = 10
    ) -> list[dict]:
        items: list[dict] = []
        page = 1
        while page <= max_pages:
            resp = await self._request(
                "GET",
                "/search/repositories",
                params={
                    "q": query,
                    "sort": sort,
                    "order": order,
                    "per_page": 100,
                    "page": page,
                },
            )
            if resp.status_code != 200:
                logger.error(
                    "github_search_failed",
                    status=resp.status_code,
                    body=resp.text[:300],
                    query=query,
                )
                break
            data = resp.json()
            batch = data.get("items", [])
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items

    async def get_repo(self, owner: str, repo: str) -> dict | None:
        resp = await self._request("GET", f"/repos/{owner}/{repo}")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            logger.error(
                "github_get_repo_failed",
                status=resp.status_code,
                owner=owner,
                repo=repo,
                body=resp.text[:300],
            )
            return None
        return resp.json()

    async def get_readme_raw(self, owner: str, repo: str) -> str | None:
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/readme",
            headers={"Accept": "application/vnd.github.raw"},
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            logger.error(
                "github_get_readme_failed",
                status=resp.status_code,
                owner=owner,
                repo=repo,
            )
            return None
        return resp.text

    async def graphql_stars_batch(self, repo_ids: list[str]) -> dict[str, int]:
        if not repo_ids:
            return {}
        aliases = "\n".join(
            f'r{i}: node(id: "{rid}") {{ ... on Repository {{ stargazerCount }} }}'
            for i, rid in enumerate(repo_ids)
        )
        query = f"query {{ {aliases} }}"
        resp = await self._request(
            "POST", "https://api.github.com/graphql", json={"query": query}
        )
        if resp.status_code != 200:
            logger.error("github_graphql_failed", status=resp.status_code, body=resp.text[:300])
            return {}
        data = resp.json().get("data", {})
        out: dict[str, int] = {}
        for i, rid in enumerate(repo_ids):
            node = data.get(f"r{i}")
            if node:
                out[rid] = node["stargazerCount"]
        return out
