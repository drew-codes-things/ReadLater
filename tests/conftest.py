import ipaddress
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

START_TIME = 1_800_000_000.0
OWNER = "demo@example.com"
SECRET = b"k" * 32
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEST_SERVER_ADDRESS = ipaddress.ip_address("127.0.0.1")
FAKE_DNS = {
    "feeds.example.test": ["127.0.0.1"],
    "site.example.test": ["127.0.0.1"],
    "blog.example.test": ["127.0.0.1"],
    "private.example.test": ["10.0.0.5"],
    "metadata.example.test": ["169.254.169.254"],
    "testnet.example.test": ["192.0.2.10"],
    "loopback.example.test": ["127.0.0.2"],
    "mixed.example.test": ["127.0.0.1", "192.0.2.20"],
    "v6.example.test": ["::1"],
    "mapped.example.test": ["::ffff:10.0.0.1"],
}


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeClock:
    def __init__(self, start: float = START_TIME):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Route:
    def __init__(self, status=200, body=b"", headers=None, handler=None):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.handler = handler


class LocalServer:
    def __init__(self):
        self.routes = {}
        self.requests = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                server.requests.append({"path": self.path, "headers": dict(self.headers)})
                route = server.routes.get(self.path.split("?")[0])
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if route.handler is not None:
                    route.handler(self)
                    return
                self.send_response(route.status)
                for name, value in route.headers.items():
                    self.send_header(name, value)
                if "Content-Length" not in route.headers:
                    self.send_header("Content-Length", str(len(route.body)))
                self.end_headers()
                self.wfile.write(route.body)

            def log_message(self, *args):
                return None

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}{path}"

    def add(self, path: str, **kwargs) -> None:
        self.routes[path] = Route(**kwargs)

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    local = LocalServer()
    yield local
    local.stop()


def fake_resolver(host: str, port: int) -> list[str]:
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    if host not in FAKE_DNS:
        raise socket.gaierror(socket.EAI_NONAME, f"unknown test host {host}")
    return list(FAKE_DNS[host])


class RecordingConnector:
    def __init__(self):
        self.addresses = []

    def __call__(self, address, timeout=None):
        self.addresses.append(address[0])
        return socket.create_connection(address, timeout=timeout)


@pytest.fixture
def connector():
    return RecordingConnector()


def allow_test_server(address) -> bool:
    from readlater.netguard import is_public_address
    return address == TEST_SERVER_ADDRESS or is_public_address(address)


@pytest.fixture
def fetcher(connector):
    from readlater.netguard import SafeFetcher
    return SafeFetcher(resolver=fake_resolver, is_allowed=allow_test_server, connector=connector,
                       connect_timeout=2.0, total_timeout=3.0, max_bytes=200_000)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def database(tmp_path):
    from readlater.db import Database
    return Database(tmp_path / "read-later.db")


@pytest.fixture
def library(database, clock):
    from readlater.library import Library
    return Library(database, clock=clock)


@pytest.fixture
def saver(library, fetcher, clock):
    from readlater.saver import Saver
    return Saver(library, fetcher, clock=clock)


@pytest.fixture
def link_checker(saver):
    from readlater.linkcheck import LinkChecker
    return LinkChecker(saver)


@pytest.fixture
def app(library, saver, link_checker):
    from readlater.auth import ProxyAuth
    from readlater.web import create_app
    auth = ProxyAuth(trusted_proxies=("127.0.0.1", "::1"), user_header="X-Forwarded-User")
    flask_app = create_app(library, saver, link_checker, auth, SECRET)
    flask_app.testing = True
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


class SignedInClient:
    def __init__(self, client):
        self.client = client
        self.headers = {"X-Forwarded-User": OWNER}
        session = client.get("/api/session", headers=self.headers).get_json()
        self.write_headers = {**self.headers, "X-CSRF-Token": session["csrfToken"]}

    def get(self, path, **kwargs):
        return self.client.get(path, headers=self.headers, **kwargs)

    def post(self, path, json=None, **kwargs):
        return self.client.post(path, json=json, headers=self.write_headers, **kwargs)

    def post_raw(self, path, data, content_type):
        return self.client.post(path, data=data, headers={**self.write_headers, "Content-Type": content_type})

    def patch(self, path, json=None, **kwargs):
        return self.client.patch(path, json=json, headers=self.write_headers, **kwargs)

    def delete(self, path, **kwargs):
        return self.client.delete(path, headers=self.write_headers, **kwargs)


@pytest.fixture
def owner(client):
    return SignedInClient(client)
