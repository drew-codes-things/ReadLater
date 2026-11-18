import gzip
import ipaddress
import shutil
import socket
import ssl
import subprocess
import threading
import time

import pytest
from conftest import fake_resolver

from readlater.netguard import (
    BlockedAddressError,
    FetchError,
    FetchTimeoutError,
    HttpStatusError,
    ResponseTooLargeError,
    SafeFetcher,
    UnsafeUrlError,
    check_url,
    is_public_address,
)

BLOCKED_ADDRESSES = [
    "127.0.0.1", "127.8.9.10", "10.0.0.1", "172.16.5.4", "172.20.1.1", "169.254.169.254", "169.254.0.1",
    "100.64.0.1", "0.0.0.0", "192.0.2.10", "198.51.100.7", "203.0.113.9", "224.0.0.1", "255.255.255.255",
    "::1", "::", "fe80::1", "fc00::1", "fd12:3456::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
    "::ffff:169.254.169.254", "2002:7f00:1::1", "2002:a00:1::1", "64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe",
    "ff02::1", "2001:db8::1", "fec0::1", "feff:ffff::1",
]
PUBLIC_ADDRESSES = ["93.184.215.14", "151.101.0.81", "1.1.1.1", "2606:4700::6810:84e5", "2a00:1450:4009:81f::200e", "64:ff9b::5db8:d70e"]


@pytest.mark.parametrize("address", BLOCKED_ADDRESSES)
def test_private_and_special_addresses_are_not_public(address):
    assert is_public_address(ipaddress.ip_address(address)) is False


@pytest.mark.parametrize("address", PUBLIC_ADDRESSES)
def test_ordinary_internet_addresses_are_public(address):
    assert is_public_address(ipaddress.ip_address(address)) is True


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/feed", "gopher://example.com/", "javascript:alert(1)",
    "http://", "https:///path", "http://user:pass@example.com/feed", "//example.com/feed", "example.com/feed",
    "http://example.com:99999/", "http://exa mple.com/", "http://example.com/" + "a" * 3000, "",
])
def test_only_plain_http_and_https_urls_pass(url):
    with pytest.raises(UnsafeUrlError):
        check_url(url)


def test_good_urls_pass():
    assert check_url("https://feeds.bbci.co.uk/news/rss.xml").hostname == "feeds.bbci.co.uk"
    assert check_url("http://example.com:8080/feed?x=1").port == 8080


@pytest.mark.parametrize("host", ["private.example.test", "metadata.example.test", "testnet.example.test",
                                  "loopback.example.test", "v6.example.test", "mapped.example.test"])
def test_names_that_resolve_to_blocked_addresses_are_refused_before_connecting(fetcher, connector, host):
    with pytest.raises(BlockedAddressError):
        fetcher.fetch(f"http://{host}/feed.xml")
    assert connector.addresses == []


def numeric_only_resolver(host, port):
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)
    return [answer[4][0] for answer in answers]


@pytest.mark.parametrize("url", ["http://192.0.2.1/", "http://[::1]:8080/", "http://169.254.169.254/latest/meta-data/",
                                 "http://2130706433/", "http://0x7f.0.0.1/", "http://0177.0.0.1/", "http://10.1/"])
def test_literal_and_odd_address_forms_are_refused(connector, url):
    production = SafeFetcher(resolver=numeric_only_resolver, connector=connector, connect_timeout=1, total_timeout=1)
    with pytest.raises((BlockedAddressError, FetchError)):
        production.fetch(url)
    assert connector.addresses == []


def test_production_policy_refuses_the_local_test_server(server, connector):
    server.add("/feed.xml", body=b"<rss/>")
    production = SafeFetcher(connector=connector, connect_timeout=1, total_timeout=1)
    with pytest.raises(BlockedAddressError):
        production.fetch(server.url("/feed.xml"))
    with pytest.raises(BlockedAddressError):
        production.fetch(server.url("/feed.xml", host="localhost"))
    assert connector.addresses == []
    assert server.requests == []


def test_a_name_with_any_blocked_address_is_refused(fetcher, connector):
    with pytest.raises(BlockedAddressError):
        fetcher.fetch("http://mixed.example.test/feed.xml")
    assert connector.addresses == []


