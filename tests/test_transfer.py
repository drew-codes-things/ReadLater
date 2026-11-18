import json
import threading

import pytest
from conftest import FakeClock, fixture_bytes

from readlater.db import Database
from readlater.extract import Article
from readlater.library import Library, LibraryError, iso
from readlater.transfer import (
    MAX_IMPORT_ITEMS,
    export_bookmarks_html,
    export_json,
    import_any,
    import_bookmarks_html,
    import_json,
)

TEXT = "Building a bird box is simple. You need one plank of untreated pine and a handful of screws."


def sample_article():
    return Article(title="How to build a bird box", site_name="Workshop", author="Sam", published_at=1_799_000_000.0,
                   language="en", excerpt="A weekend project.", content_html=f"<div><p>{TEXT}</p></div>", text=TEXT,
                   word_count=18, reading_minutes=1)


@pytest.fixture
def other(tmp_path):
    return Library(Database(tmp_path / "other.db"), clock=FakeClock(1_900_000_000.0))


def fill(library):
    woodwork = library.create_collection("Woodwork")
    bird = library.add_item("https://workshop.example.test/bird-box", article=sample_article(), tags=["birds", "pine"],
                            collection_id=woodwork["id"])
    library.update_item(bird["id"], {"favorite": True, "progress": 0.5, "note": "Try cedar next time.", "loadImages": True})
    library.add_highlight(bird["id"], "one plank of untreated pine")
    link = library.add_item("https://files.example.test/manual.pdf", title='Manual <script>alert("x")</script> & more',
                            fetch_error="not a web page (application/pdf), kept as a link")
    library.update_item(link["id"], {"archived": True})
    return bird, link


def test_json_export_holds_everything(library):
    fill(library)
    exported = export_json(library)
    assert exported["app"] == "read-later" and exported["version"] == 1
    assert exported["exportedAt"].endswith("Z")
    assert exported["collections"] == ["Woodwork"]
    bird = next(item for item in exported["items"] if item["url"].endswith("bird-box"))
    assert bird["collection"] == "Woodwork"
    assert bird["tags"] == ["birds", "pine"]
    assert bird["favorite"] is True and bird["archived"] is False and bird["progress"] == 0.5
    assert bird["note"] == "Try cedar next time."
    assert bird["contentHtml"].startswith("<div><p>Building")
    assert [highlight["text"] for highlight in bird["highlights"]] == ["one plank of untreated pine"]
    json.dumps(exported)


def test_json_round_trip_into_an_empty_library(library, other):
    fill(library)
    exported = export_json(library)
    summary = import_json(other, exported)
    assert summary == {"added": 2, "skipped": 0, "collections": 1}
    items = {item["url"]: item for item in other.list_items("all")["items"]}
    bird = other.get_item(items["https://workshop.example.test/bird-box"]["id"])
    assert bird["title"] == "How to build a bird box"
    assert bird["tags"] == ["birds", "pine"]
    assert bird["favorite"] is True and bird["progress"] == 0.5 and bird["loadImages"] is True
    assert bird["note"] == "Try cedar next time."
    assert bird["wordCount"] == 18 and bird["readingMinutes"] == 1
    assert bird["createdAt"] == next(item["createdAt"] for item in exported["items"] if item["url"] == bird["url"])
    assert bird["createdAt"] != "2030-03-17T17:46:40Z"
    assert [highlight["text"] for highlight in bird["highlights"]] == ["one plank of untreated pine"]
    assert other.list_collections()[0]["name"] == "Woodwork"
    assert other.list_items("all", query="plank")["items"][0]["id"] == bird["id"]
    manual = other.get_item(items["https://files.example.test/manual.pdf"]["id"])
    assert manual["archived"] is True and manual["hasText"] is False
    assert manual["fetchError"] == "not a web page (application/pdf), kept as a link"


def test_json_import_skips_saved_and_repeated_addresses(library):
    fill(library)
    payload = export_json(library)
    payload["items"].append(dict(payload["items"][0], url="https://new.example.test/"))
    payload["items"].append(dict(payload["items"][0], url="https://new.example.test/"))
    payload["items"].append(dict(payload["items"][0], url="ftp://old.example.test/"))
    assert import_json(library, payload) == {"added": 1, "skipped": 4, "collections": 0}


@pytest.mark.parametrize("payload", [
    [], {"app": "keep-notes", "version": 1, "items": []}, {"app": "read-later", "version": 2, "items": []},
    {"app": "read-later", "version": 1}, {"app": "read-later", "version": 1, "items": ["x"]},
    {"app": "read-later", "version": 1, "items": [{"url": "https://a.example.test/", "tags": "x"}]},
    {"app": "read-later", "version": 1, "items": [{"url": "https://a.example.test/", "progress": 3}]},
    {"app": "read-later", "version": 1, "items": [{"url": "https://a.example.test/", "createdAt": "yesterday"}]},
    {"app": "read-later", "version": 1, "items": [{"url": "https://a.example.test/", "highlights": "x"}]},
    {"app": "read-later", "version": 1, "items": [{"url": "https://a.example.test/", "contentHtml": 5}]},
])
def test_bad_json_imports_are_refused_whole(library, payload):
    with pytest.raises(LibraryError):
        import_json(library, payload)
    assert library.counts()["all"] == 0


def test_too_many_items_are_refused(library):
    payload = {"app": "read-later", "version": 1, "items": [{"url": f"https://a.example.test/{n}"} for n in range(MAX_IMPORT_ITEMS + 1)]}
    with pytest.raises(LibraryError):
        import_json(library, payload)


