from __future__ import annotations

from ossdigest.config import ReadmeConfig
from ossdigest.readme_prep import (
    clean_readme,
    collapse_code_blocks,
    readme_hash,
    strip_badge_lines,
    strip_generated_toc,
    strip_html_comments,
    strip_images_and_logos,
    strip_stopped_sections,
)

CONFIG = ReadmeConfig(
    max_chars=4000,
    min_chars=400,
    strip_sections=[
        "Installation", "Install", "Getting Started", "Contributing",
        "License", "Changelog", "Sponsors", "Contributors",
        "Acknowledgements", "Citation", "Star History", "Table of Contents",
    ],
)


def test_strip_html_comments():
    text = "before <!-- hidden\nmultiline --> after"
    assert strip_html_comments(text) == "before  after"


def test_strip_badge_lines_removes_pure_badge_line():
    text = (
        "# Project\n"
        "[![Build](https://img.shields.io/travis/x/y.svg)](https://travis-ci.org/x/y) "
        "[![Coverage](https://codecov.io/gh/x/y/badge.svg)](https://codecov.io)\n"
        "\n"
        "Real description text follows here.\n"
    )
    cleaned = strip_badge_lines(text)
    assert "shields.io" not in cleaned
    assert "Real description text follows here." in cleaned


def test_strip_badge_lines_keeps_line_with_real_text():
    text = "See ![logo](https://shields.io/x.svg) our comparison to alternatives below in detail."
    cleaned = strip_badge_lines(text)
    assert "comparison to alternatives" in cleaned


def test_strip_images_and_logos_removes_markdown_image():
    text = "Intro\n\n![Logo](logo.png)\n\nMore text"
    cleaned = strip_images_and_logos(text)
    assert "logo.png" not in cleaned
    assert "More text" in cleaned


def test_strip_images_and_logos_removes_picture_block():
    text = 'before <picture><source srcset="a.png"><img src="b.png"></picture> after'
    cleaned = strip_images_and_logos(text)
    assert "<picture" not in cleaned
    assert "before" in cleaned and "after" in cleaned


def test_strip_generated_toc_removes_long_link_block():
    toc_lines = "\n".join(f"- [Section {i}](#section-{i})" for i in range(8))
    text = f"# Title\n\n{toc_lines}\n\n## Real section\n\nContent.\n"
    cleaned = strip_generated_toc(text)
    assert "Section 0" not in cleaned
    assert "Real section" in cleaned
    assert "Content." in cleaned


def test_strip_generated_toc_keeps_short_link_list():
    text = "- [A](#a)\n- [B](#b)\n\nSome content.\n"
    cleaned = strip_generated_toc(text)
    assert "[A](#a)" in cleaned


def test_collapse_code_blocks_replaces_long_block():
    body = "\n".join(f"line{i}" for i in range(20))
    text = f"before\n```python\n{body}\n```\nafter"
    cleaned = collapse_code_blocks(text)
    assert "[код: python, 21 строк]" in cleaned or "строк]" in cleaned
    assert "line0" not in cleaned


def test_collapse_code_blocks_keeps_short_block():
    text = "before\n```bash\npip install x\n```\nafter"
    cleaned = collapse_code_blocks(text)
    assert "pip install x" in cleaned


def test_strip_stopped_sections_removes_installation():
    text = (
        "Intro paragraph about the tool.\n\n"
        "## Installation\n\nrun `pip install foo`\n\n"
        "## Features\n\nDoes real work.\n"
    )
    cleaned = strip_stopped_sections(text, CONFIG.strip_sections)
    assert "pip install foo" not in cleaned
    assert "Does real work." in cleaned
    assert "Intro paragraph" in cleaned


def test_clean_readme_keeps_meaningful_content_in_first_1500_chars():
    badges = " ".join(f"[![b{i}](https://img.shields.io/x{i}.svg)](https://x)" for i in range(6))
    toc = "\n".join(f"- [Sec {i}](#sec-{i})" for i in range(10))
    long_install = "\n".join(f"step {i}: run something" for i in range(60))
    raw = f"""# MyTool

{badges}

{toc}

## What is MyTool

MyTool is a command-line utility that lets developers batch-process large
CSV files without loading them fully into memory, solving a real problem
for data pipelines that hit memory limits with pandas.

## Installation

{long_install}

## Features

- Streams input instead of loading fully into memory
- Works with gzip-compressed CSVs directly

## License

MIT
"""
    cleaned = clean_readme(raw, CONFIG)
    assert "batch-process large" in cleaned[:1500]
    assert "shields.io" not in cleaned
    assert "step 0: run something" not in cleaned


def test_clean_readme_truncates_to_max_chars():
    config = ReadmeConfig(max_chars=200, min_chars=50, strip_sections=[])
    raw = "\n\n".join(f"Paragraph {i} with some real content about the project." for i in range(30))
    cleaned = clean_readme(raw, config)
    assert len(cleaned) <= 200


def test_readme_hash_stable_and_sensitive_to_content():
    a = readme_hash("hello world")
    b = readme_hash("hello world")
    c = readme_hash("hello world!")
    assert a == b
    assert a != c
    assert len(a) == 64
