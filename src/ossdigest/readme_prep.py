from __future__ import annotations

import hashlib
import re

from ossdigest.config import ReadmeConfig

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_IMG_RE = re.compile(r"<img\b[^>]*/?>", re.IGNORECASE)
_HTML_PICTURE_RE = re.compile(r"<picture\b[^>]*>.*?</picture>", re.IGNORECASE | re.DOTALL)
_HTML_ALIGN_CENTER_RE = re.compile(
    r'<p\s+align=["\']center["\']\s*>(.*?)</p>', re.IGNORECASE | re.DOTALL
)
_TOC_LINE_RE = re.compile(r"^\s*[-*]\s*\[[^\]]+\]\(#[^)]+\)\s*$")
_CODE_FENCE_RE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})\s*(.+?)\s*$", re.MULTILINE)
_MD_LINK_URL_RE = re.compile(r"\[[^\]]*\]\(([^)]*)\)")

_BADGE_HOSTS = ("shields.io", "badge.fury.io", "travis-ci", "codecov.io", "circleci.com")

_PRIORITY_HEADING_KEYWORDS = (
    "features", "why", "what is", "overview", "use cases", "comparison", "motivation",
)


def strip_html_comments(text: str) -> str:
    return _HTML_COMMENT_RE.sub("", text)


def strip_badge_lines(text: str) -> str:
    out_lines = []
    for line in text.split("\n"):
        urls = _MD_LINK_URL_RE.findall(line) + re.findall(r'src=["\']([^"\']+)["\']', line)
        is_badge_host = any(host in u for u in urls for host in _BADGE_HOSTS)
        if is_badge_host:
            remainder = _MD_IMAGE_RE.sub("", line)
            remainder = _HTML_IMG_RE.sub("", remainder)
            remainder = re.sub(r"\[[^\]]*\]\([^)]*\)", "", remainder)
            if len(remainder.strip()) < 20:
                continue
        out_lines.append(line)
    return "\n".join(out_lines)


def strip_images_and_logos(text: str) -> str:
    text = _HTML_PICTURE_RE.sub("", text)
    text = _HTML_IMG_RE.sub("", text)

    def _center_block(m: re.Match[str]) -> str:
        inner = _MD_IMAGE_RE.sub("", m.group(1))
        inner = _HTML_IMG_RE.sub("", inner)
        return inner if len(inner.strip()) >= 20 else ""

    text = _HTML_ALIGN_CENTER_RE.sub(_center_block, text)
    text = _MD_IMAGE_RE.sub("", text)
    return text


def strip_generated_toc(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if _TOC_LINE_RE.match(lines[i]):
            j = i
            while j < len(lines) and (_TOC_LINE_RE.match(lines[j]) or not lines[j].strip()):
                j += 1
            block = lines[i:j]
            block_link_lines = sum(1 for line_ in block if _TOC_LINE_RE.match(line_))
            if block_link_lines > 5:
                i = j
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def collapse_code_blocks(text: str, *, max_lines: int = 15) -> str:
    def _collapse(m: re.Match[str]) -> str:
        lang, body = m.group(1), m.group(2)
        lines = body.count("\n")
        if lines > max_lines:
            return f"[код: {lang or 'text'}, {lines} строк]"
        return m.group(0)

    return _CODE_FENCE_RE.sub(_collapse, text)


def _split_sections(text: str) -> list[tuple[str | None, str]]:
    matches = list(_HEADING_RE.finditer(text))
    sections: list[tuple[str | None, str]] = []
    if not matches:
        return [(None, text)]

    intro = text[: matches[0].start()]
    if intro.strip():
        sections.append((None, intro))

    for idx, m in enumerate(matches):
        heading = m.group(2)
        body_start = m.end()
        body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[m.start():body_end]
        sections.append((heading, body))

    return sections


def strip_stopped_sections(text: str, stop_sections: list[str]) -> str:
    stops_lower = [s.lower() for s in stop_sections]
    sections = _split_sections(text)
    kept = []
    for heading, body in sections:
        if heading is not None:
            heading_norm = re.sub(r"[^\w\s]", "", heading).strip().lower()
            if any(stop in heading_norm for stop in stops_lower):
                continue
        kept.append(body)
    return "".join(kept)


def normalize_whitespace(text: str) -> str:
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _section_priority(heading: str | None) -> int:
    if heading is None:
        return 0
    heading_lower = heading.lower()
    if any(kw in heading_lower for kw in _PRIORITY_HEADING_KEYWORDS):
        return 1
    return 2


def _truncate_at_paragraph(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    paragraphs = text.split("\n\n")
    out = ""
    for para in paragraphs:
        candidate = out + ("\n\n" if out else "") + para
        if len(candidate) > max_chars:
            break
        out = candidate
    if out:
        return out
    cut = text[:max_chars]
    last_space = cut.rfind(" ")
    return cut[:last_space] if last_space > 0 else cut


def truncate_by_priority(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text

    sections = _split_sections(text)
    ordered = sorted(range(len(sections)), key=lambda i: _section_priority(sections[i][0]))

    kept_idx: set[int] = set()
    total = 0
    for i in ordered:
        body = sections[i][1]
        if total + len(body) > max_chars and kept_idx:
            continue
        kept_idx.add(i)
        total += len(body)

    result = "".join(sections[i][1] for i in sorted(kept_idx))
    return _truncate_at_paragraph(result, max_chars)


def clean_readme(raw: str, config: ReadmeConfig) -> str:
    text = raw
    text = strip_html_comments(text)
    text = strip_badge_lines(text)
    text = strip_images_and_logos(text)
    text = strip_generated_toc(text)
    text = collapse_code_blocks(text)
    text = strip_stopped_sections(text, config.strip_sections)
    text = normalize_whitespace(text)
    text = truncate_by_priority(text, config.max_chars)
    return text


def readme_hash(cleaned: str) -> str:
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
