import pytest

from readlater.extract import Article
from readlater.library import LibraryError

BIRD_TEXT = "Building a bird box is simple. You need one plank of untreated pine and a handful of screws."


def article(title="Bird box", text=BIRD_TEXT, site="Workshop", html=None):
    words = len(text.split())
    return Article(title=title, site_name=site, author="Sam", published_at=1_799_000_000.0, language="en",
                   excerpt=text[:40], content_html=html or f"<div><p>{text}</p></div>", text=text, word_count=words,
                   reading_minutes=1)


def save(library, url="https://workshop.example.test/bird-box", **kwargs):
    kwargs.setdefault("article", article())
    return library.add_item(url, **kwargs)


def test_a_saved_item_keeps_its_article(library):
    item = save(library)
    assert item["title"] == "Bird box"
    assert item["siteName"] == "Workshop"
    assert item["readingMinutes"] == 1
    assert item["wordCount"] == 18
    assert item["hasText"] is True
    assert item["archived"] is False and item["favorite"] is False
    assert item["progress"] == 0
    full = library.get_item(item["id"])
    assert full["contentHtml"].startswith("<div><p>Building")
    assert full["note"] == "" and full["highlights"] == []


def test_the_same_address_cannot_be_saved_twice(library):
    first = save(library)
    with pytest.raises(LibraryError) as caught:
        save(library)
    assert caught.value.status == 409
    assert caught.value.item_id == first["id"]
    assert library.find_by_url("https://workshop.example.test/bird-box") == first["id"]
    assert library.find_by_url("https://elsewhere.example.test/") is None


def test_a_link_without_an_article_is_kept_with_the_reason(library):
    item = library.add_item("https://files.example.test/manual.pdf", title="Manual", fetch_error="not a web page (application/pdf)")
    assert item["title"] == "Manual"
    assert item["hasText"] is False
    assert item["fetchError"] == "not a web page (application/pdf)"
    assert item["siteName"] == "files.example.test"


def test_a_link_without_any_title_is_named_after_its_address(library):
    assert library.add_item("https://files.example.test/docs/")["title"] == "files.example.test/docs"


def test_tags_are_cleaned_and_shared(library):
    first = save(library, tags=["  Woodwork ", "#garden", "woodwork", "", "birds"])
    assert first["tags"] == ["birds", "garden", "Woodwork"]
    second = save(library, url="https://workshop.example.test/shed", tags=["WOODWORK"])
    assert second["tags"] == ["Woodwork"]
    assert library.list_tags() == [{"name": "birds", "count": 1}, {"name": "garden", "count": 1}, {"name": "Woodwork", "count": 2}]


@pytest.mark.parametrize("tags", ["woodwork", [1], ["x" * 51], [f"t{number}" for number in range(21)]])
def test_bad_tags_are_refused(library, tags):
    with pytest.raises(LibraryError):
        save(library, tags=tags)


def test_unused_tags_disappear(library):
    item = save(library, tags=["birds", "garden"])
    library.update_item(item["id"], {"tags": ["garden"]})
    assert [tag["name"] for tag in library.list_tags()] == ["garden"]
    library.delete_item(item["id"])
    assert library.list_tags() == []


def test_collections_life_cycle(library):
    reading = library.create_collection("Reading")
    with pytest.raises(LibraryError) as caught:
        library.create_collection("reading")
    assert caught.value.status == 409
    item = save(library, collection_id=reading["id"])
    assert item["collectionId"] == reading["id"]
    assert library.list_collections() == [{"id": reading["id"], "name": "Reading", "count": 1}]
    assert library.rename_collection(reading["id"], "Weekend")["name"] == "Weekend"
    library.delete_collection(reading["id"])
    assert library.get_item(item["id"])["collectionId"] is None
    with pytest.raises(LibraryError):
        library.delete_collection(reading["id"])
    with pytest.raises(LibraryError):
        save(library, url="https://x.example.test/", collection_id=999)
    with pytest.raises(LibraryError):
        library.create_collection("  ")


