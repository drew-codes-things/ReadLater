import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, abort, g, jsonify, redirect, request, send_from_directory
from werkzeug.exceptions import HTTPException

from . import __version__
from .auth import csrf_matches, csrf_token
from .images import RASTER_IMAGE_TYPES, ImageSigner
from .library import LARGEST_DATABASE_INTEGER, LibraryError
from .netguard import BlockedAddressError, FetchError, UnsafeUrlError
from .sanitize import clean_article_html
from .signin import SIGN_IN_PATHS, PasswordAuth, add_sign_in_pages
from .transfer import export_bookmarks_html, export_json, import_any

STATIC_DIR = Path(__file__).resolve().parent / "static"
APP_NAME = "Starfire Read Later"
MAX_REQUEST_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
PUBLIC_PATHS = ("/api/health",)
SIGNED_OUT_TEXT = "Sign in through the main sign-in page first."
NESTED_TOO_DEEPLY_TEXT = "that JSON is nested too deeply"
WRONG_HOST_TEXT = ("this app does not answer on that host name (AUTH_MODE=local answers on localhost, 127.0.0.1 or "
                   "[::1] at its own port; the own sign-in answers on the names in ALLOWED_HOSTS, or with it empty on "
                   "localhost, IP addresses and this machine's name)")
MISDIRECTED_REQUEST = 421
DEFAULT_API_LIMIT = 20
MAX_API_LIMIT = 100
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 500
MAX_QUERY_LENGTH = 200
MAX_TAG_FILTER = 50
LARGEST_ID = 2 ** 62
JSON_INDENT = 2
IMAGE_CACHE_CONTROL = "private, max-age=86400"
IMAGE_ACCEPT = "image/avif, image/webp, image/png, image/jpeg, image/gif;q=0.9"
IMAGE_POLICY = "default-src 'none'; sandbox"
EXPORT_FORMATS = {
    "json": ("application/json", "json"),
    "html": ("text/html", "html"),
}
CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "font-src 'self'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "manifest-src 'self'",
    "media-src 'none'",
    "frame-src 'none'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
))

log = logging.getLogger("readlater.web")


def _error(message: str, status: int, **extra):
    return jsonify({"error": message, **extra}), status


def _body() -> dict:
    try:
        data = request.get_json(silent=True)
    except RecursionError as error:
        raise LibraryError(NESTED_TOO_DEEPLY_TEXT) from error
    if not isinstance(data, dict):
        raise LibraryError("send a JSON object")
    return data


def _int_arg(name: str, default, minimum: int, maximum: int):
    raw = request.args.get(name, "")
    if raw == "":
        return default
    if not (raw.isascii() and raw.isdigit()):
        raise LibraryError(f"{name} must be a whole number")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise LibraryError(f"{name} must be between {minimum} and {maximum}")
    return value


def _is_same_origin() -> bool:
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    return urlsplit(origin).netloc == request.host


def _without_content(item: dict) -> dict:
    return {key: value for key, value in item.items() if key != "contentHtml"}


def _attachment(body, mimetype: str, filename: str) -> Response:
    response = Response(body, mimetype=mimetype)
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response


