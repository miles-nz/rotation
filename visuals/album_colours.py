from __future__ import annotations

import colorsys
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

import requests
from PIL import Image
from spotipy import Spotify

from core.cancellation import CancelCheck, check_cancelled
from core.paths import DATA_DIR
import lastfm.most_played as most_played
import lastfm.resolve_cache as resolve_cache

logger = logging.getLogger(__name__)

# Album art URL -> the image's palette, shared by every visualisation that
# colours by album art: {"palette": [["#rrggbb", share of pixels], ...]},
# biggest share first, or None if the image couldn't be fetched or decoded.
# Each visualisation picks the colour it wants from the palette (see
# colours_for). Entries written before palettes were kept are a bare
# "#rrggbb" dominant colour - still fine for "dominant", re-fetched when
# something asks for "vivid". Keyed by URL rather than album, since the
# same cache serves Last.fm art and the Spotify art used where Last.fm has
# none. Kept indefinitely, like the resolve cache - the art behind a URL
# doesn't change. Extracted server-side so the browser never loads
# cross-origin images, which would taint the canvas and block PNG export.
_PATH = DATA_DIR / ".lastfm_cache" / "album_colours.json"

# Small CDN images (a few KB each), so a handful of parallel downloads is
# quick without hammering anyone.
_MAX_WORKERS = 8
_THUMBNAIL_PX = 64
_PALETTE_SIZE = 8

# "vivid" ignores palette colours covering less than this share of the
# image, so a speck of colour doesn't beat a cover's real accent colour...
_VIVID_MIN_SHARE = 0.03
# ...and falls back to the dominant colour when nothing is at least this
# colourful (saturation x brightness) - a black-and-white cover stays grey.
_VIVID_MIN_CHROMA = 0.12

# Last.fm image URLs carry their size as a path segment (/i/u/300x300/...),
# and the same image is served at /i/u/64s/... - so colours come from a
# ~3 KB thumbnail instead of the full 300px image.
_LASTFM_SIZE_SEGMENT = re.compile(r"/i/u/[^/]+/")


def load() -> dict:
    if not _PATH.exists():
        return {}
    try:
        return json.loads(_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save(data: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data))


def _thumbnail_url(url: str) -> str:
    return _LASTFM_SIZE_SEGMENT.sub(f"/i/u/{_THUMBNAIL_PX}s/", url, count=1)


def palette_of(image_bytes: bytes) -> list[list]:
    """An image reduced to its _PALETTE_SIZE main colours, as
    [["#rrggbb", share of pixels], ...], biggest share first."""
    image = Image.open(BytesIO(image_bytes)).convert("RGB")
    image.thumbnail((_THUMBNAIL_PX, _THUMBNAIL_PX))
    quantized = image.quantize(colors=_PALETTE_SIZE)
    palette = quantized.getpalette()
    colours = quantized.getcolors()
    total = sum(count for count, _ in colours)
    result = []
    for count, index in sorted(colours, reverse=True):
        r, g, b = palette[index * 3 : index * 3 + 3]
        result.append([f"#{r:02x}{g:02x}{b:02x}", round(count / total, 4)])
    return result


def _saturation_value(hex_colour: str) -> tuple[float, float]:
    r, g, b = (int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
    _, saturation, value = colorsys.rgb_to_hsv(r, g, b)
    return saturation, value


def dominant_colour(palette: list[list]) -> str:
    """The colour that best represents an image: the palette colour with
    the best mix of coverage and vividness, so a bright cover on a mostly
    black background reads as its bright colour rather than black. An image
    that's genuinely all greys still comes out grey."""

    def score(entry: list) -> float:
        saturation, value = _saturation_value(entry[0])
        return entry[1] * (0.25 + saturation) * (0.25 + value)

    return max(palette, key=score)[0]


def vivid_colour(palette: list[list]) -> str:
    """The most colourful palette colour that covers a meaningful share of
    the image - a black cover's red title rather than the black - or the
    dominant colour if nothing in it is really colourful."""
    candidates = [entry for entry in palette if entry[1] >= _VIVID_MIN_SHARE]
    chroma = lambda entry: _saturation_value(entry[0])[0] * _saturation_value(entry[0])[1]  # noqa: E731
    best = max(candidates, key=chroma, default=None)
    if best is None or chroma(best) < _VIVID_MIN_CHROMA:
        return dominant_colour(palette)
    return best[0]


_PICKERS = {"dominant": dominant_colour, "vivid": vivid_colour}


class _PermanentFailure(Exception):
    """The image is gone or isn't one - not worth retrying next run."""


def _fetch_palette(url: str) -> dict:
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
        return {"palette": palette_of(response.content)}
    except (OSError, ValueError) as exc:  # PIL.UnidentifiedImageError is an OSError
        raise _PermanentFailure(str(exc)) from exc


def _pick(entry, kind: str) -> str | None:
    if entry is None:
        return None
    if isinstance(entry, str):  # an older, dominant-colour-only entry
        return entry
    return _PICKERS[kind](entry["palette"])


def colours_for(urls: set[str], cancel_check: CancelCheck | None = None, kind: str = "dominant") -> dict[str, str | None]:
    """A colour for each image URL - kind "dominant" (the colour that best
    represents the cover) or "vivid" (its most colourful accent) - from
    the cache where possible, downloading and extracting the rest. None
    means no colour (shown in the neutral colour): an image that's missing
    or undecodable is cached as None so it isn't re-downloaded every run,
    but one that failed for a possibly temporary reason (timeout, server
    error) isn't cached, so the next run tries again. Saves whatever was
    extracted even if cancelled partway."""
    cache = load()
    missing = [
        url for url in urls if url not in cache or (kind != "dominant" and isinstance(cache[url], str))
    ]
    total = len(missing)
    if total:
        logger.info("album colours: extracting %d album colour(s)", total)
    pool = ThreadPoolExecutor(max_workers=_MAX_WORKERS)
    futures = {pool.submit(_fetch_palette, url): url for url in missing}
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
    return {url: _pick(cache.get(url), kind) for url in urls}


def art_by_album(sp: Spotify | None, scrobbles: list[dict], cancel_check: CancelCheck | None) -> dict[str, str | None]:
    """album_key -> art URL for every album in scrobbles (dicts with
    "artist", "album" and "image_url", as from
    scrobble_history.scrobbles_between): the stored Last.fm image, else the
    image of the album's Spotify match (searched once, then kept in the
    resolve cache), else None. Spotify is skipped if sp is None."""
    art: dict[str, str | None] = {}
    without_art: dict[str, dict] = {}
    for s in scrobbles:
        if not s["album"]:
            continue
        key = resolve_cache.album_key(s["artist"], s["album"])
        if key in art:
            continue
        art[key] = s["image_url"]
        if not s["image_url"]:
            without_art[key] = {"artist": s["artist"], "name": s["album"]}

    if without_art and sp is not None:
        cache = resolve_cache.load("album")
        try:
            resolved_pairs = most_played._resolve_generic(
                sp,
                list(without_art.values()),
                cache,
                key_fn=lambda item: resolve_cache.album_key(item["artist"], item["name"]),
                search_fn=most_played._search_album,
                cancel_check=cancel_check,
            )
        finally:
            resolve_cache.save("album", cache)
        for item, resolved in resolved_pairs:
            if resolved.get("found"):
                art[resolve_cache.album_key(item["artist"], item["name"])] = resolved.get("image_url")
    return art
