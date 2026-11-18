import pytest
from conftest import fixture_bytes

from readlater.library import LibraryError

HTML = {"Content-Type": "text/html; charset=utf-8"}


def serve_article(server, path="/posts/bird-box"):
    server.add(path, body=fixture_bytes("article.html"), headers=HTML)
    return server.url(path, host="blog.example.test")


def test_saving_fetches_and_extracts_the_article(saver, server):
    item = saver.save(serve_article(server), tags=["birds"])
    assert item["title"] == "How to build a bird box"
    assert item["siteName"] == "The Workshop Blog"
    assert item["readingMinutes"] == 1
    assert item["tags"] == ["birds"]
    assert item["hasText"] is True
    assert item["fetchError"] == ""
    assert server.requests[0]["headers"]["Host"] == f"blog.example.test:{server.port}"
    assert "text/html" in server.requests[0]["headers"]["Accept"]


def test_the_extracted_title_beats_the_title_sent_with_the_link(saver, server):
    assert saver.save(serve_article(server), title="Bird box | blog")["title"] == "How to build a bird box"


@pytest.mark.parametrize("url", [
    "http://private.example.test/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://10.0.0.1/",
    "file:///etc/passwd", "gopher://example.test/", "javascript:alert(1)", "", None, "http://user:pw@blog.example.test/",
])
def test_unsafe_addresses_are_refused_and_not_saved(saver, library, connector, url):
    with pytest.raises(LibraryError) as caught:
        saver.save(url)
    assert caught.value.status == 400
    assert connector.addresses == []
    assert library.counts()["all"] == 0


def test_a_redirect_into_the_home_network_is_refused(saver, server, library, connector):
    server.add("/moved", status=302, headers={"Location": "http://10.0.0.1/admin"})
    with pytest.raises(LibraryError) as caught:
        saver.save(server.url("/moved"))
    assert caught.value.status == 400
    assert "not a public internet address" in str(caught.value)
    assert connector.addresses == ["127.0.0.1"]
    assert library.counts()["all"] == 0


def test_saving_the_same_link_twice_is_409_without_a_second_fetch(saver, server):
    url = serve_article(server)
    first = saver.save(url)
    with pytest.raises(LibraryError) as caught:
        saver.save(url)
    assert caught.value.status == 409
    assert caught.value.item_id == first["id"]
    assert len(server.requests) == 1


def test_bad_tags_or_collection_are_refused_before_fetching(saver, server):
    url = serve_article(server)
    with pytest.raises(LibraryError):
        saver.save(url, tags="birds")
    with pytest.raises(LibraryError):
        saver.save(url, collection_id=42)
    assert server.requests == []


def test_a_page_that_cannot_be_fetched_is_kept_as_a_link_with_the_reason(saver, server):
    server.add("/gone", status=500)
    item = saver.save(server.url("/gone"), title="Something I meant to read")
    assert item["title"] == "Something I meant to read"
    assert item["hasText"] is False
    assert item["fetchError"] == "the server answered 500"


def test_files_that_are_not_web_pages_are_kept_as_links(saver, server):
    server.add("/manual.pdf", body=b"%PDF-1.7", headers={"Content-Type": "application/pdf"})
    item = saver.save(server.url("/manual.pdf"))
    assert item["hasText"] is False
    assert item["fetchError"] == "not a web page (application/pdf), kept as a link"
    assert item["title"] == "127.0.0.1/manual.pdf"


def test_an_empty_page_is_kept_as_a_link(saver, server):
    server.add("/empty", body=b"", headers=HTML)
    item = saver.save(server.url("/empty"))
    assert item["hasText"] is False
    assert "empty" in item["fetchError"]


def test_refetch_replaces_the_text_and_keeps_it_on_failure(saver, server, library):
    server.add("/later", status=503)
    item = saver.save(server.url("/later"))
    assert item["fetchError"] == "the server answered 503"
    server.add("/later", body=fixture_bytes("article.html"), headers=HTML)
    fetched = saver.refetch(item["id"])
    assert fetched["hasText"] is True and fetched["fetchError"] == ""
    assert fetched["title"] == "How to build a bird box"
    server.add("/later", status=500)
    failed = saver.refetch(item["id"])
    assert failed["fetchError"] == "the server answered 500"
    assert failed["hasText"] is True


def test_refetch_of_an_address_that_now_points_inside_is_refused_without_connecting(saver, library, connector):
    item = library.add_item("http://private.example.test/post")
    refetched = saver.refetch(item["id"])
    assert "not a public internet address" in refetched["fetchError"]
    assert connector.addresses == []


@pytest.mark.parametrize("status, expected", [(200, "ok"), (404, "dead"), (410, "dead"), (500, "error"), (403, "error")])
def test_link_check_reads_the_status(saver, server, library, status, expected):
    server.add("/page", status=status, body=b"<p>hi</p>", headers=HTML)
    item = library.add_item(server.url("/page"))
    checked = saver.check_link(item["id"])
    assert checked["linkStatus"] == expected
    assert checked["linkCheckedAt"] is not None
    if expected != "ok":
        assert str(status) in checked["linkDetail"]


def test_link_check_counts_a_big_page_as_alive_without_reading_it_all(saver, server, library):
    server.add("/big", body=b"x" * 150_000, headers=HTML)
    item = library.add_item(server.url("/big"))
    assert saver.check_link(item["id"])["linkStatus"] == "ok"


def test_link_check_marks_names_that_no_longer_exist_as_dead(saver, library):
    item = library.add_item("http://nowhere.example.test/post")
    checked = saver.check_link(item["id"])
    assert checked["linkStatus"] == "dead"
    assert "no longer" in checked["linkDetail"]


def test_link_check_never_reaches_private_addresses(saver, library, connector):
    item = library.add_item("http://metadata.example.test/latest/meta-data/")
    checked = saver.check_link(item["id"])
    assert checked["linkStatus"] == "error"
    assert "not a public internet address" in checked["linkDetail"]
    assert connector.addresses == []


def test_link_check_follows_redirects_to_a_live_page(saver, server, library):
    server.add("/old", status=301, headers={"Location": "/new"})
    server.add("/new", body=b"<p>here</p>", headers=HTML)
    item = library.add_item(server.url("/old"))
    assert saver.check_link(item["id"])["linkStatus"] == "ok"


def test_missing_items_are_404(saver):
    for action in (saver.refetch, saver.check_link):
        with pytest.raises(LibraryError) as caught:
            action(999)
        assert caught.value.status == 404


def test_a_lookup_that_fails_for_a_while_is_not_called_dead(library, connector):
    import socket

    from readlater.netguard import SafeFetcher
    from readlater.saver import Saver

    def flaky_resolver(host, port):
        raise socket.gaierror(socket.EAI_AGAIN, "temporary failure in name resolution")

    flaky = Saver(library, SafeFetcher(resolver=flaky_resolver, connector=connector, total_timeout=2))
    item = library.add_item("https://news.example.test/story")
    checked = flaky.check_link(item["id"])
    assert checked["linkStatus"] == "error"
    assert "could not look up" in checked["linkDetail"]
