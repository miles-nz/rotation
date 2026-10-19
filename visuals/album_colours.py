from __future__ import annotations

import colorsys
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

import requests
from PIL import Image

from core.cancellation import CancelCheck, check_cancelled
from core.paths import DATA_DIR

logger = logging.getLogger(__name__)

# Album art URL -> dominant colour ("#rrggbb", or None if the image couldn't
# be fetched or decoded), shared by every visualisation that colours by
# album art. Keyed by URL rather than album, since the same cache serves
# Last.fm art and the Spotify art used where Last.fm has none. Kept
# indefinitely, like the resolve cache - the art behind a URL doesn't
# change. Extracted server-side so the browser never loads cross-origin
# images, which would taint the canvas and block PNG export.
_PATH = DATA_DIR / ".lastfm_cache" / "album_colours.json"

# Small CDN images (a few KB each), so a handful of parallel downloads is
# quick without hammering anyone.
_MAX_WORKERS = 8
_THUMBNAIL_PX = 64
_PALETTE_SIZE = 6

# Last.fm image URLs carry their size as a path segment (/i/u/300x300/...),
# and the same image is served at /i/u/64s/... - so colours come from a
# ~3 KB thumbnail instead of the full 300px image.
_LASTFM_SIZE_SEGMENT = re.compile(r"/i/u/[^/]+/")


def load() -> dict[str, str | None]:
    if not _PATH.exists():
        return {}
    try:
        return json.loads(_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save(data: dict[str, str | None]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data))


def _thumbnail_url(url: str) -> str:
    return _LASTFM_SIZE_SEGMENT.sub(f"/i/u/{_THUMBNAIL_PX}s/", url, count=1)


def dominant_colour(image_bytes: bytes) -> str:
    """The colour that best represents an image, as "#rrggbb". Reduces it
    to a small palette, then picks the palette colour with the best mix of
    coverage and vividness, so a bright cover on a mostly black background
    reads as its bright colour rather than black. An image that's
    genuinely all greys still comes out grey."""
    image = Image.open(BytesIO(image_bytes)).convert("RGB")
    image.thumbnail((_THUMBNAIL_PX, _THUMBNAIL_PX))
    quantized = image.quantize(colors=_PALETTE_SIZE)
    palette = quantized.getpalette()

    def score(entry: tuple[int, int]) -> float:
        count, index = entry
        r, g, b = palette[index * 3 : index * 3 + 3]
        _, saturation, value = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        return count * (0.25 + saturation) * (0.25 + value)

    _, index = max(quantized.getcolors(), key=score)
    r, g, b = palette[index * 3 : index * 3 + 3]
    return f"#{r:02x}{g:02x}{b:02x}"


class _PermanentFailure(Exception):
    """The image is gone or isn't one - not worth retrying next run."""


def _fetch_colour(url: str) -> str:
    """Raises _PermanentFailure for a missing or undecodable image, or a
    requests exception for anything that might work next time (timeout,
    connection error, server error)."""
    response = requests.get(_thumbnail_url(url), timeout=10)
    if response.status_code == 404 and _thumbnail_url(url) != url:
        # A few albums have no thumbnail size - fall back to the original.
        response = requests.get(url, timeout=10)
    if response.status_code in (403, 404, 410):
        raise _PermanentFailure(f"HTTP {response.status_code}")
    response.raise_for_status()
    try:
        return dominant_colour(response.content)
    except (OSError, ValueError) as exc:  # PIL.UnidentifiedImageError is an OSError
        raise _PermanentFailure(str(exc)) from exc


def colours_for(urls: set[str], cancel_check: CancelCheck | None = None) -> dict[str, str | None]:
    """Dominant colour for each image URL, from the cache where possible,
    downloading and extracting the rest. None means no colour (shown in the
    neutral colour): an image that's missing or undecodable is cached as
    None so it isn't re-downloaded every run, but one that failed for a
    possibly temporary reason (timeout, server error) isn't cached, so the
    next run tries again. Saves whatever was extracted even if cancelled
    partway."""
    cache = load()
    missing = [url for url in urls if url not in cache]
    total = len(missing)
    if total:
        logger.info("album colours: extracting %d new album colour(s)", total)
    pool = ThreadPoolExecutor(max_workers=_MAX_WORKERS)
    futures = {pool.submit(_fetch_colour, url): url for url in missing}
    try:
        for done, future in enumerate(as_completed(futures), start=1):
            url = futures[future]
            try:
                cache[url] = future.result()
            except _PermanentFailure as exc:
                logger.warning("album colours: no usable image at %s (%s)", url, exc)
                cache[url] = None
            except requests.RequestException as exc:
                logger.warning("album colours: couldn't fetch %s, will retry next time (%s)", url, exc)
            logger.info(
                "album colours progress: %d/%d",
                done,
                total,
                extra={
                    "progress": {
                        "id": "album_colours",
                        "name": "Extracting album art colours",
                        "current": done,
                        "total": total,
                    }
                },
            )
            check_cancelled(cancel_check)
    finally:
        # Not `with ThreadPoolExecutor(...)`: on cancel that would wait for
        # every queued download. Cancelling the not-yet-started ones
        # (shutdown's cancel_futures needs Python 3.9) means only the few
        # in flight finish.
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False)
        if total:
            save(cache)
    return {url: cache.get(url) for url in urls}
