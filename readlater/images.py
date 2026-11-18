import hashlib
import hmac
from urllib.parse import urlencode

IMAGE_PURPOSE = b"read-later image\x00"
PROXY_PATH = "/img"
PROXY_PREFIX = PROXY_PATH + "?"
RASTER_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"})


class ImageSigner:
    def __init__(self, secret: bytes):
        self.secret = secret

    def sign(self, url: str) -> str:
        return hmac.new(self.secret, IMAGE_PURPOSE + url.encode(), hashlib.sha256).hexdigest()

    def matches(self, url, signature) -> bool:
        if not isinstance(url, str) or not isinstance(signature, str) or not signature:
            return False
        return hmac.compare_digest(self.sign(url), signature)

    def proxy_path(self, url: str) -> str:
        return f"{PROXY_PATH}?{urlencode({'u': url, 's': self.sign(url)})}"