def test_bookmarks_html_export_is_netscape_format_and_escaped(library):
    fill(library)
    html = export_bookmarks_html(library)
    assert html.startswith("<!DOCTYPE NETSCAPE-Bookmark-file-1>")
    assert "<DT><H3>Woodwork</H3>" in html
    assert 'HREF="https://workshop.example.test/bird-box"' in html
    assert 'TAGS="birds,pine"' in html
    assert "<DD>Try cedar next time." in html
    assert "<script>" not in html
    assert "Manual &lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; more" in html


def test_bookmarks_html_round_trip(library, other):
    fill(library)
    summary = import_bookmarks_html(other, export_bookmarks_html(library))
    assert summary == {"added": 2, "skipped": 0, "collections": 1}
    items = {item["url"]: item for item in other.list_items("all")["items"]}
    bird = other.get_item(items["https://workshop.example.test/bird-box"]["id"])
    assert bird["tags"] == ["birds", "pine"]
    assert bird["collectionId"] == other.list_collections()[0]["id"]
    assert bird["note"] == "Try cedar next time."
    assert items["https://files.example.test/manual.pdf"]["title"] == 'Manual <script>alert("x")</script> & more'


def test_browser_bookmarks_import(library):
    summary = import_bookmarks_html(library, fixture_bytes("bookmarks.html").decode())
    assert summary == {"added": 4, "skipped": 3, "collections": 2}
    items = {item["url"]: item for item in library.list_items("all")["items"]}
    assert set(items) == {"https://loose.example.test/", "https://workshop.example.test/bird-box",
                          "https://workshop.example.test/saws", "https://food.example.test/bread"}
    assert items["https://loose.example.test/"]["title"] == "Loose & free"
    assert items["https://loose.example.test/"]["collectionId"] is None
    assert items["https://loose.example.test/"]["createdAt"] == "2023-11-14T22:13:20Z"
    names = {collection["id"]: collection["name"] for collection in library.list_collections()}
    assert names[items["https://workshop.example.test/saws"]["collectionId"]] == "Woodwork"
    assert names[items["https://food.example.test/bread"]["collectionId"]] == "Recipes"
    assert items["https://workshop.example.test/saws"]["createdAt"] == "2023-11-14T22:16:40Z"
    bird = library.get_item(items["https://workshop.example.test/bird-box"]["id"])
    assert bird["tags"] == ["birds", "pine"]
    assert bird["note"] == "Try cedar next time"
    assert bird["hasText"] is False


def test_pocket_export_import_keeps_the_archive(library):
    summary = import_bookmarks_html(library, fixture_bytes("pocket.html").decode())
    assert summary == {"added": 3, "skipped": 0, "collections": 0}
    archived = library.list_items("archive")["items"]
    assert [item["title"] for item in archived] == ["Lighthouse repainted"]
    assert archived[0]["tags"] == ["harbour", "history"]
    assert [item["title"] for item in library.list_items("inbox")["items"]] == ["Ferry timetable", "Storm warning"]


def test_import_any_tells_json_from_html(library):
    assert import_any(library, fixture_bytes("pocket.html"))["added"] == 3
    payload = {"app": "read-later", "version": 1, "items": [{"url": "https://json.example.test/", "title": "From JSON"}]}
    assert import_any(library, b"\xef\xbb\xbf  " + json.dumps(payload).encode())["added"] == 1
    with pytest.raises(LibraryError):
        import_any(library, b"{not json")
    with pytest.raises(LibraryError):
        import_any(library, b"")
    with pytest.raises(LibraryError):
        import_any(library, b"just some words")


@pytest.mark.parametrize("moment", ["9999-12-31T23:59:59-12:00", "0001-01-01T00:00:00+12:00"])
@pytest.mark.parametrize("field", ["createdAt", "publishedAt", "archivedAt"])
def test_json_import_drops_dates_that_cannot_be_shown(library, clock, field, moment):
    payload = {"app": "read-later", "version": 1, "items": [{"url": "https://a.example/", "archived": True, field: moment,
                                                             "highlights": [{"text": "h", "createdAt": moment}]}]}
    assert import_json(library, payload)["added"] == 1
    exported = export_json(library)["items"][0]
    assert exported[field] is None or exported[field].startswith("20")
    assert exported["highlights"][0]["createdAt"].startswith("20")


def import_in_the_background(library, page: str) -> dict:
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.update(import_bookmarks_html(library, page)), daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "the bookmarks import did not finish"
    return outcome


@pytest.mark.parametrize("add_date", ["9" * 400, "²", "٣٣", "1e300", "-5", " ", "0x10", "1" * 18])
def test_bookmark_add_dates_that_are_not_plain_numbers_are_ignored(library, clock, add_date):
    page = f'<DL><DT><A HREF="https://x.example/" ADD_DATE="{add_date}">t</A></DL>'
    assert import_in_the_background(library, page)["added"] == 1
    assert library.list_items(view="all")["items"][0]["createdAt"] == iso(clock())


def test_bookmark_add_dates_in_milliseconds_are_still_read(library):
    page = '<DL><DT><A HREF="https://x.example/" ADD_DATE="1762000000000">t</A></DL>'
    import_in_the_background(library, page)
    assert library.list_items(view="all")["items"][0]["createdAt"] == iso(1762000000.0)


def test_an_import_does_not_sweep_unused_tags_once_per_item(library, monkeypatch):
    sweeps = []
    real_drop = Library._drop_unused_tags
    monkeypatch.setattr(Library, "_drop_unused_tags", staticmethod(lambda connection: sweeps.append(1) or real_drop(connection)))
    items = [{"url": f"https://x.example/{number}", "tags": [f"t{number}-{tag}" for tag in range(5)]} for number in range(50)]
    assert import_json(library, {"app": "read-later", "version": 1, "items": items})["added"] == 50
    assert len(sweeps) <= 1
