import math
import re
import sqlite3
import time
from datetime import datetime, timezone

from .extract import address_title, host_name, reading_minutes
from .sanitize import html_to_text
from .timestamps import showable_timestamp

VIEWS = ("inbox", "favorites", "archive", "all", "broken")
VIEW_CLAUSES = {
    "inbox": "i.is_archived = 0",
    "favorites": "i.is_favorite = 1",
    "archive": "i.is_archived = 1",
    "all": "",
    "broken": "i.link_status = 'dead'",
}
LINK_STATUSES = ("ok", "dead", "error")
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500
MAX_TITLE = 300
MAX_URL_LENGTH = 2048
MAX_NOTE = 20_000
MAX_TAG = 50
MAX_TAGS_PER_ITEM = 20
MAX_COLLECTION_NAME = 100
MAX_HIGHLIGHT = 5000
MAX_HIGHLIGHTS_PER_ITEM = 500
MAX_ERROR_LENGTH = 500
MAX_SEARCH_TEXT = 200_000
MAX_SEARCH_TERMS = 12
PROGRESS_DIGITS = 4
TAG_SEPARATOR = "\x1f"
SEARCH_TERM = re.compile(r"\w+", re.UNICODE)
WHITESPACE = re.compile(r"\s+")
LARGEST_DATABASE_INTEGER = 2 ** 63 - 1
SMALLEST_DATABASE_INTEGER = -(2 ** 63)

ITEM_COLUMNS = """
    i.id, i.url, i.title, i.site_name, i.excerpt, i.word_count, i.reading_minutes, i.collection_id, i.is_favorite,
    i.is_archived, i.load_images, i.progress, i.search_text != '' AS has_text, i.fetch_error, i.link_status, i.link_detail,
    i.link_checked_at, i.created_at, i.published_at,
    (SELECT group_concat(t.name, char(31)) FROM item_tags it JOIN tags t ON t.id = it.tag_id WHERE it.item_id = i.id) AS tag_names,
    (SELECT COUNT(*) FROM highlights h WHERE h.item_id = i.id) AS highlight_count
"""
DETAIL_COLUMNS = "i.author, i.language, i.content_html, i.note, i.archived_at, i.fetched_at"


class LibraryError(ValueError):
    def __init__(self, message: str, status: int = 400, item_id: int | None = None):
        super().__init__(message)
        self.status = status
        self.item_id = item_id


