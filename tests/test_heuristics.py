from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ossdigest.config import HeuristicsConfig
from ossdigest.heuristics import check_heuristics, compile_blocklist_patterns

CONFIG = HeuristicsConfig(
    min_stars=120,
    min_desc_len=20,
    max_stale_days=60,
    require_license=True,
    max_stars_per_day=3000,
    readme_min_chars=400,
    readme_max_chars=200_000,
    blocked_languages=["Markdown", "TeX", "HTML", "Jupyter Notebook", "CSS"],
)

VALID_README = "x" * 500


def run(candidate, **kwargs):
    defaults = dict(
        config=CONFIG,
        blocklist_patterns=[],
        already_posted=False,
        owner_blocked=False,
        readme_raw=VALID_README,
    )
    defaults.update(kwargs)
    return check_heuristics(candidate, **defaults)


def test_passes_clean_candidate(candidate_factory):
    result = run(candidate_factory())
    assert result.passed is True
    assert result.reason is None


def test_rejects_archived(candidate_factory):
    result = run(candidate_factory(is_archived=True))
    assert result.passed is False and result.reason == "archived"


def test_rejects_fork_without_parent_data(candidate_factory):
    result = run(candidate_factory(is_fork=True, parent_stars=None))
    assert result.passed is False and result.reason == "is_fork"


def test_allows_fork_more_popular_than_parent(candidate_factory):
    result = run(candidate_factory(is_fork=True, parent_stars=100, stars=500))
    assert result.passed is True


def test_rejects_fork_less_popular_than_parent(candidate_factory):
    result = run(candidate_factory(is_fork=True, parent_stars=10_000, stars=500))
    assert result.passed is False and result.reason == "is_fork"


def test_rejects_template(candidate_factory):
    result = run(candidate_factory(is_template=True))
    assert result.reason == "is_template"


def test_rejects_missing_description(candidate_factory):
    result = run(candidate_factory(description=None))
    assert result.reason == "no_description"


def test_rejects_short_description(candidate_factory):
    result = run(candidate_factory(description="too short"))
    assert result.reason == "no_description"


def test_rejects_missing_license(candidate_factory):
    result = run(candidate_factory(license_spdx=None))
    assert result.reason == "no_license"


def test_rejects_stale_repo(candidate_factory):
    old = datetime.now(timezone.utc) - timedelta(days=90)
    result = run(candidate_factory(pushed_at=old))
    assert result.reason == "stale"


def test_rejects_too_few_stars(candidate_factory):
    result = run(candidate_factory(stars=50))
    assert result.reason == "too_few_stars"


def test_rejects_blocked_language(candidate_factory):
    result = run(candidate_factory(language="Jupyter Notebook"))
    assert result.reason == "doc_repo"


def test_rejects_blocklist_pattern_awesome(candidate_factory):
    patterns = compile_blocklist_patterns_from_lines([r"^awesome[-_]"])
    result = run(candidate_factory(full_name="someone/awesome-tools"), blocklist_patterns=patterns)
    assert result.reason == "blocklist_pattern"


def test_rejects_blocklist_pattern_in_description(candidate_factory):
    patterns = compile_blocklist_patterns_from_lines([r"(?i)\b(interview|leetcode)\b"])
    result = run(
        candidate_factory(description="Solutions to leetcode problems for interview prep, 100+ tasks"),
        blocklist_patterns=patterns,
    )
    assert result.reason == "blocklist_pattern"


def test_rejects_already_posted(candidate_factory):
    result = run(candidate_factory(), already_posted=True)
    assert result.reason == "already_posted"


def test_rejects_blocked_owner(candidate_factory):
    result = run(candidate_factory(), owner_blocked=True)
    assert result.reason == "blocked_owner"


def test_rejects_missing_readme(candidate_factory):
    result = run(candidate_factory(), readme_raw=None)
    assert result.reason == "readme_too_short"


def test_rejects_short_readme(candidate_factory):
    result = run(candidate_factory(), readme_raw="short readme")
    assert result.reason == "readme_too_short"


def test_rejects_long_readme(candidate_factory):
    result = run(candidate_factory(), readme_raw="x" * 250_000)
    assert result.reason == "readme_too_long"


def test_rejects_suspicious_growth(candidate_factory):
    now = datetime.now(timezone.utc)
    result = run(candidate_factory(stars=100_000, created_at=now - timedelta(days=5)))
    assert result.reason == "suspicious_growth"


def compile_blocklist_patterns_from_lines(lines: list[str]):
    import re

    return [re.compile(line) for line in lines]


def test_compile_blocklist_patterns_reads_file(tmp_path):
    patterns_file = tmp_path / "blocklist.txt"
    patterns_file.write_text("^awesome[-_]\n(?i)\\bleetcode\\b\n# comment\n\n", encoding="utf-8")
    patterns = compile_blocklist_patterns(patterns_file)
    assert len(patterns) == 2
    assert patterns[0].match("awesome-list")
    assert patterns[1].search("Prep for LEETCODE interviews")


def test_compile_blocklist_patterns_missing_file_returns_empty(tmp_path):
    assert compile_blocklist_patterns(tmp_path / "nope.txt") == []
