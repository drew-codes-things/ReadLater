import sqlite3
from contextlib import contextmanager
from pathlib import Path

BUSY_TIMEOUT_SECONDS = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    site_name TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    published_at REAL,
    language TEXT NOT NULL DEFAULT '',
    excerpt TEXT NOT NULL DEFAULT '',
    content_html TEXT NOT NULL DEFAULT '',
    search_text TEXT NOT NULL DEFAULT '',
    word_count INTEGER NOT NULL DEFAULT 0,
    reading_minutes INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    collection_id INTEGER REFERENCES collections(id) ON DELETE SET NULL,
    is_favorite INTEGER NOT NULL DEFAULT 0,
    is_archived INTEGER NOT NULL DEFAULT 0,
    load_images INTEGER NOT NULL DEFAULT 0,
    progress REAL NOT NULL DEFAULT 0,
    fetched_at REAL,
    fetch_error TEXT NOT NULL DEFAULT '',
    link_status TEXT NOT NULL DEFAULT '',
    link_detail TEXT NOT NULL DEFAULT '',
    link_checked_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    archived_at REAL
);
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS item_tags (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (item_id, tag_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS highlights (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    position INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS items_by_time ON items (created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS items_by_state ON items (is_archived, is_favorite);
CREATE INDEX IF NOT EXISTS items_by_collection ON items (collection_id);
CREATE INDEX IF NOT EXISTS item_tags_by_tag ON item_tags (tag_id);
CREATE INDEX IF NOT EXISTS highlights_by_item ON highlights (item_id, position);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, site_name, search_text, note, content='items', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS items_fts_insert AFTER INSERT ON items BEGIN
    INSERT INTO items_fts (rowid, title, site_name, search_text, note) VALUES (new.id, new.title, new.site_name, new.search_text, new.note);
END;
CREATE TRIGGER IF NOT EXISTS items_fts_delete AFTER DELETE ON items BEGIN
    INSERT INTO items_fts (items_fts, rowid, title, site_name, search_text, note) VALUES ('delete', old.id, old.title, old.site_name, old.search_text, old.note);
END;
CREATE TRIGGER IF NOT EXISTS items_fts_update AFTER UPDATE OF title, site_name, search_text, note ON items BEGIN
    INSERT INTO items_fts (items_fts, rowid, title, site_name, search_text, note) VALUES ('delete', old.id, old.title, old.site_name, old.search_text, old.note);
    INSERT INTO items_fts (rowid, title, site_name, search_text, note) VALUES (new.id, new.title, new.site_name, new.search_text, new.note);
END;
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def connect(self):
        connection = self._open()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def is_reachable(self) -> bool:
        try:
            with self.connect() as connection:
                connection.execute("SELECT COUNT(*) FROM items").fetchone()
        except sqlite3.Error:
            return False
        return True
