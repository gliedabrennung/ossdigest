from __future__ import annotations

import asyncio
import time
from collections import Counter
from datetime import datetime, timezone

from aiogram import Bot

from ossdigest.config import load_config, load_secrets
from ossdigest.db import init_db
from ossdigest.github_client import GithubClient
from ossdigest.heuristics import compile_blocklist_patterns
from ossdigest.judge import load_prompt
from ossdigest.logging_setup import configure_logging, get_logger
from ossdigest.models import Candidate
from ossdigest.moderation import queue_size
from ossdigest.normalizer import ingest
from ossdigest.groq_client import GroqClient
from ossdigest.pipeline import DailyBudgetExceeded, PipelineContext, process_candidate
from ossdigest.sources import build_sources
from ossdigest.sources.base import Source
from ossdigest.star_snapshots import refresh_all_tracked_star_snapshots

logger = get_logger("collector")

_PREFERRED_SOURCES = ("hn", "lobsters")


async def collect_all(sources: list[Source]) -> list[Candidate]:
    results = await asyncio.gather(*(s.fetch() for s in sources), return_exceptions=True)

    candidates: list[Candidate] = []
    for source, result in zip(sources, results):
        name = getattr(source, "name", source.__class__.__name__)
        if isinstance(result, Exception):
            logger.error("source_failed", source=name, error=str(result))
            continue
        logger.info("source_ok", source=name, count=len(result))
        candidates.extend(result)
    return candidates


def _select_representatives(candidates: list[Candidate]) -> dict[tuple[str, str], Candidate]:
    best: dict[tuple[str, str], Candidate] = {}
    for c in candidates:
        key = (c.host, c.remote_id)
        current = best.get(key)
        if current is None:
            best[key] = c
        elif current.source not in _PREFERRED_SOURCES and c.source in _PREFERRED_SOURCES:
            best[key] = c
    return best


async def run_once() -> dict:
    started_at = datetime.now(timezone.utc)
    secrets = load_secrets()
    config = load_config()
    configure_logging(secrets.log_level)

    conn = init_db(secrets.database_path, database_url=secrets.database_url)
    started = time.monotonic()

    blocklist_patterns = compile_blocklist_patterns(config.heuristics.blocklist_patterns_file)
    judge_system_prompt = load_prompt(config.judge.prompt_file)
    writer_system_prompt = load_prompt(config.writer.prompt_file)

    bot = Bot(token=secrets.telegram_bot_token) if secrets.telegram_bot_token else None
    admin_ids = secrets.admin_ids

    by_source: Counter[str] = Counter()
    reject_reasons: Counter[str] = Counter()
    run_stats = dict(
        candidates_total=0, candidates_new=0, heuristics_passed=0,
        judged=0, judged_reused=0, verdict_publish=0, verdict_hold=0, verdict_reject=0,
        drafts_created=0, cost_usd=0.0,
    )

    async with GithubClient(secrets.github_token) as github_client, GroqClient(
        secrets.groq_api_key,
        base_url=config.groq.base_url,
        timeout=config.groq.timeout_seconds,
        max_retries=config.groq.max_retries,
    ) as groq_client:
        sources = build_sources(config, secrets, github_client)
        logger.info("collector_start", sources=[getattr(s, "name", "?") for s in sources])
        candidates = await collect_all(sources)
        for c in candidates:
            by_source[c.source] += 1

        ingest_stats = ingest(conn, candidates)
        run_stats["candidates_total"] = ingest_stats["total"]
        run_stats["candidates_new"] = ingest_stats["new_repos"]

        representatives = _select_representatives(candidates)

        ctx = PipelineContext(
            conn=conn,
            github_client=github_client,
            groq_client=groq_client,
            bot=bot,
            heuristics_config=config.heuristics,
            readme_config=config.readme,
            judge_config=config.judge,
            writer_config=config.writer,
            blocklist_patterns=blocklist_patterns,
            judge_system_prompt=judge_system_prompt,
            writer_system_prompt=writer_system_prompt,
            admin_ids=admin_ids,
            moderation_enabled=config.moderation.enabled,
            daily_budget_usd=config.groq.daily_budget_usd,
        )

        ordered = sorted(representatives.items(), key=lambda kv: kv[1].stars, reverse=True)

        processed = 0
        for (host, remote_id), candidate in ordered:
            if processed >= config.judge.max_candidates_per_run:
                break

            row = conn.execute(
                "SELECT id FROM repos WHERE host = ? AND remote_id = ?", (host, remote_id)
            ).fetchone()
            if row is None:
                continue
            repo_id = row["id"]
            candidate_row_ids = [
                r["id"] for r in conn.execute(
                    "SELECT id FROM candidates WHERE repo_id = ? AND discovered_at >= ?",
                    (repo_id, started_at.isoformat()),
                ).fetchall()
            ]

            try:
                outcome = await process_candidate(ctx, candidate, repo_id, candidate_row_ids)
            except DailyBudgetExceeded:
                logger.critical("collector_stopped_budget_exceeded")
                break
            except Exception:
                logger.exception("candidate_processing_failed", repo=candidate.full_name)
                continue

            if outcome.heuristics_passed:
                run_stats["heuristics_passed"] += 1
            elif outcome.reject_reason:
                reject_reasons[outcome.reject_reason] += 1

            if outcome.judged:
                processed += 1
                run_stats["judged"] += 1
                if outcome.judge_reused:
                    run_stats["judged_reused"] += 1
                if outcome.verdict:
                    run_stats[f"verdict_{outcome.verdict}"] = run_stats.get(f"verdict_{outcome.verdict}", 0) + 1

            if outcome.draft_created:
                run_stats["drafts_created"] += 1
            run_stats["cost_usd"] += outcome.cost_usd

        try:
            await refresh_all_tracked_star_snapshots(conn, github_client)
        except Exception:
            logger.exception("star_snapshot_refresh_failed")

    run_stats["duration_s"] = round(time.monotonic() - started, 1)
    run_stats["by_source"] = dict(by_source)
    run_stats["top_reject_reasons"] = reject_reasons.most_common(5)
    run_stats["started_at"] = started_at.isoformat()
    run_stats["queue_size"] = queue_size(conn)

    conn.execute(
        """
        INSERT INTO runs (
            started_at, finished_at, candidates_total, candidates_new, heuristics_passed,
            judged, judged_reused, verdict_publish, verdict_hold, verdict_reject,
            drafts_created, cost_usd
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            started_at.isoformat(), datetime.now(timezone.utc).isoformat(),
            run_stats["candidates_total"], run_stats["candidates_new"], run_stats["heuristics_passed"],
            run_stats["judged"], run_stats["judged_reused"],
            run_stats.get("verdict_publish", 0), run_stats.get("verdict_hold", 0), run_stats.get("verdict_reject", 0),
            run_stats["drafts_created"], run_stats["cost_usd"],
        ),
    )

    logger.info("collector_done", **{k: v for k, v in run_stats.items() if k not in ("by_source", "top_reject_reasons")})

    if bot and admin_ids:
        from ossdigest.alerts import format_run_summary, notify_admins

        await notify_admins(bot, admin_ids, format_run_summary(run_stats))
        await bot.session.close()

    conn.close()
    return run_stats


def main() -> None:
    asyncio.run(run_once())


if __name__ == "__main__":
    main()
