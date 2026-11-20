import json
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import OWNER, SECRET, fixture_bytes

from readlater.images import ImageSigner

HTML = {"Content-Type": "text/html; charset=utf-8"}
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000")
HOSTILE_PAGE = (
    "<html><head><title>Hostile</title></head><body><article>"
    '<p onclick="steal()" style="position:fixed">A paragraph long enough to be kept as the article body by the reader, '
    "with a few more words so that nothing about it looks like a menu or a footer.</p>"
    '<script>alert(1)</script><iframe src="https://tracker.example.test/frame"></iframe>'
    '<img src="https://tracker.example.test/pixel.gif" onerror="steal()">'
    '<p>Second paragraph with <a href="javascript:steal()">a bad link</a> and <a href="/good">a good link</a> in it, '
    "long enough to count as text.</p>"
    '<form action="https://tracker.example.test/"><input name="q"></form>'
    "</article></body></html>"
).encode()


def serve(server, path="/posts/bird-box", body=None):
    server.add(path, body=fixture_bytes("article.html") if body is None else body, headers=HTML)
    return server.url(path)


def saved(owner, server, path="/posts/bird-box", body=None, **extra):
    response = owner.post("/api/items", json={"url": serve(server, path, body), **extra})
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def test_health_needs_no_sign_in(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def test_health_reports_a_broken_database(client, database):
    database.path.unlink()
    database.path.mkdir()
    assert client.get("/api/health").status_code == 503


@pytest.mark.parametrize("path", ["/", "/save?url=https://a.example.test/", "/api/library", "/api/items", "/api/items/1",
                                  "/api/link-check", "/api/export", "/api/v1/unread", "/api/v1/search?q=x", "/img?u=x&s=y",
                                  "/api/session"])
def test_everything_but_health_needs_the_user_header(client, path):
    assert client.get(path).status_code == 401


def test_user_header_from_outside_the_proxy_is_refused(client):
    response = client.get("/api/items", headers={"X-Forwarded-User": OWNER}, environ_base={"REMOTE_ADDR": "192.0.2.77"})
    assert response.status_code == 401


def test_page_and_static_files_are_served_with_a_strict_policy(owner):
    page = owner.get("/")
    assert page.status_code == 200
    assert b"<title>Starfire Read Later</title>" in page.data
    assert b'aria-label="Starfire Read Later home"' in page.data
    assert b'Starfire <span class="brand-accent">Read Later</span>' in page.data
    assert b"Save to Starfire Read Later" in page.data
    policy = page.headers["Content-Security-Policy"]
    assert "script-src 'self'" in policy and "img-src 'self' data:" in policy and "frame-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert page.headers["X-Frame-Options"] == "DENY"
    assert page.headers["Referrer-Policy"] == "no-referrer"
    assert page.headers["X-Content-Type-Options"] == "nosniff"
    for name in ("app.js", "app.css", "icon.svg", "manifest.webmanifest", "fonts/ibm-plex-sans-latin-400-normal.woff2"):
        assert owner.get(f"/static/{name}").status_code == 200, name
    assert owner.get("/static/../web.py").status_code == 404


def test_the_manifest_is_named_starfire_read_later(owner):
    manifest = json.loads(owner.get("/static/manifest.webmanifest").data)
    assert manifest["name"] == "Starfire Read Later"
    assert manifest["short_name"] == "Starfire Read Later"


def test_the_manifest_offers_a_share_target_that_opens_the_save_page(owner):
    manifest = json.loads(owner.get("/static/manifest.webmanifest").data)
    assert manifest["share_target"]["action"] == "/save"
    assert manifest["share_target"]["method"] == "GET"
    assert manifest["share_target"]["params"] == {"title": "title", "text": "text", "url": "url"}


def test_the_save_page_is_the_app_and_saves_nothing_by_itself(owner, library, connector):
    response = owner.get("/save?url=https://news.example.test/story&title=Story")
    assert response.status_code == 200
    assert b"Read Later" in response.data
    assert library.counts()["all"] == 0
    assert connector.addresses == []


def test_writes_need_the_csrf_token_and_the_same_origin(client, owner, library):
    assert client.post("/api/collections", json={"name": "x"}, headers=owner.headers).status_code == 403
    assert client.post("/api/collections", json={"name": "x"}, headers={**owner.headers, "X-CSRF-Token": "0" * 64}).status_code == 403
    other_site = owner.client.post("/api/collections", json={"name": "x"}, headers={**owner.write_headers, "Origin": "https://evil.example"})
    assert other_site.status_code == 403
    assert library.list_collections() == []
    same_site = owner.client.post("/api/collections", json={"name": "x"}, headers={**owner.write_headers, "Origin": "http://localhost"})
    assert same_site.status_code == 201


def test_saving_a_link_through_the_api(owner, server):
    item = saved(owner, server, tags=["birds"])
    assert item["title"] == "How to build a bird box"
    assert item["siteName"] == "The Workshop Blog"
    assert item["readingMinutes"] == 1
    assert item["tags"] == ["birds"]
    library = owner.get("/api/library").get_json()
    assert library["counts"] == {"inbox": 1, "favorites": 0, "archive": 0, "all": 1, "broken": 0}
    assert library["tags"] == [{"name": "birds", "count": 1}]


def test_saving_twice_answers_409_with_the_saved_item(owner, server):
    first = saved(owner, server)
    again = owner.post("/api/items", json={"url": server.url("/posts/bird-box")})
    assert again.status_code == 409
    assert again.get_json()["id"] == first["id"]


@pytest.mark.parametrize("url", ["http://private.example.test/", "http://169.254.169.254/latest/meta-data/", "http://[::1]:8099/",
                                 "file:///etc/passwd", "", None, "http://10.0.0.1/"])
def test_unsafe_links_are_refused_and_never_fetched(owner, connector, library, url):
    response = owner.post("/api/items", json={"url": url})
    assert response.status_code == 400
    assert "error" in response.get_json()
    assert connector.addresses == []
    assert library.counts()["all"] == 0


@pytest.mark.parametrize("body", [None, [], "x", {"url": "https://a.example.test/", "tags": "x"},
                                  {"url": "https://a.example.test/", "collectionId": "1"}])
def test_bad_save_requests_are_400(owner, body):
    assert owner.post("/api/items", json=body).status_code == 400


def test_reading_an_item_returns_cleaned_html_with_images_held_back(owner, server):
    item = saved(owner, server, body=HOSTILE_PAGE)
    detail = owner.get(f"/api/items/{item['id']}").get_json()
    html = detail["html"].lower()
    for forbidden in ("<script", "<iframe", "onclick", "onerror", "javascript:", "<form", "tracker.example.test", "<img", "style="):
        assert forbidden not in html, forbidden
    assert f'href="{server.url("/good")}"' in detail["html"]
    assert 'rel="noopener noreferrer nofollow"' in detail["html"]
    assert detail["hiddenImages"] == 1
    assert "contentHtml" not in detail


def test_images_for_one_item_go_through_the_signed_proxy(owner, server):
    item = saved(owner, server)
    owner.patch(f"/api/items/{item['id']}", json={"loadImages": True})
    detail = owner.get(f"/api/items/{item['id']}").get_json()
    assert detail["hiddenImages"] == 0
    assert "/img?u=" in detail["html"]
    assert "images%2Fbird-box.jpg" in detail["html"]


def test_changing_an_item(owner, server):
    item = saved(owner, server)
    collection = owner.post("/api/collections", json={"name": "Weekend"}).get_json()
    changed = owner.patch(f"/api/items/{item['id']}", json={
        "favorite": True, "progress": 0.6, "note": "Cedar next time", "tags": ["pine", "birds"], "collectionId": collection["id"],
    }).get_json()
    assert changed["favorite"] is True and changed["progress"] == 0.6 and changed["note"] == "Cedar next time"
    assert changed["tags"] == ["birds", "pine"] and changed["collectionId"] == collection["id"]
    assert "contentHtml" not in changed
    archived = owner.patch(f"/api/items/{item['id']}", json={"archived": True}).get_json()
    assert archived["archived"] is True
    assert owner.get("/api/items?view=inbox").get_json()["items"] == []
    assert [entry["id"] for entry in owner.get("/api/items?view=archive").get_json()["items"]] == [item["id"]]
    assert owner.patch(f"/api/items/{item['id']}", json={"progress": 2}).status_code == 400
    assert owner.patch("/api/items/999", json={"favorite": True}).status_code == 404


def test_listing_filters_and_paging(owner, server, library):
    for name in ("one", "two", "three"):
        saved(owner, server, path=f"/{name}", tags=[name])
    page = owner.get("/api/items?view=all&limit=2").get_json()
    assert len(page["items"]) == 2 and page["next"]
    rest = owner.get(f"/api/items?view=all&limit=2&before={page['next']}").get_json()
    assert len(rest["items"]) == 1 and rest["next"] is None
    assert [item["tags"] for item in owner.get("/api/items?view=all&tag=two").get_json()["items"]] == [["two"]]
    assert len(owner.get("/api/items?view=all&q=woodworking").get_json()["items"]) == 3
    assert owner.get("/api/items?view=all&q=zebra").get_json()["items"] == []


@pytest.mark.parametrize("query", ["view=bogus", "limit=0", "limit=abc", "limit=501", "collection=abc", "before=nonsense"])
def test_bad_listing_parameters_are_400(owner, query):
    assert owner.get(f"/api/items?{query}").status_code == 400


def test_highlights_through_the_api(owner, server):
    item = saved(owner, server)
    created = owner.post(f"/api/items/{item['id']}/highlights", json={"text": "one plank of untreated pine"})
    assert created.status_code == 201
    highlight = created.get_json()
    assert highlight["text"] == "one plank of untreated pine"
    assert owner.post(f"/api/items/{item['id']}/highlights", json={"text": "not in the article at all"}).status_code == 400
    detail = owner.get(f"/api/items/{item['id']}").get_json()
    assert [entry["text"] for entry in detail["highlights"]] == ["one plank of untreated pine"]
    assert owner.delete(f"/api/highlights/{highlight['id']}").status_code == 204
    assert owner.delete(f"/api/highlights/{highlight['id']}").status_code == 404
    assert owner.post("/api/items/999/highlights", json={"text": "x"}).status_code == 404


def test_collections_life_cycle(owner):
    created = owner.post("/api/collections", json={"name": "Weekend"})
    assert created.status_code == 201
    collection = created.get_json()
    assert owner.patch(f"/api/collections/{collection['id']}", json={"name": "Sunday"}).get_json()["name"] == "Sunday"
    assert owner.post("/api/collections", json={"name": "sunday"}).status_code == 409
    assert owner.get("/api/library").get_json()["collections"] == [{"id": collection["id"], "name": "Sunday", "count": 0}]
    assert owner.delete(f"/api/collections/{collection['id']}").status_code == 204
    assert owner.delete(f"/api/collections/{collection['id']}").status_code == 404


def test_refetch_and_delete(owner, server):
    server.add("/later", status=503)
    item = owner.post("/api/items", json={"url": server.url("/later"), "title": "Later"}).get_json()
    assert item["fetchError"] == "the server answered 503"
    serve(server, "/later")
    fetched = owner.post(f"/api/items/{item['id']}/refetch").get_json()
    assert fetched["hasText"] is True and fetched["fetchError"] == ""
    assert owner.delete(f"/api/items/{item['id']}").status_code == 204
    assert owner.get(f"/api/items/{item['id']}").status_code == 404


def test_checking_one_link(owner, server):
    item = saved(owner, server)
    server.add("/posts/bird-box", status=404)
    checked = owner.post(f"/api/items/{item['id']}/check").get_json()
    assert checked["linkStatus"] == "dead"
    assert [entry["id"] for entry in owner.get("/api/items?view=broken").get_json()["items"]] == [item["id"]]


def test_checking_every_link_in_the_background(owner, server, link_checker):
    saved(owner, server)
    started = owner.post("/api/link-check", json={"view": "all"})
    assert started.status_code == 202
    assert started.get_json()["total"] == 1
    assert link_checker.wait(10)
    status = owner.get("/api/link-check").get_json()
    assert status["running"] is False and status["checked"] == 1 and status["dead"] == 0
    assert owner.post("/api/link-check", json={"view": "bogus"}).status_code == 400


def test_export_and_import(owner, server, library):
    saved(owner, server, tags=["birds"])
    exported = owner.get("/api/export?format=json")
    assert exported.status_code == 200
    assert "attachment" in exported.headers["Content-Disposition"]
    assert exported.headers["Content-Disposition"].endswith(".json")
    payload = json.loads(exported.data)
    assert payload["app"] == "read-later" and len(payload["items"]) == 1
    bookmarks = owner.get("/api/export?format=html")
    assert bookmarks.status_code == 200
    assert bookmarks.data.startswith(b"<!DOCTYPE NETSCAPE-Bookmark-file-1>")
    assert bookmarks.headers["Content-Disposition"].endswith(".html")
    assert owner.get("/api/export?format=csv").status_code == 400
    imported = owner.post_raw("/api/import", fixture_bytes("pocket.html"), "text/html")
    assert imported.status_code == 200
    assert imported.get_json() == {"added": 3, "skipped": 0, "collections": 0}
    again = owner.post_raw("/api/import", exported.data, "application/json")
    assert again.get_json() == {"added": 0, "skipped": 1, "collections": 0}
    assert owner.post_raw("/api/import", b"nothing useful", "text/plain").status_code == 400


def test_v1_unread_lists_saved_items_for_other_programs(owner, server):
    first = saved(owner, server, path="/first")
    saved(owner, server, path="/second")
    owner.patch(f"/api/items/{first['id']}", json={"archived": True})
    response = owner.get("/api/v1/unread?limit=5")
    assert response.status_code == 200
    data = response.get_json()
    assert data["unread"] == 1
    assert len(data["items"]) == 1
    assert set(data["items"][0]) == {"id", "title", "url", "site", "excerpt", "readingMinutes", "progress", "favorite", "tags", "savedAt"}
    assert data["items"][0]["url"] == server.url("/second")
    assert response.headers["Cache-Control"] == "no-store"


def test_v1_search(owner, server):
    saved(owner, server)
    data = owner.get("/api/v1/search?q=bird").get_json()
    assert [item["title"] for item in data["items"]] == ["How to build a bird box"]
    assert owner.get("/api/v1/search?q=").status_code == 400
    assert owner.get("/api/v1/search?q=" + "x" * 201).status_code == 400
    assert owner.get("/api/v1/unread?limit=101").status_code == 400


def test_v1_is_read_only(owner):
    assert owner.post("/api/v1/unread").status_code == 405


def proxied(url):
    return ImageSigner(SECRET).proxy_path(url)


def test_image_proxy_fetches_signed_images(owner, server):
    server.add("/pic.png", body=PNG, headers={"Content-Type": "image/png", "Set-Cookie": "track=1"})
    response = owner.get(proxied(server.url("/pic.png")))
    assert response.status_code == 200
    assert response.data == PNG
    assert response.headers["Content-Type"] == "image/png"
    assert "Set-Cookie" not in response.headers
    assert "Referer" not in server.requests[0]["headers"]


def test_image_proxy_refuses_unsigned_or_tampered_links(owner, server):
    server.add("/pic.png", body=PNG, headers={"Content-Type": "image/png"})
    query = parse_qs(urlsplit(proxied(server.url("/pic.png"))).query)
    assert owner.get(f"/img?u={server.url('/pic.png')}").status_code == 403
    assert owner.get(f"/img?u={server.url('/other.png')}&s={query['s'][0]}").status_code == 403
    assert server.requests == []


@pytest.mark.parametrize("content_type", ["image/svg+xml", "text/html", ""])
def test_image_proxy_only_passes_raster_images(owner, server, content_type):
    server.add("/thing", body=b"<svg onload='alert(1)'/>", headers={"Content-Type": content_type})
    assert owner.get(proxied(server.url("/thing"))).status_code == 415


def test_image_proxy_will_not_reach_private_addresses(owner, connector):
    assert owner.get(proxied("http://169.254.169.254/latest/meta-data/")).status_code == 403
    assert connector.addresses == []


def dated_page(moment: str) -> bytes:
    return (f'<html><head><title>Dated</title><meta property="article:published_time" content="{moment}"></head><body><article><p>'
            + "A paragraph long enough to be kept as the article body by the reader. " * 10 + "</p></article></body></html>").encode()


@pytest.mark.parametrize("moment", ["9999-12-31T23:59:59-12:00", "0001-01-01T00:00:00+12:00"])
def test_a_page_with_an_unshowable_published_time_saves_and_lists(owner, server, moment):
    item = saved(owner, server, body=dated_page(moment))
    assert item["publishedAt"] is None
    serve(server, body=dated_page(moment))
    assert owner.post(f"/api/items/{item['id']}/refetch").status_code == 200
    for path in ("/api/items", f"/api/items/{item['id']}", "/api/v1/unread", "/api/export"):
        assert owner.get(path).status_code == 200, path


def test_an_import_with_a_year_10000_date_keeps_the_library_readable(owner):
    payload = {"app": "read-later", "version": 1, "items": [{"url": "https://a.example/", "createdAt": "9999-12-31T23:59:59-12:00"}]}
    response = owner.post_raw("/api/import", json.dumps(payload).encode(), "application/json")
    assert response.status_code == 200
    assert response.get_json()["added"] == 1
    assert owner.get("/api/items").status_code == 200


def test_stored_dates_that_cannot_be_shown_do_not_break_reading(owner, server, database):
    item = saved(owner, server)
    with database.connect() as connection:
        connection.execute("UPDATE items SET published_at = ?, archived_at = ?, fetched_at = ? WHERE id = ?",
                           (253402344000.0, float("inf"), -1e300, item["id"]))
    for path in ("/api/items", f"/api/items/{item['id']}", "/api/v1/unread", "/api/export"):
        assert owner.get(path).status_code == 200, path
    assert owner.get(f"/api/items/{item['id']}").get_json()["publishedAt"] is None


PORT = 8099


@pytest.fixture
def local_client(library, saver, link_checker):
    from readlater.auth import LocalAuth
    from readlater.web import create_app
    local_app = create_app(library, saver, link_checker, LocalAuth("me", port=PORT), SECRET)
    local_app.testing = True
    return local_app.test_client()


def test_local_mode_refuses_a_rebound_host_name(local_client):
    rebound = f"evil.example:{PORT}"
    assert local_client.get("/api/session", headers={"Host": rebound}).status_code == 421
    assert local_client.get("/", headers={"Host": rebound}).status_code == 421
    posted = local_client.post("/api/session", headers={"Host": rebound, "Origin": f"http://{rebound}"})
    assert posted.status_code == 421


@pytest.mark.parametrize("host", [f"localhost:{PORT}", f"127.0.0.1:{PORT}", f"[::1]:{PORT}"])
def test_local_mode_answers_on_the_loopback_names(local_client, host):
    response = local_client.get("/api/session", headers={"Host": host})
    assert response.status_code == 200
    assert response.get_json()["user"] == "me"


HUGE_ID = "999999999999999999999999999999"
DEEP_JSON = ("[" * 100_000).encode()


@pytest.mark.parametrize("method, path", [("get", f"/api/items/{HUGE_ID}"), ("delete", f"/api/highlights/{HUGE_ID}"),
                                          ("patch", f"/api/collections/{HUGE_ID}"), ("post", f"/api/items/{HUGE_ID}/refetch")])
def test_ids_too_large_for_the_database_are_not_found(owner, method, path):
    response = getattr(owner, method)(path, json={}) if method != "get" else owner.get(path)
    assert response.status_code == 404


@pytest.mark.parametrize("query", ["before=1:999999999999999999999999", "before=1e999:1", "limit=%C2%B2"])
def test_numbers_in_the_query_that_cannot_be_used_are_refused(owner, query):
    assert owner.get(f"/api/items?{query}").status_code == 400


def test_saving_into_a_collection_id_too_large_for_the_database_is_refused(owner, server, library):
    response = owner.post("/api/items", json={"url": serve(server), "collectionId": 10 ** 30})
    assert response.status_code == 400
    assert library.list_items(view="all")["items"] == []


def test_changing_an_item_to_a_collection_id_too_large_is_refused(owner, server):
    item = saved(owner, server)
    assert owner.patch(f"/api/items/{item['id']}", json={"collectionId": 10 ** 30}).status_code == 400


@pytest.mark.parametrize("path", ["/api/items", "/api/import"])
def test_json_nested_too_deeply_is_refused(owner, path):
    deep = b'{"a": ' + DEEP_JSON if path == "/api/import" else DEEP_JSON
    response = owner.client.post(path, data=deep, headers={**owner.write_headers, "Content-Type": "application/json"})
    assert response.status_code == 400


@pytest.mark.parametrize("charset", ["hex", "rot13", "idna", "undefined"])
def test_a_page_naming_a_codec_that_is_not_text_still_saves(owner, server, charset):
    server.add("/odd", body=fixture_bytes("article.html"), headers={"Content-Type": f"text/html; charset={charset}"})
    response = owner.post("/api/items", json={"url": server.url("/odd")})
    assert response.status_code == 201
    assert response.get_json()["title"] == "How to build a bird box"


def test_a_bookmarks_file_naming_a_codec_that_is_not_text_imports(owner):
    page = b'<meta charset=hex><DL><DT><A HREF="https://x.example/">t</A></DL>'
    response = owner.post_raw("/api/import", page, "text/html")
    assert response.status_code == 200
    assert response.get_json()["added"] == 1