def test_connects_to_the_vetted_address_and_sends_the_real_host(server, fetcher, connector):
    server.add("/feed.xml", body=b"<rss>hi</rss>", headers={"Content-Type": "application/rss+xml", "ETag": '"v1"'})
    result = fetcher.fetch(server.url("/feed.xml", host="feeds.example.test"))
    assert result.status == 200
    assert result.body == b"<rss>hi</rss>"
    assert result.content_type == "application/rss+xml"
    assert result.etag == '"v1"'
    assert connector.addresses == ["127.0.0.1"]
    assert server.requests[0]["headers"]["Host"] == f"feeds.example.test:{server.port}"


def test_resolves_once_per_hop_so_a_second_answer_cannot_swap_in(server, connector):
    server.add("/feed.xml", body=b"ok")
    answers = iter([["127.0.0.1"], ["10.0.0.9"]])
    calls = []

    def flipping_resolver(host, port):
        calls.append(host)
        return next(answers)

    from conftest import allow_test_server
    flipping = SafeFetcher(resolver=flipping_resolver, is_allowed=allow_test_server, connector=connector, total_timeout=3)
    assert flipping.fetch(server.url("/feed.xml", host="feeds.example.test")).body == b"ok"
    assert calls == ["feeds.example.test"]
    assert connector.addresses == ["127.0.0.1"]


@pytest.mark.parametrize("target", [
    "http://192.0.2.10/feed.xml", "http://10.0.0.1/", "http://169.254.169.254/latest/meta-data/iam/",
    "http://metadata.example.test/", "http://[::1]/", "http://127.0.0.2/",
])
def test_every_redirect_is_checked_again(server, fetcher, connector, target):
    server.add("/moved", status=302, headers={"Location": target})
    with pytest.raises(BlockedAddressError):
        fetcher.fetch(server.url("/moved"))
    assert connector.addresses == ["127.0.0.1"]


@pytest.mark.parametrize("target", ["file:///etc/passwd", "ftp://127.0.0.1/feed", "gopher://127.0.0.1:70/_x"])
def test_redirects_to_other_schemes_are_refused(server, fetcher, target):
    server.add("/moved", status=301, headers={"Location": target})
    with pytest.raises(UnsafeUrlError):
        fetcher.fetch(server.url("/moved"))


def test_relative_redirects_are_followed(server, fetcher):
    server.add("/old", status=301, headers={"Location": "/new"})
    server.add("/new", body=b"here")
    result = fetcher.fetch(server.url("/old"))
    assert result.body == b"here"
    assert result.url == server.url("/new")


def test_redirect_loops_stop(server, fetcher):
    server.add("/loop", status=302, headers={"Location": "/loop"})
    with pytest.raises(FetchError, match="redirect"):
        fetcher.fetch(server.url("/loop"))
    assert len(server.requests) == fetcher.max_redirects + 1


def test_large_bodies_are_refused_by_content_length(server, fetcher):
    server.add("/big", body=b"x" * 10, headers={"Content-Length": str(fetcher.max_bytes + 1)})
    with pytest.raises(ResponseTooLargeError):
        fetcher.fetch(server.url("/big"))


def test_large_bodies_without_a_length_are_cut_off(server, fetcher):
    def endless(handler):
        handler.send_response(200)
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        chunk = b"y" * 65536
        try:
            for _ in range(20):
                handler.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            handler.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass

    server.add("/stream", handler=endless)
    with pytest.raises(ResponseTooLargeError):
        fetcher.fetch(server.url("/stream"))


def test_compression_bombs_are_cut_off(server, fetcher):
    bomb = gzip.compress(b"\0" * (fetcher.max_bytes * 20))
    assert len(bomb) < fetcher.max_bytes
    server.add("/bomb", body=bomb, headers={"Content-Encoding": "gzip"})
    with pytest.raises(ResponseTooLargeError):
        fetcher.fetch(server.url("/bomb"))


def test_gzip_bodies_are_unpacked(server, fetcher):
    server.add("/zipped", body=gzip.compress(b"<rss>zipped</rss>"), headers={"Content-Encoding": "gzip"})
    assert fetcher.fetch(server.url("/zipped")).body == b"<rss>zipped</rss>"
    assert "gzip" in server.requests[0]["headers"]["Accept-Encoding"]


def test_slow_drip_servers_hit_the_total_time_limit(server, connector):
    def drip(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "1000")
        handler.end_headers()
        try:
            for _ in range(40):
                handler.wfile.write(b"z")
                handler.wfile.flush()
                time.sleep(0.1)
        except OSError:
            pass

    server.add("/drip", handler=drip)
    from conftest import allow_test_server
    quick = SafeFetcher(resolver=fake_resolver, is_allowed=allow_test_server, connector=connector, connect_timeout=1, total_timeout=1)
    started = time.monotonic()
    with pytest.raises(FetchTimeoutError):
        quick.fetch(server.url("/drip"))
    assert time.monotonic() - started < 2.5


