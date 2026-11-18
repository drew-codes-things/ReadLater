import contextlib
import email.utils
import http.client
import ipaddress
import socket
import ssl
import threading
import time
import zlib
from dataclasses import dataclass
from datetime import timezone
from urllib.parse import SplitResult, quote, urljoin, urlsplit

from . import __version__

ALLOWED_SCHEMES = ("http", "https")
DEFAULT_PORTS = {"http": 80, "https": 443}
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
NOT_MODIFIED = 304
MAX_URL_LENGTH = 2048
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_TOTAL_TIMEOUT = 30.0
DEFAULT_MAX_REDIRECTS = 5
READ_CHUNK_BYTES = 65536
ZLIB_AUTO_HEADER_WINDOW = 32 + zlib.MAX_WBITS
COMPRESSED_ENCODINGS = ("gzip", "x-gzip", "deflate")
PLAIN_ENCODINGS = ("", "identity")
NAT64_NETWORK = ipaddress.ip_network("64:ff9b::/96")
LOW_32_BITS = 0xFFFFFFFF
URL_SAFE_CHARACTERS = "/%:@!$&'()*+,;=-._~"
QUERY_SAFE_CHARACTERS = URL_SAFE_CHARACTERS + "?"
USER_AGENT = f"ReadLater/{__version__} (self-hosted read-later app)"
DEFAULT_ACCEPT = "text/html, application/xhtml+xml;q=0.9, */*;q=0.5"


class FetchError(Exception):
    pass


class UnsafeUrlError(FetchError):
    pass


class BlockedAddressError(FetchError):
    pass


class ResponseTooLargeError(FetchError):
    pass


class FetchTimeoutError(FetchError):
    pass


class HttpStatusError(FetchError):
    def __init__(self, status: int, retry_after: int | None = None):
        super().__init__(f"the server answered {status}")
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    headers: dict
    body: bytes

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")


def _embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        return address.teredo[1]
    if address in NAT64_NETWORK:
        return ipaddress.IPv4Address(int(address) & LOW_32_BITS)
    return None


def is_public_address(address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(address)
        if embedded is not None:
            return is_public_address(embedded)
        if address.is_site_local:
            return False
    is_special = (address.is_multicast or address.is_unspecified or address.is_loopback
                  or address.is_link_local or address.is_private or address.is_reserved)
    return not is_special and address.is_global


def _has_unsafe_characters(text: str) -> bool:
    return any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in text)


def check_url(url) -> SplitResult:
    if not isinstance(url, str) or not url:
        raise UnsafeUrlError("give a web address starting with http:// or https://")
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeUrlError(f"the address is longer than {MAX_URL_LENGTH} characters")
    if _has_unsafe_characters(url):
        raise UnsafeUrlError("the address has spaces or control characters in it")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeUrlError("only http:// and https:// addresses can be fetched")
    if parts.username is not None or parts.password is not None:
        raise UnsafeUrlError("addresses with a user name or password in them are not supported")
    if not parts.hostname:
        raise UnsafeUrlError("the address has no host name")
    try:
        parts.port
    except ValueError as error:
        raise UnsafeUrlError("the address has a bad port number") from error
    return parts


def _ascii_host(hostname: str) -> str:
    try:
        return hostname.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise UnsafeUrlError(f"the host name {hostname!r} is not valid") from error


def _request_target(parts: SplitResult) -> str:
    path = quote(parts.path or "/", safe=URL_SAFE_CHARACTERS)
    if not parts.query:
        return path
    return f"{path}?{quote(parts.query, safe=QUERY_SAFE_CHARACTERS)}"


def parse_retry_after(value: str | None, now: float) -> int | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int(when.timestamp() - now))


def _refusal(host: str, address) -> str:
    if host.strip("[]") == str(address):
        return f"{address} is not a public internet address"
    return f"{host} points at {address}, which is not a public internet address"


def system_resolver(host: str, port: int) -> list[str]:
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(answer[4][0] for answer in answers))


def _ignore_address(opener):
    def create_connection(address, timeout=None, source_address=None):
        return opener()
    return create_connection


class _GuardedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, timeout, opener):
        super().__init__(host, port, timeout=timeout)
        self._create_connection = _ignore_address(opener)


