from __future__ import annotations

import logging
from collections import Counter

from spotipy import Spotify

from core.cancellation import CancelCheck
import lastfm.resolve_cache as resolve_cache
import lastfm.scrobble_history as scrobble_history
import visuals.album_colours as album_colours

logger = logging.getLogger(__name__)

# How many of the range's top artists get an album colour - the most the
# river's artist picker can show. Everyone else is never drawn in their
# own colour, so isn't worth the lookups.
COLOURED_ARTISTS = 30


def build(sp: Spotify | None, from_ts: int, to_ts: int, cancel_check: CancelCheck | None = None) -> dict:
    """Every play in [from_ts, to_ts) as its timestamp and artist, for the
    artist river. The browser does the rest - grouping into weeks or months
    in its local time zone, picking the top artists, and stacking the
    river - so changing how many artists are shown doesn't need a new
    request.

    Tops up the scrobble store first (a full backfill the very first time).
    Artists are grouped case-insensitively, by resolve_cache.artist_key,
    and named by their most-scrobbled spelling. Each of the range's top
    COLOURED_ARTISTS artists is coloured from the art of their most-played
    album of all time (see album_colours; Spotify fills in missing art
    unless sp is None). Returns {"times": [unix seconds, ...],
    "artist_ids": [index into artists, ...], "artists": [{"key", "name",
    "colour"}, ...]}, oldest play first - parallel lists rather than one
    object per play, since all-time ranges can be well over 100k plays.
    "colour" is "#rrggbb", or None for artists outside the top or whose
    album has no usable art."""
    scrobble_history.sync(cancel_check)
    scrobbles = scrobble_history.scrobbles_between(from_ts, to_ts)

    index_by_key: dict[str, int] = {}
    spellings: list[Counter] = []
    times: list[int] = []
    artist_ids: list[int] = []
    for s in scrobbles:
        key = resolve_cache.artist_key(s["artist"])
        if key not in index_by_key:
            index_by_key[key] = len(spellings)
            spellings.append(Counter())
        index = index_by_key[key]
        spellings[index][s["artist"]] += 1
        times.append(s["timestamp"])
        artist_ids.append(index)

    colours = _top_artist_colours(sp, artist_ids, list(index_by_key), cancel_check)
    artists = [
        {"key": key, "name": spellings[index].most_common(1)[0][0], "colour": colours.get(index)}
        for key, index in index_by_key.items()
    ]
    logger.info("artist river: %d play(s) across %d artist(s)", len(times), len(artists))
    return {"times": times, "artist_ids": artist_ids, "artists": artists}


def _top_artist_colours(
    sp: Spotify | None, artist_ids: list[int], keys: list[str], cancel_check: CancelCheck | None
) -> dict[int, str | None]:
    """artist index -> the most vivid colour of their most-played album of
    all time (its accent colour, not its background), for the range's top COLOURED_ARTISTS artists by plays. All
    time rather than the range, so an artist keeps the same colour whatever
    range is shown. artist_ids is the range's plays; keys maps an artist
    index to its artist_key."""
    top = {keys[index]: index for index, _ in Counter(artist_ids).most_common(COLOURED_ARTISTS)}

    # Summed across spellings of the same artist and album.
    plays: Counter = Counter()
    example: dict[tuple[str, str], dict] = {}
    for row in scrobble_history.album_plays():
        artist = resolve_cache.artist_key(row["artist"])
        if artist not in top:
            continue
        pair = (artist, row["album"].strip().lower())
        plays[pair] += row["plays"]
        if pair not in example or (row["image_url"] and not example[pair]["image_url"]):
            example[pair] = row
    favourite: dict[int, dict] = {}
    for (artist, album), _ in plays.most_common():
        favourite.setdefault(top[artist], example[(artist, album)])

    art = album_colours.art_by_album(sp, list(favourite.values()), cancel_check)
    colours = album_colours.colours_for({url for url in art.values() if url}, cancel_check, kind="vivid")
    result = {}
    for index, row in favourite.items():
        url = art.get(resolve_cache.album_key(row["artist"], row["album"]))
        result[index] = colours.get(url) if url else None
    return result
