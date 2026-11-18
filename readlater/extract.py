import codecs
import email.message
import math
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

import lxml.etree
import lxml.html
from readability import Document
from readability.readability import Unparseable

from .sanitize import html_to_text, summarise
from .timestamps import timestamp_of

WORDS_PER_MINUTE = 230
MAX_TITLE_LENGTH = 300
MAX_SITE_NAME_LENGTH = 200
MAX_AUTHOR_LENGTH = 200
MAX_LANGUAGE_LENGTH = 20
EXCERPT_LENGTH = 280
CHARSET_SNIFF_BYTES = 4096
FALLBACK_CHARSET = "utf-8"
NOT_PAGE_CHARSETS = frozenset({"idna", "punycode", "undefined"})
READABILITY_NO_TITLE = "[no-title]"
HTML_TYPES = frozenset({"", "text/html", "application/xhtml+xml"})
META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE)
WHITESPACE = re.compile(r"\s+")
TITLE_KEYS = ("og:title", "twitter:title")
SITE_KEYS = ("og:site_name", "application-name")
AUTHOR_KEYS = ("author", "article:author", "parsely-author", "dc.creator")
DATE_KEYS = ("article:published_time", "datepublished", "date", "dc.date", "parsely-pub-date", "og:published_time")
DESCRIPTION_KEYS = ("og:description", "description", "twitter:description")
UTF8_PARSER = lxml.html.HTMLParser(encoding="utf-8")
HEADING_TAGS = frozenset({"h1", "h2"})


class ExtractError(ValueError):
    pass


@dataclass(frozen=True)
class Article:
    title: str
    site_name: str
    author: str
    published_at: float | None
    language: str
    excerpt: str
    content_html: str
    text: str
    word_count: int
    reading_minutes: int


def reading_minutes(word_count: int) -> int:
    return math.ceil(word_count / WORDS_PER_MINUTE) if word_count > 0 else 0


def is_html_type(content_type: str) -> bool:
    return (content_type or "").split(";", 1)[0].strip().lower() in HTML_TYPES


def _known_charset(name) -> str | None:
    if not name:
        return None
    try:
        codec = codecs.lookup(name.strip())
    except LookupError:
        return None
    if not getattr(codec, "_is_text_encoding", True) or codec.name in NOT_PAGE_CHARSETS:
        return None
    return codec.name


def decode_page(body: bytes, content_type: str) -> str:
    header = email.message.Message()
    header["content-type"] = content_type or "text/html"
    charset = _known_charset(header.get_content_charset())
    if charset is None:
        found = META_CHARSET.search(body[:CHARSET_SNIFF_BYTES])
        charset = _known_charset(found.group(1).decode("ascii", "replace")) if found else None
    try:
        return body.decode(charset or FALLBACK_CHARSET, "replace")
    except (LookupError, UnicodeError):
        return body.decode(FALLBACK_CHARSET, "replace")


def _tidy(value, limit: int) -> str:
    return WHITESPACE.sub(" ", value or "").strip()[:limit].strip()


def _parse(page: str):
    try:
        return lxml.html.document_fromstring(page.encode("utf-8", "replace"), parser=UTF8_PARSER)
    except (lxml.etree.ParserError, ValueError) as error:
        raise ExtractError("the page is empty") from error


def _meta_values(root) -> dict:
    values = {}
    for meta in root.iter("meta"):
        key = (meta.get("property") or meta.get("name") or meta.get("itemprop") or "").strip().lower()
        content = meta.get("content")
        if key and content and content.strip() and key not in values:
            values[key] = content.strip()
    return values


def _first(values: dict, keys) -> str:
    for key in keys:
        if values.get(key):
            return values[key]
    return ""


def _timestamp(value: str) -> float | None:
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return timestamp_of(moment)


def _published_at(values: dict, root) -> float | None:
    for key in DATE_KEYS:
        if values.get(key):
            found = _timestamp(values[key])
            if found is not None:
                return found
    for time_tag in root.iter("time"):
        found = _timestamp(time_tag.get("datetime") or "")
        if found is not None:
            return found
    return None


def _author(values: dict) -> str:
    author = _first(values, AUTHOR_KEYS)
    if urlsplit(author).scheme in ("http", "https"):
        return ""
    return _tidy(author, MAX_AUTHOR_LENGTH)


def host_name(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.")


def address_title(url: str) -> str:
    parts = urlsplit(url)
    return _tidy(host_name(url) + parts.path.rstrip("/"), MAX_TITLE_LENGTH)


def _title(values: dict, document: Document, url: str) -> str:
    title = _tidy(_first(values, TITLE_KEYS), MAX_TITLE_LENGTH)
    if title:
        return title
    readable = _tidy(document.short_title(), MAX_TITLE_LENGTH)
    if readable and readable != READABILITY_NO_TITLE:
        return readable
    return address_title(url)


def _readable_body(document: Document) -> str:
    try:
        return document.summary(html_partial=True)
    except Unparseable as error:
        raise ExtractError(f"no readable text was found on the page: {error}") from error


def _first_text_element(root):
    for element in root.iter():
        if element is root or not isinstance(element.tag, str):
            continue
        if element.tag in HEADING_TAGS or (element.text or "").strip():
            return element
    return None


def _without_repeated_title(content_html: str, title: str) -> str:
    try:
        root = lxml.html.fromstring(content_html)
    except (lxml.etree.ParserError, ValueError):
        return content_html
    first = _first_text_element(root)
    if first is None or first.tag not in HEADING_TAGS:
        return content_html
    if _tidy(first.text_content(), MAX_TITLE_LENGTH * 2).casefold() != title.casefold():
        return content_html
    first.drop_tree()
    if not root.text_content().strip():
        return content_html
    return lxml.html.tostring(root, encoding="unicode")


def extract_article(page: str, url: str) -> Article:
    if not page or not page.strip():
        raise ExtractError("the page is empty")
    root = _parse(page)
    values = _meta_values(root)
    document = Document(page, url=url)
    title = _title(values, document, url)
    content_html = _without_repeated_title(_readable_body(document), title)
    text = html_to_text(content_html)
    if not text:
        raise ExtractError("no readable text was found on the page")
    words = len(text.split())
    excerpt = _tidy(_first(values, DESCRIPTION_KEYS), EXCERPT_LENGTH * 2) or summarise(text, EXCERPT_LENGTH)
    return Article(
        title=title,
        site_name=_tidy(_first(values, SITE_KEYS), MAX_SITE_NAME_LENGTH) or host_name(url),
        author=_author(values),
        published_at=_published_at(values, root),
        language=_tidy(root.get("lang") or "", MAX_LANGUAGE_LENGTH),
        excerpt=summarise(excerpt, EXCERPT_LENGTH),
        content_html=content_html,
        text=text,
        word_count=words,
        reading_minutes=reading_minutes(words),
    )
