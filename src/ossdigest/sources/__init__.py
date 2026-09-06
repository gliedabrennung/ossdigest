from __future__ import annotations

from ossdigest.config import AppConfig, Secrets
from ossdigest.github_client import GithubClient
from ossdigest.sources.base import Source
from ossdigest.sources.github_search import GithubSearchSource
from ossdigest.sources.gitlab import GitlabSource
from ossdigest.sources.hn import HackerNewsSource
from ossdigest.sources.lobsters import LobstersSource


def build_sources(config: AppConfig, secrets: Secrets, github_client: GithubClient) -> list[Source]:
    sources: list[Source] = []
    if config.sources.github_search.enabled:
        sources.append(GithubSearchSource(config.sources.github_search, github_client))
    if config.sources.hacker_news.enabled:
        sources.append(HackerNewsSource(config.sources.hacker_news, github_client))
    if config.sources.lobsters.enabled:
        sources.append(LobstersSource(config.sources.lobsters, github_client))
    if config.sources.gitlab.enabled:
        sources.append(GitlabSource(config.sources.gitlab, secrets.gitlab_token))
    return sources
