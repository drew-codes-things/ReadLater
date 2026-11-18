import logging
import sys

from waitress import serve

from .auth import LocalAuth, ProxyAuth, SecretKeyError, load_or_create_secret
from .config import ConfigError, Settings, load_settings
from .db import Database
from .library import Library
from .linkcheck import LinkChecker
from .netguard import SafeFetcher
from .signin import OWNER_FILE_NAME, OwnerAccount, PasswordAuth
from .saver import Saver
from .web import MAX_IMAGE_BYTES, create_app

WAITRESS_THREADS = 8
SESSION_COOKIE = "read_later_session"
IMAGE_TOTAL_TIMEOUT = 10.0

log = logging.getLogger("readlater")


def build_auth(settings: Settings, secret: bytes):
    if settings.auth_mode == "local":
        return LocalAuth(settings.local_user, settings.port)
    if settings.auth_mode == "proxy":
        return ProxyAuth(settings.trusted_proxies, settings.user_header, settings.allowed_users)
    return PasswordAuth(OwnerAccount(settings.data_dir / OWNER_FILE_NAME), secret, SESSION_COOKIE, settings.secure_cookies,
                        settings.allowed_hosts, trusted_proxies=settings.trusted_proxies)


def waitress_options(settings: Settings) -> dict:
    keeps_forwarded_for = settings.auth_mode == "builtin" and bool(settings.trusted_proxies)
    return {"clear_untrusted_proxy_headers": not keeps_forwarded_for}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
    except ConfigError as error:
        log.error("configuration problem: %s", error)
        return 2
    library = Library(Database(settings.database_path))
    saver = Saver(library, SafeFetcher())
    try:
        secret = load_or_create_secret(settings.data_dir, settings.secret_key)
    except SecretKeyError as error:
        log.error("secret key problem: %s", error)
        return 2
    image_fetcher = SafeFetcher(total_timeout=IMAGE_TOTAL_TIMEOUT, max_bytes=MAX_IMAGE_BYTES)
    auth = build_auth(settings, secret)
    if isinstance(auth, PasswordAuth) and auth.needs_setup():
        auth.setup_code()
    app = create_app(library, saver, LinkChecker(saver), auth, secret, image_fetcher=image_fetcher)
    log.info("Read Later on http://%s:%d, data in %s, sign-in %s",
             settings.bind_host, settings.port, settings.data_dir, settings.auth_mode)
    serve(app, host=settings.bind_host, port=settings.port, threads=WAITRESS_THREADS, **waitress_options(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
