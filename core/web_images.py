"""Getting a picture from the web: a dragged or pasted one in the Artbook (ui.artbook_board), and one sent
by the browser extension (core.browser_link). No Qt here: the extension's helper starts without it.

A web address may be the picture, a page whose preview (og:image) stands in for it, or a search result
whose real picture is in its query (?imgurl=). What came back is checked with Pillow, whatever the server
called it; only http(s) and data: addresses are fetched.
"""

from __future__ import annotations

import base64
import html as html_lib
import io
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
TIMEOUT = 20
_IMG_SRC = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_OG_IMAGE = re.compile(
    r"""<meta\b[^>]*?(?:property|name)\s*=\s*["'](?:og:image|twitter:image)["'][^>]*?content\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_DATA_URI = re.compile(r"data:image/([a-z0-9.+-]+);base64,(.+)", re.IGNORECASE | re.DOTALL)
_BROWSER_UA = (  # some image hosts refuse requests that don't look like a browser
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


class WebImageError(ValueError):
    """The picture couldn't be had; the message is suitable to show to the user."""


class Refused(WebImageError):
    """The site said no (401 / 403 / 429): a login or a hot-link block. The browser itself may still get it."""


def is_web_url(text: str) -> bool:
    return text.startswith(("http://", "https://"))


def web_image_sources(urls: list[str], html: str = "") -> list[str]:
    """Where a dragged / pasted web image can be fetched from, best first.

    Search results (Google Images and the like) hand over a link to a page, not the image;
    the dragged picture comes along as HTML. So, in order: the real image a result link
    wraps (?imgurl=), the HTML's <img> address, the image data embedded in the HTML (a
    smaller copy, but always there), then the links themselves.
    """
    sources: list[str] = []

    def add(url: str) -> None:
        url = html_lib.unescape(url.strip())
        if url and url not in sources and (is_web_url(url) or url.startswith("data:image/")):
            sources.append(url)

    for url in urls:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        for key in ("imgurl", "mediaurl", "img_url", "image_url"):
            for value in query.get(key, []):
                add(value)
    embedded = []
    for src in _IMG_SRC.findall(html or ""):
        if is_web_url(src):
            add(src)
        elif src.startswith("data:image/"):
            embedded.append(src)
    for src in embedded:
        add(src)
    for url in urls:
        add(url)
    return sources


def download(url: str, referer: str = "") -> tuple[bytes, str]:
    """(data, the server's content type). referer: the page the picture is on (many sites only serve their
    pictures to their own pages)."""
    headers = {"User-Agent": _BROWSER_UA, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    if is_web_url(referer):
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 (http(s) only: callers check)
            data = response.read(MAX_DOWNLOAD_BYTES + 1)
            kind = response.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 429):
            raise Refused(f"The site refused it ({exc.code}).") from exc
        raise WebImageError(f"The site answered {exc.code}.") from exc
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise WebImageError("That image is too big to download (over 64 MB).")
    return data, kind


def image_ext(data: bytes) -> Optional[str]:
    """".png" etc. if data is an image Pillow can open (whatever the server called it), else None."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            return "." + (im.format or "img").lower().replace("jpeg", "jpg")
    except Exception:  # pylint: disable=broad-except
        return None


def fetch_image(url: str, referer: str = "") -> tuple[bytes, str]:
    """(image data, extension) from a data: URI or a web address. A web page's preview image
    (og:image) stands in when the address is a page rather than an image."""
    match = _DATA_URI.match(url)
    if match:
        try:
            data = base64.b64decode(match.group(2))
        except ValueError as exc:
            raise WebImageError("That picture's data is damaged.") from exc
    elif is_web_url(url):
        data, kind = download(url, referer)
        if image_ext(data) is None and kind in ("text/html", "application/xhtml+xml"):
            page = data[:2_000_000].decode("utf-8", errors="replace")
            preview = _OG_IMAGE.search(page)
            if preview:
                data, _kind = download(urllib.parse.urljoin(url, html_lib.unescape(preview.group(1))), url)
    else:
        raise WebImageError("That isn't a web address.")
    ext = image_ext(data)
    if ext is None:
        raise WebImageError("That link isn't an image.")
    return data, ext
