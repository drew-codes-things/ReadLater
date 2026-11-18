import threading

import pytest

HTML = {"Content-Type": "text/html"}


def shelf(library, server):
    server.add("/alive", body=b"<p>alive</p>", headers=HTML)
    server.add("/gone", status=404)
    alive = library.add_item(server.url("/alive"))
    gone = library.add_item(server.url("/gone"))
    archived = library.add_item(server.url("/alive?archived=1"))
    library.update_item(archived["id"], {"archived": True})
    return alive, gone, archived


def test_a_full_check_records_every_link(link_checker, library, server):
    alive, gone, archived = shelf(library, server)
    status = link_checker.start("all")
    assert status["running"] is True and status["total"] == 3
    assert link_checker.wait(10)
    finished = link_checker.status()
    assert finished["running"] is False
    assert finished["checked"] == 3 and finished["dead"] == 1 and finished["total"] == 3
    assert finished["finishedAt"] is not None
    assert library.get_item(gone["id"])["linkStatus"] == "dead"
    assert library.get_item(alive["id"])["linkStatus"] == "ok"
    assert library.counts()["broken"] == 1


def test_a_check_can_be_limited_to_one_view(link_checker, library, server):
    alive, gone, archived = shelf(library, server)
    link_checker.start("archive")
    assert link_checker.wait(10)
    assert link_checker.status()["checked"] == 1
    assert library.get_item(archived["id"])["linkStatus"] == "ok"
    assert library.get_item(gone["id"])["linkStatus"] == ""


def test_only_one_check_runs_at_a_time(link_checker, library, server):
    release = threading.Event()

    def slow(handler):
        release.wait(5)
        handler.send_response(200)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    server.add("/slow", handler=slow)
    library.add_item(server.url("/slow"))
    first = link_checker.start("all")
    second = link_checker.start("all")
    assert first["running"] is True
    assert second["running"] is True and second["startedAt"] == first["startedAt"]
    release.set()
    assert link_checker.wait(10)
    assert link_checker.status()["checked"] == 1


def test_an_item_deleted_during_a_check_is_skipped(link_checker, library, saver, monkeypatch):
    item = library.add_item("https://gone.example.test/")
    library.delete_item(item["id"])
    monkeypatch.setattr(library, "items_to_check", lambda view: [{"id": item["id"], "url": item["url"]}])
    link_checker.start("all")
    assert link_checker.wait(10)
    assert link_checker.status()["checked"] == 0
    assert link_checker.status()["error"] == ""


def test_an_unexpected_failure_stops_the_check_and_is_reported(link_checker, saver, library, monkeypatch):
    library.add_item("https://a.example.test/")

    def broken(item_id):
        raise RuntimeError("disk full")

    monkeypatch.setattr(saver, "check_link", broken)
    link_checker.start("all")
    assert link_checker.wait(10)
    assert "disk full" in link_checker.status()["error"]
    assert link_checker.status()["running"] is False


def test_a_bad_view_is_refused(link_checker):
    from readlater.library import LibraryError
    with pytest.raises(LibraryError):
        link_checker.start("bogus")


def test_status_before_any_check(link_checker):
    assert link_checker.status() == {"running": False, "total": 0, "checked": 0, "dead": 0, "startedAt": None,
                                     "finishedAt": None, "error": ""}