def test_flags_progress_title_note_and_images_can_change(library, clock):
    item = save(library)
    clock.advance(60)
    changed = library.update_item(item["id"], {"favorite": True, "archived": True, "progress": 0.42, "title": "Better title",
                                               "note": "Try cedar next time.", "loadImages": True})
    assert changed["favorite"] is True and changed["archived"] is True
    assert changed["progress"] == 0.42
    assert changed["title"] == "Better title"
    assert changed["note"] == "Try cedar next time."
    assert changed["loadImages"] is True
    assert changed["archivedAt"] is not None
    assert library.update_item(item["id"], {"archived": False})["archivedAt"] is None


@pytest.mark.parametrize("changes", [
    {"favorite": "yes"}, {"archived": 1}, {"progress": 1.5}, {"progress": -0.1}, {"progress": "half"}, {"progress": True},
    {"title": ""}, {"title": 5}, {"note": "x" * 20001}, {"collectionId": "1"}, {"loadImages": None}, {"colour": "red"}, {},
])
def test_bad_changes_are_refused(library, changes):
    item = save(library)
    with pytest.raises(LibraryError):
        library.update_item(item["id"], changes)


def test_missing_items_are_404(library):
    for action in (lambda: library.get_item(99), lambda: library.update_item(99, {"favorite": True}), lambda: library.delete_item(99)):
        with pytest.raises(LibraryError) as caught:
            action()
        assert caught.value.status == 404


def make_shelf(library, clock):
    ids = {}
    for name in ("first", "second", "third", "fourth"):
        clock.advance(10)
        ids[name] = save(library, url=f"https://workshop.example.test/{name}", article=article(title=name.title()))["id"]
    library.update_item(ids["second"], {"favorite": True})
    library.update_item(ids["third"], {"archived": True})
    library.record_link_status(ids["fourth"], "dead", "the server answered 404")
    return ids


def titles(listing):
    return [item["title"] for item in listing["items"]]


def test_views_newest_first(library, clock):
    make_shelf(library, clock)
    assert titles(library.list_items("inbox")) == ["Fourth", "Second", "First"]
    assert titles(library.list_items("favorites")) == ["Second"]
    assert titles(library.list_items("archive")) == ["Third"]
    assert titles(library.list_items("all")) == ["Fourth", "Third", "Second", "First"]
    assert titles(library.list_items("broken")) == ["Fourth"]
    assert library.counts() == {"inbox": 3, "favorites": 1, "archive": 1, "all": 4, "broken": 1}


def test_paging_with_a_cursor(library, clock):
    make_shelf(library, clock)
    page = library.list_items("all", limit=3)
    assert titles(page) == ["Fourth", "Third", "Second"]
    assert titles(library.list_items("all", limit=3, before=page["next"])) == ["First"]
    assert library.list_items("all", limit=4)["next"] is None


@pytest.mark.parametrize("kwargs", [{"view": "bogus"}, {"limit": 0}, {"limit": 501}, {"before": "nonsense"}])
def test_bad_listing_parameters(library, kwargs):
    with pytest.raises(LibraryError):
        library.list_items(**kwargs)


def test_filters_by_collection_and_tag(library):
    reading = library.create_collection("Reading")
    save(library, url="https://a.example.test/", article=article(title="In collection"), collection_id=reading["id"], tags=["pine"])
    save(library, url="https://b.example.test/", article=article(title="Loose"), tags=["oak"])
    assert titles(library.list_items("all", collection_id=reading["id"])) == ["In collection"]
    assert titles(library.list_items("all", tag="PINE")) == ["In collection"]
    assert titles(library.list_items("all", tag="nothing")) == []


def test_full_text_search_over_title_text_site_and_note(library):
    lighthouse = save(library, url="https://a.example.test/", article=article(title="Lighthouse repainted", text="The keepers chose red paint.", site="Harbour Gazette"))
    save(library, url="https://b.example.test/", article=article(title="Ferry news", text="Timetables change in winter."))
    library.update_item(lighthouse["id"], {"note": "ask about the foghorn"})
    assert titles(library.list_items("all", query="light")) == ["Lighthouse repainted"]
    assert titles(library.list_items("all", query="paint")) == ["Lighthouse repainted"]
    assert titles(library.list_items("all", query="harbour")) == ["Lighthouse repainted"]
    assert titles(library.list_items("all", query="foghorn")) == ["Lighthouse repainted"]
    assert titles(library.list_items("all", query="winter timetables")) == ["Ferry news"]
    assert titles(library.list_items("all", query='"* OR NEAR(')) == []
    assert titles(library.list_items("all", query="!!!")) == []


