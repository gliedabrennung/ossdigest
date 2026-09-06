from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ossdigest.config import HeuristicsConfig
from ossdigest.models import Candidate

FORK_MORE_POPULAR_RATIO = 1.0


@dataclass(frozen=True)
class HeuristicResult:
    passed: bool
    reason: str | None = None


def compile_blocklist_patterns(patterns_file: str | Path) -> list[re.Pattern[str]]:
    path = Path(patterns_file)
    if not path.exists():
        return []
    patterns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(re.compile(line))
    return patterns


def check_heuristics_pre_readme(
    candidate: Candidate,
    *,
    config: HeuristicsConfig,
    blocklist_patterns: list[re.Pattern[str]],
    already_posted: bool,
    owner_blocked: bool,
    now: datetime | None = None,
) -> HeuristicResult:
    now = now or datetime.now(timezone.utc)

    if candidate.is_archived:
        return HeuristicResult(False, "archived")

    if candidate.is_fork:
        parent_stars = candidate.parent_stars
        if parent_stars is None or candidate.stars < parent_stars * FORK_MORE_POPULAR_RATIO:
            return HeuristicResult(False, "is_fork")

    if candidate.is_template:
        return HeuristicResult(False, "is_template")

    if not candidate.description or len(candidate.description) < config.min_desc_len:
        return HeuristicResult(False, "no_description")

    if config.require_license and not candidate.license_spdx:
        return HeuristicResult(False, "no_license")

    pushed_at = candidate.pushed_at
    if pushed_at.tzinfo is None:
        pushed_at = pushed_at.replace(tzinfo=timezone.utc)
    stale_days = (now - pushed_at).days
    if stale_days > config.max_stale_days:
        return HeuristicResult(False, "stale")

    if candidate.stars < config.min_stars:
        return HeuristicResult(False, "too_few_stars")

    if candidate.language and candidate.language in config.blocked_languages:
        return HeuristicResult(False, "doc_repo")

    repo_name = candidate.full_name.rsplit("/", 1)[-1]
    haystack = f"{repo_name} {candidate.description or ''}"
    for pattern in blocklist_patterns:
        if pattern.search(haystack):
            return HeuristicResult(False, "blocklist_pattern")

    if already_posted:
        return HeuristicResult(False, "already_posted")

    if owner_blocked:
        return HeuristicResult(False, "blocked_owner")

    return HeuristicResult(True, None)


def check_heuristics_readme(
    candidate: Candidate,
    *,
    config: HeuristicsConfig,
    readme_raw: str | None,
    now: datetime | None = None,
) -> HeuristicResult:
    now = now or datetime.now(timezone.utc)

    if readme_raw is None:
        return HeuristicResult(False, "readme_too_short")
    readme_len = len(readme_raw)
    if readme_len < config.readme_min_chars:
        return HeuristicResult(False, "readme_too_short")
    if readme_len > config.readme_max_chars:
        return HeuristicResult(False, "readme_too_long")

    created_at = candidate.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_days = max((now - created_at).days, 1)
    if candidate.stars / age_days > config.max_stars_per_day:
        return HeuristicResult(False, "suspicious_growth")

    return HeuristicResult(True, None)


def check_heuristics(
    candidate: Candidate,
    *,
    config: HeuristicsConfig,
    blocklist_patterns: list[re.Pattern[str]],
    already_posted: bool,
    owner_blocked: bool,
    readme_raw: str | None,
    now: datetime | None = None,
) -> HeuristicResult:
    pre = check_heuristics_pre_readme(
        candidate,
        config=config,
        blocklist_patterns=blocklist_patterns,
        already_posted=already_posted,
        owner_blocked=owner_blocked,
        now=now,
    )
    if not pre.passed:
        return pre
    return check_heuristics_readme(candidate, config=config, readme_raw=readme_raw, now=now)
