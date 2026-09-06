from __future__ import annotations

import html
from dataclasses import dataclass, field
from html.parser import HTMLParser

ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}
_VOID_TAGS = {"br"}


@dataclass
class SanitizeResult:
    html: str
    errors: list[str] = field(default_factory=list)


class _TagSanitizer(HTMLParser):
    def __init__(self, *, auto_close: bool) -> None:
        super().__init__(convert_charrefs=True)
        self._auto_close = auto_close
        self.out: list[str] = []
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ALLOWED_TAGS:
            if tag == "a":
                href = dict(attrs).get("href") or ""
                self.out.append(f'<a href="{html.escape(href, quote=True)}">')
            else:
                self.out.append(f"<{tag}>")
            self.stack.append(tag)
        else:
            attr_str = "".join(f' {k}="{v}"' for k, v in attrs if v is not None)
            self.out.append(html.escape(f"<{tag}{attr_str}>"))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.out.append(html.escape(f"<{tag}/>"))

    def handle_endtag(self, tag: str) -> None:
        if tag not in ALLOWED_TAGS:
            self.out.append(html.escape(f"</{tag}>"))
            return
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
            self.out.append(f"</{tag}>")
        elif tag in self.stack:
            while self.stack and self.stack[-1] != tag:
                mismatched = self.stack.pop()
                self.out.append(f"</{mismatched}>")
                self.errors.append(f"mismatched_close:{mismatched}")
            if self.stack:
                self.stack.pop()
                self.out.append(f"</{tag}>")
        else:
            self.errors.append(f"stray_close:{tag}")
            self.out.append(html.escape(f"</{tag}>"))

    def handle_data(self, data: str) -> None:
        self.out.append(html.escape(data, quote=False))

    def finish(self) -> SanitizeResult:
        self.close()
        if self.stack:
            if self._auto_close:
                for tag in reversed(self.stack):
                    self.out.append(f"</{tag}>")
            else:
                self.errors.append(f"unclosed_tags:{','.join(self.stack)}")
        return SanitizeResult(html="".join(self.out), errors=self.errors)


def validate_and_sanitize_html(text: str, *, auto_close: bool = False) -> SanitizeResult:
    parser = _TagSanitizer(auto_close=auto_close)
    parser.feed(text)
    return parser.finish()


def truncate_html_safely(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return validate_and_sanitize_html(text, auto_close=True).html

    cut = text[:max_chars]
    for sep, keep_sep_chars in (("\n\n", 0), (". ", 1), (" ", 0)):
        idx = cut.rfind(sep)
        if idx > max_chars // 2:
            cut = cut[: idx + keep_sep_chars]
            break

    last_lt = cut.rfind("<")
    last_gt = cut.rfind(">")
    if last_lt > last_gt:
        cut = cut[:last_lt]

    return validate_and_sanitize_html(cut, auto_close=True).html
