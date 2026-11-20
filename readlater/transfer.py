import html
import json
import re
from datetime import datetime
from html.parser import HTMLParser

from .extract import decode_page
from .library import (
    MAX_COLLECTION_NAME,
    MAX_HIGHLIGHT,
    MAX_NOTE,
    MAX_TAG,
    MAX_TAGS_PER_ITEM,
    MAX_TITLE,
    LibraryError,
    clean_note,
    clean_progress,
    clean_tags,
    collection_name,
    iso,
    tidy_text,
)
from .netguard import UnsafeUrlError, check_url
from .timestamps import showable_timestamp, timestamp_of

APP_NAME = "read-later"
EXPORT_VERSION = 1
MAX_IMPORT_ITEMS = 20_000
MAX_SHORT_TEXT = 500
MAX_EXCERPT = 1000
LARGEST_SECONDS = 10 ** 11
BOOKMARK_TIME_DIGITS = re.compile(r"[0-9]{1,17}")
ARCHIVE_HEADINGS = frozenset({"read archive", "archive", "archived"})
NOTE_ENDING_TAGS = frozenset({"dt", "dl", "h1", "h2", "h3", "a", "li", "ul"})
BOOKMARKS_HEADER = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Starfire Read Later</TITLE>
<H1>Starfire Read Later</H1>
<DL><p>
"""
BOOKMARKS_FOOTER = "</DL><p>\n"


def _escape(value: str) -> str:
    return html.escape(value or "", quote=True)


def _iso_or_none(value):
    return iso(value) if value is not None else None


def export_json(library) -> dict:
    items = []
    for record in library.export_records():
        items.append({
            **record,
            "publishedAt": _iso_or_none(record["publishedAt"]),
            "createdAt": _iso_or_none(record["createdAt"]),
            "archivedAt": _iso_or_none(record["archivedAt"]),
            "highlights": [{"text": highlight["text"], "createdAt": _iso_or_none(highlight["createdAt"])}
                           for highlight in record["highlights"]],
        })
    return {
        "app": APP_NAME,
        "version": EXPORT_VERSION,
        "exportedAt": iso(library.clock()),
        "collections": [collection["name"] for collection in library.list_collections()],
        "items": items,
    }


def _bookmark_line(record: dict, indent: str) -> str:
    created = int(record["createdAt"] or 0)
    tags = _escape(",".join(record["tags"]))
    line = f'{indent}<DT><A HREF="{_escape(record["url"])}" ADD_DATE="{created}" TAGS="{tags}">{_escape(record["title"])}</A>\n'
    if record["note"]:
        line += f"{indent}<DD>{_escape(record['note'])}\n"
    return line


def export_bookmarks_html(library) -> str:
    records = library.export_records()
    parts = [BOOKMARKS_HEADER]
    parts.extend(_bookmark_line(record, "    ") for record in records if record["collection"] is None)
    for collection in library.list_collections():
        inside = [record for record in records if record["collection"] == collection["name"]]
        parts.append(f"    <DT><H3>{_escape(collection['name'])}</H3>\n    <DL><p>\n")
        parts.extend(_bookmark_line(record, "        ") for record in inside)
        parts.append("    </DL><p>\n")
    parts.append(BOOKMARKS_FOOTER)
    return "".join(parts)


def _is_safe_link(url) -> bool:
    try:
        check_url(url)
    except UnsafeUrlError:
        return False
    return True


def _optional_text(item: dict, name: str, limit: int) -> str:
    value = item.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise LibraryError(f"{name} must be text")
    return value[:limit]


def _optional_bool(item: dict, name: str) -> bool:
    value = item.get(name, False)
    if not isinstance(value, bool):
        raise LibraryError(f"{name} must be true or false")
    return value


def _optional_time(value, name: str):
    if value is None:
        return None
    if not isinstance(value, str):
        raise LibraryError(f"{name} must be a date and time like 2026-11-13T09:00:00Z")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as error:
        raise LibraryError(f"{name} must be a date and time like 2026-11-13T09:00:00Z") from error
    return timestamp_of(moment)


def _highlights(value) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LibraryError("highlights must be a list")
    cleaned = []
    for highlight in value:
        if not isinstance(highlight, dict) or not isinstance(highlight.get("text"), str) or not highlight["text"].strip():
            raise LibraryError("each highlight needs its text")
        cleaned.append({"text": tidy_text(highlight["text"])[:MAX_HIGHLIGHT],
                        "createdAt": _optional_time(highlight.get("createdAt"), "highlight createdAt")})
    return cleaned


def _json_record(item: dict) -> dict:
    collection = item.get("collection")
    return {
        "url": item["url"],
        "title": tidy_text(_optional_text(item, "title", MAX_TITLE * 4))[:MAX_TITLE],
        "siteName": _optional_text(item, "siteName", MAX_SHORT_TEXT),
        "author": _optional_text(item, "author", MAX_SHORT_TEXT),
        "language": _optional_text(item, "language", MAX_SHORT_TEXT),
        "excerpt": _optional_text(item, "excerpt", MAX_EXCERPT),
        "contentHtml": _optional_text(item, "contentHtml", 10 ** 8),
        "fetchError": _optional_text(item, "fetchError", MAX_SHORT_TEXT),
        "note": clean_note(item.get("note") or ""),
        "tags": clean_tags(item.get("tags", [])),
        "collection": None if collection is None else collection_name(collection),
        "favorite": _optional_bool(item, "favorite"),
        "archived": _optional_bool(item, "archived"),
        "loadImages": _optional_bool(item, "loadImages"),
        "progress": clean_progress(item.get("progress", 0)),
        "publishedAt": _optional_time(item.get("publishedAt"), "publishedAt"),
        "createdAt": _optional_time(item.get("createdAt"), "createdAt"),
        "archivedAt": _optional_time(item.get("archivedAt"), "archivedAt"),
        "highlights": _highlights(item.get("highlights")),
    }


def _import_list(library, records: list[dict], skipped_before: int) -> dict:
    summary = library.import_records(records)
    return {**summary, "skipped": summary["skipped"] + skipped_before}


def import_json(library, payload) -> dict:
    if not isinstance(payload, dict) or payload.get("app") != APP_NAME:
        raise LibraryError("this is not a Starfire Read Later export")
    if payload.get("version") != EXPORT_VERSION:
        raise LibraryError(f"this export is version {payload.get('version')!r}, only version {EXPORT_VERSION} can be read")
    items = payload.get("items")
    if not isinstance(items, list):
        raise LibraryError("the export has no list of items")
    if len(items) > MAX_IMPORT_ITEMS:
        raise LibraryError(f"an import can hold at most {MAX_IMPORT_ITEMS} items")
    records = []
    unsafe = 0
    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise LibraryError(f"item {position} is not an object")
        if not _is_safe_link(item.get("url")):
            unsafe += 1
            continue
        try:
            records.append(_json_record(item))
        except LibraryError as error:
            raise LibraryError(f"item {position}: {error}") from error
    return _import_list(library, records, unsafe)


def _bookmark_time(raw) -> float | None:
    if not raw or not BOOKMARK_TIME_DIGITS.fullmatch(raw.strip()):
        return None
    value = float(int(raw.strip()))
    while value > LARGEST_SECONDS:
        value /= 1000
    return showable_timestamp(value)


def _lenient_tags(raw) -> list[str]:
    words = [word.strip() for word in (raw or "").split(",")]
    usable = [word for word in words if word and len(word) <= MAX_TAG]
    return clean_tags(usable[:MAX_TAGS_PER_ITEM])


class _BookmarkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.folders = []
        self.pending_folder = None
        self.archived = False
        self.capture = None
        self.buffer = []
        self.link_attributes = {}

    def _start_capture(self, kind: str) -> None:
        self.capture = kind
        self.buffer = []

    def _captured(self) -> str:
        text = tidy_text("".join(self.buffer))
        self.capture = None
        self.buffer = []
        return text

    def _finish_note(self) -> None:
        if self.capture != "dd":
            return
        note = "".join(self.buffer).strip()
        self.capture = None
        self.buffer = []
        if note and self.links:
            self.links[-1]["note"] = note[:MAX_NOTE]

    def handle_starttag(self, tag, attrs):
        if tag in NOTE_ENDING_TAGS:
            self._finish_note()
        if tag == "h3":
            self._start_capture("h3")
        elif tag in ("h1", "h2"):
            self._start_capture("heading")
        elif tag == "dl":
            self.folders.append(self.pending_folder)
            self.pending_folder = None
        elif tag == "a":
            self.pending_folder = None
            self.link_attributes = {name.lower(): value or "" for name, value in attrs}
            self._start_capture("a")
        elif tag == "dd":
            self._start_capture("dd")

    def handle_endtag(self, tag):
        if tag == "h3" and self.capture == "h3":
            self.pending_folder = self._captured()[:MAX_COLLECTION_NAME] or None
        elif tag in ("h1", "h2") and self.capture == "heading":
            self.archived = self._captured().casefold() in ARCHIVE_HEADINGS
        elif tag == "a" and self.capture == "a":
            self._finish_link()
        elif tag == "dl":
            self._finish_note()
            if self.folders:
                self.folders.pop()

    def handle_data(self, data):
        if self.capture:
            self.buffer.append(data)

    def close(self):
        super().close()
        self._finish_note()

    def _finish_link(self) -> None:
        attributes = self.link_attributes
        self.links.append({
            "url": attributes.get("href", "").strip(),
            "title": self._captured()[:MAX_TITLE],
            "createdAt": _bookmark_time(attributes.get("add_date") or attributes.get("time_added")),
            "tags": _lenient_tags(attributes.get("tags")),
            "collection": next((folder for folder in self.folders if folder), None),
            "archived": self.archived,
            "note": "",
        })


def _bookmark_record(link: dict) -> dict:
    return {
        "url": link["url"], "title": link["title"], "siteName": "", "author": "", "language": "", "excerpt": "",
        "contentHtml": "", "fetchError": "", "note": link["note"], "tags": link["tags"], "collection": link["collection"],
        "favorite": False, "archived": link["archived"], "loadImages": False, "progress": 0.0, "publishedAt": None,
        "createdAt": link["createdAt"], "archivedAt": None, "highlights": [],
    }


def import_bookmarks_html(library, text: str) -> dict:
    parser = _BookmarkParser()
    parser.feed(text)
    parser.close()
    if not parser.links:
        raise LibraryError("no bookmarks were found in that file")
    if len(parser.links) > MAX_IMPORT_ITEMS:
        raise LibraryError(f"an import can hold at most {MAX_IMPORT_ITEMS} items")
    safe = [link for link in parser.links if _is_safe_link(link["url"])]
    return _import_list(library, [_bookmark_record(link) for link in safe], len(parser.links) - len(safe))


def import_any(library, body: bytes) -> dict:
    text = decode_page(body or b"", "text/html").lstrip("﻿ \t\r\n")
    if not text:
        raise LibraryError("the file is empty")
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except (ValueError, RecursionError) as error:
            raise LibraryError(f"the JSON file could not be read: {error}") from error
        return import_json(library, payload)
    return import_bookmarks_html(library, text)