def create_app(library, saver, link_checker, auth, secret: bytes, image_fetcher=None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    has_own_sign_in = isinstance(auth, PasswordAuth)
    if has_own_sign_in:
        add_sign_in_pages(app, auth, APP_NAME)
    signer = ImageSigner(secret)
    images = image_fetcher or saver.fetcher

    @app.url_value_preprocessor
    def refuse_ids_beyond_the_database(endpoint, values):
        if any(isinstance(value, int) and value > LARGEST_DATABASE_INTEGER for value in (values or {}).values()):
            abort(404)

    @app.before_request
    def identify_and_guard_writes():
        if request.path in PUBLIC_PATHS:
            return None
        if not auth.accepts_host(request.host):
            return _error(WRONG_HOST_TEXT, MISDIRECTED_REQUEST)
        if has_own_sign_in and request.path in SIGN_IN_PATHS:
            return None
        user = auth.user_for(request.remote_addr or "", request.headers)
        if user is None:
            if request.path.startswith("/api/") or request.path == "/img":
                return _error("sign in first", 401)
            if has_own_sign_in:
                return redirect(auth.entry_path(), code=303)
            return Response(SIGNED_OUT_TEXT, status=401, mimetype="text/plain")
        g.user = user
        if request.method in SAFE_METHODS:
            return None
        if not _is_same_origin():
            return _error("this request came from another site", 403)
        if not csrf_matches(secret, user, request.headers.get(CSRF_HEADER)):
            return _error("missing or wrong CSRF token, reload the page", 403)
        return None

    @app.after_request
    def add_security_headers(response: Response) -> Response:
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(LibraryError)
    def library_refused(error: LibraryError):
        extra = {"id": error.item_id} if error.item_id is not None else {}
        return _error(str(error), error.status, **extra)

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException):
        if error.code == 413:
            return _error("that request is too large", 413)
        if request.path.startswith("/api/"):
            return _error(error.description or error.name, error.code or 500)
        return error

    def page():
        response = send_from_directory(STATIC_DIR, "index.html", max_age=0)
        response.headers["Cache-Control"] = "no-cache"
        return response

    app.add_url_rule("/", "index", page)
    app.add_url_rule("/save", "save_page", page)

    @app.get("/static/<path:name>")
    def static_file(name: str):
        return send_from_directory(STATIC_DIR, name, max_age=0)

    @app.get("/api/health")
    def health():
        if not library.database.is_reachable():
            log.error("health check: the database could not be read at %s", library.database.path)
            return jsonify({"ok": False, "error": "database unavailable"}), 503
        return jsonify({"ok": True})

    @app.get("/api/session")
    def session_state():
        return jsonify({"user": g.user, "csrfToken": csrf_token(secret, g.user), "version": __version__})

    @app.get("/api/library")
    def library_overview():
        return jsonify({"collections": library.list_collections(), "tags": library.list_tags(), "counts": library.counts()})

    @app.get("/api/items")
    def list_items():
        return jsonify(library.list_items(
            view=request.args.get("view", "inbox"),
            collection_id=_int_arg("collection", None, 1, LARGEST_ID),
            tag=request.args.get("tag", "")[:MAX_TAG_FILTER],
            query=request.args.get("q", "")[:MAX_QUERY_LENGTH],
            before=request.args.get("before", ""),
            limit=_int_arg("limit", DEFAULT_LIST_LIMIT, 1, MAX_LIST_LIMIT),
        ))

    @app.post("/api/items")
    def save_item():
        data = _body()
        item = saver.save(data.get("url"), title=data.get("title") or "", tags=data.get("tags", []),
                          collection_id=data.get("collectionId"))
        return jsonify(item), 201

    @app.get("/api/items/<int:item_id>")
    def read_item(item_id: int):
        item = library.get_item(item_id)
        proxy = signer.proxy_path if item["loadImages"] else None
        cleaned = clean_article_html(item["contentHtml"], item["url"], proxy)
        return jsonify({**_without_content(item), "html": cleaned.html, "hiddenImages": cleaned.hidden_images})

    @app.patch("/api/items/<int:item_id>")
    def change_item(item_id: int):
        return jsonify(_without_content(library.update_item(item_id, _body())))

    @app.delete("/api/items/<int:item_id>")
    def delete_item(item_id: int):
        library.delete_item(item_id)
        return "", 204

    @app.post("/api/items/<int:item_id>/refetch")
    def refetch_item(item_id: int):
        return jsonify(_without_content(saver.refetch(item_id)))

    @app.post("/api/items/<int:item_id>/check")
    def check_item_link(item_id: int):
        return jsonify(_without_content(saver.check_link(item_id)))

    @app.post("/api/items/<int:item_id>/highlights")
    def add_highlight(item_id: int):
        return jsonify(library.add_highlight(item_id, _body().get("text"))), 201

    @app.delete("/api/highlights/<int:highlight_id>")
    def delete_highlight(highlight_id: int):
        library.delete_highlight(highlight_id)
        return "", 204

    @app.post("/api/collections")
    def create_collection():
        return jsonify(library.create_collection(_body().get("name"))), 201

    @app.patch("/api/collections/<int:collection_id>")
    def rename_collection(collection_id: int):
        return jsonify(library.rename_collection(collection_id, _body().get("name")))

    @app.delete("/api/collections/<int:collection_id>")
    def delete_collection(collection_id: int):
        library.delete_collection(collection_id)
        return "", 204

    @app.get("/api/link-check")
    def link_check_status():
        return jsonify(link_checker.status())

    @app.post("/api/link-check")
    def start_link_check():
        return jsonify(link_checker.start(_body().get("view", "all"))), 202

    @app.get("/api/export")
    def export_library():
        chosen = request.args.get("format", "json")
        if chosen not in EXPORT_FORMATS:
            raise LibraryError(f"format must be one of {', '.join(EXPORT_FORMATS)}")
        mimetype, extension = EXPORT_FORMATS[chosen]
        day = datetime.fromtimestamp(library.clock(), timezone.utc).strftime("%Y-%m-%d")
        if chosen == "json":
            body = json.dumps(export_json(library), indent=JSON_INDENT, ensure_ascii=False)
        else:
            body = export_bookmarks_html(library)
        return _attachment(body, mimetype, f"read-later-{day}.{extension}")

    @app.post("/api/import")
    def import_library():
        return jsonify(import_any(library, request.get_data(cache=False)))

    @app.get("/api/v1/unread")
    def api_unread():
        limit = _int_arg("limit", DEFAULT_API_LIMIT, 1, MAX_API_LIMIT)
        return jsonify({"unread": library.counts()["inbox"], "items": library.headlines(limit)})

    @app.get("/api/v1/search")
    def api_search():
        query = request.args.get("q", "").strip()
        if not query or len(query) > MAX_QUERY_LENGTH:
            raise LibraryError(f"q must be between 1 and {MAX_QUERY_LENGTH} characters")
        limit = _int_arg("limit", DEFAULT_API_LIMIT, 1, MAX_API_LIMIT)
        return jsonify({"query": query, "items": library.search_headlines(query, limit)})

    @app.get("/img")
    def image_proxy():
        url = request.args.get("u", "")
        if not signer.matches(url, request.args.get("s", "")):
            return _error("that image link was not made by this app", 403)
        try:
            result = images.fetch(url, accept=IMAGE_ACCEPT, max_bytes=MAX_IMAGE_BYTES)
        except (UnsafeUrlError, BlockedAddressError) as error:
            return _error(str(error), 403)
        except FetchError as error:
            return _error(f"the image could not be fetched: {error}", 502)
        if result.content_type not in RASTER_IMAGE_TYPES:
            return _error("only PNG, JPEG, GIF, WebP and AVIF images are passed through", 415)
        response = Response(result.body, mimetype=result.content_type)
        response.headers["Cache-Control"] = IMAGE_CACHE_CONTROL
        response.headers["Content-Security-Policy"] = IMAGE_POLICY
        return response

    return app