def test_conditional_headers_are_sent_and_304_is_returned(server, fetcher):
    server.add("/feed.xml", status=304)
    result = fetcher.fetch(server.url("/feed.xml"), etag='"v1"', last_modified="Wed, 11 Nov 2026 09:00:00 GMT")
    assert result.status == 304
    assert result.body == b""
    sent = server.requests[0]["headers"]
    assert sent["If-None-Match"] == '"v1"'
    assert sent["If-Modified-Since"] == "Wed, 11 Nov 2026 09:00:00 GMT"


def test_error_statuses_raise_with_retry_after(server, fetcher):
    server.add("/busy", status=429, headers={"Retry-After": "7200"})
    with pytest.raises(HttpStatusError) as caught:
        fetcher.fetch(server.url("/busy"))
    assert caught.value.status == 429
    assert caught.value.retry_after == 7200


def test_unknown_names_are_a_fetch_error(fetcher):
    with pytest.raises(FetchError):
        fetcher.fetch("http://nowhere.example.test/feed")


def test_header_injection_in_urls_is_refused(fetcher):
    with pytest.raises(UnsafeUrlError):
        fetcher.fetch("http://feeds.example.test/feed\r\nX-Evil: 1")


def test_refusal_messages_name_the_host_and_the_address(fetcher):
    with pytest.raises(BlockedAddressError, match=r"^private\.example\.test points at 10\.0\.0\.5, which is not a public internet address$"):
        fetcher.fetch("http://private.example.test/")
    with pytest.raises(BlockedAddressError, match=r"^10\.0\.0\.1 is not a public internet address$"):
        fetcher.fetch("http://10.0.0.1/")


HEADER_DRIP_SECONDS = 0.3
HEADER_DRIP_LINES = 30


def drip_headers(send) -> None:
    try:
        send(b"HTTP/1.1 200 OK\r\n")
        for number in range(HEADER_DRIP_LINES):
            send(b"X-Slow-%d: y\r\n" % number)
            time.sleep(HEADER_DRIP_SECONDS)
    except OSError:
        return


def quick_fetcher(connector, total_timeout=1.0):
    from conftest import allow_test_server
    return SafeFetcher(resolver=fake_resolver, is_allowed=allow_test_server, connector=connector, connect_timeout=1,
                       total_timeout=total_timeout)


def test_servers_that_drip_the_headers_hit_the_total_time_limit(server, connector):
    server.add("/slow-headers", handler=lambda handler: drip_headers(handler.wfile.write))
    started = time.monotonic()
    with pytest.raises(FetchTimeoutError):
        quick_fetcher(connector).fetch(server.url("/slow-headers"))
    assert time.monotonic() - started < 2.5


@pytest.fixture
def tls_files(tmp_path):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is needed to make a test certificate")
    key, certificate = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=feeds.example.test",
                    "-addext", "subjectAltName=DNS:feeds.example.test", "-keyout", str(key), "-out", str(certificate)],
                   check=True, capture_output=True)
    return key, certificate


def start_tls_dripper(key, certificate) -> int:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    listener = socket.create_server(("127.0.0.1", 0))

    def serve_one():
        accepted, _ = listener.accept()
        listener.close()
        with context.wrap_socket(accepted, server_side=True) as secure:
            secure.recv(65536)
            drip_headers(secure.sendall)

    threading.Thread(target=serve_one, daemon=True).start()
    return listener.getsockname()[1]


def test_https_servers_that_drip_the_headers_hit_the_total_time_limit(tls_files, connector):
    key, certificate = tls_files
    port = start_tls_dripper(key, certificate)
    fetcher = quick_fetcher(connector)
    fetcher.ssl_context.load_verify_locations(certificate)
    started = time.monotonic()
    with pytest.raises(FetchTimeoutError):
        fetcher.fetch(f"https://feeds.example.test:{port}/feed.xml")
    assert time.monotonic() - started < 2.5


def test_a_fetch_that_finishes_in_time_leaves_no_timer_running(server, connector):
    server.add("/quick", body=b"ok", headers={"Content-Type": "text/plain"})
    before = threading.active_count()
    assert quick_fetcher(connector, total_timeout=30).fetch(server.url("/quick")).body == b"ok"
    deadline = time.monotonic() + 2
    while threading.active_count() > before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert threading.active_count() <= before
