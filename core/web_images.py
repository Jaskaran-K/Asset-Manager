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


def download(url: str, referer: str = "", limit: int = MAX_DOWNLOAD_BYTES, timeout: float = TIMEOUT) -> tuple[bytes, str]:
    """(data, the server's content type). referer: the page the picture is on (many sites only serve their
    pictures to their own pages). limit: the most it takes (more: WebImageError)."""
    headers = {"User-Agent": _BROWSER_UA, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    if is_web_url(referer):
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (http(s) only: callers check)
            data = response.read(limit + 1)
            kind = response.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 429):
            raise Refused(f"The site refused it ({exc.code}).") from exc
        raise WebImageError(f"The site answered {exc.code}.") from exc
    if len(data) > limit:
        raise WebImageError("That image is too big to download (over 64 MB)." if limit == MAX_DOWNLOAD_BYTES else "That's too big.")
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


_META = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_LINK_TAG = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_ATTRIBUTE = re.compile(r"""([a-zA-Z:_-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_TITLE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
MAX_PAGE_READ = 2_000_000  # a web page's first this many bytes are looked at for its title and preview


def page_details(url: str, page: str) -> dict[str, str]:
    """A web page's title, a line about it, its site's name and its preview picture's address (its <meta> tags:
    Open Graph, Twitter, description; else its <title> and its address's host)."""
    metas: dict[str, str] = {}
    for tag in _META.findall(page):
        attributes = {key.lower(): html_lib.unescape(a if a else b) for key, a, b in _ATTRIBUTE.findall(tag)}
        key = (attributes.get("property") or attributes.get("name") or "").lower()
        if key and "content" in attributes and key not in metas:
            metas[key] = " ".join(attributes["content"].split())
    heading = _TITLE.search(page)
    host = urllib.parse.urlsplit(url).hostname or ""
    title = metas.get("og:title") or metas.get("twitter:title") or (" ".join(html_lib.unescape(heading.group(1)).split()) if heading else "")
    picture = metas.get("og:image") or metas.get("og:image:url") or metas.get("twitter:image") or ""
    icons = []
    for tag in _LINK_TAG.findall(page):
        attributes = {key.lower(): html_lib.unescape(a if a else b) for key, a, b in _ATTRIBUTE.findall(tag)}
        if attributes.get("href"):
            icons.append((attributes.get("rel", ""), attributes["href"], attributes.get("sizes", ""), attributes.get("type", "")))
    return {
        "title": title[:300] or url,
        "about": (metas.get("og:description") or metas.get("description") or metas.get("twitter:description") or "")[:600],
        "site": (metas.get("og:site_name") or (host[4:] if host.startswith("www.") else host))[:200],
        "picture": urllib.parse.urljoin(url, picture) if picture else "",
        "icon": best_icon(url, icons),
    }


# ----------------------------------------------------------------------
# A site's icon (its favicon), for its link cards
# ----------------------------------------------------------------------
MAX_ICON_BYTES = 512 * 1024
ICON_SIDE = 128  # an icon is kept at most this big (a card shows it at 15-60 px)
ICON_TIMEOUT = 8


def best_icon(url: str, icons: list[tuple[str, str, str, str]]) -> str:
    """A page's icon's address from its <link> tags, as (rel, href, sizes, type): the biggest one it lists (an SVG
    counts as big), else the site's /favicon.ico."""
    best, best_score = "", -1.0
    for rel, href, sizes, kind in icons:
        words = rel.lower().split()
        if "icon" not in words and not {"apple-touch-icon", "apple-touch-icon-precomposed"} & set(words):
            continue
        sizes = sizes.lower()
        if "svg" in kind.lower() or "any" in sizes or urllib.parse.urlsplit(href).path.lower().endswith(".svg"):
            score = 256.0
        else:
            found = [int(n) for n in re.findall(r"(\d+)x\d+", sizes)]
            score = float(max(found)) if found else (180.0 if "icon" not in words else 16.0)
        if score > best_score:
            best, best_score = href, score
    if best:
        return urllib.parse.urljoin(url, best)
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/favicon.ico" if parts.scheme in ("http", "https") and parts.netloc else ""


def _is_svg(data: bytes) -> bool:
    head = data[:2048].lstrip().lower()
    return head.startswith((b"<svg", b"<?xml")) and b"<svg" in head


def icon_data(data: bytes) -> Optional[tuple[bytes, str]]:
    """An icon as (PNG data, ".png"), its biggest picture fitted in ICON_SIDE (an .ico holds several), or an SVG as
    it is (".svg": the app draws it); None if it's neither."""
    from PIL import Image

    if not data or len(data) > MAX_ICON_BYTES:
        return None
    if _is_svg(data):
        return data, ".svg"
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()  # (an .ico: Pillow reads its biggest)
            picture = im.convert("RGBA")
    except Exception:  # pylint: disable=broad-except
        return None
    if picture.width < 1 or picture.height < 1:
        return None
    picture.thumbnail((ICON_SIDE, ICON_SIDE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    picture.save(out, "PNG")
    return out.getvalue(), ".png"


def fetch_icon(url: str, referer: str = "") -> Optional[tuple[bytes, str]]:
    """A site's icon from a data: URI or a web address, as icon_data; None if it can't be had (a card does
    without: never an error)."""
    match = _DATA_URI.match(url or "")
    try:
        if match:
            data = base64.b64decode(match.group(2))
        elif is_web_url(url or ""):
            data, _kind = download(url, referer, limit=MAX_ICON_BYTES, timeout=ICON_TIMEOUT)
        else:
            return None
    except Exception:  # pylint: disable=broad-except
        return None  # (any trouble: no icon)
    return icon_data(data)


def read_link(url: str) -> tuple[str, object]:
    """What a web address is: ("image", (data, extension)) for a picture, ("page", details) for a web page (its
    page_details, with "picture" its preview picture's data, or None, and "icon" its site's icon as icon_data, or
    None). Raises WebImageError if it's neither."""
    if not is_web_url(url):
        raise WebImageError("That isn't a web address.")
    data, kind = download(url)
    ext = image_ext(data)
    if ext is not None:
        return "image", (data, ext)
    head = data[:MAX_PAGE_READ]
    if kind not in ("text/html", "application/xhtml+xml") and b"<html" not in head[:4096].lower():
        raise WebImageError("That link isn't a web page or an image.")
    details: dict[str, object] = dict(page_details(url, head.decode("utf-8", errors="replace")))
    picture = None
    if details["picture"]:
        try:
            picture_data, _kind = download(str(details["picture"]), url)
            picture = picture_data if image_ext(picture_data) is not None else None
        except (WebImageError, OSError, ValueError):
            picture = None  # (a card without its picture)
    details["picture"] = picture
    details["icon"] = fetch_icon(str(details["icon"]), url) if details["icon"] else None
    return "page", details
