from __future__ import annotations

from ossdigest.html_sanitize import truncate_html_safely, validate_and_sanitize_html


def test_allowed_tags_pass_through():
    text = "Use <b>bold</b> and <code>inline code</code> here."
    result = validate_and_sanitize_html(text)
    assert result.html == text
    assert result.errors == []


def test_a_tag_keeps_only_href():
    text = '<a href="https://example.com" onclick="evil()">link</a>'
    result = validate_and_sanitize_html(text)
    assert result.html == '<a href="https://example.com">link</a>'


def test_disallowed_tag_is_escaped():
    text = "<script>alert(1)</script> normal text"
    result = validate_and_sanitize_html(text)
    assert "<script>" not in result.html
    assert "&lt;script&gt;" in result.html
    assert "normal text" in result.html


def test_raw_ampersand_and_angle_brackets_escaped():
    text = "3 < 5 and A & B"
    result = validate_and_sanitize_html(text)
    assert "&lt; 5" in result.html
    assert "A &amp; B" in result.html


def test_unclosed_tag_is_reported_as_error_without_autoclose():
    text = "Some <b>bold text without closing"
    result = validate_and_sanitize_html(text, auto_close=False)
    assert any("unclosed_tags" in e for e in result.errors)


def test_unclosed_tag_is_autoclosed_when_requested():
    text = "Some <b>bold text without closing"
    result = validate_and_sanitize_html(text, auto_close=True)
    assert result.errors == []
    assert result.html.endswith("</b>")


def test_truncate_html_safely_respects_limit_and_closes_tags():
    body = "<b>Intro</b> " + ("word " * 300) + "<code>tail</code>"
    truncated = truncate_html_safely(body, 100)
    assert len(truncated) <= 130
    result = validate_and_sanitize_html(truncated, auto_close=False)
    assert result.errors == []


def test_truncate_html_safely_noop_when_under_limit():
    body = "<b>short</b>"
    assert truncate_html_safely(body, 1000) == body