def test_search_follows_new_text_after_a_refetch(library):
    item = save(library, article=article(text="Old words about pine."))
    library.store_article(item["id"], article(text="Fresh words about cedar."))
    assert titles(library.list_items("all", query="cedar")) == ["Bird box"]
    assert titles(library.list_items("all", query="pine")) == []


def test_store_article_clears_a_fetch_error_and_record_fetch_error_keeps_old_text(library):
    item = library.add_item("https://a.example.test/", fetch_error="timed out")
    stored = library.store_article(item["id"], article())
    assert stored["fetchError"] == "" and stored["hasText"] is True
    failed = library.record_fetch_error(item["id"], "the server answered 500")
    assert failed["fetchError"] == "the server answered 500"
    assert failed["hasText"] is True


def test_highlights_must_come_from_the_text(library):
    item = save(library)
    second = library.add_highlight(item["id"], "a handful\n of screws")
    first = library.add_highlight(item["id"], "  Building a bird box ")
    assert first["text"] == "Building a bird box"
    assert [highlight["text"] for highlight in library.get_item(item["id"])["highlights"]] == ["Building a bird box", "a handful of screws"]
    assert library.list_items("all")["items"][0]["highlightCount"] == 2
    with pytest.raises(LibraryError):
        library.add_highlight(item["id"], "words that are not in the article")
    with pytest.raises(LibraryError) as caught:
        library.add_highlight(item["id"], "a handful of screws")
    assert caught.value.status == 409
    library.delete_highlight(second["id"])
    assert [highlight["id"] for highlight in library.get_item(item["id"])["highlights"]] == [first["id"]]
    with pytest.raises(LibraryError) as missing:
        library.delete_highlight(second["id"])
    assert missing.value.status == 404


@pytest.mark.parametrize("text", ["", "   ", None, 7, "x" * 5001])
def test_bad_highlights_are_refused(library, text):
    item = save(library)
    with pytest.raises(LibraryError):
        library.add_highlight(item["id"], text)


def test_an_item_without_text_cannot_be_highlighted(library):
    item = library.add_item("https://files.example.test/manual.pdf")
    with pytest.raises(LibraryError):
        library.add_highlight(item["id"], "anything")


def test_deleting_an_item_removes_its_highlights_and_search_entry(library):
    item = save(library)
    library.add_highlight(item["id"], "bird box")
    library.delete_item(item["id"])
    assert library.list_items("all", query="bird")["items"] == []
    with library.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM highlights").fetchone()[0] == 0


def test_link_status_is_recorded(library, clock):
    item = save(library)
    checked = library.record_link_status(item["id"], "ok", "")
    assert checked["linkStatus"] == "ok"
    assert checked["linkCheckedAt"] is not None
    with pytest.raises(LibraryError):
        library.record_link_status(item["id"], "maybe", "")


def test_headlines_for_other_programs_list_unarchived_items(library, clock):
    make_shelf(library, clock)
    headlines = library.headlines(limit=2)
    assert [item["title"] for item in headlines] == ["Fourth", "Second"]
    assert set(headlines[0]) == {"id", "title", "url", "site", "excerpt", "readingMinutes", "progress", "favorite", "tags", "savedAt"}


def test_items_to_check_can_be_all_or_a_view(library, clock):
    ids = make_shelf(library, clock)
    assert [entry["id"] for entry in library.items_to_check("all")] == [ids["fourth"], ids["third"], ids["second"], ids["first"]]
    assert [entry["id"] for entry in library.items_to_check("archive")] == [ids["third"]]
    assert set(library.items_to_check("archive")[0]) == {"id", "url"}
