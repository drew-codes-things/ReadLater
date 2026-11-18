import socket
import time
from urllib.parse import urlsplit

from .extract import ExtractError, decode_page, extract_article, is_html_type
from .library import LibraryError
from .netguard import (
    DEFAULT_PORTS,
    BlockedAddressError,
    FetchError,
    HttpStatusError,
    ResponseTooLargeError,
    UnsafeUrlError,
    check_url,
)

LINK_CHECK_BYTES = 64 * 1024
DEAD_STATUSES = frozenset({404, 410})
MISSING_NAME_ERRORS = frozenset(code for code in (getattr(socket, "EAI_NONAME", None), getattr(socket, "EAI_NODATA", None)) if code is not None)
NAME_GONE_TEXT = "the site's name no longer exists"


class NotAWebPageError(ExtractError):
    pass


class Saver:
    def __init__(self, library, fetcher, clock=time.time):
        self.library = library
        self.fetcher = fetcher
        self.clock = clock

    def _fetch_article(self, url: str):
        result = self.fetcher.fetch(url)
        if not is_html_type(result.content_type):
            raise NotAWebPageError(f"not a web page ({result.content_type}), kept as a link")
        page = decode_page(result.body, result.headers.get("content-type", ""))
        return extract_article(page, result.url)

    def save(self, url, title: str = "", tags=(), collection_id=None) -> dict:
        try:
            check_url(url)
        except UnsafeUrlError as error:
            raise LibraryError(str(error)) from error
        names = self.library.check_new_item(tags, collection_id)
        existing = self.library.find_by_url(url)
        if existing is not None:
            raise LibraryError("that address is already saved", 409, item_id=existing)
        try:
            article = self._fetch_article(url)
        except (UnsafeUrlError, BlockedAddressError) as error:
            raise LibraryError(str(error)) from error
        except (FetchError, ExtractError) as error:
            return self.library.add_item(url, title=title if isinstance(title, str) else "", fetch_error=str(error),
                                         tags=names, collection_id=collection_id)
        return self.library.add_item(url, article=article, tags=names, collection_id=collection_id)

    def refetch(self, item_id: int) -> dict:
        item = self.library.get_item(item_id)
        try:
            article = self._fetch_article(item["url"])
        except (FetchError, ExtractError) as error:
            return self.library.record_fetch_error(item_id, str(error))
        return self.library.store_article(item_id, article)

    def _name_is_gone(self, url: str) -> bool:
        parts = urlsplit(url)
        try:
            self.fetcher.resolver(parts.hostname, parts.port or DEFAULT_PORTS.get(parts.scheme.lower(), 80))
        except socket.gaierror as error:
            return error.errno in MISSING_NAME_ERRORS
        except (OSError, UnicodeError, ValueError):
            return False
        return False

    def _link_state(self, url: str) -> tuple[str, str]:
        try:
            self.fetcher.fetch(url, max_bytes=LINK_CHECK_BYTES)
        except ResponseTooLargeError:
            return "ok", ""
        except HttpStatusError as error:
            return ("dead" if error.status in DEAD_STATUSES else "error"), str(error)
        except (UnsafeUrlError, BlockedAddressError) as error:
            return "error", str(error)
        except FetchError as error:
            if self._name_is_gone(url):
                return "dead", NAME_GONE_TEXT
            return "error", str(error)
        return "ok", ""

    def check_link(self, item_id: int) -> dict:
        item = self.library.get_item(item_id)
        status, detail = self._link_state(item["url"])
        return self.library.record_link_status(item_id, status, detail)
