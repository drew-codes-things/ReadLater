import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import nh3

from .images import PROXY_PREFIX

WEB_SCHEMES = ("http", "https")
LINK_SCHEMES = {"http", "https", "mailto"}
LINK_REL = "noopener noreferrer nofollow"
ELLIPSIS = "..."
TEXT_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "cite", "code", "dd", "del", "dl", "dt", "em", "figcaption",
    "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "ins", "kbd", "li", "mark", "ol", "p", "pre", "q", "s",
    "small", "span", "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "time", "tr", "u", "ul",
}
ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "abbr": {"title"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
    "time": {"datetime"},
    "ol": {"start"},
}
DROPPED_WITH_CONTENT = {"script", "style", "template", "iframe", "object", "noscript", "svg", "math", "form",
                        "select", "textarea", "button", "title", "head"}
WHITESPACE = re.compile(r"\s+")
BREAKING_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption", "figure", "footer",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "img", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "td", "th", "tr", "ul",
}


@dataclass(frozen=True)
class CleanArticle:
    html: str
    hidden_images: int


def _absolute_web_url(base_url: str, value: str) -> str | None:
    if not value or not value.strip():
        return None
    try:
        absolute = urljoin(base_url or "", value.strip())
        parts = urlsplit(absolute)
        has_host = bool(parts.hostname)
        parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in WEB_SCHEMES or not has_host:
        return None
    return absolute


class _TextCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pieces = []
        self.skipping = 0
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag in DROPPED_WITH_CONTENT:
            self.skipping += 1
        if tag == "img":
            self.images.append(dict(attrs).get("src") or "")
        if tag in BREAKING_TAGS:
            self.pieces.append(" ")

    def handle_endtag(self, tag):
        if tag in DROPPED_WITH_CONTENT and self.skipping:
            self.skipping -= 1
        if tag in BREAKING_TAGS:
            self.pieces.append(" ")

    def handle_data(self, data):
        if not self.skipping:
            self.pieces.append(data)

    def text(self) -> str:
        return WHITESPACE.sub(" ", "".join(self.pieces)).strip()


def _collect(raw: str) -> _TextCollector:
    collector = _TextCollector()
    collector.feed(raw or "")
    collector.close()
    return collector


def html_to_text(raw: str) -> str:
    return _collect(raw).text()


def summarise(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit + 1].rsplit(" ", 1)[0] if " " in text[:limit + 1] else text[:limit]
    return cut.rstrip() + ELLIPSIS


def _count_showable_images(raw: str, base_url: str) -> int:
    return sum(1 for source in _collect(raw).images if _absolute_web_url(base_url, source))


def clean_article_html(raw: str, base_url: str, image_proxy=None) -> CleanArticle:
    def filter_attribute(tag, attribute, value):
        if tag == "img" and attribute == "src":
            absolute = _absolute_web_url(base_url, value)
            return image_proxy(absolute) if absolute and image_proxy else None
        return value

    def resolve_relative(url):
        if url.startswith(PROXY_PREFIX):
            return url
        return _absolute_web_url(base_url, url)

    tags = TEXT_TAGS | {"img"} if image_proxy else TEXT_TAGS
    cleaned = nh3.clean(
        raw or "",
        tags=tags,
        clean_content_tags=DROPPED_WITH_CONTENT,
        attributes=ALLOWED_ATTRIBUTES,
        attribute_filter=filter_attribute,
        url_schemes=LINK_SCHEMES,
        url_relative=resolve_relative,
        link_rel=LINK_REL,
        set_tag_attribute_values={"a": {"target": "_blank"}},
    )
    hidden = 0 if image_proxy else _count_showable_images(raw, base_url)
    return CleanArticle(html=cleaned, hidden_images=hidden)