class _GuardedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, timeout, context, opener):
        super().__init__(host, port, timeout=timeout, context=context)
        self._create_connection = _ignore_address(opener)


class _BodyDecoder:
    def __init__(self, content_encoding: str | None):
        encoding = (content_encoding or "").strip().lower()
        if encoding in PLAIN_ENCODINGS:
            self.inflater = None
        elif encoding in COMPRESSED_ENCODINGS:
            self.inflater = zlib.decompressobj(ZLIB_AUTO_HEADER_WINDOW)
        else:
            raise FetchError(f"the server sent an unsupported content encoding {encoding!r}")

    def decode(self, chunk: bytes, room: int) -> bytes:
        if self.inflater is None:
            return chunk
        try:
            return self.inflater.decompress(chunk, room + 1)
        except zlib.error as error:
            raise FetchError(f"the compressed answer is damaged: {error}") from error


class _Watchdog:
    def __init__(self, seconds: float):
        self._lock = threading.Lock()
        self._watched = []
        self.fired = False
        self._timer = threading.Timer(max(seconds, 0), self._cut_off)
        self._timer.daemon = True
        self._timer.start()

    def watch(self, live_socket) -> None:
        duplicate = live_socket.dup()
        with self._lock:
            self._watched.append(duplicate)
            fired = self.fired
        if fired:
            self._shut(duplicate)

    @staticmethod
    def _shut(watched) -> None:
        with contextlib.suppress(OSError):
            watched.shutdown(socket.SHUT_RDWR)

    def _cut_off(self) -> None:
        with self._lock:
            self.fired = True
            watched = list(self._watched)
        for duplicate in watched:
            self._shut(duplicate)

    def stop(self) -> None:
        self._timer.cancel()
        self._timer.join()
        with self._lock:
            watched, self._watched = self._watched, []
        for duplicate in watched:
            duplicate.close()


