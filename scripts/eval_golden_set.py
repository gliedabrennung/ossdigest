from __future__ import annotations

import asyncio
import csv
import sys
from pathlib import Path

from ossdigest.config import load_config, load_secrets
from ossdigest.github_client import GithubClient
from ossdigest.groq_client import GroqClient
from ossdigest.heuristics import (
    check_heuristics_pre_readme,
    check_heuristics_readme,
    compile_blocklist_patterns,
)
from ossdigest.judge import judge_candidate, load_prompt
from ossdigest.readme_prep import clean_readme, readme_hash
from ossdigest.sources.github_common import github_repo_to_candidate

GOLDEN_SET_PATH = Path("config/golden_set.csv")


async def evaluate_repo(github_client, groq_client, config, blocklist_patterns, judge_prompt, full_name: str) -> tuple[bool, str]:
    owner, name = full_name.split("/", 1)
    data = await github_client.get_repo(owner, name)
    if data is None:
        return False, "repo_not_found"

    candidate = github_repo_to_candidate(data, source="golden_set")

    pre = check_heuristics_pre_readme(
        candidate, config=config.heuristics, blocklist_patterns=blocklist_patterns,
        already_posted=False, owner_blocked=False,
    )
    if not pre.passed:
        return False, pre.reason or "heuristic_rejected"

    readme_raw = await github_client.get_readme_raw(owner, name)
    readme_result = check_heuristics_readme(candidate, config=config.heuristics, readme_raw=readme_raw)
    if not readme_result.passed:
        return False, readme_result.reason or "heuristic_rejected"

    cleaned = clean_readme(readme_raw, config.readme)
    outcome = await judge_candidate(
        groq_client, config.judge, candidate,
        readme_clean=cleaned, stars_delta_7d=None, system_prompt=judge_prompt,
    )
    return outcome.route == "publish", outcome.verdict or "parse_failed"


async def main() -> None:
    if not GOLDEN_SET_PATH.exists():
        print(f"Не найден {GOLDEN_SET_PATH}. Создайте CSV с колонками full_name,expected_publish", file=sys.stderr)
        sys.exit(1)

    secrets = load_secrets()
    config = load_config()
    blocklist_patterns = compile_blocklist_patterns(config.heuristics.blocklist_patterns_file)
    judge_prompt = load_prompt(config.judge.prompt_file)

    rows = list(csv.DictReader(GOLDEN_SET_PATH.open(encoding="utf-8")))

    tp = fp = tn = fn = 0
    false_approvals: list[str] = []

    async with GithubClient(secrets.github_token) as github_client, GroqClient(
        secrets.groq_api_key, base_url=config.groq.base_url,
        timeout=config.groq.timeout_seconds, max_retries=config.groq.max_retries,
    ) as groq_client:
        for row in rows:
            full_name = row["full_name"]
            expected = row["expected_publish"].strip() == "1"
            try:
                predicted, reason = await evaluate_repo(
                    github_client, groq_client, config, blocklist_patterns, judge_prompt, full_name
                )
            except Exception as exc:
                print(f"ERROR {full_name}: {exc}", file=sys.stderr)
                continue

            print(f"{full_name}: expected={expected} predicted={predicted} ({reason})")

            if predicted and expected:
                tp += 1
            elif predicted and not expected:
                fp += 1
                false_approvals.append(full_name)
            elif not predicted and expected:
                fn += 1
            else:
                tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0

    print("\n--- Результат ---")
    print(f"TP={tp} FP={fp} TN={tn} FN={fn}")
    print(f"Precision: {precision:.2f} (требование >= 0.80)")
    print(f"Recall:    {recall:.2f} (требование >= 0.60)")
    print(f"Ложных одобрений: {len(false_approvals)} (требование == 0): {false_approvals}")


if __name__ == "__main__":
    asyncio.run(main())
