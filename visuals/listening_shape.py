from __future__ import annotations

import logging

from spotipy import Spotify

from core.cancellation import CancelCheck
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
    None. Returns {"plays": [{"t", "n", "a", "al", "c"}, ...],
    "no_art_count"}: timestamp, track, artist, album ("" if none) and the
    album's dominant colour, "#rrggbb" or None for no art."""
    scrobble_history.sync(cancel_check)
    scrobbles = scrobble_history.scrobbles_between(from_ts, to_ts)
    art = album_colours.art_by_album(sp, scrobbles, cancel_check)
    colours = album_colours.colours_for({url for url in art.values() if url}, cancel_check)

    plays = []
    for s in scrobbles:
        url = art.get(resolve_cache.album_key(s["artist"], s["album"])) if s["album"] else None
        plays.append(
            {
                "t": s["timestamp"],
                "n": s["name"],
                "a": s["artist"],
                "al": s["album"],
                "c": colours.get(url) if url else None,
            }
        )
    no_art_count = sum(1 for p in plays if p["c"] is None)
    logger.info("listening shape: %d play(s), %d without album art", len(plays), no_art_count)
    return {"plays": plays, "no_art_count": no_art_count}

