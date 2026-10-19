from __future__ import annotations

import logging

from spotipy import Spotify

from core.cancellation import CancelCheck
import lastfm.most_played as most_played
import lastfm.resolve_cache as resolve_cache
import lastfm.scrobble_history as scrobble_history
import visuals.album_colours as album_colours

logger = logging.getLogger(__name__)

# Longest range Listening Shape draws - past a year, one-ring-per-day and
# one-row-per-day shapes get thinner than their strokes.
MAX_DAYS = 366


def build(sp: Spotify | None, from_ts: int, to_ts: int, cancel_check: CancelCheck | None = None) -> dict:
    """Everything Listening Shape draws for [from_ts, to_ts): one entry per
    scrobble with its track, artist and album-art colour. Days, weeks and
    times of day - and so every shape - are worked out in the browser, in
    its local time zone; the server only deals in UTC timestamps.

    Tops up the scrobble store first (a full backfill the very first
    time). Album art comes from the Last.fm image already stored for each
    album; albums without one fall back to a Spotify album search (cached
    in the resolve cache, so each is only searched once), skipped if sp is
    None. Returns {"plays": [{"t", "n", "a", "c"}, ...], "no_art_count"},
    where "c" is "#rrggbb" or None for no art."""
    scrobble_history.sync(cancel_check)
    scrobbles = scrobble_history.scrobbles_between(from_ts, to_ts)
    art = _art_by_album(sp, scrobbles, cancel_check)
    colours = album_colours.colours_for({url for url in art.values() if url}, cancel_check)

    plays = []
    for s in scrobbles:
        url = art.get(resolve_cache.album_key(s["artist"], s["album"])) if s["album"] else None
        plays.append({"t": s["timestamp"], "n": s["name"], "a": s["artist"], "c": colours.get(url) if url else None})
    no_art_count = sum(1 for p in plays if p["c"] is None)
    logger.info("listening shape: %d play(s), %d without album art", len(plays), no_art_count)
    return {"plays": plays, "no_art_count": no_art_count}


def _art_by_album(sp: Spotify | None, scrobbles: list[dict], cancel_check: CancelCheck | None) -> dict[str, str | None]:
    """album_key -> art URL for every album in scrobbles: the stored Last.fm
    image, else the Spotify match's image, else None."""
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