class SafeFetcher:
    def __init__(self, resolver=system_resolver, is_allowed=is_public_address, connector=socket.create_connection,
                 connect_timeout: float = DEFAULT_CONNECT_TIMEOUT, total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
                 max_bytes: int = DEFAULT_MAX_BYTES, max_redirects: int = DEFAULT_MAX_REDIRECTS,
                 user_agent: str = USER_AGENT, clock=time.monotonic):
        self.resolver = resolver
        self.is_allowed = is_allowed
        self.connector = connector
        self.connect_timeout = connect_timeout
        self.total_timeout = total_timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.user_agent = user_agent
        self.clock = clock
        self.ssl_context = ssl.create_default_context()

    def fetch(self, url: str, etag: str | None = None, last_modified: str | None = None,
              accept: str = DEFAULT_ACCEPT, max_bytes: int | None = None) -> FetchResult:
        limit = max_bytes or self.max_bytes
        deadline = self.clock() + self.total_timeout
        headers = self._request_headers(etag, last_modified, accept)
        watchdog = _Watchdog(self.total_timeout)
        try:
            result = self._follow(url, headers, limit, deadline, watchdog)
        except FetchTimeoutError:
            raise
        except FetchError as error:
            if watchdog.fired:
                raise self._timed_out() from error
            raise
        finally:
            watchdog.stop()
        if watchdog.fired:
            raise self._timed_out()
        return result

    def _timed_out(self) -> FetchTimeoutError:
        return FetchTimeoutError(f"the fetch took longer than {self.total_timeout:g} seconds")

    def _follow(self, url: str, headers: dict, limit: int, deadline: float, watchdog: _Watchdog) -> FetchResult:
        current = url
        for _ in range(self.max_redirects + 1):
            parts = check_url(current)
            connection, live_socket, response = self._send(parts, headers, deadline, watchdog)
            try:
                if response.status in REDIRECT_STATUSES:
                    current = self._redirect_target(current, response)
                    continue
                return self._finish(current, response, live_socket, limit, deadline)
            finally:
                response.close()
                connection.close()
        raise FetchError(f"gave up after {self.max_redirects} redirects")

    def _request_headers(self, etag, last_modified, accept) -> dict:
        headers = {"User-Agent": self.user_agent, "Accept": accept, "Accept-Encoding": "gzip, deflate"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        return headers

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise self._timed_out()
        return min(remaining, self.connect_timeout)

    def _vetted_addresses(self, host: str, port: int) -> list:
        try:
            resolved = self.resolver(host, port)
        except (OSError, UnicodeError) as error:
            raise FetchError(f"could not look up {host}: {error}") from error
        if not resolved:
            raise FetchError(f"{host} has no addresses")
        addresses = []
        for raw in resolved:
            address = ipaddress.ip_address(raw.split("%", 1)[0])
            if not self.is_allowed(address):
                raise BlockedAddressError(_refusal(host, address))
            addresses.append(address)
        return addresses

    def _open_socket(self, host: str, port: int, deadline: float, watchdog: _Watchdog):
        last_error = None
        for address in self._vetted_addresses(host, port):
            try:
                live_socket = self.connector((str(address), port), timeout=self._remaining(deadline))
            except OSError as error:
                last_error = error
                continue
            watchdog.watch(live_socket)
            return live_socket
        raise FetchError(f"could not connect to {host}:{port}: {last_error}")

    def _connection_for(self, parts: SplitResult, deadline: float, watchdog: _Watchdog):
        host = _ascii_host(parts.hostname)
        scheme = parts.scheme.lower()
        port = parts.port or DEFAULT_PORTS[scheme]

        def opener():
            return self._open_socket(host, port, deadline, watchdog)

        timeout = self._remaining(deadline)
        if scheme == "https":
            return _GuardedHTTPSConnection(host, port, timeout, self.ssl_context, opener)
        return _GuardedHTTPConnection(host, port, timeout, opener)

    def _send(self, parts: SplitResult, headers: dict, deadline: float, watchdog: _Watchdog):
        connection = self._connection_for(parts, deadline, watchdog)
        try:
            connection.request("GET", _request_target(parts), headers=headers)
            live_socket = connection.sock
            response = connection.getresponse()
        except FetchError:
            connection.close()
            raise
        except TimeoutError as error:
            connection.close()
            raise FetchTimeoutError(f"{parts.hostname} did not answer in time") from error
        except ssl.SSLError as error:
            connection.close()
            raise FetchError(f"the secure connection to {parts.hostname} failed: {error.reason or error}") from error
        except (OSError, http.client.HTTPException) as error:
            connection.close()
            raise FetchError(f"could not fetch from {parts.hostname}: {error}") from error
        return connection, live_socket, response

    def _redirect_target(self, current: str, response) -> str:
        location = response.getheader("Location")
        if not location:
            raise FetchError(f"the server answered {response.status} without saying where to go")
        return urljoin(current, location.strip())

    def _finish(self, url: str, response, live_socket, limit: int, deadline: float) -> FetchResult:
        headers = {name.lower(): value for name, value in response.getheaders()}
        if response.status == NOT_MODIFIED:
            return FetchResult(url=url, status=NOT_MODIFIED, headers=headers, body=b"")
        if not 200 <= response.status < 300:
            raise HttpStatusError(response.status, parse_retry_after(headers.get("retry-after"), time.time()))
        declared = headers.get("content-length", "").strip()
        if declared.isdigit() and int(declared) > limit:
            raise ResponseTooLargeError(f"the answer is {declared} bytes, more than the {limit} byte limit")
        body = self._read_limited(response, live_socket, limit, deadline)
        return FetchResult(url=url, status=response.status, headers=headers, body=body)

    def _read_limited(self, response, live_socket, limit: int, deadline: float) -> bytes:
        decoder = _BodyDecoder(response.getheader("Content-Encoding"))
        pieces = []
        size = 0
        while not response.isclosed():
            live_socket.settimeout(self._remaining(deadline))
            try:
                chunk = response.read1(READ_CHUNK_BYTES)
            except TimeoutError as error:
                raise FetchTimeoutError("the server stopped sending in the middle of the answer") from error
            except (OSError, http.client.HTTPException) as error:
                raise FetchError(f"the answer was cut off: {error}") from error
            if not chunk:
                break
            piece = decoder.decode(chunk, limit - size)
            size += len(piece)
            if size > limit:
                raise ResponseTooLargeError(f"the answer is larger than the {limit} byte limit")
            pieces.append(piece)
        return b"".join(pieces)