def iso(timestamp):
    if showable_timestamp(timestamp) is None:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def is_whole_number(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and SMALLEST_DATABASE_INTEGER <= value <= LARGEST_DATABASE_INTEGER


def squeeze(text: str) -> str:
    return "".join(text.split())


def tidy_text(text: str) -> str:
    return WHITESPACE.sub(" ", text).strip()


def _require_bool(name: str, value) -> int:
    if not isinstance(value, bool):
        raise LibraryError(f"{name} must be true or false")
    return int(value)


def clean_progress(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise LibraryError("progress must be a number from 0 to 1")
    return round(float(value), PROGRESS_DIGITS)


def _title(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LibraryError("the title cannot be empty")
    return tidy_text(value)[:MAX_TITLE]


def clean_note(value) -> str:
    if not isinstance(value, str):
        raise LibraryError("the note must be text")
    if len(value) > MAX_NOTE:
        raise LibraryError(f"notes can be at most {MAX_NOTE} characters")
    return value


def collection_name(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LibraryError("give the collection a name")
    name = tidy_text(value)
    if len(name) > MAX_COLLECTION_NAME:
        raise LibraryError(f"collection names can be at most {MAX_COLLECTION_NAME} characters")
    return name


def clean_tags(value) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise LibraryError("tags must be a list of words")
    tags = {}
    for raw in value:
        if not isinstance(raw, str):
            raise LibraryError("each tag must be text")
        tag = tidy_text(raw).lstrip("#").strip()
        if not tag:
            continue
        if len(tag) > MAX_TAG:
            raise LibraryError(f"tags can be at most {MAX_TAG} characters")
        tags.setdefault(tag.casefold(), tag)
    if len(tags) > MAX_TAGS_PER_ITEM:
        raise LibraryError(f"an item can have at most {MAX_TAGS_PER_ITEM} tags")
    return list(tags.values())


def _fts_query(text: str) -> str:
    terms = SEARCH_TERM.findall(text or "")[:MAX_SEARCH_TERMS]
    return " ".join(f'"{term}"*' for term in terms)


def _parse_cursor(cursor: str):
    try:
        created_at, item_id = cursor.split(":", 1)
        position, number = float(created_at), int(item_id)
    except (AttributeError, ValueError) as error:
        raise LibraryError("that page cursor is not valid") from error
    if not math.isfinite(position) or not is_whole_number(number):
        raise LibraryError("that page cursor is not valid")
    return position, number


def _require_view(view: str) -> str:
    if view not in VIEWS:
        raise LibraryError(f"view must be one of {', '.join(VIEWS)}")
    return VIEW_CLAUSES[view]


def _item_dict(row) -> dict:
    tags = sorted(row["tag_names"].split(TAG_SEPARATOR), key=str.casefold) if row["tag_names"] else []
    return {
        "id": row["id"],
        "url": row["url"],
        "title": row["title"],
        "siteName": row["site_name"],
        "excerpt": row["excerpt"],
        "wordCount": row["word_count"],
        "readingMinutes": row["reading_minutes"],
        "collectionId": row["collection_id"],
        "favorite": bool(row["is_favorite"]),
        "archived": bool(row["is_archived"]),
        "loadImages": bool(row["load_images"]),
        "progress": row["progress"],
        "hasText": bool(row["has_text"]),
        "fetchError": row["fetch_error"],
        "linkStatus": row["link_status"],
        "linkDetail": row["link_detail"],
        "linkCheckedAt": iso(row["link_checked_at"]),
        "createdAt": iso(row["created_at"]),
        "publishedAt": iso(row["published_at"]),
        "sortAt": row["created_at"],
        "tags": tags,
        "highlightCount": row["highlight_count"],
    }


def _highlight_dict(row) -> dict:
    return {"id": row["id"], "itemId": row["item_id"], "text": row["text"], "createdAt": iso(row["created_at"])}


def headline(item: dict) -> dict:
    return {
        "id": item["id"],
        "title": item["title"],
        "url": item["url"],
        "site": item["siteName"],
        "excerpt": item["excerpt"],
        "readingMinutes": item["readingMinutes"],
        "progress": item["progress"],
        "favorite": item["favorite"],
        "tags": item["tags"],
        "savedAt": item["createdAt"],
    }


def article_columns(article) -> dict:
    return {
        "site_name": article.site_name,
        "author": article.author,
        "published_at": article.published_at,
        "language": article.language,
        "excerpt": article.excerpt,
        "content_html": article.content_html,
        "search_text": article.text[:MAX_SEARCH_TEXT],
        "word_count": article.word_count,
        "reading_minutes": article.reading_minutes,
    }


class Library:
    def __init__(self, database, clock=time.time):
        self.database = database
        self.clock = clock

    def _item_row(self, connection, item_id: int, detail: bool = False):
        columns = f"{ITEM_COLUMNS}, {DETAIL_COLUMNS}" if detail else ITEM_COLUMNS
        row = connection.execute(f"SELECT {columns} FROM items i WHERE i.id = ?", (item_id,)).fetchone()
        if row is None:
            raise LibraryError("that item does not exist", 404)
        return row

    def _require_collection(self, connection, collection_id) -> None:
        if collection_id is None:
            return
        if not is_whole_number(collection_id):
            raise LibraryError("collectionId must be a number or null")
        if connection.execute("SELECT 1 FROM collections WHERE id = ?", (collection_id,)).fetchone() is None:
            raise LibraryError("that collection does not exist")

    def check_new_item(self, tags=(), collection_id=None) -> list[str]:
        cleaned = clean_tags(tags)
        with self.database.connect() as connection:
            self._require_collection(connection, collection_id)
        return cleaned

    def find_by_url(self, url: str) -> int | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT id FROM items WHERE url = ?", (url,)).fetchone()
        return None if row is None else row["id"]

    def _set_tags(self, connection, item_id: int, names: list[str]) -> None:
        connection.execute("DELETE FROM item_tags WHERE item_id = ?", (item_id,))
        for name in names:
            connection.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
            tag_id = connection.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
            connection.execute("INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?)", (item_id, tag_id))

    @staticmethod
    def _drop_unused_tags(connection) -> None:
        connection.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM item_tags)")

    def add_item(self, url: str, article=None, title: str = "", fetch_error: str = "", tags=(),
                 collection_id=None, created_at: float | None = None) -> dict:
        if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
            raise LibraryError("give a web address of at most 2048 characters")
        names = clean_tags(tags)
        chosen_title = tidy_text(title or "")[:MAX_TITLE] or (article.title if article else "") or address_title(url)
        now = self.clock()
        columns = {
            "url": url, "title": chosen_title, "site_name": host_name(url), "fetch_error": fetch_error[:MAX_ERROR_LENGTH],
            "collection_id": collection_id, "created_at": created_at if created_at is not None else now, "updated_at": now,
        }
        if article is not None:
            columns.update(article_columns(article))
            columns["fetched_at"] = now
        with self.database.connect() as connection:
            self._require_collection(connection, collection_id)
            existing = connection.execute("SELECT id FROM items WHERE url = ?", (url,)).fetchone()
            if existing is not None:
                raise LibraryError("that address is already saved", 409, item_id=existing["id"])
            names_sql = ", ".join(columns)
            marks = ", ".join("?" for _ in columns)
            cursor = connection.execute(f"INSERT INTO items ({names_sql}) VALUES ({marks})", tuple(columns.values()))
            self._set_tags(connection, cursor.lastrowid, names)
            return _item_dict(self._item_row(connection, cursor.lastrowid))

    def get_item(self, item_id: int) -> dict:
        with self.database.connect() as connection:
            row = self._item_row(connection, item_id, detail=True)
            highlights = connection.execute(
                "SELECT id, item_id, text, created_at FROM highlights WHERE item_id = ? ORDER BY position, id", (item_id,)
            ).fetchall()
        return {
            **_item_dict(row),
            "author": row["author"],
            "language": row["language"],
            "contentHtml": row["content_html"],
            "note": row["note"],
            "archivedAt": iso(row["archived_at"]),
            "fetchedAt": iso(row["fetched_at"]),
            "highlights": [_highlight_dict(highlight) for highlight in highlights],
        }

    def _changed_columns(self, connection, row, changes: dict) -> tuple[dict, list[str] | None]:
        columns = {}
        tags = None
        for key, value in changes.items():
            if key == "title":
                columns["title"] = _title(value)
            elif key == "favorite":
                columns["is_favorite"] = _require_bool("favorite", value)
            elif key == "archived":
                columns["is_archived"] = _require_bool("archived", value)
                if value and not row["is_archived"]:
                    columns["archived_at"] = self.clock()
                elif not value:
                    columns["archived_at"] = None
            elif key == "loadImages":
                columns["load_images"] = _require_bool("loadImages", value)
            elif key == "progress":
                columns["progress"] = clean_progress(value)
            elif key == "note":
                columns["note"] = clean_note(value)
            elif key == "collectionId":
                self._require_collection(connection, value)
                columns["collection_id"] = value
            elif key == "tags":
                tags = clean_tags(value)
            else:
                raise LibraryError(f"{key} cannot be changed")
        return columns, tags

    def update_item(self, item_id: int, changes) -> dict:
        if not isinstance(changes, dict) or not changes:
            raise LibraryError("say what to change")
        with self.database.connect() as connection:
            row = self._item_row(connection, item_id)
            columns, tags = self._changed_columns(connection, row, changes)
            columns["updated_at"] = self.clock()
            assignments = ", ".join(f"{column} = ?" for column in columns)
            connection.execute(f"UPDATE items SET {assignments} WHERE id = ?", (*columns.values(), item_id))
            if tags is not None:
                self._set_tags(connection, item_id, tags)
                self._drop_unused_tags(connection)
        return self.get_item(item_id)

    def store_article(self, item_id: int, article) -> dict:
        columns = article_columns(article)
        now = self.clock()
        columns.update({"fetched_at": now, "fetch_error": "", "updated_at": now})
        with self.database.connect() as connection:
            row = self._item_row(connection, item_id)
            if row["title"] == address_title(row["url"]) and article.title:
                columns["title"] = article.title
            assignments = ", ".join(f"{column} = ?" for column in columns)
            connection.execute(f"UPDATE items SET {assignments} WHERE id = ?", (*columns.values(), item_id))
        return self.get_item(item_id)

    def record_fetch_error(self, item_id: int, message: str) -> dict:
        with self.database.connect() as connection:
            self._item_row(connection, item_id)
            connection.execute("UPDATE items SET fetch_error = ?, updated_at = ? WHERE id = ?",
                               (message[:MAX_ERROR_LENGTH], self.clock(), item_id))
        return self.get_item(item_id)

    def record_link_status(self, item_id: int, status: str, detail: str) -> dict:
        if status not in LINK_STATUSES:
            raise LibraryError(f"link status must be one of {', '.join(LINK_STATUSES)}")
        with self.database.connect() as connection:
            self._item_row(connection, item_id)
            connection.execute("UPDATE items SET link_status = ?, link_detail = ?, link_checked_at = ? WHERE id = ?",
                               (status, detail[:MAX_ERROR_LENGTH], self.clock(), item_id))
        return self.get_item(item_id)

    def delete_item(self, item_id: int) -> None:
        with self.database.connect() as connection:
            if connection.execute("DELETE FROM items WHERE id = ?", (item_id,)).rowcount == 0:
                raise LibraryError("that item does not exist", 404)
            self._drop_unused_tags(connection)

    def _scope(self, view: str, collection_id, tag) -> tuple[list[str], list]:
        clause = _require_view(view)
        clauses = [clause] if clause else []
        params = []
        if collection_id is not None:
            if not is_whole_number(collection_id):
                raise LibraryError("collection must be a number")
            clauses.append("i.collection_id = ?")
            params.append(collection_id)
        if tag:
            clauses.append("i.id IN (SELECT it.item_id FROM item_tags it JOIN tags t ON t.id = it.tag_id WHERE t.name = ?)")
            params.append(tag)
        return clauses, params

    def list_items(self, view: str = "inbox", collection_id=None, tag: str = "", query: str = "", before: str = "",
                   limit: int = DEFAULT_PAGE_SIZE) -> dict:
        if not is_whole_number(limit) or not 1 <= limit <= MAX_PAGE_SIZE:
            raise LibraryError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        clauses, params = self._scope(view, collection_id, tag)
        if query and query.strip():
            match = _fts_query(query)
            if not match:
                return {"items": [], "next": None}
            clauses.append("i.id IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)")
            params.append(match)
        if before:
            clauses.append("(i.created_at, i.id) < (?, ?)")
            params.extend(_parse_cursor(before))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT {ITEM_COLUMNS} FROM items i {where} ORDER BY i.created_at DESC, i.id DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        items = [_item_dict(row) for row in rows[:limit]]
        next_cursor = f"{items[-1]['sortAt']!r}:{items[-1]['id']}" if len(rows) > limit else None
        return {"items": items, "next": next_cursor}

    def counts(self) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("""
                SELECT COALESCE(SUM(is_archived = 0), 0) AS inbox, COALESCE(SUM(is_favorite = 1), 0) AS favorites,
                       COALESCE(SUM(is_archived = 1), 0) AS archive, COUNT(*) AS everything,
                       COALESCE(SUM(link_status = 'dead'), 0) AS broken
                FROM items
            """).fetchone()
        return {"inbox": row["inbox"], "favorites": row["favorites"], "archive": row["archive"], "all": row["everything"],
                "broken": row["broken"]}

    def headlines(self, limit: int) -> list[dict]:
        return [headline(item) for item in self.list_items("inbox", limit=limit)["items"]]

    def search_headlines(self, query: str, limit: int) -> list[dict]:
        return [headline(item) for item in self.list_items("all", query=query, limit=limit)["items"]]

    def items_to_check(self, view: str = "all") -> list[dict]:
        clause = _require_view(view)
        where = f"WHERE {clause}" if clause else ""
        with self.database.connect() as connection:
            rows = connection.execute(f"SELECT i.id, i.url FROM items i {where} ORDER BY i.created_at DESC, i.id DESC").fetchall()
        return [{"id": row["id"], "url": row["url"]} for row in rows]

    def list_tags(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute("""
                SELECT t.name, COUNT(it.item_id) AS count FROM tags t JOIN item_tags it ON it.tag_id = t.id
                GROUP BY t.id ORDER BY t.name COLLATE NOCASE
            """).fetchall()
        return [{"name": row["name"], "count": row["count"]} for row in rows]

    def list_collections(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute("""
                SELECT c.id, c.name, (SELECT COUNT(*) FROM items i WHERE i.collection_id = c.id) AS count
                FROM collections c ORDER BY c.name COLLATE NOCASE
            """).fetchall()
        return [{"id": row["id"], "name": row["name"], "count": row["count"]} for row in rows]

    def _collection(self, connection, collection_id: int) -> dict:
        row = connection.execute("SELECT id, name FROM collections WHERE id = ?", (collection_id,)).fetchone()
        if row is None:
            raise LibraryError("that collection does not exist", 404)
        return {"id": row["id"], "name": row["name"]}

    def create_collection(self, name) -> dict:
        clean = collection_name(name)
        try:
            with self.database.connect() as connection:
                cursor = connection.execute("INSERT INTO collections (name, created_at) VALUES (?, ?)", (clean, self.clock()))
                return self._collection(connection, cursor.lastrowid)
        except sqlite3.IntegrityError as error:
            raise LibraryError(f"there is already a collection called {clean}", 409) from error

    def rename_collection(self, collection_id: int, name) -> dict:
        clean = collection_name(name)
        try:
            with self.database.connect() as connection:
                self._collection(connection, collection_id)
                connection.execute("UPDATE collections SET name = ? WHERE id = ?", (clean, collection_id))
                return self._collection(connection, collection_id)
        except sqlite3.IntegrityError as error:
            raise LibraryError(f"there is already a collection called {clean}", 409) from error

    def delete_collection(self, collection_id: int) -> None:
        with self.database.connect() as connection:
            if connection.execute("DELETE FROM collections WHERE id = ?", (collection_id,)).rowcount == 0:
                raise LibraryError("that collection does not exist", 404)

    def add_highlight(self, item_id: int, text) -> dict:
        if not isinstance(text, str) or not text.strip():
            raise LibraryError("select some text to highlight")
        clean = tidy_text(text)
        if len(clean) > MAX_HIGHLIGHT:
            raise LibraryError(f"a highlight can be at most {MAX_HIGHLIGHT} characters")
        with self.database.connect() as connection:
            row = connection.execute("SELECT search_text FROM items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise LibraryError("that item does not exist", 404)
            if not row["search_text"]:
                raise LibraryError("this item has no saved text to highlight")
            position = squeeze(row["search_text"]).find(squeeze(clean))
            if position < 0:
                raise LibraryError("that text is not in the saved article")
            if connection.execute("SELECT 1 FROM highlights WHERE item_id = ? AND text = ?", (item_id, clean)).fetchone():
                raise LibraryError("that text is already highlighted", 409)
            count = connection.execute("SELECT COUNT(*) FROM highlights WHERE item_id = ?", (item_id,)).fetchone()[0]
            if count >= MAX_HIGHLIGHTS_PER_ITEM:
                raise LibraryError(f"an item can have at most {MAX_HIGHLIGHTS_PER_ITEM} highlights")
            cursor = connection.execute("INSERT INTO highlights (item_id, text, position, created_at) VALUES (?, ?, ?, ?)",
                                        (item_id, clean, position, self.clock()))
            created = connection.execute("SELECT id, item_id, text, created_at FROM highlights WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _highlight_dict(created)

    def delete_highlight(self, highlight_id: int) -> None:
        with self.database.connect() as connection:
            if connection.execute("DELETE FROM highlights WHERE id = ?", (highlight_id,)).rowcount == 0:
                raise LibraryError("that highlight does not exist", 404)

    def export_records(self) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(f"""
                SELECT {ITEM_COLUMNS}, {DETAIL_COLUMNS}, (SELECT c.name FROM collections c WHERE c.id = i.collection_id) AS collection_name
                FROM items i ORDER BY i.created_at DESC, i.id DESC
            """).fetchall()
            highlights = connection.execute("SELECT item_id, text, created_at FROM highlights ORDER BY position, id").fetchall()
        by_item = {}
        for highlight in highlights:
            by_item.setdefault(highlight["item_id"], []).append({"text": highlight["text"], "createdAt": highlight["created_at"]})
        return [{
            "url": row["url"], "title": row["title"], "siteName": row["site_name"], "author": row["author"],
            "language": row["language"], "excerpt": row["excerpt"], "publishedAt": row["published_at"],
            "createdAt": row["created_at"], "archivedAt": row["archived_at"], "favorite": bool(row["is_favorite"]),
            "archived": bool(row["is_archived"]), "loadImages": bool(row["load_images"]), "progress": row["progress"],
            "note": row["note"], "tags": _item_dict(row)["tags"], "collection": row["collection_name"],
            "contentHtml": row["content_html"], "fetchError": row["fetch_error"], "highlights": by_item.get(row["id"], []),
        } for row in rows]

    def _collection_id_for(self, connection, name, made: list) -> int | None:
        if name is None:
            return None
        row = connection.execute("SELECT id FROM collections WHERE name = ?", (name,)).fetchone()
        if row is not None:
            return row["id"]
        made.append(name)
        return connection.execute("INSERT INTO collections (name, created_at) VALUES (?, ?)", (name, self.clock())).lastrowid

    def _insert_record(self, connection, record: dict, collection_id) -> None:
        now = self.clock()
        text = html_to_text(record["contentHtml"])[:MAX_SEARCH_TEXT] if record["contentHtml"] else ""
        words = len(text.split())
        columns = {
            "url": record["url"], "title": record["title"] or address_title(record["url"]),
            "site_name": record["siteName"] or host_name(record["url"]), "author": record["author"],
            "published_at": record["publishedAt"], "language": record["language"], "excerpt": record["excerpt"],
            "content_html": record["contentHtml"], "search_text": text, "word_count": words,
            "reading_minutes": reading_minutes(words), "note": record["note"], "collection_id": collection_id,
            "is_favorite": int(record["favorite"]), "is_archived": int(record["archived"]),
            "load_images": int(record["loadImages"]), "progress": record["progress"], "fetch_error": record["fetchError"],
            "fetched_at": now if text else None, "created_at": record["createdAt"] if record["createdAt"] is not None else now,
            "updated_at": now, "archived_at": (record["archivedAt"] or now) if record["archived"] else None,
        }
        names_sql = ", ".join(columns)
        marks = ", ".join("?" for _ in columns)
        item_id = connection.execute(f"INSERT INTO items ({names_sql}) VALUES ({marks})", tuple(columns.values())).lastrowid
        self._set_tags(connection, item_id, record["tags"])
        squeezed = squeeze(text)
        for highlight in record["highlights"][:MAX_HIGHLIGHTS_PER_ITEM]:
            connection.execute("INSERT INTO highlights (item_id, text, position, created_at) VALUES (?, ?, ?, ?)",
                               (item_id, highlight["text"], squeezed.find(squeeze(highlight["text"])),
                                highlight["createdAt"] if highlight["createdAt"] is not None else now))

    def import_records(self, records: list[dict]) -> dict:
        added = skipped = 0
        made = []
        seen = set()
        with self.database.connect() as connection:
            for record in records:
                is_known = connection.execute("SELECT 1 FROM items WHERE url = ?", (record["url"],)).fetchone() is not None
                if record["url"] in seen or is_known:
                    skipped += 1
                    continue
                seen.add(record["url"])
                self._insert_record(connection, record, self._collection_id_for(connection, record["collection"], made))
                added += 1
        return {"added": added, "skipped": skipped, "collections": len(made)}
